"""Slotfather leases responsibilities."""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any, cast
import uuid

from . import config as _config
from . import entry as _entry
from . import events as _events
from . import facts as _facts
from . import idle as _idle
from . import platform as _platform
from . import query as _query
from . import schedule as _schedule
from . import state as _state


Table = dict[int, tuple[int, str]]  # the process table: pid -> (parent pid, start time)


_skipping: list[str] = []  # the reason this process last logged a skipped look for, while ps stays unusable


def process_table(directory: Path, log: bool = True) -> Table:
    """platform.processes() for a look that reaps or sweeps the queue at `directory`. When ps gives no table that
    can be trusted (config.ProcessTableError, a SlotError) the look reaps nothing and this raises; the caller skips
    that pass (a waiter's next look tries again). With `log`, a look that changes the queue says so in queue.log
    and the event log once per failure streak of this process (`skipped reaping`, reap.skipped), and once when ps
    answers again (`reaping resumed`, reap.resumed). Read-only looks (status, the dashboard) pass log=False: they
    must not flood the log a lasting failure fills already (fathers#121 review)."""
    own = _state.foreign_owner(directory) is None
    try:
        table = _platform.processes()
    except _config.ProcessTableError as error:
        if log and own and not _skipping:
            _skipping.append(str(error))
            _state.log(directory, f"skipped reaping: {error}")
            _events.emit(directory, "reap.skipped", {"type": "queue", "id": "processes"}, {"reason": str(error)},
                         _events.actor(kind="tool"), level="warn")
        raise
    if log and own and _skipping:
        reason = _skipping.pop()
        _state.log(directory, "reaping resumed: ps answers again")
        _events.emit(directory, "reap.resumed", {"type": "queue", "id": "processes"}, {"after": reason},
                     _events.actor(kind="tool"))
    return table


def reap(directory: Path, table: Table) -> list[tuple[Path, dict[str, Any]]]:
    """Live entries in queue order; removes those whose holder exited or crashed (in another user's queue it only
    leaves them out). An entry this client does not know (entry.unknown(); fathers#19) is in neither: it is left
    alone, never reaped, and the queue is planned as if it were absent."""
    live: list[tuple[Path, dict[str, Any]]] = []
    readonly = _state.foreign_owner(directory) is not None
    for path, entry in _state.entries(directory):
        if _entry.unknown(entry) is not None:
            continue  # perhaps a newer client's live lease: `status` shows it (state.unknown())
        if _platform.alive(entry, table):
            live.append((path, entry))
        elif not readonly:
            path.unlink(missing_ok=True)
            _state.marker(path).unlink(missing_ok=True)
            _idle.path(path).unlink(missing_ok=True)
            path.with_suffix(".session").unlink(missing_ok=True)  # a session's runs (session.py)
            _state.log(directory, f"reaped {entry.get('state')} lease of {entry.get('count')} for "
                           f"{entry.get('label')} (pid {entry.get('pid')} exited)")
            item = _state.record(directory, entry, _entry.outcome(entry, reaped=True), path=path)
            _events.emit(directory, "lease.reaped" if _entry.is_held(entry) else "request.reaped",
                         _events.request(entry, path),
                         {"count": entry.get("count"), "ran": item.get("ran"), "waited": item.get("waited"),
                          "reason": "owner exited"}, _events.actor(kind="tool"), level="warn")
    return live


def legacy_holders(table: Table) -> list[tuple[Path, dict[str, Any], int]]:
    """Held leases with a live holder in the earlier queues: (path, entry, slots here). Read only: their own
    clients reap them."""
    found: list[tuple[Path, dict[str, Any], int]] = []
    for directory in _config.legacy_dirs():
        for path, entry in _state.entries(directory):
            if _entry.is_held(entry) and _platform.alive(entry, table):
                found.append((path, entry, min(_config.total(), math.ceil(entry["count"] * _config.total() / _config.LEGACY_TOTAL))))
    return found


def legacy_used(table: Table) -> int:
    return sum(count for _, _, count in legacy_holders(table))


def ask(directory: Path, live: list[tuple[Path, dict[str, Any]]], held: list[dict[str, Any]],
        waiters: list[dict[str, Any]], estimate: _schedule.Estimate, other: int, settings: dict[str, Any]) -> None:
    """Writes the yield requests yield_asks() wants and withdraws the others. Under the queue lock. `settings` are
    the saved ones without this client's environment, so that every client's look asks alike (a waiter whose
    client asks later or never says so on its entry)."""
    paths = {id(e): path for path, e in live}
    moment, slots = time.time(), _config.total()
    wanted = _schedule.yield_asks(waiters, held, slots, moment, estimate, other, settings,
                        asked=[e for e in held if _state.marker(paths[id(e)]).exists()])
    asks = {id(h): w for h, w in wanted}
    # Idle leases (#12): a yieldable one is asked while a request waits that its slots would start.
    idle = [h for h in held if id(h) in paths and id(h) not in asks
            and _idle.idle_since(paths[id(h)], settings, moment) is not None]
    asks.update({id(h): w for h, w in _schedule.idle_asks(waiters, [h for h in held if id(h) not in asks], idle,
                                                          slots, moment, estimate, other, settings)})
    for path, entry in live:
        place = _state.marker(path)
        if id(entry) in asks:
            who = asks[id(entry)]
            if place.exists():
                continue
            _state.write(place, {"by": paths[id(who)].stem, "label": who.get("label"), "priority": _entry.priority_of(who),
                          "count": who["count"], "at": _config.now()})
            _state.log(directory, f"asked {entry.get('label')} (pid {entry['pid']}) to yield {entry['count']} for "
                           f"{who.get('label')}")
            _events.emit(directory, "lease.yield_asked", _events.request(entry, path),
                         {"count": entry["count"], "for": {"id": paths[id(who)].stem, "label": who.get("label"),
                                                           "priority": _entry.priority_of(who)}},
                         _events.actor(kind="tool"))
        elif place.exists():
            if _entry.is_held(entry) and asked_by_hand(_state.asked(path), live):
                continue  # `slotfather ask-yield` asked it: it stands while the waiter it names still waits
            place.unlink(missing_ok=True)
            _state.log(directory, f"withdrew the yield request to {entry.get('label')} (pid {entry['pid']})")
            _events.emit(directory, "lease.yield_withdrawn", _events.request(entry, path), {},
                         _events.actor(kind="tool"))
    for place in [*(directory / "queue").glob("*.yield"), *(directory / "queue").glob("*" + _idle.SUFFIX),
                  *(directory / "queue").glob("*.session")]:
        if not place.with_suffix(".json").exists():  # requests and idle records left by entries gone meanwhile
            place.unlink(missing_ok=True)


def asked_by_hand(request: dict[str, Any] | None, live: list[tuple[Path, dict[str, Any]]]) -> bool:
    """Whether a yield request came from `slotfather ask-yield` and the waiter it names still waits."""
    return bool(request and request.get("asker") and
                any(p.stem == request.get("by") and _entry.is_waiting(e) for p, e in live))


def same_process(table: Table, pid: int, start: str) -> bool:
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
        table = process_table(directory)
        live = reap(directory, table)
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
        settings = _schedule.order_settings(directory, environment=False)
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
                                   _config.total(), moment, estimate=_schedule.estimator(directory),
                                   other=legacy_used(table), settings=settings)
            if any(g is waiter for g in trial.grants):
                chosen = waiter
                break
        if chosen is None:
            raise _config.SlotError(f"no waiting request of a higher class than {entry.get('label')} would start in "
                                    f"its slots: nothing was asked")
        asker = _events.actor()
        request = {"by": paths[id(chosen)].stem, "label": chosen.get("label"),
                   "priority": _schedule.effective_priority(chosen, settings, moment), "count": chosen["count"],
                   "at": _config.now(), "asker": asker.get("via") or f"pid {asker['pid']}"}
        _state.write(_state.marker(path), request)
        _state.log(directory, f"asked {entry.get('label')} (pid {entry['pid']}) to yield {entry['count']} for "
                              f"{chosen.get('label')}")
        _events.emit(directory, "lease.yield_asked", _events.request(entry, path),
                     {"count": entry["count"], "for": {"id": request["by"], "label": chosen.get("label"),
                                                       "priority": request["priority"]}, "by_hand": True}, asker)
    return entry, request, False


def yield_lease(path: Path, out=sys.stderr) -> bool:
    """Yields the held lease at `path` if it was asked to and the waiter that asked still waits: the lease goes back
    among the waiters just behind that waiter, the queue grants at once, and this returns True when the lease is
    held again. Returns False at once when nothing asked."""
    directory = path.parent.parent
    with _state.locked(directory):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            raise _config.SlotError("the lease vanished from the queue") from None
        request = _state.asked(path)
        if not _entry.is_held(entry) or request is None:
            return False
        live = reap(directory, process_table(directory))
        asker = next((e for p, e in live if p.stem == request.get("by") and _entry.is_waiting(e)), None)
        settings = _schedule.order_settings(directory, environment=False)
        behind = _schedule.rank_time(asker, settings, time.time()) + _config.YIELD_BEHIND if asker is not None else None
        if asker is not None:  # would the asker start? Things may have changed since it asked.
            table = process_table(directory)
            held = [e for p, e in live if _entry.is_held(e) and p != path]
            waiters = [e for p, e in live if _entry.is_waiting(e)] + [_entry.trial_yielded(entry, behind)]
            trial = _schedule.plan(waiters, held, _config.total(), time.time(), estimate=_schedule.estimator(directory),
                         other=legacy_used(table), settings=settings)
            if not any(e is asker for e in trial.grants):
                asker = None
        if asker is None:  # it started, left or would still not start; the next look would withdraw the request
            _state.marker(path).unlink(missing_ok=True)
            _state.log(directory, f"withdrew the yield request to {entry.get('label')} (pid {entry.get('pid')})")
            _events.emit(directory, "lease.yield_withdrawn", _events.request(entry, path), {}, _events.holder(entry))
            return False
        _entry.to_yielded(entry, time.time(), behind)
        _state.write(path, entry)
        _state.marker(path).unlink(missing_ok=True)
        _state.log(directory, f"yielded {entry['count']} ({path.name}) to {asker.get('label')}")
        _events.emit(directory, "lease.yielded", _events.request(entry, path),
                     {"count": entry["count"], "to": {"id": request.get("by"), "label": asker.get("label")},
                      "yields": entry["yields"]}, _events.holder(entry))
        with contextlib.suppress(_config.SlotError):  # held_again()'s looks grant it then
            grant(directory, process_table(directory))
    print(f"slot: {entry.get('label')} yielded {entry['count']} slot(s) to {asker.get('label')}; it resumes when "
          f"they are free", file=out, flush=True)
    held_again(path)
    print(f"slot: {entry.get('label')} resumes with {entry['count']} slot(s)", file=out, flush=True)
    return True


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
        table = process_table(directory)
        live = reap(directory, table)
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
            owner = _platform.process_uid(entry.get("pid"))
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
            _state.log(directory, f"reprioritized {entry.get('label')} (pid {entry.get('pid')}) from {before} to "
                                  f"{priority}")
            _events.emit(directory, "request.reprioritized", _events.request(entry, path),
                         {"from": before, "to": priority, "state": entry.get("state")})
            changed.append(entry)
        with contextlib.suppress(_config.SlotError):  # the change stands; the waiters' looks grant by it
            grant(directory, process_table(directory))
    return changed


def held_again(path: Path) -> None:
    """Waits until the yielded lease at `path` is granted again, looking at the queue as a waiter does."""
    directory = path.parent.parent
    poll = float(_config.setting("POLL") or 2)
    poll_max = max(poll, float(_config.setting("POLL_MAX") or _config.POLL_MAX))
    interval, look = poll, 0.0
    while True:
        if time.monotonic() >= look:
            with _state.locked(directory):
                with contextlib.suppress(_config.SlotError):  # no table to trust: this look reaps and grants nothing
                    grant(directory, process_table(directory))
                try:
                    entry = json.loads(path.read_text())
                except (OSError, ValueError):
                    raise _config.SlotError("the lease vanished from the queue") from None
                if _entry.is_held(entry):
                    if "yielded_at" in entry:  # an older client granted it
                        _entry.resume(entry)
                        _state.write(path, entry)
                    return
            interval = min(interval * 1.5, poll_max)
            look = time.monotonic() + interval
        try:
            if _entry.is_held(json.loads(path.read_text())):
                look = 0.0  # held: settle it under the lock at once
                continue
        except (OSError, ValueError):
            raise _config.SlotError("the lease vanished from the queue") from None
        time.sleep(min(poll, max(0.0, look - time.monotonic())))


def grant(directory: Path, table: Table) -> str | None:
    """Grant the waiters plan() starts, in their order, that memory lets start (schedule.memory_gate()); then ask yieldable holders to yield for
    interactive waiters that still wait (yield_asks()). Returns why the first that does not start still waits."""
    live = reap(directory, table)
    held, waiters = _entry.split(e for _, e in live)
    paths = {id(e): path for path, e in live}
    estimate = _schedule.estimator(directory)
    other = legacy_used(table)
    slots = _config.total()
    moment = time.time()
    settings = _schedule.order_settings(directory)
    found = _schedule.plan(_schedule.fit(waiters, slots), held, slots, moment, estimate=estimate, other=other,
                 settings=settings)
    # The head's reservation, kept on its entry so that the next look promises no later start; others hold none.
    # Every client keeps it by the shared settings, whatever its environment (shared_plan()).
    keeper = _schedule.shared_plan(found, waiters, held, slots, moment, estimate, other, settings,
                                   _schedule.order_settings(directory, environment=False))
    used = sum(_entry.holds(e, slots) for e in held) + other
    memory = _platform.memory_level() if found.grants else ("normal", None)
    gated = _schedule.memory_gate(found.grants, held, used, memory, settings)
    starting = [entry for entry, why in gated if not why]
    for entry in _schedule.reservations(waiters, keeper):
        if not any(entry is g for g in starting):  # a grant is written below; one memory holds back keeps its own
            _state.write(paths[id(entry)], entry)
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
        _state.log(directory, f"granted {entry['count']} to {entry.get('label')} (pid {entry['pid']})")
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
        _events.emit(directory, "lease.granted", _events.request(entry, paths[id(entry)]), details,
                     _events.holder(entry))
    if short:  # memory holds a request back: no yield is asked, since the slots it frees might not start it
        return short
    rest = [e for e in waiters if not any(e is g for g in found.grants)]
    with contextlib.suppress(OSError, _config.SlotError):
        ask(directory, live, held + found.grants, rest, estimate, other, _schedule.order_settings(directory, environment=False))
    return found.reason


class Lease:
    """A held lease, an inherited one, or none (bypass). Release it once the work has finished."""

    def __init__(self, count: int, path: Path | None = None, inherited: bool = False, yieldable: bool = False):
        self.count, self.path, self.inherited, self.yieldable = count, path, inherited, yieldable
        self.status: int | None = None  # the command's exit status, for the history, where the holder knows it
        # what the command's process tree used (TreeUsage.result), where the holder measured it
        self.usage: dict[str, Any] | None = None

    def environment(self) -> dict[str, str]:
        if self.path is None:
            return {}
        env = {}
        for prefix in _config.LEASE_ENV_PREFIXES:
            env.update({prefix + "LEASE": str(self.path), prefix + "COUNT": str(self.count)})
            if self.yieldable and not self.inherited:
                env[prefix + "YIELD"] = str(_state.marker(self.path))
        return env

    def yield_requested(self) -> dict | None:
        """The yield request for this lease, or None: {"by", "label", "class", "count", "at"} of the waiter that
        asked. Cheap (one stat and a small read); call it between steps."""
        return None if self.path is None or self.inherited else _state.asked(self.path)

    def yield_slots(self, out=sys.stderr) -> bool:
        """Yields the lease if it was asked to: back among the waiters, just behind the waiter that asked, until it is
        granted again; returns True then, False at once when nothing asked (or the asker no longer waits). Call it
        only between steps, with none of the lease's own work running."""
        if self.path is None or self.inherited:
            return False
        return yield_lease(self.path, out=out)

    def release(self) -> None:
        if self.path is not None and not self.inherited:
            directory = self.path.parent.parent
            expected = None
            with contextlib.suppress(Exception):  # before the lock: the series may need reading (runtype.py)
                from . import runtype as _runtype
                expected = _runtype.expected_of(json.loads(self.path.read_text()), _runtype.cached(directory))
            item = None
            with _state.locked(directory):
                try:
                    entry = json.loads(self.path.read_text())
                except (OSError, ValueError):
                    entry = None
                self.path.unlink(missing_ok=True)
                _state.log(directory, f"released {self.count} ({self.path.name})")
                _state.marker(self.path).unlink(missing_ok=True)
                _idle.path(self.path).unlink(missing_ok=True)
                if isinstance(entry, dict):
                    entry = cast(dict[str, Any], entry)
                    outcome = _entry.outcome(entry)  # a yielded lease ran
                    item = _state.record(directory, entry, outcome, self.status, self.usage,
                                         path=self.path, expected=expected)
                    if outcome == "released":
                        _events.emit(directory, "lease.released", _events.request(entry, self.path),
                                     {"count": self.count, "exit": item.get("status"), "ran": item.get("ran"),
                                      "cpu": item.get("cpu"), "rss_peak_mb": item.get("rss_peak_mb")},
                                     _events.holder(entry))
                    else:
                        _events.emit(directory, "request.left", _events.request(entry, self.path),
                                     {"count": self.count, "gave_up_after": item.get("waited")}, _events.holder(entry))
                else:  # the entry was gone already: queue.log has its release, nothing more is known
                    _events.emit(directory, "lease.released", {"type": "request", "id": self.path.stem},
                                 {"count": self.count, "unknown": True})
                # Hand the slots on now: waiters look at the queue less often the longer they wait.
                with contextlib.suppress(_config.SlotError, OSError):
                    grant(directory, process_table(directory))
            if item is not None and item.get("run_key"):
                from . import runtype as _runtype
                _runtype.after_release(directory, item)  # outliers and trends, outside the queue lock
            self.path = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


def inherited_lease(table: Table) -> tuple[Path, dict[str, Any], int] | None:
    """The held lease this process runs under, here or in an earlier queue: (path, entry, slots here)."""
    mine = _platform.ancestors(table, os.getpid())
    own = _config.setting("LEASE")
    for path, entry in _state.entries(_config.state_dir()):
        mine_now = _entry.phase(entry).name in (_entry.HELD, _entry.YIELDED)  # a yielded lease is still this tree's
        if mine_now and _platform.alive(entry, table) and (own == str(path) or entry["pid"] in mine):
            return path, entry, entry["count"]
    for path, entry, count in legacy_holders(table):
        if own == str(path) or entry["pid"] in mine:
            return path, entry, count
    return None


def inherited(table: Table) -> Lease | None:
    found = inherited_lease(table)
    if found is None:
        return None
    path, entry, count = found
    if "yielded_at" in entry:
        held_again(path)  # it yielded: what runs under it waits until it is granted again
    # A lease in an earlier queue is honoured but not handed on: nested runs find its holder among their ancestors.
    return Lease(count, path if path.parent.parent == _config.state_dir() else None, inherited=True)


def policy() -> str:
    value = _config.setting("POLICY") or "normal"
    if value not in _config.POLICIES:
        raise _config.SlotError(f"invalid SLOTFATHER_POLICY {value!r}: use {', '.join(_config.POLICIES)}")
    return value


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


def acquire(count: int | str, label: str, command: str = "", out=sys.stderr, wait: float | None = None,
            note: str = "", meta: dict[str, str] | None = None, yieldable: bool = False, *,
            priority: str | None = None, ticket: str | int | None = None,
            session: dict[str, Any] | None = None, naming: dict[str, str] | None = None) -> Lease:
    """Wait in the queue for `count` slots (at most the total); the lease is held by this process until released.
    Raises SlotTimeout, having left the queue, when `wait` seconds pass first. `note` follows the first word that
    the request waits. `meta` (key -> text) goes on the queue entry and the history line; a process that inherits
    a lease keeps that lease's metadata and adds none. A `yieldable` lease may be asked to yield for interactive
    waiters (see Lease.yield_slots(); the README's client protocol); it never is otherwise. `session` makes the
    lease a measurement session's (session.py): {"name", "idle", and "expect" where given}; a live request of the
    same session name refuses it."""
    if count != "all" and (type(count) is not int or count < 1):
        raise _config.SlotError("count must be a positive integer or all")
    meta = _entry.checked_meta(meta)
    if priority is not None and priority not in _config.PRIORITIES:
        raise _config.SlotError(f"priority is one of {', '.join(_config.PRIORITIES)}")
    selected_priority = priority if priority is not None else "normal"
    selected_ticket = _entry.checked_ticket(ticket)
    if policy() == "bypass":
        return Lease(_config.total())
    directory = _config.state_dir()
    table = _platform.processes()
    found = inherited(table)
    if found:
        return found
    poll = float(_config.setting("POLL") or 2)
    poll_max = max(poll, float(_config.setting("POLL_MAX") or _config.POLL_MAX))
    me = os.getpid()
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    _state.require_own(directory, "a lease")
    git = _facts.git(cwd)  # before the queue lock: git may take a moment in a large repository
    with _state.locked(directory):
        # Admission and capacity changes share this lock; resolve all only now (and again at the grant: plan()).
        if session is not None:
            fresh = _platform.processes()
            for _, other in _state.entries(directory):
                spec = other.get("session")
                same = isinstance(spec, dict) and cast(dict[str, Any], spec).get("name") == session["name"]
                if same and _platform.alive(other, fresh):
                    raise _config.SlotError(f"session {session['name']} exists (pid {other.get('pid')}, "
                                            f"{other.get('state')}): stop it first (slotfather session stop "
                                            f"{session['name']})")
        whole = count == "all"
        count = _config.total() if whole else min(count, _config.total())
        sequence = next_sequence(directory)
        path = directory / "queue" / f"{sequence:010d}-{me}.json"
        own = _schedule.order_settings(directory)["yield_after"]
        entry = _entry.new_request(
            me, table.get(me, (0, ""))[1], count, label, command, cwd, _config.now(), selected_priority,
            uuid.uuid4().hex, started=_platform.start_epoch(me), ticket=selected_ticket, whole=whole, meta=meta,
            naming=naming, session=session, git=git, yieldable=yieldable,
            # when this client gives up (plan(): a head that would give up before its expected start holds nobody back)
            until=None if wait is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + wait)),
            # this client asks later or never (SLOTFATHER_YIELD_AFTER)
            yield_after=own if own != _schedule.order_settings(directory, environment=False)["yield_after"] else None)
        _state.write(path, entry)
    lease = Lease(count, path, yieldable=yieldable)
    deadline = None if wait is None else time.monotonic() + wait
    # A terminated waiter leaves the queue at once rather than at the next reap.
    main_thread = threading.current_thread() is threading.main_thread()
    previous_term = signal.signal(signal.SIGTERM, terminate) if main_thread else None
    try:
        said, interval, look, waiting = None, poll, 0.0, None
        queued = time.monotonic()
        warned = None  # when it last said it waits long (WARN_REPEAT)
        while True:
            if time.monotonic() >= look:
                # A full look runs ps over the machine under the queue lock. While the reason to wait stays the same,
                # each pause is half as long again, up to poll_max: a release grants at once, so only reaping a
                # crashed holder and memory recovering wait for a look.
                with _state.locked(directory), contextlib.suppress(_config.ProcessTableError):
                    # no table to trust: this look reaps and grants nothing, and the waiter keeps its place
                    waiting = grant(directory, process_table(directory))
                interval = poll if waiting != said else min(interval * 1.5, poll_max)
                look = time.monotonic() + interval
            try:
                current = json.loads(path.read_text())  # written whole by rename: no lock needed
                state = current["state"]
            except (OSError, ValueError, KeyError, TypeError):
                raise _config.SlotError("the lease vanished from the queue") from None
            if state == "held":
                if isinstance(current.get("count"), int) and current["count"] >= 1:
                    count = lease.count = current["count"]  # the capacity changed while it waited (plan())
                if said:
                    print(f"slot: granted {count} slot(s) to {label}", file=out, flush=True)
                return lease
            if deadline is not None and time.monotonic() >= deadline:
                raise _config.SlotTimeout(f"no {count} slot(s) within {wait:g} s: {waiting or 'earlier requests first'}")
            warn = float(_schedule.order_settings(directory).get("warn_wait") or 0)
            if warn and time.monotonic() - queued >= warn and (warned is None or
                                                              time.monotonic() - warned >= _config.WARN_REPEAT):
                warned = time.monotonic()
                with contextlib.suppress(Exception):  # a display must never cost the request its place
                    report = _query.long_wait_report(directory, path)
                    if report:  # the client's own stderr, and queue.log (EVENTS): a long wait does not go unnoticed
                        line, facts = report
                        print(f"slot: {line}", file=out, flush=True)
                        with _state.locked(directory):
                            _state.log(directory, f"waiting long: {line}")
                            _events.emit(directory, "request.waiting_long", _events.request(entry, path), facts,
                                         _events.holder(entry), level="warn")
            if waiting != said:
                print(f"slot: {label} waits for {count} slot(s): {waiting or 'earlier requests first'}",
                      file=out, flush=True)
                with contextlib.suppress(_config.ProcessTableError):  # a display must never cost the request its place
                    for line in _query.status_lines(directory):
                        print(f"  {line}", file=out, flush=True)
                if note and said is None:
                    print(f"slot: {note}", file=out, flush=True)
                said = waiting
            pause = min(poll, max(0.0, look - time.monotonic()))
            time.sleep(pause if deadline is None else max(0.0, min(pause, deadline - time.monotonic())))
    except BaseException:
        lease.release()
        raise
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)


def execute(argv: list[str], environment: dict[str, str], background: bool = False, marker: Path | None = None,
            directory: Path | None = None) -> tuple[int, dict[str, Any] | None]:
    """Runs argv with `environment` added and waits for it, forwarding SIGTERM and SIGHUP: (its exit status, 128 +
    the signal when one ended it; what its process tree used, where measured). With `marker`, a held lease's entry,
    its tree is measured with the processes that carry that lease and its idleness recorded beside the entry; with
    only `directory`, the tree alone is measured (a run inside a session, which shares its lease with others)."""
    from . import usage as _usage
    if background:
        prefix = ["/usr/sbin/taskpolicy", "-b", "/usr/bin/nice", "-n", "10"] if os.access(
            "/usr/sbin/taskpolicy", os.X_OK) else ["/usr/bin/nice", "-n", "10"]
        argv = prefix + argv
    try:
        child = subprocess.Popen(argv, env={**os.environ, **environment})
    except OSError as error:
        print(f"slot: {error}", file=sys.stderr)
        return 127, None
    began = time.monotonic()
    watch = None
    if marker is not None:
        text = str(marker)
        def carrying(pids: list[int]) -> set[int]:
            return _platform.carrying(pids, text)
        watch = _usage.Watch(_usage.TreeUsage(child.pid, time.time() - 1, carrying), marker.parent.parent, entry=marker)
    elif directory is not None:
        watch = _usage.Watch(_usage.TreeUsage(child.pid, time.time() - 1), directory)
    if watch is not None:
        with contextlib.suppress(RuntimeError):
            watch.start()
    reaped: list[bool] = []

    def forward(signum: int, _frame: Any) -> None:
        # os.kill rather than child.send_signal, which polls and could reap the child before wait4 sees it;
        # until wait4 returns the child (a zombie at worst) keeps its pid.
        if not reaped:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child.pid, signum)
    # The terminal sends SIGINT to the child's process group itself; the lease outlives the child.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    try:
        _, raw, rusage = os.wait4(child.pid, 0)  # the rusage holds every descendant reaped inside the tree
        status = os.waitstatus_to_exitcode(raw)
    except ChildProcessError:
        status, rusage = child.wait(), None
    reaped.append(True)
    if watch is not None:
        watch.usage.root_gone = True  # before any later look: the pid may be reused from now on
    child.returncode = status
    usage: dict[str, Any] | None = None
    if watch is not None and watch.is_alive():
        usage = cast("dict[str, Any] | None", watch.finish(rusage, time.monotonic() - began))
    return (128 - status if status < 0 else status), usage


def terminate(signum, _frame):
    raise SystemExit(128 + signum)


def run(count: int | str, label: str, argv: list[str], wait: float | None = None, meta: dict[str, str] | None = None,
        yieldable: bool = False, *, priority: str | None = None, ticket: str | int | None = None,
        labelled: bool = True) -> int:
    """Run argv under a lease of `count` slots; the command's exit status. Without `labelled` (no --label), `label`
    (the directory's name) gives way to one derived from the project, the command and the metadata (labels.py); a
    label that says little gets the derived parts appended, with a hint."""
    naming: dict[str, Any] = {}
    if policy() != "bypass" and not _config.setting("LEASE"):  # a nested run takes no lease and names none
        with contextlib.suppress(Exception):  # a name must never cost the run
            from . import labels as _labels
            try:
                cwd = os.getcwd()
            except OSError:
                cwd = ""
            naming = _labels.choose(label if labelled else None, cwd, argv, meta,
                                    None if ticket is None else str(ticket))
            label = naming["label"] or label
            if naming.get("hint"):
                print(f"slot: {naming['hint']}", file=sys.stderr, flush=True)
    try:
        background = policy() == "background"
        note = _config.BATCH_NOTE if priority == "batch" else _config.ALL_NOTE if count == "all" else ""
        lease = acquire(count, label, " ".join(argv), wait=wait, note=note, meta=meta,
                        yieldable=yieldable, priority=priority, ticket=ticket,
                        naming={k: naming[k] for k in ("given", "derived", "project")
                                if naming.get(k) and (k != "given" or naming[k] != label)} if naming else None)
    except _config.SlotTimeout as error:
        print(f"slot: {label}: {error}", file=sys.stderr)
        return _config.TIMED_OUT
    except KeyboardInterrupt:
        return 130
    except SystemExit as stop:
        return int(stop.code or 1)
    if count != "all" and lease.path is not None and not lease.inherited:
        with contextlib.suppress(Exception):  # a hint must never cost the run
            from . import askuse as _askuse
            found = _askuse.hint(lease.path.parent.parent, label, lease.count)
            if found:
                print(f"slot: {found}", file=sys.stderr, flush=True)
    with lease:
        marker = lease.path if lease.path is not None and not lease.inherited else None
        lease.status, lease.usage = execute(argv, lease.environment(), background, marker)
    return lease.status
