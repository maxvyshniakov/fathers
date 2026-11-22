#!/usr/bin/env python3
"""Measurement sessions (fathers#96): `session start` drains the queue once and holds it for a series of
`run --session NAME` commands, until `session stop`, an idle timeout or its holder's death releases it."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
import eventcheck  # common/eventcheck.py, the shared event validator

import contextlib
import fcntl
import io
import json
import os
import signal
import subprocess
import time
import unittest
from typing import Any
from unittest import mock

from test_slotfather import TOOL, WORK, SlotCase

from slotfather import client as sf_client
from slotfather import config as sf_config
from slotfather import entry as sf_entry
from slotfather import leases as sf_leases
from slotfather import lockwait as sf_lockwait
from slotfather import state as sf_state
from slotfather import process as sf_process


class SessionTests(SlotCase):
    processes: list[Any]
    def spawn(self, argv: list[str]) -> subprocess.Popen[str]:
        process = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True)
        self.processes.append(process)
        return process

    def start(self, name: str, *options: str, background: bool = False) -> Any:
        argv = [TOOL, "session", "start", "--label", name, *options]
        if background:
            return self.spawn(argv)
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.addCleanup(self.kill_sessions)
        return result

    def kill_sessions(self) -> None:
        for entry in self.entries():
            if entry.get("session"):
                try:
                    os.kill(entry["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def entries(self) -> list[dict[str, Any]]:
        return json.loads(self.tool("status", "--json").stdout)["entries"]

    def session(self, name: str) -> Any:
        found: Any = None
        for entry in self.entries():
            spec: Any = entry.get("session") or {}
            if spec.get("name") == name:
                found = entry
        return found

    def inside(self, name: str, label: str, *command: str, background: bool = False) -> Any:
        argv = [TOOL, "run", "--session", name, "--", *(command or ("sh", "-c", WORK, "work", label))]
        if background:
            return self.spawn(argv)
        return subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=20)

    def history(self) -> list[Any]:
        path = self.state / "history.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def events_of(self) -> list[Any]:
        path = self.state / "events.jsonl"
        self.assertEqual([], eventcheck.validate_file(str(path), strict=True))
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_start_acknowledges_a_grant_without_another_process_table(self) -> None:
        tools = self.tmp / "tools"
        tools.mkdir()
        ps = tools / "ps"
        ps.write_text('''#!/bin/sh
if [ "$PPID" = "$SESSION_STARTER" ] && [ "$1" = -axo ] && [ "$2" = pid=,ppid=,stat=,lstart= ]; then
    if [ -f "$PS_COUNT.second" ]; then
        echo 'fixture: process inspection became unavailable' >&2
        exit 9
    fi
    if [ -f "$PS_COUNT.first" ]; then touch "$PS_COUNT.second"; else touch "$PS_COUNT.first"; fi
fi
exec /bin/ps "$@"
''')
        ps.chmod(0o755)
        self.addCleanup(self.kill_sessions)
        result = subprocess.run(["sh", "-c", 'export SESSION_STARTER=$$; exec "$@"', "starter", TOOL,
                                 "session", "start", "--label", "perf", "-n", "2"],
                                env={**self.env, "PATH": f"{tools}:{self.env['PATH']}",
                                     "PS_COUNT": str(self.tmp / "starter-ps")}, capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(self.session("perf").get("count"), 2)
        self.assertTrue((self.tmp / "starter-ps.second").exists())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("session perf holds 2 slot(s)", result.stdout)

    def test_a_session_drains_once_holds_between_runs_and_stop_releases_it(self):
        early = self.request("early", 1)
        self.started("early")
        starting = self.start("perf", "--idle", "60", background=True)
        time.sleep(0.4)
        self.assertIsNone(starting.poll(), "the session waits for the running lease, as -n all does")
        self.release("early")
        self.finish(early)
        output = self.finish(starting)
        self.assertIn("session perf holds 4 slot(s)", output)
        self.addCleanup(self.kill_sessions)
        held = self.session("perf")
        self.assertEqual(("held", 4, "perf"), (held["state"], held["count"], held["label"]))
        self.assertEqual([held["id"]], [e["id"] for e in self.entries()], "the session is one holder")

        for label in ("m1", "m2"):
            self.release(label)
            result = subprocess.run(
                [TOOL, "run", "--session", "perf", "--meta", "cache=hit", "--", "sh", "-c", WORK, "work", label],
                env=self.env, capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(["start early", "end early", "start m1", "end m1", "start m2", "end m2"], self.events())

        late = self.request("late", 1)
        time.sleep(0.4)
        self.assertNotIn("start late", self.events(), "between runs the session keeps the machine")
        self.assertEqual(2, self.session("perf")["session"]["runs"])
        stop = self.tool("session", "stop", "perf")
        self.assertEqual(0, stop.returncode, stop.stderr)
        self.started("late")
        self.release("late")
        self.finish(late)

        [line] = [h for h in self.history() if h["label"] == "perf"]
        self.assertEqual(("released", 4), (line["outcome"], line["count"]))
        self.assertEqual(("perf", 2, "stopped"), (line["session"]["name"], line["session"]["runs"],
                                                  line["session"]["ended_by"]))
        self.assertEqual(["early", "perf", "late"], [h["label"] for h in self.history()],
                         "runs inside the session add no lease lines of their own")
        kinds = [e["kind"] for e in self.events_of()]
        self.assertEqual(2, kinds.count("session.run_started"))
        self.assertEqual(2, kinds.count("session.run_ended"))
        ended = [e for e in self.events_of() if e["kind"] == "session.run_ended"]
        self.assertEqual({"cache": "hit"}, ended[0]["details"]["run"]["meta"])
        self.assertEqual(1, kinds.count("session.ended"))
        self.assertEqual([], list((self.state / "queue").glob("*.session")), "nothing is left beside the queue")

    def test_runs_inside_see_the_session_lease_and_nested_runs_take_no_second_lease(self):
        self.start("perf", "--idle", "60")
        lease = self.session("perf")["lease"]
        result = self.inside("perf", "x", "sh", "-c",
                             f'echo "$SLOTFATHER_LEASE $SLOTFATHER_COUNT"; "{TOOL}" run -n 1 --wait 2 -- true')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(f"{lease} 4", result.stdout.splitlines()[0])
        self.assertEqual(0, self.inside("perf", "x", "sh", "-c", "exit 3").returncode - 3, "its exit status")

    def lock_holder(self, lock: str) -> subprocess.Popen[str]:
        holder = self.spawn([TOOL, "lock", lock, "--", "sh", "-c", WORK, "work", "holder"])
        self.started("holder")
        return holder

    def lock_waiter(self, lock: str, label: str = "locked") -> subprocess.Popen[str]:
        waiting = self.inside("perf", label, TOOL, "lock", lock, "--", "sh", "-c", WORK, "work", label,
                              background=True)
        def marked() -> bool:
            current: dict[str, Any] = self.session("perf") or {}
            return current.get("lock") == lock

        self.wait_for(marked, "the session to wait on its lock")
        return waiting

    def wait_for_lock_end(self, why: str) -> None:
        self.wait_for(
            lambda: any(event["kind"] == "lease.lock_ended" and event["details"]["why"] == why
                        for event in self.events_of()), f"the lock wait to end with {why}")

    def test_new_session_work_resumes_a_lease_yielded_for_a_lock(self) -> None:
        lock = str(self.tmp / "cache.lock")
        holder = self.lock_holder(lock)
        self.start("perf", "--idle", "60")
        waiting = self.lock_waiter(lock)
        self.assertEqual("yielded", self.session("perf")["phase"])
        other = self.request("other", 1)
        self.started("other")

        arriving = self.inside("perf", "arriving", background=True)
        self.wait_for_lock_end("resumed")
        self.assertEqual("waiting", self.session("perf")["state"], "the other holder still has a slot")
        self.assertNotIn("start arriving", self.events())
        self.release("other")
        self.started("arriving")
        lease = self.session("perf")
        self.assertEqual("held", lease["state"])
        self.assertNotIn("lock", lease)
        self.assertNotIn("start locked", self.events(), "the original command still waits for the external lock")
        for label in ("arriving", "holder", "locked"):
            self.release(label)
        for process in (arriving, other, holder, waiting):
            self.finish(process)

    def record_run(self, pid: int | None = None, **identity: Any) -> None:
        lease_path = _Path(self.session("perf")["lease"])
        with mock.patch.dict(os.environ, self.env, clear=True), sf_state.editing(lease_path):
            uses = sf_state.session_uses(lease_path)
            assert uses is not None
            uses["active"][str(os.getpid() if pid is None else pid)] = identity
            sf_state.write(sf_state.session_path(lease_path), uses)

    def test_session_lock_yields_past_a_stale_run_and_waits_for_its_slots_back(self) -> None:
        lock = str(self.tmp / "cache.lock")
        holder = self.lock_holder(lock)
        self.start("perf", "--idle", "60")
        # The PID is live and the stored identity belongs to a previous process.
        self.record_run(start="Sat Jan  1 00:00:00 2000", started=946684800)
        waiting = self.lock_waiter(lock)
        self.assertEqual("yielded", self.session("perf")["phase"])
        other = self.request("other", 1)
        self.started("other")

        self.release("holder")
        self.wait_for_lock_end("took")
        self.assertEqual("waiting", self.session("perf")["state"])
        self.assertNotIn("start locked", self.events(), "taking the lock alone must not start the command")
        self.release("other")
        self.started("locked")
        self.assertEqual("held", self.session("perf")["state"])
        self.release("locked")
        for process in (holder, other, waiting):
            self.finish(process)

    def test_session_lock_keeps_slots_for_a_live_sibling_run(self) -> None:
        lock = str(self.tmp / "cache.lock")
        holder = self.lock_holder(lock)
        self.start("perf", "--idle", "60")
        busy = self.inside("perf", "busy", background=True)
        self.started("busy")
        waiting = self.lock_waiter(lock)

        lease = self.session("perf")
        self.assertEqual("held", lease["phase"])
        self.assertEqual(2, len(lease["session"]["active"]))
        self.assertFalse(any(event["kind"] == "lease.yielded" for event in self.events_of()))
        other = self.request("other", 1)
        self.release("busy")
        self.finish(busy)
        self.started("other")  # the external holder deliberately still owns the flock
        self.assertEqual("yielded", self.session("perf")["phase"])
        self.release("holder")
        self.wait_for_lock_end("took")
        self.assertNotIn("start locked", self.events())
        self.release("other")
        self.started("locked")
        self.assertEqual("held", self.session("perf")["state"])
        self.release("locked")
        for process in (holder, waiting, other):
            self.finish(process)

    def test_session_lock_keeps_slots_when_an_active_identity_is_unknown(self) -> None:
        lock = str(self.tmp / "cache.lock")
        holder = self.lock_holder(lock)
        self.start("perf", "--idle", "60")
        self.record_run(start="unparseable identity", started=None)
        waiting = self.lock_waiter(lock)

        self.assertEqual("held", self.session("perf")["phase"])
        self.release("holder")
        self.started("locked")
        self.release("locked")
        self.finish(holder)
        self.finish(waiting)

    def test_zombie_run_does_not_pin_a_session_lock_wait(self) -> None:
        lock = str(self.tmp / "cache.lock")
        holder = self.lock_holder(lock)
        self.start("perf", "--idle", "60")
        zombie = self.spawn(["true"])
        self.wait_for(lambda: sf_process.starts([zombie.pid]).get(zombie.pid, ("", False))[1],
                      "the exited child to remain an unreaped zombie")
        self.record_run(zombie.pid, start=sf_process.starts([zombie.pid])[zombie.pid][0])
        waiting = self.lock_waiter(lock)
        self.assertEqual("yielded", self.session("perf")["phase"])
        self.release("holder")
        self.started("locked")
        self.release("locked")
        for process in (holder, waiting, zombie):
            self.finish(process)

    def test_background_lock_does_not_yield_for_its_shell(self) -> None:
        lock = str(self.tmp / "cache.lock")
        holder = self.lock_holder(lock)
        self.start("perf", "--idle", "60")
        pipe = self.tmp / "shell-input"
        os.mkfifo(pipe)
        command = '''"$1" lock "$2" -- true &
reader=$!
exec 3< "$3"
echo 'start shell' >> "$LOG"
read -r ignored <&3
echo 'end shell' >> "$LOG"
wait "$reader"'''
        # Opening both ends lets the shell block in read without a helper process.
        handle = os.open(pipe, os.O_RDWR)
        self.addCleanup(os.close, handle)
        process = self.inside("perf", "shell", "sh", "-c", command, "shell", TOOL, lock, str(pipe), background=True)
        self.started("shell")
        self.wait_for(lambda: self.session("perf").get("lock") == lock, "the background lock to be noted")
        self.assertEqual("held", self.session("perf")["phase"])
        os.write(handle, b"work\n")
        self.wait_for(lambda: "end shell" in self.events(), "the shell to keep running while its lock waits")
        self.release("holder")
        self.finish(holder)
        self.finish(process)

    def test_exec_lock_keeps_slots_for_work_the_shell_already_started(self) -> None:
        lock = str(self.tmp / "cache.lock")
        holder = self.lock_holder(lock)
        self.start("perf", "--idle", "60")
        command = 'sh -c "$1" work child & exec "$2" lock "$3" -- true'
        process = self.inside("perf", "shell", "sh", "-c", command, "shell", WORK, TOOL, lock, background=True)
        self.started("child")
        self.wait_for(lambda: self.session("perf").get("lock") == lock, "the exec lock to be noted")
        self.assertEqual("held", self.session("perf")["phase"])
        self.release("child")
        self.release("holder")
        self.finish(holder)
        self.finish(process)

    def test_a_competing_contended_lock_waits_for_the_yielded_session_grant(self) -> None:
        first_lock = str(self.tmp / "first.lock")
        holder = self.lock_holder(first_lock)
        self.start("perf", "--idle", "60")
        first = self.lock_waiter(first_lock)
        other = self.request("other", 1)
        self.started("other")
        second_lock = self.tmp / "second.lock"
        with second_lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            inherited = f"SLOTFATHER_LEASE={self.session('perf')['lease']}"
            second = self.spawn(["env", inherited, TOOL, "lock", str(second_lock), "--",
                                 "sh", "-c", WORK, "work", "competing"])
            self.assertIn("waiting for lock", self.output_line(second))
        def resumed_or_started() -> bool:
            return ("start competing" in self.events() or
                    any(event["kind"] == "lease.lock_ended" and event["details"]["why"] == "resumed"
                        for event in self.events_of()))
        self.wait_for(resumed_or_started, "the contended command to request the slots back")
        self.assertNotIn("start competing", self.events())
        self.release("other")
        self.started("competing")
        self.release("competing")
        self.release("holder")
        self.started("locked")
        self.release("locked")
        for process in (other, second, holder, first):
            self.finish(process)

    def test_taking_a_lock_does_not_clear_another_waiters_marker(self) -> None:
        first_lock, second_lock = str(self.tmp / "first.lock"), self.tmp / "second.lock"
        holder = self.lock_holder(first_lock)
        self.start("perf", "--idle", "60")
        first = self.lock_waiter(first_lock)
        with second_lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            second = self.lock_waiter(str(second_lock), "second")
            self.release("holder")
            self.started("locked")
            self.assertEqual(str(second_lock), self.session("perf").get("lock"))
        self.started("second")
        self.release("second")
        self.release("locked")
        for process in (holder, first, second):
            self.finish(process)

    def test_an_idle_session_releases_itself(self):
        self.start("perf", "--idle", "1")
        self.release("long")
        result = self.inside("perf", "long", "sh", "-c", "sleep 1.5")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIsNotNone(self.session("perf"), "a run counts as use however long it takes")
        self.wait_for(lambda: self.session("perf") is None, "the idle session to release", timeout=10)
        [line] = self.history()
        self.assertEqual("idle", line["session"]["ended_by"])
        self.assertGreaterEqual(line["session"]["busy"], 1.4)

    def test_a_crashed_holder_is_reaped_and_its_session_is_gone(self):
        self.start("perf", "--idle", "60")
        os.kill(self.session("perf")["pid"], signal.SIGKILL)
        self.wait_for(lambda: self.session("perf") is None, "the dead session to be reaped")
        self.assertEqual(["reaped"], [h["outcome"] for h in self.history()])
        self.assertEqual([], list((self.state / "queue").iterdir()))
        missing = self.inside("perf", "x", "true")
        self.assertEqual(2, missing.returncode)
        self.assertIn("no session perf", missing.stderr)

    def test_a_second_session_of_one_name_and_a_session_inside_a_lease_are_refused(self):
        self.start("perf", "--idle", "60")
        again = subprocess.run([TOOL, "session", "start", "--label", "perf"], env=self.env, capture_output=True,
                               text=True, timeout=20)
        self.assertEqual(2, again.returncode)
        self.assertIn("session perf exists", again.stderr)
        nested = self.inside("perf", "x", TOOL, "session", "start", "--label", "other")
        self.assertEqual(2, nested.returncode)
        self.assertIn("inside a lease", nested.stdout + nested.stderr)

    def test_waiters_see_the_sessions_expected_run(self):
        self.start("perf", "--idle", "600", "--expect", "1h")
        waiting = self.request("late", 1)
        self.wait_for(lambda: any(e["label"] == "late" for e in self.entries()), "the request to queue")
        held = self.session("perf")
        late = next(e for e in self.entries() if e["label"] == "late")
        began = time.mktime(time.strptime(held["granted"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        expected = time.mktime(time.strptime(late["est_start"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        self.assertAlmostEqual(began + 3600, expected, delta=60)
        self.assertIn("perf", self.status())
        waiting.kill()

    def test_stop_waits_for_running_work_and_admits_no_more(self):
        self.start("perf", "--idle", "60")
        running = self.inside("perf", "r1", background=True)
        self.started("r1")
        stopping = self.spawn([TOOL, "session", "stop", "perf"])
        def stopping_set() -> bool:
            found: Any = self.session("perf") or {}
            return bool(found.get("session", {}).get("stopping"))
        self.wait_for(stopping_set, "the stop to land")
        refused = self.inside("perf", "r2", "true")
        self.assertEqual(2, refused.returncode)
        self.assertIn("stopping", refused.stderr)
        self.assertIsNone(stopping.poll())
        self.release("r1")
        self.finish(running)
        self.finish(stopping)
        self.assertIsNone(self.session("perf"))

    def test_releasing_or_aborting_a_session_unlinks_its_session_sidecar(self) -> None:
        # 1) Direct Lease.release() on a held session without Holder.hold()'s pre-unlink removes <stem>.session
        self.start("perf", "--idle", "60")
        held = self.session("perf")
        lease_path = _Path(held["lease"])
        self.assertTrue(sf_state.session_path(lease_path).exists())
        os.kill(held["pid"], signal.SIGKILL)
        with mock.patch.dict(os.environ, self.env, clear=True):
            sf_client.Lease(int(held["count"]), lease_path, held=True).release()
        self.assertFalse(lease_path.exists())
        self.assertFalse(sf_state.session_path(lease_path).exists())
        self.assertEqual([], list((self.state / "queue").iterdir()))

        # 2) Session acquire() aborting on timeout after a concurrent grant initialized <stem>.session
        real_release = sf_leases.release

        def grant_then_abort_release(path: _Path, count: int, status: int | None, usage: dict[str, Any] | None,
                                     **kwargs: Any) -> None:
            with sf_state.locked(self.state):
                sf_leases.grant(self.state, sf_process.processes())
            self.assertTrue(sf_state.session_path(path).exists(), "concurrent grant initialized .session")
            real_release(path, count, status, usage, **kwargs)

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(sf_client, "wait_held", side_effect=sf_config.SlotTimeout("timed out")), \
                mock.patch.object(sf_leases, "release", side_effect=grant_then_abort_release):
            with self.assertRaises(sf_config.SlotTimeout):
                sf_client.acquire(1, "aborted-session", "slotfather session aborted-session", wait=0.1,
                                  session={"name": "aborted-session", "idle": 60.0})
        self.assertEqual([], list((self.state / "queue").iterdir()))

    def test_reaping_a_lock_yielded_session_records_was_held_and_yielded_time(self) -> None:
        lock = str(self.tmp / "cache.lock")
        holder = self.lock_holder(lock)
        self.start("perf", "--idle", "60")
        waiting = self.lock_waiter(lock)
        session = self.session("perf")
        self.assertEqual("yielded", session["phase"])
        lease_path = _Path(session["lease"])
        with mock.patch.dict(os.environ, self.env, clear=True), sf_state.editing(lease_path) as raw:
            assert raw is not None
            raw["yielded_at"] = round(float(raw["yielded_at"]) - 12.5, 3)
            sf_state.write(lease_path, raw)

        os.kill(session["pid"], signal.SIGKILL)
        self.wait_for(lambda: self.session("perf") is None, "the lock-yielded session to be reaped")
        self.assertEqual([], list((self.state / "queue").iterdir()))
        [line] = [h for h in self.history() if h["label"] == "perf"]
        self.assertEqual("reaped", line["outcome"])
        self.assertIs(True, line["was_held"])
        self.assertGreater(float(line["yielded"]), 0.0)
        self.assertNotIn("yields", line)
        self.release("holder")
        self.release("locked")
        self.finish(holder)
        out, _ = waiting.communicate(timeout=20)
        self.assertEqual(2, waiting.returncode, out)
        self.assertIn("vanished", out)

    def test_lockwait_handoff_is_single_transaction_and_preserves_concurrent_second_lock_yield(self) -> None:
        self.start("perf", "--idle", "60")
        lease_path = _Path(self.session("perf")["lease"])
        first_lock = self.tmp / "first.lock"
        second_lock = str(self.tmp / "second.lock")
        owner1 = (os.getpid(), sf_process.processes()[os.getpid()][1])
        owner2 = (99999, "Wed Nov 11 00:00:00 2026")
        real_lock_taken = sf_leases.lock_taken

        def lock_taken_then_second_waiter_yields(path: _Path, why: str, **kwargs: Any) -> None:
            real_lock_taken(path, why, **kwargs)
            # Simulate a second waiter yielding the lease on `second_lock` immediately after Waiter 1's lock_taken
            with sf_state.editing(path) as current:
                assert current is not None
                effect = sf_entry.to_lock_waiting(current, time.time(), second_lock, owner2)
                sf_state.commit(path, current, effect)

        handle = os.open(first_lock, os.O_RDWR | os.O_CREAT, 0o666)
        self.addCleanup(os.close, handle)
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(sf_lockwait, "enclosing", return_value=(lease_path, owner1, True)), \
                mock.patch.object(sf_leases, "yield_for_lock", return_value="yielded"), \
                mock.patch.object(sf_leases, "lock_taken", side_effect=lock_taken_then_second_waiter_yields), \
                mock.patch.object(sf_client, "held_again"), \
                contextlib.redirect_stderr(io.StringIO()):
            with sf_state.editing(lease_path) as initial:
                assert initial is not None
                eff = sf_entry.to_lock_waiting(initial, time.time(), str(first_lock), owner1)
                sf_state.commit(lease_path, initial, eff)
            sf_lockwait.wait(handle, first_lock)

        after = sf_state.read_entry(lease_path)
        assert after is not None
        self.assertEqual(second_lock, sf_entry.lock_of(after))
        self.assertEqual(owner2, sf_entry.lock_owner(after))


if __name__ == "__main__":
    unittest.main()
