"""Lane records and receipts: one JSON file per lane in the state directory, receipts beside it.

  <state>/<id>.json                 the lane record (status, branch, base, claims, worktree, landed, ...)
  <state>/<id>.receipt.json         the lane check's receipt for the tree it verified
  <state>/<id>.first.json           the lane's first receipt, kept whatever the branch does later
  <state>/<id>.post-merge.json      the post-merge smoke's receipt
  <state>/<id>.review.json          the lane's review
  <state>/promote-<sha>.gate.json   a batch gate; perf-<sha>.perf.json a perf run
  <state>/ledger.log, logs/, reviews/, .<name>.lock

Schema 1 is the format Straylight's tools/lane wrote before the move (#648): a record without `schema` is schema 1,
and lanefather writes nothing an older tools/lane cannot read, so live lanes on older branches share the directory
with this tool in both directions. A record of a newer schema is refused rather than rewritten.
"""
from __future__ import annotations

from collections.abc import Generator
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
from typing import Any

from . import config
from .errors import LaneError

Record = dict[str, Any]

SCHEMA = 1
# Lanes in these statuses count toward the cap and hold their claims; a landed lane holds neither.
OCCUPYING = ("planned", "active")
LANDED = ("landed", "released", "done")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def state_dir() -> Path:
    path = Path(os.path.expanduser(config.current().env("STATE") or config.current().state_dir))
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_path(lane_id: str) -> Path:
    if not ID.match(lane_id):
        raise LaneError(f"lane id {lane_id!r}: use letters, digits, '_' or '-'")
    return state_dir() / f"{lane_id}.json"


def receipt_path(lane_id: str, kind: str = "receipt") -> Path:
    return state_dir() / f"{lane_id}.{kind}.json"


def read_json(path: Path) -> dict[str, Any] | None:
    """The JSON object in the file; None for a missing or unreadable file, or one that holds no object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None  # pyright: ignore[reportUnknownVariableType]


def write_json(path: Path, data: Any) -> None:
    pending = path.with_name(path.name + ".pending")
    pending.write_text(json.dumps(data, indent=2) + "\n")
    pending.replace(path)


def migrate(record: Record) -> Record:
    """A record as this schema reads it. Schema 1 needs nothing but the defaults below."""
    schema = record.get("schema", 1)
    if not isinstance(schema, int) or schema > SCHEMA:
        raise LaneError(f"lane {record.get('id')} has schema {schema}, newer than this lanefather's {SCHEMA}: "
                        "update lanefather")
    # A record from before #577 may lack the key; a planned lane's is null until adopt.
    record.setdefault("worktree", None)
    return record


def load(lane_id: str) -> Record:
    record = read_json(record_path(lane_id))
    if not isinstance(record, dict):
        raise LaneError(f"no lane {lane_id}; register it with {config.prog()} start")
    return migrate(record)  # pyright: ignore[reportUnknownArgumentType]


def save(record: Record) -> None:
    write_json(record_path(record["id"]), record)


def lanes() -> list[Record]:
    found: list[Record] = []
    for path in sorted(state_dir().glob("*.json")):
        if path.name.count(".") == 1:
            record = read_json(path)
            if isinstance(record, dict) and "id" in record:
                found.append(migrate(record))  # pyright: ignore[reportUnknownArgumentType]
    return found


def occupying(records: list[Record] | None = None) -> list[Record]:
    """The lanes that count toward the cap and hold their claims."""
    return [r for r in (lanes() if records is None else records) if r["status"] in OCCUPYING]


@contextlib.contextmanager
def locked(name: str, waiting: str | None = None) -> Generator[None]:
    """Hold the named lock in the state directory; print `waiting` first when another process holds it."""
    with open(state_dir() / f".{name}.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if waiting:
                print(waiting, flush=True)
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
