"""install.py: an immutable release, an atomic entry point on this interpreter, refusals for virtual environments and
old Pythons; a running dashboard can tell a newer install (↻)."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from helpers import ROOT


def installer() -> Any:
    spec = importlib.util.spec_from_file_location("godfather_install", ROOT / "install.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.tool = installer()

    def install(self) -> Path:
        return self.tool.install(self.home, ROOT, sys.executable)

    def test_the_command_runs_the_release_on_this_interpreter(self) -> None:
        release = self.install()
        entry = self.home / ".local/bin/godfather"
        text = entry.read_text()
        self.assertTrue(text.startswith("#!" + sys.executable + "\n"))
        self.assertIn(repr(str(release)), text)
        self.assertEqual(self.home / ".local/lib/godfather/releases", release.parent)
        for name in ("app.py", "tui.py", "views/overview.py", "adapters/slotfather.py",
                     "version.json", "CHANGELOG.md"):
            self.assertTrue((release / "godfather" / name).is_file(), name)
        self.assertFalse(any(release.rglob("__pycache__")))
        result = subprocess.run([str(entry), "--version"], capture_output=True, text=True,
                                env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertRegex(result.stdout, r"^godfather 0\.1\.0 \([0-9a-f]{7}(\+dirty)?\) source [0-9a-f]{64}\n$")
        once = subprocess.run([str(entry), "--once", "--width", "80", "--height", "12"], capture_output=True,
                              text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)})
        self.assertEqual(1, once.returncode, "no tool: the frame says so, the exit code too")
        self.assertIn("No tool found", once.stdout)

    def test_a_reinstall_reuses_an_identical_release_and_replaces_the_entry_atomically(self) -> None:
        first = self.install()
        entry = self.home / ".local/bin/godfather"
        inode = entry.stat().st_ino
        replaced: list[tuple[str, str]] = []
        real = os.replace

        def spy(source: Any, target: Any) -> None:
            replaced.append((str(source), str(target)))
            real(source, target)

        with mock.patch.object(self.tool.os, "replace", spy):
            second = self.install()
        self.assertEqual(first, second, "the same bytes: the same release")
        self.assertEqual([str(entry)], [target for _, target in replaced], "only the entry point is renamed in")
        self.assertNotEqual(inode, entry.stat().st_ino, "a new file renamed over the old one, never rewritten")
        self.assertEqual([], [path for path in entry.parent.iterdir() if path.name.startswith(".godfather-")])

    def test_a_changed_release_with_the_same_identity_is_refused(self) -> None:
        release = self.install()
        (release / "godfather" / "tui.py").write_text("tampered")
        with self.assertRaisesRegex(self.tool.InstallError, "immutable installed release differs"):
            self.install()

    def test_a_running_dashboard_tells_a_newer_install(self) -> None:
        from godfather import version

        release = self.install()
        entry = self.home / ".local/bin/godfather"
        digest = (release / "godfather" / "version.json").read_text()
        self.assertIn(str(version.installed_source(str(entry))), digest)
        self.assertIsNone(version.installed_source(str(self.home / "missing")))

    def test_a_virtual_environment_interpreter_is_refused(self) -> None:
        with mock.patch.object(self.tool.sys, "prefix", "/venv"), mock.patch.object(self.tool.sys, "base_prefix", "/usr"):
            with self.assertRaisesRegex(self.tool.InstallError, "virtual environment"):
                self.tool.interpreter()

    def test_python_38_is_refused(self) -> None:
        old = self.home / "python3.8"
        old.write_text("#!/bin/sh\necho 3.8\n")
        old.chmod(0o755)
        with self.assertRaisesRegex(self.tool.InstallError, "is Python 3.8; godfather needs 3.9 or later"):
            self.tool.interpreter(str(old))

    def test_a_virtual_environment_named_with_python_is_refused_too(self) -> None:
        venv = self.home / "python3"
        venv.write_text("#!/bin/sh\necho 3.12 True\n")
        venv.chmod(0o755)
        with self.assertRaisesRegex(self.tool.InstallError, "belongs to a virtual environment"):
            self.tool.interpreter(str(venv))

    def test_a_release_path_with_a_quote_still_tells_a_newer_install(self) -> None:
        from godfather import version

        release = self.home / "it's" / "release"
        (release / "godfather").mkdir(parents=True)
        (release / "godfather" / "version.json").write_text('{"source_sha256": "abc"}')
        entry = self.home / "entry"
        entry.write_bytes(self.tool.wrapper(release, sys.executable))
        self.assertEqual("abc", version.installed_source(str(entry)))

    def test_options_are_checked(self) -> None:
        for argv in (["--bin"], ["--python", "a", "--python", "b"], ["--user", "x"]):
            with self.subTest(argv=argv), self.assertRaises(self.tool.InstallError):
                self.tool.options(argv)
        result = subprocess.run([sys.executable, str(ROOT / "install.py"), "--bin", "relative"], capture_output=True,
                                text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)})
        self.assertEqual(2, result.returncode)
        self.assertIn("--bin takes an absolute directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
