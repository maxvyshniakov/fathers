#!/usr/bin/env python3
"""The expected-peak memory budget (fathers#9) as the readers report it (fathers#206): grant() holds a request back
beside running leases whose expected peak memory and the request's exceed `memory_share` of the memory; the
dashboard's admission, its `memory_budget_block` and `advise` say the same. budget_wait() and dashboard_admission()
alone, then grant(), the dashboard block and `advise` on injected queues."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import board as sf_board
from slotfather import config as sf_config
from slotfather import leases as sf_leases
from slotfather import process as sf_process
from slotfather import query as sf_query
from slotfather import schedule as sf_schedule


import json
import os
import unittest
from typing import Any
from unittest import mock

from test_slotfather import SlotCase

DEFAULTS: dict[str, Any] = dict(sf_config.ORDER_DEFAULTS)
HELD: list[dict[str, Any]] = [{"count": 1, "label": "running", "state": "held"}]


class BudgetWaitTests(unittest.TestCase):
    """budget_wait() alone: 16 GiB, 85% (13926.4 MB)."""

    def test_over_the_share_says_why(self) -> None:
        self.assertEqual("expected memory 14000 MB exceeds 13926 MB (85% of 16384 MB)",
                         sf_schedule.budget_wait(9500.0, 4500.0, 16384.0, 0.85))
        self.assertIsNone(sf_schedule.budget_wait(9500.0, 4426.4, 16384.0, 0.85), "at the budget it still fits")

    def test_unknown_memory_or_no_share_has_no_budget(self) -> None:
        for ram_mb, share in ((None, 0.85), (0.0, 0.85), (16384.0, 0.0)):
            with self.subTest(ram_mb=ram_mb, share=share):
                self.assertIsNone(sf_schedule.budget_wait(1e9, 1e9, ram_mb, share))

    def test_memory_gate_gives_the_same_reason(self) -> None:
        gated = sf_schedule.memory_gate([{"count": 1, "label": "b"}], [{"count": 1, "label": "a"}], 1,
                                        ("normal", None), {**DEFAULTS, "memory_share": 0.85},
                                        memory_estimate=lambda entry: 9500.0 if entry["label"] == "a" else 4500.0,
                                        ram_mb=16384.0)
        self.assertEqual([sf_schedule.budget_wait(9500.0, 4500.0, 16384.0, 0.85)], [why for _, why in gated])


class DashboardAdmissionTests(unittest.TestCase):
    """dashboard_admission() under memory_share saturation, on 4 slots."""

    def admission(self, held: list[dict[str, Any]], expected_mb: float, need_mb: float | None = None,
                  ram_mb: float | None = 16384.0, **settings: float) -> int:
        return sf_board.dashboard_admission([], held, 4, 0, None, None, {**DEFAULTS, **settings}, "normal", None,
                                            memory_expected_mb=expected_mb, memory_need_mb=need_mb, ram_mb=ram_mb)

    def test_held_leases_that_fill_the_share_close_admission(self) -> None:
        self.assertEqual(0, self.admission(HELD, 12000.0, 2048.0), "12000 + 2048 > 13926")
        self.assertEqual(3, self.admission(HELD, 11000.0, 2048.0), "11000 + 2048 fits: the free slots")

    def test_the_default_estimate_counts_for_a_request_of_unknown_peak(self) -> None:
        self.assertEqual(3, self.admission(HELD, 11000.0))
        self.assertEqual(0, self.admission(HELD, 11000.0, memory_default_mb=4000.0))

    def test_an_idle_machine_starts_its_first_request(self) -> None:
        self.assertEqual(4, self.admission([], 0.0, 99999.0), "grant()'s gate applies beside running leases only")

    def test_unknown_memory_a_share_of_0_and_older_callers_keep_the_slots(self) -> None:
        self.assertEqual(3, self.admission(HELD, 99999.0, ram_mb=None))
        self.assertEqual(3, self.admission(HELD, 99999.0, memory_share=0.0))
        self.assertEqual(3, sf_board.dashboard_admission([], HELD, 4, 0, None, None, DEFAULTS, "normal", None),
                         "without the budget's facts it admits by the other rules")

    def test_the_budget_also_closes_the_one_slot_a_warning_admits(self) -> None:
        found = sf_board.dashboard_admission([], HELD, 4, 0, sf_schedule.WARNING_WAIT, None, DEFAULTS, "warning",
                                             memory_expected_mb=12000.0, memory_need_mb=2048.0, ram_mb=16384.0)
        self.assertEqual(0, found)
        self.assertEqual(1, sf_board.dashboard_admission([], HELD, 4, 0, sf_schedule.WARNING_WAIT, None, DEFAULTS,
                                                         "warning", memory_expected_mb=1000.0, ram_mb=16384.0))


class ReaderTests(SlotCase):
    """grant(), the dashboard block and `advise` on an injected queue of 4 slots, 12 cores and 64 GiB (budget
    55705.6 MB); SLOTFATHER_MEMORY_DEFAULT_MB raises every unknown peak to fill it."""

    def setUp(self) -> None:
        super().setUp()
        self.queue = self.state / "queue"
        self.queue.mkdir(parents=True)
        self.sequence = 0

    def entry(self, label: str, count: int, state: str = "waiting") -> _Path:
        pid, start = self.holder()
        self.sequence += 1
        path = self.queue / f"{self.sequence:010d}-{pid}.json"
        path.write_text(json.dumps({"pid": pid, "start": start, "count": count, "label": label, "cmd": "fake",
                                    "since": "2026-11-12T00:00:00Z", "state": state,
                                    **({"granted": "2026-11-12T00:00:00Z"} if state == "held" else {})}))
        return path

    def look(self, default_mb: str) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        with mock.patch.dict(os.environ, {**self.env, "SLOTFATHER_MEMORY_DEFAULT_MB": default_mb}, clear=True):
            block = sf_board.dashboard_json(sf_board.dashboard_model())
            advice = sf_query.advise()
            why = sf_leases.grant(self.state, sf_process.processes())
        return block, advice, why

    def test_a_saturated_budget_closes_admission_as_grant_holds_the_waiter(self) -> None:
        self.entry("running", 1, "held")
        waiter = self.entry("waiter", 1)
        reason = "expected memory 60000 MB exceeds 55706 MB (85% of 65536 MB)"
        block, advice, why = self.look("30000")
        self.assertEqual((0, reason, None, None), (block["admission"], block["memory_budget_block"],
                                                   block["memory_block"], block["load_block"]))
        self.assertEqual((30000.0, 55705.6), (block["memory_expected_mb"], block["memory_budget_mb"]))
        self.assertEqual(reason, advice["memory_budget_block"])
        self.assertEqual(reason, why, "grant() holds the waiter for the same reason")
        self.assertEqual("waiting", json.loads(waiter.read_text())["state"])

    def test_room_in_the_budget_leaves_admission_and_the_fields_open(self) -> None:
        self.entry("running", 1, "held")
        block, advice, why = self.look("2048")
        self.assertEqual((3, None, None), (block["admission"], block["memory_budget_block"],
                                           advice["memory_budget_block"]))
        self.assertIsNone(why)

    def test_an_idle_machine_reports_no_budget_block(self) -> None:
        block, advice, _ = self.look("99999")
        self.assertEqual((4, None, None), (block["admission"], block["memory_budget_block"],
                                           advice["memory_budget_block"]))


if __name__ == "__main__":
    unittest.main()
