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

from . import config as _config
from . import lineage as _lineage
from . import platform as _platform
from . import process as _process
from .sweep_plan import LINUX, Cwd, Item, Proc, Snapshot, removed_dir, restart_loops

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
    found: dict[int, Proc] = {}
    for line in text.splitlines():
        parts = line.split(None, 10)
        if len(parts) < 10 or not all(p.isdigit() for p in parts[:4]):
            continue
        start = " ".join(parts[5:10])
        tty = "" if parts[4] in ("??", "?", "-") else parts[4]
        found[int(parts[0])] = Proc(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), tty, start,
                                    _platform.started_at(start), parts[10].strip() if len(parts) > 10 else "")
    return found


def working_dirs(procs: dict[int, Proc], uid: int) -> tuple[dict[int, Cwd], list[str]]:
    """The working directory of each of the user's processes, and notes on what could not be read. macOS: one
    lsof; Linux: /proc/PID/cwd, which says " (deleted)" for a removed one."""
    found: dict[int, Cwd] = {}
    notes: list[str] = []
    if LINUX:
        found, notes, _ = proc_cwds(procs, uid)
        return found, notes
    try:
        result = subprocess.run(["lsof", "-a", "-d", "cwd", "-u", str(uid), "-w", "-Fpin"], capture_output=True,
                                text=True, timeout=TIMEOUT * 3)
    except (OSError, subprocess.TimeoutExpired) as error:
        return found, [f"working directories unknown (lsof: {error})"]
    pid: int | None = None
    inode: int | None = None
    for line in result.stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid, inode = (int(value) if value.isdigit() else None), None
        elif tag == "i":
            inode = int(value) if value.isdigit() else None
        elif tag == "n" and pid is not None:
            found[pid] = Cwd(value, removed_dir(value, inode))
    return found, notes


def proc_cwds(procs: dict[int, Proc], uid: int) -> tuple[dict[int, Cwd], list[str], set[int]]:
    """Linux: the working directories from /proc/PID/cwd, notes, and the pids in another mount namespace (a
    container: rootless podman, `docker run --user $(id -u)`), whose paths mean nothing here; those are left out
    and never candidates. A directory is gone only when the kernel says so: " (deleted)" or no links left."""
    found: dict[int, Cwd] = {}
    notes: list[str] = []
    foreign: set[int] = set()
    try:
        own = os.readlink(_platform.proc_path("self", "ns", "mnt"))
    except OSError:
        own = None
    for pid, proc in procs.items():
        if proc.uid != uid:
            continue
        try:
            if own is not None and os.readlink(_platform.proc_path(str(pid), "ns", "mnt")) != own:
                foreign.add(pid)
                continue
            link = os.readlink(_platform.proc_path(str(pid), "cwd"))
        except OSError:
            if own is not None:
                foreign.add(pid)  # its namespace cannot be read: do not judge it
            continue
        if link.endswith(" (deleted)"):
            found[pid] = Cwd(link[:-len(" (deleted)")], True)
            continue
        try:
            removed = os.stat(_platform.proc_path(str(pid), "cwd")).st_nlink == 0
        except OSError:
            removed = None
        found[pid] = Cwd(link, removed)
    if foreign:
        notes.append(f"{len(foreign)} process(es) in another mount namespace (a container) left out")
    return found, notes, foreign


def managed_jobs(procs: dict[int, Proc], uid: int) -> set[int]:
    """Processes a service manager runs on purpose: launchd jobs of this user (macOS), systemd services' main
    processes (Linux). They have ppid 1 like an orphan; a service is not one."""
    found: set[int] = set()
    if not LINUX:
        # `launchctl list` answers for the caller's domain only (from ssh or cron not the GUI one), so the GUI
        # domain's services are asked for by name as well.
        for command in (["launchctl", "list"], ["launchctl", "print", f"gui/{uid}"]):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT)
                found |= launchd_pids(result.stdout, command[1])
        return found
    snap = Snapshot(procs, uid=uid)
    units: dict[tuple[bool, str], int] = {}
    for pid, proc in procs.items():
        if proc.uid != uid or not snap.adopted(proc):
            continue
        try:
            lines = _platform.proc_path(str(pid), "cgroup").read_text().splitlines()
        except OSError:
            continue
        key = service_of(lines)
        if key is None:
            continue
        if key not in units:
            units[key] = main_pid(*key)
        if units[key] in (pid, -1):
            found.add(pid)
    return found


def launchd_pids(text: str, how: str) -> set[int]:
    """The pids of running jobs in `launchctl list` (PID first on each line) or `launchctl print gui/UID` (its
    services block: pid, status, label) output."""
    found: set[int] = set()
    if how == "list":
        for line in text.splitlines()[1:]:
            first = line.split("\t", 1)[0].strip()
            if first.isdigit() and int(first) > 0:
                found.add(int(first))
        return found
    inside = False
    for line in text.splitlines():
        if line.strip() == "services = {":
            inside = True
        elif inside and line.strip() == "}":
            inside = False
        elif inside:
            first = (line.split() or [""])[0]
            if first.isdigit() and int(first) > 0:
                found.add(int(first))
    return found


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
    if LINUX:
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
    usage = None
    with contextlib.suppress(_config.SlotError, OSError):
        usage = _platform.swap_usage()
    pressure = None
    if not _config.setting("MEMORY_FILE"):
        with contextlib.suppress(_config.SlotError, OSError):
            pressure = _platform.pressure_level()
    found: dict[str, Any] = {"swap_total_mb": None if usage is None else round(usage[0]),
                             "swap_free_mb": None if usage is None else round(usage[1]),
                             "pressure": pressure, "short": None}
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
    found: dict[str, Any] = {**facts, "short": short, "top": []}
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
        if LINUX and proc.uid == snap.uid:
            with contextlib.suppress(OSError, ValueError):
                for line in _platform.proc_path(str(proc.pid), "status").read_text().splitlines():
                    if line.startswith("VmSwap:"):
                        entry["swap_mb"] = round(int(line.split()[1]) / 1024)
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
