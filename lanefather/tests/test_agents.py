"""Multi-provider agent attribution on lanes (fathers#200)."""
from __future__ import annotations

import json
import unittest

from support import LaneCase


class AgentAttributionTests(LaneCase):
    def test_lane_worked_by_multiple_agents_across_providers_is_exposed_in_show_json(self) -> None:
        # 1. Coordinator (Claude Code) starts the lane.
        self.env.update({
            "CLAUDE_CODE_SESSION_ID": "claude-coord-1",
            "CLAUDE_MODEL": "claude-opus-5-5",
            "CLAUDE_CODE_EFFORT_LEVEL": "high",
        })
        self.start("multi", "lib/**")

        # 2. Claude Code subagent creates a worktree and adopts the lane.
        worktree = self.tmp / "wt-multi"
        self.git(self.repo, "worktree", "add", "-q", "-b", "lane/multi", str(worktree), "integrate/q")
        self.env.update({
            "CLAUDE_CODE_SESSION_ID": "claude-coord-1",
            "CLAUDE_CODE_SUBAGENT_ID": "agent-sub-01",
            "CLAUDE_CODE_TRANSCRIPT_PATH": "/tmp/claude/subagents/agent-sub-01.jsonl",
            "CLAUDE_MODEL": "claude-sonnet-5-5",
            "CLAUDE_CODE_EFFORT_LEVEL": "medium",
        })
        self.lane("adopt", "multi", str(worktree), "lane/multi")

        # 3. Codex implementer commits and runs the first check.
        self.commit(worktree, {"lib/api.txt": "foo\nmulti\n"})
        for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SUBAGENT_ID", "CLAUDE_CODE_TRANSCRIPT_PATH",
                    "CLAUDE_MODEL", "CLAUDE_CODE_EFFORT_LEVEL"):
            self.env.pop(key, None)
        self.env.update({
            "CODEX_THREAD_ID": "codex-thread-99",
            "CODEX_TRANSCRIPT_PATH": "/tmp/codex/rollout-99.jsonl",
            "CODEX_MODEL": "gpt-6.1-sol",
            "CODEX_REASONING_EFFORT": "xhigh",
        })
        self.lane("check", "multi")

        # 4. Antigravity / T3 Code agent runs check on the already-green tree (cached receipt path).
        for key in ("CODEX_THREAD_ID", "CODEX_TRANSCRIPT_PATH", "CODEX_MODEL", "CODEX_REASONING_EFFORT"):
            self.env.pop(key, None)
        self.env.update({
            "ANTIGRAVITY_CONVERSATION_ID": "ag-conv-42",
            "T3_THREAD_ID": "t3-thread-7",
            "ANTIGRAVITY_APP_DATA_DIR": "/tmp/t3-ag",
            "ANTIGRAVITY_MODEL": "gemini-4-neon-medium",
            "ANTIGRAVITY_THOUGHT_LEVEL": "medium",
        })
        output = self.lane("check", "multi")
        self.assertIn("Receipt current", output)

        shown = json.loads(self.lane("show", "multi", "--json"))
        agents = shown["agents"]
        self.assertEqual(4, len(agents), agents)

        self.assertEqual("claude-code", agents[0]["kind"])
        self.assertEqual("claude-coord-1", agents[0]["session_id"])
        self.assertEqual("claude-opus-5-5", agents[0]["model"])
        self.assertEqual({"effort": "high"}, agents[0]["options"])
        self.assertEqual(["start"], agents[0]["events"])

        self.assertEqual("claude-code", agents[1]["kind"])
        self.assertEqual("claude-coord-1", agents[1]["session_id"])
        self.assertEqual("agent-sub-01", agents[1]["subagent_id"])
        self.assertEqual("/tmp/claude/subagents/agent-sub-01.jsonl", agents[1]["transcript_path"])
        self.assertEqual(["adopt"], agents[1]["events"])

        self.assertEqual("codex", agents[2]["kind"])
        self.assertEqual("codex-thread-99", agents[2]["session_id"])
        self.assertEqual("/tmp/codex/rollout-99.jsonl", agents[2]["transcript_path"])
        self.assertEqual("gpt-6.1-sol", agents[2]["model"])
        self.assertEqual({"effort": "xhigh"}, agents[2]["options"])
        self.assertEqual(["check"], agents[2]["events"])

        self.assertEqual("antigravity", agents[3]["kind"])
        self.assertEqual("ag-conv-42", agents[3]["session_id"])
        self.assertEqual("t3-thread-7", agents[3]["thread_id"])
        self.assertEqual("/tmp/t3-ag/brain/ag-conv-42/.system_generated/logs/transcript.jsonl",
                         agents[3]["transcript_path"])
        self.assertEqual("gemini-4-neon-medium", agents[3]["model"])
        self.assertEqual({"thought_level": "medium"}, agents[3]["options"])
        self.assertEqual(["check"], agents[3]["events"])

    def test_same_agent_across_start_adopt_and_check_merges_events_and_custom_kind_works(self) -> None:
        self.env.update({
            "LANEFATHER_AGENT_SESSION": "custom-sess-1",
            "LANEFATHER_AGENT_OPTIONS": json.dumps({"tier": "fast"}),
        })
        self.start("single", "lib/**", extra=("--agent", "custom-harness"))
        worktree = self.tmp / "wt-single"
        self.git(self.repo, "worktree", "add", "-q", "-b", "lane/single", str(worktree), "integrate/q")
        self.env["LANEFATHER_AGENT_MODEL"] = "custom-model-v1"
        self.lane("adopt", "single", str(worktree), "lane/single", "--agent", "custom-harness")
        self.commit(worktree, {"lib/api.txt": "foo\nsingle\n"})
        self.lane("check", "single", "--agent", "custom-harness")

        shown = json.loads(self.lane("show", "single", "--json"))
        self.assertEqual(1, len(shown["agents"]))
        entry = shown["agents"][0]
        self.assertEqual("custom-harness", entry["kind"])
        self.assertEqual("custom-sess-1", entry["session_id"])
        self.assertEqual("custom-model-v1", entry["model"])
        self.assertEqual({"tier": "fast"}, entry["options"])
        self.assertEqual(["start", "adopt", "check"], entry["events"])


class AgentContextUnitTests(unittest.TestCase):
    def test_explicit_agent_kind_does_not_leak_other_provider_env_vars(self) -> None:
        from lanefather import events
        env = {
            "CLAUDE_CODE_SESSION_ID": "claude-sess-leak",
            "CLAUDE_CODE_SUBAGENT_ID": "claude-sub-leak",
            "CLAUDE_CODE_TRANSCRIPT_PATH": "/tmp/claude-leak.jsonl",
            "CLAUDE_MODEL": "claude-opus-5-5",
            "CLAUDE_CODE_EFFORT_LEVEL": "high",
            "CODEX_THREAD_ID": "codex-sess-real",
            "CODEX_MODEL": "gpt-6.1-sol",
        }
        ctx = events.agent_context("codex", env=env)
        assert ctx is not None
        self.assertEqual("codex", ctx["kind"])
        self.assertEqual("codex-sess-real", ctx["session_id"])
        self.assertEqual("gpt-6.1-sol", ctx["model"])
        self.assertNotIn("subagent_id", ctx)
        self.assertNotIn("transcript_path", ctx)
        self.assertNotIn("options", ctx)

        # Custom kind does not inherit Claude's session or model when LANEFATHER_AGENT_* is unset.
        ctx_custom = events.agent_context("custom-agent", env={"CLAUDE_CODE_SESSION_ID": "claude-only",
                                                               "CLAUDE_MODEL": "claude-opus-5-5"})
        assert ctx_custom is not None
        self.assertEqual({"kind": "custom-agent"}, ctx_custom)


if __name__ == "__main__":
    unittest.main()
