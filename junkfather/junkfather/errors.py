"""How a junkfather command refuses (docs/cli.md), backed by `fathers_common.errors` (fathers#117)."""
from __future__ import annotations

from typing import Optional

from fathers_common import errors as _common
from fathers_common.errors import (
    FAILED,
    FOREIGN,
    NOT_FOUND,
    OK,
    READ_ONLY,
    REFUSED,
    TEMPORARY,
    USAGE,
    Refused,
    Usage,
)

__all__ = [
    "FAILED", "FOREIGN", "NOT_FOUND", "OK", "READ_ONLY", "REFUSED", "TEMPORARY", "TOOL", "USAGE",
    "Refused", "Usage", "pointer", "refuse", "say",
]

TOOL = "junkfather"


def say(text: str) -> None:
    """A refusal's reason or a warning that does not stop the command, on stderr with the tool's prefix."""
    _common.say(TOOL, text)


def pointer(command: Optional[str]) -> str:
    return _common.pointer(TOOL, command)


def refuse(error: Refused, command: Optional[str] = None) -> int:
    """Print `error` the one way docs/cli.md asks and return its exit code."""
    return _common.refuse(TOOL, error, command)
