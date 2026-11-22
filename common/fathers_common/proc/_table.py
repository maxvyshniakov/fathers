"""One process, and every process at one moment: trees, groups and ages (fathers#171)."""
from __future__ import annotations

import os
import time
from typing import Callable, NamedTuple

from ._lstart import Clock, norm
from ._lstart import parse as parse_lstart


class Proc(NamedTuple):
    """A process as ps showed it. `start` is its lstart text (spaces normalised) in `clock`, the one the table was
    read in; `epoch` the second it started, the identity that crosses between tools. A Proc built by hand without
    `epoch` reads its `start` in `clock` (default UTC)."""
    pid: int
    ppid: int
    pgid: int
    uid: int
    tty: str  # "" without a controlling terminal
    start: str
    cmd: str
    cpu: float = 0.0  # ps %cpu (100 = one core busy)
    rss: int = 0  # resident KB
    stat: str = ""  # ps stat; "Z..." for a zombie
    epoch: int | None = None
    clock: Clock = "utc"

    @property
    def started(self) -> float | None:
        """Epoch seconds it started, or None when that is not known."""
        if self.epoch is not None:
            return float(self.epoch)
        found = parse_lstart(self.start, self.clock)
        return None if found is None else float(found)

    @property
    def zombie(self) -> bool:
        return self.stat.startswith("Z")

    @property
    def program(self) -> str:
        return os.path.basename((self.cmd.split() or [""])[0])


def command_matches(now: str, seen: str) -> bool:
    """Whether a command read now is the one seen before: the same command line, or the same program when a
    process renamed its arguments."""
    if now == seen:
        return True

    def first(text: str) -> str:
        return os.path.basename((text.split() or [""])[0])
    return bool(first(now)) and first(now) == first(seen)


EPOCH_SLACK = 1  # seconds: Linux derives btime from the uptime and may round it either way between two reads


def same_process(started: float | None, cmd: str | None, now: Proc | None, *, slack: int = EPOCH_SLACK) -> bool:
    """Whether `now` is the process seen before as `{started, cmd}`: the start within `slack` seconds and, where
    `cmd` is given, the command (command_matches: a pid taken again within the same second is told apart by it).
    An unknown start on either side is never the same process."""
    if now is None or started is None or now.started is None:
        return False
    if abs(now.started - started) > slack:
        return False
    return cmd is None or command_matches(now.cmd, cmd)


class Table:
    """Every process at one moment. Tests build one from Procs."""

    def __init__(self, procs: dict[int, Proc], moment: float | None = None) -> None:
        self.procs = procs
        self.moment = time.time() if moment is None else moment
        self.children: dict[int, list[int]] = {}
        for proc in procs.values():
            self.children.setdefault(proc.ppid, []).append(proc.pid)

    def get(self, pid: object) -> Proc | None:
        return self.procs.get(pid) if isinstance(pid, int) else None

    def alive(self, pid: object, start: str | None = None, *, started: float | None = None,
              cmd: str | None = None) -> bool:
        """Whether this pid still runs the process seen before: by `started` (epoch, within EPOCH_SLACK, and `cmd`
        where given) when given, else by its `start` text in this table's clock."""
        proc = self.get(pid)
        if proc is None:
            return False
        if started is not None:
            return same_process(started, cmd, proc)
        return bool(start) and proc.start == norm(start or "")

    def ancestors(self, pid: int) -> list[int]:
        """Parent first, up to (not including) pid 1; a cycle ends the chain."""
        chain: list[int] = []
        here = self.procs[pid].ppid if pid in self.procs else 0
        while here in self.procs and here not in chain and here > 1:
            chain.append(here)
            here = self.procs[here].ppid
        return chain

    def descendants(self, pid: int, prune: Callable[[Proc], bool] | None = None) -> list[Proc]:
        """Every process below pid; with `prune`, a process it accepts is left out together with its subtree."""
        found: list[Proc] = []
        seen = {pid}
        stack = list(self.children.get(pid, ()))
        while stack:
            here = stack.pop()
            if here in seen or here not in self.procs:
                continue
            seen.add(here)
            if prune is not None and prune(self.procs[here]):
                continue
            found.append(self.procs[here])
            stack.extend(self.children.get(here, ()))
        return found

    def group(self, pgid: int) -> list[Proc]:
        return [proc for proc in self.procs.values() if proc.pgid == pgid]

    def age(self, proc: Proc) -> float | None:
        started = proc.started
        return None if started is None else max(0.0, self.moment - started)
