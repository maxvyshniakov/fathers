#!/usr/bin/env python3
"""Memory admission by pressure level: normal admits as the order allows; a warning (macOS level 2, Linux PSI some)
admits one 1-slot lease at a time beside running ones, `order --warning-admits 0` none; critical, a swap floor or
little memory stops; an idle machine starts its first request. grant() on injected queues, with the
SLOTFATHER_MEMORY_FILE seam, and memory_gate() alone."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import config as sf_config
from slotfather import leases as sf_leases
from slotfather import platform as sf_platform
from slotfather import query as sf_query
from slotfather import schedule as sf_schedule


import json
import os
import unittest
from typing import Any, cast
from unittest import mock

from test_linux import PROC
from test_slotfather import SlotCase

WARNING = sf_schedule.WARNING_WAIT


def clean_environment(**values: str) -> dict[str, str]:
    """os.environ without SLOTFATHER_ variables, plus `values`."""
    return {**{k: v for k, v in os.environ.items() if not k.startswith("SLOTFATHER_")}, **values}


class GateTests(unittest.TestCase):
    """memory_gate() alone: which of plan()'s grants memory lets start."""

    def gate(self, grants: list[dict[str, Any]], held: list[dict[str, Any]], used: int, memory: tuple[str, str | None],
             admits: float = 1.0) -> list[str | None]:
        return [why for _, why in sf_schedule.memory_gate(grants, held, used, memory, {"warning_admits": admits})]

    def test_normal_admits_every_grant(self):
        grants = [{"count": 1}, {"count": 2}, {"count": 4, "all": True}]
        self.assertEqual([None, None, None], self.gate(grants, [{"count": 1}], 1, ("normal", None)))

    def test_warning_admits_one_1_slot_grant_and_refuses_larger_ones(self):
        grants = [{"count": 2}, {"count": 1}, {"count": 1}, {"count": 1, "all": True}]
        self.assertEqual([WARNING, None, WARNING, WARNING], self.gate(grants, [{"count": 1}], 1, ("warning", "w")))

    def test_warning_holds_every_grant_while_a_pressed_lease_runs(self):
        held = [{"count": 1, sf_schedule.PRESSED: True}]
        self.assertEqual([WARNING], self.gate([{"count": 1}], held, 1, ("warning", "w")))
        self.assertEqual([None], self.gate([{"count": 1}], held, 1, ("normal", None)), "normal again: as before")

    def test_short_memory_and_a_disabled_allowance_stop_everything_beside_running_leases(self):
        self.assertEqual(["s", "s"], self.gate([{"count": 1}, {"count": 2}], [{"count": 1}], 1, ("short", "s")))
        self.assertEqual(["w"], self.gate([{"count": 1}], [{"count": 1}], 1, ("warning", "w"), admits=0.0))

    def test_an_idle_machine_starts_its_first_request_whatever_memory_says(self):
        for memory in (("short", "s"), ("warning", "w")):
            with self.subTest(memory=memory[0]):
                found = self.gate([{"count": 4, "all": True}, {"count": 1}], [], 0, memory)
                self.assertEqual([None, memory[1] if memory[0] == "short" else WARNING], found)


class DashboardAdmissionTests(unittest.TestCase):
    def test_the_dashboard_admits_one_slot_under_warning_and_none_beside_a_pressed_lease(self):
        from slotfather import dashboard as sf_dashboard
        settings = dict(sf_config.ORDER_DEFAULTS)
        held = [{"count": 1, "label": "running", "state": "held"}]
        self.assertEqual(1, sf_dashboard.dashboard_admission([], held, 4, 0, WARNING, None, settings, "warning"))
        self.assertEqual(0, sf_dashboard.dashboard_admission([], [{**held[0], sf_schedule.PRESSED: True}], 4, 0,
                                                             WARNING, None, settings, "warning"))
        self.assertEqual(0, sf_dashboard.dashboard_admission([], held, 4, 0, "level 4", None, settings, "short"))
        self.assertEqual(0, sf_dashboard.dashboard_admission([], held, 4, 0, "level 2", None,
                                                             {**settings, "warning_admits": 0.0}, "warning"))
        self.assertEqual(3, sf_dashboard.dashboard_admission([], held, 4, 0, None, None, settings, "normal"))


class GrantTests(SlotCase):
    """grant() on an injected queue: live fake holders, the memory seam, the shared order settings."""

    def setUp(self):
        super().setUp()
        self.queue = self.state / "queue"
        self.queue.mkdir(parents=True)
        self.sequence = 0

    def entry(self, label: str, count: int, state: str = "waiting", **extra: Any) -> _Path:
        pid, start = self.holder()
        self.sequence += 1
        path = self.queue / f"{self.sequence:010d}-{pid}.json"
        path.write_text(json.dumps({"pid": pid, "start": start, "count": count, "label": label, "cmd": "fake",
                                    "since": "2026-11-12T00:00:00Z", "state": state,
                                    **({"granted": "2026-11-12T00:00:00Z"} if state == "held" else {}), **extra}))
        return path

    def grant(self, memory: str, **env: str) -> str | None:
        self.memory.write_text(memory + "\n")
        with mock.patch.dict(os.environ, {**self.env, **env}, clear=True):
            return sf_leases.grant(self.state, sf_platform.processes())

    def state_of(self, path: _Path) -> str:
        return json.loads(path.read_text())["state"]

    def test_normal_admits_every_request_that_fits(self):
        self.entry("running", 1, "held")
        waiting = [self.entry("one", 1), self.entry("two", 2)]
        self.assertIsNone(self.grant("8000 1"))
        self.assertEqual(["held"] * 2, [self.state_of(p) for p in waiting])
        self.assertFalse(any(sf_schedule.PRESSED in json.loads(p.read_text()) for p in waiting))

    def test_warning_admits_one_1_slot_lease_and_holds_the_second_until_it_releases(self):
        self.entry("running", 1, "held")
        first, second = self.entry("first", 1), self.entry("second", 1)
        self.assertEqual(WARNING, self.grant("8000 2"))
        self.assertEqual(("held", "waiting"), (self.state_of(first), self.state_of(second)))
        self.assertTrue(json.loads(first.read_text())[sf_schedule.PRESSED])
        self.assertEqual(WARNING, self.grant("8000 2"), "the next look holds it too")
        with mock.patch.dict(os.environ, self.env, clear=True):
            found: dict[str, Any] = cast(Any, sf_query).status_json(self.state)
        entries: list[dict[str, Any]] = found["entries"]
        waiter = next(e for e in entries if e["label"] == "second")
        self.assertEqual((WARNING, None), (waiter["memory_wait"], waiter["est_start"]))
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertIn(f"est. start unknown; {WARNING}", sf_query.long_wait(self.state, second) or "")
            self.assertEqual("warning", cast(Any, sf_query).advise()["memory_level"])
        first.unlink()  # the first lease granted under warning released
        self.assertIsNone(self.grant("8000 2"))
        self.assertEqual("held", self.state_of(second))

    def test_warning_lets_the_second_start_once_the_level_is_normal_again(self):
        self.entry("running", 1, "held")
        self.entry("first", 1)
        second = self.entry("second", 1)
        self.grant("8000 2")
        self.assertEqual("waiting", self.state_of(second))
        self.assertIsNone(self.grant("8000 1"))
        self.assertEqual("held", self.state_of(second))
        self.assertNotIn(sf_schedule.PRESSED, json.loads(second.read_text()))

    def test_warning_refuses_two_slots_and_all(self):
        self.entry("running", 1, "held")
        pair, every = self.entry("pair", 2), self.entry("every", 4, all=True)
        self.assertEqual(WARNING, self.grant("8000 2"))
        self.assertEqual(("waiting", "waiting"), (self.state_of(pair), self.state_of(every)))
        lone = self.entry("lone", 1)
        self.grant("8000 2")
        self.assertEqual(("waiting", "held"), (self.state_of(pair), self.state_of(lone)),
                         "a 1-slot request behind a refused larger one starts under warning")

    def test_critical_and_low_swap_stop_every_request(self):
        for memory in ("8000 4", "100 1", "100 2"):
            with self.subTest(memory=memory):
                for path in self.queue.iterdir():
                    path.unlink()
                self.entry("running", 1, "held")
                lone = self.entry("lone", 1)
                reason = self.grant(memory)
                self.assertEqual("waiting", self.state_of(lone))
                self.assertIn("critical" if memory == "8000 4" else "free swap 100 MB", reason or "")

    def test_an_idle_machine_starts_its_first_request_under_any_level(self):
        big, small = self.queue / "none", self.queue / "none"
        for memory in ("8000 4", "100 1", "8000 2"):
            with self.subTest(memory=memory):
                for path in self.queue.iterdir():
                    path.unlink()
                big, small = self.entry("big", 4, all=True), self.entry("small", 1)
                self.grant(memory)
                self.assertEqual(("held", "waiting"), (self.state_of(big), self.state_of(small)))
        big.unlink()
        self.grant("8000 2")
        self.assertEqual("held", self.state_of(small), "the idle machine starts it as the first under warning")
        later = self.entry("later", 1)
        self.grant("8000 2")
        self.assertEqual("waiting", self.state_of(later), "the idle machine's first lease is the one under warning")

    def test_warning_admits_0_restores_the_old_stop(self):
        self.entry("running", 1, "held")
        lone = self.entry("lone", 1)
        self.memory.write_text("8000 2\n")  # saving the order grants too
        self.assertEqual(0, self.tool("order", "--warning-admits", "0").returncode)
        self.assertEqual({"warning_admits": 0.0}.items() <= json.loads((self.state / "order.json").read_text()).items(),
                         True)
        self.assertEqual("memory pressure level 2 (warning)", self.grant("8000 2"))
        self.assertEqual("waiting", self.state_of(lone))
        self.assertIn("memory pressure warning admits nothing", self.tool("order").stdout)
        self.memory.write_text("8000 4\n")
        self.assertEqual(0, self.tool("order", "--warning-admits", "1").returncode)
        self.assertNotIn("warning_admits", json.loads((self.state / "order.json").read_text()),
                         "the default is not written: older clients refuse keys they do not know")
        self.assertEqual("memory pressure level 2 (warning)", self.grant("8000 2", SLOTFATHER_WARNING_ADMITS="0"),
                         "a client's environment can turn it off")
        self.assertIsNone(self.grant("8000 2", SLOTFATHER_WARNING_ADMITS="5"), "but not loosen it")
        self.assertEqual("held", self.state_of(lone))

    def test_linux_psi_some_is_a_warning_and_full_stops(self):
        env = {k: v for k, v in self.env.items() if k != "SLOTFATHER_MEMORY_FILE"}
        self.entry("running", 1, "held")
        first, second = self.entry("first", 1), self.entry("second", 1)
        with mock.patch.dict(os.environ, clean_environment(**{**env, "SLOTFATHER_PROC": str(PROC / "thrashing")}),
                             clear=True):
            self.assertIn("PSI full avg10 12%", sf_leases.grant(self.state, sf_platform.processes()) or "")
        self.assertEqual("waiting", self.state_of(first))
        with mock.patch.dict(os.environ, clean_environment(**{**env, "SLOTFATHER_PROC": str(PROC / "stalled")}),
                             clear=True):
            self.assertEqual(WARNING, sf_leases.grant(self.state, sf_platform.processes()))
        self.assertEqual(("held", "waiting"), (self.state_of(first), self.state_of(second)))


if __name__ == "__main__":
    unittest.main()
