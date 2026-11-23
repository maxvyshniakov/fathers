# Lanefather Changelog

Versions: `lanefather/__init__.py` names the version, and an installed release also names its commit
(`lanefather --version`). The releases projects pinned before the next version are `0.1.0+<commit>`: each
installed as 0.1.0 and is told apart by its commit. State compatibility: [README](README.md#state-and-its-versions).

## Unreleased

### Added

- `lanefather status --json` names each project's main checkout (`checkout`, its real path): the project its
  lanes own their worktrees and folders under in Spawnfather, so Godfather shows a lane's spawns (fathers#171).

- Subset `--only` reruns for failing test files (fathers#147): when a verifier advertises `"only"` capability and
  reports `failed_tests: [...]`, `Rerun` passes `--only <test,...>` to both lane and base reruns, records the subset
  tests in the receipt (`classification.checks[].tests`, `classification.runs[].tests`), and leaves a check
  `unclassified` if any red test falls outside `failed_tests`; when not advertised, argv and receipts stay
  byte-identical.

- Dynamic lane check worker count via Slotfather range leases (fathers#43): detects `slot-range` in `slotfather
  capabilities --json`, passes `--jobs` max with `<slot_env>_MIN_JOBS` (and `slotfather run -n MIN-MAX` in
  `slots.wrap`), and records the granted worker count from the verify report in the lane receipt and run history
  (`history.py`) while preserving exact argv and receipts on older Slotfather releases.

- Configurable project policy in `.lanefather.toml` and `capabilities --json` (fathers#131): `[promote] branches`
  (default `["dev", "main"]`), `[lane] worktrees_root` (default `".claude/worktrees"`), `[lane] target_label`
  (default `"Cargo target"`), `[lane] cache_depfiles` (default `["debug/*.d", "release/*.d"]`), and
  `[checks] toolchain_name` (default `"Rust toolchain"`). Existing consumers need no config change; `lanefather
  capabilities --json` reports the effective `policy` values (`project.policy` capability), and single-branch
  projects without Rust land and promote without overrides.

- Bounded verify progress heartbeats during quiet runs (fathers#190): `lanefather.verify.Run` samples log silence,
  Slotfather queue state (`slotfather status --json --pid`), and process tree CPU/child activity during quiet
  intervals (`LANEFATHER_VERIFY_HEARTBEAT`, default 30 s) and prints bounded progress lines distinguishing `queued`,
  `setup`, and `active` states, active suite/check, last-output age, and activity evidence without fabricating test
  names, while preserving command outcomes and receipts.

- `lanefather land` three-phase split (`merge`, `post_merge`, `after_land`), kill-after-merge recovery, and
  lock-free `done` cleanup (fathers#72): `land` records a `landing` intent before merging and finishes the `land`
  transition on re-land if killed between merge and save (with `lanefather land ID --repair` for pre-intent
  merges); `done` runs `gh`, `du`, and `rmtree` outside the `state` lock; `land` and `review` finish lanes through
  `lifecycle`.

- Safer release management in `install.py` (fathers#50): pruning keeps every release pinned by a live lane in
  registered state directories and every release in use by a running process (lock or live pid file), plus the
  newest three releases ordered by version and build order rather than mtime; normal installs never move `current`
  backwards; `--no-switch` installs into `releases/` without changing `current` or the command (and an older commit
  re-installed this way does not count as newest); `--rollback` points `current` at the previous release;
  `--uninstall` removes the command, `current` and unpinned releases while preserving releases pinned by live lanes
  or in use; and stale `releases/.pending-<pid>` directories whose owning pid is gone are cleaned automatically.

- Antigravity / Google reviewer backend (fathers#201): `lanefather review` supports the third reviewer family
  (`antigravity` / `google` / `agy` CLI or configured `[cli.antigravity]` harness adapter from Mastack's `models.toml`,
  with `gemini-4-neon`, `gemini-4-argon`, `gemini-4-carbon`, `gemini-3.8-flash` and `thought_level`), selectable via
  `--reviewer` (`antigravity`, `google`, `agy`, model alias or `<model>[:<thought_level>]` spec) and automatic
  `not_family = true` cross-family routing across `anthropic`, `openai` and `google`. Review receipts record the
  actual `provider`, `cli`, `model`, `effort` and `options` (`effort`, `model_reasoning_effort`, `thought_level`).

- Multi-provider agent attribution on lanes (fathers#200): `start`, `adopt` and every `check` record each agent that
  worked the lane (`claude-code`, `codex`, `antigravity`, `t3-code`, or any configured harness/provider kind) with its
  session id, subagent/thread id, transcript path, model and options (`effort`, `thought_level`) on the lane record
  (`agents`) and in `lanefather show <id> --json`. `--agent` now accepts any valid provider kind identifier instead of
  only `claude-code` or `codex`.

- `lanefather wait ID --for registered|landed|done [--timeout S] [--json]` (fathers#175) blocks until the lane is in
  that state instead of a loop over `list`: it reads the lane's record with a growing pause, exits 0 on the state, 75
  with the current state at the timeout (default 3600 s; 0 answers at once) and 3 for a state that cannot come (a
  dropped lane never lands). `start` and `adopt` end with the lane's next commands (its check, the hand-back with
  the receipt path, the wait for its land). Capabilities `lane.wait` and `review.fix_of`.

- A batch fix lane is exempt from review (fathers#174): `lanefather start ID --fix-of A,B,C` names the landed lanes
  whose review findings it fixes, and its land records `review.exempted` instead of starting a review, as for a
  `review --fix-lane` lane; `review A --mark N fixed:ID` marks the parents' findings with its merge as before.
  Each parent must have landed and have a finished review with unmarked findings. A lane started with `--fix-of`
  runs its own check plan even with one parent. `start` warns when an id that reads `review-fixes` names no parent.
  The record keeps the first parent's ID in the string `review_fix_of` for older installs and lists every batch
  parent in `review_fix_of_all`. Older Lanefather versions treat the first parent as a single-parent fix.
  Existing list-valued records are normalized when saved by this version.
  `review --fix-lane` hands over one lane's worktree.

- Two routine cleanups without `--force-reason` (fathers#167). `lanefather drop ID` removes a planned or active lane
  that adds nothing to its base (no commits beyond it, its tree already the base's, or commits that change nothing)
  and whose worktree has no uncommitted or untracked changes, with `done`'s cleanup; it refuses a lane with work and
  has no override. `done` no longer refuses a landed lane whose red post-merge smoke a later green run on the same
  integration branch cleared: the batch gate, or another lane's smoke that ran each check the red one ran, on a commit
  holding the merge, finished no earlier than the red smoke, on its platform; the ledger and the record
  (`post_merge.cleared_by`) name that receipt, and `done --finished` counts such a lane as finished. `lanefather
  recheck ID` reruns the red smoke's checks on the integration tip, one recheck of a lane at a time; green, it
  replaces the red result and finishes the lane as land would. The batch gate's receipt names its `platform`. New
  event kinds `lane.dropped` and `lane.rechecked`; capabilities `lane.drop` and `post_merge.cleared`. An older
  Lanefather reads a dropped lane as done and a rechecked one as green.

- The free-disk floor is a machine setting (fathers#163): `lanefather settings set host.min_disk_gb 150` (decimal
  GB) or `auto` (the default, the derived 40 GB reserve + lane cap x lane peak). `LANEFATHER_MIN_DISK_GB` still wins
  and makes the row read-only. Admission, `capabilities --json` (`host.min_disk_gb`) and the refusal use the same
  value; a refusal names its source. The row's description gives the current derived floor and its `effective` the
  applied one, so Godfather's Settings shows and changes it. With Slotfather as the admission provider, the
  reported floor uses Slotfather's cap, as start does, and the row says when Slotfather cannot answer instead of
  showing the local cap's floor as applied; a report waits at most 1 s for Slotfather. `LANEFATHER_MIN_DISK_GB`
  keeps its numeric syntax (`1.5e2` still sets 150 GB). An older Lanefather ignores the saved value.

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
  gate work as before. A member whose conditions fail (a used-up Codex week, no CLI) is replaced by the first choice
  that holds, or skipped when that is already on the panel. A member of the implementer's family stays on the panel
  (owner decision 2026-11-16: the panel compares reviewers; the single reviewer still avoids the implementer's
  family): it runs the review role's choice for that case, and its entry, `review` and `--compare` carry that
  choice's note ("same family as the implementer: fresh context only"). One whose run fails keeps
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

- Spawnfather now removes what a lane owns (fathers#171). `start`, `adopt` and the fix-lane hand-over register the
  lane's worktree, scratch and build directories with Spawnfather; `done`, `drop` and `gc` ask it to stop what was
  left running there and remove them, and register a lane started before the upgrade on the spot. Lanefather's
  refusals stay as they were. A landed lane is removed although its integration branch is not promoted yet; an
  ignored file outside `[gc] disposable` (an `.env`) now keeps the worktree until `--force-reason`.
- Without Spawnfather (or with one too old) `done`, interactive too, and `drop` refuse with exit 3 and say how to
  install it, and `gc` keeps every worktree; no override lifts that.
- `[done] stop_inside = true` has Spawnfather stop the processes a lane left behind before the removal. `[hooks]
  process_scan` is deprecated: it is no longer loaded, and while it is set without `[done] stop_inside` it turns
  stopping on with a warning.

- `land` holds for unmarked P1/P2 findings from other finished reviews in this repository. It prints each
  finding's mark commands and the fix-round path, reports P3 findings as warnings and logs `--force-reason`
  overrides. A fix lane can pass its own valid parents. `list` ends with the pending review summary (fathers#177).

- Lane registration prints a load advisory when load15 exceeds the threshold and still honors the lane cap, claims,
  dependencies and disk floor. High load also leaves `Project.readiness` available for a lane that can register.
  Execution waits for capacity through the project's Slotfather adapter. Capability `lane.load_advisory`
  (fathers#172).

- `lanefather --help` fits one screen (fathers#175): one line per command, the details and an example in
  `lanefather <command> --help`. A usage error is two lines, as docs/cli.md asks: `lanefather: <reason>`, then
  the command's usage, one example and `Try 'lanefather <command> --help'.`; an unknown option is named before a
  missing argument. The CLI convention's conformance test has no expected failures left.

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

- Multi-project readiness and verification receipt reuse (fathers#234): multi-project readiness queries now resolve
  directory claims against each project's own repository root rather than the caller's working directory, review-fix
  lane check reuse infers the host platform for legacy parent receipts from the lane worktree, and multiple projects
  and state directories opened in one process remain isolated without cross-project configuration leakage.

- `lanefather land` now honours `[review] blocks` in `.lanefather.toml` when checking other finished reviews for
  unmarked findings, matching `lanefather promote` and `lanefather findings` (fathers#230): severities excluded from
  `[review] blocks` are reported as non-blocking warnings instead of refusing the merge, and `[review] blocks` that
  include `P3` hold `land` on unmarked `P3` findings and name the configured severities in the refusal and `land
  --help`.

- Unified post-merge smoke, clearing, and lane-completion lifecycle (fathers#227): post-merge smoke, `lanefather
  recheck`, `lanefather done`, `lanefather done --finished`, and automatic completion after `land` and `review` now
  share one phase and completion model, recover a green `<id>.recheck.json` written before an interrupted `recheck`
  could update the lane record, fence late smoke or recheck writes after `done` or a superseded merge commit, resolve
  legacy batch-gate platforms from each registered project's own results directory during `done --finished`, and
  re-check lane state under the state lock before releasing a locked worktree so concurrent completions never emit
  duplicate release output or fail on an already-finished lane.

- Single-location `pre_land_fix` storage and stale review worker protection (fathers#216): review receipt
  transitions now enforce lifecycle phase rules under `<label>.review.json`'s lock, separate partial-panel
  completion under `[review] panel_required = true` from hard review failures, fence `finish` and `fail` by worker
  PID so a superseded background review never overwrites a `--force-reason` re-review, and store `pre_land_fix`
  solely in `<id>.review.json` while keeping a guarded read fallback for legacy lane records that still carry
  `pre_land_fix`.

- Unified lane-`done` readiness and review `panel_required` / manual-start handling (fathers#213): `done <id>`,
  `done --finished` and post-review auto-done now share one readiness check (`done.done_readiness`), so auto-done
  after `review` or `review --mark` finishes a lane whose red post-merge smoke was cleared by a later green batch
  gate or smoke, kept lanes no longer print a premature finish notice while their post-merge smoke is red or unrun,
  and `done` refuses a landed lane whose post-merge smoke has not run unless `--force-reason` overrides it;
  `promote --ship`'s deferred overall review message now names `lanefather settings review.start=manual` when manual
  review start comes from machine settings; and `review` / `review --overall` evaluate `[review] panel_required`
  consistently with `receipts` and `findings`, exiting non-zero and holding auto-done when a required panel member
  failed while keeping `--compare`, `--mark` and `--fix-lane` working on the answered members' findings.

- Pre-land review fix rounds and clearer hand-over refusals (fathers#197): `lanefather review ID --fix-lane` now
  supports one truthful pre-land fix round on a reviewed `active` candidate in its existing worktree, preserving
  the reviewed commit SHA and unmarked finding linkage in both the lane record and review receipt (`pre_land_fix`,
  event `review.pre_land_fix`) without landing the defective original or running a second blind review; marking a
  finding `fixed:<sha>` in a pre-land fix round refuses a commit that does not advance past the reviewed SHA, and
  `--fix-lane` refusals now distinguish unsupported lane status from a missing or nonexistent worktree.

- Lane checks honour a step's declared minimum slots instead of falling to 1 slot on a busy machine (fathers#173):
  a check row in `--plan --json` may declare `min_slots` or `slots`, and `.lanefather.toml` accepts
  `[checks.min_slots]` mapping check or suite fnmatch patterns to a positive slot floor; `check` leases at least that
  floor (capped at the machine's total slot capacity, while an explicit `--slots` override still wins) and records
  `min_slots`, `advised_jobs` and `min_slots_for` in `jobs_decision`.

- Dependency-blocked checks (`status: "blocked"`, or `status: "skipped"` with `blocked_by`) are preserved separately
  from executed check failures across the check receipt, suite results and red-check classifier (fathers#195): only
  checks that actually ran and failed consume the rerun budget or enter `first.failed_checks`, dependency-blocked
  children are classified as `dependency-blocked` with their `blocked_by` prerequisites instead of `regression`, and
  the receipt stays red until those checks actually run and pass.

- `lanefather land` no longer prepares the post-merge plan (and so no longer waits for a slot while the project's
  plan step installs dependencies) when the lane's current green receipt already verified the exact merge tree
  under the same toolchain and platform and passed every check the smoke would run (fathers#198). A changed tree, a
  stale or red receipt, a changed toolchain, or a merge onto another base that changes paths the receipt's plan did
  not cover still prepares the plan and runs the required checks. When it prepares one, the land log says so and
  why, with `queue: slotfather status`, and then how many checks the prepared plan leaves to run.

- Usage errors follow argparse's handling of option values. A quoted merge message or force reason starting with
  a dash works again (fathers#175).

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
