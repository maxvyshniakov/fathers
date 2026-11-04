"""Processes a lane leaves in its worktree (#661), shared by lane-stop.py, tools/lane done and tools/janitor.

One read of this user's processes: pid, parent, terminal, start time (`ps -o lstart`, the form slotfather reports),
command line and working directory (one `lsof -d cwd` call). The slotfather leases they run in come from
`slotfather status --json --pid PID`, as the lease guard asks (#650); the owner's rule is that nothing here stops
a process inside a lease: such a process holds its worktree instead. `stop` signals only a pid whose start time is
still the one read, so a pid reused meanwhile is left alone.

Runs under the janitor's /usr/bin/python3 (3.9).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import shutil
import signal
import subprocess
import time

# A Claude Code session (the CLI, or the desktop app's helper that starts it): never stopped, it holds its worktree.
CLAUDE = re.compile(r"(^|/)(claude|disclaimer)(\s|$)|/claude-code/|/Claude\.app/")
C_LOCALE = {"LC_ALL": "C"}


class Process:
    __slots__ = ("pid", "ppid", "tty", "start", "command", "cwd")

    def __init__(self, pid: int, ppid: int, tty: str, start: str, command: str, cwd: str | None = None):
        self.pid, self.ppid, self.tty, self.start, self.command, self.cwd = pid, ppid, tty, start, command, cwd

    @property
    def interactive(self) -> bool:
        """A terminal's process (a person's shell), not one an agent started: those have no terminal."""
        return self.tty not in ("", "??", "-")

    @property
    def claude(self) -> bool:
        return bool(CLAUDE.search(self.command))

    def describe(self) -> str:
        return f"pid {self.pid} ({self.command[:100]})"


def run(argv: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **C_LOCALE}).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def table() -> dict[int, Process]:
    """This user's processes by pid, each with its working directory where lsof can read it."""
    found: dict[int, Process] = {}
    uid = os.getuid()
    for line in run(["ps", "-axo", "pid=,ppid=,uid=,tty=,lstart=,command="]).splitlines():
        parts = line.split(None, 9)
        if len(parts) < 9 or not (parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit()):
            continue
        if int(parts[2]) != uid:
            continue
        found[int(parts[0])] = Process(int(parts[0]), int(parts[1]), parts[3], " ".join(parts[4:9]),
                                       parts[9] if len(parts) > 9 else "")
    pid = None
    for line in run(["lsof", "-nP", "-a", "-u", str(uid), "-d", "cwd", "-Fpn"]).splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid in found:
            found[pid].cwd = os.path.realpath(line[1:])
    return found


def ancestors(processes: dict[int, Process], pid: int) -> list[int]:
    chain, seen = [], set()
    pid = processes[pid].ppid if pid in processes else 0
    while pid in processes and pid not in seen and pid > 1:
        seen.add(pid)
        chain.append(pid)
        pid = processes[pid].ppid
    return chain


def inside(path: str | None, root: str) -> bool:
    return bool(path) and (path == root or path.startswith(root.rstrip("/") + "/"))


def in_worktree(root: str, processes: dict[int, Process] | None = None) -> list[Process]:
    """Processes whose working directory is inside root, without this process and its ancestors."""
    processes = table() if processes is None else processes
    root = os.path.realpath(root)
    me = {os.getpid(), *ancestors(processes, os.getpid())}
    return [p for p in processes.values() if p.pid not in me and inside(p.cwd, root)]


def left_behind(process: Process, processes: dict[int, Process], root: str) -> bool:
    """Whoever started it is gone: the topmost of its ancestors inside root hangs off launchd (ppid 1)."""
    top = process
    while top.ppid in processes and inside(processes[top.ppid].cwd, root):
        top = processes[top.ppid]
    return top.ppid <= 1 or top.ppid not in processes


# slotfather

def slotfather() -> str | None:
    """STRAYLIGHT_SLOTFATHER (empty: none), else slotfather on PATH, else ~/.local/bin/slotfather."""
    configured = os.environ.get("STRAYLIGHT_SLOTFATHER")
    if configured is not None:
        return configured or None
    fallback = os.path.expanduser("~/.local/bin/slotfather")
    return shutil.which("slotfather") or (fallback if os.access(fallback, os.X_OK) else None)


def status(program: str, *args: str) -> dict | None:
    try:
        result = subprocess.run([program, "status", "--json", *args], capture_output=True, text=True, timeout=5)
        data = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("entries"), list) else None


def leases(processes: list[Process]) -> dict[int, list[dict] | None]:
    """Each process's slotfather leases: [] for none, None when slotfather did not answer for it (keep it then).
    Without slotfather installed no lease exists."""
    program = slotfather()
    if program is None or not processes:
        return {p.pid: [] for p in processes}

    def ask(process: Process):
        answer = status(program, "--pid", str(process.pid))
        if answer is None:
            return None
        seen = (answer.get("query") or {}).get("start")
        if seen is not None and " ".join(str(seen).split()) != process.start:
            return []  # the pid is another process now; the one read is gone
        return answer["entries"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip([p.pid for p in processes], pool.map(ask, processes)))


def lane_leases(lane: str) -> list[dict]:
    """The live leases whose metadata names this lane (tools/heavy.py writes it on every lease Straylight takes)."""
    program = slotfather()
    answer = status(program) if program and lane else None
    return [e for e in (answer or {}).get("entries", [])
            if isinstance(e, dict) and isinstance(e.get("meta"), dict) and e["meta"].get("lane") == lane]


def carrying(lease_paths: set[str]) -> set[int]:
    """This user's processes whose environment holds one of these SLOTFATHER_LEASE values."""
    if not lease_paths:
        return set()
    found = set()
    for line in run(["ps", "-wwE", "-axo", "pid=,command="]).splitlines():
        pid, _, rest = line.strip().partition(" ")
        if pid.isdigit() and any(f" SLOTFATHER_LEASE={path} " in f" {rest} " for path in lease_paths):
            found.add(int(pid))
    return found


def describe_lease(entry: dict) -> str:
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    named = ", ".join(f"{key} {meta[key]}" for key in ("lane", "ticket", "kind", "session") if meta.get(key))
    return f"lease {entry.get('label') or entry.get('id')}" + (f" ({named})" if named else "")


# Sorting a worktree's processes

def sort_for_removal(root: str, processes: dict[int, Process] | None = None,
                     orphans_only: bool = False) -> tuple[list[Process], list[str]]:
    """The processes in root that may be stopped before it is removed, and why each other one holds it.

    Never stopped: a Claude Code session, a terminal's process, a process inside a slotfather lease or whose lease
    slotfather could not tell. With orphans_only (the unattended janitor) also one whose starter still runs."""
    processes = table() if processes is None else processes
    found = in_worktree(root, processes)
    held, candidates = [], []
    for process in found:
        if process.claude:
            held.append(f"{process.describe()} is a Claude Code session")
        elif process.interactive:
            held.append(f"{process.describe()} runs in terminal {process.tty}")
        elif orphans_only and not left_behind(process, processes, os.path.realpath(root)):
            top = process
            while top.ppid in processes and inside(processes[top.ppid].cwd, os.path.realpath(root)):
                top = processes[top.ppid]
            held.append(f"{process.describe()} was started by pid {top.ppid}, which still runs")
        else:
            candidates.append(process)
    answers = leases(candidates)
    stoppable = []
    for process in candidates:
        entries = answers.get(process.pid)
        if entries is None:
            held.append(f"{process.describe()}: slotfather did not say whether a lease holds it")
        elif entries:
            held.append(f"{process.describe()} runs in {describe_lease(entries[0])}")
        else:
            stoppable.append(process)
    return stoppable, held


# Stopping

def alive(processes: list[Process]) -> list[Process]:
    """Those still running as the process read: same pid and start time, not a zombie."""
    if not processes:
        return []
    now = {}
    for line in run(["ps", "-o", "pid=,stat=,lstart=", "-p", ",".join(str(p.pid) for p in processes)]).splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and not parts[1].startswith("Z"):
            now[int(parts[0])] = " ".join(parts[2].split())
    return [p for p in processes if now.get(p.pid) == p.start]


def stop(processes: list[Process], grace: float = 5.0) -> list[Process]:
    """SIGTERM each, SIGKILL what still runs after `grace` seconds; the processes that survived both."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
        remaining = alive(processes)
        if not remaining:
            return []
        for process in remaining:
            try:
                os.kill(process.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + wait
        while alive(remaining) and time.monotonic() < deadline:
            time.sleep(0.1)
    return alive(processes)
