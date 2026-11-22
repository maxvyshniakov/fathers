#!/usr/bin/env python3
"""Slot range requests (`slotfather run -n MIN-MAX`, fathers#128): waits as MIN, granted up to MAX of what is free,
exported as SLOTFATHER_SLOTS, recorded on entry/snapshot/lease.granted, and advertised in capabilities."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import json
import subprocess
import unittest
from typing import Any

from slotfather import cli as sf_cli
from slotfather import config as sf_config
from slotfather import entry as sf_entry
from slotfather import schedule as sf_schedule
from test_slotfather import TOOL, WORK, SlotCase

T0 = 1_800_000_000.0
STAMP = "2027-01-15T08:00:00Z"


def waiter(label: str, count: int, *, max_count: int | None = None, since: str = STAMP, **extra: Any) -> dict[str, Any]:
    return sf_entry.new_request(1, "s", count, label, "echo", "/w", since, "normal", f"id-{label}",
                                max_count=max_count, **extra)


class SchedulerRangeTests(unittest.TestCase):
    def test_grants_at_min_between_and_max_according_to_free_slots(self) -> None:
        slots = 6
        settings = dict(sf_config.ORDER_DEFAULTS)
        # 5 held -> 1 free -> 1-4 granted at MIN (1)
        held_5 = [dict(waiter("h", 5), state="held", granted=STAMP)]
        w1 = waiter("r", 1, max_count=4)
        p1 = sf_schedule.plan([w1], held_5, slots, T0, settings=settings)
        self.assertEqual([w1], p1.grants)
        self.assertEqual(1, p1.granted_count(w1))
        # 3 held -> 3 free -> 1-4 granted between MIN and MAX (3)
        held_3 = [dict(waiter("h", 3), state="held", granted=STAMP)]
        w2 = waiter("r", 1, max_count=4)
        p2 = sf_schedule.plan([w2], held_3, slots, T0, settings=settings)
        self.assertEqual([w2], p2.grants)
        self.assertEqual(3, p2.granted_count(w2))
        # 0 held -> 6 free -> 1-4 granted at MAX (4), leaving 2 free for a second waiter
        w3 = waiter("r1", 1, max_count=4)
        w4 = waiter("r2", 2)
        p3 = sf_schedule.plan([w3, w4], [], slots, T0, settings=settings)
        self.assertEqual([w3, w4], p3.grants)
        self.assertEqual(4, p3.granted_count(w3))
        self.assertEqual(2, p3.granted_count(w4))

    def test_scheduling_order_treats_range_as_min_and_plain_count_unchanged(self) -> None:
        # 2-4 cannot start when only 1 slot is free, and plain -n 2 has no min/max fields
        plain = waiter("plain", 2)
        self.assertNotIn("min_count", plain)
        self.assertNotIn("max_count", plain)
        ranged = waiter("ranged", 2, max_count=4)
        self.assertEqual((2, 4), (ranged["min_count"], ranged["max_count"]))
        held_5 = [dict(waiter("h", 5), state="held", granted=STAMP)]
        p = sf_schedule.plan([ranged], held_5, 6, T0, settings=dict(sf_config.ORDER_DEFAULTS))
        self.assertEqual([], p.grants)

    def test_forecast_uses_granted_count_for_range_waiters(self) -> None:
        """fathers#228: forecast() records a granted -n MIN-MAX waiter with its granted slot count, not MIN."""
        slots = 4
        settings = dict(sf_config.ORDER_DEFAULTS)
        # 1 held (ends at T0+100), 3 free -> ranged (1-3, runs 600s) is granted 3 slots at T0.
        # Later (2 slots) waits: at T0+100 only 1 slot is free (ranged holds 3, not 1), so later starts at T0+600.
        held = [dict(waiter("h", 1), state="held", granted=STAMP)]
        ranged = waiter("ranged", 1, max_count=3)
        later = waiter("later", 2)

        def est(entry: dict[str, Any]) -> float | None:
            return {"h": 100.0, "ranged": 600.0, "later": 60.0}.get(str(entry.get("label")))

        starts = sf_schedule.forecast([ranged, later], held, slots, T0, estimate=est, settings=settings)
        self.assertEqual(T0, starts[id(ranged)])
        self.assertEqual(T0 + 600.0, starts[id(later)])

        # Also through Admission.forecast(): an idle 4-slot queue where ranged (1-4) takes all 4 slots.
        ranged_all = waiter("ranged-all", 1, max_count=4)
        behind = waiter("behind", 2)
        adm = sf_schedule.Admission(
            [ranged_all, behind],
            [],
            slots,
            T0,
            settings=settings,
            estimate=lambda e: 600.0 if e.get("label") == "ranged-all" else 60.0,
        )
        outlook = adm.forecast()
        self.assertEqual(T0, outlook[id(ranged_all)]["est_start"])
        self.assertEqual(T0 + 600.0, outlook[id(behind)]["est_start"])

    def test_parse_slots_accepts_count_range_all_and_rejects_bad_ranges(self) -> None:
        self.assertEqual(("all", None), sf_cli.parse_slots("all"))
        self.assertEqual((3, None), sf_cli.parse_slots("3"))
        self.assertEqual((2, None), sf_cli.parse_slots("2-2"))
        self.assertEqual((1, 4), sf_cli.parse_slots("1-4"))
        for bad in ("0", "0-4", "4-2", "1-2-3", "a-b", "-1", "2-"):
            with self.subTest(bad=bad):
                with self.assertRaises(sf_cli.UsageError):
                    sf_cli.parse_slots(bad)


class CliRangeTests(SlotCase):
    def test_run_range_grants_at_max_between_and_min_and_records_everywhere(self) -> None:
        # Empty 4-slot queue: -n 1-4 grants at MAX (4)
        res = subprocess.run([TOOL, "run", "-n", "1-4", "--label", "at-max", "--", "sh", "-c",
                              'printf "%s %s" "$SLOTFATHER_SLOTS" "$SLOTFATHER_LEASE_CORES"'],
                             env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, res.returncode, res.stderr)
        self.assertEqual("4 12", res.stdout.strip())

        # Hold 2 slots -> 2 free -> -n 1-4 grants at 2 (between MIN and MAX)
        holder2 = self.request("hold2", 2)
        self.started("hold2")
        out_file = self.tmp / "between.out"
        ranged = subprocess.Popen(
            [TOOL, "run", "-n", "1-4", "--label", "between", "--", "sh", "-c",
             f'printf "%s %s" "$SLOTFATHER_SLOTS" "$SLOTFATHER_LEASE_CORES" > "{out_file}"; ' + WORK,
             "work", "between"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.processes.append(ranged)
        self.started("between")
        self.assertEqual("2 6", out_file.read_text().strip())
        snap = json.loads(self.tool("status", "--json").stdout)
        by_label = {e["label"]: e for e in snap["entries"]}
        self.assertEqual((2, 1, 4), (by_label["between"]["count"], by_label["between"]["min_count"],
                                     by_label["between"]["max_count"]))
        self.release("between")
        self.finish(ranged)
        self.release("hold2")
        self.finish(holder2)

        # Hold 3 slots -> 1 free -> -n 1-4 grants at MIN (1)
        holder3 = self.request("hold3", 3)
        self.started("hold3")
        res_min = subprocess.run([TOOL, "run", "-n", "1-4", "--label", "at-min", "--", "sh", "-c",
                                  'printf "%s %s" "$SLOTFATHER_SLOTS" "$SLOTFATHER_LEASE_CORES"'],
                                 env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, res_min.returncode, res_min.stderr)
        self.assertEqual("1 3", res_min.stdout.strip())
        self.release("hold3")
        self.finish(holder3)

        # Verify lease.granted events recorded the granted counts
        events = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines() if line]
        granted = {e["subject"]["label"]: e["details"] for e in events if e["kind"] == "lease.granted"}
        self.assertEqual((4, 1, 4), (granted["at-max"]["count"], granted["at-max"]["min_count"], granted["at-max"]["max_count"]))
        self.assertEqual((2, 1, 4), (granted["between"]["count"], granted["between"]["min_count"], granted["between"]["max_count"]))
        self.assertEqual((1, 1, 4), (granted["at-min"]["count"], granted["at-min"]["min_count"], granted["at-min"]["max_count"]))

    def test_capabilities_advertises_slot_range_and_bad_range_exits_2(self) -> None:
        caps = json.loads(self.tool("capabilities", "--json").stdout)
        self.assertIn("slot-range", caps["features"])
        self.assertTrue(caps["run"]["slot_range"])
        bad = self.tool("run", "-n", "4-2", "--", "true")
        self.assertEqual(2, bad.returncode)


if __name__ == "__main__":
    unittest.main()
