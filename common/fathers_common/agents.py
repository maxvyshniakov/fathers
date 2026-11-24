"""Agent-session process patterns shared across the fathers packages (fathers#146).

One pattern table for Claude Code and Codex session executables so Slotfather, Spawnfather and Lanefather agree on
whether a process is a live agent session. Imports only `re`.
"""
from __future__ import annotations

import re

CLAUDE = re.compile(r"(^|/)claude(\s|$)|/claude/versions/[^/\s]+(\s|$)|/claude\.app/Contents/MacOS/claude(\s|$)")
CODEX = re.compile(r"(^|/)(codex|CodexCLI)(\s|$)|/codex-[\w-]+/vendor/[^\s]*/codex(\s|$)")

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("claude", CLAUDE),
    ("codex", CODEX),
)

__all__ = ["CLAUDE", "CODEX", "PATTERNS", "agent_kind", "is_agent", "is_claude", "is_codex"]


def is_claude(cmd: str) -> bool:
    """Whether `cmd` is a Claude Code session's process."""
    return bool(CLAUDE.search(cmd))


def is_codex(cmd: str) -> bool:
    """Whether `cmd` is a Codex session's process."""
    return bool(CODEX.search(cmd))


def agent_kind(cmd: str) -> str | None:
    """The agent kind ('claude' or 'codex') if `cmd` is an agent session's process, else None."""
    for kind, pattern in PATTERNS:
        if pattern.search(cmd):
            return kind
    return None


def is_agent(cmd: str) -> bool:
    """Whether `cmd` is an agent session's process (Claude Code or Codex)."""
    return agent_kind(cmd) is not None
