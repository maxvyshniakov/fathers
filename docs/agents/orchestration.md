# Orchestration

This file holds repository-specific facts for `fathers`. General orchestration, review timing, and fix-round rules live
in the `orchestrate` and `cross-review` skills; Lanefather's own commands and flags are in
[lanefather/docs/reference.md](../../lanefather/docs/reference.md).

## Repository setup

- Installed `lanefather`, configured by [.lanefather.toml](../../.lanefather.toml): Store
  `~/.local/state/lanefather/fathers/lanes`, verify command `tools/lane-check` ([tools/verify.py](../../tools/verify.py)).
- Once per clone, before the first lane: `git config core.hooksPath .githooks` activates the pre-push gate for the main
  checkout and every worktree.
- Isolated worktrees: each unit of work runs in its own git worktree (Lanefather lanes live under
  `.claude/worktrees/lane-<id>` of the main checkout).
- No `promote`: a batch reaches `main` only by the explicit push of [publishing](publishing.md).

## Report labels

- Besides the skill's lines: `Lane: <lanefather id>` and `Ticket comment: <link>`.

## Review flow

- Pre-land review runs read-only and blind against a `git archive` scratch copy of the candidate commit, isolated from
  the implementer's working tree and history.
- Delta spot-check rule: 1 review round is the default. Run a short delta spot-check
  (`git diff <reviewed-sha>..<fix-sha>`) only when the fix commit (a) introduces a new mechanism, capability, or
  recovery branch, (b) exceeds ~80 non-test lines or touches >3 production modules, or (c) alters reap/signal,
  slot-admission, or land-refusal safety gates.

## Project reviewer rules

Findings past reviews in `fathers` missed (in addition to [checks](checks.md#review) and each package's own `AGENTS.md`):

- At a JSON or other external boundary, check each value's type (`isinstance(x, str)`; an integer is
  `isinstance(x, int) and not isinstance(x, bool)`) and refuse the wrong one; never coerce it with `str(x)` or `int(x)`.
- A `git` call that prints paths reads them as they are: `-c core.quotePath=false` for `diff --name-only`,
  `--name-status` and `ls-tree` (or `-z`; the `test hygiene` check enforces it), `--no-renames` when a rename must
  count as both paths, `--literal-pathspecs` when a path argument comes from a user or a repository.
- OS-specific code (`ps`, `/proc`, `stat`, `lsof`, path rules) is read in both its macOS and Linux branches, and each
  branch has a test that runs on macOS too (a fake `/proc`, a stubbed `ps`), since CI runs Linux only.

## Implementer brief (fathers-specific)

- Work only in the assigned worktree and claims; another package's files belong to its own worktree/lane.
- Verification scope: single-package worktrees run package lint, `test hygiene`, and focused/package tests, relying on
  `pre-push` for the full gate on `dev` — except changes to `common/` or cross-package contracts, which run full
  `python3 tools/checks.py verify` in the worktree first.
- Heavy commands through `slotfather run` with `--ticket` and a label ([checks](checks.md#heavy-checks)). Do NOT wrap
  `lanefather check`, `lanefather land`, or `lanefather promote` in `slotfather run --`: they lease slots for
  themselves, and an outer 1-slot lease pins the whole run and its verifier to a single slot.
- Changing a `tools/check.py` means changing all five copies; baselines only shrink.
- A user-visible change adds its package's changelog entry in the same commit.
