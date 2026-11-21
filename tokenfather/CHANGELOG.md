# Tokenfather Changelog

Versions: `tokenfather/__init__.py` names the version, and an installed release also names its commit
(`tokenfather --version`).

## Unreleased

## 0.1.0 - 2026-11-21

### Added

- Multi-provider agent token usage collector and reporter (`fathers#199`): provider adapters for Claude Code
  (`~/.claude/projects/*/*.jsonl` and `*/subagents/*.jsonl` + `.meta.json`), Codex
  (`~/.codex/sessions/**/rollout-*.jsonl`), and Antigravity / T3 Code
  (`~/.t3/userdata/providers/antigravity/**/brain/*/.system_generated/logs/transcript.jsonl`), normalizing token and
  cache accounting into a provider-agnostic schema.

- Incremental daily rollup under `~/.local/state/tokenfather/` (`cursors.json` + `daily/YYYY-MM-DD.jsonl`) that reads
  only new transcript lines on subsequent runs and survives deleted transcripts, plus schema-v1 summary events in the
  shared fathers event ledger.

- Lane attribution joining requests to Lanefather lanes via recorded agent metadata (`fathers#200`) with worktree-cwd
  fallback, and `tokenfather report [--since D] [--until D] [--project P] [--json]` reading price weights from
  `mastack`'s `models.toml` when present else built-in defaults.
