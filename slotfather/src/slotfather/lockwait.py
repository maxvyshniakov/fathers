"""`slotfather lock PATH -- COMMAND` (fathers#159): run COMMAND holding an exclusive flock on PATH, without keeping a
slot while it waits for it, when nothing else needs that slot.

When the lock is free it is taken at once and COMMAND runs (under a lease that had given up its slots for a lock,
this is new work: the lease takes them back first, client.resume_lock_yield()). When another process holds it, the
lease this process runs under (SLOTFATHER_LEASE, or a holder among its ancestors; queue.inherited_lease()):
- gives up its slots while this waits if nothing else runs under it (no process in the holder's tree but this one
  and its ancestors): leases.yield_for_lock(), yielded in its own place and never granted while it waits;
- otherwise keeps them, since other work (a parallel step, a container) runs on them, and only says what it waits
  for (`lock` on the held entry).
Either way the mark names this process (`lock_owner`), so a waiter that dies, even by SIGKILL, leaves a mark the
next look clears (queue.reap()). Once the lock is taken, or a signal ends the wait, the mark goes and this waits until
the lease is held again before it runs COMMAND or exits: whatever runs next under the lease runs on its slots.
Outside a lease it is a plain lock. The lock is held by this process, not inherited by COMMAND, and released when
this exits. COMMAND runs as `slotfather run` runs its command (runner.execute())."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import signal
import sys
from types import FrameType

from . import config as _config
from . import entry as _entry

USAGE = "usage: slotfather lock PATH -- COMMAND..."
SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


class Stopped(Exception):
    """A signal ended the wait for the lock."""

    def __init__(self, number: int) -> None:
        super().__init__(number)
        self.number = number


def parse(argv: list[str]) -> tuple[Path, list[str]]:
    if len(argv) < 3 or argv[1] != "--" or not argv[0]:
        raise _config.SlotError(USAGE)
    return Path(os.path.expanduser(argv[0])), argv[2:]


def stop(number: int, _frame: FrameType | None) -> None:
    raise Stopped(number)


def command(argv: list[str]) -> int:
    path, run = parse(argv)
    try:
        handle = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o666)
    except OSError as error:
        raise _config.SlotError(f"cannot open lock {path}: {error.strerror}") from None
    # The handlers come first: a signal at any moment from here leaves no mark behind (fathers#159 review)
    before = {number: signal.signal(number, stop) for number in SIGNALS}
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            free = True
        except BlockingIOError:
            free = False
        if free:
            resume()
        else:
            wait(handle, path)
    except Stopped as stopped:
        raise SystemExit(128 + stopped.number) from None
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)
    from . import runner as _runner
    status, _ = _runner.execute(run, {})
    return status


def resume() -> None:
    """The lock was free: under a lease that gave up its slots for a lock, COMMAND is new work, so the lease takes
    them back and this waits for its grant before COMMAND runs."""
    from . import client as _client
    from . import process as _process
    from . import queue as _queue
    found = _queue.inherited_lease(_process.processes())
    if (found is not None and found[0].parent.parent == _config.state_dir() and not _entry.is_held(found[1])
            and (_entry.lock_of(found[1]) is not None or isinstance(found[1].get("session"), dict))):
        _client.resume_lock_yield(found[0], found[1])
        _client.held_again(found[0])


def enclosing() -> tuple[Path, tuple[int, str], bool] | None:
    """This queue's enclosing held lease or yielded session, the caller's identity and its plain-lease idleness."""
    from . import process as _process
    from . import queue as _queue
    table = _process.processes()
    found = _queue.inherited_lease(table)
    if found is None or found[0].parent.parent != _config.state_dir():
        return None
    path, entry, _ = found
    session = isinstance(entry.get("session"), dict)
    if not _entry.is_held(entry) and not session:
        return None
    me = os.getpid()
    # Sessions verify their detached runs in the yield transaction, under the lock that registers their runs.
    idle = not session and not _process.other_work(table, entry.get("pid"), me)
    return path, (me, table[me][1] if me in table else ""), idle


def wait(handle: int, path: Path) -> None:
    """Waits for the lock: the enclosing lease marked (yielded when idle, noted otherwise), then held again."""
    from . import client as _client
    from . import leases as _leases
    found = enclosing()

    def resume_waiter(why: str) -> None:
        if found is not None:
            lease, owner, _ = found
            _leases.lock_taken(lease, why, lock=str(path), owner=owner)
            _client.held_again(lease)

    try:
        outcome = None
        if found is not None:
            outcome = _leases.yield_for_lock(found[0], str(path), found[1], found[2])
        note = {"yielded": "; this lease's slots are free meanwhile",
                "noted": "; this lease keeps its slots: other work runs under it"}.get(outcome or "", "")
        print(f"slot: waiting for lock {path}{note}", file=sys.stderr, flush=True)
        fcntl.flock(handle, fcntl.LOCK_EX)
        if outcome == "yielded":
            print(f"slot: took lock {path}; waiting for this lease's slots again", file=sys.stderr, flush=True)
        resume_waiter("took")
        if outcome == "yielded":
            print("slot: holding the slots again", file=sys.stderr, flush=True)
    except Stopped:
        # A second signal ends this at once (the default handlers); a mark it leaves is cleared by the next look
        for number in SIGNALS:
            signal.signal(number, signal.SIG_DFL if number != signal.SIGINT else signal.default_int_handler)
        resume_waiter("stopped")  # whatever runs next under the lease runs on its slots
        raise
