# Lanefather reference

[Overview, requirements and quick start](../README.md)

## Commands

```
lanefather start <id> --ticket N --claims GLOB... [--base REF] [--after ID...] [--create]
lanefather next [--start]                 the first ready entry of the ranked queue
lanefather adopt <id> <worktree> <branch> register the worktree an agent created
lanefather check <id>                     the checks the plan selects for the lane's diff -> receipt
lanefather land <id> [--into WORKTREE] [--keep]
lanefather review <id> [--mark N fixed:<merge>|rejected:<reason>] [--fix-lane]
lanefather review --overall <sha> --since <base>
lanefather done <id>
lanefather gc [--dry-run] [--json]         merged idle worktrees and unused branches
lanefather promote <sha> [--ship] [--no-push] [--install [--deploy HOST]]
lanefather sync [--drop-local]
lanefather perf <sha>
lanefather released <version> --since REF [--head REF]   comment a published version on the issues it fixes
lanefather list [--all] [--json]
lanefather show <id> [--state-dir DIR] [--project-dir DIR] [--json]   read-only lane context
```

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
slots) at interactive priority that waits at most `LANEFATHER_RERUN_WAIT` seconds; only the perf suite asks for the
whole machine.

`review <id> --fix-lane` starts the fix lane from the branch the lane landed on, which holds its merge, and refuses
when no branch does; the landed lane keeps its own registration.

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
| `[lane]` `targets`, `cache_partitions` | build directories `done` removes |
| `[gate]` `stamp_tool`, `stamp_file`, `pre_push_hook` | the verified-tree stamp of the full gate |
| `[promote]` `install`, `deploy`, `after`, `ship_refuses` | what promote runs after it moved dev |
| `[gc]` `after_remove`, `after_remove_report` | project cache cleanup after removal (`{worktree}`, `{name}`, `{project}`). Stdout is ignored by default, preserving existing hooks. With `after_remove_report = true`, a successful hook must emit `{"v":1,"freed":123}` to add its actually removed bytes; diagnostics go to stderr. Invalid reports add zero bytes and a report error. |
| `[admission]` `provider` | `legacy` by default; explicit `slotfather` uses the versioned lifetime-admission query, separately from execution jobs. See [rollout contract](../docs/admission.md). |
| `[tickets]` | the ticket lifecycle below; every rule is off without it |
| `[hooks]` `process_scan` | legacy process adapter for live lanes; without it `procs` supplies read-only inspection |
| `[hooks.project]` | named project policy scripts run by `project-hook NAME -- ARGS` in the installed interpreter |

### Execution leases after promotion

An install or deploy command that may build can opt into its own execution lease:

```toml
[[promote.install]]
name = "install"
argv = ["./install.sh"]
lease = { slots = 2, ticket = "42" }
```

`slots` is a positive integer or `"all"`; the optional `priority` is `normal`
by default (`interactive` and `batch` are also supported). `ticket` is optional.
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
release_comment = true    # promote --ship and `released`: comment main's SHA or the version on each fixed issue
                          # and remove its `integrated` label
preview = "https://preview.example.com/{short}"   # optional; {sha} {short} {branch} {ticket} {lane}
repo = "owner/name"       # optional; default: origin's GitHub repository
```

`integrated` means done and landed in the integration branch, not yet on `main` or released. A merge origin does not
have yet (a remote-tracking branch, as last fetched, that contains it) is said to be local only and is not linked. A
red smoke leaves the issue unmarked for the fix's land. The issues a release closes are those a `Fixes #N` line in
the shipped range names: origin's `main` before the push to the SHA for `promote --ship`, `--since` to `--head` for
`lanefather released <version>`, which a project's release process runs after it publishes. Each of them loses its `integrated` label, which no longer holds once it is released. `gh` runs without a
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

## Reviewer configuration

Authenticate the reviewer CLIs you intend to use according to their own documentation. The current source
prefers Codex (`gpt-6.1-sol`, medium effort) when it can determine at least 15% weekly quota remains; otherwise it
tries Claude (`claude-opus-5-5`, medium effort). A failed or empty Codex run can also fall back to Claude. Quota,
provider availability and account charges remain outside Lanefather.

Set `LANEFATHER_CODEX` and `LANEFATHER_CLAUDE` to explicit executable paths if PATH discovery is unsuitable.
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
| `journal` | the ledger and run logs |
| `claims`, `queue` | path claims and rule snippets; the ranked queue |
| `gitops`, `worktrees`, `landing` | git calls; smoke, gate, base and review checkouts; the landing lock |
| `host`, `slots`, `procs` | the lane cap and load; the slotfather client; the process source |
| `plan`, `checks` | the check contract; verify runs, receipts and red-check classification |
| `gc` | generic worktree/branch retention and collection, with explicit project/state values |
| `reviewer` | configured reviewer, real CLI adapters, fallback and interpreted answer |
| `land`, `review`, `done`, `promote` | land and the smoke; the review; finishing a lane; gate, perf, promote, sync |
| `lanes`, `cli` | start, next, adopt, list; the command line |

The [architecture pass](../docs/architecture-667.md) records the survey, alternative interfaces and deployment order.

The standalone process adapter refuses PID-only live stopping until slotfather accepts an expected `(pid, start)`
identity at the actual signal boundary. Existing configured hooks keep their supported cleanup contract. A missing
`procs` on an older CLI preserves legacy `done` behavior; failed/unknown inspection retains worktrees.

### State and its versions

Records are schema 1, the format written by `tools/lane`, the project-local tool Lanefather was extracted from.
lanefather writes nothing an older `tools/lane` cannot read, and reads everything it wrote: lanes on older branches,
each running its own branch's `tools/lane`, share the state directory with an installed lanefather. `tests/test_compat.py` proves it in both
directions against that tool (`tests/fixtures/legacy-lane`). A record of a newer schema is refused, never rewritten.

Safe to change: messages, the modules' insides. Not without a schema bump and a migration: record and receipt keys,
the landing lock's file format (a project's reference-transaction hook reads it), the queue file.

## Checks

```sh
python3 tools/check.py          # ruff, pyright strict against pyright-baseline.json, the tests (through slotfather)
python3 tools/check.py --lint
python3 tools/check.py --update-baseline
```

The pyright baseline counts errors per file and rule and only shrinks.

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


## Installed runtime contract

`lanefather capabilities --json` returns version 1, the state schema, supported
Slotfather query versions and the installed source commit and payload SHA-256.
An installed release verifies its package and canonical hook against its
manifest; a changed or missing manifest refuses verification. A checkout reports
`installed: false`, so a project launcher can require the installed artifact.
The installer accepts committed shipping inputs and reuses an identical existing
release without replacing its bytes.

`lanefather --config /path/to/.lanefather.toml project-hook NAME -- ARGS` runs only
a named script from `[hooks.project]`, with the project's directory as its working
directory. The script receives `LANEFATHER_PROJECT`, an explicit
`lanefather.project.Project`; `readiness()` supplies one configured queue/state
snapshot with `planned_or_active`, the exact planned-plus-active lane-cap group; `Project.paths` resolves state/queue locations
without creating them. `update_queue(policy)` validates and atomically writes policy under
the shared queue lock. The script owns project selection and presentation; the
installed package owns the lane workflow and persistence. A script's exit status
is returned unchanged. Project code needs no source-checkout import path.
