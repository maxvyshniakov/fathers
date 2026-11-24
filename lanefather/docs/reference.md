# Lanefather reference

[Overview, requirements and quick start](../README.md)

## Commands

```
lanefather start <id> --ticket N --claims GLOB... [--ticket N|--partial N...] [--decision URL...] [--spec PATH...]
                 [--review none|evidence|full|double] [--review-decision TEXT] [--gate NAME] [--review-reason TEXT]
                 [--base REF] [--after ID...] [--fix-of ID,ID] [--create]
lanefather start <id> --create            activate a planned lane whose --after lanes have landed
lanefather scope <id> [--add-ticket N] [--add-partial N] [--add-decision URL] [--add-spec PATH]
                 [--mark-partial N] [--mark-complete N] [--json]   a lane's accepted scope
lanefather next [--start]                 the first ready entry of the ranked queue
lanefather adopt <id> <worktree> <branch> register the worktree an agent created
lanefather check <id>                     the checks the plan selects for the lane's diff -> receipt
lanefather wait <id> --for registered|landed|done [--timeout S] [--json]   block until the lane is there
lanefather land <id> [--into WORKTREE] [--keep] [--partial [--remaining TEXT]]
lanefather review <id> [--mark N fixed:<merge>|rejected:<reason>] [--fix-lane] [--record FILE --reviewer ALIAS] [--audit]
lanefather review --overall <sha> [--since <base>] [--lanes ID,ID] [--record FILE --reviewer ALIAS] [--mark N ...]
lanefather done <id>
lanefather done --finished [--dry-run]    done on every finished lane of the registered projects
lanefather drop <id> [<id> ...]           remove an active lane that adds nothing to its base
lanefather park <id> --reason REASON      park a lane on park/<id> and free its cap, claims and worktree
lanefather unpark <id> [--base REF] [--agent KIND] [--force-reason REASON]   resume a parked lane from park/<id>
lanefather abandon <id> --reason REASON [--keep-branch REF] [--force-reason REASON]   abandon an unlanded lane
lanefather recheck <id> [--slots N]       rerun a red post-merge smoke on the integration tip
lanefather smoke <id> [--rerun [--slots N] | --accept REASON]   post-merge smoke status, rerun or accept
lanefather keep <id> [--off]              auto-done and done --finished leave the lane
lanefather gc [--dry-run] [--json]         merged idle worktrees, unused branches and gone lanes
lanefather promote <sha> [--ship] [--no-push] [--install [--deploy HOST]]
lanefather sync [--drop-local]
lanefather perf <sha>
lanefather shipped <version> --since REF [--head REF]    comment a published version on the issues it fixes
lanefather stamp record|check [REV] [--token HEX32] [--json] | list [--json] | start | finish STATE | abort|complete TOKEN
lanefather list [--all] [--all-projects] [--json]   this repository's lanes
lanefather show <id> [--state-dir DIR] [--project-dir DIR] [--json]   read-only lane context
lanefather status [--json]                every registered project's lanes on this machine, read-only
lanefather stats [--days N] [--lane ID] [--json]   lane flow, outcomes, overrides, review and check health
lanefather settings [check KEY VALUE | set KEY VALUE [--revision R] | reset KEY|--all] [--json]
```

Several projects may share one Store (its `lanes` directory, fathers#149). `list` (text and `--json`) shows the
lanes of the repository it runs in, as the lane cap counts them; `--all-projects` shows every lane of the Store,
each row ending in `project: <name>` (fathers#236). Every lane object of `list --json` carries `project`, the
repository's main checkout directory name, and `gone` (below). `check`, `wait`, `land`, `review`, `done`, `drop`,
`park`, `unpark`, `abandon`, `recheck`, `smoke`, `keep` and `scope` refuse an id of another repository's lane, naming that repository and the command to
run there (`lane X belongs to sid-kit, not data-grid; run it there (<checkout>): cd <checkout> && lanefather drop
X`); nothing changes. `show` is read-only and names the lane's project, so it reads any lane of the Store; with
`--project-dir` it refuses another repository's lane. `status` lists every registered project's lanes by project.

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
`LANEFATHER_RERUN_WAIT` seconds; only the perf suite asks for the whole machine. When a lane renames or adds a check
that `base` does not define (whether detected from `plan_module` or from the base verifier rejecting or omitting the
unknown check id, fathers#253), it is classified `regression` with `base.status = "absent"` (`the base defines no such
check`), never `pre-existing`, and any other red checks that do exist on `base` still run there for their own verdicts.

Only checks that ran and failed rerun: the first `LANEFATHER_RERUNS` (default 5) of them, in report order, and the
rest stay `unclassified`. A check the report marks `blocked`, or `skipped` with a non-empty `blocked_by`, did not run
because a check it depends on failed (fathers#195). It takes no rerun, is classified `dependency-blocked` (never a
regression), names its `blocked_by` and the `prerequisites` that actually ran and failed behind it (followed through
other blocked checks) with their class, and keeps the receipt red until it runs and passes; `land` names it in its
refusal.

When a failed prerequisite passes its own rerun (`flaky-under-load`), `check` continues in the same invocation and runs
every blocked check whose failed prerequisites all passed on rerun (fathers#240). Chained dependencies unblock
transitively, sharing the `LANEFATHER_RERUNS` rerun budget across waves. The receipt combines the runs and keeps each
prerequisite's `flaky-under-load` classification (with both logs) visible so a receipt that went green only through a
solo rerun says so. If a prerequisite's rerun fails too, or another prerequisite of a blocked check stayed red, the
blocked check stays `dependency-blocked` and the receipt stays red.

`land` requires a current green check receipt for the lane's tip tree under the current toolchain and platform
(`results.env_hash`, fathers#59). If the tree changed, the toolchain or platform changed, or the toolchain resolver
refused, `land` refuses and asks to rerun `check` (`--force-reason` overrides and is logged; `land --repair` skips the
toolchain check for a merge that already landed). Read-only commands (`list`, `show`, `status`) check tree currency
only and never execute project toolchain resolvers.

`review <id> --fix-lane` starts the fix lane from the branch the lane landed on, which holds its merge, and refuses
when no branch does; the landed lane keeps its own registration. In one atomic `hand_over` transaction under the
`state` lock, it clears the parent's `worktree` (recording `handed_over`), registers the fix lane directly as `active`
with `kind: "fix"` holding that worktree (so no `planned` lane ever holds a worktree), and rolls back on write failure
so an interrupted hand-over never leaves both lanes owning the worktree (fathers#45). The fix lane takes over the
landed lane's scope with every ticket partial, so its merge says `Refs #N` and closes none of them a second time
(fathers#64).

A fix lane is exempt from review at land (`review.exempted`): the one `review <id> --fix-lane` registers for one
lane, and a batch fix lane started with `start <id> --fix-of A,B,C` for the findings of one or more landed lanes
(fathers#174). Both record `kind: "fix"` (normal lanes record `kind: "lane"`, and legacy records without `kind` read
as `"fix"` when `review_fix_of` is set, else `"lane"`). Each parent needs a finished review with at least one unmarked
finding. The record keeps the first parent's ID in the string `review_fix_of` for older Lanefather versions. The
complete batch is in `review_fix_of_all`. Mark each parent's findings with the fix lane's merge
(`review A --mark N fixed:<fix lane id>`).
A lane started with `--fix-of` has its own worktree, counts toward the cap like any lane, and runs its own check
plan even with one parent. `start` refuses an id containing `review-fixes` that names no parent (`--fix-of`,
overridable with `--force-reason`; fathers#252). A batch fix lane's
scope is what its own `--ticket` and `--partial` name: `--partial N` keeps its merge from closing N a second time,
and any ticket a `--fix-of` parent landed partially (via `land --partial` or a partial scope entry) automatically
inherits partial scope on the fix lane (fathers#64).

### Accepted scope

A lane names everything it answers to at `start`, in order (fathers#192): `--ticket N` for each ticket it completes,
`--partial N` for each it does only part of (the two keep the order they are given in), `--decision URL` for an owner
or coordinator decision, the URL of one issue comment (`https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<id>`),
and `--spec PATH` for a spec file relative to the repository. A decision names the owner's or coordinator's answer,
never an implementer's report. `start` refuses `--ticket none` beside another ticket, a ticket, decision or spec
named twice, a decision that is not one issue comment's URL, and a spec path that is absolute or has `..`. A lane
needs `--ticket` (`none` for no ticket) or `--partial`.

A ticket is a number of the lane's own repository (`N` or `#N`, stored as `N`, as always), or a ticket of another
repository (fathers#238): `owner/repo#N`, or its issue URL `https://github.com/<owner>/<repo>/issues/<n>`, both stored
as `owner/repo#N`. `start` refuses any other form, such as a pull request's URL or a comment's. The same number in
two repositories is two tickets. A foreign ticket the lane completes gets `Fixes owner/repo#N` in the merge, a
partial one `Refs owner/repo#N`: GitHub's closing keywords take `KEYWORD OWNER/REPOSITORY#ISSUE-NUMBER` for an issue
in a different repository, and a closing keyword in a commit message closes the issue when the commit is merged
into the default branch ([Linking a pull request to an issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue)),
here this repository's. Its land comment, its `integrated` label and its release comment go to that repository
(the comment links the merge in this one), and the lane review reads it there. An older Lanefather does not read a
foreign ticket (its scope reads as malformed), so land such a lane with a version that does.

The lane's primary ticket is the first ticket it completes, else its first partial ticket, else `none`; the record's
`ticket`, the brief header's first line, `list`, `show`, `status` and the ticket-comment check of `done` name it. The
brief header adds a `Scope:` line for a lane with more than that one ticket.

`lanefather scope <id>` prints the scope; its options change a planned or active lane's under the record's lock and
log `lane.scope_changed`: `--add-ticket`, `--add-partial`, `--add-decision` and `--add-spec` append, and
`--mark-partial N` and `--mark-complete N` change a ticket it already names. A landed lane's merge has already named
its tickets, so its scope does not change. `--json` prints `{"v": 1, "lane", "ticket", "scope"}`.

### What the lane review reads

The lane review reads the whole accepted scope once, after it fixes the range it reviews and before any reviewer
starts, so every panel member gets the same text (fathers#192). Each ticket gives its number, title, body, URL,
state and update time, never its comments: those hold the implementer's reports and land comments, and the review
stays blind to them. A partial ticket is marked so the reviewer reviews the part the lane does and does not report
the rest as missing. A decision is read as its one registered comment (body, author, update time) and is never an
implementer's report: a land comment registered as a decision is refused. A spec is read as it stands at the
review's base, the merge-base of the lane with its integration branch, never from the lane tip or a worktree; a spec
that is not there (one the lane adds) is not accepted scope, and a directory, submodule, symbolic link or non-UTF-8
file is not readable. Each item's text is cut at 20,000 characters with a visible `[truncated at 20000 chars]` note.

The reviewer sees each item with its source (URL, or `<path>@<base>`), its version (update time, or the spec's
blob id) and the digest of exactly the text shown. An item the review could not read (`missing`, `unreadable` or
`refused`) is listed under `Unavailable scope`, and the reviewer reports one P3 finding naming it. The review
receipt keeps `scope`, one entry per item: `item` (as in the record), `status` (`ok`, `truncated`, `missing`,
`unreadable` or `refused`), `source`, `version`, `digest` (`sha256:<hex>`) and, when there is one, `reason`; and
`scope_complete`, true when every item was read whole or truncated. `review <id>` prints `Scope reviewed:` with what
was not seen, and a warning when the lane's scope changed since the review. An incomplete scope does not hold the
gate yet; it shows in the output and the receipt. A lane without a ticket, decision or spec reads `The lane has no
ticket.`

### Review before landing

`lanefather review <id>` also works on an active lane with a clean worktree and commits beyond its base, beside
`lanefather check` (fathers#242). The review receipt records `tip`, `tree` and `pre_land: true`. Findings can be fixed
in the same lane and marked with `--mark N fixed:<sha|id>` or `rejected:<reason>` before landing.

- When a lane's pre-land review is finished and every finding is marked, `land` counts the lane as reviewed: no
  post-land review is started and the lane is not held for its review (unless `--keep` was passed).
- `land` refuses if the pre-land review is still running, incomplete, or has unmarked findings, or if the lane has
  unreviewed commits after the reviewed SHA that are not referenced by a `fixed:<sha>` mark (a clean merge of the
  integration branch that leaves the lane's own diff unchanged does not invalidate the review). `--force-reason`
  overrides and is logged.
- `list`, `status` and `show` display `reviewed before land at <sha>` for a lane with a completed pre-land review.

### Stale reviews and delta reviews

A finished lane review is bound to the tree it reviewed (fathers#53). If a lane's own diff at its landed or active tip
differs from the reviewed commit — ignoring clean integration-branch merges and valid marked pre-land fixes — the
review verdict is `Stale(reviewed_tip, landed_tip)`:

- `list`, `show`, and `status` report `review stale` (and `review.status = "stale"` in JSON).
- `promote`, the batch gate, `findings`, and `done` hold a stale lane until a delta review of
  `reviewed_tip..landed_tip` completes and all blocking findings across both the primary and delta reviews are marked
  (`--force-reason` overrides `promote` and `findings`, logged).
- Landing a lane with a stale review (or running `lanefather review <id>` on a stale landed lane) starts a delta review
  (`kind: "delta"`, `base: reviewed_tip`, `tip: landed_tip`) stored in `<id>.review.json`'s `delta` field. Its findings
  continue the primary review's numbering so `lanefather review <id> --mark <n> ...` marks findings from either round.
- A parent lane with a `Stale` review is not eligible for `--fix-of` or `review --fix-lane` (nor exempt via
  `review_exemption`) until the delta review covers its post-review commits; clean integration merges after the review
  preserve eligibility.

### Review run lifecycle and process identity

Every review receipt write (`start`, `finish`, `fail`, `exempt`, `record`, `mark`, `delta_*`) goes through
`ReviewRecord._transition`, and stale-receipt clearing (`clear_stale`) takes the same `review-<label>` lock directly
(fathers#60, fathers#74):

- Running primary and delta review receipts record `(pid, start)` process identity (UTC `lstart` via
  `fathers_common.proc`) and their detached checkout path. Readers verify `(pid, start)` so a recycled PID with a
  different start time reads as `Died` (falling back to bare PID liveness only for legacy receipts without `start`).
- `show --json` and `status --json` both report `review.status = "stopped"` for a dead review (never raw `"running"`).
- A forced rerun (`--force-reason`) while a review is still running marks the earlier live run in `superseded` on the
  receipt, leaves its detached checkout intact while its process is still alive (removing only dead/stale checkouts),
  and fences out the superseded worker with `StaleReviewRunError` if it later tries to finish or fail.

### Review findings before landing

`land` refuses if another lane in the same repository has a finished review with an unmarked P1 or P2 finding
(fathers#177). The refusal gives the exact commands to mark each finding and shows how to start the fix round.
Unmarked P3 findings are printed as warnings. A running or missing review can finish alongside later lands.
A fix lane may land while its valid parents have unmarked findings. A finished review of an unrelated lane can
still hold the merge. When several landed reviews need a fix, the refusal suggests one batch with `--fix-of`.
`--force-reason` prints each blocking finding it overrides (`lane: overridden: ...`) and logs the reason and the
findings in the override record.
The text output from `list` ends with the count of finished reviews with unmarked findings and their
`lane#number` references, for the repository it runs in (`--all-projects`: the whole Store); `list --json` gives
them as `pending_reviews`, each `{"lane": id, "findings": [n, ...]}`. The separate batch gate uses its configured `[review] blocks` severities.

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

None of these skips a refusal of `done`: an uncommitted change, an agent's lock, whatever Spawnfather keeps in the
worktree, the build directories or the scratch (a process working there, ignored files outside `[gc] disposable`),
a branch or a worktree HEAD with commits not in the integration branch, a missing integration worktree, another lane
that is not done working in the same worktree (a fix lane), or a missing ticket comment leaves the lane where it is.
Unattended (auto-done, `done --finished`, the repair), `done` also re-reads `keep` under the state lock. A lane or
project that cannot be read or finished is reported as refused, and the others go on.

### Spawnfather removes what a lane owns

A lane owns its worktree, its scratch and its build directories through Spawnfather (fathers#171), as the owner
`lane:<main checkout>/<id>`: `start`, `adopt` and the fix-lane hand-over register them (`spawnfather own-resource`;
a failure is a warning and a `lane.unregistered` ledger entry), and `done`, `drop` and `gc` register what the lane
record names again before they ask for a removal, so a lane started before the upgrade is covered. `done` and `drop`
ask `spawnfather reap --owner lane:...` with the integration tip (done) or the base (drop) as `--base`, `[gc]
disposable` as `--disposable`, a `--force-reason` as `--override`, and `--stop-inside` when `[done] stop_inside` is
true: first as a dry run under the state lock, whose refusals join done's, then for real outside it. A reap
Spawnfather refuses leaves the lane landed; done again retries. `gc` asks `spawnfather reap --worktree PATH` for each
candidate its policy lets through. Without a Spawnfather that offers lifetimes v1, `done` (interactive too), `drop`
refuse with exit 3 and `gc` keeps every candidate; no override lifts that. The lane's own throwaway checkouts (a red
check's base checkout, a review's, the smoke's and the gate's) and the shared cache partitions stay Lanefather's.

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
- `lanefather park ID --reason REASON` (fathers#57) saves a planned or active lane's current branch tip on
  `park/<id>`, frees its lane cap, claims, worktree and lane branch, keeps its receipts, and transitions the record
  to `done` with `outcome = "parked"` and `parked = {"ref", "tip", "reason", "at"}` (`lane.parked`). A parked lane
  never counts as landed: `next` skips dependants with `waits for ID (parked)`, `start --create` and `land` refuse
  dependants, and `list --all` shows `done (parked)`. `lanefather unpark ID [--base REF]` re-registers the lane
  from `park/<id>`, preserving its scope, claims and base and creating a fresh worktree; its new `created`
  timestamp ensures older receipts are not inherited (fathers#54).
- `lanefather abandon ID --reason REASON [--keep-branch REF] [--force-reason REASON]` (fathers#57) transitions a
  planned or active lane to `done` with `outcome = "abandoned"` (`lane.abandoned`) and removes its worktree, branch,
  scratch and claims. It refuses when the branch has commits not merged anywhere unless `--keep-branch park/<id>`
  saves the tip or `--force-reason` overrides (audited via `override`). An abandoned lane never counts as landed
  (`waits for ID (abandoned)` in `next`, `done (abandoned)` in `list --all`). `done` on an unlanded lane names
  `park` and `abandon`.
- A lane is *gone* when it is planned or active, its repository exists, it had a worktree, and neither that
  worktree (as a directory) nor its branch is left: a batch whose branches were deleted by hand leaves such lanes,
  and they hold the lane cap and their claims although they can never land (fathers#237). A planned lane that never
  had a worktree is not gone. `list`, `show` and `status` mark it (`gone: no branch or worktree`; `"gone": true` in
  their JSON), and `list` ends with the `drop` command that frees them. `start` refused by the cap names the gone
  lanes among the ones it counts with `lanefather drop A B`; refused by a claim overlap with a gone lane it names
  the `drop` instead of `--after`, which would wait forever. `drop` takes several ids and drops each in turn; one
  that refuses is reported and the others go on. `lanefather gc` drops this repository's gone lanes (`dropped lane
  X: its branch and worktree are gone`; in `--json` a `removed` item `{"kind": "lane", "path": "X"}`), and `gc
  --dry-run` only reports them. `start` drops none itself: a worktree on a volume that is not mounted, or one being
  recreated, looks gone for a moment, and registering a lane should never remove another agent's; the refusal
  names the command instead.
- A landed lane whose post-merge smoke went red for a reason outside it (the smoke checkout's environment) is cleared
  by a later green run on the same integration branch, on a commit that holds the lane's merge and that the
  integration worktree's HEAD holds: the batch gate there (its `promote-<sha>.gate.json`, the full gate), or another
  lane's green post-merge smoke that ran each check the red smoke ran on its own tree (a result it took from another
  tree, such as the lane's receipt or the previous tip, does not count). The run must not have finished before the red
  smoke (the stamps' one-second resolution: within its second counts) and ran on its platform (a gate's platform comes
  from its receipt, or for an older receipt from its results-store entry). `done` and `done --finished` look for that
  evidence themselves, nothing runs, and the oldest receipt that clears the smoke is named in the ledger (`lane.done`,
  details `post_merge_cleared_by`) and in the record (`post_merge.cleared_by`).
- Before a post-merge smoke starts, `land` records `post_merge.status = "running"` with `pid`, `start`, `checkout` and
  `lane_created` (fathers#56). If that holder process exits before recording a result, readers resolve it as
  `killed`. `done` requires `green`, `skipped`, or `accepted` (or a red smoke cleared by a later green run) and
  refuses `running`, `killed`, `red`, or a missing `post_merge` result (unless `--force-reason`). `list` and `status`
  report `post-merge running`, `post-merge killed`, and `post-merge accepted`.
- `lanefather smoke <id> [--rerun [--slots N] | --accept REASON]` inspects or resolves a landed lane's post-merge
  smoke: `--rerun` reruns a red, killed, or missing smoke (`recheck` stays compatible for red smokes); `--accept
  REASON` accepts a red, killed, or missing smoke with an audited `override` ledger entry (`post_merge.status =
  "accepted"`, `reason`, `by`) so `done` passes without `--force-reason`.
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
| `[checks]` `verify`, `plan_module`, `triage`, `toolchain`, `toolchain_name`, `slot_env` | the check contract below |
| `[review]` `blocks` | the unmarked review finding severities that hold the batch gate, promote and `findings` (default `["P1", "P2"]`; fathers#109) |
| `[review]` `panel`, `panel_required` | the review panel (fathers#151): two or more model aliases of the models file that review every lane at once; `panel_required = true` holds the gate while a member gave no answer ([Reviewer configuration](#reviewer-configuration)) |
| `[review.classes]` `none`, `evidence`, `full`, `double`, `max_lines`, `max_files`, `audit_every` | risk-based review classes decided at `start --review` (stored as `review_class` on the lane record) and checked at `land` (fathers#252): path globs per class (`double` and `steering` checked first, then `evidence`/`none`, then `full`), diff size bounds (`max_lines`, `max_files`; binary files escalate `none`/`evidence` when `max_lines` is set), and 1-in-N (`audit_every`, default `5`, `0` disables) non-blocking post-hoc audit sampling of `none`/`evidence` lanes at `promote` (emitting `review.audit_alert` on P0/P1 findings) |
| `[checks]` `gate_only` | check-id patterns (fnmatch) the post-merge smoke leaves to the batch gate, such as tooling self-tests (fathers#94); a tree whose plan names whole suites has no check ids to match, so its smoke runs them |
| `[checks.min_slots]` | table mapping check-id or suite fnmatch patterns (or the reserved `default` fallback key, not a literal suite/check name pattern, for checks/suites matched by no other pattern or declared floor) to positive integer minimum slot floors for lane checks and red-check reruns (fathers#173, fathers#247); raises `<slot_env>_MIN_JOBS` and `--jobs` for matching selections (clamped at the machine's total slot capacity when known, while `--slots` still overrides), leaves unmatched selections at `default` (or `1` when `default` is omitted), and is reported under `policy.min_slots` in `capabilities --json` (capability `checks.min_slots.default`) |
| `[lane]` `worktrees_root`, `targets`, `target_label`, `cache_partitions`, `cache_depfiles` | managed worktree root (default `.claude/worktrees`), per-lane build directories and label, and shared cache partition depfile globs (default `debug/*.d`, `release/*.d`) |
| `[gate]` `stamp_tool`, `stamp_file`, `pre_push_hook` | the verified-tree stamp of the full gate (`stamp_tool = "builtin"` uses Lanefather's built-in `lanefather stamp`, fathers#48; a script path keeps custom adapter compatibility) |
| `[promote]` `branches`, `install`, `deploy`, `after`, `ship_refuses` | promoted branches (default `["dev", "main"]`; fathers#131) and what promote runs after moving them |
| `[gc]` `dirs`, `branches`, `collect_uncommitted_sessions`, `disposable` | what `gc` collects (fnmatch patterns, fathers#44). A worktree under the main checkout's `.claude/worktrees` is a candidate when a `dirs` pattern matches its name (a pattern without `/`) or its path from the main checkout (`.claude/worktrees/*`), or a `branches` pattern matches its branch. Every candidate must be clean, idle for 2 h, unused by any process, owned by no open lane, unlocked (a `claude agent` lock counts only while its pid is dead), and hold no other worktree or Git repository anywhere below it. An agent or lane worktree (`agent-*`, `lane-*`, `worktree-agent-*`, `lane/*`) must be merged into origin/dev, origin/main or a local `integrate/*`. Any other candidate is a session worktree: its HEAD must be in origin/dev or origin/main, it must have a commit of its own in its HEAD reflog unless `collect_uncommitted_sessions = true`, and every ignored path in it must have a component that `disposable` names (default `node_modules`, `__pycache__`, `target`, `.build`, `dist`, `build`, `.venv`, `.turbo`, `.next`, `storybook-static`, `coverage`, `.pytest_cache`, `.ruff_cache`, `.DS_Store`). A merged branch is deleted only when `branches` names it, no worktree or lane uses it and it is merged as its family requires; any other branch outlives its worktree. Defaults: `dirs = ["agent-*", "lane-*", ".claude/worktrees/*"]` (Codex's `codex-*` worktrees are under `.claude/worktrees`), `branches = ["worktree-agent-*", "lane/*", "claude/*", "codex/*"]`; `dev`, `main` and `integrate/*` are never collected. |
| `[gc]` `after_remove`, `after_remove_report` | project cache cleanup after removal (`{worktree}`, `{name}`, `{project}`). Stdout is ignored by default, preserving existing hooks. With `after_remove_report = true`, a successful hook must emit `{"v":1,"freed":123}` to add its actually removed bytes; diagnostics go to stderr. Invalid reports add zero bytes and a report error. |
| `[slots.priority]` `check`, `post-merge`, `gate`, `rerun`, `perf`, `default` | each run's Slotfather priority; see [slot priorities](#slot-priorities) |
| `[admission]` `provider` | `legacy` by default; explicit `slotfather` uses the versioned lifetime-admission query, separately from execution jobs. See [rollout contract](../docs/admission.md). |
| `[admission]` `cap` | this repository's lane cap (fathers#149), a positive whole number. First set wins: `LANEFATHER_CAP`, then `[admission] cap`, then the cap derived from the machine; an invalid `LANEFATHER_CAP` warns and falls back to the next. An exported `LANEFATHER_CAP` overrides every project's cap. The cap counts the planned and active lanes of this repository only; `LANEFATHER_GLOBAL_CAP`, when set, is a ceiling over every project's lanes in the state directory together. A fix lane (`review --fix-lane`) registers outside both (fathers#45); after that it counts like any other lane. |
| `[done]` `auto` | `true` by default: a lane that becomes finished runs `done` by itself, and start below the disk minimum runs it on finished lanes ([Finishing lanes](#finishing-lanes)) |
| `[done]` `stop_inside` | `done`, `drop` and `gc` have Spawnfather stop the processes left working in what they remove (no terminal's or agent session's); unset, it follows `[hooks] process_scan` |
| `[tickets]` | the ticket lifecycle below; every rule is off without it |
| `[hooks]` `process_scan` | deprecated (fathers#171): no longer loaded; when set and `[done] stop_inside` is not, it turns `stop_inside` on with a warning |
| `[hooks.project]` | named project policy scripts run by `project-hook NAME -- ARGS` in the installed interpreter |

### Slot priorities

Every verification run lanefather starts asks Slotfather for slots at its phase's priority. `lanefather check`,
`lanefather land`, and `lanefather promote` lease slots for themselves and must be run directly, never wrapped in
`slotfather run --`: an outer lease (especially a 1-slot lease) pins the whole command and its verifier to the outer
lease's slot count, and Lanefather warns on stderr when it detects one (fathers#253). The built-in
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

### Verified-tree stamp

Lanefather owns the verified-tree stamp ledger and CLI (`lanefather stamp`, capability `verified-tree.stamp`,
fathers#48). A full verification run stamps the committed Git **tree** hash (not commit SHA) in
`<git common dir>/<stamp_file>` (default `verified-trees`), so every worktree of the clone shares the ledger and
commit-message edits, amends, or rebases that leave the tree identical stay verified.

- **Ledger format**: one space-separated row per stamp, retaining the latest 50 rows (oldest evicted first), written
  atomically under an exclusive `flock` on `<stamp_file>.lock`:
  ```text
  <tree> <commit> <utc-iso8601-timestamp>[ <32-hex-token>]
  ```
  When a 32-hex transaction token is supplied (`--token`), rows evicted by that write are journaled in
  `<git common dir>/verified-tree-transaction-<token>.json` before publication so `lanefather stamp abort <token>`
  rolls back only that run's stamp and restores evicted rows in order; `lanefather stamp complete <token>` removes
  the journal.
- **`.md`-only equivalence rule**: `lanefather stamp check [REV]` (and `gate.verified` with `stamp_tool = "builtin"`)
  exits 0 when `REV`'s tree is in the ledger (`match = "exact"`) **or** when `git diff-tree -r --name-only` against
  any stamped tree in the ledger contains only `.md` paths (`match = "md-only"`). Any non-`.md` path change
  invalidates the stamp (exit 1).
- **CLI subcommands**:
  - `lanefather stamp record [REV] [--token HEX32] [--file NAME] [--json]`: records `REV`'s tree (default `HEAD`)
    and prints `Verified tree <tree> stamped.` (the line `lanefather.verify` parses).
  - `lanefather stamp check [REV] [--file NAME] [--json]`: exits 0 if `REV` (default `HEAD`) is verified (exact or
    `.md`-only), 1 otherwise. `--json` prints `{"v": 1, "verified", "rev", "tree", "match", "matched_tree",
    "md_paths", "ledger"}`.
  - `lanefather stamp list [--file NAME] [--json]`: lists stamped rows (`--json` prints `{"v": 1, "ledger",
    "entries"}`).
  - `lanefather stamp start` / `finish STATE [--token HEX32] [--json]`: captures `HEAD`'s tree and clean/dirty state
    before a full verification run and stamps only if tracked files stayed clean and `HEAD`'s tree stayed unchanged
    (otherwise printing `Verified tree not stamped: <reason>.`).
  - `lanefather stamp abort TOKEN` / `complete TOKEN`: rolls back or finalizes a tokenized stamp transaction.
- **Project configuration & external adapters**: set `[gate] stamp_tool = "builtin"` in `.lanefather.toml` to use
  Lanefather's built-in stamp check in `lanefather promote` and `gate.verified` without a repo-local shell script.
  External adapters (such as Repo-Kit's `lanefather.mjs` or Straylight's `verify.sh`) run `lanefather stamp start`
  before a full verification run and `lanefather stamp finish "$snapshot"` (or `lanefather stamp record`) on green
  completion, emitting the standard stamp line for `lanefather.verify`. A repository's `.githooks/pre-push` hook
  enforces the rule by running `lanefather stamp check "$local_sha"` for each pushed ref. Projects that configure a
  custom script path in `[gate] stamp_tool` keep full backwards compatibility.

### Recorded promote runs

Each `lanefather promote` run records its step outcomes, `ship_base` (captured before push), and per-step
`[[promote.install]]` / `[[promote.deploy]]` status in `<state>/promote/promote-<sha>.json` (fathers#58). Concurrent
promotes of the same SHA serialize on a per-SHA lock so they cannot interleave or duplicate release comments. Rerunning
an unfinished promote for the same SHA resumes from the first unfinished step and reuses the recorded `ship_base` when
`--ship`, `--no-push`, the remote, and the promoted branches match, so a rerun after a failed local branch move still
runs release comments and the overall review over the original range, and a rerun of `--install` after a failed later
step resumes from that step without repeating earlier succeeded steps. A non-`--ship` rerun refuses to overwrite an
unfinished pushed `--ship` record; a damaged or unreadable record is refused rather than overwritten; and a completed
run starts fresh on a subsequent `promote` invocation.

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
fixes_trailer = true      # the landing merge's message ends with `Fixes #N` per completed ticket, `Refs #N` per
                          # partial one: GitHub closes each completed ticket when it reaches main
release_comment = true    # promote --ship and `shipped`: comment main's SHA or the version on each fixed issue
                          # and remove its `integrated` label
preview = "https://preview.example.com/{short}"   # optional; {sha} {short} {branch} {ticket} {lane}; {ticket}
                                                  # is the primary ticket
repo = "owner/name"       # optional; default: origin's GitHub repository
umbrella = [347, "owner/repo#97"]  # optional; umbrella/epic tickets that never close, never get `integrated`,
                                   # and never get release comments (fathers#64)
```

The rules cover every ticket of the lane's [accepted scope](#accepted-scope). The merge ends with one line per
ticket, in scope order: `Fixes #N` for a ticket it completes, `Refs #N` for a partial or umbrella ticket (`Fixes
owner/repo#N` and `Refs owner/repo#N` for [another repository's](#accepted-scope)); a ticket that a given
`land -m` message already names on a `Fixes` or `Refs` line of its own keeps that line. The default title names
every ticket: `Merge lane/<id> (#A, #B)`. After a green smoke, land labels each completed ticket `integrated` and
comments on every ticket (`ticket.marked`, one event per ticket); a partial or umbrella ticket's comment says the
issue stays open and is not labelled. `lanefather land <id> --partial [--remaining TEXT]` marks every ticket of the
lane partial for that land (emitting `Refs #N`, skipping `integrated`, recording `partial: true` and optional
`remaining` on `landed`, and noting `remaining: <TEXT>` in the land comment); follow-up `--fix-of` lanes inherit
partial scope from a partially landed parent. Before the merge, land warns about a lane commit, or a given message,
that says `Fixes #N` for a partial or umbrella ticket, since GitHub closes the ticket anyway when that commit reaches
`main` (`ticket.trailer_conflict`); a commit that already says `Refs #N` does not warn, and the land goes on.

`integrated` means done and landed in the integration branch, not yet on `main` or released. A merge origin does not
have yet (a remote-tracking branch, as last fetched, that contains it) is said to be local only and is not linked. A
red smoke leaves the issue unmarked for the fix's land. The issues a release closes are those a `Fixes #N` line in
the shipped range names: origin's `main` before the push to the SHA for `promote --ship`, `--since` to `--head` for
`lanefather shipped <version>`, which a project's release process runs after it publishes, skipping any umbrella
ticket and any ticket whose `Fixes` commits in that range all lie inside partial lanes (a completing lane or
external commit with `Fixes #N` in the same range overrides the partial skip). Each closed issue loses its
`integrated` label, which no longer holds once it is released. `gh` runs without a
prompt with `--repo`; a failure warns and goes to the ledger and never fails the land, promote or release. `start`
and `next --start` warn about a lane registered with `--ticket none` while any rule is on, and `done` does not count
land's comment as the lane's evidence. The capability is `tickets.lifecycle`.

### The check contract

```
<verify> --changed --base B --plan --json   {full, paths, unowned, checks: [{id, suite, paths, producer}], suites, reasons, capabilities?}
<verify> --checks A,B [--base B] [--only T1,T2] | --suites S --keep-going --jobs N
                                             prints `Verification report: <report.json>`
<verify> --keep-going --jobs N               the full gate; prints `Verified tree <tree> stamped`
```

The plan module (`plan_module`) answers `SUITES`, `checks(...)` and `smoke(root)`; `PERF = "<suite>"` names the perf
suite, and `CAPABILITIES` may advertise `"only"`. A project without one gets whole suites. In post-merge `plan` mode
(projects without `smoke(root)`), `land` reuses passing whole-suite results from the lane's current green check receipt
when the merge tree equals the receipt tree (skipping plan preparation and the verifier) or when the diff from the lane
tip to the merge touches no input of that suite (`narrowing.dropped_suites`, `narrowing.reused_suites`, and
`lane_verified_suites` in the ledger, fathers#249). Reuse fails closed on missing, red, stale, stopped, or malformed
receipts, full or unowned delta plans, delta plans that omit changed paths, and suites that did not pass as whole suites
(including pre-existing-classified failures). In per-check post-merge narrowing, checks declaring `depends_on`/`requires`
on any check selected by the delta or re-running also re-run transitively.

The report is `{checks: [{id, status, log, blocked_by, failed_tests?, outside_failed_tests?, failure_evidence?}], stopped, environment, capabilities?}`. Optional row fields feed the run
history (fathers#92): `seconds` (the check's duration; without it `stats` has no p50/p90 for the check), `started`
and `finished` (ISO times), `junit` (a JUnit XML file whose failed test cases a flake names) and `lease` (the slot
lease id, also allowed at the report's top level).

A verifier may opt into `pre-existing` failure classification with a `failure_evidence` v1 object on each failed
check row (fathers#113):

```json
{"version": 1, "comparable": true, "context": "verifier-v2/runtime-compatible/schema-v9",
 "cases": [{"id": "stable test identity", "signature": "stable assertion/cause identity"}]}
```

The producer owns these facts: set `comparable: true` only after validating required runtime, report schema,
setup and complete failing-case collection. `context` identifies compatible verifier/runtime/input conditions
(excluding expected candidate/base commit differences), and `cases` lists every failing case with unique stable
`id`s and non-empty `signature`s. Version 1 accepts exactly those four keys (`version`, `comparable`, `context`,
`cases`) and `{id, signature}` per case.

Lanefather classifies a red check as `pre-existing` only when the original candidate run, its failed solo rerun
(when run), and the base run all carry valid, strictly-typed `failure_evidence` v1 (`comparable: true`) with
identical `context`, identical candidate/alone failing cases, and every failing case on the candidate matched
(`id` and `signature`) on the base. Case order does not matter. Missing, malformed, non-comparable, interrupted,
environment-failed or cleanup-failed reports, duplicate check ids, reports whose exit code contradicts their rows
or actual process exit, failures outside any check, and candidate failing cases not matched on base all stay
`unclassified` (RED). Original `results` and `check_results` retain raw status; `raw_status`,
`classification.raw_status`, and terminal output distinguish raw RED from a classified GREEN receipt. Existing
solo-pass (`flaky-under-load`) classification is unchanged.

When a verifier advertises `"only"` (`capabilities`) and reports
`failed_tests: [...]` on a failing check (fathers#147), red-check reruns pass `--only <test,...>` to both the lane
and base reruns, record `"tests"` in `classification.checks` and `classification.runs`, and keep a check
`"unclassified"` if a red test is outside `failed_tests`; otherwise argv and receipts are unchanged. When a verifier
advertises `"base"` (`capabilities` or `CAPABILITIES`), scoped (non-full) lane check runs and their solo reruns also
pass `--base <ref>` alongside `--checks` so the verifier can scope the selected checks against the base (fathers#253);
base reruns omit `--base` because they run the check against `base` itself. Lanefather hands each run its run id and
tree in the slot meta
(`run`, `tree`) with `plan` and `checks` (fathers#107).

When Slotfather advertises `slot-range` via `slotfather capabilities --json` (fathers#43), lane checks pass the
maximum `--jobs` alongside `<slot_env>_MIN_JOBS` (`1`, or the highest selected `min_slots`) so the verifier can
request `slotfather run -n MIN-MAX` (and `slots.wrap` issues `-n MIN-MAX`), then record the granted worker count from
the verify report (`jobs`, `slots`, or `granted_jobs`) in the receipt (`jobs`, `jobs_decision.granted`) and run
history (`history.py`). When Slotfather does not advertise `slot-range`, the command arguments and receipts stay
unchanged.

During any quiet verify run (no complete output line for `LANEFATHER_VERIFY_HEARTBEAT` seconds, default `30`, `0` or
`off` to disable), the shared verify runner emits bounded heartbeat lines naming the state (`queued` when waiting in
Slotfather, `setup` during setup lines, else `active`), the active suite or check, the last-output age (separately from
process CPU and child-process activity), and any reliably observed test name without fabricating one (fathers#190).

## Reviewer configuration

Authenticate the reviewer CLIs you intend to use according to their own documentation. Without a models file
Lanefather prefers Codex (`gpt-6.1-sol`, medium effort) when it can determine at least 15% weekly quota remains;
otherwise it tries Claude (`claude-opus-5-5`, medium effort). A models file's review role sets the order across
Codex, Claude and Antigravity / Google (`gemini-4-neon`, `gemini-4-argon`, `gemini-4-carbon`, `gemini-3.8-flash`).
A failed or empty reviewer run can fall back to the next choice in the role. Quota,
provider availability and account charges remain outside Lanefather.

Set `LANEFATHER_CODEX`, `LANEFATHER_CLAUDE` and `LANEFATHER_ANTIGRAVITY` (or `LANEFATHER_AGY`) to explicit executable
paths if PATH discovery (`codex`, `claude`, `agy` or `antigravity`) is unsuitable; a set but empty value turns that
reviewer off. Antigravity is also found through `ANTIGRAVITY_CLI_PATH` or `AGY_CLI_PATH`, then on PATH, then where
its installer puts it (`~/.gemini/bin/agy`), then as the binary T3 Code bundles
(`~/.t3/tools/antigravity-acp/<platform>/versions/<release>/antigravity`, the release `active.json` names).

An Antigravity review runs `agy --model ID --effort E --mode plan --sandbox --disable-slash-commands --print-timeout
1740s --input-format stream-json --output-format stream-json -p=` in the frozen checkout, the prompt as one
stream-json user message on stdin (a large diff never rides on the command line) and the answer the `response` of
the stream's last `result` event. `agy --effort` takes `low` to `max`, so a `minimal` choice runs at `low`. A result
with `status: ERROR` (a used-up quota) fails the run with its error, as does a run that changes the checkout (`git
status` differs afterwards); a stream without a result is an empty answer. Either way the next candidate reviews.
The receipt keeps the run's `session` (the Antigravity conversation id) and `usage` (its token counts). The
receipt's `options` names Mastack's option for the provider, `thought_level` for Google, though `agy` takes the
value as `--effort`. `[cli.antigravity] review = [...]` in the models file replaces that command (`{cli}`, `{id}`,
`{effort}`, `{checkout}`, `{output}` and `{prompt}` are filled in; the raw prompt goes on stdin and the answer is its
stdout, or the `{output}` file when the command names one); `[cli.antigravity] run` is never used for a review,
since an implementing command need not be read-only.
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
(`ANTIGRAVITY_CONVERSATION_ID`, `CODEX_THREAD_ID`, `CLAUDE_CODE_SESSION_ID`, `T3CODE_THREAD_ID`). The model that
agent recorded decides first (`claude-*` anthropic, `gpt-*` openai, `gemini-*` google); without one, `claude-code`
is anthropic, `codex` openai and `antigravity` google, while `t3-code`, which runs any of the three, tells nothing.
Unknown, the flag is left out and `role`'s caution is shown. A `role` that fails or gives no choice leaves the choice to the models file.

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
empty takes a stand-in (fathers#201): the members share one pool, the candidates that hold and are not on the panel,
in order, and each failing member takes the next one nobody else reviews with, its entry's `fallback` saying why
("the Opus run failed (exit 1): ...: neon reviews in its place"). With none left the member is kept as `failed` and
the others' answers stand. `panel_required` holds the gate on a `failed` member only:
a member the rules skipped is not missing. The receipt's `panel` lists each member: `member`, `reviewer`, `model`,
`effort`, `cli`, `status` (`finished`, `failed`, `skipped`), `reason`, `error`, `fallback`, `seconds`, `output`,
`session` and `usage` (an Antigravity run's) and `numbers`, its findings in the one `findings` list, which numbers them across the panel and names each one's
`reviewer`; `review --mark N` and the gate read that list as before. `review ID --compare` prints each reviewer's
findings with their marks, and `stats --json` gives per-reviewer totals.

`lanefather review ID --reviewer ALIAS|both` (also with `--overall`) chooses the reviewer of that one review:
an alias of the models file (`sol`, `opus`, `neon`, `argon`, `carbon`, `flash`), a provider or CLI name (`antigravity`,
`google`, `agy`, `claude`, `codex`) or a model spec (`gemini-4-neon`, `gemini-4-argon:high`) runs whatever the Codex
limit says and with no stand-in (a missing CLI fails it), and `both` runs the configured panel (Sol and Opus without
one) the same way. Only then does the receipt carry `forced` (`{"by": "flag", "reviewers": [...]}`), which `review`
shows. The receipt records the reviewer's actual `provider`, `cli`, `model`, `effort` and `options` (`effort`,
`model_reasoning_effort` or `thought_level`). `--reviewer` does not combine with `--mark`, `--compare` or `--fix-lane`.

Receipts from this release add keys (`agent` in a check receipt; `panel`, `forced`, `role`, `prompt_sha256`,
`scope`, `scope_complete` and a finding's `reviewer`, `session` and `usage` in a review receipt) that an earlier
release reads past: it lists, shows, marks and gates them.
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
| `host`, `slots`, `lifetimes` | the lane cap and load; the slotfather client; the spawnfather client, which owns and removes what a lane holds |
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
each running its own branch's `tools/lane`, share the state directory with an installed lanefather. A record of a newer schema is refused, never rewritten.

Records, receipts, the settings, the registry and the installed command are written whole: each write goes through
its own temporary file beside the target, synced before the rename, and the directory after it. A lane record that is empty
or not a JSON object (a write a crash cut short) is never read as no lane. Every command that admits, claims,
lands, promotes or collects refuses while it is there, naming the file, as do `list` and `show`; `status` lists
the project's healthy lanes and the file as a `damaged` row (a damaged record cannot name its repository, so every
project sharing the directory shows it). Restore the record or remove the file. A directory sync that fails for any
reason but an unsupported one (EINVAL, ENOTSUP) fails the write.

Safe to change: messages, the modules' insides. Not without a schema bump and a migration: record and receipt keys,
the landing lock's file format (a project's reference-transaction hook reads it), the queue file. An optional key
that older versions may ignore is added within schema 1, as `review_fix_of_all` and `agents` were.

`scope` is such a key (fathers#192): the [accepted scope](#accepted-scope) as an ordered list of `{"issue": "N"}`,
`{"issue": "N", "partial": true}`, `{"decision": "<url>"}` and `{"spec": "<path>"}`. `ticket` always holds the
primary ticket. A record without `scope` reads as its one ticket, or as no scope for `none`, and a lane that
completes one ticket and names nothing else writes no `scope`, so its record is the same as before. A malformed
`scope` warns and reads as `ticket` alone. An older Lanefather reads only `ticket`: a multi-ticket lane it lands
says `Fixes` for the primary ticket only, which is why the primary is a ticket the lane completes.

`lane_created` on check (`.receipt.json`), first (`.first.json`), post-merge (`.post-merge.json`), recheck
(`.recheck.json`) and review (`.review.json`) receipts is another such key (fathers#54): copied from the lane
record's `created` timestamp so a reused lane id after `done` never inherits an earlier incarnation's receipts,
review or smoke. Readers treat a receipt whose `lane_created` does not match the current lane record's `created`
as absent. A pre-upgrade receipt written without `lane_created` still matches when its own timestamp (`started`,
else `finished`) is at or after the record's `created` (or when it carries no timestamp), so live lanes across
an upgrade keep reading their receipts while newly reused lane ids ignore older unstamped receipts.

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
status and review status/finding counts. Read-only views (`list`, `show`,
`status`) evaluate tree currency only via `receipts.check_verdict` and never
spawn project toolchain resolvers. `checks.tree_match` is true/false when the
branch tree can be compared with the receipt, otherwise null. `checks.current`
is false for a proven tree mismatch (or `StaleToolchain` when checked) and null (unknown) for a match: current
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
`lane.landed`, `lane.post_merge`, `lane.released`, `lane.done_refused`, `lane.unregistered`, `lane.done`, `lane.dropped`,
`lane.parked`, `lane.abandoned`, `lane.rechecked`, `lane.kept`, `lane.scope_changed`, `process.stopped`,
`review.started`, `review.kept`, `review.exempted`, `review.finished`, `finding.marked`, `review.marked`,
`review.handed_over`, `review.ticket_filed`, `gate.finished`, `perf.finished`, `promote.step`, `promote.finished`,
`sync.finished`, `load.waited`, `landing.lock_taken`, `ticket.marked`, `ticket.skipped`,
`ticket.release_commented`, `ticket.failed`, `ticket.trailer_conflict`, `queue.updated` and `override`; `lanefather/events.py` describes each.

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
    "checkout": "/Users/me/straylight", "cap": 4, "cap_source": "slotfather derived; derived: 4", "active": 3, "planned": 0,
    "gate": {"sha": "cedf8b97...", "status": "red", "at": "2026-11-12T04:06:19Z"},
    "lanes": [{"id": "710-speech-paste-phase", "status": "active",
               "ticket": {"number": "710", "url": "https://github.com/maxvyshniakov/straylight/issues/710"},
               "tickets": [{"number": "710", "url": "https://github.com/maxvyshniakov/straylight/issues/710",
                            "partial": false}],
               "branch": "lane/710-speech-paste-phase", "base": "integrate/queue-347", "worktree": "/...",
               "claims": ["desktop/src/**"], "after": [], "created": "2026-11-12T03:10:00Z",
               "receipt": {"status": "green", "tree_match": true, "path": "/.../710-speech-paste-phase.receipt.json"},
               "post_merge": null, "review": {"status": "running", "open": 2, "findings": 3},
               "logs": {"check": "/.../logs/lane-710-speech-paste-phase-20261112T040619.log"},
               "outcome": null}]},
   {"project": "data-grid", "state_dir": "/...", "error": "config not found at /.../.lanefather.toml"}]}
```

The lanes are those not done. `ticket` is the lane's primary ticket; `tickets` lists every ticket of its accepted
scope in order, with `partial` for one it does only part of (`show --json` carries the same list).
`receipt.tree_match` compares the receipt's tree with the lane branch's, null when
either is unknown; `review.status` is the review receipt's, `stopped` for a running review whose process is gone;
`logs` holds the newest run log of each phase (`check`, `rerun`, `base`, `post-merge`, `review`); `gate` is the newest
batch gate; `outcome` is null on the live lanes `status --json` lists, and `"landed"`, `"parked"`, or `"abandoned"` on done lanes in `list --all --json`. `checkout` is the project's main
checkout (its real path), the project a lane owner names in Spawnfather (`lane:<checkout>/<id>`, fathers#171). `cap` is the project's lifetime admission answer,
or its derived cap with the reason when admission is unavailable. A project that cannot be read gives
`{"project", "state_dir", "error"}`, never a failed snapshot; one whose configuration file is missing and that no
command has seen for 7 days is left out, and the next registration drops it. The command takes no locks, writes
nothing, runs no project hook and makes no network call. `capabilities --json` advertises it as
`dashboard.snapshot`: `{"argv": ["status", "--json"], "versions": [1], "poll": 5}`.

## Stats and orchestration metrics

`lanefather stats [--days N] [--lane ID] [--json]` summarizes check health and orchestration flow over the last `N`
days (default 7; `0` for everything kept), optionally scoped to one lane across all blocks (fathers#92, fathers#47,
fathers#151). It reads lane records, check and review receipts, `<state>/history/*.jsonl`,
`<state>/promote/promote-*.json`, `events.jsonl`, and Slotfather lease history (`slotfather history export`) when
Slotfather is available. Currently `active` and `planned` lanes are included in the window alongside lanes with
activity in the last `N` days.

Under `"v": 1`, `stats --json` returns the existing `checks` and `reviewers` tables alongside four structured blocks:

- `lane_flow`: `lanes` count, `to_first_green`, `to_land`, and `to_promote` (`{"count", "p50", "p90"}` in seconds from
  lane `created` to its earliest green `lane.checked` event in `events.jsonl` — falling back to a green check receipt
  when no green event exists — to `landed.at`, and to `promoted.at` or the earliest matching `promote.finished` /
  completed promote record; lanes that have not reached a milestone are omitted rather than counted as `0`),
  `slotfather_available`, and per-phase `phases` (`phase`, `runs`, `matched_leases`, `run_p50`, `run_p90`, `wait_p50`,
  `wait_p90`, `wait_status`). Queue wait is joined by `lease_id` (or `run`); when Slotfather is absent or has no
  matching lease, `wait_p50` and `wait_p90` are `null` and `wait_status` is `"unknown"` (never `0`).
- `outcomes`: counts of `total`, `landed`, `parked`, `abandoned`, `dropped`, `active`, `planned`, and `other` lanes,
  using `lifecycle.outcome_of` with legacy fallback for records written before `outcome` (fathers#57).
- `overrides`: `--force-reason` override counts (`total`, `lane`, `promote`, `by_command`), ranked `reasons` and
  `promote_reasons`, per-batch `batches` (grouped by promote SHA or `"unpromoted"`, with override counts, reasons, and
  fix lanes), and `fix_lanes` (`total`, `single`, `batch`, `lanes` with `id`, `fix_of`, `batch`).
- `review`: `reviewed_lanes`, `findings` (plus `p1`, `p2`, `p3`, `fixed`, `rejected`, `unmarked`), `findings_per_lane`,
  `fixed_share`, `rejected_share`, `unmarked_share`, and `gate` (`held`, `overridden`) counting held items and override
  events recorded in review-gate `override` events.

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
release without replacing its bytes. Normal installs refuse to move `current` backwards;
`--no-switch` stages a release under `releases/` without moving `current` or the command
(and an older commit re-installed that way does not count as newest), `--rollback` points
`current` at the previous release, and `--uninstall` removes the command, `current` and
unpinned/unused releases while keeping releases pinned by live lanes or in use by a running
process. Pruning orders releases by version and build order (not mtime), retains pinned and
in-use releases, and cleans stale `releases/.pending-<pid>` directories.

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
