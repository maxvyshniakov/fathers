# common

Code more than one package uses today. The Lanefather and Junkfather installers copy `fathers_common/` into
the release alongside the tool. In a checkout, links in the package directories expose the same source to Python.

- `eventcheck.py`: the validator for the shared event schema, version 1 ([godfather/docs/events.md](../godfather/docs/events.md)).
  Every package's tests check their `events.jsonl` with it in strict mode. One standard-library file with an MIT
  header; another project may copy it unchanged and record its SHA-256 beside the copy.
- `cliconvention.py`: the conformance check of the CLI convention ([docs/cli.md](../docs/cli.md)). Each package's
  `tests/test_cli_convention.py` runs it against that package's command, with the cases it does not pass yet listed
  as expected failures. Its own tests are `tests/` here (`python3 -m unittest discover -s common/tests`).
- `fathers_common/cli.py`: the argparse parser Lanefather and Junkfather use for usage errors.
