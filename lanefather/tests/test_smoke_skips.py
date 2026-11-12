"""Cheaper post-merge smokes (fathers#94): a smoke check the merge does not change and that passed on the previous
tip does not run, and a project can leave its tooling self-tests to the batch gate."""
from __future__ import annotations

import json
from typing import Any
import unittest

from support import LaneCase


class SmokeSkipTests(LaneCase):
    def post(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.post-merge.json").read_text())

    def first_lane_smoked_whole(self) -> str:
        """Lane a lands after the integration moved with a change every check reads: its whole smoke runs and passes
        on the merge tree, which the results store keeps."""
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.integration_moves()
        self.lane("land", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        return self.git(self.integration, "rev-parse", "HEAD^{tree}")

    def test_a_check_the_merge_does_not_change_reuses_the_previous_tips_pass(self):
        previous = self.first_lane_smoked_whole()
        worktree = self.created("b", "docs/**")
        self.commit(worktree, {"docs/install.md": "journey\n"})
        self.lane("check", "b")
        runs = len(self.verify_runs())
        output = self.lane("land", "b")
        # lib-check is in the smoke but not in b's plan; the merge changes nothing it reads, and it passed on the
        # previous tip's tree.
        self.assertEqual(len(self.verify_runs()), runs)
        self.assertIn(f"lib-check: the merge changes nothing it reads, and it passed on the previous tip's tree "
                      f"{previous[:10]} (post-merge)", output)
        post = self.post("b")
        self.assertEqual((post["kind"], post["status"]), ("skip", "green"))
        self.assertEqual((post["check_results"]["lib-check"]["reused_by"],
                          post["check_results"]["lib-check"]["reused_from"]), ("previous-tip", previous))
        self.assertIn("lib-check", post["skipped"])
        self.assertRegex((self.state / "ledger.log").read_text(), r"post-merge b green skip .*not_rerun=1 ")

    def test_a_check_the_merge_changes_runs_although_it_passed_on_the_previous_tip(self):
        self.first_lane_smoked_whole()
        worktree = self.created("b", "calls/**")
        self.commit(worktree, {"calls/main.txt": "foo\n\n"})
        self.lane("check", "b")
        self.commit(self.integration, {"tooling/moved.txt": "moved again\n"}, "the integration moves again")
        self.lane("land", "b")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")

    def test_tooling_self_tests_leave_the_smoke_for_the_batch_gate(self):
        config = (self.integration / ".lanefather.toml").read_text()
        config = config.replace('verify = "verify.sh"\n', 'verify = "verify.sh"\ngate_only = ["lib-*"]\n')
        self.commit(self.integration, {".lanefather.toml": config}, "lib-check is a self-test of the tooling")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.integration_moves()
        output = self.lane("land", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check")
        self.assertIn("lib-check: left to the batch gate ([checks] gate_only)", output)
        post = self.post("a")
        self.assertNotIn("lib-check", post["check_results"])
        self.lane("review", "a")
        self.git(self.repo, "branch", "dev", "main")
        self.lane("promote", self.tip(), "--no-push", "--wait", "0")
        self.assertEqual(self.verify_runs()[-1], "FULL structure,lib,native,host-journeys")


if __name__ == "__main__":
    unittest.main()
