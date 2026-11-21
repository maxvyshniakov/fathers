"""CLI refusal formatting and shared exit-code table (docs/cli.md, fathers#117).

Every command refuses on stderr as `<tool>: <reason>`, nothing on stdout, and exits with a code from the shared table.
A usage error (exit 2) adds one line pointing to `<tool> [command] --help`.
"""

from __future__ import annotations

import sys

OK = 0
FAILED = 1  # the work did not succeed, or an I/O error stopped it
USAGE = 2  # the command line or a value is invalid; nothing changed
REFUSED = 3  # valid, but the current state does not allow it
FOREIGN = 4  # another user's state, read-only to this account
READ_ONLY = 5  # this value or file cannot be written here
NOT_FOUND = 6  # the entry, lane, pid or resource the command names does not exist
TEMPORARY = 75  # try again later (EX_TEMPFAIL)


class Refused(Exception):
    """A command that stops with `reason` and exit `code`."""

    def __init__(self, reason: str, code: int = FAILED) -> None:
        super().__init__(reason)
        self.code = code


class Usage(Refused):
    """The command line is invalid: exit 2 and the pointer to the help of `command` (the tool's when None)."""

    def __init__(self, reason: str, command: str | None = None) -> None:
        super().__init__(reason, USAGE)
        self.command = command


def say(tool: str, text: str) -> None:
    """Print a refusal reason or warning on stderr with the tool's prefix."""
    print(f"{tool}: {text}", file=sys.stderr, flush=True)


def pointer(tool: str, command: str | None = None) -> str:
    """The help pointer line a usage error prints after its reason."""
    return f"Try '{tool} {command} --help'." if command else f"Try '{tool} --help'."


def refuse(tool: str, error: Refused, command: str | None = None) -> int:
    """Print `error` the way docs/cli.md specifies and return its exit code."""
    say(tool, str(error))
    if error.code == USAGE:
        chosen = error.command if isinstance(error, Usage) and error.command else command
        print(pointer(tool, chosen), file=sys.stderr)
    return error.code
