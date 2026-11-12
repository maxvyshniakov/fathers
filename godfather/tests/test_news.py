"""What's new (fathers#102): each tool's `changelog --json`, the version fallbacks, one timeline and the read state,
from fixture replies (docs/contracts/changelog.md)."""
from __future__ import annotations

import helpers  # noqa: F401  # pyright: ignore[reportUnusedImport] - src/ on sys.path

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from godfather import news

OWN = """# Godfather Changelog

## Unreleased

### Fixed

- Not out yet (fathers#17).

## 0.1.0 - 2026-11-12

### Added

- `godfather` opens
  the dashboard.
"""


def changelog(name: str, version: str, rows: list[tuple[str, str, str, str]], **extra: Any) -> dict[str, Any]:
    return {"code": 0, "json": dict({"v": 1, "tool": name, "version": version, "source": f"/x/{name}/CHANGELOG.md",
                                     "entries": [dict(zip(("version", "date", "section", "text"), row))
                                                 for row in rows]}, **extra)}


class Fixtures(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="godfather-news-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(self.root),
                                               "XDG_STATE_HOME": str(self.root / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)


class ToolTests(Fixtures):
    def reply(self, name: str, replies: dict[str, Any]) -> None:
        (self.root / name).mkdir()
        (self.root / name / "replies.json").write_text(json.dumps(replies))

    def test_a_tool_with_the_contract_gives_its_version_and_entries_with_markdown_out(self) -> None:
        self.reply("junkfather", {"changelog --json": changelog("junkfather", "0.1.0", [
            ("0.1.0", "2026-11-12", "Added", "`junkfather own` **starts** [it](x.md)"),
            ("0.1.0", "2026-11-12", "Added", ""),  # skipped: an empty field
        ])})
        row, items = news.tool("junkfather")
        self.assertEqual(news.Installed("junkfather", "0.1.0", "ok"), row)
        self.assertEqual([news.Item("junkfather", "0.1.0", "2026-11-12", "Added", "junkfather own starts it")], items)

    def test_without_the_command_the_version_comes_from_version_then_capabilities(self) -> None:
        self.reply("slotfather", {"version --json": {"code": 0, "json": {"v": 1, "version": "0.2.0"}}})
        self.reply("lanefather", {"capabilities --json": {"code": 0, "json": {"v": 1, "version": "0.3.0"}}})
        for name, version in (("slotfather", "0.2.0"), ("lanefather", "0.3.0")):
            row, items = news.tool(name)
            self.assertEqual((name, version, "versions", []), (row.tool, row.version, row.state, items))

    def test_absent_failed_and_replies_that_are_not_the_contract(self) -> None:
        self.assertEqual(news.Installed("lanefather", None, "absent"), news.tool("lanefather")[0])
        self.reply("lanefather", {})
        self.assertEqual("failed", news.tool("lanefather")[0].state)
        for bad in (dict(v=2), dict(tool="slotfather"), dict(entries={})):
            with self.subTest(bad=bad):
                self.assertIsNone(news.from_reply("junkfather", changelog("junkfather", "0.1.0", [], **bad)["json"]))
        self.assertIsNotNone(news.from_reply("junkfather", {"tool": "junkfather", "version": "0.1.0", "entries": []}),
                             "no v reads as 1")


class TimelineTests(Fixtures):
    def test_one_timeline_newest_first_godfather_own_unreleased_apart(self) -> None:
        (self.root / "slotfather").mkdir()
        (self.root / "slotfather" / "replies.json").write_text(json.dumps({"changelog --json": changelog(
            "slotfather", "0.2.0", [("0.2.0", "2026-11-12", "Added", "s2"), ("0.1.0", "2026-11-01", "Added", "s1")])}))
        (self.root / "junkfather").mkdir()
        (self.root / "junkfather" / "replies.json").write_text(json.dumps({"changelog --json": changelog(
            "junkfather", "0.1.0", [("0.1.0", "2026-11-12", "Added", "j1")])}))
        found = news.load(OWN, stale=True)
        self.assertEqual(["godfather", "slotfather", "lanefather", "junkfather"], [r.tool for r in found.installed])
        self.assertEqual(["stale", "ok", "absent", "ok"], [r.state for r in found.installed])
        self.assertEqual([("godfather", "0.1.0", "godfather opens the dashboard."), ("slotfather", "0.2.0", "s2"),
                          ("junkfather", "0.1.0", "j1"), ("slotfather", "0.1.0", "s1")],
                         [(i.tool, i.version, i.text) for i in found.items], "by date, a date's tools in order")
        self.assertEqual([news.Item("godfather", "", "", "Fixed", "Not out yet (fathers#17).")], list(found.unreleased))


class ReadTests(Fixtures):
    NEWS = news.News((), (news.Item("slotfather", "0.2.0", "d", "Added", "a"),
                          news.Item("slotfather", "0.10.0", "d", "Added", "b"),
                          news.Item("junkfather", "0.1.0", "d", "Added", "c")))

    def test_unread_is_newer_than_the_tools_read_version_all_without_one(self) -> None:
        self.assertEqual(3, len(news.unread(self.NEWS, {})))
        self.assertEqual(["b", "c"], [i.text for i in news.unread(self.NEWS, {"slotfather": "0.9.0"})])
        self.assertEqual([], news.unread(None, {}))

    def test_marking_read_takes_each_tools_newest_and_keeps_the_rest(self) -> None:
        marked = news.mark_read(self.NEWS, {"lanefather": "0.3.0"})
        self.assertEqual({"slotfather": "0.10.0", "junkfather": "0.1.0", "lanefather": "0.3.0"}, marked)
        self.assertEqual([], news.unread(self.NEWS, marked))

    def test_the_read_state_round_trips_in_godfathers_state_dir(self) -> None:
        self.assertEqual({}, news.load_read())
        news.save_read({"godfather": "0.1.0"})
        self.assertEqual(self.root / "state" / "godfather" / "news.json", news.state_path())
        self.assertEqual({"godfather": "0.1.0"}, news.load_read())
        news.state_path().write_text("not json")
        self.assertEqual({}, news.load_read())


if __name__ == "__main__":
    unittest.main()
