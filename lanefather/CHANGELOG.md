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
