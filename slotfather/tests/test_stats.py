#!/usr/bin/env python3
"""`slotfather stats` (#635) over old and new history lines and machine samples, and how commands group."""
from __future__ import annotations

import json
from typing import Any
import unittest

from test_slotfather import TOOL, SlotCase
from test_usage import load

stats: Any = load(TOOL + "-stats", "slotfather_stats_under_test")


V1: dict[str, Any] = {
    "label": "old", "cmd": "make build", "cwd": "/w", "count": 1, "pid": 1, "queued": "2026-11-02T10:00:00Z",
    "granted": "2026-11-02T10:01:00Z", "ended": "2026-11-02T10:11:00Z", "outcome": "released", "waited": 60,
    "ran": 600, "status": 0,
}


def v2(label: str, cmd: str, count: int, waited: float | None, ran: float | None, cpu: float,
       ended: str = "2026-11-02T12:00:00Z", **extra: Any) -> dict[str, Any]:
    return {"v": 2, "label": label, "cmd": cmd, "cwd": "/w", "count": count, "pid": 2, "queued": None,
            "granted": "2026-11-02T11:00:00Z", "ended": ended, "outcome": "released", "waited": waited, "ran": ran,
            "status": 0, "cores": 12, "capacity": 4, "cpu": cpu, "cpu_detached": 0.0, "wall": ran,
            "rss_peak_mb": 100.0, "rss_mean_mb": 50.0, "processes": 3, "samples": 5, **extra}


class StatsTests(SlotCase):
    def write(self, name: str, items: list[Any]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / name).write_text("".join(json.dumps(i) + "\n" for i in items))

    def stats(self, *args: str) -> str:
        result = self.tool("stats", *args)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_per_label_and_kind_waits_runs_and_cores_per_slot_with_old_lines_read_as_they_are(self):
        self.write("history.jsonl.1", [V1])
        self.write("history.jsonl", [
            "not json",
            v2("lane", "/w/verify.sh --checks a,b,c --jobs 1", 2, 0, 100, 300.0),
            v2("lane", "/w/verify.sh --checks a,b,c --jobs 1", 2, 20, 300, 300.0),
            v2("lane", "/w/verify.sh --checks a,b,c --jobs 1", 2, 40, 200, 600.0),
            v2("kit", "bun scripts/run-browser-checks.mjs --url=http://x", 1, 10, 50, 25.0),
            {**V1, "label": "kit", "cmd": "bun scripts/run-browser-checks.mjs"},
        ])
        summary = json.loads(self.stats("--json"))
        self.assertEqual({"1": 2, "2": 4}, summary["versions"])
        groups = {(g["label"], g["kind"]): g for g in summary["groups"]}
        lane = groups[("lane", "verify.sh --checks [3]")]
        self.assertEqual((3, 3, 20, 36.0, 200, 280.0), (lane["requests"], lane["measured"], lane["wait_p50"],
                                                        lane["wait_p90"], lane["run_p50"], lane["run_p90"]))
        self.assertEqual((2.0, 1.0), (lane["cores"], lane["cores_per_slot"]), "1200 CPU s over 600 s on 2 slots")
        kit = groups[("kit", "bun run-browser-checks.mjs")]
        self.assertEqual((2, 1, 0.5), (kit["requests"], kit["measured"], kit["cores_per_slot"]),
                         "a line without usage counts in waits and runs but not in cores")
        self.assertEqual(round(1225 / 1250, 2), summary["overall"]["cores_per_slot"])
        self.assertEqual(round(12 / round(1225 / 1250, 2), 1), summary["fill_slots"])
        by_kind = json.loads(self.stats("--json", "--by", "kind"))
        self.assertEqual({None}, {g["label"] for g in by_kind["groups"]})
        only = json.loads(self.stats("--json", "--label", "ki*"))
        self.assertEqual({"kit"}, {g["label"] for g in only["groups"]})
        recent = json.loads(self.stats("--json", "--since", "2026-11-02T11:30:00+00:00"))
        self.assertEqual(4, recent["overall"]["requests"])
        text = self.stats()
        self.assertRegex(text, r"lane +verify\.sh --checks \[3\] +3 +20s +36s +3m20s +4m40s +2 +2\.0 +1\.0 +100")
        self.assertIn("measured requests used 0.98 cores per slot held", text)

    def test_machine_time_over_and_under_the_cores(self):
        base = 1_800_000_000.0
        # Four samples 15 s apart: load over 12 cores for the first two; CPU half busy then fully busy.
        ticks = [[0, 0, 0, 0], [500, 0, 500, 0], [1500, 0, 500, 0], [2500, 0, 500, 0]]
        loads = [20.0, 13.0, 6.0, 2.0]
        self.write("machine.jsonl", [{"v": 1, "at": base + 15 * n, "load": [loads[n], 1, 1], "ticks": ticks[n],
                                      "cores": 12, "capacity": 4, "held": 2, "leases": 1, "pressure": 1 if n else 2,
                                      "swap_total_mb": 1024, "swap_free_mb": 900 - n}
                                     for n in range(4)])
        found = json.loads(self.stats("--json"))["machine"]
        self.assertEqual((4, 0.5, 0.5), (found["samples"], found["over_cores"], found["under_cores"]))
        self.assertEqual(0.833, found["busy_mean"])
        self.assertEqual(0.333, found["slot_idle"], "a slot's 3 cores sat idle only while half busy")
        self.assertEqual((0.25, 897), (found["pressure_elevated"], found["swap_free_mb_min"]))
        self.assertEqual(5.0, found["busy_cores_per_held_slot"])

    def test_a_gap_with_no_lease_running_is_not_busy_time(self):
        base = 1_800_000_000.0
        # Two samples 15 s apart fully busy, then an idle hour before the next sample.
        ticks = [[0, 0, 0, 0], [1000, 0, 0, 0], [1100, 0, 360000, 0]]
        self.write("machine.jsonl", [{"v": 1, "at": at, "load": [1, 1, 1], "ticks": t, "cores": 12, "capacity": 4,
                                      "held": 1} for at, t in zip((base, base + 15, base + 3615), ticks)])
        found = json.loads(self.stats("--json"))["machine"]
        self.assertEqual(1.0, found["busy_mean"], "the idle hour between leases is not averaged in")

    def test_an_empty_state_and_bad_options(self):
        self.assertIn("no request in range recorded its usage", self.stats())
        for args in (("--since", "yesterday"), ("--by", "pid"), ("--top",), ("--nonsense",)):
            with self.subTest(args=args):
                self.assertEqual(2, self.tool("stats", *args).returncode)

    def test_outcomes_four_way_split_in_json_and_text(self) -> None:
        self.write("history.jsonl", [
            v2("job", "make test", 1, 5, 100, 100.0, status=0),
            v2("job", "make test", 1, 5, 80, 80.0, status=1),
            v2("job", "make test", 1, 10, 50, 50.0, outcome="reaped", was_held=True, status=None),
            {**v2("job", "make test", 1, 20, 0, 0.0, outcome="reaped", was_held=False, status=None),
             "granted": None, "ran": None},
            {**v2("job", "make test", 1, 30, 0, 0.0, outcome="left", reason="timeout", status=None),
             "granted": None, "ran": None},
            {**v2("job", "make test", 1, 0, 0, 0.0, outcome="refused", reason="memory", status=None),
             "granted": None, "ran": None},
        ])
        summary = json.loads(self.stats("--json"))
        overall = summary["overall"]
        self.assertEqual(2, overall["released"])
        self.assertEqual(1, overall["exit_nonzero"])
        self.assertEqual(1, overall["failed"])
        self.assertEqual(2, overall["reaped"])
        self.assertEqual(1, overall["reaped_held"])
        self.assertEqual(1, overall["reaped_waiting"])
        self.assertEqual(1, overall["left"])
        self.assertEqual(1, overall["refused"])
        self.assertEqual({
            "exit_nonzero": 1,
            "reaped": {"total": 2, "held": 1, "waiting": 1},
            "gave_up": 1,
            "refused": 1,
        }, overall["outcomes"])
        group = summary["groups"][0]
        self.assertEqual(overall["outcomes"], group["outcomes"])
        text = self.stats()
        self.assertIn("ran 2 (exit nonzero 1), reaped 2 (1 held, 1 waiting), gave up 1, refused 1", text)


class KindTests(unittest.TestCase):
    def test_commands_group_by_program_and_first_argument(self):
        for command, expected in (
                ("node --test --test-name-pattern=x tests/a.mjs", "node --test"),
                ("bun scripts/run-browser-checks.mjs --url=http://127.0.0.1:6010 icon-sizes",
                 "bun run-browser-checks.mjs"),
                ("/Users/m/wt/verify.sh --checks desktop-tests --keep-going", "verify.sh --checks desktop-tests"),
                ("/Users/m/wt/verify.sh --checks a,b,c --jobs 1", "verify.sh --checks [3]"),
                ("npx vitest --config vitest.config.ts run", "npx vitest"),
                ("python3 -m unittest discover", "python3 -m unittest"),
                ("FOO=1 cargo test --manifest-path x/Cargo.toml", "cargo test"),
                ("", "?")):
            with self.subTest(command=command):
                self.assertEqual(expected, stats.kind(command))

    def test_since_takes_an_age_or_a_date(self):
        self.assertEqual(1000.0 - 7200, stats.parse_since("2h", 1000.0))
        self.assertEqual(1000.0 - 3 * 86400, stats.parse_since("3d", 1000.0))
        self.assertEqual(1793664000.0, stats.parse_since("2026-11-03T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
