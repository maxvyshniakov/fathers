"""Shared writer for Godfather's tool event schema, version 1 (godfather/docs/events.md, fathers#117).

Strips C0/DEL/C1 control characters and Unicode line/paragraph separators (U+2028/U+2029), converts non-finite floats
to None, caps strings consistently (1000 characters, 400 for commands, 64 for object keys), bounds nesting to 4 levels,
keeps every serialized line within 16 KiB, and appends with O_APPEND (mode 0600) and rotation under a bounded flock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import contextlib
import datetime
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import socket
import time
from typing import Any, cast

SCHEMA_VERSION = 1
MAX_LINE = 16 * 1024
ROTATE_AT = 4 << 20
MAX_DEPTH = 4  # details itself is level 1
MAX_STRING = 1000
MAX_COMMAND = 400
MAX_KEY = 64
LOCK_WAIT = 1.0

# C0, DEL, C1, plus U+2028/U+2029 which split lines in Python's str.splitlines().
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def host_name() -> str:
    """The machine's lowercase short host name without dots (`localhost` fallback)."""
    name = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".", 1)[0].lower()).strip("-")
    return name[:63].strip("-") or "localhost"


def timestamp(moment: datetime.datetime | float | int | None = None) -> str:
    """RFC 3339 UTC timestamp with milliseconds (`YYYY-MM-DDTHH:MM:SS.mmmZ`)."""
    if moment is None:
        utc = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(moment, (int, float)):
        utc = datetime.datetime.fromtimestamp(moment, datetime.timezone.utc)
    else:
        utc = moment.astimezone(datetime.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def clean(value: Any, depth: int = 1, limit: int = MAX_STRING) -> Any:
    """Sanitize `value` for schema v1: no control/separator chars, finite numbers, bounded depth and length."""
    if isinstance(value, str):
        text = CONTROL.sub(" ", value)
        return text if len(text) <= limit else text[:limit - 1] + "…"
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        mapping = cast(Mapping[Any, Any], value)
        if depth >= MAX_DEPTH:
            return clean(json.dumps({str(k): v for k, v in mapping.items()}, default=str, sort_keys=True), depth, limit)
        return {clean(str(k), depth, MAX_KEY): clean(v, depth + 1, limit) for k, v in mapping.items()}
    if isinstance(value, (list, tuple)):
        items = list(cast(Sequence[Any], value))
        if depth >= MAX_DEPTH:
            return clean(json.dumps(items, default=str, sort_keys=True), depth, limit)
        return [clean(item, depth + 1, limit) for item in items]
    return clean(str(value), depth, limit)


def command(value: str | Sequence[str] | None) -> str:
    """Format and cap a command string or argv sequence to 400 characters."""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        text = " ".join(value)
    return cast(str, clean(text, 1, MAX_COMMAND))


def _clean_flat_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitize `subject` or `actor`: keep an object of scalar values (any nested container stringified)."""
    return {
        cast(str, clean(str(key), MAX_DEPTH, MAX_KEY)): clean(val, MAX_DEPTH)
        for key, val in mapping.items()
        if val is not None
    }


def build_event(
    source: str,
    kind: str,
    subject: Mapping[str, Any],
    actor: Mapping[str, Any],
    details: Mapping[str, Any] | None = None,
    *,
    level: str | None = None,
    event_id: str | None = None,
    moment: datetime.datetime | float | int | None = None,
) -> dict[str, Any]:
    """Construct a schema-v1 event dictionary with sanitized fields."""
    event: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "time": timestamp(moment),
        "host": host_name(),
        "source": source,
        "kind": kind,
        "subject": _clean_flat_mapping(subject),
        "actor": _clean_flat_mapping(actor),
        "details": clean(dict(details) if details is not None else {}, 1),
    }
    if level and level != "info":
        event["level"] = level
    if event_id:
        event["id"] = clean(event_id, MAX_DEPTH, MAX_KEY)
    return event


def encode_event(found: Mapping[str, Any], *, compact: bool = True) -> bytes:
    """Serialize an event as a UTF-8 JSON line of at most 16 KiB."""
    separators = (",", ":") if compact else None
    raw_details: Mapping[str, Any] = (
        cast(Mapping[str, Any], found["details"]) if isinstance(found.get("details"), Mapping) else {}
    )
    raw_subject: Mapping[str, Any] = (
        cast(Mapping[str, Any], found["subject"]) if isinstance(found.get("subject"), Mapping) else {}
    )
    raw_actor: Mapping[str, Any] = (
        cast(Mapping[str, Any], found["actor"]) if isinstance(found.get("actor"), Mapping) else {}
    )
    base = {str(k): v for k, v in found.items()}
    base["subject"] = _clean_flat_mapping(raw_subject)
    base["actor"] = _clean_flat_mapping(raw_actor)
    for limit in (MAX_STRING, 200):
        candidate = dict(base, details=clean(raw_details, 1, limit))
        data = (
            json.dumps(candidate, sort_keys=True, allow_nan=False, ensure_ascii=False, separators=separators) + "\n"
        ).encode("utf-8")
        if len(data) <= MAX_LINE:
            return data
    fallback: dict[str, Any] = {"truncated": True}
    if isinstance(raw_details.get("message"), str):
        fallback["message"] = clean(raw_details["message"], 1, 2000)
    candidate = dict(base, details=fallback)
    data = (
        json.dumps(candidate, sort_keys=True, allow_nan=False, ensure_ascii=False, separators=separators) + "\n"
    ).encode("utf-8")
    if len(data) > MAX_LINE:
        raise ValueError("event too large")
    return data


def hold_lock(handle: Any, wait: float = LOCK_WAIT) -> bool:
    """Acquire an exclusive flock on `handle` within `wait` seconds; False if held past the deadline."""
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            pass
        except OSError as error:
            if error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                raise
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def append_event(
    target: Path | str,
    event: Mapping[str, Any] | bytes,
    *,
    rotate_at: int = ROTATE_AT,
    lock_wait: float = LOCK_WAIT,
) -> bool:
    """Append one schema-v1 event line to `target`, rotating to `<name>.1` past `rotate_at` bytes.

    Never raises: returns True when written, False when dropped (lock timeout, oversized event, or I/O error).
    """
    path = Path(target)
    with contextlib.suppress(Exception):
        line = event if isinstance(event, bytes) else encode_event(event)
        if not line.endswith(b"\n") or len(line) > MAX_LINE:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name("events.lock"), "a", encoding="utf-8") as lock:
            if not hold_lock(lock, lock_wait):
                return False
            with contextlib.suppress(FileNotFoundError):
                if path.stat().st_size + len(line) > rotate_at:
                    os.replace(path, path.with_name(path.name + ".1"))
            fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                size = os.fstat(fd).st_size
                prefix = b"\n" if size and os.pread(fd, 1, size - 1) != b"\n" else b""
                os.write(fd, prefix + line)
            finally:
                os.close(fd)
        return True
    return False
