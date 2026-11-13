# Orchestration

## Lane tool

- The installed `lanefather`, run in this repository; [.lanefather.toml](../../.lanefather.toml) gives it the project,
  its own Store (`~/.local/state/lanefather/fathers/lanes`) and the verify command `tools/lane-check`.
- Once per machine, before the first lane: `bun install` in the main checkout (the lane checks use its Repo-Kit).
  It, and every lane check's own frozen install, also activates the pre-push gate (`core.hooksPath`).
- Start: `lanefather start <id> --ticket <N> --base integrate/<batch> --claims '<package>/**' --create`; the lane's
  worktree is `.claude/worktrees/lane-<id>` of the main checkout.
- Check: `lanefather check <id>` plans the lane's diff with `tools/lane-check` (each changed package's checks, and
  the root checks) and runs them through Repo-Kit's lane adapter; the receipt is the evidence.
- Land: `lanefather land <id> --into <integration worktree>`, which merges, runs the post-merge smoke and starts the
  review. Then `lanefather done <id>`, and `lanefather gc` for leftovers.
- No `promote`: the batch reaches `main` by the explicit push of [publishing](publishing.md).

## Report labels

- Besides the pack's lines: `Lane: <lanefather id>` and `Ticket comment: <link>`.

## Review

- Every landed lane is reviewed once, blind: Lanefather's land starts it with the `review` role of `models.toml`
  (another family than the implementer). Mark each finding `lanefather review <id> --mark N fixed:<merge>` or
  `rejected:<reason>` after the one fix round. A batch is pushed only with every finding marked.

## Owner queue

- Questions go to the owner in one numbered message in the orchestrating conversation, each with the default the
  lane implemented; the ticket waiting on an answer gets the `question` label, and the answer is recorded on it.

## Implementer brief

- Work only in the lane's worktree and claims; another package's files belong to its own lane.
- Heavy commands through `slotfather run` with `--ticket` and a label ([checks](checks.md#heavy-checks)).
- Changing a `tools/check.py` means changing all four copies; baselines and `test-hygiene-baseline.json` only shrink.
- A user-visible change adds its package's changelog entry in the same commit.
