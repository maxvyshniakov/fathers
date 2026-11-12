"""Tests of the independent review (lanefather.review): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
from typing import Any
import unittest

from support import NO_FINDINGS, SPREAD_FINDINGS, TWO_FINDINGS, LaneCase


class LaneReviewTests(LaneCase):
    """#570: one independent review per lane, Sol 6.1 at medium effort unless Codex has under 15% of its weekly limit
    left or cannot run the model, then Opus 5.5 at medium effort."""

    def landed(self, lane_id: str, path: str = "lib/api.txt", text: str = "foo\nbaz\n",
               extra: tuple[str, ...] = ()) -> str:
        worktree = self.created(lane_id, path)
        self.commit(worktree, {path: text})
        self.lane("check", lane_id)
        self.lane("land", lane_id, *extra)
        return worktree

    def test_sol_reviews_the_lane_diff_against_its_base_in_a_detached_checkout(self):
        self.review_text.write_text(TWO_FINDINGS)
        previous = self.tip()
        self.landed("a")
        tip = self.record("a")["landed"]["tip"]
        output = self.lane("review", "a")
        for text in ("gpt-6.1-sol", "medium", "73% of the weekly Codex limit left", "1. P1 lib/api.txt:2",
                     "baz is defined but nothing calls it", "2. P2 lib/api.txt:1",
                     "lanefather review a --mark <n> fixed:<merge>|rejected:<reason>"):
            self.assertIn(text, output)
        calls = self.codex_calls()
        self.assertEqual([c[2] for c in calls], ["app-server", "exec"])
        execution = calls[1]
        checkout = execution[execution.index("-C") + 1]
        self.assertEqual(execution[2:], ["exec", "-C", checkout, "-s", "read-only", "-m", "gpt-6.1-sol", "-c",
                                         'model_reasoning_effort="medium"', "--ephemeral", "-o",
                                         execution[execution.index("-o") + 1], "-"])
        self.assertNotEqual(checkout, self.record("a")["worktree"])
        self.assertFalse(os.path.exists(checkout), "the detached review checkout is removed")
        # The stand-in's transcript says "rate limit" and "usage limit": Sol's availability comes from its exit
        # status and its -o answer, never from transcript text, so Opus does not run.
        self.assertEqual(self.claude_calls(), [])
        prompt = (self.tmp / "codex.log.prompt").read_text()
        for text in ("Fixture rule: a shortcut keeps every check it skips", "Fixture ticket title",
                     "The fixture ticket body.", f"git diff {previous}...{tip}", '"findings"'):
            self.assertIn(text, prompt)
        receipt = self.review("a")
        self.assertEqual((receipt["status"], receipt["reviewer"], receipt["model"], receipt["effort"],
                          receipt["codex_left"], receipt["base"], receipt["tip"], receipt["fallback"]),
                         ("finished", "sol", "gpt-6.1-sol", "medium", 73, previous, tip, None))
        self.assertEqual(receipt["tree"], self.git(self.repo, "rev-parse", tip + "^{tree}"))
        self.assertIsInstance(receipt["seconds"], (int, float))
        self.assertEqual([(f["n"], f["severity"], f["file"], f["line"], f["mark"]) for f in receipt["findings"]],
                         [(1, "P1", "lib/api.txt", 2, None), (2, "P2", "lib/api.txt", 1, None)])
        ledger = (self.state / "ledger.log").read_text()
        self.assertRegex(ledger, r"review a finished reviewer=sol model=gpt-6\.1-sol effort=medium left=73 "
                                 r"seconds=\d+ findings=2 p1=1 p2=1 p3=0")
        # Exactly one review per lane: a second run shows the first.
        self.assertIn("already reviewed", self.lane("review", "a"))
        self.assertEqual(len(self.codex_calls()), 2)

    def test_opus_reviews_when_codex_has_under_15_percent_left_or_hit_its_limit(self):
        for index, (variable, value, why) in enumerate((("FIXTURE_CODEX_USED", "90", "10% of the weekly limit left"),
                                                        ("FIXTURE_CODEX_REACHED", "rate_limit_reached",
                                                         "rate_limit_reached"))):
            with self.subTest(variable):
                lane_id = f"low{index}"
                self.landed(lane_id, f"lib/low{index}.txt", "baz\n")
                calls = len(self.codex_calls())
                self.env[variable] = value
                output = self.lane("review", lane_id)
                del self.env[variable]
                self.assertIn("claude-opus-5-5", output)
                self.assertEqual([c[2] for c in self.codex_calls()[calls:]], ["app-server"], "Sol does not run")
                claude = self.claude_calls()[-1]
                self.assertEqual(claude[1:8], ["-p", "--model", "claude-opus-5-5", "--effort", "medium",
                                               "--allowedTools",
                                               "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"])
                # The same prompt, and the diff itself, since Opus gets no shell beyond git.
                prompt = (self.tmp / "claude.log.prompt").read_text()
                self.assertIn("Fixture rule", prompt)
                self.assertIn("+baz", prompt)
                receipt = self.review(lane_id)
                self.assertEqual((receipt["reviewer"], receipt["model"], receipt["effort"]),
                                 ("opus", "claude-opus-5-5", "medium"))
                self.assertIn(why, receipt["fallback"])

    def test_a_model_error_an_empty_answer_or_a_limit_hit_mid_run_falls_back_to_opus_once(self):
        for index, mode in enumerate(("model-error", "limit", "empty")):
            with self.subTest(mode):
                lane_id = f"mid{index}"
                self.landed(lane_id, f"lib/mid{index}.txt", "baz\n")
                calls, opus = len(self.codex_calls()), len(self.claude_calls())
                self.env["FIXTURE_CODEX_EXEC"] = mode
                output = self.lane("review", lane_id)
                del self.env["FIXTURE_CODEX_EXEC"]
                self.assertEqual([c[2] for c in self.codex_calls()[calls:]], ["app-server", "exec"])
                self.assertEqual(len(self.claude_calls()) - opus, 1)
                receipt = self.review(lane_id)
                self.assertEqual((receipt["status"], receipt["reviewer"], receipt["codex_left"]),
                                 ("finished", "opus", 73))
                self.assertIn("Sol", receipt["fallback"])
                self.assertIn("claude-opus-5-5", output)

    def test_findings_that_are_not_objects_leave_one_finding_to_read(self):
        self.review_text.write_text('No findings.\n\n```json\n{"findings": ["baz is dead", 3]}\n```\n')
        self.landed("a")
        self.lane("review", "a")
        findings = self.review("a")["findings"]
        self.assertEqual(len(findings), 1)
        self.assertIn("read it and mark it", findings[0]["title"])
        self.assertIn("baz is dead", findings[0]["detail"])

    def test_findings_keep_a_known_category_and_a_package_misuse_names_its_package(self):
        self.review_text.write_text(
            '```json\n{"findings": ['
            '{"severity": "P2", "category": "package-misuse", "package": "@acme/kit", "file": "a.css", "line": 3,'
            ' "title": "selector into the kit checkbox internals", "detail": "use the documented target"},'
            '{"severity": "P3", "category": "style", "package": "@acme/kit", "file": "b.ts", "line": 1,'
            ' "title": "t", "detail": "d"},'
            '{"severity": "P1", "file": "c.ts", "line": 2, "title": "old answer shape", "detail": "d"}]}\n```\n')
        self.landed("a")
        output = self.lane("review", "a")
        findings: list[dict[str, Any]] = self.review("a")["findings"]
        self.assertEqual([(f["category"], f["package"]) for f in findings],
                         [("package-misuse", "@acme/kit"), (None, None), (None, None)])
        self.assertIn("P2 [package-misuse] a.css:3", output)

    def test_a_stale_answer_of_an_earlier_run_never_stands_in_for_sol(self):
        self.review_text.write_text(TWO_FINDINGS)
        self.landed("a")
        self.lane("review", "a")
        self.env["FIXTURE_CODEX_EXEC"] = "no-output"
        self.lane("review", "a", "--force-reason", "reviewer answer lost")
        receipt = self.review("a")
        self.assertEqual(receipt["reviewer"], "opus")
        self.assertEqual(receipt["fallback"], "Sol returned an empty review")
        self.assertIn("override review a: reviewer answer lost", (self.state / "ledger.log").read_text())

    def test_without_a_codex_cli_opus_reviews_and_a_failed_opus_leaves_the_review_failed(self):
        self.landed("a")
        self.env["LANEFATHER_CODEX"] = str(self.tmp / "no-such-codex")
        self.env["FIXTURE_CLAUDE_FAIL"] = "1"
        output = self.lane("review", "a", ok=False)
        self.assertIn("review failed", output)
        receipt = self.review("a")
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("no Codex CLI", receipt["fallback"])
        self.assertIn("overloaded", receipt["error"])
        # A failed review runs again without an override.
        del self.env["FIXTURE_CLAUDE_FAIL"]
        self.lane("review", "a")
        self.assertEqual(self.review("a")["status"], "finished")

    def test_the_codex_cli_comes_from_codex_cli_path_in_the_codex_config(self):
        app = self.tmp / "ChatGPT.app" / "codex"
        app.parent.mkdir()
        shutil.copy2(self.tmp / "bin" / "codex", app)
        self.write(self.tmp / "codex-home", "config.toml",
                   f'model = "gpt-6.1-sol"\n[mcp_servers.node_repl.env]\nCODEX_CLI_PATH = "{app}"\n')
        self.landed("a")
        self.lane("review", "a")
        self.assertEqual({c[0] for c in self.codex_calls()}, {str(app.resolve())})

    def test_land_starts_the_review_in_the_background_and_keeps_the_lane_until_it_is_marked(self):
        self.review_text.write_text(TWO_FINDINGS)
        hold = self.tmp / "release-review"
        self.env.update(FIXTURE_REVIEW_HOLD=str(hold), LANEFATHER_REVIEW_START="")
        worktree = self.landed("a")
        receipt = self.review("a")
        self.assertEqual(receipt["status"], "running", "land does not wait for the review")
        os.kill(receipt["pid"], 0)
        self.assertTrue(pathlib.Path(receipt["log"]).exists())
        self.assertIn("review running", self.lane("list"))
        hold.write_text("")
        receipt = self.wait_review("a")
        self.assertEqual((receipt["status"], len(receipt["findings"])), ("finished", 2))
        self.assertTrue(pathlib.Path(worktree).exists(), "the fix round runs in the lane's worktree")
        self.assertEqual(self.record("a")["status"], "landed")
        output = self.lane("done", "a", ok=False)
        self.assertIn("2 of 2 review findings unmarked", output)
        self.assertIn("lanefather review a --mark", output)
        self.lane("review", "a", "--mark", "1", "rejected:baz is the new entry point", "--mark", "2",
                  "rejected:covered by checks/lib.sh")
        self.assertFalse(pathlib.Path(worktree).exists(), "marking the last finding finishes the lane")
        self.assertEqual(self.record("a")["status"], "done")

    def test_a_docs_only_lane_and_a_fix_lane_get_no_review(self):
        self.env["LANEFATHER_REVIEW_START"] = ""
        worktree = self.landed("docs", "docs/guide.md", "words\n")
        receipt = self.review("docs")
        self.assertEqual(receipt["status"], "exempt")
        self.assertIn("documentation only", receipt["exempt"])
        self.assertEqual(self.codex_calls(), [])
        self.assertFalse(pathlib.Path(worktree).exists(), "an exempt lane finishes at land as before")


class ReviewGateCase(LaneCase):
    def setUp(self):
        super().setUp()
        self.git(self.repo, "branch", "dev")

    def landed(self, lane_id, path, text):
        worktree = self.created(lane_id, path)
        self.commit(worktree, {path: text})
        self.lane("check", lane_id)
        self.lane("land", lane_id)
        return worktree


class ReviewGateTests(ReviewGateCase):
    """#570: promote moves dev only once every lane landed since dev has a finished review with each finding marked;
    --ship runs one overall review."""

    def test_promote_refuses_while_a_finding_is_unmarked_and_moves_dev_once_each_is_marked(self):
        self.landed("clean", "lib/clean.txt", "clean\n")
        self.lane("review", "clean")
        self.review_text.write_text(TWO_FINDINGS)
        self.landed("a", "lib/api.txt", "foo\nbaz\n")
        self.lane("review", "a")
        tip = self.tip()
        output = self.lane("promote", tip, "--no-push", "--wait", "0", ok=False)
        # fathers#109: unmarked P1/P2 findings of a finished review hold the batch gate too, naming the commands.
        self.assertNotIn("batch gate green", output)
        self.assertIn("P1/P2 review findings unmarked (or marked fixed outside the SHA) hold the batch gate and dev", output)
        self.assertIn("lane a (#404): finding #1 P1 lib/api.txt:2 baz is defined but nothing calls it", output)
        self.assertIn("lane a (#404): finding #2 P2", output)
        self.assertNotIn("lane clean", output)
        self.assertNotEqual(self.git(self.repo, "rev-parse", "dev"), tip)
        self.assertIn("not a commit", self.lane("review", "a", "--mark", "2", "fixed:0123456789", ok=False))
        self.assertIn("give the reason", self.lane("review", "a", "--mark", "1", "rejected:", ok=False))
        self.lane("review", "a", "--mark", "1", "rejected:baz is the new entry point")
        held = self.lane("promote", tip, "--no-push", "--wait", "0", ok=False)
        self.assertIn("lane a (#404): finding #2 P2", held)
        self.assertNotIn("finding #1", held)
        fix = self.commit(self.integration, {"calls/main.txt": "foo\nbaz\n"}, "fix: call baz")
        self.lane("review", "a", "--mark", "2", f"fixed:{fix[:12]}")
        marks = [f["mark"] for f in self.review("a")["findings"]]
        self.assertEqual([m["kind"] for m in marks], ["rejected", "fixed"])
        self.assertEqual((marks[0]["reason"], marks[1]["merge"]), ("baz is the new entry point", fix))
        runs = len(self.verify_runs())
        self.lane("promote", fix, "--no-push", "--wait", "0")
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), fix)
        self.assertEqual(len(self.verify_runs()) - runs, 1, "only the moved tip is gated")
        ledger = (self.state / "ledger.log").read_text()
        self.assertIn("review-mark a 1 rejected", ledger)
        self.assertIn(f"review-mark a 2 fixed:{fix[:10]}", ledger)
        self.assertIn("review-marked a fixed=1 rejected=1", ledger)
        # Lanes in dev no longer count.
        self.review_text.write_text(NO_FINDINGS)
        self.landed("later", "lib/later.txt", "later\n")
        self.lane("review", "later")
        self.lane("promote", self.tip(), "--no-push", "--wait", "0")

    def test_a_missing_or_running_review_refuses_promote_and_force_reason_is_logged(self):
        self.landed("a", "lib/api.txt", "foo\nbaz\n")
        tip = self.tip()
        output = self.lane("promote", tip, "--no-push", "--wait", "0", ok=False)
        self.assertIn("lane a (#404): no review", output)
        self.assertIn("--force-reason", output)
        self.lane("promote", tip, "--no-push", "--wait", "0", "--force-reason", "reviewed by the interim script")
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), tip)
        self.assertRegex((self.state / "ledger.log").read_text(),
                         rf"override promote {tip}: reviewed by the interim script \(lane a \(#404\): no review")

    def test_a_docs_only_lane_does_not_hold_promote(self):
        self.landed("docs", "docs/guide.md", "words\n")
        self.lane("promote", self.tip(), "--no-push", "--wait", "0")

    def test_the_fix_round_runs_in_the_landed_lanes_worktree_on_a_follow_up_lane(self):
        self.review_text.write_text(TWO_FINDINGS)
        worktree = self.landed("a", "lib/api.txt", "foo\nbaz\n")
        self.lane("review", "a")
        output = self.lane("review", "a", "--fix-lane")
        self.assertIn("Lane a-review-fixes: ticket #404", output)
        self.assertIn("git checkout -B lane/a-review-fixes integrate/q", output)
        fix = self.record("a-review-fixes")
        self.assertEqual((fix["status"], fix["review_fix_of"], fix["worktree"]), ("planned", "a", worktree))
        self.assertIsNone(self.record("a")["worktree"])
        self.assertEqual(self.record("a")["handed_over"]["to"], "a-review-fixes")
        self.assertEqual(self.review("a")["fix_lane"], "a-review-fixes")
        # The resumed agent stays in its worktree: a new branch from the integration tip, then the usual check.
        self.git(worktree, "checkout", "-q", "-B", "lane/a-review-fixes", "integrate/q")
        self.commit(worktree, {"calls/main.txt": "foo\nbaz\n"}, "fix(review): call baz")
        self.lane("check", "a-review-fixes", cwd=worktree)
        output = self.lane("land", "a-review-fixes")
        receipt = self.review("a-review-fixes")
        self.assertEqual(receipt["status"], "exempt")
        self.assertIn("fix round of a's review", receipt["exempt"])
        self.assertEqual(sum(1 for c in self.codex_calls() if c[2] == "exec"), 1, "nobody reviews the fix")
        self.assertIn("Lane a-review-fixes done", output)
        merge = self.record("a-review-fixes")["landed"]["merge"]
        self.lane("review", "a", "--mark", "1", "fixed:a-review-fixes", "--mark", "2", "rejected:out of scope")
        self.assertEqual(self.review("a")["findings"][0]["mark"]["merge"], merge)
        self.assertEqual(self.record("a")["status"], "done")
        self.lane("promote", self.tip(), "--no-push", "--wait", "0")

    def test_a_literal_base_lane_landed_with_into_gets_a_fix_lane_on_the_integration_branch(self):
        # Replay (lanefather#1): voice-p2-provider-benchmark registered at a commit, landed with --into, and its fix
        # header said git checkout -B <fix> <that commit>, which lacks the files to fix.
        self.review_text.write_text(TWO_FINDINGS)
        registered = self.tip()
        self.lane("start", "a", "--ticket", "404", "--base", registered, "--claims", "lib/**", "--create")
        worktree = self.record("a")["worktree"]
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.lane("land", "a", "--into", str(self.integration))
        self.lane("review", "a")
        output = self.lane("review", "a", "--fix-lane")
        self.assertIn("git checkout -B lane/a-review-fixes integrate/q", output)
        self.assertNotIn(registered, output)
        fix = self.record("a-review-fixes")
        self.assertEqual((fix["base"], fix["base_sha"]), ("integrate/q", self.record("a")["landed"]["merge"]))
        self.assertEqual(self.record("a")["base"], registered, "the landed lane keeps its registration")
        # The integration advances; the fix branch starts from its tip and plans only the fix.
        self.commit(self.integration, {"unowned/arrived.txt": "x\n"}, "the integration moves on")
        self.git(worktree, "checkout", "-q", "-B", "lane/a-review-fixes", "integrate/q")
        self.commit(worktree, {"calls/main.txt": "foo\nbaz\n"}, "fix(review): call baz")
        self.lane("check", "a-review-fixes", cwd=worktree)
        receipt = json.loads((self.state / "a-review-fixes.receipt.json").read_text())
        self.assertEqual(receipt["outside_claims"], ["calls/main.txt"])
        self.assertFalse(receipt["full"])

    def test_a_fix_lane_is_refused_when_no_branch_holds_the_landed_merge(self):
        self.review_text.write_text(TWO_FINDINGS)
        self.lane("start", "a", "--ticket", "404", "--base", self.tip(), "--claims", "lib/**", "--create")
        self.commit(self.record("a")["worktree"], {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.lane("land", "a", "--into", str(self.integration))
        self.lane("review", "a")
        self.git(self.integration, "checkout", "-q", "--detach", self.record("a")["landed"]["previous"])
        output = self.lane("review", "a", "--fix-lane", ok=False)
        self.assertIn(self.record("a")["landed"]["merge"][:10], output)
        self.assertIn(str(self.integration), output)
        self.assertFalse((self.state / "a-review-fixes.json").exists())

    def test_a_fix_lane_refusal_names_an_integration_worktree_that_is_gone(self):
        self.review_text.write_text(TWO_FINDINGS)
        self.lane("start", "a", "--ticket", "404", "--base", self.tip(), "--claims", "lib/**", "--create")
        self.commit(self.record("a")["worktree"], {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.lane("land", "a", "--into", str(self.integration))
        self.lane("review", "a")
        record = self.record("a")
        gone = str(self.tmp / "gone-integration")
        record["landed"]["integration"] = gone
        (self.state / "a.json").write_text(json.dumps(record))
        output = self.lane("review", "a", "--fix-lane", ok=False)
        self.assertIn(f"its integration worktree {gone} is gone", output)
        self.assertFalse((self.state / "a-review-fixes.json").exists())

    def test_the_fix_lane_claims_only_the_files_its_findings_name_and_check_widens_it_from_its_diff(self):
        """#586: the fix lane inherits none of the landed lane's claims, so a lane started since on a path the
        landed lane widened to does not block the hand-over."""
        self.review_text.write_text(SPREAD_FINDINGS)
        worktree = self.created("a", "lib/api.txt")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n", "docs/guide.md": "foo\n"})
        self.assertIn("claims widened: docs/guide.md", self.lane("check", "a"))
        self.lane("land", "a")
        self.lane("review", "a")
        self.start("b", "docs/**")
        output = self.lane("review", "a", "--fix-lane")
        self.assertIn("Claims: lib/api.txt\n", output)
        self.assertIn("docs/guide.md stays out of the fix lane's claims: lane b (#404) claims docs/**", output)
        fix = self.record("a-review-fixes")
        self.assertEqual((fix["claims"], fix["review_fix_of"], fix["worktree"]), (["lib/api.txt"], "a", worktree))
        self.assertEqual(self.record("a")["claims"], ["lib/api.txt", "docs/guide.md"], "the landed lane keeps its own")
        # The fix grows the lane's claims from its own diff, as any lane's check does (#574).
        self.git(worktree, "checkout", "-q", "-B", "lane/a-review-fixes", "integrate/q")
        self.commit(worktree, {"calls/main.txt": "foo\nbaz\n"}, "fix(review): call baz")
        self.assertIn("claims widened: calls/main.txt", self.lane("check", "a-review-fixes", cwd=worktree))
        self.assertEqual(self.record("a-review-fixes")["claims"], ["lib/api.txt", "calls/main.txt"])
        self.lane("land", "a-review-fixes")
        self.assertIn("fix round of a's review", self.review("a-review-fixes")["exempt"])
        self.lane("review", "a", "--mark", "1", "fixed:a-review-fixes", "--mark", "2", "rejected:the guide is right",
                  "--mark", "3", "rejected:outside", "--mark", "4", "rejected:no file")
        self.assertEqual(self.review("a")["findings"][0]["mark"]["merge"],
                         self.record("a-review-fixes")["landed"]["merge"])
        # promote counts the fix lane as the fix round, not as an unreviewed lane.
        self.lane("promote", self.tip(), "--no-push", "--wait", "0")
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), self.tip())

    def test_ship_runs_one_overall_review_of_main_to_the_sha_and_files_its_findings_as_tickets(self):
        self.landed("a", "lib/api.txt", "foo\nbaz\n")
        self.lane("review", "a")
        main, tip = self.git(self.repo, "rev-parse", "main"), self.tip()
        self.review_text.write_text(TWO_FINDINGS)
        self.env["LANEFATHER_REVIEW_START"] = ""
        output = self.lane("promote", tip, "--ship", "--no-push", "--wait", "0")
        self.assertIn("overall review", output)
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), tip, "the review does not hold the ship")
        receipt = self.wait_review(f"ship-{tip[:12]}")
        self.assertEqual((receipt["status"], receipt["kind"], receipt["base"], receipt["tip"], receipt["reviewer"]),
                         ("finished", "overall", main, tip, "sol"))
        self.assertIn(f"git diff {main}...{tip}", (self.tmp / "codex.log.prompt").read_text())
        # One issue that lists every finding as a checklist (owner decision on #570).
        log = (self.tmp / "gh.log").read_text()
        self.assertEqual(log.count("issue create"), 1)
        created = log[log.index("issue create"):]
        self.assertIn(f"Overall review of {tip[:10]}: 2 findings", created)
        self.assertIn("- [ ] **P1** `lib/api.txt:2` baz is defined but nothing calls it", created)
        self.assertIn("- [ ] **P2** `lib/api.txt:1` foo lost its consumer test", created)
        self.assertIn("#347", created)
        self.assertTrue(receipt["issue"].startswith("https://github.com/fixture/"))
        # Once: shipping the same SHA again starts no second review.
        self.assertIn("ticket https://github.com/fixture/", self.lane("promote", tip, "--ship", "--no-push", "--wait",
                                                                      "0"))
        execs = sum(1 for c in self.codex_calls() if c[2] == "exec")
        self.assertIn("already ran", self.lane("promote", tip, "--ship", "--no-push", "--wait", "0"))
        self.assertEqual(sum(1 for c in self.codex_calls() if c[2] == "exec"), execs)


class ReviewFixRoundTests(ReviewGateCase):
    """The #570 review findings: fixed marks in the promoted SHA, recoverable overall reviews, and a narrow
    documentation exemption."""

    def test_a_finding_marked_fixed_by_a_merge_after_the_batch_tip_holds_that_tip(self):
        self.review_text.write_text(TWO_FINDINGS)
        self.landed("a", "lib/api.txt", "foo\nbaz\n")
        self.lane("review", "a")
        tip = self.tip()
        fix = self.commit(self.integration, {"calls/main.txt": "foo\nbaz\n"}, "fix: call baz")
        self.lane("review", "a", "--mark", "1", f"fixed:{fix}", "--mark", "2", "rejected:out of scope")
        output = self.lane("promote", tip, "--no-push", "--wait", "0", ok=False)
        self.assertIn(f"lane a (#404): finding #1 is marked fixed by {fix[:10]}, which is not in {tip[:10]}", output)
        self.assertNotEqual(self.git(self.repo, "rev-parse", "dev"), tip)
        self.lane("promote", fix, "--no-push", "--wait", "0")

    def ship(self, tip, ok=True):
        return self.lane("promote", tip, "--ship", "--no-push", "--wait", "0", ok=ok)

    def test_a_failed_overall_review_runs_again_on_its_original_base(self):
        self.landed("a", "lib/api.txt", "foo\nbaz\n")
        self.lane("review", "a")
        main, tip = self.git(self.repo, "rev-parse", "main"), self.tip()
        self.env.update(LANEFATHER_REVIEW_START="", FIXTURE_CODEX_EXEC="model-error", FIXTURE_CLAUDE_FAIL="1")
        self.ship(tip)
        self.assertEqual(self.wait_review(f"ship-{tip[:12]}")["status"], "failed")
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), tip)
        del self.env["FIXTURE_CODEX_EXEC"], self.env["FIXTURE_CLAUDE_FAIL"]
        # main now holds the SHA; the rerun reviews main's old tip..SHA, as the first run did.
        self.assertIn("started in the background", self.ship(tip))
        receipt = self.wait_review(f"ship-{tip[:12]}")
        self.assertEqual((receipt["status"], receipt["base"], receipt["tip"]), ("finished", main, tip))

    def test_a_ticket_gh_could_not_file_is_filed_again_without_a_second_review(self):
        self.landed("a", "lib/api.txt", "foo\nbaz\n")
        self.lane("review", "a")
        tip = self.tip()
        self.review_text.write_text(TWO_FINDINGS)
        self.env.update(LANEFATHER_REVIEW_START="", FAKE_GH_FAIL_CREATE="1")
        self.ship(tip)
        receipt = self.wait_review(f"ship-{tip[:12]}")
        self.assertIn("HTTP 502", receipt["issue_error"])
        self.assertRegex((self.state / "ledger.log").read_text(), rf"review-ticket ship-{tip[:12]} failed: HTTP 502")
        execs = sum(1 for c in self.codex_calls() if c[2] == "exec")
        self.assertIn("not filed", self.lane("review", "--overall", tip, ok=False))
        del self.env["FAKE_GH_FAIL_CREATE"]
        self.assertIn("ticket https://github.com/fixture/", self.ship(tip))
        receipt = self.review(f"ship-{tip[:12]}")
        self.assertTrue(receipt["issue"].startswith("https://github.com/fixture/"))
        self.assertNotIn("issue_error", receipt)
        self.assertEqual(sum(1 for c in self.codex_calls() if c[2] == "exec"), execs, "the reviewer ran once")
        self.assertIn(f"review-ticket ship-{tip[:12]} filed https://", (self.state / "ledger.log").read_text())

    def test_only_documentation_outside_the_steering_files_is_exempt(self):
        cases = {"docs/guide.md": True, "README.md": True, "lib/notes.md": True, "CODING_STANDARDS.md": False,
                 "AGENTS.md": False, ".agents/skills/x/SKILL.md": False, "docs/agents/AGENTS.md": False,
                 "tools/agent-hooks/rules/tools.md": False, "lib/other.txt": False}
        for index, (path, exempt) in enumerate(cases.items()):
            with self.subTest(path):
                lane_id = f"d{index}"
                self.landed(lane_id, path, "words\n")
                self.assertEqual(self.record(lane_id)["landed"]["docs_only"], exempt)
                receipt = self.state / f"{lane_id}.review.json"
                self.assertEqual(receipt.exists() and self.review(lane_id)["status"] == "exempt", exempt)


if __name__ == "__main__":
    unittest.main()
