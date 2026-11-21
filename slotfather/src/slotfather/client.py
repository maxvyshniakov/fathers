"""The waiting client (fathers#37): a request's lease (Lease), acquire() that queues it and waits for its grant,
held_again() that waits for a yielded lease to be granted again, and yield_now(). Their waits share wait_until(),
which reads config.poll_settings() once. The queue transactions live in `leases` and the command runner in `runner`."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Callable, TypeVar

from . import config as _config
from . import entry as _entry
from . import labels as _labels
from . import leases as _leases
from . import process as _process
from . import queue as _queue
from . import settings as _settings
from . import state as _state


T = TypeVar("T")


def read(path: Path) -> dict[str, Any]:
    """The request's entry as written (whole, by rename: no lock needed); SlotError when it is gone."""
    current = _state.read_entry(path)
    if current is None or "state" not in current:
        raise _config.SlotError("the lease vanished from the queue")
    return current


def wait_until(ready: Callable[[], T | None], *, refresh: Callable[[], str | None] | None = None,
               deadline: float | None = None, between: Callable[[str | None], None] | None = None,
               changed: Callable[[str | None], None] | None = None, back_off: bool = False,
               polling: tuple[float, float] | None = None) -> T | None:
    """Poll a readiness check. Queue refreshes grow apart while the reason stays the same. A waiter can report
    or give up between checks. A deadline bounds pauses and returns None if it comes first."""
    poll, poll_max = polling or _config.poll_settings()
    said, interval, look, waiting = None, poll, 0.0, None
    while True:
        if refresh is not None and time.monotonic() >= look:
            waiting = refresh()
            interval = min(interval * 1.5, poll_max) if back_off or waiting == said else poll
            look = time.monotonic() + interval
        found = ready()
        if found is not None:
            return found
        if deadline is not None and time.monotonic() >= deadline:
            return None
        if between is not None:
            between(waiting)
        if not back_off and waiting != said:
            if changed is not None:
                changed(waiting)
            said = waiting
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            return ready()  # another process could have granted the request while we reported its wait
        pause = poll if refresh is None else min(poll, max(0.0, look - now))
        time.sleep(pause if deadline is None else min(pause, deadline - now))


def wait_held(path: Path, again: bool = False, deadline: float | None = None,
              between: Callable[[str | None], None] | None = None,
              changed: Callable[[str | None], None] | None = None, *,
              admit: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
              timeout_error: Callable[[str | None], _config.SlotTimeout] | None = None) -> dict[str, Any]:
    """Wait for the request to hold its slots. Queue refreshes grant and reap only with a trusted process table.
    A yielded lease is settled under the lock, including one an older client granted. A session run supplies
    `admit` so observing a hold and recording the run can be rechecked together. On expiry, `timeout_error`
    can include the most recent queue reason in the caller's diagnostic."""
    directory = path.parent.parent
    reason = None

    def refresh() -> str | None:
        nonlocal reason
        with _state.locked(directory):
            with contextlib.suppress(_config.SlotError if again else _config.ProcessTableError):
                reason = _leases.grant(directory, _queue.process_table(directory))
        return reason

    def ready() -> dict[str, Any] | None:
        current = read(path)
        if admit is not None:
            return admit(current)
        if not _entry.is_held(current):
            return None
        if not again:
            return current
        with _state.editing(path) as latest:
            if latest is None:
                raise _config.SlotError("the lease vanished from the queue")
            if not _entry.is_held(latest):
                return None
            if "yielded_at" in latest:  # an older client granted it
                _entry.resume(latest)
                _state.write(path, latest)
            return latest

    found = wait_until(ready, refresh=refresh, deadline=deadline, between=between, changed=changed, back_off=again)
    if found is None:
        if timeout_error is not None:
            raise timeout_error(reason)
        raise _config.SlotTimeout("the lease did not get its slots before the deadline")
    return found


def held_again(path: Path) -> None:
    """Waits until the yielded lease at `path` is granted again, looking at the queue as a waiter does."""
    wait_held(path, again=True)


def yield_now(path: Path, out: Any = sys.stderr) -> bool:
    """Yields the held lease at `path` if it was asked to (leases.yield_lease()) and waits until it is held again:
    True then; False at once when nothing asked."""
    found = _leases.yield_lease(path)
    if found is None:
        return False
    entry, asker = found
    print(f"slot: {entry.get('label')} yielded {entry['count']} slot(s) to {asker.get('label')}; it resumes when "
          f"they are free", file=out, flush=True)
    held_again(path)
    print(f"slot: {entry.get('label')} resumes with {entry['count']} slot(s)", file=out, flush=True)
    return True


class Lease:
    """A held lease, an inherited one, or none (bypass). Release it once the work has finished."""

    def __init__(self, count: int, path: Path | None = None, inherited: bool = False, yieldable: bool = False,
                 *, held: bool | None = None):
        self.count, self.path, self.inherited, self.yieldable = count, path, inherited, yieldable
        self.held: bool | None = held  # whether this client ever observed the lease as held (fathers#10)
        self.status: int | None = None  # the command's exit status, for the history, where the holder knows it
        # what the command's process tree used (TreeUsage.result), where the holder measured it
        self.usage: dict[str, Any] | None = None

    def environment(self) -> dict[str, str]:
        if self.path is None:
            return {}
        env: dict[str, str] = {}
        for prefix in _config.LEASE_ENV_PREFIXES:
            env.update({prefix + "LEASE": str(self.path), prefix + "COUNT": str(self.count)})
            if self.yieldable and not self.inherited:
                env[prefix + "YIELD"] = str(_state.marker(self.path))
        return env

    def yield_requested(self) -> dict[str, Any] | None:
        """The yield request for this lease, or None: {"by", "label", "class", "count", "at"} of the waiter that
        asked. Cheap (one stat and a small read); call it between steps."""
        return None if self.path is None or self.inherited else _state.asked(self.path)

    def yield_slots(self, out: Any = sys.stderr) -> bool:
        """Yields the lease if it was asked to: back among the waiters, just behind the waiter that asked, until it is
        granted again; returns True then, False at once when nothing asked (or the asker no longer waits). Call it
        only between steps, with none of the lease's own work running."""
        if self.path is None or self.inherited:
            return False
        return yield_now(self.path, out=out)

    def release(self, *, left_reason: str | None = None) -> None:
        if self.path is not None and not self.inherited:
            _leases.release(self.path, self.count, self.status, self.usage, held=self.held, left_reason=left_reason)
            self.path = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


def inherited(table: _queue.Table) -> Lease | None:
    """The lease this process runs under, here or in an earlier queue, as one it does not hand on or release; a
    yielded one is waited for first."""
    found = _queue.inherited_lease(table)
    if found is None:
        return None
    path, entry, count = found
    resume_lock_yield(path, entry)
    if "yielded_at" in entry:
        held_again(path)  # it yielded: what runs under it waits until it is granted again
    # A lease in an earlier queue is honoured but not handed on: nested runs find its holder among their ancestors.
    return Lease(count, path if path.parent.parent == _config.state_dir() else None, inherited=True)


def resume_lock_yield(path: Path, entry: dict[str, Any]) -> None:
    """New work under a lease that gave up its slots while its command waits on a lock (fathers#159 review): the
    lease takes the yield back (its lock mark goes, `why` "resumed") so that it is granted again in its place; the
    work then waits for that grant as under any yielded lease (held_again()). Without this a nested run in the
    lease's tree would wait for a grant the mark forbids, while the lock it waits behind may be its own tree's."""
    if path.parent.parent == _config.state_dir() and _entry.lock_of(entry) is not None and not _entry.is_held(entry):
        _leases.lock_taken(path, "resumed")


def policy() -> str:
    value = _config.setting("POLICY") or "normal"
    if value not in _config.POLICIES:
        raise _config.SlotError(f"invalid SLOTFATHER_POLICY {value!r}: use {', '.join(_config.POLICIES)}")
    return value


def acquire(count: int | str, label: str, command: str = "", out: Any = sys.stderr, wait: float | None = None,
            note: str = "", meta: dict[str, str] | None = None, yieldable: bool = False, *,
            priority: str | None = None, ticket: str | int | None = None,
            session: dict[str, Any] | None = None, naming: dict[str, str] | None = None) -> Lease:
    """Wait in the queue for `count` slots (at most the total); the lease is held by this process until released.
    Raises SlotTimeout, having left the queue, when `wait` seconds pass first. `note` follows the first word that
    the request waits. `meta` (key -> text) goes on the queue entry and the history line; a process that inherits
    a lease keeps that lease's metadata and adds none. A `yieldable` lease may be asked to yield for interactive
    waiters (see Lease.yield_slots(); the README's client protocol); it never is otherwise. `session` makes the
    lease a measurement session's (session.py): {"name", "idle", and "expect" where given}; a live request of the
    same session name refuses it."""
    if count != "all" and (type(count) is not int or count < 1):
        raise _config.SlotError("count must be a positive integer or all")
    meta = _entry.checked_meta(meta)
    if priority is not None and priority not in _config.PRIORITIES:
        raise _config.SlotError(f"priority is one of {', '.join(_config.PRIORITIES)}")
    selected_priority = priority if priority is not None else "normal"
    selected_ticket = _entry.checked_ticket(ticket)
    if policy() == "bypass":
        return Lease(_settings.total())
    directory = _config.state_dir()
    table = _process.processes()
    found = inherited(table)
    if found:
        return found
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    _state.require_own(directory, "a lease")
    git = _labels.git(cwd)  # before the queue lock: git may take a moment in a large repository
    path, count = _leases.enqueue(directory, count, table, label, command, cwd, selected_priority, selected_ticket,
                                  meta, naming, session, git, yieldable, wait)
    lease = Lease(count, path, yieldable=yieldable, held=False)
    deadline = None if wait is None else time.monotonic() + wait
    # A terminated waiter leaves the queue at once rather than at the next reap.
    main_thread = threading.current_thread() is threading.main_thread()
    previous_term = signal.signal(signal.SIGTERM, terminate) if main_thread else None
    queued = time.monotonic()
    warned: list[float] = []  # when it last said it waits long (WARN_REPEAT)
    said: list[str | None] = [None]  # the reason it last said it waits for

    def timeout_error(waiting: str | None) -> _config.SlotTimeout:
        assert wait is not None  # a deadline exists only for a bounded wait
        return _config.SlotTimeout(f"no {count} slot(s) within {wait:g} s: {waiting or 'earlier requests first'}")

    def between(_waiting: str | None) -> None:
        warn = float(_settings.order_settings(directory).get("warn_wait") or 0)
        if warn and time.monotonic() - queued >= warn and (not warned or
                                                          time.monotonic() - warned[-1] >= _config.WARN_REPEAT):
            warned.append(time.monotonic())
            with contextlib.suppress(Exception):  # a display must never cost the request its place
                from . import query as _query  # only a request that waits loads the views
                report = _query.long_wait_report(directory, path)
                if report:  # the client's own stderr, and queue.log (EVENTS): a long wait does not go unnoticed
                    line, facts = report
                    print(f"slot: {line}", file=out, flush=True)
                    _leases.waiting_long(directory, path, read(path), line, facts)

    def changed(waiting: str | None) -> None:
        print(f"slot: {label} waits for {count} slot(s): {waiting or 'earlier requests first'}", file=out, flush=True)
        from . import query as _query  # only a request that waits loads the views
        with contextlib.suppress(_config.ProcessTableError):  # a display must never cost the request its place
            for line in _query.status_lines(directory):
                print(f"  {line}", file=out, flush=True)
        if note and said[0] is None:
            print(f"slot: {note}", file=out, flush=True)
        said[0] = waiting

    try:
        current = wait_held(path, deadline=deadline, between=between, changed=changed, timeout_error=timeout_error)
        lease.held = True
        if isinstance(current.get("count"), int) and current["count"] >= 1:
            count = lease.count = current["count"]  # the capacity changed while it waited (plan())
        if said[0]:
            print(f"slot: granted {count} slot(s) to {label}", file=out, flush=True)
        return lease
    except BaseException as exc:
        if isinstance(exc, _config.SlotTimeout):
            why = "timeout"
        elif isinstance(exc, (KeyboardInterrupt, SystemExit)):
            why = "signal"
        else:
            why = "error"
        lease.release(left_reason=why)
        raise
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)


def terminate(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)
