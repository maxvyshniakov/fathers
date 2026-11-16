"""The typed snapshot model (fathers#142): each adapter checks a snapshot once, when it decodes it. Unknown keys are
ignored; a missing required key is the source's notice, never an exception at draw time."""
from __future__ import annotations

import copy
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from helpers import ROOT

from godfather import sources
from godfather.adapters import junkfather, lanefather, slotfather

SCENE = ROOT / "tests" / "fixtures" / "scene"
# What the views read without a default, listed here rather than taken from the adapter, so dropping a key from the
# adapter's checks fails this test (docs/contracts/slotfather.md, What the adapter requires)
REQUIRED_REQUEST = ("kind", "count", "pid", "procs", "yields", "rss")
REQUIRED_DASHBOARD = ("slots", "cores", "used", "free", "advice", "memory", "overtake_window")


def scene_status() -> dict[str, Any]:
    replies = json.loads((SCENE / "slotfather" / "replies.json").read_text(encoding="utf-8"))
    return replies["status --json --full"]["json"]


class SlotfatherTests(unittest.TestCase):
    def test_a_request_without_a_required_key_is_the_notice(self) -> None:
        for key in REQUIRED_REQUEST:
            with self.subTest(key=key):
                found = copy.deepcopy(scene_status())
                del found["dashboard"]["holders"][0][key]
                with self.assertRaisesRegex(slotfather.Unsupported, f"a request has no {key}"):
                    slotfather.decode(found)

    def test_a_dashboard_without_a_required_key_is_the_notice(self) -> None:
        for key in REQUIRED_DASHBOARD:
            with self.subTest(key=key):
                found = copy.deepcopy(scene_status())
                del found["dashboard"][key]
                with self.assertRaisesRegex(slotfather.Unsupported, f"dashboard.{key}"):
                    slotfather.decode(found)

    def test_advice_without_the_unleased_load_machine_shows_is_the_notice(self) -> None:
        for advice in ({}, {"unleased_load": None}, {"unleased_load": "high"}):
            with self.subTest(advice=advice):
                found = copy.deepcopy(scene_status())
                found["dashboard"]["advice"] = advice
                with self.assertRaisesRegex(slotfather.Unsupported, "dashboard.advice.unleased_load"):
                    slotfather.decode(found)

    def test_a_process_row_is_numbers_and_a_name_or_the_notice(self) -> None:
        found = copy.deepcopy(scene_status())
        holder = found["dashboard"]["holders"][0]
        holder["tree"] = [[holder["pid"], 12, 2048.0, "python3"]]
        self.assertEqual([(holder["pid"], 12.0, 2048, "python3")], slotfather.decode(found)["holders"][0]["tree"])
        for row in ([1, "busy", 2048, "x"], [1, 1.0, None, "x"], ["1", 1.0, 2048, "x"], [1, 1.0, 2048, 7], [1, 1.0]):
            with self.subTest(row=row):
                holder["tree"] = [row]
                with self.assertRaisesRegex(slotfather.Unsupported, "process tree"):
                    slotfather.decode(found)

    def test_unknown_keys_are_ignored_and_optional_ones_take_their_default(self) -> None:
        found = copy.deepcopy(scene_status())
        holder = found["dashboard"]["holders"][0]
        holder["a_later_field"] = {"anything": 1}
        for key in ("priority", "ticket", "yieldable", "workdir", "context"):
            holder.pop(key, None)
        decoded = slotfather.decode(found)["holders"][0]
        self.assertEqual(("normal", None, False, "", {}), (decoded["priority"], decoded.get("ticket"),
                                                           decoded.get("yieldable", False), decoded["workdir"],
                                                           decoded["context"]))

    def test_once_says_why_instead_of_failing(self) -> None:
        """The fixture with a request missing its pid: `--once` prints the frame with the reason, no traceback."""
        with tempfile.TemporaryDirectory() as scratch:
            fixtures = Path(scratch) / "scene"
            shutil.copytree(SCENE, fixtures)
            path = fixtures / "slotfather" / "replies.json"
            replies = json.loads(path.read_text(encoding="utf-8"))
            del replies["status --json --full"]["json"]["dashboard"]["holders"][0]["pid"]
            path.write_text(json.dumps(replies), encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if not key.startswith(("GODFATHER_", "SLOTFATHER_"))}
            done = subprocess.run([sys.executable, str(ROOT / "godfather"), "--once", "--width", "120", "--height",
                                   "30"], env=dict(env, GODFATHER_FIXTURES=str(fixtures)), capture_output=True,
                                  text=True, timeout=60)
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn("a request has no pid", done.stdout)
        self.assertEqual(1, done.returncode)


class OtherToolTests(unittest.TestCase):
    def test_a_row_without_the_field_that_names_it_is_the_notice(self) -> None:
        """Never dropped quietly: a producer that renamed the field would make work vanish from a healthy source."""
        cases: list[tuple[Any, dict[str, Any], str]] = [(lanefather, {"v": 1, "projects": [{"state_dir": "/s", "lanes": [{"status": "active"}]}]},
                  "a lane has no id; update lanefather"),
                 (lanefather, {"v": 1, "projects": [{"project": "p", "lanes": []}]},
                  "a project has no state_dir; update lanefather"),
                 (junkfather, {"v": 2, "entries": [{"label": "no id"}]}, "an entry has no id; update junkfather"),
                 (junkfather, {"v": 2, "entries": [], "unowned": [{"cmd": "x"}]},
                  "an unowned process has no pid; update junkfather"),
                 (junkfather, {"v": 2, "entries": [], "unowned_resources": [{"kind": "worktree"}]},
                  "an unowned resource has no kind or id; update junkfather")]
        for adapter, snapshot, notice in cases:
            with self.subTest(notice=notice), self.assertRaisesRegex(sources.SnapshotError, re.escape(notice)):
                adapter.decode(snapshot)

    def test_other_gaps_read_as_empty_and_unknown_keys_are_ignored(self) -> None:
        lanes = lanefather.decode({"v": 1, "projects": [{"state_dir": "/s", "lanes": [{"id": "a", "x": 1}]}]})
        [lane] = lanefather.lanes(lanes)
        self.assertEqual(("a", "?", [], None), (lane["id"], lane["status"], lane["claims"], lane["worktree"]))
        procs = junkfather.decode({"v": 2, "entries": [{"id": "j1", "later": True}]})
        [entry] = junkfather.entries(procs)
        self.assertEqual(("j1", "running", []), (entry["id"], entry["state"], entry["argv"]))


if __name__ == "__main__":
    unittest.main()
