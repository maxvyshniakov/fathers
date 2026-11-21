"""Provider adapter tests with fixtures for Claude Code, Codex, and Antigravity / T3 Code (fathers#199)."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tokenfather.adapters.antigravity import AntigravityAdapter
from tokenfather.adapters.claude_code import ClaudeCodeAdapter
from tokenfather.adapters.codex import CodexAdapter


class AdapterFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tokenfather-adapters-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_claude_code_dedup_ttl_split_cache_miss_and_turn_patterns(self) -> None:
        sub_dir = self.tmp / ".claude" / "projects" / "-Users-test-proj" / "sess-1" / "subagents"
        sub_dir.mkdir(parents=True)
        transcript = sub_dir / "agent-sub1.jsonl"
        (sub_dir / "agent-sub1.meta.json").write_text(json.dumps({
            "agentType": "Explore",
            "pickedEffortModel": "claude-opus-5-5",
        }))

        # Message 1 split across two content blocks (thinking + tool_use): must count ONCE and collect tools.
        m1_usage = {
            "input_tokens": 100,
            "output_tokens": 400,
            "cache_read_input_tokens": 10_000,
            "cache_creation_input_tokens": 40_000,
            "cache_creation": {"ephemeral_5m_input_tokens": 40_000, "ephemeral_1h_input_tokens": 0},
            "output_tokens_details": {"thinking_tokens": 150},
        }
        # Message 2 after 400s (> 5m TTL): rewrites 45K of 50K context -> cache_miss with reason wake_after_ttl.
        m2_usage = {
            "input_tokens": 50,
            "output_tokens": 200,
            "cache_read_input_tokens": 4_950,
            "cache_creation_input_tokens": 45_000,
            "cache_creation": {"ephemeral_5m_input_tokens": 45_000, "ephemeral_1h_input_tokens": 0},
        }
        # Message 3: waiting turn (Bash with sleep) + model switch causing cache miss.
        m3_usage = {
            "input_tokens": 20,
            "output_tokens": 100,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 50_000,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 50_000},
        }
        lines = [
            json.dumps({
                "type": "assistant", "sessionId": "sess-1", "cwd": "/Users/test/proj",
                "timestamp": "2026-11-16T10:00:00.000Z", "effort": "medium",
                "message": {"id": "msg_1", "model": "claude-opus-5-5", "usage": m1_usage,
                            "content": [{"type": "thinking", "thinking": "..."}]},
            }),
            json.dumps({
                "type": "assistant", "sessionId": "sess-1", "cwd": "/Users/test/proj",
                "timestamp": "2026-11-16T10:00:01.000Z", "effort": "medium",
                "message": {"id": "msg_1", "model": "claude-opus-5-5", "usage": m1_usage,
                            "content": [{"type": "tool_use", "name": "Bash",
                                         "input": {"command": "sed -n '1,20p' a.py"}}]},
            }),
            json.dumps({
                "type": "assistant", "sessionId": "sess-1", "cwd": "/Users/test/proj",
                "timestamp": "2026-11-16T10:06:41.000Z", "effort": "medium",
                "message": {"id": "msg_2", "model": "claude-opus-5-5", "usage": m2_usage,
                            "content": [{"type": "tool_use", "name": "Monitor", "input": {}}]},
            }),
            json.dumps({
                "type": "assistant", "sessionId": "sess-1", "cwd": "/Users/test/proj",
                "timestamp": "2026-11-16T10:07:00.000Z", "effort": "high",
                "message": {"id": "msg_3", "model": "claude-sonnet-5-5", "usage": m3_usage,
                            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 5"}}]},
            }),
        ]
        transcript.write_text("\n".join(lines) + "\n")

        adapter = ClaudeCodeAdapter()
        self.assertEqual([transcript], adapter.discover(self.tmp))
        cursor: dict[str, object] = {}
        recs = adapter.parse_lines(transcript, lines, cursor, "testhost")
        self.assertEqual(3, len(recs))

        # First request: deduped, small-read turn, not a cache miss, no Anthropic-specific keys in to_dict().
        r1 = recs[0]
        self.assertEqual("msg_1", r1.request_id)
        self.assertEqual("subagent", r1.agent_role)
        self.assertEqual("Explore", r1.agent_type)
        self.assertEqual({"short": 40_000}, r1.cache_write_ttl_buckets)
        self.assertEqual(150, r1.reasoning_tokens)
        self.assertTrue(r1.is_small_read_turn)
        self.assertFalse(r1.is_wait_turn)
        self.assertFalse(r1.cache_miss)
        serialized = json.dumps(r1.to_dict())
        self.assertNotIn("ephemeral_5m", serialized)
        self.assertNotIn("ephemeral_1h", serialized)

        # Second request: wake after 5m TTL -> cache miss + waiting turn (Monitor).
        r2 = recs[1]
        self.assertTrue(r2.cache_miss)
        self.assertEqual("wake_after_ttl", r2.cache_miss_reason)
        self.assertTrue(r2.is_wait_turn)
        self.assertAlmostEqual(401.0, r2.gap_seconds or 0.0, places=1)

        # Third request: model switch -> cache miss with reason model_switch, long TTL bucket.
        r3 = recs[2]
        self.assertTrue(r3.cache_miss)
        self.assertEqual("model_switch", r3.cache_miss_reason)
        self.assertEqual({"long": 50_000}, r3.cache_write_ttl_buckets)
        self.assertTrue(r3.is_wait_turn)

    def test_codex_reads_token_usage_record_without_double_counting_token_count(self) -> None:
        codex_dir = self.tmp / ".codex" / "sessions" / "2026" / "11" / "17"
        codex_dir.mkdir(parents=True)
        rollout = codex_dir / "rollout-2026-11-17T15-00-00-sess-cx1.jsonl"
        lines = [
            json.dumps({"timestamp": "2026-11-17T15:00:00.000Z", "type": "session_meta",
                        "payload": {"session_id": "sess-cx1", "cwd": "/Users/test/data-grid"}}),
            json.dumps({"timestamp": "2026-11-17T15:00:01.000Z", "type": "turn_context",
                        "payload": {"cwd": "/Users/test/data-grid", "model": "gpt-6.1-sol",
                                    "collaboration_mode": {"settings": {"reasoning_effort": "xhigh"}}}}),
            json.dumps({"timestamp": "2026-11-17T15:00:02.000Z", "type": "response_item",
                        "payload": {"type": "function_call", "name": "shell", "arguments": "rg foo src/"}}),
            json.dumps({"timestamp": "2026-11-17T15:00:05.000Z", "type": "token_usage_record",
                        "payload": {"response_id": "resp_1",
                                    "usage": {"input_tokens": 25_000, "cached_input_tokens": 15_000,
                                              "cache_write_input_tokens": 0, "output_tokens": 300,
                                              "reasoning_output_tokens": 80, "total_tokens": 25_300}}}),
            # Duplicate event_msg:token_count for the same request must NOT double count.
            json.dumps({"timestamp": "2026-11-17T15:00:05.100Z", "type": "event_msg",
                        "payload": {"type": "token_count",
                                    "info": {"total_token_usage": {"total_tokens": 25_300},
                                             "last_token_usage": {"input_tokens": 25_000, "cached_input_tokens": 15_000,
                                                                  "output_tokens": 300}}}}),
        ]
        rollout.write_text("\n".join(lines) + "\n")

        adapter = CodexAdapter()
        self.assertEqual([rollout], adapter.discover(self.tmp))
        recs = adapter.parse_lines(rollout, lines, {}, "testhost")
        self.assertEqual(1, len(recs))
        r = recs[0]
        self.assertEqual("codex", r.provider)
        self.assertEqual("sess-cx1", r.session_id)
        self.assertEqual("gpt-6.1-sol", r.model)
        self.assertEqual("xhigh", r.effort)
        self.assertEqual(10_000, r.input_tokens)  # 25000 - 15000
        self.assertEqual(15_000, r.cache_read_tokens)
        self.assertEqual(300, r.output_tokens)
        self.assertEqual(80, r.reasoning_tokens)
        self.assertEqual(25_000, r.context_tokens)
        self.assertTrue(r.is_small_read_turn)

    def test_codex_fallback_to_event_msg_token_count_dedups_repeated_totals(self) -> None:
        codex_dir = self.tmp / ".codex" / "sessions" / "2026" / "11" / "16"
        codex_dir.mkdir(parents=True)
        rollout = codex_dir / "rollout-legacy.jsonl"
        lines = [
            json.dumps({"timestamp": "2026-11-16T12:00:00.000Z", "type": "session_meta",
                        "payload": {"id": "sess-legacy", "cwd": "/Users/test/repo"}}),
            json.dumps({"timestamp": "2026-11-16T12:00:01.000Z", "type": "turn_context",
                        "payload": {"model": "gpt-6-astra", "effort": "medium"}}),
            json.dumps({"timestamp": "2026-11-16T12:00:05.000Z", "type": "event_msg",
                        "payload": {"type": "token_count",
                                    "info": {"total_token_usage": {"total_tokens": 12_500},
                                             "last_token_usage": {"input_tokens": 12_000, "cached_input_tokens": 8_000,
                                                                  "output_tokens": 500}}}}),
            # Same total_tokens snapshot emitted again: ignore.
            json.dumps({"timestamp": "2026-11-16T12:00:06.000Z", "type": "event_msg",
                        "payload": {"type": "token_count",
                                    "info": {"total_token_usage": {"total_tokens": 12_500},
                                             "last_token_usage": {"input_tokens": 12_000, "cached_input_tokens": 8_000,
                                                                  "output_tokens": 500}}}}),
        ]
        rollout.write_text("\n".join(lines) + "\n")
        recs = CodexAdapter().parse_lines(rollout, lines, {}, "testhost")
        self.assertEqual(1, len(recs))
        self.assertEqual(4_000, recs[0].input_tokens)
        self.assertEqual(8_000, recs[0].cache_read_tokens)

    def test_antigravity_adapter_parses_planner_responses_and_workspace(self) -> None:
        logs_dir = (self.tmp / ".t3" / "userdata" / "providers" / "antigravity" / "abc123" / "antigravity-acp"
                    / "brain" / "conv-999" / ".system_generated" / "logs")
        logs_dir.mkdir(parents=True)
        transcript = logs_dir / "transcript.jsonl"
        lines = [
            json.dumps({
                "step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
                "created_at": "2026-11-21T04:18:16Z",
                "content": "<user_information>\nCommand Working Directory: /Users/test/fathers\n</user_information>",
            }),
            json.dumps({
                "step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                "created_at": "2026-11-21T04:18:17Z", "model": "gemini-4-neon-medium",
                "input_tokens": 35_000, "cache_read_tokens": 0, "output_tokens": 700,
                "tool_calls": [{"name": "read_file", "args": {"Path": "/Users/test/fathers/README.md"}}],
            }),
            json.dumps({
                "step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                "created_at": "2026-11-21T04:18:32Z", "model": "gemini-4-neon-medium",
                "input_tokens": 5_000, "cache_read_tokens": 35_000, "output_tokens": 600,
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "\"python3 -m unittest\""}}],
            }),
        ]
        transcript.write_text("\n".join(lines) + "\n")

        adapter = AntigravityAdapter()
        self.assertEqual([transcript], adapter.discover(self.tmp))
        recs = adapter.parse_lines(transcript, lines, {}, "testhost")
        self.assertEqual(2, len(recs))
        self.assertEqual("antigravity", recs[0].provider)
        self.assertEqual("conv-999", recs[0].session_id)
        self.assertEqual("/Users/test/fathers", recs[0].project)
        self.assertEqual("gemini-4-neon-medium", recs[0].model)
        self.assertEqual("medium", recs[0].effort)
        self.assertEqual(35_000, recs[0].context_tokens)
        self.assertTrue(recs[0].is_small_read_turn)

        self.assertEqual(5_000, recs[1].input_tokens)
        self.assertEqual(35_000, recs[1].cache_read_tokens)
        self.assertEqual(40_000, recs[1].context_tokens)
        self.assertAlmostEqual(15.0, recs[1].gap_seconds or 0.0, places=1)


if __name__ == "__main__":
    unittest.main()
