"""Results by tree and plan (fathers#91): a check result belongs to a tree and a plan, so a tree verified with a plan
anywhere is not checked again with it (lanefather.results, docs/results.md)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import tempfile
import time
from typing import Any
import unittest
import unittest.mock

from support import RUST_STUB, LaneCase

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
        env = hashlib.sha256(b'{"platform":"linux-x64","toolchain":null,"v":1}').hexdigest()
        self.assertEqual(results.plan_hash(results.FULL, platform="linux-x64"),
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

    def test_an_inferred_pass_never_beats_a_result_of_the_tree_and_lookup_does_not_count_it(self):
        # Review of fathers#91, finding 1: a run that took lib from an earlier tree records it after a fresh failure.
        from lanefather import results
        results.record(TREE_A, {"lib": "failed"}, source="check", lane="b")
        results.record(TREE_A, {"lib": "passed"}, source="check", lane="a", reused=["lib"],
                       origins={"lib": {"tree": TREE_B, "receipt": "/state/a.receipt.json"}})
        self.assertEqual(results.latest(TREE_A)["lib"]["status"], "failed")
        results.record(TREE_B, {"lib": "passed"}, source="post-merge", reused=["lib"],
                       origins={"lib": {"tree": TREE_A, "receipt": "/state/a.receipt.json"}})
        self.assertEqual(results.passed(TREE_B)["lib"]["origin"]["receipt"], "/state/a.receipt.json")
        answer = results.lookup(TREE_B, ["lib"])
        self.assertFalse(answer["verified"])
        self.assertIn("never ran here: lib", answer["reason"])

    def test_a_tree_keeps_its_newest_entries_and_old_files_go(self):
        from lanefather import results
        for index in range(results.KEEP_ENTRIES + 3):
            results.record(TREE_A, {f"c{index}": "passed"}, source="check")
        kept = results.entries(TREE_A)
        self.assertEqual(len(kept), results.KEEP_ENTRIES)
        self.assertEqual(list(kept[0]["checks"]), ["c3"])
        results.record(TREE_B, {"lib": "passed"}, source="check")
        old = time.time() - (results.RETENTION_DAYS + 1) * 86400
        os.utime(results.path_for(TREE_B), (old, old))
        results.record(TREE_A, {"lib": "passed"}, source="check")
        self.assertFalse(results.path_for(TREE_B).exists())
        self.assertTrue(results.path_for(TREE_A).exists())

    def test_an_unreadable_store_reuses_nothing(self):
        from lanefather import results
        results.path_for(TREE_A).write_text("{not json")
        self.assertFalse(results.lookup(TREE_A, ["unit"])["verified"])
        results.record(TREE_A, {"unit": "passed"}, source="check")
        self.assertTrue(results.lookup(TREE_A, ["unit"])["verified"])


class PlatformTests(unittest.TestCase):
    """A tree is verified only for the platform it was checked on (owner decision 2026-11-13): a result from macOS
    never stands for Linux, and a second run on the same platform is still skipped."""

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
        results.record(TREE_A, {"lint": "passed", "unit": "passed"}, source="check", platform="darwin-arm64")
        results.record(TREE_A, {}, full=True, status="green", source="gate", platform="darwin-arm64")
        results.record(TREE_B, {}, full=True, status="green", source="gate", platform="linux-x64")
        rows = [
            # (tree, plan, platform asking, verified)
            (TREE_A, ["lint", "unit"], "darwin-arm64", True),    # same platform: the rerun is skipped
            (TREE_A, ["lint", "unit"], "linux-x64", False),      # a Mac result is not a Linux result
            (TREE_A, ["lint", "unit"], "darwin-x64", False),     # nor one of another architecture
            (TREE_A, results.FULL, "darwin-arm64", True),
            (TREE_A, results.FULL, "linux-x64", False),
            (TREE_B, results.FULL, "linux-x64", True),           # a Linux result stands for Linux
            (TREE_B, results.FULL, "darwin-arm64", False),       # and not for the Mac
        ]
        for tree, plan, platform, verified in rows:
            with self.subTest(tree=tree[:1], plan=plan, platform=platform):
                answer = results.lookup(tree, plan, platform=platform)
                self.assertEqual(answer["verified"], verified, answer["reason"])
                self.assertEqual(answer["platform"], platform)
                if plan != results.FULL:
                    self.assertEqual(set(results.passed(tree, platform=platform)), set(plan) if verified else set())

    def test_the_plan_key_names_the_platform(self):
        from lanefather import results
        self.assertNotEqual(results.plan_hash(["a"], platform="darwin-arm64"),
                            results.plan_hash(["a"], platform="linux-x64"))
        self.assertNotEqual(results.env_hash(None, "linux-x64"), results.env_hash(None, "linux-arm64"))
        entry = results.record(TREE_A, {"a": "passed"}, source="check", platform="linux-x64")
        self.assertEqual((entry["platform"], entry["plan"]), ("linux-x64", results.plan_hash(["a"],
                                                                                              platform="linux-x64")))

    def test_the_host_platform_is_its_os_and_architecture_and_the_default(self):
        from lanefather import results
        os.environ.pop("LANEFATHER_PLATFORM", None)  # setUp's patch restores the caller's
        rows = [("Darwin", "arm64", "darwin-arm64"), ("Linux", "x86_64", "linux-x64"), ("Linux", "aarch64", "linux-arm64"),
                ("Linux", "AMD64", "linux-x64"), ("Windows", "AMD64", "win32-x64"), ("Linux", "armv7l", "linux-arm")]
        for system, machine, expected in rows:
            with self.subTest(system=system, machine=machine), \
                    unittest.mock.patch("platform.system", return_value=system), \
                    unittest.mock.patch("platform.machine", return_value=machine):
                self.assertEqual(results.host_platform(), expected)
        with unittest.mock.patch.dict(os.environ, {"LANEFATHER_PLATFORM": "linux-x64"}):
            self.assertEqual(results.host_platform(), "linux-x64")
            results.record(TREE_A, {"a": "passed"}, source="check")
            self.assertTrue(results.lookup(TREE_A, ["a"])["verified"])
        with unittest.mock.patch.dict(os.environ, {"LANEFATHER_PLATFORM": "darwin-arm64"}):
            self.assertFalse(results.lookup(TREE_A, ["a"])["verified"])
        from lanefather.errors import LaneError
        with unittest.mock.patch.dict(os.environ, {"LANEFATHER_PLATFORM": "Linux x64"}), \
                self.assertRaises(LaneError):
            results.host_platform()
        # Python under Rosetta reports x86_64 on an Apple silicon Mac: the machine's architecture counts.
        with unittest.mock.patch("platform.system", return_value="Darwin"), \
                unittest.mock.patch("platform.machine", return_value="x86_64"), \
                unittest.mock.patch.object(results, "apple_silicon", return_value=True):
            self.assertEqual(results.host_platform(), "darwin-arm64")

    def test_an_entry_without_a_platform_matches_no_platform(self):
        """An older release's entry names no platform, and the store cannot tell which host wrote it: the state
        directory may be shared, so it counts as no match, and the check runs once more."""
        import hashlib

        from lanefather import results, state
        legacy_env = hashlib.sha256(b'{"toolchain":null,"v":1}').hexdigest()
        legacy = {"checks": {"unit": "passed"}, "full": False, "env": legacy_env, "plan": "p" * 64, "status": "green",
                  "source": "check", "at": "2026-11-12T10:00:00Z"}
        legacy_full = dict(legacy, checks={}, full=True)
        state.write_json(results.path_for(TREE_A), {"v": 1, "tree": TREE_A, "entries": [legacy, legacy_full]})
        for platform in ("darwin-arm64", "linux-x64"):
            with self.subTest(platform=platform):
                self.assertFalse(results.lookup(TREE_A, ["unit"], platform=platform)["verified"])
                self.assertFalse(results.lookup(TREE_A, results.FULL, platform=platform)["verified"])
                self.assertEqual(results.passed(TREE_A, platform=platform), {})
        # A new entry beside it is read, and the file keeps the older entry for the older release.
        results.record(TREE_A, {"unit": "passed"}, source="check", platform="linux-x64")
        self.assertTrue(results.lookup(TREE_A, ["unit"], platform="linux-x64")["verified"])
        self.assertEqual(len(results.entries(TREE_A)), 3)

    def test_a_receipt_names_its_platform_or_the_host_whose_worktree_ran_it(self):
        """A lane check runs in the lane's worktree, so a receipt an earlier release wrote was made on the host that
        has that worktree; without it the platform is unknown and matches none."""
        from lanefather import results
        here = str(self.tmp)
        with unittest.mock.patch.dict(os.environ, {"LANEFATHER_PLATFORM": "linux-x64"}):
            rows = [
                ({"platform": "darwin-arm64"}, here, "darwin-arm64"),
                ({"platform": "darwin-arm64"}, None, "darwin-arm64"),
                ({"tree": "t"}, here, "linux-x64"),               # older receipt, its worktree is on this host
                ({"tree": "t"}, str(self.tmp / "gone"), None),    # older receipt, worktree elsewhere: unknown
                ({"tree": "t"}, None, None),
                (None, here, None),
            ]
            for receipt, worktree, expected in rows:
                with self.subTest(receipt=receipt, worktree=worktree):
                    self.assertEqual(results.receipt_platform(receipt, worktree), expected)

    def test_an_entry_whose_platform_and_environment_disagree_is_not_trusted(self):
        """A hand-edited or foreign entry naming one platform under another platform's environment counts for
        neither."""
        from lanefather import results, state
        forged = {"checks": {"unit": "passed"}, "full": False, "env": results.env_hash(None, "darwin-arm64"),
                  "platform": "linux-x64", "plan": "p" * 64, "status": "green", "source": "check", "at": "x"}
        state.write_json(results.path_for(TREE_A), {"v": 1, "tree": TREE_A, "entries": [forged]})
        self.assertFalse(results.lookup(TREE_A, ["unit"], platform="linux-x64")["verified"])
        self.assertFalse(results.lookup(TREE_A, ["unit"], platform="darwin-arm64")["verified"])


class TreeReuseTests(LaneCase):
    """check, the post-merge smoke and promote look the tree up before they run."""

    def receipt(self, lane_id: str, kind: str = "receipt") -> dict[str, Any]:
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

    def test_a_lane_checked_on_another_platform_runs_its_checks_again(self):
        """Neither the lane's receipt nor the tree's results from the Mac stand for Linux; a second check on Linux
        is current again (review of the platform change)."""
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        with unittest.mock.patch.dict(self.env, {"LANEFATHER_PLATFORM": "darwin-arm64"}):
            self.lane("check", "a")
            self.assertEqual(self.receipt("a")["platform"], "darwin-arm64")
            self.assertIn("Receipt current", self.lane("check", "a"))
        runs = len(self.verify_runs())
        with unittest.mock.patch.dict(self.env, {"LANEFATHER_PLATFORM": "linux-x64"}):
            output = self.lane("check", "a")
            self.assertNotIn("Receipt current", output)
            self.assertGreater(len(self.verify_runs()), runs)
            receipt = self.receipt("a")
            self.assertEqual(receipt["platform"], "linux-x64")
            self.assertFalse(any(r.get("reused_by") for r in receipt["check_results"].values()), receipt)
            self.assertIn("Receipt current", self.lane("check", "a"))
        with unittest.mock.patch.dict(self.env, {"LANEFATHER_PLATFORM": "Linux"}):
            self.assertIn("not a platform", self.lane("check", "a", ok=False))

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

    def test_a_smoke_the_receipt_narrowed_and_the_tree_store_emptied_says_both(self):
        worktree = self.created("a", "calls/**")
        self.commit(worktree, {"calls/main.txt": "foo\n\n"})
        self.lane("check", "a")
        self.commit(self.integration, {"docs/install.md": "journey\n"}, "another lane landed")
        merge_tree = self.git(self.repo, "merge-tree", "--write-tree", "integrate/q", "lane/a")
        self.lane("results", "record", merge_tree, "--checks", "structure-check=passed", "--source", "repo-kit")
        runs = len(self.verify_runs())
        output = self.lane("land", "a")
        self.assertEqual(len(self.verify_runs()), runs)
        self.assertIn("post-merge green (skipped: structure-check: passed on this merge tree before (external:repo-kit)"
                      "; the lane's green receipt stands for 1)", output)

    def test_the_commands_refuse_a_checkout_that_pins_another_toolchain(self):
        # Review of fathers#91, finding 2: the toolchain is resolved in this checkout, so it must pin what REV pins.
        self.commit(self.integration, {"rust-toolchain.toml": '[toolchain]\nchannel = "1.98.1"\n',
                                       "tools/rust-toolchain.py": RUST_STUB}, "pin Rust")
        worktree = self.created("a", "rust-toolchain.toml")
        self.commit(worktree, {"rust-toolchain.toml": '[toolchain]\nchannel = "1.99.0"\n'})
        output = self.lane("results", "lookup", "integrate/q", "--full", cwd=worktree, ok=False)
        self.assertIn("pins another toolchain (rust-toolchain.toml) than this checkout", output)
        self.assertIn("not verified", self.lane("results", "lookup", "integrate/q", "--full", ok=False))

    def test_the_lookup_command_answers_with_its_exit_status_and_json(self):
        # This host records as a Mac, whatever runs the test; --platform asks for another. The command reads the
        # environment LaneCase took in setUp.
        patcher = unittest.mock.patch.dict(self.env, {"LANEFATHER_PLATFORM": "darwin-arm64"})
        patcher.start()
        self.addCleanup(patcher.stop)
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
        # Another platform asks: the same tree and plan are not verified there; it records its own result.
        linux = json.loads(self.lane("results", "lookup", head, "--full", "--platform", "linux-x64", "--json",
                                     ok=False))
        self.assertEqual((linux["verified"], linux["platform"]), (False, "linux-x64"))
        self.assertNotEqual(linux["plan"], key["plan"])
        self.assertEqual((full["platform"], key["platform"]), ("darwin-arm64", "darwin-arm64"))
        self.lane("results", "record", head, "--full", "--status", "green", "--source", "ci", "--platform", "linux-x64")
        self.assertTrue(json.loads(self.lane("results", "lookup", head, "--full", "--platform", "linux-x64",
                                             "--json"))["verified"])
        self.assertIn("not a platform", self.lane("results", "lookup", head, "--full", "--platform", "Linux",
                                                  ok=False))


if __name__ == "__main__":
    unittest.main()
