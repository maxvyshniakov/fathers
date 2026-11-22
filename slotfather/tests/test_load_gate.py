#!/usr/bin/env python3
"""CPU load admission (fathers#184): sustained CPU load holds grants beside running leases; an idle machine still
starts its first request. load_wait() and memory_gate() alone, then grant(), `status`, the dashboard block, `advise`
and `order --load-gate` on injected queues, with the SLOTFATHER_LOADAVG seam."""
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
from slotfather import settings as sf_settings


import json
import os
import unittest
from typing import Any, cast
from unittest import mock

from test_slotfather import SlotCase

DEFAULTS: dict[str, Any] = dict(sf_config.ORDER_DEFAULTS)


def flat(load: float) -> tuple[float, float, float]:
    return (load, load, load)


class LoadWaitTests(unittest.TestCase):
    """load_wait() alone: 12 cores, 4 slots (3 cores a slot)."""

    def wait(self, averages: tuple[float, float, float] | None, held: int, **settings: float) -> str | None:
        return sf_schedule.load_wait(averages, 12, 4, held, {**DEFAULTS, **settings})

    def test_unleased_load_at_the_cores_holds(self) -> None:
        self.assertIsNone(self.wait(flat(11.9), 0))
        self.assertEqual("CPU load 12.0: 12.0 unleased is at or above 12 cores", self.wait(flat(12), 0))
        self.assertIsNone(self.wait(flat(14.9), 1), "one held slot's 3 cores are its own load")
        self.assertIn("15.0: 12.0 unleased", self.wait(flat(15), 1) or "")

    def test_load_at_one_and_a_half_times_the_cores_holds_whatever_the_leases_run(self) -> None:
        self.assertIsNone(self.wait(flat(17.9), 4), "every slot held: 5.9 unleased")
        self.assertEqual("CPU load 18.0 is at or above 18 (1.5 x 12 cores)", self.wait(flat(18), 4))

    def test_a_burst_does_not_count(self) -> None:
        self.assertIsNone(self.wait((40.0, 6.0, 3.0), 0), "min(load1, load5)")
        self.assertIsNotNone(self.wait((13.0, 40.0, 3.0), 0))

    def test_the_gate_scales_and_0_or_no_load_turns_it_off(self) -> None:
        self.assertIsNone(self.wait(flat(23.9), 0, load_gate=2.0))
        self.assertIsNotNone(self.wait(flat(24), 0, load_gate=2.0))
        self.assertIsNone(self.wait(flat(99), 4, load_gate=0.0))
        self.assertIsNone(self.wait(None, 4))
        self.assertIsNotNone(sf_schedule.load_wait(flat(99), 12, 4, 0, {}), "the default applies without the key")

    def test_memory_gate_starts_the_first_on_an_idle_machine_and_nothing_beside_running_leases(self) -> None:
        load = "CPU load 25.0: 25.0 unleased is at or above 12 cores"
        grants: list[dict[str, Any]] = [{"count": 1, "label": "a"}, {"count": 1, "label": "b"}]
        idle = sf_schedule.memory_gate(grants, [], 0, ("normal", None), DEFAULTS, load=load)
        self.assertEqual([None, load], [why for _, why in idle])
        busy = sf_schedule.memory_gate(grants, [{"count": 1}], 1, ("normal", None), DEFAULTS, load=load)
        self.assertEqual([load, load], [why for _, why in busy])
        free = sf_schedule.memory_gate(grants, [{"count": 1}], 1, ("normal", None), DEFAULTS)
        self.assertEqual([None, None], [why for _, why in free])

    def test_the_default_is_not_written_to_order_json(self) -> None:
        self.assertNotIn("load_gate", sf_settings.order_bytes(sf_config.ORDER_DEFAULTS))
        self.assertEqual(0.0, sf_settings.order_bytes({**sf_config.ORDER_DEFAULTS, "load_gate": 0})["load_gate"])
        self.assertEqual(["load_gate"], sf_settings.order_extension_keys({"load_gate": 2.0}))

    def test_the_dashboard_admits_nothing_beside_running_leases_while_the_load_holds(self) -> None:
        held = [{"count": 1, "label": "running", "state": "held"}]
        why = "CPU load 25.0"
        self.assertEqual(0, sf_board.dashboard_admission([], held, 4, 0, None, None, DEFAULTS, "normal", why))
        self.assertEqual(3, sf_board.dashboard_admission([], held, 4, 0, None, None, DEFAULTS, "normal", None))
        self.assertEqual(4, sf_board.dashboard_admission([], [], 4, 0, None, None, DEFAULTS, "normal", why),
                         "an idle machine starts its first request")


class GrantTests(SlotCase):
    """grant() on an injected queue of 4 slots on 12 cores: live fake holders, the load seam."""

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

    def environment(self, load: str, **env: str) -> dict[str, str]:
        return {**self.env, "SLOTFATHER_LOADAVG": load, **env}

    def grant(self, load: str, **env: str) -> str | None:
        with mock.patch.dict(os.environ, self.environment(load, **env), clear=True):
            return sf_leases.grant(self.state, sf_process.processes())

    def state_of(self, path: _Path) -> str:
        return json.loads(path.read_text())["state"]

    def test_sustained_load_holds_a_request_beside_a_running_lease_and_says_why(self) -> None:
        self.entry("running", 1, "held")
        waiter = self.entry("waiter", 1)
        reason = "CPU load 25.0: 22.0 unleased is at or above 12 cores"
        self.assertEqual(reason, self.grant("25"))
        self.assertEqual("waiting", self.state_of(waiter))
        with mock.patch.dict(os.environ, self.environment("25"), clear=True):
            found: dict[str, Any] = cast(Any, sf_query).status_json(self.state)
            line = next(line for line in sf_query.status_lines(self.state) if "waiter" in line)
            wait = sf_query.long_wait(self.state, waiter) or ""
        item = next(e for e in found["entries"] if e["label"] == "waiter")
        self.assertEqual((reason, None, None), (item["load_wait"], item["memory_wait"], item["est_start"]))
        self.assertIsNone(next(e for e in found["entries"] if e["label"] == "running")["load_wait"])
        self.assertIn(f'load-wait="{reason}"', line)
        self.assertIn(f"est. start unknown; {reason}", wait)
        self.assertIsNone(self.grant("14.9"), "under both thresholds again: it starts")
        self.assertEqual("held", self.state_of(waiter))

    def test_an_idle_machine_starts_its_first_request_under_any_load(self) -> None:
        first, second = self.entry("first", 1), self.entry("second", 1)
        self.assertIn("CPU load 99.0", self.grant("99") or "")
        self.assertEqual(("held", "waiting"), (self.state_of(first), self.state_of(second)))

    def test_the_dashboard_and_advise_report_the_block(self) -> None:
        self.entry("running", 1, "held")
        self.entry("waiter", 1)
        with mock.patch.dict(os.environ, self.environment("25"), clear=True):
            block = sf_board.dashboard_json(sf_board.dashboard_model())
            advice = sf_query.advise()
        self.assertEqual((0, None), (block["admission"], block["memory_block"]))
        self.assertIn("CPU load 25.0", block["load_block"])
        self.assertIn("CPU load 25.0", advice["load_block"])
        with mock.patch.dict(os.environ, self.environment("0"), clear=True):
            block = sf_board.dashboard_json(sf_board.dashboard_model())
            advice = sf_query.advise()
        self.assertEqual((None, None), (block["load_block"], advice["load_block"]))

    def test_order_load_gate_saves_the_multiple_and_the_environment_only_lowers_it(self) -> None:
        self.entry("running", 1, "held")
        waiter = self.entry("waiter", 1)
        saved = self.tool("order", "--load-gate", "3", SLOTFATHER_LOADAVG="99")
        self.assertEqual(0, saved.returncode, saved.stderr)
        self.assertIn("CPU load holds grants beside running leases at 3x the cores unleased or 4.5x in all (saved)",
                      saved.stdout)
        self.assertEqual(3.0, json.loads((self.state / "order.json").read_text())["load_gate"])
        self.assertIsNotNone(self.grant("25", SLOTFATHER_LOAD_GATE="1"), "a client's environment holds sooner")
        self.assertEqual("waiting", self.state_of(waiter))
        self.assertIsNone(self.grant("25", SLOTFATHER_LOAD_GATE="0"), "but cannot turn the gate off")
        self.assertEqual("held", self.state_of(waiter))
        later = self.entry("later", 1)
        self.assertEqual(0, self.tool("order", "--load-gate", "0", SLOTFATHER_LOADAVG="99").returncode)
        self.assertEqual("held", self.state_of(later), "saving the order grants what the gate no longer holds")
        self.assertIn("CPU load holds grants beside running leases never", self.tool("order").stdout)
        self.assertEqual(0, self.tool("order", "--load-gate", "1").returncode)
        self.assertNotIn("load_gate", json.loads((self.state / "order.json").read_text()),
                         "the default is not written: older clients refuse keys they do not know")


if __name__ == "__main__":
    unittest.main()
