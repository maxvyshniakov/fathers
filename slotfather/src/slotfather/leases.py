"""The queue's transactions (fathers#37): each a short change under the queue lock, recorded by the one event writer
(events.record()): a new request (enqueue()), grant(), yield requests (ask(), ask_yield()), a yield
(yield_lease(), yield_for_lock(), lock_taken()), reprioritize() and release(). The client that waits for a grant
is `client`; the command runner is `runner`; the queue's reads are `queue`."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import time
from typing import Any, cast
import uuid

from . import config as _config
from . import entry as _entry
from . import events as _events
from . import idle as _idle
from . import platform as _platform
from . import process as _process
from . import queue as _queue
from . import schedule as _schedule
from . import settings as _settings
from . import state as _state


def ask(directory: Path, live: list[tuple[Path, dict[str, Any]]], held: list[dict[str, Any]],
        waiters: list[dict[str, Any]], estimate: _schedule.Estimate, other: int, settings: dict[str, Any],
        slots: int, moment: float, window: float) -> None:
    """Writes the yield requests yield_asks() wants and withdraws the others. Under the queue lock. `settings` are
    the saved ones without this client's environment, so that every client's look asks alike (a waiter whose
    client asks later or never says so on its entry); `slots`, `moment` and `window` are the look's (grant())."""
    paths = {id(e): path for path, e in live}
    wanted = _schedule.yield_asks(waiters, held, slots, moment, estimate, other, settings,
                        asked=[e for e in held if _state.marker(paths[id(e)]).exists()], window=window)
    asks = {id(h): w for h, w in wanted}
    # Idle leases (#12): a yieldable one is asked while a request waits that its slots would start.
    idle = [h for h in held if id(h) in paths and id(h) not in asks
            and _idle.idle_since(paths[id(h)], settings, moment) is not None]
    asks.update({id(h): w for h, w in _schedule.idle_asks(waiters, [h for h in held if id(h) not in asks], idle,
                                                          slots, moment, estimate, other, settings, window)})
    for path, entry in live:
        place = _state.marker(path)
        if id(entry) in asks:
            who = asks[id(entry)]
            if place.exists():
                continue
            _state.write(place, {"by": paths[id(who)].stem, "label": who.get("label"), "priority": _entry.priority_of(who),
                          "count": who["count"], "at": _config.now()})
            _events.record(directory, f"asked {entry.get('label')} (pid {entry['pid']}) to yield {entry['count']} for "
                           f"{who.get('label')}", "lease.yield_asked", _events.request(entry, path),
                         {"count": entry["count"], "for": {"id": paths[id(who)].stem, "label": who.get("label"),
                                                           "priority": _entry.priority_of(who)}},
                         _events.actor(kind="tool"))
        elif place.exists():
            if _entry.is_held(entry) and asked_by_hand(_state.asked(path), live):
                continue  # `slotfather ask-yield` asked it: it stands while the waiter it names still waits
            place.unlink(missing_ok=True)
            _events.record(directory, f"withdrew the yield request to {entry.get('label')} (pid {entry['pid']})", "lease.yield_withdrawn", _events.request(entry, path), {},
                         _events.actor(kind="tool"))
    for place in [*(directory / "queue").glob("*.yield"), *(directory / "queue").glob("*" + _idle.SUFFIX),
                  *(directory / "queue").glob("*.session")]:
        if not place.with_suffix(".json").exists():  # requests and idle records left by entries gone meanwhile
            place.unlink(missing_ok=True)


def asked_by_hand(request: dict[str, Any] | None, live: list[tuple[Path, dict[str, Any]]]) -> bool:
    """Whether a yield request came from `slotfather ask-yield` and the waiter it names still waits."""
    return bool(request and request.get("asker") and
                any(p.stem == request.get("by") and _entry.is_waiting(e) for p, e in live))


def same_process(table: _queue.Table, pid: int, start: str) -> bool:
    """Whether `pid` is still the process the caller saw, started at `start` (as `status --json` gives it: the
    entry's own `start`, or the query's)."""
    return pid in table and bool(start.strip()) and _platform.same_holder(start, None, pid, table[pid][1])


CLASS_RANK = {"batch": 0, "normal": 1, "interactive": 2}


def ask_yield(pid: int, start: str, directory: Path | None = None) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Asks the yieldable lease held by `pid`, started at `start`, to yield for the first waiter of a higher class
    that its slots would start, writing the marker a waiting interactive request writes, with the asker recorded.
    Nothing is stopped: the holder yields between its own steps. Returns (the lease, the request, whether it was
    asked before); raises SlotError for a pid that is now another process, holds no lease here, or a lease that
    cannot yield."""
    directory = directory or _config.state_dir()
    _state.require_own(directory, "a yield request")
    with _state.locked(directory):
        table = _queue.process_table(directory)
        live = _queue.reap(directory, table)
        if not same_process(table, pid, start):
            raise _config.SlotError(f"pid {pid} is not the process started at {start!r} any more: nothing was asked")
        found = next(((p, e) for p, e in live if e.get("pid") == pid and _entry.is_held(e)), None)
        if found is None:
            raise _config.SlotError(f"pid {pid} holds no lease of this queue")
        path, entry = found
        if not entry.get("yieldable"):
            raise _config.SlotError(f"{entry.get('label')} (pid {pid}) did not take --yieldable: it cannot yield")
        if int(entry.get("yields") or 0) >= _config.YIELDS_MAX:
            raise _config.SlotError(f"{entry.get('label')} (pid {pid}) has yielded {_config.YIELDS_MAX} times, the most")
        before = _state.asked(path)
        if before is not None and any(p.stem == before.get("by") and _entry.is_waiting(e) for p, e in live):
            return entry, before, True  # a request that stands; one naming a waiter gone is replaced below
        settings = _settings.order_settings(directory, environment=False)
        moment = time.time()
        own = CLASS_RANK[_entry.priority_of(entry)]  # its own class, as schedule.yield_asks() compares it
        waiters = [(p, e) for p, e in live if _entry.is_waiting(e)]
        ranked = _schedule.ranked([e for _, e in waiters], settings, moment)
        paths = {id(e): p for p, e in waiters}
        held = [e for p, e in live if _entry.is_held(e) and p != path]
        chosen = None
        for waiter in ranked:
            if CLASS_RANK[_schedule.effective_priority(waiter, settings, moment)] <= own:
                continue
            behind = _schedule.rank_time(waiter, settings, moment) + _config.YIELD_BEHIND
            trial = _schedule.plan([*(e for e in ranked), _entry.trial_yielded(entry, behind)], held,
                                   _settings.total(), moment, estimate=_queue.estimator(directory),
                                   other=_queue.used_elsewhere(directory, table), settings=settings,
                                   window=_config.overtake_window())
            if any(g is waiter for g in trial.grants):
                chosen = waiter
                break
        if chosen is None:
            raise _config.SlotError(f"no waiting request of a higher class than {entry.get('label')} would start in "
                                    f"its slots: nothing was asked")
        asker = _events.actor()
        request = {"by": paths[id(chosen)].stem, "label": chosen.get("label"),
                   "priority": _schedule.effective_priority(chosen, settings, moment), "count": chosen["count"],
                   "at": _config.now(), "asker": asker.get("via") or f"pid {asker.get('pid')}"}
        _state.write(_state.marker(path), request)
        _events.record(directory, f"asked {entry.get('label')} (pid {entry['pid']}) to yield {entry['count']} for "
                              f"{chosen.get('label')}", "lease.yield_asked", _events.request(entry, path),
                     {"count": entry["count"], "for": {"id": request["by"], "label": chosen.get("label"),
                                                       "priority": request["priority"]}, "by_hand": True}, asker)
    return entry, request, False


def yield_lease(path: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Yields the held lease at `path` if it was asked to and the waiter that asked still waits: the lease goes back
    among the waiters just behind that waiter and the queue grants at once; returns (its entry, the asker's). None,
    changing nothing but a stale request, when nothing asked. Its client then waits to be held again
    (client.yield_now())."""
    directory = path.parent.parent
    with _state.locked(directory):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            raise _config.SlotError("the lease vanished from the queue") from None
        request = _state.asked(path)
        if not _entry.is_held(entry) or request is None:
            return None
        live = _queue.reap(directory, _queue.process_table(directory))
        asker = next((e for p, e in live if p.stem == request.get("by") and _entry.is_waiting(e)), None)
        settings = _settings.order_settings(directory, environment=False)
        behind = _schedule.rank_time(asker, settings, time.time()) + _config.YIELD_BEHIND if asker is not None else None
        if asker is not None:  # would the asker start? Things may have changed since it asked.
            table = _queue.process_table(directory)
            held = [e for other, e in live if _entry.is_held(e) and other != path]
            waiters = [e for _, e in live if _entry.is_waiting(e)] + [_entry.trial_yielded(entry, behind)]
            trial = _schedule.plan(waiters, held, _settings.total(), time.time(), estimate=_queue.estimator(directory),
                         other=_queue.used_elsewhere(directory, table), settings=settings, window=_config.overtake_window())
            if not any(e is asker for e in trial.grants):
                asker = None
        if asker is None:  # it started, left or would still not start; the next look would withdraw the request
            _state.marker(path).unlink(missing_ok=True)
            _events.record(directory, f"withdrew the yield request to {entry.get('label')} (pid {entry.get('pid')})", "lease.yield_withdrawn", _events.request(entry, path), {}, _events.holder(entry))
            return None
        _entry.to_yielded(entry, time.time(), behind)
        _state.write(path, entry)
        _state.marker(path).unlink(missing_ok=True)
        _events.record(directory, f"yielded {entry['count']} ({path.name}) to {asker.get('label')}", "lease.yielded", _events.request(entry, path),
                     {"count": entry["count"], "to": {"id": request.get("by"), "label": asker.get("label")},
                      "yields": entry["yields"]}, _events.holder(entry))
        with contextlib.suppress(_config.SlotError):  # held_again()'s looks grant it then
            grant(directory, _queue.process_table(directory))
    return entry, asker


def reprioritize(target: str, priority: str, directory: Path | None = None,
                 start: str | None = None) -> list[dict[str, Any]]:
    """Moves the requests `target` names (a pid, or the label of exactly one request) to class `priority`, keeping
    their arrival (`since`): a waiter keeps its place among the requests that came before and after it, now ranked
    by its new class. Only the owner of a request may move it (the same uid as its holder). Records it on the entry
    (`reprioritized`: the class it asked first), in queue.log and in the history line, then grants at once. Returns
    the entries as they are now; raises SlotError for a target that names none, or several, or another user's.
    With `start`, the target is a pid that must still be the process started then (as `status --json` gives it)."""
    if priority not in _config.PRIORITIES:
        raise _config.SlotError(f"priority is one of {', '.join(_config.PRIORITIES)}, not {priority!r}")
    directory = directory or _config.state_dir()
    _state.require_own(directory, "a priority change")
    changed: list[dict[str, Any]] = []
    with _state.locked(directory):
        table = _queue.process_table(directory)
        live = _queue.reap(directory, table)
        if start is not None and not (target.isdigit() and same_process(table, int(target), start)):
            raise _config.SlotError(f"pid {target} is not the process started at {start!r} any more: nothing changed"
                                    if target.isdigit() else "--start takes the request's pid, not its label")
        if target.isdigit():
            chosen = [(p, e) for p, e in live if e.get("pid") == int(target)]
        else:
            # the label, or the --label it was given before a derived one replaced it (labels.py)
            chosen = [(p, e) for p, e in live if target in (e.get("label"), e.get("label_given"))]
            if len(chosen) > 1:
                waiting = [(p, e) for p, e in chosen if _entry.is_waiting(e)]
                if len(waiting) != 1:
                    pids = ", ".join(str(e.get("pid")) for _, e in chosen)
                    raise _config.SlotError(f"{len(chosen)} requests are labelled {target} (pids {pids}): name the pid")
                chosen = waiting
        if not chosen:
            raise _config.SlotError(f"no request in the queue has pid or label {target}")
        me = os.geteuid()
        for _, entry in chosen:
            owner = _process.process_uid(entry.get("pid"))
            if owner != me:
                raise _config.SlotError(f"{entry.get('label')} (pid {entry.get('pid')}) runs as uid {owner}: only its "
                                        f"owner may change its priority")
        for path, entry in chosen:
            before = _entry.priority_of(entry)
            if before == priority:
                changed.append(entry)
                continue
            _entry.to_reprioritized(entry, priority, time.time())
            _state.write(path, entry)
            _events.record(directory, f"reprioritized {entry.get('label')} (pid {entry.get('pid')}) from {before} to "
                                  f"{priority}", "request.reprioritized", _events.request(entry, path),
                         {"from": before, "to": priority, "state": entry.get("state")})
            changed.append(entry)
        with contextlib.suppress(_config.SlotError):  # the change stands; the waiters' looks grant by it
            grant(directory, _queue.process_table(directory))
    return changed


def yield_for_lock(path: Path, lock: str) -> bool:
    """Gives up the slots of the held lease at `path` while its command waits to lock `lock` (fathers#159): the
    lease goes back among the waiters in its own place, marked with the lock (entry.to_lock_waiting()), so that
    nothing grants it while it waits, and the queue grants at once. Returns False, changing nothing, when the lease
    is not held (it waits already, or is gone)."""
    directory = path.parent.parent
    with _state.locked(directory):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        if not _entry.is_held(entry):
            return False
        _entry.to_lock_waiting(entry, time.time(), lock)
        _state.write(path, entry)
        _state.marker(path).unlink(missing_ok=True)  # a yield request is moot: its slots are free now
        _events.record(directory, f"yielded {entry['count']} ({path.name}) while waiting for lock {lock}", "lease.yielded", _events.request(entry, path),
                     {"count": entry["count"], "to": {"id": None, "label": f"lock {lock}"}, "lock": lock,
                      "yields": int(entry.get("yields") or 0)}, _events.holder(entry))
        with contextlib.suppress(_config.SlotError):  # the next look grants the freed slots otherwise
            grant(directory, _queue.process_table(directory))
    return True


def lock_taken(path: Path, wait: bool = True) -> None:
    """The lock the yielded lease at `path` waited for is taken (`wait`: its client waits to be granted again,
    client.held_again()) or its wait ended: it becomes an ordinary yielded lease in its own place."""
    directory = path.parent.parent
    with _state.locked(directory):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            raise _config.SlotError("the lease vanished from the queue") from None
        if _entry.lock_of(entry) is not None:
            lock = _entry.lock_of(entry)
            _entry.lock_taken(entry)
            _state.write(path, entry)
            _events.record(directory, f"took lock {lock} for {entry.get('label')} (pid {entry.get('pid')}); waiting for "
                                  f"{entry['count']} slot(s) again" if wait else
                       f"stopped waiting for lock {lock} for {entry.get('label')} (pid {entry.get('pid')})")
            with contextlib.suppress(_config.SlotError):
                grant(directory, _queue.process_table(directory))


def grant_now(directory: Path) -> None:
    """Grant what a settings change lets start, as a release does (settings.configure()'s `granted`); callers hold
    the queue lock."""
    grant(directory, _queue.process_table(directory))


def grant(directory: Path, table: _queue.Table) -> str | None:
    """Grant the waiters plan() starts, in their order, that memory lets start (schedule.memory_gate()); then ask yieldable holders to yield for
    interactive waiters that still wait (yield_asks()). Returns why the first that does not start still waits."""
    live = _queue.reap(directory, table)
    held, waiters = _entry.split(e for _, e in live)
    paths = {id(e): path for path, e in live}
    estimate = _queue.estimator(directory)
    other = _queue.used_elsewhere(directory, table)
    slots = _settings.total()
    moment = time.time()
    settings = _settings.order_settings(directory)
    window = _config.overtake_window()
    p90 = _queue.estimator(directory, p90=True)
    found = _schedule.plan(_schedule.fit(waiters, slots), held, slots, moment, estimate=estimate, other=other,
                 settings=settings, window=window, p90=p90)
    # The head's reservation, kept on its entry so that the next look promises no later start; others hold none.
    # Every client keeps it by the shared settings, whatever its environment (shared_plan()).
    keeper = _schedule.shared_plan(found, waiters, held, slots, moment, estimate, other, settings,
                                   _settings.order_settings(directory, environment=False), window, p90)
    used = sum(_entry.holds(e, slots) for e in held) + other
    memory = _platform.memory_level() if found.grants else ("normal", None)
    gated = _schedule.memory_gate(found.grants, held, used, memory, settings)
    starting = [entry for entry, why in gated if not why]
    for entry in _schedule.reservations(waiters, keeper):
        if not any(entry is g for g in starting):  # a grant is written below; one memory holds back keeps its own
            _state.write(paths[id(entry)], entry)
    gave_way, marked = _schedule.head_drops(waiters, keeper, moment)
    for entry in marked:
        if not any(entry is g for g in starting):
            _state.write(paths[id(entry)], entry)
    for entry in gave_way:  # its client hears it in its waiting reason; EVENTS and queue.log name the leases
        blockers = next(b for e, b in keeper.dropped if e is entry)
        names = ", ".join(f"{b.get('label')} (pid {b.get('pid')})" for b in blockers)
        late = round(moment - (_entry.reservation(entry) or moment))
        _events.record(directory, f"{entry.get('label')} (pid {entry.get('pid')}) gave way as the head: its reserved "
                                  f"start passed {late} s ago while leases with no end estimate hold its slots: "
                                  f"{names}",
                       "request.head_dropped", _events.request(entry, paths[id(entry)]),
                       {"count": entry["count"], "late": late,
                        "blocked_by": [{"id": paths[id(b)].stem if id(b) in paths else None, "label": b.get("label"),
                                        "pid": b.get("pid")} for b in blockers]},
                       _events.actor(kind="tool"), level="warn")
    short = None
    for entry, why in gated:
        if why:
            short = short or why
            continue
        served = _schedule.effective_priority(entry, settings, moment)
        back = _entry.epoch(entry.get("yielded_at"))  # a yielded lease waited from when it went back
        waited_from = back if back is not None else _config.seconds(entry.get("since"))
        granted = _config.now()
        began = _config.seconds(granted) or time.time()  # resume() puts a yielded lease's first grant back
        _entry.to_held(entry, granted, time.time(), pressed=memory[0] != "normal", served=served)
        _state.write(paths[id(entry)], entry)
        used += entry["count"]
        details: dict[str, Any] = {"count": entry["count"], "priority": _entry.priority_of(entry),
                                   "waited": None if waited_from is None else
                                   round(max(0.0, began - waited_from))}
        if served != _entry.priority_of(entry):
            details["served"] = served
        if back is not None:
            details["resumed"] = True
        meta = _entry.meta_of(entry)
        if meta:
            details["meta"] = meta
        if isinstance(entry.get("session"), dict):
            details["session"] = entry["session"].get("name")
        _events.record(directory, f"granted {entry['count']} to {entry.get('label')} (pid {entry['pid']})",
                       "lease.granted", _events.request(entry, paths[id(entry)]), details, _events.holder(entry))
    if short:  # memory holds a request back: no yield is asked, since the slots it frees might not start it
        return short
    rest = [e for e in waiters if not any(e is g for g in found.grants)]
    with contextlib.suppress(OSError, _config.SlotError):
        ask(directory, live, held + found.grants, rest, estimate, other, _settings.order_settings(directory, environment=False),
            _settings.total(), time.time(), window)
    return found.reason


def next_sequence(directory: Path) -> int:
    """The next queue sequence, under the queue lock, written whole (state.write()). It is past every entry in the
    queue too: a torn or lost `sequence` file (an older client writes it in place) must not put a new request
    ahead of those already waiting (fathers#123). Older clients read the file with int(), which takes "42\n"."""
    try:
        sequence = int((directory / "sequence").read_text() or 0)
    except (OSError, ValueError):
        sequence = 0
    for path in (directory / "queue").glob("*.json"):
        number = path.name.split("-", 1)[0]
        if number.isdigit():
            sequence = max(sequence, int(number))
    _state.write(directory / "sequence", sequence + 1)
    return sequence + 1


def enqueue(directory: Path, count: int | str, table: _queue.Table, label: str, command: str, cwd: str,
            priority: str, ticket: str | None, meta: dict[str, str], naming: dict[str, str] | None,
            session: dict[str, Any] | None, git: dict[str, Any] | None, yieldable: bool,
            wait: float | None) -> tuple[Path, int]:
    """A new request of this process in the queue at `directory`: (its entry's path, the slots it asks, the
    capacity at most). Admission and capacity changes share the queue lock; `all` resolves only now (and again at
    the grant: plan()). A live request of the same session name refuses it."""
    me = os.getpid()
    with _state.locked(directory):
        if session is not None:
            fresh = _process.processes()
            for _, other in _state.entries(directory):
                spec = other.get("session")
                same = isinstance(spec, dict) and cast(dict[str, Any], spec).get("name") == session["name"]
                if same and _platform.alive(other, fresh):
                    raise _config.SlotError(f"session {session['name']} exists (pid {other.get('pid')}, "
                                            f"{other.get('state')}): stop it first (slotfather session stop "
                                            f"{session['name']})")
        whole = count == "all"
        count = _settings.total() if whole else min(cast(int, count), _settings.total())
        sequence = next_sequence(directory)
        path = directory / "queue" / f"{sequence:010d}-{me}.json"
        own = _settings.order_settings(directory)["yield_after"]
        entry = _entry.new_request(
            me, table.get(me, (0, ""))[1], count, label, command, cwd, _config.now(), priority,
            uuid.uuid4().hex, started=_platform.start_epoch(me), ticket=ticket, whole=whole, meta=meta,
            naming=naming, session=session, git=git, yieldable=yieldable,
            # when this client gives up (plan(): a head that would give up before its expected start holds nobody back)
            until=None if wait is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + wait)),
            # this client asks later or never (SLOTFATHER_YIELD_AFTER)
            yield_after=own if own != _settings.order_settings(directory, environment=False)["yield_after"] else None)
        _state.write(path, entry)
    return path, count


def release(path: Path, count: int, status: int | None, usage: dict[str, Any] | None) -> None:
    """Takes the request at `path` out of the queue, records it in the history (released, or left when it never
    ran), and hands its slots on at once. Its outliers and trends are judged after, outside the queue lock."""
    directory = path.parent.parent
    expected = None
    with contextlib.suppress(Exception):  # before the lock: the series may need reading (runtype.py)
        from . import runtype as _runtype
        expected = _runtype.expected_of(json.loads(path.read_text()), _runtype.cached(directory))
    item = None
    with _state.locked(directory):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            entry = None
        path.unlink(missing_ok=True)
        _events.record(directory, f"released {count} ({path.name})")  # queue.log first, before the history
        _state.marker(path).unlink(missing_ok=True)
        _idle.path(path).unlink(missing_ok=True)
        if isinstance(entry, dict):
            entry = cast(dict[str, Any], entry)
            outcome = _entry.outcome(entry)  # a yielded lease ran
            item = _state.record(directory, entry, outcome, status, usage, path=path, expected=expected,
                                 capacity=_settings.total, above=_queue.run_above)
            if outcome == "released":
                _events.record(directory, None, "lease.released", _events.request(entry, path),
                               {"count": count, "exit": item.get("status"), "ran": item.get("ran"),
                                "cpu": item.get("cpu"), "rss_peak_mb": item.get("rss_peak_mb")},
                               _events.holder(entry))
            else:
                _events.record(directory, None, "request.left", _events.request(entry, path),
                               {"count": count, "gave_up_after": item.get("waited")}, _events.holder(entry))
        else:  # the entry was gone already: queue.log has its release, nothing more is known
            _events.record(directory, None, "lease.released", {"type": "request", "id": path.stem},
                           {"count": count, "unknown": True})
        # Hand the slots on now: waiters look at the queue less often the longer they wait.
        with contextlib.suppress(_config.SlotError, OSError):
            grant(directory, _queue.process_table(directory))
    if item is not None and item.get("run_key"):
        from . import runtype as _runtype
        _runtype.after_release(directory, item)  # outliers and trends, outside the queue lock


def waiting_long(directory: Path, path: Path, entry: dict[str, Any], line: str, facts: dict[str, Any]) -> None:
    """Records that the request at `path` has waited the warn time (queue.log and EVENTS)."""
    with _state.locked(directory):
        _events.record(directory, f"waiting long: {line}", "request.waiting_long", _events.request(entry, path), facts,
                       _events.holder(entry), level="warn")
