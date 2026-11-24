"""Tests of review scope, reused lane IDs, and stale/delta review lifecycle (lanefather.review)."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
from typing import Any
import unittest

from support import NO_FINDINGS, TWO_FINDINGS, LaneCase, close_process

from lanefather import review, scope

DECISION = "https://github.com/fixture/straylight/issues/161#issuecomment-2541"
LAND_DECISION = "https://github.com/fixture/straylight/issues/161#issuecomment-2542"
SPEC = "docs/specs/lanes.md"
ISSUES: dict[str, Any] = {
    "161": {"number": 161, "title": "Review the whole scope", "body": "Body of 161: every ticket is reviewed.",
            "url": "https://github.com/fixture/straylight/issues/161", "state": "OPEN",
            "updatedAt": "2026-11-01T10:00:00Z"},
    "64": {"number": 64, "title": "Scope in reviews", "body": "Body of 64: decisions and specs too.",
           "url": "https://github.com/fixture/straylight/issues/64", "state": "OPEN",
           "updatedAt": "2026-11-02T10:00:00Z"},
}
COMMENTS: dict[str, Any] = {
    "repos/fixture/straylight/issues/comments/2541": {
        "body": "Owner decision: the gate stays non-blocking on incomplete scope.", "user": {"login": "owner"},
        "updated_at": "2026-11-03T10:00:00Z",
        "issue_url": "https://api.github.com/repos/fixture/straylight/issues/161"},
    "repos/fixture/straylight/issues/comments/2542": {
        "body": "<!-- lanefather:land -->\nLanded on `integrate/q` as abc: the land comment text.",
        "user": {"login": "lanefather"}, "updated_at": "2026-11-04T10:00:00Z",
        "issue_url": "https://api.github.com/repos/fixture/straylight/issues/161"},
}


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class ScopeReviewTests(LaneCase):
    """fathers#192: the lane review reads the whole accepted scope (every ticket without its comments, each
    decision's one comment, each spec at the review's base), and its receipt keeps what it read."""

    def setUp(self):
        super().setUp()
        # The ticket thread holds an implementer's report and a land comment; neither may reach the reviewer.
        self.comments.write_text(json.dumps([
            {"body": "Implementer report: every part is done, trust me", "createdAt": "2999-01-01T00:00:00Z"},
            {"body": "<!-- lanefather:land -->\nLanded: the thread's land comment",
             "createdAt": "2999-01-01T00:00:00Z"},
        ]))
        self.gh_answers(ISSUES, COMMENTS)

    def gh_answers(self, issues: dict[str, Any], comments: dict[str, Any]) -> None:
        self.write(self.tmp, "issues.json", json.dumps(issues))
        self.write(self.tmp, "api.json", json.dumps(comments))
        self.env.update(FAKE_GH_ISSUES=str(self.tmp / "issues.json"), FAKE_GH_API=str(self.tmp / "api.json"))

    def reviewed(self, lane_id: str, *options: str, changes: dict[str, str], land: bool = True) -> str:
        self.scoped(lane_id, *options, claims=("lib/**", "docs/**"))
        self.commit(self.record(lane_id)["worktree"], changes)
        self.lane("check", lane_id)
        if land:
            self.lane("land", lane_id)
        return self.lane("review", lane_id)

    def prompt(self, name: str = "codex") -> tuple[str, str]:
        """The whole prompt and its scope section, `What the change is for`."""
        prompt = (self.tmp / f"{name}.log.prompt").read_text()
        return prompt, prompt.split("## What the change is for", 1)[1].split("## The change", 1)[0]

    def test_every_ticket_decision_and_spec_at_the_base_reaches_the_reviewer_and_the_receipt(self):
        self.commit(self.integration, {SPEC: "Spec at the base: lanes answer to their scope.\n"}, "the spec")
        output = self.reviewed("a", "--ticket", "161", "--partial", "64", "--decision", DECISION, "--spec", SPEC,
                               changes={"lib/api.txt": "foo\nbaz\n", SPEC: "Spec rewritten on the lane.\n"})
        prompt, part = self.prompt()
        for text in ("The lane's accepted scope, 4 items: 2 tickets (1 partial), 1 decision, 1 spec.",
                     "### 1. Ticket #161 (the lane completes it): Review the whole scope",
                     "Body of 161: every ticket is reviewed.",
                     "Source: https://github.com/fixture/straylight/issues/161; updated 2026-11-01T10:00:00Z",
                     "### 2. Ticket #64 (partial: the lane does only part of it): Scope in reviews",
                     "Body of 64: decisions and specs too.",
                     "do not report the ticket's other parts as missing",
                     f"### 3. Decision: comment by @owner on #161\n\nSource: {DECISION}; updated 2026-11-03T10:00:00Z",
                     "Owner decision: the gate stays non-blocking on incomplete scope.",
                     f"### 4. Spec: {SPEC}", "Spec at the base: lanes answer to their scope."):
            self.assertIn(text, part)
        self.assertNotIn("Spec rewritten on the lane.", part, "the spec is read at the base, not the lane tip")
        self.assertNotIn("Unavailable scope", part)
        for leaked in ("Implementer report", "lanefather:land", "land comment"):
            self.assertNotIn(leaked, prompt, "no ticket comment reaches the reviewer")
        receipt = self.review("a")
        base = receipt["base"]
        blob = self.git(self.repo, "rev-parse", f"{base}:{SPEC}")
        self.assertEqual(receipt["scope"], [
            {"item": {"issue": "161"}, "status": "ok", "source": "https://github.com/fixture/straylight/issues/161",
             "version": "2026-11-01T10:00:00Z", "digest": sha("Body of 161: every ticket is reviewed.")},
            {"item": {"issue": "64", "partial": True}, "status": "ok",
             "source": "https://github.com/fixture/straylight/issues/64", "version": "2026-11-02T10:00:00Z",
             "digest": sha("Body of 64: decisions and specs too.")},
            {"item": {"decision": DECISION}, "status": "ok", "source": DECISION, "version": "2026-11-03T10:00:00Z",
             "digest": sha("Owner decision: the gate stays non-blocking on incomplete scope.")},
            {"item": {"spec": SPEC}, "status": "ok", "source": f"{SPEC}@{base}", "version": blob,
             "digest": sha("Spec at the base: lanes answer to their scope.")},
        ])
        self.assertIs(receipt["scope_complete"], True)
        for entry in receipt["scope"]:
            self.assertIn(f"digest {entry['digest']}", part)
        self.assertIn("Scope reviewed: #161, #64 (partial), 1 decision, 1 spec", output)
        self.assertNotIn("scope changed", output)
        gh = (self.tmp / "gh.log").read_text().splitlines()
        self.assertIn("issue view 161 --json number,title,body,url,state,updatedAt", gh)
        self.assertIn("issue view 64 --json number,title,body,url,state,updatedAt", gh)
        self.assertIn("api repos/fixture/straylight/issues/comments/2541", gh)

    def test_scope_it_cannot_read_is_named_for_the_reviewer_and_leaves_the_receipt_incomplete(self):
        long_body = "x" * 25_000
        self.gh_answers({"161": {**ISSUES["161"], "body": long_body}, "77": "HTTP 502: Bad Gateway"}, COMMENTS)
        self.commit(self.integration, {"docs/specs/keep.md": "kept\n", "docs/specs/blob.bin": "\x00\x01\n"}, "specs")
        output = self.reviewed("a", "--ticket", "161", "--partial", "999", "--partial", "77",
                               "--decision", LAND_DECISION, "--spec", "docs/specs/new.md",
                               "--spec", "docs/specs", "--spec", "docs/specs/blob.bin",
                               changes={"lib/api.txt": "foo\nbaz\n", "docs/specs/new.md": "Spec added on the lane.\n"})
        prompt, part = self.prompt()
        shown = "x" * 20_000 + "\n\n[truncated at 20000 chars]"
        self.assertIn(shown, part)
        self.assertNotIn("x" * 20_001, part)
        self.assertNotIn("the land comment text", prompt, "a land comment is never shown as a decision")
        self.assertNotIn("Spec added on the lane.", part)
        unavailable = part.split("### Unavailable scope", 1)[1]
        for text in ("Report one P3 finding", "- 2. ticket #999 (partial): missing",
                     "Could not resolve to an issue", "- 3. ticket #77 (partial): unreadable",
                     "HTTP 502", f"- 4. decision {LAND_DECISION}: refused",
                     "a land comment is Lanefather's report of a merge, not a decision",
                     "- 5. spec docs/specs/new.md: missing", "a spec that is not at the base is not accepted scope",
                     "- 6. spec docs/specs: unreadable (docs/specs is a directory at the base, not a file)",
                     "- 7. spec docs/specs/blob.bin: unreadable (docs/specs/blob.bin is not UTF-8 text at the base)"):
            self.assertIn(text, unavailable)
        receipt = self.review("a")
        self.assertEqual([(e["status"], e["digest"]) for e in receipt["scope"]],
                         [("truncated", sha(shown)), ("missing", None), ("unreadable", None), ("refused", None),
                          ("missing", None), ("unreadable", None), ("unreadable", None)])
        self.assertTrue(all(e.get("reason") for e in receipt["scope"][1:]))
        self.assertIs(receipt["scope_complete"], False)
        self.assertEqual(receipt["status"], "finished", "an incomplete scope does not fail the review")
        self.assertIn("Scope reviewed: #161, #999 (partial), #77 (partial), 1 decision, 3 specs (not seen: ticket "
                      f"#999 (partial) missing; ticket #77 (partial) unreadable; decision {LAND_DECISION} refused; "
                      "spec docs/specs/new.md missing; spec docs/specs unreadable; spec docs/specs/blob.bin "
                      "unreadable)", output)

    def test_a_scope_changed_after_the_review_is_named_beside_it(self):
        self.reviewed("a", "--ticket", "161", changes={"lib/api.txt": "foo\nbaz\n"}, land=False)
        self.assertEqual(self.review("a")["scope"][0]["item"], {"issue": "161"})
        self.lane("scope", "a", "--add-partial", "64")
        output = self.lane("review", "a")
        self.assertIn("Scope reviewed: #161", output)
        self.assertIn("Warning: scope changed since the review: now #161, #64 (partial); lanefather review a "
                      "--force-reason <why> reviews it again", output)
        shown = json.loads(self.lane("show", "a", "--json"))
        self.assertEqual(shown["ticket"]["number"], "161")
        self.assertEqual(shown["tickets"], [{"number": "161", "url": None, "partial": False},
                                            {"number": "64", "url": None, "partial": True}])
        [entry] = [lane for project in json.loads(self.lane("status", "--json"))["projects"]
                   for lane in project.get("lanes", []) if lane["id"] == "a"]
        self.assertEqual(entry["tickets"], shown["tickets"])

    def test_a_lane_without_a_ticket_keeps_its_line_and_a_single_ticket_lane_reads_as_before(self):
        self.assertEqual(scope.prompt_text([]), "The lane has no ticket.")
        self.gh_answers({}, {})
        self.env.pop("FAKE_GH_ISSUES")
        self.reviewed("a", "--ticket", "404", changes={"lib/api.txt": "foo\nbaz\n"})
        _prompt, part = self.prompt()
        self.assertIn("### 1. Ticket #404 (the lane completes it): Fixture ticket title", part)
        self.assertIn("The fixture ticket body.", part)
        self.assertEqual(self.review("a")["scope"][0]["status"], "ok")

    def test_decision_for_another_issue_or_repo_is_refused_and_corrupt_receipt_scope_is_reported(self):
        wrong_issue = "https://github.com/fixture/straylight/issues/161#issuecomment-2543"
        wrong_repo = "https://github.com/fixture/straylight/issues/161#issuecomment-2544"
        self.gh_answers(ISSUES, {
            **COMMENTS,
            "repos/fixture/straylight/issues/comments/2543": {
                "body": "Decision on another issue.", "user": {"login": "owner"},
                "updated_at": "2026-11-05T10:00:00Z",
                "issue_url": "https://api.github.com/repos/fixture/straylight/issues/999"},
            "repos/fixture/straylight/issues/comments/2544": {
                "body": "Decision from another repo.", "user": {"login": "owner"},
                "updated_at": "2026-11-05T10:00:00Z",
                "issue_url": "https://api.github.com/repos/other/repo/issues/161"},
        })
        self.reviewed("a", "--ticket", "161", "--decision", wrong_issue, "--decision", wrong_repo,
                      changes={"lib/api.txt": "foo\nbaz\n"})
        prompt, part = self.prompt()
        self.assertNotIn("Decision on another issue.", prompt)
        self.assertNotIn("Decision from another repo.", prompt)
        self.assertIn(f"- 2. decision {wrong_issue}: refused", part)
        self.assertIn(f"- 3. decision {wrong_repo}: refused", part)
        receipt = self.review("a")
        self.assertEqual([e["status"] for e in receipt["scope"]], ["ok", "refused", "refused"])
        self.assertIs(receipt["scope_complete"], False)
        self.assertEqual(review.scope_lines({"kind": "lane", "scope": ["bad"]}, None, "a", "lanefather"),
                         ["Scope reviewed: the receipt's scope is unreadable (entry is not an object)"])
        self.assertEqual(review.scope_lines({"kind": "lane"}, None, "a", "lanefather"), [])


class ReusedLaneIdReviewTests(LaneCase):
    """fathers#54: a reused lane id after `done` never inherits the previous incarnation's receipts, review or gate."""

    def test_reused_lane_id_does_not_inherit_receipts_review_or_gate(self):
        # Generation 1: clean review and done.
        self.review_text.write_text(NO_FINDINGS)
        wt1 = self.created("a", "lib/api.txt")
        self.commit(wt1, {"lib/api.txt": "foo\ngen1\n"})
        self.lane("check", "a")
        created1 = self.record("a")["created"]
        self.assertEqual(json.loads((self.state / "a.receipt.json").read_text())["lane_created"], created1)
        self.assertEqual(json.loads((self.state / "a.first.json").read_text())["lane_created"], created1)
        self.lane("land", "a")
        self.assertEqual(json.loads((self.state / "a.post-merge.json").read_text())["lane_created"], created1)
        self.lane("review", "a")
        self.assertEqual(self.review("a")["lane_created"], created1)
        calls_after_gen1 = len(self.codex_calls())
        # Auto-done finishes the lane after a clean review and green smoke.
        self.assertEqual(self.record("a")["status"], "done")

        # Generation 2: re-register the same lane id.
        wt2 = self.created("a", "lib/api.txt")
        created2 = self.record("a")["created"]
        self.assertNotEqual(created2, created1)

        # Before gen 2 runs check/review, list/show/status must report no receipt and no review.
        listed = next(lane for lane in json.loads(self.lane("list", "--json"))["lanes"] if lane["id"] == "a")
        self.assertEqual(listed["receipt"], "no receipt")
        shown = json.loads(self.lane("show", "a", "--json"))
        self.assertEqual(shown["checks"]["status"], "not run")
        self.assertEqual(shown["review"], {"status": "not started", "findings": 0, "unresolved": 0})
        status_lane = next(
            lane
            for project in json.loads(self.lane("status", "--json", cwd=self.tmp))["projects"]
            for lane in project.get("lanes", [])
            if lane["id"] == "a"
        )
        self.assertEqual(status_lane["receipt"]["status"], "not run")
        self.assertIsNone(status_lane["receipt"]["path"])
        self.assertEqual(status_lane["review"], {"status": "not started", "open": 0, "findings": 0})

        # Commit, check and land gen 2 with findings; gen 1's clean review must NOT be kept.
        self.review_text.write_text(TWO_FINDINGS)
        self.commit(wt2, {"lib/api.txt": "foo\ngen2\n"})
        self.lane("check", "a")
        self.assertEqual(json.loads((self.state / "a.receipt.json").read_text())["lane_created"], created2)
        self.assertEqual(json.loads((self.state / "a.first.json").read_text())["lane_created"], created2)
        land_out = self.lane("land", "a")
        self.assertNotIn("review kept", land_out)
        self.assertEqual(json.loads((self.state / "a.post-merge.json").read_text())["lane_created"], created2)

        # Review gate must wait for gen 2's review instead of passing on gen 1's clean review.
        sha = self.tip()
        gate_before = json.loads(self.lane("findings", sha, "--json", ok=None))
        self.assertTrue(gate_before["held"])
        self.assertEqual([u["lane"] for u in gate_before["unfinished"]], ["a"])

        # Running review on gen 2 executes a new review and records gen 2's findings.
        rev_out = self.lane("review", "a")
        self.assertGreater(len(self.codex_calls()), calls_after_gen1)
        self.assertIn("2 findings", rev_out)
        rev2 = self.review("a")
        self.assertEqual(rev2["lane_created"], created2)
        self.assertEqual(len(rev2["findings"]), 2)

    def test_reused_lane_id_does_not_inherit_exempt_review(self):
        # Generation 1: docs-only lane lands with an exempt review and auto-finishes to done.
        wt1 = self.created("a", "docs/note.md")
        self.commit(wt1, {"docs/note.md": "doc only\n"})
        self.lane("check", "a")
        out1 = self.lane("land", "a")
        self.assertIn("review exempt", out1)
        self.assertEqual(self.review("a")["lane_created"], self.record("a")["created"])
        self.assertEqual(self.record("a")["status"], "done")

        # Generation 2: code change with same id must NOT inherit gen 1's exempt review.
        wt2 = self.created("a", "lib/api.txt")
        self.commit(wt2, {"lib/api.txt": "foo\ncode change\n"})
        self.lane("check", "a")
        out2 = self.lane("land", "a")
        self.assertNotIn("review kept", out2)
        self.assertNotIn("review exempt", out2)
        gate = json.loads(self.lane("findings", self.tip(), "--json", ok=None))
        self.assertTrue(gate["held"])
        self.assertEqual([u["lane"] for u in gate["unfinished"]], ["a"])

    def test_reused_lane_id_fix_parent_lookup_ignores_previous_generation_findings(self):
        # Generation 1: landed with findings, then finished; leave a gen-1 review with unmarked findings on disk.
        self.review_text.write_text(TWO_FINDINGS)
        wt1 = self.created("a", "lib/api.txt")
        self.commit(wt1, {"lib/api.txt": "foo\ngen1\n"})
        self.lane("check", "a")
        self.lane("land", "a")
        self.lane("review", "a")
        gen1_review = self.review("a")
        self.lane("review", "a", "--mark", "1", "rejected:ok", "--mark", "2", "rejected:ok")
        self.assertEqual(self.record("a")["status"], "done")
        # Restore gen-1's unmarked review receipt (stamped with gen-1's lane_created) on disk.
        (self.state / "a.review.json").write_text(json.dumps(gen1_review, indent=2) + "\n")

        # Generation 2: re-register "a" and land it without running its review yet.
        wt2 = self.created("a", "lib/api.txt")
        self.commit(wt2, {"lib/api.txt": "foo\ngen2\n"})
        self.lane("check", "a")
        self.lane("land", "a", "--keep")

        # start --fix-of a and review a --fix-lane must refuse (ignore gen-1's unmarked findings).
        out_start = self.lane("start", "b", "--fix-of", "a", "--ticket", "404", "--claims", "lib/api.txt", ok=False)
        self.assertIn("no finished review with unmarked findings", out_start)
        out_fix = self.lane("review", "a", "--fix-lane", ok=False)
        self.assertIn("no finished review with unmarked findings", out_fix)

        # Landing a lane claiming --fix-of a onto a promoted branch (main) must also refuse without review
        # because review_exemption checks generation-aware fix_parent state.
        wt_b = self.created("b", "lib/b.txt")
        self.commit(wt_b, {"lib/b.txt": "b\n"})
        self.lane("check", "b")
        rec_b = self.record("b")
        rec_b["review_fix_of"] = "a"
        (self.state / "b.json").write_text(json.dumps(rec_b, indent=2) + "\n")
        out_land = self.lane("land", "b", "--into", str(self.repo), ok=False)
        self.assertIn("land refused", out_land)
        self.assertIn("main", out_land)

    def test_pre_land_review_fix_in_same_lane_and_land_once(self):
        # fathers#242 (a): active lane -> review -> findings -> fix commit in same lane -> --mark -> land.
        self.review_text.write_text(TWO_FINDINGS)
        worktree = self.created("a", "lib/**", "calls/**")
        reviewed_sha = self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        reviewed_tree = self.git(worktree, "rev-parse", f"{reviewed_sha}^{{tree}}")
        self.lane("check", "a")
        self.lane("review", "a")
        receipt = self.review("a")
        self.assertTrue(receipt["pre_land"])
        self.assertEqual(receipt["tip"], reviewed_sha)
        self.assertEqual(receipt["tree"], reviewed_tree)

        # Unmarked pre-land findings refuse land.
        refused = self.lane("land", "a", ok=False)
        self.assertIn("land refused", refused)
        self.assertIn("unmarked P1/P2 findings", refused)

        # Fix finding 1 in the same lane without --fix-lane, reject finding 2, recheck, and land.
        fix_sha = self.commit(worktree, {"calls/main.txt": "foo\nbaz\n"}, "fix finding 1")
        self.lane("check", "a")
        self.lane("review", "a", "--mark", "1", f"fixed:{fix_sha}", "--mark", "2", "rejected:out of scope")
        self.assertEqual(self.review("a")["findings"][0]["mark"]["merge"], fix_sha)

        land_out = self.lane("land", "a")
        self.assertIn("review kept", land_out)
        self.assertNotIn("kept for its review", land_out)
        self.assertEqual(sum(1 for c in self.codex_calls() if c[2] == "exec"), 1, "no post-land review started")
        self.assertEqual(self.record("a")["status"], "done")

    def test_pre_land_review_refuses_planned_dirty_or_empty_active_lane(self):
        self.start("p", "docs/**")
        self.assertIn("is planned", self.lane("review", "p", ok=False))

        worktree = self.created("a", "lib/**")
        self.assertIn("no commits beyond", self.lane("review", "a", ok=False))

        self.commit(worktree, {"lib/api.txt": "foo\nbar\n"})
        self.write(worktree, "lib/api.txt", "uncommitted\n")
        self.assertIn("uncommitted changes", self.lane("review", "a", ok=False))

    def test_pre_land_review_extra_unreviewed_commit_refuses_land_unless_forced(self):
        # fathers#242 (b): extra unreviewed commit after pre-land review refuses land; --force-reason audits override.
        worktree = self.created("a", "lib/**")
        reviewed_sha = self.commit(worktree, {"lib/api.txt": "foo\nbar\n"})
        self.lane("check", "a")
        self.lane("review", "a")

        extra_sha = self.commit(worktree, {"lib/api.txt": "foo\nbar\nextra\n"}, "unreviewed extra commit")
        self.lane("check", "a")

        refused = self.lane("land", "a", ok=False)
        self.assertIn("land refused", refused)
        self.assertIn("unreviewed commits since pre-land review", refused)
        self.assertIn(reviewed_sha[:10], refused)
        self.assertIn(extra_sha[:10], refused)
        self.assertEqual(self.record("a")["status"], "active")

        forced = self.lane("land", "a", "--force-reason", "trivial comment tweak")
        self.assertIn("post-merge green", forced)
        self.assertIn("override land a: trivial comment tweak", (self.state / "ledger.log").read_text())

    def test_pre_land_review_integration_merge_does_not_invalidate_review(self):
        # fathers#242 (c): clean integration-branch merge after pre-land review keeps review valid.
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbar\n"})
        self.lane("check", "a")
        self.lane("review", "a")

        # Advance the integration branch with another commit, then merge it into lane a.
        self.commit(self.integration, {"docs/other.md": "other lane\n"}, "other integration change")
        self.git(worktree, "merge", "--no-ff", "-m", "Merge integrate/q into lane/a", "integrate/q")
        self.lane("check", "a")

        land_out = self.lane("land", "a")
        self.assertIn("review kept", land_out)
        self.assertNotIn("kept for its review", land_out)
        self.assertEqual(sum(1 for c in self.codex_calls() if c[2] == "exec"), 1, "no post-land review started")
        self.assertEqual(self.record("a")["status"], "done")

    def test_list_status_and_show_display_reviewed_before_land_at_short_sha(self):
        # fathers#242 (d): list / status / show display "reviewed before land at <sha>" for completed pre-land review.
        worktree = self.created("a", "lib/**")
        reviewed_sha = self.commit(worktree, {"lib/api.txt": "foo\nbar\n"})
        self.lane("check", "a")
        self.lane("review", "a")
        expected = f"reviewed before land at {reviewed_sha[:10]}"

        list_text = self.lane("list")
        self.assertIn(expected, list_text)
        list_json = json.loads(self.lane("list", "--json"))
        self.assertIn(expected, list_json["lanes"][0]["receipt"])

        status_text = self.lane("status", cwd=self.tmp)
        self.assertIn(expected, status_text)
        status_json = json.loads(self.lane("status", "--json", cwd=self.tmp))
        self.assertEqual(status_json["projects"][0]["lanes"][0]["review"]["summary"], expected)
        self.assertTrue(status_json["projects"][0]["lanes"][0]["review"]["pre_land"])

        show_text = self.lane("show", "a")
        self.assertIn(expected, show_text)
        show_json = json.loads(self.lane("show", "a", "--json"))
        self.assertEqual(show_json["review"]["summary"], expected)
        self.assertTrue(show_json["review"]["pre_land"])

        # P3 #10: once an unreviewed commit is added, list/status/show mark it stale.
        self.commit(worktree, {"lib/api.txt": "foo\nbar\nextra\n"}, "unreviewed extra")
        self.assertIn(f"{expected} (stale)", self.lane("list"))
        self.assertIn(f"{expected} (stale)", self.lane("status", cwd=self.tmp))
        self.assertIn(f"{expected} (stale)", self.lane("show", "a"))

    def test_pre_land_review_multi_commit_fix_and_active_guidance_and_branch_checks(self):
        # P2 #5 + P2 #9 + P3 #12 + P3 #13.
        self.review_text.write_text(TWO_FINDINGS)
        worktree = self.created("a", "lib/**", "calls/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        out = self.lane("review", "a")
        # P3 #13: active pre-land review guidance points to same-lane fix, not --fix-lane.
        self.assertIn("fixed:<fix-commit>|a", out)
        self.assertNotIn("--fix-lane", out)

        # P2 #9: wrong checked-out branch refuses review; P3 #12: refused --force-reason writes no override.
        self.git(worktree, "checkout", "--detach", "HEAD")
        ledger_before = (self.state / "ledger.log").read_text()
        wrong_branch = self.lane("review", "a", "--force-reason", "re-review on detached", ok=False)
        self.assertIn("detached HEAD", wrong_branch)
        self.assertEqual((self.state / "ledger.log").read_text(), ledger_before)
        self.git(worktree, "checkout", "lane/a")

        # P1 #1: empty commit cannot close a finding.
        self.git(worktree, "commit", "--allow-empty", "-qm", "empty commit")
        empty_sha = self.git(worktree, "rev-parse", "HEAD")
        empty_mark = self.lane("review", "a", "--mark", "1", f"fixed:{empty_sha}", ok=False)
        self.assertIn("has the same tree as the reviewed commit", empty_mark)
        self.git(worktree, "reset", "--hard", "HEAD~1")

        # P2 #5: multi-commit same-lane fix (F1 -> F2) marked with fixed:a (F2) lands cleanly.
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\nqux\n"}, "fix part 1")
        self.commit(worktree, {"calls/main.txt": "foo\nbaz\n"}, "fix part 2")
        self.lane("check", "a")
        self.lane("review", "a", "--mark", "1", "fixed:a", "--mark", "2", "rejected:out of scope")
        land_out = self.lane("land", "a")
        self.assertIn("review kept", land_out)
        self.assertEqual(self.record("a")["status"], "done")

    def test_another_active_lane_cannot_mark_findings_of_a_landed_lane(self):
        # P1 #2: fixed:<another active lane> must not resolve another lane's unlanded tip.
        self.review_text.write_text(TWO_FINDINGS)
        wt_a = self.created("a", "lib/**")
        self.commit(wt_a, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.lane("land", "a")
        self.lane("review", "a")

        wt_b = self.created("b", "docs/**")
        self.commit(wt_b, {"docs/b.md": "b\n"})
        self.review_text.write_text("No findings.\n")
        self.lane("review", "b")

        refused = self.lane("review", "a", "--mark", "1", "fixed:b", ok=False)
        self.assertIn("not a commit or a landed lane here", refused)

    def test_landed_lane_without_check_receipt_preserves_exact_no_receipt_in_list(self):
        # P3 #11: landed lane with no check receipt stays exact "no receipt" in list.
        wt = self.created("a", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\nbar\n"})
        self.lane("land", "a", "--force-reason", "no check")
        list_json = json.loads(self.lane("list", "--json"))
        self.assertEqual(list_json["lanes"][0]["receipt"], "no receipt")


class StaleReviewAndDeltaTests(LaneCase):
    """fathers#53: stale review display, delta review numbering/marking, and --fix-of stale parent refusal."""

    def test_list_show_status_report_review_stale_and_delta_review_marks_complete(self):
        self.review_text.write_text("### P2 — lib/api.txt:1 — Primary finding\nPrimary detail.\n")
        wt = self.created("a", "lib/**")
        reviewed_sha = self.commit(wt, {"lib/api.txt": "foo\nv1\n"}, "v1")
        self.lane("check", "a")
        self.lane("review", "a")

        landed_sha = self.commit(wt, {"lib/api.txt": "foo\nv1\nv2\n"}, "v2")
        self.lane("check", "a")
        self.lane("land", "a", "--force-reason", "land with extra commit")

        # list, show, and status report "review stale".
        self.assertIn("review stale", self.lane("list"))
        listed = next(lane for lane in json.loads(self.lane("list", "--json"))["lanes"] if lane["id"] == "a")
        self.assertIn("review stale", listed["receipt"])

        show_text = self.lane("show", "a")
        self.assertIn("review stale", show_text)
        shown = json.loads(self.lane("show", "a", "--json"))
        self.assertEqual(shown["review"]["status"], "stale")
        self.assertEqual(shown["review"]["reviewed_tip"], reviewed_sha)
        self.assertEqual(shown["review"]["landed_tip"], landed_sha)

        status_text = self.lane("status", cwd=self.tmp)
        self.assertIn("review stale", status_text)
        status_lane = next(
            lane
            for project in json.loads(self.lane("status", "--json", cwd=self.tmp))["projects"]
            for lane in project.get("lanes", [])
            if lane["id"] == "a"
        )
        self.assertEqual(status_lane["review"]["status"], "stale")

        # Run delta review with one finding -> numbered #2 after primary finding #1.
        self.review_text.write_text("### P2 — lib/api.txt:3 — Delta finding\nDelta detail.\n")
        delta_out = self.lane("review", "a")
        self.assertIn("Delta review of a", delta_out)
        self.assertIn("2. P2", delta_out)
        receipt = self.review("a")
        self.assertEqual(receipt["delta"]["kind"], "delta")
        self.assertEqual(receipt["delta"]["base"], reviewed_sha)
        self.assertEqual(receipt["delta"]["tip"], landed_sha)
        self.assertEqual([f["n"] for f in receipt["findings"]], [1])
        self.assertEqual([f["n"] for f in receipt["delta"]["findings"]], [2])

        # Mark both primary (#1) and delta (#2) findings -> auto-done finishes the lane.
        self.lane("review", "a", "--mark", "1", "rejected:ok1", "--mark", "2", "rejected:ok2")
        self.assertEqual(self.record("a")["status"], "done")

    def test_fix_of_and_fix_lane_refuse_stale_parent_but_allow_clean_integration_merge(self):
        # Real-repo CLI probe from fathers#53 comment: active lane reviewed with unmarked findings,
        # extra lane-owned commit added and landed -> --fix-of and --fix-lane refuse as stale.
        self.review_text.write_text(TWO_FINDINGS)
        wt = self.created("stale", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\nv1\n"}, "v1")
        self.lane("check", "stale")
        self.lane("review", "stale")
        self.commit(wt, {"lib/api.txt": "foo\nv1\nextra\n"}, "v2 unreviewed")
        self.lane("check", "stale")
        self.lane("land", "stale", "--force-reason", "land stale")

        refused_start = self.lane(
            "start", "stale-fix", "--fix-of", "stale", "--ticket", "404", "--claims", "lib/api.txt", ok=False
        )
        self.assertIn("stale", refused_start)
        refused_handover = self.lane("review", "stale", "--fix-lane", ok=False)
        self.assertIn("stale", refused_handover)

        # Clean integration merge after review preserves --fix-of eligibility.
        wt_clean = self.created("clean", "calls/**")
        self.commit(wt_clean, {"calls/a.txt": "foo\n"}, "c1")
        self.lane("check", "clean")
        self.lane("review", "clean")
        self.commit(self.integration, {"docs/other.md": "other\n"}, "integration advance")
        self.git(wt_clean, "merge", "--no-ff", "-m", "Merge integrate/q", "integrate/q")
        self.lane("check", "clean")
        self.lane("land", "clean", "--force-reason", "land with unmarked findings for fix lane")

        ok_start = self.lane(
            "start", "clean-fix", "--fix-of", "clean", "--ticket", "404", "--claims", "calls/a.txt"
        )
        self.assertIn("Lane clean-fix", ok_start)

    def test_recycled_pid_with_different_start_reads_as_died_and_show_never_reports_running(self):
        # fathers#60: live PID with mismatched start time reads as Died across list, show, status, and review.
        wt = self.created("a", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\nrecycled\n"})
        self.lane("check", "a")
        self.lane("land", "a", "--keep")
        created = self.record("a")["created"]

        live_pid = os.getpid()
        (self.state / "a.review.json").write_text(
            json.dumps({
                "version": 1,
                "label": "a",
                "kind": "lane",
                "lane": "a",
                "lane_created": created,
                "status": "running",
                "pid": live_pid,
                "start": "Mon Jan 01 00:00:00 2020",
                "started": "2026-11-24T00:00:00Z",
                "log": str(self.state / "logs" / "review-a.log"),
            }) + "\n"
        )

        shown = json.loads(self.lane("show", "a", "--json"))
        self.assertEqual(shown["review"]["status"], "stopped")
        listed = json.loads(self.lane("list", "--json"))["lanes"][0]
        self.assertIn("review died", listed["receipt"])
        status_lane = json.loads(self.lane("status", "--json", cwd=self.tmp))["projects"][0]["lanes"][0]
        self.assertEqual(status_lane["review"]["status"], "stopped")

        # Rerunning `review a` without --force-reason succeeds because the recycled PID is treated as Died.
        out = self.lane("review", "a")
        self.assertIn("Review of a", out)
        self.assertEqual(self.review("a")["status"], "finished")

    def test_forced_rerun_supersedes_live_review_and_preserves_its_checkout(self):
        # fathers#60: forced rerun while another review is live marks the first superseded, keeps its checkout
        # intact mid-run, removes dead stale checkouts, and fences out the superseded worker.
        from lanefather import config, review_record, state, worktrees

        wt = self.created("a", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\nforced\n"})
        self.lane("check", "a")
        self.lane("land", "a", "--keep")
        rec = self.record("a")
        tip = rec["landed"]["tip"]

        # Spawn a real live sleeper to stand in for the first running review worker.
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(close_process, sleeper)
        first_pid = sleeper.pid
        first_start = review_record.process_start(first_pid)
        self.assertIsInstance(first_start, str)

        reviews_dir = self.state / "reviews"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        first_checkout = reviews_dir / f"a-checkout-{first_pid}"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(first_checkout), tip)
        self.addCleanup(worktrees.remove_checkout, self.repo, first_checkout)
        marker_file = first_checkout / ".live-review-marker"
        marker_file.write_text("still running\n")

        # Also plant a dead stale checkout to verify stale checkouts ARE still cleaned up.
        dead_checkout = reviews_dir / "a-checkout-999999999"
        dead_checkout.mkdir(parents=True, exist_ok=True)
        (dead_checkout / "stale.txt").write_text("stale\n")

        cfg = config.load(self.integration / ".lanefather.toml")
        store = state.Store(cfg, directory=self.state)
        review_record.ReviewRecord.begin(
            store,
            "a",
            {"kind": "lane", "lane": "a", "lane_created": rec["created"]},
            pid=first_pid,
            pid_start=first_start,
            checkout=str(first_checkout),
            log=str(self.state / "logs" / "review-a-1.log"),
            effort="medium",
        )

        # Force a rerun while the first worker is live.
        out = self.lane("review", "a", "--force-reason", "rerun while held")
        self.assertIn("Review of a", out)

        # First worker's checkout must remain completely intact; dead checkout was removed.
        self.assertTrue(first_checkout.is_dir(), "live superseded review checkout was deleted mid-run")
        self.assertEqual(marker_file.read_text(), "still running\n")
        self.assertFalse(dead_checkout.exists(), "dead stale checkout should be cleaned up")

        # Receipt records the first run as superseded and holds the new finished review.
        receipt = self.review("a")
        self.assertEqual(receipt["status"], "finished")
        self.assertIn("superseded", receipt)
        self.assertEqual(receipt["superseded"][0]["pid"], first_pid)
        self.assertEqual(receipt["superseded"][0]["start"], first_start)
        self.assertEqual(receipt["superseded"][0]["status"], "superseded")
        self.assertEqual(receipt["superseded"][0]["checkout"], str(first_checkout))

        # Superseded worker attempting to finish is fenced out by StaleReviewRunError.
        with self.assertRaises(review_record.StaleReviewRunError):
            review_record.ReviewRecord.finish(
                store,
                "a",
                {"reviewer": "stale", "findings": []},
                owner_pid=first_pid,
                owner_start=first_start,
            )

    def test_review_checkout_protects_custom_named_live_checkout_and_cleans_recycled_pid_checkout(self):
        # fathers#60: (1) a recorded `checkout` path whose name does not embed the PID is still protected via
        # realpath matching while its process is alive; (2) a live PID with a mismatched `start` (recycled PID)
        # has its stale checkout removed; (3) an overflowing numeric suffix does not crash cleanup.
        from lanefather import config, review_record, state, worktrees

        wt = self.created("a", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\ncheckouts\n"})
        self.lane("check", "a")
        self.lane("land", "a", "--keep")
        rec = self.record("a")
        tip = rec["landed"]["tip"]

        sleeper_live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(close_process, sleeper_live)
        live_pid = sleeper_live.pid
        live_start = review_record.process_start(live_pid)
        self.assertIsInstance(live_start, str)

        sleeper_recycled = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(close_process, sleeper_recycled)
        recycled_pid = sleeper_recycled.pid

        reviews_dir = self.state / "reviews"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        # Non-PID-suffixed checkout directory protected purely by its recorded `checkout` realpath.
        custom_live_checkout = reviews_dir / "a-checkout-custom-slot"
        custom_live_checkout.mkdir(parents=True, exist_ok=True)
        (custom_live_checkout / "marker.txt").write_text("alive\n")

        # Checkout whose PID is live, but whose recorded `start` does not match (recycled PID) -> stale.
        recycled_checkout = reviews_dir / f"a-checkout-{recycled_pid}"
        recycled_checkout.mkdir(parents=True, exist_ok=True)
        (recycled_checkout / "stale.txt").write_text("recycled\n")

        # Absurd numeric suffix that would overflow C pid_t in os.kill().
        overflow_checkout = reviews_dir / "a-checkout-99999999999999999999"
        overflow_checkout.mkdir(parents=True, exist_ok=True)

        cfg = config.load(self.integration / ".lanefather.toml")
        store = state.Store(cfg, directory=self.state)
        state.write_json(
            store.receipt_path("a", "review"),
            {
                "version": 1,
                "label": "a",
                "kind": "lane",
                "lane": "a",
                "lane_created": rec["created"],
                "status": "running",
                "pid": recycled_pid,
                "start": "Mon Jan 01 00:00:00 2020",
                "checkout": str(recycled_checkout),
                "superseded": [
                    {
                        "pid": live_pid,
                        "start": live_start,
                        "checkout": str(custom_live_checkout),
                        "status": "superseded",
                    }
                ],
            },
        )

        with worktrees.review_checkout(store, str(self.repo), "a", tip) as checkout:
            self.assertTrue(pathlib.Path(checkout).is_dir())
            self.assertTrue(custom_live_checkout.is_dir(), "custom-named live checkout must be protected")
            self.assertEqual((custom_live_checkout / "marker.txt").read_text(), "alive\n")
            self.assertFalse(recycled_checkout.exists(), "recycled-PID checkout must be cleaned up")
            self.assertFalse(overflow_checkout.exists(), "overflowing-PID suffix checkout must be cleaned up")


if __name__ == "__main__":
    unittest.main()
