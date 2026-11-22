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
        from lanefather import state
        run = promote.PromoteRun(
            store=state.Store(config.DEFAULT),
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
