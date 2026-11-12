"""Review-fix lanes check only what the fix touches (fathers#93): the plan of the fix inside the parent's diff, the
plan of both diffs outside it, the parent's passes reused for what the fix does not change; promote's batch gate
is unchanged."""
from __future__ import annotations

import json
from typing import Any
import unittest

from support import TWO_FINDINGS, LaneCase


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


if __name__ == "__main__":
    unittest.main()
