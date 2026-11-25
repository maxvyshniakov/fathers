# Tokenfather Changelog

Versions: `tokenfather/__init__.py` names the version, and an installed release also names its commit
(`tokenfather --version`).

## Unreleased

### Changed

- `tokenfather --help` now uses the shared compact layout (≤ 80 columns, no wrapped command names).
- Tokenfather runs on the shared `fathers_common` modules (`fathers#208`, `fathers#117`): `tokenfather changelog
  --json` now answers Godfather's changelog contract (`{"v": 1, "tool": "tokenfather", "version", "source",
  "entries"}`, Unreleased entries first with the installed commit; an invalid `--since` exits 2) instead of the raw
  `text`, and `changelog` without `--json` lists the entries per release. The rollup's `cursors.json` and daily files
  are written with `fathers_common.atomic` (the directory is synced too), and the event log with
  `fathers_common.events`.

- `install.py` is built on `fathers_common.installer`: it refuses uncommitted shipping inputs, installs one at a time
  under `share/.install.lock`, stages an immutable release that must match an existing one byte for byte, smoke-checks
  it (`--version`, `changelog --json`) before `share/current` switches to it, and switches `current` back when the
  command cannot be written.

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
