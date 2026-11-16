"""The typed snapshot model (fathers#142): each adapter checks a snapshot once, when it decodes it. Unknown keys are
ignored; a missing required key is the source's notice, never an exception at draw time."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from helpers import ROOT

from godfather import model
from godfather.adapters import junkfather, lanefather, slotfather

SCENE = ROOT / "tests" / "fixtures" / "scene"


def scene_status() -> dict[str, Any]:
    replies = json.loads((SCENE / "slotfather" / "replies.json").read_text(encoding="utf-8"))
    return replies["status --json --full"]["json"]


class SlotfatherTests(unittest.TestCase):
    def test_a_request_without_a_required_key_is_the_notice(self) -> None:
        for key in model.REQUEST_REQUIRED:
            with self.subTest(key=key):
                found = copy.deepcopy(scene_status())
                del found["dashboard"]["holders"][0][key]
                with self.assertRaisesRegex(slotfather.Unsupported, f"a request has no {key}"):
                    slotfather.decode(found)

    def test_a_dashboard_without_a_required_key_is_the_notice(self) -> None:
        for key in model.DASHBOARD_REQUIRED:
            with self.subTest(key=key):
                found = copy.deepcopy(scene_status())
                del found["dashboard"][key]
                with self.assertRaisesRegex(slotfather.Unsupported, f"dashboard.{key}"):
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
    def test_a_lane_or_entry_missing_its_id_is_left_out_and_other_gaps_read_as_empty(self) -> None:
        lanes = lanefather.decode({"v": 1, "projects": [{"state_dir": "/s", "lanes": [{"status": "active"},
                                                                                      {"id": "a", "x": 1}]}]})
        [lane] = lanefather.lanes(lanes)
        self.assertEqual(("a", "?", [], None), (lane["id"], lane["status"], lane["claims"], lane["worktree"]))
        procs = junkfather.decode({"v": 2, "entries": [{"label": "no id"}, {"id": "j1"}]})
        [entry] = junkfather.entries(procs)
        self.assertEqual(("j1", "running", []), (entry["id"], entry["state"], entry["argv"]))


if __name__ == "__main__":
    unittest.main()
