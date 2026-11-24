"""Agent-session process patterns shared across the fathers packages (fathers#146).

One pattern table for Claude Code and Codex session executables so Slotfather and Spawnfather agree on whether a
process is a live agent session. Matches only the command's executable (and, when launched through an interpreter or
`env`, its script argument), never arbitrary command arguments. Imports only `os` and `re`.
"""
from __future__ import annotations

import os
import re

CLAUDE = re.compile(r"(^|/)claude$|/claude/versions/[^/\s]+$|/claude\.app/Contents/MacOS/claude$")
CODEX = re.compile(r"(^|/)(codex|CodexCLI)$|/codex-[\w-]+/vendor/[^\s]*/codex$")

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("claude", CLAUDE),
    ("codex", CODEX),
)

_APP_BUNDLE = re.compile(r"(/(?:[^ ]| (?![-/]))*?\.app/Contents/\S*)")
_INTERPRETER = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|Python|node|bun|deno)$")
_ENV_VALUE_FLAGS = frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})
_INLINE_CODE_FLAGS = frozenset({"-c", "-m", "-e", "--eval", "-p", "--print"})
_INTERPRETER_VALUE_FLAGS = frozenset({"-W", "-X", "-r", "--require", "--import", "--loader", "--conditions", "-C"})

__all__ = ["CLAUDE", "CODEX", "PATTERNS", "agent_kind", "is_agent", "is_claude", "is_codex"]


def _split_executable(cmd: str) -> tuple[str, list[str]]:
    """Split `cmd` into its executable (preserving macOS `.app/Contents/...` paths with spaces) and remaining words."""
    stripped = cmd.strip()
    if not stripped:
        return "", []
    matched = _APP_BUNDLE.match(stripped)
    if matched:
        return matched[1], stripped[matched.end():].split()
    words = stripped.split()
    return words[0], words[1:]


def _unwrap_env(exe: str, rest: list[str]) -> tuple[str, list[str]]:
    """If `exe` is `env`, skip its options and `NAME=VALUE` assignments to reach the wrapped command."""
    if os.path.basename(exe) != "env":
        return exe, rest
    index = 0
    while index < len(rest):
        word = rest[index]
        if word == "--":
            index += 1
            break
        if word in _ENV_VALUE_FLAGS:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        if "=" in word and not word.startswith("="):
            index += 1
            continue
        break
    if index >= len(rest):
        return exe, []
    return _split_executable(" ".join(rest[index:]))


def _script_arg(rest: list[str]) -> str | None:
    """The first non-option script argument passed to an interpreter, or None for inline code / no script."""
    index = 0
    while index < len(rest):
        word = rest[index]
        if word == "--":
            return rest[index + 1] if index + 1 < len(rest) else None
        if word in _INLINE_CODE_FLAGS:
            return None
        if word in _INTERPRETER_VALUE_FLAGS:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        return word
    return None


def _targets(cmd: str) -> tuple[str, ...]:
    """The executable of `cmd`, plus its script argument when `cmd` runs through a known interpreter."""
    exe, rest = _unwrap_env(*_split_executable(cmd))
    if not exe:
        return ()
    if _INTERPRETER.match(os.path.basename(exe)):
        script = _script_arg(rest)
        if script:
            return (exe, script)
    return (exe,)


def is_claude(cmd: str) -> bool:
    """Whether `cmd` is a Claude Code session's process."""
    return any(CLAUDE.search(target) for target in _targets(cmd))


def is_codex(cmd: str) -> bool:
    """Whether `cmd` is a Codex session's process."""
    return any(CODEX.search(target) for target in _targets(cmd))


def agent_kind(cmd: str) -> str | None:
    """The agent kind ('claude' or 'codex') if `cmd` is an agent session's process, else None."""
    for target in _targets(cmd):
        for kind, pattern in PATTERNS:
            if pattern.search(target):
                return kind
    return None


def is_agent(cmd: str) -> bool:
    """Whether `cmd` is an agent session's process (Claude Code or Codex)."""
    return agent_kind(cmd) is not None
