"""The review panel (fathers#151), the reviewer chosen through Mastack's `role` (fathers#150) and per review
(fathers#155): stand-in codex, claude and role CLIs (support.py); no test starts a real reviewer or reads the real
Codex limit. The shared cases also run Mastack's own bin/role when it is present, read-only, on the stand-in codex."""
from __future__ import annotations

import json
import os
import pathlib
import textwrap
from typing import Any
import unittest

from support import FIXTURES, NO_FINDINGS, TWO_FINDINGS, LaneCase

OPUS_FINDINGS = textwrap.dedent("""\
    ```json
    {"findings": [
      {"severity": "P2", "file": "lib/api.txt", "line": 2, "title": "baz has no test", "detail": "add one"},
      {"severity": "P3", "file": "lib/api.txt", "line": 1, "title": "foo is unsorted", "detail": "sort it"}
    ]}
    ```
    """)
# Mastack's own role command (LANEFATHER_TEST_MASTACK_ROLE names another), for the shared cases.
REAL_ROLE = pathlib.Path(os.environ.get("LANEFATHER_TEST_MASTACK_ROLE") or "~/mastack/bin/role").expanduser()
REVERSED_TABLE = textwrap.dedent("""\
    schema = 1
    [models.opus]
    id = "claude-opus-5-5"
    family = "anthropic"
    cli = "claude"
    [models.sol]
    id = "gpt-6.1-sol"
    family = "openai"
    cli = "codex"
    [roles.review]
    choices = [
      { model = "opus", effort = "high" },
      { model = "sol", effort = "medium"{when} },
    ]
    """)


class ReviewCase(LaneCase):
    def landed(self, lane_id: str = "a", path: str = "lib/api.txt", text: str = "foo\nbaz\n", *,
               agent: str | None = None) -> str:
        worktree = self.created(lane_id, path)
        self.commit(worktree, {path: text})
        self.lane("check", lane_id, *(("--agent", agent) if agent else ()))
        self.lane("land", lane_id)
        return worktree

    def configure(self, text: str) -> None:
        """Add to the project's [review] table on the integration branch, committed so land merges onto it."""
        config = self.integration / ".lanefather.toml"
        self.commit(self.integration, {".lanefather.toml": config.read_text() + "\n[review]\n" + text},
                    "configure the review")

    def panel(self, required: bool = False) -> None:
        self.configure('panel = ["sol", "opus"]\n' + ("panel_required = true\n" if required else ""))

    def table(self, text: str) -> None:
        self.write(self.tmp / "table", "models.toml", text)
        self.env["LANEFATHER_MODELS"] = str(self.tmp / "table" / "models.toml")

    def role_calls(self, log: pathlib.Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def members(self, label: str) -> list[tuple[str, str]]:
        return [(e["member"], e["status"]) for e in self.review(label)["panel"]]


class PanelTests(ReviewCase):
    def test_the_panel_runs_both_at_once_on_one_prompt_and_numbers_their_findings_together(self):
        self.review_text.write_text(TWO_FINDINGS)
        claude_text = self.tmp / "claude-review.md"
        claude_text.write_text(OPUS_FINDINGS)
        meet = self.tmp / "meet"
        os.mkfifo(meet)
        reviews = self.state / "reviews"
        self.env.update(FIXTURE_CLAUDE_TEXT=str(claude_text), FIXTURE_PANEL_MEET=str(meet),
                        FIXTURE_MEET_LIST=str(reviews))
        self.panel()
        self.landed()
        output = self.lane("review", "a")
        # codex opens the FIFO to read and claude to write: neither gets past it unless the other runs at once.
        codex = [c for c in self.codex_calls() if c[2] == "exec"]
        claude = self.claude_calls()
        self.assertEqual((len(codex), len(claude)), (1, 1))
        self.assertEqual(os.path.realpath(codex[0][codex[0].index("-C") + 1]), claude[0][0],
                         "one frozen checkout for both")
        # Sol had written its answer when Claude looked: no answer is beside the receipt while another runs.
        self.assertFalse([name for name in json.loads(pathlib.Path(str(reviews) + ".seen").read_text())
                          if name.endswith(".md")])
        prompt = (self.tmp / "codex.log.prompt").read_text()
        self.assertEqual(prompt, (self.tmp / "claude.log.prompt").read_text(), "one identical prompt")
        self.assertIn("+baz", prompt)
        receipt = self.review("a")
        self.assertEqual(receipt["status"], "finished")
        self.assertNotIn("forced", receipt, "a configured panel forces nothing")
        self.assertEqual([(e["member"], e["reviewer"], e["model"], e["effort"], e["status"], e["numbers"])
                          for e in receipt["panel"]],
                         [("sol", "sol", "gpt-6.1-sol", "medium", "finished", [1, 2]),
                          ("opus", "opus", "claude-opus-5-5", "medium", "finished", [3, 4])])
        self.assertTrue(all(isinstance(e["seconds"], (int, float)) for e in receipt["panel"]))
        self.assertEqual(pathlib.Path(receipt["panel"][1]["output"]).read_text(), OPUS_FINDINGS)
        self.assertEqual([(f["n"], f["reviewer"], f["severity"]) for f in receipt["findings"]],
                         [(1, "sol", "P1"), (2, "sol", "P2"), (3, "opus", "P2"), (4, "opus", "P3")])
        self.assertEqual(len(receipt["prompt_sha256"]), 64)
        self.assertEqual(receipt["reviewer"], "sol+opus")
        self.assertIn("panel sol: sol gpt-6.1-sol medium", output)
        self.assertIn("3. P2 lib/api.txt:2 baz has no test (opus)", output)
        # Marking works across the panel, and the gate holds while any reviewer's P1/P2 is unmarked.
        self.lane("review", "a", "--mark", "1", "rejected:not a bug", "--mark", "3", "rejected:covered")
        marked = self.lane("review", "a", "--mark", "2", "rejected:by design")
        self.assertIn("1 of 4 review findings unmarked (#4)", marked)
        compare = self.lane("review", "a", "--compare")
        self.assertIn("sol   gpt-6.1-sol medium: 2 findings (p1 1, p2 1, p3 0), fixed 0, rejected 2, unmarked 0",
                      compare)
        self.assertIn("opus  claude-opus-5-5 medium: 2 findings (p1 0, p2 1, p3 1), fixed 0, rejected 1, unmarked 1",
                      compare)
        self.assertIn("#3 P2 lib/api.txt:2 baz has no test  [rejected: covered]", compare)
        self.assertIn("#4 P3 lib/api.txt:1 foo is unsorted  [unmarked]", compare)
        stats = json.loads(self.lane("stats", "--json"))
        self.assertEqual([(r["reviewer"], r["reviews"], r["findings"], r["p1"], r["p2"], r["p3"], r["fixed"],
                           r["rejected"], r["unmarked"]) for r in stats["reviewers"]],
                         [("opus", 1, 2, 0, 1, 1, 0, 1, 1), ("sol", 1, 2, 1, 1, 0, 0, 2, 0)])
        self.assertIn("Reviewers:", self.lane("stats"))

    def test_one_failed_member_keeps_the_others_answer_and_holds_only_a_required_panel(self):
        self.review_text.write_text(NO_FINDINGS)
        self.env["FIXTURE_CLAUDE_FAIL"] = "1"
        self.panel()
        self.landed()
        output = self.lane("review", "a")
        receipt = self.review("a")
        self.assertEqual(receipt["status"], "finished")
        self.assertEqual(self.members("a"), [("sol", "finished"), ("opus", "failed")])
        self.assertIn("overloaded", receipt["panel"][1]["error"])
        self.assertIn("panel opus: failed: the Opus run failed (exit 1)", output)
        tip = self.tip()
        self.assertFalse(json.loads(self.lane("findings", tip, "--json", ok=None))["held"])
        config = self.integration / ".lanefather.toml"
        config.write_text(config.read_text() + "panel_required = true\n")
        answer = json.loads(self.lane("findings", tip, "--json", ok=None))
        self.assertTrue(answer["held"])
        self.assertIn("panel member opus gave no review", answer["unfinished"][0]["text"])
        compare = self.lane("review", "a", "--compare")
        self.assertIn("opus  claude-opus-5-5 medium: failed: the Opus run failed", compare)

    def test_a_used_up_codex_week_without_role_skips_sol_and_a_required_panel_does_not_wait_for_it(self):
        self.env["FIXTURE_CODEX_USED"] = "95"
        self.panel(required=True)
        self.landed()
        self.lane("review", "a")
        self.assertEqual(self.members("a"), [("sol", "skipped"), ("opus", "finished")])
        self.assertIn("5% of the weekly limit left, under 15%; its fallback opus is already on the panel",
                      self.review("a")["panel"][0]["reason"])
        self.assertEqual([c[2] for c in self.codex_calls()], ["app-server"], "Sol does not run")
        # The rules skipped Sol: nothing that should have answered is missing (fathers#151 review, finding 6).
        self.assertFalse(json.loads(self.lane("findings", self.tip(), "--json", ok=None))["held"])

    def test_an_openai_implementer_keeps_sol_off_the_panel_with_and_without_role(self):
        self.table(textwrap.dedent("""\
            schema = 1
            [models.opus]
            id = "claude-opus-5-5"
            family = "anthropic"
            cli = "claude"
            [models.sol]
            id = "gpt-6.1-sol"
            family = "openai"
            cli = "codex"
            [roles.review]
            choices = [
              { model = "sol", effort = "medium", when = { not_family = true, limit = { codex_weekly = 15 } } },
              { model = "opus", effort = "medium" },
            ]
            """))
        self.panel()
        self.landed("own", "lib/own.txt", "baz\n", agent="codex")
        self.lane("review", "own")
        self.assertEqual(self.members("own"), [("sol", "skipped"), ("opus", "finished")])
        self.assertIn("same family as the implementer (openai)", self.review("own")["panel"][0]["reason"])
        self.with_role()
        self.landed("role", "lib/role.txt", "baz\n", agent="codex")
        self.lane("review", "role")
        self.assertEqual(self.members("role"), [("sol", "skipped"), ("opus", "finished")])
        self.assertIn("sol: same family as the implementer (openai)", self.review("role")["panel"][0]["reason"])
        self.assertEqual([c for c in self.codex_calls() if c[2] == "exec"], [])

    def test_a_panel_member_role_chose_runs_with_roles_model_and_effort(self):
        self.with_role()
        self.env["FIXTURE_ROLE_EFFORT"] = "high"
        self.panel()
        self.landed(agent="claude-code")
        self.lane("review", "a")
        sol = self.review("a")["panel"][0]
        self.assertEqual((sol["reviewer"], sol["model"], sol["effort"]), ("sol", "gpt-6.1-sol", "high"))
        execution = next(c for c in self.codex_calls() if c[2] == "exec")
        self.assertIn('model_reasoning_effort="high"', execution)

    def test_a_panel_of_one_is_refused(self):
        self.configure('panel = ["sol"]\n')
        self.assertIn("two reviewers or more", self.lane("list", ok=False))

    def test_receipt_readers_ignore_keys_they_do_not_know(self):
        """A receipt a later release wrote (fathers#151 review, finding 11): unknown keys are read past."""
        self.landed()
        self.lane("review", "a")
        for name in ("a.receipt.json", "a.review.json"):
            path = self.state / name
            path.write_text(json.dumps(dict(json.loads(path.read_text()), later_field={"x": 1})))
        listed = json.loads(self.lane("list", "--all", "--json"))
        self.assertIn("a", [lane["id"] for lane in listed["lanes"]])
        self.assertIn("Review of a", self.lane("review", "a"))
        self.assertFalse(json.loads(self.lane("findings", self.tip(), "--json", ok=None))["held"])


class RoleTests(ReviewCase):
    def test_role_picks_sol_for_an_anthropic_implementer_with_room_on_the_codex_week(self):
        log = self.with_role()
        self.env["CLAUDE_CODE_SESSION_ID"] = "implementer-session"
        self.landed()
        del self.env["CLAUDE_CODE_SESSION_ID"]
        self.assertEqual(json.loads((self.state / "a.receipt.json").read_text())["agent"], "claude-code")
        self.lane("review", "a")
        calls = self.role_calls(log)
        self.assertEqual(calls[0]["argv"], ["review", "--implementer-family", "anthropic", "--json"])
        self.assertEqual(calls[0]["models"], str(self.tmp / "mastack" / "models.toml"))
        self.assertTrue(calls[0]["codex"].endswith("/bin/codex"), calls[0])
        receipt = self.review("a")
        self.assertEqual((receipt["reviewer"], receipt["model"], receipt["codex_left"], receipt["fallback"]),
                         ("sol", "gpt-6.1-sol", 73, None))
        # role read the limit; Lanefather did not read it again.
        self.assertEqual([c[2] for c in self.codex_calls()], ["exec"])

    def test_a_codex_thread_inside_a_claude_session_is_the_codex_implementer(self):
        log = self.with_role()
        self.env.update(CLAUDE_CODE_SESSION_ID="outer-session", CODEX_THREAD_ID="codex-thread")
        self.landed()
        for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            del self.env[name]
        self.assertEqual(json.loads((self.state / "a.receipt.json").read_text())["agent"], "codex")
        self.lane("review", "a")
        self.assertEqual(self.role_calls(log)[0]["argv"], ["review", "--implementer-family", "openai", "--json"])

    def test_a_used_up_codex_week_with_role_runs_opus_alone_with_the_reason(self):
        self.with_role()
        self.env["CLAUDE_CODE_SESSION_ID"] = "implementer-session"
        self.env["FIXTURE_CODEX_USED"] = "100"
        self.landed("single", "lib/single.txt", "baz\n")
        self.lane("review", "single")
        receipt = self.review("single")
        self.assertEqual((receipt["reviewer"], receipt["model"]), ("opus", "claude-opus-5-5"))
        self.assertEqual(receipt["fallback"], "sol: codex_weekly 0% left, needs 15%")
        self.assertEqual(receipt["role"]["skipped"], ["sol: codex_weekly 0% left, needs 15%",
                                                      "opus: same family as the implementer (anthropic)"])
        self.panel()
        self.landed("panel", "lib/panel.txt", "baz\n")
        output = self.lane("review", "panel")
        self.assertEqual(self.members("panel"), [("sol", "skipped"), ("opus", "finished")])
        self.assertIn("sol: codex_weekly 0% left, needs 15%; its fallback opus is already on the panel",
                      self.review("panel")["panel"][0]["reason"])
        self.assertIn("panel member sol:", output)
        self.assertEqual(self.codex_calls(), [], "neither Sol nor a second limit read runs")
        self.assertEqual(len(self.claude_calls()), 2)

    def test_an_unknown_implementer_leaves_the_family_out_and_shows_roles_caution(self):
        log = self.with_role()
        self.landed()
        output = self.lane("review", "a")
        self.assertEqual(self.role_calls(log)[0]["argv"], ["review", "--json"])
        self.assertIn("implementer family not given", output)
        receipt = self.review("a")
        self.assertEqual(receipt["reviewer"], "opus")
        self.assertIn("implementer family not given", receipt["role"]["caution"])

    def test_a_role_that_fails_leaves_the_choice_to_the_models_file_as_before(self):
        self.with_role()
        self.env["FIXTURE_ROLE_EXIT"] = "2"
        self.landed()
        output = self.lane("review", "a")
        self.assertIn("cannot read the table", output)
        receipt = self.review("a")
        self.assertEqual((receipt["reviewer"], receipt["codex_left"]), ("sol", 73))
        self.assertIn("exit 2", receipt["role_error"])

    def shared_cases(self, real: pathlib.Path | None) -> None:
        cases = json.loads((FIXTURES / "review-role-cases.json").read_text())
        if real:
            self.with_role(cases["table"], real=real)
        else:
            self.table(cases["table"])
        for index, case in enumerate(cases["cases"]):
            with self.subTest(case=case["name"]):
                lane_id = f"case{index}"
                self.env.update(FIXTURE_CODEX_USED=str(case["used"]), FIXTURE_CODEX_REACHED=case.get("reached") or "")
                agent = {"anthropic": "claude-code", "openai": "codex"}[case["family"]]
                self.landed(lane_id, f"lib/{lane_id}.txt", "baz\n", agent=agent)
                self.lane("review", lane_id)
                self.assertEqual(self.review(lane_id)["reviewer"], case["expect"])

    def test_the_shared_role_cases_choose_the_same_reviewer_without_role(self):
        """tests/fixtures/review-role-cases.json: the models.toml review role, stand-in limit answers, the
        implementer's family and the reviewer each must give."""
        self.shared_cases(None)

    @unittest.skipUnless(REAL_ROLE.is_file(), f"no Mastack role command at {REAL_ROLE}")
    def test_mastacks_own_role_chooses_the_same_reviewer_for_the_shared_cases(self):
        self.shared_cases(REAL_ROLE)


class ChosenReviewerTests(ReviewCase):
    """fathers#155: `review --reviewer sol|opus|both` per call, and the review role's choices as a priority list."""

    def test_a_forced_reviewer_runs_whatever_the_codex_limit_and_the_receipt_says_so(self):
        self.landed("opus", "lib/opus.txt", "baz\n")
        output = self.lane("review", "opus", "--reviewer", "opus")
        self.assertEqual(self.codex_calls(), [], "neither Sol nor a limit read")
        receipt = self.review("opus")
        self.assertEqual((receipt["reviewer"], receipt["forced"]), ("opus", {"by": "flag", "reviewers": ["opus"]}))
        self.assertIn("forced by --reviewer opus", output)
        self.env["FIXTURE_CODEX_USED"] = "100"
        self.landed("sol", "lib/sol.txt", "baz\n")
        self.lane("review", "sol", "--reviewer", "sol")
        self.assertEqual([c[2] for c in self.codex_calls()], ["exec"], "Sol runs on a used-up week when asked")
        self.assertEqual(self.review("sol")["reviewer"], "sol")

    def test_both_runs_sol_and_opus_into_one_list_that_marks_and_holds_until_every_finding_is_marked(self):
        self.review_text.write_text(TWO_FINDINGS)
        claude_text = self.tmp / "claude-review.md"
        claude_text.write_text(OPUS_FINDINGS)
        self.env["FIXTURE_CLAUDE_TEXT"] = str(claude_text)
        self.landed()
        self.lane("review", "a", "--reviewer", "both")
        receipt = self.review("a")
        self.assertEqual(receipt["forced"], {"by": "flag", "reviewers": ["sol", "opus"]})
        self.assertEqual([(f["n"], f["reviewer"]) for f in receipt["findings"]],
                         [(1, "sol"), (2, "sol"), (3, "opus"), (4, "opus")])
        for number in ("1", "2", "3"):
            self.assertIn("unmarked", self.lane("review", "a", "--mark", number, "rejected:not here"))
        done = self.lane("review", "a", "--mark", "4", "rejected:not here")
        self.assertNotIn("unmarked", done)

    def test_reviewer_with_mark_compare_or_fix_lane_is_refused(self):
        self.landed()
        self.lane("review", "a")
        for extra in (("--mark", "1", "rejected:x"), ("--compare",), ("--fix-lane",)):
            with self.subTest(extra=extra):
                self.assertIn("does not combine", self.lane("review", "a", "--reviewer", "opus", *extra, ok=False))

    def test_the_forced_reviewer_also_runs_an_overall_review(self):
        self.landed()
        sha = self.tip()
        self.lane("review", "--overall", sha, "--since", f"{sha}~1", "--reviewer", "opus")
        receipt = self.review(f"ship-{sha[:12]}")
        self.assertEqual((receipt["reviewer"], receipt["forced"]["by"]), ("opus", "flag"))
        self.assertEqual(self.codex_calls(), [])

    def test_reversing_the_review_choices_reverses_the_preference_and_keeps_sol_as_the_fallback(self):
        self.table(REVERSED_TABLE.replace("{when}", ", when = { limit = { codex_weekly = 15 } }"))
        self.landed("first")
        self.lane("review", "first")
        receipt = self.review("first")
        self.assertEqual((receipt["reviewer"], receipt["effort"], receipt["fallback"]), ("opus", "high", None))
        self.assertEqual([c[2] for c in self.codex_calls()], ["app-server"], "Sol only stands by: no run")
        # The ordered fallback goes both ways: a failed Opus hands the review to Sol.
        self.env["FIXTURE_CLAUDE_FAIL"] = "1"
        self.landed("failed", "lib/failed.txt", "baz\n")
        self.lane("review", "failed")
        receipt = self.review("failed")
        self.assertEqual((receipt["status"], receipt["reviewer"]), ("finished", "sol"))
        self.assertIn("the Opus run failed (exit 1)", receipt["fallback"])

    def test_a_codex_choice_without_a_limit_still_needs_15_percent_of_the_week(self):
        self.table(REVERSED_TABLE.replace("{when}", "").replace(
            '{ model = "opus", effort = "high" },\n  { model = "sol", effort = "medium" },',
            '{ model = "sol", effort = "medium" },\n  { model = "opus", effort = "high" },'))
        self.env["FIXTURE_CODEX_USED"] = "95"
        self.landed()
        self.lane("review", "a")
        receipt = self.review("a")
        self.assertEqual(receipt["reviewer"], "opus")
        self.assertIn("5% of the weekly limit left, under 15%", receipt["fallback"])
        self.assertEqual([c[2] for c in self.codex_calls()], ["app-server"])


class DesignQuestionTests(ReviewCase):
    def test_every_reviewer_is_asked_the_two_design_questions_and_a_design_finding_keeps_its_category(self):
        self.review_text.write_text('```json\n{"findings": [{"severity": "P2", "category": "design", "file": '
                                    '"lib/api.txt", "line": 2, "title": "baz copies foo", "detail": "reuse foo"}]}\n```\n')
        self.landed()
        output = self.lane("review", "a")
        prompt = (self.tmp / "codex.log.prompt").read_text()
        for text in ("does the change fit the structure it lands in, or is it bolted on beside it, and which "
                     "refactor would make it fit?",
                     "does it add a copy of behaviour the repository already has? name the existing code.",
                     "design (an answer to one of"):
            self.assertIn(text, " ".join(prompt.split()).lower())
        self.assertEqual(self.review("a")["findings"][0]["category"], "design")
        self.assertIn("P2 [design] lib/api.txt:2 baz copies foo", output)
