# common

Code more than one package uses today. Nothing here is installed: each package's installer copies only its own
package, so a file here is imported by tests and tools, not by an installed command.

- `eventcheck.py`: the validator for the shared event schema, version 1 ([godfather/docs/events.md](../godfather/docs/events.md)).
  Every package's tests check their `events.jsonl` with it in strict mode. One standard-library file with an MIT
  header; another project may copy it unchanged and record its SHA-256 beside the copy.
