"""The queue's policy (fathers#35): given the entries, the capacity, the order settings, a moment, the overtaking
window and an estimate, which waiters start, who is the head, who is asked to yield and when each is expected to
start. It reads no file, environment or clock: its callers read the settings (settings.order_settings()), the window
(config.overtake_window()), the moment and the estimator (estimates.py) once per look and hand them in. Where a
caller leaves the settings or the window out, the built-in defaults (config.ORDER_DEFAULTS, config.OVERTAKE_WINDOW)
apply, never a file or the environment."""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

from . import config as _config
from . import entry as _entry


Entry = _entry.Record  # a queue entry, waiting or held, as the queue files hold it
Estimate = Callable[[Entry], Optional[float]]  # an entry's expected run in seconds; None when unknown
DEFAULTS: dict[str, Any] = _config.ORDER_DEFAULTS  # the order settings where a caller gives none (read-only)
WINDOW: float = float(_config.OVERTAKE_WINDOW)  # the overtaking window where a caller gives none


def overtakable(entry: Entry, moment: float, window: float) -> bool:
    """Whether later requests that fit may still start before this waiter at `moment`: it has waited less than the
    overtaking `window` (seconds)."""
    queued = _config.seconds(entry.get("since"))
    return queued is not None and moment - queued < window


def no_estimate(entry: Entry) -> float | None:
    """An estimate for which every run is unknown."""
    return None






def effective_priority(entry: Entry, settings: dict[str, Any] | None = None, moment: float | None = None) -> str:
    """The class a waiter is served in at `moment`: its own, moved up one class for each ageing step it has waited
    through: batch -> normal once it has waited `aging_batch` seconds, normal -> interactive once it has waited
    `aging_normal` more (a normal request: `aging_normal` in all); 0 stops it there. Its arrival stays its own, so
    an aged request keeps its place among those of its new class that came after it. Ageing counts from
    entry.ageing_since(): a class set by `reprioritize` holds for its own steps. A held lease, and any entry without
    `settings` or `moment`, keeps its own class."""
    cls = _entry.priority_of(entry)
    # by its state alone: the plan gets known entries (leases.reap()), and a partial one here is a caller's copy
    if settings is None or moment is None or entry.get("state") == _entry.HELD:
        return cls
    began = _entry.ageing_since(entry)
    if began is None:
        return cls
    waited = moment - began
    for step, key, up in (("batch", "aging_batch", "normal"), ("normal", "aging_normal", "interactive")):
        after = float(settings.get(key) or 0)
        if cls != step or not after or waited < after:
            continue
        waited, cls = waited - after, up
    return cls


def aged(entry: Entry, settings: dict[str, Any] | None, moment: float | None) -> bool:
    """Whether ageing moved the waiter up a class (effective_priority())."""
    return effective_priority(entry, settings, moment) != _entry.priority_of(entry)


def deferring(entry: Entry, settings: dict[str, Any] | None = None, moment: float | None = None) -> bool:
    """Whether a waiter defers to normal and interactive work (#5): it is batch, whatever its slot count, and also
    when it is back in the queue after a yield. While a normal or interactive request waits, it is never the head
    and holds that work back nowhere; once none waits it is the head as any other (others_wait()). A batch waiter
    that has aged (effective_priority()) is normal and defers no more."""
    return effective_priority(entry, settings, moment) == "batch"






def others_wait(waiters: list[Entry], entry: Entry, grants: list[Entry], settings: dict[str, Any] | None = None,
                moment: float | None = None) -> bool:
    """Whether a normal or interactive waiter other than `entry` waits in `waiters` and is not among `grants`: work
    that a deferring() waiter lets go first."""
    return any(other is not entry and not any(other is g for g in grants) and not deferring(other, settings, moment)
               for other in waiters)




def rank_time(entry: Entry, settings: dict[str, Any], moment: float | None = None) -> float:
    """When a waiter counts as queued at `moment`: one served as interactive (effective_priority(): its class, aged)
    `interactive` seconds before it came, one served as batch `batch` seconds after. Ageing only moves a waiter up.
    A lease that yielded counts as queued at its pinned `rank`, just behind the waiter it yielded to, whatever its
    class: it is half done and holds its memory meanwhile."""
    behind = _entry.pinned(entry)  # a lease that yielded queues just behind the waiter it yielded to, and only there
    if behind is not None:
        return behind
    queued = _config.seconds(entry.get("since"))
    queued = math.inf if queued is None else queued
    cls = effective_priority(entry, settings, moment)
    return queued - settings.get("interactive", 0.0) if cls == "interactive" else \
        queued + settings.get("batch", 0.0) if cls == "batch" else queued


def ranked(waiters: list[Entry], settings: dict[str, Any], moment: float | None = None) -> list[Entry]:
    """Waiters in the order the queue serves them at `moment`: by rank_time(), then as they came (a stable sort);
    behind the head that holds a reservation, those that came after it (behind_head())."""
    found = sorted(waiters, key=lambda entry: rank_time(entry, settings, moment))
    return behind_head(found, settings, moment)




def behind_head(waiters: list[Entry], settings: dict[str, Any], moment: float | None = None) -> list[Entry]:
    """`waiters` (in rank order) with the requests that came after the head holding a reservation moved behind it,
    in their rank order among those behind it: once a waiter is the head, a later arrival of any class starts before
    it only as backfill, ending before its reserved start, so its start never moves later for work that came after
    it. Waiters that came before it keep their place; a lease back in the queue after a yield keeps its pin."""
    head = next((e for e in waiters if _entry.reservation(e) is not None), None)
    if head is None:
        return waiters
    arrived = _config.seconds(head.get("since"))
    at = next(n for n, e in enumerate(waiters) if e is head)
    if arrived is None:
        return waiters
    later = [e for e in waiters[:at] if _entry.pinned(e) is None and (_config.seconds(e.get("since")) or math.inf) > arrived]
    if not later:
        return waiters
    ahead = [e for e in waiters[:at] if not any(e is moved for moved in later)]
    behind = sorted(later + waiters[at + 1:], key=lambda entry: rank_time(entry, settings, moment))
    return ahead + [head] + behind


class Plan:
    """What grant() does to the waiters: `grants` start now, in queue order; `reason` says why the first that does
    not start waits; `head` is the waiter the others must not delay, expected to start at `start` (epoch seconds,
    by the held leases' expected ends now), with `reserved` the start it is promised (the earliest projected since
    it became the head), which nothing that passes it may end after; None where not worked out this look."""

    def __init__(self) -> None:
        self.grants: list[Entry] = []
        self.reason: str | None = None
        self.head: Entry | None = None
        self.start: float | None = None
        self.reserved: float | None = None
        self.known = True  # False: slots held in an earlier queue, whose ends are unknown, so nothing passes
        # fathers#160: the held leases the head's start waits on whose end nobody knows (no estimate)
        self.blocked: list[Entry] = []
        # fathers#160: waiters that gave way, (entry, the leases that blocked it), past their reserved start by
        # `reservation_timeout` while such leases held their slots: they hold nobody back
        self.dropped: list[tuple[Entry, list[Entry]]] = []




def expected_end(entry: Entry, moment: float, run: float | None, fallback: float, overrun: float) -> float:
    """When a lease that holds slots is expected to end: its expected `run` (`fallback` when unknown) after its
    grant; one already past it runs on for `overrun` times what it has run so far, because a run that overran its
    kind tends to be a long one."""
    ran = _entry.held_for(entry, moment)
    expected = fallback if run is None else run
    return moment + max(expected - ran, overrun * ran)


def free_at(count: int, ends: list[tuple[float, int]], have: int, moment: float) -> tuple[int, float]:
    """When `count` slots are free, walking `ends` ((expected end, slots it frees) of what holds slots) in time order
    from `have` slots free at `moment` (below zero while a shrunk capacity drains: those ends free nothing yet).
    Returns the slots free then and that moment; fewer than `count` when the ends do not free enough (slots held in an
    earlier queue, whose ends are unknown), with the moment of the last end."""
    start = moment
    for end, size in sorted(ends):
        if have >= count:
            break
        have, start = have + size, end
    return have, start


def unknown_end(entry: Entry, estimate: Estimate) -> bool:
    """Whether nobody knows when a held lease ends (fathers#160): the history has no run like it, so its end is the
    fallback's guess and then the overrun's. A lease past a known estimate is not: its end moves by a known rule."""
    return estimate(entry) is None


def blocking(count: int, held: list[Entry], have: int, slots: int, moment: float, estimate: Estimate,
             fallback: float, overrun: float) -> list[Entry]:
    """The leases of `held` whose expected ends free_at() walks to free `count` slots from `have`, that end nobody
    knows (unknown_end()): what a head waiting for those slots is blocked by."""
    ends = sorted(((expected_end(e, moment, estimate(e), fallback, overrun), _entry.holds(e, slots), n)
                   for n, e in enumerate(held)), key=lambda end: (end[0], end[1]))
    found: list[Entry] = []
    for _, size, n in ends:
        if have >= count:
            break
        have += size
        if unknown_end(held[n], estimate):
            found.append(held[n])
    return found


def reserve(found: Plan, held: list[Entry], slots: int, used: int, moment: float, estimate: Estimate,
            fallback: float, overrun: float) -> int:
    """Sets the head's expected start: when, by the expected ends of what holds slots (`held` and the grants so
    far), enough slots will be free for it. Returns the slots spare then, those the head will not need even if the
    leases expected to run on past its start are the largest ones (estimates are rough, and which lease ends first
    is too)."""
    assert found.head is not None
    need: int = found.head["count"]
    ends = [(expected_end(entry, moment, estimate(entry), fallback, overrun), _entry.holds(entry, slots))
            for entry in held + found.grants]
    have, start = free_at(need, ends, slots - used, moment)
    found.start = start
    found.blocked = blocking(need, held + found.grants, slots - used, slots, moment, estimate, fallback, overrun)
    if have < need:  # slots held in an earlier queue, whose end is unknown: nothing passes
        found.start, found.known = moment, False
        return 0
    staying = sum(1 for end, _ in ends if end > start)
    largest = sum(sorted((count for _, count in ends), reverse=True)[:staying])
    other = used - sum(count for _, count in ends)  # held in an earlier queue
    return max(0, min(have, slots - other - largest) - need)


def soonest_start(count: int, holding: list[Entry], slots: int, used: int, moment: float, estimate: Estimate,
                  overrun: float) -> float | None:
    """When `count` slots are expected to be free if nothing more starts: by the expected ends of the leases in
    `holding`, a lease past its expected run running on for the overrun as reserve() counts it (#5). None when that
    is unknown: a lease whose run the history does not know (it may end any moment), or slots held in an earlier
    queue. It says whether a waiter can start at all before its client gives up."""
    ends: list[tuple[float, int]] = []
    for entry in holding:
        run = estimate(entry)
        if run is None:
            return None
        ends.append((expected_end(entry, moment, run, run, overrun), _entry.holds(entry, slots)))
    have, start = free_at(count, ends, slots - used, moment)
    return start if have >= count else None


def fit(waiters: list[Entry], slots: int) -> list[Entry]:
    """Makes each waiter ask for at most the capacity `slots`, and an `-n all` one (its entry's `all`) for exactly
    it: a capacity that shrank under a waiter (a memory budget when a VM's balloon takes memory) would otherwise
    leave it waiting for slots that no longer exist. The change is the waiter's own entry's: a grant writes it and
    the holder takes its count from there. Only for the queue's whole capacity, not a share of it."""
    for entry in waiters:
        if entry.get("all") or entry["count"] > slots:
            entry["count"] = slots
    return waiters


def plan(waiters: list[Entry], held: list[Entry], slots: int, moment: float, order: str | None = None,
         estimate: Estimate | None = None, other: int = 0, reserve_head: bool = False,
         settings: dict[str, Any] | None = None, window: float = WINDOW, p90: Estimate | None = None) -> Plan:
    """Which of `waiters` (queue order) start now beside `held` (entries; `other` slots more held without one) on
    `slots` slots at epoch `moment`. `estimate(entry)` is its expected run in seconds or None (unknown).

    window: in arrival order; a waiter that does not fit lets later ones that fit start first until it has waited
    `window` seconds (SLOTFATHER_OVERTAKE_WINDOW, read by the caller), then holds back everyone behind it.

    backfill: waiters in their class order (ranked(): one served as interactive `interactive` seconds earlier than
    it came, one served as batch `batch` seconds later, a class moving up as the waiter ages, a yielded lease just
    behind the waiter it yielded to, and those that came after the head behind it); the same within the window; the
    first waiter past it that does not fit is the head. Its start is reserved: the moment enough slots will be free,
    from the expected ends of the held leases, and never later than an earlier look reserved it (its entry's
    `reserved`, which grant() keeps: reservations()). A later waiter that fits starts before it when `safety` times
    its expected run ends by that reserved start (and, with a `limit`, by the time the head has waited it; past
    the limit nothing passes), or when it takes only slots the head will not need then (reserve()). An unknown run
    counts `fallback` seconds; a held lease past its expected run is expected to run on for `overrun` times what it
    has run so far. A waiter whose client gives up (its entry's `until`, from `run --wait`) before the held leases
    are expected to free its slots, counting the overrun of those past their estimate (soonest_start(); a lease of
    unknown run keeps it the head), is no head: it would leave without running, so it holds nobody back. Nor is a
    batch waiter (deferring(); one that aged is normal) while a normal or interactive one waits (others_wait()),
    and a batch one for the whole machine does not start then; once none waits it is the head as any other.
    A yielded lease whose command waits on a lock (entry.lock_of(), fathers#159) is left out: neither granted nor
    the head until it has the lock.
    A head whose reserved start has passed while leases whose end nobody knows (Plan.blocked: no estimate;
    fathers#160) still hold its slots was promised by guesses that failed: a later waiter that fits the free
    slots also starts when its p90 run (`p90(entry)`, else its expected run, else the fallback) ends by the head's
    start as now projected. A head
    whose reserved start passed `reservation_timeout` seconds ago while such leases still hold its slots gives
    way (Plan.dropped): it holds nobody back, keeps its reservation and place, and is the head again once they end.
    `settings` are the caller's settings.order_settings() (DEFAULTS where not given); `order` overrides theirs. The
    head's start is worked out when it holds no reservation yet, when a request behind it fits, or with
    `reserve_head`."""
    settings = settings or DEFAULTS
    order = order or settings["order"]
    estimate = estimate or no_estimate
    timeout = float(settings.get("reservation_timeout") or 0)
    waiters = [entry for entry in waiters if _entry.lock_of(entry) is None]  # waiting on a lock: not grantable
    if order == "backfill":
        waiters = ranked(waiters, settings, moment)
    fallback, safety, overrun, limit = (float(settings[key]) for key in ("fallback", "safety", "overrun", "limit"))
    found = Plan()
    used = sum(_entry.holds(e, slots) for e in held) + other
    extra = None  # slots free at the head's start that it does not need; reserved once a request behind it fits
    bound = math.inf  # what a request that passes the head must end by: its reserved start
    deadline = math.inf  # with a limit, when the head has waited it: from then on nothing passes

    def quiet(free: int, entry: Entry) -> str:
        return f"{free} of {slots} slots free; {entry.get('label')} is batch and lets the normal and interactive " \
               "requests that wait go first"

    def promise() -> int:
        """reserve() for the head, and the start it is promised: the earlier of what it held and what is
        projected now; slots held in an earlier queue leave both as they were (nothing passes meanwhile)."""
        nonlocal bound, deadline
        assert found.head is not None
        spare = reserve(found, held, slots, used, moment, estimate, fallback, overrun)
        held_before = _entry.reservation(found.head)
        if not found.known:
            found.reserved, bound = held_before, moment
        else:
            projected = float(math.floor(found.start if found.start is not None else moment))
            found.reserved = projected if held_before is None else min(held_before, projected)
            bound = found.reserved
        # A yielded head counts its limit from when it yielded: by its arrival the limit is long past.
        began = _entry.waiting_since(found.head) or moment
        deadline = began + limit if limit else math.inf
        return spare

    for entry in waiters:
        free = max(0, slots - used)
        if found.head is not None:
            if entry["count"] > free:
                continue
            if extra is None:
                extra = promise()
            if moment >= deadline:
                continue  # the head has waited the limit: it holds back everyone behind it
            run = estimate(entry)
            run = fallback if run is None else run
            if moment + run * safety <= min(bound, deadline):
                pass  # ends before the head's reserved start (and before it has waited the limit)
            elif entry["count"] <= extra:
                extra -= entry["count"]
            elif found.blocked and found.start is not None and moment >= bound and \
                    moment + longest(entry, p90, run) <= min(found.start, deadline):
                pass  # its promise broke on ends nobody knows: what ends by its projected start passes (#160)
            else:
                continue
            found.grants.append(entry)
            used += entry["count"]
            continue
        # A batch waiter defers while normal or interactive work waits (#5): it is no head then, and one for the
        # whole machine does not take it either, since that would shut the waiting work out for its whole run.
        defers = order == "backfill" and deferring(entry, settings, moment) and \
            others_wait(waiters, entry, found.grants, settings, moment)
        if defers and entry["count"] <= free and _entry.whole_machine(entry, slots):
            found.reason = found.reason or quiet(free, entry)
            continue
        if entry["count"] <= free:
            found.grants.append(entry)
            used += entry["count"]
            continue
        queued = _config.seconds(entry.get("since"))
        passable = queued is not None and moment - queued < window
        if order != "backfill":
            if not passable:
                found.reason = found.reason or f"{free} of {slots} slots free"
                return found
        elif defers:
            found.reason = found.reason or quiet(free, entry)
            continue
        elif not passable:
            found.head = entry
            gives_up = None if entry.get("granted") else _config.seconds(entry.get("until"))
            if gives_up is not None:
                soonest = soonest_start(entry["count"], held + found.grants, slots, used, moment, estimate, overrun)
                if soonest is not None and soonest > gives_up:
                    # Its client gives up before the held leases are expected to free its slots: holding the
                    # others back would only leave slots idle until it leaves without running. Past its limit too
                    # (#5): nothing passes then, but the leases it waits for still run on.
                    found.head = None
                    clock = time.strftime("%H:%M:%S", time.localtime(gives_up))
                    found.reason = found.reason or (f"{free} of {slots} slots free; {entry.get('label')} gives up at "
                                                    f"{clock}, before they are expected, so requests that fit pass it")
                    continue
            promised = _entry.reservation(entry)
            if timeout and promised is not None and moment - promised >= timeout:
                trial = Plan()
                trial.head = entry
                reserve(trial, held, slots, used, moment, estimate, fallback, overrun)
                if trial.known and trial.blocked and (trial.start or moment) > moment:
                    # Its reserved start passed long ago and leases nobody knows the end of still hold its slots:
                    # it gives way rather than idle them (fathers#160); it keeps its reservation and place.
                    found.head = None
                    found.dropped.append((entry, trial.blocked))
                    names = ", ".join(str(e.get("label")) for e in trial.blocked)
                    found.reason = found.reason or (
                        f"{free} of {slots} slots free; {entry.get('label')} gave way: its reserved start passed "
                        f"{_config.duration(moment - promised)} ago while leases with no end estimate hold its "
                        f"slots ({names})")
                    continue
            if reserve_head or _entry.reservation(entry) is None:
                extra = promise()
            else:
                found.reserved = _entry.reservation(entry)
            found.reason = (f"{free} of {slots} slots free; {entry.get('label')} is next, and a later request that fits "
                            f"starts first only if it ends before {entry.get('label')} can start")
            continue
        until = time.strftime("%H:%M:%S", time.localtime(queued + window))
        found.reason = found.reason or (f"{free} of {slots} slots free; requests that fit may pass "
                                        f"{entry.get('label')} until {until}")
    return found


def longest(entry: Entry, p90: Estimate | None, run: float) -> float:
    """How long a request that passes a blocked head may run (fathers#160): its p90 where known, else `run`."""
    found = p90(entry) if p90 is not None else None
    return run if found is None else found


def shared_plan(found: Plan, waiters: list[Entry], held: list[Entry], slots: int, moment: float, estimate: Estimate,
                other: int, settings: dict[str, Any], shared: dict[str, Any], window: float = WINDOW,
                p90: Estimate | None = None) -> Plan:
    """The plan whose head keeps the reservations (reservations()): `found`, a plan by `settings`, when they are the
    shared ones (settings.order_settings(environment=False)), else a plan by the shared settings, so that a client whose
    environment tightens the order (SLOTFATHER_QUEUE=window, an ageing step it lengthens) neither drops nor resets
    the reservation every client's looks keep; a shared window order keeps none."""
    if all(settings.get(key) == shared.get(key) for key in _config.ORDER_DEFAULTS):
        return found
    if shared["order"] != "backfill":
        return Plan()
    return plan(waiters, held, slots, moment, estimate=estimate, other=other, settings=shared, window=window,
                p90=p90)


def reservations(waiters: list[Entry], found: Plan) -> list[Entry]:
    """Keeps the waiters' entries' reservations as `found` (a plan of them) leaves them: the head holds the start it
    is promised (`reserved`), every other waiter none. Returns the entries it changed, which grant() writes; plan()
    reads them at the next look, so the promised start only ever moves earlier while the waiter stays the head."""
    changed: list[Entry] = []
    dropped = [entry for entry, _ in found.dropped]
    for entry in waiters:
        # A head that gave way keeps the start it was promised: its time past it goes on counting (fathers#160)
        want = found.reserved if entry is found.head else \
            _entry.reservation(entry) if any(entry is d for d in dropped) else None
        if want is None:
            if "reserved" in entry:
                entry.pop("reserved")
                changed.append(entry)
        elif _entry.reservation(entry) != want:
            entry["reserved"] = want
            changed.append(entry)
    return changed


def head_drops(waiters: list[Entry], found: Plan, moment: float) -> tuple[list[Entry], list[Entry]]:
    """Marks on the waiters' entries which gave way (Plan.dropped, fathers#160): `head_dropped`, the moment one
    first did, so that its client and EVENTS hear of it once; removed when it no longer does. Returns (the entries
    that gave way just now, the entries it changed), for grant() to record and write."""
    dropped = [entry for entry, _ in found.dropped]
    new: list[Entry] = []
    changed: list[Entry] = []
    for entry in waiters:
        if any(entry is d for d in dropped):
            if _entry.HEAD_DROPPED not in entry:
                entry[_entry.HEAD_DROPPED] = round(moment, 3)
                new.append(entry)
                changed.append(entry)
        elif _entry.HEAD_DROPPED in entry:
            entry.pop(_entry.HEAD_DROPPED)
            changed.append(entry)
    return new, changed


def yield_asks(waiters: list[Entry], held: list[Entry], slots: int, moment: float, estimate: Estimate | None = None,
               other: int = 0, settings: dict[str, Any] | None = None,
               asked: list[Entry] | tuple[Entry, ...] = (), window: float = WINDOW) -> list[tuple[Entry, Entry]]:
    """Polite preemption (#643): (holder, waiter) pairs, the holders of `held` (entries, after this look's grants)
    that a waiter asks to yield now. Only an interactive waiter asks, once it has waited `yield_after` seconds, does
    not fit, and would not get its slots within `yield_after` by the held leases' expected ends; it asks only
    holders that opted in (`yieldable`), are of a lower class, have held their slots `yield_after` seconds since
    their last grant and were asked fewer than YIELDS_MAX times, and only as many as it needs (holders in `asked`,
    their ids, first; then the lowest class; then the one expected to run longest), and only when grant() would
    then start it, with the asked holders back among the waiters at their own rank. Nobody is ever stopped: a
    holder yields when its client gets round to it, or not at all."""
    settings = settings or DEFAULTS
    after = settings.get("yield_after") or 0
    if not after or settings["order"] != "backfill":
        return []
    estimate = estimate or no_estimate
    level = {name: n for n, name in enumerate(_config.PRIORITIES)}
    asked_ids = {id(entry) for entry in asked}
    holding: list[Entry] = list(held)
    found: list[tuple[Entry, Entry]] = []
    granted: list[Entry] = []
    fallback, overrun = float(settings["fallback"]), float(settings["overrun"])

    def ends(entry: Entry) -> float:
        return expected_end(entry, moment, estimate(entry), fallback, overrun)

    for entry in ranked(waiters, settings, moment):
        asking = effective_priority(entry, settings, moment)  # an aged waiter asks as the class it is served in
        if asking not in _config.ASKERS or any(entry is g for g in granted) or _entry.lock_of(entry) is not None:
            continue
        own = entry.get("yield_after")  # its client's environment asks later, or never (0)
        if isinstance(own, (int, float)) and not isinstance(own, bool):
            wait_for = max(after, float(own)) if own > 0 else None
        else:
            wait_for = after
        queued = _config.seconds(entry.get("since"))
        if wait_for is None or queued is None or moment - queued < wait_for:
            continue
        free = slots - other - sum(_entry.holds(h, slots) for h in holding)
        if entry["count"] <= free:
            continue  # it fits: what holds it back is not slots
        have, when = free_at(entry["count"], [(ends(h), _entry.holds(h, slots)) for h in holding], free, moment)
        if have >= entry["count"] and when - moment <= after:
            continue  # its slots are expected soon anyway
        candidates = [h for h in holding if h.get("yieldable") and level[_entry.priority_of(h)] > level[asking]
                      and int(h.get("yields") or 0) < _config.YIELDS_MAX
                      and moment - (_config.seconds(h.get("resumed") or h.get("granted")) or moment) >= after]
        candidates.sort(key=lambda h: (id(h) not in asked_ids, -level[_entry.priority_of(h)], -ends(h)))
        chosen: list[Entry] = []
        need = entry["count"] - free
        for h in candidates:
            if need <= 0:
                break
            chosen.append(h)
            need -= _entry.holds(h, slots)
        if need > 0:
            continue
        behind = rank_time(entry, settings, moment) + _config.YIELD_BEHIND
        back = [_entry.trial_yielded(h, behind) for h in chosen]
        rest = [w for w in waiters if not any(w is g for g in granted)] + back
        trial = plan(rest, [h for h in holding if not any(h is c for c in chosen)], slots, moment, estimate=estimate,
                     other=other, settings=settings, window=window)
        if not any(g is entry for g in trial.grants):
            continue
        found += [(h, entry) for h in chosen]
        holding = [h for h in holding if not any(h is c for c in chosen)] + [entry]
        granted.append(entry)
    return found


def idle_asks(waiters: list[Entry], held: list[Entry], idle: list[Entry], slots: int, moment: float,
              estimate: Estimate | None = None, other: int = 0,
              settings: dict[str, Any] | None = None, window: float = WINDOW) -> list[tuple[Entry, Entry]]:
    """Idle leases asked to yield (#12): (holder, waiter) pairs for the `idle` holders (idle.idle_since()) that opted
    in (`yieldable`), were asked fewer than YIELDS_MAX times, and whose slots would start a waiting request that would
    not start otherwise; that waiter is the one the request names. Nobody is asked while nobody waits, a lease that
    cannot yield is only marked, and nobody is ever stopped: the holder yields when its client gets round to it."""
    if not waiters:
        return []
    settings = settings or DEFAULTS
    holding = list(held)
    found: list[tuple[Entry, Entry]] = []
    for lease in idle:
        if not lease.get("yieldable") or int(lease.get("yields") or 0) >= _config.YIELDS_MAX:
            continue
        before = plan(waiters, holding, slots, moment, estimate=estimate, other=other, settings=settings,
                      window=window).grants
        after = plan(waiters, [h for h in holding if h is not lease], slots, moment, estimate=estimate, other=other,
                     settings=settings, window=window).grants
        started = [w for w in after if not any(w is b for b in before)
                   and not any(w is asked for _, asked in found)]
        if started:
            found.append((lease, started[0]))
            holding = [h for h in holding if h is not lease]
    return found


WARNING_WAIT = "memory pressure warning: one 1-slot lease at a time"

def memory_gate(grants: list[Entry], held: list[Entry], used: int, memory: tuple[str, str | None],
                settings: dict[str, Any]) -> list[tuple[Entry, str | None]]:
    """Which of plan()'s `grants` memory lets start now beside `held` (`used` slots held in all), in their order:
    (entry, None) starts, (entry, why) waits. Normal memory starts them all. While memory is short nothing starts
    beside a running lease. Under warning, with `warning_admits` on, a 1-slot request starts when no running lease
    was granted while memory was not normal (entry.PRESSED), so one such lease runs at a time; others wait. An idle machine
    (nothing used) always starts its first request."""
    kind, why = memory
    allowance = kind == "warning" and float(settings.get("warning_admits", 1)) >= 1
    pressed = any(e.get(_entry.PRESSED) for e in held)
    found: list[tuple[Entry, str | None]] = []
    for entry in grants:
        reason = None
        if used and kind != "normal":
            if not allowance:
                reason = why
            elif entry["count"] != 1 or entry.get("all") or pressed:
                reason = WARNING_WAIT
        if reason is None:
            used += max(1, int(entry["count"]))
            pressed = pressed or kind != "normal"
        found.append((entry, reason))
    return found


FORECAST_STEP = 300.0  # seconds between a forecast's looks when no lease is expected to end sooner
FORECAST_LOOKS = 200  # looks a forecast takes at most


def forecast(waiters: list[Entry], held: list[Entry], slots: int, moment: float, estimate: Estimate | None = None,
             other: int = 0, settings: dict[str, Any] | None = None, window: float = WINDOW,
             p90: Estimate | None = None) -> dict[int, float]:
    """When each waiter is expected to start if nothing more comes: id(entry) -> epoch seconds, for the waiters a
    forecast reaches. It plays the queue forward as grant() would: each held lease ends when it is expected to
    (expected_end(); an unknown run counts the fallback), each grant runs its expected run, and plan() decides at
    every end and every FORECAST_STEP seconds between them (windows end and waiters age meanwhile); the head keeps
    its reservation as grant() keeps it. Memory, yields and give-ups are left out. Slots held in an earlier queue
    have no known end: then only the waiters that start now are forecast."""
    settings = settings or DEFAULTS
    estimate = estimate or no_estimate
    fallback, overrun = float(settings["fallback"]), float(settings["overrun"])
    ends: dict[int, float] = {}  # id(a running copy) -> when it is expected to end
    running: list[Entry] = []
    for entry in held:
        copy = dict(entry)
        ends[id(copy)] = expected_end(entry, moment, estimate(entry), fallback, overrun)
        running.append(copy)
    originals: dict[int, Entry] = {}  # id(a waiting copy) -> its waiter
    queue: list[Entry] = []
    for entry in waiters:
        copy = dict(entry)
        originals[id(copy)] = entry
        queue.append(copy)
    fixed = dict(settings, overrun=0.0)  # an end comes when it is expected: overrun 0 keeps expected_end() there

    def expected(entry: Entry) -> float | None:
        if id(entry) in ends:  # running: until its end
            granted = _config.seconds(entry.get("granted"))
            return None if granted is None else ends[id(entry)] - granted - _entry.yielded_of(entry)
        return estimate(entry)

    found: dict[int, float] = {}
    kept: list[Entry] = list(running)
    at = moment
    for _ in range(FORECAST_LOOKS):
        step = plan(queue, running, slots, at, estimate=expected, other=other, settings=fixed, window=window, p90=p90)
        reservations(queue, step)
        for entry in step.grants:
            found[id(originals[id(entry)])] = at
            queue = [e for e in queue if e is not entry]
            started = _entry.trial_held(entry, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)))
            run = estimate(entry)
            ends[id(started)] = at + (fallback if run is None else run)
            running.append(started)
            kept.append(started)  # alive to the end, so that no later copy takes its id in `ends`
        if not queue or other:
            break
        later = min((ends[id(e)] for e in running if ends[id(e)] > at), default=math.inf)
        if later == math.inf and not step.grants:
            break  # nothing runs and nothing started: nothing changes from here
        at = min(later, at + FORECAST_STEP)
        running = [e for e in running if ends[id(e)] > at]
    return found


def room(waiters: list[Entry], free: int, held: list[Entry] | None = None, estimate: Estimate | None = None,
         other: int = 0, settings: dict[str, Any] | None = None, label: str = "", command: str = "",
         slots: int | None = None, *, moment: float, window: float = WINDOW) -> int:
    """The slots a new request gets at once behind `waiters` with `free` slots unheld, as grant() admits it: one
    for `command` under `label`, whose run the estimates know, or else one of unknown run (the fallback). Without
    `held` (the entries holding slots; `other` slots more held without one), the window order's answer. `slots`
    is the capacity; without it, what `free` and the held slots add up to (wrong only while a shrink drains).
    `moment` is when the request would come (the policy reads no clock)."""
    if slots is None:
        slots = free + sum(int(e["count"]) for e in held or ()) + other
    if held is not None:  # the whole capacity: what grant() fits the waiters to
        fit(waiters, slots)
    settings = settings or DEFAULTS
    order = settings["order"] if held is not None else "window"
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))
    for count in range(free, 0, -1):
        probe: Entry = {"count": count, "label": label, "cmd": command, "since": since}
        if any(entry is probe for entry in plan(waiters + [probe], held or [], slots, moment, order, estimate,
                                                         other, settings=settings, window=window).grants):
            return count
    return 0
