"""Finishing lanes by themselves (fathers#157): auto-done when a lane becomes finished, `keep`, `done --finished`
and start's disk repair, in scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any
import unittest
import unittest.mock

from support import TWO_FINDINGS, LaneCase, project, template_repository


class FinishedLaneTests(LaneCase):
    def agent_lane(self, lane_id: str, claim: str) -> pathlib.Path:
        self.start(lane_id, claim)
        worktree = self.repo / ".claude" / "worktrees" / f"agent-{lane_id}"
        self.git(self.repo, "worktree", "add", "-q", "-b", f"worktree-agent-{lane_id}", str(worktree), "integrate/q")
        self.git(worktree, "checkout", "-q", "-B", f"lane/{lane_id}", "integrate/q")
        self.lane("adopt", lane_id, str(worktree), f"lane/{lane_id}")
        self.commit(worktree, {claim: f"{lane_id}\n"})
        self.lane("check", lane_id)
        self.write(self.record(lane_id)["scratch"][0], "probe.log", "evidence")
        return worktree

    def assert_removed(self, lane_id: str, worktree: pathlib.Path) -> None:
        self.assertFalse(worktree.exists())
        self.assertNotIn(f"lane/{lane_id}", self.git(self.repo, "branch", "--list"))
        self.assertFalse(pathlib.Path(self.record(lane_id)["scratch"][0]).exists())
        self.assertEqual(self.record(lane_id)["status"], "done")

    def landed_unfinished(self, lane_id: str, claim: str) -> pathlib.Path:
        """A finished lane done refused when it became finished, as a transient refusal leaves it."""
        worktree = self.agent_lane(lane_id, claim)
        saved = self.comments.read_text()
        self.comments.write_text(json.dumps([]))
        self.lane("land", lane_id)
        self.assertIn("gh issue comment 404", self.lane("review", lane_id))
        self.comments.write_text(saved)
        self.assertEqual(self.record(lane_id)["status"], "landed")
        return worktree

    def test_marking_the_last_finding_removes_worktree_branches_and_scratch(self):
        self.review_text.write_text(TWO_FINDINGS)
        worktree = self.agent_lane("a", "lib/a.txt")
        self.lane("land", "a")
        self.lane("review", "a")
        self.lane("review", "a", "--mark", "1", "rejected:not a bug")
        self.assertTrue(worktree.exists())
        output = self.lane("review", "a", "--mark", "2", "rejected:covered elsewhere")
        self.assertIn("Lane a done", output)
        self.assertIn(f"removed {worktree.resolve()}", output)
        self.assert_removed("a", worktree)

    def test_a_kept_lane_survives_auto_done_and_done_finished_until_keep_off(self):
        worktree = self.agent_lane("a", "lib/a.txt")
        self.assertIn("Lane a kept", self.lane("keep", "a"))
        self.lane("land", "a")
        self.assertIn("Lane a kept (lanefather keep)", self.lane("review", "a"))
        self.assertTrue(worktree.exists())
        self.assertTrue(self.record("a")["landed"]["keep"], "the flag older installs read")
        output = self.lane("done", "--finished")
        self.assertIn("0 lanes finished", output)
        self.assertTrue(worktree.exists())
        self.lane("keep", "a", "--off")
        listed = self.lane("done", "--finished", "--dry-run")
        self.assertRegex(listed, r"a: \d+\.\d GB  " + str(worktree.resolve()))
        self.assertIn("1 finished lane", listed)
        self.assertIn("dry run: nothing removed", listed)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("a")["status"], "landed")
        self.assertIn("1 lane finished", self.lane("done", "--finished"))
        self.assert_removed("a", worktree)
        ledger = (self.state / "ledger.log").read_text()
        self.assertIn("keep a", ledger)
        self.assertIn("unkeep a", ledger)

    def test_auto_done_off_leaves_a_finished_lane_to_done_finished(self):
        # In the repository, so the registry lists the project with it.
        configured = self.repo / "auto-off.toml"
        configured.write_text((self.integration / ".lanefather.toml").read_text() + "\n[done]\nauto = false\n")
        self.env["LANEFATHER_CONFIG"] = str(configured)
        worktree = self.agent_lane("a", "lib/a.txt")
        self.lane("land", "a")
        self.assertIn("auto-done is off ([done] auto = false)", self.lane("review", "a"))
        self.assertTrue(worktree.exists())
        self.lane("done", "--finished")
        self.assert_removed("a", worktree)

    def test_done_finished_keeps_every_refusal_of_done(self):
        worktree = self.landed_unfinished("a", "lib/a.txt")
        self.write(worktree, "lib/a.txt", "uncommitted\n")
        output = self.lane("done", "--finished")
        self.assertIn("has uncommitted changes", output)
        self.assertIn("1 refused", output)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("a")["status"], "landed")

    def add_project(self, name: str, lanes: list[dict[str, Any]], config_text: str | None = None,
                    state: pathlib.Path | None = None, common: str | None = None) -> tuple[pathlib.Path, pathlib.Path]:
        """A registered project with its own repository and state directory, and lanes its done refused when they
        became finished: each landed, smoke green, review exempt, ticket none, at the main checkout's commit. A lane
        gives "id" and may override any field (None removes it). Returns the repository and the state directory."""
        other = self.tmp / name
        if not other.exists():
            shutil.copytree(template_repository(), other, symlinks=True)
        state = state or self.tmp / f"{name}-state"
        state.mkdir(exist_ok=True)
        common = common or str((other / ".git").resolve())
        for lane in lanes:
            lane_id = lane["id"]
            worktree = other / ".claude" / "worktrees" / f"lane-{lane_id}"
            self.git(other, "worktree", "add", "-q", "-b", f"lane/{lane_id}", str(worktree))
            record: dict[str, Any] = {
                "id": lane_id, "ticket": "none", "status": "landed", "branch": f"lane/{lane_id}", "base": "main",
                "common_dir": common, "worktree": str(worktree.resolve()), "claims": ["x/**"], "after": [],
                "scratch": [str(state / f"{lane_id}.scratch")], "created": "2026-11-16T00:00:00Z",
                "overrides": [], "post_merge": {"status": "green"},
                "landed": {"merge": self.git(other, "rev-parse", "HEAD"), "integration": str(other)}}
            record.update(lane)
            self.write(state, f"{lane_id}.json", json.dumps({k: v for k, v in record.items() if v is not None}))
            self.write(state, f"{lane_id}.review.json", json.dumps({"status": "exempt", "exempt": "docs only"}))
            self.write(state / f"{lane_id}.scratch", "build.log", "x" * 4096)
        configured = other / ".lanefather.toml"
        if config_text is not None:
            configured = other / f"{name}.toml"
            configured.write_text(config_text)
        registry = pathlib.Path(self.env["LANEFATHER_REGISTRY"])
        known: dict[str, Any] = json.loads(registry.read_text()) if registry.exists() else {"v": 1, "projects": []}
        known["projects"].append({"state_dir": str(state), "common_dir": common, "config": str(configured),
                                  "project": name, "seen": "2026-11-16T00:00:00Z"})
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps(known))
        return other, state

    def other_status(self, state: pathlib.Path, lane_id: str) -> str:
        return json.loads((state / f"{lane_id}.json").read_text())["status"]

    def below_floor(self, until: str | None = None) -> Any:
        """Free disk at 100 GB under a 150 GB floor (LANEFATHER_MIN_DISK_GB); 500 GB once lane `until` is done."""
        from lanefather import host
        self.env["LANEFATHER_MIN_DISK_GB"] = "150"

        def disk(project: Any = None) -> float:
            del project
            return 500.0 if until and self.record(until)["status"] == "done" else 100.0

        return unittest.mock.patch.object(host, "disk_free_gb", disk)

    def start_n(self, lane_id: str = "n", claims: str = "z/**", ok: bool = True) -> str:
        return self.lane("start", lane_id, "--ticket", "404", "--base", "integrate/q", "--claims", claims,
                         process=False, ok=ok)

    def test_start_below_the_disk_floor_frees_the_finished_lanes_of_every_project_and_admits(self):
        worktree = self.landed_unfinished("a", "lib/a.txt")
        kept = self.landed_unfinished("k", "lib/k.txt")
        self.lane("keep", "k")
        other, other_state = self.add_project("other", [{"id": "o"}])
        with self.below_floor(until="a"):
            output = self.start_n()
        self.assertIn("free disk is 100 GB (10^9), under the floor of 150 GB (10^9; LANEFATHER_MIN_DISK_GB): "
                      "running done", output)
        self.assertIn("Lane a done", output)
        self.assertIn("Lane o done", output)
        self.assertIn("2 lanes finished", output)
        self.assert_removed("a", worktree)
        self.assertFalse((other / ".claude" / "worktrees" / "lane-o").exists())
        self.assertEqual(self.other_status(other_state, "o"), "done")
        self.assertIn("done o", (other_state / "ledger.log").read_text())
        self.assertTrue(kept.exists(), "a kept lane stays")
        self.assertEqual(self.record("n")["status"], "planned")

    def test_start_still_short_of_disk_after_the_repair_says_what_it_freed(self):
        self.landed_unfinished("a", "lib/a.txt")
        with self.below_floor():
            output = self.start_n(ok=False)
        self.assertIn("done on finished lanes freed", output)
        self.assertIn("on 1", output)
        self.assertEqual(self.record("a")["status"], "done")
        self.assertFalse((self.state / "n.json").exists())

    # Review of fathers#157 (P1): fail closed.

    def test_unattended_done_needs_a_process_source_while_done_id_keeps_todays_behaviour(self):
        configured = self.repo / "no-hook.toml"
        configured.write_text((self.integration / ".lanefather.toml").read_text()
                              .replace('process_scan = "tools/process_scan.py"', ""))
        self.env["LANEFATHER_CONFIG"] = str(configured)
        worktree = self.agent_lane("a", "lib/a.txt")
        self.lane("land", "a")
        self.assertIn("no process source answers", self.lane("review", "a"))
        self.assertTrue(worktree.exists())
        self.assertIn("1 refused", self.lane("done", "--finished"))
        self.assertTrue(worktree.exists())
        self.lane("done", "a")
        self.assert_removed("a", worktree)

    def test_a_lane_kept_after_the_finished_lanes_were_read_is_not_removed(self):
        from lanefather import done
        worktree = self.landed_unfinished("a", "lib/a.txt")
        self.lane("keep", "a")
        # As if `keep a` came between the read of the finished lanes and done: the read still says finished.
        def finished(*record_and_store: Any) -> None:
            del record_and_store

        with unittest.mock.patch.object(done, "unfinished", finished):
            output = self.lane("done", "--finished", process=False)
        self.assertIn("lane a is kept", output)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("a")["status"], "landed")

    def test_one_broken_project_or_lane_never_stops_the_repair_or_start(self):
        broken = self.tmp / "gone.toml"
        registry = pathlib.Path(self.env["LANEFATHER_REGISTRY"])
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({"v": 1, "projects": [{
            "state_dir": str(self.tmp / "gone-state"), "common_dir": "/nowhere/.git", "config": str(broken),
            "project": "gone", "seen": "2999-01-01T00:00:00Z"}]}))
        other, state = self.add_project("other", [{"id": "b-norepo", "common_dir": None},
                                                  {"id": "c-moved", "landed": {"integration": "/nowhere/moved"}},
                                                  {"id": "o"}])
        worktree = self.landed_unfinished("a", "lib/a.txt")
        with self.below_floor(until="a"):
            output = self.start_n()
        self.assertIn("skipped gone: config not found", output)
        self.assertIn("refused: lane b-norepo names no repository that exists", output)
        self.assertIn("2 refused", output)
        self.assertIn("integration worktree /nowhere/moved is gone", output)
        self.assertEqual(self.other_status(state, "o"), "done")
        self.assertEqual(self.other_status(state, "c-moved"), "landed")
        self.assertTrue((other / ".claude" / "worktrees" / "lane-c-moved").exists())
        self.assert_removed("a", worktree)
        self.assertEqual(self.record("n")["status"], "planned")

    def test_a_process_in_the_scratch_or_build_directory_holds_the_lane(self):
        worktree = self.landed_unfinished("a", "lib/a.txt")
        scratch = pathlib.Path(self.record("a")["scratch"][0])
        child = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(300)",
                                  "scratch-holder"], cwd=scratch, stdout=subprocess.PIPE, text=True,
                                 start_new_session=True)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        assert child.stdout is not None
        self.assertEqual(child.stdout.readline().strip(), "ready")
        output = self.lane("done", "a", ok=False)
        self.assertIn(f"pid {child.pid}", output)
        self.assertIn(f"it works in {scratch}", output)
        self.assertTrue(worktree.exists())
        self.assertTrue(scratch.exists())

    # Review of fathers#157 (P2).

    def test_the_floor_is_the_reserve_plus_the_cap_times_the_largest_recent_lane(self):
        from lanefather import config, host
        records = [{"status": "done", "done": f"2026-11-{day:02d}T00:00:00Z", "freed_bytes": size}
                   for day, size in ((1, 9 * 10**9), *((d, 2 * 10**9) for d in range(2, 22)), (22, 3 * 10**9))]
        with unittest.mock.patch.dict(os.environ, {"LANEFATHER_MIN_DISK_GB": ""}):
            floor = host.disk_floor(config.Config(), 10, records)
            # The 9 GB lane is older than the 20 most recent: the peak is 3 GB.
            self.assertEqual((floor.gb, floor.peak, floor.source), (70.0, 3.0, "derived"))
            self.assertEqual(floor.words(), "70 GB (10^9) = 40 reserve + 10 x 3.0 GB lane peak")
            self.assertEqual(host.disk_floor(config.Config(), 4, []).gb, 40 + 4 * host.DEFAULT_LANE_PEAK_GB)
        with unittest.mock.patch.dict(os.environ, {"LANEFATHER_MIN_DISK_GB": "25"}):
            self.assertEqual(host.disk_floor(config.Config(), 10, records).words(),
                             "25 GB (10^9; LANEFATHER_MIN_DISK_GB)")
        with unittest.mock.patch.dict(os.environ, {"LANEFATHER_MIN_DISK_GB": "lots"}):
            self.assertEqual(host.disk_floor(config.Config(), 10, records).gb, 70.0)

    def test_a_disk_refusal_states_the_derived_floor(self):
        from lanefather import host
        def disk(project: Any = None) -> float:
            del project
            return 10.0

        with unittest.mock.patch.object(host, "disk_free_gb", disk):
            output = self.start_n(ok=False)
        # 6 cores and 20 GB give a cap of 3; no lane is done yet, so the peak is the default 3 GB.
        self.assertIn("free disk is 10 GB (10^9), under the floor of 49 GB (10^9) = 40 reserve + 3 x 3.0 GB "
                      "lane peak", output)

    def test_the_repair_runs_once_per_window_and_a_lane_done_meanwhile_is_no_refusal(self):
        from lanefather import done
        self.landed_unfinished("a", "lib/a.txt")
        with self.below_floor():
            self.start_n(ok=False)
        self.assertEqual(self.record("a")["status"], "done")
        worktree = self.landed_unfinished("b", "lib/b.txt")
        with self.below_floor():
            output = self.start_n(ok=False)
        self.assertIn("s ago", output)
        self.assertEqual(self.record("b")["status"], "landed")
        self.assertTrue(worktree.exists())
        with unittest.mock.patch.dict(os.environ, self.env), project(self.repo):
            self.assertEqual(done.finish("a", None, unattended=True), 0)
        # Only the refusal the fixture made when a became finished: done on a lane already done is no refusal.
        self.assertEqual((self.state / "ledger.log").read_text().count("auto-done a refused"), 1)

    def test_the_repair_waits_for_every_cheaper_refusal(self):
        worktree = self.landed_unfinished("a", "lib/a.txt")
        self.start("c", "q/**")
        with self.below_floor():
            self.assertIn("claims overlap lane c", self.start_n(claims="q/**", ok=False))
            self.assertIn("lane c exists", self.start_n(lane_id="c", ok=False))
            self.assertIn("--after nobody", self.lane("start", "n", "--ticket", "404", "--base", "integrate/q",
                                                      "--claims", "z/**", "--after", "nobody", process=False,
                                                      ok=False))
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record("a")["status"], "landed")

    def test_done_refuses_while_a_fix_lane_works_in_the_worktree_or_its_head_is_not_merged(self):
        worktree = self.landed_unfinished("a", "lib/a.txt")
        fix = dict(self.record("a"), id="a-review-fixes", status="active", branch="lane/a-review-fixes")
        self.write(self.state, "a-review-fixes.json", json.dumps(fix))
        self.assertIn(f"lane a-review-fixes works in {worktree.resolve()} too", self.lane("done", "a", ok=False))
        (self.state / "a-review-fixes.json").unlink()
        self.git(worktree, "checkout", "-q", "--detach")
        self.commit(worktree, {"lib/a.txt": "detached work\n"})
        self.assertIn("with commits that are not in", self.lane("done", "a", ok=False))
        self.assertTrue(worktree.exists())

    # Review of fathers#157 (P3).

    def test_the_repair_leaves_projects_whose_auto_done_is_off_which_done_finished_does_not(self):
        text = (self.integration / ".lanefather.toml").read_text() + "\n[done]\nauto = false\n"
        other, state = self.add_project("other", [{"id": "o"}], config_text=text)
        with self.below_floor():
            self.start_n(ok=False)
        self.assertEqual(self.other_status(state, "o"), "landed")
        self.assertIn("Lane o done", self.lane("done", "--finished"))
        self.assertEqual(self.other_status(state, "o"), "done")
        del other

    def test_a_lane_of_a_state_directory_two_projects_share_is_listed_once(self):
        other, state = self.add_project("other", [{"id": "o", "common_dir": None}])
        self.add_project("other", [], state=state, common="/elsewhere/.git")
        listed = self.lane("done", "--finished", "--dry-run")
        self.assertEqual(listed.count(" o: "), 1, listed)
        del other


if __name__ == "__main__":
    unittest.main()
