"""Processes a lane leaves in its worktree (#661), shared by lane-stop.py, tools/lane done and tools/janitor.

One read of the machine's processes: pid, parent, owner, terminal, start time (`ps -o lstart` in the C locale, the
form slotfather reports) and command line, with this user's working directories from one `lsof -d cwd` call. A
read that fails or times out raises ScanError: nothing here takes an unreadable table for "nothing runs".

Which of a worktree's processes may be stopped is slotfather's call, not this module's: `slotfather sweep --json
--pid PID...` classifies them (left behind in a removed directory, an adopted dev or test process past its age;
never a session, a terminal, another user's process or one in a live lease; locale-safe start times) and
`slotfather sweep --live --pid PID...` stops them. A process the sweep does not offer to stop holds its worktree,
and so does every process when slotfather is missing or does not answer: removal fails closed.

Runs under the janitor's /usr/bin/python3 (3.9).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

# A Claude Code session (the CLI, or the desktop app's helper that starts it): not a process a lane left behind.
CLAUDE = re.compile(r"(^|/)(claude|disclaimer)(\s|$)|/claude-code/|/Claude\.app/")
C_LOCALE = {"LC_ALL": "C", "LANG": "C"}
TIMEOUT = 10.0
STOPPED = ("stopped", "killed", "gone")


class ScanError(Exception):
    """A process table, or slotfather's verdict, that could not be read."""


class Process:
    __slots__ = ("pid", "ppid", "uid", "tty", "start", "command", "cwd")

    def __init__(self, pid: int, ppid: int, tty: str, start: str, command: str, cwd: str | None = None,
                 uid: int | None = None):
        self.pid, self.ppid, self.tty, self.start, self.command, self.cwd = pid, ppid, tty, start, command, cwd
        self.uid = os.getuid() if uid is None else uid

    @property
    def interactive(self) -> bool:
        """A terminal's process (a person's shell), not one an agent started: those have no terminal."""
        return self.tty not in ("", "??", "-")

    @property
    def claude(self) -> bool:
        return bool(CLAUDE.search(self.command))

    def describe(self) -> str:
        return f"pid {self.pid} ({self.command[:100]})"


class Deadline:
    """One time budget shared by several calls (the stop hook runs under a 10 s hook timeout)."""

    def __init__(self, seconds: float | None):
        self.end = None if seconds is None else time.monotonic() + seconds

    def left(self, cap: float = TIMEOUT) -> float:
        if self.end is None:
            return cap
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise ScanError("the time budget ran out")
        return min(cap, remaining)


def run(argv: list[str], deadline: Deadline | None = None, cap: float = TIMEOUT, ok=(0,)) -> subprocess.CompletedProcess:
    """The finished command; ScanError when it cannot start, does not finish in time or exits outside `ok`."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=(deadline or Deadline(None)).left(cap),
                                env={**os.environ, **C_LOCALE})
    except subprocess.TimeoutExpired:
        raise ScanError(f"{argv[0]} did not answer in time") from None
    except OSError as error:
        raise ScanError(f"{argv[0]}: {error}") from None
    if result.returncode not in ok:
        raise ScanError(f"{' '.join(argv[:3])} exited {result.returncode}: {result.stderr.strip()[:200]}")
    return result


def table(deadline: Deadline | None = None) -> dict[int, Process]:
    """Every process by pid (all users, so a parent is never mistaken for gone), each with its working directory
    where lsof can read it. ScanError when ps or lsof fails or times out."""
    found: dict[int, Process] = {}
    for line in run(["ps", "-axo", "pid=,ppid=,uid=,tty=,lstart=,command="], deadline).stdout.splitlines():
        parts = line.split(None, 9)
        if len(parts) < 9 or not (parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit()):
            continue
        found[int(parts[0])] = Process(int(parts[0]), int(parts[1]), parts[3], " ".join(parts[4:9]),
                                       parts[9] if len(parts) > 9 else "", uid=int(parts[2]))
    if not found:
        raise ScanError("ps listed no process")
    # lsof exits 1 when one of the processes could not be read; no output at all is a failure.
    listed = run(["lsof", "-nP", "-d", "cwd", "-Fpn"], deadline, ok=(0, 1)).stdout
    if not listed.strip():
        raise ScanError("lsof listed no working directory")
    pid = None
    for line in listed.splitlines():
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


def in_worktree(root: str, processes: dict[int, Process]) -> list[Process]:
    """This user's processes whose working directory is inside root, without this process and its ancestors."""
    root = os.path.realpath(root)
    me = {os.getpid(), *ancestors(processes, os.getpid())}
    return [p for p in processes.values()
            if p.pid not in me and p.uid == os.getuid() and inside(p.cwd, root)]


# slotfather

def slotfather() -> str | None:
    """STRAYLIGHT_SLOTFATHER (empty: none), else slotfather on PATH, else ~/.local/bin/slotfather."""
    configured = os.environ.get("STRAYLIGHT_SLOTFATHER")
    if configured is not None:
        return configured or None
    fallback = os.path.expanduser("~/.local/bin/slotfather")
    return shutil.which("slotfather") or (fallback if os.access(fallback, os.X_OK) else None)


def answer(argv: list[str], deadline: Deadline | None = None, cap: float = TIMEOUT, ok=(0,)) -> dict:
    output = run(argv, deadline, cap, ok).stdout
    try:
        data = json.loads(output)
    except ValueError:
        raise ScanError(f"{' '.join(argv[1:3])} gave no JSON answer: {output.strip()[:120]!r}") from None
    if not isinstance(data, dict):
        raise ScanError(f"{' '.join(argv[1:3])} gave no JSON answer")
    return data


def lane_leases(lane: str, deadline: Deadline | None = None) -> list[dict]:
    """The live leases whose metadata names this lane (tools/heavy.py writes it on every lease Straylight takes)."""
    program = slotfather()
    if not (program and lane):
        return []
    entries = answer([program, "status", "--json"], deadline).get("entries")
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("meta"), dict)
            and e["meta"].get("lane") == lane] if isinstance(entries, list) else []


def carrying(lease_paths: set[str], deadline: Deadline | None = None) -> set[int]:
    """This user's processes whose environment holds one of these SLOTFATHER_LEASE values."""
    if not lease_paths:
        return set()
    found = set()
    for line in run(["ps", "-wwE", "-axo", "pid=,command="], deadline).stdout.splitlines():
        pid, _, rest = line.strip().partition(" ")
        if pid.isdigit() and any(f" SLOTFATHER_LEASE={path} " in f" {rest} " for path in lease_paths):
            found.add(int(pid))
    return found


def describe_lease(entry: dict) -> str:
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    named = ", ".join(f"{key} {meta[key]}" for key in ("lane", "ticket", "kind", "session") if meta.get(key))
    return f"lease {entry.get('label') or entry.get('id')}" + (f" ({named})" if named else "")


def sweep(pids: list[int], live: bool = False) -> list[dict]:
    """slotfather sweep's items for these pids: dry (--json --no-log) or stopping them (--live --json)."""
    program = slotfather()
    if program is None:
        raise ScanError("slotfather is not installed, so nothing says which of them may be stopped")
    argv = [program, "sweep", "--json", *(["--live"] if live else ["--no-log"])]
    for pid in pids:
        argv += ["--pid", str(pid)]
    # A live sweep waits out its grace (10 s by default) and then SIGKILLs; exit 1 says a stop failed.
    data = answer(argv, cap=120 if live else 30, ok=(0, 1) if live else (0,))
    items = data.get("items")
    if not isinstance(items, list):
        raise ScanError("slotfather sweep gave no items")
    if live and data.get("live_refused"):
        raise ScanError(f"slotfather sweep stopped nothing: {data.get('settings_error')}")
    return [item for item in items if isinstance(item, dict) and isinstance(item.get("pid"), int)]


# A worktree's processes before it is removed: the seam tools/lane done, tools/janitor and lanefather's done
# ([hooks] process_scan in .lanefather.toml) call. Neither function raises: what cannot be read holds the worktree.

class Item:
    """A process slotfather sweep would stop, with its tree."""
    __slots__ = ("pid", "command", "klass", "reason", "tree")

    def __init__(self, entry: dict):
        self.pid, self.command = entry["pid"], str(entry.get("cmd") or "")
        self.klass, self.reason = str(entry.get("class") or "?"), str(entry.get("reason") or "")
        self.tree = [p for p in entry.get("tree") or [] if isinstance(p, int)] or [self.pid]

    def describe(self) -> str:
        return f"pid {self.pid} ({self.command[:100]}; {self.klass}: {self.reason})"


def sort_for_removal(root: str, processes: dict[int, Process] | None = None) -> tuple[list[Item], list[str]]:
    """What slotfather sweep would stop in root, and why each other process there holds it (a process the sweep
    spares or does not offer; every process when the table or the sweep's answer cannot be read)."""
    try:
        processes = table() if processes is None else processes
        found = in_worktree(root, processes)
        if not found:
            return [], []
        entries = sweep([p.pid for p in found])
    except ScanError as error:
        return [], [f"which processes in {root} may be stopped is unknown: {error}"]
    stopping = [Item(e) for e in entries if e.get("action") == "would-stop"]
    covered = {pid for item in stopping for pid in item.tree}
    reasons = {e["pid"]: f"{e.get('class')}: {e.get('reason')}" for e in entries}
    held = [f"{p.describe()}: " + (f"slotfather sweep spares it ({reasons[p.pid]})" if p.pid in reasons else
                                   "slotfather sweep does not take it for left behind")
            for p in found if p.pid not in covered]
    return stopping, held


def stop(items: list[Item], root: str | None = None) -> list:
    """Stop the items through `slotfather sweep --live --pid`; the items that did not stop and, with root, the
    processes still in it after (each with .pid and .describe()). Empty: done. A failed read leaves every item."""
    if not items:
        return []
    try:
        done = {e["pid"]: e for e in sweep([item.pid for item in items], live=True)}
        left = [item for item in items if done.get(item.pid, {}).get("action") not in STOPPED]
        for item in left:
            item.reason += f"; sweep: {done[item.pid].get('action') if item.pid in done else 'no longer offered'}"
        return left + (in_worktree(root, table()) if root else [])
    except ScanError as error:
        for item in items:
            item.reason += f"; the stop could not be confirmed: {error}"
        return list(items)
