# Lanefather Changelog

Versions: `lanefather/__init__.py` names the version; an installed release also names its commit
(`lanefather --version`). State compatibility: [README](README.md#state-and-its-versions).

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
