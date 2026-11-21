"""Shared fathers event log writer for Tokenfather (Godfather schema v1, validated by common/eventcheck.py)."""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, cast

from .schema import format_timestamp, host_name

SCHEMA = 1
SOURCE = "tokenfather"
TAG = "tokens"
MAX_LINE = 16 * 1024
ROTATE_AT = 4 * 2**20
MAX_DEPTH = 4
MAX_STRING = 1000
LOCK_WAIT = 1.0

KINDS: dict[str, str] = {
    "usage.collected": "collect updated the daily rollup with new model requests",
    "usage.daily": "daily usage summary across providers",
}

_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def machine_dir() -> Path:
    return Path.home() / ".local" / "state" / "tokenfather"


def path() -> Path:
    configured = os.environ.get("TOKENFATHER_EVENTS")
    return Path(os.path.expanduser(configured)) if configured else machine_dir() / "events.jsonl"


def temporary(directory: Path) -> bool:
    try:
        resolved = directory.expanduser().resolve()
    except OSError:
        return False
    roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve(), Path("/var/folders").resolve()}
    return any(resolved == root or root in resolved.parents for root in roots)


def clean(value: Any, depth: int = 1, limit: int = MAX_STRING) -> Any:
    if isinstance(value, str):
        text = _CONTROL.sub(" ", value)
        return text if len(text) <= limit else text[: limit - 1] + "…"
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (dict, list, tuple)):
        if depth >= MAX_DEPTH:
            return clean(json.dumps(value, default=str, sort_keys=True), depth, limit)
        if isinstance(value, dict):
            mapping = cast(dict[Any, Any], value)
            return {clean(str(k), depth, 64): clean(v, depth + 1, limit) for k, v in mapping.items()}
        items: list[Any] = list(cast(Any, value))
        return [clean(item, depth + 1, limit) for item in items]
    return clean(str(value), depth, limit)


def event(
    kind: str,
    subject: dict[str, Any],
    details: dict[str, Any],
    *,
    level: str = "info",
    moment: datetime.datetime | None = None,
) -> dict[str, Any]:
    found: dict[str, Any] = {
        "v": SCHEMA,
        "time": format_timestamp(moment or datetime.datetime.now(datetime.UTC)),
        "host": host_name(),
        "source": SOURCE,
        "kind": kind,
        "subject": {k: clean(v, MAX_DEPTH) for k, v in subject.items() if v is not None},
        "actor": {"type": "tool", "name": SOURCE, "pid": os.getpid()},
        "details": clean(details, 1),
    }
    if level != "info":
        found["level"] = level
    return found


def encode(found: dict[str, Any]) -> bytes:
    for limit in (MAX_STRING, 200):
        payload = dict(found, details=clean(found["details"], 1, limit))
        line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(line) <= MAX_LINE:
            return line
    details = {"message": clean(found["details"].get("message", ""), 1, 2000), "truncated": True}
    return (json.dumps(dict(found, details=details), ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _hold(handle: Any, wait: float) -> bool:
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def append(found: dict[str, Any], log: Path | None = None) -> bool:
    target = log or path()
    with contextlib.suppress(Exception):
        line = encode(found)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target.with_name("events.lock"), "a", encoding="utf-8") as lock:
            if not _hold(lock, LOCK_WAIT):
                return False
            with contextlib.suppress(FileNotFoundError):
                if target.stat().st_size + len(line) > ROTATE_AT:
                    target.replace(target.with_name(target.name + ".1"))
            fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        return True
    return False
