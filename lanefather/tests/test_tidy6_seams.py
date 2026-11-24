"""Focused unit tests for Tidy 6 seams (fathers#74, fathers#216): github.py and lock-free gh in done.
Static architecture guards live in `test_static_guards.py`."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from support import FIXTURE_CONFIG, LaneCase, ScratchCase, close_process

from lanefather import config, github


class GitHubAdapterTests(ScratchCase):
    def test_github_adapter_origin_repo_and_gh_command(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:example/widgets.git"],
                       check=True, capture_output=True)
        cfg = config.load(self.write(repo, ".lanefather.toml", FIXTURE_CONFIG))
        common = str(repo / ".git")
        self.assertEqual(github.origin_repo(common), "example/widgets")
        self.assertEqual(github.repo_args(common), ["-R", "example/widgets"])
        self.assertEqual(github.repository(common, cfg.tickets), "example/widgets")
        self.assertTrue(github.gh_command(cfg).endswith("gh") or "gh" in github.gh_command(cfg))


class DoneLockFreeGhTests(LaneCase):
    def test_done_runs_gh_outside_state_lock_so_list_proceeds(self):
        # Land a lane with a ticket, then hold `gh` inside `done` and verify `list` takes the state lock meanwhile.
        self.lane("start", "a", "--ticket", "42", "--claims", "a.txt", "--create")
        wt = Path(self.record("a")["worktree"])
        (wt / "a.txt").write_text("hello\n")
        self.git(wt, "add", "a.txt")
        self.git(wt, "commit", "-m", "add a")
        self.lane("check", "a")
        self.lane("land", "a", "--wait", "0", "--keep")
        self.lane("review", "a")

        started = self.tmp / "gh-started"
        release = self.tmp / "gh-release"
        blocking_gh = self.tmp / "blocking-gh"
        blocking_gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, time\n"
            "from pathlib import Path\n"
            f"Path({str(started)!r}).write_text('started')\n"
            f"deadline = time.monotonic() + 10\n"
            f"while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.05)\n"
            "print(json.dumps({'comments': [{'createdAt': '2099-01-01T00:00:00Z', 'body': 'evidence'}]}))\n"
        )
        blocking_gh.chmod(0o755)

        env = {**os.environ, **self.env, "LANEFATHER_GH": str(blocking_gh)}
        proc = subprocess.Popen(
            [sys.executable, "-m", "lanefather", "done", "a"],
            cwd=self.repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(close_process, proc)
        try:
            deadline = time.monotonic() + 10
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(started.exists(), "stand-in gh did not start")

            # `list` must acquire the state lock without blocking on `done`'s gh call.
            listed = subprocess.run(
                [sys.executable, "-m", "lanefather", "list", "--json"],
                cwd=self.repo,
                env=env,
                capture_output=True,
                text=True,
                timeout=3,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
        finally:
            release.write_text("go")
            out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err + out)


if __name__ == "__main__":
    unittest.main()
