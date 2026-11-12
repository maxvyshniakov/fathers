"""The dashboard's model: what the main screen shows, gathered from one look at the queue under its lock (#20).

The in-tree dashboard draws it (dashboard.dashboard_draw()), and `slotfather status --json --full` prints it as its
`dashboard` block (dashboard_json()) for Godfather, which renders the same screen without importing Slotfather. The
model is scheduler facts and plain data only: no terminal, theme or layout code lives here.
"""
from __future__ import annotations

import contextlib
import datetime
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, cast

from . import config as _config
from . import platform as _platform
from . import query as _query
from . import schedule as _schedule
from . import state as _state

BLOCK_VERSION = 1  # the `dashboard` block of `status --json --full`
HISTORY_WINDOW = 3600  # seconds of queue.log the utilisation strip rebuilds
LOG_TAIL = 262144  # bytes of queue.log read per refresh
EVENT_HISTORY = 600  # history lines read to describe the events shown
EVENT_MATCH = 2.0  # seconds between a queue.log line and the history stamp of the same moment
JOINER = "‍"  # the zero-width joiner of an emoji sequence

GRANTED = re.compile(r"granted (\d+) to (.*) \(pid (\d+)\)$")
RELEASED = re.compile(r"released (\d+) \(\d+-(\d+)\.json\)$")
REAPED = re.compile(r"reaped (\S+) lease of (\d+) for (.*) \(pid (\d+) exited\)$")
REPRIORITIZED = re.compile(r"reprioritized (.*) \(pid (\d+)\) from (\w+) to (\w+)$")
WAITING_LONG = re.compile(r"waiting long: (.*) \(pid (\d+), [^)]*\) waiting (\S+) behind .*: est\. start (\S+)")

Steps = list[tuple[datetime.datetime, datetime.datetime, int]]


def dashboard_text(value: object) -> str:
    """A label or command as plain text: any process writes queue entries, so control bytes are blanked. The
    zero-width joiner of an emoji sequence (👨‍🦳) stays."""
    return "".join(char if char.isprintable() or char == JOINER else " " for char in str(value))


def span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds // 60 % 60:02}m"
    return f"{seconds // 86400}d{seconds // 3600 % 24:02}h"


def parse_utc(value: object) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(cast(str, value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def process_usage() -> tuple[dict[int, tuple[float, int, str]], dict[int, list[int]]]:
    """pid -> (CPU percent, resident KB, command name) for every process, and ppid -> its child pids."""
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,%cpu=,rss=,comm="], capture_output=True, text=True)
    if result.returncode:
        raise OSError(f"CPU process sample failed: ps exited {result.returncode}")
    own: dict[int, tuple[float, int, str]] = {}
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
            with contextlib.suppress(ValueError):
                own[int(parts[0])] = (float(parts[2].replace(",", ".")), int(parts[3]), os.path.basename(parts[4]))
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return own, children


def process_tree(pid: object, own: dict[int, tuple[float, int, str]],
                 children: dict[int, list[int]]) -> list[tuple[int, float, int, str]]:
    """(pid, CPU percent, resident KB, name) for pid and all its descendants: a lease's whole command."""
    found: list[tuple[int, float, int, str]] = []
    stack: list[object] = [pid]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if not isinstance(current, int) or current in seen or current not in own:
            continue
        seen.add(current)
        found.append((current, *own[current]))
        stack.extend(children.get(current, ()))
    return found


def macos_used_memory(text: str) -> float | None:
    """Physical MB in active, wired and compressor pages; inactive cache is excluded."""
    page = re.search(r"page size of (\d+) bytes", text)
    fields = dict(re.findall(r"^(Pages [^:]+):\s+(\d+)\.", text, re.MULTILINE))
    names = ("Pages active", "Pages wired down", "Pages occupied by compressor")
    if not page or any(name not in fields for name in names):
        return None
    return sum(int(fields[name]) for name in names) * int(page[1]) / (1024 * 1024)


def memory_facts() -> dict[str, Any]:
    """Free and total swap in MB and the kernel's memory-pressure level, as far as this host reports them."""
    seam = _config.setting("MEMORY_FILE")
    if seam:
        with contextlib.suppress(OSError, ValueError):
            free, level = (float(v) for v in Path(seam).read_text().split()[:2])
            return {"swap_free": free, "swap_total": None, "level": level}
        return {"swap_free": None, "swap_total": None, "level": None}
    if _platform.linux():  # no pressure level: MemAvailable and PSI, as the memory policy reads them
        facts = cast(dict[str, Any], cast(Any, _platform).linux_memory())
        return {"swap_total": facts["swap_total"], "swap_free": facts["swap_free"], "level": None,
                "available": facts["available"], "total": facts["total"], "psi_some": facts["psi_some"],
                "psi_full": facts["psi_full"], "cgroup_available": facts.get("cgroup_available"),
                "cgroup": facts.get("cgroup")}
    total, free = _platform.swap_usage() or (None, None)
    found: dict[str, Any] = {"swap_total": total, "swap_free": free, "level": _platform.pressure_level()}
    with contextlib.suppress(OSError, ValueError, subprocess.TimeoutExpired):
        physical = _platform.sysctl("hw.memsize")
        stats = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2)
        if physical is not None:
            found.update(total=int(physical) / (1024 * 1024), used=macos_used_memory(stats.stdout))
    return found


def memory_low(memory: dict[str, Any]) -> bool | None:
    """Whether RAM or the kernel's pressure says memory is low (MACHINE's 🔥 on mem); None where it is unknown. Swap
    has a status of its own (swap_low())."""
    level = memory.get("level")
    if level is not None:
        return bool(level > _config.MAX_PRESSURE_LEVEL)
    if any(memory.get(key) is not None for key in ("available", "cgroup_available", "psi_some", "psi_full")):
        facts = dict(memory, swap_total=0, swap_free=None)
        for key in ("available", "psi_some", "psi_full"):
            facts.setdefault(key, None)
        return bool(cast(Any, _platform).linux_short(facts))
    return None


def psi_low(memory: dict[str, Any]) -> bool:
    """Linux: PSI memory some or full at or over SLOTFATHER_MAX_PSI_SOME / _FULL, so MACHINE's mem row shows the
    pressure that holds admission back (`psi N%`); False where PSI is not reported (macOS)."""
    limits = (_config.number_setting("MAX_PSI_SOME", _config.MAX_PSI_SOME),
              _config.number_setting("MAX_PSI_FULL", _config.MAX_PSI_FULL))
    values = (memory.get("psi_some"), memory.get("psi_full"))
    return any(isinstance(value, (int, float)) and value >= limit for value, limit in zip(values, limits))


def swap_low(memory: dict[str, Any]) -> bool:
    """Free swap under SLOTFATHER_MIN_FREE_SWAP_MB, where swap is on and its free amount known."""
    if not float(memory.get("swap_total") or 0) or memory.get("swap_free") is None:
        return False
    return float(memory["swap_free"]) < _config.number_setting("MIN_FREE_SWAP_MB", _config.MIN_FREE_SWAP_MB)


def queue_events(directory: Path) -> list[dict[str, Any]]:
    """The grants, releases and reaps at the end of queue.log, oldest first. Each event carries `delta`, the change
    it made to held slots. A release whose grant precedes the part read is taken to end a held lease; one whose
    pid was never granted in a fully read log is a waiter that gave up."""
    try:
        with open(directory / "queue.log", "rb") as handle:
            start = max(0, handle.seek(0, os.SEEK_END) - LOG_TAIL)
            handle.seek(start)
            lines = handle.read().decode("utf-8", "replace").splitlines()[1 if start else 0:]
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    granted: dict[int, str] = {}
    for line in lines:
        moment, _, message = line.partition(" ")
        when = parse_utc(moment)
        if when is None:
            continue
        if found := GRANTED.match(message):
            count, label, pid = int(found[1]), found[2], int(found[3])
            granted[pid] = label
            events.append({"when": when, "verb": "granted", "count": count, "label": label, "pid": pid,
                           "delta": count})
        elif found := RELEASED.match(message):
            count, pid = int(found[1]), int(found[2])
            held = pid in granted or start > 0
            events.append({"when": when, "verb": "released" if held else "left", "count": count,
                           "label": granted.pop(pid, ""), "pid": pid, "delta": -count if held else 0,
                           # granted before the part read, or never: describe_events() asks its history line
                           "unsure": held and pid not in granted})
        elif found := REAPED.match(message):
            state, count, label, pid = found[1], int(found[2]), found[3], int(found[4])
            granted.pop(pid, None)
            events.append({"when": when, "verb": "reaped", "count": count, "label": label, "pid": pid,
                           "delta": -count if state == "held" else 0, "state": state})
        elif found := REPRIORITIZED.match(message):  # slotfather reprioritize: a class, not slots
            events.append({"when": when, "verb": "priority", "count": 0, "label": found[1], "pid": int(found[2]),
                           "delta": 0, "detail": f"{found[3]} → {found[4]}"})
        elif found := WAITING_LONG.match(message):  # a long wait, said by the waiter itself
            events.append({"when": when, "verb": "waiting", "count": 0, "label": found[1], "pid": int(found[2]),
                           "delta": 0, "detail": f"{found[3]}, est. {found[4]}"})
    return events


def describe_events(events: list[dict[str, Any]], directory: Path, live: list[dict[str, Any]]) -> None:
    """Adds each event's `detail` from the history line of its request (state.record() writes it as the request
    leaves): `waited 3m00s` for a grant (a running lease's from its queue entry), `exit 1, ran 4m00s` for a release,
    `gave up after 15m00s` for a waiter that left, `owner exited, ran 5m55s` for a reap. The history line also
    settles whether a release was a lease ending or a waiter giving up, which queue.log alone cannot say once it is
    longer than the part queue_events() reads; a waiter that gave up changed no held slots."""
    by_pid: dict[int, list[dict[str, Any]]] = {}
    history = cast(list[dict[str, Any]], cast(Any, _state).tail_objects(directory / "history.jsonl", EVENT_HISTORY))
    for item in history:
        pid = item.get("pid")
        if isinstance(pid, int):
            by_pid.setdefault(pid, []).append(item)
    running = {cast(int, entry["pid"]): entry for entry in live if isinstance(entry.get("pid"), int)}

    def near(stamp: Any, when: datetime.datetime) -> bool:
        moment = parse_utc(stamp)
        return moment is not None and abs((moment - when).total_seconds()) <= EVENT_MATCH

    for event in events:
        if event["verb"] in ("priority", "waiting"):  # their detail is their own line's
            continue
        event["detail"] = ""
        verb, when = event["verb"], event["when"]
        if verb == "granted":
            item = next((i for i in by_pid.get(event["pid"], []) if near(i.get("granted"), when)), None)
            if item is None and event["pid"] in running and near(running[event["pid"]].get("granted"), when):
                since, granted = parse_utc(running[event["pid"]].get("since")), when
                waited = None if since is None else (granted - since).total_seconds()
            else:
                waited = item.get("waited") if item else None
            if isinstance(waited, (int, float)):
                event["detail"] = f"waited {span(waited)}" if waited >= 1 else "no wait"
            continue
        item = next((i for i in by_pid.get(event["pid"], []) if near(i.get("ended"), when)), None)
        if item is None and event.get("unsure"):
            # No grant in the part of queue.log read and no history line that it held slots: a request that left
            # without one (queue.log has its release, never a grant). Counting it as a lease that ended put slots
            # back into the chart's past that nothing held (`peak 9` of 8).
            event.update(verb="left", delta=0, detail="never granted")
            continue
        if item is None:
            event["detail"] = "owner exited" if verb == "reaped" else "gave up" if verb == "left" else ""
            continue
        ran = item.get("ran")
        ran_text = f"ran {span(ran)}" if isinstance(ran, (int, float)) else ""
        if verb == "reaped":
            event["detail"] = ", ".join(part for part in ("owner exited", ran_text) if part)
        elif item.get("outcome") == "left":
            event.update(verb="left", delta=0, label=event["label"] or str(item.get("label") or ""))
            waited = item.get("waited")
            event["detail"] = f"gave up after {span(waited)}" if isinstance(waited, (int, float)) else "gave up"
        else:
            status = item.get("status")
            event["detail"] = ", ".join(part for part in (
                f"exit {status}" if isinstance(status, int) else "", ran_text) if part)
            event["label"] = event["label"] or str(item.get("label") or "")


def held_history(events: list[dict[str, Any]], held_now: int, now_moment: datetime.datetime,
                 window: float = HISTORY_WINDOW * 2) -> Steps:
    """Held slots over the last `window` seconds as (start, end, slots) steps, walked back from held_now."""
    begin = now_moment - datetime.timedelta(seconds=window)
    steps: Steps = []
    level, end = held_now, now_moment
    for event in reversed(events):
        if event["when"] <= begin:
            break
        if event["delta"] and event["when"] < end:
            steps.append((event["when"], end, max(0, level)))
            end = event["when"]
        level -= event["delta"]
    steps.append((begin, end, max(0, level)))
    return steps[::-1]


def dashboard_admission(waiters: list[dict[str, Any]], held: list[dict[str, Any]], slots: int, other: int,
                        memory_block: str | None, estimate: _schedule.Estimate | None, settings: dict[str, Any],
                        memory_kind: str = "short") -> int:
    """Largest generic request grant() would admit, including earlier grants consuming the memory exemption; under a
    memory-pressure warning (`memory_kind`) at most one slot, none beside a lease granted under pressure."""
    used = sum(_schedule.holds(entry, slots) for entry in held) + other
    free = max(0, slots - used)
    if free and memory_block and used and memory_kind == "warning" and \
            float(settings.get("warning_admits", 1)) >= 1:  # schedule.memory_gate()
        if any(entry.get(_schedule.PRESSED) for entry in held):
            return 0
        return min(1, _schedule.room(waiters, free, held, estimate, other, settings=settings, slots=slots)
                   if waiters else free)
    if not free or memory_block and used:
        return 0
    if not waiters:
        return free
    if memory_block:
        # On an idle host only the first planned grant bypasses the memory gate.
        # A new generic request may rank ahead of a lower-priority existing waiter.
        probe: dict[str, Any] = dict(count=free, label="", cmd="", since=_config.now())
        found = _schedule.plan(waiters + [probe], held, slots, time.time(), estimate=estimate,
                               other=other, settings=settings)
        return free if found.grants and found.grants[0] is probe else 0
    return _schedule.room(waiters, free, held, estimate, other, settings=settings, slots=slots)


def dashboard_model(look: _query.Look | None = None) -> dict[str, Any]:
    """The main screen's facts from one look at the queue (query.look(), taken now when not given): the lease and
    waiter rows in the order the screen shows them, capacity, admission, the machine and the recent events."""
    look = look or _query.look()
    directory = look.directory
    live = look.live
    legacy = look.legacy
    slots: int = look.basis["slots"]
    saved: bool = look.basis["saved"]
    own, children = process_usage()

    def item(entry: dict[str, Any], kind: str, count: int, queue: str = "") -> dict[str, Any]:
        tree = process_tree(entry.get("pid"), own, children) if kind != "waiting" else []
        foreseen: dict[str, Any] = expected.get(id(entry)) or {}  # query.outlook(): its class and expected start
        # The yield request on this entry's own marker, read under the queue lock (#20: by entry, not by pid)
        request = look.asked_of(entry) or {}
        return {"tree": sorted(tree, key=lambda process: -process[1]), "cpu": sum(p[1] for p in tree),
                "rss": sum(p[2] for p in tree), "procs": len(tree), "key": (entry.get("pid"), entry.get("since")),
                "kind": kind, "count": count, "queue": queue,
                "label": dashboard_text(entry.get("label", "")), "pid": entry.get("pid"),
                "cmd": dashboard_text(entry.get("cmd", "")), "since": parse_utc(entry.get("since")),
                "meta": dashboard_text(_config.meta_text(_config.meta_of(entry))),
                "context": _config.meta_of(entry), "workdir": entry.get("cwd") or "",
                "priority": _config.priority_of(entry), "ticket": _config.ticket_of(entry),
                "yieldable": bool(entry.get("yieldable")), "yields": int(entry.get("yields") or 0),
                "asked": dashboard_text(request.get("label") or "?") if request else "",
                "asked_at": parse_utc(request.get("at")) if request else None,
                # #12: since when the holder's record says the lease has been idle (query.look()).
                "idle_since": datetime.datetime.fromtimestamp(entry["idle_since"], datetime.timezone.utc)
                if isinstance(entry.get("idle_since"), (int, float)) else None,
                "served": foreseen.get("priority") or
                (entry.get("aged") if entry.get("aged") in _config.PRIORITIES else None),
                "reprioritized": entry.get("reprioritized") if entry.get("reprioritized") in _config.PRIORITIES else None,
                "est_start": datetime.datetime.fromtimestamp(foreseen["est_start"], datetime.timezone.utc)
                if foreseen.get("est_start") is not None else None,
                "granted": parse_utc(entry.get("granted")), "passable": kind == "waiting" and bool(cast(Any, _schedule).overtakable(entry)),
                "all": bool(entry.get("all")),
                "style": "text" if kind == "waiting" else "legacy"}

    # The class each waiter is served in and its expected start (query.outlook(); nothing when it cannot be read).
    expected: dict[int, dict[str, Any]] = {}
    with contextlib.suppress(Exception):
        expected = _query.outlook(directory, live, slots, sum(count for _, _, count in legacy))
    # An `-n all` lease holds the whole capacity, also one grown under it (#7).
    holders = [item(e, "held", _schedule.holds(e, slots)) for e in live if e["state"] == "held"]
    holders += [item(e, "legacy", count, str(path.parent.parent)) for path, e, count in legacy]
    settings = _schedule.order_settings(directory)
    start: datetime.datetime | None = None
    reserved: datetime.datetime | None = None
    order = settings["order"]
    live_waiters = [e for e in live if e["state"] != "held"]
    _schedule.fit(live_waiters, slots)  # as the queue grants them when the capacity shrank under them
    if order == "backfill":
        live_waiters = _schedule.ranked(live_waiters, settings, time.time())  # the order the queue serves them in
    waiters = [item(e, "waiting", e["count"]) for e in live_waiters]
    used = sum(h["count"] for h in holders)
    if order == "backfill" and live_waiters:
        with contextlib.suppress(Exception):  # an estimate that cannot be read leaves the sentence without a time
            found = _schedule.plan(live_waiters, [e for e in live if e["state"] == "held"], slots, time.time(), order,
                                   _schedule.estimator(directory), sum(count for _, _, count in legacy),
                                   reserve_head=True, settings=settings)
            if found.head is not None and found.start is not None:
                start = datetime.datetime.fromtimestamp(found.start, datetime.timezone.utc)
            if found.head is not None and found.reserved is not None:
                reserved = datetime.datetime.fromtimestamp(found.reserved, datetime.timezone.utc)
    (memory_kind, memory_block), machine, averages = _platform.memory_level(), _config.cores(), _platform.loads()
    if memory_kind == "warning" and float(settings.get("warning_admits", 1)) >= 1:
        memory_block = _schedule.WARNING_WAIT
    events = queue_events(directory)
    describe_events(events, directory, live)
    moment = datetime.datetime.now(datetime.timezone.utc)
    free = max(0, slots - used)
    admission = dashboard_admission(live_waiters, [e for e in live if e["state"] == "held"], slots,
                                    sum(count for _, _, count in legacy), memory_block,
                                    _schedule.estimator(directory) if live_waiters else None, settings, memory_kind)
    own_held = sum(h["count"] for h in holders if h["kind"] == "held")
    return {"directory": str(directory), "slots": slots, "saved": saved, "basis": look.basis, "cores": machine,
            "averages": averages, "draining": _query.draining(used, slots),
            "advice": cast(dict[str, Any], cast(Any, _query).size(slots, machine, slots, averages, min(used, slots),
                                                                  bool(waiters) and admission == 0)),
            "holders": holders, "waiters": waiters, "used": used, "free": free, "memory_block": memory_block,
            "admission": admission,
            "order": order, "start": start, "reserved": reserved, "limit": settings["limit"],
            "memory": memory_facts(), "history": held_history(events, own_held, moment), "events": events[-60:],
            "updated": moment}


def stamp(moment: datetime.datetime) -> str:
    """A moment as RFC 3339 UTC with milliseconds: 2026-11-12T02:24:16.204Z."""
    utc = moment.astimezone(datetime.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def plain(value: Any) -> Any:
    """A model value as JSON data: a datetime as stamp(), a tuple as an array, a float that is not finite as null."""
    if isinstance(value, datetime.datetime):
        return stamp(value)
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in cast(dict[Any, Any], value).items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in cast(list[Any], value)]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


PRESENTATION = ("style", "lane")  # an item's fields that are the renderer's, never part of the block


def dashboard_json(model: dict[str, Any]) -> dict[str, Any]:
    """The `dashboard` block of `status --json --full` (docs/api.md): the model as plain JSON data with its own
    `v`, the rows without their presentation fields, and the policy facts the screen reads from Slotfather's
    config: the overtaking window and whether memory, swap and PSI are low."""
    block = cast(dict[str, Any], plain({key: value for key, value in model.items() if key not in ("holders", "waiters")}))
    for name in ("holders", "waiters"):
        block[name] = [plain({key: value for key, value in row.items() if key not in PRESENTATION})
                       for row in cast(list[dict[str, Any]], model[name])]
    memory = cast(dict[str, Any], model["memory"])
    block["memory"] = dict(block["memory"], low=memory_low(memory), swap_low=swap_low(memory),
                           psi_low=psi_low(memory))
    block["overtake_window"] = _config.overtake_window()
    block["v"] = BLOCK_VERSION
    return block


def full_status(directory: Path | None = None) -> dict[str, Any]:
    """`status --json --full`: `status --json` (version 2, as it is for older readers) and the dashboard block,
    both from one look at the queue, with the host and the time."""
    from .events import host
    look = _query.look(directory)
    found = _query.status_json(look.directory, look=look)
    found.update(host=host(), time=stamp(datetime.datetime.now(datetime.timezone.utc)),
                 dashboard=dashboard_json(dashboard_model(look)))
    return found

