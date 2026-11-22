#!/usr/bin/env python3
"""The expected-peak memory budget (fathers#9) as the readers report it (fathers#206, fathers#214): grant() holds a
request back beside running leases whose expected peak memory and the request's exceed `memory_share` of the memory
(including the 2nd+ grant on an initially idle host); the dashboard's admission, its `memory_budget_block` and
`advise` share the same Admission seam. Admission alone, then grant(), the dashboard block and `advise` on injected
queues."""
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
import time
import unittest
from typing import Any
from unittest import mock

from test_slotfather import SlotCase

DEFAULTS: dict[str, Any] = dict(sf_config.ORDER_DEFAULTS)
M = 1_800_000_000.0
SINCE = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(M))
HELD: list[dict[str, Any]] = [{"count": 1, "label": "running", "since": SINCE, "granted": SINCE, "state": "held"}]


class AdmissionBudgetTests(unittest.TestCase):
    """Admission under memory_share saturation: 16 GiB (85% = 13926.4 MB), 4 slots."""

    def adm(
        self,
        waiters: list[dict[str, Any]],
        held: list[dict[str, Any]],
        *,
        peaks: dict[str, float] | None = None,
        ram_mb: float | None = 16384.0,
        memory: tuple[str, str | None] = ("normal", None),
        **settings: float,
    ) -> sf_schedule.Admission:
        table = peaks or {}
        mem_est: sf_schedule.Estimate | None = (lambda e: table.get(str(e.get("label", "")))) if peaks is not None else None
        return sf_schedule.Admission(
            waiters,
            held,
            4,
            M,
            settings={**DEFAULTS, **settings},
            host=sf_schedule.Host(memory=memory, ram_mb=ram_mb),
            memory_estimate=mem_est,
        )

    def test_over_the_share_holds_and_says_why(self) -> None:
        waiter = {"count": 1, "label": "b", "since": SINCE, "state": "waiting"}
        over = self.adm([waiter], HELD, peaks={"running": 9500.0, "b": 4500.0}).plan_grants()
        expected = "expected memory 14000 MB exceeds 13926 MB (85% of 16384 MB)"
        self.assertEqual([], over.started)
        self.assertEqual([(waiter, sf_schedule.GateBlock("budget", expected))], over.gated)
        self.assertEqual(expected, over.reason)

        at_limit = self.adm([waiter], HELD, peaks={"running": 9500.0, "b": 4426.4}).plan_grants()
        self.assertEqual([waiter], [s.entry for s in at_limit.started])
        self.assertEqual([], at_limit.gated)

    def test_unknown_memory_or_no_share_has_no_budget(self) -> None:
        waiter = {"count": 1, "label": "b", "since": SINCE, "state": "waiting"}
        for ram_mb, share in ((None, 0.85), (0.0, 0.85), (16384.0, 0.0)):
            with self.subTest(ram_mb=ram_mb, share=share):
                adm = self.adm([waiter], HELD, peaks={"running": 1e9, "b": 1e9}, ram_mb=ram_mb, memory_share=share)
                outcome = adm.plan_grants()
                self.assertEqual([waiter], [s.entry for s in outcome.started])
                self.assertEqual(2, adm.dashboard_room().admission)
                self.assertEqual(3, self.adm([], HELD, peaks={"running": 1e9}, ram_mb=ram_mb,
                                             memory_share=share).dashboard_room().admission)
                self.assertIsNone(adm.dashboard_room().memory_budget_block)

    def test_two_waiters_on_an_idle_host_exceeding_memory_share_admit_only_the_first(self) -> None:
        """fathers#214: _gate checks memory_share on the 2nd+ grant of an initially idle host."""
        first = {"count": 1, "label": "first", "since": SINCE, "state": "waiting"}
        second = {"count": 1, "label": "second", "since": SINCE, "state": "waiting"}
        adm = self.adm([first, second], [], peaks={"first": 9000.0, "second": 9000.0}, ram_mb=16384.0)
        outcome = adm.plan_grants()
        expected = "expected memory 18000 MB exceeds 13926 MB (85% of 16384 MB)"
        self.assertEqual([first], [s.entry for s in outcome.started])
        self.assertEqual([(second, sf_schedule.GateBlock("budget", expected))], outcome.gated)
        self.assertEqual(expected, outcome.reason)
        # Dashboard room also sees that a generic probe behind `first` exceeds memory_share on the idle host.
        probe_adm = self.adm([first], [], peaks={"first": 9000.0, "": 9000.0}, ram_mb=16384.0)
        self.assertEqual(0, probe_adm.dashboard_room().admission)

    def test_held_leases_that_fill_the_share_close_dashboard_admission(self) -> None:
        over = self.adm([], HELD, peaks={"running": 12000.0, "": 2048.0})
        self.assertEqual(0, over.dashboard_room().admission)
        self.assertIn("expected memory 14048 MB exceeds 13926 MB", over.dashboard_room().memory_budget_block or "")
        fits = self.adm([], HELD, peaks={"running": 11000.0, "": 2048.0})
        self.assertEqual(3, fits.dashboard_room().admission)
        self.assertIsNone(fits.dashboard_room().memory_budget_block)

    def test_the_default_estimate_counts_for_a_request_of_unknown_peak(self) -> None:
        self.assertEqual(3, self.adm([], HELD, peaks={"running": 11000.0}).dashboard_room().admission)
        self.assertEqual(
            0, self.adm([], HELD, peaks={"running": 11000.0}, memory_default_mb=4000.0).dashboard_room().admission
        )

    def test_an_idle_machine_starts_its_first_request(self) -> None:
        room = self.adm([], [], peaks={"": 99999.0}).dashboard_room()
        self.assertEqual((4, None), (room.admission, room.memory_budget_block))

    def test_the_budget_also_closes_the_one_slot_a_warning_admits(self) -> None:
        warn = ("warning", "level 2")
        self.assertEqual(
            0, self.adm([], HELD, peaks={"running": 12000.0, "": 2048.0}, memory=warn).dashboard_room().admission
        )
        self.assertEqual(
            1, self.adm([], HELD, peaks={"running": 1000.0}, memory=warn).dashboard_room().admission
        )


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

    def test_two_waiters_on_an_idle_host_exceeding_memory_share_admit_only_the_first(self) -> None:
        first = self.entry("first", 1)
        second = self.entry("second", 1)
        reason = "expected memory 60000 MB exceeds 55706 MB (85% of 65536 MB)"
        with mock.patch.dict(os.environ, {**self.env, "SLOTFATHER_MEMORY_DEFAULT_MB": "30000"}, clear=True):
            why = sf_leases.grant(self.state, sf_process.processes())
        self.assertEqual(reason, why)
        self.assertEqual(("held", "waiting"),
                         (json.loads(first.read_text())["state"], json.loads(second.read_text())["state"]))

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
