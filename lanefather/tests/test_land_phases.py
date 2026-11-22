"""Tests for Tidy 4 (`lanefather.land` three-phase split, kill-after-merge recovery, and `lanefather.done`
lock-free cleanup, fathers#72).
"""
from __future__ import annotations

import ast
import fcntl
import inspect
import json
import os
import pathlib
import threading
from typing import Any
import unittest
from unittest import mock

from support import LaneCase

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "lanefather"


class _KilledAfterMerge(RuntimeError):
    """Stand-in exception simulating a process kill between git merge and record save."""


class LandPhasesTests(LaneCase):
    """Kill-after-merge recovery, legacy `--repair`, and lock-free `done` cleanup."""

    def _in_process_store(self) -> Any:
        from lanefather import config, state

        env_patch = mock.patch.dict(os.environ, self.env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        cfg = config.load(self.repo / ".lanefather.toml")
        return state.Store(cfg, directory=self.state)

    def test_kill_after_merge_recovery_finishes_without_second_merge(self) -> None:
        from lanefather import land, lifecycle, state

        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nrecovered\n"})
        self.lane("check", "a")
        store = self._in_process_store()
        integration = self.record("a")["integration"]
        real_transition = lifecycle.transition
        head_before = self.git(integration, "rev-parse", "HEAD")

        def kill_on_land_save(*args: object, **kwargs: object) -> state.Record:
            if len(args) >= 3 and args[2] == "land":
                raise _KilledAfterMerge("simulated kill between merge and save")
            return real_transition(*args, **kwargs)  # type: ignore[arg-type]

        with (mock.patch.object(lifecycle, "transition", side_effect=kill_on_land_save),
              self.assertRaises(_KilledAfterMerge)):
            land.merge(store, "a", None, None, True, None, 0)

        merge_after_kill = self.git(integration, "rev-parse", "HEAD")
        self.assertNotEqual(merge_after_kill, head_before)

        rec_after_kill = self.record("a")
        self.assertEqual(rec_after_kill["status"], "active")
        self.assertIn("landing", rec_after_kill)
        self.assertEqual(rec_after_kill["landing"]["previous"], head_before)

        # Re-land finishes the lane from the landing intent without making a second merge.
        output = self.lane("land", "a", "--keep")
        self.assertIn("post-merge green", output)
        self.assertEqual(self.git(integration, "rev-parse", "HEAD"), merge_after_kill)

        rec_final = self.record("a")
        self.assertEqual(rec_final["status"], "landed")
        self.assertNotIn("landing", rec_final)
        self.assertEqual(rec_final["landed"]["merge"], merge_after_kill)
        self.assertEqual(rec_final["landed"]["previous"], head_before)

    def test_already_merged_without_intent_names_repair_and_repair_finishes(self) -> None:
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nlegacy\n"})
        self.lane("check", "a")

        # Simulate a pre-Tidy-4 merge committed without a `landing` intent on the record.
        integration = self.record("a")["integration"]
        self.git(integration, "merge", "--no-ff", "-m", "Merge lane/a", "lane/a")
        merge_sha = self.git(integration, "rev-parse", "HEAD")

        refused = self.lane("land", "a", ok=False)
        self.assertIn("--repair", refused)
        self.assertEqual(self.record("a")["status"], "active")

        output = self.lane("land", "a", "--repair", "--keep")
        self.assertIn("post-merge green", output)
        self.assertEqual(self.git(integration, "rev-parse", "HEAD"), merge_sha)
        rec = self.record("a")
        self.assertEqual(rec["status"], "landed")
        self.assertEqual(rec["landed"]["merge"], merge_sha)

    def test_done_releases_state_lock_across_gh_du_and_rmtree(self) -> None:
        from lanefather import config, done, state, worktrees

        self.start("d", "lib/**", "--ticket", "404")
        worktree = self.repo / ".claude" / "worktrees" / "agent-d1"
        self.git(self.repo, "worktree", "add", "-q", "-b", "worktree-agent-d1", str(worktree), "integrate/q")
        self.git(worktree, "checkout", "-q", "-B", "lane/d", "integrate/q")
        self.lane("adopt", "d", str(worktree), "lane/d")
        self.commit(worktree, {"lib/api.txt": "foo\nd\n"})
        self.lane("check", "d")
        self.lane("land", "d", "--keep")
        store = self._in_process_store()
        lock_path = (store.directory / ".state.lock").resolve()

        gh_entered = threading.Event()
        other_took_lock = threading.Event()
        done_errors: list[BaseException] = []
        other_errors: list[BaseException] = []
        size_lock_free = False
        rmtree_lock_free = False

        def can_acquire_state_lock_now() -> bool:
            with open(lock_path, "a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                fcntl.flock(handle, fcntl.LOCK_UN)
                return True

        def slow_ticket_commented(record: state.Record, project: config.Config) -> str | None:
            gh_entered.set()
            if not other_took_lock.wait(timeout=10.0):
                raise AssertionError("other command could not acquire state lock while gh ran")
            return None

        real_size = worktrees.size
        real_remove_tree = worktrees.remove_tree

        def checked_size(path: str) -> int:
            nonlocal size_lock_free
            size_lock_free = can_acquire_state_lock_now()
            return real_size(path)

        def checked_remove_tree(path: str, protected: list[str]) -> int:
            nonlocal rmtree_lock_free
            rmtree_lock_free = can_acquire_state_lock_now()
            return real_remove_tree(path, protected)

        def run_done() -> None:
            try:
                done.finish("d", None, store)
            except BaseException as exc:  # pragma: no cover
                done_errors.append(exc)

        def run_other_lane_command() -> None:
            gh_entered.wait(timeout=10.0)
            try:
                # Another lane command (`store.locked("state")` + read/write) proceeds while `gh` is held.
                with store.locked("state"):
                    _ = store.lanes()
                other_took_lock.set()
            except BaseException as exc:  # pragma: no cover
                other_errors.append(exc)

        with (mock.patch.object(done, "ticket_commented", side_effect=slow_ticket_commented),
              mock.patch.object(worktrees, "size", side_effect=checked_size),
              mock.patch.object(worktrees, "remove_tree", side_effect=checked_remove_tree)):
            t_done = threading.Thread(target=run_done)
            t_other = threading.Thread(target=run_other_lane_command)
            t_done.start()
            t_other.start()
            t_done.join(timeout=15.0)
            t_other.join(timeout=15.0)

        self.assertEqual(done_errors, [])
        self.assertEqual(other_errors, [])
        self.assertTrue(other_took_lock.is_set())
        self.assertTrue(size_lock_free, "worktrees.size (du) ran while state lock was held")
        self.assertTrue(rmtree_lock_free, "worktrees.remove_tree (rmtree) ran while state lock was held")
        self.assertEqual(self.record("d")["status"], "done")

    def test_done_crash_during_cleanup_resumes_on_rerun(self) -> None:
        from lanefather import done, worktrees

        worktree = self.created("d", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\ncrash\n"})
        self.lane("check", "d")
        self.lane("land", "d", "--keep")
        store = self._in_process_store()

        with (mock.patch.object(worktrees, "remove_tree", side_effect=RuntimeError("simulated crash during rmtree")),
              self.assertRaises(RuntimeError)):
            done.finish("d", None, store)

        # Lane remains landed after crash during cleanup, and rerunning done finishes it.
        self.assertEqual(self.record("d")["status"], "landed")
        self.lane("done", "d")
        self.assertEqual(self.record("d")["status"], "done")
        self.assertFalse(pathlib.Path(worktree).exists())
        self.assertEqual(json.loads((self.state / "d.json").read_text())["status"], "done")


class Tidy4StructureTests(unittest.TestCase):
    """Static acceptance checks for #72."""

    def test_land_and_done_function_sizes_and_signatures(self) -> None:
        from lanefather import done, land

        land_lines = len(inspect.getsourcelines(land.land)[0])
        finish_lines = len(inspect.getsourcelines(done.finish)[0])
        self.assertLessEqual(land_lines, 40, f"land.land is {land_lines} lines")
        self.assertLessEqual(finish_lines, 40, f"done.finish is {finish_lines} lines")

        allowed_namespace = {
            "land.py": {"land", "recheck"},
            "done.py": {"done", "drop", "keep"},
        }
        for filename, allowed in allowed_namespace.items():
            tree = ast.parse((PKG / filename).read_text())
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name not in allowed:
                    arg_names = [a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)]
                    self.assertNotIn("args", arg_names, f"{filename}:{node.name} must not take namespace `args`")


if __name__ == "__main__":
    unittest.main()
