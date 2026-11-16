# common

Code more than one package uses today. Nothing here is installed: each package's installer copies only its own
package, so a file here is imported by tests and tools, not by an installed command.

- `eventcheck.py`: the validator for the shared event schema, version 1 ([godfather/docs/events.md](../godfather/docs/events.md)).
  Every package's tests check their `events.jsonl` with it in strict mode. One standard-library file with an MIT
  header; another project may copy it unchanged and record its SHA-256 beside the copy.
- `cliconvention.py`: the conformance check of the CLI convention ([docs/cli.md](../docs/cli.md)). Each package's
  `tests/test_cli_convention.py` runs it against that package's command, with the cases it does not pass yet listed
  as expected failures. Its own tests are `tests/` here (`python3 -m unittest discover -s common/tests`).
