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

    def test_done_reaps_unrecorded_canonical_scratch_with_stale_base_checkout(self):
        """fathers#259: done reaps `<state>/<id>.scratch` even when missing from record['scratch'] and cleans stale
        `base-*` checkouts inside it first."""
        worktree, record, *_ = self.landed_agent_lane()
        canonical = self.state / "d.scratch"
        self.write(canonical, "probe.log", "leftover evidence\n")
        stale_base = canonical / "base-0123456789ab"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(stale_base), "integrate/q")
        # Simulate a legacy or damaged record whose scratch list omits the canonical path.
        rec_path = self.state / "d.json"
        mutated = dict(record)
        mutated["scratch"] = []
        rec_path.write_text(json.dumps(mutated))
        self.lane("done", "d")
        self.assertFalse(worktree.exists())
        self.assertFalse(canonical.exists())
        self.assertNotIn(str(stale_base), self.git(self.repo, "worktree", "list"))
        self.assertEqual(self.record("d")["status"], "done")

    def test_done_never_follows_symlinked_scratch_outside_state_dir(self):
        """fathers#259: done never follows a symlinked scratch pointing outside the state directory (both when
        previously owned by Spawnfather and when unowned)."""
        worktree, record, *_ = self.landed_agent_lane()
        canonical = self.state / "d.scratch"
        if canonical.exists():
            shutil.rmtree(canonical)
        outside = self.tmp / "precious-outside"
        self.write(outside, "keep.txt", "do not delete\n")
        # 1. Previously owned canonical scratch replaced with symlink -> refused cleanly, target untouched.
        canonical.symlink_to(outside)
        refused = self.lane("done", "d", ok=False)
        self.assertIn("a symbolic link", refused)
        self.assertTrue((outside / "keep.txt").exists(), "symlink target outside state dir must never be deleted")
        # 2. Unowned symlinked scratch (not registered in Spawnfather) -> skipped safely, done succeeds, target intact.
        canonical.unlink()
        extra_link = self.state / "extra-link.scratch"
        extra_link.symlink_to(outside)
        mutated = dict(record)
        mutated["scratch"] = [str(extra_link)]
        (self.state / "d.json").write_text(json.dumps(mutated))
        self.lane("done", "d")
        self.assertFalse(worktree.exists())
        self.assertTrue(extra_link.is_symlink())
        self.assertTrue((outside / "keep.txt").exists(), "symlink target outside state dir must never be deleted")
        self.assertEqual((outside / "keep.txt").read_text(), "do not delete\n")
        self.assertEqual(self.record("d")["status"], "done")

    def test_done_reaps_remaining_store_scratch_if_spawnfather_leaves_it_and_never_deletes_outside_store(self):
        """fathers#259: if Spawnfather returns without removing `<state>/<id>.scratch`, done still reaps it inside
        the store directory, while never directly deleting a scratch directory outside `store.directory`."""
        from unittest import mock

        from lanefather import config, done, lifetimes, state

        worktree, record, *_ = self.landed_agent_lane()
        canonical = self.state / "d.scratch"
        self.write(canonical, "leftover.log", "x" * 1024)
        outside = self.tmp / "outside-extra-scratch"
        self.write(outside, "keep.txt", "outside")
        rec_path = self.state / "d.json"
        mutated = dict(record)
        mutated["scratch"] = [str(canonical), str(outside)]
        rec_path.write_text(json.dumps(mutated))

        real_reap = lifetimes.Spawnfather.reap

        def reap_leaving_scratch(self_sf: lifetimes.Spawnfather, lane: str, ev: lifetimes.Evidence,
                                 *, dry_run: bool, reason: str) -> lifetimes.Reaped:
            res = real_reap(self_sf, lane, ev, dry_run=dry_run, reason=reason)
            if not dry_run:
                # Recreate both dirs as if Spawnfather skipped dir cleanup while returning no kept refusals.
                self.write(canonical, "leftover.log", "x" * 1024)
                self.write(outside, "keep.txt", "outside")
            return res

        cfg = config.load(self.integration / ".lanefather.toml")
        store = state.Store(cfg, directory=self.state)
        with (mock.patch.dict("os.environ", self.env, clear=False),
              mock.patch.object(lifetimes.Spawnfather, "reap", reap_leaving_scratch)):
            freed = done.finish("d", None, store)
        self.assertGreater(freed, 0)
        self.assertFalse(worktree.exists())
        self.assertFalse(canonical.exists(), "canonical scratch inside store must be reaped")
        self.assertTrue((outside / "keep.txt").exists(), "fallback must never delete outside store.directory")


class AutoDoneTests(LaneCase):
    """#446: land finishes the lane after a green post-merge smoke, so it no longer counts toward the lane cap."""

    def agent_lane(self, lane_id: str, claim: str) -> pathlib.Path:
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


class PostMergeSmokeLifecycleTests(LaneCase):
    """fathers#56: post-merge smoke recorded while running, dead holder reads as killed, `smoke --rerun` / `--accept`,
    and `done` requiring green / skipped / accepted."""

    def _checked(self, lane_id: str, path: str = "lib/a.txt") -> pathlib.Path:
        wt = pathlib.Path(self.created(lane_id, path))
        self.commit(wt, {path: f"{lane_id}\n"})
        self.lane("check", lane_id)
        return wt

    def test_running_smoke_shows_running_and_killed_holder_reads_killed_then_rerun_passes_done(self) -> None:
        wt = self._checked("a", "lib/a.txt")
        self.integration_moves()
        hold = self.tmp / "hold-smoke"
        land_proc = self.background_land("a", hold)
        checkout = self.wait_started(hold, land_proc)
        try:
            # While the smoke is held, post_merge is recorded as `running` with pid, start, checkout, and lane_created.
            rec = self.record("a")
            pm = rec["post_merge"]
            self.assertEqual(pm["status"], "running")
            self.assertEqual(pm["pid"], land_proc.pid)
            self.assertEqual(pm["checkout"], checkout)
            self.assertEqual(pm["lane_created"], rec["created"])
            self.assertIn("start", pm)
            receipt = json.loads((self.state / "a.post-merge.json").read_text())
            self.assertEqual(receipt["status"], "running")
            self.assertEqual(receipt["lane_created"], rec["created"])

            # `list`, `status`, and `smoke <id>` show `post-merge running`, and `done` refuses.
            self.assertIn("post-merge running", self.lane("list"))
            self.assertIn("post-merge running", self.lane("status"))
            status_json = json.loads(self.lane("status", "--json"))
            self.assertEqual(status_json["projects"][0]["lanes"][0]["post_merge"], "running")
            self.assertIn("post-merge running", self.lane("smoke", "a"))
            done_running = self.lane("done", "a", ok=False)
            self.assertIn("post-merge smoke is running", done_running)
            self.assertTrue(wt.exists())

            # Kill the holder process -> reads as `killed`, and `done` still refuses.
            land_proc.kill()
            land_proc.wait(timeout=10)
        finally:
            if land_proc.poll() is None:
                land_proc.kill()
                land_proc.wait(timeout=10)

        self.assertIn("post-merge killed", self.lane("list"))
        self.assertIn("post-merge killed", self.lane("status"))
        status_after = json.loads(self.lane("status", "--json"))
        self.assertEqual(status_after["projects"][0]["lanes"][0]["post_merge"], "killed")
        self.assertIn("post-merge killed", self.lane("smoke", "a"))
        done_killed = self.lane("done", "a", ok=False)
        self.assertIn("post-merge smoke was killed", done_killed)
        self.assertIn("lanefather smoke a --rerun", done_killed)
        self.assertIn("lanefather smoke a --accept REASON", done_killed)
        self.assertTrue(wt.exists())

        # `smoke <id> --rerun` runs to green, after which `done` passes.
        rerun_out = self.lane("smoke", "a", "--rerun")
        self.assertIn("post-merge green", rerun_out)
        self.assertEqual(self.record("a")["post_merge"]["status"], "green")
        self.lane("done", "a")
        self.assertEqual(self.record("a")["status"], "done")
        self.assertFalse(wt.exists())

    def test_red_smoke_refusal_and_accept_override_let_done_pass(self) -> None:
        wt = self._checked("a", "lib/a.txt")
        self.integration_moves()
        self.env["FIXTURE_STATUS"] = "post-merge-a:lib-check=failed"
        self.assertIn("post-merge red", self.lane("land", "a", ok=False))
        del self.env["FIXTURE_STATUS"]

        # Refusal message names the real paths: smoke --rerun, smoke --accept REASON, or a follow-up lane.
        refused = self.lane("done", "a", ok=False)
        self.assertIn("lanefather smoke a --rerun", refused)
        self.assertIn("lanefather smoke a --accept REASON", refused)
        self.assertIn("follow-up lane", refused)
        self.assertNotIn("fix forward", refused)

        # `smoke <id> --accept REASON` logs via `journal.override`, shows `post-merge accepted`, and lets `done` pass.
        accepted_out = self.lane("smoke", "a", "--accept", "unrelated infra flake")
        self.assertIn("post-merge smoke accepted (unrelated infra flake)", accepted_out)
        rec = self.record("a")
        self.assertEqual(rec["post_merge"]["status"], "accepted")
        self.assertEqual(rec["post_merge"]["reason"], "unrelated infra flake")
        self.assertTrue(rec["post_merge"]["by"])
        self.assertTrue(any(o.get("reason") == "unrelated infra flake" for o in rec.get("overrides", [])))
        ledger = (self.state / "ledger.log").read_text()
        self.assertIn("override smoke a --accept: unrelated infra flake (post-merge smoke is red)", ledger)

        self.assertIn("post-merge accepted", self.lane("list"))
        self.assertIn("post-merge accepted", self.lane("status"))
        status_json = json.loads(self.lane("status", "--json"))
        self.assertEqual(status_json["projects"][0]["lanes"][0]["post_merge"], "accepted")

        self.lane("done", "a")
        self.assertEqual(self.record("a")["status"], "done")
        self.assertFalse(wt.exists())

    def test_smoke_rerun_on_red_smoke_clears_and_lets_done_pass(self) -> None:
        wt = self._checked("r", "lib/r.txt")
        self.integration_moves()
        self.env["FIXTURE_STATUS"] = "post-merge-r:lib-check=failed"
        self.assertIn("post-merge red", self.lane("land", "r", ok=False))
        del self.env["FIXTURE_STATUS"]
        self.assertEqual(self.record("r")["post_merge"]["status"], "red")

        rerun_out = self.lane("smoke", "r", "--rerun")
        self.assertIn("post-merge green", rerun_out)
        self.assertEqual(self.record("r")["post_merge"]["status"], "green")
        self.lane("done", "r")
        self.assertEqual(self.record("r")["status"], "done")
        self.assertFalse(wt.exists())

    def test_skipped_smoke_passes_done_and_missing_post_merge_refuses_done(self) -> None:
        wt_skip = self._checked("s", "lib/s.txt")
        self.lane("land", "s", "--keep")
        # Explicit `status: "skipped"` in post_merge counts as green and lets `done` pass.
        rec_path = self.state / "s.json"
        rec_data = json.loads(rec_path.read_text())
        rec_data["post_merge"] = {"status": "skipped", "kind": "skip", "reason": "exact tree match"}
        rec_path.write_text(json.dumps(rec_data))
        self.lane("done", "s")
        self.assertEqual(self.record("s")["status"], "done")
        self.assertFalse(wt_skip.exists())

        # Legacy record missing `post_merge` -> `done` refuses without `--force-reason`.
        wt_miss = self._checked("m", "lib/m.txt")
        self.lane("land", "m", "--keep")
        miss_path = self.state / "m.json"
        miss_data = json.loads(miss_path.read_text())
        miss_data.pop("post_merge", None)
        miss_path.write_text(json.dumps(miss_data))
        refused = self.lane("done", "m", ok=False)
        self.assertIn("post-merge smoke is not run", refused)
        self.assertTrue(wt_miss.exists())

    def test_killed_recheck_preserves_red_for_clearing_and_accept_audit(self) -> None:
        import subprocess
        import sys
        import time

        wt_a = self._checked("a", "lib/a.txt")
        self.integration_moves()
        self.env["FIXTURE_STATUS"] = "post-merge-a:lib-check=failed"
        self.assertIn("post-merge red", self.lane("land", "a", ok=False))
        del self.env["FIXTURE_STATUS"]

        # Start recheck held on a flag, then kill its holder mid-run.
        hold = self.tmp / "hold-recheck-kill"
        recheck_proc = subprocess.Popen(
            [sys.executable, "-m", "lanefather", "recheck", "a"],
            cwd=self.integration,
            env={**self.env, "FIXTURE_HOLD": f"recheck-a:{hold}"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            started = pathlib.Path(f"{hold}.started")
            deadline = time.monotonic() + 30
            while not started.exists():
                self.assertIsNone(recheck_proc.poll(), "recheck exited before holding")
                # load-tolerant: waiting for fixture recheck process to reach hold file.
                self.assertLess(time.monotonic(), deadline, "recheck did not reach hold")
                time.sleep(0.05)
            self.assertEqual(self.record("a")["post_merge"]["status"], "running")
            recheck_proc.kill()
            recheck_proc.wait(timeout=10)
        finally:
            if recheck_proc.poll() is None:
                recheck_proc.kill()
                recheck_proc.wait(timeout=10)
            if recheck_proc.stdout is not None:
                recheck_proc.stdout.close()

        # Dead recheck resolves back to `red` (not `killed`), so a later green smoke clears it and `done` passes.
        self.assertIn("post-merge red", self.lane("list"))
        wt_b = self._checked("b", "tooling/b.txt")
        self.commit(self.integration, {"tooling/moved-b.txt": "moved\n"}, "integration moves")
        self.assertIn("post-merge green", self.lane("land", "b"))
        self.assertTrue(wt_b.exists())
        done_out = self.lane("done", "a")
        self.assertIn("Lane a done", done_out)
        self.assertIn("cleared by the post-merge smoke of b", done_out)
        self.assertFalse(wt_a.exists())

    def test_in_process_exception_in_run_smoke_resets_running_status(self) -> None:
        import os
        from unittest import mock

        from lanefather import config, land, postmerge, state

        # Case 1: initial `land.post_merge` raises -> `running` resets to `killed`.
        self._checked("e1", "lib/e1.txt")
        self.integration_moves()
        with mock.patch.dict(os.environ, self.env, clear=False):
            cfg = config.load(self.integration / ".lanefather.toml")
            store = state.Store(cfg, directory=self.state)
            landed_obj = land.merge(store, "e1", None, None, True, None, 0)
            with (mock.patch.object(land, "run_smoke", side_effect=RuntimeError("boom")),
                  self.assertRaises(RuntimeError)):
                land.post_merge(store, landed_obj, None)
            self.assertEqual(store.load("e1")["post_merge"]["status"], "killed")
            self.assertEqual(postmerge.live_phase(store.load("e1")), (postmerge.PostMergePhase.KILLED, "killed"))

        # Case 2: `land.recheck` raises -> `running` resets back to `red`.
        self._checked("e2", "lib/e2.txt")
        self.commit(self.integration, {"tooling/moved-e2.txt": "moved again\n"}, "integration moves again")
        self.env["FIXTURE_STATUS"] = "post-merge-e2:lib-check=failed"
        self.assertIn("post-merge red", self.lane("land", "e2", ok=False))
        del self.env["FIXTURE_STATUS"]
        with mock.patch.dict(os.environ, self.env, clear=False):
            cfg = config.load(self.integration / ".lanefather.toml")
            store = state.Store(cfg, directory=self.state)
            recheck_args = mock.MagicMock(id="e2", slots=None)
            with (mock.patch.object(land, "run_smoke", side_effect=RuntimeError("recheck boom")),
                  self.assertRaises(RuntimeError)):
                land.recheck(recheck_args, store)
            self.assertEqual(store.load("e2")["post_merge"]["status"], "red")
            self.assertEqual(postmerge.live_phase(store.load("e2")), (postmerge.PostMergePhase.RED, "red"))


if __name__ == "__main__":
    unittest.main()
