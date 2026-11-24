"""Who uses the machine outside leases (fathers#239): what a machine sample stores of the processes no lease holds.

A sample taken while the machine is busy (usage.machine_facts()) reads one ps of the whole machine. Every process
that is not a held lease's, by the one membership rule (process.lease_membership(): the holder's tree, a process
carrying the lease's SLOTFATHER_LEASE, a detached browser/XPC helper attributed to the lease, fathers#202), is
outside. When the CPU share of those comes to at least one core, the sample stores them grouped by command family
and owner, the busiest TOP groups. A family is a program's name or the outermost app bundle it runs from, never an
argument or a directory (they can hold secrets and paths); an owner is the Spawnfather entry whose tree or process
group holds the process, else the nearest agent or terminal session above it, else none. CPU is ps's share (100 =
one core): on macOS a decaying average of the last seconds, on Linux the average over the process's life.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Iterable, Mapping, Sequence, cast

from fathers_common import agents as _agents
from fathers_common.proc import Proc

from . import lineage as _lineage
from . import platform as _platform
from . import process as _process

THRESHOLD = 100.0  # ps CPU percent outside leases (one core) from which a sample stores the families
TOP = 8  # the most (family, owner) groups a sample keeps
FAMILY_MAX = 40  # characters of a family
OWNER_MAX = 48  # characters of an owner
MARK_MIN = 1.0  # ps CPU percent below which a process outside every lease's tree is not looked into
MARK_MAX = 32  # the most processes whose environment one sample reads for a lease's marker
SPAWNFATHER_TIMEOUT = 3.0  # seconds for `spawnfather list --json`
SPAWNFATHER_EVERY = 60.0  # seconds a holder reuses Spawnfather's entries (the call costs about 0.2 s of CPU)
LOAD_MIN = 1.0  # one-minute load below which the machine has not a core's worth of work outside leases to name

_APP = re.compile(r"(?:^|/)([^/]+?)\.app/Contents/")

Spawned = Callable[[], Sequence[Mapping[str, Any]]]


def clip(text: str, most: int) -> str:
    """`text` on one line and at most `most` characters."""
    flat = " ".join(text.split())
    return flat if len(flat) <= most else flat[: most - 1] + "…"


def family(cmd: str) -> str:
    """The command's family: the outermost app bundle it runs from (every helper of a browser is the browser), else
    its program's base name, a login shell's dash and a process title's colon dropped; "?" for none."""
    bundle = _APP.search(cmd)
    if bundle:
        name = bundle[1]
    else:
        program = _lineage.program(cmd)
        name = _agents.agent_kind(program) or os.path.basename(program).lstrip("-").rstrip(":")
    return clip(name, FAMILY_MAX) or "?"


def spawn_owners(spawned: Sequence[Mapping[str, Any]]) -> tuple[dict[int, str], dict[int, str]]:
    """Spawnfather entries as owners: their pids and their process groups, each -> "spawnfather LABEL"."""
    by_pid: dict[int, str] = {}
    by_group: dict[int, str] = {}
    for entry in spawned:
        name = clip(f"spawnfather {entry.get('label') or entry.get('id') or ''}".strip(), OWNER_MAX)
        pid, group = entry.get("pid"), entry.get("pgid")
        if type(pid) is int and pid > 1:
            by_pid.setdefault(pid, name)
        if type(group) is int and group > 1:
            by_group.setdefault(group, name)
    return by_pid, by_group


def owner(pid: int, procs: Mapping[int, Proc], parents: Mapping[int, int], by_pid: Mapping[int, str],
          by_group: Mapping[int, str]) -> str | None:
    """Who a process outside the leases is for: the Spawnfather entry of it or an ancestor (by pid, then by process
    group), else the nearest agent or terminal session among it and its ancestors ("claude 4120"), else None."""
    chain = [pid, *_lineage.ancestors(parents, pid)]
    for member in chain:
        if member in by_pid:
            return by_pid[member]
        if procs[member].pgid in by_group:
            return by_group[procs[member].pgid]
    for member in chain:
        cmd = procs[member].cmd
        agent = _agents.agent_kind(cmd)
        if agent is not None:
            return clip(f"{agent} {member}", OWNER_MAX)
        if _lineage.TERMINAL_SESSION.search(_lineage.program(cmd)):
            return clip(f"{family(cmd)} {member}", OWNER_MAX)
    return None


def measure(procs: Mapping[int, Proc], leases: Sequence[_process.Lease], *,
            markers: _process.Markers = _process.lease_markers, spawned: Spawned | None = None,
            top: int = TOP, threshold: float = THRESHOLD) -> dict[str, Any] | None:
    """What a sample stores of the use outside `leases` in the table `procs`: {"cpu": cores outside, "processes":
    how many, "top": [{"family", "owner", "cpu", "processes"}], the busiest `top` groups first}; None below
    `threshold` percent. `markers` reads the environments of at most MARK_MAX processes of at least MARK_MIN percent
    outside every lease's tree; `spawned` (Spawnfather's entries) is asked only once the threshold is met, and a
    fault there leaves the session owners."""
    parents = {pid: row.ppid for pid, row in procs.items()}
    commands = {pid: row.cmd for pid, row in procs.items()} if _platform.macos() else None
    busy = sorted((pid for pid, row in procs.items() if row.cpu >= MARK_MIN), key=lambda pid: -procs[pid].cpu)
    looked = set(busy[:MARK_MAX])

    def bounded(pids: Sequence[int], paths: Iterable[str]) -> tuple[dict[int, set[str]], list[int]]:
        wanted = [pid for pid in pids if pid in looked]
        found, hidden = markers(wanted, paths) if wanted else ({}, [])
        return {**{pid: set() for pid in pids if pid not in looked}, **found}, hidden

    membership = _process.lease_membership(
        parents, leases, list(procs), markers=bounded if leases else None, detached_only=True, commands=commands,
        uids={pid: row.uid for pid, row in procs.items()} if commands is not None else None,
        starts={pid: row.started for pid, row in procs.items()} if commands is not None else None,
    )
    out = [pid for pid in procs if not membership.leases.get(pid)]
    total = sum(procs[pid].cpu for pid in out)
    if total < threshold:
        return None
    try:
        by_pid, by_group = spawn_owners(spawned() if spawned is not None else [])
    except Exception:  # noqa: BLE001 - an owner is a nicety; the families still count
        by_pid, by_group = {}, {}
    groups: dict[tuple[str, str | None], list[float]] = {}
    for pid in out:
        if procs[pid].cpu <= 0:
            continue
        key = (family(procs[pid].cmd), owner(pid, procs, parents, by_pid, by_group))
        group = groups.setdefault(key, [0.0, 0])
        group[0] += procs[pid].cpu
        group[1] += 1
    ranked = sorted(groups.items(), key=lambda item: (-item[1][0], item[0][0], item[0][1] or ""))
    return {"cpu": round(total / 100, 2), "processes": len(out),
            "top": [{"family": name, "owner": who, "cpu": round(cpu / 100, 2), "processes": int(count)}
                    for (name, who), (cpu, count) in ranked[:top]]}


_spawned: tuple[float, list[dict[str, Any]]] | None = None  # (monotonic moment, entries) of the last answer


def spawnfather_entries() -> list[dict[str, Any]]:
    """Spawnfather's running entries, within SPAWNFATHER_TIMEOUT, the same answer for SPAWNFATHER_EVERY seconds in
    this process; [] when it is not there."""
    global _spawned
    from . import sweep_disk as _sweep_disk

    if _spawned is not None and time.monotonic() - _spawned[0] < SPAWNFATHER_EVERY:
        return _spawned[1]

    def quick(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        options["timeout"] = SPAWNFATHER_TIMEOUT
        return cast("subprocess.CompletedProcess[str]", subprocess.run(argv, **options))
    found = _sweep_disk.spawnfather_entries(run=quick)
    _spawned = (time.monotonic(), found)
    return found


def sample(held: Sequence[tuple[Path, Mapping[str, Any]]]) -> dict[str, Any] | None:
    """measure() over a ps of the machine, the held leases being `held` (their entry paths and entries); None when
    ps cannot give the table."""
    procs = _process.outside_snapshot()
    if procs is None:
        return None
    leases = [_process.Lease(str(path), entry.get("pid"), since=_process.lease_started_epoch(entry))
              for path, entry in held]
    return measure(procs, leases, spawned=spawnfather_entries)
