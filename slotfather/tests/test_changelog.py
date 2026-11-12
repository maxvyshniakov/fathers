"""CHANGELOG.md keeps the Sid-Kit layout and its newest release is the version (until repo-kit checks it)."""
from __future__ import annotations

import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import re
import unittest

from slotfather import version

ROOT = _Path(__file__).resolve().parents[1]
SECTIONS = ["Breaking", "Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]


class ChangelogLayoutTests(unittest.TestCase):
    def test_title_unreleased_dated_releases_newest_first_and_ordered_sections(self) -> None:
        lines = (ROOT / "CHANGELOG.md").read_text().splitlines()
        self.assertEqual("# Slotfather Changelog", lines[0])
        self.assertEqual(1, sum(line.startswith("# ") for line in lines))
        headings = [line for line in lines if line.startswith("## ")]
        self.assertEqual("## Unreleased", headings[0])
        self.assertEqual(1, headings.count("## Unreleased"))
        releases: list[tuple[tuple[int, ...], str]] = []
        for heading in headings[1:]:
            found = re.fullmatch(r"## (\d+)\.(\d+)\.(\d+) - (\d{4}-\d\d-\d\d)", heading)
            self.assertIsNotNone(found, heading)
            assert found
            releases.append((tuple(int(part) for part in found.groups()[:3]), found[4]))
        self.assertEqual(sorted(releases, reverse=True), releases, "newest first, dates too")
        section: list[str] = []
        for line in lines + ["## end"]:
            if line.startswith("## "):
                self.assertEqual(sorted(section, key=SECTIONS.index), section, f"section order before {line}")
                section = []
            elif line.startswith("### "):
                self.assertIn(line[4:], SECTIONS)
                section.append(line[4:])
        for index, line in enumerate(lines):
            if line.startswith("### "):
                self.assertEqual("", lines[index + 1], f"a blank line after {line}")
                self.assertTrue(lines[index + 2].startswith("- "), f"{line} has entries")

    def test_the_newest_release_is_the_version_everywhere(self) -> None:
        newest = next(line for line in (ROOT / "CHANGELOG.md").read_text().splitlines()
                      if re.match(r"## \d", line)).split()[1]
        project = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
        assert project
        self.assertEqual(newest, project[1], "pyproject.toml")
        self.assertEqual(newest, version.VERSION, "src/slotfather/version.py")


if __name__ == "__main__":
    unittest.main()
