"""The process table every fathers tool reads (fathers#171): `ps` rows by column, identities, trees, groups and
working directories. Read-only: nothing here signals a process.

A process is known by `{pid, started, cmd}`: its pid, the epoch second it started and its command. A pid alone can
be reused, and so can the pair (lstart has one-second granularity), so a decision to signal also checks the command
(`same_process`, `command_matches`). lstart text is presentation: UTC in Spawnfather's entries, local time in
Slotfather's (`_lstart`); two tools compare epochs, never text.

Fail-closed (fathers#121): a ps that cannot run, times out, exits non-zero or lists nothing (with `caller`, a table
without this process) raises ProcTableError, never an empty table that would read every process as gone; the same
holds for working directories (`working_dirs`, strict) or is said in `Cwds.complete` (lenient).

Imports only os, subprocess, sys and time, so a command's fast path can load it.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from ._cwd import Cwd, Cwds, proc_start_epoch, working_dirs
from ._lstart import Clock, epochs, names, norm, render
from ._lstart import FORMAT as LSTART
from ._lstart import parse as parse_lstart
from ._ps import ProcTableError, columns, environment, fields, parse, rows, run, table_rows
from ._table import EPOCH_SLACK, Proc, Table, command_matches, same_process

__all__ = [
    "EPOCH_SLACK", "LSTART", "TABLE", "Clock", "Cwd", "Cwds", "Proc", "ProcTableError", "Table", "columns",
    "command_matches", "environment", "epochs", "fields", "identities", "names", "norm", "parse", "parse_lstart",
    "proc_start_epoch", "read", "render", "rows", "run", "same_process", "table_rows", "working_dirs",
]

TABLE = "pid=,ppid=,pgid=,uid=,pcpu=,tty=,lstart=,command="  # read()'s default columns


def read(spec: str | Sequence[str] = TABLE, *, clock: Clock = "utc", timeout: float,
         env: Mapping[str, str] | None = None, caller: bool = False, zombies: bool = True) -> Table:
    """Every process at this moment: one `ps -ax -ww -o fields(spec)` in the C locale and `clock` (env: this
    process's environment set by environment(clock)). Without `zombies`, zombies (a `stat` column tells them) are
    left out. Raises ProcTableError for a table that cannot be trusted (rows(); with `caller`, one without this
    process)."""
    lines = rows(["ps", "-ax", "-ww", "-o", fields(spec)], caller=caller,
                 env=environment(clock) if env is None else env, timeout=timeout)
    found = parse(lines, spec, clock)
    if not zombies:
        found = {pid: proc for pid, proc in found.items() if not proc.zombie}
    if not found:
        raise ProcTableError("ps listed no live processes")
    return Table(found)


def identities(pids: Sequence[int], spec: str | Sequence[str] = "pid=,stat=,lstart=,command=", *,
               clock: Clock = "utc", timeout: float, env: Mapping[str, str] | None = None,
               strict: bool = False) -> dict[int, Proc]:
    """The live, non-zombie processes among `pids`, from one targeted `ps -ww -o fields(spec) -p PIDS`. A pid that
    is not listed is gone. Raises ProcTableError when ps cannot be asked or exits other than 0 or 1 (`ps -p` exits
    1 when some are gone); with `strict` also for exit 1 with an error message: an unreadable answer is never
    "they are gone"."""
    wanted = sorted(set(pids))
    if not wanted:
        return {}
    result = run(["ps", "-ww", "-o", fields(spec), "-p", ",".join(map(str, wanted))],
                 env=environment(clock) if env is None else env, timeout=timeout, ok=(0, 1))
    if strict and result.returncode and result.stderr.strip():
        raise ProcTableError(f"ps exited {result.returncode}: {result.stderr.strip()[:200]}",
                             f"ps exited {result.returncode}")
    return {pid: proc for pid, proc in parse(result.stdout, spec, clock).items() if not proc.zombie}
