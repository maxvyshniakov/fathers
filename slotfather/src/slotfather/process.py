"""Process observation: the one module that runs `ps` (fathers#38).

It holds the process-table readers with their named column sets, the tree helpers, the session pattern, the reader
of SLOTFATHER_LEASE markers in other processes' environments, and lease_membership(), the one rule for which live
lease a process belongs to (`status --json --pid`, `procs --json` and the sweep's protection).

Failure policy (fathers#121): a ps that cannot run, times out or exits non-zero gives no table, and neither does an
empty one; a table that reaps or protects leases must also list its caller. Such a table raises
config.ProcessTableError (PsError), never an empty answer that would read every holder as dead.

An environment that cannot be read (another user's process, an Apple platform binary on macOS, /proc mounted with
hidepid) is unknown, never "not carrying a lease" (owner decision on fathers#38).
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from typing import Callable, Iterable, Mapping, NamedTuple, Sequence, cast

from . import config as _config
from . import platform as _platform

# The column sets each reader asks ps for; every one starts with the pid.
IDENTITY = "pid=,ppid=,stat=,lstart="  # processes(): the queue's identities
USAGE = "pid=,ppid=,uid=,rss=,time=,lstart="  # usage_table(): the lease's sampler
MEMBERS = "pid=,ppid=,uid=,tty=,stat=,lstart=,command="  # procs: identities, sessions and commands
SWEEP = "pid=,ppid=,uid=,rss=,tty=,lstart=,command="  # the sweep's snapshot
CPU = "pid=,ppid=,%cpu=,rss=,comm="  # the dashboard block's CPU sample
STARTS = "pid=,stat=,lstart="  # the sweep's check before a signal
OWNER = "uid="  # process_uid()
ENVIRONMENT = "pid=,command="  # with -wwE: the command and, where shown, the environment
ARGUMENTS = "pid=,command="  # the full arguments, without the environment

# Agent and terminal sessions: an executable that is one, or a process with a terminal (session_command()).
SESSION = re.compile(r"(^|/)(claude|codex|CodexCLI|tmux|screen|zellij|mosh-server|sshd|sshd-session|login|"
                     r"Runner\.Listener|Runner\.Worker)$|/claude/versions/[^/]+$")

MARKER = "SLOTFATHER_LEASE="


class PsError(_config.ProcessTableError):
    """ps gave no usable answer; `detail` is the cause without the "ps" prefix some callers word themselves."""

    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


def ps_environment() -> dict[str, str] | None:
    """The environment ps runs in for processes(), whose lstart a new entry writes as its `start`: on Linux the C
    locale (procps writes lstart in the locale's words; every client since #653 writes C there); macOS keeps the
    caller's locale and time zone, as every older client does, so that an older client still reads a current
    client's `start` exactly as it read its own (fathers#122). Current clients compare `started` instead."""
    return _platform.c_locale() if sys.platform.startswith("linux") else None


def run(arguments: Sequence[str], *, env: Mapping[str, str] | None = None, timeout: float | None = None,
        executable: str = "ps") -> subprocess.CompletedProcess[str]:
    """One ps run; raises PsError when ps cannot run or takes longer than `timeout` (default PS_TIMEOUT). Its exit
    status is the caller's to judge: `ps -p` exits 1 when some of the pids are gone."""
    try:
        return subprocess.run([executable, *arguments], capture_output=True, text=True,
                              env=None if env is None else dict(env),
                              timeout=_config.PS_TIMEOUT if timeout is None else timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PsError(f"ps: {error}", str(error)) from None


def table(arguments: Sequence[str], *, caller: bool = True, env: Mapping[str, str] | None = None,
          timeout: float | None = None) -> list[str]:
    """The rows (lines whose first word is a pid) of a ps over the machine. Raises PsError for a ps that fails or
    times out, a table without a row, and, with `caller`, a table that leaves out this process: a filtered or cut
    table would read the holders it hides as dead (fathers#121)."""
    result = run(arguments, env=env, timeout=timeout)
    if result.returncode:
        raise PsError(f"ps exited {result.returncode}: {result.stderr.strip()[:200]}", f"ps exited {result.returncode}")
    rows = [line for line in result.stdout.splitlines() if (line.split() or [""])[0].isdigit()]
    if caller:
        me = str(os.getpid())
        if not any(line.split(None, 1)[0] == me for line in rows):
            message = f"ps listed {len(rows)} processes and not this one"
            raise PsError(f"{message} ({os.getpid()})", message)
    elif not rows:
        raise PsError("ps listed no live processes", "ps listed no live processes")
    return rows


# The machine's tables

def processes() -> dict[int, tuple[int, str]]:
    """Live pid -> (ppid, start time); the start time tells a reused pid from the holder. Zombies are dead. The same
    ps flags serve macOS and Linux (procps-ng). Raises ProcessTableError for a ps that fails, takes longer than
    PS_TIMEOUT, or gives a table without this process in it: such a table is no table, and reaping by it would
    take every lease from commands that still run."""
    found: dict[int, tuple[int, str]] = {}
    for line in table(["-axo", IDENTITY], env=ps_environment()):
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[1].isdigit() and not parts[2].startswith("Z"):
            found[int(parts[0])] = (int(parts[1]), parts[3].strip())
    if os.getpid() not in found:
        raise PsError(f"ps listed {len(found)} processes and not this one ({os.getpid()})",
                      f"ps listed {len(found)} processes and not this one")
    return found


def usage_table() -> dict[int, tuple[int, str, int, int, float]]:
    """Live pid -> (ppid, start, uid, resident KB, CPU seconds of its own), from one ps over the machine (on Linux
    the CPU from /proc/PID/stat where it can be read). Raises SlotError when ps fails or takes longer than
    PS_TIMEOUT. An unusable answer gives the rows it has: the sampler only adds up what it sees."""
    try:
        result = run(["-axo", USAGE], env=_platform.c_locale())
    except PsError as error:
        raise _config.SlotError(str(error)) from None
    found: dict[int, tuple[int, str, int, int, float]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) == 6 and all(p.isdigit() for p in parts[:4]):
            cpu = _platform.cpu_seconds(parts[4])
            if _platform.linux():
                cpu = _platform.proc_cpu_seconds(int(parts[0])) or cpu
            if cpu is not None:
                found[int(parts[0])] = (int(parts[1]), parts[5].strip(), int(parts[2]), int(parts[3]), cpu)
    return found


def cpu_table() -> tuple[dict[int, tuple[float, int, str]], dict[int, list[int]]]:
    """pid -> (CPU percent, resident KB, command name) for every process, and ppid -> its child pids. Raises PsError
    for a ps that fails or times out and for a table without this process (fathers#121)."""
    own: dict[int, tuple[float, int, str]] = {}
    children: dict[int, list[int]] = {}
    for line in table(["-axo", CPU], caller=False):
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[1].isdigit():
            with contextlib.suppress(ValueError):
                own[int(parts[0])] = (float(parts[2].replace(",", ".")), int(parts[3]), os.path.basename(parts[4]))
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
    if os.getpid() not in own:  # an empty or cut table is no sample
        raise PsError(f"ps listed {len(own)} processes and not this one",
                      f"ps listed {len(own)} processes and not this one")
    return own, children


def starts(pids: Iterable[int], timeout: float | None = None) -> dict[int, tuple[str, bool]]:
    """pid -> (lstart in the C locale, a zombie) for those of `pids` still in the process table; {} when ps gives
    no answer."""
    wanted = list(pids)
    if not wanted:
        return {}
    try:
        result = run(["-o", STARTS, "-p", ",".join(map(str, wanted))], env=_platform.c_locale(), timeout=timeout)
    except PsError:
        return {}
    found: dict[int, tuple[str, bool]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            found[int(parts[0])] = (" ".join(parts[2].split()), parts[1].startswith("Z"))
    return found


def process_uid(pid: object) -> int | None:
    """The real uid a live process runs as, or None when it is not running or cannot be inspected."""
    if type(pid) is not int or pid < 1:
        return None
    try:
        result = run(["-o", OWNER, "-p", str(pid)], timeout=5)
    except PsError:
        return None
    words = result.stdout.split()
    return int(words[0]) if result.returncode == 0 and len(words) == 1 and words[0].isdigit() else None


def arguments(pids: Iterable[int], timeout: float = 5) -> subprocess.CompletedProcess[str]:
    """One targeted /bin/ps of the full arguments of `pids`, without any environment (macos_process.snapshot())."""
    return run(["-ww", "-p", ",".join(map(str, sorted(pids))), "-o", ARGUMENTS], env=_platform.c_locale(),
               timeout=timeout, executable="/bin/ps")


# Trees

def children_of(parents: Mapping[int, int]) -> dict[int, list[int]]:
    """ppid -> its child pids, from pid -> ppid."""
    found: dict[int, list[int]] = {}
    for child, parent in parents.items():
        found.setdefault(parent, []).append(child)
    return found


def ancestors(parents: Mapping[int, int], pid: int) -> list[int]:
    """The live ancestors of `pid`, nearest parent first; cycles and PID 1 end the chain."""
    found: list[int] = []
    seen = {pid}
    parent = parents.get(pid, 0)
    while parent > 1 and parent in parents and parent not in seen:
        seen.add(parent)
        found.append(parent)
        parent = parents[parent]
    return found


def tree(children: Mapping[int, Sequence[int]], roots: Iterable[object], present: Callable[[int], bool]) -> list[int]:
    """The live processes of the trees under `roots` (each root and its descendants), depth first from the last
    root; `present` says which pids are live."""
    found: list[int] = []
    seen: set[int] = set()
    stack: list[object] = list(roots)
    while stack:
        here = stack.pop()
        if type(here) is not int or here in seen or not present(here):
            continue
        seen.add(here)
        found.append(here)
        stack.extend(children.get(here, ()))
    return found


def parents_of(table: Mapping[int, Sequence[object]]) -> dict[int, int]:
    """pid -> ppid of a table of pid -> (ppid, ...), such as processes()."""
    return {pid: cast(int, row[0]) for pid, row in table.items()}


def descendants(table: Mapping[int, Sequence[object]], pid: object) -> list[int]:
    """`pid` and its live descendants, from a table of pid -> (ppid, ...); [] when `pid` is not live."""
    if type(pid) is not int or pid not in table:
        return []
    return sorted(tree(children_of(parents_of(table)), [pid], table.__contains__))


def session_command(cmd: str, tty: str = "") -> bool:
    """Whether a process is a session: it has a terminal, or its executable (the command's first word) is an agent
    or a terminal multiplexer, sshd or login."""
    return bool(tty or SESSION.search((cmd.split() or [""])[0]))


# Lease markers in environments

def lease_markers(pids: Sequence[int], leases: Iterable[str]) -> tuple[dict[int, set[str]], list[int]]:
    """Which of `leases` (the paths SLOTFATHER_LEASE holds) each of `pids` carries in its environment, for each pid
    whose environment could be read, and the pids whose environment could not be read (unknown, never "not
    carrying"). Only markers are read; no environment contents are kept or returned.
    - Linux: /proc/PID/environ (the user's own processes; every one for root).
    - macOS: one `ps -wwE`; a pid it shows a marker for is read. For the others ps cannot prove that it showed the
      environment (it prints nothing for a hidden one), so the kernel's copy (macos_process.environment()) decides:
      an Apple platform binary's or another user's is hidden, and stays unknown."""
    paths = set(leases)
    found: dict[int, set[str]] = {}
    if not pids or not paths:
        return found, []
    if _platform.linux():
        unknown: list[int] = []
        for number in pids:
            try:
                fields = _platform.proc_path(str(number), "environ").read_bytes().split(b"\0")
            except OSError:
                unknown.append(number)
                continue
            found[number] = {path for path in paths if os.fsencode(MARKER + path) in fields}
        return found, unknown
    try:
        result = run(["-wwE", "-o", ENVIRONMENT, "-p", ",".join(map(str, pids))], env=_platform.c_locale())
    except PsError:
        return found, list(pids)
    if result.returncode and not result.stdout.strip():
        return found, list(pids)
    wanted = set(pids)
    for line in result.stdout.splitlines():
        number, _, rest = line.strip().partition(" ")
        if number.isdigit() and int(number) in wanted:
            marked = {path for path in paths if f" {MARKER}{path} " in f" {rest} "}
            if marked:
                found[int(number)] = marked
    from . import macos_process
    for number in pids:
        if number in found:
            continue
        environment = macos_process.environment(number)
        if environment is not None:
            found[number] = {path for path in paths if os.fsencode(MARKER + path) in environment}
    return found, [number for number in pids if number not in found]


def carrying(pids: list[int], lease: str) -> set[int]:
    """Those of `pids` whose environment holds SLOTFATHER_LEASE=<lease> (lease_markers()); one whose environment
    cannot be read is not among them."""
    found, _ = lease_markers(pids, [lease])
    return {pid for pid, marked in found.items() if marked}


# Lease membership

class Lease(NamedTuple):
    """A live lease as membership needs it: the path its SLOTFATHER_LEASE holds and its holder's pid."""
    path: str
    pid: object


class Membership(NamedTuple):
    """lease_membership()'s answer: pid -> [(index into the leases, "tree" | "env")], in the leases' order, and the
    pids whose environment could not be read where it decides (unknown: neither in nor out of those leases)."""
    leases: dict[int, list[tuple[int, str]]]
    unknown: list[int]


Markers = Callable[[Sequence[int], Iterable[str]], "tuple[dict[int, set[str]], list[int]]"]


def lease_membership(parents: Mapping[int, int], leases: Sequence[Lease], pids: Iterable[int],
                     markers: Markers | None = lease_markers) -> Membership:
    """The one rule for which live leases each of `pids` belongs to (fathers#38): a lease's holder or one of its
    descendants is in it ("tree"); a process outside a lease's tree is in it when its environment carries the
    lease's SLOTFATHER_LEASE ("env": it left the tree by a double fork or setsid). A pid that is not live (not in
    `parents`) belongs to none. A pid outside some lease's tree whose environment cannot be read is unknown.
    `markers` reads the environments of all the pids that need it at once; None reads none (the tree alone)."""
    found: dict[int, list[tuple[int, str]]] = {}
    outside: dict[int, list[int]] = {}
    for pid in pids:
        if pid not in parents:
            continue
        chain = {pid, *ancestors(parents, pid)}
        found[pid] = [(index, "tree") for index, lease in enumerate(leases) if lease.pid in chain]
        rest = [index for index, lease in enumerate(leases) if lease.pid not in chain]
        if rest:
            outside[pid] = rest
    unknown: list[int] = []
    if markers is not None and outside:
        marked, hidden = markers(list(outside), sorted({leases[i].path for rest in outside.values() for i in rest}))
        unknown = sorted(pid for pid in hidden if pid in outside)
        for pid, rest in outside.items():
            carried = marked.get(pid, set())
            found[pid] = sorted(found[pid] + [(index, "env") for index in rest if leases[index].path in carried])
    return Membership(found, unknown)
