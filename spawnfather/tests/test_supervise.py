"""Shared supervision seam (`supervise.py`): pure `StreakState`/`compute_wait`, `Timer.poll_until`,
`launch.start_detached`/`record_child` without real sleeps, and `supervise.run` loop mechanics."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple, cast
import unittest

from support import Case, as_entry, proc, table

from spawnfather import defaults, entry as entry_module, launch, procs, supervise
from spawnfather.entry import Entry
from spawnfather.state import Store


def make_entry(expires: float = 1030.0, **extra: Any) -> Entry:
    data: Dict[str, Any] = {
        "v": 1,
        "id": "jsup",
        "state": entry_module.RUNNING,
        "mode": "adopted",
        "pid": 500,
        "pgid": 500,
        "start": "Thu Nov 12 10:00:00 2026",
        "seen": "sleep 600",
        "group": True,
        "known": [[500, "Thu Nov 12 10:00:00 2026"]],
        "known_seen": [[500, "Thu Nov 12 10:00:00 2026", "sleep 600"]],
        "argv": ["sleep", "600"],
        "cwd": "/",
        "created": 1000.0,
        "ttl": max(0.0, expires - 1000.0),
        "expires": expires,
        "keep": "owner",
        "owner": {"kind": "claude", "session": "s1"},
        "supervisor": None,
        "log": None,
        **extra,
    }
    return as_entry(data)


class StreakAndWaitTests(unittest.TestCase):
    def test_retry_delay_doubles_and_caps_at_retry_max(self) -> None:
        got = [supervise.retry_delay(n) for n in range(1, 10)]
        self.assertEqual([1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0], got)
        self.assertEqual(defaults.RETRY_MAX, supervise.retry_delay(50))

    def test_compute_wait_table(self) -> None:
        cases: List[Tuple[str, Optional[Entry], float, float, supervise.StreakState, float, float]] = [
            ("ttl shorter than max_wait", make_entry(expires=1030.0), 1000.0, 0.0,
             supervise.StreakState(), 120.0, 30.0),
            ("ttl longer than max_wait", make_entry(expires=2000.0), 1000.0, 0.0,
             supervise.StreakState(), 120.0, 120.0),
            ("foreground poll caps ttl", make_entry(expires=1030.0), 1000.0, 0.0,
             supervise.StreakState(), defaults.POLL, defaults.POLL),
            ("no_ttl returns max_wait", make_entry(expires=900.0, no_ttl=True), 1000.0, 0.0,
             supervise.StreakState(), 120.0, 120.0),
            ("missing entry returns max_wait", None, 1000.0, 0.0,
             supervise.StreakState(), 120.0, 120.0),
            ("at expiry with clean streak", make_entry(expires=1010.0), 1010.0, 10.0,
             supervise.StreakState(), 120.0, 0.0),
            ("at expiry with backoff streak", make_entry(expires=1010.0), 1010.0, 10.0,
             supervise.StreakState(failures=3, retry_at=14.0, deadline=1010.0), 120.0, 4.0),
            ("backoff clamped to max_wait", make_entry(expires=1010.0), 1010.0, 10.0,
             supervise.StreakState(failures=5, retry_at=26.0, deadline=1010.0), defaults.POLL, defaults.POLL),
            ("shortened extend overrides stale retry_at", make_entry(expires=1015.0), 1010.0, 10.0,
             supervise.StreakState(failures=6, retry_at=42.0, deadline=1000.0), 120.0, 5.0),
            ("new already-due deadline clears stale retry_at", make_entry(expires=1005.0), 1010.0, 10.0,
             supervise.StreakState(failures=6, retry_at=42.0, deadline=1000.0), 120.0, 0.0),
            ("read_backoff prevents 0.0s spin at expiry", make_entry(expires=1010.0), 1010.0, 10.0,
             supervise.StreakState().record_read_failure(10.0), 120.0, defaults.READ_BACKOFF),
            ("read_backoff clamped to max_wait", make_entry(expires=1010.0), 1010.0, 10.0,
             supervise.StreakState().record_read_failure(10.0), defaults.POLL, defaults.POLL),
        ]
        for label, record, now, now_mono, streak, max_wait, expected in cases:
            with self.subTest(label):
                self.assertAlmostEqual(expected, supervise.compute_wait(record, now, now_mono, streak, max_wait))

    def test_streak_report_failure_resets_after_extend_or_skip(self) -> None:
        s0 = supervise.StreakState()
        self.assertTrue(s0.report_failure)
        s1 = s0.record_reap_failure(10.0, deadline=1000.0)
        self.assertFalse(s1.report_failure)
        self.assertEqual((1, 11.0), (s1.failures, s1.retry_at))
        s2 = s1.record_reap_failure(11.0, deadline=1000.0)
        self.assertFalse(s2.report_failure)
        self.assertEqual((2, 13.0), (s2.failures, s2.retry_at))

        # Reconciling against an extended entry resets the streak so the next failure reports again.
        extended = s2.reconcile(make_entry(expires=1050.0), now=1012.0)
        self.assertTrue(extended.report_failure)
        self.assertIsNone(extended.retry_at)

        # Clearing after a skipped reap also resets without dropping an active read backoff.
        with_read = s2.record_read_failure(12.0)
        cleared = with_read.clear_reap()
        self.assertTrue(cleared.report_failure)
        self.assertEqual(12.0 + defaults.READ_BACKOFF, cleared.read_retry_at)
        self.assertIsNone(cleared.clear_read().read_retry_at)


class FakeChild:
    """Minimal child-handle stub for `record_child` tests with zero OS processes."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.stopped = False

    def poll(self) -> Optional[int]:
        return 0 if self.stopped else None

    def wait(self, timeout: Optional[float] = None) -> int:
        self.stopped = True
        return 0


class TimerAndDetachedTests(Case):
    def fake_timer(self, start: float = 0.0) -> Tuple[supervise.Timer, List[float]]:
        now = [start]
        pauses: List[float] = []

        def pause(seconds: float) -> None:
            pauses.append(seconds)
            now[0] += seconds

        return supervise.Timer(clock=lambda: now[0], wall=lambda: 1000.0 + now[0], pause=pause), pauses

    def test_poll_until_returns_immediately_or_on_match_or_at_exact_timeout(self) -> None:
        timer, pauses = self.fake_timer()
        self.assertEqual("hit", timer.poll_until(lambda: "hit", timeout=5.0, interval=0.5))
        self.assertEqual([], pauses)

        tries = iter([None, None, "ready"])
        self.assertEqual("ready", timer.poll_until(lambda: next(tries), timeout=5.0, interval=0.5))
        self.assertEqual([0.5, 0.5], pauses)

        timer2, pauses2 = self.fake_timer()
        self.assertIsNone(timer2.poll_until(lambda: None, timeout=0.12, interval=0.05))
        self.assertAlmostEqual(0.12, sum(pauses2))
        self.assertEqual(3, len(pauses2))
        self.assertAlmostEqual(0.02, pauses2[-1])

    def test_start_detached_succeeds_fails_and_times_out_without_real_sleeps(self) -> None:
        # Success on 3rd poll
        entry = launch.new_entry(self.store, ["sleep", "600"], 60.0, "owner", None,
                                 {"kind": "process", "pid": None}, "detached", str(self.tmp))
        entry["log"] = str(self.tmp / "detached-ok.log")
        entry_module.save_starting(self.store, entry)
        timer, pauses = self.fake_timer()
        polls = [0]

        def pause_and_start(seconds: float) -> None:
            timer.pause(seconds)
            polls[0] += 1
            if polls[0] == 2:
                self.store.update(entry["id"], lambda e: e.update(state="running", pid=4242, pgid=4242))

        t_ok = supervise.Timer(clock=timer.clock, wall=timer.wall, pause=pause_and_start)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = launch.start_detached(self.store, entry, quiet=False, timer=t_ok, starter=lambda _e, _p: None)
        self.assertEqual(0, code)
        self.assertEqual([0.05, 0.05], pauses)
        self.assertIn(f"{entry['id']} owns pid 4242", out.getvalue())

        # Failure reported by supervisor
        failed = launch.new_entry(self.store, ["missing"], 60.0, "owner", None,
                                  {"kind": "process", "pid": None}, "detached", str(self.tmp))
        failed["log"] = str(self.tmp / "detached-fail.log")
        entry_module.save_starting(self.store, failed)
        entry_module.failed(self.store, failed["id"], "no such file", start_error=True)
        t_fail, pauses_fail = self.fake_timer()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = launch.start_detached(self.store, failed, quiet=True, timer=t_fail, starter=lambda _e, _p: None)
        self.assertEqual(127, code)
        self.assertEqual([], pauses_fail)
        self.assertIn("no such file", err.getvalue())

        # Timeout after full DETACH_WAIT (15 s simulated in 0 ms real time)
        stuck = launch.new_entry(self.store, ["sleep", "600"], 60.0, "owner", None,
                                 {"kind": "process", "pid": None}, "detached", str(self.tmp))
        stuck["log"] = str(self.tmp / "detached-timeout.log")
        entry_module.save_starting(self.store, stuck)
        t_timeout, pauses_timeout = self.fake_timer()
        err_timeout = io.StringIO()
        with contextlib.redirect_stderr(err_timeout):
            code = launch.start_detached(self.store, stuck, quiet=True, timer=t_timeout, starter=lambda _e, _p: None)
        self.assertEqual(1, code)
        self.assertAlmostEqual(defaults.DETACH_WAIT, sum(pauses_timeout))
        self.assertIn(f"in {defaults.DETACH_WAIT:.0f} s", err_timeout.getvalue())

    def test_record_child_retries_transient_proc_error_through_injected_timer(self) -> None:
        record = launch.new_entry(self.store, ["sleep", "600"], 60.0, "owner", None,
                                  {"kind": "process", "pid": None}, "attached", str(self.tmp))
        entry_module.save_starting(self.store, record)
        timer, pauses = self.fake_timer()
        reads = [0]
        child_proc = proc(777, ppid=os.getpid(), pgid=777, cmd="sleep 600")
        me_proc = proc(os.getpid(), ppid=1, pgid=os.getpid(), cmd="python3")

        def flaky_reader() -> procs.Table:
            reads[0] += 1
            if reads[0] < 3:
                raise procs.ProcError("ps busy")
            return table(child_proc, me_proc)

        stub = cast(subprocess.Popen[bytes], FakeChild(777))
        recorded = launch.record_child(self.store, record["id"], stub, os.getpid(), timer=timer, reader=flaky_reader)
        self.assertIsNotNone(recorded)
        self.assertEqual([0.2, 0.4], pauses)
        self.assertEqual("running", self.state_of(record["id"]))


class FakeDriver:
    """In-memory `supervise.Driver[str]` for testing `supervise.run` without processes or sleeps."""

    def __init__(self, *, max_wait: float = defaults.WAKE, wait_first: bool = True,
                 holds: bool = False,
                 on_wait: Optional[Callable[[float], None]] = None,
                 on_refresh: Optional[Callable[[Store, Entry, float], Optional[str]]] = None) -> None:
        self._max_wait = max_wait
        self._wait_first = wait_first
        self._holds = holds
        self._on_wait = on_wait
        self._on_refresh = on_refresh
        self.waits: List[float] = []
        self.notices: List[str] = []
        self.external_states: List[Optional[str]] = []

    @property
    def max_wait(self) -> float:
        return self._max_wait

    @property
    def wait_first(self) -> bool:
        return self._wait_first

    def check_interrupt(self, store: Store, entry_id: str) -> Optional[str]:
        return None

    def holds_leader(self) -> bool:
        return self._holds

    def wait(self, timeout: float) -> None:
        self.waits.append(timeout)
        if self._on_wait is not None:
            self._on_wait(timeout)

    def refresh(self, store: Store, entry: Entry, seen_at: float) -> Optional[str]:
        if self._on_refresh is not None:
            return self._on_refresh(store, entry, seen_at)
        return None

    def finish_external(self, store: Store, entry: Optional[Entry]) -> str:
        state = str(entry.get("state")) if entry is not None else None
        self.external_states.append(state)
        return f"external:{state}"

    def finish_ttl(self, store: Store, entry_id: str, result: Dict[str, Any]) -> str:
        self.notices.append(f"{entry_id} reached its TTL: {result['result']}")
        return f"ttl:{result['result']}"

    def notice(self, message: str) -> None:
        self.notices.append(message)


class SuperviseRunTests(Case):
    def clocks(self, wall_start: float = 1000.0, mono_start: float = 0.0
               ) -> Tuple[List[float], List[float], List[float], supervise.Timer]:
        wall = [wall_start]
        mono = [mono_start]
        pauses: List[float] = []

        def pause(seconds: float) -> None:
            pauses.append(seconds)
            wall[0] += seconds
            mono[0] += seconds

        timer = supervise.Timer(clock=lambda: mono[0], wall=lambda: wall[0], pause=pause)
        return wall, mono, pauses, timer

    def test_ttl_reap_uses_wall_clock_and_require_due_guard(self) -> None:
        wall, mono, _, timer = self.clocks(1000.0, 0.0)
        self.store.save(make_entry(expires=1030.0))
        seen_guards: List[Tuple[Optional[str], Optional[str]]] = []

        def reaper(store: Store, ident: str, reason: str, **options: Any) -> Dict[str, Any]:
            guard = options["allowed"]
            seen_guards.append((
                entry_module.guard_why(guard, make_entry(expires=1030.0), reason),
                entry_module.guard_why(guard, make_entry(expires=1050.0), reason),
            ))
            store.update(ident, lambda e: e.update(state="reaped"))
            return {"id": ident, "result": "stopped", "reason": reason, "pids": [500], "how": "leader"}

        def advance(dt: float) -> None:
            wall[0] += dt
            mono[0] += dt

        driver = FakeDriver(on_wait=advance)
        outcome = supervise.run(self.store, "jsup", driver, timer=timer, reaper=reaper)
        self.assertEqual("ttl:stopped", outcome)
        self.assertEqual([30.0], driver.waits)
        self.assertEqual([(None, "not expired")], seen_guards)

    def test_backoff_report_failure_extend_reset_and_shortened_override(self) -> None:
        wall, mono, _, timer = self.clocks(1000.0, 0.0)
        self.store.save(make_entry(expires=1010.0))
        attempts: List[Tuple[float, bool]] = []

        def on_wait(dt: float) -> None:
            wall[0] += dt
            mono[0] += dt

        def reaper_with_extend(store: Store, ident: str, reason: str, **options: Any) -> Dict[str, Any]:
            attempts.append((mono[0], bool(options["report_failure"])))
            if len(attempts) <= 3:
                return {"id": ident, "result": "failed", "reason": "ps failed", "pids": [], "how": ""}
            if len(attempts) == 4:
                # 4th failure sets retry_at = mono + 8s; meanwhile user extends TTL to 3s from now!
                entry_module.extended(store, ident, 3.0, now=wall[0])
                return {"id": ident, "result": "failed", "reason": "ps failed", "pids": [], "how": ""}
            if len(attempts) == 5:
                return {"id": ident, "result": "failed", "reason": "ps failed again", "pids": [], "how": ""}
            store.update(ident, lambda e: e.update(state="reaped"))
            return {"id": ident, "result": "killed", "reason": reason, "pids": [500], "how": "leader"}

        driver = FakeDriver(on_wait=on_wait)
        outcome = supervise.run(self.store, "jsup", driver, timer=timer, reaper=reaper_with_extend)
        self.assertEqual("ttl:killed", outcome)
        # Waits: 10s (TTL), 1s (fail 1), 2s (fail 2), 4s (fail 3),
        # 3s (shortened extend beats 8s!), 1s (fail 1 of streak 2).
        self.assertEqual([10.0, 1.0, 2.0, 4.0, 3.0, 1.0], driver.waits)
        # report_failure: True on 1st failure, False on 2,3,4, True again after extend reset, False on retry!
        self.assertEqual([True, False, False, False, True, False], [rep for _, rep in attempts])
        failure_notices = [n for n in driver.notices if "trying again with a backoff" in n]
        self.assertEqual(2, len(failure_notices), "one failure notice per streak")

    def test_skipped_reap_clears_streak_without_notice(self) -> None:
        wall, mono, _, timer = self.clocks(1000.0, 0.0)
        self.store.save(make_entry(expires=1010.0))
        calls: List[bool] = []

        def reaper(store: Store, ident: str, reason: str, **options: Any) -> Dict[str, Any]:
            calls.append(bool(options["report_failure"]))
            if len(calls) == 1:
                return {"id": ident, "result": "failed", "reason": "ps failed", "pids": [], "how": ""}
            if len(calls) == 2:
                # Concurrent extend wins under lock -> reap returns skipped.
                entry_module.extended(store, ident, 5.0, now=wall[0])
                return {"id": ident, "result": "skipped", "reason": "not expired", "pids": [], "how": ""}
            if len(calls) == 3:
                return {"id": ident, "result": "failed", "reason": "ps failed", "pids": [], "how": ""}
            store.update(ident, lambda e: e.update(state="reaped"))
            return {"id": ident, "result": "stopped", "reason": reason, "pids": [500], "how": "leader"}

        def on_wait(dt: float) -> None:
            wall[0] += dt
            mono[0] += dt

        driver = FakeDriver(on_wait=on_wait)
        outcome = supervise.run(self.store, "jsup", driver, timer=timer, reaper=reaper)
        self.assertEqual("ttl:stopped", outcome)
        self.assertEqual([10.0, 1.0, 5.0, 1.0], driver.waits)
        self.assertEqual([True, False, True, False], calls)
        self.assertFalse(any("skipped" in n for n in driver.notices))

    def test_proc_error_at_expiry_waits_read_backoff_instead_of_zero(self) -> None:
        wall, mono, _, timer = self.clocks(1000.0, 0.0)
        self.store.save(make_entry(expires=1010.0))
        refreshes = [0]

        def on_wait(dt: float) -> None:
            wall[0] += dt
            mono[0] += dt

        def on_refresh(_store: Store, _entry: Entry, _seen_at: float) -> Optional[str]:
            refreshes[0] += 1
            if refreshes[0] == 1:
                raise procs.ProcError("ps failed")
            return None

        def reaper(store: Store, ident: str, reason: str, **options: Any) -> Dict[str, Any]:
            store.update(ident, lambda e: e.update(state="reaped"))
            return {"id": ident, "result": "stopped", "reason": reason, "pids": [500], "how": "leader"}

        driver = FakeDriver(on_wait=on_wait, on_refresh=on_refresh)
        self.assertEqual("ttl:stopped", supervise.run(self.store, "jsup", driver, timer=timer, reaper=reaper))
        self.assertEqual([10.0, defaults.READ_BACKOFF], driver.waits)

    def test_concurrent_reaping_waits_and_external_reaped_notifies_and_finishes(self) -> None:
        wall, mono, pauses, _ = self.clocks(1000.0, 0.0)
        self.store.save(make_entry(expires=2000.0, state=entry_module.REAPING))
        ticks = [0]

        def pause_and_finish_reap(seconds: float) -> None:
            pauses.append(seconds)
            wall[0] += seconds
            mono[0] += seconds
            ticks[0] += 1
            if ticks[0] == 2:
                self.store.update("jsup", lambda e: e.update(
                    state=entry_module.REAPED, end={"reason": "session-end", "result": "stopped"}
                ))

        t = supervise.Timer(clock=lambda: mono[0], wall=lambda: wall[0], pause=pause_and_finish_reap)
        driver = FakeDriver(wait_first=False, max_wait=defaults.POLL)
        outcome = supervise.run(self.store, "jsup", driver, timer=t)
        self.assertEqual("external:reaped", outcome)
        self.assertEqual([defaults.POLL, defaults.POLL], pauses)
        self.assertEqual(["reaped"], driver.external_states)
        self.assertEqual(["jsup reaped (session-end)"], driver.notices)

    def test_gone_revival_only_when_driver_holds_leader(self) -> None:
        # Driver that holds leader revives `gone` -> `running`
        _, _, _, timer = self.clocks(1000.0, 0.0)
        self.store.save(make_entry(expires=2000.0, state=entry_module.GONE, ended=999.0))
        saw_states: List[str] = []

        def on_refresh(_store: Store, current: Entry, _seen_at: float) -> Optional[str]:
            saw_states.append(str(current["state"]))
            return "done"

        holder = FakeDriver(wait_first=False, holds=True, on_refresh=on_refresh)
        self.assertEqual("done", supervise.run(self.store, "jsup", holder, timer=timer))
        self.assertEqual(["running"], saw_states)
        self.assertEqual("running", self.state_of("jsup"))

        # Driver that does NOT hold leader finishes externally without reviving
        self.store.update("jsup", lambda e: e.update(state=entry_module.GONE, ended=999.0))
        non_holder = FakeDriver(wait_first=False, holds=False)
        self.assertEqual("external:gone", supervise.run(self.store, "jsup", non_holder, timer=timer))
        self.assertEqual("gone", self.state_of("jsup"))


if __name__ == "__main__":
    unittest.main()
