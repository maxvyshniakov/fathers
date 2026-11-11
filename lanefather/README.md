# Lanefather

Coordinate parallel changes in a Git repository: give each task a worktree and a declared path scope, record what
was checked, merge through one integration branch, then review, publish and clean up.

When several people or coding agents work at once, branches alone do not tell you who owns a file, whether a test
result belongs to the latest code, or which worktree is safe to remove. Lanefather makes that workflow explicit
and keeps its evidence outside the disposable worktrees.

## What it solves

| Pain | Lanefather's part |
| --- | --- |
| Parallel tasks unexpectedly edit the same files | Lanes declare path claims and dependencies; registration checks conflicts, and checking widens claims to the actual diff. |
| A task says “tests passed” but its code changed afterwards | A verification receipt identifies the committed tree and configured check results. |
| Every task reruns everything while integration keeps moving | Lane checks, post-merge verification and the batch gate have distinct evidence and reuse rules. |
| Several agents push or move shared branches at once | Landing and promotion use a shared lock; an optional reference-transaction hook guards agent updates to `dev` and `main`. |
| Finished work leaves branches, processes and build folders behind | `done` and conservative `gc` check ownership, merge state and worktree use before cleanup. |

A **lane** is a registered unit of work, not an agent process or a machine slot. Path claims coordinate edits;
they are not filesystem locks. A landed lane can still have review or cleanup pending.

## Where it fits

Lanefather is aimed at POSIX development machines running parallel Git worktrees. Promotion currently follows an
opinionated `dev`/`main` branch policy. Full lane verification requires your project's check-contract adapter;
a plain `bun run verify` or `make test` command alone does not implement that contract.

[Repo-Kit](https://github.com/maxvyshniakov/repo-kit) can supply the verification adapter for configured Bun
libraries. [Slotfather](https://github.com/maxvyshniakov/slotfather) coordinates expensive executions across the
machine. They solve different problems: a lane can live for hours while acquiring execution leases only for its
checks or builds. You can also use protected-branch promotion with your own pre-push gate before adopting lanes.

## Requirements

- Python 3.11 or newer and Git. The installed Python runtime has no third-party dependencies.
- macOS or Linux/POSIX process and filesystem tools. Windows-native operation is not supported.
- Your project's verification adapter for `check`/`land`; project-specific build, install and deployment commands remain configuration.
- Authenticated Git/GitHub access for remote publication and the GitHub operations you use.
- Authenticated Codex and/or Claude CLIs for automatic AI reviews; `gh` for issue/comment integration.
- Slotfather when enabling its admission provider or configured execution leases.

Admission is intentionally conservative. The current default disk guard requires **150 GB free on the home
filesystem**; cap and load checks also apply. This can exclude small disks even for a small repository. See
[admission configuration and override semantics](docs/admission.md) before adoption.

## Install

```sh
git clone https://github.com/maxvyshniakov/lanefather.git
cd lanefather
python3 install.py
```

Put `~/.local/bin` on `PATH`, then run `lanefather --version` and `lanefather capabilities --json`.
Installation creates an immutable release and atomically updates the command; already-running commands keep their
release. Install from committed source. There is no documented PyPI/package-manager installation path.

## Configure one repository

Create `.lanefather.toml` at its root. For a project using a Repo-Kit release containing the lane adapter:

```toml
project = "example-library"
state_dir = "~/.local/state/lanefather/example-library/lanes"
coding_standards = "AGENTS.md"

[checks]
verify = ".githooks/lane-check"
slot_env = "REPO_KIT_SLOT"

[admission]
provider = "slotfather"
```

Point `coding_standards` at your checked-in project rules. Commit the Repo-Kit-managed entry after `bun run repo-kit hooks sync`. The Slotfather provider requires a receiver with
`lane-admission --json` v1 support. It is explicit opt-in; existing configurations keep their previous admission
provider. Projects using another verifier implement [the check contract](#the-check-contract).

Keep state outside worktrees and use the same state location for all participants in an integration group.
The default Store is shared; choose a project-specific directory when you want independent lane IDs and queues.
`--config PATH` selects an explicit configuration. [Configuration reference](docs/reference.md#project-configuration)

## A lane's workflow

Start with a configured verifier, a clean repository and an integration worktree. Examples assume a local `dev`:

```sh
git worktree add -b integrate/work .claude/worktrees/integrate dev
lanefather start example-change --ticket none --base integrate/work --claims 'src/**' --create
```

The command prints the task worktree and branch. Work there, commit the change, then:

```sh
lanefather check example-change
lanefather land example-change --into /path/to/repository/.claude/worktrees/integrate
```

Landing merges the verified task, starts its independent review and verifies the merged tree. Resolve or
explicitly disposition findings before promoting the integration SHA. Use `lanefather done ID` for cleanup that
was retained. Ticket-linked tasks also follow the configured issue/report requirements.

### AI review and external effects

By default, landing starts an authenticated AI reviewer in the background; source diffs and context can be sent
to that provider and can consume its paid usage or quota. `promote --ship` also starts an overall review; findings
can create a GitHub issue. [Configure the reviewer](docs/reference.md#reviewer-configuration) before using those commands.

Set `LANEFATHER_REVIEW_START=manual` to defer automatic starts and receive the review command instead. Required
review still has to finish before promotion. Documentation-only changes outside steering files have the explicit
review exemption documented in the source.

## Inspect before changing anything

```sh
lanefather list --json
lanefather show example-change --json
lanefather gc --dry-run --json
```

`show` reads saved lane context without starting checks, reviews or project hooks. Saved green evidence is not a
fresh execution of the current toolchain. Cleanup retains dirty, unmerged, owned or in-use worktrees when safety
cannot be established. A previously published active lane that bypassed `land` has no automatic retirement route;
its state must not be rewritten to pretend the missing workflow ran.

## Publish an integration

```sh
lanefather promote COMMIT_SHA          # dev
lanefather promote COMMIT_SHA --ship   # dev and main
```

Replace `COMMIT_SHA` with the integration commit you intend to publish. These commands publish remote branches. A configured verified-tree contract gets a batch gate; a library without
one relies on its mandatory pre-push gate. `--install` and `--deploy` execute only the project's configured steps.
Review, publication, local installation and remote deployment remain separate outcomes.

## The check contract

```text
<verify> --changed --base B --plan --json
<verify> --checks A,B | --suites S --keep-going --jobs N
```

The first answers a JSON verification plan; the second executes it and reports real per-check results.
[Full protocol and command reference](docs/reference.md#the-check-contract) ·
[Repo-Kit adapter](https://github.com/maxvyshniakov/repo-kit#working-with-lanefather)

## State and its versions

Lane records use schema 1. Existing extraction-era records and the historical tool are covered by compatibility
tests. A newer unknown schema is refused. Record/receipt keys, queue and lock formats require an explicit migration
when changed. Project launchers that pin an exact installed source may also require coordinated pin updates;
state compatibility does not make a different executable satisfy an old exact-source pin.

[State details](docs/reference.md#state-and-its-versions) · [Installed runtime contract](docs/reference.md#installed-runtime-contract)

## Lifetime admission

The opt-in Slotfather provider supplies lifetime cap/load advice to registration, `next`, `list` and
`Project.readiness`. Lanefather retains Store counting/locking, claims, dependencies, disk policy and lifecycle.
The stateless query does not reserve execution slots, and execution `advise.jobs` is not a lane cap.
[Contract, failure handling and rollout](docs/admission.md)

## More detail

[Commands, configuration and architecture](docs/reference.md) · [Glossary](GLOSSARY.md) ·
[Terminology and compatibility](docs/terminology.md) · [Verification](docs/agents/checks.md)

## License

[MIT](LICENSE). Lanefather has no third-party runtime dependencies and vendors no third-party code. The files under
`tests/fixtures/` are frozen copies of the older tool Lanefather was extracted from, by the same author and under
the same license; `tests/fixtures/PROVENANCE.json` records their source.

Security reports: [SECURITY.md](SECURITY.md). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).
