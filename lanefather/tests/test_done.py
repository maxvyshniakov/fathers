"""Tests of finishing a landed lane (lanefather.done, lanefather.worktrees): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import pathlib
import shutil
import unittest

from support import FIXTURES, TWO_FINDINGS, LaneCase, run_in_process


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

    def test_done_asks_spawnfather_to_reap_what_the_lane_owns_with_its_evidence(self):
        # fathers#171: the lane's worktree, build directory and scratch are Spawnfather's to remove, owner
        # lane:<main checkout>/<id>; done vouches for the integration tip and the disposable build output.
        worktree, record, cargo, *_ = self.landed_agent_lane()
        self.lane("done", "d")
        owner = f"lane:{self.repo.resolve()}/d"
        calls = self.spawnfather_calls()
        owned = {(c[c.index("--kind") + 1], c[c.index("--id") + 1]) for c in calls if c[0] == "own-resource"}
        self.assertIn(("worktree", str(worktree.resolve())), owned)
        self.assertIn(("dir", str(cargo.resolve())), owned)
        self.assertIn(("dir", str(pathlib.Path(record["scratch"][0]).resolve())), owned)
        self.assertTrue(all(c[c.index("--owner") + 1] == owner for c in calls if c[0] == "own-resource"))
        reaps = [c for c in calls if c[0] == "reap"]
        self.assertEqual(["--dry-run" in c for c in reaps], [True, False])
        tip = self.tip()
        for call in reaps:
            self.assertEqual(call[call.index("--owner") + 1], owner)
            self.assertEqual(call[call.index("--base") + 1], tip)
            self.assertIn("node_modules", call)
            self.assertNotIn("--override", call)
            self.assertNotIn("--stop-inside", call)
        self.assertFalse(worktree.exists())
        self.assertFalse(cargo.exists())

    def test_done_refuses_what_spawnfather_keeps_and_an_override_passes_its_reason(self):
        worktree, *_ = self.landed_agent_lane()
        self.env["FAKE_SPAWNFATHER_KEEP"] = "agent-d1$"
        output = self.lane("done", "d", ok=False)
        self.assertIn("Spawnfather keeps", output)
        self.assertIn("in use by pid 4242", output)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("d")["status"], "landed")
        self.lane("done", "d", "--force-reason", "the fake holder is a test")
        reaps = [c for c in self.spawnfather_calls() if c[0] == "reap"]
        self.assertEqual(reaps[-1][reaps[-1].index("--override") + 1], "the fake holder is a test")
        self.assertFalse(worktree.exists())
        self.assertEqual(self.record("d")["status"], "done")

    def test_done_without_spawnfather_refuses_with_exit_3_and_the_install_hint(self):
        # Decision 1 of fathers#171: interactive done fails closed as unattended done does.
        worktree, *_ = self.landed_agent_lane()
        (self.tmp / "bin" / "spawnfather").unlink()
        self.env["LANEFATHER_SPAWNFATHER"] = str(self.tmp / "bin" / "no-spawnfather")
        for words in (("done", "d"), ("done", "d", "--force-reason", "no override lifts this")):
            code, output = run_in_process(list(words), self.integration, self.env)
            self.assertEqual(3, code, output)
            self.assertIn("python3 install.py spawnfather", output)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("d")["status"], "landed")
        shutil.copy2(FIXTURES / "spawnfather", self.tmp / "bin" / "spawnfather")
        del self.env["LANEFATHER_SPAWNFATHER"]
        self.env["FAKE_SPAWNFATHER_OLD"] = "1"
        code, output = run_in_process(["done", "d"], self.integration, self.env)
        self.assertEqual(3, code, output)
        self.assertIn("needs lifetimes v1", output)

    def test_done_stops_what_is_left_inside_when_the_project_asks(self):
        # Decision 2 of fathers#171: [done] stop_inside, and the deprecated [hooks] process_scan, ask Spawnfather to
        # stop the unowned processes the lane left before it removes anything.
        worktree, *_ = self.landed_agent_lane()
        config = self.integration / ".lanefather.toml"
        config.write_text(config.read_text() + '\n[hooks]\nprocess_scan = "tools/process_scan.py"\n')
        output = self.lane("done", "d")
        self.assertIn("[hooks] process_scan is deprecated", output)
        self.assertTrue(all("--stop-inside" in c for c in self.spawnfather_calls() if c[0] == "reap"))
        self.assertFalse(worktree.exists())

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
