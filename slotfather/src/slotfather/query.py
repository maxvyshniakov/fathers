"""Slotfather query responsibilities."""
from __future__ import annotations

import contextlib
import math
import time
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

from . import config as _config
from . import entry as _entry
from . import events as _events
from . import idle as _idle
from . import lineage as _lineage
from . import platform as _platform
from . import process as _process
from . import queue as _queue
from . import schedule as _schedule
from . import settings as _settings
from . import state as _state


def snapshot(directory: Path | None = None) -> tuple[list[dict[str, Any]], list[tuple[Path, dict[str, Any], int]],
                                                    dict[int, tuple[int, str]]]:
    """The live queue entries, the earlier queues' holders and the process table, from one look()."""
    found = look(directory)
    return [entry for _, entry in found.found], found.legacy, found.table


class Look(NamedTuple):
    """One look at the queue under its lock (#20, fathers#36): the process table, the live entries with their paths
    (reaped as every look reaps them), the capacity basis and each entry's yield request read under that same lock,
    then the earlier queues' holders, each held lease's idle mark, and the order settings and estimator the outlook
    plans by. `live` are the entries as `status` and the dashboard read them: a held one with `idle_since` (epoch
    seconds or None). `unknown` are the entries this client does not know (state.unknown(); fathers#19), read under
    the same lock and left alone. Every reader of the queue takes one (`status`, `status --json`, the long-wait
    line, `available`, `advise`, `slots`, the dashboard block, the sweep's protection)."""
    directory: Path
    table: dict[int, tuple[int, str]]
    found: list[tuple[Path, dict[str, Any]]]
    live: list[dict[str, Any]]
    basis: dict[str, Any]
    legacy: list[tuple[Path, dict[str, Any], int]]
    asks: dict[str, dict[str, Any] | None]  # an entry's id (its file's stem) -> its yield request
    unknown: tuple[tuple[Path, dict[str, Any]], ...] = ()
    reserved: tuple[tuple[Path, dict[str, Any], int], ...] = ()  # the unknown ones whose holder is alive
    settings: dict[str, Any] | None = None  # settings.order_settings(), with this client's environment
    estimate: _schedule.Estimate | None = None  # queue.estimator(): the history read only when a plan asks

    def id_of(self, entry: dict[str, Any]) -> str | None:
        """The id (file stem) of an entry of this look, by identity: one of `live` or of `found`."""
        for (path, raw), item in zip(self.found, self.live):
            if entry is item or entry is raw:
                return path.stem
        return None

    def asked_of(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        found = self.id_of(entry)
        return None if found is None else self.asks.get(found)

    def phase_of(self, entry: dict[str, Any]) -> _entry.Phase:
        """The entry's phase (entry.phase()), with its yield request and idle mark from this look."""
        at = next((n for n, ((_, raw), item) in enumerate(zip(self.found, self.live)) if entry is item or entry is raw),
                  None)
        idle = None if at is None else self.live[at].get("idle_since")
        return _entry.phase(entry, bool(self.asked_of(entry)), idle)

    @property
    def slots(self) -> int:
        return int(self.basis["slots"])

    @property
    def other(self) -> int:
        """The slots held without a known entry of this queue: earlier queues' holders and live unknown entries."""
        return sum(count for _, _, count in [*self.legacy, *self.reserved])

    @property
    def held(self) -> int:
        return held_slots(self.live, [*self.legacy, *self.reserved], self.slots)

    @property
    def free(self) -> int:
        return max(0, self.slots - self.held)

    @property
    def draining(self) -> int:
        return draining(self.held, self.slots)


Table = dict[int, tuple[int, str]]


def look(directory: Path | None = None, processes: Callable[[], Table] | None = None, log: bool = False) -> Look:
    """One look at the queue under its lock. See looking() for readers that also change queue state."""
    with looking(directory, processes, log) as found:
        return found


@contextlib.contextmanager
def looking(directory: Path | None = None, processes: Callable[[], Table] | None = None,
            log: bool = False) -> Iterator[Look]:
    """Look at the queue under its lock and keep it through the caller's read or transaction. A capacity saved
    meanwhile counts for the next look. The process table is also read under the lock; status --pid can supply
    a native one. A look changing the queue sets `log` so failed process reads appear in its event log."""
    directory = directory or _config.state_dir()
    with _queue.looking(directory, processes, log) as (table, found):
        basis: dict[str, Any] = _settings.capacity_basis()
        requests = {path.stem: _state.asked(path) for path, _ in found}
        strange = tuple(_state.unknown(directory))
        kept = tuple(_queue.reserved(directory, table))
        # all under the one lock (fathers#36 review): idle records, the earlier queues' holders, the order settings
        facts = idle_facts(directory, found)
        legacy = _queue.legacy_holders(table)
        settings = _settings.order_settings(directory)
        live = [dict(entry, idle_since=facts[id(entry)][0], recent_cpu=facts[id(entry)][1])
                if _entry.is_held(entry) else entry
                for _, entry in found]
        yield Look(directory, table, found, live, basis, legacy, requests, strange, kept, settings,
                   _queue.estimator(directory))


def locked_snapshot(directory: Path | None = None) -> tuple[list[dict[str, Any]], list[tuple[Path, dict[str, Any], int]],
                                                           dict[int, tuple[int, str]], dict[str, Any]]:
    """look() as (live entries, the slots held without a known entry: earlier queues' holders and live unknown
    entries (queue.reserved()), process table, capacity basis)."""
    found = look(directory)
    return found.live, [*found.legacy, *found.reserved], found.table, found.basis


def idle_facts(directory: Path, live: list[tuple[Path, dict[str, Any]]]) -> dict[int, tuple[float | None, float | None]]:
    """id(entry) -> (idle_since, recent_cpu_pct) for each held lease from one read of its .idle record (#12,
    fathers#20), by the queue's saved settings."""
    settings = _settings.order_settings(directory, environment=False)
    moment = time.time()
    return {id(entry): _idle.facts(path, settings, moment) for path, entry in live if _entry.is_held(entry)}


def idle_of(directory: Path, live: list[tuple[Path, dict[str, Any]]]) -> dict[int, float | None]:
    """id(entry) -> since when each held lease has been idle (#12; idle.idle_since(), by the queue's saved
    settings), None where it is not."""
    return {entry_id: since for entry_id, (since, _) in idle_facts(directory, live).items()}


def iso(moment: float | None) -> str | None:
    """Epoch seconds as a queue timestamp (UTC), None for None."""
    return None if moment is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


def outlook(directory: Path, entries: list[dict[str, Any]], slots: int, other: int,
            moment: float | None = None, settings: dict[str, Any] | None = None) -> dict[int, dict[str, Any]]:
    """expected() that never fails: {} when the forecast cannot be worked out (a malformed entry or estimate), so
    that `status`, a waiter's own lines and the dashboard show the queue without it."""
    try:
        return expected(directory, entries, slots, other, moment, settings)
    except Exception:  # a display must never fail the command, nor cost a waiter its place
        return {}


def expected(directory: Path, entries: list[dict[str, Any]], slots: int, other: int,
             moment: float | None = None, settings: dict[str, Any] | None = None) -> dict[int, dict[str, Any]]:
    """What the queue expects of each waiter in `entries` (live queue entries) at `moment`: id(entry) ->
    {"priority" (the class it is served in), "aged", "est_start" (epoch seconds, None where the forecast does not
    reach it or memory holds it back), "head", "reserved" (the head's promised start), "waited", "long_wait",
    "memory_wait" (why memory holds back a request the order would start now, else None), "load_wait" (why
    sustained CPU load does, fathers#184, else None)}. Read only: the forecast works on copies (schedule.forecast())."""
    moment = time.time() if moment is None else moment
    settings = settings or _settings.order_settings(directory)  # a look's own where it gives them (fathers#36)
    held, waiting = _entry.split(entries)
    waiters = [dict(e) for e in waiting]
    originals = {id(copy): e for copy, e in zip(waiters, waiting)}
    if not waiters:
        return {}
    estimate = _queue.estimator(directory)
    p90 = _queue.estimator(directory, p90=True)
    window = _config.overtake_window()
    _schedule.fit(waiters, slots)
    starts = _schedule.forecast(waiters, held, slots, moment, estimate, other, settings, window, p90) \
        if settings["order"] == "backfill" else {}
    found = _schedule.plan(waiters, held, slots, moment, estimate=estimate, other=other, reserve_head=True,
                           settings=settings, window=window, p90=p90)
    # The order would start these now; memory may hold them back (grant()'s gate), with no start a forecast can tell.
    memory = _platform.memory_level() if found.grants else ("normal", None)
    used = sum(_entry.holds(e, slots) for e in held) + other
    shared = _settings.order_settings(directory, environment=False)  # the queue's rule, not this reader's
    ram_gib = _platform.memory_gib() if (found.grants and used) else None
    ram_mb = None if ram_gib is None else ram_gib * 1024.0
    load = _schedule.load_wait(_platform.load_averages(), _config.cores(), slots, used, shared) \
        if found.grants else None
    held_back = {id(e): why for e, why in _schedule.memory_gate(
        found.grants, held, used, memory, shared,
        memory_estimate=_queue.memory_estimator(directory) if ram_mb is not None else None,
        ram_mb=ram_mb, load=load) if why}
    warn = float(settings.get("warn_wait") or 0)
    # fathers#160: the slots no request takes now, and the leases with no end estimate a head waits on
    idle = max(0, slots - used - sum(_entry.holds(e, slots) for e in found.grants))
    stalled = bool(found.blocked) and found.reserved is not None and moment >= found.reserved  # its promise broke
    dropped = {id(entry): blockers for entry, blockers in found.dropped}

    def names(leases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"label": e.get("label"), "pid": e.get("pid")} for e in leases]

    result: dict[int, dict[str, Any]] = {}
    for copy in waiters:
        began = _entry.waiting_since(copy)
        waited = None if began is None else max(0.0, moment - began)
        is_head = copy is found.head
        result[id(originals[id(copy)])] = {
            "priority": _schedule.effective_priority(copy, settings, moment) if settings["order"] == "backfill"
            else _entry.priority_of(copy),
            "aged": settings["order"] == "backfill" and _schedule.aged(copy, settings, moment),
            "est_start": None if id(copy) in held_back else starts.get(id(copy)), "head": is_head,
            "memory_wait": held_back.get(id(copy)) if held_back.get(id(copy)) != load else None,
            "load_wait": load if load and held_back.get(id(copy)) == load else None,
            "reserved": found.reserved if is_head else None,
            "waited": waited, "long_wait": bool(warn and waited is not None and waited >= warn),
            "blocked_by": names(found.blocked) if is_head and stalled else None,
            "idle": idle if is_head and stalled else None,
            "gave_way": names(dropped[id(copy)]) if id(copy) in dropped else None}
    return result


def status_lines(directory: Path | None = None) -> list[str]:
    """`status`: one line per entry of one look(), the earlier queues' holders, unreadable and unknown entries, and
    the slots held."""
    found = look(directory)  # the processes and capacity the plans of this moment read
    directory, table, live, slots = found.directory, found.table, found.found, found.slots
    legacy = found.legacy
    expect = outlook(directory, [e for _, e in live], slots, found.other, settings=found.settings)
    lines: list[str] = []
    for (path, e), item in zip(live, found.live):
        meta = _entry.meta_of(e)
        tags = f" meta={_config.meta_text(meta)}" if meta else ""
        since = item.get("idle_since") if _entry.is_held(e) else None
        if since is not None:  # #12: below the idle threshold for at least the idle duration
            tags += f" idle={_config.duration(time.time() - since)}"
        if e.get("yieldable"):
            request = found.asks.get(path.stem) if _entry.is_held(e) else None
            tags += " yieldable" + (f" asked-to-yield-for={request.get('label')}" if request else "") + \
                (f" yielded={e['yields']}" if e.get("yields") else "")
        if _entry.lock_of(e) is not None:  # fathers#159: its slots are free while its command waits on a lock
            tags += f" waiting-for-lock={_entry.lock_of(e)}"
        if isinstance(e.get("session"), dict):
            from .session import tags as session_tags
            tags += session_tags(path, e)
        tags += waiter_tags(e, expect.get(id(e)))
        lines.append(f"{e['state']}: {e['count']} slot(s) {e.get('label')} pid={e['pid']} since={e['since']}{tags} "
                     f"cmd={e.get('cmd')}")
    lines += blocked_line([e for _, e in live], expect)
    lines += [f"legacy: {count} slot(s) {e.get('label')} pid={e.get('pid')} (earlier queue {path.parent.parent})"
              for path, e, count in legacy]
    lines += [f"unreadable: queue/{path.name} (empty or not JSON; left in place)"
              for path in _state.unreadable(directory)]
    lines += [f"unknown: {unknown_text(path, e, table)}" for path, e in found.unknown]
    return lines + [held_line(found.held, slots)]


def unknown_holds(entry: dict[str, Any], table: dict[int, tuple[int, str]]) -> int:
    """The slots an unknown entry holds now: its count while its holder is alive (leases.reserved()), else 0."""
    count = _entry.reserved(entry)
    return count if count and _platform.alive(entry, table) else 0


def unknown_text(path: Path, entry: dict[str, Any], table: dict[int, tuple[int, str]]) -> str:
    """The warning for an entry this client does not know (fathers#19): its file, why, what it is said to be, what
    it holds, and how to clear it when its holder is gone."""
    found = (f"queue/{path.name} ({_entry.unknown(entry)}; label={entry.get('label')} pid={entry.get('pid')}): "
             f"this client does not know it and leaves it alone")
    holds = unknown_holds(entry, table)
    if holds:
        return found + f"; its holder runs, so its {holds} slot(s) stay reserved"
    if _entry.checkable(entry):
        return found + f"; its holder is not running: remove {path} to clear it"
    return found + f"; its holder cannot be checked, so it reserves no slots: remove {path} once no client holds it"


def entry_item(path: Path, entry: dict[str, Any], table: dict[int, tuple[int, str]]) -> dict[str, Any]:
    """`status --json`'s fields of an entry as it was written, with its holder's process group and live tree."""
    pid = entry.get("pid") if type(entry.get("pid")) is int else None
    return {"id": path.stem, "lease_id": _entry.lease_id(path.stem, entry), "lease": str(path),
            "state": entry.get("state"), "count": entry.get("count"), "label": entry.get("label"),
            "pid": entry.get("pid"), "pgid": _platform.process_group(pid), "start": entry.get("start"),
            "since": entry.get("since"), "granted": entry.get("granted"), "cmd": entry.get("cmd"),
            "cwd": entry.get("cwd"), "meta": _entry.meta_of(entry), "tree": _lineage.descendants(table, pid),
            "priority": _entry.priority_of(entry), "ticket": _entry.ticket_of(entry),
            "yieldable": bool(entry.get("yieldable")), "yields": int(entry.get("yields") or 0),
            **({"min_count": entry["min_count"], "max_count": entry["max_count"]}
               if type(entry.get("max_count")) is int and type(entry.get("min_count")) is int else {}),
            **({"lock": _entry.lock_of(entry)} if _entry.lock_of(entry) is not None else {})}


def unknown_item(path: Path, entry: dict[str, Any], table: dict[int, tuple[int, str]]) -> dict[str, Any]:
    """`status --json`'s item for an entry this client does not know: the fields it has, as read, with `phase`
    "unknown", the reason, and none of the order's (no position, forecast or class served)."""
    item = entry_item(path, dict(entry, yields=None), table)
    item.update(yield_asked=None, position=None, idle_since=None, phase=_entry.UNKNOWN,
                unknown=_entry.unknown(entry), holds=unknown_holds(entry, table),
                warning=unknown_text(path, entry, table),
                effective_priority=item["priority"], aged=False, reprioritized=None, est_start=None, head=False,
                reserved=None, waited=None, long_wait=False, memory_wait=None, load_wait=None)
    return item


def waiter_tags(entry: dict[str, Any], expected: dict[str, Any] | None) -> str:
    """`status`' words for a waiter: its class when not normal (`priority=batch`, and `aged-from=batch` when ageing
    moved it up), `head` with its promised start, `est-start=` (UTC; `?` where the forecast does not reach it),
    `memory-wait="..."` while memory holds it back, `load-wait="..."` while sustained CPU load does, and
    `long-wait=` once it has waited the warn time; for a lease granted after ageing, `aged-from=`."""
    if _entry.is_held(entry):
        served = entry.get("aged")
        return f" priority={served} aged-from={_entry.priority_of(entry)}" if served in _config.PRIORITIES else ""
    if expected is None:
        return ""
    own = _entry.priority_of(entry)
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
    if expected.get("load_wait"):  # fathers#184
        tags += f' load-wait="{expected["load_wait"]}"'
    if expected["long_wait"]:
        tags += f" long-wait={_config.duration(expected['waited'])}"
    if expected.get("gave_way"):  # fathers#160
        tags += " gave-way-to=" + ",".join(str(e["label"]) for e in expected["gave_way"])
    return tags


def blocked_line(entries: list[dict[str, Any]], expect: dict[int, dict[str, Any]]) -> list[str]:
    """`status`' line for a head the leases with no end estimate hold back (fathers#160): "head LABEL blocked by
    leases X, Y, Z; N slots idle"."""
    for entry in entries:
        found = expect.get(id(entry))
        if found and found.get("blocked_by"):
            leases = ", ".join(f"{e['label']} (pid {e['pid']})" for e in found["blocked_by"])
            return [f"head {entry.get('label')} blocked by leases {leases} with no end estimate; "
                    f"{found['idle']} slot{'' if found['idle'] == 1 else 's'} idle"]
    return []


def long_wait(directory: Path, path: Path) -> str | None:
    """The line a waiter that has waited the warn time says (its client's stderr; queue.log, which EVENTS shows):
    how long it has waited, what it waits behind, when it is expected to start, and how to move it up. None when
    it no longer waits."""
    report = long_wait_report(directory, path)
    return report[0] if report else None


def long_wait_report(directory: Path, path: Path) -> tuple[str, _events.WaitingLong] | None:
    """long_wait()'s line and its facts for the request.waiting_long event: {"waited" (seconds), "est_start" (UTC
    stamp or None), "behind": {"earlier", "running"}, "priority" (served), "aged"}."""
    found = look(directory)
    live = found.found
    mine = next((e for p, e in live if p == path), None)
    if mine is None or _entry.is_held(mine):
        return None
    expect = outlook(directory, [e for _, e in live], found.slots, found.other, settings=found.settings).get(id(mine))
    if expect is None:
        return None
    settings = found.settings or _settings.order_settings(directory)
    held, waiting = _entry.split(e for _, e in live)
    serving = _schedule.ranked(waiting, settings, time.time()) if settings["order"] == "backfill" else waiting
    ahead = next((n for n, e in enumerate(serving) if e is mine), 0)
    running = len(held)
    behind = [f"{running} running lease{'s' if running != 1 else ''}"] if running else []
    if ahead:
        behind.insert(0, f"{ahead} earlier request{'s' if ahead != 1 else ''}")
    start = expect["est_start"]
    when = time.strftime("%H:%M", time.localtime(start)) if start is not None else "unknown"
    up = {"batch": "normal", "normal": "interactive"}.get(expect["priority"])
    hint = f"; reprioritize with: slotfather reprioritize {mine.get('pid')} {up}" if up else ""
    served = expect["priority"] + (f", aged from {_entry.priority_of(mine)}" if expect["aged"] else "")
    memory = f"; {expect['memory_wait']}" if expect.get("memory_wait") else ""
    memory += f"; {expect['load_wait']}" if expect.get("load_wait") else ""
    line = (f"{mine.get('label')} (pid {mine.get('pid')}, {served}) waiting {_config.duration(expect['waited'] or 0)} "
            f"behind {' and '.join(behind) or 'nothing'}: est. start {when}{memory}{hint}")
    return line, {"waited": round(expect["waited"] or 0), "est_start": iso(start),
                  "behind": {"earlier": ahead, "running": running}, "priority": expect["priority"],
                  "aged": bool(expect["aged"])}


def held_slots(live: list[dict[str, Any]], legacy: list[tuple[Path, dict[str, Any], int]], slots: int) -> int:
    """The slots held now: each held lease's (an `-n all` one's at least the capacity, schedule.holds()) and the
    earlier queues'."""
    return sum(_entry.holds(e, slots) for e in live if _entry.is_held(e)) + sum(c for _, _, c in legacy)


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
                return _queue.process_table(directory, log=False)
            return table
        except OSError:
            pass
    return _queue.process_table(directory, log=False)


def pid_look(directory: Path | None, pid: int | None) -> Look:
    """`status --json`'s look: its process table from status_processes() (the native one where every holder can
    be seen)."""
    directory = directory or _config.state_dir()
    return look(directory, lambda: status_processes(directory, pid))


def status_json(directory: Path | None = None, pid: int | None = None, look: Look | None = None) -> dict[str, Any]:
    """The queue for programs (`status --json`): every live entry with its lease id (the entry's name), the lease
    path (what SLOTFATHER_LEASE holds), holder pid and process group, its live process tree and its metadata. With
    `pid`, only the entries that process belongs to, each with `via`: "tree" (the holder or a descendant) or "env"
    (it left the tree, by a double fork or setsid and launchd adopting it, but its environment carries the lease's
    SLOTFATHER_LEASE; macOS shows that only for the user's own processes that are not Apple platform binaries), and
    `query`: the pid and its start time as `ps -o lstart=` gives it (None: not running), for a guard to check again
    that it is the same process before it acts."""
    if look is None:  # status --json --full passes the look the dashboard block reads too (#20)
        look = pid_look(directory, pid)
    directory, table, slots = look.directory, look.table, look.slots
    live = look.found
    strange = list(look.unknown)
    kept = list(look.reserved)
    marked = {id(entry): item.get("idle_since") for (_, entry), item in zip(look.found, look.live)}
    entries_found: list[dict[str, Any]] = []
    for path, entry in live:
        item = entry_item(path, entry, table)
        item.update(yield_asked=look.asks.get(path.stem), position=None, idle_since=marked.get(id(entry)))
        item["phase"] = _entry.phase(entry, bool(item["yield_asked"]), item["idle_since"]).name  # fathers#19
        if isinstance(entry.get("session"), dict):
            from .session import view
            item["session"] = view(path, entry)
        entries_found.append(item)
    settings = look.settings or _settings.order_settings(directory)
    moment = time.time()
    waiting = [e for _, e in live if _entry.is_waiting(e)]
    serving = _schedule.ranked(waiting, settings, moment) if settings["order"] == "backfill" else waiting
    places = {id(e): n for n, e in enumerate(serving, 1)}
    earlier = look.legacy
    # A guard asking about one process (--pid) needs no forecast.
    expect = outlook(directory, [e for _, e in live], slots, sum(c for _, _, c in [*earlier, *kept]), moment,
                     look.settings) \
        if pid is None else {}
    known = None
    if pid is None:  # expected runs per run type (fathers#106); not for a guard asking about one process
        with contextlib.suppress(Exception):
            from . import runtype as _runtype
            known = _runtype.cached(directory)
    mem_est = _queue.memory_estimator(directory) if (pid is None and live) else None
    default_mb = float(settings.get("memory_default_mb", _config.ORDER_DEFAULTS["memory_default_mb"]))
    for (_, entry), item in zip(live, entries_found):
        if known is not None:
            with contextlib.suppress(Exception):  # the dashboard's poll never fails for it
                from . import runtype as _runtype
                item.update(_runtype.entry_view(entry, known, moment))
        if mem_est is not None:
            with contextlib.suppress(Exception):
                peak = mem_est(entry)
                item["est_memory_mb"] = round(peak if peak is not None else default_mb, 1)
                item["est_memory_source"] = "history" if peak is not None else "default"
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
                    long_wait=bool(found and found["long_wait"]), memory_wait=found["memory_wait"] if found else None,
                    load_wait=found.get("load_wait") if found else None)
        if found and found.get("blocked_by"):  # fathers#160: added only where a head is blocked
            item.update(blocked_by=found["blocked_by"], idle_slots=found["idle"])
        if found and found.get("gave_way"):
            item["gave_way"] = found["gave_way"]
    legacy: list[dict[str, Any]] = []
    for path, entry, count in earlier:
        legacy.append({"id": path.stem, "lease": str(path), "state": "held", "count": count,
                       "label": entry.get("label"), "pid": entry.get("pid"), "pgid": _platform.process_group(entry.get("pid")),
                       "tree": _lineage.descendants(table, entry.get("pid"))})
    held = held_slots([entry for _, entry in live], [*earlier, *kept], slots)
    # Entries this client does not know, after the others: left alone; a live one's slots count as held (`holds`).
    entries_found += [unknown_item(path, entry, table) for path, entry in strange]
    found = {}
    if pid is not None:
        # The one membership rule (process.lease_membership(), fathers#38): `procs` and the sweep decide the same way.
        both: list[dict[str, Any]] = [*entries_found, *legacy]
        membership = _process.lease_membership(_lineage.parents_of(table),
                                               [_process.Lease(str(item["lease"]), item["pid"], n < len(entries_found))
                                                for n, item in enumerate(both)], [pid])
        via = dict(membership.leases.get(pid, []))
        mine = [(n < len(entries_found), {**item, "via": via[n]}) for n, item in enumerate(both) if n in via]
        entries_found = [item for current, item in mine if current]
        legacy = [item for current, item in mine if not current]
        found["query"] = {"pid": pid, "start": table[pid][1] if pid in table else None}
        if membership.unknown:  # its environment cannot be read: in or out of the leases outside its tree is unknown
            found["lease_unknown"] = membership.unknown
    return {"v": 2, "slots": slots, "held": held, "free": max(0, slots - held), "draining": draining(held, slots),
            "entries": entries_found,
            "legacy": legacy, **found}


def available(directory: Path | None = None, label: str = "", command: str = "") -> int:
    """How many slots a new request fits by queue order, before memory checks: the free ones less what waiters take first, or none while a
    waiter past the overtaking window holds back everyone behind it, unless (backfill) the request is expected to
    end before that waiter can start. Without `command` its run counts as unknown (the fallback)."""
    directory = directory or _config.state_dir()
    live, legacy, _, basis = locked_snapshot(directory)
    held, waiters = _entry.split(live)
    other = sum(count for _, _, count in legacy)
    slots = int(basis["slots"])
    return _schedule.room(waiters, max(0, slots - held_slots(live, legacy, slots)),
                          held, _queue.estimator(directory), other, _settings.order_settings(), label=label,
                          command=command, slots=slots, moment=time.time(), window=_config.overtake_window())


def size(maximum: int, cores: int, total: int, averages: tuple[float, float, float], held: int,
         waiting: bool = False, exclusive: bool = False) -> dict[str, Any]:
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
           averages: tuple[float, float, float] | None = None, label: str = "", command: str = "") -> dict[str, Any]:
    """size() for the queue as it is now: slots held by others (the caller's own lease is not one) and
    whether a request waits, a request for `command` under `label` when given (else one of unknown run), with
    "run_estimate", the run the history expects of it (None: unknown, counted as the fallback)."""
    live, legacy, table, basis = locked_snapshot()
    mine = _queue.inherited_lease(table)
    holders = [e for e in live if _entry.is_held(e) and not (mine and e["pid"] == mine[1]["pid"])]
    other = sum(count for path, _, count in legacy if not (mine and path == mine[0]))
    slots = int(basis["slots"])
    held = sum(_entry.holds(e, slots) for e in holders) + other
    waiters = [e for e in live if _entry.is_waiting(e)]
    estimate = _queue.estimator(_config.state_dir())
    waiting = bool(waiters) and _schedule.room(waiters, max(0, slots - held), holders, estimate, other,
                                               _settings.order_settings(), label=label, command=command, slots=slots,
                                               moment=time.time(), window=_config.overtake_window()) == 0
    machine = cores_now or _config.cores()
    averages = averages or _platform.loads()
    found = size(maximum or slots, machine, slots, averages, min(held, slots), waiting, exclusive)
    found["run_estimate"] = estimate({"label": label, "cmd": command}) if command else None
    # How the capacity was found (#653), and why memory holds new leases back now, at warning or worse (None: it
    # does not), with the level ("normal", "warning": one 1-slot lease at a time, "short"); the job count does not
    # shrink for it: a caller that starts memory-heavy workers should.
    memory_kind, memory_why = _platform.memory_level()
    found.update(capacity_mode=basis["mode"], gib_per_slot=basis["gib_per_slot"], memory_gib=basis["memory_gib"],
                 memory_short=memory_why, memory_level=memory_kind)
    # fathers#184: why sustained CPU load holds new leases beside running ones now (None: it does not, or nothing is
    # held, and an idle machine starts its first request); like memory, it does not shrink the job count.
    found["load_block"] = _schedule.load_wait(averages, machine, slots, min(held, slots),
                                              _settings.order_settings()) if held else None
    return found
