"""Results by tree and plan (fathers#91): a check result belongs to a tree and a plan, so a tree verified with a plan
anywhere is not checked again with it (lanefather.results, docs/results.md)."""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import shutil
import unittest
import unittest.mock

from support import LaneCase

TREE_A = "a" * 40
TREE_B = "b" * 40


class ResultKeyTableTests(unittest.TestCase):
    """The owner's rule as a table: same tree and plan reuse; a changed plan or another tree runs."""

    def setUp(self):
        from lanefather import config
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-results-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = unittest.mock.patch.dict(os.environ, {"LANEFATHER_STATE": str(self.tmp / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        config.use(config.Config())
        self.addCleanup(config.use, None)

    def test_the_table(self):
        from lanefather import results
        results.record(TREE_A, {"lint": "passed", "unit": "passed"}, source="check")
        results.record(TREE_A, {}, full=True, status="green", source="gate", sha="c" * 40)
        rows = [
            # (tree, plan, toolchain, verified)
            (TREE_A, ["lint", "unit"], None, True),                 # same tree, same plan
            (TREE_A, ["unit", "lint"], None, True),                 # the order does not make another plan
            (TREE_A, ["lint"], None, True),                         # each check of the plan passed here
            (TREE_A, ["lint", "unit", "browser"], None, False),     # changed plan: a new check
            (TREE_A, ["lint", "unit"], {"rustc": "1.99"}, False),   # changed plan: another toolchain
            (TREE_B, ["lint", "unit"], None, False),                # another tree
            (TREE_A, results.FULL, None, True),                     # the full gate passed this tree
            (TREE_B, results.FULL, None, False),
            (TREE_A, results.FULL, {"rustc": "1.99"}, False),
        ]
        for tree, plan, toolchain, verified in rows:
            with self.subTest(tree=tree[:1], plan=plan, toolchain=toolchain):
                self.assertEqual(results.lookup(tree, plan, toolchain)["verified"], verified)

    def test_the_plan_key_names_the_checks_and_the_environment(self):
        from lanefather import results
        self.assertEqual(results.plan_hash(["b", "a"]), results.plan_hash(["a", "b", "a"]))
        self.assertNotEqual(results.plan_hash(["a"]), results.plan_hash(["a", "b"]))
        self.assertNotEqual(results.plan_hash(["a"]), results.plan_hash(["a"], {"rustc": "1.99"}))
        self.assertNotEqual(results.plan_hash(results.FULL), results.plan_hash(["full"]))
        # The documented recipe (docs/results.md): a runner without Lanefather computes the same key.
        import hashlib
        env = hashlib.sha256(b'{"toolchain":null,"v":1}').hexdigest()
        self.assertEqual(results.plan_hash(results.FULL),
                         hashlib.sha256(('{"checks":"full","env":"' + env + '","v":1}').encode()).hexdigest())

    def test_a_newer_red_result_hides_an_older_pass_and_a_newer_pass_stands_again(self):
        from lanefather import results
        results.record(TREE_A, {"unit": "passed"}, source="check")
        results.record(TREE_A, {"unit": "failed"}, source="check")
        answer = results.lookup(TREE_A, ["unit"])
        self.assertFalse(answer["verified"])
        self.assertIn("newest result not passed: unit", answer["reason"])
        results.record(TREE_A, {"unit": "passed"}, source="check")
        self.assertTrue(results.lookup(TREE_A, ["unit"])["verified"])

    def test_a_red_full_gate_after_a_green_one_is_not_verified(self):
        from lanefather import results
        results.record(TREE_A, {}, full=True, status="green", source="gate")
        results.record(TREE_A, {}, full=True, status="red", source="external:repo-kit")
        self.assertFalse(results.lookup(TREE_A, results.FULL)["verified"])

    def test_an_unreadable_store_reuses_nothing(self):
        from lanefather import results
        results.path_for(TREE_A).write_text("{not json")
        self.assertFalse(results.lookup(TREE_A, ["unit"])["verified"])
        results.record(TREE_A, {"unit": "passed"}, source="check")
        self.assertTrue(results.lookup(TREE_A, ["unit"])["verified"])


class TreeReuseTests(LaneCase):
    """check, the post-merge smoke and promote look the tree up before they run."""

    def receipt(self, lane_id, kind="receipt"):
        return json.loads((self.state / f"{lane_id}.{kind}.json").read_text())

    def test_a_second_lane_with_the_same_tree_runs_nothing_and_names_the_first_run(self):
        first = self.created("a", "lib/**")
        self.commit(first, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.start("b", "lib/**", extra=("--after", "a", "--create"))
        second = self.record("b")["worktree"]
        self.commit(second, {"lib/api.txt": "foo\nbaz\n"})
        self.assertEqual(self.receipt("a")["tree"], self.git(second, "rev-parse", "HEAD^{tree}"))
        output = self.lane("check", "b")
        self.assertEqual(self.verify_runs(), ["checks structure-check,lib-check"])
        receipt = self.receipt("b")
        self.assertEqual(receipt["status"], "green")
        first_receipt = str(self.state / "a.receipt.json")
        for check in ("structure-check", "lib-check"):
            entry = receipt["check_results"][check]
            self.assertEqual((entry["reused_by"], entry["reused_source"], entry["reused_lane"]), ("tree", "check", "a"))
            self.assertEqual(entry["reused_receipt"], first_receipt)
        self.assertIn(f"passed on this tree before: {first_receipt}", output)

    def test_a_rerun_runs_again_and_a_red_run_is_not_reused(self):
        first = self.created("a", "lib/**")
        self.commit(first, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.lane("check", "a", "--rerun")
        self.assertEqual(len(self.verify_runs()), 2)
        broken = self.created("b", "calls/**")
        self.commit(broken, {"calls/main.txt": "nothere\n"})
        self.lane("check", "b", ok=False)
        self.start("c", "calls/**", extra=("--after", "b", "--create"))
        again = self.record("c")["worktree"]
        self.commit(again, {"calls/main.txt": "nothere\n"})
        runs = len(self.verify_runs())
        self.lane("check", "c", ok=False)
        # The red lib-check runs again (then reruns alone and on the base); the passed structure check does not.
        self.assertEqual(self.verify_runs()[runs], "checks lib-check")
        results = self.receipt("c")["check_results"]
        self.assertEqual(results["structure-check"]["reused_by"], "tree")
        self.assertNotIn("reused_by", results["lib-check"])

    def test_a_smoke_check_that_passed_on_the_merge_tree_does_not_run_again(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        # The integration moves on with a change every check reads: without the store the whole smoke runs.
        self.integration_moves()
        merge_tree = self.git(self.repo, "merge-tree", "--write-tree", "integrate/q", "lane/a")
        self.lane("results", "record", merge_tree, "--checks", "structure-check=passed,lib-check=passed",
                  "--source", "repo-kit", "--receipt", "https://ci.example/run/1")
        runs = len(self.verify_runs())
        output = self.lane("land", "a")
        self.assertEqual(self.git(self.integration, "rev-parse", "HEAD^{tree}"), merge_tree)
        self.assertEqual(len(self.verify_runs()), runs)
        self.assertIn("post-merge green (skipped: structure-check: passed on this merge tree before "
                      "(external:repo-kit, https://ci.example/run/1)", output)
        post = self.receipt("a", "post-merge")
        self.assertEqual((post["kind"], post["status"]), ("skip", "green"))
        self.assertEqual(post["check_results"]["lib-check"]["reused_from"], merge_tree)
        self.assertRegex((self.state / "ledger.log").read_text(), r"post-merge a green skip jobs=0 suites=- not_rerun=2 ")

    def test_promote_of_a_dev_verified_sha_to_main_runs_no_checks_and_says_why(self):
        self.git(self.repo, "branch", "dev")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/extra.txt": "a\n"})
        self.lane("check", "a")
        self.lane("land", "a")
        self.lane("review", "a")
        tip = self.tip()
        self.lane("promote", tip, "--no-push", "--wait", "0")
        runs = len(self.verify_runs())
        self.assertEqual(self.verify_runs()[-1], "FULL structure,lib,native,host-journeys")
        output = self.lane("promote", tip, "--ship", "--no-push", "--wait", "0")
        self.assertEqual(len(self.verify_runs()), runs)
        tree = self.git(self.repo, "rev-parse", tip + "^{tree}")
        self.assertIn(f"tree is already verified (the batch gate of {tip[:10]} passed tree {tree[:10]} at ", output)
        self.assertIn(f"promote-{tip[:10]}.gate.json); no batch gate needed", output)
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), tip)

    def test_the_lookup_command_answers_with_its_exit_status_and_json(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        head = self.git(worktree, "rev-parse", "HEAD")
        answer = json.loads(self.lane("results", "lookup", head, "--checks", "lib-check,structure-check", "--json"))
        self.assertEqual((answer["v"], answer["verified"], answer["tree"]), (1, True, self.receipt("a")["tree"]))
        self.assertEqual(answer["results"]["lib-check"]["receipt"], str(self.state / "a.receipt.json"))
        missing = self.lane("results", "lookup", head, "--checks", "lib-check,native-check", ok=False)
        self.assertIn("not verified", missing)
        self.assertIn("never ran here: native-check", missing)
        self.assertIn("not verified", self.lane("results", "lookup", head, "--full", ok=False))
        key = json.loads(self.lane("results", "key", head, "--full", "--json"))
        self.lane("results", "record", head, "--full", "--status", "green", "--source", "repo-kit")
        full = json.loads(self.lane("results", "lookup", head, "--full", "--json"))
        self.assertEqual((full["verified"], full["plan"]), (True, key["plan"]))
        self.assertEqual(full["results"]["full"]["source"], "external:repo-kit")
        self.assertIn("give exactly one of --full or --checks", self.lane("results", "lookup", head, ok=False))


if __name__ == "__main__":
    unittest.main()
