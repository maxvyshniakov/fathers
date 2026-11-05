# lanefather

Coordinate parallel repository work from declared path claims through verification, landing, review and cleanup.
Each **lane** tracks one unit of work; the ranked queue orders proposed lanes, and promotion publishes verified
changes to `dev` and `main`. It began as Straylight's `tools/lane` (#648) and serves any repository
that names its own commands in a `.lanefather.toml`.

lanefather is standard library Python (3.11 or newer), installed without a build step beside
[slotfather](https://github.com/maxvyshniakov/slotfather), which manages machine capacity through slot requests
and leases. A lane does not reserve slots for its lifetime.

See the [glossary](GLOSSARY.md) for domain definitions and
[terminology](docs/terminology.md) for naming decisions and compatibility.

## Install

```sh
python3 install.py            # ~/.local/bin/lanefather on this interpreter; releases in ~/.local/share/lanefather
lanefather --version          # the commit installed
```

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
lanefather list [--all] [--json]
lanefather show <id> [--state-dir DIR] [--project-dir DIR] [--json]   read-only lane context
```

`lanefather --help` describes the commands. The [glossary](GLOSSARY.md) distinguishes lane states, receipts,
reviews and verification gates.

## Project configuration: `.lanefather.toml`

lanefather reads `.lanefather.toml` at the top of the repository it runs in (or `--config PATH`,
`LANEFATHER_CONFIG`). Every key is optional; see `lanefather/config.py` for the full list and Straylight's file for a
complete example.

| Key | What it names |
| --- | --- |
| `command` | how messages name the command (`tools/lane` in Straylight) |
| `state_dir` | the lane records, receipts and ledger |
| `legacy_env_prefix`, `[env]` | the variables older tools set, read after `LANEFATHER_*` |
| `rules_dir`, `coding_standards`, `steering`, `report_doc` | rule/standards inputs and the report-link text |
| `unclaimed`, `merge_config` | paths no lane claims; `git -c` settings for land's merge |
| `[checks]` `verify`, `plan_module`, `triage`, `toolchain`, `slot_env` | the check contract below |
| `[lane]` `targets`, `cache_partitions` | build directories `done` removes |
| `[gate]` `stamp_tool`, `stamp_file`, `pre_push_hook` | the verified-tree stamp of the full gate |
| `[promote]` `install`, `deploy`, `after`, `ship_refuses` | what promote runs after it moved dev |
| `[gc]` `after_remove`, `after_remove_report` | project cache cleanup after removal (`{worktree}`, `{name}`, `{project}`). Stdout is ignored by default, preserving existing hooks. With `after_remove_report = true`, a successful hook must emit `{"v":1,"freed":123}` to add its actually removed bytes; diagnostics go to stderr. Invalid reports add zero bytes and a report error. |
| `[hooks]` `process_scan` | legacy process adapter for live lanes; without it `procs` supplies read-only inspection |
| `[hooks.project]` | named project policy scripts run by `project-hook NAME -- ARGS` in the installed interpreter |

### The check contract

```
<verify> --changed --base B --plan --json   {full, paths, unowned, checks: [{id, suite, paths, producer}], suites, reasons}
<verify> --checks A,B | --suites S --keep-going --jobs N
                                             prints `Verification report: <report.json>`
<verify> --keep-going --jobs N               the full gate; prints `Verified tree <tree> stamped`
```

The plan module (`plan_module`) answers `SUITES`, `checks(...)` and `smoke(root)`; `PERF = "<suite>"` names the perf
suite. A project without one gets whole suites.

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

The [architecture pass](docs/architecture-667.md) records the survey, alternative interfaces and deployment order.

The standalone process adapter refuses PID-only live stopping until slotfather accepts an expected `(pid, start)`
identity at the actual signal boundary. Existing configured hooks keep their supported cleanup contract. A missing
`procs` on an older CLI preserves legacy `done` behavior; failed/unknown inspection retains worktrees.

### State and its versions

Records are schema 1, the format Straylight's `tools/lane` wrote before the move. lanefather writes nothing an older
`tools/lane` cannot read, and reads everything it wrote: lanes on older branches, each running its own branch's
`tools/lane`, share the state directory with an installed lanefather. `tests/test_compat.py` proves it in both
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


## Unfinished machine-admission ownership

Execution worker budgets already use `slotfather advise`. Lifetime lane admission
is a separate unfinished requirement of Straylight #648: committed Slotfather
5f76d1be exposes execution `jobs`/capacity/load advice, not a lifetime lane-cap
or lane-admission decision. Lanefather therefore retains its existing cores/memory
lane cap, load15 and disk admission guards, including explicit configured overrides.
`Project.readiness()` reports these retained guards; it does not imply that all
machine policy has moved to Slotfather. Do not interpret execution `jobs` as a
lifetime lane cap or remove admission protection to simulate that migration.
The owning follow-up [slotfather#2](https://github.com/maxvyshniakov/slotfather/issues/2)
must define a versioned lane-admission contract in Slotfather,
then migrate CLI start/next and Project readiness together with compatibility tests.
