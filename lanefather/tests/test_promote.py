"""Tests of the batch gate, promote, perf and sync (lanefather.promote): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Any
import unittest

from support import FAKE_PLAN, ROOT, LaneCase


class PromoteGateTests(LaneCase):
    """#446: the full gate runs once per batch, at lanefather promote, on a gate worktree of the batch tip."""

    def setUp(self):
        super().setUp()
        self.git(self.repo, "branch", "dev")

    def stamps(self):
        ledger = self.repo / ".git" / "verified-trees"
        return [row.split()[0] for row in ledger.read_text().splitlines()] if ledger.exists() else []

    def landed(self, lane_id, changes):
        worktree = self.created(lane_id, *changes)
        self.commit(worktree, changes)
        self.lane("check", lane_id)
        self.lane("land", lane_id)
        # promote waits for the lane review (#570); the stand-in reviewer finds nothing.
        self.lane("review", lane_id)
        return self.tip()

    def leases(self):
        return pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()

    def test_promote_runs_the_full_gate_on_the_tip_and_moves_dev_only_when_it_stamps(self):
        tip = self.landed("a", {"lib/extra.txt": "a\n"})
        output = self.lane("promote", tip[:12], "--no-push", "--wait", "0")
        self.assertEqual(self.verify_runs()[-1], "FULL structure,lib,native,host-journeys")
        self.assertEqual(self.leases()[-1], f"promote-{tip[:10]} --jobs 2")
        self.assertIn(self.git(self.repo, "rev-parse", tip + "^{tree}"), self.stamps())
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), tip)
        self.assertIn("batch gate green", output)
        gate = json.loads((self.state / f"promote-{tip[:10]}.gate.json").read_text())
        self.assertEqual((gate["status"], gate["full"], gate["jobs"], gate["sha"]), ("green", True, 2, tip))
        self.assertEqual((gate["jobs_decision"]["jobs"], gate["jobs_decision"]["exclusive"]), (2, False))
        # The gate ran on its own worktree, so landing goes on meanwhile; the integration tree was not read.
        self.assertEqual(self.git(self.repo, "-C", str(self.repo / ".claude" / "worktrees" / "batch-gate"),
                                  "rev-parse", "HEAD"), tip)
        # A stamped tree needs no second gate.
        runs = len(self.verify_runs())
        self.assertIn("already verified", self.lane("promote", tip, "--no-push", "--wait", "0"))
        self.assertEqual(len(self.verify_runs()), runs)

    def test_promote_runs_the_after_promote_command_once_dev_moved(self):
        # #588: Straylight runs its Sid-Kit and Data Grid adoption check here; a failure only warns.
        log = self.tmp / "after-promote.log"
        self.env.update(FIXTURE_AFTER_PROMOTE_LOG=str(log))
        tip = self.landed("after", {"lib/after.txt": "after\n"})
        output = self.lane("promote", tip, "--no-push", "--wait", "0")
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), tip)
        self.assertGreater(output.index(f"after-promote: checked {tip[:10]}"), output.index(f"Promoted {tip[:10]}"))
        self.assertEqual(log.read_text().split(), [tip, "--source", "promote", tip[:10]])
        self.env.update(FIXTURE_AFTER_PROMOTE_EXIT="3")
        output = self.lane("promote", tip, "--no-push", "--wait", "0")
        self.assertIn("lane: warning: the after-promote command failed (exit 3)", output)

    def test_a_red_batch_gate_prints_the_lane_merges_since_the_last_green_tree_and_moves_nothing(self):
        green = self.landed("first", {"lib/extra.txt": "first\n"})
        self.lane("promote", green, "--no-push", "--wait", "0")
        # The corpus edit selects only the structure check, so its lane check and smoke pass; the batch
        # gate's native check catches it.
        self.landed("corpus", {"checks/frozen-corpus.txt": "v2\n"})
        tip = self.landed("other", {"lib/other.txt": "other\n"})
        output = self.lane("promote", tip, "--no-push", "--wait", "0", ok=False)
        self.assertIn("batch gate red", output)
        self.assertIn("native-check", output)
        bisect = output.split("bisect")[-1]
        self.assertIn(f"since the last green verified tree {green[:10]}", output)
        self.assertLess(bisect.index("corpus"), bisect.index("other"))
        self.assertNotIn("lane first", bisect)
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), green)
        self.assertNotIn(self.git(self.repo, "rev-parse", tip + "^{tree}"), self.stamps())
        self.assertRegex((self.state / "ledger.log").read_text(), rf"promote-gate {tip} red jobs=2")

    def test_a_tree_that_changes_during_the_gate_is_not_stamped_or_promoted(self):
        tip = self.landed("a", {"lib/extra.txt": "a\n"})
        self.env["FIXTURE_DIRTY_DURING_RUN"] = "1"
        output = self.lane("promote", tip, "--no-push", "--wait", "0", ok=False)
        self.assertIn("tracked files changed during the run", output)
        self.assertNotIn(self.git(self.repo, "rev-parse", tip + "^{tree}"), self.stamps())
        self.assertNotEqual(self.git(self.repo, "rev-parse", "dev"), tip)

    def promote_in_background(self, sha):
        return subprocess.Popen([sys.executable, "-m", "lanefather", "promote", sha, "--no-push", "--wait", "60"],
                                cwd=self.integration, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)

    def test_a_second_promote_waits_for_the_first_batch_gate_instead_of_moving_its_worktree(self):
        # #454: concurrent promotes force-checked-out the shared batch-gate worktree under each other's verify.
        first = self.landed("a", {"lib/extra.txt": "a\n"})
        second = self.landed("b", {"lib/other.txt": "b\n"})
        hold = self.tmp / "release-gate"
        heads = pathlib.Path(str(hold) + ".heads")
        self.env["FIXTURE_GATE_HOLD"] = str(hold)
        gate = self.repo / ".claude" / "worktrees" / "batch-gate"
        procs = [self.promote_in_background(first)]
        try:
            deadline = time.monotonic() + 60
            # The fixture opens the file before it writes the line.
            while not (heads.exists() and heads.read_text().endswith("\n")) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(heads.read_text(), f"start {first}\n", "the first gate is running")
            procs.append(self.promote_in_background(second))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.git(gate, "rev-parse", "HEAD") == first:
                time.sleep(0.05)
            self.assertEqual(self.git(gate, "rev-parse", "HEAD"), first,
                             "the second promote checked out the gate worktree during the first gate")
        finally:
            hold.write_text("")
            outputs = [proc.communicate(timeout=120)[0] for proc in procs]
        for proc, output in zip(procs, outputs):
            self.assertEqual(proc.returncode, 0, output)
        self.assertIn("waiting for the batch gate of another promote", outputs[1])
        self.assertEqual(heads.read_text().splitlines(),
                         [f"start {first}", f"end {first}", f"start {second}", f"end {second}"])
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), second)

    def test_ship_pushes_dev_and_main_atomically(self):
        # #454: a push the remote refused for main still moved the remote's dev.
        tip = self.landed("a", {"lib/extra.txt": "a\n"})
        remote = self.tmp / "remote.git"
        self.git(self.tmp, "init", "-q", "--bare", str(remote))
        # The fixture has no pre-push gate; keep any global hooks out of it.
        (self.tmp / "no-hooks").mkdir()
        self.git(self.repo, "config", "core.hooksPath", str(self.tmp / "no-hooks"))
        self.git(self.repo, "remote", "add", "origin", str(remote))
        self.git(self.repo, "push", "-q", "origin", "main", "dev")
        before = self.git(remote, "rev-parse", "dev")
        hook = self.write(remote, "hooks/update", "#!/bin/sh\n"
                          "[ \"$1\" = refs/heads/main ] && { echo 'main is protected'; exit 1; }\nexit 0\n")
        hook.chmod(0o755)
        output = self.lane("promote", tip, "--ship", "--wait", "0", ok=False)
        self.assertIn("main is protected", output)
        self.assertEqual(self.git(remote, "rev-parse", "dev"), before, "the remote moved dev although it refused main")
        self.assertNotEqual(self.git(self.repo, "rev-parse", "dev"), tip)

    def test_ship_refuses_a_tree_with_unreleased_fragments_until_a_release_commit_folds_them(self):
        """#447: main takes only released notes; dev takes fragments."""
        main = self.git(self.repo, "rev-parse", "main")
        tip = self.commit(self.integration, {"release-notes/unreleased/447-entry.md": "entry\n"}, "fragment")
        runs = len(self.verify_runs())
        output = self.lane("promote", tip, "--ship", "--no-push", "--wait", "0", ok=False)
        self.assertIn(f"promote --ship refused: {tip[:10]} still has release-notes/unreleased/", output)
        self.assertIn("node tools/release-notes.mjs release", output)
        self.assertEqual(len(self.verify_runs()), runs, "the refusal comes before the batch gate")
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), main)
        # A dev promotion carries fragments.
        self.lane("promote", tip, "--no-push", "--wait", "0")
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), tip)
        # The release commit removes them, and that commit ships.
        self.git(self.integration, "rm", "-rq", "release-notes/unreleased")
        self.write(self.integration, "release-notes/0.1.0.md", "entry\n")
        released = self.commit(self.integration, {}, "chore(release-notes): release 0.1.0")
        self.lane("promote", released, "--ship", "--no-push", "--wait", "0")
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), released)


STEP_STAND_IN = r'''import os, sys
name = {name!r}
with open(os.environ["FIXTURE_STEPS"], "a") as log:
    log.write("|".join([" ".join([name, *sys.argv[1:]]), os.path.realpath(os.getcwd()),
                        os.environ.get("STRAYLIGHT_LINK_PACKAGE", ""), os.environ.get("STRAYLIGHT_UNPROMOTED", "")]) + "\n")
sys.exit(7 if os.environ.get("FIXTURE_FAIL_STEP") == name else 0)
'''

# The steps promote --install --deploy runs after the promotion (#569): their stand-ins record the call.
STEPS = {"install": "install-control-app.sh", "check-installed": "tools/check-installed.py",
         "link-package": "tools/straylight-link/build-package.sh", "deploy": "tools/straylight-link/deploy.sh"}


class PerfSuiteTests(LaneCase):
    """#579, owner decision 2026-10-27: the perf suite runs only before main. promote --ship runs it once, after the
    batch gate and before any branch moves, through verify.sh --suites perf (which holds every slot); a red run refuses
    the ship and names the lanes since main. lanefather perf runs it by hand; dev promotions never run it."""

    def setUp(self):
        super().setUp()
        self.git(self.repo, "branch", "dev")
        # A tree whose verification plan has the perf suite.
        self.commit(self.integration, {"tools/verification_plan.py": FAKE_PLAN + 'PERF = "perf"\n'}, "perf suite")

    def landed(self, lane_id, changes):
        worktree = self.created(lane_id, *changes)
        self.commit(worktree, changes)
        self.lane("check", lane_id)
        self.lane("land", lane_id)
        self.lane("review", lane_id)
        return self.tip()

    def perf_runs(self):
        return [line for line in self.verify_runs() if line == "perf"]

    def red(self):
        self.write(self.integration, "checks/perf.sh", "echo 'perf budget missed: a turn took 900 ms'; exit 1\n")

    def test_ship_runs_perf_once_after_the_gate_and_before_main_moves(self):
        tip = self.landed("a", {"lib/extra.txt": "a\n"})
        output = self.lane("promote", tip, "--ship", "--no-push", "--wait", "0")
        self.assertEqual(self.verify_runs()[-2:], ["FULL structure,lib,native,host-journeys", "perf"])
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        self.assertEqual(leases[-1], f"perf-{tip[:10]} --jobs 1")
        self.assertIn("perf suite green", output)
        self.assertEqual((self.git(self.repo, "rev-parse", "dev"), self.git(self.repo, "rev-parse", "main")), (tip, tip))
        receipt = json.loads((self.state / f"perf-{tip[:10]}.perf.json").read_text())
        self.assertEqual((receipt["status"], receipt["sha"], receipt["tree"]),
                         ("green", tip, self.git(self.repo, "rev-parse", tip + "^{tree}")))
        # Once: shipping the tree again reuses its green perf run.
        self.assertIn("perf suite already green", self.lane("promote", tip, "--ship", "--no-push", "--wait", "0"))
        self.assertEqual(len(self.perf_runs()), 1)

    def test_a_red_perf_run_refuses_the_ship_names_the_lanes_since_main_and_moves_nothing(self):
        main = self.git(self.repo, "rev-parse", "main")
        self.landed("first", {"lib/extra.txt": "first\n"})
        self.red()
        tip = self.commit(self.integration, {}, "a slow change")
        output = self.lane("promote", tip, "--ship", "--no-push", "--wait", "0", ok=False)
        self.assertIn("perf suite red", output)
        self.assertIn("perf:budgets", output)
        self.assertIn("performance regression", output)
        bisect = output.split("bisect")[-1]
        self.assertIn(f"since main {main[:10]}", output)
        self.assertIn("lane first", bisect)
        self.assertIn("a slow change", bisect)
        self.assertIn("promote --ship refused", output)
        self.assertEqual((self.git(self.repo, "rev-parse", "dev"), self.git(self.repo, "rev-parse", "main")),
                         (main, main), "a red perf run moves neither branch")
        self.assertRegex((self.state / "ledger.log").read_text(), rf"perf {tip} red")

    def test_a_dev_promotion_never_runs_perf(self):
        tip = self.landed("a", {"lib/extra.txt": "a\n"})
        self.lane("promote", tip, "--no-push", "--wait", "0")
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), tip)
        self.assertEqual(self.perf_runs(), [])
        # Nor does a lane check or the post-merge smoke: their plans never name the perf suite.
        self.assertNotIn("perf", "\n".join(line for line in self.verify_runs() if line != "perf").split(","))

    def test_lane_perf_runs_by_hand_and_moves_nothing(self):
        tip = self.landed("a", {"lib/extra.txt": "a\n"})
        dev = self.git(self.repo, "rev-parse", "dev")
        self.assertIn("perf suite green", self.lane("perf", tip))
        self.assertEqual(self.perf_runs(), ["perf"])
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), dev)
        # A by-hand run measures again, even a tree with a green run.
        self.assertIn("perf suite green", self.lane("perf", tip))
        self.assertEqual(len(self.perf_runs()), 2)
        self.red()
        red = self.commit(self.integration, {}, "slow")
        output = self.lane("perf", red, ok=False)
        self.assertIn("perf suite red", output)
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), dev)

    def test_a_tree_without_the_perf_suite_ships_without_it(self):
        tip = self.commit(self.integration, {"tools/verification_plan.py": FAKE_PLAN}, "no perf suite")
        output = self.lane("promote", tip, "--ship", "--no-push", "--wait", "0")
        self.assertIn("no perf suite", output)
        self.assertEqual(self.perf_runs(), [])
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), tip)


class PromoteInstallDeployTests(LaneCase):
    """#569: promote --install --deploy runs promote, the Mac install from the gate worktree, check-installed, the Link
    package build and the deploy, each only after the previous one succeeded."""

    def setUp(self):
        super().setUp()
        self.steps = self.tmp / "steps.log"
        self.env.update(FIXTURE_STEPS=str(self.steps), STRAYLIGHT_UNPROMOTED="an orchestrator's leftover override")
        for name, path in STEPS.items():
            script = STEP_STAND_IN.format(name=name)
            if not path.endswith(".py"):
                script = f"#!{sys.executable}\n" + script
            self.write(self.integration, path, script).chmod(0o755)
        self.commit(self.integration, {}, "step stand-ins")
        self.git(self.repo, "branch", "dev")
        self.remote = self.tmp / "remote.git"
        self.git(self.tmp, "init", "-q", "--bare", str(self.remote))
        (self.tmp / "no-hooks").mkdir()
        self.git(self.repo, "config", "core.hooksPath", str(self.tmp / "no-hooks"))
        self.git(self.repo, "remote", "add", "origin", str(self.remote))
        self.git(self.repo, "push", "-q", "origin", "main", "dev")
        self.gate = os.path.realpath(self.repo / ".claude" / "worktrees" / "batch-gate")

    def ran(self):
        return [line.split("|") for line in self.steps.read_text().splitlines()] if self.steps.exists() else []

    def landed(self, lane_id, changes):
        worktree = self.created(lane_id, *changes)
        self.commit(worktree, changes)
        self.lane("check", lane_id)
        self.lane("land", lane_id)
        # promote waits for the lane review (#570); the stand-in reviewer finds nothing.
        self.lane("review", lane_id)
        return self.tip()

    def test_each_step_runs_in_the_gate_worktree_after_the_promotion(self):
        tip = self.landed("a", {"lib/extra.txt": "a\n"})
        output = self.lane("promote", tip, "--install", "--deploy", "max@link-host", "--wait", "0")
        self.assertEqual(self.git(self.remote, "rev-parse", "dev"), tip, "promote pushed dev first")
        package = str(self.state / "link-package")
        self.assertEqual(self.ran(), [
            ["install", self.gate, "", ""],
            [f"check-installed --expect {tip}", self.gate, "", ""],
            [f"link-package {package} linux/amd64", self.gate, "", ""],
            ["deploy max@link-host", self.gate, package, ""],
        ], "the steps ran in order, from the gate worktree, without the caller's override")
        self.assertEqual(self.git(self.gate, "rev-parse", "HEAD"), tip)
        self.assertIn(f"Installed and deployed {tip[:10]}", output)
        ledger = (self.state / "ledger.log").read_text()
        for step in STEPS:
            self.assertRegex(ledger, rf"promote-{step} {tip} exit=0")

    def test_install_alone_skips_the_link_steps(self):
        tip = self.landed("a", {"lib/extra.txt": "a\n"})
        self.lane("promote", tip, "--install", "--wait", "0")
        self.assertEqual([row[0] for row in self.ran()], ["install", f"check-installed --expect {tip}"])

    def test_a_refused_promote_installs_and_deploys_nothing(self):
        self.landed("corpus", {"checks/frozen-corpus.txt": "v2\n"})
        tip = self.landed("other", {"lib/other.txt": "other\n"})
        output = self.lane("promote", tip, "--install", "--deploy", "max@link-host", "--wait", "0", ok=False)
        self.assertIn("promote refused", output)
        self.assertEqual(self.ran(), [], "nothing was installed or deployed after the refused promote")
        self.assertNotEqual(self.git(self.remote, "rev-parse", "dev"), tip)

    def test_a_failed_step_stops_the_chain(self):
        tip = self.landed("a", {"lib/extra.txt": "a\n"})
        for failing, expected in (("install", ["install"]),
                                  ("check-installed", ["install", "check-installed"]),
                                  ("link-package", ["install", "check-installed", "link-package"])):
            with self.subTest(failing=failing):
                self.steps.unlink(missing_ok=True)
                self.env["FIXTURE_FAIL_STEP"] = failing
                output = self.lane("promote", tip, "--install", "--deploy", "max@link-host", "--wait", "0", ok=False)
                self.assertEqual([row[0].split()[0] for row in self.ran()], expected)
                self.assertIn(f"{failing} failed (exit 7)", output)
                self.assertRegex((self.state / "ledger.log").read_text(), rf"promote-{failing} {tip} exit=7")

    def test_install_and_deploy_need_the_push_and_deploy_needs_the_install(self):
        tip = self.landed("a", {"lib/extra.txt": "a\n"})
        runs = len(self.verify_runs())
        for arguments, refusal in (
                (("--install", "--no-push"), "--install and --deploy need the push"),
                (("--deploy", "max@link-host"), "--deploy needs --install")):
            with self.subTest(arguments=arguments):
                output = self.lane("promote", tip, *arguments, "--wait", "0", ok=False)
                self.assertIn(refusal, output)
        self.assertEqual(len(self.verify_runs()), runs, "the refusals come before the batch gate")
        self.assertEqual(self.ran(), [])
        self.assertNotEqual(self.git(self.repo, "rev-parse", "dev"), tip)


class SyncTests(LaneCase):
    """#666: local dev drifted ahead of origin/dev after promote --no-push, and only the owner could clean it up.
    lanefather sync catches local dev and main up with origin under the landing lock; --drop-local resets them to
    origin and keeps the dropped commits on a backup branch; every refusal names the command that does it right."""

    def setUp(self):
        super().setUp()
        self.git(self.repo, "branch", "dev")
        self.origin = self.tmp / "origin.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.git(self.repo, "push", "-q", "origin", "main", "dev")
        self.git(self.repo, "fetch", "-q", "origin")
        # The guard as Straylight, Sid-Kit and Data Grid run it, and an agent session around every lane call.
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        shutil.copy2(ROOT / "hooks" / "reference-transaction", hooks / "reference-transaction")
        self.git(self.repo, "config", "core.hooksPath", str(hooks))
        self.env = {k: v for k, v in self.env.items()
                    if not k.startswith(("CODEX_", "LANDING_")) or k == "CODEX_HOME"}
        self.env["CLAUDECODE"] = "1"
        self.landing = self.repo / ".git" / "landing.lock"
        self.other = self.tmp / "elsewhere"
        self.owner(self.tmp, "clone", "-q", str(self.origin), str(self.other))
        self.owner(self.other, "config", "user.name", "Elsewhere")
        self.owner(self.other, "config", "user.email", "elsewhere@example.com")

    def owner(self, cwd: Any, *args: str) -> str:
        """git as the owner in a terminal: no agent session, so the guard lets it through."""
        environment = {k: v for k, v in os.environ.items()
                       if k != "CLAUDECODE" and not k.startswith(("CODEX_", "LANDING_", "GIT_"))}
        return subprocess.run(["git", *args], cwd=cwd, env=environment, check=True, capture_output=True,
                              text=True).stdout.strip()

    def published(self, name: str, *branches: str) -> str:
        """A commit pushed to origin's branches from another clone; returns its SHA."""
        self.owner(self.other, "fetch", "-q", "origin")
        self.owner(self.other, "checkout", "-q", "-B", "work", f"origin/{branches[0]}")
        self.write(self.other, f"{name}.txt", name + "\n")
        self.owner(self.other, "add", "-A")
        self.owner(self.other, "commit", "-qm", f"published {name}")
        self.owner(self.other, "push", "-q", "origin", *[f"HEAD:refs/heads/{b}" for b in branches])
        return self.owner(self.other, "rev-parse", "HEAD")

    def stale(self, subject: str, branch: str = "dev") -> str:
        """A commit on local `branch` that origin never got, as promote --no-push left them (#666)."""
        parent = self.git(self.repo, "rev-parse", branch)
        tree = self.git(self.repo, "rev-parse", parent + "^{tree}")
        commit = self.owner(self.repo, "commit-tree", tree, "-p", parent, "-m", subject)
        self.owner(self.repo, "update-ref", f"refs/heads/{branch}", commit)
        return commit

    def rev(self, ref: str) -> str:
        return self.git(self.repo, "rev-parse", ref)

    def branches(self, pattern: str) -> list[str]:
        return self.git(self.repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}").split()

    def test_sync_fast_forwards_local_dev_and_main_to_origin_under_the_landing_lock(self):
        dev = self.published("dev-change", "dev")
        main = self.published("shipped", "dev", "main")
        # main is checked out in the main checkout; dev is not checked out anywhere.
        output = self.lane("sync", cwd=self.repo)
        self.assertEqual((self.rev("dev"), self.rev("main")), (main, main))
        self.git(self.repo, "merge-base", "--is-ancestor", dev, "dev")
        self.assertEqual((self.repo / "shipped.txt").read_text(), "shipped\n", "the checked-out main moved its files")
        self.assertEqual(self.git(self.repo, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertIn("dev", output)
        self.assertFalse(self.landing.exists(), "sync leaves the landing lock behind")
        self.assertRegex((self.state / "ledger.log").read_text(), "sync .*dev.*main")
        # The guard still refuses the same move outside the lock.
        self.published("later", "dev")
        self.git(self.repo, "fetch", "-q", "origin")
        refused = subprocess.run(["git", "update-ref", "refs/heads/dev", "refs/remotes/origin/dev"], cwd=self.repo,
                                 env=self.env, capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        # The vendored hook is Straylight's, which names its command.
        self.assertIn("lanefather sync", refused.stderr)
        # Up to date: nothing to move, and it says so.
        self.lane("sync", cwd=self.repo)
        self.assertIn("up to date", self.lane("sync", cwd=self.repo))

    def test_sync_fast_forwards_a_dev_checked_out_in_a_worktree(self):
        worktree = self.repo / ".claude" / "worktrees" / "dev"
        # The owner checks dev out: git worktree add writes ORIG_HEAD there, which the guard refuses an agent.
        self.owner(self.repo, "worktree", "add", "-q", str(worktree), "dev")
        tip = self.published("dev-change", "dev")
        self.lane("sync", cwd=self.integration)
        self.assertEqual(self.git(worktree, "rev-parse", "HEAD"), tip)
        self.assertEqual((worktree / "dev-change.txt").read_text(), "dev-change\n")

    def test_sync_refuses_local_commits_origin_lacks_and_names_drop_local(self):
        self.stale("stale dev work")
        before = (self.rev("dev"), self.rev("main"))
        self.published("shipped", "main", "dev")
        output = self.lane("sync", cwd=self.repo, ok=False)
        self.assertIn("local dev has 1 commit that is not on origin/dev", output)
        self.assertIn("stale dev work", output)
        self.assertIn("lanefather sync --drop-local", output)
        self.assertIn("nothing moved", output)
        self.assertEqual((self.rev("dev"), self.rev("main")), before, "a refused sync moves neither branch")
        self.assertEqual(self.branches("backup/*"), [])

    def test_drop_local_keeps_the_dropped_commits_on_a_backup_branch(self):
        today = datetime.date.today().isoformat()
        first = self.stale("stale dev work")
        # A stale commit keeps its parent's tree, so the checked-out main needs no checkout of its own.
        stale_main = self.stale("stale main work", "main")
        tip = self.published("dev-change", "dev")
        output = self.lane("sync", "--drop-local", cwd=self.repo)
        self.assertEqual(self.rev("dev"), tip)
        self.assertEqual(self.rev("main"), self.rev("origin/main"))
        self.assertEqual(self.rev(f"backup/dev-{today}"), first)
        self.assertEqual(self.rev(f"backup/main-{today}"), stale_main)
        self.assertIn(f"backup/dev-{today}", output)
        self.assertIn("stale dev work", output)
        self.assertIn("stale main work", output)
        self.assertEqual(self.git(self.repo, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertFalse(self.landing.exists())
        self.assertIn(f"backup/dev-{today}", (self.state / "ledger.log").read_text())
        # A second drop the same day gets the next free name.
        second = self.stale("more stale work")
        output = self.lane("sync", "--drop-local", cwd=self.repo)
        self.assertEqual(self.rev(f"backup/dev-{today}-2"), second)
        self.assertEqual(self.rev(f"backup/dev-{today}"), first)
        self.assertEqual(self.rev("dev"), tip)
        self.assertIn(f"backup/dev-{today}-2", output)

    def test_drop_local_refuses_a_dirty_worktree_on_dev(self):
        worktree = self.repo / ".claude" / "worktrees" / "dev"
        # The owner checks dev out: git worktree add writes ORIG_HEAD there, which the guard refuses an agent.
        self.owner(self.repo, "worktree", "add", "-q", str(worktree), "dev")
        stale = self.stale("stale dev work")
        self.write(worktree, "README.md", "uncommitted\n")
        self.published("dev-change", "dev")
        output = self.lane("sync", "--drop-local", cwd=self.repo, ok=False)
        self.assertIn("uncommitted changes", output)
        self.assertIn(str(worktree.resolve()), output)
        self.assertIn("nothing moved", output)
        self.assertEqual(self.rev("dev"), stale)
        self.assertEqual(self.branches("backup/*"), [])
        self.assertEqual((worktree / "README.md").read_text(), "uncommitted\n")

    def test_promote_no_push_refuses_a_sha_origin_does_not_have(self):
        tip = self.commit(self.integration, {"lib/extra.txt": "unpushed\n"}, "unpushed work")
        runs = len(self.verify_runs())
        output = self.lane("promote", tip, "--no-push", "--wait", "0", ok=False)
        self.assertIn(f"{tip[:10]} is not on origin/dev", output)
        self.assertIn("unpushed work lives on its own branch (agent/", output)
        self.assertIn(f"lanefather promote {tip[:10]}", output)
        self.assertEqual(len(self.verify_runs()), runs, "the refusal comes before the batch gate")
        self.assertEqual(self.rev("dev"), self.rev("origin/dev"))
        # A SHA origin already has moves the local branch.
        published = self.published("dev-change", "dev")
        self.git(self.repo, "fetch", "-q", "origin")
        self.lane("promote", published, "--no-push", "--wait", "0", cwd=self.repo)
        self.assertEqual(self.rev("dev"), published)

    def test_promote_names_sync_drop_local_for_stale_local_dev(self):
        self.stale("stale dev work")
        tip = self.commit(self.integration, {"lib/extra.txt": "batch\n"}, "the batch")
        output = self.lane("promote", tip, "--wait", "0", ok=False)
        self.assertIn("local dev is 1 commit ahead of origin; `lanefather sync --drop-local` drops them with a "
                      "backup", output)
        self.assertIn("local dev has commits that are not in", output)
        self.assertIn("Land them through a lane", output)
        self.assertIn("lanefather sync --drop-local", output.split("local dev has commits that are not in")[1])

    def test_list_shows_how_far_local_dev_is_ahead_of_origin(self):
        self.assertNotIn("ahead of origin", self.lane("list", cwd=self.repo))
        self.stale("one")
        self.stale("two")
        self.assertIn("local dev is 2 commits ahead of origin; `lanefather sync --drop-local` drops them with a "
                      "backup", self.lane("list", cwd=self.repo))

    def test_drop_local_reads_dev_under_the_lock_so_a_land_during_the_wait_is_kept(self):
        # Review of #666, finding 1: sync read dev before it waited for the lock, and reset --keep compared nothing,
        # so a merge a land made onto the checked-out dev meanwhile left dev without reaching the backup.
        worktree = self.repo / ".claude" / "worktrees" / "dev"
        self.owner(self.repo, "worktree", "add", "-q", str(worktree), "dev")
        self.stale("stale dev work")
        tip = self.published("dev-change", "dev")
        self.landing.write_text(f"token theirs\nholder tools/lane land other (pid 4242)\nacquired {int(time.time())}\n")
        process = subprocess.Popen([sys.executable, "-m", "lanefather", "sync", "--drop-local", "--wait", "60"], cwd=self.repo,
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        guard = self.repo / ".git" / "landing.lock.guard"
        deadline = time.monotonic() + 60
        while not guard.exists():
            self.assertLess(time.monotonic(), deadline, "sync never reached the landing lock")
            assert process.stdout is not None
            self.assertIsNone(process.poll(), process.stdout.read() if process.poll() is not None else "")
            time.sleep(0.05)
        # The land holding the lock merges M onto the checked-out dev, then lets go.
        self.write(worktree, "landed.txt", "M\n")
        self.owner(worktree, "add", "-A")
        self.owner(worktree, "commit", "-qm", "landed M")
        landed = self.owner(worktree, "rev-parse", "HEAD")
        self.landing.unlink()
        output = process.communicate(timeout=60)[0]
        self.assertEqual(process.returncode, 0, output)
        today = datetime.date.today().isoformat()
        self.assertEqual(self.rev(f"backup/dev-{today}"), landed, "the backup lost the commit landed during the wait")
        self.assertIn("landed M", output)
        self.assertEqual(self.rev("dev"), tip)

    def test_a_failed_drop_reports_the_failure_and_leaves_no_backup(self):
        # Review of #666, finding 5: an untracked file where origin's tree has one passes the dirty check, and
        # reset --keep refuses it; the backup and the "dropped" lines came before the reset.
        worktree = self.repo / ".claude" / "worktrees" / "dev"
        self.owner(self.repo, "worktree", "add", "-q", str(worktree), "dev")
        stale = self.stale("stale dev work")
        self.published("dev-change", "dev")
        self.write(worktree, "dev-change.txt", "mine, untracked\n")
        output = self.lane("sync", "--drop-local", cwd=self.repo, ok=False)
        self.assertIn("did not move", output)
        self.assertIn("dev-change.txt", output)
        self.assertNotIn("keeps the", output)
        self.assertEqual(self.rev("dev"), stale)
        self.assertEqual(self.branches("backup/*"), [])
        self.assertEqual((worktree / "dev-change.txt").read_text(), "mine, untracked\n")

    def test_promote_names_a_merge_of_origin_when_the_missing_commits_are_on_origin(self):
        # Review of #666, finding 3: sync --drop-local cannot help when local dev only holds what origin has.
        published = self.published("dev-change", "dev")
        self.git(self.repo, "fetch", "-q", "origin")
        self.owner(self.repo, "update-ref", "refs/heads/dev", published)
        tip = self.commit(self.integration, {"lib/extra.txt": "batch\n"}, "the batch")
        output = self.lane("promote", tip, "--wait", "0", ok=False)
        refusal = output.split("local dev has commits that are not in")[1]
        self.assertIn("published dev-change", refusal)
        self.assertIn("They are on origin/dev: merge origin/dev into the lane or integration branch and promote "
                      "the new tip", refusal)
        self.assertNotIn("--drop-local", refusal)
        self.assertEqual(self.rev("dev"), published)

    def test_sync_needs_the_remote(self):
        self.git(self.repo, "remote", "remove", "origin")
        self.assertIn("no remote origin", self.lane("sync", cwd=self.repo, ok=False))


if __name__ == "__main__":
    unittest.main()
