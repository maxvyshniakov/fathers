#!/usr/bin/env python3
"""Leases weighted by the cores they use (fathers#148): `run --cores C|auto` turns cores into slots, `run` warns when
a label family's measured use runs far above the cores its slots hold, `advise` reports the core-weighted slot count
(`weight`), and `stats`, `history` and `status` show the requests that use more than they hold. The test machine has
12 cores and 4 slots: 3 cores per slot."""
from __future__ import annotations

import json
import unittest
from typing import Any

from test_lease_facts import line
from test_slotfather import SlotCase
from slotfather import query as sf_query
from slotfather import runtype as sf_runtype
from slotfather import state as sf_state


def browser(runs: int, cpu: float = 600, count: int = 1) -> list[dict[str, Any]]:
    """`runs` released one-slot browser checks of 100 s that used `cpu` / 100 cores each."""
    return [line(f"browser-{n}", count, cpu, cmd="npx playwright test") for n in range(runs)]


class CoreArithmeticTests(unittest.TestCase):
    def test_slots_hold_the_cores_asked_rounding_up_within_the_capacity(self):
        self.assertEqual([1, 1, 2, 3, 4, 4], [sf_runtype.slots_for(c, 3.0, 4) for c in (0.5, 3, 3.01, 7, 12, 100)])
        self.assertEqual([2, 4], [sf_runtype.slots_for(c, 1.5, 8) for c in (3, 6)], "the 12-core, 8-slot machine")
        self.assertEqual(1, sf_runtype.slots_for(6, 0.0, 4), "no width: one slot, as without --cores")

    def test_a_family_running_far_above_its_ask_is_under_asked_with_the_slots_its_use_needs(self):
        row = sf_runtype.family_row("browser", browser(6, cpu=600) + browser(1, cpu=900), 3.0)
        self.assertEqual((7, 1, 6.0, 3.0, 2, True, False),
                         (row["measured"], row["slots_p50"], row["cores_p50"], row["cores_asked_p50"],
                          row["slots_needed"], row["under_ask"], row["over_ask"]))
        self.assertEqual(7.2, row["cores_p90"])
        near = sf_runtype.family_row("browser", browser(6, cpu=400), 3.0)  # 4 cores of 3: under 1.5 times the ask
        self.assertFalse(near["under_ask"])
        fitted = sf_runtype.family_row("browser", browser(6, cpu=600, count=2), 3.0)  # 6 cores in 2 slots
        self.assertEqual((False, False), (fitted["under_ask"], fitted["over_ask"]))

    def test_history_lines_mark_a_lease_that_used_far_more_cores_than_it_held(self):
        self.assertEqual(3.0, sf_state.held_cores(line("a-1", 1, 600)))
        self.assertIsNone(sf_state.held_cores(line("a-1", 4, 600)), "an all-slot lease asks for the machine")
        self.assertIsNone(sf_state.held_cores(line("a-1", 1, 600, session={"name": "s"})))
        self.assertIsNone(sf_state.held_cores({"count": 1, "cpu": 600}), "an older line without cores")

    def test_status_tags_a_held_lease_measured_far_above_its_slots(self):
        self.assertEqual(" over-cores=6.0/3", sf_query.cores_tag(1, 600.0, 12, 4))
        self.assertEqual(" over-cores=3.0/1.5", sf_query.cores_tag(1, 300, 12, 8))
        self.assertEqual("", sf_query.cores_tag(1, 400.0, 12, 4), "4 cores of 3: under 1.5 times the slots")
        self.assertEqual("", sf_query.cores_tag(4, 1200.0, 12, 4), "an all-slot lease holds the machine")
        self.assertEqual("", sf_query.cores_tag(1, None, 12, 4), "no fresh record from its holder")
        self.assertEqual("", sf_query.cores_tag(1, float("inf"), 12, 4))
        self.assertEqual("", sf_query.cores_tag(True, 600.0, 12, 4))


class CoreWeightTests(SlotCase):
    def history(self, items: list[dict[str, Any]]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "history.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))

    def run_slots(self, *options: str, label: str = "browser-9") -> tuple[int, list[str]]:
        """`run OPTIONS --label LABEL` of a command that prints its SLOTFATHER_SLOTS: (the slots, its stderr)."""
        result = self.tool("run", *options, "--label", label, "--", "sh", "-c", 'printf %s "$SLOTFATHER_SLOTS"')
        self.assertEqual(0, result.returncode, result.stderr)
        return int(result.stdout), result.stderr.splitlines()

    def last_history(self) -> dict[str, Any]:
        return json.loads((self.state / "history.jsonl").read_text().splitlines()[-1])

    def test_run_warns_when_its_family_used_far_more_cores_than_its_slots_hold(self):
        self.history(browser(6))  # 6 cores each in one slot of 3
        slots, stderr = self.run_slots("-n", "1")
        self.assertEqual(1, slots, "the warning never changes what a request asked for")
        [warning] = [text for text in stderr if "warning" in text]
        self.assertIn("browser requests used a median 6.0 cores (p90 6.0) over their last 6 runs", warning)
        self.assertIn("1 slot holds 3 cores, 2 would hold that use (-n 2, or --cores auto)", warning)
        self.assertEqual([], [t for t in self.run_slots("-n", "2")[1] if "warning" in t], "6 cores fit 2 slots")
        self.assertEqual([], [t for t in self.run_slots("-n", "all")[1] if "warning" in t])
        self.assertEqual([], [t for t in self.run_slots("-n", "1", label="other-1")[1] if "warning" in t])

    def test_no_warning_with_few_runs_or_use_near_the_ask(self):
        self.history(browser(4))
        self.assertEqual([], [t for t in self.run_slots("-n", "1")[1] if "warning" in t], "four runs are too few")
        self.history(browser(6, cpu=400))
        self.assertEqual([], [t for t in self.run_slots("-n", "1")[1] if "warning" in t], "4 cores of 3")

    def test_run_cores_holds_the_slots_those_cores_need(self):
        self.assertEqual(3, self.run_slots("--cores", "7")[0])
        self.assertEqual(3, self.last_history()["count"])
        self.assertEqual(1, self.run_slots("--cores", "1.5")[0])
        self.assertEqual(4, self.run_slots("--cores", "40")[0], "at most the capacity")

    def test_run_cores_auto_takes_the_family_median_use_or_one_slot(self):
        slots, stderr = self.run_slots("--cores", "auto")
        self.assertEqual(1, slots)
        self.assertIn("slot: --cores auto: too few measured browser runs; asking 1 slot", stderr)
        self.history(browser(6))
        slots, stderr = self.run_slots("--cores", "auto")
        self.assertEqual(2, slots)
        self.assertIn("slot: --cores auto: browser requests used a median 6.0 cores over their last 6 runs; "
                      "asking 2 slot(s)", stderr)
        self.assertEqual([], [text for text in stderr if "warning" in text], "the weighted ask holds that use")

    def test_run_refuses_a_cores_value_it_cannot_weigh_or_beside_n_or_a_session(self):
        for options in (("--cores", "0"), ("--cores", "-2"), ("--cores", "x"), ("--cores", "nan"),
                        ("--cores", "inf"), ("-n", "2", "--cores", "3"), ("--session", "s", "--cores", "2")):
            result = self.tool("run", *options, "--", "true")
            self.assertEqual(2, result.returncode, options)
        self.assertIn("--cores takes a positive number of cores or auto", self.tool("run", "--cores", "x", "--", "true").stderr)
        self.assertIn("-n or --cores, not both", self.tool("run", "-n", "2", "--cores", "3", "--", "true").stderr)
        self.assertFalse((self.state / "history.jsonl").exists(), "a refused run takes no lease")

    def test_advise_reports_the_core_weighted_slots_beside_its_worker_budget(self):
        advice = json.loads(self.tool("advise", "--label", "browser-1").stdout)
        self.assertEqual(1, advice["v"])
        self.assertIn("jobs", advice, "the worker budget stays as it was")
        self.assertEqual({"slots": 1, "cores": None, "source": "default", "cores_per_slot": 3.0, "family": "browser",
                          "measured": 0, "slots_p50": None, "cores_p50": None, "cores_p90": None,
                          "slots_needed": None, "over_ask": False, "under_ask": False}, advice["weight"])
        self.history(browser(6))
        weight = json.loads(self.tool("advise", "--label", "browser-1").stdout)["weight"]
        self.assertEqual((2, 6.0, "history", 6, True), (weight["slots"], weight["cores"], weight["source"],
                                                         weight["measured"], weight["under_ask"]))
        self.assertEqual(weight, json.loads(self.tool("advise", "--cores", "auto", "--label", "browser-1").stdout)["weight"])
        declared = json.loads(self.tool("advise", "--cores", "7", "--label", "browser-1").stdout)["weight"]
        self.assertEqual((3, 7.0, "declared", True), (declared["slots"], declared["cores"], declared["source"],
                                                      declared["under_ask"]))
        self.assertEqual(2, self.tool("advise", "--cores", "none").returncode)

    def test_stats_and_history_show_the_requests_that_use_more_than_they_hold(self):
        self.history(browser(6) + [line("lane-1", 1, 150)])
        summary = json.loads(self.tool("stats", "--json").stdout)
        asks = {row["family"]: row for row in summary["asks"]}
        self.assertEqual((True, 6.0, 2), (asks["browser"]["under_ask"], asks["browser"]["cores_p90"],
                                          asks["browser"]["slots_needed"]))
        self.assertFalse(asks["lane"]["under_ask"])
        text = self.tool("stats").stdout
        self.assertIn("browser: 1 slots, 6.0 (6.0) of 3.0 cores over 6 runs; -n 2 would hold it",
                      text.split("asks far below use")[1])
        lines = self.tool("history", "2").stdout.splitlines()
        self.assertIn("used 6.0 cores > 3 held, 10 MB", lines[0])
        self.assertIn("used 1.5 cores, 10 MB", lines[1], "a lease within its slots reads as before")


if __name__ == "__main__":
    unittest.main()
