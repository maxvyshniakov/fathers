"""The reviewer crosses the same explicit project interface from direct and CLI callers, and supports the third
reviewer backend (Antigravity / Google / agy) matching Mastack's models.toml (fathers#201)."""
from __future__ import annotations

import os
import textwrap
import unittest
import unittest.mock

from support import TWO_FINDINGS, LaneCase

from lanefather import config, reviewer

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


class ReviewerProjectTests(LaneCase):
    def test_the_review_request_uses_its_project_when_another_project_is_current(self) -> None:
        tip = self.tip()
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            configured = config.Config(project="Selected project", coding_standards="SELECTED.md")
            runner = reviewer.Reviewer(configured)
            config.use(config.Config(project="Other project", coding_standards="OTHER.md"))
            self.addCleanup(config.use, None)
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
                self.assertEqual(calls[0][2:], ["-p", "--model", expected_id, "--thought-level", expected_effort])
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
        custom_table = THREE_FAMILY_TABLE.replace(
            'delegate_tool = "delegate_task"',
            'review = ["agy", "exec", "-C", "{checkout}", "--model", "{id}", "--thought-level", "{thought_level}", '
            '"-o", "{output}"]',
        )
        self.table(custom_table)
        self.landed("custom", "lib/custom.txt", "baz\n")
        self.lane("review", "custom", "--reviewer", "antigravity")
        call = self.antigravity_calls()[-1]
        self.assertEqual(call[2:4], ["exec", "-C"])
        self.assertIn("--model", call)
        self.assertIn("gemini-4-neon", call)
        self.assertIn("--thought-level", call)
        self.assertIn("medium", call)
        self.assertIn("-o", call)
        receipt = self.review("custom")
        self.assertEqual(receipt["status"], "finished")
        self.assertEqual(receipt["provider"], "google")
        self.assertEqual(receipt["options"], {"thought_level": "medium"})

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


if __name__ == "__main__":
    unittest.main()
