# common

Code shared across the fathers packages (`fathers#117`, `fathers#118`, `fathers#119`). Each package's `install.py`
vendors `fathers_common/` into its immutable release beside the tool's package, so every tool still installs and runs
standalone; in a checkout, symlinks expose the same `fathers_common/` directory to Python.

- `fathers_common/`: the vendored runtime kit (Python 3.11+, standard library only):
  - `cli.py`: `UsageParser` — the `argparse` subclass that turns option errors into CLI-convention usage errors.
  - `errors.py`: `Refused`, `Usage`, `say`, `pointer`, `refuse`, and the shared exit-code constants (`docs/cli.md`).
  - `atomic.py`: `atomic_write`, `atomic_symlink`, `sync_directory` — unique temp file, `fsync`, rename, dir `fsync`,
    and temp cleanup on failure.
  - `events.py`: schema-v1 event sanitizer, encoder, and bounded-lock rotating log writer
    ([godfather/docs/events.md](../godfather/docs/events.md)).
  - `changelog.py`: shared `changelog --json` parser and contract builder
    ([godfather/docs/contracts/changelog.md](../godfather/docs/contracts/changelog.md)).
  - `proc/`: the one process-table reader (`fathers#171`), read-only and fail-closed: `ps` rows by column
    (`parse`, `read`, `identities`, `table_rows`), the `Proc`/`Table` model (trees, groups, ages), lstart text in
    UTC or local time and the epoch it names (`parse_lstart`, `epochs`, `render`; no `time.mktime`), the
    `{pid, started, cmd}` identity (`same_process`, `EPOCH_SLACK`), working directories from `/proc` or `lsof`
    (`working_dirs`) and Linux start times (`proc_start_epoch`). It imports only `os`, `subprocess`, `sys` and
    `time`, so a command's fast path can load it. Spawnfather reads in UTC, Slotfather in local time.
  - `installer.py`: shared release installer primitives (interpreter validation, Git checkout/dirty checks, source
    digests, immutable release staging, atomic symlink switching, and rollback-safe wrapper swapping).
- `eventcheck.py`: standalone validator for the shared event schema, version 1. Every package's tests check their
  `events.jsonl` with it in strict mode.
- `cliconvention.py`: conformance harness for [docs/cli.md](../docs/cli.md).

Run `common`'s checks from the repository root:

```sh
python3 tools/checks.py run "common lint"
python3 tools/checks.py run "common tests"
```
