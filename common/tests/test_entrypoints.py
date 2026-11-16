"""The root's scripts as commands: common/eventcheck.py and install.py, on every Python CI runs (fathers#120).

These were the shell smoke steps of .github/workflows/common.yml; as tests they run in the `common tests` check
locally too. Run from the repository root: python3 -m unittest discover -s common/tests
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)


class EventCheckScript(unittest.TestCase):
    def test_a_bad_log_exits_1_in_strict_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "bad.jsonl"
            log.write_text("{}\n")
            done = run("common/eventcheck.py", "--strict", str(log))
        self.assertEqual(1, done.returncode, done.stderr)
        self.assertIn("bad.jsonl:1:", done.stdout + done.stderr)


class InstallDispatch(unittest.TestCase):
    def test_without_a_known_package_it_prints_its_usage_and_exits_2(self) -> None:
        for argv in ((), ("nosuch",)):
            with self.subTest(argv=argv):
                done = run("install.py", *argv)
                self.assertEqual(2, done.returncode)
                self.assertIn("python3 install.py PACKAGE", done.stderr)

    def test_help_exits_0(self) -> None:
        self.assertEqual(0, run("install.py", "--help").returncode)

    def test_a_package_gets_the_arguments(self) -> None:
        done = run("install.py", "godfather", "--help")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("--bin DIR", done.stdout + done.stderr)  # godfather/install.py's usage, not the root's
        self.assertNotIn("PACKAGE", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
