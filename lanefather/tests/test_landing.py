"""Tests of the landing lock (lanefather.landing): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

from support import ROOT, LaneCase


class LandingLockTests(LaneCase):
    """land holds the landing lock that .githooks/reference-transaction checks for main and dev (B2)."""

    def setUp(self):
        super().setUp()
        self.dev = self.repo / ".claude" / "worktrees" / "dev"
        self.git(self.repo, "worktree", "add", "-q", "-b", "dev", str(self.dev))
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        shutil.copy2(ROOT / "hooks" / "reference-transaction", hooks / "reference-transaction")
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


class LandingTakeoverTests(unittest.TestCase):
    """A stale landing lock is taken over and the takeover logged; a live holder's never is (fathers#129)."""

    def setUp(self):
        from lanefather import config
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-landing-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.common = self.tmp / "common"
        self.common.mkdir()
        self.lock = self.common / "landing.lock"
        self.state = self.tmp / "state"
        self.state.mkdir()
        patcher = unittest.mock.patch.dict(os.environ, {"LANEFATHER_STATE": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        config.use(config.Config())
        self.addCleanup(config.use, None)

    def held(self, pid: int, acquired: float) -> str:
        text = (f"token theirs\nholder other land (pid {pid})\nacquired {int(acquired)}\n"
                f"pid {pid}\nhost {os.uname().nodename}\n")
        self.lock.write_text(text)
        return text

    def sleeper(self) -> subprocess.Popen[bytes]:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def take(self) -> str:
        from lanefather import landing
        with landing.landing(str(self.common), "me", 0) as env:
            fields = landing.read_landing(self.lock) or {}
            self.assertEqual(fields.get("token"), env["LANDING_TOKEN"])
            self.assertEqual(fields.get("pid"), str(os.getpid()))
        self.assertFalse(self.lock.exists(), "the taker releases the lock it took")
        return (self.state / "ledger.log").read_text()

    def test_a_killed_holders_lock_is_taken_over(self):
        ready = self.tmp / "ready"
        code = ("import pathlib, sys, time\nfrom lanefather import landing\n"
                "with landing.landing(sys.argv[1], 'killed land', 0):\n"
                "    pathlib.Path(sys.argv[2]).touch()\n    time.sleep(60)\n")
        child = subprocess.Popen([sys.executable, "-c", code, str(self.common), str(ready)], cwd=self.tmp,
                                 env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        deadline = time.monotonic() + 30
        while not ready.exists():
            self.assertIsNone(child.poll(), "the holder exited before it took the lock")
            self.assertLess(time.monotonic(), deadline, "the holder never took the lock")
            time.sleep(0.05)
        child.send_signal(signal.SIGKILL)
        child.wait()
        self.assertIn("holder killed land", self.lock.read_text(), "a killed holder leaves its lock behind")
        ledger = self.take()
        self.assertIn(f"landing lock taken over from killed land (holder pid {child.pid} has exited) by me", ledger)

    def test_an_expired_lock_is_taken_over_even_from_a_live_holder(self):
        from lanefather import landing
        self.held(self.sleeper().pid, time.time() - landing.LANDING_TTL - 60)
        self.assertIn("(expired) by me", self.take())

    def test_a_live_holders_lock_is_never_taken_over(self):
        from lanefather import landing
        from lanefather.errors import LaneError
        text = self.held(self.sleeper().pid, time.time())
        with self.assertRaises(LaneError) as caught, landing.landing(str(self.common), "me", 0):
            self.fail("the lock of a live holder was taken")
        self.assertIn("held by other land", str(caught.exception))
        self.assertEqual(self.lock.read_text(), text)
        self.assertFalse((self.state / "ledger.log").exists())


if __name__ == "__main__":
    unittest.main()
