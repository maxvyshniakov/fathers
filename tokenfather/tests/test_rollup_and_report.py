"""Incremental rollup, deleted-transcript survival, event schema, lane attribution, pricing, and report tests."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))
import eventcheck  # pyright: ignore[reportMissingImports]

from tokenfather.attribution import LaneIndex, load_lanes
from tokenfather.pricing import load_pricing
from tokenfather.report import build_report, format_report
from tokenfather.store import RollupStore


class RollupAndReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tokenfather-rollup-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.state = self.tmp / "state" / "tokenfather"
        self.events = self.tmp / "state" / "events.jsonl"
        self.lanes_dir = self.tmp / "lanes"
        self.lanes_dir.mkdir(parents=True)

    def _seed_all_three_providers(self) -> tuple[Path, Path, Path]:
        # 1. Claude Code main + subagent
        cc_dir = self.home / ".claude" / "projects" / "-Users-test-fathers"
        cc_sub_dir = cc_dir / "cc-sess-1" / "subagents"
        cc_sub_dir.mkdir(parents=True)
        cc_sub = cc_sub_dir / "agent-impl1.jsonl"
        (cc_sub_dir / "agent-impl1.meta.json").write_text(json.dumps({"agentType": "implementer"}))
        cc_sub.write_text(json.dumps({
            "type": "assistant", "sessionId": "cc-sess-1", "cwd": "/Users/test/fathers",
            "timestamp": "2026-11-16T10:00:00.000Z", "effort": "medium",
            "message": {
                "id": "msg_cc_1", "model": "claude-opus-5-5",
                "usage": {
                    "input_tokens": 100, "output_tokens": 1_000,
                    "cache_read_input_tokens": 200_000, "cache_creation_input_tokens": 40_000,
                    "cache_creation": {"ephemeral_5m_input_tokens": 40_000, "ephemeral_1h_input_tokens": 0},
                },
                "content": [{"type": "tool_use", "name": "Edit", "input": {}}],
            },
        }) + "\n")

        # 2. Codex rollout (matched via worktree fallback!)
        wt_path = self.tmp / "worktrees" / "lane-alpha"
        wt_path.mkdir(parents=True)
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "16"
        cx_dir.mkdir(parents=True)
        cx_file = cx_dir / "rollout-2026-11-16T11-00-00-cx1.jsonl"
        cx_file.write_text("\n".join([
            json.dumps({"timestamp": "2026-11-16T11:00:00.000Z", "type": "session_meta",
                        "payload": {"session_id": "cx-sess-1", "cwd": str(wt_path)}}),
            json.dumps({"timestamp": "2026-11-16T11:00:01.000Z", "type": "turn_context",
                        "payload": {"model": "gpt-6.1-sol", "effort": "xhigh"}}),
            json.dumps({"timestamp": "2026-11-16T11:00:05.000Z", "type": "token_usage_record",
                        "payload": {"response_id": "resp_cx_1",
                                    "usage": {"input_tokens": 30_000, "cached_input_tokens": 20_000,
                                              "cache_write_input_tokens": 0, "output_tokens": 500}}}),
        ]) + "\n")

        # 3. Antigravity transcript (matched via fathers#200 agent metadata!)
        ag_dir = (self.home / ".t3" / "userdata" / "providers" / "antigravity" / "hash1"
                  / "antigravity-acp" / "brain" / "ag-conv-1" / ".system_generated" / "logs")
        ag_dir.mkdir(parents=True)
        ag_file = ag_dir / "transcript.jsonl"
        ag_file.write_text(json.dumps({
            "step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
            "created_at": "2026-11-16T12:00:00Z", "model": "gemini-4-neon-medium",
            "input_tokens": 10_000, "cache_read_tokens": 50_000, "output_tokens": 800,
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "\"pytest\""}}],
        }) + "\n")

        # Lane records: lane-beta has explicit fathers#200 agents; lane-alpha relies on worktree fallback.
        (self.lanes_dir / "beta.json").write_text(json.dumps({
            "id": "beta", "status": "landed", "common_dir": "/Users/test/fathers/.git", "worktree": None,
            "agents": [
                {"kind": "claude-code", "session_id": "cc-sess-1", "subagent_id": "agent-impl1",
                 "transcript_path": str(cc_sub)},
                {"kind": "antigravity", "session_id": "ag-conv-1", "transcript_path": str(ag_file)},
            ],
        }))
        (self.lanes_dir / "alpha.json").write_text(json.dumps({
            "id": "alpha", "status": "active", "common_dir": "/Users/test/fathers/.git", "worktree": str(wt_path),
            "agents": [],
        }))
        return cc_sub, cx_file, ag_file

    def test_incremental_collect_survives_deleted_transcripts_and_attributes_all_three_providers(self) -> None:
        cc_sub, cx_file, ag_file = self._seed_all_three_providers()
        store = RollupStore(self.state, events_log=self.events)

        first = store.collect(home=self.home, lane_dirs=[self.lanes_dir])
        self.assertEqual(3, first.files_scanned)
        self.assertEqual(3, first.files_updated)
        self.assertEqual(3, first.new_requests)
        self.assertEqual({"claude-code": 1, "codex": 1, "antigravity": 1}, first.by_provider)

        # Shared fathers event log validates in strict mode.
        problems: list[str] = eventcheck.validate_file(str(self.events), strict=True)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        self.assertEqual([], problems)

        # Second collect with no new lines reads nothing.
        second = store.collect(home=self.home, lane_dirs=[self.lanes_dir])
        self.assertEqual(0, second.files_updated)
        self.assertEqual(0, second.new_requests)

        # Append one new line to Antigravity transcript: only that 1 file and 1 request are read.
        with open(ag_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                "created_at": "2026-11-16T12:01:00Z", "model": "gemini-4-neon-medium",
                "input_tokens": 2_000, "cache_read_tokens": 60_000, "output_tokens": 400,
                "tool_calls": [],
            }) + "\n")
        third = store.collect(home=self.home, lane_dirs=[self.lanes_dir])
        self.assertEqual(1, third.files_updated)
        self.assertEqual(1, third.new_requests)

        # Delete ALL source transcripts: rollup and report still survive intact!
        cc_sub.unlink()
        cx_file.unlink()
        ag_file.unlink()

        lane_index = LaneIndex(load_lanes([self.lanes_dir]))
        records = store.read_records(since="2026-11-16T00:00Z", until="2026-11-17T00:00Z", lane_index=lane_index)
        self.assertEqual(4, len(records))

        pricing = load_pricing(self.tmp / "nonexistent-models.toml")
        self.assertEqual("built-in", pricing.source)
        rep = build_report(records, pricing, lane_index=lane_index)

        self.assertEqual(4, rep["totals"]["requests"])
        self.assertEqual(["antigravity", "claude-code", "codex"], sorted(rep["by_provider"]))
        # Lane beta attributed via fathers#200 agent metadata (Claude Code subagent + 2 Antigravity turns).
        self.assertEqual(3, rep["by_lane"]["beta"]["requests"])
        self.assertEqual(["antigravity", "claude-code"], rep["by_lane"]["beta"]["providers"])
        # Lane alpha attributed via worktree-cwd fallback (Codex).
        self.assertEqual(1, rep["by_lane"]["alpha"]["requests"])
        self.assertEqual(["codex"], rep["by_lane"]["alpha"]["providers"])
        # Landed lane average includes beta (landed) and excludes alpha (active).
        self.assertEqual(1, rep["per_landed_lane"]["count"])
        self.assertEqual(3.0, rep["per_landed_lane"]["avg_turns"])

        text = format_report(rep)
        self.assertIn("antigravity:", text)
        self.assertIn("claude-code:", text)
        self.assertIn("codex:", text)

    def test_pricing_loads_models_toml_weights_and_tiers(self) -> None:
        models_toml = self.tmp / "models.toml"
        models_toml.write_text(
            '[models.opus]\nid = "claude-opus-5-5"\ntier = "strong"\n'
            '[models.sonnet]\nid = "claude-sonnet-5-5"\ntier = "routine"\n'
            '[pricing.default]\ninput = 1.0\noutput = 5.0\ncache_read = 0.1\n'
            'cache_write_short = 1.25\ncache_write_long = 2.0\n'
        )
        pricing = load_pricing(models_toml)
        self.assertEqual(str(models_toml), pricing.source)
        self.assertEqual(1.0, pricing.for_model("claude-opus-5-5").tier_multiplier)
        self.assertEqual(0.2, pricing.for_model("claude-sonnet-5-5").tier_multiplier)


if __name__ == "__main__":
    unittest.main()
