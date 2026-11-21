"""Incremental daily rollup store under ~/.local/state/tokenfather/."""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import secrets
from typing import Any

from . import events
from .adapters import ProviderAdapter, default_adapters
from .attribution import LaneIndex, discover_lane_dirs, load_lanes
from .schema import RequestRecord, as_dict, as_list, host_name


def default_state_dir() -> Path:
    configured = os.environ.get("TOKENFATHER_STATE", "").strip()
    if configured:
        return Path(os.path.expanduser(configured))
    return Path.home() / ".local" / "state" / "tokenfather"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@dataclass
class CollectSummary:
    files_scanned: int
    files_updated: int
    new_requests: int
    days_updated: list[str]
    by_provider: dict[str, int]


class RollupStore:
    """Manages cursors.json and daily/YYYY-MM-DD.jsonl under the tokenfather state directory."""

    def __init__(self, state_dir: Path | None = None, *, events_log: Path | None = None):
        self.state_dir = (state_dir or default_state_dir()).expanduser()
        self.daily_dir = self.state_dir / "daily"
        self.cursors_path = self.state_dir / "cursors.json"
        self.events_log = events_log

    def load_cursors(self) -> dict[str, dict[str, Any]]:
        if not self.cursors_path.is_file():
            return {}
        try:
            data = as_dict(json.loads(self.cursors_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}
        if data is None:
            return {}
        raw_files = as_dict(data.get("files"))
        if raw_files is None:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for k, v in raw_files.items():
            entry = as_dict(v)
            if entry is not None:
                result[str(k)] = dict(entry)
        return result

    def save_cursors(self, cursors: dict[str, dict[str, Any]]) -> None:
        payload = {"v": 1, "files": cursors}
        _atomic_write(self.cursors_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _read_new_lines(self, path: Path, cursor: dict[str, Any]) -> tuple[list[str], int, int, int]:
        st = path.stat()
        inode = int(st.st_ino)
        size = int(st.st_size)
        prev_inode = cursor.get("inode")
        prev_offset = int(cursor.get("offset", 0))
        if prev_inode != inode or size < prev_offset:
            # File was replaced or truncated: re-read from start (seen_ids still prevents duplicate output).
            prev_offset = 0
        if size == prev_offset:
            return [], inode, size, prev_offset
        with open(path, "rb") as handle:
            handle.seek(prev_offset)
            chunk = handle.read()
        # Only advance offset through the last complete newline so a partially written line is retried next time.
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return [], inode, size, prev_offset
        complete = chunk[: last_nl + 1]
        new_offset = prev_offset + len(complete)
        lines = complete.decode("utf-8", errors="replace").splitlines()
        return lines, inode, size, new_offset

    def _append_daily(self, records: list[RequestRecord]) -> list[str]:
        if not records:
            return []
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        by_day: dict[str, list[RequestRecord]] = {}
        for rec in records:
            by_day.setdefault(rec.day, []).append(rec)
        updated_days: list[str] = []
        for day, day_records in sorted(by_day.items()):
            day_path = self.daily_dir / f"{day}.jsonl"
            existing_keys: set[tuple[str, str]] = set()
            key_to_idx: dict[tuple[str, str], int] = {}
            rows: list[dict[str, Any]] = []
            if day_path.is_file():
                with open(day_path, encoding="utf-8", errors="replace") as handle:
                    for raw_line in handle:
                        stripped = raw_line.strip()
                        if not stripped:
                            continue
                        try:
                            obj = as_dict(json.loads(stripped))
                        except ValueError:
                            continue
                        if obj is not None:
                            k = (str(obj.get("provider", "")), str(obj.get("request_id", "")))
                            if k not in existing_keys:
                                existing_keys.add(k)
                                key_to_idx[k] = len(rows)
                                rows.append(obj)
            changed = False
            for rec in day_records:
                k = (rec.provider, rec.request_id)
                if k not in existing_keys:
                    existing_keys.add(k)
                    key_to_idx[k] = len(rows)
                    rows.append(rec.to_dict())
                    changed = True
                else:
                    idx = key_to_idx[k]
                    prev = rows[idx]
                    raw_out = prev.get("output_tokens")
                    prev_out = raw_out if isinstance(raw_out, int) and not isinstance(raw_out, bool) else 0
                    prev_tools = as_list(prev.get("tools")) or []
                    if rec.output_tokens > prev_out or len(rec.tools) > len(prev_tools):
                        rows[idx] = rec.to_dict()
                        changed = True
            if not changed:
                continue
            payload = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
            _atomic_write(day_path, payload)
            updated_days.append(day)
        return updated_days

    def collect(
        self,
        *,
        home: Path | None = None,
        adapters: list[ProviderAdapter] | None = None,
        lane_dirs: list[Path] | None = None,
    ) -> CollectSummary:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_dir / "collect.lock"
        with open(lock_path, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                root_home = (home or Path.home()).expanduser()
                active_adapters = adapters if adapters is not None else default_adapters()
                cursors = self.load_cursors()
                host = host_name()

                discovered_lane_dirs = discover_lane_dirs(root_home, lane_dirs)
                lane_index = LaneIndex(load_lanes(discovered_lane_dirs))

                files_scanned = 0
                files_updated = 0
                all_new: list[RequestRecord] = []
                by_provider: dict[str, int] = {}

                for adapter in active_adapters:
                    for path in adapter.discover(root_home):
                        files_scanned += 1
                        key = str(path)
                        cursor = cursors.get(key, {})
                        try:
                            lines, inode, size, new_offset = self._read_new_lines(path, cursor)
                        except OSError:
                            continue
                        if not lines:
                            continue
                        files_updated += 1
                        new_records = adapter.parse_lines(path, lines, cursor, host)
                        cursor["inode"] = inode
                        cursor["size"] = size
                        cursor["offset"] = new_offset
                        cursor["provider"] = adapter.name
                        cursors[key] = cursor
                        if new_records:
                            lane_index.attribute(new_records)
                            all_new.extend(new_records)
                            by_provider[adapter.name] = by_provider.get(adapter.name, 0) + len(new_records)

                days_updated = self._append_daily(all_new)
                self.save_cursors(cursors)

                summary = CollectSummary(
                    files_scanned=files_scanned,
                    files_updated=files_updated,
                    new_requests=len(all_new),
                    days_updated=days_updated,
                    by_provider=by_provider,
                )
                self._emit_event(summary, all_new)
                return summary
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _emit_event(self, summary: CollectSummary, new_records: list[RequestRecord]) -> None:
        if summary.new_requests == 0:
            return
        if self.events_log is None and events.temporary(self.state_dir) and not os.environ.get("TOKENFATHER_EVENTS"):
            return
        total_in = sum(r.input_tokens for r in new_records)
        total_out = sum(r.output_tokens for r in new_records)
        total_cread = sum(r.cache_read_tokens for r in new_records)
        total_cwrite = sum(r.cache_write_tokens for r in new_records)
        ev = events.event(
            "usage.collected",
            {"type": "rollup", "id": summary.days_updated[-1] if summary.days_updated else "none"},
            {
                "message": f"collected {summary.new_requests} requests across {summary.files_updated} files",
                "requests": summary.new_requests,
                "files_updated": summary.files_updated,
                "days": summary.days_updated,
                "by_provider": summary.by_provider,
                "input_tokens": total_in,
                "output_tokens": total_out,
                "cache_read_tokens": total_cread,
                "cache_write_tokens": total_cwrite,
            },
        )
        events.append(ev, log=self.events_log)

    def read_records(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        project: str | None = None,
        lane_index: LaneIndex | None = None,
    ) -> list[RequestRecord]:
        """Read normalized RequestRecords from daily/*.jsonl, filtering by time range and project."""
        if not self.daily_dir.is_dir():
            return []
        from .schema import parse_timestamp

        since_dt = parse_timestamp(since) if since else None
        if since and since_dt is None:
            raise ValueError(f"invalid --since timestamp: {since!r}")
        until_dt = parse_timestamp(until) if until else None
        if until and until_dt is None:
            raise ValueError(f"invalid --until timestamp: {until!r}")
        # If --until is a bare date YYYY-MM-DD, treat it as exclusive end of that date (midnight UTC).
        records: list[RequestRecord] = []
        for path in sorted(self.daily_dir.glob("*.jsonl")):
            day = path.stem
            if since_dt and day < since_dt.strftime("%Y-%m-%d"):
                continue
            if until_dt and day > until_dt.strftime("%Y-%m-%d"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for raw_line in text.splitlines():
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    obj = as_dict(json.loads(stripped))
                except ValueError:
                    continue
                if obj is None:
                    continue
                rec = RequestRecord.from_dict(obj)
                rec_dt = parse_timestamp(rec.time)
                if since_dt and rec_dt and rec_dt < since_dt:
                    continue
                if until_dt and rec_dt and rec_dt >= until_dt:
                    continue
                if lane_index is not None and not rec.lane_id:
                    lane = lane_index.match(rec)
                    if lane is not None:
                        rec.lane_id = lane.lane_id
                        rec.lane_project = lane.project
                if project:
                    needle = project.lower()
                    haystack = f"{rec.project} {rec.lane_project}".lower()
                    if needle not in haystack:
                        continue
                records.append(rec)
        return records
