# Orchestration

## Lane tool

- The installed `lanefather`, run in this repository; [.lanefather.toml](../../.lanefather.toml) gives it the project,
  its own Store (`~/.local/state/lanefather/fathers/lanes`) and the verify command `tools/lane-check`.
- Once per clone, before the first lane: `git config core.hooksPath .githooks` in the main checkout activates the
  pre-push gate for it and every lane's worktree.
- Start: `lanefather start <id> --ticket <N> --base integrate/<batch> --claims '<package>/**' --create`; the lane's
  worktree is `.claude/worktrees/lane-<id>` of the main checkout. Start (and `adopt`) print the lane's next commands:
  its check and the hand-back.
- Chained tickets: one lane that completes several tickets names each, in order, with `--ticket <N> --ticket <M>`;
  one it does only part of with `--partial <N>`, and the owner's answer or a spec it follows with
  `--decision <comment URL>` or `--spec <path>`. The first completed ticket is the primary one: the implementer's
  evidence goes there. Its merge says `Fixes` for each completed ticket and `Refs` for each partial one, and a
  later answer joins the scope with `lanefather scope <id> --add-decision <URL>`
  ([accepted scope](../../lanefather/docs/reference.md#accepted-scope)).
- Wait: `lanefather wait <id> --for registered|landed|done [--timeout S]` blocks until the lane is in that state
  (exit 0), or exits 75 with its current state when the timeout (default 3600 s) passes; never a loop over
  `lanefather list`. `lanefather --help` lists each command in one line; `lanefather <command> --help` has its
  options and an example.
- Check: `lanefather check <id>` plans the lane's diff with `tools/lane-check` (each changed package's checks, and
  the root checks) and runs them ([tools/verify.py](../../tools/verify.py)); the receipt is the evidence.
- Land: `lanefather land <id> --into <integration worktree>`, which merges and runs the post-merge smoke; with
  `LANEFATHER_REVIEW_START=manual` it does not start a review of its own (see Review). Then `lanefather done <id>`,
  and `lanefather gc` for leftovers.
- Without `--force-reason`: a lane with nothing to land goes with `lanefather drop <id>`; a post-merge smoke that went
  red for the environment clears with `lanefather recheck <id>`, or by itself once a later green smoke or gate on
  the integration branch ran its checks
  ([routine cleanups](../../lanefather/docs/reference.md#routine-cleanups-without---force-reason)).
- No `promote`: the batch reaches `main` by the explicit push of [publishing](publishing.md).

## Report labels

- Besides the pack's lines: `Lane: <lanefather id>` and `Ticket comment: <link>`.

## Review

- Every lane is reviewed once, blind, before it lands, as `orchestrate` step 5 says: the review (the `review` role
  of `models.toml`, another family than the implementer) runs on the committed lane beside `lanefather check`, its
  findings go to the same implementer in the same lane, and the lane lands once. Lanefather cannot run a review
  before the land yet (#242), so land with `LANEFATHER_REVIEW_START=manual` and keep the findings and their
  marks in the review's report file. A batch is pushed only with every finding marked.
- A lane that landed unreviewed is reviewed after the land: `lanefather review <id>`, then
  `lanefather review <id> --mark N fixed:<merge>` or `rejected:<reason>` after the one fix round.
- The review reads the lane's whole accepted scope: every ticket's title and body (never its comments), each
  `--decision` comment and each `--spec` at the lane's base. Register an owner answer as a decision rather than
  copying it into a ticket body, and never register an implementer's report as one.
- One fix lane for the findings of several lanes that landed unreviewed names them: `lanefather start review-fixes-<n> --fix-of A,B,C
  --ticket <N> --claims '<paths>' --create`. Its land is exempt from review like a `review <id> --fix-lane` lane's;
  mark each parent's findings `fixed:review-fixes-<n>`.
- Reviewer rules, from findings past reviews missed:
  - At a JSON or other external boundary, check each value's type (`isinstance(x, str)`; an integer is
    `isinstance(x, int) and not isinstance(x, bool)`) and refuse the wrong one; never coerce it with `str(x)` or
    `int(x)`.
  - A `git` call that prints paths reads them as they are: `-c core.quotePath=false` for `diff --name-only`,
    `--name-status` and `ls-tree` (or `-z`; the `test hygiene` check enforces it), `--no-renames` when a rename must
    count as both paths, `--literal-pathspecs` when a path argument comes from a user or a repository.
  - OS-specific code (`ps`, `/proc`, `stat`, `lsof`, path rules) is read in both its macOS and Linux branches, and
    each branch has a test that runs on macOS too (a fake `/proc`, a stubbed `ps`), since CI runs Linux only.

## Owner queue

- Questions go to the owner in one numbered message in the orchestrating conversation, each with the default the
  lane implemented; the ticket waiting on an answer gets the `question` label, and the answer is recorded on it.

## Implementer brief

- Work only in the lane's worktree and claims; another package's files belong to its own lane.
- Heavy commands through `slotfather run` with `--ticket` and a label ([checks](checks.md#heavy-checks)).
- Changing a `tools/check.py` means changing all four copies; baselines only shrink.
- A user-visible change adds its package's changelog entry in the same commit.
