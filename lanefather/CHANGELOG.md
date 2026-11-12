# Lanefather Changelog

Versions: `lanefather/__init__.py` names the version, and an installed release also names its commit
(`lanefather --version`). The releases projects pinned before the next version are `0.1.0+<commit>`: each
installed as 0.1.0 and is told apart by its commit. State compatibility: [README](README.md#state-and-its-versions).

## Unreleased

### Added

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

### Changed

- Lanefather lives in the fathers repository with Slotfather, Junkfather and Godfather: `python3 install.py
  lanefather` from its root installs it, and its documentation links point there.

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
