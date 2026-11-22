# Event schema, version 1

The tools that feed Godfather (Slotfather, Lanefather, Spawnfather) each append one JSON object per line to
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

[`common/eventcheck.py`](../../common/eventcheck.py) is one standard-library file with an MIT header.
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

### Sharing

The tools in this repository import the one file from `common/`, so a new schema rule reaches every tool's tests at
once. A project outside this repository copies the file unchanged and records its SHA-256 beside the copy.

## Slotfather lock and reservation events

| Kind | Details |
| --- | --- |
| `lease.yielded` | When a lease gives up its slots while waiting for a lock, `lock` is the path and `to.id` is null. `to.label` describes the lock. A yield to another request carries that request's id and label. |
| `lease.lock_ended` | `lock` is the path. `why` is `took`, `stopped`, `resumed` (work resumed under the lease), or `gone` (the waiter died). `yielded` records whether the lease gave up its slots, and `waited` is its wait in seconds if known, otherwise null. |
| `request.head_dropped` | The head gave way after its reserved start. `count` is the slots requested, `late` is seconds past the reservation, and `blocked_by` lists the leases with no end estimate by `id`, `label` and `pid` (each may be null). |

## Reading (Godfather)

`godfather.events` follows each tool's log, at the path its capabilities give under `dashboard.events` (a version
it reads in `versions`):

- by inode and offset; after a rename to `events.jsonl.1` it reads the rest of the old file, then the new one from
  its start, so a rotation loses no line and repeats none; a log truncated in place is read again from its start;
- a last line without its newline waits until it is whole; a line that never ends within 16 KiB is skipped;
- lines that fail the validator (lenient), name another `v`, another `host` than this machine's (looked up on every
  read, as the tools do on every write: a Mac's name follows its network) or another `source` than the log's tool
  are skipped and counted per source; EVENTS' bottom border says how many and why, nothing is printed, and nothing
  stops the reader;
- one read takes at most 1 MiB of a log; the rest comes with the next;
- on start, the newest 600 events or 256 KiB per source, whichever is less, the end of `events.jsonl.1` included
  while the current log is shorter;
- growth is checked by `stat` every 250 ms (`godfather events --follow`) or at each refresh (the dashboard);
- the sources merge by `time`, then source name, then file order.

`godfather events [--source NAME]... [--follow] [--json]` prints that feed, newest last; `--json` prints the lines
as read.

The overview's EVENTS shows one tool's log at a time (fathers#152): its title, `SLOTS LOG`, `LANES LOG`, `SPAWNS
LOG` or `FULL LOG`, is the switch (← → with EVENTS focused, `f`, a click on it), each log has its own columns, a
Slotfather run is one row from its grant to what ended it (`r` shows the events as logged), and a run's trend is a
mark on its row. Legacy `junkfather` lines in a migrated Spawnfather event log are read as Spawnfather events. What
follows is how fathers#84 drew the merged feed; its formatters are the ones each log uses.

The overview's EVENTS (fathers#84) was the same feed, newest first: time, source (the tag from the tool's
capabilities, in its own colour), event, slots (Slotfather's only), label, detail and pid. Each source has a
formatter from a kind and its details to the event word and the detail; a kind it does not know shows the kind
itself and `key=value` pairs. Nothing is parsed out of prose: Lanefather's `details.message` is shown only for a kind
with no other field. With Slotfather alone the source and event columns are not drawn. `f` cycles the source filter
(all, then each present source in the order slotfather, lanefather, spawnfather); `/` opens a text field in the bottom
border, matched case-insensitively against the kind, subject id, label and detail, Enter keeps it and Esc clears it.
The top border names the filter (`all sources`, `lanefather`, `"710"`). A narrow panel shortens the label first, then
drops pid, then slots, then shows the source as a one-cell coloured mark. Under `GODFATHER_FIXTURES=DIR` a tool's log is `DIR/<tool>/events.jsonl` and `DIR/host.json`'s `host`
stands in for this machine's name.
