"""Tests of the landing lock (lanefather.landing): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import shutil
import time
import unittest

from support import FIXTURES, LaneCase


class LandingLockTests(LaneCase):
    """land holds the landing lock that .githooks/reference-transaction checks for main and dev (B2)."""

    def setUp(self):
        super().setUp()
        self.dev = self.repo / ".claude" / "worktrees" / "dev"
        self.git(self.repo, "worktree", "add", "-q", "-b", "dev", str(self.dev))
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        shutil.copy2(FIXTURES / "reference-transaction", hooks / "reference-transaction")
        self.git(self.repo, "config", "core.hooksPath", str(hooks))
        self.env = {k: v for k, v in self.env.items()
                    if not k.startswith(("CODEX_", "LANDING_")) or k == "CODEX_HOME"}
        self.env["CLAUDECODE"] = "1"
        self.landing = self.repo / ".git" / "landing.lock"
        self.lane("start", "into-dev", "--ticket", "404", "--base", "dev", "--claims", "src/**", "--create")
        self.commit(self.record("into-dev")["worktree"], {"src/x.txt": "x\n"})
        self.lane("check", "into-dev")

    def test_an_agent_lands_into_a_dev_worktree_under_the_lock(self):
        before = self.git(self.dev, "rev-parse", "HEAD")
        # #570: promote holds only lanes not yet in dev, so a lane lands on dev only after its review.
        output = self.lane("land", "into-dev", "--wait", "0", ok=False)
        self.assertIn("is on dev", output)
        self.assertIn("lanefather review into-dev", output)
        self.assertEqual(before, self.git(self.dev, "rev-parse", "HEAD"))
        self.lane("review", "into-dev")
        reviewed = self.review("into-dev")
        self.assertIn("post-merge green", self.lane("land", "into-dev", "--wait", "0"))
        self.assertEqual(before, self.git(self.dev, "rev-parse", "HEAD^1"))
        self.assertFalse(self.landing.exists(), "land leaves the landing lock behind")
        self.assertEqual(self.review("into-dev"), reviewed, "land keeps the review it was given")
        self.assertEqual(sum(1 for c in self.codex_calls() if c[2] == "exec"), 1)
        ledger = (self.state / "ledger.log").read_text()
        self.assertIn("land into-dev onto dev: reviewed by sol", ledger)
        self.assertIn("review into-dev kept at land: finished", ledger)

    def test_an_unreviewed_land_onto_dev_needs_a_logged_override(self):
        self.assertIn("post-merge green", self.lane("land", "into-dev", "--wait", "0", "--force-reason",
                                                    "owner hotfix"))
        self.assertIn("override land into-dev onto dev unreviewed: owner hotfix",
                      (self.state / "ledger.log").read_text())

    def test_land_waits_for_another_holder_and_then_refuses(self):
        self.lane("review", "into-dev")
        self.landing.write_text(f"token theirs\nholder lanefather promote abc (pid 4242)\nacquired {int(time.time())}\n")
        before = self.git(self.dev, "rev-parse", "HEAD")
        output = self.lane("land", "into-dev", "--wait", "0", ok=False)
        self.assertIn("held by lanefather promote abc (pid 4242)", output)
        self.assertEqual(before, self.git(self.dev, "rev-parse", "HEAD"))
        self.assertEqual("active", self.record("into-dev")["status"])


if __name__ == "__main__":
    unittest.main()
