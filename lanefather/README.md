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
lanefather promote <sha> [--ship] [--no-push] [--install [--deploy HOST]]
lanefather sync [--drop-local]
lanefather perf <sha>
lanefather list [--all] [--json]
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
| `rules_dir`, `coding_standards`, `steering`, `report_doc` | what a brief and a review read |
| `unclaimed`, `merge_config` | paths no lane claims; `git -c` settings for land's merge |
| `[checks]` `verify`, `plan_module`, `triage`, `toolchain`, `slot_env` | the check contract below |
| `[lane]` `targets`, `cache_partitions` | build directories `done` removes |
| `[gate]` `stamp_tool`, `stamp_file`, `pre_push_hook` | the verified-tree stamp of the full gate |
| `[promote]` `install`, `deploy`, `after`, `ship_refuses` | what promote runs after it moved dev |
| `[hooks]` `process_scan` | processes a lane left running in its worktree |

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
| `land`, `review`, `done`, `promote` | land and the smoke; the review; finishing a lane; gate, perf, promote, sync |
| `lanes`, `cli` | start, next, adopt, list; the command line |

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
