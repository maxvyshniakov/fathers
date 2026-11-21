"""events.jsonl: the queue's events in Godfather's shared event schema, version 1 (#29; docs/api.md, Events).

record() is the one writer of a queue transaction's record (fathers#37): its queue.log line and its event, written
together after the change it describes has happened; nothing else calls state.log() or emit(). queue.log and
history.jsonl stay as they are. An event that cannot be written never fails or alters the operation.
Each line is one write on a file opened with O_APPEND and mode 0600; past EVENTS_MAX bytes the log becomes
events.jsonl.1, replacing the previous one, under an flock on events.lock beside it.

Kinds: lease.granted, lease.released, request.left, lease.reaped, request.reaped, request.reprioritized,
request.waiting_long, request.head_dropped, lease.yield_asked, lease.yield_withdrawn, lease.yielded, lease.lock_ended,
capacity.changed, order.changed,
process.swept, session.run_started, session.run_ended, session.ended, run.outlier, run.trend, reap.skipped,
reap.resumed.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any, Literal, Mapping, overload

from fathers_common import events as _common_events

from . import entry as _entry
from .contract import (
    KINDS,
    Actor,
    Blocker,
    CapacityChanged,
    Details,
    Empty,
    Event,
    Granted,
    HeadDropped,
    Left,
    LockEnded,
    OrderChanged,
    Outlier,
    Peer,
    ReapResumed,
    ReapSkipped,
    Reaped,
    Released,
    Reprioritized,
    SessionEnded,
    SessionRun,
    Subject,
    Swept,
    Trend,
    WaitingLong,
    YieldAsked,
    Yielded,
)

__all__ = [
    "COMMAND_MAX",
    "EVENTS_MAX",
    "KINDS",
    "LINE_MAX",
    "VERSION",
    "Actor",
    "Blocker",
    "CapacityChanged",
    "Details",
    "Empty",
    "Event",
    "Granted",
    "HeadDropped",
    "Left",
    "LockEnded",
    "OrderChanged",
    "Outlier",
    "Peer",
    "ReapResumed",
    "ReapSkipped",
    "Reaped",
    "Released",
    "Reprioritized",
    "SessionEnded",
    "SessionRun",
    "Subject",
    "Swept",
    "Trend",
    "WaitingLong",
    "YieldAsked",
    "Yielded",
    "actor",
    "clean",
    "command",
    "emit",
    "holder",
    "host",
    "line",
    "moment",
    "path",
    "record",
    "request",
    "rotate",
]

VERSION = _common_events.SCHEMA_VERSION
EVENTS_MAX = _common_events.ROTATE_AT  # bytes of events.jsonl before it becomes events.jsonl.1
LINE_MAX = _common_events.MAX_LINE  # bytes of one line with its newline
COMMAND_MAX = _common_events.MAX_COMMAND  # characters of a command line kept
LOCK_WAIT = _common_events.LOCK_WAIT


def path(directory: Path) -> Path:
    return directory / "events.jsonl"


def host() -> str:
    """The machine's short host name, lowercase."""
    return _common_events.host_name()


def moment() -> str:
    """Now, RFC 3339 UTC with milliseconds."""
    return _common_events.timestamp()


def clean(value: Any) -> Any:
    """Strings without control/separator characters, capped and bounded via `fathers_common.events.clean`."""
    return _common_events.clean(value)


def command(text: Any) -> str:
    return _common_events.command(str(text or ""))


def request(entry: Mapping[str, Any], entry_path: Path | None = None) -> Subject:
    """The subject for a queue entry: its id (the entry's file name, as `status --json` gives it), label and pid."""
    subject: Subject = {"type": "request",
                               "id": entry_path.stem if entry_path is not None else f"pid-{entry.get('pid')}"}
    if entry.get("label") is not None:
        subject["label"] = str(entry.get("label"))
    if entry_path is not None or isinstance(entry.get("lease_id"), str):
        # unique (fathers#101), as status --json gives it; `id` stays the entry's name
        subject["lease_id"] = _entry.lease_id(entry_path.stem if entry_path is not None else "", entry)
    if isinstance(entry.get("pid"), int) and entry["pid"] > 0:
        subject["pid"] = entry["pid"]
    return subject


def actor(pid: int | None = None, lane: str | None = None, kind: str = "process") -> Actor:
    """Who caused it: a process (this one unless `pid`), with its lane where it names one, and `via: godfather` when
    this process is a command Godfather ran (GODFATHER_ACTOR); never for another process, such as a holder whose
    grant this command's look made."""
    own = pid is None or pid == os.getpid()
    found: Actor = {"type": kind, "pid": pid if pid is not None else os.getpid()}
    if kind == "tool":
        found["name"] = "slotfather"
    if lane:
        found["lane"] = lane
    if own and (os.environ.get("GODFATHER_ACTOR") or "").startswith("godfather"):
        found["via"] = "godfather"
    return found


def holder(entry: Mapping[str, Any]) -> Actor:
    """The actor of what a lease's own client does: its holder process and lane."""
    pid = entry.get("pid") if isinstance(entry.get("pid"), int) and entry["pid"] > 0 else None
    return actor(pid, _entry.meta_of(entry).get("lane"))


def line(kind: str, subject: Mapping[str, Any], details: Mapping[str, Any], who: Mapping[str, Any],
         level: str | None = None) -> bytes:
    built = _common_events.build_event("slotfather", kind, subject, who, details, level=level)
    built["time"] = moment()
    built["host"] = host()
    return _common_events.encode_event(built, compact=False)


@overload
def record(directory: Path, log: str, kind: None = None, subject: None = None, details: None = None,
           who: None = None, level: None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["lease.granted"], subject: Mapping[str, Any],
           details: Granted, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["lease.released"], subject: Mapping[str, Any],
           details: Released, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["request.left"], subject: Mapping[str, Any],
           details: Left, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["lease.reaped", "request.reaped"], subject: Mapping[str, Any],
           details: Reaped, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["request.reprioritized"], subject: Mapping[str, Any],
           details: Reprioritized, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["request.waiting_long"], subject: Mapping[str, Any],
           details: WaitingLong, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["request.head_dropped"], subject: Mapping[str, Any],
           details: HeadDropped, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["lease.yield_asked"], subject: Mapping[str, Any],
           details: YieldAsked, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["lease.yield_withdrawn"], subject: Mapping[str, Any],
           details: Empty, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["lease.yielded"], subject: Mapping[str, Any],
           details: Yielded, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["lease.lock_ended"], subject: Mapping[str, Any],
           details: LockEnded, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["capacity.changed"], subject: Mapping[str, Any],
           details: CapacityChanged, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["order.changed"], subject: Mapping[str, Any],
           details: OrderChanged, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["process.swept"], subject: Mapping[str, Any],
           details: Swept, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["session.run_started", "session.run_ended"], subject: Mapping[str, Any],
           details: SessionRun, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["session.ended"], subject: Mapping[str, Any],
           details: SessionEnded, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["run.outlier"], subject: Mapping[str, Any],
           details: Outlier, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["run.trend"], subject: Mapping[str, Any],
           details: Trend, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["reap.skipped"], subject: Mapping[str, Any],
           details: ReapSkipped, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


@overload
def record(directory: Path, log: str | None, kind: Literal["reap.resumed"], subject: Mapping[str, Any],
           details: ReapResumed, who: Mapping[str, Any] | None = None, level: str | None = None) -> None: ...


def record(directory: Path, log: str | None, kind: str | None = None, subject: Mapping[str, Any] | None = None,
           details: Mapping[str, Any] | None = None, who: Mapping[str, Any] | None = None,
           level: str | None = None) -> None:
    """The one writer of what a queue transaction did (fathers#37), after it did it: its queue.log line `log`
    (typed per kind by the overloads above: each kind's `details` is its TypedDict, so a malformed event fails the
    type check),
    (state.log(); callers hold the queue lock), then its event of `kind` (one of KINDS, `details` of that kind's
    shape) about `subject`, by `who` (this process unless given). Either may be None: a line no event mirrors (a
    long wait's), an event with no line (a session's, a settings change's). The event never fails the change."""
    if log is not None:
        from .state import log as queue_log
        queue_log(directory, log)
    if kind is not None:
        emit(directory, kind, subject or {}, details, who, level)


def emit(directory: Path, kind: str, subject: Mapping[str, Any], details: Mapping[str, Any] | None = None,
         who: Mapping[str, Any] | None = None, level: str | None = None) -> None:
    """Appends one event (record()'s second sink); never raises. Nothing is written in another user's queue."""
    with contextlib.suppress(Exception):
        from .state import foreign_owner
        if foreign_owner(directory) is not None:
            return
        data = line(kind, subject, details or {}, who or actor(), level)
        _common_events.append_event(path(directory), data, rotate_at=EVENTS_MAX, lock_wait=LOCK_WAIT)


def rotate(target: Path) -> None:
    with open(target.with_name("events.lock"), "a", encoding="utf-8") as lock:
        if _common_events.hold_lock(lock, LOCK_WAIT) and target.exists() and target.stat().st_size > EVENTS_MAX:
            target.replace(target.with_name(target.name + ".1"))
