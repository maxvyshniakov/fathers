"""Finishing lanes by themselves (fathers#157): auto-done when a lane becomes finished, `keep`, `done --finished`
and start's disk repair, in scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import pathlib
import shutil
from typing import Any
import unittest
import unittest.mock

from support import TWO_FINDINGS, LaneCase, template_repository


class FinishedLaneTests(LaneCase):
    def agent_lane(self, lane_id: str, claim: str) -> pathlib.Path:
        self.start(lane_id, claim)
        worktree = self.repo / ".claude" / "worktrees" / f"agent-{lane_id}"
        self.git(self.repo, "worktree", "add", "-q", "-b", f"worktree-agent-{lane_id}", str(worktree), "integrate/q")
        self.git(worktree, "checkout", "-q", "-B", f"lane/{lane_id}", "integrate/q")
        self.lane("adopt", lane_id, str(worktree), f"lane/{lane_id}")
        self.commit(worktree, {claim: f"{lane_id}\n"})
        self.lane("check", lane_id)
        self.write(self.record(lane_id)["scratch"][0], "probe.log", "evidence")
        return worktree

    def assert_removed(self, lane_id: str, worktree: pathlib.Path) -> None:
        self.assertFalse(worktree.exists())
        self.assertNotIn(f"lane/{lane_id}", self.git(self.repo, "branch", "--list"))
        self.assertFalse(pathlib.Path(self.record(lane_id)["scratch"][0]).exists())
        self.assertEqual(self.record(lane_id)["status"], "done")

    def landed_unfinished(self, lane_id: str, claim: str) -> pathlib.Path:
        """A finished lane done refused when it became finished, as a transient refusal leaves it."""
        worktree = self.agent_lane(lane_id, claim)
        saved = self.comments.read_text()
        self.comments.write_text(json.dumps([]))
        self.lane("land", lane_id)
        self.assertIn("gh issue comment 404", self.lane("review", lane_id))
        self.comments.write_text(saved)
        self.assertEqual(self.record(lane_id)["status"], "landed")
        return worktree

    def test_marking_the_last_finding_removes_worktree_branches_and_scratch(self):
        self.review_text.write_text(TWO_FINDINGS)
        worktree = self.agent_lane("a", "lib/a.txt")
        self.lane("land", "a")
        self.lane("review", "a")
        self.lane("review", "a", "--mark", "1", "rejected:not a bug")
        self.assertTrue(worktree.exists())
        output = self.lane("review", "a", "--mark", "2", "rejected:covered elsewhere")
        self.assertIn("Lane a done", output)
        self.assertIn(f"removed {worktree.resolve()}", output)
        self.assert_removed("a", worktree)

    def test_a_kept_lane_survives_auto_done_and_done_finished_until_keep_off(self):
        worktree = self.agent_lane("a", "lib/a.txt")
        self.assertIn("Lane a kept", self.lane("keep", "a"))
        self.lane("land", "a")
        self.assertIn("Lane a kept (lanefather keep)", self.lane("review", "a"))
        self.assertTrue(worktree.exists())
        self.assertTrue(self.record("a")["landed"]["keep"], "the flag older installs read")
        output = self.lane("done", "--finished")
        self.assertIn("0 lanes finished", output)
        self.assertTrue(worktree.exists())
        self.lane("keep", "a", "--off")
        listed = self.lane("done", "--finished", "--dry-run")
        self.assertRegex(listed, r"a: \d+\.\d GB  " + str(worktree.resolve()))
        self.assertIn("1 finished lane", listed)
        self.assertIn("dry run: nothing removed", listed)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("a")["status"], "landed")
        self.assertIn("1 lane finished", self.lane("done", "--finished"))
        self.assert_removed("a", worktree)
        ledger = (self.state / "ledger.log").read_text()
        self.assertIn("keep a", ledger)
        self.assertIn("unkeep a", ledger)

    def test_auto_done_off_leaves_a_finished_lane_to_done_finished(self):
        # In the repository, so the registry lists the project with it.
        configured = self.repo / "auto-off.toml"
        configured.write_text((self.integration / ".lanefather.toml").read_text() + "\n[done]\nauto = false\n")
        self.env["LANEFATHER_CONFIG"] = str(configured)
        worktree = self.agent_lane("a", "lib/a.txt")
        self.lane("land", "a")
        self.assertIn("auto-done is off ([done] auto = false)", self.lane("review", "a"))
        self.assertTrue(worktree.exists())
        self.lane("done", "--finished")
        self.assert_removed("a", worktree)

    def test_done_finished_keeps_every_refusal_of_done(self):
        worktree = self.landed_unfinished("a", "lib/a.txt")
        self.write(worktree, "lib/a.txt", "uncommitted\n")
        output = self.lane("done", "--finished")
        self.assertIn("has uncommitted changes", output)
        self.assertIn("1 refused", output)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("a")["status"], "landed")

    def other_project(self) -> tuple[pathlib.Path, pathlib.Path]:
        """A second registered project with its own state directory and a finished lane its done refused."""
        other = self.tmp / "other"
        shutil.copytree(template_repository(), other, symlinks=True)
        state = self.tmp / "other-state"
        state.mkdir()
        worktree = other / ".claude" / "worktrees" / "lane-o"
        self.git(other, "worktree", "add", "-q", "-b", "lane/o", str(worktree))
        common = str((other / ".git").resolve())
        self.write(state, "o.json", json.dumps({
            "id": "o", "ticket": "none", "status": "landed", "branch": "lane/o", "base": "main",
            "common_dir": common, "worktree": str(worktree.resolve()), "claims": ["x/**"], "after": [],
            "scratch": [str(state / "o.scratch")], "created": "2026-11-16T00:00:00Z", "overrides": [],
            "landed": {"merge": self.git(other, "rev-parse", "HEAD"), "integration": str(other)},
            "post_merge": {"status": "green"}}))
        self.write(state, "o.review.json", json.dumps({"status": "exempt", "exempt": "documentation only"}))
        self.write(state / "o.scratch", "build.log", "x" * 4096)
        registry = pathlib.Path(self.env["LANEFATHER_REGISTRY"])
        known: dict[str, Any] = json.loads(registry.read_text()) if registry.exists() else {"v": 1, "projects": []}
        known["projects"].append({"state_dir": str(state), "common_dir": common,
                                  "config": str(other / ".lanefather.toml"), "project": "Other",
                                  "seen": "2026-11-16T00:00:00Z"})
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps(known))
        return worktree, state

    def test_start_below_the_disk_floor_frees_the_finished_lanes_of_every_project_and_admits(self):
        from lanefather import host
        worktree = self.landed_unfinished("a", "lib/a.txt")
        kept = self.landed_unfinished("k", "lib/k.txt")
        self.lane("keep", "k")
        other, other_state = self.other_project()

        def disk(project: Any = None) -> float:
            del project
            freed = self.record("a")["status"] == "done"
            return 500.0 if freed else 100.0

        with unittest.mock.patch.object(host, "disk_free_gb", disk):
            output = self.lane("start", "n", "--ticket", "404", "--base", "integrate/q", "--claims", "z/**",
                               process=False)
        self.assertIn("free disk is 100 GB, under 150 GB: running done", output)
        self.assertIn("Lane a done", output)
        self.assertIn("Lane o done", output)
        self.assertIn("2 lanes finished", output)
        self.assert_removed("a", worktree)
        self.assertFalse(other.exists())
        self.assertEqual(json.loads((other_state / "o.json").read_text())["status"], "done")
        self.assertIn("done o", (other_state / "ledger.log").read_text())
        self.assertTrue(kept.exists(), "a kept lane stays")
        self.assertEqual(self.record("n")["status"], "planned")

    def test_start_still_short_of_disk_after_the_repair_says_what_it_freed(self):
        from lanefather import host
        self.landed_unfinished("a", "lib/a.txt")
        def short(project: Any = None) -> float:
            del project
            return 100.0

        with unittest.mock.patch.object(host, "disk_free_gb", short):
            output = self.lane("start", "n", "--ticket", "404", "--base", "integrate/q", "--claims", "z/**",
                               process=False, ok=False)
        self.assertIn("done on finished lanes freed", output)
        self.assertIn("on 1", output)
        self.assertEqual(self.record("a")["status"], "done")
        self.assertFalse((self.state / "n.json").exists())


if __name__ == "__main__":
    unittest.main()
