"""`lanefather changelog --json` (fathers#104): the release's CHANGELOG.md as entries for Godfather's what's new, and
the layout that keeps it readable."""
from __future__ import annotations

import json
import re
import unittest

from support import ROOT, LaneCase

SECTIONS = ["Breaking", "Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]
FIXTURE = """# Tool Changelog

Intro text that is no entry.

- nor is this bullet

## Unreleased

### Added

- the next thing,
  on two lines

## 0.2.0 - 2026-11-20

### Fixed

- a fix

Loose text ends the bullet.
  So this indented line is no continuation.

### Unknown heading

- kept under its heading

## 0.1.0+abc1234 - 2026-11-11

### Added

- a pinned build

## Notes

- not a release
"""


class ChangelogParseTests(unittest.TestCase):
    def test_the_fixture_gives_unreleased_first_then_each_release_newest_first(self):
        from lanefather import changelog
        found = changelog.entries(FIXTURE, commit="c" * 40)
        self.assertEqual([(e["version"], e["date"], e["section"], e["text"]) for e in found], [
            (None, None, "Added", "the next thing, on two lines"),
            ("0.2.0", "2026-11-20", "Fixed", "a fix"),
            ("0.2.0", "2026-11-20", "Unknown heading", "kept under its heading"),
            ("0.1.0+abc1234", "2026-11-11", "Added", "a pinned build"),
        ])
        self.assertEqual((found[0]["unreleased"], found[0]["source"]), (True, "c" * 40))
        self.assertNotIn("unreleased", found[1])

    def test_since_keeps_newer_releases_and_unreleased(self):
        from lanefather import changelog
        found = changelog.entries(FIXTURE, since="0.1.0")
        self.assertEqual([e["version"] for e in found], [None, "0.2.0", "0.2.0"])
        self.assertEqual([e["version"] for e in changelog.entries(FIXTURE, since="0.2.0")], [None])

    def test_no_text_gives_no_entries(self):
        from lanefather import changelog
        self.assertEqual(changelog.entries(""), [])

    def test_the_packages_changelog_parses_and_keeps_its_layout(self):
        from lanefather import changelog
        text = (ROOT / "CHANGELOG.md").read_text()
        found = changelog.entries(text)
        releases = [e["version"] for e in found if e["version"]]
        self.assertIn("0.1.0", releases)
        self.assertTrue(all(e["text"] and e["section"] in SECTIONS for e in found))
        lines = text.splitlines()
        self.assertEqual(lines[0], "# Lanefather Changelog")
        headings = [line for line in lines if line.startswith("## ")]
        self.assertEqual(headings[0], "## Unreleased")
        dates = [re.fullmatch(r"## \d+\.\d+\.\d+\S* - (\d{4}-\d\d-\d\d)", h) for h in headings[1:]]
        self.assertTrue(all(dates), headings)
        self.assertEqual([d[1] for d in dates if d], sorted((d[1] for d in dates if d), reverse=True))
        section: list[str] = []
        for line in [*lines, "## end"]:
            if line.startswith("## "):
                self.assertEqual(sorted(section, key=SECTIONS.index), section, f"section order before {line}")
                section = []
            elif line.startswith("### "):
                section.append(line[4:])


class ChangelogCommandTests(LaneCase):
    def test_the_command_answers_the_contract_and_refuses_a_bad_since(self):
        from lanefather import __version__
        answer = json.loads(self.lane("changelog", "--json"))
        self.assertEqual((answer["v"], answer["tool"], answer["version"]), (1, "lanefather", __version__))
        self.assertEqual(answer["source"], str((ROOT / "CHANGELOG.md").resolve()))
        self.assertTrue(answer["entries"])
        self.assertIn("--since takes a version", self.lane("changelog", "--json", "--since", "later", ok=False))
        self.assertEqual(json.loads(self.lane("changelog", "--json", "--since", "99.0.0"))["entries"],
                         [e for e in answer["entries"] if e.get("unreleased")])


if __name__ == "__main__":
    unittest.main()
