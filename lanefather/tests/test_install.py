"""Tests of install.py: releases beside each other, `current` swapped in one rename, the command on its interpreter."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from support import ROOT


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lanefather-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def install(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ROOT / "install.py"), "--bin", str(self.tmp / "bin"),
                               "--share", str(self.tmp / "share"), *extra], capture_output=True, text=True)

    def test_the_command_runs_the_current_release_and_names_its_commit(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        command = self.tmp / "bin" / "lanefather"
        # The interpreter as found, not its resolved versioned file, which a Python upgrade removes.
        found = os.path.abspath(shutil.which(sys.executable) or sys.executable)
        self.assertTrue(command.read_text().startswith(f"#!{found}\n"))
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "LANEFATHER_CONFIG")}
        shown = subprocess.run([str(command), "--version"], capture_output=True, text=True, env=env, cwd=self.tmp)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        self.assertIn(f"lanefather 0.1.0 ({head}", shown.stdout)
        current = (self.tmp / "share" / "current").resolve()
        self.assertTrue((current / "lanefather" / "cli.py").is_file())
        self.assertFalse(list(current.rglob("__pycache__")))

    def test_old_releases_beyond_three_go(self):
        releases = self.tmp / "share" / "releases"
        releases.mkdir(parents=True)
        for index in range(4):
            (releases / f"0.0.{index}-old").mkdir()
            os.utime(releases / f"0.0.{index}-old", (index, index))
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(sorted(p.name for p in releases.iterdir())[:2], ["0.0.2-old", "0.0.3-old"])
        self.assertEqual(len(list(releases.iterdir())), 3)

    def test_an_old_interpreter_is_refused(self):
        result = self.install("--python", "/usr/bin/false")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not Python 3.11 or newer", result.stderr)
        self.assertFalse((self.tmp / "bin" / "lanefather").exists())


if __name__ == "__main__":
    unittest.main()
