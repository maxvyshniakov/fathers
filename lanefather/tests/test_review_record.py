"""Unit and integration tests for `lanefather.review_record` (fathers#216):
- Exhaustive `(ReceiptPhase, ReviewEvent)` transition table, `force=True` on `START`, `owner_pid` self-handoff, and
  `StaleReviewRunError` fencing (including `perform_review` propagation without calling `fail`).
- All 8 verdicts (`Absent`, `Running`, `Died`, `Failed`, `PartialPanel`, `Exempt`, `Unmarked`, `Complete`), with
  `PartialPanel` separated from hard `Failed`.
- Named mutators (`begin`, `spawn_and_begin`, `finish`, `fail`, `exempt`, `mark`, `open_pre_land_fix`,
  `attach_fix_lane`, `record_ticket`), verifying `open_pre_land_fix` writes only to `.review.json` while
  `pre_land_fix(record)` preserves the guarded legacy `record['pre_land_fix']` read fallback.
"""
from __future__ import annotations

import contextlib
from itertools import product
import os
import pathlib
import shutil
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock
import warnings

from support import LaneCase

from lanefather import config, review, review_record, state
from lanefather.errors import LaneError
from lanefather.review_record import (
    REVIEW_TRANSITIONS,
    Absent,
    Complete,
    Died,
    Exempt,
    Failed,
    PartialPanel,
    PreLandFix,
    ReceiptPhase,
    ReviewEvent,
    ReviewRecord,
    Running,
    StaleReviewRunError,
    Unmarked,
)


def _seed_for_phase(phase: ReceiptPhase, *, pid: int) -> dict[str, Any] | None:
    if phase is ReceiptPhase.ABSENT:
        return None
    if phase is ReceiptPhase.RUNNING:
        return {"version": 1, "label": "a", "status": "running", "pid": pid, "started": "t0", "log": "/tmp/a.log"}
    if phase is ReceiptPhase.DIED:
        return {"version": 1, "label": "a", "status": "running", "pid": 999_999_999, "started": "t0", "log": "/tmp/a"}
    if phase is ReceiptPhase.FAILED:
        return {"version": 1, "label": "a", "status": "failed", "error": "boom"}
    if phase is ReceiptPhase.EXEMPT:
        return {"version": 1, "label": "a", "status": "exempt", "exempt": "docs only"}
    return {
        "version": 1,
        "label": "a",
        "status": "finished",
        "pid": pid,
        "reviewer": "sol",
        "findings": [{"n": 1, "severity": "P2", "title": "bug", "mark": None}],
    }


def _payload_for_event(event: ReviewEvent, existing: dict[str, Any] | None, *, pid: int) -> dict[str, Any]:
    if event is ReviewEvent.START:
        return {"version": 1, "label": "a", "status": "running", "pid": pid, "started": "t1", "effort": "medium"}
    if event is ReviewEvent.FINISH:
        return {**(existing or {}), "status": "finished", "findings": [], "finished": "t2"}
    if event is ReviewEvent.FAIL:
        return {**(existing or {}), "status": "failed", "error": "err", "finished": "t2"}
    if event is ReviewEvent.EXEMPT:
        return {"version": 1, "label": "a", "status": "exempt", "exempt": "docs only", "finished": "t2"}
    if event is ReviewEvent.MARK:
        return {**(existing or {}), "status": "finished"}
    if event is ReviewEvent.PRE_LAND_FIX:
        return {**(existing or {}), "status": "finished", "pre_land_fix": {"reviewed_sha": "abc"}}
    if event is ReviewEvent.FIX_LANE:
        return {**(existing or {}), "status": "finished", "fix_lane": "a-review-fixes"}
    return {**(existing or {}), "status": "finished", "issue": "https://example.test/1"}


class ReviewTransitionTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-review-rec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = state.Store(config.Config(project="Alpha"), directory=self.tmp / "state")

    def _write_seed(self, label: str, seed: dict[str, Any] | None) -> pathlib.Path:
        target = self.store.receipt_path(label, "review")
        if seed is None:
            target.unlink(missing_ok=True)
        else:
            self.store.directory.mkdir(parents=True, exist_ok=True)
            state.write_json(target, seed)
        return target

    def test_exhaustive_phase_event_transition_matrix(self) -> None:
        live_pid = os.getpid()
        for phase, event in product(ReceiptPhase, ReviewEvent):
            with self.subTest(phase=phase, event=event):
                seed = _seed_for_phase(phase, pid=live_pid)
                path = self._write_seed("a", seed)
                before_bytes = path.read_bytes() if path.exists() else None
                expected_target = REVIEW_TRANSITIONS.get((phase, event))
                if expected_target is not None:
                    rev = ReviewRecord._transition(  # pyright: ignore[reportPrivateUsage]
                        self.store,
                        "a",
                        event,
                        lambda cur, ev=event: _payload_for_event(ev, cur, pid=live_pid),
                    )
                    self.assertEqual(rev.phase, expected_target)
                    self.assertEqual((state.read_json(path) or {}).get("status"), expected_target.value)
                else:
                    with self.assertRaises(LaneError):
                        ReviewRecord._transition(  # pyright: ignore[reportPrivateUsage]
                            self.store,
                            "a",
                            event,
                            lambda cur, ev=event: _payload_for_event(ev, cur, pid=live_pid),
                        )
                    after_bytes = path.read_bytes() if path.exists() else None
                    self.assertEqual(after_bytes, before_bytes)

    def test_force_allows_start_from_every_phase_and_never_unlocks_other_events(self) -> None:
        live_pid = os.getpid()
        for phase in ReceiptPhase:
            with self.subTest(phase=phase):
                self._write_seed("a", _seed_for_phase(phase, pid=live_pid))
                rev = ReviewRecord.begin(
                    self.store, "a", {"kind": "lane"}, pid=live_pid, log="/tmp/r.log", effort="medium", force=True
                )
                self.assertEqual(rev.phase, ReceiptPhase.RUNNING)
        # force=True does not bypass REVIEW_TRANSITIONS for non-START events.
        self._write_seed("a", _seed_for_phase(ReceiptPhase.ABSENT, pid=live_pid))
        with self.assertRaises(LaneError):
            ReviewRecord._transition(  # pyright: ignore[reportPrivateUsage]
                self.store,
                "a",
                ReviewEvent.MARK,
                lambda _c: {"status": "finished"},
                force=True,
            )

    def test_running_start_self_handoff_requires_matching_owner_pid(self) -> None:
        live_pid = os.getpid()
        self._write_seed("a", _seed_for_phase(ReceiptPhase.RUNNING, pid=live_pid))
        # Different PID without force is refused.
        with self.assertRaises(LaneError):
            ReviewRecord.begin(
                self.store, "a", {"kind": "lane"}, pid=live_pid + 1, log="/tmp/r.log", effort="medium", force=False
            )
        # Same PID (spawned background child picking up its own receipt) succeeds.
        rev = ReviewRecord.begin(
            self.store, "a", {"kind": "lane"}, pid=live_pid, log="/tmp/r.log", effort="high", force=False
        )
        self.assertEqual(rev.phase, ReceiptPhase.RUNNING)
        self.assertEqual((rev.raw or {}).get("effort"), "high")

    def test_stale_worker_pid_raises_stale_review_run_error_on_finish_and_fail(self) -> None:
        live_pid = os.getpid()
        stale_pid = live_pid + 100
        # Superseded by a newer running worker.
        self._write_seed("a", _seed_for_phase(ReceiptPhase.RUNNING, pid=live_pid))
        before = (self.store.receipt_path("a", "review")).read_bytes()
        for fn in (
            lambda: ReviewRecord.finish(self.store, "a", {"findings": []}, owner_pid=stale_pid),
            lambda: ReviewRecord.fail(self.store, "a", "late crash", owner_pid=stale_pid),
        ):
            with self.assertRaises(StaleReviewRunError):
                fn()
            self.assertEqual(self.store.receipt_path("a", "review").read_bytes(), before)

        # Superseded by a newer worker that already finished.
        self._write_seed("a", _seed_for_phase(ReceiptPhase.FINISHED, pid=live_pid))
        finished_bytes = self.store.receipt_path("a", "review").read_bytes()
        with self.assertRaises(StaleReviewRunError):
            ReviewRecord.finish(self.store, "a", {"findings": []}, owner_pid=stale_pid)
        self.assertEqual(self.store.receipt_path("a", "review").read_bytes(), finished_bytes)

    def test_transition_rejects_mismatched_target_status(self) -> None:
        self._write_seed("a", None)
        with self.assertRaises(LaneError):
            ReviewRecord._transition(  # pyright: ignore[reportPrivateUsage]
                self.store,
                "a",
                ReviewEvent.START,
                lambda _c: {"version": 1, "label": "a", "status": "finished"},
            )
        self.assertFalse(self.store.receipt_path("a", "review").exists())


class VerdictAndReadSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-verdicts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = state.Store(
            config.Config(project="Alpha", review_panel=("sol", "opus"), review_panel_required=True),
            directory=self.tmp / "state",
        )

    def test_all_eight_verdicts_and_failed_has_no_missing_members(self) -> None:
        live_pid = os.getpid()
        f_open = {"n": 1, "severity": "P2", "title": "bug", "mark": None}
        f_marked = {"n": 2, "severity": "P1", "title": "fixed", "mark": {"kind": "rejected", "reason": "ok"}}

        self.assertEqual(review_record.derive_verdict(None), Absent())
        self.assertEqual(
            review_record.derive_verdict({"status": "running", "pid": live_pid, "started": "t0", "log": "/l"}),
            Running(pid=live_pid, started="t0", log="/l"),
        )
        self.assertEqual(
            review_record.derive_verdict({"status": "running", "pid": 999_999_999, "started": "t0", "log": "/l"}),
            Died(pid=999_999_999, started="t0", log="/l"),
        )
        hard_failed = review_record.derive_verdict({"status": "failed", "error": "boom"})
        self.assertEqual(hard_failed, Failed(error="boom", raw_status="failed"))
        self.assertFalse(hasattr(hard_failed, "missing_members"))

        self.assertEqual(
            review_record.derive_verdict({"status": "exempt", "exempt": "docs only"}),
            Exempt(reason="docs only"),
        )
        self.assertEqual(
            review_record.derive_verdict(
                {"status": "finished", "reviewer": "sol", "findings": [f_open, f_marked]}
            ),
            Unmarked(findings=(f_open, f_marked), unmarked=(f_open,), reviewer="sol"),
        )
        self.assertEqual(
            review_record.derive_verdict({"status": "finished", "reviewer": "sol", "findings": [f_marked]}),
            Complete(findings=(f_marked,), reviewer="sol"),
        )

        partial_raw: dict[str, Any] = {
            "status": "finished",
            "reviewer": "sol+opus",
            "findings": [f_open, f_marked],
            "panel": [
                {"member": "sol", "reviewer": "sol", "model": "gpt-6.1-sol", "effort": "medium",
                 "status": "finished", "numbers": [1, 2]},
                {"member": "opus", "reviewer": "opus", "model": "claude-opus-5-5", "effort": "medium",
                 "status": "failed", "error": "overloaded"},
            ],
        }
        partial_req = ReviewRecord.from_raw("a", partial_raw, panel_required=True)
        self.assertEqual(
            partial_req.verdict,
            PartialPanel(
                missing_members=("opus",),
                findings=(f_open, f_marked),
                unmarked=(f_open,),
                reviewer="sol+opus",
            ),
        )
        self.assertTrue(partial_req.exists)
        self.assertTrue(partial_req.has_finished_receipt)
        self.assertFalse(partial_req.allows_promote)
        self.assertEqual(partial_req.findings, (f_open, f_marked))
        self.assertEqual(partial_req.open_findings, (f_open,))
        ok, words = partial_req.summary(prog="lanefather")
        self.assertFalse(ok)
        self.assertIn("panel member opus gave no review ([review] panel_required)", words)
        self.assertEqual(len(partial_req.reviewer_views()), 2)

        # With panel_required=False, the same receipt evaluates to Unmarked (or Complete when marked).
        partial_opt = ReviewRecord.from_raw("a", partial_raw, panel_required=False)
        self.assertIsInstance(partial_opt.verdict, Unmarked)

    def test_attach_fix_lane_and_record_ticket_transitions(self) -> None:
        live_pid = os.getpid()
        ReviewRecord.begin(self.store, "ship-abc", {"kind": "overall"}, pid=live_pid, log=None, effort="medium")
        ReviewRecord.finish(
            self.store,
            "ship-abc",
            {"findings": [{"n": 1, "severity": "P2", "title": "t", "mark": None}]},
            owner_pid=live_pid,
        )
        failed_ticket = ReviewRecord.record_ticket(self.store, "ship-abc", issue_error="HTTP 502")
        self.assertEqual((failed_ticket.raw or {}).get("issue_error"), "HTTP 502")
        filed_ticket = ReviewRecord.record_ticket(self.store, "ship-abc", issue="https://github.com/x/y/issues/1")
        self.assertEqual((filed_ticket.raw or {}).get("issue"), "https://github.com/x/y/issues/1")
        self.assertNotIn("issue_error", filed_ticket.raw or {})

        attached = ReviewRecord.attach_fix_lane(self.store, "ship-abc", "fix-1")
        self.assertEqual((attached.raw or {}).get("fix_lane"), "fix-1")
        self.assertEqual([r.label for r in ReviewRecord.all(self.store)], ["ship-abc"])

    def test_spawn_and_begin_starts_detached_process_and_records_pid(self) -> None:
        log = self.tmp / "logs" / "review-bg.log"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            rev = ReviewRecord.spawn_and_begin(
                self.store,
                "bg",
                {"kind": "lane", "lane": "bg"},
                cmd=[sys.executable, "-c", "print('spawned')"],
                cwd=str(self.tmp),
                env=dict(os.environ),
                log=log,
                effort="medium",
            )
        pid = (rev.raw or {}).get("pid")
        self.assertIsInstance(pid, int)
        assert isinstance(pid, int)
        self.assertTrue(log.exists())
        with contextlib.suppress(OSError):
            os.kill(pid, 9)
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, 0)


class PreLandFixMarkAndStaleWorkerIntegrationTests(LaneCase):
    def test_open_pre_land_fix_writes_only_review_receipt_and_legacy_fallback_respects_tip(self) -> None:
        cfg = config.load(self.integration / ".lanefather.toml")
        store = state.Store(cfg, directory=self.state)
        wt = pathlib.Path(self.created("a", "lib/**"))
        reviewed_sha = self.commit(wt, {"lib/api.txt": "foo\nbaz\n"})

        live_pid = os.getpid()
        ReviewRecord.begin(
            store, "a", {"kind": "lane", "lane": "a", "tip": reviewed_sha}, pid=live_pid, log=None, effort="medium"
        )
        ReviewRecord.finish(
            store,
            "a",
            {"reviewer": "sol", "findings": [{"n": 1, "severity": "P2", "title": "bug", "mark": None}]},
            owner_pid=live_pid,
        )

        record_path = store.record_path("a")
        record_bytes_before = record_path.read_bytes()
        record = store.load("a")
        updated_rev, fix = ReviewRecord.open_pre_land_fix(store, record, str(wt))

        # `<id>.json` was not touched at all; `.review.json` holds `pre_land_fix`.
        self.assertEqual(record_path.read_bytes(), record_bytes_before)
        self.assertNotIn("pre_land_fix", store.load("a"))
        self.assertEqual(fix.reviewed_sha, reviewed_sha)
        self.assertEqual(fix.findings, (1,))
        self.assertEqual(updated_rev.pre_land_fix(record), fix)

        # Duplicate pre_land_fix is refused.
        with self.assertRaises(LaneError):
            ReviewRecord.open_pre_land_fix(store, record, str(wt))

        # Mark refuses reviewed_sha itself and accepts a descendant commit (or active lane id `a`).
        with self.assertRaises(LaneError):
            ReviewRecord.mark(store, record, [("1", f"fixed:{reviewed_sha}")])
        fix_sha = self.commit(wt, {"lib/api.txt": "foo\nbaz\nfixed\n"}, "fix finding")
        marked = ReviewRecord.mark(store, record, [("1", "fixed:a")])
        self.assertEqual(marked.fixed_count, 1)
        self.assertEqual(marked.rejected_count, 0)
        self.assertEqual(marked.applied[0].merge, fix_sha)
        self.assertIsInstance(marked.review.verdict, Complete)

        # Legacy fallback: when `.review.json` lacks `pre_land_fix`, `record["pre_land_fix"]` applies iff tip matches.
        legacy_fix = {"reviewed_sha": reviewed_sha, "branch": "lane/a", "worktree": str(wt), "findings": [1]}
        raw_no_plf = ReviewRecord.from_raw("a", {"status": "finished", "tip": reviewed_sha, "findings": []})
        self.assertEqual(
            raw_no_plf.pre_land_fix({"pre_land_fix": legacy_fix}),
            PreLandFix(reviewed_sha=reviewed_sha, branch="lane/a", worktree=str(wt), findings=(1,)),
        )
        stale_tip_rev = ReviewRecord.from_raw("a", {"status": "finished", "tip": fix_sha, "findings": []})
        self.assertIsNone(stale_tip_rev.pre_land_fix({"pre_land_fix": legacy_fix}))

    def test_perform_review_propagates_stale_worker_error_without_overwriting_newer_receipt(self) -> None:
        wt = pathlib.Path(self.created("a", "lib/**"))
        tip = self.commit(wt, {"lib/api.txt": "foo\nbaz\n"})
        base = self.record("a")["base_sha"]

        with mock.patch.dict(os.environ, self.env, clear=False):
            cfg = config.load(self.integration / ".lanefather.toml")
            store = state.Store(cfg, directory=self.state)

            other_pid = os.getppid()
            self.assertNotEqual(other_pid, os.getpid())

            def _supersede_during_run(*_args: Any, **_kwargs: Any) -> Any:
                # Another live worker forces a re-review and finishes while this worker is mid-flight.
                ReviewRecord.begin(
                    store,
                    "a",
                    {"kind": "lane", "lane": "a", "base": base, "tip": tip},
                    pid=other_pid,
                    log=None,
                    effort="high",
                    force=True,
                )
                ReviewRecord.finish(
                    store,
                    "a",
                    {"reviewer": "opus", "model": "claude-opus-5-5", "findings": []},
                    owner_pid=other_pid,
                )
                from lanefather import reviewer as reviewer_mod
                return reviewer_mod.Answer(evidence={"reviewer": "sol", "findings": []}, error=None)

            with (
                mock.patch("lanefather.reviewer.Reviewer.run", side_effect=_supersede_during_run),
                self.assertRaises(StaleReviewRunError),
            ):
                review.perform_review(store, "a", {"kind": "lane", "lane": "a"}, str(self.repo), base, tip, "subj")

            # Newer worker's finished receipt stands untouched (not overwritten with `failed`).
            winner = ReviewRecord.load(store, "a")
            self.assertEqual(winner.phase, ReceiptPhase.FINISHED)
            self.assertEqual((winner.raw or {}).get("reviewer"), "opus")

            # Same guarantee when the stale worker itself fails mid-run after being superseded.
            def _supersede_then_raise(*_args: Any, **_kwargs: Any) -> Any:
                ReviewRecord.begin(
                    store,
                    "a",
                    {"kind": "lane", "lane": "a", "base": base, "tip": tip},
                    pid=other_pid,
                    log=None,
                    effort="high",
                    force=True,
                )
                raise RuntimeError("stale worker exploded")

            with (
                mock.patch("lanefather.reviewer.Reviewer.run", side_effect=_supersede_then_raise),
                self.assertRaises(StaleReviewRunError),
            ):
                review.perform_review(
                    store, "a", {"kind": "lane", "lane": "a"}, str(self.repo), base, tip, "subj", force=True
                )
            still_running = state.read_json(store.receipt_path("a", "review")) or {}
            self.assertEqual((still_running.get("status"), still_running.get("pid")), ("running", other_pid))


class StaleAndDeltaReviewRecordTests(unittest.TestCase):
    """fathers#53: Stale verdict, synthetic fix_parent refusal, and delta review state transitions."""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-stale-rec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = state.Store(config.Config(project="Alpha"), directory=self.tmp / "state")
        self.store.directory.mkdir(parents=True, exist_ok=True)

    def test_synthetic_stale_parent_is_refused_by_fix_parent_and_review_exemption(self) -> None:
        parent: state.Record = {
            "id": "reused",
            "status": "landed",
            "common_dir": "/nonexistent/repo/.git",
            "created": "2026-11-17T00:00:00Z",
            "landed": {"merge": "merge-two", "tip": "tip-two", "tree": "tree-two"},
        }
        self.store.save(parent)
        state.write_json(
            self.store.receipt_path("reused", "review"),
            {
                "version": 1,
                "label": "reused",
                "kind": "lane",
                "lane": "reused",
                "lane_created": parent["created"],
                "status": "finished",
                "tip": "tip-one",
                "tree": "tree-one",
                "reviewer": "sol",
                "findings": [{"n": 1, "severity": "P2", "title": "bug", "mark": None}],
            },
        )
        loaded = ReviewRecord.load(self.store, "reused", record=parent)
        self.assertIsInstance(loaded.verdict, review_record.Stale)
        assert isinstance(loaded.verdict, review_record.Stale)
        self.assertEqual((loaded.verdict.reviewed_tip, loaded.verdict.landed_tip), ("tip-one", "tip-two"))
        self.assertFalse(loaded.allows_promote)

        with self.assertRaisesRegex(LaneError, "review is stale"):
            ReviewRecord.fix_parent(self.store, "reused", "/nonexistent/repo/.git", "review exemption refused")

        fix_rec: state.Record = {
            "id": "reused-fix",
            "status": "active",
            "common_dir": "/nonexistent/repo/.git",
            "review_fix_of": "reused",
            "review_fix_of_all": ["reused"],
        }
        self.assertIsNone(review.review_exemption(fix_rec, self.store))

    def test_delta_review_lifecycle_numbering_and_stale_worker_fencing(self) -> None:
        rec: state.Record = {
            "id": "a",
            "status": "landed",
            "common_dir": "/nonexistent/repo/.git",
            "created": "2026-11-17T00:00:00Z",
            "landed": {"merge": "m2", "tip": "tip-two", "tree": "tree-two"},
        }
        self.store.save(rec)
        state.write_json(
            self.store.receipt_path("a", "review"),
            {
                "version": 1,
                "label": "a",
                "kind": "lane",
                "lane": "a",
                "lane_created": rec["created"],
                "status": "finished",
                "tip": "tip-one",
                "tree": "tree-one",
                "reviewer": "sol",
                "findings": [
                    {"n": 1, "severity": "P2", "title": "p1", "mark": {"kind": "rejected", "reason": "ok"}}
                ],
            },
        )
        pid = os.getpid()
        started = ReviewRecord.begin_delta(
            self.store, "a", {}, base="tip-one", tip="tip-two", tree="tree-two", pid=pid, log=None, effort="medium",
            record=rec,
        )
        self.assertIsInstance(started.verdict, review_record.Stale)
        assert isinstance(started.verdict, review_record.Stale)
        self.assertIsInstance(started.verdict.delta, Running)

        with self.assertRaises(StaleReviewRunError):
            ReviewRecord.finish_delta(
                self.store, "a", {"reviewer": "sol", "findings": []}, owner_pid=pid + 999, record=rec
            )

        finished = ReviewRecord.finish_delta(
            self.store,
            "a",
            {"reviewer": "sol", "findings": [{"n": 1, "severity": "P2", "title": "delta bug", "mark": None}]},
            owner_pid=pid,
            record=rec,
        )
        self.assertIsInstance(finished.verdict, Unmarked)
        self.assertEqual([f["n"] for f in finished.findings], [1, 2])
        self.assertEqual([f["n"] for f in finished.open_findings], [2])

        marked = ReviewRecord.mark(self.store, rec, [("2", "rejected:fixed elsewhere")])
        self.assertIsInstance(marked.review.verdict, Complete)
        self.assertTrue(marked.review.allows_promote)

    def test_non_ancestor_stale_start_lane_review_does_not_crash(self) -> None:
        # P2 #7: start_lane_review with force_fresh=False on non-ancestor Stale returns a clean message.
        rec: state.Record = {
            "id": "a",
            "ticket": "42",
            "status": "landed",
            "common_dir": "/nonexistent/repo/.git",
            "created": "2026-11-17T00:00:00Z",
            "landed": {"merge": "m2", "tip": "tip-two", "tree": "tree-two"},
        }
        self.store.save(rec)
        state.write_json(
            self.store.receipt_path("a", "review"),
            {
                "version": 1,
                "label": "a",
                "kind": "lane",
                "lane": "a",
                "lane_created": rec["created"],
                "status": "finished",
                "tip": "tip-one",
                "tree": "tree-one",
                "reviewer": "sol",
                "findings": [],
            },
        )
        msg = review.start_lane_review(self.store, rec, force_fresh=False)
        self.assertIn("review stale (cannot delta)", msg)
        self.assertIn("--force-reason", msg)

    def test_partial_panel_takes_precedence_over_stale_and_corrupt_tip_fails_closed(self) -> None:
        # P2 #6 + P2 #8 + P2 #9: PartialPanel is not masked by Stale; corrupt tip fails closed; exempt delta rejected.
        rec: state.Record = {
            "id": "p",
            "status": "landed",
            "common_dir": "/nonexistent/repo/.git",
            "created": "2026-11-17T00:00:00Z",
            "landed": {"merge": "m2", "tip": "tip-two", "tree": "tree-two"},
        }
        panel_store = state.Store(
            config.Config(project="Alpha", review_panel_required=True), directory=self.tmp / "state"
        )
        panel_store.save(rec)
        state.write_json(
            panel_store.receipt_path("p", "review"),
            {
                "version": 1,
                "label": "p",
                "kind": "lane",
                "lane": "p",
                "lane_created": rec["created"],
                "status": "finished",
                "tip": "tip-one",
                "tree": "tree-one",
                "reviewer": "sol",
                "panel": [{"member": "opus", "status": "failed", "error": "boom"}],
                "findings": [],
            },
        )
        loaded = ReviewRecord.load(panel_store, "p", record=rec)
        self.assertIsInstance(loaded.verdict, PartialPanel)

        # Corrupt/empty tip fails closed as Stale; exempt delta does NOT clear Stale.
        corrupt_verdict = review_record.derive_verdict(
            {
                "status": "finished",
                "kind": "lane",
                "tip": "",
                "tree": "tree-one",
                "findings": [],
                "delta": {"kind": "delta", "base": "", "tip": "tip-two", "status": "exempt", "exempt": "fake"},
            },
            record=rec,
        )
        self.assertIsInstance(corrupt_verdict, review_record.Stale)

        # Delta with status=exempt on matching base/tip stays Stale.
        exempt_delta = review_record.derive_verdict(
            {
                "status": "finished",
                "kind": "lane",
                "tip": "tip-one",
                "tree": "tree-one",
                "findings": [],
                "delta": {"kind": "delta", "base": "tip-one", "tip": "tip-two", "status": "exempt", "exempt": "fake"},
            },
            record=rec,
        )
        self.assertIsInstance(exempt_delta, review_record.Stale)

    def test_status_latest_logs_recognizes_delta_review_log(self) -> None:
        from lanefather import status

        logs_dir = self.tmp / "state" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        delta_log = logs_dir / "review-a-delta-20261124T100000.log"
        delta_log.write_text("delta\n")
        found = status.latest_logs(self.tmp / "state", {"a"})
        self.assertEqual(found.get("a", {}).get("review"), str(delta_log))


class ProcessIdentityAndSupersededRunTests(unittest.TestCase):
    """fathers#60: `(pid, start)` process identity, recycled PID -> Died, start fencing, and superseded tracking."""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-proc-id-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = state.Store(config.Config(project="Alpha"), directory=self.tmp / "state")
        self.store.directory.mkdir(parents=True, exist_ok=True)

    def test_recycled_pid_with_mismatched_start_reads_as_died_for_primary_and_delta(self) -> None:
        live_pid = os.getpid()
        real_start = review_record.process_start(live_pid)
        self.assertIsInstance(real_start, str)
        stale_start = "Mon Jan 01 00:00:00 2020"

        # Matching (pid, start) reads Running.
        running_v = review_record.derive_verdict(
            {"status": "running", "pid": live_pid, "start": real_start, "started": "t0", "log": "/l"}
        )
        self.assertIsInstance(running_v, Running)
        assert isinstance(running_v, Running)
        self.assertEqual(running_v.start, real_start)

        # Live PID with different recorded start time reads Died.
        died_v = review_record.derive_verdict(
            {"status": "running", "pid": live_pid, "start": stale_start, "started": "t0", "log": "/l"}
        )
        self.assertIsInstance(died_v, Died)
        assert isinstance(died_v, Died)
        self.assertEqual(died_v.start, stale_start)

        # Malformed or non-finite start values fail closed to Died (strict JSON boundary typing).
        bad_starts: tuple[Any, ...] = ("", "   ", True, False, [], {}, float("nan"), float("inf"), float("-inf"))
        for bad_start in bad_starts:
            with self.subTest(bad_start=bad_start):
                self.assertIsInstance(
                    review_record.derive_verdict(
                        {"status": "running", "pid": live_pid, "start": bad_start, "started": "t0", "log": "/l"}
                    ),
                    Died,
                )

        # Overflowing PIDs never crash `alive()`.
        self.assertFalse(review_record.alive(99_999_999_999_999_999_999))

        # Corrupted start in `live_checkout_info` falls back to `alive(pid)` so a live review keeps its checkout.
        corrupt_checkout = self.tmp / "reviews" / "a-checkout-corrupt"
        state.write_json(
            self.store.receipt_path("a", "review"),
            {
                "version": 1,
                "label": "a",
                "status": "running",
                "pid": live_pid,
                "start": float("nan"),
                "checkout": str(corrupt_checkout),
            },
        )
        protected, pid_liveness = review_record.live_checkout_info(self.store, "a")
        self.assertIn(pathlib.Path(os.path.realpath(corrupt_checkout)), protected)
        self.assertIs(pid_liveness.get(live_pid), True)

        # Overall review Died hint names `--overall <sha>`, not `review ship-...`.
        dead_overall = ReviewRecord.from_raw(
            "ship-abcdef123456",
            {
                "version": 1,
                "label": "ship-abcdef123456",
                "kind": "overall",
                "sha": "abcdef1234567890abcdef1234567890abcdef12",
                "status": "running",
                "pid": 999_999_999,
                "started": "t0",
                "log": "/tmp/o.log",
            },
        )
        overall_lines = review.review_lines(dead_overall, prog="lanefather")
        self.assertIn("rerun lanefather review --overall abcdef1234", overall_lines[0])

        # Legacy receipt without `start` falls back to PID liveness.
        self.assertIsInstance(
            review_record.derive_verdict({"status": "running", "pid": live_pid, "started": "t0", "log": "/l"}),
            Running,
        )
        self.assertIsInstance(
            review_record.derive_verdict({"status": "running", "pid": 999_999_999, "started": "t0", "log": "/l"}),
            Died,
        )

        # Delta review with live PID but mismatched start time also reads Died under Stale.
        rec: state.Record = {
            "id": "a",
            "status": "landed",
            "common_dir": "/nonexistent/repo/.git",
            "created": "2026-11-17T00:00:00Z",
            "landed": {"merge": "m2", "tip": "tip-two", "tree": "tree-two"},
        }
        delta_stale = review_record.derive_verdict(
            {
                "status": "finished",
                "kind": "lane",
                "tip": "tip-one",
                "tree": "tree-one",
                "findings": [],
                "delta": {
                    "kind": "delta",
                    "base": "tip-one",
                    "tip": "tip-two",
                    "tree": "tree-two",
                    "status": "running",
                    "pid": live_pid,
                    "start": stale_start,
                    "started": "t0",
                    "log": "/d.log",
                },
            },
            record=rec,
        )
        self.assertIsInstance(delta_stale, review_record.Stale)
        assert isinstance(delta_stale, review_record.Stale)
        self.assertIsInstance(delta_stale.delta, Died)

    def test_start_fencing_rejects_same_pid_with_different_start_and_records_superseded(self) -> None:
        live_pid = os.getpid()
        other_pid = os.getppid()
        self.assertNotEqual(live_pid, other_pid)
        live_start = review_record.process_start(live_pid)
        other_start = review_record.process_start(other_pid)
        stale_start = "Mon Jan 01 00:00:00 2020"

        # Worker 1 starts; Worker 2 forces a rerun -> Worker 1 is recorded as superseded.
        rev1 = ReviewRecord.begin(
            self.store, "a", {"kind": "lane"}, pid=other_pid, pid_start=other_start, log="/l1", effort="medium"
        )
        self.assertEqual((rev1.raw or {}).get("start"), other_start)
        self.assertTrue(str((rev1.raw or {}).get("checkout") or "").endswith("a-checkout-" + str(other_pid)))

        rev2 = ReviewRecord.begin(
            self.store,
            "a",
            {"kind": "lane"},
            pid=live_pid,
            pid_start=live_start,
            log="/l2",
            effort="high",
            force=True,
        )
        superseded: list[dict[str, Any]] = (rev2.raw or {}).get("superseded") or []
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["pid"], other_pid)
        self.assertEqual(superseded[0]["start"], other_start)
        self.assertEqual(superseded[0]["status"], "superseded")

        # Superseded worker (different PID) AND same PID with different start are both fenced out.
        with self.assertRaises(StaleReviewRunError):
            ReviewRecord.finish(self.store, "a", {"findings": []}, owner_pid=other_pid, owner_start=other_start)
        with self.assertRaises(StaleReviewRunError):
            ReviewRecord.finish(self.store, "a", {"findings": []}, owner_pid=live_pid, owner_start=stale_start)
        with self.assertRaises(StaleReviewRunError):
            ReviewRecord.fail(self.store, "a", "late error", owner_pid=live_pid, owner_start=stale_start)

        # Current owner (matching PID and start) finishes cleanly.
        finished = ReviewRecord.finish(self.store, "a", {"findings": []}, owner_pid=live_pid, owner_start=live_start)
        self.assertEqual(finished.phase, ReceiptPhase.FINISHED)

        # Delta start fencing with mismatched start on same PID.
        ReviewRecord.begin_delta(
            self.store,
            "a",
            {},
            base="tip-one",
            tip="tip-two",
            tree="tree-two",
            pid=live_pid,
            pid_start=live_start,
            log="/d.log",
            effort="medium",
        )
        with self.assertRaises(StaleReviewRunError):
            ReviewRecord.finish_delta(
                self.store, "a", {"findings": []}, owner_pid=live_pid, owner_start=stale_start
            )
        with self.assertRaises(StaleReviewRunError):
            ReviewRecord.fail_delta(
                self.store, "a", "late delta err", owner_pid=live_pid, owner_start=stale_start
            )


if __name__ == "__main__":
    unittest.main()
