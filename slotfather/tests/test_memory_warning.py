#!/usr/bin/env python3
"""Memory admission by pressure level: normal admits as the order allows; a warning (macOS level 2, Linux PSI some)
admits one 1-slot lease at a time beside running ones, `order --warning-admits 0` none; critical, a swap floor or
little memory stops; an idle machine starts its first request. grant() on injected queues, with the
SLOTFATHER_MEMORY_FILE seam, and Admission alone."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import config as sf_config
from slotfather import entry as sf_entry
from slotfather import leases as sf_leases
from slotfather import process as sf_process
from slotfather import query as sf_query
from slotfather import schedule as sf_schedule


import json
import os
import time
import unittest
from typing import Any, cast
from unittest import mock

from test_linux import PROC
from test_slotfather import SlotCase

WARNING = sf_schedule.WARNING_WAIT
DEFAULTS: dict[str, Any] = dict(sf_config.ORDER_DEFAULTS)
M = 1_800_000_000.0
SINCE = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(M))


def clean_environment(**values: str) -> dict[str, str]:
    """os.environ without SLOTFATHER_ variables, plus `values`."""
    return {**{k: v for k, v in os.environ.items() if not k.startswith("SLOTFATHER_")}, **values}


def w(label: str, count: int, **extra: Any) -> dict[str, Any]:
    return {"label": label, "count": count, "cmd": "work", "since": SINCE, "state": "waiting", **extra}


def h(label: str, count: int, **extra: Any) -> dict[str, Any]:
    return {"label": label, "count": count, "cmd": "work", "since": SINCE, "granted": SINCE, "state": "held", **extra}


class GateTests(unittest.TestCase):
    """Admission.plan_grants() under memory pressure and peak-memory budgets."""

    def outcome(
        self,
        waiters: list[dict[str, Any]],
        held: list[dict[str, Any]],
        slots: int,
        memory: tuple[str, str | None],
        admits: float = 1.0,
    ) -> sf_schedule.GrantOutcome:
        return sf_schedule.Admission(
            waiters,
            held,
            slots,
            M,
            settings={**DEFAULTS, "warning_admits": admits},
            host=sf_schedule.Host(memory=memory),
        ).plan_grants()

    def test_normal_admits_every_grant(self) -> None:
        grants = [w("a", 1), w("b", 2)]
        res = self.outcome(grants, [h("r", 1)], 4, ("normal", None))
        self.assertEqual(["a", "b"], [s.entry["label"] for s in res.started])
        self.assertEqual([], res.gated)

    def test_warning_admits_one_1_slot_grant_and_refuses_larger_ones(self) -> None:
        grants = [w("two", 2), w("one", 1), w("second", 1)]
        res = self.outcome(grants, [h("r", 1)], 6, ("warning", "w"))
        self.assertEqual(["one"], [s.entry["label"] for s in res.started])
        self.assertTrue(res.started[0].pressed)
        self.assertEqual([("two", WARNING), ("second", WARNING)], [(e["label"], b.reason) for e, b in res.gated])

    def test_warning_holds_every_grant_while_a_pressed_lease_runs(self) -> None:
        held = [h("r", 1, **{sf_entry.PRESSED: True})]
        warn = self.outcome([w("a", 1)], held, 4, ("warning", "w"))
        self.assertEqual(([], [("a", WARNING)]), (warn.started, [(e["label"], b.reason) for e, b in warn.gated]))
        norm = self.outcome([w("a", 1)], held, 4, ("normal", None))
        self.assertEqual((["a"], []), ([s.entry["label"] for s in norm.started], norm.gated))

    def test_short_memory_and_a_disabled_allowance_stop_everything_beside_running_leases(self) -> None:
        short = self.outcome([w("a", 1), w("b", 2)], [h("r", 1)], 4, ("short", "s"))
        self.assertEqual(([], [("a", "s"), ("b", "s")]), (short.started, [(e["label"], b.reason) for e, b in short.gated]))
        off = self.outcome([w("a", 1)], [h("r", 1)], 4, ("warning", "w"), admits=0.0)
        self.assertEqual(([], [("a", "w")]), (off.started, [(e["label"], b.reason) for e, b in off.gated]))

    def test_an_idle_machine_starts_its_first_request_whatever_memory_says(self) -> None:
        for memory in (("short", "s"), ("warning", "w")):
            with self.subTest(memory=memory[0]):
                whole = self.outcome([w("big", 4, all=True)], [], 4, memory)
                self.assertEqual((["big"], []), ([s.entry["label"] for s in whole.started], whole.gated))
                pair = self.outcome([w("first", 1), w("second", 1)], [], 4, memory)
                self.assertEqual(["first"], [s.entry["label"] for s in pair.started])
                expected_why = memory[1] if memory[0] == "short" else WARNING
                self.assertEqual([("second", expected_why)], [(e["label"], b.reason) for e, b in pair.gated])

    def test_expected_peak_memory_defers_grant_although_slots_are_free(self) -> None:
        from slotfather import estimates as sf_estimates
        from slotfather import settings as sf_settings
        est = sf_estimates.Estimates()
        est.add_memory("lane-1", "verify.sh", 9000.0)
        est.add_memory("lane-2", "verify.sh", 9500.0)
        # 16 GiB RAM * 0.85 = 13926.4 MB; one 9500 MB lease held, second 9500 MB exceeds budget
        held = [h("lane-3", 1, cmd="verify.sh")]
        waiters = [w("lane-4", 1, cmd="verify.sh")]
        host = sf_schedule.Host(ram_mb=16384.0)

        def mem_est(entry: sf_schedule.Entry) -> float | None:
            return est.peak_mb(entry.get("label"), entry.get("cmd"))
        gated = sf_schedule.Admission(
            waiters, held, 4, M,
            settings={**DEFAULTS, "warning_admits": 1.0, "memory_share": 0.85, "memory_default_mb": 2048.0},
            host=host, memory_estimate=mem_est,
        ).plan_grants()
        self.assertEqual([], gated.started)
        self.assertIn("expected memory", gated.reason or "")
        # Unknown family uses configurable default (2048 MB): 9500 + 2048 = 11548 <= 13926 -> admitted
        unknown_waiters = [w("brand-new", 1, cmd="echo hi")]
        admitted = sf_schedule.Admission(
            unknown_waiters, held, 4, M,
            settings={**DEFAULTS, "warning_admits": 1.0, "memory_share": 0.85, "memory_default_mb": 2048.0},
            host=host, memory_estimate=mem_est,
        ).plan_grants()
        self.assertEqual(["brand-new"], [s.entry["label"] for s in admitted.started])
        # Raising default_mb to 6000 MB causes unknown family to exceed budget too
        deferred = sf_schedule.Admission(
            unknown_waiters, held, 4, M,
            settings={**DEFAULTS, "warning_admits": 1.0, "memory_share": 0.85, "memory_default_mb": 6000.0},
            host=host, memory_estimate=mem_est,
        ).plan_grants()
        self.assertEqual([], deferred.started)
        self.assertIsNotNone(deferred.reason)
        # order.json bytes stay unchanged when memory knobs are at defaults
        self.assertNotIn("memory_share", sf_settings.order_bytes(sf_config.ORDER_DEFAULTS))
        self.assertNotIn("memory_default_mb", sf_settings.order_bytes(sf_config.ORDER_DEFAULTS))
        custom = sf_settings.order_bytes({**sf_config.ORDER_DEFAULTS, "memory_share": 0.75})
        self.assertEqual(0.75, custom["memory_share"])

    def test_replay_2026_11_10_reports_memory_deferrals(self) -> None:
        from test_slotfather import ROOT
        from test_usage import load
        replay_mod = cast(Any, load(str(ROOT / "tools" / "replay.py"), "slotfather_replay_mem_test"))
        fixture = ROOT / "tests" / "fixtures" / "replay-2026-11-10"
        now = sf_config.seconds("2026-11-10T20:51:00Z")
        assert now is not None
        reqs = replay_mod.requests(fixture, now)
        # Tight 16 GiB RAM budget with memory_share=0.5 should defer heavy concurrent runs
        policy = {"order": "backfill", "env": replay_mod.WINDOW, "settings": {**replay_mod.RESERVED, "memory_share": 0.5},
                  "step": 300, "memory_ram_mb": 16384.0}
        sim = replay_mod.Replay(reqs, 6, policy)
        results = sim.simulate()
        start = min(r["arrival"] for r in reqs if r["source"] == "history")
        until = max(r["arrival"] for r in reqs)
        measured = replay_mod.measure(reqs, results, start, until, sim)
        self.assertIn("memory_deferred", measured)
        self.assertGreater(measured["memory_deferred"], 0)


class DashboardAdmissionTests(unittest.TestCase):
    def test_the_dashboard_admits_one_slot_under_warning_and_none_beside_a_pressed_lease(self) -> None:
        held = [h("running", 1)]
        warn = sf_schedule.Host(memory=("warning", "level 2"))
        short = sf_schedule.Host(memory=("short", "level 4"))
        self.assertEqual(1, sf_schedule.Admission([], held, 4, M, settings=DEFAULTS, host=warn).dashboard_room().admission)
        self.assertEqual(0, sf_schedule.Admission([], [{**held[0], sf_entry.PRESSED: True}], 4, M,
                                                  settings=DEFAULTS, host=warn).dashboard_room().admission)
        self.assertEqual(0, sf_schedule.Admission([], held, 4, M, settings=DEFAULTS, host=short).dashboard_room().admission)
        self.assertEqual(0, sf_schedule.Admission([], held, 4, M, settings={**DEFAULTS, "warning_admits": 0.0},
                                                  host=warn).dashboard_room().admission)
        self.assertEqual(3, sf_schedule.Admission([], held, 4, M, settings=DEFAULTS).dashboard_room().admission)
        # fathers#211: under warning, an earlier 1-slot waiter consumes the warning allowance; a refused 2-slot one
        # or lower-priority batch waiter does not.
        one = w("one", 1)
        two = w("two", 2)
        batch = w("low", 1, priority="batch")
        self.assertEqual(0, sf_schedule.Admission([one], held, 4, M, settings=DEFAULTS, host=warn).dashboard_room().admission)
        self.assertEqual(1, sf_schedule.Admission([two], held, 4, M, settings=DEFAULTS, host=warn).dashboard_room().admission)
        self.assertEqual(1, sf_schedule.Admission([batch], held, 4, M, settings=DEFAULTS, host=warn).dashboard_room().admission)


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
            return sf_leases.grant(self.state, sf_process.processes())

    def state_of(self, path: _Path) -> str:
        return json.loads(path.read_text())["state"]

    def test_normal_admits_every_request_that_fits(self):
        self.entry("running", 1, "held")
        waiting = [self.entry("one", 1), self.entry("two", 2)]
        self.assertIsNone(self.grant("8000 1"))
        self.assertEqual(["held"] * 2, [self.state_of(p) for p in waiting])
        self.assertFalse(any(sf_entry.PRESSED in json.loads(p.read_text()) for p in waiting))

    def test_warning_admits_one_1_slot_lease_and_holds_the_second_until_it_releases(self):
        self.entry("running", 1, "held")
        first, second = self.entry("first", 1), self.entry("second", 1)
        self.assertEqual(WARNING, self.grant("8000 2"))
        self.assertEqual(("held", "waiting"), (self.state_of(first), self.state_of(second)))
        self.assertTrue(json.loads(first.read_text())[sf_entry.PRESSED])
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
        self.assertNotIn(sf_entry.PRESSED, json.loads(second.read_text()))

    def test_warning_refuses_two_slots_and_all(self):
        self.entry("running", 1, "held")
        pair, every = self.entry("pair", 2), self.entry("every", 4, all=True)
        self.assertEqual(WARNING, self.grant("8000 2"))
        self.assertEqual(("waiting", "waiting"), (self.state_of(pair), self.state_of(every)))
        lone = self.entry("lone", 1)
        self.grant("8000 2")
        self.assertEqual(("waiting", "held"), (self.state_of(pair), self.state_of(lone)),
                         "a 1-slot request behind a refused larger one starts under warning")

    def test_expected_matches_grant_for_range_requests_under_warning(self) -> None:
        """fathers#211: query.expected() routes granted_counts through Admission's resource gate, so a -n 1-2 request
        planned for 2 free slots under warning reports memory_wait and does not falsely consume the allowance."""
        self.entry("running", 1, "held")
        ranged = self.entry("ranged", 1, min_count=1, max_count=2)
        lone = self.entry("lone", 1)
        self.memory.write_text("8000 2\n")
        with mock.patch.dict(os.environ, self.env, clear=True):
            snap: dict[str, Any] = cast(Any, sf_query).status_json(self.state)
        by_label = {e["label"]: e for e in snap["entries"]}
        self.assertEqual((WARNING, None), (by_label["ranged"]["memory_wait"], by_label["ranged"]["est_start"]))
        self.assertIsNone(by_label["lone"]["memory_wait"])
        self.assertEqual(WARNING, self.grant("8000 2"))
        self.assertEqual(("waiting", "held"), (self.state_of(ranged), self.state_of(lone)))

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
            self.assertIn("PSI full avg10 12%", sf_leases.grant(self.state, sf_process.processes()) or "")
        self.assertEqual("waiting", self.state_of(first))
        with mock.patch.dict(os.environ, clean_environment(**{**env, "SLOTFATHER_PROC": str(PROC / "stalled")}),
                             clear=True):
            self.assertEqual(WARNING, sf_leases.grant(self.state, sf_process.processes()))
        self.assertEqual(("held", "waiting"), (self.state_of(first), self.state_of(second)))


if __name__ == "__main__":
    unittest.main()
