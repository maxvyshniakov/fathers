"""The machine: the lane cap, disk and load, and how many jobs a verify run takes.

The lane cap is a lane per 2 cores and per 4 GB of physical memory beyond 8 GB, whichever is fewer (#572);
LANEFATHER_CAP overrides it, and start logs the cap with its source. start refuses while load15 is above 1.5 x the
cores. With LANEFATHER_LOAD_THROTTLE=1 (off by default, owner decision on the #572 review) each verify run (check,
the post-merge smoke, the batch gate) also waits until it is not, at most LANEFATHER_LOAD_WAIT seconds.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any

from . import config, journal, slots

# A lane per CORES_PER_LANE cores and per LANE_MEMORY_GB of the memory left after MEMORY_RESERVE_GB for the system
# and the owner's apps. 12 cores and 32 GB give 6.
CORES_PER_LANE = 2
LANE_MEMORY_GB = 4
MEMORY_RESERVE_GB = 8
MIN_DISK_GB = 150
LOAD_FACTOR = 1.5
SMOKE_MAX_JOBS = 3
FALLBACK_JOBS = 2
GATE_MAX_JOBS = 2
# A lane check's most jobs (#642 review): a second slot lets the slot-sized checks run side by side; advice gives
# it only on a quiet machine.
CHECK_MAX_JOBS = 2
DEFAULT_LOAD_WAIT = 3600.0
DEFAULT_LOAD_POLL = 30.0


def machine_cores(project: config.Config | None = None) -> int:
    """The core count; LANEFATHER_CORES stands in for it in tests."""
    stub = (project or config.current()).env("CORES")
    return int(stub) if stub and stub.isdigit() and int(stub) > 0 else os.cpu_count() or 1


def machine_memory_gb(project: config.Config | None = None) -> float:
    """Physical memory in GB; LANEFATHER_MEMORY_GB stands in for it in tests. macOS keeps its free page count near
    zero (the page cache holds the rest), so the cap counts the physical memory less a reserve."""
    stub = (project or config.current()).env("MEMORY_GB")
    if stub:
        with contextlib.suppress(ValueError):
            return float(stub)
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30
    except (OSError, ValueError):
        return 0.0


def derived_cap(cores: int, memory_gb: float) -> int:
    return max(1, min(cores // CORES_PER_LANE, int((memory_gb - MEMORY_RESERVE_GB) // LANE_MEMORY_GB)))


def lane_cap(project: config.Config | None = None) -> tuple[int, str]:
    """The lane cap and where it comes from: LANEFATHER_CAP, which the owner may raise for a stretch and every start
    logs, else the machine's derived cap. A bad value warns and keeps the derived cap, so a typo does not stop every
    run (#454)."""
    cores, memory = machine_cores(project), machine_memory_gb(project)
    derived = derived_cap(cores, memory)
    found = (project or config.current()).lookup("CAP")
    if found and found[1].strip().isdigit() and int(found[1]) > 0:
        return int(found[1]), f"{found[0]} override; derived: {derived}"
    if found and found[1]:
        print(f"lane: warning: {found[0]}={found[1]!r} is not a positive whole number; the cap is {derived}",
              file=sys.stderr)
    return derived, f"derived from {cores} cores and {memory:.0f} GB of memory"


def load15(project: config.Config | None = None) -> float:
    """The 15-minute load average; LANEFATHER_LOAD15 stands in for it in tests."""
    stub = (project or config.current()).env("LOAD15")
    return float(stub) if stub else os.getloadavg()[2]


def disk_free_gb(project: config.Config | None = None) -> float:
    disk = (project or config.current()).env("DISK_FREE_GB")
    return float(disk) if disk else shutil.disk_usage(Path.home()).free / 1e9


def host(project: config.Config | None = None) -> dict[str, Any]:
    return {"disk_free_gb": disk_free_gb(project),
            "load15": load15(project), "cores": machine_cores(project)}


def load_limit(cores: int) -> float:
    """The load15 at which start refuses a lane and a throttled verify run waits (#572)."""
    return LOAD_FACTOR * cores


def host_fields(info: dict[str, Any]) -> str:
    """The host snapshot in ledger lines, which orchestration metrics read for disk and load peaks."""
    return f"disk_free_gb={info['disk_free_gb']:.0f} load15={info['load15']:.1f} cores={info['cores']}"


def host_line(info: dict[str, Any]) -> str:
    limit = info["load_limit"] if "load_limit" in info else load_limit(info["cores"])
    return (f"disk free {info['disk_free_gb']:.0f} GB (minimum {MIN_DISK_GB} GB); load15 {info['load15']:.1f} on "
            f"{info['cores']} cores (maximum {limit:.1f})")


def jobs_decision(info: dict[str, Any], maximum: int, exclusive: bool = False) -> dict[str, Any]:
    """verify --jobs for a run, and why: `slotfather advise --max <maximum>` (#599, #619), which takes out the cores
    of load that no lease explains and, without --exclusive, the slots others hold. Without an answer (slotfather
    is not installed or failed), FALLBACK_JOBS, recording why. `info` is the host snapshot of the caller."""
    del info  # the snapshot the caller logs; slotfather reads the load itself
    try:
        return slots.advise(maximum, exclusive)
    except Exception as error:
        return {"jobs": min(FALLBACK_JOBS, maximum), "fallback": f"{type(error).__name__}: {error}"}


def post_merge_jobs(info: dict[str, Any]) -> dict[str, Any]:
    """The post-merge smoke runs beside the lanes' checks: at most 3 jobs and never more than the free slots."""
    return jobs_decision(info, SMOKE_MAX_JOBS)


def gate_jobs(info: dict[str, Any]) -> dict[str, Any]:
    """The batch gate is an ordinary full gate (#610): at most two jobs and no more than the free slots."""
    return jobs_decision(info, GATE_MAX_JOBS)


def check_jobs(info: dict[str, Any]) -> dict[str, Any]:
    """A lane check shares the machine like the batch gate: at most CHECK_MAX_JOBS and no more than the free
    slots."""
    return jobs_decision(info, CHECK_MAX_JOBS)


def given_jobs(jobs: int, option: str) -> dict[str, Any]:
    return {"jobs": jobs, "override": option}


def seconds_setting(name: str, default: float) -> float:
    try:
        value = float(config.current().env(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def load_averages() -> list[float]:
    """The 1, 5 and 15-minute load averages; the last is load15(), which tests stub."""
    one, five, _ = os.getloadavg()
    return [round(one, 2), round(five, 2), round(load15(), 2)]


def load_wait_enabled() -> bool:
    """Owner decision on the #572 review (2026-10-27): no load throttle by default; timing tests move to a perf
    suite that only the batch gate runs. LANEFATHER_LOAD_THROTTLE=1 turns the wait on."""
    return config.current().env("LOAD_THROTTLE") == "1"


def wait_for_load(label: str) -> dict[str, Any]:
    """With the throttle on, wait until load15 is at or below the limit start enforces before a verify run starts
    (#572); the wait for the receipt, which says whether it was on. It runs before verify asks for its slot lease,
    so it never holds a slot. A process that already runs under a lease does not wait: it would hold those slots
    idle. After LANEFATHER_LOAD_WAIT seconds (default 3600) the run starts anyway, marked gave_up."""
    limit = load_limit(machine_cores())
    before = load15()
    wait: dict[str, Any] = {"enabled": load_wait_enabled(), "limit": limit, "load15_before": before,
                            "load15": before, "seconds": 0.0}
    if before <= limit or not wait["enabled"]:
        return wait
    if os.environ.get("SLOTFATHER_LEASE"):
        return dict(wait, skipped="runs under a slot lease it holds; a wait would hold those slots idle")
    patience = seconds_setting("LOAD_WAIT", DEFAULT_LOAD_WAIT)
    poll = seconds_setting("LOAD_POLL", DEFAULT_LOAD_POLL)
    print(f"lane: load15 {before:.1f} is above the limit {limit:.1f} ({LOAD_FACTOR} x {machine_cores()} cores); "
          f"{label} waits for it to drop, at most {patience:.0f} s", flush=True)
    clock = time.monotonic()
    current = before
    while current > limit and time.monotonic() - clock < patience:
        time.sleep(max(0.0, min(poll, patience - (time.monotonic() - clock))))
        current = load15()
    wait.update(load15=current, seconds=round(time.monotonic() - clock, 1))
    if current > limit:
        wait["gave_up"] = True
    print(f"lane: {label} starts after {wait['seconds']:.0f} s at load15 {current:.1f}"
          + (" (still above the limit)" if current > limit else ""), flush=True)
    journal.ledger(f"load-wait {label} seconds={wait['seconds']} load15={before}->{current} limit={limit}"
                   + (" gave up" if current > limit else ""))
    return wait
