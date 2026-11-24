"""Lane records and receipts: one JSON file per lane in the state directory, receipts beside it.

  <state>/<id>.json                 the lane record (status, branch, base, claims, worktree, landed, ...)
  <state>/<id>.receipt.json         the lane check's receipt for the tree it verified (lanefather.receipts)
  <state>/<id>.first.json           the lane's first receipt, kept whatever the branch does later
  <state>/<id>.post-merge.json      the post-merge smoke's receipt
  <state>/<id>.review.json          the lane's review
  <state>/promote-<sha>.gate.json   a batch gate; perf-<sha>.perf.json a perf run
  <state>/promote/promote-<sha>.json a promote/ship run record (fathers#58)
  <state>/results/<tree>.json        check results by tree and plan (lanefather.results, fathers#91)
  <state>/history/<YYYY-MM>.jsonl    the run history: per-check runs and flakes (lanefather.history, fathers#92)
  <state>/ledger.log, logs/, reviews/, .<name>.lock

Schema 1 is the format Straylight's tools/lane wrote before the move (#648): a record without `schema` is schema 1,
and lanefather writes nothing an older tools/lane cannot read, so live lanes on older branches share the directory
with this tool in both directions. A record of a newer schema is refused rather than rewritten.
"""
from __future__ import annotations

from collections.abc import Callable, Generator, Iterable
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from fathers_common.atomic import atomic_write as _atomic_write

from . import config
from .errors import LaneError

Record = dict[str, Any]

SCHEMA = 1
# Lanes in these statuses count toward the cap and hold their claims; a landed lane holds neither.
OCCUPYING = ("planned", "active")
LANDED = ("landed", "released", "done")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_LOCKED = threading.local()


class Store:
    """A project's state directory. Instances keep one project's reads and locks together, independently of the CLI."""

    def __init__(self, project: config.Config, directory: Path | None = None):
        """`directory` names the state directory explicitly (the registry's, for status); by default it is the
        environment's LANEFATHER_STATE, else the project's state_dir."""
        self.project = project
        self.directory = directory or Path(os.path.expanduser(project.env("STATE") or project.state_dir))

    @property
    def prog(self) -> str:
        return self.project.command

    @property
    def results_dir(self) -> Path:
        return self.directory / "results"

    @property
    def history_dir(self) -> Path:
        return self.directory / "history"

    @property
    def reviews_dir(self) -> Path:
        return self.directory / "reviews"

    @property
    def promote_dir(self) -> Path:
        return self.directory / "promote"

    def promote_run_path(self, sha: str) -> Path:
        return self.promote_dir / f"promote-{sha}.json"

    def logs_dir(self) -> Path:
        path = self.directory / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_path(self, label: str) -> Path:
        return self.logs_dir() / f"{label}-{time.strftime('%Y%m%dT%H%M%S')}.log"

    def record_path(self, lane_id: str) -> Path:
        if not ID.match(lane_id):
            raise LaneError(f"lane id {lane_id!r}: use letters, digits, '_' or '-'")
        return self.directory / f"{lane_id}.json"

    def receipt_path(self, lane_id: str, kind: str = "receipt") -> Path:
        actual = "receipt" if kind == "check" else kind
        return self.directory / f"{lane_id}.{actual}.json"

    def load(self, lane_id: str) -> Record:
        path = self.record_path(lane_id)
        record = read_json(path)
        if record is None and path.exists():
            raise LaneError(cut_short(path))
        if not isinstance(record, dict):
            raise LaneError(f"no lane {lane_id}; register it with {self.project.command} start")
        return migrate(record)  # pyright: ignore[reportUnknownArgumentType]

    def save(self, record: Record) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        write_json(self.record_path(record["id"]), record)

    def update(self, lane_id: str, change: Callable[[Record], Any]) -> Record:
        """Reload the lane record under the `state` lock, apply `change(record)`, save and return it.
        When `change` returns False, the record is not rewritten."""
        with self.locked("state"):
            record = self.load(lane_id)
            if change(record) is not False:
                self.save(record)
            return record

    def lanes(self, *, strict: bool = False, damaged: list[Path] | None = None) -> list[Record]:
        """The lane records. A record a write cut short raises, so nothing that admits, claims or collects acts on
        a partial set; a read-only listing passes `damaged` to get the healthy lanes and the damaged paths."""
        found: list[Record] = []
        for path in sorted(self.directory.glob("*.json")):
            if path.name.count(".") == 1:
                record = read_json(path, reject_duplicates=strict)
                if record is None and read_json(path) is None and path.exists():
                    if damaged is None:
                        raise LaneError(cut_short(path))
                    damaged.append(path)
                    continue
                if record is not None and "id" in record:
                    if strict and (record["id"] != path.stem or record.get("status") not in (*OCCUPYING, *LANDED)):
                        raise LaneError(f"lane state {path} has invalid identity or status; collection refuses")
                    found.append(migrate(record))
                elif strict:
                    raise LaneError(f"lane state {path} is unreadable or has no id; collection refuses")
        return found

    @contextlib.contextmanager
    def locked(self, name: str, waiting: str | None = None) -> Generator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = (self.directory / f".{name}.lock").resolve()
        held: set[Path] | None = getattr(_LOCKED, "paths", None)
        if held is None:
            held = set()
            _LOCKED.paths = held
        if lock_path in held:
            yield
            return
        with open(lock_path, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if waiting:
                    print(waiting, flush=True)
                fcntl.flock(handle, fcntl.LOCK_EX)
            held.add(lock_path)
            try:
                yield
            finally:
                held.discard(lock_path)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("duplicate JSON field")
        record[key] = value
    return record


def read_json(path: Path, *, reject_duplicates: bool = False) -> dict[str, Any] | None:
    """The JSON object in the file; None for a missing or unreadable file, or one that holds no object."""
    try:
        data = json.loads(path.read_text(), object_pairs_hook=unique_object if reject_duplicates else None)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None  # pyright: ignore[reportUnknownVariableType]


def cut_short(path: Path) -> str:
    return (f"lane state {path} is unreadable, empty or not a JSON object: a write was cut short or the file is "
            "damaged. Restore the record or remove the file; until then it is not read as a lane, and start does not "
            "replace it")


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Replace `path` with `text` durably via `fathers_common.atomic.atomic_write` (fathers#129, fathers#117)."""
    _atomic_write(path, text, mode=mode)


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2) + "\n")


def migrate(record: Record) -> Record:
    """A record as this schema reads it. Schema 1 needs nothing but the defaults below."""
    schema = record.get("schema", 1)
    if not isinstance(schema, int) or schema > SCHEMA:
        raise LaneError(f"lane {record.get('id')} has schema {schema}, newer than this lanefather's {SCHEMA}: "
                        "update lanefather")
    # A record from before #577 may lack the key; a planned lane's is null until adopt.
    record.setdefault("worktree", None)
    # fathers#200: every agent that touched the lane at start, adopt or check.
    if not isinstance(record.get("agents"), list):
        record["agents"] = []
    if isinstance(record.get("review_fix_of"), list):
        # A brief schema-1 format put the batch in the legacy string field. Normalize it on the next write.
        parents = fix_parents(record)
        if parents:
            set_fix_parents(record, parents, batch=True)
    return record


def record_agent(record: Record, phase: str, agent: dict[str, Any] | None, *, now: str | None = None) -> bool:
    """Attach or update an agent entry on `record["agents"]` (fathers#200). Returns True when recorded."""
    if not agent or not agent.get("kind"):
        return False
    raw_agents: Any = record.get("agents")
    agents: list[dict[str, Any]]
    if isinstance(raw_agents, list):
        agents = raw_agents  # pyright: ignore[reportUnknownVariableType]
    else:
        agents = []
        record["agents"] = agents
    kind = agent["kind"]
    session_id = agent.get("session_id")
    subagent_id = agent.get("subagent_id")
    thread_id = agent.get("thread_id")
    transcript_path = agent.get("transcript_path")
    for entry in agents:
        if (entry.get("kind") == kind
                and entry.get("session_id") == session_id
                and entry.get("subagent_id") == subagent_id
                and entry.get("thread_id") == thread_id
                and (not transcript_path or not entry.get("transcript_path")
                     or entry.get("transcript_path") == transcript_path)):
            phases: list[str] = list(entry.get("events") or [])
            if phase not in phases:
                phases.append(phase)
            entry["events"] = phases
            if now:
                entry["last_seen"] = now
            for field in ("transcript_path", "model"):
                if agent.get(field) and not entry.get(field):
                    entry[field] = agent[field]
            if isinstance(agent.get("options"), dict) and agent["options"]:
                merged = dict(entry.get("options") or {})
                merged.update(agent["options"])
                entry["options"] = merged
            return True
    entry_new: dict[str, Any] = {"kind": kind}
    for field in ("session_id", "subagent_id", "thread_id", "transcript_path", "model"):
        if agent.get(field):
            entry_new[field] = agent[field]
    if isinstance(agent.get("options"), dict) and agent["options"]:
        entry_new["options"] = dict(agent["options"])
    entry_new["events"] = [phase]
    if now:
        entry_new["first_seen"] = now
        entry_new["last_seen"] = now
    agents.append(entry_new)
    return True


def batch_fix(record: Record) -> bool:
    return "review_fix_of_all" in record or isinstance(record.get("review_fix_of"), list)


def fix_parents(record: Record) -> list[str]:
    """Read a hand-over or batch, including the list written by early batch checkouts."""
    named: Any = record.get("review_fix_of_all", record.get("review_fix_of"))
    if isinstance(named, str):
        return [named] if named else []
    if isinstance(named, list):
        return [str(n) for n in named if isinstance(n, str) and n]  # pyright: ignore[reportUnknownVariableType]
    return []


def hand_over_parent(record: Record) -> str | None:
    parents = fix_parents(record)
    return parents[0] if len(parents) == 1 and not batch_fix(record) and ID.match(parents[0]) else None


def set_fix_parents(record: Record, parents: list[str], *, batch: bool) -> None:
    if parents:
        # Older checks read this key as a lane ID, even if the current lane has a literal-SHA base.
        record["review_fix_of"] = parents[0]
        if batch:
            record["review_fix_of_all"] = parents


def occupying(records: Iterable[Record]) -> list[Record]:
    """The lanes that count toward the cap and hold their claims."""
    return [r for r in records if r["status"] in OCCUPYING]


def of_project(records: list[Record], common: str | None) -> list[Record]:
    """The records of the repository whose Git common directory is `common` (fathers#149): a state directory several
    projects share holds each one's lanes. None keeps every record; a record that names no repository is kept too,
    as it may be this one's."""
    return [r for r in records if common is None or r.get("common_dir") in (None, common)]


def project_name(common: str | None) -> str:
    """The repository a Git common directory belongs to, by its main checkout's directory name."""
    if not common:
        return "an unnamed repository"
    return os.path.basename(os.path.dirname(common) if os.path.basename(common) == ".git" else common)
