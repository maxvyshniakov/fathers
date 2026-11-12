"""Tests of install.py: releases beside each other, `current` swapped in one rename, the command on its interpreter."""
from __future__ import annotations

import json
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
        # fathers#104: the release ships its changelog, and `changelog --json` reads that copy.
        self.assertEqual((current / "lanefather" / "CHANGELOG.md").read_text(), (ROOT / "CHANGELOG.md").read_text())
        news = subprocess.run([str(command), "changelog", "--json"], capture_output=True, text=True, env=env,
                              cwd=self.tmp)
        answer = json.loads(news.stdout)
        self.assertEqual((answer["tool"], answer["source"]),
                         ("lanefather", str((current / "lanefather" / "CHANGELOG.md").resolve())))
        unreleased = [e for e in answer["entries"] if e.get("unreleased")]
        self.assertTrue(unreleased and all(e["source"] == head and e["version"] is None for e in unreleased))

    def test_old_releases_beyond_three_go(self):
        releases = self.tmp / "share" / "releases"
        releases.mkdir(parents=True)
        for index in range(4):
            (releases / f"0.0.{index}-old").mkdir()
            os.utime(releases / f"0.0.{index}-old", (index, index))
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(sorted(p.name for p in releases.iterdir())[:2], ["0.0.2-old", "0.0.3-old"])
        self.assertEqual(len(list(releases.iterdir())), 3)

    def test_uncommitted_shipping_inputs_are_refused_before_current_or_launcher_changes(self):
        source = self.tmp / "source"
        shutil.copytree(ROOT / "lanefather", source / "lanefather",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "hooks", source / "hooks")
        shutil.copy2(ROOT / "install.py", source / "install.py")
        clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        def git(*args: str):
            result = subprocess.run(["git", "-C", str(source), *args], text=True, capture_output=True, env=clean_env)
            self.assertEqual(result.returncode, 0, result.stderr)
        git("init", "-q", "-b", "codex/source")
        git("config", "user.email", "install@example.invalid")
        git("config", "user.name", "Install test")
        git("add", ".")
        git("commit", "-qm", "source")
        command = [sys.executable, str(source / "install.py"), "--bin", str(self.tmp / "bin"),
                   "--share", str(self.tmp / "share")]
        installed = subprocess.run(command, capture_output=True, text=True, env=clean_env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        current = self.tmp / "share" / "current"
        launcher = self.tmp / "bin" / "lanefather"
        before = (current.readlink(), launcher.read_bytes(), sorted((self.tmp / "share" / "releases").iterdir()))
        decoy = self.tmp / "decoy"
        shutil.copytree(source, decoy)
        hook_env = {**clean_env, "GIT_DIR": str(decoy / ".git"), "GIT_WORK_TREE": str(decoy)}
        for relative in ["lanefather/__init__.py", "hooks/reference-transaction", "install.py", "lanefather/untracked.py"]:
            with self.subTest(relative=relative):
                path = source / relative
                original = path.read_bytes() if path.exists() else None
                path.write_bytes((original or b"") + b"\n# uncommitted input\n")
                for environment in (clean_env, hook_env):
                    refused = subprocess.run(command, capture_output=True, text=True, env=environment)
                    self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                    self.assertIn("uncommitted", refused.stderr)
                self.assertEqual((current.readlink(), launcher.read_bytes(),
                                  sorted((self.tmp / "share" / "releases").iterdir())), before)
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_an_old_interpreter_is_refused(self):
        result = self.install("--python", "/usr/bin/false")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not Python 3.11 or newer", result.stderr)
        self.assertFalse((self.tmp / "bin" / "lanefather").exists())


if __name__ == "__main__":
    unittest.main()
