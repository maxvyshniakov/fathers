"""Tokenfather's events in the shared fathers event log (Godfather schema v1, validated by common/eventcheck.py).

Backed by `fathers_common.events` (fathers#117): it strips C0/DEL/C1 and U+2028/U+2029, caps strings, bounds nesting at
four levels, keeps lines within 16 KiB, and appends with rotation past 4 MiB under a bounded flock on `events.lock`.
"""
from __future__ import annotations

from collections.abc import Mapping
import datetime
import os
from pathlib import Path
import tempfile
from typing import Any

from fathers_common import events as _common

SCHEMA = _common.SCHEMA_VERSION
SOURCE = "tokenfather"
TAG = "tokens"
MAX_LINE = _common.MAX_LINE
ROTATE_AT = _common.ROTATE_AT
MAX_DEPTH = _common.MAX_DEPTH
MAX_STRING = _common.MAX_STRING
LOCK_WAIT = _common.LOCK_WAIT

KINDS: dict[str, str] = {
    "usage.collected": "collect updated the daily rollup with new model requests",
    "usage.daily": "daily usage summary across providers",
}


def machine_dir() -> Path:
    return Path.home() / ".local" / "state" / "tokenfather"


def path() -> Path:
    configured = os.environ.get("TOKENFATHER_EVENTS")
    return Path(os.path.expanduser(configured)) if configured else machine_dir() / "events.jsonl"


def temporary(directory: Path) -> bool:
    """Whether `directory` lies under a temporary root, where a test's state must not write the machine's log."""
    try:
        resolved = directory.expanduser().resolve()
    except OSError:
        return False
    roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve(), Path("/var/folders").resolve()}
    return any(resolved == root or root in resolved.parents for root in roots)


def clean(value: Any, depth: int = 1, limit: int = MAX_STRING) -> Any:
    return _common.clean(value, depth, limit)


def event(
    kind: str,
    subject: Mapping[str, Any],
    details: Mapping[str, Any],
    *,
    level: str = "info",
    moment: datetime.datetime | None = None,
) -> dict[str, Any]:
    """A schema-v1 event from Tokenfather itself (the actor is this process)."""
    actor = {"type": "tool", "name": SOURCE, "pid": os.getpid()}
    return _common.build_event(SOURCE, kind, subject, actor, details, level=level, moment=moment)


def encode(found: Mapping[str, Any]) -> bytes:
    return _common.encode_event(found)


def append(found: Mapping[str, Any], log: Path | None = None) -> bool:
    """Append `found` to `log` (default `path()`); never raises, False when the event was dropped."""
    return _common.append_event(log or path(), found, rotate_at=ROTATE_AT, lock_wait=LOCK_WAIT)
