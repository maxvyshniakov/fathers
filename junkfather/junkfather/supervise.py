"""Shared per-entry supervision policy and loop (fathers#226).

Both `launch.Supervisor` (attached/detached `own`) and `background.watch` (auto-owned background Bash
shells) supervise one running entry until its processes exit, an external reap ends it, or its TTL
expires. This module holds the shared mechanics once:

- `StreakState` + `compute_wait`: pure reducer for TTL horizon, `no_ttl` -> `max_wait`, capped
  exponential backoff (`1s, 2s, 4s ... 60s`), single failure reporting per streak (reset on `extend`
  or `skipped`), shortened `extend` overriding stale `retry_at`, and `READ_BACKOFF` preventing
  `0.0 s` spin-waits at expiry when `ps` fails (`ProcError`).
- `Timer`: injectable `clock`, `wall`, `pause` and `poll_until` for supervision and startup waits.
- `run`: the shared loop handling interrupts, concurrent `reaping`, `gone` revival when the driver
  holds the live leader, external end handling, liveness refresh, and TTL reaping with
  `allowed=_entry.require_due(wall)`.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Dict, Optional, Protocol, Tuple, TypeVar

from . import entry as _entry
from . import events as _events
from . import procs as _procs
from . import reap as _reap
from .defaults import POLL, READ_BACKOFF, RETRY_FIRST, RETRY_MAX
from .entry import Entry
from .state import Store

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
Reaper = Callable[..., Dict[str, Any]]


def retry_delay(failures: int) -> float:
    """How long the supervisor waits after the `failures`-th failed TTL reap in a row."""
    return min(RETRY_MAX, RETRY_FIRST * 2 ** min(max(failures - 1, 0), 16))


@dataclass(frozen=True)
class StreakState:
    """Pure state for a supervision loop's TTL-reap failure streak and process-table read backoff."""

    failures: int = 0
    retry_at: Optional[float] = None
    deadline: Optional[float] = None
    read_retry_at: Optional[float] = None

    @property
    def report_failure(self) -> bool:
        """True only for the first failure of a streak; retries stay quiet until `extend` or `skipped` resets it."""
        return self.failures == 0

    def ready_to_reap(self, now_mono: float) -> bool:
        """Whether backoff permits a TTL reap attempt at `now_mono`."""
        return self.retry_at is None or now_mono >= self.retry_at

    def reconcile(self, entry: Optional[Entry], now: float) -> StreakState:
        """Clear a reap-failure streak when `entry` has no TTL, is not due, or moved to a different deadline."""
        if self.failures == 0 and self.retry_at is None and self.deadline is None:
            return self
        if entry is None or entry.get("no_ttl") or not _entry.due(entry, now):
            return StreakState(read_retry_at=self.read_retry_at)
        expires = float(entry.get("expires") or 0)
        if self.deadline is not None and expires != self.deadline:
            return StreakState(read_retry_at=self.read_retry_at)
        return self

    def record_reap_failure(self, now_mono: float, deadline: Optional[float] = None) -> StreakState:
        """Advance the failure streak and compute the next monotonic retry timestamp."""
        failures = self.failures + 1
        return StreakState(
            failures=failures,
            retry_at=now_mono + retry_delay(failures),
            deadline=deadline,
            read_retry_at=self.read_retry_at,
        )

    def clear_reap(self) -> StreakState:
        """Reset the reap-failure streak while keeping any active process-table read backoff."""
        if self.failures == 0 and self.retry_at is None and self.deadline is None:
            return self
        return StreakState(read_retry_at=self.read_retry_at)

    def record_read_failure(self, now_mono: float) -> StreakState:
        """Back off by `READ_BACKOFF` after a `ProcError` reading the process table."""
        return StreakState(
            failures=self.failures,
            retry_at=self.retry_at,
            deadline=self.deadline,
            read_retry_at=now_mono + READ_BACKOFF,
        )

    def clear_read(self) -> StreakState:
        """Clear process-table read backoff after a successful refresh."""
        if self.read_retry_at is None:
            return self
        return StreakState(failures=self.failures, retry_at=self.retry_at, deadline=self.deadline)


def compute_wait(entry: Optional[Entry], now: float, now_mono: float, streak: StreakState, max_wait: float) -> float:
    """How long the supervision loop waits before its next look: at most `max_wait`, shortened to the entry's
    finite TTL horizon (`no_ttl` -> `max_wait`), extended by reap-failure backoff while still due on the same
    deadline, and bounded by `read_retry_at` after a `ProcError` so expiry never spin-waits at 0.0 s."""
    active = streak.reconcile(entry, now)
    if active.read_retry_at is not None:
        return min(max_wait, max(0.0, active.read_retry_at - now_mono))
    wait = max_wait
    if entry is not None and not entry.get("no_ttl"):
        wait = min(wait, max(0.0, float(entry.get("expires") or 0) - now))
        if _entry.due(entry, now) and active.retry_at is not None:
            wait = max(wait, 0.0, active.retry_at - now_mono)
    return min(max_wait, max(0.0, wait))


@dataclass(frozen=True)
class Timer:
    """Injectable clocks and pause for supervision and startup wait loops."""

    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    pause: Callable[[float], None] = time.sleep

    def poll_until(self, check: Callable[[], Optional[T]], timeout: float, interval: float) -> Optional[T]:
        """Call `check` until it returns a non-None value or `timeout` monotonic seconds pass."""
        deadline = self.clock() + timeout
        while True:
            result = check()
            if result is not None:
                return result
            remaining = deadline - self.clock()
            if remaining <= 0:
                return None
            self.pause(min(interval, remaining))


class Driver(Protocol[T_co]):
    """Per-mode hooks for `supervise.run` (`launch.ChildDriver` and `background.BackgroundDriver`)."""

    @property
    def max_wait(self) -> float: ...

    @property
    def wait_first(self) -> bool: ...

    def check_interrupt(self, store: Store, entry_id: str) -> Optional[T_co]:
        """Handle an external interrupt (e.g. SIGTERM/SIGINT/SIGHUP); return a value to stop, else None."""
        ...

    def holds_leader(self) -> bool:
        """Whether this driver still holds the live unwaited leader (eligible to revive a `gone` entry)."""
        ...

    def wait(self, timeout: float) -> None:
        """Wait up to `timeout` seconds for events or the next tick."""
        ...

    def refresh(self, store: Store, entry: Entry, seen_at: float) -> Optional[T_co]:
        """Refresh liveness/known members. Raise `procs.ProcError` on unreadable process table.
        Return a value to end supervision, or None to keep supervising."""
        ...

    def finish_external(self, store: Store, entry: Optional[Entry]) -> T_co:
        """The entry disappeared or moved to a non-running state (`reaped`, `failed`, `gone`, `exited`)."""
        ...

    def finish_ttl(self, store: Store, entry_id: str, result: Dict[str, Any]) -> T_co:
        """The entry's TTL reap succeeded (`result['result'] in reap.STOPPED`)."""
        ...

    def notice(self, message: str) -> None:
        """Emit a user-facing notice on stderr (no-op when quiet or detached background watcher)."""
        ...


def _wait_reaping(store: Store, entry_id: str, entry: Optional[Entry], driver: Driver[T], timer: Timer
                  ) -> Tuple[Optional[Entry], Optional[T]]:
    """Wait out a concurrent reaper (`state == reaping`), still checking driver interrupts."""
    while entry is not None and entry.get("state") == _entry.REAPING:
        interrupted = driver.check_interrupt(store, entry_id)
        if interrupted is not None:
            return entry, interrupted
        timer.pause(POLL)
        entry = store.load(entry_id)
    return entry, None


def _finish_external(store: Store, entry_id: str, entry: Optional[Entry], driver: Driver[T]) -> T:
    """Complete supervision after an external state change, notifying on `reaped` after cleanup succeeds."""
    result = driver.finish_external(store, entry)
    if entry is not None and entry.get("state") == _entry.REAPED:
        driver.notice(f"{entry_id} reaped ({(entry.get('end') or {}).get('reason')})")
    return result


def run(store: Store, entry_id: str, driver: Driver[T], *,
        timer: Optional[Timer] = None,
        reaper: Optional[Reaper] = None,
        reader: Optional[Callable[[], _procs.Table]] = None) -> T:
    """Run the shared per-entry supervision loop for `driver`."""
    timer = timer or Timer()
    reap_entry = reaper or _reap.reap
    streak = StreakState()
    first = True

    while True:
        interrupted = driver.check_interrupt(store, entry_id)
        if interrupted is not None:
            return interrupted

        if not first or driver.wait_first:
            current = store.load(entry_id)
            wait = compute_wait(current, timer.wall(), timer.clock(), streak, driver.max_wait)
            driver.wait(wait)
            interrupted = driver.check_interrupt(store, entry_id)
            if interrupted is not None:
                return interrupted
        first = False

        seen_at = timer.wall()
        entry, interrupted = _wait_reaping(store, entry_id, store.load(entry_id), driver, timer)
        if interrupted is not None:
            return interrupted

        if entry is not None and entry.get("state") == _entry.GONE and driver.holds_leader():
            # A sweep took it for ended while our unwaited child still runs: it is still ours.
            entry = _entry.revived(store, entry_id) or store.load(entry_id)

        if entry is None or entry.get("state") != _entry.RUNNING:
            try:
                return _finish_external(store, entry_id, entry, driver)
            except _procs.ProcError:
                streak = streak.record_read_failure(timer.clock())
                continue

        try:
            finished = driver.refresh(store, entry, seen_at)
        except _procs.ProcError:
            streak = streak.record_read_failure(timer.clock())
            continue
        streak = streak.clear_read()
        if finished is not None:
            return finished

        entry, interrupted = _wait_reaping(store, entry_id, store.load(entry_id), driver, timer)
        if interrupted is not None:
            return interrupted
        if entry is None or entry.get("state") != _entry.RUNNING:
            try:
                return _finish_external(store, entry_id, entry, driver)
            except _procs.ProcError:
                streak = streak.record_read_failure(timer.clock())
                continue

        now = timer.wall()
        streak = streak.reconcile(entry, now)
        if not _entry.due(entry, now) or not streak.ready_to_reap(timer.clock()):
            continue

        reap_kwargs: Dict[str, Any] = {
            "actor": _events.tool("supervisor"),
            "report_failure": streak.report_failure,
            "allowed": _entry.require_due(timer.wall),
        }
        if reader is not None:
            reap_kwargs["reader"] = reader
        result = reap_entry(store, entry_id, "ttl expired", **reap_kwargs)
        outcome = result.get("result")
        if outcome in _reap.STOPPED:
            return driver.finish_ttl(store, entry_id, result)
        if outcome == "failed":
            first_failure = streak.report_failure
            streak = streak.record_reap_failure(timer.clock(), float(entry.get("expires") or 0))
            if first_failure:
                driver.notice(
                    f"{entry_id} reached its TTL: failed ({result.get('reason')}); "
                    f"trying again with a backoff up to {RETRY_MAX:.0f} s"
                )
            continue

        # `skipped` (e.g. concurrent `extend` won under lock or another reaper claimed it) or `unknown`:
        # clear any failure streak without emitting a TTL-reached notice.
        streak = streak.clear_reap()
        after, interrupted = _wait_reaping(store, entry_id, store.load(entry_id), driver, timer)
        if interrupted is not None:
            return interrupted
        if after is None or after.get("state") != _entry.RUNNING:
            try:
                return _finish_external(store, entry_id, after, driver)
            except _procs.ProcError:
                streak = streak.record_read_failure(timer.clock())
                continue
