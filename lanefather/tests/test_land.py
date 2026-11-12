"""Tests of land and the post-merge smoke (lanefather.land, lanefather.plan): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import os
import pathlib
import unittest

from support import RUST_STUB, LaneCase, load_lane, project


class CheckAndLandTests(LaneCase):
    """B4 check and land: receipts keyed by tree, post-merge affected suites."""

    def test_landing_without_a_receipt_is_refused(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        before = self.tip()
        output = self.lane("land", "a", ok=False)
        self.assertIn("no receipt", output)
        self.assertIn("lanefather check a", output)
        self.assertEqual(self.tip(), before)

    def test_a_stale_receipt_is_refused_and_a_rerun_reuses_unaffected_suites(self):
        worktree = self.created("a", "lib/**", "README.md")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.assertIn("green", self.lane("check", "a"))
        self.assertEqual(self.verify_runs(), ["checks structure-check,lib-check"])
        self.assertIn("current", self.lane("check", "a"))
        self.assertEqual(len(self.verify_runs()), 1)
        newer = self.commit(worktree, {"README.md": "edited after the check\n"})
        output = self.lane("land", "a", ok=False)
        self.assertIn("stale", output)
        self.assertIn(self.git(worktree, "rev-parse", newer + "^{tree}"), output)
        self.lane("check", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check")
        receipt = json.loads((self.state / "a.receipt.json").read_text())
        self.assertEqual(receipt["tree"], self.git(worktree, "rev-parse", "HEAD^{tree}"))
        self.assertEqual({r["suite"]: bool(r.get("reused_from")) for r in receipt["results"]},
                         {"structure": False, "lib": True})
        self.lane("land", "a")
        self.assertEqual(self.git(self.integration, "rev-parse", "HEAD^2"), newer)

    def test_a_rerun_reuses_each_check_no_change_since_selects_within_a_suite(self):
        # #422: reuse is per check. lib holds lib-check (lib/, calls/) and schema-check (lib/schema/); a later
        # change to calls/ reruns lib-check alone and keeps schema-check's result.
        worktree = self.created("a", "lib/**", "calls/**")
        self.commit(worktree, {"lib/schema/v1.txt": "v1\n", "calls/main.txt": "foo\n\n"})
        self.lane("check", "a")
        self.assertEqual(self.verify_runs(), ["checks structure-check,lib-check,schema-check"])
        first = json.loads((self.state / "a.receipt.json").read_text())
        self.assertEqual({c: e["status"] for c, e in first["check_results"].items()},
                         {"structure-check": "passed", "lib-check": "passed", "schema-check": "passed"})
        self.commit(worktree, {"calls/main.txt": "foo\n"})
        output = self.lane("check", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        self.assertIn("lib: passed (1 of 2 checks reused)", output)
        receipt = json.loads((self.state / "a.receipt.json").read_text())
        self.assertEqual({c: e.get("reused_from") for c, e in receipt["check_results"].items()},
                         {"structure-check": None, "lib-check": None, "schema-check": first["tree"]})
        lib = next(r for r in receipt["results"] if r["suite"] == "lib")
        self.assertEqual((lib["checks"], lib["reused"]), (["lib-check", "schema-check"], ["schema-check"]))
        self.assertRegex((self.state / "ledger.log").read_text(), r"check a tree=\S+ green suites=structure,lib "
                                                                  r"checks=2 reused=1 ")

    def test_a_red_check_is_rerun_and_its_passed_neighbours_are_reused(self):
        worktree = self.created("a", "lib/**", "calls/**", "native/**")
        self.commit(worktree, {"lib/schema/v1.txt": "v1\n", "native/straylight-core/corpus.txt": "v2\n"})
        self.lane("check", "a", ok=False)
        self.commit(worktree, {"native/straylight-core/corpus.txt": "v1\n", "native/notes.txt": "fixed\n"})
        self.lane("check", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,native-check")

    def test_a_full_plan_runs_every_check_and_a_later_change_reruns_only_what_it_selects(self):
        # A full plan ran whole suites without per-check results; a lane fixing a typo after a tooling change
        # reran every suite its fix touched.
        worktree = self.created("tool", "tooling/**", "docs/**")
        self.commit(worktree, {"tooling/runner.txt": "v2\n"})
        self.lane("check", "tool")
        self.commit(worktree, {"docs/install.md": "journey\n"})
        output = self.lane("check", "tool")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,host-journeys-check")
        self.assertIn("native: passed (reused from tree", output)
        receipt = json.loads((self.state / "tool.receipt.json").read_text())
        self.assertTrue(receipt["full"])
        self.assertEqual([row["id"] for row in receipt["selection"]],
                         ["structure-check", "lib-check", "schema-check", "native-check", "host-journeys-check"])

    def test_a_path_no_check_reads_runs_every_check_of_the_gate(self):
        # The plan of a path no check reads is the full gate, but only the structure check names that path; the
        # lane check ran --suites structure for it, and the post-merge ran that structure-only plan for it.
        worktree = self.created("odd", "unowned/**")
        self.commit(worktree, {"unowned/thing.bin": "x\n"})
        output = self.lane("check", "odd")
        self.assertIn("no check reads unowned/thing.bin", output)
        self.assertEqual(self.verify_runs(),
                         ["checks structure-check,lib-check,schema-check,native-check,host-journeys-check"])
        receipt = json.loads((self.state / "odd.receipt.json").read_text())
        self.assertEqual(receipt["suites"], ["structure", "lib", "native", "host-journeys"])
        why = {row["id"]: row["paths"] for row in receipt["selection"]}
        self.assertEqual(why["native-check"], ["unowned/thing.bin"])
        self.integration_moves()
        self.assertIn("post-merge green (smoke: structure-check, lib-check; whole, since what the integration "
                      "brought in selects the full gate)", self.lane("land", "odd"))
        post = json.loads((self.state / "odd.post-merge.json").read_text())
        self.assertEqual((post["kind"], post["selection"]),
                         ("smoke", [{"id": "structure-check", "suite": "structure", "paths": ["unowned/thing.bin"]},
                                    {"id": "lib-check", "suite": "lib", "paths": ["unowned/thing.bin"]}]))
        self.assertEqual({c: e["status"] for c, e in post["check_results"].items()},
                         {"structure-check": "passed", "lib-check": "passed"})

    def test_check_and_land_queue_their_verify_runs_under_the_lane_label(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a", "--slots", "2")
        self.integration_moves()
        self.lane("land", "a")
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        # The post-merge smoke runs three jobs on an idle machine (#446).
        self.assertEqual(["lane-a --jobs 2", "post-merge-a --jobs 3"], leases)
        requests = [json.loads(line) for line in pathlib.Path(str(self.verify_log) + ".requests").read_text().splitlines()]
        self.assertEqual([("normal", "404"), ("interactive", "404")],
                         [(entry["priority"], entry["ticket"]) for entry in requests])
        for entry in requests:
            self.assertTrue({"lane", "phase", "lanefather.state"} <= set(entry["meta"]))
            self.assertFalse({"class", "priority", "ticket"} & set(entry["meta"]))

    def test_a_lane_check_sizes_its_lease_from_slotfather_advise(self):
        """#642 review: a lane check asks slotfather advise --max 2, as the gate does, and records the decision; an
        explicit --slots stands. Advice takes out the load no lease explains, the held slots and short memory."""
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        receipt = json.loads((self.state / "a.receipt.json").read_text())
        self.assertEqual((2, 2, 2), (receipt["jobs"], receipt["jobs_decision"]["jobs"],
                                     receipt["jobs_decision"]["maximum"]))
        self.env.update(FAKE_SLOTFATHER_HELD="3")
        self.lane("check", "a", "--rerun")
        self.assertEqual(1, json.loads((self.state / "a.receipt.json").read_text())["jobs"])
        self.lane("check", "a", "--rerun", "--slots", "3")
        receipt = json.loads((self.state / "a.receipt.json").read_text())
        self.assertEqual((3, {"jobs": 3, "override": "--slots"}), (receipt["jobs"], receipt["jobs_decision"]))
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        self.assertEqual(["lane-a --jobs 2", "lane-a --jobs 1", "lane-a --jobs 3"], leases)
        advice = [json.loads(line)["args"] for line in (self.tmp / "slotfather.log").read_text().splitlines()
                  if json.loads(line)["command"] == "advise"]
        self.assertEqual(advice, [["--max", "2"], ["--max", "2"]])

    def test_the_smoke_takes_only_the_free_slots(self):
        """#599: slotfather advise --max 3 gives no more jobs than slots are free, since the queue is FIFO and a
        bigger lease would wait for the lanes' checks."""
        self.env.update(FAKE_SLOTFATHER_HELD="2")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.integration_moves()
        self.lane("land", "a")
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        self.assertEqual(leases[-1], "post-merge-a --jobs 2")
        decision = json.loads((self.state / "a.post-merge.json").read_text())["jobs_decision"]
        self.assertEqual((decision["held"], decision["free"], decision["jobs"]), (2, 2, 2))
        advice = [json.loads(line)["args"] for line in (self.tmp / "slotfather.log").read_text().splitlines()
                  if json.loads(line)["command"] == "advise"]
        # The lane check asked first, at most two (#642 review).
        self.assertEqual(advice, [["--max", "2"], ["--max", "3"]])

    def test_check_refuses_uncommitted_work(self):
        worktree = self.created("a", "lib/**")
        self.write(worktree, "lib/api.txt", "dirty\n")
        self.assertIn("commit", self.lane("check", "a", ok=False))

    def test_a_frozen_corpus_change_runs_the_checks_that_read_it(self):
        # Replay audit 5.3 lane/trap-1: a corpus change its own focused tests do not cover. The plan's
        # graph declares that native-check reads native/, so the lane runs it.
        worktree = self.created("core", "native/**")
        self.commit(worktree, {"native/straylight-core/corpus.txt": "v2\n"})
        output = self.lane("check", "core", ok=False)
        self.assertIn("native: failed failed checks: native-check", output)
        # The lane's run, then native-check alone on the lane and on its base (#568).
        self.assertEqual(self.verify_runs(), ["checks structure-check,native-check", "checks native-check",
                                              "checks native-check"])
        self.assertIn("red", self.lane("land", "core", ok=False))

    def test_a_lane_check_runs_only_the_checks_its_plan_selects(self):
        # Replay 404-finish (10-21): orchestration tooling ran all seven suites for 6,125 s.
        worktree = self.created("a", "lib/**", "docs/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n", "docs/install.md": "journey\n"})
        output = self.lane("check", "a")
        # host-journeys runs in the plan's own suite order, which tools/lane no longer copies.
        self.assertEqual(self.verify_runs(), ["checks structure-check,lib-check,host-journeys-check"])
        self.assertIn("host-journeys: passed", output)
        receipt = json.loads((self.state / "a.receipt.json").read_text())
        self.assertEqual(receipt["checks"], {"structure": ["structure-check"], "lib": ["lib-check"],
                                             "host-journeys": ["host-journeys-check"]})
        self.assertEqual([r["checks"] for r in receipt["results"]],
                         [["structure-check"], ["lib-check"], ["host-journeys-check"]])
        self.integration_moves()
        output = self.lane("land", "a")
        # After the merge the smoke runs, not the lane's plan again (#446): host-journeys waits for the batch gate.
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        self.assertIn("post-merge green (smoke: structure-check, lib-check; whole, since", output)
        post = json.loads((self.state / "a.post-merge.json").read_text())
        self.assertEqual((post["kind"], post["full"], post["jobs"]), ("smoke", False, 3))
        # The receipt records how the job count was chosen (#599): 6 cores and 4 slots, load15 0.1, nothing held.
        self.assertEqual({k: post["jobs_decision"][k] for k in ("jobs", "maximum", "held", "free", "load",
                                                                 "unleased_load", "exclusive")},
                         {"jobs": 3, "maximum": 3, "held": 0, "free": 4, "load": 0.1, "unleased_load": 0.1,
                          "exclusive": False})
        self.assertEqual(post["checks"], {"structure": ["structure-check"], "lib": ["lib-check"]})

    def test_a_structure_only_merge_runs_its_plan_instead_of_the_smoke(self):
        worktree = self.created("docs", "checks/**")
        self.commit(worktree, {"checks/frozen-corpus.txt": "v1\n\n"})
        self.lane("check", "docs")
        self.integration_moves()
        self.assertIn("post-merge green (structure-check; whole, since", self.lane("land", "docs"))
        self.assertEqual(self.verify_runs()[-1], "checks structure-check")
        self.assertEqual(json.loads((self.state / "docs.post-merge.json").read_text())["kind"], "plan")

    def test_a_tree_whose_plan_names_whole_suites_runs_those_suites(self):
        self.env["FIXTURE_SUITE_PLAN"] = "1"
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.assertEqual(self.verify_runs(), ["structure,lib"])
        receipt = json.loads((self.state / "a.receipt.json").read_text())
        self.assertNotIn("checks", receipt["results"][0])

    def test_post_merge_check_catches_a_seam_between_two_green_lanes(self):
        # Replay batch 8, #397c x #401: each lane green alone, the combination breaks.
        rename = self.created("rename", "lib/**", "calls/main.txt")
        caller = self.created("caller", "calls/new.txt")
        self.commit(rename, {"lib/api.txt": "bar\n", "calls/main.txt": "bar\n"})
        self.commit(caller, {"calls/new.txt": "foo\n"})
        self.lane("check", "rename")
        self.lane("check", "caller")
        self.assertIn("post-merge green", self.lane("land", "rename"))
        before = self.tip()
        output = self.lane("land", "caller", ok=False)
        self.assertIn("post-merge red", output)
        self.assertIn("lib", output)
        self.assertIn("lib-check", output)
        self.assertIn("1 unclassified", output)
        # The lanes whose checks ran the failed check, per check.
        self.assertRegex(output.split("ran the failed checks:")[-1], r"\n  lib-check: caller \(#404\), rename \(#404\)")
        self.assertEqual(self.git(self.integration, "rev-parse", "HEAD^1"), before)
        record = self.record("caller")
        self.assertEqual((record["status"], record["post_merge"]["status"]), ("landed", "red"))
        post = json.loads((self.state / "caller.post-merge.json").read_text())
        self.assertEqual((post["base"], post["status"]), (before, "red"))
        self.assertIn("red", self.lane("list"))

    def test_a_red_smoke_names_a_lane_whose_receipt_ran_whole_suites(self):
        # Review of #422, finding 1: a receipt without per-check results (whole suites, or one from before them)
        # ran every check of its suites.
        rename = self.created("rename", "lib/**", "calls/main.txt")
        caller = self.created("caller", "calls/new.txt")
        self.commit(rename, {"lib/api.txt": "bar\n", "calls/main.txt": "bar\n"})
        self.commit(caller, {"calls/new.txt": "foo\n"})
        self.env["FIXTURE_SUITE_PLAN"] = "1"
        self.lane("check", "rename")
        self.assertNotIn("checks", self.receipt_of("rename")["results"][0])
        self.assertIn("post-merge green", self.lane("land", "rename"))
        # Its smoke ran per check; a post-merge receipt from before per-check results named whole suites.
        post = self.state / "rename.post-merge.json"
        old = json.loads(post.read_text())
        old["results"] = [{k: v for k, v in result.items() if k != "checks"} for result in old["results"]]
        post.write_text(json.dumps(old))
        del self.env["FIXTURE_SUITE_PLAN"]
        self.lane("check", "caller")
        output = self.lane("land", "caller", ok=False)
        self.assertIn("post-merge red", output)
        self.assertRegex(output.split("ran the failed checks:")[-1], r"\n  lib-check: caller \(#404\), rename \(#404\)")

    def receipt_of(self, lane_id):
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def test_the_post_merge_receipt_names_the_merge_tree_when_the_integration_moves_during_the_smoke(self):
        # #454: the receipt read the integration worktree's HEAD after the smoke, outside the landing lock.
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.integration_moves()
        hold = self.tmp / "hold-a"
        land = self.background_land("a", hold)
        smoke = self.wait_started(hold, land)
        self.commit(self.integration, {"moved.txt": "committed during the smoke\n"}, "moved during the smoke")
        hold.touch()
        output = land.communicate(timeout=60)[0]
        self.assertEqual(land.returncode, 0, output)
        merge = self.record("a")["landed"]["merge"]
        self.assertNotEqual(self.tip(), merge, "the integration worktree moved during the smoke")
        post = json.loads((self.state / "a.post-merge.json").read_text())
        self.assertEqual((post["merge"], post["tree"]), (merge, self.git(self.repo, "rev-parse", merge + "^{tree}")))
        # #573: the smoke ran in its own checkout of the merge, not in the integration worktree.
        self.assertNotEqual(os.path.realpath(smoke), os.path.realpath(self.integration))
        self.assertEqual(post["checkout"], smoke)

    def test_parallel_release_notes_entries_merge_by_union(self):
        notes = "release-notes/1.0.md"
        for lane_id in ("r1", "r2"):
            worktree = self.created(lane_id, f"src/{lane_id}/**")
            self.commit(worktree, {f"src/{lane_id}/x.txt": "x\n", notes: f"# 1.0\n\n- {lane_id} entry\n"})
            self.lane("check", lane_id)
        self.lane("land", "r1")
        self.lane("land", "r2")
        text = (self.integration / notes).read_text()
        self.assertIn("r1 entry", text)
        self.assertIn("r2 entry", text)

    def test_a_docs_only_lane_lands_with_an_empty_post_merge_plan(self):
        worktree = self.created("docs", "docs/**")
        self.git(worktree, "commit", "-q", "--allow-empty", "-m", "nothing to check")
        self.lane("check", "docs")
        self.assertIn("post-merge green", self.lane("land", "docs"))

    def stamps(self):
        ledger = self.repo / ".git" / "verified-trees"
        return [row.split()[0] for row in ledger.read_text().splitlines()] if ledger.exists() else []

    def merged_tree(self):
        return self.git(self.integration, "rev-parse", "HEAD^{tree}")

    def test_a_full_plan_lands_without_a_smoke_and_stamps_nothing(self):
        # Verification tooling selects the full plan. Since #446 the merge runs the smoke, and since #640 a merge
        # whose tree the lane's green receipt covers runs nothing more; the full gate runs once per batch at
        # lanefather promote.
        worktree = self.created("tool", "tooling/**")
        self.commit(worktree, {"tooling/runner.txt": "v2\n"})
        self.lane("check", "tool")
        # The lane check runs every check of the gate as a selection and stamps nothing; the lane tree is not what
        # ships.
        self.assertEqual((self.verify_runs()[-1], self.stamps()),
                         ("checks structure-check,lib-check,schema-check,native-check,host-journeys-check", []))
        output = self.lane("land", "tool")
        self.assertEqual(len(self.verify_runs()), 1)
        self.assertEqual(self.stamps(), [])
        self.assertNotIn("stamped", output)
        self.assertRegex((self.state / "ledger.log").read_text(),
                         r"post-merge tool green skip jobs=0 suites=- lane_verified=2 disk_free_gb=500 load15=0\.1")

    def test_selected_checks_never_stamp(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.integration_moves()
        self.assertNotIn("stamped", self.lane("land", "a"))
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        self.assertEqual(self.stamps(), [])

    def test_failed_checks_map_to_the_plan_modules_suites(self):
        lane = load_lane()
        with project(self.integration):
            self.assertEqual(lane.plan.suites_for_checks(self.integration, ["lib-check"], ["structure", "lib"]),
                             ["lib"])
            self.assertEqual(lane.plan.suites_for_checks(self.integration, ["native-check", "structure-check"],
                                                         ["structure", "native", "lib"]), ["structure", "native"])

    def test_the_smoke_comes_from_the_plan_module(self):
        lane = load_lane()
        with project(self.integration):
            self.assertEqual(lane.plan.smoke_checks(self.integration), ["structure-check", "lib-check"])
            self.assertEqual(lane.plan.known_checks(self.integration),
                             {"structure-check", "lib-check", "schema-check", "native-check", "host-journeys-check"})
        # A project without a plan module answers None, and lanefather falls back to whole suites.
        lane.config.use(lane.config.Config())
        self.addCleanup(lane.config.use, None)
        self.assertIsNone(lane.plan.smoke_checks(self.integration))


class OverlappingLandTests(LaneCase):
    """#573: land holds its lock only for the merge; each post-merge smoke runs on its own detached checkout of its
    merge, so lands overlap. 154 serial post-merge runs had taken about 59 h."""

    def checked(self, lane_id, path):
        worktree = self.created(lane_id, path)
        self.commit(worktree, {path: f"{lane_id}\n"})
        self.lane("check", lane_id)
        return worktree

    def test_two_lands_started_together_merge_one_after_the_other_and_smoke_at_the_same_time(self):
        self.checked("a", "lib/a.txt")
        self.checked("b", "lib/b.txt")
        self.integration_moves()
        hold_a, hold_b = self.tmp / "hold-a", self.tmp / "hold-b"
        land_a = self.background_land("a", hold_a)
        land_b = self.background_land("b", hold_b)
        smoke_a = self.wait_started(hold_a, land_a)
        smoke_b = self.wait_started(hold_b, land_b)
        # Both smokes are running, each in its own checkout of its own merge.
        self.assertNotEqual(smoke_a, smoke_b)
        merges = {lane_id: self.record(lane_id)["landed"]["merge"] for lane_id in ("a", "b")}
        self.assertEqual(self.git(smoke_a, "rev-parse", "HEAD"), merges["a"])
        self.assertEqual(self.git(smoke_b, "rev-parse", "HEAD"), merges["b"])
        # One merged after the other: the later merge's first parent is the earlier merge.
        later, earlier = self.git(self.integration, "log", "--first-parent", "--format=%H", "-2").split()
        self.assertEqual({later, earlier}, set(merges.values()))
        self.assertEqual(self.git(self.integration, "rev-parse", later + "^1"), earlier)
        hold_a.touch()
        hold_b.touch()
        for land in (land_a, land_b):
            output = land.communicate(timeout=60)[0]
            self.assertEqual(land.returncode, 0, output)
            self.assertIn("post-merge green", output)
        for lane_id in ("a", "b"):
            post = json.loads((self.state / f"{lane_id}.post-merge.json").read_text())
            self.assertEqual((post["merge"], post["status"]), (merges[lane_id], "green"))
        # The smoke checkouts stay for the next smokes' warm builds, detached and clean.
        self.assertEqual(self.git(smoke_a, "status", "--porcelain"), "")

    def test_a_red_smoke_names_its_merge_after_a_later_merge_landed(self):
        self.checked("a", "lib/a.txt")
        self.checked("b", "lib/b.txt")
        self.integration_moves()
        hold = self.tmp / "hold-a"
        land_a = self.background_land("a", hold, FIXTURE_STATUS="post-merge-a:lib-check=failed")
        self.wait_started(hold, land_a)
        merge_a = self.record("a")["landed"]["merge"]
        # The second land does not wait for the first one's smoke.
        self.assertIn("post-merge green", self.lane("land", "b", timeout=60))
        merge_b = self.record("b")["landed"]["merge"]
        self.assertEqual(self.tip(), merge_b)
        hold.touch()
        output = land_a.communicate(timeout=60)[0]
        self.assertEqual(land_a.returncode, 1, output)
        self.assertIn("post-merge red", output)
        self.assertIn(f"The merge {merge_a[:10]} stays in place, marked red", output)
        self.assertIn(f"has moved on to {merge_b[:10]}", output)
        post = json.loads((self.state / "a.post-merge.json").read_text())
        self.assertEqual((post["merge"], post["status"]), (merge_a, "red"))
        # Only lanes in the red merge touched its suites: b landed after it.
        touched = output.split("touched the failed suites:")[-1]
        self.assertIn("a (#404)", touched)
        self.assertNotIn("b (#404)", touched)
        record = self.record("a")
        self.assertEqual((record["status"], record["post_merge"]["status"]), ("landed", "red"))


class SmokeNarrowingTests(LaneCase):
    """#640: a post-merge smoke runs only what the merge changed beyond the tree the lane's green receipt verified.
    56 of 200 post-merge trees equalled their lane's receipt tree, and smokes took 71.5 h of receipt time."""

    def post(self, lane_id):
        return json.loads((self.state / f"{lane_id}.post-merge.json").read_text())

    def receipt(self, lane_id):
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def slotfather_calls(self):
        log = self.tmp / "slotfather.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_a_merge_whose_tree_the_lanes_green_receipt_covers_records_the_receipt_and_runs_nothing(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        receipt = self.receipt("a")
        calls = self.slotfather_calls()
        output = self.lane("land", "a")
        merge_tree = self.git(self.integration, "rev-parse", "HEAD^{tree}")
        self.assertEqual(merge_tree, receipt["tree"])
        self.assertIn(f"post-merge green (skipped: the lane's green receipt verified the merge tree {merge_tree[:10]})",
                      output)
        # Only the lane check ran; the smoke asked slotfather for no jobs.
        self.assertEqual(self.verify_runs(), ["checks structure-check,lib-check"])
        self.assertEqual(self.slotfather_calls(), calls)
        post = self.post("a")
        self.assertEqual((post["kind"], post["status"], post["suites"], post["selection"], post["jobs"]),
                         ("skip", "green", [], [], 0))
        self.assertEqual({k: post["narrowing"][k] for k in ("narrowed", "receipt_tree", "dropped", "integration_paths")},
                         {"narrowed": True, "receipt_tree": receipt["tree"], "dropped": ["structure-check", "lib-check"],
                          "integration_paths": []})
        # The receipt's results stand as the smoke's, naming the tree they ran on.
        self.assertEqual({c: (e["status"], e["reused_from"]) for c, e in post["check_results"].items()},
                         {"structure-check": ("passed", receipt["tree"]), "lib-check": ("passed", receipt["tree"])})
        self.assertRegex((self.state / "ledger.log").read_text(), r"post-merge a green skip jobs=0 suites=- lane_verified=2 ")
        self.assertEqual(self.record("a")["post_merge"]["status"], "green")

    def test_the_smoke_runs_what_the_integration_brought_in_beyond_the_lanes_tree(self):
        worktree = self.created("a", "calls/**")
        self.commit(worktree, {"calls/main.txt": "foo\n\n"})
        self.lane("check", "a")
        # After the check the integration takes a journey document: it selects the structure check, not lib-check.
        self.commit(self.integration, {"docs/install.md": "journey\n"}, "another lane landed")
        output = self.lane("land", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check")
        self.assertIn("post-merge green (narrowed smoke: structure-check; the lane's receipt stands for 1 more)", output)
        post = self.post("a")
        self.assertEqual(post["kind"], "narrowed")
        self.assertEqual(post["selection"], [{"id": "structure-check", "suite": "structure",
                                              "paths": ["docs/install.md"]}])
        self.assertEqual((post["narrowing"]["dropped"], post["narrowing"]["integration_paths"]),
                         (["lib-check"], ["docs/install.md"]))
        self.assertEqual(post["check_results"]["lib-check"]["reused_from"], self.receipt("a")["tree"])
        self.assertEqual(post["check_results"]["structure-check"]["status"], "passed")
        self.assertNotIn("reused_from", post["check_results"]["structure-check"])

    def test_a_fault_planted_on_the_integration_side_of_a_merge_turns_the_smoke_red(self):
        # The lane's tree is green; the integration, after the lane's check, takes a call to a name lib/api.txt does
        # not define. The merge's tree differs from the receipt's only by that file, and the smoke runs its readers.
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.commit(self.integration, {"calls/planted.txt": "missing\n"}, "planted fault")
        output = self.lane("land", "a", ok=False)
        self.assertIn("post-merge red", output)
        self.assertIn("lib: failed checks lib-check", output)
        self.assertIn("calls missing, which lib/api.txt does not define", pathlib.Path(
            self.post("a")["results"][-1]["log"]).read_text())
        post = self.post("a")
        self.assertEqual((post["kind"], post["status"]), ("narrowed", "red"))
        self.assertEqual({row["id"]: row["paths"] for row in post["selection"]},
                         {"structure-check": ["calls/planted.txt"], "lib-check": ["calls/planted.txt"]})
        self.assertEqual(post["check_results"]["lib-check"]["status"], "failed")

    def test_a_check_the_receipt_let_land_red_runs_again_on_the_merge(self):
        # lib-check failed under load and passed alone: the receipt is green, but it never showed lib-check passed on
        # the lane's tree, so the merge's own risk checks run it although the trees are equal.
        flag = self.tmp / "flake-once"
        flag.touch()
        self.env["FIXTURE_FLAKE_ONCE"] = f"lib-check:{flag}"
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.assertEqual(self.receipt("a")["check_results"]["lib-check"]["status"], "failed")
        output = self.lane("land", "a")
        self.assertEqual(self.verify_runs()[-1], "checks lib-check")
        self.assertIn("narrowed smoke: lib-check; the lane's receipt stands for 1 more", output)
        post = self.post("a")
        self.assertEqual([(row["id"], row["unverified"]) for row in post["selection"]], [("lib-check", "failed")])
        self.assertEqual(post["narrowing"]["dropped"], ["structure-check"])

    def test_a_smoke_check_the_lane_did_not_run_runs_on_the_merge(self):
        # Review of #640, finding 1: an earlier merge left native-check red on the integration; a lane that branched
        # after it and touches only lib/ never ran native-check, so its receipt cannot stand for it, although neither
        # the merged diff nor what the integration brought in selects it.
        self.env["FIXTURE_SMOKE"] = "structure-check,lib-check,native-check"
        self.commit(self.integration, {"native/straylight-core/corpus.txt": "v2\n"}, "an earlier merge broke native")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.assertNotIn("native-check", self.receipt("a")["check_results"])
        output = self.lane("land", "a", ok=False)
        self.assertEqual(self.verify_runs()[-1], "checks native-check")
        self.assertIn("post-merge red (narrowed smoke: native-check; the lane's receipt stands for 2 more)", output)
        post = self.post("a")
        self.assertEqual([(row["id"], row["paths"], row["unverified"]) for row in post["selection"]],
                         [("native-check", [], "not selected")])
        self.assertEqual(post["check_results"]["native-check"]["status"], "failed")
        # Review of #640, finding 4: the ledger counts the checks the receipt stands for, nothing it never ran.
        self.assertEqual(post["narrowing"]["dropped"], ["structure-check", "lib-check"])
        self.assertRegex((self.state / "ledger.log").read_text(), r"post-merge a red narrowed jobs=\d+ suites=native "
                                                                  r"lane_verified=2 ")

    def test_a_skip_on_another_tree_says_what_the_integration_brought_in(self):
        # Review of #640, finding 3: the integration brought in a path no smoke check reads; nothing runs, but the
        # receipt did not verify the merge tree.
        self.env["FIXTURE_SMOKE"] = "lib-check"
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.commit(self.integration, {"docs/install.md": "journey\n"}, "another lane landed")
        output = self.lane("land", "a")
        self.assertEqual(self.post("a")["kind"], "skip")
        self.assertNotIn("verified the merge tree", output)
        self.assertIn("post-merge green (skipped: what the integration brought in (docs/install.md) selects no smoke "
                      "check; the lane's green receipt stands for 1)", output)

    def test_a_runner_change_from_the_integration_runs_its_canary_checks_not_the_whole_smoke(self):
        # Review of #638, finding 3: since #638 the plan gives a runner change its canary checks, not the full gate;
        # tools/lane's plan consumers narrow around it.
        self.env["FIXTURE_SMOKE"] = "structure-check,lib-check,native-check"
        worktree = self.created("a", "native/**")
        self.commit(worktree, {"native/straylight-core/notes.txt": "lane\n"})
        self.lane("check", "a")
        self.commit(self.integration, {"runner/verify.txt": "v2\n"}, "the runner changed")
        output = self.lane("land", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        post = self.post("a")
        self.assertEqual((post["kind"], post["narrowing"]["narrowed"], post["narrowing"]["dropped"]),
                         ("narrowed", True, ["native-check"]))
        self.assertIn("narrowed smoke: structure-check, lib-check; the lane's receipt stands for 1 more", output)

    def test_a_receipt_for_another_tree_leaves_the_whole_smoke(self):
        worktree = self.created("a", "lib/**", "README.md")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.commit(worktree, {"README.md": "edited after the check\n"})
        output = self.lane("land", "a", "--force-reason", "replay a stale receipt")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        self.assertIn("smoke: structure-check, lib-check; whole, since the lane's receipt covers tree", output)
        post = self.post("a")
        self.assertEqual((post["kind"], post["narrowing"]["narrowed"]), ("smoke", False))
        self.assertIn("not the landed tree", post["narrowing"]["reason"])

    def test_verification_tooling_from_the_integration_leaves_the_whole_smoke(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.integration_moves()
        self.lane("land", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        narrowing = self.post("a")["narrowing"]
        self.assertEqual((narrowing["narrowed"], narrowing["integration_paths"]), (False, ["tooling/moved.txt"]))
        self.assertEqual(narrowing["reason"], "what the integration brought in selects the full gate")

    def test_a_changed_rust_toolchain_leaves_the_whole_smoke(self):
        # #571: a receipt certifies only the Rust it ran with.
        self.commit(self.integration, {"rust-toolchain.toml": '[toolchain]\nchannel = "1.98.1"\n',
                                       "tools/rust-toolchain.py": RUST_STUB}, "pin Rust")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.env["FIXTURE_RUST"] = "1.99.0"
        self.lane("land", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        self.assertEqual(self.post("a")["narrowing"]["reason"], "the Rust toolchain changed since the lane check")


if __name__ == "__main__":
    unittest.main()
