"""Tests of red lane check classification and isolated reruns (lanefather.checks, lanefather.host)."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
import time
from typing import Any
import unittest

from support import LaneCase


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

    def base_checkouts(self, lane_id: str) -> list[str]:
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

    def test_failed_check_names_without_comparison_evidence_cannot_make_a_green_receipt(self):
        # fathers#113: the same check can fail in a candidate case and an unrelated base setup.
        self.commit(self.integration, {"native/straylight-core/corpus.txt": "v2\n"})
        worktree = self.created("core", "native/**")
        self.commit(worktree, {"native/straylight-core/notes.txt": "lane\n"})
        self.lane("check", "core", ok=False)
        self.assertEqual(self.entries("core")["native-check"]["class"], "unclassified")
        self.assertEqual(self.receipt("core")["status"], "red")
        self.lane("land", "core", ok=False)

    def test_a_check_its_base_fails_too_is_pre_existing_and_names_the_base_failure(self):
        self.env["FIXTURE_FAILURE_EVIDENCE"] = "1"
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
        self.assertEqual(self.receipt("core")["raw_status"], "red")
        self.assertEqual(self.receipt("core")["classification"]["raw_status"], "red")
        self.assertEqual(self.receipt("core")["check_results"]["native-check"]["status"], "failed")
        self.assertIn("raw verification RED", output)
        self.assertEqual(self.base_checkouts("core"), [])
        self.lane("land", "core")

    def test_different_failures_inside_one_check_do_not_count_as_pre_existing(self):
        self.env["FIXTURE_FAILURE_EVIDENCE"] = "1"
        self.commit(self.integration, {"checks/native.sh": "cat native/case.txt; exit 1\n",
                                       "native/case.txt": "prerequisite pin invalid\n"})
        worktree = self.created("core", "native/**")
        self.commit(worktree, {"native/case.txt": "candidate assertion A failed\n"})
        output = self.lane("check", "core", ok=False)
        entry = self.entries("core")["native-check"]
        self.assertEqual(entry["class"], "unclassified")
        self.assertIn("differ", entry["reason"])
        self.assertEqual(self.receipt("core")["classification"]["raw_status"], "red")
        self.assertNotIn("classified receipt is green", output)
        self.lane("land", "core", ok=False)

    def test_a_base_process_setup_failure_cannot_reuse_its_failed_report_as_proof(self):
        self.env["FIXTURE_FAILURE_EVIDENCE"] = "1"
        self.env["FIXTURE_EXIT"] = "lane-core-base:9"
        self.commit(self.integration, {"native/straylight-core/corpus.txt": "v2\n"})
        worktree = self.created("core", "native/**")
        self.commit(worktree, {"native/straylight-core/notes.txt": "lane\n"})
        self.lane("check", "core", ok=False)
        self.assertEqual(self.entries("core")["native-check"]["class"], "unclassified")
        self.assertIn("exit", self.entries("core")["native-check"]["reason"])

    def test_original_process_exit_mismatch_is_not_hidden_by_matching_failed_reruns(self):
        self.env["FIXTURE_FAILURE_EVIDENCE"] = "1"
        self.env["FIXTURE_EXIT"] = "lane-core:9"
        self.commit(self.integration, {"native/straylight-core/corpus.txt": "v2\n"})
        worktree = self.created("core", "native/**")
        self.commit(worktree, {"native/straylight-core/notes.txt": "lane\n"})
        self.lane("check", "core", ok=False)
        receipt = self.receipt("core")
        self.assertEqual(receipt["results"][1]["exit"], 9)
        self.assertEqual(receipt["status"], "red")
        self.assertFalse(receipt["classification"]["green"])
        self.assertIn("process exit differs", receipt["classification"]["skipped"])
        self.lane("land", "core", ok=False)

    def test_original_process_exit_mismatch_cannot_be_hidden_by_flaky_alone_pass(self):
        flag = self.tmp / "flake-exit-mismatch"
        flag.touch()
        self.env["FIXTURE_FLAKE_ONCE"] = f"lib-check:{flag}"
        self.env["FIXTURE_EXIT"] = "lane-a:9"
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a", ok=False)
        receipt = self.receipt("a")
        self.assertEqual(receipt["status"], "red")
        self.assertFalse(receipt["classification"]["green"])
        self.assertIn("process exit differs", receipt["classification"]["skipped"])
        self.lane("land", "a", ok=False)

    def break_structure(self, base_too: bool = False) -> None:
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
        self.env["FIXTURE_FAILURE_EVIDENCE"] = "1"
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

    def _install_only_verifier(self) -> pathlib.Path:
        only_log = self.state / "only-runs.jsonl"
        script = textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, os, pathlib, subprocess, sys, time, uuid

            SUITES = ("structure", "lib")
            CHECKS = {"structure-check": ("structure", ("",)), "lib-check": ("lib", ("lib/",))}
            root = pathlib.Path(__file__).resolve().parent
            args = sys.argv[1:]
            if args[:1] == ["--changed"]:
                base = args[args.index("--base") + 1]
                paths = sorted(set(subprocess.check_output(
                    ["git", "diff", "--name-only", base, "--"], cwd=root, text=True).split()))
                rows = [{"id": cid, "suite": suite, "paths": [p for p in paths if p.startswith(reads)],
                         "producer": False} for cid, (suite, reads) in CHECKS.items()]
                rows = [r for r in rows if r["paths"]]
                suites = [s for s in SUITES if any(r["suite"] == s for r in rows)]
                reasons = {s: sorted({p for r in rows if r["suite"] == s for p in r["paths"]}) for s in suites}
                print(json.dumps({
                    "full": False, "paths": paths, "unowned": [], "checks": rows, "suites": suites,
                    "reasons": reasons, "capabilities": ["only"],
                }))
                sys.exit(0)

            ids = args[args.index("--checks") + 1].split(",") if "--checks" in args else ["structure-check", "lib-check"]
            only = args[args.index("--only") + 1].split(",") if "--only" in args else None
            label = os.environ.get("LANEFATHER_SLOT_LABEL", "-")
            with open(os.environ["ONLY_VERIFY_LOG"], "a") as handle:
                handle.write(json.dumps({"label": label, "checks": ids, "only": only, "args": args}) + "\\n")

            run = root / ".build" / "verification-runs" / (time.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
            run.mkdir(parents=True, exist_ok=True)
            rows = []
            status = 0
            for cid in ids:
                log_path = run / f"{cid}.log"
                failed_tests = []
                outside = []
                code = 0
                if cid == "lib-check":
                    mode = os.environ.get("ONLY_MODE", "regression")
                    if only is None:
                        if mode == "flaky":
                            flag = pathlib.Path(os.environ["ONLY_FLAKE_FLAG"])
                            if flag.exists():
                                flag.unlink()
                                code = 1
                                failed_tests = ["tests/flake.test.ts"]
                        elif mode == "pre-existing":
                            code = 1
                            failed_tests = ["tests/shared.test.ts"]
                        elif mode == "outside-rerun":
                            code = 1
                            failed_tests = ["tests/a.test.ts"]
                        elif mode == "outside-initial":
                            code = 1
                            failed_tests = ["tests/a.test.ts"]
                            outside = ["tests/unscoped.test.ts"]
                        else:
                            if (root / "lib/reg/broken.txt").exists():
                                code = 1
                                failed_tests = ["tests/broken.test.ts"]
                    else:
                        if mode == "outside-rerun" and label.endswith("-alone"):
                            code = 1
                            failed_tests = ["tests/other.test.ts"]
                        elif mode == "pre-existing" and "tests/shared.test.ts" in only:
                            code = 1
                            failed_tests = ["tests/shared.test.ts"]
                        elif "tests/broken.test.ts" in only and (root / "lib/reg/broken.txt").exists():
                            code = 1
                            failed_tests = ["tests/broken.test.ts"]
                log_path.write_text(f"{cid} exit {code}\\n")
                row = {"id": cid, "status": "passed" if code == 0 else "failed", "exit": code, "log": str(log_path)}
                if failed_tests:
                    row["failed_tests"] = failed_tests
                    row["failure_evidence"] = {
                        "version": 1,
                        "comparable": True,
                        "context": "only-verifier-v1",
                        "cases": [{"id": t, "signature": f"fail:{t}"} for t in failed_tests],
                    }
                if outside:
                    row["outside_failed_tests"] = outside
                rows.append(row)
                status = status or code

            report = {"exit": status, "checks": rows, "stopped": None, "capabilities": ["only"]}
            report_path = run / "report.json"
            report_path.write_text(json.dumps(report))
            print(f"Verification report: {report_path}")
            sys.exit(status)
        """)
        self.commit(self.integration, {"verify.sh": script}, "verifier supporting --only subset reruns")
        self.env["ONLY_VERIFY_LOG"] = str(only_log)
        return only_log

    def test_only_reruns_failing_tests_across_regression_pre_existing_and_flaky(self):
        only_log = self._install_only_verifier()
        base = self.tip()

        # 1. Regression: passes --only to both lane and base reruns and records tests in receipt.
        self.env["ONLY_MODE"] = "regression"
        wt_reg = self.created("reg", "lib/reg/**")
        self.commit(wt_reg, {"lib/reg/broken.txt": "bad\n"})
        self.lane("check", "reg", ok=False)
        entry = self.entries("reg")["lib-check"]
        self.assertEqual(entry["class"], "regression")
        self.assertEqual(entry["base"]["sha"], base)
        self.assertEqual(entry["base"]["status"], "passed")
        self.assertEqual(entry["tests"], ["tests/broken.test.ts"])
        rec_reg = self.receipt("reg")
        self.assertEqual(rec_reg["status"], "red")
        self.assertEqual([r.get("tests") for r in rec_reg["classification"]["runs"]],
                         [["tests/broken.test.ts"], ["tests/broken.test.ts"]])
        logged: list[dict[str, Any]] = [json.loads(line) for line in only_log.read_text().splitlines() if line]
        self.assertEqual([(r["label"], r["only"]) for r in logged[-3:]], [
            ("lane-reg", None),
            ("lane-reg-alone", ["tests/broken.test.ts"]),
            ("lane-reg-base", ["tests/broken.test.ts"]),
        ])

        # 2. Pre-existing: both alone and base run with --only and fail on the same test file.
        self.env["ONLY_MODE"] = "pre-existing"
        wt_pre = self.created("pre", "lib/pre/**")
        self.commit(wt_pre, {"lib/pre/ok.txt": "change\n"})
        self.lane("check", "pre")
        entry_pre = self.entries("pre")["lib-check"]
        self.assertEqual(entry_pre["class"], "pre-existing")
        self.assertEqual(entry_pre["tests"], ["tests/shared.test.ts"])
        self.assertEqual(self.receipt("pre")["status"], "green")

        # 3. Flaky-under-load: alone runs with --only and passes; base does not run.
        flake_flag = self.state / "flake.flag"
        flake_flag.write_text("1")
        self.env.update(ONLY_MODE="flaky", ONLY_FLAKE_FLAG=str(flake_flag))
        wt_flk = self.created("flk", "lib/flk/**")
        self.commit(wt_flk, {"lib/flk/flk.txt": "change\n"})
        before = len(only_log.read_text().splitlines())
        self.lane("check", "flk")
        after_lines: list[dict[str, Any]] = [
            json.loads(line) for line in only_log.read_text().splitlines()[before:] if line]
        self.assertEqual([(r["label"], r["only"]) for r in after_lines], [
            ("lane-flk", None),
            ("lane-flk-alone", ["tests/flake.test.ts"]),
        ])
        entry_flk = self.entries("flk")["lib-check"]
        self.assertEqual(entry_flk["class"], "flaky-under-load")
        self.assertEqual(entry_flk["tests"], ["tests/flake.test.ts"])
        self.assertEqual(self.receipt("flk")["status"], "green")

    def test_only_leaves_check_unclassified_when_red_test_falls_outside_failed_tests(self):
        self._install_only_verifier()

        # Case A: alone rerun reports a red test not listed in the initial failed_tests.
        self.env["ONLY_MODE"] = "outside-rerun"
        wt_out = self.created("out", "lib/out/**")
        self.commit(wt_out, {"lib/out/broken.txt": "bad\n"})
        self.lane("check", "out", ok=False)
        entry_out = self.entries("out")["lib-check"]
        self.assertEqual(entry_out["class"], "unclassified")
        self.assertIn("tests/other.test.ts", entry_out["outside_failed_tests"])
        self.assertEqual(self.receipt("out")["status"], "red")

        # Case B: initial lane report already notes a red test outside failed_tests.
        self.env["ONLY_MODE"] = "outside-initial"
        wt_init = self.created("initout", "lib/initout/**")
        self.commit(wt_init, {"lib/initout/other.txt": "bad\n"})
        self.lane("check", "initout", ok=False)
        entry_init = self.entries("initout")["lib-check"]
        self.assertEqual(entry_init["class"], "unclassified")
        self.assertIn("tests/unscoped.test.ts", entry_init["outside_failed_tests"])
        self.assertEqual(self.receipt("initout")["status"], "red")

    def test_without_only_capability_keeps_argv_and_receipt_classification_unchanged(self):
        # Default fixture verifier does not advertise "only": verify argv and classification keys match legacy schema.
        worktree = self.created("core", "native/**")
        self.commit(worktree, {"native/straylight-core/corpus.txt": "v2\n"})
        self.lane("check", "core", ok=False)
        selections = [selection for _, _, _, _, selection in self.runs()]
        self.assertTrue(all("--only" not in selection for selection in selections))
        classification = self.receipt("core")["classification"]
        self.assertTrue(all("tests" not in run for run in classification["runs"]))
        self.assertTrue(all("tests" not in entry for entry in classification["checks"]))


if __name__ == "__main__":
    unittest.main()
