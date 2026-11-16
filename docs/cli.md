# CLI convention

How the four tools' command lines behave, so that learning one teaches the others (fathers#118). Slotfather,
Lanefather, Junkfather and Godfather follow it; [common/cliconvention.py](../common/cliconvention.py) checks a tool
against it, and each package's tests run that check (below). Wire behaviour that Godfather or Repo-Kit already reads
stays accepted where this page says so.

## Errors

A refused command says why on stderr and prints nothing on stdout:

```text
slotfather: unknown option --nosuch for run
Try 'slotfather run --help'.
```

- The first line is `<tool>: <reason>`: the tool's command name, a colon, and why, in one sentence a reader can act
  on. It names the word it refuses (`unknown command 'statsu'`, `-n takes a number of slots or all, not 'x'`).
- A usage error (exit 2) adds one line pointing to the help: `Try '<tool> --help'.`, or
  `Try '<tool> <command> --help'.` when the command is known. Never the whole usage text.
- Any other refusal is the one line alone; a command with a `--json` reply also prints that reply on stdout where
  its contract has one (the settings replies below).
- An unexpected exception is still one line (`<tool>: internal error: <type>: <message>`), exit 1; the traceback
  goes to the tool's own log, not to the terminal.
- Warnings that do not stop the command go to stderr with the same `<tool>: ` prefix.

## Help and version

- `<tool> --help` and `<tool> -h` print the help on stdout and exit 0. It names every command the tool accepts.
- `<tool> <command> --help` prints that command's help on stdout and exits 0, for every command, including those
  that run another command (`slotfather run --help`, `junkfather own --help`) and those with subcommands.
- The help text is generated from the parser's table, so it cannot list fewer commands or values than the parser
  accepts.
- `<tool> --version` prints `<tool> <version>` on stdout and exits 0; words may follow on the same line (the commit,
  `(from a checkout)`). A `version` command, where one exists, stays (`slotfather version --json`).

## Options

- Long options take their value as `--opt value` or `--opt=value`; short ones as `-n 2`. A flag takes no value.
- `--` ends the options: what follows is a command to run or an operand that starts with `-`.
- An option a command does not know is a usage error, never ignored.
- One name per idea across the tools: `--json`, `--label`, `--ticket`, `--since`, `--priority`. A renamed option
  keeps its old name as an alias for at least one release.

## JSON

- `--json` is an optional flag on every command that prints state, and on every command whose reply a program reads.
  Without it the command prints text for people. A command whose only output is JSON (`capabilities`) still accepts
  `--json`, so callers can always pass it.
- A reply is one JSON object on stdout (JSON Lines for a stream: `history export`, `events --follow`), nothing else on
  stdout, with an integer `v` at the top level: the reply's version. An addition keeps `v`; a change an old reader
  would misread raises it, and the tool keeps answering the old shape where a reader still asks for it.
- `v` is the only version key of a reply. `"version"` is the tool's release (`changelog --json`, `version --json`);
  older replies that used it for the reply's version keep it beside `v` for one release.
- Snapshots, settings, capabilities, events and changelog replies keep the contracts in [Godfather's
  SPEC](../godfather/SPEC.md) and [godfather/docs/contracts/](../godfather/docs/contracts/).

## Exit codes

One table for all four tools. A code means the same in every tool; a tool that has no use for a code does not use it
for something else.

| Code | Meaning | Examples |
| --- | --- | --- |
| 0 | Done. A question answered yes or with data. | `status`, a saved setting, `lookup` found a receipt |
| 1 | Failed, or the answer is no: the command ran and the work or check did not succeed, or an I/O error stopped it. | a lane check is red, `results lookup` misses, `junkfather settings set` on a full disk |
| 2 | Usage: the command line or a value is invalid. Nothing changed. | an unknown command or option, a bad number, `settings set` with an out-of-range value |
| 3 | Refused by the current state: valid, but what it was based on changed or does not allow it now. | a stale `--revision` (conflict), `lanefather next` with nothing ready, `slotfather sweep --live` with broken settings |
| 4 | Another user's state: Slotfather's queue that belongs to another account, read only to this one. | `slotfather settings set` in another user's queue |
| 5 | Read-only: this value or file cannot be written here. | a setting the environment sets, a settings file that is not the tool's, a file without write permission |
| 6 | Not found: the thing the command names does not exist. | no such lane, entry, request, session or pid |
| 75 | Temporary: try again later (`EX_TEMPFAIL`). | `slotfather run --wait` gave up, an admission provider that did not answer |
| 128+N | Ended by signal N. | |

- A command that runs another command (`slotfather run`, `junkfather own`) passes that command's exit status
  through; its own codes above apply only before the command starts.
- Settings replies, all tools: 0 done, 2 invalid, 3 conflict (the reply is the current settings with
  `"conflict": true`), 4 another user's queue (Slotfather), 5 read-only, 1 not saved for another reason.
- Moved codes: Lanefather's settings answered 4 for read-only before fathers#130; it answers 5 from then, and
  Godfather's settings client accepts 4 from Lanefather as read-only for one release after.
- 6 is new: a refusal that means "not found" today exits 1 (Lanefather's `LaneError`) or 2 (Slotfather's `session`
  and `reprioritize`); each moves in its package's conformance ticket, and a reader that tests for the old code is
  changed in the same release.

## Conformance

[common/cliconvention.py](../common/cliconvention.py) runs a tool's command line in the environment a test gives it
and reports each rule above that the tool breaks. It checks what it can check without knowing the tool: the error
line and the help pointer for unknown commands and options, `--help`, `-h` and `<command> --help`, `--version`, that
the help names every command, `--json` on the commands that print state, and `--opt=value`. Exit codes beyond 0 and 2
are each package's own tests.

Each package's tests run it once (`tests/test_cli_convention.py`), with the package's state in a temporary
directory. A rule the package does not meet yet is listed there as an expected failure with the ticket that fixes it;
the list only shrinks, and a listed rule that starts passing fails the suite until it is removed:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))
import cliconvention
from cliconvention import Command

TOOL = cliconvention.Tool("junkfather", (sys.executable, "-m", "junkfather"),
                          (Command("list", json=()), Command("changelog", json=(), option=("--since", "0.0.1"))))
EXPECTED = dict.fromkeys(("version", "unknown command"), "fathers#NNN")


class CLIConvention(cliconvention.conformance(TOOL, expected=EXPECTED)):
    def prepare(self, tool: cliconvention.Tool) -> cliconvention.Tool:
        ...  # the same tool with this test's temporary state directory in its environment
```

| Tool | Conformance ticket |
| --- | --- |
| Slotfather | fathers#124 |
| Lanefather | fathers#130 |
| Junkfather | none yet: its expected failures (argparse's error format, `--version`) name fathers#118 |
| Godfather | none yet: its expected failures (the usage after the reason) name fathers#118 |
