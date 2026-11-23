"""The reviewer crosses the same explicit project interface from direct and CLI callers, and supports the third
reviewer backend (Antigravity / Google / agy) matching Mastack's models.toml (fathers#201)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import tempfile
import textwrap
import unittest
import unittest.mock

from support import TWO_FINDINGS, LaneCase

from lanefather import config, events, receipts, review, reviewer, state

THREE_FAMILY_TABLE = textwrap.dedent("""\
    schema = 1

    [models.fable]
    id = "claude-fable-5-1"
    family = "anthropic"
    cli = "claude"
    tier = "frontier"

    [models.opus]
    id = "claude-opus-5-5"
    family = "anthropic"
    cli = "claude"
    tier = "strong"

    [models.sonnet]
    id = "claude-sonnet-5-5"
    family = "anthropic"
    cli = "claude"
    tier = "routine"

    [models.sol]
    id = "gpt-6.1-sol"
    family = "openai"
    cli = "codex"
    tier = "strong"

    [models.neon]
    id = "gemini-4-neon"
    family = "google"
    cli = "antigravity"
    tier = "frontier"

    [models.argon]
    id = "gemini-4-argon"
    family = "google"
    cli = "antigravity"
    tier = "strong"

    [models.carbon]
    id = "gemini-4-carbon"
    family = "google"
    cli = "antigravity"
    tier = "strong"

    [models.flash]
    id = "gemini-3.8-flash"
    family = "google"
    cli = "antigravity"
    tier = "routine"

    [cli.claude]
    option = "effort"
    run = ["claude", "-p", "--model", "{id}", "--effort", "{effort}"]
    review = ["claude", "-p", "--model", "{id}", "--effort", "{effort}"]

    [cli.codex]
    option = "model_reasoning_effort"
    run = ["codex", "exec", "-c", "model=\\"{id}\\"", "-c", "model_reasoning_effort=\\"{effort}\\""]
    review = ["codex", "review", "-c", "model=\\"{id}\\"", "-c", "model_reasoning_effort=\\"{effort}\\""]

    [cli.antigravity]
    option = "thought_level"
    delegate_tool = "delegate_task"

    [providers.anthropic]
    option = "effort"

    [providers.openai]
    option = "model_reasoning_effort"

    [providers.google]
    option = "thought_level"

    [roles.review]
    choices = [
      { model = "sol", effort = "medium", when = { not_family = true, limit = { codex_weekly = 15 } } },
      { model = "opus", effort = "medium", when = { not_family = true } },
      { model = "neon", effort = "medium", when = { not_family = true } },
      { model = "opus", effort = "medium", note = "same family as the implementer: fresh context only" },
    ]
    """)


def agy_argv(model: str, effort: str) -> list[str]:
    """The built-in `agy` review command line after the executable (fathers#201)."""
    return ["--model", model, "--effort", effort, "--mode", "plan", "--sandbox", "--disable-slash-commands",
            "--print-timeout", "1740s", "--input-format", "stream-json", "--output-format", "stream-json", "-p="]


# A `[cli.antigravity].review` command: logs like the stand-in agy, takes the raw prompt and writes the answer to -o.
TEMPLATE_STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
log = os.environ["FIXTURE_ANTIGRAVITY_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps([os.path.realpath(sys.argv[0]), os.path.realpath(os.getcwd()), *args]) + "\n")
pathlib.Path(log + ".prompt").write_text(sys.stdin.read())
pathlib.Path(args[args.index("-o") + 1]).write_text(pathlib.Path(os.environ["FIXTURE_REVIEW_TEXT"]).read_text())
'''


class ReviewerProjectTests(LaneCase):
    def test_the_review_request_uses_its_project_when_another_project_is_current(self) -> None:
        tip = self.tip()
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            configured = config.Config(project="Selected project", coding_standards="SELECTED.md")
            other = config.Config(project="Other project", coding_standards="OTHER.md")
            runner = reviewer.Reviewer(configured)
            _other_runner = reviewer.Reviewer(other)
            answer = runner.run("direct", str(self.integration),
                                reviewer.Request("selected standards", "selected ticket", tip, tip),
                                self.tmp / "answer.md", self.tmp / "reviewer.log")
        self.assertIsNone(answer.error)
        self.assertEqual(answer.evidence["findings"], [])
        prompt = (self.tmp / "codex.log.prompt").read_text()
        for expected in ("Selected project", "SELECTED.md", "selected standards", "selected ticket"):
            self.assertIn(expected, prompt)
        self.assertNotIn("Other project", prompt)
        self.assertFalse(self.state.exists(), "the reviewer does not read or write lane state")


class AntigravityReviewerTests(LaneCase):
    """fathers#201: Antigravity / Google / agy reviewer backend selectable via `--reviewer` and `not_family = true`
    cross-family routing, recording actual provider/model/options in the review receipt."""

    def landed(self, lane_id: str, path: str = "lib/api.txt", text: str = "foo\nbaz\n", *,
               agent: str | None = None) -> str:
        worktree = self.created(lane_id, path)
        self.commit(worktree, {path: text})
        self.lane("check", lane_id, *(("--agent", agent) if agent else ()))
        self.lane("land", lane_id)
        return worktree

    def table(self, text: str = THREE_FAMILY_TABLE) -> None:
        self.write(self.tmp / "table", "models.toml", text)
        self.env["LANEFATHER_MODELS"] = str(self.tmp / "table" / "models.toml")

    def test_explicit_antigravity_google_agy_and_model_specs_select_antigravity_reviewer(self) -> None:
        self.table()
        self.review_text.write_text(TWO_FINDINGS)
        cases = (
            ("antigravity", "neon", "gemini-4-neon", "medium"),
            ("google", "neon", "gemini-4-neon", "medium"),
            ("agy", "neon", "gemini-4-neon", "medium"),
            ("neon", "neon", "gemini-4-neon", "medium"),
            ("argon", "argon", "gemini-4-argon", "medium"),
            ("carbon", "carbon", "gemini-4-carbon", "medium"),
            ("flash", "flash", "gemini-3.8-flash", "medium"),
            ("gemini-4-argon:high", "argon", "gemini-4-argon", "high"),
            ("antigravity:gemini-4-carbon:high", "carbon", "gemini-4-carbon", "high"),
        )
        for index, (spec, expected_alias, expected_id, expected_effort) in enumerate(cases):
            with self.subTest(spec=spec):
                lane_id = f"ag{index}"
                self.landed(lane_id, f"lib/{lane_id}.txt", f"foo\n{lane_id}\n")
                before = len(self.antigravity_calls())
                output = self.lane("review", lane_id, "--reviewer", spec)
                calls = self.antigravity_calls()[before:]
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][2:], agy_argv(expected_id, expected_effort))
                prompt = (self.tmp / "antigravity.log.prompt").read_text()
                self.assertIn(f"+{lane_id}", prompt)
                receipt = self.review(lane_id)
                self.assertEqual(
                    (receipt["status"], receipt["reviewer"], receipt["model"], receipt["effort"],
                     receipt["provider"], receipt["cli"], receipt["options"]),
                    ("finished", expected_alias, expected_id, expected_effort, "google", "antigravity",
                     {"thought_level": expected_effort}),
                )
                self.assertEqual(receipt["forced"], {"by": "flag", "reviewers": [spec]})
                member = receipt["panel"][0]
                self.assertEqual((member["session"], member["usage"]["total_tokens"]), ("conv-fixture-1", 1320))
                self.assertEqual(len(receipt["findings"]), 2)
                self.assertIn(f"by {expected_id} {expected_effort}", output)
                self.assertIn(f"forced by --reviewer {spec}", output)
                # Clean up findings so next lane in the same repo is not blocked at land.
                self.lane("review", lane_id, "--mark", "1", "rejected:test", "--mark", "2", "rejected:test")

    def test_cross_family_not_family_routes_to_antigravity_for_anthropic_and_openai_authors(self) -> None:
        self.table()
        # 1. Anthropic implementer + Codex quota exhausted -> skips Sol (limit), skips Opus (same family), picks Neon.
        self.env["FIXTURE_CODEX_USED"] = "95"
        self.landed("from-claude", "lib/from-claude.txt", "baz\n", agent="claude-code")
        self.lane("review", "from-claude")
        receipt = self.review("from-claude")
        self.assertEqual(
            (receipt["status"], receipt["reviewer"], receipt["model"], receipt["provider"],
             receipt["cli"], receipt["options"]),
            ("finished", "neon", "gemini-4-neon", "google", "antigravity", {"thought_level": "medium"}),
        )
        self.assertIn("5% of the weekly limit left", receipt["fallback"])
        self.assertIn("same family as the implementer (anthropic)", receipt["fallback"])

        # 2. OpenAI implementer + Claude CLI unavailable (or failed) -> skips Sol (same family), falls back to Neon.
        self.env["FIXTURE_CODEX_USED"] = "20"
        self.env["LANEFATHER_CLAUDE"] = str(self.tmp / "no-such-claude")
        self.landed("from-codex", "lib/from-codex.txt", "baz\n", agent="codex")
        self.lane("review", "from-codex")
        receipt2 = self.review("from-codex")
        self.assertEqual(
            (receipt2["status"], receipt2["reviewer"], receipt2["model"], receipt2["provider"],
             receipt2["cli"], receipt2["options"]),
            ("finished", "neon", "gemini-4-neon", "google", "antigravity", {"thought_level": "medium"}),
        )
        self.assertIn("same family as the implementer (openai)", receipt2["fallback"])
        self.assertIn("no claude CLI", receipt2["fallback"])
        del self.env["LANEFATHER_CLAUDE"]

        # 3. Google / Antigravity implementer (`antigravity` or `t3-code`) -> Sol when Codex has quota, Opus when low.
        self.env["FIXTURE_CODEX_USED"] = "20"
        self.landed("from-ag", "lib/from-ag.txt", "baz\n", agent="antigravity")
        self.lane("review", "from-ag")
        receipt3 = self.review("from-ag")
        self.assertEqual(
            (receipt3["reviewer"], receipt3["provider"], receipt3["cli"], receipt3["options"]),
            ("sol", "openai", "codex", {"model_reasoning_effort": "medium"}),
        )

        self.env["FIXTURE_CODEX_USED"] = "95"
        self.landed("from-t3", "lib/from-t3.txt", "baz\n", agent="t3-code")
        self.lane("review", "from-t3")
        receipt4 = self.review("from-t3")
        self.assertEqual(
            (receipt4["reviewer"], receipt4["provider"], receipt4["cli"], receipt4["options"]),
            ("opus", "anthropic", "claude", {"effort": "medium"}),
        )

    def test_runtime_failure_falls_back_across_codex_claude_and_antigravity(self) -> None:
        self.table()
        self.env["FIXTURE_CODEX_EXEC"] = "model-error"
        self.env["FIXTURE_CLAUDE_FAIL"] = "1"
        self.landed("chain", "lib/chain.txt", "baz\n")
        output = self.lane("review", "chain")
        del self.env["FIXTURE_CODEX_EXEC"], self.env["FIXTURE_CLAUDE_FAIL"]
        receipt = self.review("chain")
        self.assertEqual(
            (receipt["status"], receipt["reviewer"], receipt["model"], receipt["provider"],
             receipt["cli"], receipt["options"]),
            ("finished", "neon", "gemini-4-neon", "google", "antigravity", {"thought_level": "medium"}),
        )
        self.assertIn("the Opus run failed", receipt["fallback"])
        self.assertIn("gemini-4-neon medium", output)

    def test_custom_antigravity_cli_review_template_and_output_placeholder(self) -> None:
        """`[cli.antigravity].review` stays the escape hatch: its own command, the raw prompt on stdin, the answer
        in `{output}`."""
        stub = self.tmp / "bin" / "agy-review"
        stub.write_text(TEMPLATE_STUB)
        stub.chmod(0o755)
        custom_table = THREE_FAMILY_TABLE.replace(
            'delegate_tool = "delegate_task"',
            'review = ["agy-review", "-C", "{checkout}", "--model", "{id}", "--thought-level", "{thought_level}", '
            '"-o", "{output}"]',
        )
        self.table(custom_table)
        self.landed("custom", "lib/custom.txt", "baz\n")
        self.lane("review", "custom", "--reviewer", "antigravity")
        call = self.antigravity_calls()[-1]
        self.assertEqual(call[2], "-C")
        self.assertEqual(call[4:], ["--model", "gemini-4-neon", "--thought-level", "medium", "-o", call[-1]])
        self.assertIn("+baz", (self.tmp / "antigravity.log.prompt").read_text(), "the raw prompt, not stream-json")
        receipt = self.review("custom")
        self.assertEqual(receipt["status"], "finished")
        self.assertEqual(receipt["provider"], "google")
        self.assertEqual(receipt["options"], {"thought_level": "medium"})
        self.assertNotIn("session", receipt)

    def test_a_run_template_is_not_used_for_reviews(self) -> None:
        """A `run` command implements and need not be read-only: a review keeps the built-in `agy` contract."""
        self.table(THREE_FAMILY_TABLE.replace(
            'delegate_tool = "delegate_task"', 'run = ["agy-implement", "--model", "{id}"]'))
        self.landed("run", "lib/run.txt", "baz\n")
        self.lane("review", "run", "--reviewer", "neon")
        self.assertEqual(self.antigravity_calls()[-1][2:], agy_argv("gemini-4-neon", "medium"))
        self.assertEqual(self.review("run")["status"], "finished")

    def test_an_antigravity_error_result_hands_the_review_to_the_next_choice(self) -> None:
        """`status: ERROR` (a used-up Gemini quota) fails the run with its error even when the stream is readable;
        the next choice reviews and the receipt says why."""
        self.table(THREE_FAMILY_TABLE.replace(
            '{ model = "sol", effort = "medium", when = { not_family = true, limit = { codex_weekly = 15 } } },',
            '{ model = "neon", effort = "minimal" },'))
        self.env["FIXTURE_ANTIGRAVITY_MODE"] = "error"
        self.landed("quota", "lib/quota.txt", "baz\n")
        self.lane("review", "quota")
        self.assertEqual(self.antigravity_calls()[-1][2:], agy_argv("gemini-4-neon", "low"), "minimal runs as low")
        receipt = self.review("quota")
        self.assertEqual((receipt["status"], receipt["reviewer"]), ("finished", "opus"))
        self.assertIn("the Neon run failed (exit 1): RESOURCE_EXHAUSTED: the Gemini quota is used up",
                      receipt["fallback"])
        self.assertNotIn("session", receipt, "the session is the failed run's, not the reviewer's")

    def test_a_forced_antigravity_reviewer_that_answers_nothing_fails_the_review(self) -> None:
        for mode, expected in (("empty", "Neon returned an empty review (no result in its output)"),
                               ("fail", "the Neon run failed (exit 1): agy: backend unavailable"),
                               ("write", "the Neon run changed the review checkout: ?? written-by-agy.txt")):
            with self.subTest(mode=mode):
                self.env["FIXTURE_ANTIGRAVITY_MODE"] = mode
                self.landed(mode, f"lib/{mode}.txt", "baz\n")
                self.lane("review", mode, "--reviewer", "neon", ok=False)
                receipt = self.review(mode)
                self.assertEqual(receipt["status"], "failed")
                self.assertIn(expected, receipt["panel"][0]["error"])
                self.assertEqual(receipt["panel"][0]["status"], "failed", "--reviewer takes no stand-in")

    def test_a_prompt_over_one_arguments_limit_goes_on_stdin(self) -> None:
        """Linux caps one argument at 128 KiB; the inlined diff can be 400 KB, so the prompt never rides on argv."""
        big = "".join(f"line {n:06d} of a large generated file\n" for n in range(6000))
        self.landed("big", "lib/big.txt", big)
        self.lane("review", "big", "--reviewer", "neon")
        call = self.antigravity_calls()[-1]
        self.assertLess(sum(len(arg) for arg in call), 4096)
        prompt = (self.tmp / "antigravity.log.prompt").read_text()
        self.assertGreater(len(prompt.encode()), 128 * 1024)
        self.assertIn("+line 005999 of a large generated file", prompt)
        self.assertEqual(self.review("big")["status"], "finished")

    def test_three_family_panel_records_provider_model_and_options_per_member(self) -> None:
        self.table()
        config_file = self.integration / ".lanefather.toml"
        self.commit(
            self.integration,
            {".lanefather.toml": config_file.read_text() + '\n[review]\npanel = ["sol", "opus", "neon"]\n'},
            "configure 3-family panel",
        )
        self.landed("panel3", "lib/panel3.txt", "baz\n", agent="antigravity")
        self.lane("review", "panel3")
        receipt = self.review("panel3")
        self.assertEqual(receipt["status"], "finished")
        self.assertEqual(receipt["reviewer"], "sol+opus+neon")
        self.assertEqual(receipt["provider"], "openai+anthropic+google")
        self.assertEqual(receipt["cli"], "codex+claude+antigravity")
        self.assertEqual(
            receipt["options"],
            {"model_reasoning_effort": "medium", "effort": "medium", "thought_level": "medium"},
        )
        panel = receipt["panel"]
        self.assertEqual(
            [(e["member"], e["reviewer"], e["provider"], e["cli"], e["model"], e["options"], e["status"])
             for e in panel],
            [
                ("sol", "sol", "openai", "codex", "gpt-6.1-sol", {"model_reasoning_effort": "medium"}, "finished"),
                ("opus", "opus", "anthropic", "claude", "claude-opus-5-5", {"effort": "medium"}, "finished"),
                ("neon", "neon", "google", "antigravity", "gemini-4-neon", {"thought_level": "medium"}, "finished"),
            ],
        )
        # Neon is the same family as the implementer (google), so it carries the same-family note on the panel.
        self.assertEqual(panel[2]["note"], "same family as the implementer (google)")

    def panel(self, *members: str) -> None:
        config_file = self.integration / ".lanefather.toml"
        self.commit(self.integration, {".lanefather.toml": config_file.read_text()
                                       + f"\n[review]\npanel = {json.dumps(list(members))}\n"}, "configure the panel")

    def test_a_panel_member_that_fails_at_runtime_takes_a_stand_in_off_the_panel(self) -> None:
        """Both quotas out (2026-11-21): Sol is skipped for the Codex week, Opus fails on Claude's, and Neon, the
        next candidate nobody on the panel reviews with, reviews in Opus's place."""
        self.table()
        self.panel("sol", "opus")
        self.env["FIXTURE_CODEX_USED"] = "95"
        self.env["FIXTURE_CLAUDE_FAIL"] = "1"
        self.landed("both", "lib/both.txt", "baz\n")
        output = self.lane("review", "both")
        receipt = self.review("both")
        self.assertEqual(receipt["status"], "finished")
        self.assertEqual([(e["member"], e["reviewer"], e["status"]) for e in receipt["panel"]],
                         [("sol", "sol", "skipped"), ("opus", "neon", "finished")])
        opus = receipt["panel"][1]
        self.assertIn("the Opus run failed (exit 1)", opus["fallback"])
        self.assertTrue(opus["fallback"].endswith(": neon reviews in its place"), opus["fallback"])
        self.assertEqual((opus["model"], opus["cli"], opus["session"]), ("gemini-4-neon", "antigravity",
                                                                        "conv-fixture-1"))
        self.assertIn("opus: the Opus run failed", receipt["fallback"])
        self.assertIn("gemini-4-neon medium", output)
        self.assertIn("neon reviews in its place", output)
        compared = self.lane("review", "both", "--compare")
        self.assertIn("for opus: the Opus run failed", compared)
        self.assertNotIn("for opus: None", compared)

    def test_two_failing_members_never_take_the_same_stand_in(self) -> None:
        self.table()
        self.panel("opus", "fable")
        self.env["FIXTURE_CODEX_USED"] = "95"
        self.env["FIXTURE_CLAUDE_FAIL"] = "1"
        self.landed("pool", "lib/pool.txt", "baz\n")
        self.lane("review", "pool")
        receipt = self.review("pool")
        self.assertEqual([(e["member"], e["reviewer"], e["status"]) for e in receipt["panel"]],
                         [("opus", "neon", "finished"), ("fable", "fable", "failed")])
        self.assertEqual(len([c for c in self.antigravity_calls() if c[2:3] == ["--model"]]), 1)

    def test_a_forced_panel_takes_no_stand_in(self) -> None:
        self.table()
        self.env["FIXTURE_CLAUDE_FAIL"] = "1"
        self.landed("strict", "lib/strict.txt", "baz\n")
        self.lane("review", "strict", "--reviewer", "opus", ok=False)
        receipt = self.review("strict")
        self.assertEqual([(e["reviewer"], e["status"]) for e in receipt["panel"]], [("opus", "failed")])
        self.assertEqual(self.antigravity_calls(), [])


class AgyResultTests(unittest.TestCase):
    """The `agy --output-format stream-json` decoder (fathers#201)."""

    def test_the_last_result_events_response_is_the_answer_with_its_session_and_usage(self) -> None:
        stream = "\n".join([
            "agy 1.5.13 starting",
            json.dumps({"event": "init", "conversation_id": "c-1"}),
            "{not json",
            json.dumps(["an", "array"]),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "first"}}),
            json.dumps({"event": "result", "result": {
                "status": "SUCCESS", "response": "No findings.", "conversation_id": "c-1",
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "note": "text", "cached": True}}}),
            "",
        ])
        self.assertEqual(reviewer._agy_result(stream), reviewer.Decoded(  # pyright: ignore[reportPrivateUsage]
            "No findings.", session="c-1", usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}))

    def test_no_result_event_is_an_empty_answer(self) -> None:
        for stream in ("", "junk\n", json.dumps({"event": "assistant", "text": "partial"}) + "\n"):
            with self.subTest(stream=stream):
                decoded = reviewer._agy_result(stream)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual((decoded.text, decoded.error, decoded.missing), ("", None, True))

    def test_an_error_result_is_the_runs_error_and_an_empty_response_is_empty(self) -> None:
        error = reviewer._agy_result(json.dumps({  # pyright: ignore[reportPrivateUsage]
            "event": "result", "result": {"status": "ERROR", "error": "quota exhausted", "response": "partial",
                                          "conversation_id": "c-2"}}))
        self.assertEqual((error.text, error.error, error.session), ("", "quota exhausted", "c-2"))
        bare = reviewer._agy_result(json.dumps({"event": "result", "result": {"status": "error"}}))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(bare.error, "Antigravity reported an error")
        empty = reviewer._agy_result(json.dumps({"event": "result", "result": {"status": "SUCCESS"}}))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((empty.text, empty.error, empty.missing), ("", None, False))

    def test_the_command_line_and_the_stdin_message(self) -> None:
        model = reviewer.Model("neon", "gemini-4-neon", "antigravity", "minimal", family="google")
        self.assertEqual(reviewer._agy_argv("/opt/agy", model),  # pyright: ignore[reportPrivateUsage]
                         ["/opt/agy", *agy_argv("gemini-4-neon", "low")])
        message = reviewer._agy_message('a "quoted"\nprompt')  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(message.endswith("\n") and message.count("\n") == 1)
        self.assertEqual(json.loads(message), {"event": "user", "message": {"content": 'a "quoted"\nprompt'}})


class AntigravityDiscoveryTests(unittest.TestCase):
    """Where the review finds `agy` (fathers#201): overrides, PATH, the installer's ~/.gemini/bin, T3 Code's bundle."""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-agy-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        (self.tmp / "empty").mkdir()
        self.env = {"HOME": str(self.home), "PATH": str(self.tmp / "empty")}

    def executable(self, path: pathlib.Path) -> pathlib.Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def bundle(self, active: str | None, release: str = "abc123") -> pathlib.Path:
        root = self.home / ".t3" / "tools" / "antigravity-acp" / reviewer._t3_platform()  # pyright: ignore[reportPrivateUsage]
        binary = self.executable(root / "versions" / release / "antigravity")
        if active is not None:
            (root / "active.json").write_text(active)
        return binary

    def found(self, **env: str) -> str | None:
        with unittest.mock.patch.dict(os.environ, {**self.env, **env}, clear=True):
            return reviewer._antigravity_cli(config.Config())  # pyright: ignore[reportPrivateUsage]

    def test_nothing_installed_finds_nothing(self) -> None:
        self.assertIsNone(self.found())

    def test_t3_codes_bundle_through_active_json(self) -> None:
        binary = self.bundle(json.dumps({"releaseId": "abc123"}))
        self.assertEqual(self.found(), str(binary))

    def test_a_missing_or_unreadable_active_json_finds_nothing(self) -> None:
        for active in (None, "{not json", "[]", json.dumps({"releaseId": 7}), json.dumps({"releaseId": "../abc123"}),
                       json.dumps({"releaseId": "other"})):
            with self.subTest(active=active):
                shutil.rmtree(self.home, ignore_errors=True)
                self.bundle(active)
                self.assertIsNone(self.found())

    def test_the_installers_agy_comes_before_the_bundle_and_path_before_both(self) -> None:
        self.bundle(json.dumps({"releaseId": "abc123"}))
        installed = self.executable(self.home / ".gemini" / "bin" / "agy")
        self.assertEqual(self.found(), str(installed))
        on_path = self.executable(self.tmp / "empty" / "agy")
        self.assertEqual(self.found(), str(on_path))

    def test_overrides_win_and_an_empty_one_disables_antigravity(self) -> None:
        self.executable(self.home / ".gemini" / "bin" / "agy")
        chosen = self.executable(self.tmp / "chosen" / "agy")
        self.assertEqual(self.found(LANEFATHER_ANTIGRAVITY=str(chosen)), str(chosen))
        self.assertEqual(self.found(ANTIGRAVITY_CLI_PATH=str(chosen)), str(chosen))
        self.assertEqual(self.found(AGY_CLI_PATH=str(chosen)), str(chosen))
        for name in ("LANEFATHER_ANTIGRAVITY", "LANEFATHER_AGY", "ANTIGRAVITY_CLI_PATH", "AGY_CLI_PATH"):
            with self.subTest(name=name):
                self.assertIsNone(self.found(**{name: ""}))


class ImplementerFamilyTests(unittest.TestCase):
    """The implementer's family (fathers#201): the recorded model first, a harness only when it runs one family."""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-family-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = state.Store(config.DEFAULT, directory=self.tmp / "state")
        self.lanes = 0

    def family(self, checker: str | None, *agents: dict[str, str]) -> str | None:
        self.lanes += 1
        lane = f"l{self.lanes}"
        state.write_json(self.store.record_path(lane), {"id": lane, "agents": list(agents)})
        receipts.write(self.store, lane, {"version": 1, "lane": lane, **({"agent": checker} if checker else {})})
        return review.implementer_family(self.store, lane)

    def test_t3_code_tells_its_family_only_by_its_model(self) -> None:
        self.assertEqual(self.family("t3-code", {"kind": "t3-code", "model": "claude-opus-5-5"}), "anthropic")
        self.assertEqual(self.family("t3-code", {"kind": "t3-code", "model": "gemini-4-neon-medium"}), "google")
        self.assertEqual(self.family("t3-code", {"kind": "t3-code", "model": "gpt-6.1-sol"}), "openai")
        self.assertIsNone(self.family("t3-code", {"kind": "t3-code"}))

    def test_a_single_family_harness_tells_without_a_model_and_a_model_tells_first(self) -> None:
        self.assertEqual(self.family("claude-code", {"kind": "claude-code"}), "anthropic")
        self.assertEqual(self.family("claude-code"), "anthropic")
        self.assertEqual(self.family("codex", {"kind": "codex"}), "openai")
        self.assertEqual(self.family("antigravity", {"kind": "antigravity", "model": "claude-opus-5-5"}),
                         "anthropic")

    def test_without_a_check_agent_the_latest_agent_that_tells_decides(self) -> None:
        self.assertEqual(self.family(None, {"kind": "codex"}, {"kind": "t3-code"}), "openai")
        self.assertEqual(self.family(None, {"kind": "t3-code", "model": "gemini-4-argon"}), "google")
        self.assertIsNone(self.family(None, {"kind": "t3-code"}))

    def test_t3_codes_thread_variable_names_the_agent(self) -> None:
        self.assertEqual(events.agent_session({"T3CODE_THREAD_ID": "thread-1"}), ("t3-code", "thread-1"))
        self.assertEqual(events.agent_session({"T3CODE_THREAD_ID": "thread-1", "CLAUDE_CODE_SESSION_ID": "s"}),
                         ("claude-code", "s"))
        context = events.agent_context("t3-code", env={"T3CODE_THREAD_ID": "thread-1", "T3_MODEL": "claude-opus-5-5"})
        assert context is not None
        self.assertEqual((context.get("thread_id"), context.get("model")), ("thread-1", "claude-opus-5-5"))


if __name__ == "__main__":
    unittest.main()
