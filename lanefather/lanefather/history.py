"""The run history and orchestration metrics (fathers#92, fathers#47, fathers#151).

  <state>/history/<YYYY-MM>.jsonl   one JSON object per line, newest last; KEEP_MONTHS files are kept

A line is {"v": 1, "kind", "at", "lane", "phase", "tree", ...}:
- kind "check": one check of one verify run: check, suite, status, seconds, started, finished, run (the run id
  Lanefather gave the run, also in its slot lease's meta as `run`), lease (the lease id when verify's report names
  one), report;
- kind "flake": a check that failed and then passed on the same tree under the same environment: check, failed_at,
  passed_at, how ("rerun-alone" or "later-run"), tests (the failed JUnit test names when the check reported them).

`done` and `gc` leave the history alone; `lanefather stats` reads it alongside lane records, receipts, `events.jsonl`,
promote records, and Slotfather lease history when available.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
from pathlib import Path
import statistics
from typing import Any, TypeGuard, cast
from xml.etree import ElementTree

from . import events, gitops, lifecycle, receipts, review_record, slots, state
from .errors import LaneError
from .journal import now, parse_time

VERSION = 1
KEEP_MONTHS = 12
_PHASE_ORDER = ("check", "rerun", "post-merge", "recheck", "gate", "perf")


def _is_strict_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_dict(value: Any) -> dict[str, Any] | None:
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _as_list(value: Any) -> list[Any] | None:
    return cast(list[Any], value) if isinstance(value, list) else None


def _parse_iso(value: Any) -> datetime.datetime | None:
    text = _as_str(value)
    if text is None:
        return None
    try:
        parsed = parse_time(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _duration_seconds(start: datetime.datetime | None, end: datetime.datetime | None) -> float | None:
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds()
    return delta if delta >= 0 else None


def _percentile_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "p50": None, "p90": None}
    return {
        "count": len(values),
        "p50": round(percentile(values, 0.5), 3),
        "p90": round(percentile(values, 0.9), 3),
    }


def directory(store: state.Store) -> Path:
    path = store.history_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def append(store: state.Store, kind: str, **fields: Any) -> None:
    """Append one line to this month's file; never fails the caller."""
    line = {"v": VERSION, "kind": kind, "at": now(), **{k: v for k, v in fields.items() if v is not None}}
    try:
        folder = directory(store)
        month = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
        with open(folder / f"{month}.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        for old in sorted(folder.glob("*.jsonl"))[:-KEEP_MONTHS]:
            old.unlink(missing_ok=True)
    except OSError:
        return


def read(store: state.Store, days: float | None = None) -> list[dict[str, Any]]:
    """Every line of the window (the last `days`, else everything kept), oldest first."""
    cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)) if days else None
    found: list[dict[str, Any]] = []
    if not store.history_dir.is_dir():
        return []
    try:
        files = sorted(store.history_dir.glob("*.jsonl"))
    except OSError:
        return []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                parsed: Any = json.loads(raw)
            except ValueError:
                continue
            line = _as_dict(parsed)
            if line is None or line.get("v") != VERSION:
                continue
            if not isinstance(line.get("kind"), str):
                continue
            if cutoff is not None:
                moment = _parse_iso(line.get("at"))
                if moment is None or moment < cutoff:
                    continue
            found.append(line)
    return found


def has_flake(store: state.Store, check: str, tree: str, days: float = 60) -> bool:
    """Whether the history already marks the check a flake on the tree: one red-to-green is one flake."""
    return any(line["kind"] == "flake" and line.get("check") == check and line.get("tree") == tree
               for line in read(store, days))


def junit_failures(path: Any) -> list[str]:
    """The failed or errored test cases a JUnit XML report names, as classname.name; [] without a readable one."""
    if not path:
        return []
    try:
        root = ElementTree.parse(os.fspath(str(path))).getroot()
    except (OSError, ElementTree.ParseError):
        return []
    names: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            names.append(".".join(part for part in (case.get("classname"), case.get("name")) if part))
    return names


def _positive_int(value: Any) -> int | None:
    return value if _is_strict_int(value) and value > 0 else None


def record_run(store: state.Store, report: dict[str, Any] | None, *, lane: str | None, phase: str, tree: str | None,
               run: str, started: str, finished: str, seconds: float, checks: list[str] | None = None,
               jobs: int | None = None) -> None:
    """One "check" line per row of a verify report; a run without a report gets one line for the run."""
    raw_checks: Any = report.get("checks") if isinstance(report, dict) else None
    given: list[Any] = cast(list[Any], raw_checks) if isinstance(raw_checks, list) else []
    rows: list[dict[str, Any]] = [
        cast(dict[str, Any], r)
        for r in given
        if isinstance(r, dict) and isinstance(cast(dict[str, Any], r).get("id"), str) and cast(dict[str, Any], r)["id"]
    ]
    lease = (report or {}).get("lease")
    report_jobs = _positive_int(jobs) or _positive_int((report or {}).get("jobs"))
    if not rows:
        append(store, "check", lane=lane, phase=phase, tree=tree, check=",".join(checks or []) or None,
               status="no report", seconds=seconds, started=started, finished=finished, run=run, lease=lease,
               jobs=report_jobs)
        return
    for row in rows:
        append(store, "check", lane=lane, phase=phase, tree=tree, check=row["id"], status=row.get("status"),
               seconds=row.get("seconds"), started=row.get("started") or started,
               finished=row.get("finished") or finished, run=run, lease=row.get("lease") or lease,
               junit=row.get("junit"), jobs=_positive_int(row.get("jobs")) or report_jobs)


def flake(store: state.Store, check: str, *, lane: str | None, tree: str, failed_at: str | None, how: str,
          junit: Any = None, phase: str | None = None) -> None:
    append(store, "flake", lane=lane, phase=phase, tree=tree, check=check, failed_at=failed_at, passed_at=now(),
           how=how, tests=junit_failures(junit) or None)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    return statistics.quantiles(ordered, n=100, method="inclusive")[round(fraction * 100) - 1]


def _load_records(store: state.Store, lane: str | None) -> list[state.Record]:
    """Read lane records in `store` without failing on missing directories or damaged/unrecognized JSON files."""
    if not store.directory.is_dir():
        return []
    found: list[state.Record] = []
    try:
        candidates = sorted(store.directory.glob("*.json"))
    except OSError:
        return []
    for path in candidates:
        if path.name.count(".") != 1:
            continue
        raw = state.read_json(path)
        if raw is None:
            continue
        rec_id = _as_str(raw.get("id"))
        if rec_id is None:
            continue
        if lane is not None and rec_id != lane:
            continue
        try:
            migrated = state.migrate(dict(raw))
        except LaneError:
            continue
        found.append(migrated)
    return found


def _resolved_dir(path_str: str) -> Path | None:
    try:
        return Path(path_str).expanduser().resolve()
    except OSError:
        return None


def _read_events(store: state.Store, cutoff: datetime.datetime | None) -> list[dict[str, Any]]:
    """Read Lanefather events for `store` from `events.jsonl` (and rotated `.1`), skipping malformed lines."""
    log_path = events.path()
    files = (log_path.with_name(log_path.name + ".1"), log_path)
    target_dir = _resolved_dir(str(store.directory))
    found: list[dict[str, Any]] = []
    for candidate in files:
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw_line in text.splitlines():
            if not raw_line.strip():
                continue
            try:
                parsed: Any = json.loads(raw_line)
            except ValueError:
                continue
            entry = _as_dict(parsed)
            if entry is None:
                continue
            source = entry.get("source")
            if source is not None and source != events.SOURCE:
                continue
            if _as_str(entry.get("kind")) is None:
                continue
            subject = _as_dict(entry.get("subject"))
            if subject is not None:
                raw_state_dir = _as_str(subject.get("state_dir"))
                if raw_state_dir is not None and target_dir is not None:
                    event_dir = _resolved_dir(raw_state_dir)
                    if event_dir is not None and event_dir != target_dir:
                        continue
            moment = _parse_iso(entry.get("time"))
            if cutoff is not None and (moment is None or moment < cutoff):
                continue
            found.append(entry)
    return found


def _load_promote_runs(store: state.Store) -> list[dict[str, Any]]:
    """Read completed promote records from `<state>/promote/promote-*.json`, ignoring malformed files."""
    if not store.promote_dir.is_dir():
        return []
    found: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for candidate in sorted(store.promote_dir.glob("promote-*.json")):
            raw = state.read_json(candidate)
            if raw is None or raw.get("status") != "completed":
                continue
            sha = _as_str(raw.get("sha"))
            finished = _parse_iso(raw.get("finished"))
            if sha is not None and finished is not None:
                found.append({"sha": sha, "finished": finished})
    return found


def _lane_timestamps(record: state.Record, lane_events: list[dict[str, Any]]) -> list[datetime.datetime]:
    stamps: list[datetime.datetime] = []
    created = _parse_iso(record.get("created"))
    if created is not None:
        stamps.append(created)
    for key in ("landed", "parked", "abandoned", "dropped", "promoted"):
        sub = _as_dict(record.get(key))
        if sub is not None:
            ts = _parse_iso(sub.get("at"))
            if ts is not None:
                stamps.append(ts)
        else:
            ts = _parse_iso(record.get(key))
            if ts is not None:
                stamps.append(ts)
    for ev in lane_events:
        ts = _parse_iso(ev.get("time"))
        if ts is not None:
            stamps.append(ts)
    return stamps


def _first_green_time(
    store: state.Store,
    record: state.Record,
    lane_events: list[dict[str, Any]],
    created: datetime.datetime | None,
) -> datetime.datetime | None:
    lane_id = _as_str(record.get("id"))
    if lane_id is None:
        return None
    event_candidates: list[datetime.datetime] = []
    for ev in lane_events:
        if ev.get("kind") != "lane.checked":
            continue
        details = _as_dict(ev.get("details"))
        if details is not None and details.get("status") == "green":
            ts = _parse_iso(ev.get("time"))
            if ts is not None and (created is None or ts >= created):
                event_candidates.append(ts)
    if event_candidates:
        return min(event_candidates)
    rec = receipts.read(store, lane_id, "receipt", record=record)
    if rec is not None and rec.get("status") == "green":
        ts = _parse_iso(rec.get("finished")) or _parse_iso(rec.get("started"))
        if ts is not None and (created is None or ts >= created):
            return ts
    return None


def _land_time(
    record: state.Record,
    lane_events: list[dict[str, Any]],
    created: datetime.datetime | None,
) -> datetime.datetime | None:
    landed_info = _as_dict(record.get("landed"))
    if landed_info is not None:
        ts = _parse_iso(landed_info.get("at"))
        if ts is not None and (created is None or ts >= created):
            return ts
    candidates: list[datetime.datetime] = []
    for ev in lane_events:
        if ev.get("kind") == "lane.landed":
            ts = _parse_iso(ev.get("time"))
            if ts is not None and (created is None or ts >= created):
                candidates.append(ts)
    return min(candidates) if candidates else None


def _is_ancestor(
    common_dir: str,
    merge_sha: str,
    target_sha: str,
    cache: dict[tuple[str, str, str], bool],
) -> bool:
    key = (common_dir, merge_sha, target_sha)
    if key in cache:
        return cache[key]
    checkout = gitops.main_checkout(common_dir)
    result = bool(checkout and Path(checkout).is_dir() and gitops.git_ok(
        checkout, "merge-base", "--is-ancestor", merge_sha, target_sha
    ))
    cache[key] = result
    return result


def _sorted_promotes(
    promote_events: list[dict[str, Any]],
    promote_runs: list[dict[str, Any]],
) -> list[tuple[datetime.datetime, str]]:
    """Chronologically sorted `(timestamp, sha)` promote candidates, deduplicated by SHA (earliest wins)."""
    earliest_by_sha: dict[str, datetime.datetime] = {}
    for ev in promote_events:
        details = _as_dict(ev.get("details")) or {}
        if _as_list(details.get("failed")):
            continue
        ev_time = _parse_iso(ev.get("time"))
        subject = _as_dict(ev.get("subject"))
        ev_sha = _as_str(subject.get("id")) if subject is not None else None
        if ev_time is None or ev_sha is None:
            continue
        prev = earliest_by_sha.get(ev_sha)
        if prev is None or ev_time < prev:
            earliest_by_sha[ev_sha] = ev_time
    for run in promote_runs:
        run_sha = cast(str, run["sha"])
        run_time = cast(datetime.datetime, run["finished"])
        prev = earliest_by_sha.get(run_sha)
        if prev is None or run_time < prev:
            earliest_by_sha[run_sha] = run_time
    return sorted(((ts, sha) for sha, ts in earliest_by_sha.items()), key=lambda item: (item[0], item[1]))


def _promote_info(
    record: state.Record,
    landed_at: datetime.datetime | None,
    created: datetime.datetime | None,
    sorted_promotes: list[tuple[datetime.datetime, str]],
    ancestor_cache: dict[tuple[str, str, str], bool],
) -> tuple[datetime.datetime | None, str | None]:
    """Return `(promoted_at, batch_sha)` for `record`, stopping at the earliest chronological match."""
    floor = landed_at or created
    promoted_info = _as_dict(record.get("promoted"))
    if promoted_info is not None:
        ts = _parse_iso(promoted_info.get("at"))
        if ts is not None and (floor is None or ts >= floor):
            return ts, _as_str(promoted_info.get("sha"))
    else:
        ts = _parse_iso(record.get("promoted"))
        if ts is not None and (floor is None or ts >= floor):
            return ts, None

    landed_info = _as_dict(record.get("landed"))
    merge_sha = _as_str(landed_info.get("merge")) if landed_info is not None else None
    if merge_sha is None:
        return None, None
    common = _as_str(record.get("common_dir"))

    for p_time, p_sha in sorted_promotes:
        if floor is not None and p_time < floor:
            continue
        if merge_sha == p_sha or (
            common is not None and _is_ancestor(common, merge_sha, p_sha, ancestor_cache)
        ):
            return p_time, p_sha
    return None, None


def _phase_stats(
    store: state.Store,
    lines: list[dict[str, Any]],
    window_records: list[state.Record],
    cutoff: datetime.datetime | None,
    slotfather_available: bool,
    by_lease: dict[str, dict[str, float | None]],
    by_run: dict[str, dict[str, float | None]],
) -> list[dict[str, Any]]:
    """Aggregate run time and Slotfather queue wait per check phase, joined by `lease_id` (or `run`)."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    seen_leases: dict[str, dict[str, Any]] = {}
    seen_runs: dict[str, dict[str, Any]] = {}
    anon_seq = 0
    for line in lines:
        if line.get("kind") != "check":
            continue
        phase = _as_str(line.get("phase")) or "check"
        run_id = _as_str(line.get("run"))
        lease_id = _as_str(line.get("lease"))
        if lease_id is not None:
            run_key = f"lease:{lease_id}"
        elif run_id is not None:
            run_key = f"run:{run_id}"
        else:
            anon_seq += 1
            run_key = f"anon:{anon_seq}"
        bucket = grouped.setdefault(
            (phase, run_key),
            {"phase": phase, "run": run_id, "lease": lease_id, "starts": [], "ends": [], "seconds": []},
        )
        if lease_id is not None:
            if bucket["lease"] is None:
                bucket["lease"] = lease_id
            seen_leases.setdefault(lease_id, bucket)
        if run_id is not None:
            if bucket["run"] is None:
                bucket["run"] = run_id
            seen_runs.setdefault(run_id, bucket)
        start_dt = _parse_iso(line.get("started"))
        end_dt = _parse_iso(line.get("finished"))
        if start_dt is not None:
            bucket["starts"].append(start_dt)
        if end_dt is not None:
            bucket["ends"].append(end_dt)
        raw_secs = line.get("seconds")
        if _is_number(raw_secs) and raw_secs >= 0:
            bucket["seconds"].append(float(raw_secs))

    # Also include in-window receipt lease records not already present in history/*.jsonl (dedup across all phases).
    receipt_kinds: tuple[tuple[receipts.ReceiptKind, str], ...] = (
        ("receipt", "check"),
        ("post-merge", "post-merge"),
        ("recheck", "post-merge"),
    )
    for record in window_records:
        lane_id = _as_str(record.get("id"))
        if lane_id is None:
            continue
        for kind, default_phase in receipt_kinds:
            rec = receipts.read(store, lane_id, kind, record=record)
            if rec is None:
                continue
            raw_leases = _as_list(rec.get("leases"))
            if not raw_leases:
                continue
            rec_start = _parse_iso(rec.get("started"))
            rec_end = _parse_iso(rec.get("finished"))
            if cutoff is not None:
                rec_ts = rec_end or rec_start
                if rec_ts is None or rec_ts < cutoff:
                    continue
            rec_secs = rec.get("seconds")
            for raw_entry in raw_leases:
                entry = _as_dict(raw_entry)
                if entry is None:
                    continue
                phase = "rerun" if _as_str(entry.get("rerun")) is not None else default_phase
                lease_id = _as_str(entry.get("lease")) or _as_str(entry.get("lease_id"))
                run_id = _as_str(entry.get("run"))
                if lease_id is None and run_id is None:
                    continue
                if lease_id is not None and lease_id in seen_leases:
                    continue
                if run_id is not None and run_id in seen_runs:
                    existing = seen_runs[run_id]
                    if lease_id is not None and existing["lease"] is None:
                        existing["lease"] = lease_id
                        seen_leases.setdefault(lease_id, existing)
                    continue
                run_key = f"lease:{lease_id}" if lease_id is not None else f"run:{run_id}"
                starts = [rec_start] if rec_start is not None else []
                ends = [rec_end] if rec_end is not None else []
                secs = [float(rec_secs)] if _is_number(rec_secs) and rec_secs >= 0 else []
                bucket = {
                    "phase": phase,
                    "run": run_id,
                    "lease": lease_id,
                    "starts": starts,
                    "ends": ends,
                    "seconds": secs,
                }
                grouped[(phase, run_key)] = bucket
                if lease_id is not None:
                    seen_leases[lease_id] = bucket
                if run_id is not None:
                    seen_runs[run_id] = bucket

    by_phase: dict[str, dict[str, Any]] = {}
    for entry in grouped.values():
        phase = cast(str, entry["phase"])
        row = by_phase.setdefault(phase, {"runs": 0, "run_secs": [], "wait_secs": []})
        row["runs"] += 1

        matched: dict[str, float | None] | None = None
        if slotfather_available:
            lease_id = cast(str | None, entry["lease"])
            run_id = cast(str | None, entry["run"])
            if lease_id is not None and lease_id in by_lease:
                matched = by_lease[lease_id]
            elif run_id is not None and run_id in by_run:
                matched = by_run[run_id]

        starts = cast(list[datetime.datetime], entry["starts"])
        ends = cast(list[datetime.datetime], entry["ends"])
        secs = cast(list[float], entry["seconds"])
        wall = _duration_seconds(min(starts), max(ends)) if starts and ends else None
        if wall is not None and wall > 0:
            row["run_secs"].append(wall)
        elif secs:
            row["run_secs"].append(sum(secs))
        elif matched is not None and matched["ran"] is not None:
            row["run_secs"].append(matched["ran"])

        if matched is not None and matched["waited"] is not None:
            row["wait_secs"].append(matched["waited"])

    def _phase_sort_key(name: str) -> tuple[int, str]:
        try:
            return (_PHASE_ORDER.index(name), name)
        except ValueError:
            return (len(_PHASE_ORDER), name)

    phases: list[dict[str, Any]] = []
    for phase in sorted(by_phase, key=_phase_sort_key):
        row = by_phase[phase]
        run_secs = cast(list[float], row["run_secs"])
        wait_secs = cast(list[float], row["wait_secs"])
        phases.append({
            "phase": phase,
            "runs": row["runs"],
            "matched_leases": len(wait_secs),
            "run_p50": round(percentile(run_secs, 0.5), 3) if run_secs else None,
            "run_p90": round(percentile(run_secs, 0.9), 3) if run_secs else None,
            "wait_p50": round(percentile(wait_secs, 0.5), 3) if wait_secs else None,
            "wait_p90": round(percentile(wait_secs, 0.9), 3) if wait_secs else None,
            "wait_status": "known" if wait_secs else "unknown",
        })
    return phases


def _lane_flow_stats(
    store: state.Store,
    window_records: list[state.Record],
    flow_events: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    cutoff: datetime.datetime | None,
    slotfather_available: bool,
    by_lease: dict[str, dict[str, float | None]],
    by_run: dict[str, dict[str, float | None]],
) -> tuple[dict[str, Any], dict[str, str]]:
    events_by_lane: dict[str, list[dict[str, Any]]] = {}
    promote_events: list[dict[str, Any]] = []
    for ev in flow_events:
        kind = ev.get("kind")
        if kind == "promote.finished":
            promote_events.append(ev)
        subject = _as_dict(ev.get("subject"))
        if subject is not None and subject.get("type") == "lane":
            sub_id = _as_str(subject.get("id"))
            if sub_id is not None:
                events_by_lane.setdefault(sub_id, []).append(ev)

    sorted_promotes = _sorted_promotes(promote_events, _load_promote_runs(store))
    ancestor_cache: dict[tuple[str, str, str], bool] = {}

    to_first_green: list[float] = []
    to_land: list[float] = []
    to_promote: list[float] = []
    lane_batches: dict[str, str] = {}

    for record in window_records:
        lane_id = _as_str(record.get("id"))
        if lane_id is None:
            continue
        created = _parse_iso(record.get("created"))
        if created is None:
            continue
        lane_evs = events_by_lane.get(lane_id, [])
        green_at = _first_green_time(store, record, lane_evs, created)
        green_dur = _duration_seconds(created, green_at)
        if green_dur is not None:
            to_first_green.append(green_dur)

        landed_at = _land_time(record, lane_evs, created)
        land_dur = _duration_seconds(created, landed_at)
        if land_dur is not None:
            to_land.append(land_dur)

        promoted_at, batch_sha = _promote_info(record, landed_at, created, sorted_promotes, ancestor_cache)
        if batch_sha is not None:
            lane_batches[lane_id] = batch_sha
        promote_dur = _duration_seconds(created, promoted_at)
        if promote_dur is not None:
            to_promote.append(promote_dur)

    phases = _phase_stats(store, lines, window_records, cutoff, slotfather_available, by_lease, by_run)
    return (
        {
            "lanes": len(window_records),
            "to_first_green": _percentile_summary(to_first_green),
            "to_land": _percentile_summary(to_land),
            "to_promote": _percentile_summary(to_promote),
            "phases": phases,
            "slotfather_available": slotfather_available,
        },
        lane_batches,
    )


def _outcome_stats(window_records: list[state.Record]) -> dict[str, int]:
    counts: dict[str, int] = {
        "total": len(window_records),
        "landed": 0,
        "parked": 0,
        "abandoned": 0,
        "dropped": 0,
        "active": 0,
        "planned": 0,
        "other": 0,
    }
    for record in window_records:
        if lifecycle.planned(record):
            counts["planned"] += 1
        elif lifecycle.active(record):
            counts["active"] += 1
        elif lifecycle.parked(record):
            counts["parked"] += 1
        elif lifecycle.abandoned(record):
            counts["abandoned"] += 1
        elif lifecycle.dropped(record):
            counts["dropped"] += 1
        elif lifecycle.landed(record):
            counts["landed"] += 1
        else:
            counts["other"] += 1
    return counts


def _fix_parents_strict(record: state.Record) -> list[str]:
    parents = state.fix_parents(record)
    if parents:
        return parents
    raw_fix = record.get("fix_of")
    if isinstance(raw_fix, str) and raw_fix:
        return [raw_fix]
    raw_list = _as_list(raw_fix)
    if raw_list is not None:
        return [item for item in raw_list if isinstance(item, str) and item]
    return []


def _refusal_strings(raw_refusals: Any) -> list[str]:
    items = _as_list(raw_refusals)
    if items is None:
        return []
    return [item for item in items if isinstance(item, str) and item]


def _is_review_gate_override(command_name: str, refusals: list[str]) -> bool:
    if command_name in ("promote", "findings"):
        return True
    if command_name == "land":
        return any("review" in text.lower() or "finding" in text.lower() for text in refusals)
    return False


def _collect_overrides(
    window_records: list[state.Record],
    all_events: list[dict[str, Any]],
    cutoff: datetime.datetime | None,
    lane: str | None,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    event_keys: list[tuple[str | None, str, datetime.datetime | None]] = []

    for ev in all_events:
        if ev.get("kind") != "override":
            continue
        details = _as_dict(ev.get("details")) or {}
        subject = _as_dict(ev.get("subject")) or {}
        sub_type = _as_str(subject.get("type"))
        sub_id = _as_str(subject.get("id"))
        command_name = _as_str(details.get("command")) or "unknown"
        reason = _as_str(details.get("reason")) or "(no reason)"
        refusals = _refusal_strings(details.get("refusals"))
        at = _parse_iso(ev.get("time"))
        is_promote = command_name in ("promote", "findings") or sub_type == "commit"
        lane_id = sub_id if sub_type == "lane" else None

        if lane is not None:
            if lane_id is not None:
                if lane_id != lane:
                    continue
            elif not any(f"lane {lane} " in text or f"lane {lane}:" in text for text in refusals):
                continue

        collected.append({
            "command": command_name,
            "scope": "promote" if is_promote else "lane",
            "lane": lane_id,
            "commit": sub_id if sub_type == "commit" else None,
            "reason": reason,
            "refusals": refusals,
            "at": at,
        })
        event_keys.append((lane_id, reason, at))

    for record in window_records:
        lane_id = _as_str(record.get("id"))
        raw_overrides = _as_list(record.get("overrides"))
        if lane_id is None or not raw_overrides:
            continue
        for raw_entry in raw_overrides:
            entry = _as_dict(raw_entry)
            if entry is None:
                continue
            at = _parse_iso(entry.get("at"))
            if cutoff is not None and at is not None and at < cutoff:
                continue
            reason = _as_str(entry.get("reason")) or "(no reason)"
            refusals = _refusal_strings(entry.get("refusals"))
            duplicate = False
            for ev_lane, ev_reason, ev_at in event_keys:
                if (
                    ev_lane == lane_id
                    and ev_reason == reason
                    and (at is None or ev_at is None or abs((at - ev_at).total_seconds()) <= 5.0)
                ):
                    duplicate = True
                    break
            if duplicate:
                continue
            command_name = _as_str(entry.get("command"))
            if command_name is None:
                if any("smoke" in text.lower() for text in refusals):
                    command_name = "smoke"
                elif any("cap" in text.lower() or "disk" in text.lower() for text in refusals):
                    command_name = "start"
                else:
                    command_name = "land"
            collected.append({
                "command": command_name,
                "scope": "lane",
                "lane": lane_id,
                "reason": reason,
                "refusals": refusals,
                "at": at,
            })
    return collected


def _rank_reasons(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        reason = cast(str, item["reason"])
        counts[reason] = counts.get(reason, 0) + 1
    return [{"reason": reason, "count": count} for reason, count in sorted(counts.items(), key=lambda r: (-r[1], r[0]))]


def _override_stats(
    window_records: list[state.Record],
    override_entries: list[dict[str, Any]],
    lane_batches: dict[str, str],
) -> dict[str, Any]:
    by_command: dict[str, int] = {}
    lane_count = 0
    promote_entries: list[dict[str, Any]] = []
    batch_buckets: dict[str, dict[str, Any]] = {}

    def _batch_row(key: str) -> dict[str, Any]:
        return batch_buckets.setdefault(
            key,
            {"entries": [], "lane_overrides": 0, "promote_overrides": 0, "fix_lanes": []},
        )

    for item in override_entries:
        cmd = cast(str, item["command"])
        by_command[cmd] = by_command.get(cmd, 0) + 1
        if item["scope"] == "promote":
            promote_entries.append(item)
            batch_key = _as_str(item.get("commit")) or "unpromoted"
            bucket = _batch_row(batch_key)
            bucket["entries"].append(item)
            bucket["promote_overrides"] += 1
        else:
            lane_count += 1
            lane_id = _as_str(item.get("lane"))
            batch_key = lane_batches.get(lane_id, "unpromoted") if lane_id is not None else "unpromoted"
            bucket = _batch_row(batch_key)
            bucket["entries"].append(item)
            bucket["lane_overrides"] += 1

    fix_rows: list[dict[str, Any]] = []
    single_fix = 0
    batch_fix = 0
    for record in window_records:
        lane_id = _as_str(record.get("id"))
        if lane_id is None:
            continue
        parents = _fix_parents_strict(record)
        if not parents:
            continue
        is_batch = state.batch_fix(record) or len(parents) > 1
        if is_batch:
            batch_fix += 1
        else:
            single_fix += 1
        fix_rows.append({"id": lane_id, "fix_of": parents, "batch": is_batch})
        batch_key = lane_batches.get(lane_id, "unpromoted")
        _batch_row(batch_key)["fix_lanes"].append(lane_id)
    fix_rows.sort(key=lambda r: str(r["id"]))

    batches: list[dict[str, Any]] = []
    for batch_key in sorted(batch_buckets, key=lambda k: (k == "unpromoted", k)):
        bucket = batch_buckets[batch_key]
        b_overrides = cast(list[dict[str, Any]], bucket["entries"])
        batches.append({
            "batch": batch_key,
            "overrides": len(b_overrides),
            "lane_overrides": bucket["lane_overrides"],
            "promote_overrides": bucket["promote_overrides"],
            "reasons": _rank_reasons(b_overrides),
            "fix_lanes": sorted(cast(list[str], bucket["fix_lanes"])),
        })

    return {
        "total": len(override_entries),
        "lane": lane_count,
        "promote": len(promote_entries),
        "by_command": dict(sorted(by_command.items())),
        "reasons": _rank_reasons(override_entries),
        "promote_reasons": _rank_reasons(promote_entries),
        "batches": batches,
        "fix_lanes": {
            "total": len(fix_rows),
            "single": single_fix,
            "batch": batch_fix,
            "lanes": fix_rows,
        },
    }


def _review_stats(
    store: state.Store,
    cutoff: datetime.datetime | None,
    lane: str | None,
    override_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    reviewed_lanes = 0
    totals: dict[str, int] = dict.fromkeys(review_record.mark_counts(()), 0)
    for rev in review_record.ReviewRecord.all(store):
        raw = _as_dict(rev.raw)
        if raw is None or not rev.has_finished_receipt or rev.is_recovered:
            continue
        if raw.get("overall") is True or rev.label.startswith("overall-"):
            continue
        rev_lane = _as_str(raw.get("lane")) or rev.label
        if lane is not None and rev_lane != lane:
            continue
        if cutoff is not None:
            finished = _parse_iso(raw.get("finished"))
            if finished is None or finished < cutoff:
                continue
        reviewed_lanes += 1
        for key, count in review_record.mark_counts(rev.findings).items():
            totals[key] += count

    gate_overridden = 0
    gate_held = 0
    for item in override_entries:
        cmd = cast(str, item["command"])
        refusals = cast(list[str], item["refusals"])
        if _is_review_gate_override(cmd, refusals):
            gate_overridden += 1
            gate_held += len(refusals) if refusals else 1

    total_findings = totals["findings"]
    return {
        "reviewed_lanes": reviewed_lanes,
        "findings": total_findings,
        "p1": totals["p1"],
        "p2": totals["p2"],
        "p3": totals["p3"],
        "fixed": totals["fixed"],
        "rejected": totals["rejected"],
        "unmarked": totals["unmarked"],
        "findings_per_lane": round(total_findings / reviewed_lanes, 3) if reviewed_lanes else None,
        "fixed_share": round(totals["fixed"] / total_findings, 3) if total_findings else None,
        "rejected_share": round(totals["rejected"] / total_findings, 3) if total_findings else None,
        "unmarked_share": round(totals["unmarked"] / total_findings, 3) if total_findings else None,
        "gate": {
            "held": gate_held,
            "overridden": gate_overridden,
        },
    }


def stats(store: state.Store, days: float | None = 7, lane: str | None = None) -> dict[str, Any]:
    """Check health, lane flow, outcomes, overrides and review metrics over the window."""
    cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)) if days else None
    lines = [line for line in read(store, days) if lane is None or line.get("lane") == lane]
    table: dict[str, dict[str, Any]] = {}
    for line in lines:
        check = line.get("check")
        if not isinstance(check, str) or not check:
            continue
        if line["kind"] == "check" and (line.get("status") not in ("passed", "failed") or check.startswith(
                "environment:")):
            continue  # not run, blocked, a run without a report, or the environment's pseudo-check
        row = table.setdefault(check, {"check": check, "runs": 0, "failed": 0, "flakes": 0, "seconds": []})
        if line["kind"] == "check":
            row["runs"] += 1
            row["failed"] += line.get("status") == "failed"
            raw_secs = line.get("seconds")
            if _is_number(raw_secs):
                row["seconds"].append(float(raw_secs))
        elif line["kind"] == "flake":
            row["flakes"] += 1
    checks: list[dict[str, Any]] = []
    for row in table.values():
        seconds: list[float] = row.pop("seconds")
        checks.append(dict(row, p50=round(percentile(seconds, 0.5), 3) if seconds else None,
                           p90=round(percentile(seconds, 0.9), 3) if seconds else None,
                           total_seconds=round(sum(seconds), 3)))
    checks.sort(key=lambda r: (-(r["total_seconds"] or 0), r["check"]))

    all_events = _read_events(store, cutoff=None)
    window_events = (
        all_events
        if cutoff is None
        else [
            ev
            for ev in all_events
            if (ts := _parse_iso(ev.get("time"))) is not None and ts >= cutoff
        ]
    )
    all_records = _load_records(store, lane)
    window_events_by_lane: dict[str, list[dict[str, Any]]] = {}
    for ev in window_events:
        subject = _as_dict(ev.get("subject"))
        if subject is not None and subject.get("type") == "lane":
            sub_id = _as_str(subject.get("id"))
            if sub_id is not None:
                window_events_by_lane.setdefault(sub_id, []).append(ev)

    window_records: list[state.Record] = []
    for record in all_records:
        if cutoff is None or lifecycle.occupying(record):
            window_records.append(record)
            continue
        rec_id = _as_str(record.get("id")) or ""
        stamps = _lane_timestamps(record, window_events_by_lane.get(rec_id, []))
        if any(ts >= cutoff for ts in stamps):
            window_records.append(record)

    slotfather_available, by_lease, by_run = slots.lease_history()
    lane_flow, lane_batches = _lane_flow_stats(
        store, window_records, all_events, lines, cutoff, slotfather_available, by_lease, by_run
    )
    outcomes = _outcome_stats(window_records)
    override_entries = _collect_overrides(window_records, window_events, cutoff, lane)
    overrides = _override_stats(window_records, override_entries, lane_batches)
    review = _review_stats(store, cutoff, lane, override_entries)

    return {
        "v": VERSION,
        "days": days,
        "lane": lane,
        "lines": len(lines),
        "checks": checks,
        "reviewers": reviewer_totals(store, days, lane),
        "lane_flow": lane_flow,
        "outcomes": outcomes,
        "overrides": overrides,
        "review": review,
    }


def reviewer_totals(store: state.Store, days: float | None = 7, lane: str | None = None) -> list[dict[str, Any]]:
    """Per reviewer (fathers#151), over the finished review receipts of the window (and lane): reviews, failed
    panel runs, findings by severity and their marks, via ReviewRecord.reviewer_views and review_record.mark_counts."""
    cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)) if days else None
    table: dict[str, dict[str, Any]] = {}
    for rev in review_record.ReviewRecord.all(store):
        raw = _as_dict(rev.raw) or {}
        rev_lane = _as_str(raw.get("lane")) or rev.label
        if not rev.has_finished_receipt or rev.is_recovered or (lane is not None and rev_lane != lane):
            continue
        if cutoff is not None:
            finished = _parse_iso(raw.get("finished"))
            if finished is None or finished < cutoff:
                continue
        for view in rev.reviewer_views():
            name = _as_str(view.get("reviewer"))
            if name is None or view.get("status") == "skipped":
                continue
            row = table.setdefault(name, {"reviewer": name, "models": [], "reviews": 0, "failed": 0,
                                          **dict.fromkeys(review_record.mark_counts(()), 0)})
            model = _as_str(view.get("model"))
            if model is not None and model not in row["models"]:
                row["models"].append(model)
            if view.get("status") != "finished":
                row["failed"] += 1
                continue
            row["reviews"] += 1
            for key, count in review_record.mark_counts(view["findings"]).items():
                row[key] += count
    return sorted(table.values(), key=lambda r: r["reviewer"])


def _fmt_seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}s"


def _fmt_share(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def command(args: Any, store: state.Store) -> int:
    report = stats(store, None if args.days == 0 else args.days, args.lane)
    if args.json:
        print(json.dumps(report))
        return 0
    window = "everything kept" if not report["days"] else f"the last {report['days']:g} days"
    print(f"Checks over {window}" + (f", lane {args.lane}" if args.lane else "") + ":")
    if not report["checks"]:
        print("  no recorded runs")
    for row in report["checks"]:
        p50 = _fmt_seconds(row["p50"])
        p90 = _fmt_seconds(row["p90"])
        print(f"  {row['check']}: {row['runs']} runs, p50 {p50}, p90 {p90}, {row['failed']} red, "
              f"{row['flakes']} flakes")

    flow = report["lane_flow"]
    print("Lane flow:")
    for label, key in (
        ("to first green", "to_first_green"),
        ("to land", "to_land"),
        ("to promote", "to_promote"),
    ):
        metric = flow[key]
        if metric["count"]:
            print(f"  {label}: p50 {_fmt_seconds(metric['p50'])}, p90 {_fmt_seconds(metric['p90'])} "
                  f"(n={metric['count']})")
        else:
            print(f"  {label}: - (n=0)")
    if flow["phases"]:
        print("  phases:")
        for phase in flow["phases"]:
            wait_text = (
                f"wait p50 {_fmt_seconds(phase['wait_p50'])}, p90 {_fmt_seconds(phase['wait_p90'])}"
                if phase["wait_p50"] is not None
                else "wait unknown"
            )
            print(f"    {phase['phase']}: {phase['runs']} runs, run p50 {_fmt_seconds(phase['run_p50'])}, "
                  f"p90 {_fmt_seconds(phase['run_p90'])}, {wait_text}")

    outcomes = report["outcomes"]
    print("Outcomes:")
    other_part = f", {outcomes['other']} other" if outcomes["other"] else ""
    print(f"  total {outcomes['total']}: {outcomes['landed']} landed, {outcomes['parked']} parked, "
          f"{outcomes['abandoned']} abandoned, {outcomes['dropped']} dropped, "
          f"{outcomes['active']} active, {outcomes['planned']} planned{other_part}")

    overrides = report["overrides"]
    fix = overrides["fix_lanes"]
    print("Overrides:")
    print(f"  total {overrides['total']} (lane {overrides['lane']}, promote {overrides['promote']}); "
          f"fix lanes: {fix['total']} ({fix['single']} single, {fix['batch']} batch)")
    if overrides["reasons"]:
        top_reasons = ", ".join(f"{r['reason']} ({r['count']})" for r in overrides["reasons"][:5])
        print(f"  reasons: {top_reasons}")

    rev = report["review"]
    print("Review:")
    gate = rev["gate"]
    if rev["reviewed_lanes"]:
        per_lane = f"{rev['findings_per_lane']:.2f}" if rev["findings_per_lane"] is not None else "-"
        print(f"  {rev['reviewed_lanes']} reviewed lanes, {rev['findings']} findings ({per_lane}/lane; "
              f"p1 {rev['p1']}, p2 {rev['p2']}, p3 {rev['p3']}): {_fmt_share(rev['fixed_share'])} fixed, "
              f"{_fmt_share(rev['rejected_share'])} rejected, {_fmt_share(rev['unmarked_share'])} unmarked; "
              f"gate: {gate['held']} held, {gate['overridden']} overridden")
    else:
        print(f"  no finished lane reviews; gate: {gate['held']} held, {gate['overridden']} overridden")

    if report["reviewers"]:
        print("Reviewers:")
    for row in report["reviewers"]:
        print(f"  {row['reviewer']} ({', '.join(row['models'])}): {row['reviews']} reviews, {row['findings']} findings "
              f"(p1 {row['p1']}, p2 {row['p2']}, p3 {row['p3']}), {row['fixed']} fixed, {row['rejected']} rejected, "
              f"{row['unmarked']} unmarked" + (f", {row['failed']} failed" if row["failed"] else ""))
    return 0
