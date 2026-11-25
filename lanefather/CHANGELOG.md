# Lanefather Changelog

Versions: `lanefather/__init__.py` names the version, and an installed release also names its commit
(`lanefather --version`). The releases projects pinned before the next version are `0.1.0+<commit>`: each
installed as 0.1.0 and is told apart by its commit. State compatibility: [README](README.md#state-and-its-versions).

## Unreleased

### Added

- `lanefather review <id> --record --reviewer NAME --verdict approve|changes_requested [--ref URL] [--note TEXT]`
  records the verdict of a review delegated outside Lanefather (fathers#256) without a report file, bound to the lane's
  tip and tree like a review Lanefather ran, so `land`, `promote` and `done` take it without `--force-reason` and a
  later commit makes it stale. `changes_requested` becomes one P1 finding to mark; `--verdict` with a report file is
  checked against its findings. `show` and `status` name a recorded review's verdict, reviewer and ref
  (`review.recorded` in JSON). External `--reviewer` names outside the models file are restricted to letters, digits
  and `. _ + -` (at most 64 characters) for filename safety.
- Historical review evidence recovery without rerunning completed reviews (fathers#179):
  - `lanefather review <id> --recover [--source URL...] [--target REF] [--reviewed-range BASE..TIP]` fetches historical
    review dispositions from explicit GitHub issue/PR comment URLs (or auto-discovers them from linked tickets), verifies
    each named fix commit's git ancestry against the target ref, and checks reviewed/landed scope against the lane record
    (rejecting scope mismatches and reused lane IDs whose fix commits predate the lane's landed base).
  - Recovered receipts record `recovered: true`, `evidence_source: "historical"`, `complete`, `sources`,
    `reviewed_scope`, `landed_scope`, `dispositions`, `missing_facts` (never fabricating missing `model`, `reviewer`,
    `severity`, or `tree`), `recovered_by`, and `recovered_at`; any prior interrupted duplicate review is preserved under
    `preserved_artifacts`, complete executed reviews are refused without overwriting, and repeated `--recover` runs are
    idempotent.
  - Partial or unverified evidence (`PartialEvidence`) is recorded visibly as `recovered (partial)` and never satisfies
    `review_gate`, `promote`, or `done`; complete verified evidence satisfies the gate while remaining visibly
    distinguished in `list`, `show`, and `status` (text and JSON), with `done` preserving provenance. `show`, `status`,
    `review_gate`, `done`, and `review` surface `lanefather review <id> --recover` for landed lanes lacking an executed
    review before recommending a duplicate review.
- CLI convention conformance and versioned JSON replies (fathers#130):
  - Every `--json` reply now carries top-level `"v": 1`: added `"v": 1` to `list --json` and `show --json` (keeping
    legacy `"version": 1` beside `"v"` on `show --json` for existing readers), and added `--json` structured replies
    to `start`, `check`, `land`, `promote`, `done`, and `smoke`.
  - Unified the worker-budget flag to `--slots` across `check`, `land`, `recheck`, `smoke`, and `promote`, keeping
    `--jobs` as a compatible alias on all five commands.
  - Errors in `cli.main` now prefix with the configured program name instead of hard-coded `lane:`; unexpected
    non-`LaneError` exceptions print one clean `<prog>: internal error: <type>: <message>` line to stderr (exit 1)
    while recording the full traceback in the event log (`cli.internal_error`).
- Built-in verified-tree stamp ledger and `lanefather stamp` CLI (fathers#48):
  - Shipped `lanefather.stamp` with the tree-keyed ledger in `<git common dir>/<stamp_file>` (default
    `verified-trees`, 50-entry retention, atomic replacement via `fathers_common.atomic.atomic_write`, exclusive
    `flock` locking, and transactional `abort`/`complete` rollback journals).
  - Added `lanefather stamp` (`record`, `check`, `list`, `start`, `finish`, `abort`, `complete`) with `--json`
    (`"v": 1`) output, standard `Verified tree <tree> stamped.` / `Verified tree not stamped: <reason>.` lines
    parsed by `lanefather.verify`, and the `.md`-only equivalence rule (a tree differing from a stamped tree only in
    `.md` files counts as verified).
  - Added `[gate] stamp_tool = "builtin"` support in `lanefather.gate` and `lanefather.promote` (advertised as
    capability `verified-tree.stamp`) so external adapters such as Repo-Kit's `lanefather.mjs` and Straylight can
    use Lanefather's built-in stamp tool while preserving full backwards compatibility for custom `stamp_tool`
    scripts.
- Area-split and path-filtered overall reviews (fathers#155):
  - `lanefather review --overall <sha> --since <base> --paths GLOB...` (repeatable) restricts the diff and file list
    shown to the reviewer to paths matching the given globs, skips cleanly when no changed files match, and stores
    its receipt under a distinct `paths-<sha>-<digest>` label recording `paths`.
  - `lanefather review --overall <sha> --since <base> --areas FILE` reads named path groups from a TOML (`[areas]`
    table or top-level) or JSON file, reviews each area with matching diff changes separately under `ship-<sha>-<area>`
    (skipping empty areas), records `area`, `paths`, and `issue_label` on each receipt, and files one GitHub issue per
    area with findings labelled `area:<name>`. Use `--area NAME` with `--overall <sha>` to inspect, retry ticket filing,
    or mark (`--mark`) an area receipt's findings. Both `--paths` and `--areas` work with `--reviewer ALIAS|both`.
- Fix lane kind and atomic hand-over transaction (fathers#45 Part (b)):
  - Lane records now carry `kind: "lane" | "fix"` (`lifecycle.kind_of` defaults legacy records without `kind` to
    `"fix"` when `review_fix_of` is set, else `"lane"`), while preserving `review_fix_of` / `review_fix_of_all`
    for compatibility.
  - `lanefather review <id> --fix-lane` now executes one `lifecycle.hand_over` transaction under the `state` lock:
    clearing the parent's `worktree` and recording `handed_over`, registering the fix lane directly as `active`
    (`kind: "fix"`) so no `planned` lane ever holds a worktree, rolling back on write failure so an interrupted
    hand-over never leaves both lanes owning the worktree, and deriving `fix_lane` from lane records
    (`ReviewRecord.fix_lane`, with legacy receipt fallback) instead of writing a separate `fix_lane` field to
    `<id>.review.json`.
- Review run lifecycle under one lock with `(pid, start)` process identity (fathers#60): primary and delta review runs
  now record `start` (UTC `lstart` via `fathers_common.proc`) and their checkout path on `<label>.review.json`.
  Recycled PIDs with a mismatched start time read as `Died` (with bare-PID fallback preserved for legacy receipts
  without `start`), and a forced rerun (`--force-reason`) while another review is live records the earlier run as
  `superseded`, leaves its checkout intact until its process exits, and fences out the superseded worker by
  `(pid, start)`.
- Lane flow, outcomes, overrides, and review metrics in `lanefather stats` (fathers#47): `lanefather stats [--days N]
  [--lane ID] [--json]` now adds four structured blocks (`lane_flow`, `outcomes`, `overrides`, and `review`) alongside
  the existing `checks` and `reviewers` blocks under `"v": 1`, with matching human-readable sections. `lane_flow`
  reports p50/p90 durations from lane registration (`created`) to earliest green `lane.checked` event (falling back to
  a green check receipt), land (`landed.at`), and promote (`promoted.at` or earliest matching `promote.finished` /
  promote record), omitting lanes that have not reached a milestone rather than counting them as 0, plus per-phase run
  time vs Slotfather queue wait (`slotfather history export`) joined by `lease_id` (or `run`), reporting wait as
  unknown (`null` / `"unknown"`) whenever Slotfather is unavailable or has no matching lease. `outcomes` counts
  `landed`, `parked`, `abandoned`, `dropped`, `active`, and `planned` lanes (always including currently active/planned
  lanes); `overrides` summarizes `--force-reason` counts and reasons (overall, lane vs promote, and per promote batch
  under `batches`) plus single and batch fix lanes (`fix_of`); `review` reports findings per lane,
  fixed/rejected/unmarked shares, and review-gate held/overridden counts from `override` events.
- Umbrella tickets and partial land flags in the ticket lifecycle (fathers#64):
  - `[tickets] umbrella = [...]` accepts positive integer issue numbers or `owner/repo#N` / GitHub issue URL strings
    with strict type validation. Umbrella tickets never get `Fixes #N` in `merge_message` (using `Refs #N` instead),
    never get the `integrated` label in `after_land` (while still receiving a land comment noting they stay open),
    warn on manual `Fixes #N` commits (`ticket.trailer_conflict`), and are skipped by `promote --ship` / `shipped`
    release comments and label removal.
  - `lanefather land <id> --partial [--remaining TEXT]` treats all tickets of the lane as partial for that land:
    emits `Refs #N` instead of `Fixes #N`, skips the `integrated` label, includes `partial:` and `remaining: <TEXT>`
    in the land comment, records `partial: true` (and `remaining`) on `record["landed"]`, skips release comments for
    tickets that only landed partially, and causes follow-up `--fix-of` lanes to inherit partial scope.
- Stale review verdict and delta reviews (fathers#53): `ReviewRecord` / `derive_verdict` now binds finished lane
  reviews to the reviewed tip/tree and returns `Stale(reviewed_tip, landed_tip)` when a landed or active lane's own
  diff changed after the reviewed commit (ignoring clean integration-branch merges and valid marked pre-land fixes).
  When a lane lands with a stale review (or `lanefather review <id>` runs on a stale landed lane), Lanefather runs a
  delta review (`kind: "delta"`, `base: reviewed_tip`, `tip: landed_tip`) stored under `delta` in `<id>.review.json`
  with unified finding numbering so `review <id> --mark` marks findings from both rounds. `list`, `show`, and `status`
  report `review stale`; `promote`, `findings`, and `done` hold a stale lane until the delta review is complete and all
  findings are marked; and `fix_parent` / `--fix-of` / `review --fix-lane` refuse a stale parent until its post-review
  commits are covered while preserving eligibility when only clean integration merges followed the review.
- Risk-based review classes decided at lane start and checked at land (fathers#252):
  - `lanefather start --review none|evidence|full|double` stores `review_class` on the lane record (with optional
    `--review-decision`, `--gate` and `--review-reason`). At `land`, Lanefather computes the minimum required review
    class from the diff's changed paths and size under `[review.classes]` (`none`, `evidence`, `full`, `double`,
    `max_lines`, `max_files`, `audit_every`) and refuses a lane declared lower than computed (overridable via
    `--force-reason`, audited in the ledger).
  - `evidence` class lands without independent review when the lane carries both an owner decision (`--decision URL`
    or `--review-decision`) and a guarding check/test (`--gate`), and is refused at `land` if either is missing.
  - `lanefather review <id> --record <report file> --reviewer <alias>` records an external review (Markdown or JSON),
    parsing findings so they can be marked with `--mark` and satisfy the review gate.
  - Batch coverage: `lanefather review --overall <sha> [--since <base>] --lanes ID,ID [--record FILE --reviewer ALIAS]`
    covers multiple landed lanes as one diff; `review --overall <sha> --mark ...` marks its findings, and a clean or
    fully marked batch review satisfies each covered lane in `review_gate` / `promote`.
  - Anti-shirking audit sampling: `promote` lists every `none`/`evidence` lane with its class and reason, and
    deterministically samples 1 in `audit_every` (default 5) for a non-blocking post-hoc audit (`review <id> --audit`);
    P0/P1 findings on an audited lane emit a `review.audit_alert` naming the class that let it through.
  - Starting a lane whose id contains `review-fixes` without `--fix-of` is now refused at `start` (overridable via
    `--force-reason`) instead of merely printing a warning.
- Lane parking and abandoning as explicit outcomes (fathers#57): `lanefather park <id> --reason REASON` saves the
  lane's tip on `park/<id>`, frees its cap, claims and worktree, keeps its receipts, and transitions `planned|active
  -> done` with `outcome = "parked"` (`parked = {ref, tip, reason, at}`, event `lane.parked`). `lanefather unpark
  <id>` re-registers the lane from `park/<id>` preserving scope, claims and base without inheriting older receipts.
  `lanefather abandon <id> --reason REASON [--keep-branch REF] [--force-reason REASON]` transitions `planned|active
  -> done` with `outcome = "abandoned"` (event `lane.abandoned`), refusing unmerged commits unless `--keep-branch`
  or an audited `--force-reason` is given. `lifecycle.landed` checks `outcome` / `landed` metadata rather than
  `status == "done"` alone, so parked, abandoned, and legacy forced-`done` lanes never satisfy `--after` without
  `--force-reason` (dropped lanes still satisfy `--after`; `next` reports `waits for X (parked)` / `waits for X
  (abandoned)`), `done` on an unlanded lane names `park` and `abandon`, and `list --all` shows `done (parked)` /
  `done (abandoned)`.
- Pre-land lane review (fathers#242): `lanefather review <id>` works on an active lane with a clean worktree and
  commits beyond its base, recording `tip`, `tree` and `pre_land: true` on the review receipt. Findings can be fixed
  in the same lane and marked with `--mark N fixed:<sha|id>` or `rejected:<reason>` before landing. When the pre-land
  review is complete and every finding is marked, `land` counts the lane as reviewed without starting a post-land
  review or keeping the lane for review; `land` refuses (overridable with `--force-reason`) if the pre-land review is
  still running, incomplete, has unmarked findings, or if unreviewed commits were added after the reviewed SHA (while
  allowing clean integration-branch merges that leave the lane's own diff unchanged). `list`, `status` and `show`
  report `reviewed before land at <sha>`.
- `[checks.min_slots]` policy reporting and `default` fallback (fathers#247): `[checks.min_slots]` now supports a
  reserved `default` fallback key for running checks/suites matched by no other pattern or declared floor (advertised
  as `checks.min_slots.default`), is exposed under `policy.min_slots` in `lanefather capabilities --json`, and is
  documented in `docs/reference.md`.
- Post-merge smoke lifecycle and `lanefather smoke` command (fathers#56): `land` now records `post_merge.status =
  "running"` (`pid`, `start`, `checkout`, `lane_created`) before the post-merge smoke starts; a `running` smoke whose
  holder process has exited reads as `killed` via `fathers_common.proc`. `lanefather smoke <id> [--rerun | --accept
  REASON]` reports smoke state, reruns red, killed or missing smokes (keeping `lanefather recheck` compatible), or
  accepts them with an audited `override` entry (`status = "accepted"`, `reason`, `by`). `done` requires `green`,
  `skipped`, or `accepted` (or a cleared red smoke), refuses `running`, `killed`, `red` or missing results, and names
  `smoke <id> --rerun`, `smoke <id> --accept REASON`, or a follow-up lane; `list` and `status` display `post-merge
  running`, `post-merge killed`, and `post-merge accepted`.
- Recorded and resumable promote/ship runs (fathers#58): `lanefather promote` records each run's step outcomes and
  evidence in `<state>/promote/promote-<sha>.json`, including `ship_base` captured before push and per-step status
  for `[[promote.install]]` and `[[promote.deploy]]`. Rerunning an unfinished promote or `--ship` resumes from the
  first unfinished step and reuses the recorded `ship_base`, so a rerun after a failed local branch move still runs
  `release_comments` and `overall_review` over the original `ship_base..sha`, and a rerun of `--install` after a
  failed later step resumes from that step without repeating earlier succeeded steps.
- `lanefather list --all-projects` shows every repository's lanes in a shared Store, each row naming its project;
  `list --json` gives each lane its `project` (fathers#236).
- Gone lanes (fathers#237): a planned or active lane whose repository exists but whose branch and worktree are both
  gone is marked `gone` by `list`, `show` and `status` (text and JSON). `start` refused by the lane cap names the
  gone lanes it counted and the `lanefather drop A B` that frees them; refused by a claim overlap with a gone lane,
  it names the `drop` instead of `--after`. `drop` takes several ids. `gc` drops this repository's gone lanes, and
  `gc --dry-run` reports them; `start` never drops one itself.
  `drop` (and so `gc`) refuses a lane whose review has unmarked findings: a dropped lane is done and gets no fix
  round, while its findings go on holding lands.

- A lane can name all of its accepted scope (fathers#192, fathers#64): `start` takes `--ticket N` more than once for
  the tickets it completes and `--partial N` for those it does only part of, kept in the order given, and
  `--decision URL` and `--spec PATH` for the owner decisions and specs it answers to. On land the merge says
  `Fixes #N` for each ticket it completes and `Refs #N` for each partial one, every completed ticket is labelled
  `integrated`, and every ticket gets the land comment, a partial one with a note that it stays open. GitHub closes
  every completed ticket when the merge reaches the default branch, and `promote --ship` and `shipped` comment on
  each of them. `lanefather scope <id>` prints a lane's scope or adds to it while the lane is planned or active. A
  fix lane from `review --fix-lane` refers to its parent's tickets with `Refs` instead of closing them again. Land
  warns when a lane commit says `Fixes` for a partial ticket. A lane with one ticket keeps today's record, merge
  message and comments, and older Lanefather versions read every lane by its first completed ticket.

- A lane can name a ticket of another repository (fathers#238): `--ticket`, `--partial` and `scope`'s `--add-*` and
  `--mark-*` options take `owner/repo#N` or the issue's URL as well as a bare number, which stays the lane's own
  repository's and is stored as before. The merge says `Fixes owner/repo#N` for such a ticket the lane completes,
  which GitHub closes when the merge reaches this repository's default branch, and `Refs owner/repo#N` for a partial
  one; land's comment and `integrated` label, and the release comment, go to that repository. An older Lanefather
  does not read a foreign ticket: land such a lane with this version.

- The lane review reads the lane's whole accepted scope (fathers#192): every ticket's title and body without its
  comments, each decision's one comment and each spec as it stands at the review's base, so every panel member sees
  the same text. A partial ticket is marked so the reviewer does not report its other parts as missing; a land
  comment registered as a decision is refused; a long item is cut at 20,000 characters with a visible note. Scope
  the review could not read is listed for the reviewer, who reports it as a P3 finding. The review receipt records
  each item's source, version and the digest of the text shown (`scope`) and whether all of it was read
  (`scope_complete`); `review` prints `Scope reviewed:` and warns when the lane's scope changed since. `show --json`
  and `status --json` list every ticket of a lane's scope under `tickets`, beside the primary `ticket`.

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

- `lanefather --help` now uses the shared compact, grouped layout (`workflow`, `manage`, `release & system`, ≤ 80
  columns, ≤ 48 lines across Python 3.11–3.15) with no wrapped command names.
- Exit codes conform to `docs/cli.md` (fathers#130): `settings` read-only refusals moved from exit 4 to exit 5, and
  missing-lane lookups (`state.Store.load`, `show`) exit 6 (`LaneNotFound`).
- `lanefather show` (`show --json`) now reports `review.status = "stopped"` for a dead review instead of raw
  `"running"`, matching `lanefather status` (fathers#60).
- Unified check-receipt staleness under `receipts.check_verdict` using the results store's environment hash
  (`results.env_hash`, fathers#59): `land`, `check`, `list`, `show`, and `status` all evaluate check receipts through
  one verdict (`NoReceipt | Green | Red | StaleTree | StaleToolchain`), so a toolchain or platform change invalidates
  both lane receipts and results-store reuse for the same reason. `lanefather land` now refuses a green receipt whose
  toolchain or platform changed (or whose toolchain resolver refused) unless overridden via `--force-reason`, while
  read-only `list`, `show`, and `status` check tree currency only and never execute project toolchain resolvers.

- `lanefather land` post-merge `plan` mode now reuses passing whole-suite results from the lane's green check receipt
  instead of re-running every suite when the merge tree equals the lane's verified tree or when the diff between the
  lane tree and merge tree touches no input of a suite (fathers#249). When all planned suites are reused, post-merge
  settles as `skip` (`green`, `jobs=0`) without invoking the verifier and records provenance in `narrowing`
  (`dropped_suites`, `reused_suites`) and `lane_verified_suites` in the ledger. Reuse fails closed on missing, red,
  stale, stopped, or malformed receipts, full or unowned delta plans, delta plans that omit changed paths, and suites
  not passed as whole suites; in per-check post-merge narrowing, checks whose `depends_on`/`requires` rows point to a
  check selected by the delta or re-running also re-run transitively.

- `lanefather check` now runs dependency-blocked checks in the same invocation once their failed prerequisites pass
  their own rerun (`flaky-under-load`), instead of leaving those checks unresolved and the receipt red (fathers#240).
  Chained dependencies unblock transitively while sharing the `LANEFATHER_RERUNS` rerun budget across waves, and the
  combined receipt keeps each prerequisite's `flaky-under-load` classification and both logs visible. Checks whose
  prerequisite failed on rerun too, or that still depend on another failed check, stay `dependency-blocked` and keep
  the receipt red as before.

- `lanefather list` shows only the lanes of the repository it runs in, as its cap line counts them, not every
  project's lanes in a shared Store (fathers#236). `check`, `wait`, `land`, `review`, `done`, `drop`, `recheck`,
  `keep` and `scope` refuse another repository's lane and name where to run them: from the wrong checkout `drop`
  used to remove another project's lane. `show` still reads any lane.

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

- `lanefather promote`'s batch gate no longer freezes at `--jobs 1` when transient peer leases occupy the machine at
  launch (fathers#257): on multi-slot machines it now requests a floor of 2 slots (`GATE_MIN_JOBS = 2`, via
  `ranged_jobs`) at the gate's priority (`interactive` by default), waiting in the queue with no lease timeout for a
  second slot instead of running a full gate single-threaded, while clamping to 1 on single-slot machines and honouring
  explicit `--slots` / `--jobs` (pass `--jobs 1` to escape).
- The post-merge smoke no longer stays on the one slot `slotfather advise` named at launch on a busy machine
  (fathers#255): with Slotfather's `slot-range` it requests `1-3` slots like a lane check requests `1-2`, takes what is
  free when the lease is granted, and records the grant in `<id>.post-merge.json` (`jobs`, `jobs_decision.granted`).
- `hooks/reference-transaction` no longer refuses Git's automatic `pack-refs` (packing loose `refs/heads/main` /
  `refs/heads/dev` into `packed-refs` at their current OID and pruning the matching loose files) during agent
  sessions, while continuing to refuse creating, moving, or deleting protected branches—including `git branch -D` and
  `git update-ref -d` across loose, packed, and dual-stored refs (fathers#254).
- Scoped check runs and outer-lease / renamed-check base rerun handling (fathers#253):
  - `verify.Selection.checks(..., base=...)` now passes `--base <ref>` alongside `--checks` for scoped (non-full)
    lane checks and their solo reruns when the verifier advertises `"base"` in `capabilities` (or `CAPABILITIES`), so
    verifiers such as `package-kit-lane` can scope the selected checks against the base; verifiers without `"base"`,
    full runs, post-merge smokes, and base reruns omit `--base`.
  - `lanefather check`, `land`, and `promote` now detect when they are invoked inside an outer Slotfather lease
    (`SLOTFATHER_LEASE`, reading `SLOTFATHER_SLOTS` / `SLOTFATHER_COUNT` when set) and warn on stderr that they lease
    for themselves and must not be wrapped in `slotfather run --`; CLI help and docs state the same rule.
  - Base reruns without a Python `plan_module` now detect when a check name changed or does not exist on `base`
    (rejected by a short CLI unknown-check error, or omitted from a valid base report), probing multi-check selections
    individually so checks that exist on base still get their real verdict while absent/renamed checks are classified
    `regression` with `base.status = "absent"` (`the base defines no such check`), never `pre-existing`. Real crashes
    and tracebacks stay `unclassified`.
- Red checks are no longer classified `pre-existing` (landable) merely because the base rerun of the same check id
  also failed (fathers#113): `lanefather check` now requires valid, strictly-typed `failure_evidence` v1
  (`version: 1`, `comparable: true`, `context`, and complete `cases` with `id` and `signature`) across the original
  candidate run, its solo rerun (when run), and the base run, with identical `context`, matching candidate/alone
  failing cases, and every candidate failing case matched on base. Missing, non-comparable, malformed, interrupted,
  cleanup-failed, or exit-mismatched reports and unmatched candidate failing cases stay `unclassified` (RED);
  receipts and terminal output keep `raw_status` separate from the classified status.
- Planned lanes can now be activated with `lanefather start <id> --create` without repeating `--ticket` or `--claims`
  once every `--after` dependency has landed, creating the worktree from the current base and keeping the lane's
  existing scope and claims; duplicate `start` refusals on planned or active lanes now point at `start <id> --create`,
  `land <id>`, or `drop <id>` instead of `done`. Repeating `--after A --after B` now keeps every named dependency
  instead of only the last flag (fathers#241).
- Reusing a lane id after `done` no longer inherits the previous incarnation's check receipt, first receipt, review,
  post-merge smoke or recheck receipt (fathers#54): every lane receipt is stamped with `lane_created` from the lane
  record's `created` timestamp, and readers (`list`, `show`, `status`, `check`, `land` including promoted-branch
  fix-parent exemption checks, `review`, `done`, the review gate, and `start --fix-of` / `review --fix-lane` parent
  lookups) treat a `lane_created` mismatch as absent. Pre-upgrade unstamped receipts still match live lanes whose
  `created` is at or before the receipt's `started`/`finished` timestamp, while reused lane ids ignore older
  unstamped receipts.
- `lanefather check` (and direct verifier runs) now refuse cleanly, naming the missing or unrunnable verifier path and
  `[checks].verify` in `.lanefather.toml`, instead of crashing with an unhandled `FileNotFoundError` when the
  configured verifier script does not exist (fathers#244).
- A land forced past other lanes' unmarked review findings with `--force-reason` prints each finding it overrides
  (`lane: overridden: <finding>`, worded as the refusal) before merging; before, they reached only the override
  record and the ledger (review of fathers#172).
- `list`'s count of finished reviews with unmarked findings covers the repository it runs in, like its lanes and its
  cap line (`--all-projects` the whole Store), and `list --json` gives it as `pending_reviews` (each lane with its
  finding numbers); before, it counted every repository's reviews and was text-only (review of fathers#172).
- `drop`'s list of the paths a lane changes beyond its base, `land`'s merge conflict paths and
  `promote --ship`'s refused-directory check read paths with non-ASCII or unusual bytes as they are, not as Git's
  quoted escapes (`"caf\303\251.txt"`).
- A check that did not run because a check it depends on failed now names the check that actually failed behind it
  (fathers#195): when the report says it waits on another skipped check, Lanefather follows the chain to the failed
  prerequisite and says whether that one is a regression, pre-existing or flaky under load. The check output adds a
  line counting these checks, which take no rerun and count as neither passed nor regressed, and `land`'s refusal of
  a red receipt lists them with their prerequisite.

- Antigravity reviews run (fathers#201): Lanefather now finds `agy` where its installer puts it
  (`~/.gemini/bin/agy`) or the copy T3 Code bundles, and calls it the way the real CLI works: read-only plan mode in
  its sandbox, the prompt as a stream-json message on stdin, so a large diff no longer overflows the command line,
  and the answer read from the result event. An Antigravity error result, such as a used-up quota, or a run that
  changes the review checkout hands the review to the next choice. The receipt keeps the run's `session` and token
  `usage`. A models file's `[cli.antigravity] run` command is no longer used for reviews; `review` still is.

- A failing review panel member takes a stand-in (fathers#201): when a member's run fails, as on a used-up Claude
  quota, the next candidate not on the panel reviews in its place, its entry's `fallback` saying why; two failing
  members never share one. `--reviewer` still takes no stand-in.

- The implementer's family for `not_family` comes from the model the lane's agent recorded (fathers#201): a lane
  built in T3 Code with Claude counts as Anthropic, not Google, and T3 Code without a recorded model is unknown.
  T3 Code's `T3CODE_THREAD_ID` now names the agent session.

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
