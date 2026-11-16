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
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import socket
from typing import Any, Literal, Mapping, Optional, TypedDict, Union, cast, overload

from . import entry as _entry

VERSION = 1
EVENTS_MAX = 4 << 20  # bytes of events.jsonl before it becomes events.jsonl.1
LINE_MAX = 16 * 1024  # bytes of one line with its newline
COMMAND_MAX = 400  # characters of a command line kept
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")
class Subject(TypedDict, total=False):
    """What an event is about: `type` and `id` always (a request's id is its entry's name), the rest where known."""
    type: str
    id: str
    label: str
    lease_id: str
    pid: int


class Actor(TypedDict, total=False):
    """Who caused it: `type` always (process, tool), `pid`, and `name`, `lane` and `via` where they apply."""
    type: str
    pid: int
    name: str
    lane: str
    via: str


class Peer(TypedDict, total=False):
    """Another request an event names: the waiter a yield is for, or the lock a lease yields to wait for."""
    id: Optional[str]
    label: Optional[str]
    priority: str


class Granted(TypedDict, total=False):
    count: int
    priority: str
    waited: Optional[int]
    served: str
    resumed: bool
    meta: dict[str, str]
    session: Optional[str]


class Released(TypedDict, total=False):
    count: int
    exit: Optional[int]
    ran: Optional[float]
    cpu: Optional[float]
    rss_peak_mb: Optional[float]
    unknown: bool


class Left(TypedDict):
    count: int
    gave_up_after: Optional[float]


class Reaped(TypedDict):
    count: Optional[int]
    ran: Optional[float]
    waited: Optional[float]
    reason: str


Reprioritized = TypedDict("Reprioritized", {"from": str, "to": str, "state": Optional[str]})  # `from`: a keyword


class WaitingLong(TypedDict, total=False):
    waited: int
    est_start: Optional[str]
    behind: dict[str, int]
    priority: str
    aged: bool


YieldAsked = TypedDict("YieldAsked", {"count": int, "for": Peer, "by_hand": bool}, total=False)  # `for`: a keyword


class Yielded(TypedDict, total=False):
    count: int
    to: Peer
    lock: str  # a lease that yields while its command waits on a lock (fathers#159)
    yields: int


class Blocker(TypedDict):
    id: Optional[str]
    label: Optional[str]
    pid: Optional[int]


class HeadDropped(TypedDict):
    """request.head_dropped (fathers#160): a head gave way, `late` seconds past its reserved start, to the leases
    with no end estimate that hold its slots."""
    count: int
    late: int
    blocked_by: list[Blocker]


class LockEnded(TypedDict):
    """lease.lock_ended (fathers#159): a lease's wait for a lock is over: "took", "stopped", "resumed" (work went on
    under the lease) or "gone" (its waiter died); `yielded`: it had given up its slots; `waited`: seconds, if so."""
    lock: str
    why: str
    yielded: bool
    waited: Optional[int]


class Empty(TypedDict):
    """An event whose subject says it all (lease.yield_withdrawn)."""


class CapacityChanged(TypedDict):
    saved: Any  # what capacity.json now holds, or None after a reset
    mode: str
    slots: int


class OrderChanged(TypedDict, total=False):
    saved: Any  # what order.json now holds
    reset: bool


Swept = TypedDict("Swept", {"action": str, "class": str, "kind": str, "age": int, "processes": int, "cmd": str})


class SessionRun(TypedDict, total=False):
    session: str
    run: Any


class SessionEnded(TypedDict):
    reason: str
    runs: int
    busy: Any
    held: int


class Outlier(TypedDict):
    lease_id: Optional[str]
    ran: float
    p50: float
    p90: float
    ended: Optional[str]
    later: list[float]


class Trend(TypedDict, total=False):
    verdict: str
    p50_recent: Optional[float]
    p50_before: Optional[float]
    ratio: Optional[float]
    recent: int
    before: int
    plan_change: Any
    why: Optional[str]


class ReapSkipped(TypedDict):
    reason: str


class ReapResumed(TypedDict):
    after: str


Details = Union[Granted, Released, Left, Reaped, Reprioritized, WaitingLong, YieldAsked, Yielded, Empty,
                CapacityChanged, OrderChanged, Swept, SessionRun, SessionEnded, Outlier, Trend, ReapSkipped,
                ReapResumed, HeadDropped, LockEnded]

# The event-kind table: each kind this writer emits, with the shape of its details (godfather/docs/events.md).
KINDS: dict[str, type] = {
    "lease.granted": Granted, "lease.released": Released, "request.left": Left, "lease.reaped": Reaped,
    "request.reaped": Reaped, "request.reprioritized": Reprioritized, "request.waiting_long": WaitingLong,
    "lease.yield_asked": YieldAsked, "lease.yield_withdrawn": Empty, "lease.yielded": Yielded,
    "lease.lock_ended": LockEnded,
    "capacity.changed": CapacityChanged, "order.changed": OrderChanged, "process.swept": Swept,
    "session.run_started": SessionRun, "session.run_ended": SessionRun, "session.ended": SessionEnded,
    "request.head_dropped": HeadDropped, "run.outlier": Outlier, "run.trend": Trend, "reap.skipped": ReapSkipped, "reap.resumed": ReapResumed,
}


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
