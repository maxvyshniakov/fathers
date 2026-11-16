"""`slotfather lock PATH -- COMMAND` (fathers#159): run COMMAND holding an exclusive flock on PATH, without keeping a
slot while it waits for it.

When the lock is free it is taken at once and COMMAND runs. When another process holds it, the lease this process
runs under (SLOTFATHER_LEASE, or a holder among its ancestors; queue.inherited_lease()) gives up its slots while
this waits (leases.yield_for_lock(): yielded, in its own place, and never granted while it waits); once the lock is
taken it waits to be granted again (leases.lock_taken()) and only then runs COMMAND. Outside a lease it is a plain
lock. The lock is held by this process, not inherited by COMMAND, and released when this exits."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import FrameType

from . import config as _config
from . import entry as _entry

USAGE = "usage: slotfather lock PATH -- COMMAND..."


class Stopped(Exception):
    """A signal ended the wait for the lock."""

    def __init__(self, number: int) -> None:
        super().__init__(number)
        self.number = number


def parse(argv: list[str]) -> tuple[Path, list[str]]:
    if len(argv) < 3 or argv[1] != "--" or not argv[0]:
        raise _config.SlotError(USAGE)
    return Path(os.path.expanduser(argv[0])), argv[2:]


def enclosing() -> Path | None:
    """The held lease of this queue this process runs under, or None (none, an earlier queue's, or one already
    waiting)."""
    from . import process as _process
    from . import queue as _queue
    found = _queue.inherited_lease(_process.processes())
    if found is None or found[0].parent.parent != _config.state_dir() or not _entry.is_held(found[1]):
        return None
    return found[0]


def command(argv: list[str]) -> int:
    path, run = parse(argv)
    try:
        handle = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o666)
    except OSError as error:
        raise _config.SlotError(f"cannot open lock {path}: {error.strerror}") from None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        wait(handle, path)
    return execute(run)


def wait(handle: int, path: Path) -> None:
    """Waits for the lock with the enclosing lease's slots given up, then for the slots again."""
    from . import leases as _leases
    lease = enclosing()
    if lease is not None and not _leases.yield_for_lock(lease, str(path)):
        lease = None
    print(f"slot: waiting for lock {path}" + ("; this lease's slots are free meanwhile" if lease else ""),
          file=sys.stderr, flush=True)

    def stop(number: int, _frame: FrameType | None) -> None:
        raise Stopped(number)

    before = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
    except Stopped as stopped:
        if lease is not None:  # what runs on under the lease waits as an ordinary yielded lease, never stuck
            _leases.lock_taken(lease, wait=False)
        raise SystemExit(128 + stopped.number) from None
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)
    if lease is not None:
        print(f"slot: took lock {path}; waiting for this lease's slots again", file=sys.stderr, flush=True)
        _leases.lock_taken(lease)
        print("slot: holding the slots again", file=sys.stderr, flush=True)


def execute(run: list[str]) -> int:
    """Runs the command with the lock held; its exit status, or 128 + the signal that ended it. SIGTERM and SIGHUP
    are passed on to it; SIGINT, which a terminal sends the whole group, is left to it."""
    try:
        child = subprocess.Popen(run)
    except OSError as error:
        raise _config.SlotError(f"cannot run {run[0]}: {error.strerror}") from None

    def forward(number: int, _frame: FrameType | None) -> None:
        child.send_signal(number)

    before = {number: signal.signal(number, forward) for number in (signal.SIGTERM, signal.SIGHUP)}
    before[signal.SIGINT] = signal.signal(signal.SIGINT, signal.SIG_IGN)  # the terminal sends it to the command
    try:
        status = child.wait()
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)
    return 128 - status if status < 0 else status
