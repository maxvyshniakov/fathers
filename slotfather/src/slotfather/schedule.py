"""The queue's policy (fathers#35): given the entries, the capacity, the order settings, a moment, the overtaking
window and an estimate, which waiters start, who is the head, who is asked to yield and when each is expected to
start. It reads no file, environment or clock: its callers read the settings (settings.order_settings()), the window
(config.overtake_window()), the moment and the estimator (estimates.py) once per look and hand them in. Where a
caller leaves the settings or the window out, the built-in defaults (config.ORDER_DEFAULTS, config.OVERTAKE_WINDOW)
apply, never a file or the environment."""
from __future__ import annotations

import math
import time
from typing import Any, Callable, NamedTuple, Optional, cast

from . import config as _config
from . import entry as _entry


Entry = _entry.Record  # a queue entry, waiting or held, as the queue files hold it
Estimate = Callable[[Entry], Optional[float]]  # an entry's expected run in seconds; None when unknown
MemoryState = tuple[str, Optional[str]]  # ("normal", None), ("warning", why) or ("short", why)
LoadAverages = tuple[float, float, float]  # (1m, 5m, 15m) load averages
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
        self.granted_counts: dict[int, int] = {}  # id(entry) -> granted slots (for -n MIN-MAX range requests, fathers#128)
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
        # fathers#251: waiters held back by a per-group concurrency cap (id(entry) -> reason)
        self.group_capped: dict[int, str] = {}

    def granted_count(self, entry: Entry) -> int:
        """The slots granted to `entry` in this plan (its range grant, else its requested count)."""
        return self.granted_counts.get(id(entry), int(entry["count"]))


def _grant_size(entry: Entry, limit: int, slots: int) -> int:
    """The slots granted to `entry` when `limit` slots are free: `entry['count']` for a fixed request, or up to
    `max_count` (capped by `limit` and `slots`) for a `-n MIN-MAX` range request (fathers#128)."""
    base = int(entry["count"])
    upper = entry.get("max_count")
    if type(upper) is int and upper > base:
        return max(base, min(upper, limit, slots))
    return base




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


def _holds(entry: Entry, slots: int, granted_counts: dict[int, int] | None = None) -> int:
    """Slots `entry` holds from `slots`, using its range grant size when already granted in this plan."""
    if granted_counts is not None and id(entry) in granted_counts:
        return granted_counts[id(entry)]
    return _entry.holds(entry, slots)


def blocking(count: int, held: list[Entry], have: int, slots: int, moment: float, estimate: Estimate,
             fallback: float, overrun: float, granted_counts: dict[int, int] | None = None) -> list[Entry]:
    """The leases of `held` whose expected ends free_at() walks to free `count` slots from `have`, that end nobody
    knows (unknown_end()): what a head waiting for those slots is blocked by."""
    ends = sorted(((expected_end(e, moment, estimate(e), fallback, overrun), _holds(e, slots, granted_counts), n)
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
    ends = [(expected_end(entry, moment, estimate(entry), fallback, overrun),
             _holds(entry, slots, found.granted_counts))
            for entry in held + found.grants]
    have, start = free_at(need, ends, slots - used, moment)
    found.start = start
    found.blocked = blocking(need, held + found.grants, slots - used, slots, moment, estimate, fallback, overrun,
                             found.granted_counts)
    if have < need:  # slots held in an earlier queue, whose end is unknown: nothing passes
        found.start, found.known = moment, False
        return 0
    staying = sum(1 for end, _ in ends if end > start)
    largest = sum(sorted((count for _, count in ends), reverse=True)[:staying])
    other = used - sum(count for _, count in ends)  # held in an earlier queue
    return max(0, min(have, slots - other - largest) - need)


def soonest_start(count: int, holding: list[Entry], slots: int, used: int, moment: float, estimate: Estimate,
                  overrun: float, granted_counts: dict[int, int] | None = None) -> float | None:
    """When `count` slots are expected to be free if nothing more starts: by the expected ends of the leases in
    `holding`, a lease past its expected run running on for the overrun as reserve() counts it (#5). None when that
    is unknown: a lease whose run the history does not know (it may end any moment), or slots held in an earlier
    queue. It says whether a waiter can start at all before its client gives up."""
    ends: list[tuple[float, int]] = []
    for entry in holding:
        run = estimate(entry)
        if run is None:
            return None
        ends.append((expected_end(entry, moment, run, run, overrun), _holds(entry, slots, granted_counts)))
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
    Once it gave way (`head_dropped` on its entry) only leases granted before then let it give way again, and while
    it does, a request with no run like it in the history is not admitted before it.
    `settings` are the caller's settings.order_settings() (DEFAULTS where not given); `order` overrides theirs. The
    head's start is worked out when it holds no reservation yet, when a request behind it fits, or with
    `reserve_head`."""
    settings = settings or DEFAULTS
    order = order or settings["order"]
    estimate = estimate or no_estimate
    timeout = float(settings.get("reservation_timeout") or 0)
    waiters = [
        entry for entry in waiters
        if _entry.lock_of(entry) is None and _entry.stage_of(entry) != _entry.Stage.SESSION_ENDED
    ]  # waiting on a lock or session already ending: not grantable
    if order == "backfill":
        waiters = ranked(waiters, settings, moment)
    fallback, safety, overrun, limit = (float(settings[key]) for key in ("fallback", "safety", "overrun", "limit"))
    group_caps = _group_caps_of(settings)
    active_groups = _count_active_groups(held, group_caps)
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
        if group_caps:
            cap_reason = _group_cap_wait(entry, active_groups, group_caps)
            if cap_reason is not None:
                found.group_capped[id(entry)] = cap_reason
                found.reason = found.reason or cap_reason
                continue
        free = max(0, slots - used)
        if found.head is not None:
            if entry["count"] > free:
                continue
            if extra is None:
                extra = promise()
            if not found.known or moment >= deadline:
                continue  # earlier-queue slots have unknown ends, or the head waited the limit: nothing passes
            run = estimate(entry)
            if found.dropped and run is None:
                continue  # a head gave way: unknown-run requests wait behind it even under a second head (fathers#228)
            run = fallback if run is None else run
            if moment + run * safety <= min(bound, deadline):
                take = _grant_size(entry, free, slots)  # ends before the head's reserved start
            elif entry["count"] <= extra:
                take = _grant_size(entry, min(extra, free), slots)
                extra -= take
            elif found.blocked and found.start is not None and moment >= bound and \
                    moment + longest(entry, p90, run) <= min(found.start, deadline):
                take = _grant_size(entry, free, slots)  # its promise broke on ends nobody knows (#160)
            else:
                continue
            found.grants.append(entry)
            found.granted_counts[id(entry)] = take
            if group_caps:
                _record_active_groups(entry, active_groups, group_caps)
            used += take
            continue
        # A batch waiter defers while normal or interactive work waits (#5): it is no head then, and one for the
        # whole machine does not take it either, since that would shut the waiting work out for its whole run.
        defers = order == "backfill" and deferring(entry, settings, moment) and \
            others_wait(waiters, entry, found.grants, settings, moment)
        if defers and entry["count"] <= free and _entry.whole_machine(entry, slots):
            found.reason = found.reason or quiet(free, entry)
            continue
        if entry["count"] <= free and found.dropped and estimate(entry) is None:
            # A head gave way: a request nobody knows the run of is no backfill against it, or a stream of them
            # would keep it from its slots for good (fathers#160 review)
            found.reason = found.reason or (f"{free} of {slots} slots free; {entry.get('label')} has no run like it "
                                            f"in the history and waits behind {found.dropped[0][0].get('label')}, "
                                            f"which gave way")
            continue
        if entry["count"] <= free:
            take = _grant_size(entry, free, slots)
            found.grants.append(entry)
            found.granted_counts[id(entry)] = take
            if group_caps:
                _record_active_groups(entry, active_groups, group_caps)
            used += take
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
                soonest = soonest_start(entry["count"], held + found.grants, slots, used, moment, estimate, overrun,
                                        found.granted_counts)
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
                trial.grants = list(found.grants)
                trial.granted_counts = dict(found.granted_counts)
                reserve(trial, held, slots, used, moment, estimate, fallback, overrun)
                # Once it gave way, only the leases that blocked it then let it give way again: what was granted
                # since (with an estimate, or by an older client) holds it back no more (fathers#160 review)
                gave_way = _entry.epoch(entry.get(_entry.HEAD_DROPPED))
                blockers = [e for e in trial.blocked if gave_way is None
                            or (_config.seconds(e.get("granted")) or math.inf) < gave_way]
                if trial.known and blockers and (trial.start or moment) > moment:
                    # Its reserved start passed long ago and leases nobody knows the end of still hold its slots:
                    # it gives way rather than idle them (fathers#160); it keeps its reservation and place.
                    found.head = None
                    found.dropped.append((entry, blockers))
                    names = ", ".join(str(e.get("label")) for e in blockers)
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
        until = time.strftime("%H:%M:%S", time.localtime((queued or moment) + window))
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
                      and _entry.lock_of(h) is None and _entry.stage_of(h) != _entry.Stage.SESSION_ENDED
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
    settings = settings or DEFAULTS
    if not waiters or settings["order"] != "backfill":
        return []
    holding = list(held)
    found: list[tuple[Entry, Entry]] = []
    for lease in idle:
        if (
            not lease.get("yieldable")
            or _entry.lock_of(lease) is not None
            or _entry.stage_of(lease) == _entry.Stage.SESSION_ENDED
            or int(lease.get("yields") or 0) >= _config.YIELDS_MAX
        ):
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
LOAD_PEAK = 1.5  # fathers#184: min(load1, load5) at this multiple of the gate's cores holds grants too


class _Unset:
    pass


_UNSET = _Unset()


class Host:
    """Host resource readings for Admission: plain values or zero-arg callables memoized on first property read."""

    def __init__(
        self,
        memory: MemoryState | Callable[[], MemoryState] = ("normal", None),
        ram_mb: float | None | Callable[[], float | None] = None,
        averages: LoadAverages | None | Callable[[], LoadAverages | None] = None,
        cores: int | Callable[[], int] = 0,
    ) -> None:
        self._memory_src = memory
        self._ram_mb_src = ram_mb
        self._averages_src = averages
        self._cores_src = cores
        self._memory: MemoryState | _Unset = _UNSET
        self._ram_mb: float | None | _Unset = _UNSET
        self._averages: LoadAverages | None | _Unset = _UNSET
        self._cores: int | _Unset = _UNSET

    @property
    def memory(self) -> tuple[str, str | None]:
        if isinstance(self._memory, _Unset):
            src = self._memory_src
            self._memory = src() if callable(src) else src
        return self._memory

    @property
    def ram_mb(self) -> float | None:
        if isinstance(self._ram_mb, _Unset):
            src = self._ram_mb_src
            self._ram_mb = src() if callable(src) else src
        return self._ram_mb

    @property
    def averages(self) -> tuple[float, float, float] | None:
        if isinstance(self._averages, _Unset):
            src = self._averages_src
            self._averages = src() if callable(src) else src
        return self._averages

    @property
    def cores(self) -> int:
        if isinstance(self._cores, _Unset):
            src = self._cores_src
            self._cores = src() if callable(src) else src
        return self._cores


class GateBlock(NamedTuple):
    kind: str  # "memory", "budget", or "load"
    reason: str


class StartedGrant(NamedTuple):
    entry: Entry
    count: int  # pyright: ignore[reportIncompatibleMethodOverride]
    served: str
    pressed: bool


class GrantOutcome(NamedTuple):
    plan: Plan
    keeper: Plan
    started: list[StartedGrant]
    gated: list[tuple[Entry, GateBlock]]
    reason: str | None


class DashboardRoom(NamedTuple):
    admission: int
    memory_block: str | None
    load_block: str | None
    memory_budget_block: str | None
    memory_expected_mb: float
    memory_budget_mb: float | None
    start: float | None
    reserved: float | None


class AdviseBlock(NamedTuple):
    queue_room: int
    waiting: bool
    run_estimate: float | None
    memory_level: str
    memory_short: str | None
    load_block: str | None
    memory_budget_block: str | None
    memory_budget_mb: float | None = None
    memory_expected_mb: float = 0.0
    group_cap_block: str | None = None


def entry_groups(entry: Entry) -> tuple[str, ...]:
    """Concurrency-cap groups matching `entry` (fathers#251): explicit metadata (`group`, `suite`, `kind`) plus
    browser-suite detection across metadata (`suite`, `kind`, `phase`, `step`), `label`, and `cmd`."""
    groups: set[str] = set()
    meta = _entry.meta_of(entry)
    raw_group = meta.get("group")
    if raw_group:
        for token in raw_group.split(","):
            name = token.strip().lower()
            if _config.GROUP_NAME.fullmatch(name):
                groups.add(name)
    for key in ("suite", "kind"):
        val = meta.get(key)
        if not val:
            continue
        name = val.strip().lower()
        if _config.GROUP_NAME.fullmatch(name):
            groups.add(name)
        if _config.BROWSER_SUITE_RE.search(val):
            groups.add("browser")
    for key in ("phase", "step"):
        val = meta.get(key)
        if val and _config.BROWSER_SUITE_RE.search(val):
            groups.add("browser")
    for field in ("label", "cmd"):
        text = entry.get(field)
        if isinstance(text, str) and _config.BROWSER_SUITE_RE.search(text):
            groups.add("browser")
    return tuple(sorted(groups))


def _group_caps_of(settings: dict[str, Any]) -> dict[str, int]:
    """Validated {group: cap} from order settings, ignoring malformed values defensively."""
    raw = settings.get("group_caps")
    if not isinstance(raw, dict):
        return {}
    found: dict[str, int] = {}
    for group, cap in cast(dict[Any, Any], raw).items():
        if isinstance(group, str) and isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1:
            found[group] = cap
    return found


def _count_active_groups(held: list[Entry], group_caps: dict[str, int]) -> dict[str, int]:
    active: dict[str, int] = {}
    if not group_caps:
        return active
    for entry in held:
        _record_active_groups(entry, active, group_caps)
    return active


def _record_active_groups(entry: Entry, active: dict[str, int], group_caps: dict[str, int]) -> None:
    for group in entry_groups(entry):
        if group in group_caps:
            active[group] = active.get(group, 0) + 1


def _group_cap_wait(entry: Entry, active_groups: dict[str, int], group_caps: dict[str, int]) -> str | None:
    """Why a per-group concurrency cap holds `entry` back (fathers#251), or None."""
    if not group_caps:
        return None
    for group in entry_groups(entry):
        cap = group_caps.get(group)
        if cap is not None:
            running = active_groups.get(group, 0)
            if running >= cap:
                return f'group "{group}" cap {cap} reached ({running} running)'
    return None


def _load_wait(averages: tuple[float, float, float] | None, cores: int, slots: int, held: int,
               settings: dict[str, Any]) -> str | None:
    """Why sustained CPU load holds back grants beside running leases (fathers#184), or None. With `load_gate` G
    (an order setting; 0 disables) and `held` slots of `slots` held on `cores` cores, under load `averages` (1, 5
    and 15 minutes; None: unknown, no gate): the load is min(load1, load5), so a short burst does not count; the
    unleased load is that less the held slots' share of the cores, as `advise` counts it (query.size()). It holds
    at an unleased load of G x cores, or at a load of LOAD_PEAK x G x cores, whatever the leases themselves run.
    _gate() applies it beside running leases only: an idle machine always starts its first request."""
    gate = float(settings.get("load_gate", _config.ORDER_DEFAULTS["load_gate"]))
    if averages is None or not gate > 0 or cores < 1 or slots < 1:
        return None
    if not (math.isfinite(averages[0]) and math.isfinite(averages[1])):
        return None
    load = min(averages[0], averages[1])
    unleased = max(0.0, load - min(max(0, held), slots) * cores / slots)
    if unleased >= gate * cores:
        return f"CPU load {load:.1f}: {unleased:.1f} unleased is at or above {gate * cores:g} cores"
    if load >= LOAD_PEAK * gate * cores:
        return f"CPU load {load:.1f} is at or above {LOAD_PEAK * gate * cores:g} ({LOAD_PEAK * gate:g} x {cores} cores)"
    return None


def _effective_memory_budget_mb(
    ram_mb: float | None, share: float, budget_mb: float = 0.0
) -> tuple[float | None, str]:
    """Effective expected-peak memory budget in MB and its explanation text: the lower of `ram_mb * share` (when
    RAM is known and share > 0) and explicit `budget_mb` (when > 0; fathers#251), or (None, "") when neither applies."""
    share_mb = ram_mb * share if (ram_mb is not None and ram_mb > 0 and share > 0) else None
    cap_mb = budget_mb if budget_mb > 0 else None
    if share_mb is not None and cap_mb is not None:
        if cap_mb < share_mb:
            return cap_mb, f"{cap_mb:.0f} MB (configured budget)"
        assert ram_mb is not None
        return share_mb, f"{share_mb:.0f} MB ({share * 100:.0f}% of {ram_mb:.0f} MB)"
    if cap_mb is not None:
        return cap_mb, f"{cap_mb:.0f} MB (configured budget)"
    if share_mb is not None:
        assert ram_mb is not None
        return share_mb, f"{share_mb:.0f} MB ({share * 100:.0f}% of {ram_mb:.0f} MB)"
    return None, ""


def _budget_wait(
    held_mb: float, need_mb: float, ram_mb: float | None, share: float, budget_mb: float = 0.0
) -> str | None:
    """Why expected peak memory holds a request back beside running leases (fathers#9, fathers#251), or None: when
    the held leases' expected peak (`held_mb`) and the request's (`need_mb`) exceed the effective memory budget.
    _gate() applies it beside running leases only, and the dashboard's admission and `advise` read the same rule
    through Admission (fathers#206, fathers#214)."""
    limit_mb, detail = _effective_memory_budget_mb(ram_mb, share, budget_mb)
    if limit_mb is None:
        return None
    if held_mb + need_mb > limit_mb:
        return f"expected memory {held_mb + need_mb:.0f} MB exceeds {detail}"
    return None


def _gate(
    grants: list[Entry],
    held: list[Entry],
    used: int,
    slots: int,
    host: Host,
    settings: dict[str, Any],
    *,
    memory_estimate: Estimate | None = None,
    granted_counts: dict[int, int] | None = None,
) -> list[tuple[Entry, int, GateBlock | None]]:
    """Which of plan()'s `grants` memory, expected-peak budget and CPU load let start now beside `held` (`used` slots
    held in all), in their order: (entry, grant_count, None) starts (clamping a `-n 1-MAX` range grant to 1 under
    warning), (entry, grant_count, GateBlock) waits. Normal memory starts them all unless expected peak memory exceeds
    `memory_share` of `host.ram_mb` (fathers#9). While memory is short nothing starts beside a running lease. Under
    warning, with `warning_admits` on, a 1-slot request (or a `-n 1-MAX` range request, clamped to 1 slot) starts
    when no running lease was granted while memory was not normal (entry.PRESSED), so one such lease runs at a time;
    others wait. While sustained CPU load holds grants (_load_wait(), fathers#184) nothing starts beside a running
    lease either. An idle machine (nothing used) always starts its first request, and checks budget/load/memory on
    any 2nd+ grant."""
    if not grants:
        return []
    kind, why = host.memory
    allowance = kind == "warning" and float(settings.get("warning_admits", 1)) >= 1
    pressed = any(e.get(_entry.PRESSED) for e in held)
    share = float(settings.get("memory_share", _config.ORDER_DEFAULTS["memory_share"]))
    budget_mb = float(settings.get("memory_budget_mb", _config.ORDER_DEFAULTS["memory_budget_mb"]))
    default_mb = float(settings.get("memory_default_mb", _config.ORDER_DEFAULTS["memory_default_mb"]))
    load_gate = float(settings.get("load_gate", _config.ORDER_DEFAULTS["load_gate"]))
    initial_used = used

    def expected_mb(item: Entry) -> float:
        if memory_estimate is None:
            return default_mb
        found_mb = memory_estimate(item)
        return float(found_mb) if found_mb is not None else default_mb

    budget_ready = False
    ram_mb: float | None = None
    limit_mb: float | None = None
    held_mb = 0.0
    load_ready = False
    load_why: str | None = None
    admitted: list[Entry] = []
    found: list[tuple[Entry, int, GateBlock | None]] = []
    for entry in grants:
        grant_count = (granted_counts or {}).get(id(entry), int(entry["count"]))
        block: GateBlock | None = None
        if used and kind != "normal":
            if not allowance:
                block = GateBlock("memory", why or WARNING_WAIT)
            elif entry.get("all") or pressed:
                block = GateBlock("memory", WARNING_WAIT)
            elif grant_count != 1:
                min_count = entry.get("min_count")
                base = min_count if type(min_count) is int else int(entry["count"])
                upper = entry.get("max_count")
                if base == 1 and int(entry["count"]) == 1 and type(upper) is int and upper > 1:
                    grant_count = 1
                else:
                    block = GateBlock("memory", WARNING_WAIT)
        need_mb: float | None = None
        if block is None and used and (share > 0 or budget_mb > 0):
            if not budget_ready:
                budget_ready = True
                ram_mb = host.ram_mb if share > 0 else None
                limit_mb, _ = _effective_memory_budget_mb(ram_mb, share, budget_mb)
                if limit_mb is not None:
                    held_mb = sum(expected_mb(e) for e in held) + sum(expected_mb(e) for e in admitted)
            if limit_mb is not None:
                need_mb = expected_mb(entry)
                budget_reason = _budget_wait(held_mb, need_mb, ram_mb, share, budget_mb)
                if budget_reason is not None:
                    block = GateBlock("budget", budget_reason)
        if block is None and used and load_gate > 0:
            if not load_ready:
                load_ready = True
                load_why = _load_wait(host.averages, host.cores, slots, initial_used, settings)
            if load_why is not None:
                block = GateBlock("load", load_why)
        if block is None:
            used += max(1, grant_count)
            admitted.append(entry)
            if budget_ready and limit_mb is not None and need_mb is not None:
                held_mb += need_mb
            pressed = pressed or kind != "normal"
        found.append((entry, grant_count, block))
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
            started = _entry.trial_held(
                entry,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)),
                count=step.granted_count(entry),
            )
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


def _room(
    waiters: list[Entry],
    free: int,
    held: list[Entry],
    estimate: Estimate | None,
    other: int,
    settings: dict[str, Any],
    label: str,
    command: str,
    slots: int,
    *,
    moment: float,
    window: float = WINDOW,
    p90: Estimate | None = None,
) -> int:
    """The slots a new request gets at once behind `waiters` with `free` slots unheld, by queue order alone."""
    if free <= 0:
        return 0
    if not waiters:
        return free
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))
    for count in range(free, 0, -1):
        probe: Entry = {"count": count, "label": label, "cmd": command, "since": since}
        found = plan(
            waiters + [probe],
            held,
            slots,
            moment,
            settings["order"],
            estimate,
            other,
            settings=settings,
            window=window,
            p90=p90,
        )
        if any(entry is probe for entry in found.grants):
            return count
    return 0


class Admission:
    """The single admission & resource-gate seam across grant(), status/forecast, dashboard, advise and replay
    (fathers#214). Enforces fit(), p90, plan(), and _gate() (memory pressure, memory_share budget, CPU load) on
    private copies of `waiters` without mutating caller dicts, keeping host/estimator reads strictly lazy."""

    def __init__(
        self,
        waiters: list[Entry],
        held: list[Entry],
        slots: int,
        moment: float,
        *,
        other: int = 0,
        settings: dict[str, Any] | None = None,
        shared_settings: dict[str, Any] | None = None,
        window: float = WINDOW,
        host: Host | None = None,
        estimate: Estimate | None = None,
        p90: Estimate | None = None,
        memory_estimate: Estimate | None = None,
    ) -> None:
        self._originals: dict[int, Entry] = {}
        self._waiters: list[Entry] = []
        for entry in waiters:
            copy = dict(entry)
            self._originals[id(copy)] = entry
            self._waiters.append(copy)
        fit(self._waiters, slots)
        self._held: list[Entry] = list(held)
        self._slots: int = slots
        self._moment: float = moment
        self._other: int = other
        self._settings: dict[str, Any] = settings if settings is not None else DEFAULTS
        self._shared_settings: dict[str, Any] = shared_settings if shared_settings is not None else self._settings
        self._window: float = window
        self._host: Host = host if host is not None else Host()
        self._estimate: Estimate = estimate if estimate is not None else no_estimate
        self._p90: Estimate | None = p90
        self._memory_estimate: Estimate | None = memory_estimate
        self._used: int = sum(_entry.holds(e, slots) for e in self._held) + other
        self._reserved_plan: Plan | None = None
        self._forecast_cache: dict[int, dict[str, Any]] | None = None

    def _original(self, entry: Entry) -> Entry:
        return self._originals.get(id(entry), entry)

    def _remap_plan(self, found: Plan) -> Plan:
        mapped = Plan()
        mapped.grants = [self._original(e) for e in found.grants]
        mapped.granted_counts = {
            id(self._original(e)): found.granted_counts[id(e)]
            for e in found.grants
            if id(e) in found.granted_counts
        }
        mapped.reason = found.reason
        mapped.head = None if found.head is None else self._original(found.head)
        mapped.start = found.start
        mapped.reserved = found.reserved
        mapped.known = found.known
        mapped.blocked = [self._original(b) for b in found.blocked]
        mapped.dropped = [
            (self._original(e), [self._original(b) for b in blockers])
            for e, blockers in found.dropped
        ]
        mapped.group_capped = {
            id(self._original(e)): found.group_capped[id(e)]
            for e in self._waiters
            if id(e) in found.group_capped
        }
        return mapped

    def _plan_with_head(self) -> Plan:
        if self._reserved_plan is None:
            self._reserved_plan = plan(
                self._waiters,
                self._held,
                self._slots,
                self._moment,
                estimate=self._estimate,
                other=self._other,
                reserve_head=True,
                settings=self._settings,
                window=self._window,
                p90=self._p90,
            )
        return self._reserved_plan

    def expected_memory(self, entry: Entry, *, settings: dict[str, Any] | None = None) -> tuple[float, str]:
        """An entry's expected peak RSS in MB and its source ("history" or "default")."""
        cfg = settings if settings is not None else self._settings
        default_mb = float(cfg.get("memory_default_mb", _config.ORDER_DEFAULTS["memory_default_mb"]))
        if self._memory_estimate is not None:
            peak = self._memory_estimate(entry)
            if peak is not None:
                return float(peak), "history"
        return default_mb, "default"

    def plan_grants(self, *, gate_by_shared: bool = False) -> GrantOutcome:
        """Plan grants and apply resource gates, returning original waiter references and gate blocks."""
        found = plan(
            self._waiters,
            self._held,
            self._slots,
            self._moment,
            estimate=self._estimate,
            other=self._other,
            settings=self._settings,
            window=self._window,
            p90=self._p90,
        )
        keeper = shared_plan(
            found,
            self._waiters,
            self._held,
            self._slots,
            self._moment,
            self._estimate,
            self._other,
            self._settings,
            self._shared_settings,
            self._window,
            self._p90,
        )
        started: list[StartedGrant] = []
        gated: list[tuple[Entry, GateBlock]] = []
        if found.grants:
            gate_settings = self._shared_settings if gate_by_shared else self._settings
            found, keeper, checked = self._gate_with_clamp(found, keeper, gate_settings, reserve_head=False)
            pressed = self._host.memory[0] != "normal"
            for copy, count, block in checked:
                orig = self._original(copy)
                if block is None:
                    started.append(
                        StartedGrant(
                            orig,
                            count,
                            effective_priority(copy, self._settings, self._moment),
                            pressed,
                        )
                    )
                else:
                    gated.append((orig, block))
        for copy in self._waiters:
            cap_reason = found.group_capped.get(id(copy))
            if cap_reason is not None:
                gated.append((self._original(copy), GateBlock("group_cap", cap_reason)))
        reason = gated[0][1].reason if gated else found.reason
        mapped_plan = self._remap_plan(found)
        mapped_keeper = mapped_plan if keeper is found else self._remap_plan(keeper)
        return GrantOutcome(mapped_plan, mapped_keeper, started, gated, reason)

    def _gate_with_clamp(
        self,
        found: Plan,
        keeper: Plan,
        gate_settings: dict[str, Any],
        *,
        reserve_head: bool,
    ) -> tuple[Plan, Plan, list[tuple[Entry, int, GateBlock | None]]]:
        """Run `_gate` and, if any `-n 1-MAX` grant clamped down to 1 under warning, re-plan with clamped bounds so
        `grants`, `granted_counts`, `head`, `reserved`, `gated`, and `reason` stay consistent when `MAX >= free`."""
        checked = _gate(
            found.grants,
            self._held,
            self._used,
            self._slots,
            self._host,
            gate_settings,
            memory_estimate=self._memory_estimate,
            granted_counts=found.granted_counts,
        )
        clamped = {
            id(copy): count
            for copy, count, block in checked
            if block is None and count < found.granted_count(copy)
        }
        if not clamped:
            return found, keeper, checked
        clamped_waiters: list[Entry] = []
        back: dict[int, Entry] = {}
        for copy in self._waiters:
            if id(copy) in clamped:
                adj = dict(copy)
                adj["count"] = clamped[id(copy)]
                adj["max_count"] = clamped[id(copy)]
            else:
                adj = dict(copy)
            back[id(adj)] = copy
            clamped_waiters.append(adj)

        def remap_to_waiters(p: Plan) -> Plan:
            mapped = Plan()
            mapped.grants = [back[id(e)] for e in p.grants]
            mapped.granted_counts = {id(back[eid]): cnt for eid, cnt in p.granted_counts.items()}
            mapped.reason = p.reason
            mapped.head = back[id(p.head)] if p.head is not None else None
            mapped.start = p.start
            mapped.reserved = p.reserved
            mapped.blocked = list(p.blocked)
            mapped.dropped = [(back[id(e)], list(blockers)) for e, blockers in p.dropped]
            mapped.group_capped = {id(back[eid]): r for eid, r in p.group_capped.items()}
            mapped.known = p.known
            return mapped

        replanned = plan(
            clamped_waiters,
            self._held,
            self._slots,
            self._moment,
            estimate=self._estimate,
            other=self._other,
            reserve_head=reserve_head,
            settings=self._settings,
            window=self._window,
            p90=self._p90,
        )
        replanned_keeper = (
            replanned
            if keeper is found
            else shared_plan(
                replanned,
                clamped_waiters,
                self._held,
                self._slots,
                self._moment,
                self._estimate,
                self._other,
                self._settings,
                self._shared_settings,
                self._window,
                self._p90,
            )
        )
        mapped_found = remap_to_waiters(replanned)
        mapped_keeper = mapped_found if replanned_keeper is replanned else remap_to_waiters(replanned_keeper)
        rechecked = _gate(
            mapped_found.grants,
            self._held,
            self._used,
            self._slots,
            self._host,
            gate_settings,
            memory_estimate=self._memory_estimate,
            granted_counts=mapped_found.granted_counts,
        )
        return mapped_found, mapped_keeper, rechecked

    def forecast(self) -> dict[int, dict[str, Any]]:
        """Expected outlook for each waiter keyed by id(original waiter), gating immediate grants by shared_settings."""
        if self._forecast_cache is not None:
            return self._forecast_cache
        if not self._waiters:
            self._forecast_cache = {}
            return self._forecast_cache
        starts = (
            forecast(
                self._waiters,
                self._held,
                self._slots,
                self._moment,
                self._estimate,
                self._other,
                self._settings,
                self._window,
                self._p90,
            )
            if self._settings["order"] == "backfill"
            else {}
        )
        found = self._plan_with_head()
        if found.grants:
            found, _, checked = self._gate_with_clamp(found, found, self._shared_settings, reserve_head=True)
        else:
            checked = []
        blocks = {id(entry): block for entry, _count, block in checked if block is not None}
        warn = float(self._settings.get("warn_wait") or 0)
        idle = max(0, self._slots - self._used - sum(_holds(e, self._slots, found.granted_counts) for e in found.grants))
        stalled = bool(found.blocked) and found.reserved is not None and self._moment >= found.reserved
        dropped = {id(entry): blockers for entry, blockers in found.dropped}

        def names(leases: list[Entry]) -> list[dict[str, Any]]:
            return [{"label": e.get("label"), "pid": e.get("pid")} for e in leases]

        result: dict[int, dict[str, Any]] = {}
        for copy in self._waiters:
            began = _entry.waiting_since(copy)
            waited = None if began is None else max(0.0, self._moment - began)
            is_head = copy is found.head
            block = blocks.get(id(copy))
            group_wait = found.group_capped.get(id(copy))
            result[id(self._originals[id(copy)])] = {
                "priority": effective_priority(copy, self._settings, self._moment)
                if self._settings["order"] == "backfill"
                else _entry.priority_of(copy),
                "aged": self._settings["order"] == "backfill" and aged(copy, self._settings, self._moment),
                "est_start": None if (block is not None or group_wait is not None) else starts.get(id(copy)),
                "head": is_head,
                "memory_wait": block.reason if block is not None and block.kind != "load" else None,
                "load_wait": block.reason if block is not None and block.kind == "load" else None,
                "group_wait": group_wait,
                "reserved": found.reserved if is_head else None,
                "waited": waited,
                "long_wait": bool(warn and waited is not None and waited >= warn),
                "blocked_by": names(found.blocked) if is_head and stalled else None,
                "idle": idle if is_head and stalled else None,
                "gave_way": names(dropped[id(copy)]) if id(copy) in dropped else None,
            }
        self._forecast_cache = result
        return result

    def room(self, label: str = "", command: str = "", *, gated: bool = False) -> int:
        """Largest request size admitted now: by queue order alone (`gated=False`) or through _gate() (`gated=True`)."""
        free = max(0, self._slots - self._used)
        if free <= 0:
            return 0
        if not gated:
            return _room(
                self._waiters,
                free,
                self._held,
                self._estimate,
                self._other,
                self._settings,
                label,
                command,
                self._slots,
                moment=self._moment,
                window=self._window,
                p90=self._p90,
            )
        if not self._waiters and not self._used:
            return free
        max_count = free
        probe_meta: Entry = {"label": label, "cmd": command}
        group_caps = _group_caps_of(self._settings)
        if group_caps:
            active_groups = _count_active_groups(self._held, group_caps)
            if _group_cap_wait(probe_meta, active_groups, group_caps) is not None:
                return 0
        if self._used:
            kind, _ = self._host.memory
            allowance = kind == "warning" and float(self._settings.get("warning_admits", 1)) >= 1
            if kind != "normal":
                if not allowance or any(e.get(_entry.PRESSED) for e in self._held):
                    return 0
                max_count = 1
            share = float(self._settings.get("memory_share", _config.ORDER_DEFAULTS["memory_share"]))
            budget_mb = float(self._settings.get("memory_budget_mb", _config.ORDER_DEFAULTS["memory_budget_mb"]))
            if share > 0 or budget_mb > 0:
                ram_mb = self._host.ram_mb if share > 0 else None
                limit_mb, _ = _effective_memory_budget_mb(ram_mb, share, budget_mb)
                if limit_mb is not None:
                    held_mb = sum(self.expected_memory(e)[0] for e in self._held)
                    need_mb = self.expected_memory(probe_meta)[0]
                    if _budget_wait(held_mb, need_mb, ram_mb, share, budget_mb) is not None:
                        return 0
            load_gate = float(self._settings.get("load_gate", _config.ORDER_DEFAULTS["load_gate"]))
            if (
                load_gate > 0
                and _load_wait(self._host.averages, self._host.cores, self._slots, self._used, self._settings)
                is not None
            ):
                return 0
            if not self._waiters:
                return max_count
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._moment))
        for count in range(max_count, 0, -1):
            probe: Entry = {"count": count, "label": label, "cmd": command, "since": since}
            found = plan(
                self._waiters + [probe],
                self._held,
                self._slots,
                self._moment,
                self._settings["order"],
                self._estimate,
                self._other,
                settings=self._settings,
                window=self._window,
                p90=self._p90,
            )
            if not any(entry is probe for entry in found.grants):
                continue
            checked = _gate(
                found.grants,
                self._held,
                self._used,
                self._slots,
                self._host,
                self._settings,
                memory_estimate=self._memory_estimate,
                granted_counts=found.granted_counts,
            )
            if any(entry is probe and block is None for entry, _count, block in checked):
                return count
        return 0

    def dashboard_room(self) -> DashboardRoom:
        """Gated admission count, resource blocks, expected-peak budget totals, and head projection for the dashboard."""
        start: float | None = None
        reserved: float | None = None
        if self._settings["order"] == "backfill" and self._waiters:
            found = self._plan_with_head()
            if found.head is not None:
                start = found.start
                reserved = found.reserved
        memory_kind, memory_block = self._host.memory
        if memory_kind == "warning" and float(self._settings.get("warning_admits", 1)) >= 1:
            memory_block = WARNING_WAIT
        load_block = _load_wait(
            self._host.averages, self._host.cores, self._slots, min(self._used, self._slots), self._settings
        )
        share = float(self._settings.get("memory_share", _config.ORDER_DEFAULTS["memory_share"]))
        budget_mb = float(self._settings.get("memory_budget_mb", _config.ORDER_DEFAULTS["memory_budget_mb"]))
        ram_mb = self._host.ram_mb
        limit_mb, _ = _effective_memory_budget_mb(ram_mb, share, budget_mb)
        held_mb = sum(self.expected_memory(e)[0] for e in self._held)
        memory_budget_block: str | None = None
        if self._used and limit_mb is not None:
            need_mb = self.expected_memory({"label": "", "cmd": ""})[0]
            memory_budget_block = _budget_wait(held_mb, need_mb, ram_mb, share, budget_mb)
        memory_budget_mb = round(limit_mb, 1) if limit_mb is not None else None
        memory_expected_mb = round(held_mb, 1)
        admission = self.room(gated=True)
        return DashboardRoom(
            admission,
            memory_block,
            load_block,
            memory_budget_block,
            memory_expected_mb,
            memory_budget_mb,
            start,
            reserved,
        )

    def advise_block(self, label: str = "", command: str = "") -> AdviseBlock:
        """Queue room, waiting state, run estimate, and machine gates for `advise`."""
        queue_room = self.room(label, command, gated=False)
        waiting = bool(self._waiters) and queue_room == 0
        run_estimate = self._estimate({"label": label, "cmd": command}) if command else None
        memory_level, memory_short = self._host.memory
        load_block = (
            _load_wait(
                self._host.averages, self._host.cores, self._slots, min(self._used, self._slots), self._settings
            )
            if self._used
            else None
        )
        share = float(self._settings.get("memory_share", _config.ORDER_DEFAULTS["memory_share"]))
        budget_mb = float(self._settings.get("memory_budget_mb", _config.ORDER_DEFAULTS["memory_budget_mb"]))
        ram_mb = self._host.ram_mb
        limit_mb, _ = _effective_memory_budget_mb(ram_mb, share, budget_mb)
        held_mb = sum(self.expected_memory(e)[0] for e in self._held) if self._held else 0.0
        memory_budget_block: str | None = None
        if self._used and limit_mb is not None:
            need_mb = self.expected_memory({"label": label, "cmd": command})[0]
            memory_budget_block = _budget_wait(held_mb, need_mb, ram_mb, share, budget_mb)
        memory_budget_mb = round(limit_mb, 1) if limit_mb is not None else None
        memory_expected_mb = round(held_mb, 1)
        group_caps = _group_caps_of(self._settings)
        group_cap_block = (
            _group_cap_wait({"label": label, "cmd": command}, _count_active_groups(self._held, group_caps), group_caps)
            if group_caps
            else None
        )
        return AdviseBlock(
            queue_room,
            waiting,
            run_estimate,
            memory_level,
            memory_short,
            load_block,
            memory_budget_block,
            memory_budget_mb,
            memory_expected_mb,
            group_cap_block,
        )
