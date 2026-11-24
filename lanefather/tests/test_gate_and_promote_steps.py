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
            ["install:install", "install:check-installed", "deploy:link-package", "deploy:deploy", "install_summary"],
        )

        # Same step name in [[promote.install]] and [[promote.deploy]] must not collide.
        dup_cfg = config.parse(tomllib.loads(
            'project = "Dup"\n'
            '[[promote.install]]\nname = "build"\nargv = ["true"]\n'
            '[[promote.deploy]]\nname = "build"\nargv = ["true"]\n'
        ))
        run_dup = promote.PromoteRun(
            store=state.Store(dup_cfg),
            cwd="/repo",
            common="/repo/.git",
            sha="deadbeef" * 5,
            branches=["dev"],
            remote="origin",
            install=True,
            deploy="myhost",
        )
        dup_names = [name for name, _ in promote.promote_steps(run_dup)]
        self.assertEqual(dup_names[len(names):], ["install:build", "deploy:build", "install_summary"])

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
            self.assertIsNone(promote.load_promote_record(store, sha))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(valid))
            loaded = promote.load_promote_record(store, sha)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded["ship_base"], "a" * 40)
            # Corrupt/truncated JSON is refused (naming the path) and never silently overwritten.
            path.write_text('{"version": 1, "sha": ')
            with self.assertRaises(LaneError) as caught:
                promote.promote_sha(promote.PromoteRun(
                    store=store, cwd="/repo", common="/repo/.git", sha=sha, branches=["dev"], remote="origin",
                ))
            self.assertIn(str(path), str(caught.exception))
            self.assertEqual(path.read_text(), '{"version": 1, "sha": ')
            # Bool must not be accepted where strict int is required, nor invalid types.
            for bad in (
                {**valid, "version": True},
                {**valid, "steps": {"publish_refs": {"status": "failed", "returncode": True}}},
                {**valid, "steps": {"install:install": {"status": "failed", "exit": False}}},
                {**valid, "ship": 1},
                {**valid, "branches": [123]},
                {**valid, "sha": "0" * 40},
            ):
                path.write_text(json.dumps(bad))
                with self.assertRaises(LaneError, msg=str(bad)):
                    promote.load_promote_record(store, sha)
            # A newer record version raises LaneError.
            path.write_text(json.dumps({**valid, "version": 2}))
            with self.assertRaises(LaneError):
                promote.load_promote_record(store, sha)

    def test_concurrent_promote_sha_serializes_per_sha_and_does_not_interleave(self) -> None:
        import tempfile
        import threading

        from lanefather import state

        sha = "abcdef12" * 5
        old_main = "11111111" * 5
        first_entered = threading.Event()
        release_first = threading.Event()
        events_order: list[str] = []
        comment_calls: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            store = state.Store(config.DEFAULT, directory=Path(tmp))

            def step_publish(r: promote.PromoteRun) -> None:
                tag = "first" if not first_entered.is_set() else "second"
                events_order.append(f"{tag}:publish:start")
                if tag == "first":
                    r.ship_base = old_main
                    r.pushed = True
                    first_entered.set()
                    release_first.wait(timeout=10)
                else:
                    # By the time second enters, first completed and origin/main is already sha.
                    r.ship_base = sha
                    r.pushed = True
                events_order.append(f"{tag}:publish:end")

            def step_comments(r: promote.PromoteRun) -> None:
                if r.ship and r.ship_base and r.ship_base != r.sha:
                    comment_calls.append(r.ship_base)

            steps = [("publish_refs", step_publish), ("release_comments", step_comments)]
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    promote.promote_sha(promote.PromoteRun(
                        store=store, cwd="/repo", common="/repo/.git", sha=sha,
                        branches=["dev", "main"], remote="origin", ship=True,
                    ))
                except BaseException as exc:  # pragma: no cover - surfaced by assertion below
                    errors.append(exc)

            with mock.patch.object(promote, "promote_steps", return_value=steps):
                t1 = threading.Thread(target=worker)
                t2 = threading.Thread(target=worker)
                t1.start()
                self.assertTrue(first_entered.wait(timeout=10))
                t2.start()
                release_first.set()
                t1.join(timeout=10)
                t2.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertEqual(
                events_order,
                ["first:publish:start", "first:publish:end", "second:publish:start", "second:publish:end"],
            )
            self.assertEqual(comment_calls, [old_main], "release_comments ran only once for old_main..sha")

    def test_non_ship_rerun_refuses_to_overwrite_unfinished_pushed_ship(self) -> None:
        import json
        import tempfile

        from lanefather import state
        from lanefather.errors import LaneError

        sha = "22222222" * 5
        old_main = "33333333" * 5
        unfinished_ship = {
            "version": 1,
            "sha": sha,
            "branches": ["dev", "main"],
            "remote": "origin",
            "ship": True,
            "no_push": False,
            "install": False,
            "deploy": None,
            "ship_base": old_main,
            "pushed": True,
            "status": "failed",
            "started": "2026-11-24T00:00:00Z",
            "updated": "2026-11-24T00:00:01Z",
            "finished": None,
            "steps": {"publish_refs": {"status": "failed", "returncode": 1, "pushed": True, "ship_base": old_main}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = state.Store(config.DEFAULT, directory=Path(tmp))
            path = store.promote_run_path(sha)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(unfinished_ship))
            with self.assertRaises(LaneError) as caught:
                promote.promote_sha(promote.PromoteRun(
                    store=store, cwd="/repo", common="/repo/.git", sha=sha,
                    branches=["dev"], remote="origin", ship=False,
                ))
            self.assertIn("--ship", str(caught.exception))
            self.assertIn(old_main[:10], str(caught.exception))
            preserved = promote.load_promote_record(store, sha)
            assert preserved is not None
            self.assertEqual((preserved["ship"], preserved["pushed"], preserved["ship_base"]), (True, True, old_main))

    def test_dropping_install_on_rerun_does_not_mark_failed_install_completed(self) -> None:
        import tempfile

        from lanefather import state

        sha = "44444444" * 5
        fail_step2 = {"fail": True}
        called: list[str] = []

        def ok_step(_r: promote.PromoteRun) -> None:
            called.append("publish_refs")

        def inst1(_r: promote.PromoteRun) -> None:
            called.append("install:one")

        def inst2(_r: promote.PromoteRun) -> int:
            called.append("install:two")
            if fail_step2["fail"]:
                fail_step2["fail"] = False
                return 1
            return 0

        def fake_steps(r: promote.PromoteRun) -> list[tuple[str, promote.PromoteStep]]:
            base: list[tuple[str, promote.PromoteStep]] = [("publish_refs", ok_step)]
            if r.install:
                base.extend([("install:one", inst1), ("install:two", inst2)])
            return base

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(promote, "promote_steps", side_effect=fake_steps):
            store = state.Store(config.DEFAULT, directory=Path(tmp))
            # Run 1: --install fails at step 2.
            rc1 = promote.promote_sha(promote.PromoteRun(
                store=store, cwd="/repo", common="/repo/.git", sha=sha,
                branches=["dev"], remote="origin", install=True,
            ))
            self.assertEqual(rc1, 1)
            self.assertEqual(called, ["publish_refs", "install:one", "install:two"])
            # Run 2: rerun without --install must NOT mark the run completed.
            called.clear()
            rc2 = promote.promote_sha(promote.PromoteRun(
                store=store, cwd="/repo", common="/repo/.git", sha=sha,
                branches=["dev"], remote="origin", install=False,
            ))
            self.assertEqual(rc2, 0)
            self.assertEqual(called, [])
            mid = promote.load_promote_record(store, sha)
            assert mid is not None
            self.assertEqual(mid["status"], "failed")
            self.assertTrue(mid["install"])
            # Run 3: rerun with --install still resumes at install:two and completes.
            rc3 = promote.promote_sha(promote.PromoteRun(
                store=store, cwd="/repo", common="/repo/.git", sha=sha,
                branches=["dev"], remote="origin", install=True,
            ))
            self.assertEqual(rc3, 0)
            self.assertEqual(called, ["install:two"])
            done = promote.load_promote_record(store, sha)
            assert done is not None
            self.assertEqual(done["status"], "completed")

    def test_killed_mid_push_preserves_or_refreshes_ship_base(self) -> None:
        import contextlib
        import json
        import tempfile

        from lanefather import state

        sha = "55555555" * 5
        old_main = "66666666" * 5
        newer_main = "77777777" * 5

        @contextlib.contextmanager
        def fake_landing(*_a: object, **_kw: object):
            yield None

        for label, current_remote_main, expected_base in (
            ("origin/main already at sha", sha, old_main),
            ("origin/main moved before push", newer_main, newer_main),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                store = state.Store(config.DEFAULT, directory=Path(tmp))
                path = store.promote_run_path(sha)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({
                    "version": 1, "sha": sha, "branches": ["dev", "main"], "remote": "origin",
                    "ship": True, "no_push": False, "install": False, "deploy": None,
                    "ship_base": old_main, "pushed": False, "status": "running",
                    "started": "2026-11-24T00:00:00Z", "updated": "2026-11-24T00:00:01Z", "finished": None,
                    "steps": {"publish_refs": {"status": "running", "at": "2026-11-24T00:00:01Z"}},
                }))
                run = promote.PromoteRun(
                    store=store, cwd="/repo", common="/repo/.git", sha=sha,
                    branches=["dev", "main"], remote="origin", ship=True,
                )
                def fake_git(_cwd: str, *args: str, **_kw: object) -> str:
                    return "subj" if args[0] == "log" else sha

                with (
                    mock.patch.object(promote, "promote_steps", return_value=[("publish_refs", promote.step_publish_refs)]),
                    mock.patch.object(promote, "landing", side_effect=fake_landing),
                    mock.patch.object(promote, "fetch"),
                    mock.patch.object(promote.gate, "main_base", return_value=current_remote_main),
                    mock.patch.object(promote, "git", side_effect=fake_git),
                    mock.patch.object(promote, "git_ok", return_value=True),
                    mock.patch.object(promote, "worktree_list", return_value=[]),
                    mock.patch.object(promote.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)),
                    mock.patch.object(promote.journal, "ledger"),
                ):
                    rc = promote.promote_sha(run)
                self.assertEqual(rc, 0)
                self.assertEqual(run.ship_base, expected_base)
                rec = promote.load_promote_record(store, sha)
                assert rec is not None
                self.assertEqual(rec["ship_base"], expected_base)

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
