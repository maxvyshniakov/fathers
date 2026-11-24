"""Tests for the single lane lifecycle module (lanefather.lifecycle, fathers#69 / #55):
- Transition table over every (status, event) pair, including idempotent repeats and `done -> release` refused.
- Static guard ensuring lane-record status mutations and saves stay inside `lifecycle.py` and `Store.update`.
- Threaded race tests: concurrent `finish_landed`, and `check` / `widen_claims` / `adopt_worktree` during `land`.
"""
from __future__ import annotations

import argparse
import ast
import os
import pathlib
import shutil
import tempfile
import threading
from typing import Any
import unittest
from unittest import mock

from support import LaneCase

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "lanefather"


class LifecycleTableTests(unittest.TestCase):
    def setUp(self) -> None:
        from lanefather import config, state
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-lifecycle-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.store = state.Store(config.Config(), directory=self.state_dir)

    def _seed(self, lane_id: str, status: str, **extra: Any) -> None:
        record: dict[str, Any] = {
            "id": lane_id, "ticket": "42", "branch": f"lane/{lane_id}", "base": "integrate/q",
            "base_sha": "0" * 40, "claims": ["lib/**"], "after": [], "common_dir": str(self.tmp),
            "integration": str(self.tmp), "worktree": str(self.tmp / lane_id), "status": status,
            "created": "2026-11-21T00:00:00Z", "scratch": [], "cargo_targets": [], "overrides": [],
            "agents": [], "rules": [], **extra,
        }
        self.store.save(record)

    def _ledger_lines(self) -> list[str]:
        path = self.state_dir / "ledger.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_predicates_cover_all_statuses_and_none(self) -> None:
        from lanefather import lifecycle
        expected = {
            "planned":  dict(planned=True, active=False, occupying=True, landed=False, released=False, awaiting_done=False, finished=False, live=True, parked=False, abandoned=False),
            "active":   dict(planned=False, active=True, occupying=True, landed=False, released=False, awaiting_done=False, finished=False, live=True, parked=False, abandoned=False),
            "landed":   dict(planned=False, active=False, occupying=False, landed=True, released=False, awaiting_done=True, finished=False, live=True, parked=False, abandoned=False),
            "released": dict(planned=False, active=False, occupying=False, landed=True, released=True, awaiting_done=True, finished=False, live=True, parked=False, abandoned=False),
            "done":     dict(planned=False, active=False, occupying=False, landed=False, released=False, awaiting_done=False, finished=True, live=False, parked=False, abandoned=False),
        }
        for status, checks in expected.items():
            rec = {"status": status}
            for name, want in checks.items():
                self.assertEqual(getattr(lifecycle, name)(rec), want, f"{name}({status})")
        for name in ("planned", "active", "occupying", "landed", "released", "awaiting_done", "finished", "live",
                     "parked", "abandoned"):
            self.assertFalse(getattr(lifecycle, name)(None), f"{name}(None)")
        # `done` reads as landed only when its outcome is `landed` or legacy `landed` metadata is present.
        self.assertTrue(lifecycle.landed({"status": "done", "landed": {"merge": "a" * 40}}))
        self.assertTrue(lifecycle.landed({"status": "done", "outcome": "landed"}))
        self.assertFalse(lifecycle.landed({"status": "done", "outcome": "parked"}))
        self.assertFalse(lifecycle.landed({"status": "done", "outcome": "parked", "landed": {"merge": "a" * 40}}))
        self.assertFalse(lifecycle.landed({"status": "done", "outcome": "abandoned"}))
        self.assertTrue(lifecycle.parked({"status": "done", "outcome": "parked"}))
        self.assertTrue(lifecycle.abandoned({"status": "done", "outcome": "abandoned"}))
        self.assertEqual(lifecycle.display_state({"status": "done", "outcome": "parked"}), "parked")
        self.assertEqual(lifecycle.display_state({"status": "done", "outcome": "abandoned"}), "abandoned")
        self.assertEqual(lifecycle.display_state({"status": "done", "outcome": "landed"}), "done")
        self.assertEqual(lifecycle.display_state({"status": "done", "dropped": {"why": "empty"}}), "dropped")
        self.assertEqual(lifecycle.display_state(None), "not started")

    def test_every_status_event_pair(self) -> None:
        from lanefather import lifecycle
        from lanefather.errors import LaneError

        allowed: dict[tuple[str, str], str] = {
            ("planned", "adopt"): "active",
            ("active", "land"): "landed",
            ("landed", "release"): "released",
            ("landed", "finish"): "done",
            ("released", "finish"): "done",
            ("planned", "drop"): "done",
            ("active", "drop"): "done",
            ("planned", "park"): "done",
            ("active", "park"): "done",
            ("planned", "abandon"): "done",
            ("active", "abandon"): "done",
            ("landed", "hand_over"): "landed",
            ("released", "hand_over"): "released",
            ("landed", "record_smoke"): "landed",
            ("released", "record_smoke"): "released",
            ("landed", "record_recheck"): "landed",
            ("released", "record_recheck"): "released",
        }
        idempotent: set[tuple[str, str]] = {
            ("active", "adopt"),
            ("landed", "land"),
            ("released", "release"),
            ("done", "finish"),
        }

        for status in lifecycle.STATUSES:
            for event in lifecycle.ALL_EVENTS:
                with self.subTest(status=status, event=event):
                    lane_id = f"{status}-{event}".replace("_", "-")
                    self._seed(lane_id, status)
                    before_ledger = self._ledger_lines()
                    if (status, event) in allowed:
                        want = allowed[(status, event)]
                        if event == "hand_over":
                            result = lifecycle.transition(self.store, lane_id, event,
                                                          worktree=None, handed_over={"to": "fix"})
                        elif event in ("record_smoke", "record_recheck"):
                            result = lifecycle.transition(
                                self.store, lane_id, event,
                                post_merge={"status": "green", "receipt": "/tmp/r.json"},
                            )
                            self.assertEqual(result["post_merge"]["status"], "green")
                        else:
                            result = lifecycle.transition(self.store, lane_id, event)
                        self.assertEqual(result["status"], want)
                        self.assertEqual(self.store.load(lane_id)["status"], want)
                        after_ledger = self._ledger_lines()
                        if event in lifecycle.EVENT_KINDS:
                            self.assertEqual(len(after_ledger), len(before_ledger) + 1)
                        else:
                            self.assertEqual(len(after_ledger), len(before_ledger))
                    elif (status, event) in idempotent:
                        result = lifecycle.transition(self.store, lane_id, event)
                        self.assertEqual(result["status"], status)
                        self.assertEqual(self._ledger_lines(), before_ledger, "idempotent repeat emits no ledger line")
                    else:
                        with self.assertRaises(LaneError):
                            lifecycle.transition(self.store, lane_id, event)
                        self.assertEqual(self.store.load(lane_id)["status"], status)
                        self.assertEqual(self._ledger_lines(), before_ledger)

    def test_done_to_released_is_explicitly_refused(self) -> None:
        from lanefather import lifecycle
        from lanefather.errors import LaneError
        self._seed("x", "done")
        with self.assertRaises(LaneError):
            lifecycle.transition(self.store, "x", "release")
        self.assertEqual(self.store.load("x")["status"], "done")

    def test_unknown_event_is_refused(self) -> None:
        from lanefather import lifecycle
        from lanefather.errors import LaneError
        self._seed("x", "active")
        with self.assertRaises(LaneError):
            lifecycle.transition(self.store, "x", "teleport")
        self.assertEqual(self.store.load("x")["status"], "active")

    def test_transition_does_not_consult_config_current(self) -> None:
        from lanefather import config, lifecycle
        self._seed("x", "planned")
        self.assertFalse(hasattr(config, "current"))
        result = lifecycle.transition(self.store, "x", "adopt", worktree="/tmp/wt", branch="lane/x")
        kept_rec = lifecycle.set_keep(self.store, "x", True)
        self.assertEqual(result["status"], "active")
        self.assertTrue(kept_rec["keep"])

    def test_set_keep_updates_landed_flag_and_refuses_done(self) -> None:
        from lanefather import lifecycle
        from lanefather.errors import LaneError
        self._seed("k", "landed", landed={"merge": "a" * 40, "keep": False})
        rec_on = lifecycle.set_keep(self.store, "k", True)
        self.assertTrue(rec_on["keep"])
        self.assertTrue(rec_on["landed"]["keep"])
        rec_off = lifecycle.set_keep(self.store, "k", False)
        self.assertFalse(rec_off["keep"])
        self.assertFalse(rec_off["landed"]["keep"])
        self._seed("d", "done")
        with self.assertRaises(LaneError):
            lifecycle.set_keep(self.store, "d", True)


class LifecycleGuardTests(unittest.TestCase):
    """Static guard (fathers#69): no module outside `state.py` / `lifecycle.py` mutates lane status or saves a lane
    record directly, nor compares lane record status against string literals."""

    def test_no_direct_status_mutation_or_record_save_outside_lifecycle_and_state(self) -> None:
        from lanefather import lifecycle
        status_literals = set(lifecycle.STATUSES)
        transition_kinds = {"lane.started", "lane.kept", *lifecycle.EVENT_KINDS.values()}
        violations: list[str] = []

        for path in sorted(PKG.glob("*.py")):
            if path.name != "lifecycle.py":
                rec_tree = ast.parse(path.read_text(), filename=str(path))
                for node in ast.walk(rec_tree):
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                                    and target.slice.value in ("post_merge", "keep")):
                                violations.append(f"{path.name}:{node.lineno}: direct [{target.slice.value!r}] = assignment")
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr in ("update", "setdefault", "pop", "__setitem__")):
                        for kw in node.keywords:
                            if kw.arg in ("post_merge", "keep"):
                                violations.append(f"{path.name}:{node.lineno}: .{node.func.attr}({kw.arg}=...) call")
                        if (node.func.attr in ("setdefault", "pop", "__setitem__") and node.args
                                and isinstance(node.args[0], ast.Constant)
                                and node.args[0].value in ("post_merge", "keep")):
                            violations.append(f"{path.name}:{node.lineno}: .{node.func.attr}({node.args[0].value!r}) call")
            if path.name in ("state.py", "lifecycle.py"):
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                # 1. No `state.save(...)` or `store.save(...)` calls outside state.py / lifecycle.py
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "save":
                    violations.append(f"{path.name}:{node.lineno}: direct .save() call")
                # 2. No `record.update(status=...)` calls on lane records
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "update"):
                    for kw in node.keywords:
                        if kw.arg == "status":
                            val_const = kw.value.value if isinstance(kw.value, ast.Constant) else None
                            recv = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
                            if val_const in status_literals or recv in ("record", "rec", "current", "latest"):
                                violations.append(f"{path.name}:{node.lineno}: .update(status=...) call")
                # 3. No `...["status"] = <lane status>` or `record["status"] = ...`
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                                and target.slice.value == "status"):
                            val_const = node.value.value if isinstance(node.value, ast.Constant) else None
                            if val_const in status_literals or path.name != "reviewer.py":
                                violations.append(f"{path.name}:{node.lineno}: direct ['status'] = assignment")
                # 4. No `kind="lane.<transition>"` outside lifecycle.py (events.py only lists KINDS dict)
                if path.name != "events.py" and isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if (kw.arg == "kind" and isinstance(kw.value, ast.Constant)
                                and kw.value.value in transition_kinds):
                            violations.append(f"{path.name}:{node.lineno}: emits {kw.value.value!r} outside lifecycle.py")
                # 5. No comparing a lane record's status against lane status literals
                if isinstance(node, ast.Compare):
                    operands = [node.left, *node.comparators]
                    reads_status = any(
                        (isinstance(op, ast.Subscript) and isinstance(op.slice, ast.Constant)
                         and op.slice.value == "status")
                        or (isinstance(op, ast.Call) and isinstance(op.func, ast.Attribute)
                            and op.func.attr == "get" and op.args
                            and isinstance(op.args[0], ast.Constant) and op.args[0].value == "status")
                        or (isinstance(op, ast.Name) and op.id == "status")
                        for op in operands
                    )
                    if not reads_status:
                        continue
                    has_lane_literal = False
                    for op in operands:
                        if ((isinstance(op, ast.Constant) and op.value in status_literals)
                                or (isinstance(op, (ast.Tuple, ast.List, ast.Set))
                                    and any(isinstance(el, ast.Constant) and el.value in status_literals
                                            for el in op.elts))
                                or (isinstance(op, ast.Attribute) and op.attr in ("OCCUPYING", "LANDED"))):
                            has_lane_literal = True
                    if has_lane_literal:
                        violations.append(f"{path.name}:{node.lineno}: compares status against literal/constant")

        self.assertEqual(violations, [], "\n".join(violations))

    def test_lifecycle_never_calls_config_current(self) -> None:
        tree = ast.parse((PKG / "lifecycle.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "current":
                self.fail(f"lifecycle.py:{node.lineno} must take an explicit state.Store, never config.current()")


class LifecycleRaceTests(LaneCase):
    """Concurrent races guarded by `Store.update` and `lifecycle.transition` (fathers#69)."""

    def _prepare_active_lane(self, lane_id: str, claim: str = "lib/a.txt", file_path: str = "lib/a.txt") -> pathlib.Path:
        # A lane without a ticket: start refuses `none` beside another ticket, so it is named alone.
        self.lane("start", lane_id, "--ticket", "none", "--base", "integrate/q", "--claims", claim, "--create")
        wt = pathlib.Path(self.record(lane_id)["worktree"])
        self.commit(wt, {file_path: f"change for {lane_id}\n"})
        self.lane("check", lane_id, cwd=wt)
        return wt

    def _in_process_store(self) -> Any:
        from lanefather import config, state
        env_patch = mock.patch.dict(os.environ, self.env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        cfg = config.load(self.repo / ".lanefather.toml")
        return state.Store(cfg, directory=self.state)

    def test_concurrent_settle_lane_with_locked_worktree_ends_done(self) -> None:
        from lanefather import done, postmerge, state
        self._prepare_active_lane("r")
        self.lane("land", "r", "--keep")
        # Mark review exempt/finished and unkeep so settle_lane will run done.
        self.lane("keep", "r", "--off")
        state.write_json(self.state / "r.review.json", {"version": 1, "lane": "r", "status": "exempt",
                                                        "reason": "test"})
        store = self._in_process_store()

        lock_seen = threading.Event()
        finish_done = threading.Event()
        errors: list[BaseException] = []
        real_worktree_lock = done.worktree_lock
        real_finish = done.finish
        thread_a_id: list[int] = []

        def fake_worktree_lock(main: str, wt: str) -> str | None:
            if thread_a_id and threading.get_ident() == thread_a_id[0]:
                lock_seen.set()
                # Wait until thread B finishes the lane to `done` before thread A tries to release it!
                finish_done.wait(timeout=10.0)
                return "claude agent r"
            return real_worktree_lock(main, wt)

        def wrapped_finish(*a: Any, **kw: Any) -> int:
            try:
                return real_finish(*a, **kw)
            finally:
                finish_done.set()

        def run_a() -> None:
            thread_a_id.append(threading.get_ident())
            try:
                done.settle_lane(store, "r", mode=postmerge.CompletionMode.AUTO_AFTER_REVIEW)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        def run_b() -> None:
            lock_seen.wait(timeout=10.0)
            try:
                done.settle_lane(store, "r", mode=postmerge.CompletionMode.AUTO_AFTER_REVIEW)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        with (mock.patch.object(done, "worktree_lock", side_effect=fake_worktree_lock),
              mock.patch.object(done, "finish", side_effect=wrapped_finish)):
            t_a = threading.Thread(target=run_a)
            t_b = threading.Thread(target=run_b)
            t_a.start()
            t_b.start()
            t_a.join(timeout=15.0)
            t_b.join(timeout=15.0)

        self.assertEqual(errors, [])
        self.assertEqual(self.record("r")["status"], "done")

    def test_check_during_land_preserves_widened_claims_and_ends_landed(self) -> None:
        from lanefather import claims, land, lanes
        from lanefather.errors import LaneError

        wt = self._prepare_active_lane("c", claim="lib/a.txt", file_path="lib/a.txt")
        store = self._in_process_store()

        entered_merge = threading.Event()
        concurrent_done = threading.Event()
        errors: list[BaseException] = []
        real_landing = land.landing

        def paused_landing(*a: Any, **kw: Any):
            entered_merge.set()
            concurrent_done.wait(timeout=10.0)
            return real_landing(*a, **kw)

        args = argparse.Namespace(
            id="c", into=None, message=None, keep=True, slots=None, force_reason=None, wait=False,
        )

        def run_land() -> None:
            try:
                land.land(args, store)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        def run_concurrent_check_mutations() -> None:
            entered_merge.wait(timeout=10.0)
            try:
                # Simulate `check` widening claims and re-adopting/recording agent while `land` holds `.land.lock`.
                claims.widen_claims(store, "c", ["lib/widened.txt"])
                rec = self.record("c")
                lanes.adopt_worktree(store, rec, str(wt), rec["branch"], agent="codex")
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                concurrent_done.set()

        with mock.patch.object(land, "landing", side_effect=paused_landing):
            t_land = threading.Thread(target=run_land)
            t_check = threading.Thread(target=run_concurrent_check_mutations)
            t_land.start()
            t_check.start()
            t_land.join(timeout=30.0)
            t_check.join(timeout=30.0)

        self.assertEqual(errors, [])
        final = self.record("c")
        self.assertEqual(final["status"], "landed")
        self.assertIn("lib/widened.txt", final["claims"])
        self.assertTrue(any(a.get("kind") == "codex" for a in final.get("agents", [])))

        # And a late auto-adopt after `land` saved is refused and leaves the lane `landed`.
        stale = dict(final, status="planned")
        with self.assertRaises(LaneError):
            lanes.adopt_worktree(store, stale, str(wt), final["branch"], agent="codex")
        self.assertEqual(self.record("c")["status"], "landed")


if __name__ == "__main__":
    unittest.main()
