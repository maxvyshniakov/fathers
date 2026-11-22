"""Tests for `lanefather.postmerge` and `done.settle_lane` (fathers#227):
- Exhaustive `(PostMergePhase, PostMergeEvent)` transition table and `StalePostMergeError` fencing.
- Exhaustive zero-I/O `decide_completion` table across `(stage, smoke, review_verdict, kept, done_auto, worktree_lock, mode)`.
- Crashed green `<id>.recheck.json` recovery in both `PostMergeRecord.load` (`done`) and `recheck`.
- Cross-project `gate_platform` resolution reading `store.directory / "results"` without `state.state_dir()`.
- Concurrent `done.settle_lane` race tests (unlocked cleanup, locked release deduplication, keep/done re-evaluation,
  and missing `common_dir` guard).
"""
from __future__ import annotations

import contextlib
import io
from itertools import product
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
from typing import Any
import unittest
from unittest import mock

from support import LaneCase, template_repository

from lanefather import config, done, lifecycle, postmerge, results, review_record, state
from lanefather.errors import LaneError
from lanefather.postmerge import (
    POST_MERGE_TRANSITIONS,
    CompletionAction,
    CompletionMode,
    PostMergeEvent,
    PostMergePhase,
    PostMergeRecord,
    StalePostMergeError,
    apply_post_merge_event,
    decide_completion,
)
from lanefather.review_record import (
    Absent,
    Complete,
    Died,
    Exempt,
    Failed,
    PartialPanel,
    ReviewVerdict,
    Running,
    Unmarked,
)


class PostMergeTransitionAndFencingTests(unittest.TestCase):
    """Exhaustive `PostMergePhase x PostMergeEvent` transitions and `StalePostMergeError` fencing."""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-postmerge-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = state.Store(config.Config(project="Alpha"), directory=self.tmp / "state")
        self.merge_sha = "1" * 40
        self.tip_sha = "2" * 40

    def _seed_lane(
        self,
        lane_id: str,
        *,
        stage: str = "landed",
        phase: PostMergePhase = PostMergePhase.UNRUN,
        merge_sha: str | None = None,
    ) -> pathlib.Path:
        actual_merge = self.merge_sha if merge_sha is None else merge_sha
        record: dict[str, Any] = {
            "id": lane_id,
            "ticket": "42",
            "branch": f"lane/{lane_id}",
            "base": "integrate/q",
            "base_sha": "0" * 40,
            "claims": ["lib/**"],
            "after": [],
            "common_dir": str(self.tmp),
            "integration": str(self.tmp),
            "worktree": str(self.tmp / lane_id),
            "status": stage,
            "created": "2026-11-22T00:00:00Z",
            "scratch": [],
            "cargo_targets": [],
            "overrides": [],
            "agents": [],
            "rules": [],
            "landed": {"merge": actual_merge, "integration": str(self.tmp)},
        }
        if phase is PostMergePhase.GREEN:
            record["post_merge"] = {"status": "green", "receipt": str(self.store.receipt_path(lane_id, "post-merge"))}
        elif phase is PostMergePhase.RED:
            record["post_merge"] = {"status": "red", "receipt": str(self.store.receipt_path(lane_id, "post-merge"))}
        elif phase is PostMergePhase.CLEARED:
            record["post_merge"] = {
                "status": "green",
                "receipt": str(self.store.receipt_path(lane_id, "recheck")),
                "cleared_by": {"kind": "recheck", "sha": self.tip_sha},
            }
        self.store.save(record)
        return self.store.record_path(lane_id)

    def test_exhaustive_phase_event_transition_matrix(self) -> None:
        for phase, event in product(PostMergePhase, PostMergeEvent):
            with self.subTest(phase=phase, event=event):
                expected = POST_MERGE_TRANSITIONS.get((phase, event))
                if expected is not None:
                    self.assertEqual(apply_post_merge_event("a", phase, event), expected)
                else:
                    with self.assertRaises(LaneError):
                        apply_post_merge_event("a", phase, event)

                # Also exercise `record_smoke` and `record_recheck` on disk for the 4 write events.
                if event in (PostMergeEvent.SMOKE_GREEN, PostMergeEvent.SMOKE_RED):
                    lane_id = f"s-{phase.value}-{event.value}".replace("_", "-")
                    rec_path = self._seed_lane(lane_id, phase=phase)
                    before = rec_path.read_bytes()
                    status = "green" if event is PostMergeEvent.SMOKE_GREEN else "red"
                    if expected is not None:
                        pm, written = PostMergeRecord.record_smoke(
                            self.store, lane_id, merge_sha=self.merge_sha, status=status, payload={"tree": "a" * 40}
                        )
                        self.assertEqual(pm.phase, expected)
                        self.assertTrue(written.exists())
                        self.assertEqual(self.store.load(lane_id)["post_merge"]["status"], status)
                    else:
                        with self.assertRaises(LaneError):
                            PostMergeRecord.record_smoke(
                                self.store, lane_id, merge_sha=self.merge_sha, status=status, payload={"tree": "a" * 40}
                            )
                        self.assertEqual(rec_path.read_bytes(), before)
                        self.assertFalse(self.store.receipt_path(lane_id, "post-merge").exists())

                elif event in (PostMergeEvent.RECHECK_GREEN, PostMergeEvent.RECHECK_RED):
                    lane_id = f"r-{phase.value}-{event.value}".replace("_", "-")
                    rec_path = self._seed_lane(lane_id, phase=phase)
                    before = rec_path.read_bytes()
                    status = "green" if event is PostMergeEvent.RECHECK_GREEN else "red"
                    if expected is not None:
                        pm, written = PostMergeRecord.record_recheck(
                            self.store,
                            lane_id,
                            merge_sha=self.merge_sha,
                            tip=self.tip_sha,
                            status=status,
                            payload={"tree": "b" * 40},
                        )
                        self.assertEqual(pm.phase, expected)
                        self.assertTrue(written.exists())
                        saved_post = self.store.load(lane_id)["post_merge"]
                        if event is PostMergeEvent.RECHECK_GREEN:
                            self.assertEqual(saved_post["status"], "green")
                            self.assertEqual(saved_post["cleared_by"]["kind"], "recheck")
                        else:
                            self.assertEqual(saved_post["status"], "red")
                    else:
                        with self.assertRaises(LaneError):
                            PostMergeRecord.record_recheck(
                                self.store,
                                lane_id,
                                merge_sha=self.merge_sha,
                                tip=self.tip_sha,
                                status=status,
                                payload={"tree": "b" * 40},
                            )
                        self.assertEqual(rec_path.read_bytes(), before)
                        self.assertFalse(self.store.receipt_path(lane_id, "recheck").exists())

    def test_stale_post_merge_error_fences_done_unlanded_and_mismatched_merge_sha(self) -> None:
        stale_merge = "9" * 40
        cases: list[tuple[str, str, PostMergePhase, str]] = [
            ("done-lane", "done", PostMergePhase.UNRUN, self.merge_sha),
            ("done-red", "done", PostMergePhase.RED, self.merge_sha),
            ("planned-lane", "planned", PostMergePhase.UNRUN, self.merge_sha),
            ("active-lane", "active", PostMergePhase.UNRUN, self.merge_sha),
            ("wrong-merge-smoke", "landed", PostMergePhase.UNRUN, stale_merge),
            ("wrong-merge-recheck", "landed", PostMergePhase.RED, stale_merge),
        ]
        for lane_id, stage, phase, target_merge in cases:
            with self.subTest(case=lane_id):
                rec_path = self._seed_lane(lane_id, stage=stage, phase=phase, merge_sha=self.merge_sha)
                before_bytes = rec_path.read_bytes()
                with self.assertRaises(StalePostMergeError):
                    PostMergeRecord.record_smoke(
                        self.store, lane_id, merge_sha=target_merge, status="green", payload={"tree": "a" * 40}
                    )
                with self.assertRaises(StalePostMergeError):
                    PostMergeRecord.record_recheck(
                        self.store,
                        lane_id,
                        merge_sha=target_merge,
                        tip=self.tip_sha,
                        status="green",
                        payload={"tree": "b" * 40},
                    )
                self.assertEqual(rec_path.read_bytes(), before_bytes)
                self.assertFalse(self.store.receipt_path(lane_id, "post-merge").exists())
                self.assertFalse(self.store.receipt_path(lane_id, "recheck").exists())


class DecideCompletionExhaustiveTests(unittest.TestCase):
    """Exhaustive zero-I/O table over all 5,120 combinations of `decide_completion`."""

    def test_exhaustive_zero_io_completion_table(self) -> None:
        stages = ("planned", "active", "landed", "released", "done")
        smokes = tuple(PostMergePhase)
        f_unmarked = {"n": 1, "severity": "P2", "title": "bug", "mark": None}
        verdicts: tuple[tuple[str, ReviewVerdict], ...] = (
            ("absent", Absent()),
            ("running", Running(pid=123, started="t0", log="/tmp/r.log")),
            ("died", Died(pid=123, started="t0", log="/tmp/r.log")),
            ("failed", Failed(error="boom")),
            ("partial_panel", PartialPanel(missing_members=("opus",), findings=(f_unmarked,), unmarked=(f_unmarked,))),
            ("exempt", Exempt(reason="docs only")),
            ("unmarked", Unmarked(findings=(f_unmarked,), unmarked=(f_unmarked,), reviewer="sol")),
            ("complete", Complete(findings=(), reviewer="sol")),
        )
        kept_values = (False, True)
        done_auto_values = (False, True)
        locks: tuple[str | None, ...] = (None, "claude agent a")
        modes = tuple(CompletionMode)
        sample_cleared = {"kind": "gate", "sha": "f" * 40, "receipt": "/tmp/gate.json"}

        checked = 0
        with (
            mock.patch.object(subprocess, "run", side_effect=AssertionError("zero I/O violated: subprocess.run")),
            mock.patch.object(os.path, "isdir", side_effect=AssertionError("zero I/O violated: os.path.isdir")),
            mock.patch.object(state, "read_json", side_effect=AssertionError("zero I/O violated: state.read_json")),
        ):
            for stage, smoke, (v_name, verdict), is_kept, done_auto, lock, mode in product(
                stages, smokes, verdicts, kept_values, done_auto_values, locks, modes
            ):
                checked += 1
                decision = decide_completion(
                    stage,
                    smoke,
                    verdict,
                    kept=is_kept,
                    done_auto=done_auto,
                    worktree_lock=lock,
                    mode=mode,
                    lane_id="a",
                    prog="lanefather",
                    cleared_by=sample_cleared,
                )

                is_auto = mode in (CompletionMode.AUTO_AFTER_SMOKE, CompletionMode.AUTO_AFTER_REVIEW)
                smoke_ok = smoke in (PostMergePhase.GREEN, PostMergePhase.CLEARED)
                review_done = v_name in ("exempt", "complete")
                review_ok_interactive = review_done or v_name == "absent"

                # Verify exact expected action for every cell of the 7-dimensional space.
                if stage == "done":
                    expected = CompletionAction.REFUSE if mode is CompletionMode.INTERACTIVE else CompletionAction.NOOP
                    self.assertTrue(decision.already_done)
                elif stage not in ("landed", "released") or not smoke_ok:
                    expected = CompletionAction.NOOP if is_auto else CompletionAction.REFUSE
                    self.assertFalse(decision.already_done)
                elif mode is CompletionMode.INTERACTIVE:
                    if not review_ok_interactive or lock is not None:
                        expected = CompletionAction.REFUSE
                    else:
                        expected = CompletionAction.CLEANUP
                elif mode is CompletionMode.FINISHED:
                    if not review_done or is_kept or lock is not None:
                        expected = CompletionAction.REFUSE
                    else:
                        expected = CompletionAction.CLEANUP
                elif mode is CompletionMode.AUTO_AFTER_SMOKE:
                    if is_kept:
                        expected = CompletionAction.HOLD_KEPT
                    elif not review_done:
                        expected = CompletionAction.HOLD_REVIEW
                    elif not done_auto:
                        expected = CompletionAction.HOLD_AUTO_OFF
                    elif lock is not None:
                        expected = CompletionAction.RELEASE
                    else:
                        expected = CompletionAction.CLEANUP
                else:
                    assert mode is CompletionMode.AUTO_AFTER_REVIEW
                    if not review_done:
                        expected = CompletionAction.NOOP
                    elif is_kept:
                        expected = CompletionAction.HOLD_KEPT
                    elif not done_auto:
                        expected = CompletionAction.HOLD_AUTO_OFF
                    elif lock is not None:
                        expected = CompletionAction.RELEASE
                    else:
                        expected = CompletionAction.CLEANUP

                self.assertEqual(
                    decision.action,
                    expected,
                    f"mismatch for {(stage, smoke, v_name, is_kept, done_auto, lock, mode)}",
                )
                self.assertEqual(decision.ready, expected is CompletionAction.CLEANUP)
                if smoke is PostMergePhase.CLEARED and stage in ("landed", "released"):
                    self.assertEqual(decision.cleared_by, sample_cleared)
                else:
                    self.assertIsNone(decision.cleared_by)
                if expected is CompletionAction.REFUSE:
                    self.assertTrue(decision.refusal)

        self.assertEqual(checked, 5120)


class CrashedRecheckAndCrossProjectTests(LaneCase):
    """Real-repo integration tests for crashed green `<id>.recheck.json` recovery and cross-project `gate_platform`."""

    def _red_lane(self, lane_id: str = "a") -> pathlib.Path:
        wt = pathlib.Path(self.created(lane_id, "lib/**"))
        self.commit(wt, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", lane_id)
        self.integration_moves()
        self.env["FIXTURE_STATUS"] = f"post-merge-{lane_id}:lib-check=failed"
        self.assertIn("post-merge red", self.lane("land", lane_id, ok=False))
        del self.env["FIXTURE_STATUS"]
        self.assertEqual(self.record(lane_id)["post_merge"]["status"], "red")
        return wt

    def _write_crashed_recheck(self, lane_id: str, tip: str, **overrides: Any) -> pathlib.Path:
        red: dict[str, Any] = json.loads((self.state / f"{lane_id}.post-merge.json").read_text())
        checks_by_suite: dict[str, list[str]] = red.get("checks") or {}
        suites: list[str] = list(red["suites"])
        tree = self.git(self.integration, "rev-parse", f"{tip}^{{tree}}")
        recheck_path = self.state / f"{lane_id}.recheck.json"
        payload: dict[str, Any] = {
            "version": 1,
            "lane": lane_id,
            "kind": "recheck",
            "merge": red["merge"],
            "sha": tip,
            "tree": tree,
            "platform": results.host_platform(),
            "red_receipt": str(self.state / f"{lane_id}.post-merge.json"),
            "suites": suites,
            "checks": checks_by_suite,
            "check_results": {
                cid: {"status": "passed", "suite": suite}
                for suite, ids in checks_by_suite.items()
                for cid in ids
            },
            "results": [{"suite": s, "status": "passed", "checks": checks_by_suite.get(s, [])}
                        for s in suites],
            "status": "green",
            "finished": "2999-01-01T00:00:00Z",
            **overrides,
        }
        state.write_json(recheck_path, payload)
        return recheck_path

    def test_crashed_green_recheck_recovered_by_load_and_done(self) -> None:
        wt = self._red_lane("a")
        tip = self.commit(self.integration, {"notes.txt": "later\n"}, "later on integration")
        recheck_path = self._write_crashed_recheck("a", tip)

        # Record on disk still says `post_merge.status == "red"`, simulating crash before record save.
        self.assertEqual(self.record("a")["post_merge"]["status"], "red")
        cfg = config.load(self.integration / ".lanefather.toml")
        store = state.Store(cfg, directory=self.state)
        pm = PostMergeRecord.load(store, store.load("a"))
        self.assertEqual(pm.phase, PostMergePhase.CLEARED)
        assert pm.cleared_by is not None
        self.assertEqual(pm.cleared_by["kind"], "recheck")

        output = self.lane("done", "a")
        self.assertIn("Lane a done", output)
        self.assertIn(f"cleared by the recheck of {tip[:10]} ({recheck_path})", output)
        self.assertFalse(wt.exists())
        final = self.record("a")
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["post_merge"]["cleared_by"]["kind"], "recheck")

    def test_crashed_green_recheck_recovered_by_recheck_command_without_rerunning_suites(self) -> None:
        self._red_lane("a")
        tip = self.commit(self.integration, {"notes.txt": "later\n"}, "later on integration")
        recheck_path = self._write_crashed_recheck("a", tip)

        runs_file = pathlib.Path(str(self.verify_log) + ".runs")
        runs_before = runs_file.read_text().splitlines()

        output = self.lane("recheck", "a")
        self.assertIn(f"post-merge green on {tip[:10]}", output)
        self.assertEqual(runs_file.read_text().splitlines(), runs_before, "recovered recheck must not rerun verify")
        post = self.record("a")["post_merge"]
        self.assertEqual(post["status"], "green")
        self.assertEqual(post["receipt"], str(recheck_path))
        self.assertEqual(post["cleared_by"]["kind"], "recheck")
        self.assertIn("replaces red", (self.state / "ledger.log").read_text())

    def test_stale_or_incomplete_crashed_recheck_does_not_clear_red_smoke(self) -> None:
        self._red_lane("a")
        tip = self.commit(self.integration, {"notes.txt": "later\n"}, "later on integration")
        cfg = config.load(self.integration / ".lanefather.toml")
        store = state.Store(cfg, directory=self.state)
        other_plat = "linux-x64" if results.host_platform() != "linux-x64" else "darwin-arm64"

        for label, bad_overrides in (
            ("wrong_merge", {"merge": "f" * 40}),
            ("wrong_platform", {"platform": other_plat}),
            ("red_status", {"status": "red"}),
            ("uncovered_check", {"check_results": {"structure-check": {"status": "passed"}}}),
            ("before_red_smoke", {"finished": "2000-01-01T00:00:00Z"}),
        ):
            with self.subTest(bad=label):
                self._write_crashed_recheck("a", tip, **bad_overrides)
                pm = PostMergeRecord.load(store, store.load("a"))
                self.assertEqual(pm.phase, PostMergePhase.RED)

    def test_cross_project_gate_platform_reads_explicit_store_results_dir(self) -> None:
        other_repo = self.tmp / "project-b"
        shutil.copytree(template_repository(), other_repo, symlinks=True)
        state_b = self.tmp / "state-b"
        state_b.mkdir()
        common_b = str((other_repo / ".git").resolve())
        wt_b = other_repo / ".claude" / "worktrees" / "lane-b"
        self.git(other_repo, "worktree", "add", "-q", "-b", "lane/b", str(wt_b))
        merge_b = self.git(other_repo, "rev-parse", "HEAD")
        tree_b = self.git(other_repo, "rev-parse", f"{merge_b}^{{tree}}")
        host_plat = results.host_platform()
        wrong_plat = "linux-x64" if host_plat != "linux-x64" else "darwin-arm64"

        # Seed Project B with a red smoke and a legacy `promote-<sha>.gate.json` (`platform=None`) whose platform is
        # recorded ONLY in `state_b / "results"`, while ambient `self.state / "results"` holds `wrong_plat`!
        red_b_path = state_b / "b.post-merge.json"
        state.write_json(red_b_path, {
            "version": 1, "lane": "b", "merge": merge_b, "tree": tree_b, "status": "red",
            "platform": host_plat, "suites": ["lib"], "checks": {"lib": ["lib-check"]},
            "finished": "2026-11-22T10:00:00Z",
        })
        gate_b_path = state_b / f"promote-{merge_b[:10]}.gate.json"
        state.write_json(gate_b_path, {
            "version": 1, "lane": f"promote-{merge_b[:10]}", "sha": merge_b, "tree": tree_b,
            "full": True, "status": "green", "platform": None, "finished": "2026-11-22T11:00:00Z",
        })
        (state_b / "results").mkdir()
        state.write_json(state_b / "results" / f"{tree_b}.json", {
            "v": 1,
            "tree": tree_b,
            "entries": [{
                "checks": {}, "full": True, "platform": host_plat, "status": "green",
                "receipt": str(gate_b_path), "at": "2026-11-22T11:00:00Z",
            }],
        })
        (self.state / "results").mkdir(parents=True, exist_ok=True)
        state.write_json(self.state / "results" / f"{tree_b}.json", {
            "v": 1,
            "tree": tree_b,
            "entries": [{
                "checks": {}, "full": True, "platform": wrong_plat, "status": "green",
                "receipt": str(gate_b_path), "at": "2026-11-22T11:00:00Z",
            }],
        })

        rec_b: dict[str, Any] = {
            "id": "b", "ticket": "none", "status": "landed", "branch": "lane/b", "base": "main",
            "common_dir": common_b, "worktree": str(wt_b.resolve()), "claims": ["lib/**"], "after": [],
            "scratch": [], "created": "2026-11-22T09:00:00Z", "overrides": [],
            "post_merge": {"status": "red", "receipt": str(red_b_path)},
            "landed": {"merge": merge_b, "integration": str(other_repo)},
        }
        cfg_b = config.load(other_repo / ".lanefather.toml")
        store_b = state.Store(cfg_b, directory=state_b)
        store_b.save(rec_b)
        review_record.ReviewRecord.exempt(store_b, "b", "docs only")

        self.assertFalse(hasattr(config, "current"))
        self.assertFalse(hasattr(state, "state_dir"))
        pm_b = PostMergeRecord.load(store_b, store_b.load("b"))
        self.assertEqual(pm_b.phase, PostMergePhase.CLEARED)
        assert pm_b.cleared_by is not None
        self.assertEqual(pm_b.cleared_by["kind"], "gate")

        # Also verify end-to-end `done --finished` from Project A finishes Project B's lane.
        registry_path = pathlib.Path(self.env["LANEFATHER_REGISTRY"])
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps({
            "v": 1,
            "projects": [{
                "state_dir": str(state_b),
                "common_dir": common_b,
                "config": str(other_repo / ".lanefather.toml"),
                "project": "project-b",
                "seen": "2026-11-22T12:00:00Z",
            }],
        }))
        out = self.lane("done", "--finished")
        self.assertIn("1 lane finished", out)
        self.assertEqual(store_b.load("b")["status"], "done")
        self.assertEqual(store_b.load("b")["post_merge"]["cleared_by"]["kind"], "gate")


class SettleLaneRaceTests(LaneCase):
    """Concurrent `done.settle_lane` races and `common_dir` guard."""

    def _in_process_store(self) -> state.Store:
        env_patch = mock.patch.dict(os.environ, self.env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        cfg = config.load(self.repo / ".lanefather.toml")
        return state.Store(cfg, directory=self.state)

    def _ready_landed_lane(self, lane_id: str) -> pathlib.Path:
        self.start(lane_id, "lib/a.txt", "--create", "--ticket", "none")
        wt = pathlib.Path(self.record(lane_id)["worktree"])
        self.commit(wt, {"lib/a.txt": f"{lane_id}\n"})
        self.lane("check", lane_id, cwd=wt)
        self.lane("land", lane_id, "--keep")
        self.lane("keep", lane_id, "--off")
        state.write_json(self.state / f"{lane_id}.review.json", {"version": 1, "lane": lane_id, "status": "exempt"})
        return wt

    def test_concurrent_settle_lane_cleanup_runs_once_without_duplicate_output(self) -> None:
        wt = self._ready_landed_lane("c1")
        store = self._in_process_store()
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        shared_buf = io.StringIO()

        def worker(mode: CompletionMode) -> None:
            try:
                barrier.wait(timeout=10.0)
                done.settle_lane(store, "c1", mode=mode)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        with contextlib.redirect_stdout(shared_buf):
            t1 = threading.Thread(target=worker, args=(CompletionMode.AUTO_AFTER_SMOKE,))
            t2 = threading.Thread(target=worker, args=(CompletionMode.AUTO_AFTER_REVIEW,))
            t1.start()
            t2.start()
            t1.join(timeout=15.0)
            t2.join(timeout=15.0)

        self.assertEqual(errors, [])
        self.assertFalse(wt.exists())
        self.assertEqual(store.load("c1")["status"], "done")
        combined = shared_buf.getvalue()
        self.assertEqual(combined.count("Lane c1 done:"), 1, combined)
        ledger = (self.state / "ledger.log").read_text()
        self.assertEqual(ledger.count("done c1 freed="), 1, ledger)

    def test_concurrent_and_repeated_settle_lane_release_suppresses_duplicate_output(self) -> None:
        wt = self._ready_landed_lane("r1")
        self.git(self.repo, "worktree", "lock", "--reason", "claude agent r1", str(wt))
        store = self._in_process_store()
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        shared_buf = io.StringIO()

        def worker(mode: CompletionMode) -> None:
            try:
                barrier.wait(timeout=10.0)
                done.settle_lane(store, "r1", mode=mode)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        with contextlib.redirect_stdout(shared_buf):
            t1 = threading.Thread(target=worker, args=(CompletionMode.AUTO_AFTER_SMOKE,))
            t2 = threading.Thread(target=worker, args=(CompletionMode.AUTO_AFTER_REVIEW,))
            t1.start()
            t2.start()
            t1.join(timeout=15.0)
            t2.join(timeout=15.0)

        self.assertEqual(errors, [])
        self.assertEqual(store.load("r1")["status"], "released")
        combined = shared_buf.getvalue()
        self.assertEqual(combined.count("Lane r1 released:"), 1, combined)

        # Calling settle_lane again on an already-released locked lane prints nothing.
        repeat_buf = io.StringIO()
        with contextlib.redirect_stdout(repeat_buf):
            done.settle_lane(store, "r1", mode=CompletionMode.AUTO_AFTER_REVIEW)
        self.assertEqual(repeat_buf.getvalue(), "")
        ledger = (self.state / "ledger.log").read_text()
        self.assertEqual(ledger.count("release r1:"), 1, ledger)

    def test_settle_lane_rechecks_keep_under_state_lock_before_releasing(self) -> None:
        wt = self._ready_landed_lane("k1")
        self.git(self.repo, "worktree", "lock", "--reason", "claude agent k1", str(wt))
        store = self._in_process_store()

        saw_unlocked_probe = threading.Event()
        keep_saved = threading.Event()
        real_eval = done.evaluate_completion
        calls = 0

        def staged_eval(s: state.Store, rec: state.Record, **kwargs: Any) -> postmerge.CompletionDecision:
            nonlocal calls
            calls += 1
            res = real_eval(s, rec, **kwargs)
            if calls == 1:
                saw_unlocked_probe.set()
                keep_saved.wait(timeout=10.0)
            return res

        errors: list[BaseException] = []
        out_buf = io.StringIO()

        def run_settle() -> None:
            try:
                done.settle_lane(store, "k1", mode=CompletionMode.AUTO_AFTER_REVIEW)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        def run_keep() -> None:
            saw_unlocked_probe.wait(timeout=10.0)
            lifecycle.set_keep(store, "k1", True)
            keep_saved.set()

        with contextlib.redirect_stdout(out_buf), mock.patch.object(done, "evaluate_completion", side_effect=staged_eval):
            t_settle = threading.Thread(target=run_settle)
            t_keep = threading.Thread(target=run_keep)
            t_settle.start()
            t_keep.start()
            t_settle.join(timeout=15.0)
            t_keep.join(timeout=15.0)

        self.assertEqual(errors, [])
        self.assertEqual(store.load("k1")["status"], "landed", "concurrent keep must prevent release")
        self.assertIn("Lane k1 kept", out_buf.getvalue())

    def test_settle_lane_guards_missing_common_dir_and_logs_refusal(self) -> None:
        wt = self._ready_landed_lane("m1")
        store = self._in_process_store()
        store.update("m1", lambda rec: rec.__setitem__("common_dir", str(self.tmp / "nonexistent-git")))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            decision = done.settle_lane(store, "m1", mode=CompletionMode.AUTO_AFTER_REVIEW)
        self.assertEqual(decision.action, CompletionAction.CLEANUP)
        self.assertEqual(store.load("m1")["status"], "landed")
        self.assertTrue(wt.exists())
        self.assertIn("names no repository that exists", buf.getvalue())
        self.assertIn("auto-done m1 refused", (self.state / "ledger.log").read_text())


if __name__ == "__main__":
    unittest.main()
