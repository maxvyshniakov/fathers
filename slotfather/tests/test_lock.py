"""`slotfather lock PATH -- COMMAND` (fathers#159): a lease gives up its slots while its command waits on a lock,
takes them back before the command runs, shows the wait in status, and an older client reads such a lease."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import json
import subprocess
import sys
import time
from typing import Any
import unittest

from test_live_cutover import CutoverCase
from test_slotfather import TOOL, WORK, SlotCase

from slotfather import entry as sf_entry
from slotfather import idle as sf_idle
from slotfather import schedule as sf_schedule


class LockTests(SlotCase):
    def setUp(self) -> None:
        super().setUp()
        self.lock = self.tmp / "cache.lock"
        self.env.update(TOOL=TOOL, LOCK=str(self.lock), WORK=WORK)
        self.assertEqual(0, self.tool("slots", "2").returncode)

    def entries(self, *options: str) -> dict[str, dict[str, Any]]:
        found = json.loads(self.tool("status", "--json", *options).stdout)
        return {e["label"]: e for e in found["entries"]}

    def locked(self, label: str, then: str = "") -> subprocess.Popen[str]:
        """`slotfather run -n 1 --label LABEL -- slotfather lock LOCK -- <work LABEL>`, then `<work THEN>` under
        the same lease without the lock."""
        script = '"$TOOL" lock "$LOCK" -- sh -c "$WORK" work "$1"' + ('; sh -c "$WORK" work "$2"' if then else "")
        return self.request(label, 1, "sh", "-c", script, "locked", label, then)

    def test_a_lease_waiting_on_a_lock_frees_its_slot_and_takes_it_back_before_it_runs(self) -> None:
        first = self.locked("a", then="a2")
        self.started("a")
        second = self.locked("b")
        self.wait_for(lambda: self.entries().get("b", {}).get("lock") == str(self.lock), "b to wait for the lock")
        waiting = self.entries()["b"]
        self.assertEqual(("waiting", "yielded"), (waiting["state"], waiting["phase"]))
        self.assertEqual(0, waiting["yields"], "a lock wait is no polite yield: YIELDS_MAX is not spent on it")
        self.assertIn("waiting: 1 slot(s) b ", self.status())
        self.assertIn(f"waiting-for-lock={self.lock}", self.status())
        self.assertIn("1 of 2 slots held", self.status())
        dashboard = json.loads(self.tool("status", "--json", "--full").stdout)["dashboard"]
        self.assertEqual([str(self.lock)], [w["lock"] for w in dashboard["waiters"] if w["label"] == "b"])
        self.assertNotIn("lock", self.entries()["a"], "status --json adds `lock` only where a lease waits on one")
        # Its slot is free for others meanwhile
        third = self.request("c", 1)
        self.started("c")
        # a releases the lock and runs on under its lease; b takes the lock but no slot is free: it waits for one
        self.release("a")
        self.started("a2")
        self.wait_for(lambda: "took lock" in (self.state / "queue.log").read_text(), "b to take the lock")
        self.assertNotIn("lock", self.entries()["b"])
        self.assertEqual("waiting", self.entries()["b"]["state"])
        self.assertNotIn("start b", self.events(), "b runs only once it holds its slot again")
        self.release("c")
        self.started("b")
        self.assertEqual("held", self.entries()["b"]["state"])
        log = (self.state / "queue.log").read_text()
        self.assertIn(f"while waiting for lock {self.lock}", log)
        events = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]
        yielded = [e for e in events if e["kind"] == "lease.yielded"]
        self.assertEqual([(str(self.lock), f"lock {self.lock}")],
                         [(e["details"]["lock"], e["details"]["to"]["label"]) for e in yielded])
        for label in ("a2", "b"):
            self.release(label)
        for process in (first, second, third):
            self.finish(process)
        history = [json.loads(line) for line in (self.state / "history.jsonl").read_text().splitlines()]
        self.assertEqual("released", next(h for h in history if h["label"] == "b")["outcome"])

    def test_a_free_lock_runs_at_once_and_outside_a_lease_it_is_a_plain_lock(self) -> None:
        result = subprocess.run([TOOL, "lock", str(self.lock), "--", "sh", "-c", "exit 3"], env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(3, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertFalse((self.state / "queue").exists() and any((self.state / "queue").iterdir()))
        for argv in (["lock"], ["lock", str(self.lock)], ["lock", str(self.lock), "true"], ["lock", str(self.lock), "--"]):
            with self.subTest(argv=argv):
                self.assertEqual(2, self.tool(*argv).returncode)


class OlderClientTests(CutoverCase):
    def test_an_older_client_reads_a_lease_waiting_on_a_lock_as_a_yielded_waiter(self) -> None:
        state = self.tmp / "state"
        (state / "queue").mkdir(parents=True)
        pid, start = self.sleeper()
        entry: dict[str, Any] = {"pid": pid, "start": start, "count": 1, "label": "b", "cmd": "make b",
                                 "since": "2026-11-11T00:00:00Z", "state": "held", "granted": "2026-11-11T00:01:00Z"}
        sf_entry.to_lock_waiting(entry, time.time(), "/tmp/cache.lock")
        (state / "queue" / f"0000000001-{pid}.json").write_text(json.dumps(entry))
        old = self.drive(self.old_root, state, "look")
        self.assertEqual([["b", "waiting", "normal", None, 1]], old["status"])
        self.assertTrue(any(line.startswith("waiting: 1 slot(s) b") for line in old["lines"]), old["lines"])
        new = self.drive(self.new_root, state, "look")
        self.assertEqual([], new["grants"], "this client never grants a lease while its command waits on the lock")


class RuleTests(unittest.TestCase):
    def test_plan_neither_grants_nor_heads_a_lease_waiting_on_a_lock(self) -> None:
        waiting: dict[str, Any] = {"label": "b", "count": 1, "since": "2026-11-11T00:00:00Z", "state": "waiting",
                                   "lock": "/tmp/x.lock"}
        found = sf_schedule.plan([waiting], [], 2, 1_800_000_000.0)
        self.assertEqual(([], None), (found.grants, found.head))
        sf_entry.lock_taken(waiting)
        self.assertEqual([waiting], sf_schedule.plan([waiting], [], 2, 1_800_000_000.0).grants)

    def test_a_container_or_vm_program_counts_as_work(self) -> None:
        for name in ("docker", "docker-compose", "podman", "qemu-system-aarch64", "limactl", "vfkit", "colima"):
            with self.subTest(name=name):
                self.assertTrue(sf_idle.container(name))
        for name in ("python3", "cargo", "dockerize", "sh"):
            with self.subTest(name=name):
                self.assertFalse(sf_idle.container(name))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
