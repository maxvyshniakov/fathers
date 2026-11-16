"""Routine cleanups without --force-reason (fathers#167): `drop` of a lane that adds nothing to its base, and a red
post-merge smoke cleared by a later green run on the integration branch or by `recheck`, in scratch Git
repositories (support.py)."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
from typing import Any
import unittest

from support import LaneCase

from lanefather import results


class DropTests(LaneCase):
    """`drop ID` removes an active lane only when nothing of it can be lost."""

    def assert_dropped(self, lane_id: str, worktree: Any) -> dict[str, Any]:
        self.assertFalse(pathlib.Path(worktree).exists())
        self.assertNotIn(f"lane/{lane_id}", self.git(self.repo, "branch", "--list"))
        record = self.record(lane_id)
        self.assertFalse(pathlib.Path(record["scratch"][0]).exists())
        self.assertEqual(record["status"], "done")
        return record

    def test_drop_removes_a_lane_without_commits_and_releases_its_claims(self):
        worktree = self.created("e", "lib/**")
        self.write(self.record("e")["scratch"][0], "probe.log", "scratch")
        output = self.lane("drop", "e")
        self.assertIn("Lane e dropped", output)
        record = self.assert_dropped("e", worktree)
        self.assertIn("no commits beyond integrate/q", record["dropped"]["why"])
        ledger = (self.state / "ledger.log").read_text()
        self.assertRegex(ledger, r"drop e: no commits beyond integrate/q .*removed=")
        self.start("next", "lib/**")

    def test_drop_removes_a_lane_whose_change_the_integration_already_made(self):
        # dg-pointer-dismiss-ring: registered for a prune integration had already made; its tree equals its base.
        worktree = self.created("e", "lib/**")
        self.commit(self.integration, {"lib/api.txt": "foo\npruned\n"}, "the integration made the change")
        self.commit(worktree, {"lib/api.txt": "foo\npruned\n"}, "the same change on the lane")
        self.lane("drop", "e")
        record = self.assert_dropped("e", worktree)
        self.assertIn("its tree equals integrate/q", record["dropped"]["why"])

    def test_drop_removes_a_lane_whose_commits_change_nothing(self):
        worktree = self.created("e", "lib/**")
        self.commit(self.integration, {"notes.txt": "the integration moves on\n"}, "moves on")
        self.commit(worktree, {"lib/api.txt": "foo\ntried\n"}, "try")
        self.commit(worktree, {"lib/api.txt": "foo\n"}, "revert the try")
        self.lane("drop", "e")
        self.assertIn("change nothing", self.assert_dropped("e", worktree)["dropped"]["why"])

    def test_drop_refuses_a_lane_with_unlanded_commits_and_keeps_everything(self):
        worktree = self.created("u", "lib/**")
        self.commit(worktree, {"lib/api.txt": "unlanded work\n"})
        output = self.lane("drop", "u", ok=False)
        self.assertIn("drop refused", output)
        self.assertIn("lib/api.txt", output)
        self.assertNotIn("--force-reason", output)
        self.assertTrue(pathlib.Path(worktree).exists())
        self.assertIn("lane/u", self.git(self.repo, "branch", "--list"))
        self.assertEqual(self.record("u")["status"], "active")
        # No flag overrides it.
        self.assertIn("unrecognized arguments", self.lane("drop", "u", "--force-reason", "r", ok=False))

    def test_drop_refuses_a_worktree_head_with_commits_the_branch_lacks(self):
        worktree = self.created("u", "lib/**")
        self.git(worktree, "checkout", "-q", "-b", "elsewhere")
        self.commit(worktree, {"lib/api.txt": "work on another branch\n"})
        self.assertIn(f"{pathlib.Path(worktree)} is at", self.lane("drop", "u", ok=False))
        self.assertTrue(pathlib.Path(worktree).exists())

    def test_drop_refuses_uncommitted_or_untracked_changes(self):
        worktree = self.created("u", "lib/**")
        self.write(worktree, "notes/draft.md", "an untracked draft\n")
        output = self.lane("drop", "u", ok=False)
        self.assertIn("uncommitted or untracked changes", output)
        self.assertIn("notes/draft.md", output)
        pathlib.Path(worktree, "notes/draft.md").unlink()
        self.write(worktree, "lib/api.txt", "an uncommitted edit\n")
        self.assertIn("lib/api.txt", self.lane("drop", "u", ok=False))
        self.assertTrue(pathlib.Path(worktree).exists())
        self.assertEqual(self.record("u")["status"], "active")

    def test_drop_refuses_a_landed_lane(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.lane("land", "a")
        self.assertIn("is landed: lanefather done a", self.lane("drop", "a", ok=False))


class ClearedPostMergeTests(LaneCase):
    """A landed lane whose post-merge smoke went red for a reason outside it becomes done-able without
    --force-reason: a later green run on the integration branch that contains its merge, or `recheck`."""

    def red_lane(self, lane_id: str = "a") -> pathlib.Path:
        """A lane whose smoke went red although its change is fine: the smoke environment failed lib-check."""
        worktree = pathlib.Path(self.created(lane_id, "lib/**"))
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", lane_id)
        # The integration moves on with a change every check reads, so the whole smoke runs.
        self.integration_moves()
        self.env["FIXTURE_STATUS"] = f"post-merge-{lane_id}:lib-check=failed"
        self.assertIn("post-merge red", self.lane("land", lane_id, ok=False))
        del self.env["FIXTURE_STATUS"]
        self.assertEqual(self.record(lane_id)["post_merge"]["status"], "red")
        return worktree

    def green_lane(self, lane_id: str, path: str) -> str:
        """A later lane whose whole smoke runs green on a tip that holds the red lane's merge; its merge."""
        worktree = self.created(lane_id, path.split("/")[0] + "/**")
        self.commit(worktree, {path: f"{lane_id}\n"})
        self.lane("check", lane_id)
        self.commit(self.integration, {f"tooling/moved-{lane_id}.txt": "moved on\n"}, "the integration moves on")
        self.assertIn("post-merge green", self.lane("land", lane_id))
        return self.record(lane_id)["landed"]["merge"]

    def test_done_refuses_a_red_lane_without_later_evidence(self):
        worktree = self.red_lane()
        output = self.lane("done", "a", ok=False)
        self.assertIn("post-merge check is red", output)
        self.assertIn("lanefather recheck a", output)
        self.assertTrue(worktree.exists())

    def test_a_later_green_smoke_that_ran_the_red_checks_clears_done_and_is_named(self):
        worktree = self.red_lane()
        merge = self.green_lane("b", "tooling/b.txt")
        output = self.lane("done", "a")
        self.assertIn("Lane a done", output)
        self.assertFalse(worktree.exists())
        receipt = str(self.state / "b.post-merge.json")
        cleared = self.record("a")["post_merge"]["cleared_by"]
        self.assertEqual((cleared["kind"], cleared["receipt"], cleared["sha"]), ("post-merge", receipt, merge))
        ledger = (self.state / "ledger.log").read_text()
        self.assertIn(f"post-merge red cleared by the post-merge smoke of b {merge[:10]} ({receipt})", ledger)
        self.assertNotIn("override", ledger)

    def test_a_green_smoke_that_finished_in_the_red_smokes_second_clears_it(self):
        # CI run 43896662055 (Linux): the stamps have one-second resolution, and on a fast runner the later lane's
        # smoke finished within the second the red smoke did. Same second is not earlier.
        self.red_lane()
        self.green_lane("b", "tooling/b.txt")
        red = self.state / "a.post-merge.json"
        finished = json.loads((self.state / "b.post-merge.json").read_text())["finished"]
        red.write_text(json.dumps({**json.loads(red.read_text()), "finished": finished}))
        self.lane("done", "a")

    def test_a_green_smoke_that_did_not_run_the_red_check_or_precedes_the_merge_clears_nothing(self):
        # A green smoke before the red lane's merge does not hold it.
        self.green_lane("z", "tooling/z.txt")
        self.red_lane()
        # A later green land whose smoke did not run lib-check.
        worktree = self.created("c", "calls/**")
        self.commit(worktree, {"calls/c.txt": "foo\n"})
        self.lane("check", "c")
        self.env["FIXTURE_SMOKE"] = "structure-check"
        self.assertIn("post-merge green", self.lane("land", "c"))
        self.assertNotIn("lib-check", json.loads((self.state / "c.post-merge.json").read_text())["check_results"])
        self.assertIn("post-merge check is red", self.lane("done", "a", ok=False))

    def gate_receipt(self, sha: str, **changes: Any) -> pathlib.Path:
        path = self.state / f"promote-{sha[:10]}.gate.json"
        receipt = {"version": 1, "lane": f"promote-{sha[:10]}", "sha": sha, "full": True, "status": "green",
                   "tree": self.git(self.repo, "rev-parse", sha + "^{tree}"), "finished": "2999-01-01T00:00:00Z",
                   "platform": results.host_platform()}
        path.write_text(json.dumps({**receipt, **changes}))
        return path

    def test_a_green_batch_gate_on_a_later_tip_clears_done(self):
        self.red_lane()
        self.commit(self.integration, {"notes.txt": "later\n"}, "later on the integration branch")
        tip = self.tip()
        self.gate_receipt(tip, tree="0" * 40)
        self.assertIn("post-merge check is red", self.lane("done", "a", ok=False))
        gate = self.gate_receipt(tip)
        self.lane("done", "a")
        self.assertIn(f"cleared by the batch gate of {tip[:10]} ({gate})", (self.state / "ledger.log").read_text())

    def test_a_gate_off_the_integration_branch_or_before_the_merge_clears_nothing(self):
        self.red_lane()
        merge = self.record("a")["landed"]["merge"]
        self.gate_receipt(self.git(self.repo, "rev-parse", merge + "^1"))
        side = self.repo / ".claude" / "worktrees" / "side"
        self.git(self.repo, "worktree", "add", "-q", "-b", "side", str(side), merge)
        self.gate_receipt(self.commit(side, {"side.txt": "off the branch\n"}))
        self.assertIn("post-merge check is red", self.lane("done", "a", ok=False))

    def test_a_gate_on_another_platform_or_before_the_red_smoke_clears_nothing(self):
        # Review of fathers#167: a Linux gate in a shared state directory, or a gate that passed the merge before its
        # smoke went red, says nothing about the red smoke.
        self.red_lane()
        tip = self.tip()
        other = "linux-x64" if results.host_platform() != "linux-x64" else "darwin-arm64"
        self.gate_receipt(tip, platform=other)
        self.assertIn("post-merge check is red", self.lane("done", "a", ok=False))
        self.gate_receipt(tip, finished="2000-01-01T00:00:00Z")
        self.assertIn("post-merge check is red", self.lane("done", "a", ok=False))
        # A gate receipt from before receipts named their platform: its results-store entry names it.
        gate = self.gate_receipt(tip, platform=None)
        tree = self.git(self.repo, "rev-parse", tip + "^{tree}")
        self.lane("results", "record", tip, "--full", "--status", "green", "--source", "gate", "--receipt", str(gate),
                  "--platform", other)
        self.assertIn("post-merge check is red", self.lane("done", "a", ok=False))
        self.lane("results", "record", tree, "--full", "--status", "green", "--source", "gate", "--receipt", str(gate))
        self.lane("done", "a")

    def test_concurrent_rechecks_run_one_at_a_time(self):
        # Review of fathers#167: a failing second recheck rewrote the receipt a green one left the record pointing at.
        self.red_lane()
        hold = self.tmp / "hold-recheck"
        first = subprocess.Popen([sys.executable, "-m", "lanefather", "recheck", "a"], cwd=self.integration,
                                 env={**self.env, "FIXTURE_HOLD": f"recheck-a:{hold}"}, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: first.poll() is None and (first.kill(), first.wait()))
        self.addCleanup(hold.touch)
        started = pathlib.Path(f"{hold}.started")
        deadline = time.monotonic() + 60
        while not started.exists():
            # load-tolerant: the first recheck's run starts in about a second.
            self.assertLess(time.monotonic(), deadline, "the first recheck's run did not start")
            self.assertIsNone(first.poll())
            time.sleep(0.05)
        log = self.tmp / "second.log"
        with open(log, "w") as out:
            second = subprocess.Popen([sys.executable, "-m", "lanefather", "recheck", "a"], cwd=self.integration,
                                      env={**self.env, "FIXTURE_STATUS": "recheck-a:lib-check=failed"}, stdout=out,
                                      stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: second.poll() is None and (second.kill(), second.wait()))
        deadline = time.monotonic() + 60
        while "waiting for another recheck of a" not in log.read_text():
            # load-tolerant: the second recheck reaches the lock in about a second.
            self.assertLess(time.monotonic(), deadline, "the second recheck did not wait for the first")
            self.assertIsNone(second.poll(), log.read_text())
            time.sleep(0.05)
        hold.touch()
        self.assertEqual(first.wait(timeout=120), 0, first.stdout.read() if first.stdout else "")
        self.assertEqual(second.wait(timeout=120), 1)
        self.assertIn("nothing to recheck", log.read_text())
        receipt = json.loads((self.state / "a.recheck.json").read_text())
        self.assertEqual(receipt["status"], "green")
        self.assertEqual(self.record("a")["post_merge"]["receipt"], str(self.state / "a.recheck.json"))

    def test_recheck_reruns_the_red_smoke_on_the_integration_tip_and_replaces_it(self):
        self.red_lane()
        self.commit(self.integration, {"notes.txt": "later\n"}, "later on the integration branch")
        tip = self.tip()
        output = self.lane("recheck", "a")
        self.assertIn(f"post-merge green on {tip[:10]}", output)
        runs = [line.split() for line in pathlib.Path(str(self.verify_log) + ".runs").read_text().splitlines()]
        self.assertIn(["recheck-a", tip, "detached"], [run[:3] for run in runs])
        receipt = json.loads((self.state / "a.recheck.json").read_text())
        self.assertEqual((receipt["status"], receipt["sha"], receipt["merge"]),
                         ("green", tip, self.record("a")["landed"]["merge"]))
        self.assertEqual(receipt["checks"], {"structure": ["structure-check"], "lib": ["lib-check"]})
        post = self.record("a")["post_merge"]
        self.assertEqual((post["status"], post["cleared_by"]["kind"]), ("green", "recheck"))
        # The red receipt stays as it was.
        self.assertEqual(json.loads((self.state / "a.post-merge.json").read_text())["status"], "red")
        self.lane("done", "a")
        ledger = (self.state / "ledger.log").read_text()
        self.assertIn(f"post-merge red cleared by the recheck of {tip[:10]} ({self.state / 'a.recheck.json'})", ledger)

    def test_a_red_recheck_leaves_the_lane_red_and_a_green_lane_has_nothing_to_recheck(self):
        self.red_lane()
        self.env["FIXTURE_STATUS"] = "recheck-a:lib-check=failed"
        self.assertIn("post-merge still red", self.lane("recheck", "a", ok=False))
        self.assertEqual(self.record("a")["post_merge"]["status"], "red")
        self.assertIn("post-merge check is red", self.lane("done", "a", ok=False))
        del self.env["FIXTURE_STATUS"]
        self.green_lane("b", "tooling/b.txt")
        self.assertIn("nothing to recheck", self.lane("recheck", "b", ok=False))


if __name__ == "__main__":
    unittest.main()
