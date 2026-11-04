"""Tests of finishing a landed lane (lanefather.done, lanefather.worktrees): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import pathlib
import signal
import subprocess
import unittest

from support import LaneCase


class DoneTests(LaneCase):
    """B4 done: cleanup after landing, claims released, ticket comment verified."""

    def landed_agent_lane(self):
        self.start("d", "lib/**")
        worktree = self.repo / ".claude" / "worktrees" / "agent-d1"
        self.git(self.repo, "worktree", "add", "-q", "-b", "worktree-agent-d1", str(worktree), "integrate/q")
        self.git(worktree, "checkout", "-q", "-B", "lane/d", "integrate/q")
        self.lane("adopt", "d", str(worktree), "lane/d")
        self.commit(worktree, {"lib/api.txt": "foo\nd\n"})
        self.lane("check", "d")
        self.assertIn("kept (--keep)", self.lane("land", "d", "--keep"))
        self.write(worktree, "desktop/node_modules/pkg/index.js", "x")
        self.write(worktree, "native/core/target/debug/libcore.a", "x")
        self.write(worktree, ".build/verification-runs/x/report.json", "{}")
        record = self.record("d")
        self.write(record["scratch"][0], "probe.log", "evidence")
        cargo = self.cache / "lane-d-target"
        self.write(cargo, "debug/libcore.a", "x")
        mine, other = self.cache / "cargo-v2" / ("a" * 64), self.cache / "cargo-v2" / ("b" * 64)
        self.write(mine, "release/libstraylight_core.d", f"{worktree.resolve()}/native/core/src/lib.rs:\n")
        self.write(other, "release/libstraylight_core.d", "/elsewhere/native/core/src/lib.rs:\n")
        return worktree, record, cargo, mine, other

    def test_done_removes_worktree_branches_targets_and_scratch_and_releases_claims(self):
        worktree, record, cargo, mine, other = self.landed_agent_lane()
        # A build in the base checkout a red check classified in (#568).
        base = self.cache / "cargo-v2" / ("c" * 64)
        self.write(base, "debug/libcore.d", f"{record['scratch'][0]}/base-0123456789ab/native/core/src/lib.rs:\n")
        self.lane("done", "d")
        self.assertFalse(base.exists())
        branches = self.git(self.repo, "branch", "--list")
        self.assertFalse(worktree.exists())
        self.assertNotIn(str(worktree), self.git(self.repo, "worktree", "list"))
        self.assertNotIn("lane/d", branches)
        self.assertNotIn("worktree-agent-d1", branches)
        self.assertFalse(pathlib.Path(record["scratch"][0]).exists())
        self.assertFalse(cargo.exists())
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())
        self.assertEqual(self.record("d")["status"], "done")
        self.assertIn("issue view 404", (self.tmp / "gh.log").read_text())
        self.start("next", "lib/**")

    def process_in(self, cwd, name):
        """A process of this test's own whose command line shows `name`; it runs until stopped."""
        program = self.tmp / "processes" / name  # not bin/: the reviewers' stand-in claude lives there
        program.parent.mkdir(exist_ok=True)
        if not program.exists():
            program.symlink_to("/bin/sleep")
        child = subprocess.Popen([str(program), "300"], cwd=cwd, start_new_session=True)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        return child

    def test_done_stops_the_processes_left_in_the_worktree_before_removing_it(self):
        # #661: a dev server and a verify outlived the lane that started them, holding memory for a day.
        worktree, *_ = self.landed_agent_lane()
        server = self.process_in(worktree / "desktop", "storybook-dev")
        output = self.lane("done", "d")
        self.assertIn(f"stopped pid {server.pid} ", output)
        self.assertEqual(server.wait(timeout=10), -signal.SIGTERM)
        self.assertFalse(worktree.exists())
        self.assertIn(f"stopped pid {server.pid}", (self.state / "ledger.log").read_text())

    def test_done_refuses_while_a_claude_session_works_in_the_worktree(self):
        worktree, *_ = self.landed_agent_lane()
        session = self.process_in(worktree, "claude")
        output = self.lane("done", "d", ok=False)
        self.assertIn(f"pid {session.pid}", output)
        self.assertIn("Claude Code session", output)
        self.assertTrue(worktree.exists())
        self.assertIsNone(session.poll())
        self.assertEqual(self.record("d")["status"], "landed")

    def test_done_without_a_ticket_comment_is_refused_and_keeps_everything(self):
        worktree, *_ = self.landed_agent_lane()
        self.comments.write_text(json.dumps([{"body": "old", "createdAt": "2000-01-01T00:00:00Z"}]))
        output = self.lane("done", "d", ok=False)
        self.assertIn("gh issue comment 404", output)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("d")["status"], "landed")

    def test_done_refuses_an_unlanded_lane(self):
        worktree = self.created("u", "lib/**")
        self.commit(worktree, {"lib/api.txt": "unmerged work\n"})
        self.assertIn("not landed", self.lane("done", "u", ok=False))
        self.assertTrue(pathlib.Path(worktree).exists())


class AutoDoneTests(LaneCase):
    """#446: land finishes the lane after a green post-merge smoke, so its slot frees at once."""

    def agent_lane(self, lane_id, claim):
        self.start(lane_id, claim)
        worktree = self.repo / ".claude" / "worktrees" / f"agent-{lane_id}"
        self.git(self.repo, "worktree", "add", "-q", "-b", f"worktree-agent-{lane_id}", str(worktree), "integrate/q")
        self.git(worktree, "checkout", "-q", "-B", f"lane/{lane_id}", "integrate/q")
        self.lane("adopt", lane_id, str(worktree), f"lane/{lane_id}")
        self.commit(worktree, {claim: f"{lane_id}\n"})
        self.lane("check", lane_id)
        return worktree

    def test_land_keeps_the_lane_for_its_review_and_a_clean_review_runs_done(self):
        worktree = self.agent_lane("a", "lib/a.txt")
        output = self.lane("land", "a")
        self.assertIn("post-merge green", output)
        # #570: the fix round of a review runs in the lane's worktree, so land keeps it until the review is marked.
        self.assertIn("kept for its review", output)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("a")["status"], "landed")
        output = self.lane("review", "a")
        self.assertIn("Lane a done", output)
        self.assertFalse(worktree.exists())
        self.assertEqual(self.record("a")["status"], "done")
        self.assertNotIn("lane/a", self.git(self.repo, "branch", "--list"))

    def test_a_worktree_an_agent_still_locks_is_kept_and_the_lane_released(self):
        worktree = self.agent_lane("a", "lib/a.txt")
        self.git(self.repo, "worktree", "lock", "--reason", "claude agent agent-a (pid 1 start now)", str(worktree))
        self.lane("land", "a")
        output = self.lane("review", "a")
        self.assertIn("released", output)
        self.assertIn("lanefather done a", output)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("a")["status"], "released")
        self.assertIn("release a", (self.state / "ledger.log").read_text())
        # A released lane holds no slot and no claims.
        for index, claim in enumerate(("lib/**", "x/**", "y/**")):
            self.start(f"n{index}", claim)
        self.assertIn("3 of 3 lanes active", self.lane("list"))
        # done finishes it once the agent lets go of the worktree.
        self.assertIn("claude agent agent-a", self.lane("done", "a", ok=False))
        self.assertTrue(worktree.exists())
        self.git(self.repo, "worktree", "unlock", str(worktree))
        self.lane("done", "a")
        self.assertFalse(worktree.exists())
        self.assertEqual(self.record("a")["status"], "done")

    def test_a_refused_done_leaves_the_lane_landed_without_a_slot(self):
        worktree = self.agent_lane("a", "lib/a.txt")
        self.comments.write_text(json.dumps([]))
        self.lane("land", "a")
        output = self.lane("review", "a")
        self.assertIn("gh issue comment 404", output)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("a")["status"], "landed")
        for index, claim in enumerate(("lib/**", "x/**", "y/**")):
            self.start(f"n{index}", claim)

    def test_a_red_smoke_keeps_the_lane_and_its_worktree(self):
        rename = self.created("rename", "lib/**", "calls/main.txt")
        caller = self.created("caller", "calls/new.txt")
        self.commit(rename, {"lib/api.txt": "bar\n", "calls/main.txt": "bar\n"})
        self.commit(caller, {"calls/new.txt": "foo\n"})
        self.lane("check", "rename")
        self.lane("check", "caller")
        self.lane("land", "rename")
        self.lane("land", "caller", ok=False)
        self.assertTrue(pathlib.Path(caller).exists())
        self.assertEqual(self.record("caller")["status"], "landed")


if __name__ == "__main__":
    unittest.main()
