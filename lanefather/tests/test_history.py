"""The run history (fathers#92): per-check durations, the lease run id and flakes, kept after the lane is done."""
from __future__ import annotations

import json
import pathlib
from typing import Any
import unittest

from support import LaneCase


class HistoryTests(LaneCase):
    def lines(self, kind: str | None = None) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for path in sorted((self.state / "history").glob("*.jsonl")):
            found += [json.loads(line) for line in path.read_text().splitlines()]
        return [line for line in found if kind is None or line["kind"] == kind]

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def test_each_check_of_a_lane_is_kept_after_done_with_its_run_and_tree(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        receipt = self.receipt("a")
        run = receipt["leases"][0]["run"]
        self.assertEqual({r["run"] for r in receipt["results"]}, {run})
        # The lease's meta names the run and the tree, so a slotfather entry finds its receipt.
        meta = json.loads(pathlib.Path(str(self.verify_log) + ".requests").read_text().splitlines()[-1])["meta"]
        self.assertEqual((meta["run"], meta["tree"]), (run, receipt["tree"]))
        self.lane("land", "a")
        self.lane("review", "a")
        self.lane("done", "a", ok=None)
        checks = [line for line in self.lines("check") if line["lane"] == "a" and line["phase"] == "check"]
        self.assertEqual([line["check"] for line in checks], ["structure-check", "lib-check"])
        for line in checks:
            self.assertEqual((line["status"], line["tree"], line["run"]), ("passed", receipt["tree"], run))
            self.assertTrue(line["started"] and line["finished"])
        stats = json.loads(self.lane("stats", "--json"))
        self.assertEqual(stats["v"], 1)
        row = next(r for r in stats["checks"] if r["check"] == "lib-check")
        self.assertEqual((row["runs"], row["failed"], row["flakes"]), (1, 0, 0))

    def test_a_check_red_then_green_on_one_tree_is_one_flake(self):
        flag = self.tmp / "flake-once"
        flag.touch()
        self.env["FIXTURE_FLAKE_ONCE"] = f"lib-check:{flag}"
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        flakes = self.lines("flake")
        self.assertEqual([(f["check"], f["how"], f["tree"]) for f in flakes],
                         [("lib-check", "rerun-alone", self.receipt("a")["tree"])])
        # The rerun's own run is in the history too, under its phase.
        self.assertIn("rerun", {line["phase"] for line in self.lines("check")})
        stats = json.loads(self.lane("stats", "--json", "--lane", "a"))
        self.assertEqual(next(r for r in stats["checks"] if r["check"] == "lib-check")["flakes"], 1)
        self.assertIn("lib-check:", self.lane("stats"))

    def test_a_later_pass_after_a_red_check_on_the_same_tree_is_a_flake(self):
        flag = self.tmp / "flake-once"
        flag.touch()
        # Only the first run is red, and with no reruns it stays red: the next check passes on the same tree.
        self.env.update(FIXTURE_FLAKE_ONCE=f"lib-check:{flag}", LANEFATHER_RERUNS="1", FAKE_SLOTFATHER_BUSY="1")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a", ok=False)
        del self.env["FAKE_SLOTFATHER_BUSY"]
        self.lane("check", "a")
        flakes = self.lines("flake")
        self.assertEqual([(f["check"], f["how"]) for f in flakes], [("lib-check", "later-run")])

    def test_a_flake_names_the_failed_junit_tests(self):
        from lanefather import history
        report = self.tmp / "junit.xml"
        report.write_text('<testsuite><testcase classname="grid" name="scrolls"><failure/></testcase>'
                          '<testcase classname="grid" name="sorts"/></testsuite>')
        self.assertEqual(history.junit_failures(report), ["grid.scrolls"])
        self.assertEqual(history.junit_failures(self.tmp / "missing.xml"), [])


if __name__ == "__main__":
    unittest.main()
