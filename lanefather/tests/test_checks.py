"""Tests of the lane check and its receipt (lanefather.checks, lanefather.host): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import time
from typing import Any
import unittest
import unittest.mock

from support import RUST_STUB, LaneCase, load_lane, project


class ClaimsFollowTheDiffTests(LaneCase):
    """#574: about 100 of 144 lane reports had files outside their claims, and the orchestrator widened claims by
    script before every land. check adds the lane's changed paths to its claims and warns only on an overlap with
    another planned or active lane."""

    def receipt(self, lane_id):
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def test_a_changed_path_no_other_lane_claims_joins_the_claims_without_a_warning(self):
        worktree = self.created("a", "lib/**")
        self.start("other", "docs/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n", "calls/extra.txt": "foo\n",
                               "release-notes/unreleased/a.md": "- a\n"})
        output = self.lane("check", "a")
        self.assertNotIn("warning", output)
        self.assertIn("claims widened: calls/extra.txt", output)
        # Release notes merge by union, so nobody claims them.
        self.assertEqual(self.record("a")["claims"], ["lib/**", "calls/extra.txt"])
        receipt = self.receipt("a")
        self.assertEqual((receipt["claims_added"], receipt["claim_overlaps"]), (["calls/extra.txt"], []))
        self.assertIn("claims a + calls/extra.txt", (self.state / "ledger.log").read_text())
        # The widened claim now holds: a new lane on that path waits for this one.
        self.assertIn("claims overlap lane a", self.start("late", "calls/**", ok=False))
        # A second check adds nothing new.
        self.commit(worktree, {"lib/api.txt": "foo\nqux\n"})
        self.assertNotIn("claims widened", self.lane("check", "a"))
        self.assertEqual(self.record("a")["claims"], ["lib/**", "calls/extra.txt"])

    def test_a_changed_path_another_active_lane_claims_warns_naming_that_lane(self):
        worktree = self.created("a", "lib/**")
        self.created("calls-lane", "calls/**")
        self.start("planned-docs", "docs/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n", "calls/extra.txt": "foo\n", "docs/install.md": "x\n"})
        output = self.lane("check", "a")
        self.assertIn("lane: warning: calls/extra.txt overlaps the claims of lane calls-lane (#404): calls/**", output)
        self.assertIn("lane: warning: docs/install.md overlaps the claims of lane planned-docs (#404): docs/**", output)
        self.assertEqual(self.record("a")["claims"], ["lib/**", "calls/extra.txt", "docs/install.md"])
        self.assertEqual(self.receipt("a")["claim_overlaps"], [
            {"path": "calls/extra.txt", "lane": "calls-lane", "ticket": "404", "claim": "calls/**"},
            {"path": "docs/install.md", "lane": "planned-docs", "ticket": "404", "claim": "docs/**"}])

    def test_a_declared_sequence_or_a_landed_lane_does_not_warn(self):
        first = self.created("first", "lib/**")
        self.commit(first, {"lib/api.txt": "foo\nfirst\n"})
        self.lane("check", "first")
        self.start("second", "calls/**", extra=("--after", "first", "--create"))
        second = self.record("second")["worktree"]
        self.commit(second, {"calls/main.txt": "foo\n\n", "lib/second.txt": "x\n"})
        self.assertNotIn("warning", self.lane("check", "second"))
        self.lane("land", "first")
        third = self.created("third", "native/**")
        self.commit(third, {"native/straylight-core/notes.txt": "x\n", "lib/third.txt": "x\n"})
        output = self.lane("check", "third")
        # first landed: it holds no claims; second is active and claims lib/second.txt only.
        self.assertNotIn("warning", output)


class NewestMergeBaseTests(LaneCase):
    """lanefather#3: a lane registered at a literal commit keeps it after it merges the integration branch, and
    check then planned and widened claims from everything the integration gained since (Straylight 515: 148 files
    instead of 10, the full gate, and false overlap warnings)."""

    def queue(self, base: str) -> None:
        path = self.state.parent / "queue" / "queue.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'base = "{base}"\n')

    def literal_lane(self, lane_id: str, claim: str) -> str:
        self.lane("start", lane_id, "--ticket", "404", "--base", self.tip(), "--claims", claim, "--create")
        return self.record(lane_id)["worktree"]

    def test_a_literal_base_lane_that_merged_the_integration_branch_plans_against_its_tip(self):
        self.queue("integrate/q")
        worktree = self.literal_lane("a", "lib/**")
        self.created("other", "unowned/**")
        # The integration gains a path no check reads (the full gate) and one another lane claims.
        moved = self.commit(self.integration, {"unowned/arrived.txt": "x\n"}, "the integration moves on")
        self.git(worktree, "merge", "-q", "--no-edit", "integrate/q")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        output = self.lane("check", "a")
        receipt = json.loads((self.state / "a.receipt.json").read_text())
        self.assertEqual(receipt["base"], moved)
        self.assertFalse(receipt["full"], output)
        self.assertEqual((receipt["outside_claims"], receipt["claim_overlaps"]), ([], []))
        self.assertNotIn("warning", output)
        self.assertEqual(self.record("a")["claims"], ["lib/**"])
        self.assertEqual(self.record("a")["base"], self.record("a")["base_sha"], "the registration stays as it was")

    def test_a_malformed_queue_entry_does_not_drop_the_queue_base(self):
        # Review of lanefather#3: the queue's base is a top-level setting; an entry next for its own reasons
        # refuses must not silently change what check plans against.
        self.queue("integrate/q")
        path = self.state.parent / "queue" / "queue.toml"
        path.write_text(path.read_text() + '[[lane]]\nticket = 1\nid = "bad id"\nclaims = ["a/**"]\n')
        worktree = self.literal_lane("a", "lib/**")
        moved = self.commit(self.integration, {"unowned/arrived.txt": "x\n"}, "the integration moves on")
        self.git(worktree, "merge", "-q", "--no-edit", "integrate/q")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.assertEqual(json.loads((self.state / "a.receipt.json").read_text())["base"], moved)

    def test_an_unreadable_queue_warns_that_check_plans_without_its_base(self):
        self.queue("integrate/q")
        (self.state.parent / "queue" / "queue.toml").write_text("base = \n")
        worktree = self.literal_lane("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.assertIn("lane: warning: ", self.lane("check", "a"))

    def test_a_lane_that_did_not_merge_the_integration_branch_keeps_its_own_base(self):
        self.queue("integrate/q")
        worktree = self.literal_lane("a", "lib/**")
        base = self.record("a")["base_sha"]
        self.commit(self.integration, {"unowned/arrived.txt": "x\n"}, "the integration moves on")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.assertEqual(json.loads((self.state / "a.receipt.json").read_text())["base"], base)


    def test_a_lane_based_on_another_branch_that_merges_the_queue_branch_keeps_its_own_base(self):
        # Review of lanefather#3: the queue's base is not the branch every lane lands into. A dev-based lane that
        # merges integrate/q would plan against that merge and land integrate/q's commits into dev un-gated.
        self.queue("integrate/q")
        self.git(self.repo, "branch", "dev")
        self.lane("start", "a", "--ticket", "404", "--base", "dev", "--claims", "lib/**", "--create")
        worktree = self.record("a")["worktree"]
        dev = self.git(self.repo, "rev-parse", "dev")
        self.commit(self.integration, {"unowned/arrived.txt": "x\n"}, "the integration moves on")
        self.git(worktree, "merge", "-q", "--no-edit", "integrate/q")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        receipt = json.loads((self.state / "a.receipt.json").read_text())
        self.assertEqual(receipt["base"], dev)
        self.assertTrue(receipt["full"], "what the integration brought in is checked against dev")


class LoadThrottleTests(LaneCase):
    """#572: every result records the load at its verify run's start and end. Owner decision on the #572 review: the
    load wait is off by default; with LANEFATHER_LOAD_THROTTLE=1 a verify run of check, land or promote starts only while
    load15 is at or below the local advisory limit (1.5 x cores), and the result records that wait."""

    def receipt(self, name):
        return json.loads((self.state / f"{name}.json").read_text())

    def test_by_default_a_loaded_machine_runs_at_once_and_the_receipt_records_the_load(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        # A run that waited for the load would wait the whole ten minutes: the bound only bounds machine load.
        self.env.update(LANEFATHER_LOAD15="12.5", LANEFATHER_LOAD_WAIT="600", LANEFATHER_LOAD_POLL="0.1")
        started = time.monotonic()
        output = self.lane("check", "a")
        self.assertLess(time.monotonic() - started, 300)
        self.assertNotIn("waits", output)
        for result in self.receipt("a.receipt")["results"]:
            self.assertEqual(result["load_wait"], {"enabled": False, "limit": 9.0, "load15_before": 12.5,
                                                   "load15": 12.5, "seconds": 0.0})
            self.assertEqual((result["load"]["start"][2], result["load"]["end"][2]), (12.5, 12.5))
        self.assertNotIn("load-wait", (self.state / "ledger.log").read_text())

    def test_check_and_land_wait_while_load15_is_above_the_limit_and_the_receipts_show_it(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        # 6 cores: the limit is 9. The load never drops here, so each run waits its whole patience and starts.
        self.env.update(LANEFATHER_LOAD_THROTTLE="1", LANEFATHER_LOAD15="12.5", LANEFATHER_LOAD_WAIT="0.4",
                        LANEFATHER_LOAD_POLL="0.1")
        output = self.lane("check", "a")
        self.assertIn("load15 12.5 is above the limit 9.0", output)
        for result in self.receipt("a.receipt")["results"]:
            wait = result["load_wait"]
            self.assertGreaterEqual(wait["seconds"], 0.4)
            self.assertEqual((wait["limit"], wait["load15_before"], wait["load15"], wait["gave_up"]),
                             (9.0, 12.5, 12.5, True))
            self.assertEqual([len(result["load"][when]) for when in ("start", "end")], [3, 3])
            self.assertEqual(result["load"]["start"][2], 12.5)
        self.integration_moves()
        self.lane("land", "a")
        [result] = [r for r in self.receipt("a.post-merge")["results"] if r["suite"] == "lib"]
        self.assertTrue(result["load_wait"]["gave_up"])
        ledger = (self.state / "ledger.log").read_text()
        self.assertRegex(ledger, r"load-wait lane-a seconds=0\.\d load15=12\.5->12\.5 limit=9\.0 gave up")
        self.assertRegex(ledger, r"load-wait post-merge-a seconds=0\.\d")

    def test_under_the_limit_nothing_waits_and_the_load_is_still_recorded(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        for result in self.receipt("a.receipt")["results"]:
            self.assertEqual(result["load_wait"], {"enabled": False, "limit": 9.0, "load15_before": 0.1,
                                                   "load15": 0.1, "seconds": 0.0})
            self.assertEqual(result["load"]["end"][2], 0.1)
        self.assertNotIn("load-wait", (self.state / "ledger.log").read_text())

    def in_process(self, **env):
        lane = load_lane()
        patcher = unittest.mock.patch.dict(os.environ, {**self.env, "LANEFATHER_LOAD_THROTTLE": "1",
                                                        "LANEFATHER_LOAD_POLL": "0.01", **env}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.enterContext(project(self.integration))
        return lane

    def test_a_run_starts_once_load15_drops_under_the_limit(self):
        lane = self.in_process(LANEFATHER_LOAD_POLL="0.06")
        loads = iter([20.0, 12.0, 8.9])
        with unittest.mock.patch.object(lane.host, "load15", side_effect=lambda: next(loads)), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            wait = lane.host.wait_for_load("lane-a")
        self.assertIn("lane-a starts after 0 s at load15 8.9", printed.getvalue())
        self.assertEqual((wait["load15_before"], wait["load15"], wait["limit"]), (20.0, 8.9, 9.0))
        self.assertNotIn("gave_up", wait)
        self.assertGreater(wait["seconds"], 0)

    def test_a_run_under_a_lease_it_already_holds_does_not_wait(self):
        # #568's rerun alone holds every slot: a wait there would hold them idle while the load can only drop.
        lane = self.in_process(SLOTFATHER_LEASE=str(self.tmp / "lease"), LANEFATHER_LOAD15="30")
        wait = lane.host.wait_for_load("lane-a-alone")
        self.assertEqual(wait["seconds"], 0.0)
        self.assertIn("slot lease", wait["skipped"])

    def test_the_batch_gate_sizes_its_jobs_after_the_load_wait(self):
        """Review of #599: the gate's job count comes from the load once its wait is over, not before it."""
        tip = self.tip()
        common = self.git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        lane = self.in_process(SLOTFATHER_LOADAVG="12")

        def wait(label):
            os.environ["SLOTFATHER_LOADAVG"] = "0.1"  # the load dropped while the gate waited
            return {"enabled": True, "limit": 9.0, "load15_before": 12.0, "load15": 0.1, "seconds": 1.0}
        with unittest.mock.patch.object(lane.host, "wait_for_load", side_effect=wait), \
                contextlib.redirect_stdout(io.StringIO()):
            lane.promote.run_batch_gate(str(self.repo), common, tip, None)
        decision = self.receipt(f"promote-{tip[:10]}.gate")["jobs_decision"]
        self.assertEqual((decision["load"], decision["jobs"]), (0.1, 2))

    def test_the_batch_gate_waits_for_load_too(self):
        self.git(self.repo, "branch", "dev")
        self.env.update(LANEFATHER_LOAD_THROTTLE="1", LANEFATHER_LOAD15="12.5", LANEFATHER_LOAD_WAIT="0.2",
                        LANEFATHER_LOAD_POLL="0.1")
        tip = self.tip()
        self.lane("promote", tip, "--no-push")
        gate = self.receipt(f"promote-{tip[:10]}.gate")
        self.assertTrue(gate["load_wait"]["gave_up"])
        self.assertEqual(gate["results"][0]["load_wait"], gate["load_wait"])


class RedCheckClassificationTests(LaneCase):
    """#568: a red lane check reruns each failed check by itself on the lane and on the lane's base, and says why
    it failed: regression, pre-existing or flaky-under-load. Replays #551, whose agent called a regression load.
    lanefather#4: each rerun runs at the check's own lease size (at most 2 slots) at interactive priority, not under
    a lease of the whole machine that never came within its wait; the base reruns only after the lane's rerun is
    red, never beside it."""

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def entries(self, lane_id: str) -> dict[str, dict[str, Any]]:
        return {entry["check"]: entry for entry in self.receipt(lane_id)["classification"]["checks"]}

    def runs(self) -> list[tuple[str, ...]]:
        """Each verify run as (label, commit, branch or detached, checkout, selection)."""
        path = pathlib.Path(str(self.verify_log) + ".runs")
        return [tuple(line.split(" ", 4)) for line in path.read_text().splitlines()] if path.exists() else []

    def base_checkouts(self, lane_id):
        scratch = pathlib.Path(self.record(lane_id)["scratch"][0])
        listed = [line for line in self.git(self.repo, "worktree", "list", "--porcelain").splitlines()
                  if line.startswith("worktree ") and "base-" in line]
        return listed + [str(p) for p in scratch.glob("base-*")]

    def test_a_check_the_lane_breaks_and_its_base_passes_is_a_regression_that_land_refuses(self):
        worktree = self.created("core", "native/**")
        base = self.tip()
        self.commit(worktree, {"native/straylight-core/corpus.txt": "v2\n"})
        head = self.git(worktree, "rev-parse", "HEAD")
        output = self.lane("check", "core", ok=False)
        runs = [(label, commit, where, selection) for label, commit, where, _, selection in self.runs()]
        self.assertEqual(runs[0], ("lane-core", head, "branch", "checks structure-check,native-check"))
        # The lane reruns the red check, then, red again, its base does.
        self.assertEqual(runs[1:], [("lane-core-alone", head, "branch", "checks native-check"),
                                    ("lane-core-base", base, "detached", "checks native-check")])
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        # Each rerun holds the check's own lease size (the lane check's 2 jobs here), never the whole machine.
        self.assertEqual(leases[0], "lane-core --jobs 2")
        self.assertEqual(leases[1:], ["lane-core-alone --jobs 2 held 2", "lane-core-base --jobs 2 held 2"])
        requests = [json.loads(line) for line in pathlib.Path(str(self.verify_log) + ".requests").read_text().splitlines()]
        self.assertEqual([("normal", "404"), ("interactive", "404"), ("interactive", "404")],
                         [(entry["priority"], entry["ticket"]) for entry in requests])
        self.assertTrue(all(not ({"class", "priority", "ticket"} & set(entry["meta"])) for entry in requests))
        queued = [json.loads(line)["args"] for line in (self.tmp / "slotfather.log").read_text().splitlines()
                  if json.loads(line)["command"] == "run"]
        self.assertEqual(["404", "404"], [argv[argv.index("--ticket") + 1] for argv in queued])
        entry = self.entries("core")["native-check"]
        self.assertEqual(entry["class"], "regression")
        self.assertEqual(entry["base"]["sha"], base)
        self.assertEqual(entry["base"]["status"], "passed")
        self.assertEqual(self.receipt("core")["status"], "red")
        self.assertIn(f"native-check: regression: fails on the lane again when rerun by itself, the base {base[:10]} "
                      "passes it", output)
        # The disposable base checkout is gone.
        self.assertEqual(self.base_checkouts("core"), [])
        refusal = self.lane("land", "core", ok=False)
        self.assertIn("regression: native-check fails on the lane again when rerun by itself and passes on its base",
                      refusal)

    def test_a_check_its_base_fails_too_is_pre_existing_and_names_the_base_failure(self):
        # The integration branch already breaks the frozen corpus; the lane changes another native file.
        self.commit(self.integration, {"native/straylight-core/corpus.txt": "v2\n"}, "the base breaks the corpus")
        base = self.tip()
        worktree = self.created("core", "native/**")
        self.commit(worktree, {"native/straylight-core/notes.txt": "lane\n"})
        output = self.lane("check", "core")
        entry = self.entries("core")["native-check"]
        self.assertEqual(entry["class"], "pre-existing")
        self.assertEqual(entry["base"]["sha"], base)
        self.assertEqual(entry["base"]["status"], "failed")
        # The base run's evidence outlives its disposable checkout.
        self.assertIn("checks/frozen-corpus.txt", pathlib.Path(entry["base"]["log"]).read_text())
        self.assertIn(f"native-check: pre-existing: the base {base[:10]} fails it too", output)
        self.assertEqual(self.receipt("core")["status"], "green")
        self.assertEqual(self.base_checkouts("core"), [])
        self.lane("land", "core")

    def break_structure(self, base_too=False):
        """The structure check (the fixture's cheap tier, #639) fails while lib/structure-broken exists."""
        files = {"checks/structure.sh": "test ! -e lib/structure-broken\n"}
        self.commit(self.integration, dict(files, **({"lib/structure-broken": "base\n"} if base_too else {})),
                    "a structure rule")

    def test_a_red_first_check_stops_the_lane_check_before_any_heavier_check(self):
        """#639: the lane check stops when its cheap tier is red and says so; nothing heavier starts, and the red
        check reruns only on the base, as verify.sh queues it, never alone holding the machine."""
        self.break_structure()
        base = self.tip()
        worktree = self.created("cheap", "lib/**")
        self.commit(worktree, {"lib/structure-broken": "lane\n", "lib/thing.txt": "lane\n"})
        head = self.git(worktree, "rev-parse", "HEAD")
        output = self.lane("check", "cheap", ok=False)
        self.assertEqual([(label, commit, where, selection) for label, commit, where, _, selection in self.runs()], [
            ("lane-cheap", head, "branch", "checks structure-check,lib-check"),
            ("lane-cheap-base", base, "detached", "checks structure-check"),
        ])
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        self.assertEqual(leases[1:], ["lane-cheap-base --jobs 1"])
        receipt = self.receipt("cheap")
        self.assertEqual((receipt["status"], receipt["stopped"]),
                         ("red", {"failed": ["structure-check"], "not_run": 1}))
        self.assertEqual({c: e["status"] for c, e in receipt["check_results"].items()},
                         {"structure-check": "failed", "lib-check": "not_run"})
        self.assertEqual({r["suite"]: r["status"] for r in receipt["results"]},
                         {"structure": "failed", "lib": "not_run"})
        self.assertEqual(self.entries("cheap")["structure-check"]["class"], "regression")
        self.assertIn("lane: stopped at the first checks (#639): structure-check red, so the 1 heavier checks did "
                      "not start", output)
        self.assertIn(f"structure-check: regression: fails on the lane, the base {base[:10]} passes it", output)
        # Land names the red suite and the one that did not start apart; the first check never reran alone.
        refusal = self.lane("land", "cheap", ok=False)
        self.assertIn("is red (structure; not run: lib)", refusal)
        self.assertIn("regression: structure-check fails on the lane and passes on its base", refusal)
        # The fix reuses nothing the stop left unrun.
        (pathlib.Path(worktree) / "lib/structure-broken").unlink()
        self.commit(worktree, {})
        self.lane("check", "cheap")
        self.assertEqual(self.runs()[-1][4], "checks structure-check,lib-check")
        self.assertEqual(self.receipt("cheap")["status"], "green")

    def test_a_red_first_check_the_base_fails_too_runs_the_heavier_checks(self):
        """#639: a pre-existing red first check does not block the lane: its heavier checks run and decide."""
        self.break_structure(base_too=True)
        worktree = self.created("cheap", "lib/**")
        self.commit(worktree, {"lib/thing.txt": "lane\n"})
        head = self.git(worktree, "rev-parse", "HEAD")
        output = self.lane("check", "cheap")
        self.assertEqual([(label, selection) for label, _, _, _, selection in self.runs()], [
            ("lane-cheap", "checks structure-check,lib-check"), ("lane-cheap-base", "checks structure-check"),
            ("lane-cheap", "checks lib-check")])
        receipt = self.receipt("cheap")
        self.assertEqual((receipt["status"], receipt["stopped"], receipt["head"]), ("green", None, head))
        self.assertEqual({c: e["status"] for c, e in receipt["check_results"].items()},
                         {"structure-check": "failed", "lib-check": "passed"})
        self.assertEqual(self.entries("cheap")["structure-check"]["class"], "pre-existing")
        self.assertIn("running the heavier checks", output)

    def test_a_check_that_passes_alone_is_flaky_under_load_with_the_load_averages(self):
        flag = self.tmp / "flake-once"
        flag.touch()
        self.env["FIXTURE_FLAKE_ONCE"] = f"lib-check:{flag}"
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        output = self.lane("check", "a")
        # Green on its rerun: the base run would add nothing.
        self.assertEqual([run[0] for run in self.runs()], ["lane-a", "lane-a-alone"])
        entry = self.entries("a")["lib-check"]
        self.assertEqual(entry["class"], "flaky-under-load")
        self.assertEqual(sorted(entry["load"]), ["alone", "first_run"])
        self.assertTrue(all(len(entry["load"][when]) == 3 for when in entry["load"]))
        self.assertIn("lib-check: flaky-under-load", output)
        self.assertEqual(self.receipt("a")["status"], "green")
        self.lane("land", "a")

    def test_only_the_first_red_checks_rerun_and_the_rest_stay_unclassified_and_red(self):
        self.env["LANEFATHER_RERUNS"] = "1"
        worktree = self.created("a", "calls/**", "native/**")
        self.commit(worktree, {"calls/main.txt": "foo\nbar\n", "native/straylight-core/corpus.txt": "v2\n"})
        self.lane("check", "a", ok=False)
        self.assertEqual([run[4] for run in self.runs()][1:], ["checks lib-check", "checks lib-check"])
        entries = self.entries("a")
        self.assertEqual({check: entry["class"] for check, entry in entries.items()},
                         {"lib-check": "regression", "native-check": "unclassified"})
        self.assertIn("only the first 1", entries["native-check"]["reason"])
        self.assertEqual(self.receipt("a")["status"], "red")

    def test_the_base_checkout_is_removed_when_the_check_is_terminated(self):
        hold = self.tmp / "hold"
        self.env["FIXTURE_DETACHED_HOLD"] = str(hold)
        worktree = self.created("core", "native/**")
        self.commit(worktree, {"native/straylight-core/corpus.txt": "v2\n"})
        check = subprocess.Popen([sys.executable, "-m", "lanefather", "check", "core"], cwd=self.integration, env=self.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(hold.touch)
        started = pathlib.Path(str(hold) + ".started")
        deadline = time.monotonic() + 60
        while not started.exists():
            self.assertLess(time.monotonic(), deadline, "the base run did not start")
            self.assertIsNone(check.poll(), "the check exited before its base run")
            time.sleep(0.05)
        self.assertEqual(len(self.base_checkouts("core")), 2)
        check.terminate()
        self.assertNotEqual(check.wait(timeout=60), 0)
        self.assertEqual(self.base_checkouts("core"), [])

    def test_an_environment_failed_run_is_not_classified(self):
        self.env["FIXTURE_ENVIRONMENT"] = "toolchain changed"
        worktree = self.created("core", "native/**")
        self.commit(worktree, {"native/straylight-core/notes.txt": "lane\n"})
        output = self.lane("check", "core", ok=False)
        self.assertEqual(len(self.runs()), 1)
        self.assertIn("environment", self.receipt("core")["classification"]["skipped"])
        self.assertIn("not classified", output)

    def test_a_test_the_lane_changed_after_its_first_receipt_was_red_shows_in_the_report(self):
        worktree = self.created("a", "calls/**", "lib/**")
        self.commit(worktree, {"calls/test-calls.txt": "bar\n"})
        self.lane("check", "a", ok=False)
        self.assertEqual(self.entries("a")["lib-check"]["class"], "regression")
        # The agent edits the red test instead of the code it tests.
        self.commit(worktree, {"calls/test-calls.txt": "foo\n"}, "relax the test")
        output = self.lane("check", "a")
        receipt = self.receipt("a")
        self.assertEqual(receipt["first"]["failed_checks"], ["lib-check"])
        self.assertEqual(receipt["changed_red_tests"], [{"path": "calls/test-calls.txt", "check": "lib-check"}])
        self.assertIn("test files the lane changed whose check was red in its first receipt: "
                      "calls/test-calls.txt (lib-check)", output)
        # A later green check keeps the lane's first receipt.
        self.commit(worktree, {"lib/api.txt": "foo\nqux\n"})
        self.lane("check", "a")
        self.assertEqual(self.receipt("a")["first"]["failed_checks"], ["lib-check"])
        self.assertIn("calls/test-calls.txt", self.lane("land", "a"))

    def test_amending_the_red_commit_keeps_the_first_receipt(self):
        # Review of #568: an amend left the first head unreachable, and a new first receipt replaced it.
        worktree = self.created("a", "calls/**", "lib/**")
        self.commit(worktree, {"calls/test-calls.txt": "bar\n"})
        self.lane("check", "a", ok=False)
        first = self.receipt("a")["first"]
        self.write(worktree, "calls/test-calls.txt", "foo\n")
        self.git(worktree, "commit", "-qa", "--amend", "--no-edit")
        self.lane("check", "a")
        receipt = self.receipt("a")
        self.assertEqual(receipt["first"], first)
        self.assertEqual(receipt["changed_red_tests"], [{"path": "calls/test-calls.txt", "check": "lib-check"}])

    def test_a_test_changed_before_the_first_check_is_named_against_the_base(self):
        # Review of #568: the lane changes the test, its first check is red, and the code fix leaves the test as is.
        worktree = self.created("a", "calls/**", "lib/**")
        self.commit(worktree, {"calls/test-calls.txt": "bar\n"})
        self.lane("check", "a", ok=False)
        self.commit(worktree, {"lib/api.txt": "foo\nbar\n"}, "fix the code")
        output = self.lane("check", "a")
        self.assertEqual(self.receipt("a")["changed_red_tests"],
                         [{"path": "calls/test-calls.txt", "check": "lib-check"}])
        self.assertIn("calls/test-calls.txt (lib-check)", output)

    def red_core(self, **env: str) -> str:
        self.env.update(env)
        worktree = self.created("core", "native/**")
        self.commit(worktree, {"native/straylight-core/corpus.txt": "v2\n"})
        return self.lane("check", "core", ok=False)

    def test_a_base_that_did_not_run_the_check_leaves_it_unclassified_and_red(self):
        # Review of #568: only a failure on the base is pre-existing; a check blocked by a failed producer there
        # never ran.
        output = self.red_core(FIXTURE_STATUS="lane-core-base:native-check=blocked")
        entry = self.entries("core")["native-check"]
        self.assertEqual(entry["class"], "unclassified")
        self.assertIn("the base did not run it (blocked by fixture-build)", entry["reason"])
        self.assertEqual(self.receipt("core")["status"], "red")
        self.assertIn("native-check: unclassified", output)

    def test_an_unrun_base_result_leaves_it_unclassified(self):
        self.red_core(FIXTURE_STATUS="lane-core-base:native-check=unrun")
        self.assertEqual(self.entries("core")["native-check"]["class"], "unclassified")
        self.assertEqual(self.receipt("core")["status"], "red")

    def test_a_rerun_alone_that_did_not_run_the_check_leaves_it_unclassified(self):
        self.red_core(FIXTURE_STATUS="lane-core-alone:native-check=unrun")
        entry = self.entries("core")["native-check"]
        self.assertEqual(entry["class"], "unclassified")
        self.assertIn("the lane's rerun did not run it (unrun)", entry["reason"])
        # Without a result on the lane there is nothing to compare with the base.
        self.assertEqual([run[0] for run in self.runs()], ["lane-core", "lane-core-alone"])

    def test_a_rerun_asks_for_the_checks_own_lease_size_and_a_timeout_leaves_it_unclassified(self):
        # lanefather#4: reruns under a lease of all six slots waited 3301 s and never ran (Straylight 515, 644),
        # and as the queue head held one-slot lane checks back. A rerun asks for its own lease size with a wait;
        # here the machine stays busy and slotfather gives up (exit 75).
        self.env.update(FAKE_SLOTFATHER_BUSY="1")
        output = self.red_core(LANEFATHER_RERUN_WAIT="1")
        entry = self.entries("core")["native-check"]
        self.assertEqual(entry["class"], "unclassified")
        self.assertIn("no lease of 2 slot(s) within 1 s", entry["reason"])
        self.assertEqual(self.receipt("core")["status"], "red")
        self.assertEqual([run[0] for run in self.runs()], ["lane-core"])
        runs = [json.loads(line)["args"] for line in (self.tmp / "slotfather.log").read_text().splitlines()
                if json.loads(line)["command"] == "run"]
        # The lane's rerun timed out: nothing to compare, so the base never asked.
        self.assertEqual([argv[:4] for argv in runs], [["-n", "2", "--label", "lane-core-alone"]])
        for argv in runs:
            lease = argv[:argv.index("--")]
            self.assertEqual(("1", "interactive"),
                             (lease[lease.index("--wait") + 1], lease[lease.index("--priority") + 1]))
            self.assertIn("kind=rerun", lease)
        self.assertIn("native-check: unclassified", output)

    def test_the_environment_sets_the_check_and_rerun_priorities(self):
        # lanefather#24: an orchestrator demotes its runs to batch while another project measures.
        self.red_core(LANEFATHER_PRIORITY="batch", LANEFATHER_PRIORITY_RERUN="normal")
        requests = [json.loads(line) for line in pathlib.Path(str(self.verify_log) + ".requests").read_text().splitlines()]
        self.assertEqual([(entry["priority"], entry["meta"]["lanefather.priority"]) for entry in requests],
                         [("batch", "batch (LANEFATHER_PRIORITY)"),
                          ("normal", "normal (LANEFATHER_PRIORITY_RERUN)"),
                          ("normal", "normal (LANEFATHER_PRIORITY_RERUN)")])
        queued = [json.loads(line)["args"] for line in (self.tmp / "slotfather.log").read_text().splitlines()
                  if json.loads(line)["command"] == "run"]
        self.assertEqual([argv[argv.index("--priority") + 1] for argv in queued], ["normal", "normal"])

    def test_a_rerun_is_one_slot_when_the_lane_check_had_one_and_never_more_than_two(self):
        self.red_core(FAKE_SLOTFATHER_HELD="3")
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        self.assertEqual(leases[0], "lane-core --jobs 1")
        self.assertEqual(leases[1:], ["lane-core-alone --jobs 1 held 1", "lane-core-base --jobs 1 held 1"])
        self.lane("check", "core", "--rerun", "--slots", "4", ok=False)
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        self.assertEqual(leases[4:], ["lane-core-alone --jobs 2 held 2", "lane-core-base --jobs 2 held 2"])

    def test_a_regression_in_a_check_two_copies_cannot_share_stays_a_regression(self):
        # Review of lanefather#4: a check with a fixed port, /tmp path or launchd label fails when two copies run at
        # once (Straylight's test-dial-plugin port race). A base rerun beside the lane's would fail on the collision
        # and call the lane's regression pre-existing. The base reruns only after the lane's rerun is red.
        lock = self.tmp / "exclusive"
        self.env["FIXTURE_EXCLUSIVE"] = str(lock)
        self.commit(self.integration, {"checks/native.sh": textwrap.dedent("""\
            mkdir "$FIXTURE_EXCLUSIVE" 2>/dev/null || { echo "another copy holds $FIXTURE_EXCLUSIVE"; exit 1; }
            sleep 1
            rmdir "$FIXTURE_EXCLUSIVE"
            cmp native/straylight-core/corpus.txt checks/frozen-corpus.txt
            """)}, "a check two copies cannot share")
        worktree = self.created("core", "native/**")
        self.commit(worktree, {"native/straylight-core/corpus.txt": "v2\n"})
        self.lane("check", "core", ok=False)
        entry = self.entries("core")["native-check"]
        self.assertEqual(entry["class"], "regression", entry)
        self.assertEqual(entry["base"]["status"], "passed")
        self.assertEqual([run[0] for run in self.runs()], ["lane-core", "lane-core-alone", "lane-core-base"])

    def test_without_slotfather_the_reruns_run_without_a_lease_and_classify(self):
        # Without slotfather no rerun can wait for a lease; the lane and then its base rerun as the lane check ran,
        # under no lease, and their comparison classifies the check.
        (self.tmp / "bin" / "slotfather").unlink()
        path = [d for d in self.env["PATH"].split(os.pathsep) if d and not os.path.exists(os.path.join(d, "slotfather"))]
        output = self.red_core(PATH=os.pathsep.join(path))
        self.assertEqual(self.entries("core")["native-check"]["class"], "regression")
        self.assertEqual([run[0] for run in self.runs()], ["lane-core", "lane-core-alone", "lane-core-base"])
        self.assertEqual(self.receipt("core")["status"], "red")
        self.assertIn("native-check: regression", output)

    def test_under_an_outer_lease_the_reruns_run_inside_it_one_after_another(self):
        # lanefather itself under `slotfather run -n 3`: verify and the reruns take no second lease.
        output = self.red_core(SLOTFATHER_LEASE=str(self.tmp / "outer"), SLOTFATHER_COUNT="3")
        self.assertEqual(self.entries("core")["native-check"]["class"], "regression")
        self.assertEqual([run[0] for run in self.runs()], ["lane-core", "lane-core-alone", "lane-core-base"])
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        self.assertTrue(all(line.endswith(" held 3") for line in leases), leases)
        queued = [json.loads(line) for line in (self.tmp / "slotfather.log").read_text().splitlines()]
        self.assertEqual([entry for entry in queued if entry["command"] == "run"], [])
        self.assertIn("native-check: regression", output)

    def test_a_lane_whose_first_receipt_was_green_lists_no_tests(self):
        worktree = self.created("a", "calls/**")
        self.commit(worktree, {"calls/test-calls.txt": "foo\n"})
        self.lane("check", "a")
        self.commit(worktree, {"calls/test-calls.txt": "foo\nfoo\n"})
        self.lane("check", "a")
        self.assertEqual(self.receipt("a")["changed_red_tests"], [])



class ToolchainAndEnvironmentReceiptTests(LaneCase):
    """#571 review findings: a receipt or its results never outlive the Rust toolchain they ran with."""

    def receipt(self, lane_id):
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def pin_rust(self):
        self.commit(self.integration, {"rust-toolchain.toml": '[toolchain]\nchannel = "1.98.1"\n',
                                       "tools/rust-toolchain.py": RUST_STUB}, "pin Rust")

    def test_a_green_receipt_is_not_current_after_the_rust_toolchain_changes(self):
        self.pin_rust()
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.assertEqual(self.receipt("a")["toolchain"]["bin"], "/rust/1.98.1")
        self.assertIn("Receipt current", self.lane("check", "a"))
        self.assertEqual(len(self.verify_runs()), 1)
        self.env["FIXTURE_RUST"] = "1.99.0"
        output = self.lane("check", "a")
        self.assertNotIn("Receipt current", output)
        self.assertIn("the Rust toolchain changed", output)
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        self.assertEqual(self.receipt("a")["toolchain"]["bin"], "/rust/1.99.0")
        self.env["FIXTURE_RUST"] = "missing"
        self.assertNotIn("Receipt current", self.lane("check", "a"))
        self.assertEqual(len(self.verify_runs()), 3)
        self.assertIn("refused", self.receipt("a")["toolchain"])

    def test_results_from_another_toolchain_are_not_reused_for_a_later_tree(self):
        self.pin_rust()
        worktree = self.created("a", "lib/**", "README.md")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.env["FIXTURE_RUST"] = "1.99.0"
        self.commit(worktree, {"README.md": "edited\n"})
        self.lane("check", "a")
        # Without the change only structure-check would run; lib passed under the old Rust.
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")

    def test_a_run_failing_outside_any_check_voids_its_results_and_none_are_reused(self):
        # Review of #422, finding 3: verify.sh exits non-zero (setup, the slot limiter) although every check of its
        # report passed; nothing of that run counts as passed.
        worktree = self.created("a", "lib/**", "README.md")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.env["FIXTURE_EXIT"] = "lane-a:3"
        self.lane("check", "a", ok=False)
        receipt = self.receipt("a")
        self.assertEqual({c: e["status"] for c, e in receipt["check_results"].items()},
                         {"structure-check": "void", "lib-check": "void"})
        self.assertEqual({r["suite"]: r["status"] for r in receipt["results"]}, {"structure": "failed", "lib": "failed"})
        del self.env["FIXTURE_EXIT"]
        self.commit(worktree, {"README.md": "edited\n"})
        self.lane("check", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        self.assertFalse(any(r.get("reused_from") for r in self.receipt("a")["results"]))

    def test_an_environment_failed_run_passes_no_suite_and_none_of_its_results_are_reused(self):
        worktree = self.created("a", "lib/**", "native/**", "README.md")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n", "native/straylight-core/notes.txt": "lane\n"})
        self.env["FIXTURE_ENVIRONMENT"] = "toolchain changed"
        self.lane("check", "a", ok=False)
        results = {r["suite"]: r for r in self.receipt("a")["results"]}
        self.assertEqual({suite: r["status"] for suite, r in results.items()},
                         {"structure": "failed", "lib": "failed", "native": "failed"})
        self.assertEqual({r["environment"] for r in results.values()}, {"toolchain changed"})
        del self.env["FIXTURE_ENVIRONMENT"]
        self.commit(worktree, {"README.md": "edited\n"})
        self.lane("check", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check,native-check")
        self.assertFalse(any(r.get("reused_from") for r in self.receipt("a")["results"]))


class DependencyBlockedTests(LaneCase):
    """fathers#195: checks skipped/blocked because a dependency failed stay separate from executed failures, never
    consume rerun budget, are never labeled independent regressions, and never turn green."""

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def install_dep_verify(self) -> pathlib.Path:
        """A consumer verify script (like Repo-Kit, no plan_module) with 1 failed prerequisite, 3 dependency-blocked
        children (two `blocked`, one `skipped` with `blocked_by`), and 2 unrelated executed failures."""
        runs_log = self.tmp / "dep-verify-runs.log"
        config = (self.integration / ".lanefather.toml").read_text().replace(
            'plan_module = "tools/verification_plan.py"\n', "")
        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, os, pathlib, sys, uuid
            args = sys.argv[1:]
            root = pathlib.Path(__file__).resolve().parent
            checks = [
                ("verify:unit", "verify"),
                ("verify:typecheck", "verify"),
                ("verify:lint", "verify"),
                ("browser:build", "browser"),
                ("browser:storybook", "browser"),
                ("browser:story-baseline", "browser"),
            ]
            if args[:1] == ["--changed"]:
                paths = ["src/unit.test.ts", "src/app.ts"]
                rows = [{{"id": cid, "suite": suite, "paths": paths, "producer": False}} for cid, suite in checks]
                print(json.dumps({{"full": True, "paths": paths, "unowned": [], "suites": ["verify", "browser"],
                                   "reasons": {{"verify": paths, "browser": paths}}, "checks": rows}}))
                sys.exit(0)
            ids = args[args.index("--checks") + 1].split(",") if "--checks" in args else [c for c, _ in checks]
            label = os.environ.get("LANEFATHER_SLOT_LABEL", "")
            with open({str(runs_log)!r}, "a") as handle:
                handle.write(label + " " + ",".join(ids) + "\\n")
            run = root / ".build" / "verification-runs" / ("dep-" + uuid.uuid4().hex[:8])
            run.mkdir(parents=True)
            rows = []
            exit_code = 0
            flaky = os.environ.get("DEP_FLAKY_ALONE") == "1"
            alone_blocked = os.environ.get("DEP_ALONE_BLOCKED", "")
            for cid in ids:
                if label.endswith("-base"):
                    st, bb = "passed", []
                elif label.endswith("-alone"):
                    if cid == alone_blocked:
                        st, bb = "blocked", ["verify:unit"]
                    elif flaky:
                        st, bb = "passed", []
                    else:
                        st, bb = "failed", []
                else:
                    if cid in ("verify:unit", "verify:typecheck", "verify:lint"):
                        st, bb = "failed", []
                    elif cid == "browser:story-baseline":
                        st, bb = "skipped", ["verify:unit"]
                    else:
                        st, bb = "blocked", ["verify:unit"]
                if st in ("failed", "blocked") or (st == "skipped" and bb):
                    exit_code = 1
                rows.append({{"id": cid, "status": st, "exit": 0 if st == "passed" else 1,
                             "log": str(run / (cid.replace(":", "_") + ".log")), "seconds": 0.01,
                             **({{"blocked_by": bb}} if bb else {{}})}})
            report = {{"exit": exit_code, "checks": rows, "stopped": None}}
            (run / "report.json").write_text(json.dumps(report))
            print("Verification report: " + str(run / "report.json"))
            sys.exit(exit_code)
            """)
        self.commit(self.integration, {".lanefather.toml": config, "verify.sh": script},
                    "install dependency-blocked verify fixture")
        return runs_log

    def test_dependency_blocked_children_do_not_consume_rerun_budget_or_become_regressions(self):
        runs_log = self.install_dep_verify()
        self.env["LANEFATHER_RERUNS"] = "3"
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/unit.test.ts": "expect(1).toBe(2)\n", "src/app.ts": "export const x = 1\n"})
        output = self.lane("check", "a", ok=False)
        receipt = self.receipt("a")
        self.assertEqual(receipt["status"], "red")

        # Only the 3 actual executed failures were rerun on alone and base; none of the 3 blocked browser checks.
        logged = runs_log.read_text().splitlines()
        self.assertEqual(len(logged), 3, logged)
        self.assertEqual(logged[1], "lane-a-alone verify:unit,verify:typecheck,verify:lint")
        self.assertEqual(logged[2], "lane-a-base verify:unit,verify:typecheck,verify:lint")

        # Suite results separate executed failures from blocked suites.
        by_suite = {r["suite"]: r for r in receipt["results"]}
        self.assertEqual(by_suite["verify"]["status"], "failed")
        self.assertEqual(by_suite["verify"]["failed_checks"], ["verify:unit", "verify:typecheck", "verify:lint"])
        self.assertEqual(by_suite["browser"]["status"], "blocked")
        self.assertEqual(by_suite["browser"]["failed_checks"], [])
        self.assertEqual(by_suite["browser"]["blocked_checks"],
                         ["browser:build", "browser:storybook", "browser:story-baseline"])

        # Per-check results preserve blocked status and blocked_by (including skipped-with-blocked_by).
        for child in ("browser:build", "browser:storybook", "browser:story-baseline"):
            self.assertEqual(receipt["check_results"][child]["status"], "blocked")
            self.assertEqual(receipt["check_results"][child]["blocked_by"], ["verify:unit"])

        # Classification: executed failures are regressions; blocked children are dependency-blocked, never regressions.
        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        for real in ("verify:unit", "verify:typecheck", "verify:lint"):
            self.assertEqual(entries[real]["class"], "regression")
        for child in ("browser:build", "browser:storybook", "browser:story-baseline"):
            self.assertEqual(entries[child]["class"], "dependency-blocked")
            self.assertEqual(entries[child]["blocked_by"], ["verify:unit"])
            self.assertIn("skipped because dependency failed (verify:unit)", entries[child]["reason"])
            self.assertIn(f"{child}: dependency-blocked: skipped because dependency failed (verify:unit)", output)

        # First receipt and changed_red_tests attribute only executed failures, never blocked browser checks.
        self.assertEqual(receipt["first"]["failed_checks"], ["verify:unit", "verify:typecheck", "verify:lint"])
        self.assertEqual({t["check"] for t in receipt["changed_red_tests"]},
                         {"verify:unit", "verify:typecheck", "verify:lint"})

    def test_dependency_blocked_children_keep_receipt_red_even_when_executed_failures_pass_alone(self):
        self.install_dep_verify()
        self.env.update(LANEFATHER_RERUNS="3", DEP_FLAKY_ALONE="1")
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/app.ts": "export const x = 2\n"})
        self.lane("check", "a", ok=False)
        receipt = self.receipt("a")
        self.assertEqual(receipt["status"], "red")
        self.assertFalse(receipt["classification"]["green"])
        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        self.assertEqual(entries["verify:unit"]["class"], "flaky-under-load")
        self.assertEqual(entries["browser:build"]["class"], "dependency-blocked")

    def test_check_blocked_during_alone_rerun_is_not_labeled_regression(self):
        runs_log = self.install_dep_verify()
        self.env.update(LANEFATHER_RERUNS="3", DEP_ALONE_BLOCKED="verify:typecheck")
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/app.ts": "export const x = 3\n"})
        self.lane("check", "a", ok=False)
        receipt = self.receipt("a")
        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        self.assertEqual(entries["verify:typecheck"]["class"], "dependency-blocked")
        self.assertEqual(entries["verify:typecheck"]["blocked_by"], ["verify:unit"])
        # verify:typecheck was blocked on alone, so it did not run on base.
        logged = runs_log.read_text().splitlines()
        self.assertEqual(logged[2], "lane-a-base verify:unit,verify:lint")


class MinSlotsTests(LaneCase):
    """fathers#173: a lane check honours a step's declared minimum slots (from --plan --json or [checks.min_slots])
    instead of falling to 1 slot on a busy machine."""

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def test_plan_declared_slots_floor_prevents_falling_to_one_slot_on_busy_machine(self):
        # Mark lib-check as declaring slots=2 in --plan --json.
        verify = (self.integration / "verify.sh").read_text().replace(
            'rows = [dict(row, producer=not row["paths"]) for row in rows if full or row["paths"]]',
            'rows = [dict(row, producer=not row["paths"], **({"slots": 2} if row["id"] == "lib-check" else {})) '
            'for row in rows if full or row["paths"]]')
        self.commit(self.integration, {"verify.sh": verify}, "declare slots=2 for lib-check")
        # 3 of 4 slots held -> slotfather advise returns jobs=1.
        self.env.update(FAKE_SLOTFATHER_HELD="3")
        worktree = self.created("a", "lib/**", "README.md")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        receipt = self.receipt("a")
        self.assertEqual(receipt["jobs"], 2)
        self.assertEqual(receipt["jobs_decision"]["advised_jobs"], 1)
        self.assertEqual(receipt["jobs_decision"]["min_slots"], 2)
        self.assertEqual(receipt["jobs_decision"]["min_slots_for"], ["lib-check"])
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        self.assertEqual(leases[-1], "lane-a --jobs 2")

        # When lib-check is reused (not remaining), only structure-check runs -> falls back to advise (1 slot).
        self.commit(worktree, {"README.md": "docs edit\n"})
        self.lane("check", "a")
        receipt2 = self.receipt("a")
        self.assertEqual(receipt2["jobs"], 1)
        self.assertNotIn("min_slots", receipt2["jobs_decision"])

        # Explicit --slots 1 override still wins over declared min_slots.
        self.lane("check", "a", "--rerun", "--slots", "1")
        receipt3 = self.receipt("a")
        self.assertEqual(receipt3["jobs"], 1)
        self.assertEqual(receipt3["jobs_decision"], {"jobs": 1, "override": "--slots"})

    def test_config_min_slots_pattern_honours_floor_on_busy_machine(self):
        config = (self.integration / ".lanefather.toml").read_text() + '\n[checks.min_slots]\n"lib-*" = 2\n'
        self.commit(self.integration, {".lanefather.toml": config}, "configure [checks.min_slots]")
        self.env.update(FAKE_SLOTFATHER_HELD="3")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        receipt = self.receipt("a")
        self.assertEqual(receipt["jobs"], 2)
        self.assertEqual(receipt["jobs_decision"]["advised_jobs"], 1)
        self.assertEqual(receipt["jobs_decision"]["min_slots"], 2)
        self.assertEqual(receipt["jobs_decision"]["min_slots_for"], ["lib-check"])

    def test_min_slots_is_capped_at_machine_total_slot_capacity(self):
        from lanefather import host, slots
        with unittest.mock.patch.object(slots, "advise", return_value={"jobs": 1, "slots": 1, "held": 0, "free": 1}):
            decision = host.check_jobs({"disk_free_gb": 500, "load15": 0.1, "cores": 2}, minimum=2,
                                       min_for=["browser:story-checks"])
        self.assertEqual(decision["jobs"], 1)
        self.assertEqual(decision["min_slots"], 1)

    def test_classify_reruns_honour_declared_min_slots(self):
        from lanefather import checks
        lane_plan: dict[str, Any] = {
            "selection": [{"id": "browser:e2e", "suite": "browser", "paths": ["a.ts"], "min_slots": 4}],
        }
        fresh = [{"suite": "browser", "status": "failed", "failed_checks": ["browser:e2e"]}]
        report = {"checks": [{"id": "browser:e2e", "status": "failed"}]}
        seen_jobs: list[int] = []

        class FakeRerun:
            def __init__(self, root: Any, ids: list[str], label: str, jobs: int, **kwargs: Any) -> None:
                seen_jobs.append(jobs)
                self.run_id = f"run-{label}"

        @contextlib.contextmanager
        def fake_base(*_args: Any, **_kwargs: Any) -> Any:
            yield "/tmp"

        with (unittest.mock.patch.object(checks, "Rerun", FakeRerun),
              unittest.mock.patch.object(checks, "run_rerun", return_value=(report, pathlib.Path("/tmp/l"), None, None)),
              unittest.mock.patch.object(checks.worktrees, "base_checkout", side_effect=fake_base),
              unittest.mock.patch.object(checks.plan, "known_checks", return_value={"browser:e2e"})):
            checks.classify({"id": "a", "ticket": "1"}, "/tmp", "deadbeef", fresh, report, jobs=4,
                            lane_plan=lane_plan)
        self.assertEqual(seen_jobs, [4, 4])

    def test_fix_plan_preserves_min_slots_on_added_parent_checks(self):
        from lanefather import checks
        lane_plan: dict[str, Any] = {
            "paths": ["src/new.ts"],
            "selection": [{"id": "unit", "suite": "verify", "paths": ["src/new.ts"]}],
            "selected": ["verify"],
            "by_suite": {"verify": ["unit"]},
        }
        parent: dict[str, Any] = {
            "lane": "p",
            "tree": "abc",
            "paths": ["src/old.ts"],
            "selection": [{"id": "browser:e2e", "suite": "browser", "paths": ["src/old.ts"], "min_slots": 4}],
        }
        about = checks.fix_plan(lane_plan, parent, known={"unit", "browser:e2e"})
        self.assertTrue(about["union"])
        selection: list[dict[str, Any]] = lane_plan["selection"]
        added = next(r for r in selection if r["id"] == "browser:e2e")
        self.assertEqual(added.get("min_slots"), 4)


class SlotRangeWorkerTests(LaneCase):
    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def leases(self) -> list[str]:
        return pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()

    def history_entries(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted((self.state / "history").glob("*.jsonl")):
            for line in path.read_text().splitlines():
                if line:
                    rows.append(json.loads(line))
        return rows

    def test_slot_range_requests_range_and_records_granted_count(self):
        from lanefather import slots

        sf_log_path = self.state / "slotfather.log"
        self.env.update(
            FAKE_SLOTFATHER_RANGE="1",
            FAKE_SLOTFATHER_TOTAL="4",
            FAKE_SLOTFATHER_HELD="3",
            FAKE_SLOTFATHER_LOG=str(sf_log_path),
        )
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/a.txt": "range\n"})
        self.lane("check", "a")

        receipt = self.receipt("a")
        self.assertEqual(receipt["jobs"], 1)
        self.assertTrue(receipt["jobs_decision"]["slot_range"])
        self.assertEqual(receipt["jobs_decision"]["min_jobs"], 1)
        self.assertEqual(receipt["jobs_decision"]["max_jobs"], 2)
        self.assertEqual(receipt["jobs_decision"]["granted"], 1)
        self.assertEqual(self.leases(), ["lane-a --jobs 2 held 1"])

        sf_log: list[dict[str, Any]] = [json.loads(line) for line in sf_log_path.read_text().splitlines() if line]
        runs = [entry for entry in sf_log if entry["command"] == "run"]
        self.assertTrue(any("-n" in entry["args"] and "1-2" in entry["args"] for entry in runs))

        entries = [row for row in self.history_entries() if row["kind"] == "check" and row.get("lane") == "a"]
        self.assertTrue(entries)
        self.assertTrue(all(row.get("jobs") == 1 for row in entries))

        # With free capacity and FAKE_SLOTFATHER_GRANTED=2, the same range yields 2 workers.
        self.env.update(FAKE_SLOTFATHER_HELD="0", FAKE_SLOTFATHER_GRANTED="2")
        self.commit(worktree, {"lib/a.txt": "range-two\n"})
        self.lane("check", "a")
        receipt2 = self.receipt("a")
        self.assertEqual(receipt2["jobs"], 2)
        self.assertEqual(receipt2["jobs_decision"]["granted"], 2)
        latest = [row for row in self.history_entries()
                  if row["kind"] == "check" and row.get("lane") == "a" and row.get("tree") == receipt2["tree"]]
        self.assertTrue(latest)
        self.assertTrue(all(row.get("jobs") == 2 for row in latest))

        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            wrapped = slots.wrap(2, "lane-a", ["echo", "hi"], min_count=1)
        self.assertIn("1-2", wrapped)

    def test_without_slot_range_preserves_exact_argv_receipt_and_history(self):
        from lanefather import slots

        self.env.pop("FAKE_SLOTFATHER_RANGE", None)
        self.env.update(FAKE_SLOTFATHER_TOTAL="4", FAKE_SLOTFATHER_HELD="3")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/a.txt": "legacy\n"})
        self.lane("check", "a")

        receipt = self.receipt("a")
        self.assertEqual(receipt["jobs"], 1)
        self.assertNotIn("slot_range", receipt["jobs_decision"])
        self.assertNotIn("min_jobs", receipt["jobs_decision"])
        self.assertNotIn("max_jobs", receipt["jobs_decision"])
        self.assertNotIn("granted", receipt["jobs_decision"])
        self.assertEqual(self.leases(), ["lane-a --jobs 1"])

        entries = [row for row in self.history_entries() if row["kind"] == "check" and row.get("lane") == "a"]
        self.assertTrue(entries)
        self.assertTrue(all("jobs" not in row for row in entries))

        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            wrapped = slots.wrap(2, "lane-a", ["echo", "hi"], min_count=1)
        self.assertIn("-n", wrapped)
        self.assertEqual(wrapped[wrapped.index("-n") + 1], "2")


if __name__ == "__main__":
    unittest.main()
