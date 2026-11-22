"""A queue entry (`queue/*.json`): its persisted fields, the accessors that read them whatever wrote the file, its
phase, and the one definition of each transition a request goes through.

Every function here is pure apart from the dict it is given: callers read and write the files under the queue lock
(state, leases) and log what happened (queue.log, events). The on-disk format is the one older clients write and
read (docs/architecture.md, "State on disk"); a field added here must be one they read past."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import math
import time
from typing import Any, Iterable, Mapping, NamedTuple, TypedDict, cast

from . import config as _config


class Entry(TypedDict, total=False):
    """The fields a client writes on an entry. `pid`, `start`, `count`, `since` and `state` identify and place it;
    the rest are optional, and every client reads past the fields it does not know."""
    pid: int
    start: str  # the holder's start as `ps -o lstart` prints it for its writer
    started: int  # the holder's start in epoch seconds (fathers#122)
    count: int
    min_count: int  # `-n MIN-MAX`: minimum slots requested while waiting (fathers#128)
    max_count: int  # `-n MIN-MAX`: maximum slots granted if free at grant time (fathers#128)
    label: str
    cmd: str
    cwd: str
    since: str  # its arrival, a config.now() stamp
    state: str  # "waiting" or "held"
    priority: str
    lease_id: str  # unique across queues and restarts (fathers#101)
    ticket: str
    all: bool  # `-n all`: the capacity at the grant
    meta: dict[str, str]
    label_given: str
    label_derived: str
    project: str
    session: dict[str, Any]
    git: dict[str, Any]
    yieldable: bool
    until: str  # when its client gives up waiting (run --wait)
    yield_after: float  # its client's own SLOTFATHER_YIELD_AFTER where it differs from the queue's
    granted: str  # its first grant; a resumed lease keeps it
    pressed: bool  # granted while memory was not normal (PRESSED)
    aged: str  # the class ageing moved it to by its grant
    reserved: float  # the start the head is promised
    yields: int
    yielded_at: float  # when it last went back in the queue
    first_granted: str  # its first grant, while it waits after a yield
    rank: float  # where a yielded lease queues: just behind the waiter it yielded to
    yielded: float  # seconds it spent back in the queue
    resumed: str  # its last grant after a yield
    reprioritized: str  # the class it asked first
    reprioritized_at: float


Record = dict[str, Any]  # an entry as read from its file: any JSON object, whoever wrote it


WAITING, HELD = "waiting", "held"  # the states on disk

PRESSED = "pressed"  # on an entry granted while memory was not normal (memory_level()); read under warning


# Phases: what an entry is to a reader, worked out from its state and what lies beside it.
YIELDED = "yielded"  # waiting again after a yield
ASKED = "asked"  # held, with a yield request standing
UNKNOWN = "unknown"  # an entry this client does not know (unknown()): left alone

NEEDED = (("pid", int), ("start", str), ("count", int), ("since", str))  # what the scheduler reads of every entry
POSITIVE = ("pid", "count")  # of those, the ones that must be above zero
IDENTITY = NEEDED[:3]  # what a process query reads: no arrival


class Phase(NamedTuple):
    name: str  # WAITING, YIELDED, HELD, ASKED or UNKNOWN
    idle: bool  # a held lease below the idle threshold for the idle duration (idle.idle_since())


def unknown(entry: Mapping[str, Any], needed: tuple[tuple[str, type], ...] = NEEDED) -> str | None:
    """Why this client does not know an entry, or None when it does (fathers#19): a state other than waiting and
    held (a newer client's, perhaps), no state, or a field the scheduler needs (NEEDED) missing or of another type.
    Such an entry may be a live lease of a newer client: it is never granted, reaped, ranked or deleted
    automatically; while its holder is alive its count is reserved (reserved()), and `status` shows it. The one rule
    of what a known entry is: procs.lease_entries() and the scheduler read it alike (valid_identity())."""
    if "state" not in entry:
        return "no state"
    state = entry.get("state")
    if state not in (WAITING, HELD):
        return f"state {state!r}" if isinstance(state, str) else "a state that is not text"
    for field, kind in needed:
        value = entry.get(field)
        if type(value) is not kind:
            return f"no {field}" if value is None else f"a {field} that is not {kind.__name__}"
        if field in POSITIVE and isinstance(value, int) and value <= 0:
            return f"a {field} that is not positive"
    return None


def checkable(entry: Mapping[str, Any]) -> bool:
    """Whether an entry names a holder whose liveness can be checked: a positive int pid and a start (platform.alive()
    then decides). An unknown entry without them reserves nothing and is never provably dead."""
    pid = entry.get("pid")
    return type(pid) is int and pid > 0 and isinstance(entry.get("start"), str)


def reserved(entry: Mapping[str, Any]) -> int:
    """The slots an unknown entry reserves while its holder is alive (fathers#19 review): its count, a positive int;
    0 where it has none or its holder cannot be checked (checkable())."""
    count = entry.get("count")
    return count if type(count) is int and count > 0 and checkable(entry) else 0


def yielded_of(entry: Mapping[str, Any]) -> float:
    """The seconds a lease has spent back in the queue (`yielded`); 0 where it has none, or a value that is not a
    finite number, as a foreign writer could leave."""
    value: object = entry.get("yielded")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return 0.0


def is_held(entry: Mapping[str, Any]) -> bool:
    """Whether the entry holds its slots: a known entry (unknown()) whose state is held."""
    return entry.get("state") == HELD and unknown(entry) is None


def is_waiting(entry: Mapping[str, Any]) -> bool:
    """Whether the entry waits for its slots: a known entry whose state is waiting (after a yield too)."""
    return entry.get("state") == WAITING and unknown(entry) is None


def phase(entry: Mapping[str, Any], asked: bool = False, idle_since: float | None = None) -> Phase:
    """The entry's phase: held (asked, when a yield request stands beside it), waiting (yielded, when it is back
    in the queue after a yield), or unknown (unknown()); idle for a held lease idle since `idle_since`."""
    if is_held(entry):
        return Phase(ASKED if asked else HELD, idle_since is not None)
    if is_waiting(entry):
        return Phase(YIELDED if "yielded_at" in entry else WAITING, False)
    return Phase(UNKNOWN, False)


def split(entries: Iterable[Record]) -> tuple[list[Record], list[Record]]:
    """(held, waiting) of `entries`, each in their order; an unknown entry is in neither."""
    held: list[Record] = []
    waiting: list[Record] = []
    for entry in entries:
        if is_held(entry):
            held.append(entry)
        elif is_waiting(entry):
            waiting.append(entry)
    return held, waiting


def valid_identity(entry: object) -> bool:
    """Whether a raw entry names its holder as process queries need it: known by the same rule as the scheduler's
    (unknown(), without the arrival: a positive pid and count, a known state) with a start that is not empty (procs.lease_entries(); install.py keeps its own copy
    of the identity part)."""
    if not isinstance(entry, dict):
        return False
    found = cast(Record, entry)
    start = found.get("start")
    return unknown(found, IDENTITY) is None and isinstance(start, str) and bool(start.strip())


# Accessors: each field read as every writer may have left it.

def epoch(value: Any) -> float | None:
    """A moment stored as epoch seconds or as a now() stamp, as epoch seconds; None for anything else."""
    return float(value) if type(value) in (int, float) and math.isfinite(value) else _config.seconds(value)


def lease_id(stem: str, entry: Mapping[str, object]) -> str:
    """An entry's unique id (fathers#101): the one its client wrote, else, for an entry of an older client, one made
    of its name, its holder's start time and its arrival, the same from one look to the next."""
    found = entry.get("lease_id")
    if isinstance(found, str) and found:
        return found
    return "old-" + hashlib.sha256(f"{stem}|{entry.get('start')}|{entry.get('since')}".encode()).hexdigest()[:24]


def checked_meta(meta: object) -> dict[str, str]:
    """Lease metadata, validated: raises SlotError for a key that is not a short lowercase word, a value that is
    not short printable text, or too many keys."""
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        raise _config.SlotError("meta must be a mapping of key to value")
    given = cast(dict[object, object], meta)
    if len(given) > _config.META_KEYS_MAX:
        raise _config.SlotError(f"at most {_config.META_KEYS_MAX} --meta keys")
    found: dict[str, str] = {}
    for key, value in given.items():
        if not isinstance(key, str) or not _config.META_KEY.fullmatch(key):
            raise _config.SlotError(f"--meta key {key!r} must be a lowercase word (a-z, 0-9, _ . -), at most 32 "
                                    f"characters")
        if not isinstance(value, str) or len(value) > _config.META_VALUE_MAX or not value.isprintable():
            raise _config.SlotError(f"--meta {key} takes printable text of at most {_config.META_VALUE_MAX} "
                                    f"characters")
        if key in ("class", "priority", "ticket"):
            option = "priority" if key == "class" else key
            raise _config.SlotError(f"{key} is a request field; use --{option}, not --meta")
        found[key] = value
    return found


def parse_meta(pairs: list[str]) -> dict[str, str]:
    """`key=value` words as lease metadata; a later key replaces an earlier one."""
    found: dict[str, str] = {}
    for pair in pairs:
        key, equals, value = pair.partition("=")
        if not equals:
            raise _config.SlotError(f"--meta takes key=value, not {pair!r}")
        if key == "cache" and value not in _config.CACHE_VALUES:  # the command line only: library callers keep any
            values = _config.CACHE_VALUES
            raise _config.SlotError(f"--meta cache takes {', '.join(values[:-1])} or {values[-1]}, not {value!r}")
        found[key] = value
    return checked_meta(found)


def meta_of(entry: Mapping[str, object]) -> dict[str, str]:
    """An entry's or history line's metadata: {} where it has none (an older client) or it is not a mapping of
    text, as a foreign writer could leave."""
    meta = entry.get("meta")
    if not isinstance(meta, dict):
        return {}
    return {k: str(v) for k, v in cast(dict[object, object], meta).items()
            if isinstance(k, str) and _config.META_KEY.fullmatch(k) and isinstance(v, (str, int, float))}


def priority_of(entry: Mapping[str, object]) -> str:
    value = entry.get("priority")
    if isinstance(value, str) and value in _config.PRIORITIES:
        return value
    return "normal"


def checked_ticket(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise _config.SlotError("ticket must be a short printable identifier")
    if len(value) > _config.META_VALUE_MAX or not value.isprintable():
        raise _config.SlotError("ticket must be short printable text")
    value = value.strip()
    if not value or value.lower() == "none":
        return None
    return value[1:] if value.startswith("#") and value[1:].isdigit() else value


def ticket_of(entry: Mapping[str, object]) -> str | None:
    value = entry.get("ticket")
    try:
        return checked_ticket(value)
    except _config.SlotError:
        return None


def waiting_since(entry: Mapping[str, Any]) -> float | None:
    """When a waiter's present wait began: when it yielded, for a lease back in the queue, else when it queued."""
    return epoch(entry.get("yielded_at")) or _config.seconds(entry.get("since"))


def ageing_since(entry: Mapping[str, Any]) -> float | None:
    """When a waiter's ageing began: when its class was last set (`slotfather reprioritize`: `reprioritized_at`),
    or it yielded, if later than when it queued."""
    yielded = None if entry.get(LOCK_YIELD) else epoch(entry.get("yielded_at"))  # a lock wait keeps its place
    found = [epoch(entry.get("reprioritized_at")), yielded, _config.seconds(entry.get("since"))]
    return max((moment for moment in found if moment is not None), default=None)


def holds(entry: Mapping[str, Any], slots: int) -> int:
    """The slots a held lease takes from a capacity of `slots`: its count, and for an `-n all` lease (its entry's
    `all`) at least the whole capacity, so that a capacity grown under it keeps it exclusive (#7)."""
    count = int(entry["count"])
    return max(count, slots) if entry.get("all") else count


def whole_machine(entry: Mapping[str, Any], slots: int) -> bool:
    """Whether a waiter asks for every slot: `-n all`, or a count of at least the capacity."""
    return bool(entry.get("all")) or entry["count"] >= slots


def pinned(entry: Mapping[str, Any]) -> float | None:
    """The rank a lease back in the queue after a yield keeps, just behind the waiter it yielded to; else None."""
    behind: object = entry.get("rank")
    if isinstance(behind, (int, float)) and not isinstance(behind, bool) and math.isfinite(behind):
        return float(behind)
    return None


def reservation(entry: Mapping[str, Any]) -> float | None:
    """The start a head has been promised (`reserved`, epoch seconds, on its entry; schedule.plan() and
    reservations()): the earliest start its looks have projected since it became the head. None when it holds
    none."""
    value: object = entry.get("reserved")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def held_for(entry: Mapping[str, Any], moment: float) -> float:
    """How long a lease has run: since its first grant, less the time it spent yielded."""
    granted = _config.seconds(entry.get("granted"))
    return 0.0 if granted is None else max(0.0, moment - granted - yielded_of(entry))


# Transitions: the explicit lifecycle state machine and declarative sidecar effects.
# Functions here are pure apart from the dicts they are given: callers commit entry and sidecar mutations under the
# queue lock (`state.commit()`, `state.remove_entry()`) and log what happened (`queue.log`, `events.jsonl`).

HEAD_DROPPED = "head_dropped"  # when a head gave way to leases with no end estimate (fathers#160; epoch seconds)
LOCK_OWNER = "lock_owner"  # {"pid", "start"} of the process that waits for the lock (fathers#159 review)
LOCK_YIELD = "lock_yield"  # a lease that yielded for a lock: its ageing counts on from before (fathers#159 review)
LOCK = "lock"  # the path a yielded lease's command waits to lock (fathers#159); granted again once it has it


class Stage(StrEnum):
    """Explicit lifecycle stage of a queue entry, lease, or measurement session."""
    UNKNOWN = "unknown"
    WAITING = "waiting"
    HELD = "held"
    UNSETTLED = "unsettled"
    YIELDED = "yielded"
    LOCK_NOTED = "lock-noted"
    LOCK_YIELDED = "lock-yielded"
    SESSION_ACTIVE = "session-active"
    SESSION_STOPPING = "session-stopping"
    SESSION_ENDED = "session-ended"
    RELEASED = "released"
    LEFT = "left"
    REAPED = "reaped"
    REFUSED = "refused"


LIVE_SESSION_STAGES: frozenset[Stage] = frozenset({
    Stage.SESSION_ACTIVE,
    Stage.SESSION_STOPPING,
    Stage.LOCK_NOTED,
    Stage.LOCK_YIELDED,
    Stage.YIELDED,
})

REPRIORITIZABLE_STAGES: frozenset[Stage] = frozenset({
    Stage.WAITING,
    Stage.HELD,
    Stage.YIELDED,
    Stage.LOCK_NOTED,
    Stage.LOCK_YIELDED,
    Stage.SESSION_ACTIVE,
    Stage.SESSION_STOPPING,
})

SETTLED_HELD_STAGES: frozenset[Stage] = frozenset({
    Stage.HELD,
    Stage.LOCK_NOTED,
    Stage.SESSION_ACTIVE,
    Stage.SESSION_STOPPING,
})

LEGAL_TRANSITIONS: dict[Stage, frozenset[Stage]] = {
    Stage.UNKNOWN: frozenset(),
    Stage.WAITING: frozenset({
        Stage.WAITING,
        Stage.HELD,
        Stage.SESSION_ACTIVE,
        Stage.SESSION_STOPPING,
        Stage.LEFT,
        Stage.REAPED,
        Stage.REFUSED,
    }),
    Stage.HELD: frozenset({
        Stage.HELD,
        Stage.YIELDED,
        Stage.LOCK_NOTED,
        Stage.LOCK_YIELDED,
        Stage.RELEASED,
        Stage.LEFT,
        Stage.REAPED,
    }),
    Stage.UNSETTLED: frozenset({
        Stage.HELD,
        Stage.SESSION_ACTIVE,
        Stage.SESSION_STOPPING,
        Stage.RELEASED,
        Stage.LEFT,
        Stage.REAPED,
    }),
    Stage.YIELDED: frozenset({
        Stage.YIELDED,
        Stage.HELD,
        Stage.SESSION_ACTIVE,
        Stage.SESSION_STOPPING,
        Stage.SESSION_ENDED,
        Stage.RELEASED,
        Stage.LEFT,
        Stage.REAPED,
    }),
    Stage.LOCK_NOTED: frozenset({
        Stage.LOCK_NOTED,
        Stage.HELD,
        Stage.SESSION_ACTIVE,
        Stage.SESSION_STOPPING,
        Stage.LOCK_YIELDED,
        Stage.SESSION_ENDED,
        Stage.RELEASED,
        Stage.LEFT,
        Stage.REAPED,
    }),
    Stage.LOCK_YIELDED: frozenset({
        Stage.LOCK_YIELDED,
        Stage.YIELDED,
        Stage.WAITING,
        Stage.SESSION_ENDED,
        Stage.RELEASED,
        Stage.LEFT,
        Stage.REAPED,
    }),
    Stage.SESSION_ACTIVE: frozenset({
        Stage.SESSION_ACTIVE,
        Stage.SESSION_STOPPING,
        Stage.SESSION_ENDED,
        Stage.LOCK_NOTED,
        Stage.LOCK_YIELDED,
        Stage.RELEASED,
        Stage.LEFT,
        Stage.REAPED,
    }),
    Stage.SESSION_STOPPING: frozenset({
        Stage.SESSION_STOPPING,
        Stage.SESSION_ENDED,
        Stage.LOCK_NOTED,
        Stage.LOCK_YIELDED,
        Stage.RELEASED,
        Stage.LEFT,
        Stage.REAPED,
    }),
    Stage.SESSION_ENDED: frozenset({
        Stage.RELEASED,
        Stage.REAPED,
    }),
    Stage.RELEASED: frozenset(),
    Stage.LEFT: frozenset(),
    Stage.REAPED: frozenset(),
    Stage.REFUSED: frozenset(),
}


class IllegalTransitionError(ValueError):
    """Raised when an entry, lock, or session transition violates `LEGAL_TRANSITIONS`."""


@dataclass(frozen=True)
class SidecarEffect:
    """Declarative sidecar mutations coupled to an entry transition, applied by `state.commit()`."""
    unlink_yield: bool = False
    unlink_idle: bool = False
    unlink_session: bool = False
    write_session: dict[str, Any] | None = None
    init_session: dict[str, Any] | None = None
    write_yield: dict[str, Any] | None = None

    @property
    def empty(self) -> bool:
        return (not self.unlink_yield and not self.unlink_idle and not self.unlink_session
                and self.write_session is None and self.init_session is None and self.write_yield is None)


NO_EFFECT = SidecarEffect()
TERMINAL_EFFECT = SidecarEffect(unlink_yield=True, unlink_idle=True, unlink_session=True)


@dataclass(frozen=True)
class SessionStep:
    """Result of one measurement-session lifecycle check (`session_step`)."""
    uses: dict[str, Any]
    effect: SidecarEffect
    changed: bool = False
    ended_by: str | None = None


def initial_session_uses(moment: float) -> dict[str, Any]:
    """Initial `.session` sidecar record for a newly granted measurement session."""
    return {"active": {}, "runs": 0, "busy": 0.0, "cpu": 0.0, "rss_peak_mb": 0.0, "last_use": round(moment, 3)}


def lock_of(entry: Mapping[str, Any]) -> str | None:
    """The lock a lease's command waits for (`slotfather lock`, fathers#159), or None. On a yielded lease the
    mark keeps it from a grant (schedule.plan()) until the lock is taken; on a held one (other work runs under
    the lease, so it keeps its slots) it only says so. An older client reads past the field: it sees a yielded
    lease and may grant it early, which `settle()` reconciles."""
    value: object = entry.get(LOCK)
    return value if isinstance(value, str) and value else None


def lock_owner(entry: Mapping[str, Any]) -> tuple[int, str] | None:
    """The process that waits for the lock (`lock_owner`: its pid and start), whose death clears the mark
    (queue.reap()); None for a mark without one, which nothing can keep."""
    owner: object = entry.get(LOCK_OWNER)
    if isinstance(owner, dict):
        data = cast(dict[str, Any], owner)
        pid, start = data.get("pid"), data.get("start")
        if type(pid) is int and pid > 0 and isinstance(start, str):
            return pid, start
    return None


def _session_spec(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    spec: object = entry.get("session")
    return cast(dict[str, Any], spec) if isinstance(spec, dict) else None


def stage_of(entry: Mapping[str, Any]) -> Stage:
    """Derive the explicit lifecycle `Stage` of a persisted queue entry."""
    if unknown(entry) is not None:
        return Stage.UNKNOWN
    spec = _session_spec(entry)
    if is_held(entry):
        if spec is not None and spec.get("ended_by"):
            return Stage.SESSION_ENDED
        if "yielded_at" in entry or bool(entry.get(LOCK_YIELD)):
            return Stage.UNSETTLED
        if lock_of(entry) is not None:
            return Stage.LOCK_NOTED
        if spec is not None:
            return Stage.SESSION_STOPPING if spec.get("stopping") else Stage.SESSION_ACTIVE
        return Stage.HELD
    if spec is not None and spec.get("ended_by"):
        return Stage.SESSION_ENDED
    if lock_of(entry) is not None:
        return Stage.LOCK_YIELDED
    if "yielded_at" in entry or bool(entry.get("yields")) or bool(entry.get(LOCK_YIELD)):
        return Stage.YIELDED
    return Stage.WAITING


def can_transition(before: Stage, after: Stage) -> bool:
    """Whether `before -> after` is a legal transition in `LEGAL_TRANSITIONS`."""
    return after in LEGAL_TRANSITIONS.get(before, frozenset())


def require_transition(before: Stage, after: Stage, *, via: str,
                       allowed: frozenset[Stage] | None = None) -> Stage:
    """Validate that `before` is allowed for `via` and `before -> after` is in `LEGAL_TRANSITIONS`."""
    if (allowed is not None and before not in allowed) or not can_transition(before, after):
        raise IllegalTransitionError(f"illegal transition in {via}: {before.value} -> {after.value}")
    return after


def new_request(pid: int, start: str, count: int, label: str, command: str, cwd: str, since: str, priority: str,
                lease_id: str, *, started: int | None = None, ticket: str | None = None, whole: bool = False,
                meta: dict[str, str] | None = None, naming: Mapping[str, str] | None = None,
                session: dict[str, Any] | None = None, git: dict[str, Any] | None = None, yieldable: bool = False,
                until: str | None = None, yield_after: float | None = None,
                max_count: int | None = None) -> Record:
    """A request as it enters the queue, waiting. Fields an older client does not know follow the ones it does,
    in the order clients have always written them."""
    entry: Entry = {"pid": pid, "start": start, "count": count, "label": label, "cmd": command, "cwd": cwd,
                    "since": since, "state": WAITING, "priority": priority, "lease_id": lease_id}
    if started is not None:
        # the start in epoch seconds, which current clients compare whatever their locale and time zone (#122);
        # older clients read past it and compare `start`, written as they write it
        entry["started"] = started
    if ticket is not None:
        entry["ticket"] = ticket
    if whole:
        entry["all"] = True  # the capacity at the grant
    if meta:
        entry["meta"] = meta
    found = cast(Record, entry)
    for key, value in (naming or {}).items():
        # label_given, label_derived, and project: the git repository (labels.py)
        found["project" if key == "project" else "label_" + key] = value
    if session is not None:
        entry["session"] = dict(session)  # to older clients it is a lease like any other
    if git:
        entry["git"] = git  # the tree it checks (fathers#97), copied to the history line
    if yieldable:
        entry["yieldable"] = True
    if until is not None:
        entry["until"] = until  # when its client gives up (plan(): such a head holds nobody back)
    if yield_after is not None:
        entry["yield_after"] = yield_after  # this client asks later or never; others' looks read it
    if max_count is not None and max_count > count:
        entry["min_count"] = count
        entry["max_count"] = max_count
    return found


def _resume_fields(entry: Record, moment: float | None = None) -> float | None:
    """Apply resume accounting for a yielded or lock-yielded entry; returns the resolved resume moment if settled."""
    away = epoch(entry.get("yielded_at"))
    back: float | None = None
    if away is not None:
        again = entry.get("granted")
        back = moment if moment is not None else _config.seconds(again) or time.time()
        entry["yielded"] = round(yielded_of(entry) + max(0.0, back - away), 2)
        entry.update(resumed=again, granted=entry.get("first_granted") or again)
    for key in ("yielded_at", "first_granted", "rank", LOCK_YIELD, LOCK, LOCK_OWNER):
        entry.pop(key, None)
    return back


def settle(entry: Record, moment: float | None = None) -> SidecarEffect:
    """Settle an older-client grant (`Stage.UNSETTLED`) into its clean held stage, preserving `first_granted`,
    accumulated `yielded` time, and clearing stale lock/yield markers and sidecars. Idempotent on already-settled
    held stages."""
    before = stage_of(entry)
    if before in SETTLED_HELD_STAGES:
        return NO_EFFECT
    spec = _session_spec(entry)
    if before != Stage.UNSETTLED or (spec is not None and spec.get("ended_by")):
        raise IllegalTransitionError(f"illegal transition in settle: {before.value} -> held")
    back = _resume_fields(entry, moment)
    after = require_transition(before, stage_of(entry), via="settle", allowed=frozenset({Stage.UNSETTLED}))
    resolved = back if back is not None else (moment if moment is not None else time.time())
    session_init = initial_session_uses(resolved) if after in (Stage.SESSION_ACTIVE, Stage.SESSION_STOPPING) else None
    return SidecarEffect(unlink_yield=True, unlink_idle=True, init_session=session_init)


def resume(entry: Record, moment: float | None = None) -> SidecarEffect:
    """Compatibility alias for `settle()` on a held entry resumed after a yield."""
    return settle(entry, moment)


def to_held(entry: Record, granted: str, moment: float, pressed: bool = False, served: str | None = None,
            count: int | None = None) -> SidecarEffect:
    """A waiter granted at `granted` (a now() stamp of `moment`): `pressed` while memory is not normal, `aged` when
    it is served in class `served` (ageing) rather than its own, `count` for a range request (`-n MIN-MAX`) granted
    at what is free, and resumed if it had yielded."""
    before = stage_of(entry)
    allowed = frozenset({Stage.WAITING, Stage.YIELDED})
    if before not in allowed:
        raise IllegalTransitionError(f"illegal transition in to_held: {before.value} -> held")
    entry.update(state=HELD, granted=granted)
    if count is not None and count >= 1:
        entry["count"] = count
    if pressed:
        entry[PRESSED] = True  # under warning, the one lease at a time until it releases
    else:
        entry.pop(PRESSED, None)
    if served is not None and served != priority_of(entry):
        entry["aged"] = served  # the class ageing moved it to by its grant; status and the history show it
    if "yielded_at" in entry or bool(entry.get(LOCK_YIELD)):
        _resume_fields(entry, moment)
    after = require_transition(before, stage_of(entry), via="to_held", allowed=allowed)
    if after in (Stage.SESSION_ACTIVE, Stage.SESSION_STOPPING):
        uses = initial_session_uses(moment)
        if before == Stage.WAITING:
            return SidecarEffect(unlink_yield=True, unlink_idle=True, write_session=uses)
        return SidecarEffect(unlink_yield=True, unlink_idle=True, init_session=uses)
    return SidecarEffect(unlink_yield=True, unlink_idle=True)


def _yield_fields(entry: Record, moment: float, rank: float | None, *, polite: bool) -> None:
    if polite:
        entry.update(state=WAITING, yields=int(entry.get("yields") or 0) + 1, yielded_at=round(moment, 3),
                     first_granted=entry.get("granted"), rank=rank)
    else:
        yields, previous_rank = entry.get("yields"), entry.get("rank")
        entry.update(state=WAITING, yields=int(yields or 0) + 1, yielded_at=round(moment, 3),
                     first_granted=entry.get("granted"), rank=previous_rank)
        if previous_rank is None:
            entry.pop("rank", None)
        if yields is None:
            entry.pop("yields", None)
        else:
            entry["yields"] = yields
    if type(entry.get("min_count")) is int and entry["min_count"] >= 1:
        entry["count"] = entry["min_count"]  # a range lease waits as its MIN again until re-granted
    entry.pop("reserved", None)  # a reservation an older client granted it with is no promise now
    entry.pop(PRESSED, None)  # back in the queue it is no lease of a warning; its next grant decides


def to_yielded(entry: Record, moment: float, rank: float | None) -> SidecarEffect:
    """A held lease back among the waiters at `moment`, queued at `rank`, just behind the waiter it yields to."""
    raw_stage = stage_of(entry)
    if _session_spec(entry) is not None or raw_stage not in (Stage.HELD, Stage.UNSETTLED):
        raise IllegalTransitionError(f"illegal transition in to_yielded: {raw_stage.value} -> {Stage.YIELDED.value}")
    if raw_stage == Stage.UNSETTLED:
        settle(entry)
    before = stage_of(entry)
    _yield_fields(entry, moment, rank, polite=True)
    require_transition(before, stage_of(entry), via="to_yielded", allowed=frozenset({Stage.HELD}))
    return SidecarEffect(unlink_yield=True, unlink_idle=True)


def to_lock_waiting(entry: Record, moment: float, lock: str, owner: tuple[int, str]) -> SidecarEffect:
    """A held lease back among the waiters at `moment` while its command (the process `owner`) waits to lock
    `lock`: yielded as to_yielded() writes it (an older client reads it as a lease that yielded), keeping its place
    (its rank, pinned or not, and its ageing, which counts on from before: LOCK_YIELD) and without counting a polite
    yield (`yields`, which YIELDS_MAX caps)."""
    raw_stage = stage_of(entry)
    allowed = frozenset({Stage.HELD, Stage.LOCK_NOTED, Stage.SESSION_ACTIVE, Stage.SESSION_STOPPING})
    spec = _session_spec(entry)
    if (raw_stage not in (allowed | {Stage.UNSETTLED})) or (spec is not None and spec.get("ended_by")):
        raise IllegalTransitionError(
            f"illegal transition in to_lock_waiting: {raw_stage.value} -> {Stage.LOCK_YIELDED.value}"
        )
    if raw_stage == Stage.UNSETTLED:
        settle(entry)
    before = stage_of(entry)
    _yield_fields(entry, moment, entry.get("rank"), polite=False)
    entry.update({LOCK: lock, LOCK_OWNER: {"pid": owner[0], "start": owner[1]}, LOCK_YIELD: True})
    require_transition(before, stage_of(entry), via="to_lock_waiting", allowed=allowed)
    return SidecarEffect(unlink_yield=True, unlink_idle=True)


def to_lock_noted(entry: Record, lock: str, owner: tuple[int, str]) -> SidecarEffect:
    """A held lease whose command waits to lock `lock` while other work runs under it: it keeps its slots and only
    says what it waits for (fathers#159 review)."""
    raw_stage = stage_of(entry)
    allowed = frozenset({Stage.HELD, Stage.LOCK_NOTED, Stage.SESSION_ACTIVE, Stage.SESSION_STOPPING})
    spec = _session_spec(entry)
    if (raw_stage not in (allowed | {Stage.UNSETTLED})) or (spec is not None and spec.get("ended_by")):
        raise IllegalTransitionError(
            f"illegal transition in to_lock_noted: {raw_stage.value} -> {Stage.LOCK_NOTED.value}"
        )
    settle_effect = settle(entry) if raw_stage == Stage.UNSETTLED else NO_EFFECT
    before = stage_of(entry)
    entry.update({LOCK: lock, LOCK_OWNER: {"pid": owner[0], "start": owner[1]}})
    require_transition(before, stage_of(entry), via="to_lock_noted", allowed=allowed)
    return settle_effect


def lock_taken(entry: Record) -> SidecarEffect:
    """The lock wait is over (taken, given up, or its waiter gone): the mark goes; a yielded lease then waits to be
    granted again as any yielded lease, in its place."""
    raw_stage = stage_of(entry)
    if raw_stage == Stage.UNSETTLED and lock_of(entry) is not None:
        return settle(entry)
    allowed = frozenset({Stage.LOCK_NOTED, Stage.LOCK_YIELDED})
    if raw_stage not in allowed:
        raise IllegalTransitionError(f"illegal transition in lock_taken: {raw_stage.value}")
    entry.pop(LOCK, None)
    entry.pop(LOCK_OWNER, None)
    require_transition(raw_stage, stage_of(entry), via="lock_taken", allowed=allowed)
    return NO_EFFECT


def trial_yielded(entry: Mapping[str, Any], rank: float | None) -> Record:
    """A copy of a held lease as it would wait if it yielded now, at `rank`: for a plan that tries it."""
    return dict(entry, state=WAITING, rank=rank)


def trial_held(entry: Mapping[str, Any], granted: str, count: int | None = None) -> Record:
    """A copy of a waiter as it would hold from `granted`: for a forecast that plays the queue forward."""
    found = dict(entry, state=HELD, granted=granted)
    if count is not None and count >= 1:
        found["count"] = count
    found.pop("yielded", None)
    return found


def to_reprioritized(entry: Record, priority: str, moment: float) -> SidecarEffect:
    """A request moved to class `priority` at `moment`: `reprioritized` keeps the class it asked first, `since` its
    arrival; ageing counts from now."""
    raw_stage = stage_of(entry)
    spec = _session_spec(entry)
    if (raw_stage not in (REPRIORITIZABLE_STAGES | {Stage.UNSETTLED})) or (spec is not None and spec.get("ended_by")):
        raise IllegalTransitionError(f"illegal transition in to_reprioritized: {raw_stage.value}")
    settle_effect = settle(entry) if raw_stage == Stage.UNSETTLED else NO_EFFECT
    before = stage_of(entry)
    entry.setdefault("reprioritized", priority_of(entry))
    entry["priority"] = priority
    entry["reprioritized_at"] = round(moment, 3)  # ageing counts from here (ageing_since())
    entry.pop("aged", None)
    require_transition(before, stage_of(entry), via="to_reprioritized", allowed=REPRIORITIZABLE_STAGES)
    return settle_effect


def _copy_uses(uses: Mapping[str, Any] | None, began: float) -> dict[str, Any]:
    if uses is None:
        return initial_session_uses(began)
    copied = {**initial_session_uses(began), **dict(uses)}
    active_raw: object = copied.get("active")
    copied["active"] = dict(cast(Mapping[str, Any], active_raw)) if isinstance(active_raw, Mapping) else {}
    return copied


def session_start_run(entry: Record, uses: Mapping[str, Any], *, pid: int, start: str, started: int | None,
                      label: str | None, cmd: str, since: str) -> SidecarEffect:
    """Register an active run on a held, accepting measurement session."""
    raw_stage = stage_of(entry)
    spec = _session_spec(entry)
    allowed = frozenset({Stage.SESSION_ACTIVE, Stage.LOCK_NOTED})
    if spec is None or spec.get("stopping") or spec.get("ended_by") or raw_stage not in (allowed | {Stage.UNSETTLED}):
        raise IllegalTransitionError(f"illegal transition in session_start_run: {raw_stage.value}")
    settle_effect = settle(entry) if raw_stage == Stage.UNSETTLED else NO_EFFECT
    before = stage_of(entry)
    require_transition(before, stage_of(entry), via="session_start_run", allowed=allowed)
    updated = _copy_uses(uses, 0.0)
    updated["active"][str(pid)] = {"start": start, "started": started, "label": label, "cmd": cmd, "since": since}
    return SidecarEffect(
        unlink_yield=settle_effect.unlink_yield,
        unlink_idle=settle_effect.unlink_idle,
        write_session=updated,
    )


def session_finish_run(entry: Record, uses: Mapping[str, Any], *, pid: int, ran_seconds: float, moment: float,
                       usage: Mapping[str, Any] | None = None) -> SidecarEffect:
    """Record a completed run inside a live measurement session."""
    raw_stage = stage_of(entry)
    spec = _session_spec(entry)
    if spec is None or spec.get("ended_by") or raw_stage not in (LIVE_SESSION_STAGES | {Stage.UNSETTLED}):
        raise IllegalTransitionError(f"illegal transition in session_finish_run: {raw_stage.value}")
    settle_effect = settle(entry, moment) if raw_stage == Stage.UNSETTLED else NO_EFFECT
    before = stage_of(entry)
    require_transition(before, stage_of(entry), via="session_finish_run", allowed=LIVE_SESSION_STAGES)
    updated = _copy_uses(uses, moment)
    updated["active"].pop(str(pid), None)
    updated.update(
        runs=int(updated.get("runs") or 0) + 1,
        busy=round(float(updated.get("busy") or 0) + ran_seconds, 2),
        last_use=round(moment, 3),
    )
    if usage:
        updated["cpu"] = round(float(updated.get("cpu") or 0) + float(usage.get("cpu") or 0), 2)
        updated["rss_peak_mb"] = max(float(updated.get("rss_peak_mb") or 0), float(usage.get("rss_peak_mb") or 0))
    return SidecarEffect(
        unlink_yield=settle_effect.unlink_yield,
        unlink_idle=settle_effect.unlink_idle,
        write_session=updated,
    )


def session_step(entry: Record, uses: Mapping[str, Any] | None, *, moment: float, began: float, idle: float,
                 stop_asked: bool = False, pruned: bool = False) -> SessionStep:
    """Advance a live measurement session by one holder check."""
    raw_stage = stage_of(entry)
    spec = _session_spec(entry)
    if spec is None or spec.get("ended_by") or raw_stage not in (LIVE_SESSION_STAGES | {Stage.UNSETTLED}):
        raise IllegalTransitionError(f"illegal transition in session_step: {raw_stage.value}")
    settled = raw_stage == Stage.UNSETTLED
    settle_effect = settle(entry, moment) if settled else NO_EFFECT
    before = stage_of(entry)
    current_uses = _copy_uses(uses, began)
    changed = settled
    if stop_asked and not spec.get("stopping"):
        spec["stopping"] = True
        changed = True
    reason = (
        None if current_uses["active"]
        else "stopped" if (stop_asked or spec.get("stopping"))
        else "idle" if moment - float(current_uses.get("last_use") or began) >= idle
        else None
    )
    if reason is None:
        require_transition(before, stage_of(entry), via="session_step", allowed=LIVE_SESSION_STAGES)
        write_uses = current_uses if (pruned or uses is None) else None
        return SessionStep(
            uses=current_uses,
            effect=SidecarEffect(
                unlink_yield=settle_effect.unlink_yield,
                unlink_idle=settle_effect.unlink_idle,
                write_session=write_uses,
            ),
            changed=changed,
            ended_by=None,
        )
    entry.pop(LOCK, None)
    entry.pop(LOCK_OWNER, None)
    spec.update(stopping=True, runs=int(current_uses.get("runs") or 0),
                busy=round(float(current_uses.get("busy") or 0), 1), ended_by=reason)
    require_transition(before, stage_of(entry), via="session_step", allowed=LIVE_SESSION_STAGES)
    return SessionStep(
        uses=current_uses,
        effect=SidecarEffect(
            unlink_yield=settle_effect.unlink_yield,
            unlink_idle=settle_effect.unlink_idle,
            unlink_session=True,
        ),
        changed=True,
        ended_by=reason,
    )


@dataclass(frozen=True)
class Ran:
    """The client saw the lease as held and ran its command to completion."""
    exit: int | None = None


@dataclass(frozen=True)
class Left:
    """The client left the queue without ever observing the lease as held."""
    reason: str = "error"  # "timeout" | "signal" | "error"


@dataclass(frozen=True)
class Reaped:
    """The holder died and another client reaped the entry."""
    was_held: bool


@dataclass(frozen=True)
class Refused:
    """The request was refused by admission or memory policy."""
    reason: str


Outcome = Ran | Left | Reaped | Refused
TERMINAL_STAGES: frozenset[Stage] = frozenset({Stage.RELEASED, Stage.LEFT, Stage.REAPED, Stage.REFUSED})


def outcome_stage(result: Outcome) -> Stage:
    """The terminal `Stage` corresponding to an `Outcome`."""
    if isinstance(result, Ran):
        return Stage.RELEASED
    if isinstance(result, Left):
        return Stage.LEFT
    if isinstance(result, Reaped):
        return Stage.REAPED
    return Stage.REFUSED


def to_terminal(entry: Mapping[str, Any], target: Stage) -> SidecarEffect:
    """Validate a terminal transition (`RELEASED`, `LEFT`, `REAPED`, `REFUSED`) and return its sidecar cleanup."""
    if target not in TERMINAL_STAGES:
        raise IllegalTransitionError(f"illegal terminal target: {target.value}")
    before = stage_of(entry)
    require_transition(before, target, via="to_terminal")
    return TERMINAL_EFFECT


def ran(entry: Mapping[str, Any]) -> bool:
    """Whether a request ran: it holds now, or held before yielding (politely or for an external lock)."""
    return (is_held(entry) or bool(entry.get("yields")) or "yielded_at" in entry
            or yielded_of(entry) > 0 or "resumed" in entry or bool(entry.get(LOCK_YIELD)))


def decide_outcome(entry: Mapping[str, Any], *, held_seen: bool | None = None, status: int | None = None,
                   left_reason: str | None = None, reaped: bool = False, refused: str | None = None) -> Outcome:
    """Decide a request's outcome once from what the client actually knows (fathers#10).

    A lease the client never saw as held (`held_seen=False`) is always Left, even if another client granted it
    between the client's last read and its deadline or termination."""
    result: Outcome
    if refused is not None:
        result = Refused(refused)
    elif reaped:
        result = Reaped(was_held=ran(entry))
    elif held_seen is False:
        result = Left(left_reason or "error")
    elif held_seen is True or ran(entry):
        result = Ran(exit=status)
    else:
        result = Left(left_reason or "error")
    before = stage_of(entry)
    if before != Stage.UNKNOWN:
        require_transition(before, outcome_stage(result), via="decide_outcome")
    return result


def outcome_name(result: Outcome) -> str:
    """The history `outcome` string kept compatible for older readers."""
    if isinstance(result, Ran):
        return "released"
    if isinstance(result, Left):
        return "left"
    if isinstance(result, Reaped):
        return "reaped"
    return "refused"


def outcome_fields(result: Outcome) -> dict[str, Any]:
    """History fields for `result`: `outcome` plus `reason` / `was_held` where applicable."""
    fields: dict[str, Any] = {"outcome": outcome_name(result)}
    if isinstance(result, Left):
        fields["reason"] = result.reason
    elif isinstance(result, Reaped):
        fields["was_held"] = result.was_held
    elif isinstance(result, Refused):
        fields["reason"] = result.reason
    return fields


def outcome(entry: Mapping[str, Any], reaped: bool = False) -> str:
    """How a request left the queue, as its history line says: `reaped` when its holder died, else `released` when
    it ran (ran()) and `left` when it gave up waiting."""
    return outcome_name(decide_outcome(entry, reaped=reaped))
