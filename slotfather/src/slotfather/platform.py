"""Slotfather platform responsibilities."""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, cast

from . import config as _config


def physical_cores() -> int | None:
    """Logical host CPU count, without execution-budget environment overrides."""
    return os.cpu_count()


def physical_memory_gib() -> float | None:
    """Physical host memory, not cgroup capacity; None when unavailable (the lifetime policy applies its fallback)."""
    with contextlib.suppress(ValueError, OSError, AttributeError):
        value = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1 << 30)
        return value if math.isfinite(value) and value >= 0 else None
    return None


def physical_load15() -> float:
    """Unrounded host load15, without execution-budget environment overrides."""
    value = os.getloadavg()[2]
    if not math.isfinite(value) or value < 0:
        raise OSError("load15 unavailable")
    return value


def memory_gib(group: str | None = None) -> float | None:
    """The memory the queue's leases share, in GiB (SLOTFATHER_MEMORY_GIB replaces it), or None where unknown: on
    Linux the limit of `group` (else of the queue's cgroup, memory_cgroup(); "" for none) where it has one, else the
    machine's."""
    value = _config.setting("MEMORY_GIB") or ""
    with contextlib.suppress(ValueError):
        if float(value) > 0:
            return float(value)
    if linux():
        # A cgroup's limit alone where there is one: MemTotal moves with a VM's balloon, and stays in the guard.
        limit = cgroup_limit(memory_cgroup() if group is None else group)
        if limit is not None:
            return limit / (1 << 30)
        found = meminfo().get("MemTotal")
        return found / (1 << 20) if found else None
    if sys.platform == "darwin":
        found = sysctl("hw.memsize")
        return int(found) / (1 << 30) if found and found.isdigit() else None
    with contextlib.suppress(ValueError, OSError, AttributeError):
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1 << 30)
    return None


def loads() -> tuple[float, float, float]:
    """The 1, 5 and 15-minute load averages."""
    value = (_config.setting("LOADAVG") or "").split()
    with contextlib.suppress(ValueError):
        if len(value) == 1:
            return (float(value[0]),) * 3
        if len(value) == 3:
            return tuple(float(v) for v in value)
    return os.getloadavg()


def ps_environment() -> dict[str, str]:
    """The environment ps runs in for processes(): the C locale on every system, since ps writes lstart in the
    locale's words and every client must write a holder's start alike (fathers#122). Clients before it ran macOS's
    ps in the caller's locale; same_start() still matches what they wrote."""
    return {**os.environ, "LC_ALL": "C"}


def processes() -> dict[int, tuple[int, str]]:
    """Live pid -> (ppid, start time); the start time tells a reused pid from the holder. Zombies are dead. The same
    ps flags serve macOS and Linux (procps-ng). Raises ProcessTableError for a ps that fails, takes longer than
    PS_TIMEOUT, or gives a table without this process in it: such a table is no table, and reaping by it would
    take every lease from commands that still run."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,stat=,lstart="], capture_output=True, text=True,
                                env=ps_environment(), timeout=_config.PS_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _config.ProcessTableError(f"ps: {error}") from None
    if result.returncode:
        raise _config.ProcessTableError(f"ps exited {result.returncode}: {result.stderr.strip()[:200]}")
    table: dict[int, tuple[int, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit() and not parts[2].startswith("Z"):
            table[int(parts[0])] = (int(parts[1]), parts[3].strip())
    if os.getpid() not in table:
        raise _config.ProcessTableError(f"ps listed {len(table)} processes and not this one ({os.getpid()})")
    return table


def ancestors(table: dict, pid: int) -> set[int]:
    found = set()
    while pid in table and pid not in found and pid > 1:
        found.add(pid)
        pid = table[pid][0]
    return found


def alive(entry: dict[str, Any], table: dict[int, tuple[int, str]]) -> bool:
    pid = entry.get("pid")
    process = table.get(pid) if isinstance(pid, int) else None
    start = entry.get("start")
    return process is not None and isinstance(start, str) and same_start(start, process[1])


CLOCK = re.compile(r"(?<!\d)(\d{1,2}):(\d{2}):(\d{2})(?!\d)")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def c_start(text: str) -> tuple[int, int, int, tuple[int, int, int]] | None:
    """A `ps -o lstart` time in the C locale ("Fri Nov 13 15:50:34 2026") as (year, month, day, clock), else None."""
    words = text.split()
    clock = CLOCK.fullmatch(words[3]) if len(words) == 5 else None
    if clock is None or words[1] not in MONTHS or not words[2].isdigit() or not words[4].isdigit():
        return None
    return int(words[4]), MONTHS.index(words[1]) + 1, int(words[2]), (int(clock[1]), int(clock[2]), int(clock[3]))


def same_start(written: str, observed: str) -> bool:
    """Whether a queue entry's `start` (as its client wrote it) names the process ps now shows started at
    `observed` (C locale). "" matches any (an entry that names no start). Clients before fathers#122 ran macOS's ps
    in their caller's locale ("пятница, 13 ноября 2026 г. 15:50:34", "Fr. 13 Nov. 15:50:34 2026"): such a start
    matches when it holds the same clock time, day and year as whole numbers and no other number than the month's,
    so a recycled pid, which starts at another second, never inherits the lease."""
    written, observed = " ".join(written.split()), " ".join(observed.split())
    if not written or written == observed:
        return True
    moment = c_start(observed)
    if moment is None or c_start(written) is not None:
        return False  # two C-locale times that differ name two processes
    year, month, day, clock = moment
    clocks = CLOCK.findall(written)
    if [tuple(int(part) for part in found) for found in clocks] != [clock]:
        return False
    words = [word for word in re.split(r"[\s,./]+", CLOCK.sub(" ", written)) if re.search(r"\d", word)]
    if not all(re.fullmatch(r"[0-9]+", word) for word in words):
        return False  # a number run into letters ("2026x") is no date this matches
    numbers = {int(word) for word in words}
    return year in numbers and day in numbers and numbers <= {year, month, day}


def memory_short() -> str | None:
    """Why new leases should wait for memory, or None: memory_level()'s reason, at warning or above."""
    return memory_level()[1]


def memory_level() -> tuple[str, str | None]:
    """How tight memory is for new leases: ("normal", None), ("warning", why) or ("short", why). macOS: the kernel's
    memory-pressure level (2 warning, 4 critical, which is short), with a free-swap fallback when pressure is
    unavailable (allocated swap can grow while pressure is normal); Linux: linux_level(). Free swap under its floor
    is short."""
    seam = _config.setting("MEMORY_FILE")  # test seam: "<free swap MB> <pressure level>"
    if seam:
        try:
            free, level = (float(v) for v in Path(seam).read_text().split()[:2])
        except (OSError, ValueError):
            return "normal", None
    elif linux():
        return linux_level(cast(dict[str, Any], linux_memory()))
    else:
        free, level = swap_free_mb(), pressure_level()
    pressed = None
    if level is not None and level >= _config.CRITICAL_PRESSURE_LEVEL:
        return "short", f"memory pressure level {level:.0f} (critical)"
    if level is not None and level > _config.MAX_PRESSURE_LEVEL:
        pressed = f"memory pressure level {level:.0f} (warning)"
    if not seam and sys.platform == "darwin" and level is not None and level >= 1:
        # vm.swapusage is the current allocation, not macOS's expandable swap limit.
        return ("warning", pressed) if pressed else ("normal", None)
    floor = _config.number_setting("MIN_FREE_SWAP_MB", _config.MIN_FREE_SWAP_MB)
    if free is not None and free < floor:
        return "short", f"free swap {free:.0f} MB is under {floor:.0f} MB"
    return ("warning", pressed) if pressed else ("normal", None)


def linux() -> bool:
    """Whether memory and processes are read from /proc: on Linux, or wherever SLOTFATHER_PROC names a /proc."""
    return sys.platform.startswith("linux") or bool(_config.setting("PROC"))


def proc_path(*parts: str) -> Path:
    return Path(_config.setting("PROC") or "/proc", *parts)


def meminfo(text: str | None = None) -> dict[str, int]:
    """/proc/meminfo as name -> kB (counts without a unit as they are); {} where it cannot be read."""
    if text is None:
        try:
            text = proc_path("meminfo").read_text()
        except OSError:
            return {}
    found = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        value = rest.split()
        if value and value[0].isdigit():
            found[name.strip()] = int(value[0])
    return found


def psi(text: str | None = None) -> dict[str, float] | None:
    """avg10 of /proc/pressure/memory's "some" and "full" lines, in percent: {"some": x, "full": y}; None where the
    kernel does not expose PSI (built without it, booted with psi=0, or a container that hides it)."""
    if text is None:
        try:
            text = proc_path("pressure", "memory").read_text()
        except OSError:
            return None
    found = {}
    for line in text.splitlines():
        kind, *fields = line.split()
        for field in fields:
            name, _, value = field.partition("=")
            if name == "avg10" and kind in ("some", "full"):
                with contextlib.suppress(ValueError):
                    found[kind] = float(value)
    return found or None


def cgroup_path(group: str) -> Path:
    """A cgroup v2 path ("/ci.slice/ci-runners.slice") in the mounted hierarchy (SLOTFATHER_CGROUP_ROOT replaces
    /sys/fs/cgroup, a test seam)."""
    return Path(_config.setting("CGROUP_ROOT") or "/sys/fs/cgroup", group.strip("/"))


def cgroup_value(group: str | None, name: str) -> int | None:
    """A cgroup's memory.max or memory.current in bytes; None for no group, "max" (no limit) or a file not there."""
    if not group:
        return None
    try:
        value = (cgroup_path(group) / name).read_text().strip()
    except OSError:
        return None
    return int(value) if value.isdigit() else None


def cgroup_limit(group: str | None) -> int | None:
    """A cgroup's effective memory limit in bytes: the smallest memory.max on its path from the root; None for none."""
    if not group:
        return None
    parts = [p for p in group.strip("/").split("/") if p]
    limits = [cgroup_value("/" + "/".join(parts[:n]), "memory.max") for n in range(len(parts) + 1)]
    return min((v for v in limits if v is not None), default=None)


def own_cgroup() -> str | None:
    """This process's cgroup v2 path, from /proc/self/cgroup's "0::" line; None on cgroup v1 or off Linux."""
    with contextlib.suppress(OSError):
        for line in proc_path("self", "cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return line[3:].strip() or "/"
    return None


def limited_cgroup(group: str | None = None) -> str | None:
    """The outermost cgroup at or above `group` (default this process's) with a memory limit: on a CI VM whose
    runner services each have a MemoryMax inside a slice with a MemoryMax of its own, the slice all of them share."""
    group = own_cgroup() if group is None else group
    if not group:
        return None
    parts, found = [p for p in group.strip("/").split("/") if p], None
    for n in range(len(parts), -1, -1):  # "/" last: a host's root has no memory.max, a container's namespace may
        candidate = "/" + "/".join(parts[:n])
        if cgroup_value(candidate, "memory.max") is not None:
            found = candidate
    return found


def memory_cgroup() -> str | None:
    """The cgroup whose memory the queue's leases share: SLOTFATHER_CGROUP, else the one install.py --cgroup wrote
    in (DEFAULT_CGROUP), else the one saved with the capacity
    (`slots memory|lower` saves the saver's), else this process's limited_cgroup(); None off Linux or without a
    limit. Every client of a queue should name the same one: the capacity and the guard read its limit and pressure."""
    if not linux():
        return None
    named = _config.setting("CGROUP") or _config.DEFAULT_CGROUP
    if named:
        return named
    with contextlib.suppress(OSError, ValueError, AttributeError):
        saved = json.loads((_config.state_dir() / "capacity.json").read_text()).get("cgroup")
        if isinstance(saved, str) and saved.startswith("/"):
            return saved
    return limited_cgroup()


def linux_memory(info: dict[str, int] | None = None, pressure: dict | None = None,
                 group: str | None = None) -> dict:
    """The Linux memory facts in MB and percent: total, available, swap_total, swap_free, psi_some, psi_full, and
    for the queue's cgroup (memory_cgroup(), or `group`) cgroup, cgroup_max and cgroup_available (None where not
    reported). PSI is the cgroup's memory.pressure where there is one, else /proc/pressure/memory's."""
    info = meminfo() if info is None else info
    group = memory_cgroup() if group is None else group
    limit = cgroup_limit(group)
    if pressure is None and group:
        with contextlib.suppress(OSError):
            pressure = psi((cgroup_path(group) / "memory.pressure").read_text())
    pressure = psi() if pressure is None else pressure
    megabytes = {key: (info[name] / 1024 if name in info else None)
                 for key, name in (("total", "MemTotal"), ("available", "MemAvailable"),
                                   ("swap_total", "SwapTotal"), ("swap_free", "SwapFree"))}
    if megabytes["available"] is None and "MemFree" in info:  # kernels before 3.14 have no MemAvailable
        megabytes["available"] = (info["MemFree"] + info.get("Cached", 0) + info.get("Buffers", 0)) / 1024
    used = cgroup_value(group, "memory.current") if limit is not None else None
    if used is not None:  # the working set: the cgroup's page cache it can drop at once does not count
        with contextlib.suppress(OSError, ValueError):
            for line in (cgroup_path(group) / "memory.stat").read_text().splitlines():
                if line.startswith("inactive_file "):
                    used = max(0, used - int(line.split()[1]))
    return {**megabytes, "psi_some": (pressure or {}).get("some"), "psi_full": (pressure or {}).get("full"),
            "cgroup": group if limit is not None else None, "cgroup_max": None if limit is None else limit / (1 << 20),
            "cgroup_available": None if used is None else max(0, limit - used) / (1 << 20)}


def linux_short(facts: dict) -> str | None:
    """Why new leases should wait, from linux_memory(), at warning or above (linux_level())."""
    return linux_level(cast(dict[str, Any], facts))[1]


def linux_level(facts: dict[str, Any]) -> tuple[str, str | None]:
    """memory_level() from linux_memory(): short for free swap under its floor (where swap is on), MemAvailable (or
    the cgroup's headroom) under SLOTFATHER_MIN_AVAILABLE_MB, or PSI full avg10 at SLOTFATHER_MAX_PSI_FULL percent
    or above; warning for PSI some avg10 at SLOTFATHER_MAX_PSI_SOME or above."""
    floor = _config.number_setting("MIN_FREE_SWAP_MB", _config.MIN_FREE_SWAP_MB)
    if facts["swap_total"] and facts["swap_free"] is not None and facts["swap_free"] < floor:
        return "short", f"free swap {facts['swap_free']:.0f} MB is under {floor:.0f} MB"
    least = _config.number_setting("MIN_AVAILABLE_MB", _config.MIN_AVAILABLE_MB)
    if facts["available"] is not None and facts["available"] < least:
        return "short", f"available memory {facts['available']:.0f} MB is under {least:.0f} MB"
    if facts.get("cgroup_available") is not None and facts["cgroup_available"] < least:
        return "short", (f"memory left under {facts['cgroup']}'s limit, {facts['cgroup_available']:.0f} MB, is under "
                         f"{least:.0f} MB")
    found: tuple[str, str | None] = ("normal", None)
    for kind, name, default, level in (("full", "MAX_PSI_FULL", _config.MAX_PSI_FULL, "short"),
                                       ("some", "MAX_PSI_SOME", _config.MAX_PSI_SOME, "warning")):
        value, limit = facts["psi_" + kind], _config.number_setting(name, default)
        if value is not None and limit > 0 and value >= limit:
            if level == "short":
                return level, f"memory pressure: PSI {kind} avg10 {value:g}% (limit {limit:g}%)"
            found = (level, f"memory pressure: PSI {kind} avg10 {value:g}% (limit {limit:g}%)")
    return found


def sysctl(name: str) -> str | None:
    try:
        result = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=_config.PS_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def swap_usage(report: str | None = None) -> tuple[float, float] | None:
    """(total, free) swap in MB from `sysctl vm.swapusage` (Linux: /proc/meminfo), or None where the host does not
    report it."""
    if report is None and linux():
        facts = linux_memory(pressure={})
        return None if facts["swap_total"] is None else (facts["swap_total"], facts["swap_free"] or 0.0)
    found = re.search(r"total = ([\d.]+)([MG]).*?free = ([\d.]+)([MG])",
                      (sysctl("vm.swapusage") or "") if report is None else report)
    if not found:
        return None
    return tuple(float(found[n]) * (1024 if found[n + 1] == "G" else 1) for n in (1, 3))


def swap_free_mb(report: str | None = None) -> float | None:
    """Free swap in MB, or None when there is no swap to run short of: with swap turned off the kernel reports
    0 MB free at all times, and the floor would hold every lease behind the one running."""
    usage = swap_usage(report)
    return usage[1] if usage and usage[0] > 0 else None


def pressure_level() -> float | None:
    """macOS's memory-pressure level (1 normal, 2 warning, 4 critical); None elsewhere."""
    if linux():
        return None
    value = sysctl("kern.memorystatus_vm_pressure_level")
    return float(value) if value and value.isdigit() else None


def cpu_seconds(text: str) -> float | None:
    """ps `time` ([days-][hours:]minutes:seconds.fraction) as seconds, or None."""
    days, _, rest = text.strip().rpartition("-")
    try:
        value = 0.0
        for part in rest.split(":"):
            value = value * 60 + float(part)
        return value + (int(days) * 86400 if days else 0)
    except ValueError:
        return None


def started_at(lstart: str) -> float | None:
    """A `ps -o lstart` time (C locale) as epoch seconds, or None."""
    try:
        return time.mktime(time.strptime(" ".join(lstart.split()), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def proc_cpu_seconds(pid: int, text: str | None = None) -> float | None:
    """utime + stime of /proc/PID/stat in seconds, or None. procps' `time` counts whole seconds, which loses the
    short processes a lease's tree is full of; /proc counts clock ticks (10 ms)."""
    if text is None:
        try:
            text = proc_path(str(pid), "stat").read_text()
        except OSError:
            return None
    fields = text.rpartition(")")[2].split()  # after the command name, which may hold spaces and parentheses
    try:
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (IndexError, ValueError, OSError):
        return None


def children_cpu(pid: int) -> float | None:
    """CPU seconds of the children `pid` has waited for (Linux cutime + cstime; macOS proc_pid_rusage), or None
    where it cannot be read. A process's own CPU leaves them out: a busy worker started and reaped between two
    samples shows only here."""
    if linux():
        try:
            fields = proc_path(str(pid), "stat").read_text().rpartition(")")[2].split()
            return (int(fields[13]) + int(fields[14])) / os.sysconf("SC_CLK_TCK")
        except (OSError, IndexError, ValueError):
            return None
    from . import macos_process
    return macos_process.children_cpu_seconds(pid)


def usage_table() -> dict[int, tuple[int, str, int, int, float]]:
    """Live pid -> (ppid, start, uid, resident KB, CPU seconds of its own), from one ps over the machine (on Linux
    the CPU from /proc/PID/stat where it can be read). Raises SlotError when ps fails or takes longer than
    PS_TIMEOUT."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,uid=,rss=,time=,lstart="], capture_output=True,
                                text=True, env={**os.environ, "LC_ALL": "C"}, timeout=_config.PS_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _config.SlotError(f"ps: {error}") from None
    table = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) == 6 and all(p.isdigit() for p in parts[:4]):
            cpu = cpu_seconds(parts[4])
            if linux():
                cpu = proc_cpu_seconds(int(parts[0])) or cpu
            if cpu is not None:
                table[int(parts[0])] = (int(parts[1]), parts[5].strip(), int(parts[2]), int(parts[3]), cpu)
    return table


def carrying(pids: list[int], lease: str) -> set[int]:
    """Those of `pids` whose environment holds SLOTFATHER_LEASE=<lease>. macOS shows another process's environment
    only for the user's own processes that are not platform binaries; Linux (/proc/PID/environ) only for the user's
    own (all of them for root); the rest read as not carrying it."""
    if not pids:
        return set()
    if linux():
        wanted = ("SLOTFATHER_LEASE=" + lease).encode()
        found = set()
        for pid in pids:
            with contextlib.suppress(OSError):
                if wanted in proc_path(str(pid), "environ").read_bytes().split(b"\0"):
                    found.add(pid)
        return found
    try:
        result = subprocess.run(["ps", "-wwE", "-o", "pid=,command=", "-p", ",".join(map(str, pids))],
                                capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}, timeout=_config.PS_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    mark = " SLOTFATHER_LEASE=" + lease
    found = set()
    for line in result.stdout.splitlines():
        pid, _, rest = line.strip().partition(" ")
        at = (" " + rest).find(mark)
        tail = (" " + rest)[at + len(mark):] if at >= 0 else None
        if pid.isdigit() and tail is not None and (not tail or tail[0] == " "):
            found.add(int(pid))
    return found


def cpu_ticks() -> list[int] | None:
    """Cumulative CPU ticks of the whole machine: [user, system, idle, nice], or None where unavailable."""
    global _host_port
    if sys.platform == "darwin":
        try:
            import ctypes
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            if _config._host_port is None:
                libc.mach_host_self.restype = ctypes.c_uint
                _config._host_port = libc.mach_host_self()
            counts, size = (ctypes.c_uint * 4)(), ctypes.c_uint(4)
            if libc.host_statistics(_config._host_port, 3, counts, ctypes.byref(size)) == 0:  # HOST_CPU_LOAD_INFO
                return list(counts)
        except (OSError, AttributeError):
            return None
        return None
    with contextlib.suppress(OSError, ValueError):
        fields = Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]
        user, nice, system, idle = (int(v) for v in fields[:4])
        return [user, system, idle + sum(int(v) for v in fields[4:5]), nice]  # iowait counts as idle
    return None


def process_uid(pid: object) -> int | None:
    """The real uid a live process runs as, or None when it is not running or cannot be inspected."""
    if type(pid) is not int or pid < 1:
        return None
    try:
        result = subprocess.run(["ps", "-o", "uid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    words = result.stdout.split()
    return int(words[0]) if result.returncode == 0 and len(words) == 1 and words[0].isdigit() else None


def process_group(pid) -> int | None:
    try:
        return os.getpgid(pid)
    except (OSError, TypeError):
        return None


def descendants(table: dict, pid) -> list[int]:
    """`pid` and its live descendants, from a processes() table; [] when `pid` is not live."""
    if pid not in table:
        return []
    children = {}
    for child, (parent, _) in table.items():
        children.setdefault(parent, []).append(child)
    found, stack = [], [pid]
    while stack:
        here = stack.pop()
        if here not in found:
            found.append(here)
            stack.extend(children.get(here, ()))
    return sorted(found)
