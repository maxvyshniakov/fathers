"""Focused tests for closing file handles and subprocess pipes across cleanup and failure paths (fathers#188)."""
from __future__ import annotations

import gc
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest
import unittest.mock
import warnings

from support import (
    FAKE_GH,
    FAKE_ROLE,
    FIXTURES,
    MODELS_TABLE,
    InProcessTimeout,
    close_process,
    run_in_process,
)
from test_tickets import FAKE_TICKETS_GH

from lanefather import admission, verify


class ResourceCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lanefather-cleanup-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_close_process_reaps_and_closes_all_pipes_without_resource_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            process = subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            close_process(process)
            self.assertIsNotNone(process.returncode)
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
            del process
            gc.collect()
        resource_warnings = [item for item in caught if issubclass(item.category, ResourceWarning)]
        self.assertEqual(resource_warnings, [])

    def test_run_in_process_closes_temporary_files_on_timeout(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with (
                unittest.mock.patch("lanefather.cli.main", side_effect=InProcessTimeout("timed out")),
                self.assertRaises(InProcessTimeout),
            ):
                run_in_process(["status"], self.tmp, dict(os.environ), timeout=10)
            gc.collect()
        resource_warnings = [item for item in caught if issubclass(item.category, ResourceWarning)]
        self.assertEqual(resource_warnings, [])

    def test_fixture_scripts_emit_no_resource_warnings(self) -> None:
        gh_script = self.tmp / "fake-gh.py"
        gh_script.write_text(FAKE_GH)
        gh_log = self.tmp / "gh.log"
        gh_comments = self.tmp / "gh-comments.json"
        gh_comments.write_text(json.dumps([{"body": "ok"}]))

        role_script = self.tmp / "fake-role.py"
        role_script.write_text(FAKE_ROLE)
        models = self.tmp / "models.toml"
        models.write_text(MODELS_TABLE)

        tickets_gh = self.tmp / "fake-tickets-gh.py"
        tickets_gh.write_text(FAKE_TICKETS_GH)
        tickets_state = self.tmp / "tickets-state.json"
        tickets_state.write_text(json.dumps({"labels": {}, "issues": {"1": {"labels": [], "comments": []}}}))

        queue_hold = self.tmp / "queue-hold"
        pathlib.Path(f"{queue_hold}.waiting").write_text(json.dumps({"pid": os.getpid(), "label": "q", "count": 1}))

        cases: list[tuple[str, list[str], dict[str, str]]] = [
            ("fake_gh create", [sys.executable, "-W", "always::ResourceWarning", str(gh_script), "issue", "create"],
             {"FAKE_GH_LOG": str(gh_log), "FAKE_GH_COMMENTS": str(gh_comments)}),
            ("fake_gh view", [sys.executable, "-W", "always::ResourceWarning", str(gh_script), "issue", "view", "1"],
             {"FAKE_GH_LOG": str(gh_log), "FAKE_GH_COMMENTS": str(gh_comments)}),
            ("fake_role", [sys.executable, "-W", "always::ResourceWarning", str(role_script), "--implementer-family", "anthropic"],
             {"MASTACK_MODELS": str(models), "FIXTURE_ROLE_LOG": str(self.tmp / "role.log")}),
            ("fake_tickets_gh", [sys.executable, "-W", "always::ResourceWarning", str(tickets_gh), "issue", "view", "1"],
             {"FAKE_TICKETS_STATE": str(tickets_state), "FAKE_TICKETS_LOG": str(self.tmp / "tickets.log")}),
            ("fixture_slotfather status", [sys.executable, "-W", "always::ResourceWarning", str(FIXTURES / "slotfather"), "status", "--json"],
             {"FAKE_SLOTFATHER_QUEUE_HOLD": str(queue_hold)}),
        ]
        for label, argv, extra_env in cases:
            with self.subTest(case=label):
                completed = subprocess.run(argv, env={**os.environ, **extra_env}, capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertNotIn("ResourceWarning", completed.stderr)

    def test_verify_run_start_closes_log_handle_when_popen_fails(self) -> None:
        run = object.__new__(verify.Run)
        run.log = self.tmp / "verify.log"
        run.argv = ["nonexistent-verifier"]
        run.root = str(self.tmp)
        run.env = dict(os.environ)
        run.label = "test"
        run.wait_for_load = False
        run._given_load_wait = None  # pyright: ignore[reportPrivateUsage]
        run._banner = ""  # pyright: ignore[reportPrivateUsage]
        run._progress = verify._ProgressTracker(run.log, verify.Selection.checks(["lib-check"]))  # pyright: ignore[reportPrivateUsage]
        run._handle = None  # pyright: ignore[reportPrivateUsage]
        run._child = None  # pyright: ignore[reportPrivateUsage]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with (
                unittest.mock.patch.object(verify.subprocess, "Popen", side_effect=OSError("missing binary")),
                self.assertRaises(OSError),
            ):
                run.start()
            self.assertIsNone(run._handle)  # pyright: ignore[reportPrivateUsage]
            gc.collect()
        resource_warnings = [item for item in caught if issubclass(item.category, ResourceWarning)]
        self.assertEqual(resource_warnings, [])

    def test_admission_transport_closes_pipes_even_if_wait_times_out(self) -> None:
        fake_cmd = self.tmp / "fake-slotfather"
        fake_cmd.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
        fake_cmd.chmod(0o755)

        class TimeoutOnWaitPopen(subprocess.Popen[bytes]):
            def wait(self, timeout: float | None = None) -> int:
                if timeout == 1:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                return super().wait(timeout=timeout)

            def real_wait(self) -> int:
                return super().wait(timeout=5)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            created: list[TimeoutOnWaitPopen] = []

            def factory(*args: Any, **kwargs: Any) -> TimeoutOnWaitPopen:
                proc = TimeoutOnWaitPopen(*args, **kwargs)
                created.append(proc)
                return proc

            with (
                unittest.mock.patch.object(admission.subprocess, "Popen", side_effect=factory),
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                admission.transport(str(fake_cmd), {"v": 1}, timeout=5)
            self.assertEqual(len(created), 1)
            proc = created.pop()
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            self.assertTrue(proc.stdin.closed)
            self.assertTrue(proc.stdout.closed)
            self.assertTrue(proc.stderr.closed)
            proc.real_wait()
            del proc
            gc.collect()
        resource_warnings = [item for item in caught if issubclass(item.category, ResourceWarning)]
        self.assertEqual(resource_warnings, [])


if __name__ == "__main__":
    unittest.main()
