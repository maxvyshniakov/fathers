"""Lane records and receipts: one JSON file per lane in the state directory, receipts beside it.

  <state>/<id>.json                 the lane record (status, branch, base, claims, worktree, landed, ...)
  <state>/<id>.receipt.json         the lane check's receipt for the tree it verified
  <state>/<id>.first.json           the lane's first receipt, kept whatever the branch does later
  <state>/<id>.post-merge.json      the post-merge smoke's receipt
  <state>/<id>.review.json          the lane's review
  <state>/promote-<sha>.gate.json   a batch gate; perf-<sha>.perf.json a perf run
  <state>/results/<tree>.json        check results by tree and plan (lanefather.results, fathers#91)
  <state>/history/<YYYY-MM>.jsonl    the run history: per-check runs and flakes (lanefather.history, fathers#92)
  <state>/ledger.log, logs/, reviews/, .<name>.lock

Schema 1 is the format Straylight's tools/lane wrote before the move (#648): a record without `schema` is schema 1,
and lanefather writes nothing an older tools/lane cannot read, so live lanes on older branches share the directory
with this tool in both directions. A record of a newer schema is refused rather than rewritten.
"""
from __future__ import annotations

from collections.abc import Generator
import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any

from . import config
from .errors import LaneError

Record = dict[str, Any]

SCHEMA = 1
# Lanes in these statuses count toward the cap and hold their claims; a landed lane holds neither.
OCCUPYING = ("planned", "active")
LANDED = ("landed", "released", "done")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class Store:
    """A project's state directory. Instances keep one project's reads and locks together, independently of the CLI."""

    def __init__(self, project: config.Config, directory: Path | None = None):
        """`directory` names the state directory explicitly (the registry's, for status); by default it is the
        environment's LANEFATHER_STATE, else the project's state_dir."""
        self.project = project
        self.directory = directory or Path(os.path.expanduser(project.env("STATE") or project.state_dir))

    def record_path(self, lane_id: str) -> Path:
        if not ID.match(lane_id):
            raise LaneError(f"lane id {lane_id!r}: use letters, digits, '_' or '-'")
        return self.directory / f"{lane_id}.json"

    def receipt_path(self, lane_id: str, kind: str = "receipt") -> Path:
        return self.directory / f"{lane_id}.{kind}.json"

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
        with open(self.directory / f".{name}.lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if waiting:
                    print(waiting, flush=True)
                fcntl.flock(handle, fcntl.LOCK_EX)
            yield


def state_dir() -> Path:
    path = Store(config.current()).directory
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_path(lane_id: str) -> Path:
    state_dir()
    return Store(config.current()).record_path(lane_id)


def receipt_path(lane_id: str, kind: str = "receipt") -> Path:
    return state_dir() / f"{lane_id}.{kind}.json"


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


UNSYNCABLE = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}


def cut_short(path: Path) -> str:
    return (f"lane state {path} is unreadable, empty or not a JSON object: a write was cut short or the file is "
            "damaged. Restore the record or remove the file; until then it is not read as a lane, and start does not "
            "replace it")


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Replace `path` with `text` so a reader sees the old file or the whole new one, and a crash after the call
    returns keeps the new one (fathers#129).

    Each writer gets its own temporary file beside the target, named so no glob of records matches it; the file
    and then its directory are synced; a failed write removes the temporary file and leaves the target as it was.
    `mode` sets the file's permissions; None leaves them to the umask, as for an ordinary new file."""
    pending = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    # The rename lasts once the directory entry does. A file system that cannot sync a directory says so with
    # EINVAL or ENOTSUP and has done what it can; any other failure (EIO) is the caller's to see.
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    except OSError as error:
        if error.errno not in UNSYNCABLE:
            raise
    finally:
        os.close(folder)


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
    if isinstance(record.get("review_fix_of"), list):
        # A brief schema-1 format put the batch in the legacy string field. Normalize it on the next write.
        parents = fix_parents(record)
        if parents:
            set_fix_parents(record, parents, batch=True)
    return record


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


def load(lane_id: str) -> Record:
    state_dir()
    return Store(config.current()).load(lane_id)


def save(record: Record) -> None:
    Store(config.current()).save(record)


def lanes() -> list[Record]:
    return Store(config.current()).lanes()


def occupying(records: list[Record] | None = None) -> list[Record]:
    """The lanes that count toward the cap and hold their claims."""
    return [r for r in (lanes() if records is None else records) if r["status"] in OCCUPYING]


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


@contextlib.contextmanager
def locked(name: str, waiting: str | None = None) -> Generator[None]:
    """Hold the current project's state lock; explicit project callers use Store directly."""
    with Store(config.current()).locked(name, waiting):
        yield
