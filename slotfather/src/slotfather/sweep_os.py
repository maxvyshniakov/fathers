"""The sweep's adapters to the machine (fathers#38): its process table, working directories (lsof, /proc), the
jobs launchd and systemd run, Docker's containers, the memory facts, and stopping a process tree. sweep_plan
decides on what these return; `slotfather sweep` (sweep.py) runs them."""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from typing import Any, Callable, Mapping, cast

from fathers_common import proc as _proc

from . import config as _config
from . import lineage as _lineage
from . import platform as _platform
from . import process as _process
from .sweep_plan import Cwd, Item, Proc, Snapshot, removed_dir, restart_loops

TIMEOUT = 10  # seconds a ps, lsof, launchctl or docker call may take


class SweepError(Exception):
    pass


def table() -> dict[int, Proc]:
    """Every process: one ps over the machine in the C locale (lstart in the words every client writes). A ps that
    fails, times out or leaves out the sweep gives no table (process.table(), fathers#121): this sweep stops
    nothing."""
    try:
        rows = _process.table(["-ax", "-ww", "-o", _process.SWEEP], env=_platform.c_locale(), timeout=TIMEOUT)
    except _process.PsError as error:
        raise SweepError(str(error)) from None
    return parse_table("\n".join(rows))


def parse_table(text: str) -> dict[int, Proc]:
    """`ps -o SWEEP` lines (local lstart) as the sweep's Procs, through the shared reader (fathers_common.proc)."""
    return {pid: Proc(pid, proc.ppid, proc.uid, proc.rss, proc.tty, proc.start, proc.started, proc.cmd)
            for pid, proc in _proc.parse(text, _process.SWEEP, "local").items()}


def working_dirs(procs: dict[int, Proc], uid: int) -> tuple[dict[int, Cwd], list[str]]:
    """The working directory of each of the user's processes, and notes on what could not be read. macOS: one
    lsof; Linux: /proc/PID/cwd, which says " (deleted)" for a removed one."""
    if _platform.linux():
        found, notes, _ = proc_cwds(procs, uid)
        return found, notes
    raw, _, notes = _process.working_dirs(
        {pid: proc.uid for pid, proc in procs.items()},
        uid,
        timeout=TIMEOUT * 3,
        check_removed=removed_dir,
        strict=False,
        use_proc=False,
    )
    return {pid: Cwd(item.path, item.removed) for pid, item in raw.items()}, notes


def proc_cwds(procs: dict[int, Proc], uid: int) -> tuple[dict[int, Cwd], list[str], set[int]]:
    """Linux: the working directories from /proc/PID/cwd, notes, and the pids in another mount namespace (a
    container: rootless podman, `docker run --user $(id -u)`), whose paths mean nothing here; those are left out
    and never candidates. A directory is gone only when the kernel says so: " (deleted)" or no links left."""
    raw, foreign, notes = _process.working_dirs(
        {pid: proc.uid for pid, proc in procs.items()},
        uid,
        strict=False,
        use_proc=True,
    )
    return {pid: Cwd(item.path, item.removed) for pid, item in raw.items()}, notes, foreign


def managed_jobs(procs: dict[int, Proc], uid: int) -> set[int]:
    """Processes a service manager runs on purpose: launchd jobs of this user (macOS), systemd services' main
    processes (Linux). They have ppid 1 like an orphan; a service is not one."""
    if not _platform.linux():
        return _process.launchd_managed_pids(uid, timeout=TIMEOUT)
    found: set[int] = set()
    units: dict[tuple[bool, str], int] = {}
    for pid, proc in procs.items():
        if proc.uid != uid:
            continue
        parent = procs.get(proc.ppid)
        if _process.is_systemd_service_main(
            pid,
            proc.ppid,
            proc.cmd,
            parent.cmd if parent is not None else None,
            uid,
            units,
            fail_closed=False,
            timeout=TIMEOUT,
            run_main_pid=main_pid,
        ):
            found.add(pid)
    return found


launchd_pids = _process.launchd_pids
service_of = _platform.service_of
Run = Callable[..., "subprocess.CompletedProcess[str]"]


def main_pid(user: bool, unit: str, run: Run = subprocess.run) -> int:
    """The unit's MainPID from its manager (`systemctl --user` for a user's), or -1 when it cannot be asked: then
    every process in the service is taken as the service's."""
    return _platform.main_pid(user, unit, run, TIMEOUT)


def snapshot() -> Snapshot:
    procs = table()
    uid = os.geteuid()
    foreign: set[int] = set()
    if _platform.linux():
        cwds, notes, foreign = proc_cwds(procs, uid)
    else:
        cwds, notes = working_dirs(procs, uid)
    me = {os.getpid(), *_lineage.ancestors({pid: proc.ppid for pid, proc in procs.items()}, os.getpid())}
    return Snapshot(procs, cwds, managed_jobs(procs, uid), uid=uid, me=me, notes=notes, foreign=foreign)


# Containers and memory: reports

def containers(settings: Mapping[str, Any]) -> dict[str, Any]:
    """{"available": bool, "items": [...], "note"}: Docker is asked, never started."""
    try:
        listed = subprocess.run(["docker", "ps", "-aq", "--no-trunc"], capture_output=True, text=True,
                                timeout=TIMEOUT)
        if listed.returncode != 0:
            return {"available": False, "items": [], "note": (listed.stderr.strip().splitlines() or ["docker failed"])[0]}
        ids = listed.stdout.split()
        if not ids:
            return {"available": True, "items": []}
        inspected = subprocess.run(["docker", "inspect", *ids], capture_output=True, text=True, timeout=TIMEOUT)
        data: object = json.loads(inspected.stdout or "[]")
    except FileNotFoundError:
        return {"available": False, "items": [], "note": "no docker command"}
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        return {"available": False, "items": [], "note": f"docker: {error}"}
    return {"available": True, "items": restart_loops(cast(list[dict[str, Any]], data) if isinstance(data, list) else [],
                                                      settings)}


def memory_facts() -> dict[str, Any]:
    """Swap and memory now: total and free swap in MB, macOS pressure level when read, and why memory is short
    (None: it is not)."""
    include_pressure = not bool(_config.setting("MEMORY_FILE"))
    snap = _platform.MemorySnapshot()
    with contextlib.suppress(_config.SlotError, OSError):
        snap = _platform.memory_snapshot(include_pressure=include_pressure, honor_memory_file=False)
    found: dict[str, Any] = {
        "swap_total_mb": None if snap.swap_total_mb is None else round(snap.swap_total_mb),
        "swap_free_mb": None if snap.swap_total_mb is None else round(snap.swap_free_mb or 0.0),
        "pressure": snap.pressure_level if include_pressure else None,
        "short": None,
    }
    with contextlib.suppress(_config.SlotError, OSError):
        found["short"] = _platform.memory_short()
    return found


def memory_report(facts: Mapping[str, Any], snap: Snapshot, settings: Mapping[str, Any], items: list[Item],
                  protected: Mapping[int, str]) -> dict[str, Any]:
    """The largest resident processes with their class while memory is short (`short`), else just the facts."""
    total, free = facts.get("swap_total_mb"), facts.get("swap_free_mb")
    short = facts.get("short")
    pressure = facts.get("pressure")
    level = float(pressure) if isinstance(pressure, (int, float)) and not isinstance(pressure, bool) else None
    floor = max(settings["swap_free_mb"], (total or 0) * settings["swap_free_pct"] / 100)
    if short is None and not _platform.macos_pressure_active(level) and total and free is not None and free < floor:
        short = f"free swap {free} of {total} MB is under {floor:.0f} MB ({settings['swap_free_mb']:g} MB or " \
                f"{settings['swap_free_pct']:g}%)"
    found: dict[str, Any] = {**{k: v for k, v in facts.items() if k != "pressure"}, "short": short, "top": []}
    if short is None:
        return found
    classed: dict[int, str] = {}
    for item in items:
        for pid in item.get("tree", ()):
            classed.setdefault(pid, item["class"])
    top = sorted(snap.procs.values(), key=lambda p: -p.rss)[:int(settings["memory_top"])]
    for proc in top:
        label = protected.get(proc.pid)
        age = snap.age(proc)
        entry: dict[str, Any] = {"pid": proc.pid, "rss_mb": round(proc.rss / 1024), "cmd": proc.cmd[:200],
                                 "class": f"lease {label}" if label else classed.get(proc.pid, "-"),
                                 "age": None if age is None else round(age)}
        if _platform.linux() and proc.uid == snap.uid:
            swap_kb = _platform.proc_swap_kb(proc.pid)
            if swap_kb is not None:
                entry["swap_mb"] = round(swap_kb / 1024)
        found["top"].append(entry)
    return found


# Acting

def identities(pids: list[int]) -> dict[int, tuple[str, bool]]:
    """pid -> (lstart in the C locale, a zombie) for those of `pids` still in the process table."""
    return _process.starts(pids, timeout=TIMEOUT)


Look = Callable[[list[int]], Mapping[int, "tuple[str, bool]"]]


def stop(item: Item, snap: Snapshot, grace: float, now: Callable[[], float] = time.monotonic,
         pause: Callable[[float], None] = time.sleep, look: Look | None = None,
         kill: Callable[[int, int], None] = os.kill) -> dict[str, Any]:
    """SIGTERM to the item's tree, SIGKILL to what is left after `grace` seconds; only to pids whose start time is
    the one the sweep saw. Returns {"result": stopped|killed|gone|failed, "signalled": [...], "left": [...]}."""
    seen_by = look or identities
    expected = {p: " ".join(snap.procs[p].start.split()) for p in item["tree"] if p in snap.procs}
    seen = seen_by(list(expected))
    targets = [p for p, start in expected.items() if p in seen and seen[p][0] == start and not seen[p][1]]
    if not targets:
        return {"result": "gone", "signalled": [], "left": []}
    signalled: list[int] = []
    for pid in targets:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            kill(pid, signal.SIGTERM)
            signalled.append(pid)

    def remaining() -> list[int]:
        here = seen_by(targets)
        return [p for p in targets if p in here and here[p][0] == expected[p] and not here[p][1]]
    deadline = now() + grace
    left = remaining()
    while left and now() < deadline:
        pause(0.2)
        left = remaining()
    if not left:
        return {"result": "stopped", "signalled": signalled, "left": []}
    for pid in left:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            kill(pid, signal.SIGKILL)
    deadline = now() + 2
    while left and now() < deadline:
        pause(0.1)
        left = remaining()
    return {"result": "failed" if left else "killed", "signalled": signalled, "left": left}
