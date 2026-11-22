"""Review-fix lanes check only what the fix touches (fathers#93): the plan of the fix inside the parent's diff, the
plan of both diffs outside it, the parent's passes reused for what the fix does not change; promote's batch gate
is unchanged."""
from __future__ import annotations

import json
from typing import Any
import unittest

from support import FIXTURE_CONFIG, TWO_FINDINGS, LaneCase, ScratchCase, project


class FixLaneTests(LaneCase):
    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def fix_lane(self) -> str:
        """Lane a, whose diff selects lib-check and native-check, landed and reviewed with findings; its fix lane's
        worktree on a branch from the integration tip."""
        self.review_text.write_text(TWO_FINDINGS)
        worktree = self.created("a", "lib/**", "native/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n", "native/straylight-core/notes.txt": "a\n"})
        self.lane("check", "a")
        self.assertEqual(sorted(self.receipt("a")["check_results"]),
                         ["lib-check", "native-check", "structure-check"])
        self.lane("land", "a")
        self.lane("review", "a")
        self.lane("review", "a", "--fix-lane")
        self.git(worktree, "checkout", "-q", "-B", "lane/a-review-fixes", "integrate/q")
        return worktree

    def test_a_fix_inside_the_parents_diff_runs_only_its_own_plan(self):
        worktree = self.fix_lane()
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\nqux\n"}, "fix(review): keep baz")
        runs = len(self.verify_runs())
        output = self.lane("check", "a-review-fixes", cwd=worktree)
        self.assertIn("review-fix lane of a: the plan of its fix, inside the parent's diff", output)
        self.assertEqual(self.verify_runs()[runs:], ["checks structure-check,lib-check"])
        receipt = self.receipt("a-review-fixes")
        self.assertEqual(sorted(receipt["check_results"]), ["lib-check", "structure-check"])
        self.assertEqual((receipt["fix_of"]["lane"], receipt["fix_of"]["union"]), ("a", False))

    def test_a_fix_outside_the_parents_diff_plans_both_and_reuses_the_parents_passes(self):
        worktree = self.fix_lane()
        self.commit(worktree, {"docs/install.md": "the fixed journey\n"}, "fix(review): document baz")
        runs = len(self.verify_runs())
        output = self.lane("check", "a-review-fixes", cwd=worktree)
        self.assertIn("the plan of both diffs, since the fix changes docs/install.md outside a's diff", output)
        # The parent's lib-check and native-check join the plan; what the fix does not change is the parent's pass.
        self.assertEqual(self.verify_runs()[runs:], ["checks structure-check,host-journeys-check"])
        receipt = self.receipt("a-review-fixes")
        self.assertEqual(sorted(receipt["check_results"]),
                         ["host-journeys-check", "lib-check", "native-check", "structure-check"])
        parent_tree = self.receipt("a")["tree"]
        for check in ("lib-check", "native-check"):
            entry = receipt["check_results"][check]
            self.assertEqual((entry["status"], entry["reused_by"], entry["reused_from"]),
                             ("passed", "parent", parent_tree))
        self.assertEqual((receipt["fix_of"]["union"], receipt["fix_of"]["added"]),
                         (True, ["lib-check", "native-check"]))
        self.assertEqual(receipt["status"], "green")

    def test_a_check_the_delta_since_the_parent_selects_runs_and_an_old_parent_receipt_still_plans(self):
        worktree = self.fix_lane()
        # A parent receipt from before `paths`: the paths that chose its checks stand for its diff.
        parent = self.receipt("a")
        del parent["paths"]
        (self.state / "a.receipt.json").write_text(json.dumps(parent))
        self.commit(worktree, {"docs/install.md": "journey\n", "lib/api.txt": "foo\nbaz\nqux\n"}, "fix(review)")
        runs = len(self.verify_runs())
        self.lane("check", "a-review-fixes", cwd=worktree)
        # lib/api.txt changed since the parent's head: lib-check runs; native-check stands as the parent's pass.
        self.assertEqual(self.verify_runs()[runs:], ["checks structure-check,lib-check,host-journeys-check"])
        receipt = self.receipt("a-review-fixes")
        self.assertEqual(receipt["fix_of"]["outside_parent"], ["docs/install.md"])
        native = receipt["check_results"]["native-check"]
        self.assertEqual((native["reused_by"], native["reused_lane"], native["reused_receipt"]),
                         ("parent", "a", str(self.state / "a.receipt.json")))
        self.assertNotIn("reused_by", receipt["check_results"]["lib-check"])

    def test_a_rerun_or_a_parent_without_per_check_results_reuses_nothing(self):
        worktree = self.fix_lane()
        self.commit(worktree, {"docs/install.md": "journey\n"}, "fix(review)")
        runs = len(self.verify_runs())
        self.lane("check", "a-review-fixes", "--rerun", cwd=worktree)
        self.assertEqual(self.verify_runs()[runs:],
                         ["checks structure-check,host-journeys-check,lib-check,native-check"])
        parent = self.receipt("a")
        del parent["check_results"]
        (self.state / "a.receipt.json").write_text(json.dumps(parent))
        self.commit(worktree, {"docs/install.md": "journey, again\n"}, "fix(review): again")
        output = self.lane("check", "a-review-fixes", cwd=worktree)
        self.assertNotIn("review-fix lane of a", output)
        self.assertNotIn("fix_of", self.receipt("a-review-fixes"))

    def test_promote_still_runs_the_batch_gate_after_a_fix_lane(self):
        worktree = self.fix_lane()
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\nqux\n"}, "fix(review): keep baz")
        self.lane("check", "a-review-fixes", cwd=worktree)
        self.lane("land", "a-review-fixes")
        self.lane("review", "a", "--mark", "1", "fixed:a-review-fixes", "--mark", "2", "rejected:out of scope")
        self.git(self.repo, "branch", "dev", "main")
        self.lane("promote", self.tip(), "--no-push", "--wait", "0")
        self.assertEqual(self.verify_runs()[-1], "FULL structure,lib,native,host-journeys")


class FixLaneCapTests(LaneCase):
    """fathers#45 (a): a fix lane takes its landed, kept parent's place, so a full cap does not stall the fix round;
    new queue work is still refused, and the disk floor still applies."""

    def landed_with_findings(self) -> None:
        self.review_text.write_text(TWO_FINDINGS)
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.lane("land", "a")
        self.lane("review", "a")
        for index, claim in enumerate(("b/**", "c/**", "d/**")):
            self.start(f"l{index}", claim)

    def test_at_a_full_cap_the_fix_lane_registers_while_next_start_is_refused(self):
        self.landed_with_findings()
        self.write(self.state.parent, "queue/queue.toml", '[[lane]]\nid="next"\nticket=2\nclaims=["e/**"]\n')
        self.assertIn("3 lanes are planned or active", self.lane("next", "--start", ok=False))
        self.lane("review", "a", "--fix-lane")
        fix = json.loads((self.state / "a-review-fixes.json").read_text())
        self.assertEqual((fix["review_fix_of"], fix["cap_source"]), ("a", "fix-lane of a"))
        self.assertIn("start a-review-fixes", ledger := (self.state / "ledger.log").read_text())
        self.assertIn("(fix-lane of a)", ledger)
        started = [json.loads(line) for line in self.events.read_text().splitlines()
                   if '"lane.started"' in line and '"a-review-fixes"' in line]
        self.assertEqual(started[-1]["details"]["cap_source"], "fix-lane of a")
        # The fix lane now counts like any planned or active lane: new work stays refused.
        self.assertIn("4 lanes are planned or active", self.lane("next", "--start", ok=False))

    def test_disk_still_refuses_a_fix_lane(self):
        self.landed_with_findings()
        # Under the derived floor (fathers#157): 40 GB reserve + the cap times a 3 GB lane peak.
        self.env["LANEFATHER_DISK_FREE_GB"] = "20"
        output = self.lane("review", "a", "--fix-lane", ok=False)
        self.assertIn("free disk is 20 GB (10^9)", output)
        self.assertNotIn("lanes are planned or active", output)
        self.assertFalse((self.state / "a-review-fixes.json").exists())

    def test_a_fix_lane_can_register_at_high_load_and_the_global_ceiling(self):
        self.landed_with_findings()
        self.env.update(LANEFATHER_LOAD15="9.5", LANEFATHER_GLOBAL_CAP="3")
        output = self.lane("review", "a", "--fix-lane")
        self.assertIn("load15 is 9.5, above the advisory limit of 9.0", output)
        self.assertNotIn("lanes are planned or active", output)
        self.assertEqual(json.loads((self.state / "a-review-fixes.json").read_text())["cap_source"],
                         "fix-lane of a")

    def test_another_repositorys_claim_keeps_no_file_out_of_the_fix_lane(self):
        self.landed_with_findings()
        (self.state / "dg-1.json").write_text(json.dumps({
            "id": "dg-1", "ticket": "7", "branch": "lane/dg-1", "base": "main", "claims": ["lib/**"], "after": [],
            "common_dir": str(self.tmp / "data-grid" / ".git"), "worktree": None, "status": "planned"}))
        self.env["LANEFATHER_CAP"] = "9"
        output = self.lane("review", "a", "--fix-lane")
        self.assertNotIn("stays out of the fix lane's claims", output)
        self.assertIn("lib/api.txt", json.loads((self.state / "a-review-fixes.json").read_text())["claims"])


class FixParentUnitTests(ScratchCase):
    def test_only_a_landed_parent_holding_the_worktree_exempts_a_lane(self) -> None:
        from unittest.mock import patch

        from lanefather import lanes
        self.write(self.tmp, ".lanefather.toml", FIXTURE_CONFIG)
        self.state.mkdir(parents=True)
        worktree = str(self.tmp / "kept")
        for status, expected in (("landed", "p"), ("released", "p"), ("active", None), ("done", None)):
            with self.subTest(status):
                (self.state / "p.json").write_text(json.dumps({"id": "p", "status": status, "worktree": worktree}))
                with patch.dict("os.environ", self.env, clear=True), project(self.tmp):
                    self.assertEqual(lanes.fix_parent({"review_fix_of": "p", "worktree": worktree}), expected)
                    self.assertIsNone(lanes.fix_parent({"review_fix_of": "p", "worktree": str(self.tmp / "x")}))


if __name__ == "__main__":
    unittest.main()
