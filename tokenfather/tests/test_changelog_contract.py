"""`tokenfather changelog --json` against Godfather's changelog contract (godfather/docs/contracts/changelog.md).

The fixtures Godfather's cross-tool contract test runs against Slotfather, Lanefather and Junkfather
(godfather/tests/fixtures/changelog/) are run against Tokenfather's reply, through `fathers_common.changelog`
(fathers#119, fathers#208), and the CLI is checked from the checkout and from an installed release layout.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, cast
import unittest

from tokenfather import __version__, changelog

PACKAGE = Path(__file__).resolve().parents[1]
FIXTURES = PACKAGE.parent / "godfather" / "tests" / "fixtures" / "changelog"
KEYS = {"v", "tool", "version", "source", "entries"}


def run(*args: str, path: Path = PACKAGE, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("TOKENFATHER_")}
    environment.update(PYTHONPATH=str(path), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-m", "tokenfather", *args], capture_output=True, text=True,
                          env=environment, cwd=str(cwd or path))


@unittest.skipUnless(FIXTURES.is_dir(), "Godfather's changelog fixtures are not beside this package")
class SharedFixtureTests(unittest.TestCase):
    expected: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = cast(dict[str, Any], json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8")))

    def reply(self, since: str | None, path: Path, commit: str | None) -> dict[str, Any]:
        found = changelog.contract(since, path, commit)
        self.assertEqual(KEYS, set(found))
        self.assertEqual((1, "tokenfather", __version__), (found["v"], found["tool"], found["version"]))
        return found

    def test_the_full_fixture_gives_the_contract_entries_installed_and_from_a_checkout(self) -> None:
        fixture = (FIXTURES / "full.md").resolve()
        for commit in ("81abd86c0ffe", None):
            with self.subTest(commit=commit):
                wanted = [
                    {**entry, "source": commit} if entry.get("unreleased") else dict(entry)
                    for entry in cast(list[dict[str, Any]], self.expected["full_installed"])
                ]
                found = self.reply(None, fixture, commit)
                self.assertEqual(str(fixture), found["source"])
                self.assertEqual(wanted, found["entries"])

    def test_since_keeps_unreleased_and_newer_releases_for_numbers_and_build_headings(self) -> None:
        fixture = (FIXTURES / "full.md").resolve()
        cases = (
            ("0.2.0", self.expected["since_0_2_0"]),
            ("0.1.9", self.expected["since_0_1_9"]),
            ("0.1.0", self.expected["since_0_1_0"]),
            ("0.1.0+7eca44a", self.expected["since_0_1_0_build"]),
            ("99.0.0", [None, None, None]),
        )
        for since, versions in cases:
            with self.subTest(since=since):
                found = self.reply(since, fixture, "81abd86c0ffe")
                self.assertEqual(versions, [entry["version"] for entry in found["entries"]])

    def test_released_only_empty_missing_and_invalid_since(self) -> None:
        released = (FIXTURES / "released-only.md").resolve()
        self.assertEqual(self.expected["released_only"], self.reply(None, released, "81abd86c0ffe")["entries"])
        empty = (FIXTURES / "empty.md").resolve()
        empty_reply = self.reply(None, empty, None)
        self.assertEqual((str(empty), []), (empty_reply["source"], empty_reply["entries"]))
        missing = self.reply(None, FIXTURES / "does-not-exist.md", None)
        self.assertEqual((None, []), (missing["source"], missing["entries"]))
        for bad in ("latest", "x", "0.1", ""):
            with self.subTest(since=bad), self.assertRaisesRegex(ValueError, "--since"):
                changelog.contract(bad, released, None)


class CommandTests(unittest.TestCase):
    def test_json_from_the_checkout_names_the_tool_and_its_own_changelog(self) -> None:
        result = run("changelog", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        found = json.loads(result.stdout)
        self.assertEqual(KEYS, set(found))
        self.assertEqual((1, "tokenfather", __version__), (found["v"], found["tool"], found["version"]))
        self.assertEqual(os.path.realpath(PACKAGE / "CHANGELOG.md"), found["source"])
        self.assertTrue(found["entries"])
        for entry in found["entries"]:
            self.assertIsInstance(entry["section"], str)
            self.assertIsInstance(entry["text"], str)
            if entry.get("unreleased"):
                self.assertEqual((None, None, None), (entry["version"], entry["date"], entry["source"]))
            else:
                self.assertRegex(entry["date"], r"^\d{4}-\d\d-\d\d$")
                self.assertNotIn("source", entry)

    def test_an_invalid_since_is_a_usage_error(self) -> None:
        result = run("changelog", "--json", "--since", "latest")
        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("--since", result.stderr)

    def test_an_installed_release_gives_its_commit_as_the_unreleased_source(self) -> None:
        release = Path(tempfile.mkdtemp(prefix="tokenfather-release-"))
        self.addCleanup(shutil.rmtree, release, True)
        shutil.copytree(PACKAGE / "tokenfather", release / "tokenfather",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(PACKAGE / "fathers_common", release / "fathers_common",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (release / "tokenfather" / "CHANGELOG.md").write_text(
            "# Tokenfather Changelog\n\n## Unreleased\n\n### Fixed\n\n- Installed news.\n\n"
            "## 0.1.0 - 2026-11-21\n\n### Added\n\n- First.\n",
            encoding="utf-8",
        )
        (release / "tokenfather" / "COMMIT").write_text("81abd86c0ffee0123456789abcdef0123456789a\n", encoding="utf-8")
        result = run("changelog", "--json", path=release)
        self.assertEqual(0, result.returncode, result.stderr)
        found = json.loads(result.stdout)
        self.assertEqual(os.path.realpath(release / "tokenfather" / "CHANGELOG.md"), found["source"])
        self.assertEqual(
            [
                {"version": None, "unreleased": True, "date": None, "source": "81abd86c0ffe", "section": "Fixed",
                 "text": "Installed news."},
                {"version": "0.1.0", "date": "2026-11-21", "section": "Added", "text": "First."},
            ],
            found["entries"],
        )
        text = run("changelog", "--since", "0.1.0", path=release)
        self.assertEqual(0, text.returncode, text.stderr)
        self.assertEqual("Unreleased\n  Fixed: Installed news.\n", text.stdout)


if __name__ == "__main__":
    unittest.main()
