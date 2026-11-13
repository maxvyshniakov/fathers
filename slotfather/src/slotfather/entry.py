"""A queue entry (`queue/*.json`): its persisted fields, the accessors that read them whatever wrote the file, its
phase, and the one definition of each transition a request goes through.

Every function here is pure apart from the dict it is given: callers read and write the files under the queue lock
(state, leases) and log what happened (queue.log, events). The on-disk format is the one older clients write and
read (docs/architecture.md, "State on disk"); a field added here must be one they read past."""
from __future__ import annotations

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
    found = [epoch(entry.get("reprioritized_at")), epoch(entry.get("yielded_at")), _config.seconds(entry.get("since"))]
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


# Transitions: the one definition of each. They change the entry they are given (or return a copy, for a trial),
# nothing else.

def new_request(pid: int, start: str, count: int, label: str, command: str, cwd: str, since: str, priority: str,
                lease_id: str, *, started: int | None = None, ticket: str | None = None, whole: bool = False,
                meta: dict[str, str] | None = None, naming: Mapping[str, str] | None = None,
                session: dict[str, Any] | None = None, git: dict[str, Any] | None = None, yieldable: bool = False,
                until: str | None = None, yield_after: float | None = None) -> Record:
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
    return found


def resume(entry: Record, moment: float | None = None) -> None:
    """A yielded lease that was granted again (at `moment`, else its `granted` stamp): `granted` is its first grant
    again (waits and runs in the history stay its own), `resumed` this grant, `yielded` the seconds it spent back in
    the queue. Nothing for a lease that never yielded."""
    away = epoch(entry.get("yielded_at"))
    if away is None:
        return
    again = entry.get("granted")
    back = moment if moment is not None else _config.seconds(again) or time.time()
    entry["yielded"] = round(yielded_of(entry) + max(0.0, back - away), 2)
    entry.update(resumed=again, granted=entry.get("first_granted") or again)
    for key in ("yielded_at", "first_granted", "rank"):
        entry.pop(key, None)


def to_held(entry: Record, granted: str, moment: float, pressed: bool = False, served: str | None = None) -> None:
    """A waiter granted at `granted` (a now() stamp of `moment`): `pressed` while memory is not normal, `aged` when
    it is served in class `served` (ageing) rather than its own, and resumed if it had yielded."""
    entry.update(state=HELD, granted=granted)
    if pressed:
        entry[PRESSED] = True  # under warning, the one lease at a time until it releases
    else:
        entry.pop(PRESSED, None)
    if served is not None and served != priority_of(entry):
        entry["aged"] = served  # the class ageing moved it to by its grant; status and the history show it
    resume(entry, moment)


def to_yielded(entry: Record, moment: float, rank: float | None) -> None:
    """A held lease back among the waiters at `moment`, queued at `rank`, just behind the waiter it yields to."""
    entry.update(state=WAITING, yields=int(entry.get("yields") or 0) + 1, yielded_at=round(moment, 3),
                 first_granted=entry.get("granted"), rank=rank)
    entry.pop("reserved", None)  # a reservation an older client granted it with is no promise now
    entry.pop(PRESSED, None)  # back in the queue it is no lease of a warning; its next grant decides


def trial_yielded(entry: Mapping[str, Any], rank: float | None) -> Record:
    """A copy of a held lease as it would wait if it yielded now, at `rank`: for a plan that tries it."""
    return dict(entry, state=WAITING, rank=rank)


def trial_held(entry: Mapping[str, Any], granted: str) -> Record:
    """A copy of a waiter as it would hold from `granted`: for a forecast that plays the queue forward."""
    found = dict(entry, state=HELD, granted=granted)
    found.pop("yielded", None)
    return found


def to_reprioritized(entry: Record, priority: str, moment: float) -> None:
    """A request moved to class `priority` at `moment`: `reprioritized` keeps the class it asked first, `since` its
    arrival; ageing counts from now."""
    entry.setdefault("reprioritized", priority_of(entry))
    entry["priority"] = priority
    entry["reprioritized_at"] = round(moment, 3)  # ageing counts from here (ageing_since())
    entry.pop("aged", None)


def ran(entry: Mapping[str, Any]) -> bool:
    """Whether a request ran: it holds, or held before it yielded."""
    return is_held(entry) or bool(entry.get("yields"))


def outcome(entry: Mapping[str, Any], reaped: bool = False) -> str:
    """How a request left the queue, as its history line says: `reaped` when its holder died, else `released` when
    it ran (ran()) and `left` when it gave up waiting."""
    if reaped:
        return "reaped"
    return "released" if ran(entry) else "left"
