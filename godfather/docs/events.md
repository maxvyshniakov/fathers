# Event schema, version 1

The tools that feed Godfather (Slotfather, Lanefather, junkfather) each append one JSON object per line to
`events.jsonl` in their state directory; the tool's `capabilities --json` gives the absolute path. The design and
the example events are in [SPEC.md](../SPEC.md#event-schema-version-1); this page is the reference.

| Field | Required | Rule |
| --- | --- | --- |
| `v` | yes | The integer `1`. |
| `time` | yes | RFC 3339 UTC with milliseconds: `2026-11-12T02:24:16.204Z`. |
| `host` | yes | The short host name, lowercase, no dots (`socket.gethostname()` up to the first dot). |
| `source` | yes | The tool's name, `[a-z][a-z0-9-]{0,31}`. |
| `kind` | yes | Lowercase `noun.verb` (`[a-z][a-z0-9_]*` segments joined by dots), at most 64 characters, from the tool's documented list. A tool may document a one-word kind such as Lanefather's `override`. |
| `subject` | yes | An object with non-empty string `type` and `id`; optional string `label` and `project`, positive integer `pid`, and other scalars. |
| `actor` | yes | An object with `type` one of `agent`, `process`, `user`, `tool`; optional strings `session`, `agent`, `lane`, `name`, `via`, positive integer `pid`, and other scalars. |
| `details` | yes | An object, possibly empty, nested at most four levels (`details` itself is the first). |
| `level` | no | `info` (the default), `warn` or `error`. |
| `id` | no | A non-empty string of at most 64 characters, unique within the source. |

Also:

- A line is UTF-8 JSON, one object, at most 16 KiB with its newline, with no duplicate keys and no `NaN` or
  `Infinity`. No string, key or value, carries a control character (C0, DEL or C1).
- No environment contents, tokens or secrets; a command line is cut to 400 characters.
- Writers append each line with one `write` on a file opened with `O_APPEND`, mode 0600, after the change happened.
  Past 4 MiB the log is renamed to `events.jsonl.1` under an `flock` on `events.lock` beside it.
- Readers ignore unknown fields and skip, counting them, lines with an unknown `v` or that fail validation.

## Validator

[`src/godfather/eventcheck.py`](../src/godfather/eventcheck.py) is one standard-library file with an MIT header.
[`schema/event-v1.schema.json`](../schema/event-v1.schema.json) is the same contract as JSON Schema, minus the
checks JSON Schema cannot express.

```python
from eventcheck import validate, validate_line, validate_file
validate(obj, strict=False) -> list[str]        # empty when valid; each message starts with the field
validate_line(line, strict=False) -> list[str]  # bytes or str: size, UTF-8 and JSON, then validate()
validate_file(path, strict=False) -> list[str]  # "LINE: problem" for every line
```

```text
python3 eventcheck.py [--strict] FILE...   # prints FILE:LINE: problem; exit 0 clean, 1 problems, 2 usage
```

Lenient mode is what a reader accepts. Strict mode, for the tools' own tests, also refuses unknown top-level fields.
A file that does not end in a newline is a problem (a torn or partial last write).

### Vendoring

Copy the file into the tool's tests unchanged and record its SHA-256 beside it (for example
`tests/vendor/eventcheck.py` and `tests/vendor/eventcheck.py.sha256`). A test compares the copy with
`$GODFATHER_DIR/src/godfather/eventcheck.py` when `GODFATHER_DIR` points at a Godfather checkout and is skipped
otherwise; the tool's CI cannot read this private repository. A new schema rule changes the file and its hash, and
each tool picks it up by copying it again.
