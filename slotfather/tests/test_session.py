#!/usr/bin/env python3
"""Measurement sessions (fathers#96): `session start` drains the queue once and holds it for a series of
`run --session NAME` commands, until `session stop`, an idle timeout or its holder's death releases it."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
import eventcheck  # common/eventcheck.py, the shared event validator

import json
import os
import signal
import subprocess
import time
import unittest
from typing import Any

from test_slotfather import TOOL, WORK, SlotCase


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


if __name__ == "__main__":
    unittest.main()
