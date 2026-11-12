#!/usr/bin/env python3
"""`slotfather ask-yield PID --start START` and `reprioritize ... --start START` (slotfather#30): dashboard actions
checked against the process the caller saw, and a yield request the holder takes at its next check."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import json
import time
from typing import Any

from test_classes import YIELDER
from test_slotfather import TOOL, SlotCase


class AskYieldTests(SlotCase):
    def setUp(self) -> None:
        super().setUp()
        self.env["TOOL"] = TOOL
        # Nobody asks on their own within this test: only ask-yield does
        self.assertEqual(0, self.tool("order", "--yield-after", "3600").returncode)

    def entries(self) -> dict[str, dict[str, Any]]:
        return {e["label"]: e for e in json.loads(self.tool("status", "--json").stdout)["entries"]}

    def gate(self, *options: str, script: str = YIELDER) -> Any:
        return self.request("gate", 4, "sh", "-c", script, "work", "gate", options=("--priority", "batch", *options))

    def test_the_holder_yields_at_its_next_check_and_is_never_stopped(self) -> None:
        gate = self.gate("--yieldable")
        self.started("gate")
        quick = self.request("quick", 1, options=("--priority", "interactive"))
        self.wait_for(lambda: "waiting: 1 slot(s) quick" in self.status(), "quick to queue")
        held = self.entries()["gate"]
        result = self.tool("ask-yield", str(held["pid"]), "--start", held["start"], GODFATHER_ACTOR="godfather/0.1.0")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("gate (pid", result.stdout)
        self.assertIn("asked to yield 4 slot(s) for quick", result.stdout)
        self.started("quick")
        self.assertEqual(["start gate", "yield gate", "start quick"], self.events(), "it yielded between its steps")
        self.assertIn("asked gate (pid", (self.state / "queue.log").read_text())
        events = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]
        asked = next(e for e in events if e["kind"] == "lease.yield_asked")
        self.assertEqual(("godfather", "quick", True), (asked["actor"]["via"], asked["details"]["for"]["label"],
                                                        asked["details"]["by_hand"]))
        self.release("quick")
        self.wait_for(lambda: "resume gate" in self.events(), "the gate to resume")
        self.release("gate")
        for process in (gate, quick):
            self.finish(process)

    def test_the_request_shows_in_status_until_the_holder_takes_it(self) -> None:
        # A holder that does not look between steps: the request stands, ❓ in the dashboard, and nothing stops it
        gate = self.gate("--yieldable", script='echo "start $1" >> "$LOG"; while [ ! -e "$RELEASE/$1" ]; do sleep 0.05; '
                                               'done; echo "end $1" >> "$LOG"')
        self.started("gate")
        quick = self.request("quick", 1, options=("--priority", "interactive"))
        self.wait_for(lambda: "waiting: 1 slot(s) quick" in self.status(), "quick to queue")
        held = self.entries()["gate"]
        self.assertEqual(0, self.tool("ask-yield", str(held["pid"]), "--start", held["start"]).returncode)
        time.sleep(0.6)  # several looks of the waiting client: a request by hand is not withdrawn while quick waits
        self.assertEqual("quick", self.entries()["gate"]["yield_asked"]["label"])
        self.assertRegex(self.status(), r"held: 4 slot\(s\) gate .*asked-to-yield-for=quick")
        full = json.loads(self.tool("status", "--json", "--full").stdout)
        self.assertEqual("quick", full["dashboard"]["holders"][0]["asked"])
        again = self.tool("ask-yield", str(held["pid"]), "--start", held["start"])
        self.assertIn("already asked to yield for quick", again.stdout)
        self.assertEqual(["start gate"], self.events(), "the holder runs on")
        self.release("gate")
        self.started("quick")
        self.release("quick")
        for process in (gate, quick):
            self.finish(process)

    def test_a_reused_pid_or_a_lease_that_cannot_yield_is_refused(self) -> None:
        gate = self.gate()
        self.started("gate")
        quick = self.request("quick", 1, options=("--priority", "interactive"))
        self.wait_for(lambda: "waiting: 1 slot(s) quick" in self.status(), "quick to queue")
        held = self.entries()["gate"]
        stranger, began = self.holder()
        for argv, words in (((str(held["pid"]), "--start", "Mon Jan  1 00:00:00 2001"), "is not the process started"),
                            ((str(held["pid"]), "--start", held["start"]), "did not take --yieldable"),
                            ((str(stranger), "--start", began), "holds no lease"),
                            ((str(held["pid"]),), "takes the holder's pid and --start START")):
            with self.subTest(argv=argv):
                result = self.tool("ask-yield", *argv)
                self.assertEqual(2, result.returncode)
                self.assertIn(words, result.stderr)
        self.assertFalse(list((self.state / "queue").glob("*.yield")))
        self.release("gate")
        self.started("quick")
        self.release("quick")
        for process in (gate, quick):
            self.finish(process)

    def test_no_higher_class_waiter_means_nothing_is_asked(self) -> None:
        gate = self.gate("--yieldable")
        self.started("gate")
        slow = self.request("slow", 1, options=("--priority", "batch"))
        self.wait_for(lambda: "waiting: 1 slot(s) slow" in self.status(), "slow to queue")
        held = self.entries()["gate"]
        result = self.tool("ask-yield", str(held["pid"]), "--start", held["start"])
        self.assertEqual(2, result.returncode)
        self.assertIn("no waiting request of a higher class", result.stderr)
        self.release("gate")
        self.started("slow")
        self.release("slow")
        for process in (gate, slow):
            self.finish(process)

    def test_reprioritize_checks_the_start_it_is_given(self) -> None:
        holder = self.request("hog", 4)
        self.started("hog")
        late = self.request("late", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) late" in self.status(), "late to queue")
        entry = self.entries()["late"]
        stale = self.tool("reprioritize", str(entry["pid"]), "batch", "--start", "Mon Jan  1 00:00:00 2001")
        self.assertEqual(2, stale.returncode)
        self.assertIn("is not the process started", stale.stderr)
        self.assertEqual(2, self.tool("reprioritize", "late", "batch", "--start", entry["start"]).returncode)
        self.assertEqual("normal", self.entries()["late"]["priority"])
        moved = self.tool("reprioritize", str(entry["pid"]), "batch", "--start", entry["start"])
        self.assertEqual(0, moved.returncode, moved.stderr)
        self.assertEqual("batch", self.entries()["late"]["priority"])
        self.release("hog")
        self.started("late")
        self.release("late")
        for process in (holder, late):
            self.finish(process)

    def test_capabilities_offer_both_actions(self) -> None:
        found = json.loads(self.tool("capabilities", "--json").stdout)
        self.assertEqual(["reprioritize", "ask-yield"], found["dashboard"]["actions"])
