"""Focused unit tests for gate.py, gitops.move_branch, and promote.py's ordered step list (fathers#73)."""
from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import tomllib
import unittest
from unittest import mock

from support import FIXTURE_CONFIG, LaneCase

from lanefather import config, gate, gitops, promote


class PromoteStepsTests(unittest.TestCase):
    def test_lanes_does_not_import_promote(self) -> None:
        lanes_path = Path(__file__).resolve().parent.parent / "lanefather" / "lanes.py"
        tree = ast.parse(lanes_path.read_text(), filename=str(lanes_path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
        self.assertNotIn("promote", imported)
        self.assertNotIn("lanefather.promote", imported)

    def test_promote_steps_order_without_and_with_install_deploy(self) -> None:
        from lanefather import state
        run = promote.PromoteRun(
            store=state.Store(config.DEFAULT),
            cwd="/repo",
            common="/repo/.git",
            sha="deadbeef" * 5,
            branches=["dev"],
            remote="origin",
        )
        names = [name for name, _ in promote.promote_steps(run)]
        self.assertEqual(
            names,
            [
                "no_push_refusal",
                "ahead_warnings",
                "ship_refusal",
                "review_gate",
                "batch_gate",
                "review_refusals",
                "perf_suite",
                "publish_refs",
                "after_promote",
                "release_comments",
                "overall_review",
            ],
        )

        fixture_store = state.Store(config.parse(tomllib.loads(FIXTURE_CONFIG)))
        run_install = promote.PromoteRun(
            store=fixture_store,
            cwd="/repo",
            common="/repo/.git",
            sha="deadbeef" * 5,
            branches=["dev", "main"],
            remote="origin",
            ship=True,
            install=True,
            deploy="myhost",
        )
        install_names = [name for name, _ in promote.promote_steps(run_install)]
        self.assertEqual(
            install_names[len(names):],
            ["step:install", "step:check-installed", "step:link-package", "step:deploy", "install_summary"],
        )

    def test_promote_sha_stops_on_nonzero_step_return(self) -> None:
        import tempfile

        from lanefather import state
        with tempfile.TemporaryDirectory() as tmp:
            run = promote.PromoteRun(
                store=state.Store(config.DEFAULT, directory=Path(tmp)),
                cwd="/repo",
                common="/repo/.git",
                sha="deadbeef" * 5,
                branches=["dev"],
                remote="origin",
            )
            called: list[str] = []

            def ok_step(_r: promote.PromoteRun) -> None:
                called.append("ok")

            def fail_step(_r: promote.PromoteRun) -> int:
                called.append("fail")
                return 1

            def later_step(_r: promote.PromoteRun) -> None:
                called.append("later")

            with mock.patch.object(promote, "promote_steps", return_value=[
                ("ok", ok_step),
                ("fail", fail_step),
                ("later", later_step),
            ]):
                rc = promote.promote_sha(run)
            self.assertEqual(rc, 1)
            self.assertEqual(called, ["ok", "fail"])

    def test_promote_sha_resumes_from_first_unfinished_step(self) -> None:
        import tempfile

        from lanefather import state
        with tempfile.TemporaryDirectory() as tmp:
            store = state.Store(config.DEFAULT, directory=Path(tmp))
            sha = "cafebabe" * 5
            called: list[str] = []
            fail_once = {"fail": True}

            def step1(_r: promote.PromoteRun) -> None:
                called.append("step1")

            def step2(_r: promote.PromoteRun) -> int:
                called.append("step2")
                if fail_once["fail"]:
                    fail_once["fail"] = False
                    return 1
                return 0

            def step3(_r: promote.PromoteRun) -> None:
                called.append("step3")

            steps = [("step1", step1), ("step2", step2), ("step3", step3)]
            with mock.patch.object(promote, "promote_steps", return_value=steps):
                first = promote.PromoteRun(store=store, cwd="/repo", common="/repo/.git", sha=sha,
                                           branches=["dev"], remote="origin")
                self.assertEqual(promote.promote_sha(first), 1)
                self.assertEqual(called, ["step1", "step2"])
                called.clear()
                second = promote.PromoteRun(store=store, cwd="/repo", common="/repo/.git", sha=sha,
                                            branches=["dev"], remote="origin")
                self.assertEqual(promote.promote_sha(second), 0)
                self.assertEqual(called, ["step2", "step3"])

    def test_load_promote_record_strict_validation(self) -> None:
        import json
        import tempfile

        from lanefather import state
        from lanefather.errors import LaneError

        sha = "12345678" * 5
        valid = {
            "version": 1,
            "sha": sha,
            "branches": ["dev", "main"],
            "remote": "origin",
            "ship": True,
            "no_push": False,
            "install": False,
            "deploy": None,
            "ship_base": "a" * 40,
            "pushed": True,
            "status": "failed",
            "started": "2026-11-24T00:00:00Z",
            "updated": "2026-11-24T00:00:01Z",
            "finished": None,
            "steps": {
                "publish_refs": {"status": "failed", "returncode": 1, "pushed": True, "ship_base": "a" * 40},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = state.Store(config.DEFAULT, directory=Path(tmp))
            path = store.promote_run_path(sha)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(valid))
            loaded = promote.load_promote_record(store, sha)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded["ship_base"], "a" * 40)
            # Bool must not be accepted where strict int is required.
            for bad in (
                {**valid, "version": True},
                {**valid, "steps": {"publish_refs": {"status": "failed", "returncode": True}}},
                {**valid, "steps": {"step:install": {"status": "failed", "exit": False}}},
                {**valid, "ship": 1},
                {**valid, "branches": [123]},
                {**valid, "sha": "0" * 40},
            ):
                path.write_text(json.dumps(bad))
                self.assertIsNone(promote.load_promote_record(store, sha), bad)
            # A newer record version raises LaneError.
            path.write_text(json.dumps({**valid, "version": 2}))
            with self.assertRaises(LaneError):
                promote.load_promote_record(store, sha)

    def test_gate_format_bisect_lines_separates_merges_and_direct(self) -> None:
        import tempfile

        from lanefather import state
        with tempfile.TemporaryDirectory() as tmp:
            store = state.Store(config.DEFAULT, directory=Path(tmp))
            lines = gate._format_bisect_lines(  # pyright: ignore[reportPrivateUsage]
                store,
                [("1111111111abcdef", "Merge lane a")],
                [("2222222222abcdef", "Direct commit")],
                "Header:",
                "Other:",
            )
        self.assertEqual(lines[0], "Header:")
        self.assertIn("1111111111 Merge lane a", lines[1])
        self.assertEqual(lines[2], "Other:")
        self.assertIn("2222222222 Direct commit", lines[3])


class MoveBranchAndBisectTests(LaneCase):
    def test_move_branch_update_ref_and_checked_out_worktree(self):
        base = self.tip()
        self.git(self.repo, "branch", "side", base)
        (self.repo / "f.txt").write_text("x\n")
        self.git(self.repo, "add", "f.txt")
        self.git(self.repo, "commit", "-m", "advance")
        new_sha = self.tip()

        # side is not checked out -> update-ref
        err = gitops.move_branch(str(self.repo), "side", base, new_sha, None, "test move")
        self.assertIsNone(err)
        self.assertEqual(self.git(self.repo, "rev-parse", "side"), new_sha)

        # non-existent expected old -> update-ref fails and returns error message
        bad = gitops.move_branch(str(self.repo), "side", "0" * 40, base, None, "bad move")
        self.assertIsNotNone(bad)
        assert bad is not None
        self.assertIn("local side in", bad)

        # checked-out worktree reset=True
        wt = self.tmp / "side-wt"
        subprocess.run(["git", "worktree", "add", str(wt), "side"], cwd=self.repo, check=True, capture_output=True)
        err2 = gitops.move_branch(str(self.repo), "side", new_sha, base, None, "reset move", reset=True)
        self.assertIsNone(err2)
        self.assertEqual(self.git(self.repo, "rev-parse", "side"), base)


if __name__ == "__main__":
    unittest.main()
