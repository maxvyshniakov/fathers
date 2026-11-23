# Orchestration

## Lane tool

- The installed `lanefather`, run in this repository; [.lanefather.toml](../../.lanefather.toml) gives it the project,
  its own Store (`~/.local/state/lanefather/fathers/lanes`) and the verify command `tools/lane-check`.
- Once per clone, before the first lane: `git config core.hooksPath .githooks` in the main checkout activates the
  pre-push gate for it and every lane's worktree.
- Start: `lanefather start <id> --ticket <N> --base integrate/<batch> --claims '<package>/**' --create`; the lane's
  worktree is `.claude/worktrees/lane-<id>` of the main checkout. Start (and `adopt`) print the lane's next commands:
  its check and the hand-back.
- Wait: `lanefather wait <id> --for registered|landed|done [--timeout S]` blocks until the lane is in that state
  (exit 0), or exits 75 with its current state when the timeout (default 3600 s) passes; never a loop over
  `lanefather list`. `lanefather --help` lists each command in one line; `lanefather <command> --help` has its
  options and an example.
- Check: `lanefather check <id>` plans the lane's diff with `tools/lane-check` (each changed package's checks, and
  the root checks) and runs them ([tools/verify.py](../../tools/verify.py)); the receipt is the evidence.
- Land: `lanefather land <id> --into <integration worktree>`, which merges, runs the post-merge smoke and starts the
  review. Then `lanefather done <id>`, and `lanefather gc` for leftovers.
- Without `--force-reason`: a lane with nothing to land goes with `lanefather drop <id>`; a post-merge smoke that went
  red for the environment clears with `lanefather recheck <id>`, or by itself once a later green smoke or gate on
  the integration branch ran its checks
  ([routine cleanups](../../lanefather/docs/reference.md#routine-cleanups-without---force-reason)).
- No `promote`: the batch reaches `main` by the explicit push of [publishing](publishing.md).

## Report labels

- Besides the pack's lines: `Lane: <lanefather id>` and `Ticket comment: <link>`.

## Review

- Every landed lane is reviewed once, blind: Lanefather's land starts it with the `review` role of `models.toml`
  (another family than the implementer). Mark each finding `lanefather review <id> --mark N fixed:<merge>` or
  `rejected:<reason>` after the one fix round. A batch is pushed only with every finding marked.
- One fix lane for the findings of several landed lanes names them: `lanefather start review-fixes-<n> --fix-of A,B,C
  --ticket <N> --claims '<paths>' --create`. Its land is exempt from review like a `review <id> --fix-lane` lane's;
  mark each parent's findings `fixed:review-fixes-<n>`.

## Owner queue

- Questions go to the owner in one numbered message in the orchestrating conversation, each with the default the
  lane implemented; the ticket waiting on an answer gets the `question` label, and the answer is recorded on it.

## Implementer brief

- Work only in the lane's worktree and claims; another package's files belong to its own lane.
- Heavy commands through `slotfather run` with `--ticket` and a label ([checks](checks.md#heavy-checks)).
- Changing a `tools/check.py` means changing all four copies; baselines only shrink.
- A user-visible change adds its package's changelog entry in the same commit.
