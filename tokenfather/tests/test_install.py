"""install.py on `fathers_common.installer`: committed inputs, immutable releases, smoke check, rollback.

fathers#208, fathers#117.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

import install  # noqa: E402

HEAD = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tokenfather-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.share = self.tmp / "share"
        self.bin = self.tmp / "bin"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("TOKENFATHER_")}
        self.env.update(HOME=str(self.tmp / "home"), TOKENFATHER_STATE=str(self.tmp / "state"),
                        TOKENFATHER_EVENTS=str(self.tmp / "events.jsonl"), PYTHONDONTWRITEBYTECODE="1")

    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.bin / "tokenfather"), *args], capture_output=True, text=True, env=self.env,
                              cwd=self.tmp)


class InstallFromThisCheckoutTests(Case):
    def test_a_committed_checkout_installs_and_uncommitted_shipping_inputs_are_refused(self) -> None:
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", *install.SHIPPING],
                               cwd=PACKAGE, capture_output=True, text=True).stdout.strip()
        result = subprocess.run([sys.executable, str(PACKAGE / "install.py"), "--bin", str(self.bin),
                                 "--share", str(self.share)], capture_output=True, text=True, env=self.env)
        if dirty:
            self.assertEqual(2, result.returncode, "uncommitted shipping inputs are refused")
            self.assertRegex(result.stderr, "uncommitted|committed Git checkout")
            self.assertFalse((self.bin / "tokenfather").exists(), "nothing was changed")
            return
        self.assertEqual(0, result.returncode, result.stderr)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PACKAGE, capture_output=True, text=True).stdout
        self.assertIn(f"({head.strip()})", self.command("--version").stdout)
        found = json.loads(self.command("changelog", "--json").stdout)
        self.assertEqual("tokenfather", found["tool"])


class InstallTests(Case):
    """install() with the Git check replaced by a fixed commit, so it runs the same on a dirty working tree."""

    def install(self, commit: str = HEAD) -> Path:
        with mock.patch.object(install, "commit", return_value=commit):
            return install.install(self.share, self.bin, sys.executable)

    def test_the_release_is_complete_immutable_and_the_command_runs_it(self) -> None:
        release = self.install()
        self.assertEqual(self.share / "releases" / f"{install.version()}-{HEAD[:12]}", release)
        current = self.share / "current"
        self.assertTrue(current.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(current)), "current points inside share, relatively")
        self.assertEqual(release.resolve(), current.resolve())
        for name in ("tokenfather/cli.py", "tokenfather/CHANGELOG.md", "fathers_common/changelog.py",
                     "fathers_common/installer.py"):
            self.assertTrue((release / name).is_file(), name)
        self.assertFalse((release / "fathers_common").is_symlink(), "fathers_common is copied, not linked")
        self.assertEqual(HEAD + "\n", (release / "tokenfather" / "COMMIT").read_text(encoding="utf-8"))
        self.assertFalse(list(release.rglob("__pycache__")))
        self.assertEqual(0o755, (self.bin / "tokenfather").stat().st_mode & 0o777)
        self.assertIn(f"({HEAD})", self.command("--version").stdout)
        reply: dict[str, Any] = json.loads(self.command("changelog", "--json").stdout)
        self.assertEqual(os.path.realpath(release / "tokenfather" / "CHANGELOG.md"), reply["source"])
        self.assertEqual(release, self.install(), "the same commit again reuses the identical release")
        self.assertEqual([release.name], [p.name for p in (self.share / "releases").iterdir()])

    def test_an_existing_release_that_differs_from_the_source_is_refused(self) -> None:
        release = self.install()
        (release / "tokenfather" / "cli.py").write_text("# tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(install.InstallError, "differs"):
            self.install()

    def test_a_failed_smoke_check_changes_nothing(self) -> None:
        with mock.patch.object(install, "smoke_check", side_effect=install.InstallError("smoke check failed")), \
                self.assertRaisesRegex(install.InstallError, "smoke"):
            self.install()
        self.assertFalse((self.share / "current").exists())
        self.assertFalse((self.bin / "tokenfather").exists())

    def test_the_smoke_check_runs_the_staged_release(self) -> None:
        broken = self.tmp / "broken"
        (broken / "tokenfather").mkdir(parents=True)
        (broken / "tokenfather" / "__init__.py").write_text("", encoding="utf-8")
        (broken / "tokenfather" / "__main__.py").write_text("raise SystemExit('cannot start')\n", encoding="utf-8")
        with self.assertRaisesRegex(install.InstallError, "smoke check failed for broken .*cannot start"):
            install.smoke_check(broken, sys.executable)

    def test_a_failed_launcher_write_switches_current_back(self) -> None:
        first = self.install()
        before = (self.bin / "tokenfather").read_bytes()
        with mock.patch.object(install, "write_launcher", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            self.install(OTHER)
        self.assertEqual(first.resolve(), (self.share / "current").resolve())
        self.assertEqual(before, (self.bin / "tokenfather").read_bytes())

    def test_only_the_newest_releases_stay(self) -> None:
        installed: list[Path] = []
        for index in range(1, 6):
            installed.append(self.install(f"{index:x}" * 40))
            os.utime(installed[-1], (1_000_000 + index, 1_000_000 + index))
        kept = sorted(p.name for p in (self.share / "releases").iterdir())
        self.assertEqual(sorted(p.name for p in installed[-install.KEEP_RELEASES:]), kept)

    def test_a_second_install_waits_for_the_lock_and_gives_up_without_changes(self) -> None:
        from fathers_common.installer import install_lock

        def impatient(path: Path, label: str = "install") -> AbstractContextManager[None]:
            return install_lock(path, 0, label=label)

        with install_lock(self.share / ".install.lock"), \
                mock.patch.object(install, "install_lock", side_effect=impatient), \
                self.assertRaisesRegex(install.InstallError, "in progress"):
            self.install()
        self.assertFalse((self.share / "current").exists())


class InterpreterTests(unittest.TestCase):
    def test_this_python_is_accepted_and_a_missing_one_refused(self) -> None:
        self.assertTrue(os.path.isabs(install.interpreter(None)))
        with self.assertRaises(install.InstallError):
            install.interpreter("/nonexistent/python3")


if __name__ == "__main__":
    unittest.main()
