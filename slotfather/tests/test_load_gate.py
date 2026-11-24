#!/usr/bin/env python3
"""CPU load admission (fathers#184): sustained CPU load holds grants beside running leases; an idle machine still
starts its first request. Admission alone, then grant(), `status`, the dashboard block, `advise` and
`order --load-gate` on injected queues, with the SLOTFATHER_LOADAVG seam."""
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
import time
import unittest
from typing import Any, cast
from unittest import mock

from test_slotfather import SlotCase

DEFAULTS: dict[str, Any] = dict(sf_config.ORDER_DEFAULTS)
M = 1_800_000_000.0
SINCE = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(M))


def flat(load: float) -> tuple[float, float, float]:
    return (load, load, load)


class LoadWaitTests(unittest.TestCase):
    """Admission load gate: 12 cores, 4 slots (3 cores a slot)."""

    def wait(self, averages: tuple[float, float, float] | None, held: int, **settings: float) -> str | None:
        holders = [{"count": 1, "label": f"h{n}", "state": "held", "since": SINCE, "granted": SINCE}
                   for n in range(held)]
        adm = sf_schedule.Admission([], holders, 4, M, settings={**DEFAULTS, **settings},
                                    host=sf_schedule.Host(averages=averages, cores=12))
        return adm.dashboard_room().load_block

    def test_unleased_load_at_the_cores_holds(self) -> None:
        self.assertIsNone(self.wait(flat(11.9), 0))
        self.assertEqual("CPU load 12.0: 12.0 unleased is at or above 12 cores", self.wait(flat(12), 0))
        self.assertIsNone(self.wait(flat(14.9), 1), "one held slot's 3 cores are its own load")
        self.assertIn("15.0: 12.0 unleased", self.wait(flat(15), 1) or "")

    def test_load_at_one_and_a_half_times_the_cores_holds_whatever_the_leases_run(self) -> None:
        self.assertIsNone(self.wait(flat(17.9), 4), "every slot held: 5.9 unleased")
        self.assertEqual("CPU load 18.0 is at or above 18 (1.5 x 12 cores)", self.wait(flat(18), 4))

    def test_measured_unleased_cpu_caps_loadavg_inflation_without_disabling_peak_gate(self) -> None:
        # Linux loadavg counts leased runnable/I/O-wait threads: 8 cores, 6 slots, 4 held, load 11.5 -> 6.17 unleased,
        # or load 11.5 with 1 held -> 10.17 unleased >= 8, while real unleased ps CPU is only 0.08 cores.
        holders = [{"count": 1, "label": "h0", "state": "held", "since": SINCE, "granted": SINCE}]
        called = 0

        def quiet_unleased() -> float:
            nonlocal called
            called += 1
            return 0.08

        # Below peak (1.5 * 8 = 12), loadavg-only would falsely blame unleased (10.17 >= 8); measured 0.08 clears it.
        adm = sf_schedule.Admission([], holders, 6, M, settings=DEFAULTS,
                                    host=sf_schedule.Host(averages=flat(11.5), cores=8, unleased_cpu=quiet_unleased))
        self.assertIsNone(adm.dashboard_room().load_block)
        self.assertEqual(1, called, "unleased_cpu is memoized on Host and called once")

        # Above peak (18.1 >= 12), LOAD_PEAK still holds, without falsely blaming unleased.
        adm_peak = sf_schedule.Admission([], holders, 6, M, settings=DEFAULTS,
                                         host=sf_schedule.Host(averages=flat(18.1), cores=8, unleased_cpu=0.08))
        self.assertEqual("CPU load 18.1 is at or above 12 (1.5 x 8 cores)", adm_peak.dashboard_room().load_block)

        # Real unleased CPU above the gate still reports unleased load.
        adm_real = sf_schedule.Admission([], holders, 6, M, settings=DEFAULTS,
                                         host=sf_schedule.Host(averages=flat(11.5), cores=8, unleased_cpu=9.0))
        self.assertEqual("CPU load 11.5: 9.0 unleased is at or above 8 cores", adm_real.dashboard_room().load_block)

        # query.size() caps unleased_load by unleased_cpu when provided.
        sized = sf_query.size(6, 8, 6, flat(18.1), 4, unleased_cpu=0.08)
        self.assertEqual(0.08, sized["unleased_load"])
        self.assertEqual(12.77, sf_query.size(6, 8, 6, flat(18.1), 4)["unleased_load"])

        # Below the unleased gate, the lazy reader is never invoked.
        never_called = 0

        def fail_if_called() -> float:
            nonlocal never_called
            never_called += 1
            return 0.0

        adm_calm = sf_schedule.Admission([], holders, 6, M, settings=DEFAULTS,
                                         host=sf_schedule.Host(averages=flat(2.0), cores=8, unleased_cpu=fail_if_called))
        self.assertIsNone(adm_calm.dashboard_room().load_block)
        self.assertEqual(0, never_called)

    def test_a_burst_does_not_count(self) -> None:
        self.assertIsNone(self.wait((40.0, 6.0, 3.0), 0), "min(load1, load5)")
        self.assertIsNotNone(self.wait((13.0, 40.0, 3.0), 0))

    def test_the_gate_scales_and_0_or_no_load_turns_it_off(self) -> None:
        self.assertIsNone(self.wait(flat(23.9), 0, load_gate=2.0))
        self.assertIsNotNone(self.wait(flat(24), 0, load_gate=2.0))
        self.assertIsNone(self.wait(flat(99), 4, load_gate=0.0))
        self.assertIsNone(self.wait(None, 4))
        without_key = {k: v for k, v in DEFAULTS.items() if k != "load_gate"}
        adm = sf_schedule.Admission([], [], 4, M, settings=without_key,
                                    host=sf_schedule.Host(averages=flat(99), cores=12))
        self.assertIsNotNone(adm.dashboard_room().load_block, "the default applies without the key")

    def test_admission_starts_the_first_on_an_idle_machine_and_nothing_beside_running_leases(self) -> None:
        grants: list[dict[str, Any]] = [
            {"count": 1, "label": "a", "since": SINCE, "state": "waiting"},
            {"count": 1, "label": "b", "since": SINCE, "state": "waiting"},
        ]
        hot = sf_schedule.Host(averages=flat(25.0), cores=12)
        idle = sf_schedule.Admission(grants, [], 4, M, settings=DEFAULTS, host=hot).plan_grants()
        self.assertEqual(["a"], [s.entry["label"] for s in idle.started])
        self.assertEqual([("b", "load")], [(e["label"], b.kind) for e, b in idle.gated])
        self.assertEqual("CPU load 25.0: 25.0 unleased is at or above 12 cores", idle.gated[0][1].reason)

        held = [{"count": 1, "label": "running", "since": SINCE, "granted": SINCE, "state": "held"}]
        busy = sf_schedule.Admission(grants, held, 4, M, settings=DEFAULTS, host=hot).plan_grants()
        self.assertEqual([], busy.started)
        self.assertEqual([("a", "load"), ("b", "load")], [(e["label"], b.kind) for e, b in busy.gated])

        free = sf_schedule.Admission(grants, held, 4, M, settings=DEFAULTS).plan_grants()
        self.assertEqual(["a", "b"], [s.entry["label"] for s in free.started])
        self.assertEqual([], free.gated)

    def test_the_default_is_not_written_to_order_json(self) -> None:
        self.assertNotIn("load_gate", sf_settings.order_bytes(sf_config.ORDER_DEFAULTS))
        self.assertEqual(0.0, sf_settings.order_bytes({**sf_config.ORDER_DEFAULTS, "load_gate": 0})["load_gate"])
        self.assertEqual(["load_gate"], sf_settings.order_extension_keys({"load_gate": 2.0}))

    def test_the_dashboard_admits_nothing_beside_running_leases_while_the_load_holds(self) -> None:
        held = [{"count": 1, "label": "running", "since": SINCE, "granted": SINCE, "state": "held"}]
        hot = sf_schedule.Host(averages=flat(25.0), cores=12)
        calm = sf_schedule.Host(averages=flat(0.0), cores=12)
        self.assertEqual(0, sf_schedule.Admission([], held, 4, M, settings=DEFAULTS, host=hot).dashboard_room().admission)
        self.assertEqual(3, sf_schedule.Admission([], held, 4, M, settings=DEFAULTS, host=calm).dashboard_room().admission)
        self.assertEqual(4, sf_schedule.Admission([], [], 4, M, settings=DEFAULTS, host=hot).dashboard_room().admission,
                         "an idle machine starts its first request")
        # fathers#211: load_block stays untangled from memory_block around warning_admits and idle-host waiters.
        warn_hot = sf_schedule.Host(memory=("warning", "level 2"), averages=flat(25.0), cores=12)
        self.assertEqual(0, sf_schedule.Admission([], held, 4, M, settings=DEFAULTS, host=warn_hot).dashboard_room().admission,
                         "warning_admits never admits beside running leases while load_block holds")
        waiter = {"count": 1, "label": "waiting", "cmd": "work", "since": SINCE, "state": "waiting"}
        self.assertEqual(0, sf_schedule.Admission([waiter], [], 4, M, settings=DEFAULTS, host=hot).dashboard_room().admission,
                         "an earlier normal waiter consumes the idle-host exemption under load_block")
        batch = dict(waiter, priority="batch")
        self.assertEqual(4, sf_schedule.Admission([batch], [], 4, M, settings=DEFAULTS, host=hot).dashboard_room().admission,
                         "a generic normal probe ranks ahead of a batch waiter on an idle host under load_block")


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

    def test_settings_cli_refuses_load_gate_without_mutating_order_json(self) -> None:
        held = [{"count": 2, "label": "h", "since": SINCE, "granted": SINCE, "state": "held"}]
        self.assertIsNone(sf_schedule.Admission(
            [], held, 8, M, settings={**DEFAULTS, "load_gate": 1.0},
            host=sf_schedule.Host(averages=(50.0, float("nan"), 5.0), cores=12),
        ).dashboard_room().load_block)
        self.assertIsNone(sf_schedule.Admission(
            [], held, 8, M, settings={**DEFAULTS, "load_gate": 1.0},
            host=sf_schedule.Host(averages=(50.0, 50.0, 50.0), cores=0),
        ).dashboard_room().load_block)
        self.assertIsNone(sf_schedule.Admission(
            [], held, 0, M, settings={**DEFAULTS, "load_gate": 1.0},
            host=sf_schedule.Host(averages=(50.0, 50.0, 50.0), cores=12),
        ).dashboard_room().load_block)
        for argv in (("settings", "check", "order.load_gate", "2", "--json"),
                     ("settings", "set", "order.load_gate", "2", "--json"),
                     ("settings", "reset", "order.load_gate", "--json")):
            res = self.tool(*argv)
            self.assertEqual(2, res.returncode, res.stderr)
            self.assertFalse(json.loads(res.stdout)["ok"])
        self.assertFalse((self.state / "order.json").exists())


if __name__ == "__main__":
    unittest.main()
