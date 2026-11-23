"""The process table: one `ps` read, identities (pid plus start time), trees and process groups.

The reader is the shared one, `fathers_common.proc` (fathers#171); this module fixes Spawnfather's columns and clock.
A process is known by its pid and its start time (`ps -o lstart` in the C locale and UTC, so every caller reads the
same text whatever its TZ). A pid alone can be reused, and so can the pair: lstart has one-second granularity, so a
pid that ends and is taken again within the same second keeps the same pair. The command is therefore the second
factor: every decision to signal a process checks the pid, the start time and the command (command_matches).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from fathers_common import proc as _proc

from .defaults import PS_TIMEOUT

C_LOCALE = {"LC_ALL": "C", "LANG": "C", "TZ": "UTC"}
LSTART = _proc.LSTART
FIELDS = "pid=,ppid=,pgid=,uid=,pcpu=,tty=,lstart=,command="

ProcError = _proc.ProcTableError
Proc = _proc.Proc
norm = _proc.norm
command_matches = _proc.command_matches
same_process = _proc.same_process
EPOCH_SLACK = _proc.EPOCH_SLACK


def started_at(lstart: str) -> Optional[float]:
    """Epoch seconds of a UTC lstart; None when it does not parse."""
    found = _proc.parse_lstart(lstart, "utc")
    return None if found is None else float(found)


def run(argv: List[str], ok: Tuple[int, ...] = (0,)) -> str:
    """The output of `argv` in the C locale and UTC; ProcError when it cannot run, times out or exits outside `ok`."""
    return _proc.run(argv, env=_proc.environment("utc"), timeout=PS_TIMEOUT, ok=ok).stdout


def parse(text: str) -> Dict[int, Proc]:
    """`ps -o FIELDS` lines as Procs; a line that does not read is skipped."""
    return _proc.parse(text, FIELDS, "utc")


class Table(_proc.Table):
    """Every process at one moment. Tests build one from Procs."""

    @classmethod
    def read(cls) -> "Table":
        return cls(_proc.read(FIELDS, clock="utc", timeout=PS_TIMEOUT).procs)


def read_or_none() -> Optional[Table]:
    """The process table, or None when it cannot be read (for callers to whom unknown is an answer)."""
    try:
        return Table.read()
    except ProcError:
        return None


def identities(pids: List[int], command: bool = False) -> Dict[int, Any]:
    """pid -> start time (with `command`, pid -> (start time, command)) for those of `pids` that still run and are
    not zombies. ProcError when ps cannot be asked: an unreadable answer is never "they are gone"."""
    spec = "pid=,stat=,lstart=" + (",command=" if command else "")
    found = _proc.identities(pids, spec, clock="utc", timeout=PS_TIMEOUT)
    if not command:
        return {pid: here.start for pid, here in found.items()}
    return {pid: (here.start, here.cmd) for pid, here in found.items()}


def working_dirs(uid: int) -> Dict[int, str]:
    """This user's processes' working directories (one lsof call); {} when lsof cannot be asked."""
    return working_dirs_or_none(uid) or {}


def working_dirs_or_none(uid: int) -> Optional[Dict[int, str]]:
    """This user's processes' working directories (one lsof call); None when lsof cannot be asked or lists none."""
    listed = _proc.working_dirs({}, uid, timeout=PS_TIMEOUT, use_proc=False)
    if not listed.complete:
        return None
    return {pid: cwd.path for pid, cwd in listed.paths.items()} or None
