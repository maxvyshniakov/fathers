"""Unmarked P1/P2 review findings hold the batch gate, promote and a publish (fathers#109): table tests over finding
states and severities, the override, and the `findings` command a publish (Repo-Kit's release:publish) calls."""
from __future__ import annotations

from itertools import product
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest

from support import LaneCase

from lanefather import config, review, state


class LandingGateTableTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="lane-landing-findings-")))
        self.store = state.Store(config.Config(), directory)
        self.target: state.Record = {"id": "target", "status": "active", "common_dir": "/repo/.git"}

    def test_other_lanes_marks_severity_review_status_and_repository(self):
        # An open finding of an active lane reviewed before landing still belongs to this repository.
        self.store.save(self.target)
        state.write_json(self.store.receipt_path("target", "review"),
                         {"status": "finished", "findings": [{"n": 1, "severity": "P1"}]})
        marks: tuple[dict[str, str] | None, ...] = (
            None, {"kind": "fixed", "merge": "fix"}, {"kind": "rejected", "reason": "out of scope"})
        for mark, severity, status, same_repo in product(marks, ("P1", "P2", "P3"), ("running", "finished"),
                                                        (False, True)):
            with self.subTest(mark=mark, severity=severity, status=status, same_repo=same_repo):
                common = self.target["common_dir"] if same_repo else "/different/.git"
                self.store.save({"id": "a", "status": "active", "common_dir": common})
                finding = {"n": 1, "severity": severity, "mark": mark}
                state.write_json(self.store.receipt_path("a", "review"), {"status": status, "findings": [finding]})
                blockers, warnings = review.landing_findings(self.target, self.store)
                visible = mark is None and status == "finished" and same_repo
                self.assertEqual([lane["id"] for lane, _ in blockers], ["a"] if visible and severity != "P3" else [])
                self.assertEqual([lane["id"] for lane, _ in warnings], ["a"] if visible and severity == "P3" else [])

    def test_the_fix_suggestion_combines_landed_blockers(self):
        landed = [dict(self.target, id=lane_id, status="landed", landed={"merge": "merge"}, ticket="12")
                  for lane_id in ("a", "b")]
        commands = review.fix_round_commands(landed)
        self.assertEqual(commands, ["lanefather start <fix-id> --fix-of a,b --ticket <ticket> --claims <paths> --create"])
        commands = review.fix_round_commands(landed, alongside=["current"])
        self.assertIn("--fix-of current,a,b ", commands[0])

    def test_only_a_valid_named_parent_can_pass_its_own_findings(self):
        for lane_id in ("parent", "unrelated", "unlanded"):
            parent = dict(self.target, id=lane_id)
            if lane_id != "unlanded":
                parent.update(status="landed", landed={"merge": "merge"})
            self.store.save(parent)
            state.write_json(self.store.receipt_path(lane_id, "review"),
                             {"status": "finished", "findings": [{"n": 1, "severity": "P2"}]})
        fix = dict(self.target, review_fix_of="parent", review_fix_of_all=["parent", "unlanded"])
        blockers, warnings = review.landing_findings(fix, self.store)
        self.assertEqual([record["id"] for record, _ in blockers], ["unlanded", "unrelated"])
        self.assertEqual(warnings, [])

    def test_landing_findings_honors_configured_review_blocks(self):
        self.store.save(self.target)
        self.store.save({"id": "a", "status": "landed", "landed": {"merge": "merge"}, "common_dir": "/repo/.git"})
        state.write_json(
            self.store.receipt_path("a", "review"),
            {
                "status": "finished",
                "findings": [
                    {"n": 1, "severity": "P1"},
                    {"n": 2, "severity": "P2"},
                    {"n": 3, "severity": "P3"},
                ],
            },
        )
        p1_only = state.Store(config.Config(review_blocks=("P1",)), self.store.directory)
        blockers, warnings = review.landing_findings(self.target, p1_only)
        self.assertEqual([f["severity"] for _, f in blockers], ["P1"])
        self.assertEqual([f["severity"] for _, f in warnings], ["P2", "P3"])

        all_three = state.Store(config.Config(review_blocks=("P1", "P2", "P3")), self.store.directory)
        blockers, warnings = review.landing_findings(self.target, all_three)
        self.assertEqual([f["severity"] for _, f in blockers], ["P1", "P2", "P3"])
        self.assertEqual(warnings, [])

        none_blocking = state.Store(config.Config(review_blocks=()), self.store.directory)
        blockers, warnings = review.landing_findings(self.target, none_blocking)
        self.assertEqual(blockers, [])
        self.assertEqual([f["severity"] for _, f in warnings], ["P1", "P2", "P3"])


class ReviewGateTableTests(LaneCase):
    def setUp(self):
        super().setUp()
        self.git(self.repo, "branch", "dev")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/extra.txt": "a\n"})
        self.lane("check", "a")
        self.lane("land", "a")
        self.lane("review", "a")
        self.sha = self.tip()

    def findings(self, *found: dict[str, Any], status: str = "finished", pid: int = 999999999) -> None:
        """Lane a's review, finished (or not) with these findings."""
        path = self.state / "a.review.json"
        receipt = json.loads(path.read_text())
        receipt.update(status=status, findings=[{"n": n, "title": f"finding {n}", "file": "lib/extra.txt",
                                                 "line": 1, "mark": None, **f} for n, f in enumerate(found, 1)])
        if status == "running":
            receipt["pid"] = pid  # by default a process that is gone: the review died
        path.write_text(json.dumps(receipt))

    def answer(self, *extra: str) -> dict[str, Any]:
        return json.loads(self.lane("findings", self.sha, "--json", *extra, ok=None))

    def test_states_and_severities(self):
        outside = self.git(self.repo, "commit-tree", "-m", "a fix elsewhere", f"{self.sha}^{{tree}}")
        rows: list[tuple[list[dict[str, Any]], str, bool, int, int]] = [
            # (findings, review status, held, blocking, reported)
            ([{"severity": "P1"}], "finished", True, 1, 0),
            ([{"severity": "P2"}], "finished", True, 1, 0),
            ([{"severity": "P3"}], "finished", False, 0, 1),
            ([{"severity": "P1", "mark": {"kind": "rejected", "reason": "out of scope"}}], "finished", False, 0, 0),
            ([{"severity": "P2", "mark": {"kind": "fixed", "merge": self.sha}}], "finished", False, 0, 0),
            ([{"severity": "P2", "mark": {"kind": "fixed", "merge": outside}}], "finished", True, 1, 0),
            ([{"severity": "P3", "mark": {"kind": "fixed", "merge": outside}}], "finished", False, 0, 1),
            ([{"severity": "P3"}, {"severity": "P2"}], "finished", True, 1, 1),
            ([{"severity": "P1"}], "running", True, 0, 0),
            ([], "exempt", False, 0, 0),
        ]
        for found, status, held, blocking, reported in rows:
            with self.subTest(found=found, status=status):
                self.findings(*found, status=status)
                answer = self.answer()
                self.assertEqual((answer["held"], len(answer["blocking"]), len(answer["reported"])),
                                 (held, blocking, reported))
                self.assertEqual(bool(answer["unfinished"]), status == "running")
        (self.state / "a.review.json").unlink()
        self.assertEqual(self.answer()["unfinished"][0]["lane"], "a")

    def test_a_blocking_finding_holds_the_batch_gate_with_its_mark_commands_and_the_override_is_logged(self):
        self.findings({"severity": "P1"}, {"severity": "P3"})
        output = self.lane("promote", self.sha, "--no-push", "--wait", "0", ok=False)
        self.assertIn("P1/P2 review findings unmarked (or marked fixed outside the SHA) hold the batch gate and dev", output)
        self.assertIn("lanefather review a --mark 1 fixed:<merge> | lanefather review a --mark 1 rejected:<reason>",
                      output)
        self.assertIn("reported, does not block: lane a (#404): finding #2 P3", output)
        self.assertNotIn("FULL structure,lib,native,host-journeys", self.verify_runs())
        self.lane("promote", self.sha, "--no-push", "--wait", "0", "--force-reason", "owner accepted the risk")
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), self.sha)
        self.assertRegex((self.state / "ledger.log").read_text(),
                         rf"override promote {self.sha}: owner accepted the risk \(lane a \(#404\): finding #1 P1")

    def test_a_running_review_lets_the_batch_gate_run_and_holds_dev(self):
        self.findings(status="running", pid=os.getpid())
        output = self.lane("promote", self.sha, "--no-push", "--wait", "0", ok=False)
        self.assertIn("batch gate green", output, "the gate runs while reviews finish (#570)")
        self.assertIn("dev waits for the review of every lane landed since dev", output)
        self.assertIn("lane a (#404): review running since", output)
        self.assertNotEqual(self.git(self.repo, "rev-parse", "dev"), self.sha)

    def test_a_state_that_knows_no_lane_of_the_range_holds_rather_than_clears(self):
        self.env["LANEFATHER_STATE"] = str(self.tmp / "elsewhere" / "lanes")
        answer = self.answer()
        self.assertTrue(answer["held"])
        self.assertEqual(answer["unknown"][0]["merge"], self.sha)
        self.assertIn("no lane state here knows it", self.lane("findings", self.sha, ok=False))

    def test_p3_alone_does_not_block_unless_the_project_says_so(self):
        self.findings({"severity": "P3"})
        self.assertFalse(self.answer()["held"])
        config = (self.integration / ".lanefather.toml").read_text() + '\n[review]\nblocks = ["P1", "P2", "P3"]\n'
        (self.integration / ".lanefather.toml").write_text(config)
        answer = self.answer()
        self.assertEqual((answer["held"], answer["blocks"]), (True, ["P1", "P2", "P3"]))

    def test_land_honors_configured_review_blocks_for_other_lanes(self):
        base_config = (self.integration / ".lanefather.toml").read_text()
        self.commit(self.integration, {".lanefather.toml": base_config + '\n[review]\nblocks = ["P1"]\n'}, "blocks P1")
        self.assertIn("unmarked P1 findings hold the merge", self.lane("land", "--help"))
        self.findings({"severity": "P2"})
        worktree_b = self.created("b", "calls/**")
        self.commit(worktree_b, {"calls/b.txt": "foo\n"})
        self.lane("check", "b")
        land_b = self.lane("land", "b")
        self.assertIn("reported (P2, does not block): lane a (#404): finding #1 P2", land_b)

        self.commit(
            self.integration,
            {".lanefather.toml": base_config + '\n[review]\nblocks = ["P1", "P2", "P3"]\n'},
            "blocks P1-P3",
        )
        self.assertIn("unmarked P1/P2/P3 findings hold the merge", self.lane("land", "--help"))
        self.findings({"severity": "P3"})
        worktree_c = self.created("c", "docs/**")
        self.commit(worktree_c, {"docs/c.md": "c\n"})
        self.lane("check", "c")
        previous = self.tip()
        blocked = self.lane("land", "c", ok=False)
        self.assertEqual(self.tip(), previous)
        self.assertIn("land refused: other finished reviews have unmarked P1/P2/P3 findings", blocked)
        self.assertIn("lane a (#404): finding #1 P3 lib/extra.txt:1 finding 1", blocked)

    def test_the_publish_path_holds_with_exit_1_and_an_override_answers_0_logged(self):
        self.findings({"severity": "P2"})
        output = self.lane("findings", self.sha, ok=False)
        self.assertIn("held: lane a (#404): finding #1 P2 lib/extra.txt:1 finding 1", output)
        # A publish compares against its last release, not dev: lanes landed after it count.
        base = self.git(self.repo, "rev-list", "--max-parents=0", "HEAD")
        self.assertTrue(self.answer("--since", base)["held"])
        self.assertFalse(self.answer("--since", self.sha)["held"])
        self.lane("findings", self.sha, "--force-reason", "release of a hotfix")
        self.assertRegex((self.state / "ledger.log").read_text(), rf"override findings {self.sha}: release of a hotfix")


class PreLandReviewGateTests(LaneCase):
    """Pre-land review gating on `land` (fathers#242): running, unmarked, unreviewed commits, and clean merges."""

    def test_running_pre_land_review_refuses_land_unless_forced(self):
        worktree = self.created("a", "lib/**")
        tip = self.commit(worktree, {"lib/api.txt": "foo\nbar\n"})
        self.lane("check", "a")
        rec = self.record("a")
        (self.state / "a.review.json").write_text(
            json.dumps({
                "version": 1, "label": "a", "kind": "lane", "lane": "a", "ticket": "404",
                "lane_created": rec["created"], "pre_land": True, "tip": tip,
                "tree": self.git(worktree, "rev-parse", f"{tip}^{{tree}}"),
                "status": "running", "pid": os.getpid(), "started": "2026-11-24T00:00:00Z",
                "log": str(self.state / "logs" / "review-a.log"), "effort": "medium",
            })
        )
        refused = self.lane("land", "a", ok=False)
        self.assertIn("land refused", refused)
        self.assertIn("pre-land review is still running", refused)
        self.assertEqual(self.record("a")["status"], "active")

        forced = self.lane("land", "a", "--force-reason", "urgent hotfix")
        self.assertIn("post-merge green", forced)
        self.assertIn("override land a: urgent hotfix", (self.state / "ledger.log").read_text())

    def test_amended_commit_after_pre_land_review_refuses_land(self):
        worktree = self.created("a", "lib/**")
        reviewed_sha = self.commit(worktree, {"lib/api.txt": "foo\nbar\n"})
        self.lane("check", "a")
        self.lane("review", "a")

        # Rewrite history so reviewed_sha is no longer an ancestor of tip.
        self.write(worktree, "lib/api.txt", "foo\namended\n")
        self.git(worktree, "add", "lib/api.txt")
        self.git(worktree, "commit", "--amend", "-qm", "amended commit")
        self.lane("check", "a")

        refused = self.lane("land", "a", ok=False)
        self.assertIn("land refused", refused)
        self.assertIn(f"does not contain pre-land reviewed commit {reviewed_sha[:10]}", refused)

    def test_previous_generation_pre_land_review_does_not_block_new_lane(self):
        # Generation 1: pre-land reviewed with unmarked findings, then dropped after clearing.
        wt1 = self.created("a", "lib/**")
        tip1 = self.commit(wt1, {"lib/api.txt": "foo\ngen1\n"})
        rec1 = self.record("a")
        self.lane("check", "a")
        self.lane("land", "a")
        self.lane("review", "a")
        self.assertEqual(self.record("a")["status"], "done")
        # Plant a gen-1 pre-land running review receipt on disk.
        (self.state / "a.review.json").write_text(
            json.dumps({
                "version": 1, "label": "a", "kind": "lane", "lane": "a", "ticket": "404",
                "lane_created": rec1["created"], "pre_land": True, "tip": tip1,
                "status": "running", "pid": os.getpid(), "started": "2026-11-24T00:00:00Z",
                "log": str(self.state / "logs" / "review-a.log"), "effort": "medium",
            })
        )
        # Generation 2: new lane with same id must ignore gen-1's receipt and land normally.
        wt2 = self.created("a", "lib/**")
        self.commit(wt2, {"lib/api.txt": "foo\ngen1\ngen2\n"})
        self.lane("check", "a")
        land_out = self.lane("land", "a")
        self.assertIn("post-merge green", land_out)

    def test_fix_sha_missing_from_tip_or_reverted_refuses_land(self):
        # P1 #1: fixed:<sha> not an ancestor of tip (reset) or reverted back to reviewed_tree refuses land.
        self.review_text.write_text(
            "### P2 — lib/api.txt:1 — Needs fix\nFix it.\n"
        )
        worktree = self.created("a", "lib/**", "calls/**")
        reviewed_sha = self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.lane("review", "a")

        fix_sha = self.commit(worktree, {"calls/main.txt": "foo\nbaz\n"}, "fix")
        self.lane("review", "a", "--mark", "1", f"fixed:{fix_sha}")

        # Case 1: reset back to reviewed_sha so fix_sha is no longer an ancestor of tip.
        self.git(worktree, "reset", "--hard", reviewed_sha)
        self.lane("check", "a")
        reset_refused = self.lane("land", "a", ok=False)
        self.assertIn("does not contain fix commit", reset_refused)

        # Case 2: restore fix_sha then revert it so tree == reviewed_tree while fix_sha is an ancestor.
        self.git(worktree, "reset", "--hard", fix_sha)
        self.git(worktree, "revert", "--no-edit", fix_sha)
        self.lane("check", "a")
        revert_refused = self.lane("land", "a", ok=False)
        self.assertIn("matches the unfixed pre-land reviewed commit", revert_refused)

    def test_side_branch_merge_and_conflicted_integration_merge_refuse_land(self):
        # P1 #3 + P1 #4: side-branch merge marked fixed:M and conflicted integration merge both refuse land.
        self.review_text.write_text("### P2 — lib/api.txt:1 — Needs fix\nFix it.\n")
        worktree = self.created("a", "lib/**", "calls/**")
        reviewed_sha = self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.lane("review", "a")

        # Side branch with unreviewed commits merged into lane/a and marked fixed:M.
        self.git(worktree, "checkout", "-b", "side-unreviewed", reviewed_sha)
        self.commit(worktree, {"calls/main.txt": "foo\nbaz\n"}, "side commit 1")
        self.commit(worktree, {"lib/extra.txt": "sneaky\n"}, "side commit 2")
        self.git(worktree, "checkout", "lane/a")
        self.git(worktree, "merge", "--no-ff", "-m", "merge side", "side-unreviewed")
        merge_sha = self.git(worktree, "rev-parse", "HEAD")
        self.lane("review", "a", "--mark", "1", f"fixed:{merge_sha}")
        self.lane("check", "a")
        side_refused = self.lane("land", "a", ok=False)
        self.assertIn("unreviewed merge commits", side_refused)

        # Reset and test evil/modified integration merge (extra edit folded into merge commit).
        self.git(worktree, "reset", "--hard", reviewed_sha)
        fix_sha = self.commit(worktree, {"calls/main.txt": "foo\nbaz\n"}, "real fix")
        self.lane("review", "a", "--mark", "1", f"fixed:{fix_sha}")
        self.commit(self.integration, {"docs/other.md": "other\n"}, "integration advance")
        self.git(worktree, "merge", "--no-ff", "--no-commit", "integrate/q")
        self.write(worktree, "lib/api.txt", "foo\nbaz\nevil\n")
        self.git(worktree, "add", "lib/api.txt")
        self.git(worktree, "commit", "-qm", "evil integration merge")
        self.lane("check", "a")
        evil_refused = self.lane("land", "a", ok=False)
        self.assertIn("unreviewed merge commits", evil_refused)

    def test_p3_only_pre_land_finding_does_not_block_land(self):
        # P2 #6: P3-only unmarked finding does not block land when review_blocks is P1/P2.
        self.review_text.write_text(
            '```json\n{"findings": [{"severity": "P3", "file": "lib/api.txt", "line": 1, '
            '"title": "Minor nit", "detail": "Just a nit."}]}\n```\n'
        )
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbar\n"})
        self.lane("check", "a")
        self.lane("review", "a")

        land_out = self.lane("land", "a")
        self.assertIn("post-merge green", land_out)
        self.assertIn("review kept", land_out)

    def test_invalid_reviewed_tip_fails_closed(self):
        # P2 #7: Complete receipt with missing/invalid tip OID refuses land.
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbar\n"})
        self.lane("check", "a")
        self.lane("review", "a")
        receipt = self.review("a")
        receipt["tip"] = "not-an-oid"
        (self.state / "a.review.json").write_text(json.dumps(receipt))
        refused = self.lane("land", "a", ok=False)
        self.assertIn("no valid reviewed commit SHA", refused)

    def test_forced_land_with_stale_pre_land_review_clears_stale_receipt_and_audits_promoted(self):
        # P2 #8: Forced land with unreviewed commits clears stale pre-land receipt (no auto-done,
        # fresh post-land review required) and logs unreviewed override when landing onto dev.
        dev_wt = self.repo / ".claude" / "worktrees" / "dev"
        self.git(self.repo, "worktree", "add", "-q", "-b", "dev", str(dev_wt))
        self.lane("start", "a", "--ticket", "404", "--base", "dev", "--claims", "lib/**", "--create")
        worktree = self.record("a")["worktree"]
        self.commit(worktree, {"lib/api.txt": "foo\nbar\n"})
        self.lane("check", "a")
        self.lane("review", "a")
        self.commit(worktree, {"lib/api.txt": "foo\nbar\nextra\n"}, "unreviewed extra")
        self.lane("check", "a")

        forced = self.lane("land", "a", "--into", str(dev_wt), "--force-reason", "hotfix override")
        self.assertIn("post-merge green", forced)
        self.assertIn("review not started", forced)
        # Must NOT auto-finish with the stale pre-land review.
        self.assertEqual(self.record("a")["status"], "landed")
        self.assertFalse((self.state / "a.review.json").exists())
        ledger = (self.state / "ledger.log").read_text()
        self.assertIn("override land a onto dev unreviewed: hotfix override", ledger)
        self.assertNotIn("land a onto dev:", ledger)


if __name__ == "__main__":
    unittest.main()
