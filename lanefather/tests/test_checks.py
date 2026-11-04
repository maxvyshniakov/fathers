"""Tests of the lane check and its receipt (lanefather.checks, lanefather.host): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import time
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


class LoadThrottleTests(LaneCase):
    """#572: every result records the load at its verify run's start and end. Owner decision on the #572 review: the
    load wait is off by default; with LANEFATHER_LOAD_THROTTLE=1 a verify run of check, land or promote starts only while
    load15 is under the limit lanefather start enforces (1.5 x cores), and the result records that wait."""

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
    """#568: a red lane check reruns each failed check alone on the lane and on the lane's base, and says why
    it failed: regression, pre-existing or flaky-under-load. Replays #551, whose agent called a regression load."""

    def receipt(self, lane_id):
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def entries(self, lane_id):
        return {entry["check"]: entry for entry in self.receipt(lane_id)["classification"]["checks"]}

    def runs(self):
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
        self.assertEqual([(label, commit, where, selection) for label, commit, where, _, selection in self.runs()], [
            ("lane-core", head, "branch", "checks structure-check,native-check"),
            # Alone: one job, no other check beside it.
            ("lane-core-alone", head, "branch", "checks native-check"),
            ("lane-core-base", base, "detached", "checks native-check"),
        ])
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        # Each rerun holds all four machine-wide slots: nothing else verifies beside it.
        self.assertEqual(leases[1:], ["lane-core-alone --jobs 1 held 4", "lane-core-base --jobs 1 held 4"])
        entry = self.entries("core")["native-check"]
        self.assertEqual(entry["class"], "regression")
        self.assertEqual(entry["base"]["sha"], base)
        self.assertEqual(entry["base"]["status"], "passed")
        self.assertEqual(self.receipt("core")["status"], "red")
        self.assertIn("native-check: regression", output)
        # The disposable base checkout is gone.
        self.assertEqual(self.base_checkouts("core"), [])
        refusal = self.lane("land", "core", ok=False)
        self.assertIn("regression: native-check", refusal)

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
        # Green alone: the base run would add nothing.
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

    def red_core(self, **env):
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
        self.assertIn("the rerun alone did not run it (unrun)", entry["reason"])
        # Without a result alone there is nothing to compare with the base.
        self.assertEqual([run[0] for run in self.runs()], ["lane-core", "lane-core-alone"])

    def test_a_rerun_waits_for_the_whole_machine_and_a_timeout_leaves_it_unclassified(self):
        # Review of #568: --jobs 1 took one of four slots, so other verifications ran beside "alone". The rerun asks
        # slotfather for every slot with a wait; here the machine stays busy and slotfather gives up (exit 75).
        self.env.update(FAKE_SLOTFATHER_BUSY="1")
        output = self.red_core(LANEFATHER_RERUN_WAIT="1")
        entry = self.entries("core")["native-check"]
        self.assertEqual(entry["class"], "unclassified")
        self.assertIn("no lease of every machine slot within 1 s", entry["reason"])
        self.assertEqual(self.receipt("core")["status"], "red")
        self.assertEqual([run[0] for run in self.runs()], ["lane-core"])
        runs = [json.loads(line)["args"] for line in (self.tmp / "slotfather.log").read_text().splitlines()
                if json.loads(line)["command"] == "run"]
        self.assertEqual(runs[0][:4], ["-n", "all", "--label", "lane-core-alone"])
        lease = runs[0][:runs[0].index("--")]
        self.assertEqual(("1", "interactive"), (lease[lease.index("--wait") + 1], lease[lease.index("--class") + 1]))
        self.assertIn("kind=rerun-alone", lease)
        self.assertIn("native-check: unclassified", output)

    def test_without_slotfather_a_rerun_is_not_alone_and_leaves_the_check_unclassified(self):
        # Review of #610: without slotfather nothing keeps other verifications beside "alone", and its result
        # classified a regression as load. No slotfather on PATH and no lease: the rerun does not run.
        (self.tmp / "bin" / "slotfather").unlink()
        path = [d for d in self.env["PATH"].split(os.pathsep) if d and not os.path.exists(os.path.join(d, "slotfather"))]
        output = self.red_core(PATH=os.pathsep.join(path))
        entry = self.entries("core")["native-check"]
        self.assertEqual(entry["class"], "unclassified")
        self.assertIn("slotfather is not on PATH", entry["reason"])
        self.assertEqual([run[0] for run in self.runs()], ["lane-core"])
        self.assertEqual(self.receipt("core")["status"], "red")
        self.assertIn("native-check: unclassified", output)

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


if __name__ == "__main__":
    unittest.main()
