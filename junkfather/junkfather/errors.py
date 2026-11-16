"""How a junkfather command refuses (docs/cli.md): one line `junkfather: <reason>` on stderr, nothing on stdout, and
an exit code from the table every tool shares. A usage error adds the one line that points to the help.

A command raises Refused (or Usage) and cli.main formats it; nothing else prints a refusal's first line itself.
Standard library and no other Junkfather module. Godfather has the same formatter in app.py; both move to
common/ first once installed commands can import it (fathers#117)."""
from __future__ import annotations

import sys
from typing import Optional

TOOL = "junkfather"

FAILED = 1  # the work did not succeed, or an I/O error stopped it
USAGE = 2  # the command line or a value is invalid; nothing changed
REFUSED = 3  # valid, but the current state does not allow it
READ_ONLY = 5  # this value or file cannot be written here
NOT_FOUND = 6  # the entry, pid or resource the command names does not exist
TEMPORARY = 75  # try again later (EX_TEMPFAIL)


class Refused(Exception):
    """A command that stops with `reason` and exit `code`."""

    def __init__(self, reason: str, code: int = FAILED):
        super().__init__(reason)
        self.code = code


class Usage(Refused):
    """The command line is invalid: exit 2 and the pointer to the help of `command` (the tool's when None)."""

    def __init__(self, reason: str, command: Optional[str] = None):
        super().__init__(reason, USAGE)
        self.command = command


def say(text: str) -> None:
    """A refusal's reason or a warning that does not stop the command, on stderr with the tool's prefix."""
    print(f"{TOOL}: {text}", file=sys.stderr, flush=True)


def pointer(command: Optional[str]) -> str:
    return f"Try '{TOOL} {command} --help'." if command else f"Try '{TOOL} --help'."


def refuse(error: Refused, command: Optional[str] = None) -> int:
    """Print `error` the one way docs/cli.md asks and return its exit code; `command` names the help a usage error
    points to when the error itself names none."""
    say(str(error))
    if error.code == USAGE:
        print(pointer(error.command if isinstance(error, Usage) and error.command else command), file=sys.stderr)
    return error.code
