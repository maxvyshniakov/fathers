"""install.py: a release beside the others, `current` swapped in one rename, the command on its interpreter."""
from __future__ import annotations

import os
import subprocess
import sys

from support import ROOT, Case


class InstallTests(Case):
    def install(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "install.py"), "--bin", str(self.tmp / "bin"),
                               "--share", str(self.tmp / "share"), *extra], capture_output=True, text=True)

    def test_the_command_runs_the_current_release_and_names_its_commit(self):
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "junkfather", "install.py"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        result = self.install()
        if dirty:
            self.assertNotEqual(0, result.returncode, "uncommitted shipping inputs are refused")
            self.assertRegex(result.stderr, "uncommitted|committed Git checkout")
            return
        self.assertEqual(0, result.returncode, result.stderr)
        command = self.tmp / "bin" / "junkfather"
        self.assertTrue(command.read_text().startswith("#!/"))
        shown = subprocess.run([str(command), "version"], capture_output=True, text=True, env=self.env,
                               cwd=self.tmp)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
        self.assertIn(f"({head.strip()[:12]})", shown.stdout)
        current = (self.tmp / "share" / "current").resolve()
        self.assertTrue((current / "junkfather" / "cli.py").is_file())
        self.assertTrue((current / "CHANGELOG.md").is_file())
        self.assertFalse(list(current.rglob("__pycache__")))
        self.assertEqual(0, self.install().returncode, "a second install of the same commit")
        self.assertEqual(1, len(os.listdir(self.tmp / "share" / "releases")))
