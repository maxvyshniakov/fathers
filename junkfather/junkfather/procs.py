"""The process table: one `ps` read, identities (pid plus start time), trees and process groups.

A process is known by its pid and its start time (`ps -o lstart` in the C locale). A pid alone can be reused; the
pair cannot within the lifetime of a machine's clock. Every decision to signal a process goes through that pair.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Dict, List, NamedTuple, Optional

TIMEOUT = 10.0  # seconds a ps or lsof call may take
C_LOCALE = {"LC_ALL": "C", "LANG": "C"}
LSTART = "%a %b %d %H:%M:%S %Y"


class ProcError(Exception):
    """A process table that could not be read: never taken for "nothing runs"."""


class Proc(NamedTuple):
    pid: int
    ppid: int
    pgid: int
    uid: int
    tty: str  # "" without a controlling terminal
    start: str  # lstart in the C locale, spaces normalized
    cmd: str

    @property
    def started(self) -> Optional[float]:
        return started_at(self.start)

    @property
    def program(self) -> str:
        return os.path.basename((self.cmd.split() or [""])[0])


def norm(start: str) -> str:
    return " ".join(str(start).split())


def started_at(lstart: str) -> Optional[float]:
    """Epoch seconds of a local-time lstart; None when it does not parse."""
    try:
        return time.mktime(time.strptime(norm(lstart), LSTART))
    except ValueError:
        return None


def run(argv: List[str], ok=(0,)) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT,
                                env={**os.environ, **C_LOCALE})
    except subprocess.TimeoutExpired:
        raise ProcError(f"{argv[0]} did not answer in {TIMEOUT:.0f} s") from None
    except OSError as error:
        raise ProcError(f"{argv[0]}: {error}") from None
    if result.returncode not in ok:
        raise ProcError(f"{' '.join(argv[:2])} exited {result.returncode}: {result.stderr.strip()[:200]}")
    return result.stdout


def parse(text: str) -> Dict[int, Proc]:
    found: Dict[int, Proc] = {}
    for line in text.splitlines():
        parts = line.split(None, 10)
        if len(parts) < 10 or not all(part.isdigit() for part in parts[:4]):
            continue
        tty = "" if parts[4] in ("??", "?", "-") else parts[4]
        found[int(parts[0])] = Proc(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), tty,
                                    norm(" ".join(parts[5:10])), parts[10].strip() if len(parts) > 10 else "")
    return found


class Table:
    """Every process at one moment. Tests build one from Procs."""

    def __init__(self, procs: Dict[int, Proc], moment: Optional[float] = None):
        self.procs = procs
        self.moment = time.time() if moment is None else moment
        self.children: Dict[int, List[int]] = {}
        for proc in procs.values():
            self.children.setdefault(proc.ppid, []).append(proc.pid)

    @classmethod
    def read(cls) -> "Table":
        found = parse(run(["ps", "-ax", "-ww", "-o", "pid=,ppid=,pgid=,uid=,tty=,lstart=,command="]))
        if not found:
            raise ProcError("ps listed no process")
        return cls(found)

    def get(self, pid: Optional[int]) -> Optional[Proc]:
        return self.procs.get(pid) if isinstance(pid, int) else None

    def alive(self, pid: Optional[int], start: Optional[str]) -> bool:
        """Whether this pid still runs the process that had this start time."""
        proc = self.get(pid)
        return proc is not None and bool(start) and proc.start == norm(start or "")

    def ancestors(self, pid: int) -> List[int]:
        chain: List[int] = []
        here = self.procs[pid].ppid if pid in self.procs else 0
        while here in self.procs and here not in chain and here > 1:
            chain.append(here)
            here = self.procs[here].ppid
        return chain

    def descendants(self, pid: int) -> List[Proc]:
        found: List[Proc] = []
        seen = {pid}
        stack = list(self.children.get(pid, ()))
        while stack:
            here = stack.pop()
            if here in seen or here not in self.procs:
                continue
            seen.add(here)
            found.append(self.procs[here])
            stack.extend(self.children.get(here, ()))
        return found

    def group(self, pgid: int) -> List[Proc]:
        return [proc for proc in self.procs.values() if proc.pgid == pgid]

    def age(self, proc: Proc) -> Optional[float]:
        started = proc.started
        return None if started is None else max(0.0, self.moment - started)


def identities(pids: List[int]) -> Dict[int, str]:
    """pid -> start time for those of `pids` that still run and are not zombies; {} when ps cannot be asked."""
    if not pids:
        return {}
    try:
        text = run(["ps", "-o", "pid=,stat=,lstart=", "-p", ",".join(str(pid) for pid in pids)], ok=(0, 1))
    except ProcError:
        return {}
    found: Dict[int, str] = {}
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and not parts[1].startswith("Z"):
            found[int(parts[0])] = norm(parts[2])
    return found


def working_dirs(uid: int) -> Dict[int, str]:
    """This user's processes' working directories (one lsof call); {} when lsof cannot be asked."""
    try:
        text = run(["lsof", "-nP", "-a", "-u", str(uid), "-d", "cwd", "-Fpn"], ok=(0, 1))
    except ProcError:
        return {}
    found: Dict[int, str] = {}
    pid = None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            found[pid] = line[1:]
    return found
