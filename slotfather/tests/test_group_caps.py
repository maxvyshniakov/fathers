#!/usr/bin/env python3
"""Mac memory budget, advise memory fields, and per-group concurrency caps (fathers#251)."""
from __future__ import annotations

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
        expected = 'group "browser" cap 3 reached (3 running)'
        self.assertEqual([(browser_4, sf_schedule.GateBlock("group_cap", expected))], outcome.gated)
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
        self.assertEqual(["b3:browser"], [e["label"] for e, _ in outcome.gated])
        self.assertEqual("group_cap", outcome.gated[0][1].kind)

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
              meta: dict[str, str] | None = None) -> Path:
        pid, start = self.holder()
        self.sequence += 1
        path = self.queue / f"{self.sequence:010d}-{pid}.json"
        payload: dict[str, Any] = {
            "pid": pid,
            "start": start,
            "count": count,
            "label": label,
            "cmd": cmd,
            "since": "2026-11-12T00:00:00Z",
            "state": state,
        }
        if state == "held":
            payload["granted"] = "2026-11-12T00:00:00Z"
        if meta:
            payload["meta"] = meta
        path.write_text(json.dumps(payload))
        return path

    def state_of(self, path: Path) -> str:
        return cast(str, json.loads(path.read_text())["state"])

    def test_macos_fixed_capacity_advise_reports_memory_and_recommendation(self) -> None:
        """On macOS with fixed slots (e.g. 8 on 32 GiB), advise reports non-null memory_gib, gib_per_slot,
        memory_budget_mb, and recommended_gib_per_slot from history peaks; Linux stubbed branch also works."""
        (self.state / "capacity.json").write_text(json.dumps({"slots": 8}))
        history = self.state / "history.jsonl"
        history.write_text("\n".join(
            json.dumps({"label": f"browser-{i}", "cmd": "playwright", "ran": 30.0, "granted": SINCE,
                        "outcome": "released", "rss_peak_mb": peak})
            for i, peak in enumerate((3200.0, 4100.0, 4600.0, 5200.0, 5400.0))
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
        self.assertEqual(5.5, advice["recommended_gib_per_slot"])
        self.assertEqual(round(32 * 1024 * 0.85, 1), advice["memory_budget_mb"])
        self.assertEqual(0.0, advice["memory_expected_mb"])

        # Linux branch stubbed on macOS
        with mock.patch.dict(os.environ, base_env, clear=True), \
                mock.patch.object(sf_platform, "linux", return_value=True), \
                mock.patch.object(sf_platform, "cgroup_limit", return_value=None), \
                mock.patch.object(sf_platform, "meminfo", return_value={"MemTotal": 16 * (1 << 20), "MemAvailable": 12 * (1 << 20)}), \
                mock.patch.object(sf_platform, "psi", return_value=None):
            linux_advice = sf_query.advise(label="unit", command="pytest")
        self.assertEqual(16.0, linux_advice["memory_gib"])
        self.assertEqual(2.0, linux_advice["gib_per_slot"])

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

    def test_browser_group_cap_lifecycle_and_tuning(self) -> None:
        (self.state / "capacity.json").write_text(json.dumps({"slots": 8}))
        b1 = self.entry("repo1:browser", 1, "held", cmd="npm run browser")
        self.entry("repo2:control", 1, "held", cmd="npm run control")
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

        # status, status --json, settings --json, and advise all report the group cap
        with mock.patch.dict(os.environ, self.env, clear=True):
            snap = sf_query.status_json(self.state)
            lines = sf_query.status_lines(self.state)
            advice = sf_query.advise(label="repo5:browser", command="playwright test")
        self.assertEqual({"browser": 3}, snap["group_caps"])
        b4_item = next(e for e in snap["entries"] if e["label"] == "repo4:browser")
        expected_reason = 'group "browser" cap 3 reached (3 running)'
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
