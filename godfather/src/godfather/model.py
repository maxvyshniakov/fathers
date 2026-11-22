"""The snapshot model (fathers#142): what each adapter builds from its tool's snapshot, typed. An adapter checks a
snapshot once, when it decodes it: unknown keys are ignored, a missing optional one takes its documented default, and
a missing required one (`missing()`) is the source's notice, as an unknown version is, never a KeyError at draw time.
Past the adapter the screen reads these shapes and nothing else of the tools' JSON.

Python 3.9 has no NotRequired: a shape's optional keys are a `total=False` subclass of its required ones."""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, TypedDict, Union

Moment = Optional[datetime.datetime]
TreeRow = Tuple[int, float, int, str]  # pid, cpu %, rss KB, name
PsRow = Tuple[int, int, float, int, str]  # pid, parent pid, cpu %, rss KB, name: a row of `ps` (fathers#203)


def missing(raw: Mapping[str, Any], required: Tuple[str, ...]) -> Optional[str]:
    """The first of `required` that `raw` lacks or holds as null, None when it has them all."""
    return next((key for key in required if raw.get(key) is None), None)


# Slotfather: `status --json --full` (docs/contracts/slotfather.md)

class Expected(TypedDict):
    """A run type's expected run time (fathers#106)."""
    key: str
    p50: float
    p90: float
    runs: int
    fallback: bool


class _Request(TypedDict):
    key: Any  # the lease id, else (pid, since) from an older Slotfather: the row's identity
    kind: str  # held · legacy · waiting
    count: int
    pid: int
    procs: int
    yields: int
    cpu: Optional[float]
    rss: int
    tree: List[TreeRow]
    queue: str
    label: str
    cmd: str
    meta: str
    workdir: str
    asked: str
    context: Dict[str, Any]
    priority: str
    since: Moment
    granted: Moment
    asked_at: Moment
    idle_since: Moment
    est_start: Moment
    lease_id: Optional[str]
    expected: Optional[Expected]
    overrun: bool
    start: Optional[str]
    style: str  # the holder colour, the app's: `legacy` and `text` until it assigns one


class Request(_Request, total=False):
    """A QUEUE row: a running lease or a waiting request."""
    ticket: Optional[str]
    lock: str  # the lock this lease waits on, if any
    served: Optional[str]
    reprioritized: Optional[str]
    yieldable: bool
    passable: bool
    all: bool
    lane: Any  # the lane summary DETAILS fetched for it
    uid: int  # the holder's uid, where Slotfather gives it


# The keys a request must carry in the snapshot; the others have defaults (docs/contracts/slotfather.md, item)
REQUEST_REQUIRED = ("kind", "count", "pid", "procs", "yields", "rss")

HistoryStep = Tuple[datetime.datetime, datetime.datetime, int]  # held slots from start to end


class _Dashboard(TypedDict):
    v: int
    directory: str
    slots: int
    cores: int
    saved: bool
    basis: Dict[str, Any]
    averages: Tuple[float, ...]
    draining: int
    advice: Dict[str, Any]
    holders: List[Request]
    waiters: List[Request]
    used: int
    free: int
    memory_block: Optional[str]
    admission: Optional[int]
    order: str
    start: Moment
    reserved: Moment
    limit: float
    memory: Dict[str, Any]
    history: List[HistoryStep]
    events: List[Dict[str, Any]]
    updated: datetime.datetime
    overtake_window: float
    host: Optional[str]


class Dashboard(_Dashboard, total=False):
    """The overview's model: Slotfather's dashboard block decoded, plus what the app adds each load."""
    installed: Any
    sources: List[Tuple[str, str]]
    events_skipped: Dict[str, Dict[str, int]]  # lines skipped per source and why
    fresh_sources: List[str]
    uid: int
    actions: List[str]
    events_note: str
    stale_since: datetime.datetime
    history_window: float  # the chart's span, set only by the demo scene and its goldens
    unleased_top: List[TreeRow]  # MACHINE's top processes outside leases while unleased load is high (fathers#203)
    # fathers#206: why CPU load or the expected-peak memory budget holds new leases beside running ones; the adapter
    # always sets them (None from an older Slotfather), models drawn by tests and the demo may leave them out
    load_block: Optional[str]
    memory_budget_block: Optional[str]


DASHBOARD_REQUIRED = ("slots", "cores", "used", "free", "advice", "memory", "overtake_window")
ADVICE_REQUIRED = ("unleased_load",)  # what MACHINE's border reads of `advice`


# Lanefather: `lanefather status --json` (docs/contracts/lanefather.md); a missing field reads as empty

LaneKey = Tuple[str, str]  # (the project's state directory, the lane id)


class Ticket(TypedDict):
    number: Optional[str]
    url: Optional[str]


class Receipt(TypedDict):
    status: str
    tree_match: Optional[bool]
    path: Optional[str]


class Review(TypedDict):
    status: str
    open: int
    findings: int


class Lane(TypedDict):
    id: str
    key: LaneKey
    project: str
    state_dir: str
    repository: Optional[str]
    status: str
    ticket: Ticket
    branch: Optional[str]
    base: Optional[str]
    worktree: Optional[str]
    claims: List[str]
    after: List[str]
    created: Moment
    receipt: Receipt
    post_merge: Optional[str]
    review: Review
    logs: Dict[str, str]
    outcome: Any


class Gate(TypedDict):
    status: Optional[str]
    sha: Optional[str]
    at: Moment


class _Project(TypedDict):
    project: str
    state_dir: str
    repository: Optional[str]
    lanes: List[Lane]


class Project(_Project, total=False):
    """A registered project: its lanes, or the error that kept them from being read."""
    error: str
    cap: Optional[int]
    active: int
    planned: int
    gate: Optional[Gate]


class Lanes(TypedDict):
    time: Moment
    projects: List[Project]


class LaneRow(Lane):
    """A lane as a LANES row."""
    kind: Literal["lane"]


class ProjectRow(TypedDict):
    """A project as LANES' unselectable heading row."""
    kind: Literal["project"]
    project: str
    state_dir: str
    error: Optional[str]
    active: int
    cap: Optional[int]
    count: int


LaneTableRow = Union[ProjectRow, LaneRow]


# Junkfather: `junkfather status --json` (docs/contracts/junkfather.md); a missing field reads as empty

class Resource(TypedDict):
    kind: Optional[str]
    id: Optional[str]
    name: Optional[str]


class Entry(TypedDict):
    """An owned entry."""
    id: str
    key: Tuple[str, str]  # ("entry", id)
    kind: Literal["entry"]
    phase: str
    owner: str
    session: Optional[str]
    agent: Optional[str]
    lane: Optional[str]
    owner_alive: Optional[bool]
    age: float
    ttl: Optional[float]
    ttl_left: float
    keep: Optional[str]
    pid: Optional[int]
    pgid: Optional[int]
    processes: Optional[int]
    label: Optional[str]
    mode: Optional[str]
    cwd: Optional[str]
    argv: List[str]
    log: Optional[str]
    resource: Optional[Resource]
    state: str  # running · orphan · expired


class Unowned(TypedDict):
    """A process the last sweep found that no entry owns."""
    kind: Literal["unowned"]
    key: Tuple[str, int, str]  # ("pid", pid, start)
    pid: int
    start: str
    cmd: str
    cwd: Optional[str]
    root: Optional[str]
    why: str
    age: Optional[float]
    alive: Optional[bool]


class UnownedResource(TypedDict):
    """A resource the last sweep found that no entry owns (a worktree, a browser session)."""
    kind: Literal["resource"]
    key: Tuple[str, str, str]  # ("resource", kind, id)
    resource_kind: str
    id: str
    name: Optional[str]
    why: str
    branch: Optional[str]
    repo: Optional[str]
    age: Optional[float]
    size: Optional[float]


class Separator(TypedDict):
    """PROCESSES' unselectable heading row for a sweep section."""
    kind: Literal["separator"]
    label: str


ProcessRow = Union[Entry, Unowned, UnownedResource]
ProcessTableRow = Union[Entry, Separator, Unowned, UnownedResource]


class Sweep(TypedDict):
    time: Optional[str]
    result: Any


class Processes(TypedDict):
    time: Moment
    entries: List[Entry]
    unowned: List[Unowned]
    unowned_resources: List[UnownedResource]
    sweep: Sweep
    errors: List[str]
