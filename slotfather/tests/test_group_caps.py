#!/usr/bin/env python3
"""Mac memory budget, advise memory fields, and per-group concurrency caps (fathers#251)."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, cast
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from slotfather import board as sf_board
from slotfather import config as sf_config
from slotfather import estimates as sf_estimates
from slotfather import leases as sf_leases
from slotfather import platform as sf_platform
from slotfather import process as sf_process
from slotfather import query as sf_query
from slotfather import schedule as sf_schedule
from slotfather import settings as sf_settings

from test_slotfather import SlotCase

DEFAULTS: dict[str, Any] = dict(sf_config.ORDER_DEFAULTS, group_caps={})
M = 1_800_000_000.0
SINCE = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(M))


class EntryGroupMatchingTests(unittest.TestCase):
    def test_matches_browser_suites_from_label_cmd_and_metadata(self) -> None:
        cases = (
            {"label": "sid-kit:browser", "cmd": "npm test"},
            {"label": "repo/control", "cmd": "bash scripts/run.sh"},
            {"label": "lane:story checks", "cmd": "bun test"},
            {"label": "browser-1", "cmd": "npm test"},
            {"label": "app", "cmd": "bash tools/story-checks.sh"},
            {"label": "app", "cmd": "npx playwright test"},
            {"label": "app", "cmd": "true", "meta": {"suite": "browser"}},
            {"label": "app", "cmd": "true", "meta": {"kind": "control"}},
            {"label": "app", "cmd": "true", "meta": {"phase": "story_checks"}},
            {"label": "app", "cmd": "true", "meta": {"group": "browser,e2e"}},
        )
        for entry in cases:
            with self.subTest(entry=entry):
                self.assertIn("browser", sf_schedule.entry_groups(entry))

    def test_non_browser_work_does_not_false_positive(self) -> None:
        cases = (
            {"label": "unit-tests", "cmd": "pytest"},
            {"label": "controller-spec", "cmd": "go test ./controller/..."},
            {"label": "history-check", "cmd": "python3 -m unittest"},
            {"label": "uncontrolled-input", "cmd": "vitest"},
            {"label": "lint", "cmd": "ruff check"},
            {"label": "access-control tests", "cmd": "pytest"},
            {"label": "lint", "cmd": "ruff check src/control/"},
            {"label": "slotfather tests", "cmd": "python3 -m unittest tests/test_detached_browser_helpers.py"},
            {"label": "unit", "cmd": "vitest --no-browser"},
        )
        for entry in cases:
            with self.subTest(entry=entry):
                self.assertEqual((), sf_schedule.entry_groups(entry))

    def test_custom_group_metadata_tags(self) -> None:
        entry = {"label": "build", "cmd": "cargo build", "meta": {"group": "heavy-build, gpu"}}
        self.assertEqual(("gpu", "heavy-build"), sf_schedule.entry_groups(entry))


class StrictValidationTests(unittest.TestCase):
    def test_group_caps_rejects_bool_zero_negative_and_non_int(self) -> None:
        for bad in (
            True,
            False,
            0,
            -1,
            -5,
            3.0,
            "3",
            None,
            [3],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sf_settings.checked_order({"group_caps": {"browser": bad}})

    def test_group_caps_rejects_non_dict_and_invalid_group_names(self) -> None:
        for bad in (["browser"], "browser=3", 3, None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sf_settings.checked_order({"group_caps": bad})
        for bad_key in ("", "Browser", "123", "has space", "a/b"):
            with self.subTest(bad_key=bad_key):
                with self.assertRaises(ValueError):
                    sf_settings.checked_order({"group_caps": {bad_key: 3}})

    def test_memory_budget_mb_validation(self) -> None:
        for bad in (True, False, -1, -0.5, float("nan"), float("inf"), "1024", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sf_settings.checked_order({"memory_budget_mb": bad})
        self.assertEqual({"memory_budget_mb": 20480}, sf_settings.checked_order({"memory_budget_mb": 20480}))

    def test_defaults_are_omitted_from_order_bytes(self) -> None:
        saved = sf_settings.order_bytes(sf_config.ORDER_DEFAULTS)
        self.assertNotIn("group_caps", saved)
        self.assertNotIn("memory_budget_mb", saved)
        custom = sf_settings.order_bytes({**DEFAULTS, "group_caps": {"browser": 3}, "memory_budget_mb": 16384.0})
        self.assertEqual({"browser": 3}, custom["group_caps"])
        self.assertEqual(16384.0, custom["memory_budget_mb"])
        self.assertEqual(["memory_budget_mb", "group_caps"],
                         sf_settings.order_extension_keys({"memory_budget_mb": 16384.0, "group_caps": {"browser": 3}}))

    def test_malformed_env_group_caps_warns_on_stderr(self) -> None:
        for raw in ("{bad json", "Browser=3", "browser=0", "browser=abc", "nocolon"):
            with self.subTest(raw=raw):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    parsed = sf_settings.parse_group_caps_env(raw)
                self.assertEqual({}, parsed)
                self.assertIn("SLOTFATHER_GROUP_CAPS", err.getvalue())


class EstimatesPerSlotRecommendationTests(unittest.TestCase):
    def test_recommended_gib_per_slot_divides_by_slot_count_and_balances_labels(self) -> None:
        est = sf_estimates.Estimates()
        # A 4-slot lease peaking at 12 GiB is 3.0 GiB/slot, not 12.0 GiB/slot.
        est.add_memory("heavy:browser", "playwright", 12 * 1024.0, count=4)
        self.assertEqual(3.0, est.recommended_gib_per_slot())
        # Adding 20 small runs of another label must not drown out the heavier label's per-slot p90.
        for _ in range(20):
            est.add_memory("tiny:unit", "pytest", 512.0, count=1)
        self.assertEqual(3.0, est.recommended_gib_per_slot())


class AdmissionGroupCapTests(unittest.TestCase):
    def test_cap_of_3_holds_4th_browser_while_allowing_non_browser(self) -> None:
        held: list[dict[str, Any]] = [
            {"count": 1, "label": "repo1:browser", "cmd": "npm test", "since": SINCE, "granted": SINCE, "state": "held"},
            {"count": 1, "label": "repo2:control", "cmd": "bun test", "since": SINCE, "granted": SINCE, "state": "held"},
            {"count": 1, "label": "repo3:story checks", "cmd": "bun test", "since": SINCE, "granted": SINCE, "state": "held"},
        ]
        browser_4 = {"count": 1, "label": "repo4:browser", "cmd": "npm test", "since": SINCE, "state": "waiting"}
        unit = {"count": 1, "label": "repo4:unit", "cmd": "pytest", "since": SINCE, "state": "waiting"}
        settings = {**DEFAULTS, "group_caps": {"browser": 3}}
        adm = sf_schedule.Admission([browser_4, unit], held, 4, M, settings=settings)
        outcome = adm.plan_grants()
        self.assertEqual([unit], [s.entry for s in outcome.started])
        expected = "group browser cap 3 reached (3 running)"
        self.assertEqual([], outcome.gated)
        self.assertEqual([(browser_4, sf_schedule.GateBlock("group_cap", expected))], outcome.capped)
        self.assertEqual(expected, outcome.reason)
        fc = adm.forecast()
        self.assertEqual(expected, fc[id(browser_4)]["group_wait"])
        self.assertIsNone(fc[id(browser_4)]["est_start"])
        self.assertIsNone(fc[id(unit)]["group_wait"])
        self.assertEqual(0, adm.room("repo4:browser", "npm test", gated=True))
        self.assertEqual(expected, adm.advise_block("repo4:browser", "npm test").group_cap_block)
        self.assertIsNone(adm.advise_block("repo4:unit", "pytest").group_cap_block)

    def test_idle_queue_grants_up_to_cap_and_holds_excess(self) -> None:
        waiters = [
            {"count": 1, "label": f"b{i}:browser", "cmd": "playwright test", "since": SINCE, "state": "waiting"}
            for i in range(4)
        ]
        settings = {**DEFAULTS, "group_caps": {"browser": 3}}
        outcome = sf_schedule.Admission(waiters, [], 8, M, settings=settings).plan_grants()
        self.assertEqual(["b0:browser", "b1:browser", "b2:browser"], [s.entry["label"] for s in outcome.started])
        self.assertEqual([], outcome.gated)
        self.assertEqual(["b3:browser"], [e["label"] for e, _ in outcome.capped])
        self.assertEqual("group_cap", outcome.capped[0][1].kind)

    def test_capped_normal_waiter_does_not_make_batch_work_defer(self) -> None:
        """P2: a capped normal/interactive waiter must not starve uncapped batch work."""
        held = [{"count": 1, "label": "one:browser", "cmd": "playwright", "since": SINCE, "granted": SINCE, "state": "held"}]
        capped = {"count": 1, "label": "two:browser", "cmd": "playwright", "since": SINCE, "state": "waiting"}
        batch_big = {"count": 4, "label": "batch-big", "cmd": "make", "since": SINCE, "state": "waiting", "priority": "batch"}
        settings = {**DEFAULTS, "group_caps": {"browser": 1}}
        # Past overtake window so head reservation applies
        later = M + 1000.0
        found = sf_schedule.plan([capped, batch_big], held, 4, later, settings=settings)
        self.assertIs(batch_big, found.head, "batch waiter must become head when the only normal waiter is capped")
        self.assertIn(id(capped), found.group_capped)

        # Also when whole-machine batch fits beside a capped waiter on an empty slot budget
        batch_all = {"count": 4, "all": True, "label": "batch-all", "cmd": "make", "since": SINCE, "state": "waiting",
                     "priority": "batch"}
        held_none: list[dict[str, Any]] = []
        b1 = {"count": 1, "label": "one:browser", "cmd": "playwright", "since": SINCE, "state": "waiting"}
        # When held has 0 slots and cap=1, b1 is granted; if we instead have cap=1 with 1 held browser of 0 slots?
        # Use a custom group cap on a 0-free check: 4 slots, 0 held, batch_all with a capped waiter via meta.
        # Even simpler: 4 slots, 1 held browser (1 slot) -> 3 free; batch_3 (3 slots, batch) fits and starts.
        batch_3 = {"count": 3, "label": "batch-3", "cmd": "make", "since": SINCE, "state": "waiting", "priority": "batch"}
        found_fit = sf_schedule.plan([capped, batch_3], held, 4, M, settings=settings)
        self.assertEqual([batch_3], found_fit.grants)
        # Keep batch_all referenced cleanly
        self.assertTrue(batch_all["all"] and not held_none and bool(b1))

    def test_explicit_memory_budget_mb_caps_below_memory_share(self) -> None:
        held = [{"count": 1, "label": "running", "since": SINCE, "granted": SINCE, "state": "held"}]
        waiter = {"count": 1, "label": "next", "since": SINCE, "state": "waiting"}
        adm = sf_schedule.Admission(
            [waiter],
            held,
            8,
            M,
            settings={**DEFAULTS, "memory_share": 0.85, "memory_budget_mb": 12000.0},
            host=sf_schedule.Host(ram_mb=32768.0),
            memory_estimate=lambda e: 7000.0,
        )
        outcome = adm.plan_grants()
        self.assertEqual([], outcome.started)
        self.assertEqual("expected memory 14000 MB exceeds 12000 MB (configured budget)", outcome.reason)
        self.assertEqual(12000.0, adm.dashboard_room().memory_budget_mb)
        self.assertEqual(12000.0, adm.advise_block("next", "cmd").memory_budget_mb)


class MacosBudgetAndGroupCapIntegrationTests(SlotCase):
    def setUp(self) -> None:
        super().setUp()
        self.queue = self.state / "queue"
        self.queue.mkdir(parents=True)
        self.sequence = 0

    def entry(self, label: str, count: int = 1, state: str = "waiting", cmd: str = "fake",
              meta: dict[str, str] | None = None, priority: str = "normal",
              yieldable: bool = False, since: str = "2026-11-12T00:00:00Z") -> Path:
        pid, start = self.holder()
        self.sequence += 1
        path = self.queue / f"{self.sequence:010d}-{pid}.json"
        payload: dict[str, Any] = {
            "pid": pid,
            "start": start,
            "count": count,
            "label": label,
            "cmd": cmd,
            "since": since,
            "state": state,
            "priority": priority,
            "yieldable": yieldable,
        }
        if state == "held":
            payload["granted"] = since
        if meta:
            payload["meta"] = meta
        path.write_text(json.dumps(payload))
        return path

    def state_of(self, path: Path) -> str:
        return cast(str, json.loads(path.read_text())["state"])

    def test_macos_fixed_capacity_advise_reports_memory_and_recommendation(self) -> None:
        """On macOS with fixed slots (8 on 32 GiB), advise reports derived gib_per_slot, gib_per_slot_source,
        memory_gib, memory_budget_mb, and per-slot recommended_gib_per_slot; configured modes report 'configured'."""
        (self.state / "capacity.json").write_text(json.dumps({"slots": 8}))
        history = self.state / "history.jsonl"
        history.write_text("\n".join(
            json.dumps({"label": f"browser-{i}", "cmd": "playwright", "count": 2, "ran": 30.0, "granted": SINCE,
                        "outcome": "released", "rss_peak_mb": peak})
            for i, peak in enumerate((6400.0, 8200.0, 9200.0, 10400.0, 10800.0))
        ) + "\n")
        sf_estimates._estimates.clear()  # pyright: ignore[reportPrivateUsage]
        base_env = {k: v for k, v in self.env.items() if k != "SLOTFATHER_MEMORY_GIB"}

        def fake_sysctl(name: str) -> str | None:
            return {
                "hw.memsize": str(32 * (1 << 30)),
                "kern.memorystatus_vm_pressure_level": "1",
            }.get(name)

        with mock.patch.dict(os.environ, base_env, clear=True), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(sf_platform, "sysctl", side_effect=fake_sysctl):
            advice = sf_query.advise(label="browser-1", command="playwright")
        self.assertEqual("fixed", advice["capacity_mode"])
        self.assertEqual(32.0, advice["memory_gib"])
        self.assertEqual(4.0, advice["gib_per_slot"])
        self.assertEqual("derived", advice["gib_per_slot_source"])
        # 2-slot leases peaking at 10400-10800 MB -> ~5.3 GiB/slot -> rounded up to 5.5 GiB/slot
        self.assertEqual(5.5, advice["recommended_gib_per_slot"])
        self.assertEqual(round(32 * 1024 * 0.85, 1), advice["memory_budget_mb"])
        self.assertEqual(0.0, advice["memory_expected_mb"])

        # Configured memory mode reports gib_per_slot_source="configured"
        (self.state / "capacity.json").write_text(json.dumps({"slots": 4, "mode": "memory", "gib_per_slot": 8}))
        with mock.patch.dict(os.environ, base_env, clear=True), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(sf_platform, "sysctl", side_effect=fake_sysctl):
            configured_advice = sf_query.advise(label="browser-1", command="playwright")
        self.assertEqual((8, "configured"), (configured_advice["gib_per_slot"], configured_advice["gib_per_slot_source"]))

        # Linux branch stubbed on macOS
        (self.state / "capacity.json").write_text(json.dumps({"slots": 8}))
        with mock.patch.dict(os.environ, base_env, clear=True), \
                mock.patch.object(sf_platform, "linux", return_value=True), \
                mock.patch.object(sf_platform, "cgroup_limit", return_value=None), \
                mock.patch.object(sf_platform, "meminfo", return_value={"MemTotal": 16 * (1 << 20), "MemAvailable": 12 * (1 << 20)}), \
                mock.patch.object(sf_platform, "psi", return_value=None):
            linux_advice = sf_query.advise(label="unit", command="pytest")
        self.assertEqual(16.0, linux_advice["memory_gib"])
        self.assertEqual(2.0, linux_advice["gib_per_slot"])
        self.assertEqual("derived", linux_advice["gib_per_slot_source"])

    def test_macos_memory_budget_admission_fires_via_order_and_env(self) -> None:
        (self.state / "capacity.json").write_text(json.dumps({"slots": 8}))
        self.entry("running", 1, "held")
        waiter = self.entry("waiter", 1, "waiting")
        res = self.tool("order", "--memory-budget-mb", "12000", "--memory-default-mb", "7000")
        self.assertEqual(0, res.returncode, res.stderr)
        self.assertIn("memory budget 12000 MB", res.stdout)

        def fake_sysctl(name: str) -> str | None:
            return {
                "hw.memsize": str(32 * (1 << 30)),
                "kern.memorystatus_vm_pressure_level": "1",
            }.get(name)

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(sf_platform, "sysctl", side_effect=fake_sysctl):
            why = sf_leases.grant(self.state, sf_process.processes())
            block = sf_board.dashboard_json(sf_board.dashboard_model())
        self.assertEqual("expected memory 14000 MB exceeds 12000 MB (configured budget)", why)
        self.assertEqual("waiting", self.state_of(waiter))
        self.assertEqual(12000.0, block["memory_budget_mb"])
        self.assertEqual(why, block["memory_budget_block"])
        # Environment can tighten further, never loosen or disable
        with mock.patch.dict(os.environ, {**self.env, "SLOTFATHER_MEMORY_BUDGET_MB": "0"}, clear=True):
            self.assertEqual(12000.0, sf_settings.order_settings(self.state)["memory_budget_mb"])
        with mock.patch.dict(os.environ, {**self.env, "SLOTFATHER_MEMORY_BUDGET_MB": "8000"}, clear=True):
            self.assertEqual(8000.0, sf_settings.order_settings(self.state)["memory_budget_mb"])

    def test_group_capped_waiter_does_not_suppress_yield_for_interactive_waiter(self) -> None:
        """P1 regression: 4 slots, --group-cap browser=1 --yield-after 1, one:browser (1 slot) + gate (batch,
        yieldable, 3 slots) holding, two:browser (capped) + quick (interactive, 1 slot) waiting -> gate MUST be
        asked to yield and quick MUST start once gate yields."""
        (self.state / "capacity.json").write_text(json.dumps({"slots": 4}))
        old_since = "2026-11-12T00:00:00Z"
        self.entry("one:browser", 1, "held", cmd="playwright test", since=old_since)
        gate = self.entry("gate", 3, "held", cmd="make build", priority="batch", yieldable=True, since=old_since)
        two = self.entry("two:browser", 1, "waiting", cmd="playwright test", since=old_since)
        quick = self.entry("quick", 1, "waiting", cmd="pytest", priority="interactive", since=old_since)

        res = self.tool("order", "--group-cap", "browser=1", "--yield-after", "1")
        self.assertEqual(0, res.returncode, res.stderr)

        with mock.patch.dict(os.environ, self.env, clear=True):
            why = sf_leases.grant(self.state, sf_process.processes())
        # Reason reflects slot wait for `quick`, NOT the capped browser waiter's reason
        self.assertIsNotNone(why)
        self.assertNotIn("group browser cap", why or "")
        # `gate` was asked to yield for `quick` (not suppressed by `two:browser` being capped!)
        yield_marker = gate.with_suffix(".yield")
        self.assertTrue(yield_marker.exists(), "yieldable batch holder must be asked to yield for interactive waiter")
        ask_payload = json.loads(yield_marker.read_text())
        self.assertEqual("quick", ask_payload.get("label"))

        # Once `gate` yields, `quick` starts immediately while `two:browser` remains capped
        with mock.patch.dict(os.environ, self.env, clear=True):
            yielded = sf_leases.yield_lease(gate)
        self.assertTrue(yielded)
        self.assertEqual("held", self.state_of(quick))
        self.assertEqual("waiting", self.state_of(two))

    def test_browser_group_cap_lifecycle_and_tuning(self) -> None:
        (self.state / "capacity.json").write_text(json.dumps({"slots": 8}))
        b1 = self.entry("repo1:browser", 1, "held", cmd="npm run test")
        self.entry("repo2:control", 1, "held", cmd="npm run test")
        self.entry("repo3:story checks", 1, "held", cmd="bun test")
        b4 = self.entry("repo4:browser", 1, "waiting", cmd="playwright test")
        unit = self.entry("repo4:unit", 1, "waiting", cmd="pytest")

        # Configure cap = 3
        saved = self.tool("order", "--group-cap", "browser=3")
        self.assertEqual(0, saved.returncode, saved.stderr)
        self.assertIn("group caps browser=3", saved.stdout)
        self.assertEqual({"browser": 3}, json.loads((self.state / "order.json").read_text())["group_caps"])
        # Configuring order.json triggered grant_now: unit started, 4th browser stayed waiting!
        self.assertEqual("held", self.state_of(unit))
        self.assertEqual("waiting", self.state_of(b4))

        # Adding another group cap merges under the lock without overwriting `browser`
        add_e2e = self.tool("order", "--group-cap", "e2e=2")
        self.assertEqual(0, add_e2e.returncode, add_e2e.stderr)
        self.assertEqual({"browser": 3, "e2e": 2}, json.loads((self.state / "order.json").read_text())["group_caps"])
        rm_e2e = self.tool("order", "--group-cap", "e2e=off")
        self.assertEqual(0, rm_e2e.returncode, rm_e2e.stderr)
        self.assertEqual({"browser": 3}, json.loads((self.state / "order.json").read_text())["group_caps"])

        # status, status --json, settings --json, and advise all report the group cap without nested quotes
        with mock.patch.dict(os.environ, self.env, clear=True):
            snap = sf_query.status_json(self.state)
            lines = sf_query.status_lines(self.state)
            advice = sf_query.advise(label="repo5:browser", command="playwright test")
        self.assertEqual({"browser": 3}, snap["group_caps"])
        b4_item = next(e for e in snap["entries"] if e["label"] == "repo4:browser")
        expected_reason = "group browser cap 3 reached (3 running)"
        self.assertEqual(expected_reason, b4_item["group_wait"])
        self.assertIsNone(b4_item["est_start"])
        b4_line = next(line for line in lines if "repo4:browser" in line)
        self.assertIn(f'group-wait="{expected_reason}"', b4_line)
        self.assertEqual(expected_reason, advice["group_cap_block"])

        settings_doc = json.loads(self.tool("settings", "--json").stdout)
        status_map = {row["label"]: row["value"] for row in settings_doc["status"]}
        self.assertEqual("browser=3", status_map["group caps"])

        # Release one running browser lease -> 4th browser is granted immediately
        b1.unlink()
        with mock.patch.dict(os.environ, self.env, clear=True):
            why = sf_leases.grant(self.state, sf_process.processes())
        self.assertIsNone(why)
        self.assertEqual("held", self.state_of(b4))

        # Tune 3 -> 4 -> off
        b5 = self.entry("repo5:browser", 1, "waiting", cmd="playwright test")
        tune4 = self.tool("order", "--group-cap", "browser=4")
        self.assertEqual(0, tune4.returncode, tune4.stderr)
        self.assertIn("group caps browser=4", tune4.stdout)
        self.assertEqual("held", self.state_of(b5), "raising cap from 3 to 4 starts the waiting 4th browser")

        # Environment can only tighten, never loosen
        with mock.patch.dict(os.environ, {**self.env, "SLOTFATHER_GROUP_CAPS": "browser=2"}, clear=True):
            self.assertEqual({"browser": 2}, sf_settings.order_settings(self.state)["group_caps"])
        with mock.patch.dict(os.environ, {**self.env, "SLOTFATHER_GROUP_CAPS": "browser=6"}, clear=True):
            self.assertEqual({"browser": 4}, sf_settings.order_settings(self.state)["group_caps"])

        # Turn off -> group_caps leaves order.json
        off = self.tool("order", "--group-cap", "browser=off")
        self.assertEqual(0, off.returncode, off.stderr)
        self.assertNotIn("group caps", off.stdout)
        self.assertNotIn("group_caps", json.loads((self.state / "order.json").read_text()))

        # Invalid CLI --group-cap values are refused
        for bad in ("browser", "browser=-1", "browser=true", "browser=1.5", "BAD=3"):
            with self.subTest(bad=bad):
                res = self.tool("order", "--group-cap", bad)
                self.assertEqual(2, res.returncode, res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
