"""Tests of finishing a landed lane (lanefather.done, lanefather.worktrees): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any
import unittest

from support import TWO_FINDINGS, LaneCase


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

    def process_in(self, cwd: Any, name: str, ignore_term: bool = False) -> subprocess.Popen[bytes]:
        """A process of this test's own whose command line shows `name`; it runs until stopped."""
        handler = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else ""
        child = subprocess.Popen([sys.executable, "-c", handler + "import time; time.sleep(300)", name], cwd=cwd,
                                 start_new_session=True)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        deadline = time.monotonic() + 30
        while name not in subprocess.run(["ps", "-o", "command=", "-p", str(child.pid)], capture_output=True,
                                         text=True).stdout:
            self.assertLess(time.monotonic(), deadline, "the process did not start")
            time.sleep(0.05)
        return child

    def test_done_stops_through_slotfather_sweep_what_the_lane_left_in_its_worktree(self):
        # #661: a verify outlived the lane that started it, holding memory for a day.
        self.use_real_process_scan()
        worktree, *_ = self.landed_agent_lane()
        left = self.process_in(worktree / "desktop", "orphan-661")
        self.env["FAKE_SLOTFATHER_SWEEP_STOP"] = "orphan-661"
        output = self.lane("done", "d")
        self.assertIn(f"stopped pid {left.pid} ", output)
        self.assertEqual(left.wait(timeout=10), -signal.SIGTERM)
        self.assertFalse(worktree.exists())
        self.assertIn(f"slotfather sweep stopped pid {left.pid}", (self.state / "ledger.log").read_text())
        sweeps = [json.loads(line) for line in (self.tmp / "slotfather.log").read_text().splitlines()]
        self.assertIn(["--json", "--live", "--pid", str(left.pid)],
                      [call["args"] for call in sweeps if call["command"] == "sweep"])

    def test_done_refuses_while_a_process_slotfather_sweep_does_not_stop_works_in_the_worktree(self):
        # Review of #661: a session, a terminal, a lease or a process of no known kind holds the worktree.
        self.use_real_process_scan()
        worktree, *_ = self.landed_agent_lane()
        held = self.process_in(worktree, "held-661")
        self.env["FAKE_SLOTFATHER_SWEEP_SPARE"] = "held-661"
        output = self.lane("done", "d", ok=False)
        self.assertIn(f"pid {held.pid}", output)
        self.assertIn("inside the live lease lane-641-other", output)
        del self.env["FAKE_SLOTFATHER_SWEEP_SPARE"]
        self.assertIn("slotfather sweep does not take it for left behind", self.lane("done", "d", ok=False))
        self.assertTrue(worktree.exists())
        self.assertIsNone(held.poll())
        self.assertEqual(self.record("d")["status"], "landed")

    def test_done_refuses_without_a_verdict_or_when_the_stop_fails(self):
        # Review of #661: a sweep that fails, or a stop it cannot confirm, keeps the worktree.
        self.use_real_process_scan()
        worktree, *_ = self.landed_agent_lane()
        stubborn = self.process_in(worktree, "orphan-661", ignore_term=True)
        self.env.update(FAKE_SLOTFATHER_SWEEP_STOP="orphan-661", FAKE_SLOTFATHER_SWEEP_FAIL="queue unreadable")
        self.assertIn("may be stopped is unknown", self.lane("done", "d", ok=False))
        del self.env["FAKE_SLOTFATHER_SWEEP_FAIL"]
        output = self.lane("done", "d", ok=False)
        self.assertIn("it is still in use", output)
        self.assertIn(f"pid {stubborn.pid} (", output)
        self.assertIn("; sweep: failed)", output)
        self.assertTrue(worktree.exists())
        self.assertIsNone(stubborn.poll())
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
    """#446: land finishes the lane after a green post-merge smoke, so it no longer counts toward the lane cap."""

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
        # A released lane does not count toward the lane cap or hold claims.
        for index, claim in enumerate(("lib/**", "x/**", "y/**")):
            self.start(f"n{index}", claim)
        self.assertIn("3 of 3 lanes planned or active", self.lane("list"))
        # done finishes it once the agent lets go of the worktree.
        self.assertIn("claude agent agent-a", self.lane("done", "a", ok=False))
        self.assertTrue(worktree.exists())
        self.git(self.repo, "worktree", "unlock", str(worktree))
        self.lane("done", "a")
        self.assertFalse(worktree.exists())
        self.assertEqual(self.record("a")["status"], "done")

    def test_a_refused_done_leaves_the_lane_landed_outside_the_lane_cap(self):
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


class DoneReadinessUnifiedTests(LaneCase):
    """fathers#213, fathers#227: `done.unfinished`, `done.finish_refusals` and `done.settle_lane` share
    `postmerge.decide_completion` and agree across `Absent` review receipts, unrun/cleared post-merge smokes, and kept lanes."""

    def _landed_lane(self, lane_id: str = "a", path: str = "lib/a.txt", *, keep: bool = False) -> pathlib.Path:
        wt = pathlib.Path(self.created(lane_id, path))
        self.commit(wt, {path: f"{lane_id}\n"})
        self.lane("check", lane_id)
        self.lane("land", lane_id, *(("--keep",) if keep else ()))
        return wt

    def test_absent_review_and_unrun_smoke_across_readiness_callers(self) -> None:
        import contextlib
        import io
        import os
        from unittest import mock

        from lanefather import config, done, postmerge, state
        from lanefather.errors import LaneError

        wt = self._landed_lane("a")
        # `LANEFATHER_REVIEW_START=manual` in LaneCase leaves `<id>.review.json` absent.
        self.assertFalse((self.state / "a.review.json").exists())

        with mock.patch.dict(os.environ, self.env, clear=False):
            cfg = config.load(self.integration / ".lanefather.toml")
            config.use(cfg)
            self.addCleanup(config.use, None)
            store = state.Store(cfg, directory=self.state)
            rec = store.load("a")

            # Absent review: explicit `done <id>` allows it, while `done --finished`, unattended `finish_refusals`
            # and `settle_lane(..., mode=AUTO_AFTER_REVIEW)` refuse or ignore it.
            self.assertTrue(done.done_readiness(rec, store, allow_absent_review=True, allow_kept=True).ready)
            self.assertFalse(done.done_readiness(rec, store, allow_absent_review=False, allow_kept=True).ready)
            self.assertIn("no review", done.unfinished(rec, store) or "")
            with self.assertRaises(LaneError) as unattended_err:
                done.finish_refusals("a", None, store, unattended=True)
            self.assertIn("no review", str(unattended_err.exception))
            done.settle_lane(store, "a", mode=postmerge.CompletionMode.AUTO_AFTER_REVIEW)
            self.assertEqual(self.record("a")["status"], "landed")
            self.assertTrue(wt.exists())

            # Unrun post-merge smoke (e.g. while `land` is still in phase 2): all callers refuse without override.
            rec_unrun = dict(rec)
            rec_unrun.pop("post_merge", None)
            (self.state / "a.json").write_text(json.dumps(rec_unrun))
            (self.state / "a.review.json").write_text(json.dumps({"status": "exempt", "exempt": "test"}))
            self.assertEqual(done.unfinished(rec_unrun, store), "its post-merge smoke is not run")
            with self.assertRaises(LaneError) as attended_unrun:
                done.finish_refusals("a", None, store, unattended=False)
            self.assertIn("post-merge smoke is not run", str(attended_unrun.exception))
            with self.assertRaises(LaneError) as unattended_unrun:
                done.finish_refusals("a", None, store, unattended=True)
            self.assertIn("post-merge smoke is not run", str(unattended_unrun.exception))

            # A kept lane whose smoke is unrun or uncleared red does NOT print a premature kept-finish notice.
            rec_kept_unrun = dict(rec_unrun, keep=True)
            (self.state / "a.json").write_text(json.dumps(rec_kept_unrun))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                done.settle_lane(store, "a", mode=postmerge.CompletionMode.AUTO_AFTER_REVIEW)
            self.assertEqual(buf.getvalue(), "")

            rec_kept_red = dict(rec, keep=True, post_merge={"status": "red"})
            (self.state / "a.json").write_text(json.dumps(rec_kept_red))
            buf_red = io.StringIO()
            with contextlib.redirect_stdout(buf_red):
                done.settle_lane(store, "a", mode=postmerge.CompletionMode.AUTO_AFTER_REVIEW)
            self.assertEqual(buf_red.getvalue(), "")

    def test_review_auto_done_finishes_lane_with_cleared_red_post_merge_smoke(self) -> None:
        from lanefather import results

        wt = pathlib.Path(self.created("a", "lib/**"))
        self.commit(wt, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.integration_moves()
        self.env["FIXTURE_STATUS"] = "post-merge-a:lib-check=failed"
        self.assertIn("post-merge red", self.lane("land", "a", ok=False))
        del self.env["FIXTURE_STATUS"]
        self.assertEqual(self.record("a")["post_merge"]["status"], "red")

        # Uncleared red smoke: even a clean review leaves the lane landed.
        self.lane("review", "a")
        self.assertEqual(self.record("a")["status"], "landed")
        self.assertTrue(wt.exists())

        # Clear the red smoke via a later green batch gate on the integration branch, then re-review with findings
        # and mark the last finding: `review_complete_finish` now auto-finishes the lane and records `cleared_by`.
        tip = self.commit(self.integration, {"notes.txt": "later\n"}, "later on integration")
        gate_path = self.state / f"promote-{tip[:10]}.gate.json"
        gate_path.write_text(json.dumps({
            "version": 1,
            "lane": f"promote-{tip[:10]}",
            "sha": tip,
            "full": True,
            "status": "green",
            "tree": self.git(self.repo, "rev-parse", tip + "^{tree}"),
            "finished": "2999-01-01T00:00:00Z",
            "platform": results.host_platform(),
        }))
        self.review_text.write_text(TWO_FINDINGS)
        self.lane("review", "a", "--force-reason", "re-review with findings")
        self.assertEqual(self.record("a")["status"], "landed")
        self.lane("review", "a", "--mark", "1", "rejected:ok")
        self.assertEqual(self.record("a")["status"], "landed")
        out = self.lane("review", "a", "--mark", "2", "rejected:ok")
        self.assertIn("Lane a done", out)
        self.assertIn(f"cleared by the batch gate of {tip[:10]}", out)
        self.assertFalse(wt.exists())
        final = self.record("a")
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["post_merge"]["cleared_by"]["kind"], "gate")


if __name__ == "__main__":
    unittest.main()
