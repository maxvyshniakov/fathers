#!/usr/bin/env python3
"""Expected durations per run type, outliers and trends (fathers#106)."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
import eventcheck  # noqa: E402
from slotfather import runtype  # noqa: E402

import json
import unittest
from typing import Any

from test_slotfather import SlotCase


def run(ran: float, n: int, plan: str | None = "p1", checks: int | None = 10, phase: str | None = "check",
        label: str = "lane-106-x", **extra: Any) -> dict[str, Any]:
    meta: dict[str, str] = {}
    if phase:
        meta["phase"] = phase
    if plan:
        meta["plan"] = plan
    if checks is not None:
        meta["checks"] = str(checks)
    day = 1 + n // 24
    return {"v": 2, "label": label, "label_derived": "fathers · lanefather", "cmd": "./verify.sh", "count": 1,
            "granted": f"2026-10-{day:02d}T{n % 24:02d}:00:00Z", "ended": f"2026-10-{day:02d}T{n % 24:02d}:30:00Z",
            "outcome": "released", "ran": ran, "status": 0, "meta": meta, "lease_id": f"id{n:04d}", **extra}


class KeyTests(unittest.TestCase):
    def test_keys_by_run_type_with_fallbacks(self):
        self.assertEqual(["fathers · check · plan p1", "fathers · check", "lane-106-x · verify.sh", "lane-106-x"],
                         runtype.keys(run(1, 0)))
        self.assertEqual(["fathers · check", "lane-106-x · verify.sh", "lane-106-x"], runtype.keys(run(1, 0, plan=None)))
        self.assertEqual(["lane-106-x · verify.sh", "lane-106-x"], runtype.keys(run(1, 0, phase=None)))

    def test_expected_falls_back_with_few_runs(self):
        known = runtype.series([run(100, 0, plan="old"), run(110, 1, plan="old"), run(120, 2, plan="new")])
        found = runtype.expected_of(run(0, 3, plan="new"), known)
        assert found is not None
        self.assertEqual(("fathers · check", 3, True, 110.0), (found["key"], found["runs"], found["fallback"],
                                                                found["p50"]))
        self.assertIsNone(runtype.expected_of(run(0, 3, phase=None, label="other"), known))
        many = runtype.series([run(100 + n, n) for n in range(5)])
        exact = runtype.expected_of(run(0, 9), many)
        assert exact is not None
        self.assertEqual(("fathers · check · plan p1", False, 102.0, 103.6),
                         (exact["key"], exact["fallback"], exact["p50"], exact["p90"]))

    def test_trend_with_and_without_a_plan_change(self):
        same = [run(100, n) for n in range(30)] + [run(150, 30 + n) for n in range(10)]
        found = runtype.trend(runtype.series(same)["fathers · check"])
        assert found is not None
        self.assertEqual(("slower", "same plan", 1.5), (found["verdict"], found["why"], found["ratio"]))
        grown = [run(100, n) for n in range(30)] + [run(150, 30 + n, plan="p2", checks=12) for n in range(10)]
        found = runtype.trend(runtype.series(grown)["fathers · check"])
        assert found is not None
        self.assertEqual(("slower", "plan grew: +2 checks"), (found["verdict"], found["why"]))
        self.assertEqual(("p1", "p2", 2), (found["plan_change"]["from"], found["plan_change"]["to"],
                                           found["plan_change"]["checks"]))
        steady = runtype.trend(runtype.series([run(100 + n % 3, n) for n in range(40)])["fathers · check"])
        assert steady is not None
        self.assertEqual("steady", steady["verdict"])
        self.assertIsNone(runtype.trend(runtype.series([run(1, n) for n in range(19)])["fathers · check"]))


class VerdictTests(SlotCase):
    def write(self, items: list[dict[str, Any]]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "history.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))

    def events_of(self) -> list[Any]:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        self.assertEqual([], eventcheck.validate_file(str(path), strict=True))
        return [json.loads(x) for x in path.read_text().splitlines()]

    def settle(self, later: list[float]) -> dict[str, str]:
        key = "fathers · check · plan p1"
        items = [dict(run(100 + n % 5, n), run_key=key, verdict="normal") for n in range(10)]
        items.append(dict(run(900, 99), run_key="fathers · check", verdict="pending"))  # judged elsewhere
        items.append(dict(run(400, 10), run_key=key, verdict="pending"))
        items += [dict(run(ran, 11 + n), run_key=key, verdict="normal") for n, ran in enumerate(later)]
        self.write(items)
        runtype.after_release(self.state, items[-1])
        return runtype.settled(self.state)

    def test_a_pending_run_becomes_an_outlier_once_later_runs_are_normal(self):
        self.assertEqual({}, self.settle([101, 102]), "two later runs settle nothing yet")
        self.assertEqual({"id0010": "outlier"}, self.settle([101, 102, 103]), "only runs judged by this key")
        [event] = [e for e in self.events_of() if e["kind"] == "run.outlier"]
        self.assertEqual(("fathers · check · plan p1", 400, "id0010"), (event["subject"]["id"], event["details"]["ran"],
                                                                         event["details"]["lease_id"]))

    def test_a_pending_run_followed_by_slow_ones_is_slower(self):
        self.assertEqual({"id0010": "slower"}, self.settle([380, 390, 101]))
        self.assertEqual([], [e for e in self.events_of() if e["kind"] == "run.outlier"])

    def test_a_trend_change_is_an_event_once(self):
        items = [run(100, n) for n in range(30)] + [run(150, 30 + n) for n in range(10)]
        items = [dict(i, run_key="fathers · check · plan p1") for i in items]
        self.write(items)
        runtype.after_release(self.state, items[-1])
        runtype.after_release(self.state, items[-1])
        [event] = [e for e in self.events_of() if e["kind"] == "run.trend"]
        self.assertEqual(("slower", "warn"), (event["details"]["verdict"], event["level"]))

    def test_runs_record_their_key_and_verdict_and_status_shows_what_to_expect(self):
        options = ("--label", "lane-106-y", "--meta", "phase=check", "--meta", "plan=p9", "--meta", "checks=3")
        for _ in range(3):
            self.assertEqual(0, self.tool("run", *options, "--", "true").returncode)
        self.assertEqual(0, self.tool("run", *options, "--", "true").returncode)
        lines: list[Any] = [json.loads(x) for x in (self.state / "history.jsonl").read_text().splitlines()]
        self.assertNotIn("verdict", lines[0], "no runs before it to judge by")
        self.assertEqual("normal", lines[-1]["verdict"])
        self.assertTrue(lines[-1]["run_key"].endswith("check · plan p9"))
        process = self.request("lane-106-y", 1, options=options[2:])
        self.started("lane-106-y")
        def entry() -> Any:
            return json.loads(self.tool("status", "--json", "--full").stdout)["entries"][0]
        self.assertEqual(4, entry()["expected"]["runs"])
        self.assertLessEqual(entry()["expected"]["p90"], 1)
        self.wait_for(lambda: entry().get("overrun"), "the running entry to pass its p90", timeout=10)
        self.assertIn("expected", json.loads(self.tool("status", "--json").stdout)["entries"][0],
                      "--full stays status --json and more")
        self.release("lane-106-y")
        self.finish(process)
        report = json.loads(self.tool("stats", "--trend", "--json", "--phase", "check").stdout)
        [row] = report["keys"]
        self.assertEqual((5, "check", "p9"), (row["runs"], row["phase"], row["plan"]))
        self.assertEqual(5, sum(w["count"] for w in row["weeks"]))
        self.assertEqual([], json.loads(self.tool("stats", "--trend", "--json", "--phase", "gate").stdout)["keys"])

    def test_stats_trend_follows_the_phase_across_a_plan_change(self):
        items = [run(100, n) for n in range(30)] + [run(150, 30 + n, plan="p2", checks=12) for n in range(10)]
        self.write(items)
        [row] = json.loads(self.tool("stats", "--trend", "--json").stdout)["keys"]
        self.assertEqual(("fathers · check", "p2", "plan grew: +2 checks"), (row["key"], row["plan"],
                                                                           row["trend"]["why"]))
        self.assertEqual([("p1", "p2")], [(c["from"], c["to"]) for c in row["plan_changes"]])
        runtype.after_release(self.state, dict(items[-1], run_key="fathers · check · plan p2"))
        [event] = [e for e in self.events_of() if e["kind"] == "run.trend"]
        self.assertEqual(("fathers · check", "plan grew: +2 checks"), (event["subject"]["id"], event["details"]["why"]))


if __name__ == "__main__":
    unittest.main()
