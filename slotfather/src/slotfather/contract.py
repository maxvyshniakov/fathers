"""TypedDict contracts for Slotfather's persisted entries, JSON status snapshots, dashboard block, and events.jsonl.

The entry contract lives in `entry.py` (re-exported here as `Entry`); `StatusSnapshot`, `StatusEntry`, `FullSnapshot`,
`DashboardBlock`, `DashboardRow`, `DashboardUnknown`, `DashboardEvent`, and `Event` (with per-kind `Details` in `KINDS`)
define the JSON wire shapes documented in `docs/api.md` and guarded by `tests/test_contract.py` (fathers#125).
"""
from __future__ import annotations

from typing import Any, Optional, TypedDict, Union

from .entry import Entry

__all__ = [
    "KINDS",
    "Actor",
    "Blocker",
    "CapacityChanged",
    "DashboardBlock",
    "DashboardEvent",
    "DashboardRow",
    "DashboardUnknown",
    "Details",
    "Empty",
    "Entry",
    "Event",
    "FullSnapshot",
    "Granted",
    "HeadDropped",
    "Left",
    "LegacyEntry",
    "LockEnded",
    "OrderChanged",
    "Outlier",
    "Peer",
    "PidQuery",
    "ReapResumed",
    "ReapSkipped",
    "Reaped",
    "Released",
    "Reprioritized",
    "SessionEnded",
    "SessionRun",
    "StatusEntry",
    "StatusSnapshot",
    "Subject",
    "Swept",
    "Trend",
    "TruncatedDetails",
    "WaitingLong",
    "YieldAsked",
    "Yielded",
]


class PidQuery(TypedDict):
    """`status --json --pid PID`'s `query` object: the queried PID and its live `ps -o lstart=` (None if gone)."""
    pid: int
    start: Optional[str]


class _LegacyEntryBase(TypedDict):
    id: str
    lease: str
    state: str
    count: int
    label: Optional[str]
    pid: Optional[int]
    pgid: Optional[int]
    tree: list[int]


class LegacyEntry(_LegacyEntryBase, total=False):
    """An active lease in an earlier queue directory during migration (`status --json` `legacy`)."""
    via: str


class _StatusEntryBase(TypedDict):
    id: str
    lease_id: str
    lease: str
    state: Any
    phase: str
    count: Any
    label: Any
    pid: Any
    pgid: Optional[int]
    start: Any
    since: Any
    granted: Any
    cmd: Any
    cwd: Any
    meta: dict[str, str]
    tree: list[int]
    priority: str
    ticket: Optional[str]
    yieldable: bool
    yields: int
    yield_asked: Optional[dict[str, Any]]
    position: Optional[int]
    idle_since: Optional[float]
    effective_priority: str
    aged: bool
    reprioritized: Optional[str]
    est_start: Optional[str]
    head: bool
    reserved: Optional[str]
    waited: Optional[int]
    long_wait: bool
    memory_wait: Optional[str]
    load_wait: Optional[str]


class StatusEntry(_StatusEntryBase, total=False):
    """One item in `status --json`'s `entries` list (known holder, waiter, or unknown entry)."""
    min_count: int
    max_count: int
    lock: str
    session: dict[str, Any]
    expected: Optional[dict[str, Any]]
    overrun: bool
    est_memory_mb: float
    est_memory_source: str
    blocked_by: list[dict[str, Any]]
    idle_slots: int
    gave_way: list[dict[str, Any]]
    unknown: Optional[str]
    holds: int
    warning: str
    via: str


class _StatusSnapshotBase(TypedDict):
    v: int
    slots: int
    held: int
    free: int
    draining: int
    entries: list[StatusEntry]
    legacy: list[LegacyEntry]


class StatusSnapshot(_StatusSnapshotBase, total=False):
    """`slotfather status --json` top-level response (version 2)."""
    query: PidQuery
    lease_unknown: list[int]


class DashboardRow(TypedDict):
    """One row of `holders` or `waiters` in `status --json --full`'s `dashboard` block."""
    tree: list[list[Any]]
    cpu: float
    rss: int
    procs: int
    key: list[Any]
    kind: str
    count: int
    queue: str
    label: str
    pid: Optional[int]
    cmd: str
    since: Optional[str]
    meta: str
    context: dict[str, str]
    workdir: str
    priority: str
    ticket: Optional[str]
    yieldable: bool
    yields: int
    asked: str
    asked_at: Optional[str]
    idle_since: Optional[str]
    served: Optional[str]
    reprioritized: Optional[str]
    est_start: Optional[str]
    granted: Optional[str]
    passable: bool
    all: bool
    est_memory_mb: float
    est_memory_source: str
    lock: str


class _DashboardEventBase(TypedDict):
    when: str
    verb: str
    count: int
    label: str
    pid: int
    delta: int
    detail: str


class DashboardEvent(_DashboardEventBase, total=False):
    """One recent event in `status --json --full`'s `dashboard.events` list."""
    state: str
    unsure: bool


class DashboardUnknown(TypedDict):
    """One unknown entry summary in `status --json --full`'s `dashboard.unknown` list."""
    id: str
    lease: str
    label: str
    pid: Any
    state: Any
    count: Any
    reason: Optional[str]
    holds: int
    warning: str


class DashboardBlock(TypedDict):
    """`status --json --full`'s `dashboard` block (version 1)."""
    v: int
    directory: str
    slots: int
    saved: bool
    basis: dict[str, Any]
    cores: int
    averages: list[float]
    draining: int
    advice: dict[str, Any]
    holders: list[DashboardRow]
    waiters: list[DashboardRow]
    used: int
    free: int
    memory_block: Optional[str]
    load_block: Optional[str]
    memory_budget_block: Optional[str]
    memory_budget_mb: Optional[float]
    memory_expected_mb: float
    admission: int
    order: str
    start: Optional[str]
    reserved: Optional[str]
    limit: float
    memory: dict[str, Any]
    history: list[list[Any]]
    events: list[DashboardEvent]
    updated: str
    unknown: list[DashboardUnknown]
    overtake_window: float


class FullSnapshot(StatusSnapshot):
    """`slotfather status --json --full` top-level response."""
    host: str
    time: str
    dashboard: DashboardBlock


# events.jsonl contracts (Godfather shared event schema v1)


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
    min_count: int
    max_count: int
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


class Left(TypedDict, total=False):
    count: int
    gave_up_after: Optional[float]
    reason: str


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


class TruncatedDetails(TypedDict):
    truncated: bool


Details = Union[Granted, Released, Left, Reaped, Reprioritized, WaitingLong, YieldAsked, Yielded, Empty,
                CapacityChanged, OrderChanged, Swept, SessionRun, SessionEnded, Outlier, Trend, ReapSkipped,
                ReapResumed, HeadDropped, LockEnded]


class _EventBase(TypedDict):
    v: int
    time: str
    host: str
    source: str
    kind: str
    subject: Subject
    actor: Actor
    details: Details | TruncatedDetails | dict[str, Any]


class Event(_EventBase, total=False):
    """One JSON line in `events.jsonl` (Godfather shared event schema version 1)."""
    level: str


# The event-kind table: each kind this writer emits, with the shape of its details (godfather/docs/events.md).
KINDS: dict[str, type] = {
    "lease.granted": Granted, "lease.released": Released, "request.left": Left, "lease.reaped": Reaped,
    "request.reaped": Reaped, "request.reprioritized": Reprioritized, "request.waiting_long": WaitingLong,
    "lease.yield_asked": YieldAsked, "lease.yield_withdrawn": Empty, "lease.yielded": Yielded,
    "lease.lock_ended": LockEnded,
    "capacity.changed": CapacityChanged, "order.changed": OrderChanged, "process.swept": Swept,
    "session.run_started": SessionRun, "session.run_ended": SessionRun, "session.ended": SessionEnded,
    "request.head_dropped": HeadDropped, "run.outlier": Outlier, "run.trend": Trend,
    "reap.skipped": ReapSkipped, "reap.resumed": ReapResumed,
}
