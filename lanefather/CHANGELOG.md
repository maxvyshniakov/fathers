# Lanefather Changelog

Versions: `lanefather/__init__.py` names the version, and an installed release also names its commit
(`lanefather --version`). The releases projects pinned before the next version are `0.1.0+<commit>`: each
installed as 0.1.0 and is told apart by its commit. State compatibility: [README](README.md#state-and-its-versions).

## Unreleased

### Added

- Two routine cleanups without `--force-reason` (fathers#167). `lanefather drop ID` removes a planned or active
  lane that adds nothing to its base (no commits beyond it, its tree already the base's, or commits that change
  nothing) and whose worktree has no uncommitted or untracked changes, with `done`'s cleanup; it refuses a lane
  with work and has no override. `done` no longer refuses a landed lane whose red post-merge smoke a later green
  run on the same integration branch cleared: the batch gate, or another lane's smoke that ran each check the red
  one ran, on a commit holding the merge; the ledger and the record (`post_merge.cleared_by`) name that receipt,
  and `done --finished` counts such a lane as finished. `lanefather recheck ID` reruns the red smoke's checks on
  the integration tip; green, it replaces the red result and finishes the lane as land would. New event kinds
  `lane.dropped` and `lane.rechecked`; capabilities `lane.drop` and `post_merge.cleared`. An older Lanefather
  reads a dropped lane as done and a rechecked one as green.

- The free-disk floor is a machine setting (fathers#163): `lanefather settings set host.min_disk_gb 150` (decimal
  GB) or `auto` (the default, the derived 40 GB reserve + lane cap x lane peak). `LANEFATHER_MIN_DISK_GB` still wins
  and makes the row read-only. Admission, `capabilities --json` (`host.min_disk_gb`) and the refusal use the same
  value; a refusal names its source. The row's description gives the current derived floor and its `effective` the
  applied one, so Godfather's Settings shows and changes it. With Slotfather as the admission provider, the
  reported floor uses Slotfather's cap, as start does, and the row says when Slotfather cannot answer instead of
  showing the local cap's floor as applied. An older Lanefather ignores the saved value.

- Lanes finish by themselves (fathers#157). A lane that becomes finished (landed, smoke green, review finished or
  exempt with every finding marked) runs `done`, as before, unless it is kept; `[done] auto = false` turns that off.
  `lanefather keep ID [--off]` keeps a lane from it. `lanefather done --finished` runs `done` on every finished lane
  of the registered projects, and `--dry-run` lists them with their sizes. `start` under the free-disk minimum now
  runs that first, at most once in 10 minutes and only when the disk is the only refusal, and admits the lane when
  it freed enough, instead of only refusing. Every refusal of `done` still applies; unattended, `done` also needs a
  process source that answers, and scans the lane's build directories and scratch as well as its worktree.
  Install this release before a project sets `[done]` (an older Lanefather refuses a configuration with a key it
  does not know) and before `keep` is used on a lane an older Lanefather will land (an older land does not carry
  the flag).

- A review panel (fathers#151): `[review] panel = ["sol", "opus"]` runs every listed reviewer at once on the same
  frozen checkout with the same prompt (its `prompt_sha256` in the receipt), each in its own process, its answer in
  a private directory until every member finished. The receipt's `panel` keeps each reviewer's model, effort,
  seconds, status and findings; the one `findings` list numbers them across the panel, so `review --mark N` and the
  gate work as before. A member whose conditions fail (a used-up Codex week, the implementer's own family) is
  replaced by the first choice that holds, or skipped when that is already on the panel; one whose run fails keeps
  the others' answers, and holds the gate only with `[review] panel_required = true`. `review ID --compare` shows
  who found what and how it was marked; `stats --json` adds per-reviewer totals (`reviewers`). Without `panel`,
  one reviewer as before. An earlier release refuses the `panel` key: install this one first.
- The reviewer is chosen by Mastack's `role review --json` when the models file has `bin/role` beside it
  (fathers#150): its model, effort and skip reasons, printed and kept in the receipt's `role`. The implementer's
  family comes from the check receipt's new `agent`: `check --agent claude-code|codex`, else the agent session the
  check runs in, a Codex thread before a Claude Code session. Without `role`, the models file decides.
- `lanefather review ID --reviewer sol|opus|both`, also with `--overall` (fathers#155): this review runs the named
  reviewer whatever the Codex limit says, or `both` (the configured panel, else Sol and Opus) into one findings list
  that `--mark` marks; the receipt's `forced` says it was asked for.

- The reviewer is asked two design questions besides bugs and the standards (does the change fit the structure it
  lands in or is it bolted on beside it, and does it copy behaviour the repository already has), answered as findings
  of the new category `design`.
- The free-disk minimum under which `start` refuses a lane is part of the runtime contract (fathers#46):
  `capabilities --json` reports it as `"host": {"min_disk_gb": 150}` (capability `host.min_disk_gb`), and a
  project hook finds it in `LANEFATHER_MIN_DISK_GB` and `<legacy_env_prefix>MIN_DISK_GB`, next to `DISK_FREE_GB`,
  so a project no longer reads `MIN_DISK_GB` out of the installed `host.py`.
- A check result stands only for the platform it ran on (owner decision 2026-11-13): the results store's entries
  name their `platform` (`<os>-<arch>`, such as `darwin-arm64` or `linux-x64`), its `env` and `plan` hash it, and a
  lookup from another platform reuses nothing, so a green gate on the Mac never skips the Linux run, while a second
  run of the tree on the same platform is still skipped. `lanefather results key|lookup|record --platform` asks
  for or records another platform; `LANEFATHER_PLATFORM` stands in for the host's. An entry an earlier release
  wrote names no platform and matches none: its checks run once more ([docs/results.md](docs/results.md)). Lane
  and post-merge receipts name their platform as well, and one from another platform is not current; an older
  receipt counts as this host's when the lane's worktree is here. The `plan` in
  slot requests changes with it, so each run type's duration history starts once more.
- Check results belong to a tree and a plan (fathers#91): `check`, the post-merge smoke and the batch gate record
  each result in `<state>/results/<tree>.json`, and a check that already passed on the same tree under the same
  toolchain, in any lane, is not run again; the receipt and the output name the run that stands for it. `promote`
  of a commit whose tree the batch gate already passed says which gate passed it. `lanefather results
  key|lookup|record` is the interface for Repo-Kit's gates and CI templates ([docs/results.md](docs/results.md)).
- Every slot request Lanefather makes names its run type for Slotfather's expected durations (fathers#107): `plan`,
  the results store's key of the plan it runs (fathers#91), and `checks`, how many checks it runs, beside the
  `phase` it already named (check, post-merge, rerun, gate, perf, or a promote step's name).
- The run history (fathers#92): each check's start, end, status and duration, the run id its slot lease names in its
  meta (`run`, with `tree`), and flakes (a check that failed and then passed on the same tree, rerun by itself or in
  a later run, with the failed JUnit tests when the check names a report) go to `<state>/history/<YYYY-MM>.jsonl`,
  which `done` leaves alone; twelve months are kept. Receipts name their runs in `leases`. `lanefather stats [--days
  N] [--lane ID] [--json]` reports per-check p50 and p90, red runs and flakes.
- A review-fix lane checks only what its fix touches (fathers#93): the plan of its own diff when every path it changes
  is in the parent lane's diff, otherwise the plan of both diffs; either way the parent's passed results stand for
  each check no change since the parent's head selects. The receipt says which (`fix_of`). Land, the smoke and
  promote's batch gate are unchanged.
- `lanefather changelog --json [--since VERSION]` gives the installed release's changelog as entries for
  Godfather's what's new (fathers#104), the Unreleased ones first with the installed commit; install.py ships the
  changelog in the release ([docs/api.md](docs/api.md)).
- Cheaper post-merge smokes (fathers#94): a smoke check the merge does not select that passed on the previous
  tip's tree does not run again, as the lane's receipt already stands for what the integration did not change; and
  `[checks] gate_only = ["tools/test-*"]` leaves the matching checks (tooling self-tests) to the batch gate. The
  land output, the post-merge receipt (`skipped`) and the ledger give each skip's reason.
- A review finding can carry a category (bug, standards, docs, package-misuse), and a package-misuse finding names
  the package it misuses (`package`), for the agent-friction journal; the reviewer is asked for both, `review`
  shows the category, and receipts without it read as before.

### Changed

- The free-disk floor under which `start` refuses a lane is derived (fathers#157): 40 GB plus the lane cap times the
  largest of the 20 last done lanes (3 GB before any), in decimal GB, instead of a fixed 150 GB.
  `LANEFATHER_MIN_DISK_GB` sets it, refusals give the derivation, and `capabilities --json` reports the floor of the
  project it runs in as `host.min_disk_gb`.
- `done` refuses a lane whose worktree HEAD has commits not in the integration branch, whose integration worktree
  is gone, or whose worktree another lane that is not done works in (a fix lane).
- The review role's `choices` are a priority list (fathers#155): the first whose conditions hold reviews and the next
  is its fallback, in either direction, so listing Opus first makes Opus the reviewer and Sol its fallback. A Codex
  choice that names no limit needs 15% of the weekly limit, as before. The single reviewer gets the diff inline in
  its prompt, as the panel and Opus always did.

- The lane cap counts the planned and active lanes of one repository, not of every project sharing the state directory
  (fathers#149): a lane is never refused, nor held by claims, because of another repository's lanes. A project sets
  its cap with `[admission] cap = N` in `.lanefather.toml`. First set wins: `LANEFATHER_CAP`, then `[admission] cap`,
  then the cap derived from the machine as before; an invalid `LANEFATHER_CAP` warns and falls back to the next. An
  exported `LANEFATHER_CAP` overrides every project's cap. `LANEFATHER_GLOBAL_CAP`, when set, is a ceiling over every
  project's lanes together, and its refusal lists them by repository. `list` counts this repository's lanes and `list
  --json` adds `global` (`active`, `cap`, `cap_source`); the Slotfather lifetime query gets this repository's count.
- `review --fix-lane` registers the fix lane outside the lane cap, in its landed parent's place (fathers#45 (a)): a
  full cap no longer stalls a finished fix round. Disk, load and claims still apply. Once registered, the fix lane
  counts toward the cap like any planned or active lane. The fix lane's record and its `lane.started` event say why
  (`cap_source: fix-lane of <parent>`).
- `gc` now also collects the worktrees Claude Code and Codex sessions leave under `.claude/worktrees`, not only
  `agent-*` and `lane-*` ones (fathers#44), with no configuration change: the patterns are `[gc] dirs` (default
  `agent-*`, `lane-*`, `.claude/worktrees/*`, which covers Codex's `codex-*`) and `[gc] branches` (default
  `worktree-agent-*`, `lane/*`, `claude/*`, `codex/*`). Agent and lane worktrees keep their rules. Any other
  worktree there is a session worktree and is removed only when it is clean, idle for 2 h, unused, owned by no open
  lane, its HEAD is in origin/dev or origin/main (a local `integrate/*` branch is not enough), it has a commit of its
  own (`[gc] collect_uncommitted_sessions = true` drops that rule), and it holds no ignored file outside
  `[gc] disposable` (build output such as `node_modules`, `target`, `__pycache__`); a merged `claude/*` or
  `codex/*` branch is deleted only once it is in origin/dev or origin/main. To keep the old behaviour, set
  `[gc] dirs = ["agent-*", "lane-*"]` and `branches = ["worktree-agent-*", "lane/*"]`; a `.keep` file or a
  `git worktree lock` keeps one worktree. Every candidate is now kept when it holds another worktree or Git
  repository, ignored directories included, and a `claude agent` lock whose pid is alive keeps its worktree.
- Lane records and receipts are written durably (fathers#129): each write has its own temporary file and is synced
  with its directory before the rename, as the settings and the registry already were, so two writers no longer
  share a `<name>.pending` file and a crash leaves the old record or the new one. A lane record that is empty or not
  JSON is reported, naming the file and that restoring or removing it is the fix, instead of reading as no lane:
  `start`, `land`, `promote`, `gc`, `list` and `show` refuse, so nothing registers over it or drops its claims,
  while `status` lists the healthy lanes with the damaged file as a `damaged` row. A directory sync that fails
  for any reason but an unsupported one (EINVAL, ENOTSUP) fails the write.
- Unmarked P1 and P2 review findings hold the batch gate, not only the move of dev, and P3 findings no longer
  block (fathers#109, owner decision): the refusal lists each finding with the two commands that mark it, P3 findings
  are reported, and `[review] blocks = ["P1", "P2", "P3"]` keeps P3 blocking. `lanefather findings <sha> [--since
  REF] [--json]` answers the same rule for a publish Lanefather does not run (Repo-Kit's `release:publish`);
  `--force-reason` overrides and is logged. An unmarked P3 finding still keeps its lane open until it is marked.
- Lanefather lives in the fathers repository with Slotfather, Junkfather and Godfather: `python3 install.py
  lanefather` from its root installs it, and its documentation links point there.

### Fixed

- `done` no longer refuses a landed lane because short-lived processes elsewhere on the machine came and went during
  the process query (fathers#158): an answer with unknown pids is asked again up to four times, and a refusal names
  each pid that stays unknown with its command.

## 0.1.0+1138486 - 2026-11-12

### Added

- Every ledger entry is also an event in Godfather's schema in one machine-wide
  `~/.local/state/lanefather/events.jsonl` (LANEFATHER_EVENTS names another), and `capabilities --json` names the
  tool and a dashboard block with the event log.
- `lanefather status --json` reports every registered project's lanes on this machine, read-only: cap, counts, the
  newest batch gate, and each open lane's ticket, receipt, smoke, review and newest logs. Commands record their
  project in `~/.local/state/lanefather/projects.json`.
- `lanefather settings [check|set|reset]` reads and changes this machine's settings in Godfather's settings
  contract: `review.start`, `rerun.wait` and `slots.priority.<phase>`, below their environment variables and above
  the project's configuration.

### Changed

- The release-comment command is `lanefather shipped <version> --since REF`; `released` is gone.

## 0.1.0+7eca44a - 2026-11-11

### Added

- `[tickets]` in `.lanefather.toml` turns on the ticket lifecycle rule by rule: after a green smoke, land labels the
  lane's issue `integrated` and comments the merge; the landing merge can end with `Fixes #N`; `promote --ship` and
  a release comment the SHA or version on each issue they close and take the `integrated` label off it.
- A project sets each phase's Slotfather priority in `[slots.priority]`, and an invocation with LANEFATHER_PRIORITY
  or LANEFATHER_PRIORITY_<PHASE>; `list` shows each phase's priority and where it comes from.

## 0.1.0 - 2026-11-11

### Added

- The lane workflow as its own package, moved from Straylight's `tools/lane`: `start`, `next`, `adopt`, `check`,
  `land`, `review`, `done`, `promote`, `sync`, `perf`, `gc`, `list` and `show`, with Straylight's lane records,
  receipts and ledger read and written as before.
- `capabilities --json` and a verified installed runtime: each install is an immutable release with a manifest of
  its files, so a project can pin an exact source and certify it before it runs.
- Projects configure their seams in `.lanefather.toml`: the check contract, the plan module, the stamp tool,
  install and deploy steps, project hooks for readiness and queue operations, and the canonical protected-reference
  hook Lanefather owns.
- Leased promote steps: an install or deploy step can run under its own Slotfather lease.
- An opt-in Slotfather admission provider for the lane cap, load and disk policy.
- `gc` reports the bytes a project's cleanup frees when the project opts in.

### Changed

- A lane check plans against the newest merge-base with its integration branch, and a review-fix lane starts on the
  branch its parent landed on.
- A red check reruns by itself at the lane check's own lease size, never on the whole machine, and on its base only
  after that rerun is red too; the messages say "rerun by itself".
- A file claim is one path, not a directory that globs reach into; claim kinds are asked at the project's HEAD, and
  submodules count as directories.

### Fixed

- `done` fails closed: it keeps a worktree whose cleanup observations changed while it ran.
- A refused fix lane names the integration branch it needs.
