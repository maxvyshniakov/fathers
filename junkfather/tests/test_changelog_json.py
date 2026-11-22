"""`junkfather changelog --json`: the installed release's CHANGELOG for Godfather's what's new (fathers#105).

Shared Markdown parsing (`fathers_common.changelog`) is covered by `common/tests/test_kit.py`.
"""
from __future__ import annotations

import json
import re
import unittest

from support import ROOT, Case

from junkfather import __version__


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
