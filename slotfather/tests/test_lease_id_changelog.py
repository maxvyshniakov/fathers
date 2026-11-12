#!/usr/bin/env python3
"""A unique lease id in status, history and events (fathers#101), and `changelog --json` (fathers#103)."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
import eventcheck  # noqa: E402
from slotfather import changelog  # noqa: E402

import json
import unittest
from typing import Any

from test_slotfather import ROOT, SlotCase


class LeaseIdTests(SlotCase):
    queue_entry: Any
    def test_every_lease_has_one_id_in_status_history_and_events(self):
        first = self.request("a", 1)
        self.started("a")
        [entry] = json.loads(self.tool("status", "--json").stdout)["entries"]
        lease_id = entry["lease_id"]
        self.assertRegex(lease_id, r"\A[0-9a-f]{32}\Z")
        full = json.loads(self.tool("status", "--json", "--full").stdout)
        self.assertEqual([lease_id], [e["lease_id"] for e in full["entries"]])
        self.release("a")
        self.finish(first)
        for _ in range(2):  # one pid's leases in one second keep apart
            self.assertEqual(0, self.tool("run", "--label", "b", "--", "true").returncode)
        history: list[Any] = [json.loads(x) for x in (self.state / "history.jsonl").read_text().splitlines()]
        self.assertEqual(lease_id, history[0]["lease_id"])
        self.assertEqual(3, len({h["lease_id"] for h in history}))
        path = self.state / "events.jsonl"
        self.assertEqual([], eventcheck.validate_file(str(path), strict=True))
        events: list[Any] = [json.loads(x) for x in path.read_text().splitlines()]
        self.assertEqual({lease_id}, {e["subject"]["lease_id"] for e in events if e["subject"]["id"] == entry["id"]})

    def test_an_older_clients_entry_gets_a_stable_id(self):
        pid, start = self.holder()
        self.queue_entry(self.state, pid, start, 1, "old")
        ids = [json.loads(self.tool("status", "--json").stdout)["entries"][0]["lease_id"] for _ in range(2)]
        self.assertEqual(ids[0], ids[1])
        self.assertTrue(ids[0].startswith("old-"))
        import os
        import signal
        os.kill(pid, signal.SIGKILL)  # reaped by the next look: its history line and event carry the same id
        self.tool("status")
        [line] = [json.loads(x) for x in (self.state / "history.jsonl").read_text().splitlines()]
        self.assertEqual(ids[0], line["lease_id"])
        events = [json.loads(x) for x in (self.state / "events.jsonl").read_text().splitlines()]
        self.assertEqual([ids[0]], [e["subject"]["lease_id"] for e in events if e["kind"] == "request.reaped"
                                     or e["kind"] == "lease.reaped"])


SAMPLE = """# Tool Changelog

## Unreleased

### Added

- Not released yet.

## 0.2.0 - 2026-11-12

### Added

- One entry that
  wraps over two lines.
- Another, with a list:
  - a nested point

### Fixed

* A star bullet.

Prose after the list.

#### A small heading

- Still in 0.2.0.

## 0.1.0 - 2026-11-01

Some prose before any section.

- An entry with no section.

## Not a release

- Ignored.
"""


class ChangelogTests(unittest.TestCase):
    def test_edges(self):
        entries = changelog.parse(SAMPLE, "abc1234")
        self.assertEqual({"version": None, "date": None, "section": "Added", "text": "Not released yet.",
                          "unreleased": True, "source": "abc1234"}, entries.pop(0))
        self.assertFalse(entries[0]["unreleased"])
        self.assertNotIn("source", entries[0])
        self.assertEqual([("0.2.0", "2026-11-12", "Added", "One entry that wraps over two lines."),
                          ("0.2.0", "2026-11-12", "Added", "Another, with a list: - a nested point"),
                          ("0.2.0", "2026-11-12", "Fixed", "A star bullet."),
                          ("0.2.0", "2026-11-12", "Fixed", "Still in 0.2.0."),
                          ("0.1.0", "2026-11-01", None, "An entry with no section.")],
                         [(e["version"], e["date"], e["section"], e["text"]) for e in entries])
        self.assertEqual([], changelog.parse(""))

    def test_the_packages_own_changelog(self):
        path = str(ROOT / "CHANGELOG.md")
        found = changelog.changelog(path=path)
        self.assertEqual(("slotfather", path), (found["tool"], found["source"]))
        unreleased = [e for e in found["entries"] if e["unreleased"]]
        self.assertTrue(unreleased, "this checkout has Unreleased entries")
        self.assertEqual(unreleased, found["entries"][:len(unreleased)], "they come first")
        versions = [e["version"] for e in found["entries"] if not e["unreleased"]]
        self.assertTrue(versions)
        self.assertEqual(versions, sorted(versions, key=changelog.version_key, reverse=True), "newest first")
        self.assertTrue(all(e["section"] in ("Breaking", "Added", "Changed", "Deprecated", "Removed", "Fixed",
                                             "Security") for e in found["entries"]))
        since = {e["version"] for e in changelog.changelog("0.0.6", path=path)["entries"] if not e["unreleased"]}
        self.assertIn("0.1.0", since)
        self.assertTrue(all(changelog.version_key(v) > (0, 0, 6) for v in since))
        self.assertEqual([], changelog.changelog(path=str(ROOT / "missing.md"))["entries"])


class ChangelogCommandTests(SlotCase):
    def test_the_command(self):
        result = self.tool("changelog", "--json", "--since", "0.0.1")
        self.assertEqual(0, result.returncode, result.stderr)
        found = json.loads(result.stdout)
        self.assertEqual(1, found["v"])
        self.assertNotIn("0.0.1", {e["version"] for e in found["entries"]})
        self.assertTrue(any(e["unreleased"] and e["source"] for e in found["entries"]), "the installed commit")
        self.assertEqual(2, self.tool("changelog").returncode)
        self.assertEqual(2, self.tool("changelog", "--json", "--since", "x").returncode)


if __name__ == "__main__":
    unittest.main()
