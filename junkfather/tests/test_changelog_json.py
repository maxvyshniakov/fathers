"""`junkfather changelog --json`: the installed release's CHANGELOG for Godfather's what's new (fathers#105)."""
from __future__ import annotations

import json
import re
import unittest

from support import ROOT, Case

from junkfather import __version__
from junkfather import changelog

EDGES = """# Junkfather Changelog

Versions and release rules: somewhere.

## Unreleased

### Added

- Not released yet.

## 0.10.0 - 2026-12-01

### Breaking

- `own` takes **one** command; see [the reference](docs/reference.md).
  Its old form is gone,
  and says so.

### Fixed

  not a continuation: no entry under Fixed yet
- A fix.

## 0.9.1 - 2026-11-20

## 0.2.0 - 2026-11-15

### Added

- First.
- Second.

### Notes

- not a Sid-Kit section, still an entry
"""


class ParseTests(unittest.TestCase):
    def test_unreleased_first_with_the_installed_commit_then_releases_newest_first_continuations_joined(self) -> None:
        self.assertEqual([
            {"version": None, "unreleased": True, "date": None, "source": "81abd86c0ffe", "section": "Added",
             "text": "Not released yet."},
            {"version": "0.10.0", "date": "2026-12-01", "section": "Breaking",
             "text": "`own` takes **one** command; see [the reference](docs/reference.md). Its old form is gone, "
                     "and says so."},
            {"version": "0.10.0", "date": "2026-12-01", "section": "Fixed", "text": "A fix."},
            {"version": "0.2.0", "date": "2026-11-15", "section": "Added", "text": "First."},
            {"version": "0.2.0", "date": "2026-11-15", "section": "Added", "text": "Second."},
            {"version": "0.2.0", "date": "2026-11-15", "section": "Notes",
             "text": "not a Sid-Kit section, still an entry"},
        ], changelog.entries(EDGES, commit="81abd86c0ffe"))
        self.assertIsNone(changelog.entries(EDGES)[0]["source"], "a checkout: no commit")

    def test_since_keeps_only_newer_releases_by_number_not_text(self) -> None:
        self.assertEqual({None, "0.10.0"}, {e["version"] for e in changelog.entries(EDGES, since="0.9.1")})
        self.assertEqual([None], [e["version"] for e in changelog.entries(EDGES, since="0.10.0")],
                         "Unreleased is newer than any release")
        self.assertEqual(6, len(changelog.entries(EDGES, since="0.1.9")))

    def test_nothing_to_read(self) -> None:
        self.assertEqual([], changelog.entries(""))
        self.assertEqual([], changelog.entries("# Junkfather Changelog\n\n## Unreleased\n\n- no section\n"))


class CommandTests(Case):
    def test_the_real_changelog_as_the_contract_says(self) -> None:
        result = self.cli("changelog", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual({"v", "tool", "version", "source", "entries"}, set(report))
        self.assertEqual((1, "junkfather", __version__), (report["v"], report["tool"], report["version"]))
        self.assertEqual(str((ROOT / "CHANGELOG.md").resolve()), report["source"])
        self.assertTrue(report["entries"], "the shipped releases have entries")
        unreleased = [e for e in report["entries"] if e.get("unreleased")]
        dated = report["entries"][len(unreleased):]
        self.assertTrue(unreleased, "this checkout's Unreleased section, first")
        self.assertEqual(__version__, dated[0]["version"], "newest first")
        released = re.findall(r"^## (\d\S*) - (\S+)", (ROOT / "CHANGELOG.md").read_text(), re.M)
        self.assertEqual([version for version, _ in released], list(dict.fromkeys(e["version"] for e in dated)))
        for entry in unreleased:
            self.assertEqual({"version", "unreleased", "date", "source", "section", "text"}, set(entry))
            self.assertEqual((None, None), (entry["version"], entry["date"]))
            self.assertTrue(entry["source"] is None or re.fullmatch(r"[0-9a-f]{12}", entry["source"]), entry)
        for entry in dated:
            self.assertEqual({"version", "date", "section", "text"}, set(entry))
            self.assertTrue(all(isinstance(value, str) and value for value in entry.values()), entry)

    def test_since_the_newest_release_is_empty_and_a_bad_version_is_a_usage_error(self) -> None:
        report = json.loads(self.cli("changelog", "--json", "--since", __version__).stdout)
        self.assertTrue(all(e.get("unreleased") for e in report["entries"]), "only what is not released")
        bad = self.cli("changelog", "--json", "--since", "latest")
        self.assertEqual(2, bad.returncode)
        self.assertIn("--since", bad.stderr)


if __name__ == "__main__":
    unittest.main()
