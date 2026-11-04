"""Live lanes on older branches (#648, must-not-break 1): each lane runs the tools/lane of its own branch, so the old
tool and lanefather share one state directory for a while. Lanes started, checked and landed by either are read and
finished by the other, through the same records, receipts, queue and landing lock.

The old tool is Straylight's tools/lane from before the move (tests/fixtures/legacy-lane, PROVENANCE.json), run with
its STRAYLIGHT_LANE_* settings beside lanefather's LANEFATHER_* ones. With LANEFATHER_LEGACY_TOOLS=<project>/tools the
tests run that project's current tools/lane instead, as Straylight's tooling suite does.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import unittest

from support import FIXTURES, LaneCase

# lanefather's settings and the names the old tool reads them by.
LEGACY_NAMES = {"LANEFATHER_LOAD_THROTTLE": "STRAYLIGHT_LOAD_WAIT", "LANEFATHER_BUILD_CACHE": "STRAYLIGHT_BUILD_CACHE"}


class LegacyCase(LaneCase):
    def setUp(self):
        super().setUp()
        tools = self.tmp / "legacy" / "tools"
        (tools / "agent-hooks").mkdir(parents=True)
        # LANEFATHER_LEGACY_TOOLS names a project's live tools/ directory: Straylight's tooling suite runs these tests
        # against its current tools/lane, so a change to its state format fails there (review of #648, finding 1).
        live = os.environ.get("LANEFATHER_LEGACY_TOOLS")
        sources = ((pathlib.Path(live) / "lane", pathlib.Path(live) / "heavy.py",
                    pathlib.Path(live) / "agent-hooks" / "process_scan.py") if live
                   else (FIXTURES / "legacy-lane", FIXTURES / "legacy-heavy.py", FIXTURES / "process_scan.py"))
        for source, target in zip(sources, ("lane", "heavy.py", "agent-hooks/process_scan.py"), strict=True):
            shutil.copy2(source, tools / target)
        self.legacy_tool = tools / "lane"

    def legacy_env(self) -> dict[str, str]:
        env = dict(self.env)
        for key, value in self.env.items():
            if key in LEGACY_NAMES:
                env[LEGACY_NAMES[key]] = value
            elif key.startswith("LANEFATHER_"):
                env["STRAYLIGHT_LANE_" + key[len("LANEFATHER_"):]] = value
        return {k: v for k, v in env.items() if not k.startswith("LANEFATHER_")}

    def legacy(self, *args: str, cwd: pathlib.Path | None = None, ok: bool = True) -> str:
        proc = subprocess.run([sys.executable, str(self.legacy_tool), *args], cwd=cwd or self.integration,
                              env=self.legacy_env(), capture_output=True, text=True, timeout=120)
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode == 0, ok, f"old tools/lane {' '.join(args)} exit {proc.returncode}:\n{output}")
        return output

    def listed(self, output: str) -> dict[str, dict[str, object]]:
        return {lane["id"]: lane for lane in json.loads(output)["lanes"]}


class StateCompatibilityTests(LegacyCase):
    def test_a_lane_the_old_tool_started_and_checked_lands_with_lanefather(self):
        self.legacy("start", "old", "--ticket", "404", "--base", "integrate/q", "--claims", "lib/**", "--create")
        worktree = pathlib.Path(self.record("old")["worktree"])
        self.commit(worktree, {"lib/api.txt": "foo\nold\n"})
        self.legacy("check", "old", cwd=worktree)
        # lanefather reads the old record and its receipt as current and green.
        lane = self.listed(self.lane("list", "--json"))["old"]
        self.assertEqual((lane["status"], lane["receipt"]), ("active", "green"))
        self.assertIn("Receipt current", self.lane("check", "old", cwd=worktree))
        # It refuses an overlapping lane against the old lane's claims, and lands and finishes the old lane.
        self.assertIn("claims overlap lane old", self.start("new", "lib/api.txt", ok=False))
        self.assertIn("post-merge green", self.lane("land", "old"))
        self.assertEqual(self.record("old")["status"], "landed")
        # The old tool's review finishes the lane lanefather landed.
        self.assertIn("Lane old done", self.legacy("review", "old"))
        self.assertEqual(self.record("old")["status"], "done")
        self.assertFalse(worktree.exists())
        self.assertEqual(self.listed(self.legacy("list", "--json", "--all"))["old"]["status"], "done")

    def test_a_lane_lanefather_started_and_checked_lands_with_the_old_tool(self):
        worktree = pathlib.Path(self.created("new", "calls/**"))
        self.commit(worktree, {"calls/main.txt": "foo\n", "calls/more.txt": "foo\n"})
        self.lane("check", "new", cwd=worktree)
        lane = self.listed(self.legacy("list", "--json"))["new"]
        self.assertEqual((lane["status"], lane["receipt"], lane["worktree"]), ("active", "green", str(worktree)))
        self.assertIn("Receipt current", self.legacy("check", "new", cwd=worktree))
        self.assertIn("claims overlap lane new", self.legacy("start", "old", "--ticket", "404", "--base",
                                                             "integrate/q", "--claims", "calls/**", ok=False))
        self.assertIn("post-merge green", self.legacy("land", "new"))
        self.assertEqual(self.record("new")["status"], "landed")
        self.assertIn("Lane new done", self.lane("review", "new"))
        self.assertEqual(self.record("new")["status"], "done")
        self.assertFalse(worktree.exists())

    def test_reviews_and_marks_cross_between_the_tools(self):
        # A review receipt lanefather writes holds a landed lane in the old tool's promote gate, and the old tool's
        # mark finishes it.
        finding = {"severity": "P2", "file": "lib/api.txt", "line": 1, "title": "a finding"}
        self.review_text.write_text("```json\n" + json.dumps({"findings": [finding]}) + "\n```\n")
        worktree = pathlib.Path(self.created("rev", "lib/**"))
        self.commit(worktree, {"lib/api.txt": "foo\nrev\n"})
        self.lane("check", "rev", cwd=worktree)
        self.lane("land", "rev")
        self.lane("review", "rev")
        self.assertIn("1 of 1 review findings unmarked", self.legacy("list"))
        self.legacy("review", "rev", "--mark", "1", "rejected:the fixture's finding")
        review = self.review("rev")
        self.assertEqual(review["findings"][0]["mark"]["kind"], "rejected")
        self.assertEqual(self.record("rev")["status"], "done")

    def test_the_queue_and_the_ledger_are_shared(self):
        queue = self.state.parent / "queue" / "queue.toml"
        queue.parent.mkdir(parents=True)
        queue.write_text('base = "integrate/q"\n\n[[lane]]\nticket = 501\nid = "501-q"\nclaims = ["docs/**"]\n')
        self.legacy("next", "--start")
        self.assertIn("501-q (#501): registered (planned)", self.lane("next", ok=False))
        ledger = (self.state / "ledger.log").read_text()
        self.assertIn("start 501-q ticket=501", ledger)

    def test_a_newer_schema_is_refused_rather_than_rewritten(self):
        self.start("future", "docs/**")
        record = self.record("future")
        record["schema"] = 2
        (self.state / "future.json").write_text(json.dumps(record))
        self.assertIn("schema 2, newer than this lanefather's 1", self.lane("list", ok=False))
        self.assertEqual(json.loads((self.state / "future.json").read_text())["schema"], 2)


class LandingLockCompatibilityTests(LegacyCase):
    def test_each_tool_waits_for_the_others_landing_lock(self):
        # The lock file format is the reference-transaction hook's contract; both tools write and honour it.
        common = self.git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        lock = pathlib.Path(common) / "landing.lock"
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        lock.write_text(f"token abc\nholder another tool\nacquired {int(time.time())}\n"
                        f"pid {holder.pid}\nhost {os.uname().nodename}\n")
        worktree = pathlib.Path(self.created("locked", "lib/**"))
        self.commit(worktree, {"lib/api.txt": "foo\nlocked\n"})
        self.lane("check", "locked", cwd=worktree)
        self.assertIn("held by another tool", self.legacy("land", "locked", "--wait", "0", ok=False))
        self.assertIn("held by another tool", self.lane("land", "locked", "--wait", "0", ok=False))
        holder.kill()
        holder.wait()
        # A holder that exited leaves a stale lock either tool takes over.
        self.assertIn("post-merge green", self.lane("land", "locked", "--wait", "5"))
        self.assertIn("landing lock taken over from another tool", (self.state / "ledger.log").read_text())


if __name__ == "__main__":
    unittest.main()
