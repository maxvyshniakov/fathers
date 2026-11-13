"""Slotfather query responsibilities."""
from __future__ import annotations

import contextlib
import math
import time
import sys
from pathlib import Path
from typing import Any, NamedTuple

from . import config as _config
from . import idle as _idle
from . import leases as _leases
from . import platform as _platform
from . import schedule as _schedule
from . import state as _state


def snapshot(directory: Path | None = None) -> tuple[list[dict[str, Any]], list[tuple[Path, dict[str, Any], int]],
                                                    dict[int, tuple[int, str]]]:
    """The live queue entries, the earlier queues' holders and the process table, taken under the queue lock so
    that no holder starts or ends between the table and the entries."""
    directory = directory or _config.state_dir()
    with _state.locked(directory):
        table = _leases.process_table(directory, log=False)
        live = [entry for _, entry in _leases.reap(directory, table)]
    return live, _leases.legacy_holders(table), table


class Look(NamedTuple):
    """One look at the queue under its lock (#20): the process table, the live entries with their paths (reaped as
    every look reaps them), the capacity basis and each entry's yield request read under that same lock, then the
    earlier queues' holders and each held lease's idle mark. `live` are the entries as `status` and the dashboard
    read them: a held one with `idle_since` (epoch seconds or None)."""
    directory: Path
    table: dict[int, tuple[int, str]]
    found: list[tuple[Path, dict[str, Any]]]
    live: list[dict[str, Any]]
    basis: dict[str, Any]
    legacy: list[tuple[Path, dict[str, Any], int]]
    asks: dict[int, dict[str, Any] | None]  # id() of an entry in `live` -> its yield request

    def asked_of(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        return self.asks.get(id(entry))


def look(directory: Path | None = None) -> Look:
    """Look at the queue once, under its lock, as a plan reads it: a capacity saved meanwhile counts for the next
    look, never for half of this one."""
    directory = directory or _config.state_dir()
    with _state.locked(directory):
        table = _leases.process_table(directory, log=False)
        found = _leases.reap(directory, table)
        basis: dict[str, Any] = _config.capacity_basis()
        requests = [_state.asked(path) for path, _ in found]
    marked = idle_of(directory, found)
    live = [dict(entry, idle_since=marked.get(id(entry))) if entry.get("state") == "held" else entry
            for _, entry in found]
    return Look(directory, table, found, live, basis, _leases.legacy_holders(table),
                {id(entry): request for entry, request in zip(live, requests)})


def locked_snapshot(directory: Path | None = None) -> tuple[list[dict[str, Any]], list[tuple[Path, dict[str, Any], int]],
                                                           dict[int, tuple[int, str]], dict[str, Any]]:
    """look() as (live entries, earlier queues' holders, process table, capacity basis)."""
    found = look(directory)
    return found.live, found.legacy, found.table, found.basis


def idle_of(directory: Path, live: list[tuple[Path, dict[str, Any]]]) -> dict[int, float | None]:
    """id(entry) -> since when each held lease has been idle (#12; idle.idle_since(), by the queue's saved
    settings), None where it is not."""
    settings = _schedule.order_settings(directory, environment=False)
    moment = time.time()
    return {id(entry): _idle.idle_since(path, settings, moment) for path, entry in live if entry.get("state") == "held"}


def iso(moment: float | None) -> str | None:
    """Epoch seconds as a queue timestamp (UTC), None for None."""
    return None if moment is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


def outlook(directory: Path, entries: list[dict[str, Any]], slots: int, other: int,
            moment: float | None = None) -> dict[int, dict[str, Any]]:
    """expected() that never fails: {} when the forecast cannot be worked out (a malformed entry or estimate), so
    that `status`, a waiter's own lines and the dashboard show the queue without it."""
    try:
        return expected(directory, entries, slots, other, moment)
    except Exception:  # a display must never fail the command, nor cost a waiter its place
        return {}


def expected(directory: Path, entries: list[dict[str, Any]], slots: int, other: int,
             moment: float | None = None) -> dict[int, dict[str, Any]]:
    """What the queue expects of each waiter in `entries` (live queue entries) at `moment`: id(entry) ->
    {"priority" (the class it is served in), "aged", "est_start" (epoch seconds, None where the forecast does not
    reach it or memory holds it back), "head", "reserved" (the head's promised start), "waited", "long_wait",
    "memory_wait" (why memory holds back a request the order would start now, else None)}. Read only: the forecast
    works on copies (schedule.forecast())."""
    moment = time.time() if moment is None else moment
    settings = _schedule.order_settings(directory)
    held = [e for e in entries if e.get("state") == "held"]
    waiters = [dict(e) for e in entries if e.get("state") != "held"]
    originals = {id(copy): e for copy, e in zip(waiters, [e for e in entries if e.get("state") != "held"])}
    if not waiters:
        return {}
    estimate = _schedule.estimator(directory)
    _schedule.fit(waiters, slots)
    starts = _schedule.forecast(waiters, held, slots, moment, estimate, other, settings) \
        if settings["order"] == "backfill" else {}
    found = _schedule.plan(waiters, held, slots, moment, estimate=estimate, other=other, reserve_head=True,
                           settings=settings)
    # The order would start these now; memory may hold them back (grant()'s gate), with no start a forecast can tell.
    memory = _platform.memory_level() if found.grants else ("normal", None)
    used = sum(_schedule.holds(e, slots) for e in held) + other
    shared = _schedule.order_settings(directory, environment=False)  # the queue's rule, not this reader's
    held_back = {id(e): why for e, why in _schedule.memory_gate(found.grants, held, used, memory, shared) if why}
    warn = float(settings.get("warn_wait") or 0)
    result: dict[int, dict[str, Any]] = {}
    for copy in waiters:
        began = _schedule.waiting_since(copy)
        waited = None if began is None else max(0.0, moment - began)
        is_head = copy is found.head
        result[id(originals[id(copy)])] = {
            "priority": _schedule.effective_priority(copy, settings, moment) if settings["order"] == "backfill"
            else _config.priority_of(copy),
            "aged": settings["order"] == "backfill" and _schedule.aged(copy, settings, moment),
            "est_start": None if id(copy) in held_back else starts.get(id(copy)), "head": is_head,
            "memory_wait": held_back.get(id(copy)),
            "reserved": found.reserved if is_head else None,
            "waited": waited, "long_wait": bool(warn and waited is not None and waited >= warn)}
    return result


def status_lines(directory: Path | None = None) -> list[str]:
    directory = directory or _config.state_dir()
    with _state.locked(directory):  # the processes and capacity the plans of this moment read
        table = _leases.process_table(directory, log=False)
        live = _leases.reap(directory, table)
        slots = _config.total()
    legacy = _leases.legacy_holders(table)
    marked = idle_of(directory, live)
    expect = outlook(directory, [e for _, e in live], slots, sum(c for _, _, c in legacy))
    lines: list[str] = []
    for path, e in live:
        meta = _config.meta_of(e)
        tags = f" meta={_config.meta_text(meta)}" if meta else ""
        since = marked.get(id(e))
        if since is not None:  # #12: below the idle threshold for at least the idle duration
            tags += f" idle={_config.duration(time.time() - since)}"
        if e.get("yieldable"):
            request = _state.asked(path) if e.get("state") == "held" else None
            tags += " yieldable" + (f" asked-to-yield-for={request.get('label')}" if request else "") + \
                (f" yielded={e['yields']}" if e.get("yields") else "")
        if isinstance(e.get("session"), dict):
            from .session import tags as session_tags
            tags += session_tags(path, e)
        tags += waiter_tags(e, expect.get(id(e)))
        lines.append(f"{e['state']}: {e['count']} slot(s) {e['label']} pid={e['pid']} since={e['since']}{tags} "
                     f"cmd={e['cmd']}")
    lines += [f"legacy: {count} slot(s) {e.get('label')} pid={e.get('pid')} (earlier queue {path.parent.parent})"
              for path, e, count in legacy]
    lines += [f"unreadable: queue/{path.name} (empty or not JSON; left in place)"
              for path in _state.unreadable(directory)]
    used = held_slots([e for _, e in live], legacy, slots)
    return lines + [held_line(used, slots)]


def waiter_tags(entry: dict[str, Any], expected: dict[str, Any] | None) -> str:
    """`status`' words for a waiter: its class when not normal (`priority=batch`, and `aged-from=batch` when ageing
    moved it up), `head` with its promised start, `est-start=` (UTC; `?` where the forecast does not reach it),
    `memory-wait="..."` while memory holds it back, and `long-wait=` once it has waited the warn time; for a lease granted after ageing, `aged-from=`."""
    if entry.get("state") == "held":
        served = entry.get("aged")
        return f" priority={served} aged-from={_config.priority_of(entry)}" if served in _config.PRIORITIES else ""
    if expected is None:
        return ""
    own = _config.priority_of(entry)
    tags = f" priority={expected['priority']}" if expected["priority"] != "normal" or own != "normal" else ""
    if expected["aged"]:
        tags += f" aged-from={own}"
    if entry.get("reprioritized"):
        tags += f" reprioritized-from={entry['reprioritized']}"
    if expected["head"]:
        tags += " head" + (f" reserved={iso(expected['reserved'])}" if expected["reserved"] is not None else "")
    tags += f" est-start={iso(expected['est_start']) or '?'}"
    if expected.get("memory_wait"):
        tags += f' memory-wait="{expected["memory_wait"]}"'
    if expected["long_wait"]:
        tags += f" long-wait={_config.duration(expected['waited'])}"
    return tags


def long_wait(directory: Path, path: Path) -> str | None:
    """The line a waiter that has waited the warn time says (its client's stderr; queue.log, which EVENTS shows):
    how long it has waited, what it waits behind, when it is expected to start, and how to move it up. None when
    it no longer waits."""
    report = long_wait_report(directory, path)
    return report[0] if report else None


def long_wait_report(directory: Path, path: Path) -> tuple[str, dict[str, Any]] | None:
    """long_wait()'s line and its facts for the request.waiting_long event: {"waited" (seconds), "est_start" (UTC
    stamp or None), "behind": {"earlier", "running"}, "priority" (served), "aged"}."""
    with _state.locked(directory):
        table = _leases.process_table(directory, log=False)
        live = _leases.reap(directory, table)
        slots = _config.total()
    mine = next((e for p, e in live if p == path), None)
    if mine is None or mine.get("state") == "held":
        return None
    legacy = _leases.legacy_holders(table)
    expect = outlook(directory, [e for _, e in live], slots, sum(c for _, _, c in legacy)).get(id(mine))
    if expect is None:
        return None
    settings = _schedule.order_settings(directory)
    serving = _schedule.ranked([e for _, e in live if e.get("state") != "held"], settings, time.time()) \
        if settings["order"] == "backfill" else [e for _, e in live if e.get("state") != "held"]
    ahead = next((n for n, e in enumerate(serving) if e is mine), 0)
    running = sum(1 for _, e in live if e.get("state") == "held")
    behind = [f"{running} running lease{'s' if running != 1 else ''}"] if running else []
    if ahead:
        behind.insert(0, f"{ahead} earlier request{'s' if ahead != 1 else ''}")
    start = expect["est_start"]
    when = time.strftime("%H:%M", time.localtime(start)) if start is not None else "unknown"
    up = {"batch": "normal", "normal": "interactive"}.get(expect["priority"])
    hint = f"; reprioritize with: slotfather reprioritize {mine.get('pid')} {up}" if up else ""
    served = expect["priority"] + (f", aged from {_config.priority_of(mine)}" if expect["aged"] else "")
    memory = f"; {expect['memory_wait']}" if expect.get("memory_wait") else ""
    line = (f"{mine.get('label')} (pid {mine.get('pid')}, {served}) waiting {_config.duration(expect['waited'] or 0)} "
            f"behind {' and '.join(behind) or 'nothing'}: est. start {when}{memory}{hint}")
    return line, {"waited": round(expect["waited"] or 0), "est_start": iso(start),
                  "behind": {"earlier": ahead, "running": running}, "priority": expect["priority"],
                  "aged": bool(expect["aged"])}


def held_slots(live: list[dict[str, Any]], legacy: list[tuple[Path, dict[str, Any], int]], slots: int) -> int:
    """The slots held now: each held lease's (an `-n all` one's at least the capacity, schedule.holds()) and the
    earlier queues'."""
    return sum(_schedule.holds(e, slots) for e in live if e["state"] == "held") + sum(c for _, _, c in legacy)


def draining(held: int, slots: int) -> int:
    """Slots held above a capacity that shrank under running leases (#7). They keep their slots; nothing more starts
    until enough of them end."""
    return max(0, held - slots)


def held_line(held: int, slots: int) -> str:
    """`status`' last line: "3 of 4 slots held", with ", draining N" while held is above the capacity."""
    drain = draining(held, slots)
    return f"{held} of {slots} slots held" + (f", draining {drain}" if drain else "")


def status_processes(directory: Path, pid: int | None = None) -> dict[int, tuple[int, str]]:
    """The legacy identity format, using the native adapter only when every holder is observable."""
    if sys.platform == "darwin":
        from .macos_process import basic_snapshot
        sources = [directory, *_config.legacy_dirs()]
        required = {entry["pid"] for source in sources for _, entry in _state.entries(source)
                    if type(entry.get("pid")) is int}
        if pid is not None:
            required.add(pid)
        try:
            table = basic_snapshot(required)
            # Legacy clients use their own lock: a lease may appear during this observation.
            if any(entry["pid"] in table and not table[entry["pid"]][1]
                   for source in sources for _, entry in _state.entries(source) if type(entry.get("pid")) is int):
                return _leases.process_table(directory, log=False)
            return table
        except OSError:
            pass
    return _leases.process_table(directory, log=False)


def status_json(directory: Path | None = None, pid: int | None = None, look: Look | None = None) -> dict[str, Any]:
    """The queue for programs (`status --json`): every live entry with its lease id (the entry's name), the lease
    path (what SLOTFATHER_LEASE holds), holder pid and process group, its live process tree and its metadata. With
    `pid`, only the entries that process belongs to, each with `via`: "tree" (the holder or a descendant) or "env"
    (it left the tree, by a double fork or setsid and launchd adopting it, but its environment carries the lease's
    SLOTFATHER_LEASE; macOS shows that only for the user's own processes that are not Apple platform binaries), and
    `query`: the pid and its start time as `ps -o lstart=` gives it (None: not running), for a guard to check again
    that it is the same process before it acts."""
    asks: dict[int, dict[str, Any] | None] | None = None
    if look is not None:  # status --json --full: the look the dashboard block reads too (#20)
        directory, table, slots = look.directory, look.table, int(look.basis["slots"])
        live = look.found
        marked = {id(entry): item.get("idle_since") for (_, entry), item in zip(look.found, look.live)}
        asks = {id(entry): look.asked_of(item) for (_, entry), item in zip(look.found, look.live)}
    else:
        directory = directory or _config.state_dir()
        with _state.locked(directory):
            table = status_processes(directory, pid)
            live = _leases.reap(directory, table)
            slots = _config.total()
        marked = idle_of(directory, live)
    entries_found: list[dict[str, Any]] = []
    for path, entry in live:
        tree = _platform.descendants(table, entry.get("pid"))
        priority = _config.priority_of(entry)
        item: dict[str, Any] = {"id": path.stem, "lease_id": _config.lease_id(path.stem, entry), "lease": str(path), "state": entry.get("state"), "count": entry.get("count"),
                "label": entry.get("label"), "pid": entry.get("pid"), "pgid": _platform.process_group(entry.get("pid")),
                "start": entry.get("start"), "since": entry.get("since"), "granted": entry.get("granted"),
                "cmd": entry.get("cmd"), "cwd": entry.get("cwd"), "meta": _config.meta_of(entry), "tree": tree,
                "priority": priority, "ticket": _config.ticket_of(entry),
                "yieldable": bool(entry.get("yieldable")),
                "yields": int(entry.get("yields") or 0),
                "yield_asked": _state.asked(path) if asks is None else asks.get(id(entry)), "position": None,
                "idle_since": marked.get(id(entry))}
        if isinstance(entry.get("session"), dict):
            from .session import view
            item["session"] = view(path, entry)
        entries_found.append(item)
    settings = _schedule.order_settings(directory)
    moment = time.time()
    serving = _schedule.ranked([e for _, e in live if e.get("state") != "held"], settings, moment) \
        if settings["order"] == "backfill" else [e for _, e in live if e.get("state") != "held"]
    places = {id(e): n for n, e in enumerate(serving, 1)}
    earlier = _leases.legacy_holders(table)
    # A guard asking about one process (--pid) needs no forecast.
    expect = outlook(directory, [e for _, e in live], slots, sum(c for _, _, c in earlier), moment) \
        if pid is None else {}
    known = None
    if pid is None:  # expected runs per run type (fathers#106); not for a guard asking about one process
        with contextlib.suppress(Exception):
            from . import runtype as _runtype
            known = _runtype.cached(directory)
    for (path, entry), item in zip(live, entries_found):
        if known is not None:
            with contextlib.suppress(Exception):  # the dashboard's poll never fails for it
                from . import runtype as _runtype
                item.update(_runtype.entry_view(entry, known, moment))
        item["position"] = places.get(id(entry))  # waiters: their place in the order the queue serves them
        found = expect.get(id(entry))
        aged_to = entry.get("aged") if entry.get("aged") in _config.PRIORITIES else None
        item.update(effective_priority=found["priority"] if found else aged_to or item["priority"],
                    aged=bool(found["aged"]) if found else aged_to is not None,
                    reprioritized=entry.get("reprioritized") if entry.get("reprioritized") in _config.PRIORITIES
                    else None,
                    est_start=iso(found["est_start"]) if found else None, head=bool(found and found["head"]),
                    reserved=iso(found["reserved"]) if found else None,
                    waited=None if not found or found["waited"] is None else round(found["waited"]),
                    long_wait=bool(found and found["long_wait"]), memory_wait=found["memory_wait"] if found else None)
    legacy = []
    for path, entry, count in earlier:
        legacy.append({"id": path.stem, "lease": str(path), "state": "held", "count": count,
                       "label": entry.get("label"), "pid": entry.get("pid"), "pgid": _platform.process_group(entry.get("pid")),
                       "tree": _platform.descendants(table, entry.get("pid"))})
    held = held_slots([entry for _, entry in live], earlier, slots)
    found = {}
    if pid is not None:
        from .procs import environment_leases
        candidates = [item for item in entries_found if pid not in item["tree"]]
        marked, _ = environment_leases([pid] if pid in table else [], candidates)
        mine = []
        for item in entries_found:
            if pid in item["tree"]:
                mine.append(dict(item, via="tree"))
            elif item["lease"] in marked.get(pid, set()):
                mine.append(dict(item, via="env"))
        entries_found = mine
        legacy = [dict(e, via="tree") for e in legacy if pid in e["tree"]]
        found["query"] = {"pid": pid, "start": table[pid][1] if pid in table else None}
    return {"v": 2, "slots": slots, "held": held, "free": max(0, slots - held), "draining": draining(held, slots),
            "entries": entries_found,
            "legacy": legacy, **found}


def available(directory: Path | None = None, label: str = "", command: str = "") -> int:
    """How many slots a new request fits by queue order, before memory checks: the free ones less what waiters take first, or none while a
    waiter past the overtaking window holds back everyone behind it, unless (backfill) the request is expected to
    end before that waiter can start. Without `command` its run counts as unknown (the fallback)."""
    directory = directory or _config.state_dir()
    live, legacy, _, basis = locked_snapshot(directory)
    held = [e for e in live if e["state"] == "held"]
    other = sum(count for _, _, count in legacy)
    slots = int(basis["slots"])
    return _schedule.room([e for e in live if e["state"] != "held"], max(0, slots - held_slots(live, legacy, slots)),
                          held, _schedule.estimator(directory), other, label=label, command=command, slots=slots)


def size(maximum: int, cores: int, total: int, averages: tuple[float, float, float], held: int,
         waiting: bool = False, exclusive: bool = False) -> dict:
    """A job count for work that runs beside `held` leased slots (or after them, when `exclusive`) under load
    `averages` (1, 5 and 15 minutes) that include processes taking no lease. The module docstring has the formula."""
    per_slot = cores / total
    load1, load5, load15 = averages
    load = min(load1, load5)
    unleased = max(0.0, load - held * per_slot)
    busy = unleased + (0.0 if exclusive else held * per_slot)
    capacity = max(0, math.floor((cores - busy) / per_slot + 0.5))
    free = total if exclusive else 0 if waiting else max(0, total - held)
    worker_budget = max(1, min(maximum, capacity, free))
    return {"jobs": worker_budget, "maximum": maximum, "exclusive": exclusive, "cores": cores, "total": total,
            "cores_per_slot": round(per_slot, 2), "held": held, "waiting": waiting, "free": free,
            "load": round(load, 2), "load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2),
            "unleased_load": round(unleased, 2), "capacity": capacity}


def advise(maximum: int | None = None, exclusive: bool = False, cores_now: int | None = None,
           averages: tuple[float, float, float] | None = None, label: str = "", command: str = "") -> dict:
    """size() for the queue as it is now: slots held by others (the caller's own lease is not one) and
    whether a request waits, a request for `command` under `label` when given (else one of unknown run), with
    "run_estimate", the run the history expects of it (None: unknown, counted as the fallback)."""
    live, legacy, table, basis = locked_snapshot()
    mine = _leases.inherited_lease(table)
    holders = [e for e in live if e["state"] == "held" and not (mine and e["pid"] == mine[1]["pid"])]
    other = sum(count for path, e, count in legacy if not (mine and path == mine[0]))
    slots = int(basis["slots"])
    held = sum(_schedule.holds(e, slots) for e in holders) + other
    waiters = [e for e in live if e["state"] != "held"]
    estimate = _schedule.estimator(_config.state_dir())
    waiting = bool(waiters) and _schedule.room(waiters, max(0, slots - held), holders, estimate, other, label=label,
                                               command=command, slots=slots) == 0
    found = size(maximum or slots, cores_now or _config.cores(), slots, averages or _platform.loads(), min(held, slots), waiting,
                 exclusive)
    found["run_estimate"] = estimate({"label": label, "cmd": command}) if command else None
    # How the capacity was found (#653), and why memory holds new leases back now, at warning or worse (None: it
    # does not), with the level ("normal", "warning": one 1-slot lease at a time, "short"); the job count does not
    # shrink for it: a caller that starts memory-heavy workers should.
    memory_kind, memory_why = _platform.memory_level()
    found.update(capacity_mode=basis["mode"], gib_per_slot=basis["gib_per_slot"], memory_gib=basis["memory_gib"],
                 memory_short=memory_why, memory_level=memory_kind)
    return found
