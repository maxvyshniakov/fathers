"""events.jsonl: the queue's events in Godfather's shared event schema, version 1 (#29; docs/api.md, Events).

Every site that writes a queue.log line also appends one event here, after the change it describes has happened;
queue.log and history.jsonl stay as they are. An event that cannot be written never fails or alters the operation.
Each line is one write on a file opened with O_APPEND and mode 0600; past EVENTS_MAX bytes the log becomes
events.jsonl.1, replacing the previous one, under an flock on events.lock beside it.

Kinds: lease.granted, lease.released, request.left, lease.reaped, request.reaped, request.reprioritized,
request.waiting_long, lease.yield_asked, lease.yield_withdrawn, lease.yielded, capacity.changed, order.changed,
process.swept, session.run_started, session.run_ended, session.ended, run.outlier, run.trend.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import socket
from typing import Any, cast

from . import config as _config

VERSION = 1
EVENTS_MAX = 4 << 20  # bytes of events.jsonl before it becomes events.jsonl.1
LINE_MAX = 16 * 1024  # bytes of one line with its newline
COMMAND_MAX = 400  # characters of a command line kept
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")
KINDS = ("lease.granted", "lease.released", "request.left", "lease.reaped", "request.reaped", "request.reprioritized",
         "request.waiting_long", "lease.yield_asked", "lease.yield_withdrawn", "lease.yielded", "capacity.changed",
         "order.changed", "process.swept", "session.run_started", "session.run_ended", "session.ended",
         "run.outlier", "run.trend")


def path(directory: Path) -> Path:
    return directory / "events.jsonl"


def host() -> str:
    """The machine's short host name, lowercase."""
    name = socket.gethostname().split(".")[0].lower()
    name = re.sub(r"[^a-z0-9-]", "-", name).strip("-")[:63]
    return name or "localhost"


def moment() -> str:
    """Now, RFC 3339 UTC with milliseconds."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def clean(value: Any) -> Any:
    """Strings without control characters (none reaches a terminal as an escape), recursively."""
    if isinstance(value, str):
        return CONTROL.sub(" ", value)
    if isinstance(value, dict):
        return {CONTROL.sub(" ", str(key)): clean(item) for key, item in cast(dict[Any, Any], value).items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in cast(list[Any], value)]
    if isinstance(value, float) and value != value:
        return None
    return value


def command(text: Any) -> str:
    return str(text or "")[:COMMAND_MAX]


def request(entry: dict[str, Any], entry_path: Path | None = None) -> dict[str, Any]:
    """The subject for a queue entry: its id (the entry's file name, as `status --json` gives it), label and pid."""
    subject: dict[str, Any] = {"type": "request",
                               "id": entry_path.stem if entry_path is not None else f"pid-{entry.get('pid')}"}
    if entry.get("label") is not None:
        subject["label"] = str(entry.get("label"))
    if entry_path is not None or isinstance(entry.get("lease_id"), str):
        # unique (fathers#101), as status --json gives it; `id` stays the entry's name
        subject["lease_id"] = _config.lease_id(entry_path.stem if entry_path is not None else "", entry)
    if isinstance(entry.get("pid"), int) and entry["pid"] > 0:
        subject["pid"] = entry["pid"]
    return subject


def actor(pid: int | None = None, lane: str | None = None, kind: str = "process") -> dict[str, Any]:
    """Who caused it: a process (this one unless `pid`), with its lane where it names one, and `via: godfather` when
    this process is a command Godfather ran (GODFATHER_ACTOR); never for another process, such as a holder whose
    grant this command's look made."""
    own = pid is None or pid == os.getpid()
    found: dict[str, Any] = {"type": kind, "pid": pid if pid is not None else os.getpid()}
    if kind == "tool":
        found["name"] = "slotfather"
    if lane:
        found["lane"] = lane
    if own and (os.environ.get("GODFATHER_ACTOR") or "").startswith("godfather"):
        found["via"] = "godfather"
    return found


def holder(entry: dict[str, Any]) -> dict[str, Any]:
    """The actor of what a lease's own client does: its holder process and lane."""
    pid = entry.get("pid") if isinstance(entry.get("pid"), int) and entry["pid"] > 0 else None
    return actor(pid, _config.meta_of(entry).get("lane"))


def line(kind: str, subject: dict[str, Any], details: dict[str, Any], who: dict[str, Any],
         level: str | None = None) -> bytes:
    event: dict[str, Any] = {"v": VERSION, "time": moment(), "host": host(), "source": "slotfather", "kind": kind,
                             "subject": clean(subject), "actor": clean(who), "details": clean(details)}
    if level and level != "info":
        event["level"] = level
    data = (json.dumps(event, sort_keys=True, allow_nan=False, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > LINE_MAX:  # never an oversized line: the details go, the event stays
        event["details"] = {"truncated": True}
        data = (json.dumps(event, sort_keys=True, allow_nan=False, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > LINE_MAX:  # a subject or actor too large even so: no line at all
        raise ValueError("event too large")
    return data


def emit(directory: Path, kind: str, subject: dict[str, Any], details: dict[str, Any] | None = None,
         who: dict[str, Any] | None = None, level: str | None = None) -> None:
    """Appends one event; never raises. Nothing is written in another user's queue."""
    with contextlib.suppress(Exception):
        from .state import foreign_owner
        if foreign_owner(directory) is not None:
            return
        data = line(kind, subject, details or {}, who or actor(), level)
        target = path(directory)
        fd = os.open(target, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            size = os.fstat(fd).st_size
            # A torn earlier write must not swallow this line: start it on a line of its own.
            prefix = b"\n" if size and os.pread(fd, 1, size - 1) != b"\n" else b""
            os.write(fd, prefix + data)  # one write, at the end even with other appenders
        finally:
            os.close(fd)
        if target.stat().st_size > EVENTS_MAX:
            rotate(target)


def rotate(target: Path) -> None:
    with open(target.with_name("events.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists() and target.stat().st_size > EVENTS_MAX:  # another writer may have rotated meanwhile
            target.replace(target.with_name(target.name + ".1"))
