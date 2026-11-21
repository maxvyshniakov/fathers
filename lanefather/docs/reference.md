# Lanefather reference

[Overview, requirements and quick start](../README.md)

## Commands

```
lanefather start <id> --ticket N --claims GLOB... [--base REF] [--after ID...] [--fix-of ID,ID] [--create]
lanefather next [--start]                 the first ready entry of the ranked queue
lanefather adopt <id> <worktree> <branch> register the worktree an agent created
lanefather check <id>                     the checks the plan selects for the lane's diff -> receipt
lanefather wait <id> --for registered|landed|done [--timeout S] [--json]   block until the lane is there
lanefather land <id> [--into WORKTREE] [--keep]
lanefather review <id> [--mark N fixed:<merge>|rejected:<reason>] [--fix-lane]
lanefather review --overall <sha> --since <base>
lanefather done <id>
lanefather done --finished [--dry-run]    done on every finished lane of the registered projects
lanefather drop <id>                      remove an active lane that adds nothing to its base
lanefather recheck <id> [--slots N]       rerun a red post-merge smoke on the integration tip
lanefather keep <id> [--off]              auto-done and done --finished leave the lane
lanefather gc [--dry-run] [--json]         merged idle worktrees and unused branches
lanefather promote <sha> [--ship] [--no-push] [--install [--deploy HOST]]
lanefather sync [--drop-local]
lanefather perf <sha>
lanefather shipped <version> --since REF [--head REF]    comment a published version on the issues it fixes
lanefather list [--all] [--json]
lanefather show <id> [--state-dir DIR] [--project-dir DIR] [--json]   read-only lane context
lanefather status [--json]                every registered project's lanes on this machine, read-only
lanefather settings [check KEY VALUE | set KEY VALUE [--revision R] | reset KEY|--all] [--json]
```

`lanefather --help` lists each command in one line; `lanefather <command> --help` gives its options and an
example, and a misused command prints its usage and that example in the line after the error (fathers#175). `start`
and `adopt` end with the lane's next commands: its check, the hand-back with the receipt path, and the wait for its
land.

`wait` replaces a loop over `list` for an agent that waits for its coordinator: it reads the lane's record with a
growing pause (0.2 s up to 5 s) and exits 0 once the lane is registered (planned or later), landed (landed, released,
or done after landing) or done; 75 with the current state on stderr when `--timeout` seconds (default 3600; 0 answers
at once) pass first; 3 when the state cannot come (a lane done without landing never lands). A lane already done when
the wait begins is an earlier lane of that id, so `--for registered` waits for a new one. `--json` prints
`{"v": 1, "lane", "for", "status", "reached", "waited", "merge"}`.

A claim is a glob relative to the repository: `*` and `?` stay within a path segment and `**` spans segments. A
claim without wildcards names a directory, and everything under it, when it ends in `/`, when HEAD has a directory
or a submodule there, or, for a path HEAD does not have, when its last segment has no extension; otherwise it names
that one file. HEAD is that of the checkout holding the project's `.lanefather.toml` (`--config`), else of the
working directory.

check plans the lane's diff, and widens its claims, against the newest merge-base of the lane's HEAD: with its
base, or with the tip of the branch it lands into (the branch of its integration worktree) when that merge-base
descends from the former. A lane registered at a literal commit, whose landing branch is unknown until it lands,
also counts its parent's landed branch for a fix lane and the ranked queue's `base`; so when it merged the
integration branch it is planned from that merge, not from everything the integration gained since. A lane based
on a branch is never planned against another branch it merged.

A red check reruns by itself on the lane and, when it is red again, then on its base (never both at once: two
copies of a check with a fixed port or path collide), each under a lease of the lane check's own size (one or two
slots) at the rerun's [slot priority](#slot-priorities) (interactive by default) that waits at most
`LANEFATHER_RERUN_WAIT` seconds; only the perf suite asks for the whole machine.

`review <id> --fix-lane` starts the fix lane from the branch the lane landed on, which holds its merge, and refuses
when no branch does; the landed lane keeps its own registration.

A fix lane is exempt from review at land (`review.exempted`): the one `review <id> --fix-lane` registers for one
lane, and a batch fix lane started with `start <id> --fix-of A,B,C` for the findings of one or more landed lanes
(fathers#174). Each parent needs a finished review with at least one unmarked finding. The record keeps the first
parent's ID in the string `review_fix_of` for older Lanefather versions. The complete batch is in
`review_fix_of_all`. Mark each parent's findings with the fix lane's merge (`review A --mark N fixed:<fix lane id>`).
A lane started with `--fix-of` has its own worktree, counts toward the cap like any lane, and runs its own check
plan even with one parent. `start` warns when an id that reads `review-fixes` names no parent.

### Review findings before landing

`land` refuses if another lane in the same repository has a finished review with an unmarked P1 or P2 finding
(fathers#177). The refusal gives the exact commands to mark each finding and shows how to start the fix round.
Unmarked P3 findings are printed as warnings. A running or missing review can finish alongside later lands.
A fix lane may land while its valid parents have unmarked findings. A finished review of an unrelated lane can
still hold the merge. When several landed reviews need a fix, the refusal suggests one batch with `--fix-of`.
`--force-reason` logs the reason and the blocking findings in the override record.
The text output from `list` ends with the count of finished reviews with unmarked findings and their
`lane#number` references. The separate batch gate uses its configured `[review] blocks` severities.

### Finishing lanes

A lane is finished when it is landed (or released), its post-merge smoke is green, its review is finished or exempt
with every finding marked (each panel member's), and it is not kept. Auto-done (fathers#157) runs `done` on a lane
the moment it becomes finished: at the land of a reviewed or exempt lane, at its clean review, or at `review --mark`
of its last unmarked finding. `[done] auto = false` turns that off; the lane then says it is finished and waits for
`done`.

- `lanefather keep ID` keeps a lane, landed or not, from auto-done, `done --finished` and start's disk repair, for a
  lane whose worktree or scratch is still in use (a packed dependency other worktrees overlay); `keep ID --off`
  releases it. `land --keep` keeps the lane too. `done ID` still finishes a kept lane.
- `lanefather done --finished` runs `done` on every finished lane of the projects in the machine's registry, each
  in its own state directory and configuration, and prints what each removed and freed and why done refused the
  others. `--dry-run` lists the finished lanes with their sizes (worktree, build directories and scratch), and the
  landed lanes that are not finished with the reason, and removes nothing.
- `start` (and `next --start`, and `review --fix-lane`, which registers through it) whose only refusal is the
  free-disk floor first runs `done --finished` over the registry, leaving the projects whose `[done] auto` is false,
  then decides again. An existing lane, an unknown `--after`, the cap or a claim overlap refuses first,
  without the repair. The repair runs at most once in 10 minutes on the machine: a start within that window reuses
  the last result (`disk-repair.json` beside the project registry), and a second start waits for a running one. If
  the disk is still short, the refusal says what the repair freed. A failing repair never fails start.
  `--force-reason` skips it.

High load prints an advisory on successful registration. The project's checks wait for Slotfather capacity where
the verifier uses execution leases. `start --create` runs `git worktree add`. The project's check adapter handles
dependency installation and builds. `capabilities --json` advertises `lane.load_advisory` (fathers#172).

The free-disk floor (fathers#157) is `40 GB reserve + lane cap x lane peak`, in decimal GB (10^9); the lane peak is
the largest lane `done` freed among the 20 last done in the state directory (3 GB before any). Refusals give the
derivation, for example `the floor of 70 GB (10^9) = 40 reserve + 10 x 3.0 GB lane peak`. The [machine
setting](#machine-settings) `host.min_disk_gb` sets the floor instead (fathers#163), and `LANEFATHER_MIN_DISK_GB` (or
`<legacy_env_prefix>MIN_DISK_GB`) above it; either takes `auto` (or `0`) for the derived floor. An invalid variable
warns and the setting or the derived floor applies. A refusal names what set the floor, for example `the floor of 150
GB (10^9; lanefather settings host.min_disk_gb)`.

None of these skips a refusal of `done`: an uncommitted change, an agent's lock, a process the source does not take
for left behind or cannot see in the worktree, the build directories or the scratch, a branch or a worktree HEAD
with commits not in the integration branch, a missing integration worktree, another lane that is not done working in
the same worktree (a fix lane), or a missing ticket comment leaves the lane where it is. Unattended (auto-done,
`done --finished`, the repair), `done` also refuses without a process source that answers (a Slotfather with
`procs`, or `[hooks] process_scan`) and re-reads `keep` under the state lock; `done ID` without a source removes the
lane as before. A lane or project that cannot be read or finished is reported as refused, and the others go on.

### Routine cleanups without --force-reason

`--force-reason` stays for the exceptional cases; these two cleanups need none (fathers#167).

- `lanefather drop ID` removes a planned or active lane that adds nothing to its base: its branch (and its
  worktree's HEAD) has no commits beyond the base, its tree is the base's (the integration made the change
  already), or its commits since its merge-base with the base change nothing. Its worktree must have no
  uncommitted or untracked changes (ignored build output does not count). It cleans up as `done` does, with the
  same refusals for an agent's lock, a process in the worktree, the build directories or the scratch, and a fix
  lane in the same worktree. The record becomes `done` with `dropped` (`why`, `tip`, `base`) and the ledger names
  why (`lane.dropped`). A lane with work refuses, with no flag to override it: land it, or revert its commits on
  the lane first.
- A landed lane whose post-merge smoke went red for a reason outside it (the smoke checkout's environment) is cleared
  by a later green run on the same integration branch, on a commit that holds the lane's merge and that the
  integration worktree's HEAD holds: the batch gate there (its `promote-<sha>.gate.json`, the full gate), or another
  lane's green post-merge smoke that ran each check the red smoke ran on its own tree (a result it took from another
  tree, such as the lane's receipt or the previous tip, does not count). The run must not have finished before the red
  smoke (the stamps' one-second resolution: within its second counts) and ran on its platform (a gate's platform comes
  from its receipt, or for an older receipt from its results-store entry). `done` and `done --finished` look for that
  evidence themselves, nothing runs, and the oldest receipt that clears the smoke is named in the ledger (`lane.done`,
  details `post_merge_cleared_by`) and in the record (`post_merge.cleared_by`).
- `lanefather recheck ID` reruns the checks the red smoke ran on the integration tip, in a smoke checkout, when the
  tip holds the merge. Green, its receipt `<id>.recheck.json` replaces the red result in the record (the red
  receipt stays), the lane's issue is marked as after a green land, and the lane is finished as land would have
  finished it; red, the lane stays red (`lane.rechecked`, error when red). Rechecks of one lane run one at a
  time: a second waits, then finds the lane green and has nothing to recheck.

`lanefather --help` describes the commands. The [glossary](../GLOSSARY.md) distinguishes lane states, receipts,
reviews and verification gates.

## Project configuration

lanefather reads `.lanefather.toml` at the top of the repository it runs in (or `--config PATH`,
`LANEFATHER_CONFIG`). The defaults suit an existing verification-contract project; see `lanefather/config.py` for the full list and the
self-contained configuration in the README for a Repo-Kit consumer.

| Key | What it names |
| --- | --- |
| `command` | how messages name the command, for a project that wraps it (for example `tools/lane`) |
| `state_dir` | the lane records, receipts and ledger |
| `legacy_env_prefix`, `[env]` | the variables older tools set, read after `LANEFATHER_*` |
| `rules_dir`, `coding_standards`, `steering`, `report_doc` | rule/standards inputs and the report-link text |
| `unclaimed`, `merge_config` | paths no lane claims; `git -c` settings for land's merge |
| `[checks]` `verify`, `plan_module`, `triage`, `toolchain`, `slot_env` | the check contract below |
| `[review]` `blocks` | the unmarked review finding severities that hold the batch gate, promote and `findings` (default `["P1", "P2"]`; fathers#109) |
| `[review]` `panel`, `panel_required` | the review panel (fathers#151): two or more model aliases of the models file that review every lane at once; `panel_required = true` holds the gate while a member gave no answer ([Reviewer configuration](#reviewer-configuration)) |
| `[checks]` `gate_only` | check-id patterns (fnmatch) the post-merge smoke leaves to the batch gate, such as tooling self-tests (fathers#94); a tree whose plan names whole suites has no check ids to match, so its smoke runs them |
| `[lane]` `targets`, `cache_partitions` | build directories `done` removes |
| `[gate]` `stamp_tool`, `stamp_file`, `pre_push_hook` | the verified-tree stamp of the full gate |
| `[promote]` `install`, `deploy`, `after`, `ship_refuses` | what promote runs after it moved dev |
| `[gc]` `dirs`, `branches`, `collect_uncommitted_sessions`, `disposable` | what `gc` collects (fnmatch patterns, fathers#44). A worktree under the main checkout's `.claude/worktrees` is a candidate when a `dirs` pattern matches its name (a pattern without `/`) or its path from the main checkout (`.claude/worktrees/*`), or a `branches` pattern matches its branch. Every candidate must be clean, idle for 2 h, unused by any process, owned by no open lane, unlocked (a `claude agent` lock counts only while its pid is dead), and hold no other worktree or Git repository anywhere below it. An agent or lane worktree (`agent-*`, `lane-*`, `worktree-agent-*`, `lane/*`) must be merged into origin/dev, origin/main or a local `integrate/*`. Any other candidate is a session worktree: its HEAD must be in origin/dev or origin/main, it must have a commit of its own in its HEAD reflog unless `collect_uncommitted_sessions = true`, and every ignored path in it must have a component that `disposable` names (default `node_modules`, `__pycache__`, `target`, `.build`, `dist`, `build`, `.venv`, `.turbo`, `.next`, `storybook-static`, `coverage`, `.pytest_cache`, `.ruff_cache`, `.DS_Store`). A merged branch is deleted only when `branches` names it, no worktree or lane uses it and it is merged as its family requires; any other branch outlives its worktree. Defaults: `dirs = ["agent-*", "lane-*", ".claude/worktrees/*"]` (Codex's `codex-*` worktrees are under `.claude/worktrees`), `branches = ["worktree-agent-*", "lane/*", "claude/*", "codex/*"]`; `dev`, `main` and `integrate/*` are never collected. |
| `[gc]` `after_remove`, `after_remove_report` | project cache cleanup after removal (`{worktree}`, `{name}`, `{project}`). Stdout is ignored by default, preserving existing hooks. With `after_remove_report = true`, a successful hook must emit `{"v":1,"freed":123}` to add its actually removed bytes; diagnostics go to stderr. Invalid reports add zero bytes and a report error. |
| `[slots.priority]` `check`, `post-merge`, `gate`, `rerun`, `perf`, `default` | each run's Slotfather priority; see [slot priorities](#slot-priorities) |
| `[admission]` `provider` | `legacy` by default; explicit `slotfather` uses the versioned lifetime-admission query, separately from execution jobs. See [rollout contract](../docs/admission.md). |
| `[admission]` `cap` | this repository's lane cap (fathers#149), a positive whole number. First set wins: `LANEFATHER_CAP`, then `[admission] cap`, then the cap derived from the machine; an invalid `LANEFATHER_CAP` warns and falls back to the next. An exported `LANEFATHER_CAP` overrides every project's cap. The cap counts the planned and active lanes of this repository only; `LANEFATHER_GLOBAL_CAP`, when set, is a ceiling over every project's lanes in the state directory together. A fix lane (`review --fix-lane`) registers outside both (fathers#45); after that it counts like any other lane. |
| `[done]` `auto` | `true` by default: a lane that becomes finished runs `done` by itself, and start below the disk minimum runs it on finished lanes ([Finishing lanes](#finishing-lanes)) |
| `[tickets]` | the ticket lifecycle below; every rule is off without it |
| `[hooks]` `process_scan` | legacy process adapter for live lanes; without it `procs` supplies read-only inspection |
| `[hooks.project]` | named project policy scripts run by `project-hook NAME -- ARGS` in the installed interpreter |

### Slot priorities

Every verification run lanefather starts asks Slotfather for slots at its phase's priority. The built-in
priorities: `check` normal; `post-merge`, `gate` and `rerun` interactive; `perf` batch. A project lowers or raises
them in `.lanefather.toml`, and an orchestrator for one invocation in the environment, without code changes:

```toml
[slots.priority]
default = "batch"     # every phase this table does not name
gate = "normal"       # one phase: check, post-merge, gate, rerun or perf
```

```sh
LANEFATHER_PRIORITY=batch lanefather land a          # every phase of this invocation
LANEFATHER_PRIORITY_GATE=normal lanefather promote <sha>
LANEFATHER_PRIORITY_POST_MERGE=batch lanefather land a   # post-merge: the dash becomes an underscore
```

The first that is set wins: `LANEFATHER_PRIORITY_<PHASE>`, `LANEFATHER_PRIORITY`, this machine's
`slots.priority.<phase>` [setting](#machine-settings), the phase's key in `[slots.priority]`, its `default`, the
built-in. An empty variable counts as unset. The variables also take the
project's `legacy_env_prefix` and `[env]` names, as every setting does (`STRAYLIGHT_LANE_PRIORITY`). A value other
than `interactive`, `normal` or `batch`, a `LANEFATHER_PRIORITY_<NAME>` that names no phase, or an unknown key in
`[slots.priority]` is an error that names it. An orchestrator that exports `LANEFATHER_PRIORITY=batch` demotes all
of its runs until it unsets it.

`lanefather list` prints each phase's effective priority, with its source when it is not the built-in
(`slot priorities: check batch (LANEFATHER_PRIORITY), ...`); `list --json` gives them as `priorities`. Each slot
request carries the effective priority and its source as metadata `lanefather.priority` (for example
`batch (.lanefather.toml [slots.priority] default)`), which `slotfather status` shows. This feature advertises
`slots.priority`; older engines reject the unknown `[slots]` table.

### Execution leases after promotion

An install or deploy command that may build can opt into its own execution lease:

```toml
[[promote.install]]
name = "install"
argv = ["./install.sh"]
lease = { slots = 2, ticket = "42" }
```

`slots` is a positive integer or `"all"`; the optional `priority` (`interactive`,
`normal` or `batch`) is the step's own. Without one the step takes the `gate`
phase's [slot priority](#slot-priorities) as its environment and `[slots.priority]`
set it, and `normal` when neither does. `ticket` is optional.
These are Slotfather request fields, not metadata. Lanefather adds the step's
phase, candidate SHA and project state directory as metadata. The command still
runs from the exact promoted gate worktree, in order, and a failure stops later
commands. The lease covers a rebuild even when the tree's gate stamp is reused
or an earlier bundle is missing; a gate's finished lease supplies no capacity
for later execution.

Configured steps require Slotfather on PATH before promotion publishes anything.
Every such execution enters Slotfather, which owns admission and deduplicates a
real outer lease. Steps without `lease` keep their existing direct execution.
This feature advertises `promote.step-leases`; older engines reject the unknown
configuration key. Machine budgets and command costs belong in the project
adapter. Lane lifetime admission is separate from these execution leases.

### The ticket lifecycle

Opt-in rules for a lane registered with `--ticket N`, so the issue shows its land before it reaches `main`:

```toml
[tickets]
integrated_label = true   # after a green post-merge smoke: label the issue `integrated`, created when missing
land_comment = true       # ...and comment the merge, its branch, whether origin has it, the checks and the preview
fixes_trailer = true      # the landing merge's message ends with `Fixes #N`: GitHub closes N when it reaches main
release_comment = true    # promote --ship and `shipped`: comment main's SHA or the version on each fixed issue
                          # and remove its `integrated` label
preview = "https://preview.example.com/{short}"   # optional; {sha} {short} {branch} {ticket} {lane}
repo = "owner/name"       # optional; default: origin's GitHub repository
```

`integrated` means done and landed in the integration branch, not yet on `main` or released. A merge origin does not
have yet (a remote-tracking branch, as last fetched, that contains it) is said to be local only and is not linked. A
red smoke leaves the issue unmarked for the fix's land. The issues a release closes are those a `Fixes #N` line in
the shipped range names: origin's `main` before the push to the SHA for `promote --ship`, `--since` to `--head` for
`lanefather shipped <version>`, which a project's release process runs after it publishes. Each of them loses its `integrated` label, which no longer holds once it is released. `gh` runs without a
prompt with `--repo`; a failure warns and goes to the ledger and never fails the land, promote or release. `start`
and `next --start` warn about a lane registered with `--ticket none` while any rule is on, and `done` does not count
land's comment as the lane's evidence. The capability is `tickets.lifecycle`.

### The check contract

```
<verify> --changed --base B --plan --json   {full, paths, unowned, checks: [{id, suite, paths, producer}], suites, reasons}
<verify> --checks A,B | --suites S --keep-going --jobs N
                                             prints `Verification report: <report.json>`
<verify> --keep-going --jobs N               the full gate; prints `Verified tree <tree> stamped`
```

The plan module (`plan_module`) answers `SUITES`, `checks(...)` and `smoke(root)`; `PERF = "<suite>"` names the perf
suite. A project without one gets whole suites.

The report is `{checks: [{id, status, log, blocked_by}], stopped, environment}`. Optional row fields feed the run
history (fathers#92): `seconds` (the check's duration; without it `stats` has no p50/p90 for the check), `started`
and `finished` (ISO times), `junit` (a JUnit XML file whose failed test cases a flake names) and `lease` (the slot
lease id, also allowed at the report's top level). Lanefather hands each run its run id and tree in the slot meta
(`run`, `tree`) with `plan` and `checks` (fathers#107).

## Reviewer configuration

Authenticate the reviewer CLIs you intend to use according to their own documentation. Without a models file
Lanefather prefers Codex (`gpt-6.1-sol`, medium effort) when it can determine at least 15% weekly quota remains;
otherwise it tries Claude (`claude-opus-5-5`, medium effort). A models file's review role sets the order across
Codex, Claude and Antigravity / Google (`gemini-4-neon`, `gemini-4-argon`, `gemini-4-carbon`, `gemini-3.8-flash`).
A failed or empty reviewer run can fall back to the next choice in the role. Quota,
provider availability and account charges remain outside Lanefather.

Set `LANEFATHER_CODEX`, `LANEFATHER_CLAUDE` and `LANEFATHER_ANTIGRAVITY` (or `LANEFATHER_AGY`) to explicit executable
paths if PATH discovery (`codex`, `claude`, `agy` or `antigravity`) is unsuitable.
`LANEFATHER_MODELS=/path/to/models.toml` selects model configuration. For example:

```toml
schema = 1

[models.primary]
cli = "codex"
id = "gpt-6.1-sol"

[models.fallback]
cli = "claude"
id = "claude-opus-5-5"

[roles.review]
choices = [
  { model = "primary", effort = "medium", when = { limit = { codex_weekly = 15 } } },
  { model = "fallback", effort = "medium" },
]
```

`roles.review.choices` is a priority list (fathers#155): the first choice whose conditions hold reviews, and the
next one that holds (of another model) is its fallback when the run fails or answers empty, in either direction:
listing Opus first makes Opus the reviewer and Sol its fallback. A choice's conditions: its CLI is there; its
`when.limit.codex_weekly` share of the weekly Codex limit is left (a Codex choice that names no limit needs 15%,
so no review runs on credits unread); `when.not_family` holds when the implementer's family is known and differs.
The receipt's `fallback` says why the first choice did not review (its skip reasons, or the failed run).

With Mastack's `role` command beside the models file (`<models dir>/bin/role`), Lanefather asks `role review
[--implementer-family F] --json` for the reviewer and uses its model id, effort and skip reasons, printed and kept in
the receipt's `role` (fathers#150); the choices after role's are its fallbacks. `role` reads the Codex limit with the
CLI the review runs (`MASTACK_CODEX`) and the same models file (`MASTACK_MODELS`); Lanefather reuses that read. The
implementer's family is that of the lane's builder as its last `check` recorded it in the receipt's `agent`:
`check --agent claude-code|codex|antigravity|t3-code`, else the agent session the check runs in
(`ANTIGRAVITY_CONVERSATION_ID`, `CODEX_THREAD_ID`, `CLAUDE_CODE_SESSION_ID`, `T3_THREAD_ID`); `claude-code` is
anthropic, `codex` is openai, `antigravity` and `t3-code` are google. Unknown, the flag is left out and `role`'s
caution is shown. A `role` that fails or gives no choice leaves the choice to the models file.

A review panel runs several reviewers on every lane at once:

```toml
[review]
panel = ["sol", "opus"]   # model aliases of the models file
panel_required = false    # true: a member that should have answered and did not holds the gate
```

Every member gets the same prompt, with the diff inline, on the same frozen checkout, each in its own process; the
receipt keeps the prompt's `prompt_sha256`. Answers are written to a private temporary directory and move beside
the receipt (`reviews/<label>.<reviewer>.md`) only once every member finished, so no member can read another's. A
member whose conditions fail (the Codex limit, judged on role's read when role made one, or its CLI) is replaced by
the first choice that holds (role's choice when present), or skipped when that one is already on the panel; the
reason is in its entry, and role's model id and effort are used for the model role chose. `not_family` never takes a
member off the panel (owner decision 2026-11-16): the panel exists to compare reviewers on the same diff, and a
member of the implementer's family is a second opinion beside the other. It runs the review role's choice of that
model that does not ask for another family (Mastack's same-family Opus), and its entry carries that choice's `note`
(else "same family as the implementer (F)"), which `review` and `--compare` print. The single reviewer keeps
`not_family` as above. A member whose run fails or answers
empty is kept as `failed` and the others' answers stand. `panel_required` holds the gate on a `failed` member only:
a member the rules skipped is not missing. The receipt's `panel` lists each member: `member`, `reviewer`, `model`,
`effort`, `cli`, `status` (`finished`, `failed`, `skipped`), `reason`, `error`, `fallback`, `seconds`, `output` and
`numbers`, its findings in the one `findings` list, which numbers them across the panel and names each one's
`reviewer`; `review --mark N` and the gate read that list as before. `review ID --compare` prints each reviewer's
findings with their marks, and `stats --json` gives per-reviewer totals.

`lanefather review ID --reviewer ALIAS|both` (also with `--overall`) chooses the reviewer of that one review:
an alias of the models file (`sol`, `opus`, `neon`, `argon`, `carbon`, `flash`), a provider or CLI name (`antigravity`,
`google`, `agy`, `claude`, `codex`) or a model spec (`gemini-4-neon`, `gemini-4-argon:high`) runs whatever the Codex
limit says and with no stand-in (a missing CLI fails it), and `both` runs the configured panel (Sol and Opus without
one) the same way. Only then does the receipt carry `forced` (`{"by": "flag", "reviewers": [...]}`), which `review`
shows. The receipt records the reviewer's actual `provider`, `cli`, `model`, `effort` and `options` (`effort`,
`model_reasoning_effort` or `thought_level`). `--reviewer` does not combine with `--mark`, `--compare` or `--fix-lane`.

Receipts from this release add keys (`agent` in a check receipt; `panel`, `forced`, `role`, `prompt_sha256` and a
finding's `reviewer` in a review receipt) that an earlier release reads past: it lists, shows, marks and gates them.
The `[review] panel` and `panel_required` keys of `.lanefather.toml`, though, are refused by an earlier release as
unknown: install this release before a project sets them.

Use model IDs available to your accounts. Without an explicit file, the loader searches
`~/.config/mastack/models.toml` and `~/mastack/models.toml`; Mastack is optional, and compiled defaults apply when
no file is found. An invalid review role emits a warning and uses defaults. Setting a model file does not itself
disable external review. Set `LANEFATHER_REVIEW_START=manual` during setup to defer automatic starts, then invoke
`lanefather review ID` when you intend to submit a review. This defers required review rather than waiving it.

## Architecture

| Module | Owns |
| --- | --- |
| `config` | `.lanefather.toml`, the `LANEFATHER_*` settings and their legacy names |
| `state` | records and receipts, the schema version, locks in the state directory |
| `journal`, `events` | the ledger, the machine-wide event log mirroring it, and run logs |
| `claims`, `queue` | path claims and rule snippets; the ranked queue |
| `gitops`, `worktrees`, `landing` | git calls; smoke, gate, base and review checkouts; the landing lock |
| `host`, `slots`, `procs` | the lane cap and load; the slotfather client; the process source |
| `plan`, `checks` | the check contract; verify runs, receipts and red-check classification |
| `gc` | generic worktree/branch retention and collection, with explicit project/state values |
| `reviewer` | the reviewer choice (conditions, role, ordered fallback), the single review and the panel as one pipeline, real CLI adapters and interpreted answers |
| `land`, `review`, `done`, `promote` | land and the smoke; the review; finishing a lane; gate, perf, promote, sync |
| `lanes`, `cli` | start, next, adopt, list; the command line |
| `registry`, `status`, `settings` | the machine's project registry; the machine-wide status snapshot; machine settings |

The [architecture pass](../docs/architecture-667.md) records the survey, alternative interfaces and deployment order.

The standalone process adapter refuses PID-only live stopping until slotfather accepts an expected `(pid, start)`
identity at the actual signal boundary. Existing configured hooks keep their supported cleanup contract. A missing
`procs` on an older CLI preserves legacy `done` behavior; failed/unknown inspection retains worktrees. An answer
with pids whose working directory or lease membership is unknown is asked again up to four times, half a second
apart (fathers#158): short-lived helpers that exited between the scan and the read do not hold a worktree. Unknowns
that remain refuse `done`, naming each pid with its command (from Slotfather's `unknown`, or `procs --pid` for an
older Slotfather).

### State and its versions

Records are schema 1, the format written by `tools/lane`, the project-local tool Lanefather was extracted from.
lanefather writes nothing an older `tools/lane` cannot read, and reads everything it wrote: lanes on older branches,
each running its own branch's `tools/lane`, share the state directory with an installed lanefather. `tests/test_compat.py` proves it in both
directions against that tool (`tests/fixtures/legacy-lane`). A record of a newer schema is refused, never rewritten.

Records, receipts, the settings, the registry and the installed command are written whole: each write goes through
its own temporary file beside the target, synced before the rename, and the directory after it. A lane record that is empty
or not a JSON object (a write a crash cut short) is never read as no lane. Every command that admits, claims,
lands, promotes or collects refuses while it is there, naming the file, as do `list` and `show`; `status` lists
the project's healthy lanes and the file as a `damaged` row (a damaged record cannot name its repository, so every
project sharing the directory shows it). Restore the record or remove the file. A directory sync that fails for any
reason but an unsupported one (EINVAL, ENOTSUP) fails the write.

Safe to change: messages, the modules' insides. Not without a schema bump and a migration: record and receipt keys,
the landing lock's file format (a project's reference-transaction hook reads it), the queue file.

## Checks

```sh
python3 tools/check.py                    # Ruff and strict Pyright against tools/type-baseline.json
python3 tools/check.py --shrink-baseline  # after fixing debt: remove its budget; refuses to add any
python3 -m unittest discover -s tests -p 'test_*.py'
```

`tools/check.py` is the lint gate every package of the repository shares; the baseline counts diagnostics per file,
rule and message and only shrinks. The repository's [checks](../../docs/agents/checks.md) run both on a change here.

## Protected reference hook

`hooks/reference-transaction` is the canonical guard for agent updates to `main` and `dev`. Projects and repo-kit
pin its committed bytes; `tests/fixtures/reference-transaction` remains historical compatibility evidence.
The canonical hook keeps the existing landing token, two-hour lock and non-agent behavior. Refusals name the
generic `lanefather land`, `promote` and `sync` commands, resolved through the repository's configuration.

## Dashboard integration

Verification requests carry the lane ID and its state directory as `meta.lane`
and `meta.lanefather.state`. Slotfather uses that pair to read the selected
lane through `lanefather show ID --state-dir DIR --json`.

The version-1 summary contains project and repository identity, ticket number
and URL, lane status, branch, base, worktree, claims, dependencies, receipt
status and review status/finding counts. `checks.tree_match` is true/false when the
branch tree can be compared with the receipt, otherwise null. `checks.current`
is false for a proven tree mismatch and null (unknown) for a match: current
toolchain resolution and other verification conditions cannot be certified
without executing project code. A green saved status is not a currentness claim.
The version-1 nullable field remains compatible with receivers that omit a
current/stale annotation for null; `tree_match` is an additive narrow field. Ticket URLs are derived
from the GitHub origin without a network call. A missing origin leaves the
URL empty. The command does not create state directories, take locks, run
project hooks, start reviews or change records. A missing lane is an error.

For older requests that carry only a lane ID, `--project-dir DIR` resolves the
project configuration and checks the lane's repository identity. An unrelated
repository or missing project configuration is refused rather than matched by
ID alone.


## Event log

Every ledger line (`<state>/ledger.log`, unchanged, which orchestration metrics read) is also written as an event in
Godfather's shared schema, version 1, to one machine-wide log for every project:
`~/.local/state/lanefather/events.jsonl` (`LANEFATHER_EVENTS` names another file). `capabilities --json` gives its
absolute path as `dashboard.events.path`. One `journal.ledger` call writes both, at the same moment: the event's
`time` has milliseconds, `details.message` is the ledger text, and the other details are the kind's own fields.

- `subject`: `{"type": "lane", "id", "ticket"}` for a lane, otherwise `commit` (gate, perf, promote), `review` (an
  overall review), `remote` (sync), `queue`, `landing_lock`, `run` (a load wait) or `ticket`; always with
  `project` and `state_dir`, which tell the projects apart (lane ids repeat across projects).
- `actor`: `agent` with `session` and `name` (`claude-code` or `codex`) when `CLAUDE_CODE_SESSION_ID` or
  `CODEX_THREAD_ID` is set, else `process`; always `pid`; `via: godfather` when `GODFATHER_ACTOR` is
  `godfather/<version>`.
- `level`: `warn` for an override, a red lane check, a failed review or ticket rule; `error` for a merge conflict, a
  red post-merge smoke, recheck, gate or perf run and a failed promote step.

Kinds: `lane.started`, `lane.adopted`, `claims.added`, `lane.checked`, `landing.reviewed`, `lane.conflict`,
`lane.landed`, `lane.post_merge`, `lane.released`, `lane.done_refused`, `lane.done`, `lane.dropped`,
`lane.rechecked`, `lane.kept`, `process.stopped`,
`review.started`, `review.kept`, `review.exempted`, `review.finished`, `finding.marked`, `review.marked`,
`review.handed_over`, `review.ticket_filed`, `gate.finished`, `perf.finished`, `promote.step`, `promote.finished`,
`sync.finished`, `load.waited`, `landing.lock_taken`, `ticket.marked`, `ticket.skipped`,
`ticket.release_commented`, `ticket.failed`, `queue.updated` and `override`; `lanefather/events.py` describes each.

The writer appends each line with one `write` on the file opened with `O_APPEND`, mode 0600, under an `flock` on
`events.lock`; past 4 MiB the log becomes `events.jsonl.1`, replacing the previous one. A line over 16 KiB keeps
shorter strings; control characters become spaces. An event that cannot be written is dropped and never fails the
action. A project whose state directory is in a temporary directory writes no machine-wide event unless
`LANEFATHER_EVENTS` is set, so test runs stay out of the machine's feed. The tests validate every line in strict
mode with the shared validator, `common/eventcheck.py`.

## Machine-wide status

Every command that loads a project configuration, except the read-only `capabilities`, `status`, `settings` and
`show`, records the project in a registry beside the default state directory: `~/.local/state/lanefather/projects.json`
(`LANEFATHER_REGISTRY` names another), one entry per state directory with its config path, GitHub repository,
project name and when a command last saw it. The entry is refreshed at most every ten minutes, under an `flock` on
`projects.lock` with an atomic rename. A project whose state directory is in a temporary directory is not registered
unless `LANEFATHER_REGISTRY` is set.

`lanefather status --json` reports every registered project from any directory, for Godfather's Lanes page:

```json
{"v": 1, "host": "mbp", "time": "2026-11-12T04:26:00Z", "registry": "/Users/me/.local/state/lanefather/projects.json",
 "projects": [
   {"project": "Straylight", "state_dir": "/Users/me/.local/state/straylight-orchestration/lanes",
    "config": "/Users/me/straylight/.lanefather.toml", "repository": "maxvyshniakov/straylight",
    "cap": 4, "cap_source": "slotfather derived; derived: 4", "active": 3, "planned": 0,
    "gate": {"sha": "cedf8b97...", "status": "red", "at": "2026-11-12T04:06:19Z"},
    "lanes": [{"id": "710-speech-paste-phase", "status": "active",
               "ticket": {"number": "710", "url": "https://github.com/maxvyshniakov/straylight/issues/710"},
               "branch": "lane/710-speech-paste-phase", "base": "integrate/queue-347", "worktree": "/...",
               "claims": ["desktop/src/**"], "after": [], "created": "2026-11-12T03:10:00Z",
               "receipt": {"status": "green", "tree_match": true, "path": "/.../710-speech-paste-phase.receipt.json"},
               "post_merge": null, "review": {"status": "running", "open": 2, "findings": 3},
               "logs": {"check": "/.../logs/lane-710-speech-paste-phase-20261112T040619.log"},
               "outcome": null}]},
   {"project": "data-grid", "state_dir": "/...", "error": "config not found at /.../.lanefather.toml"}]}
```

The lanes are those not done. `receipt.tree_match` compares the receipt's tree with the lane branch's, null when
either is unknown; `review.status` is the review receipt's, `stopped` for a running review whose process is gone;
`logs` holds the newest run log of each phase (`check`, `rerun`, `base`, `post-merge`, `review`); `gate` is the newest
batch gate; `outcome` stays null until parking records one (#19). `cap` is the project's lifetime admission answer,
or its derived cap with the reason when admission is unavailable. A project that cannot be read gives
`{"project", "state_dir", "error"}`, never a failed snapshot; one whose configuration file is missing and that no
command has seen for 7 days is left out, and the next registration drops it. The command takes no locks, writes
nothing, runs no project hook and makes no network call. `capabilities --json` advertises it as
`dashboard.snapshot`: `{"argv": ["status", "--json"], "versions": [1], "poll": 5}`.

## Machine settings

A few knobs belong to the machine, not to a project. They live in a file Lanefather owns,
`~/.config/lanefather/settings.json` (`LANEFATHER_SETTINGS` names another), and the `settings` command reads and
writes them; Godfather's Settings section goes through it.

| Key | Values | Read by | Environment that wins |
| --- | --- | --- | --- |
| `review.start` | `auto` (default), `manual` | land: start the lane's review in the background, or leave it for `review ID` | `LANEFATHER_REVIEW_START` |
| `rerun.wait` | seconds, `90`, `15m`, `1h` (default 3300, at most 86400) | a red check's rerun: how long it waits for its lease | `LANEFATHER_RERUN_WAIT` |
| `slots.priority.<phase>` | `project` (default), `interactive`, `normal`, `batch` | each phase's [slot priority](#slot-priorities), above the project's `[slots.priority]` | `LANEFATHER_PRIORITY_<PHASE>`, `LANEFATHER_PRIORITY` |
| `host.min_disk_gb` | `auto` (default; shown and saved as `0`), or decimal GB up to 100000 (`150`, `60.5`, `1.5e2`) | start, `capabilities` and a project hook: the [free-disk floor](#finishing-lanes) | `LANEFATHER_MIN_DISK_GB` (and its other names) |

With a slot priority saved, the order of [slot priorities](#slot-priorities) becomes: `LANEFATHER_PRIORITY_<PHASE>`,
`LANEFATHER_PRIORITY`, the machine's `slots.priority.<phase>`, the project's `[slots.priority]` phase and default,
the built-in; its source reads `lanefather settings slots.priority.<phase>`.

The `host.min_disk_gb` row is a `number` in GB with `min` 0, `step` 5 and `zero: "auto"`; its `effective` is the floor
start applies in the project the command runs in, and its description gives the derived floor `auto` stands for, for
example `auto: 58 GB (10^9) = 40 reserve + 6 x 3.0 GB lane peak`, so Godfather's Settings shows it as `auto  effective
here: 58 GB`. Admission, `capabilities --json` (`host.min_disk_gb`) and the refusal resolve the same value, with
the cap start applies: Slotfather's when `[admission] provider = "slotfather"`, which they ask as start does but
wait at most 1 s for, inside Godfather's deadlines. When Slotfather cannot answer in that time, the description says
so (`auto: cap unknown (lifetime admission unavailable: query timed out); local cap 3 gives 49 GB …`), the row has no
`effective`, and `capabilities` reports the local cap's floor. `LANEFATHER_MIN_DISK_GB` takes any finite positive
number, as before the setting, beyond the setting's 100000 too.

```text
lanefather settings [--json]                                  the schema and the current values
lanefather settings check KEY VALUE [--json]                  {"v": 1, "ok", "error", "preview"}
lanefather settings set KEY VALUE [--revision R] [--json]     save; the reply is the settings with "ok"
lanefather settings reset (KEY | --all) [--revision R] [--json]
```

The `--json` schema follows Godfather's settings contract, version 1: `{"v": 1, "tool": "lanefather", "writable",
"reason", "revision", "status": [{"label", "value"}], "sections": [{"id", "title", "settings": [{"key", "label",
"type", "value", "default", "source", "writable", "revision", "description", "choices"?, "min"?, "max"?, "step"?,
"unit"?}]}]}`. A knob the environment sets is a read-only row whose `source` names the variable; the project's own
`[slots.priority]` values, from the `.lanefather.toml` of the repository the command runs in, are read-only rows of a
`project` section. `revision` is the SHA-256 of the settings file; a `set` or `reset` with a stale `--revision` is
refused and its reply is the current settings, and without `--revision` it merges. Writes take an `flock` on
`settings.lock` and replace the file atomically, mode 0600. Exit codes: 0 done, 2 invalid, 3 conflict, 4 read-only.
`capabilities --json` advertises it as `dashboard.settings`: `{"versions": [1]}`.

## Installed runtime contract

`lanefather capabilities --json` returns version 1, the state schema, supported
Slotfather query versions and the installed source commit and payload SHA-256, and a
`dashboard` block for Godfather: `{"v": 1, "tag": "lane", "events": {"path", "versions": [1]},
"snapshot": {"argv", "versions", "poll"}, "settings": {"versions": [1]}}`.
An installed release verifies its package and canonical hook against its
manifest; a changed or missing manifest refuses verification. A checkout reports
`installed: false`, so a project launcher can require the installed artifact.
The installer accepts committed shipping inputs and reuses an identical existing
release without replacing its bytes.

Free disk (fathers#46). Two values, not to be confused:

| Value | Meaning | Where a project reads it |
| --- | --- | --- |
| `host.min_disk_gb` | the free disk, in decimal GB, under which `start` refuses a lane in the project the command runs in: `LANEFATHER_MIN_DISK_GB`, the machine setting `host.min_disk_gb`, or the derived floor of [Finishing lanes](#finishing-lanes) | `capabilities --json` (`"host": {"min_disk_gb": 150}`, capability `host.min_disk_gb`); in a project hook, `LANEFATHER_MIN_DISK_GB` and each other name of the setting (`<legacy_env_prefix>MIN_DISK_GB`) |
| `DISK_FREE_GB` | a stand-in for the measured free space of the home volume: tests and hooks set it, admission reads it instead of measuring | `LANEFATHER_DISK_FREE_GB` or `<legacy_env_prefix>DISK_FREE_GB`, as the caller set it |

Lanefather sets the minimum's variables for the hook's run to the project's floor and restores the caller's
environment after it. Admission reads `LANEFATHER_MIN_DISK_GB` (fathers#157): a caller that sets it sets the floor,
above the machine setting `host.min_disk_gb`.

`lanefather --config /path/to/.lanefather.toml project-hook NAME -- ARGS` runs only
a named script from `[hooks.project]`, with the project's directory as its working
directory. The script receives `LANEFATHER_PROJECT`, an explicit
`lanefather.project.Project`; `readiness()` supplies one configured queue/state
snapshot with `planned_or_active`, the exact planned-plus-active lane-cap group; `Project.paths` resolves state/queue locations
without creating them. `update_queue(policy)` validates and atomically writes policy under
the shared queue lock. The script owns project selection and presentation; the
installed package owns the lane workflow and persistence. A script's exit status
is returned unchanged. Project code needs no source-checkout import path.
