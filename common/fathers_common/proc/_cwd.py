"""Working directories (`/proc/PID/cwd` on Linux, `lsof` elsewhere) and Linux start times from /proc (fathers#171).

A listing that cannot be read is never "no process is in there": strict readers raise, lenient ones say so in
`notes` and `complete`.
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Mapping, NamedTuple

from ._ps import ProcTableError, Runner, run

DELETED = " (deleted)"


class Cwd(NamedTuple):
    """An observed working directory and whether the kernel or filesystem marks it removed (None: unknown)."""
    path: str
    removed: bool | None


class Cwds(NamedTuple):
    """`paths`: pid -> Cwd; `foreign`: pids in another mount namespace (a container), whose paths mean nothing
    here; `notes`: what could not be read; `complete`: whether the listing itself could be read (False: a process
    missing from `paths` may still be anywhere)."""
    paths: dict[int, Cwd]
    foreign: set[int]
    notes: list[str]
    complete: bool


def _proc(root: str, *parts: str) -> str:
    return os.path.join(root, *parts)


def working_dirs(
    uids_by_pid: Mapping[int, int],
    uid: int,
    *,
    timeout: float = 10,
    check_removed: Callable[[str, int | None], bool | None] | None = None,
    on_denied: Callable[[int], None] | None = None,
    strict: bool = False,
    use_proc: bool | None = None,
    proc_root: str = "/proc",
    runner: Runner | None = None,
) -> Cwds:
    """The working directories of `uid`'s processes: from `/proc/PID/cwd` for the pids of `uids_by_pid` that are
    `uid`'s (Linux, or `use_proc`), else from one `lsof` over the user (only the pids of `uids_by_pid` with
    `strict`, every listed one without).

    With `strict`, a listing that cannot be read raises ProcTableError and paths are `os.path.realpath`ed. Without,
    failures become `notes` (and `complete` False for an unreadable lsof), and `removed` is filled from /proc or
    `check_removed(path, inode)`. `runner` runs lsof (default subprocess.run)."""
    found: dict[int, Cwd] = {}
    foreign: set[int] = set()
    notes: list[str] = []
    if sys.platform.startswith("linux") if use_proc is None else use_proc:
        own: str | None = None
        try:
            own = os.readlink(_proc(proc_root, "self", "ns", "mnt"))
        except OSError as error:
            if strict:
                raise ProcTableError(f"cannot read own mount namespace: {error}", str(error)) from None
        for pid, proc_uid in uids_by_pid.items():
            if proc_uid != uid:
                continue
            try:
                if own is not None and os.readlink(_proc(proc_root, str(pid), "ns", "mnt")) != own:
                    foreign.add(pid)
                    continue
                link = os.readlink(_proc(proc_root, str(pid), "cwd"))
            except PermissionError:
                if on_denied is not None:
                    on_denied(pid)
                if not strict and own is not None:
                    foreign.add(pid)
                continue
            except OSError:
                if not strict and own is not None:
                    foreign.add(pid)  # its namespace cannot be read: do not judge it
                continue
            if link.endswith(DELETED):
                path = link[:-len(DELETED)]
                found[pid] = Cwd(os.path.realpath(path) if strict else path, True)
                continue
            if strict:
                found[pid] = Cwd(os.path.realpath(link), None)
                continue
            try:
                removed: bool | None = os.stat(_proc(proc_root, str(pid), "cwd")).st_nlink == 0
            except OSError:
                removed = None
            found[pid] = Cwd(link, removed)
        if foreign and not strict:
            notes.append(f"{len(foreign)} process(es) in another mount namespace (a container) left out")
        return Cwds(found, foreign, notes, True)
    flags = "-Fpin" if check_removed is not None else "-Fpn"
    try:
        result = run(["lsof", "-nP", "-a", "-d", "cwd", "-u", str(uid), "-w", flags], timeout=timeout, runner=runner)
    except ProcTableError as error:
        if strict:
            raise ProcTableError(f"cannot read working directories: {error.detail}", error.detail) from None
        return Cwds(found, foreign, [f"working directories unknown (lsof: {error.detail})"], False)
    if strict and (result.returncode not in (0, 1) or not result.stdout.strip()):
        raise ProcTableError(f"lsof could not read working directories (exit {result.returncode})",
                             f"lsof exited {result.returncode}")
    current: int | None = None
    inode: int | None = None
    for line in result.stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            current, inode = (int(value) if value.isdigit() else None), None
        elif tag == "i":
            inode = int(value) if value.isdigit() else None
        elif tag == "n" and current is not None:
            if strict:
                if uids_by_pid.get(current) == uid:
                    found[current] = Cwd(os.path.realpath(value), None)
            else:
                found[current] = Cwd(value, check_removed(value, inode) if check_removed is not None else None)
    return Cwds(found, foreign, notes, result.returncode in (0, 1))


def proc_start_epoch(pid: int, proc_root: str = "/proc") -> int | None:
    """Linux: the second a live process started, from /proc/PID/stat's starttime after /proc/stat's btime; None
    where it cannot be read. btime is derived from the uptime and may differ by a second between two reads
    (EPOCH_SLACK)."""
    try:
        with open(_proc(proc_root, str(pid), "stat"), encoding="utf-8", errors="replace") as handle:
            ticks = int(handle.read().rpartition(")")[2].split()[19])
        with open(_proc(proc_root, "stat"), encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("btime "):
                    return int(line.split()[1]) + ticks // os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return None
    return None
