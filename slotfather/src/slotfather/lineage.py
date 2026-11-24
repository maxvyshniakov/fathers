"""Process trees and sessions on a table already read (fathers#38): parent links, ancestors, descendants and the
session rule. No command runs here; process.py reads the tables."""
from __future__ import annotations

import re
from typing import Callable, Iterable, Mapping, Sequence, cast

from fathers_common import agents as _agents

# Non-agent terminal, remote and CI sessions; agent sessions (Claude Code, Codex) come from `fathers_common.agents`.
TERMINAL_SESSION = re.compile(
    r"(^|/)(tmux|screen|zellij|mosh-server|sshd|sshd-session|login|Runner\.Listener|Runner\.Worker)$"
)


def children_of(parents: Mapping[int, int]) -> dict[int, list[int]]:
    """ppid -> its child pids, from pid -> ppid."""
    found: dict[int, list[int]] = {}
    for child, parent in parents.items():
        found.setdefault(parent, []).append(child)
    return found


def ancestors(parents: Mapping[int, int], pid: int) -> list[int]:
    """The live ancestors of `pid`, nearest parent first; cycles and PID 1 end the chain."""
    found: list[int] = []
    seen = {pid}
    parent = parents.get(pid, 0)
    while parent > 1 and parent in parents and parent not in seen:
        seen.add(parent)
        found.append(parent)
        parent = parents[parent]
    return found


def tree(children: Mapping[int, Sequence[int]], roots: Iterable[object], present: Callable[[int], bool]) -> list[int]:
    """The live processes of the trees under `roots` (each root and its descendants), depth first from the last
    root; `present` says which pids are live."""
    found: list[int] = []
    seen: set[int] = set()
    stack: list[object] = list(roots)
    while stack:
        here = stack.pop()
        if type(here) is not int or here in seen or not present(here):
            continue
        seen.add(here)
        found.append(here)
        stack.extend(children.get(here, ()))
    return found


def parents_of(rows: Mapping[int, Sequence[object]]) -> dict[int, int]:
    """pid -> ppid of a table of pid -> (ppid, ...), such as processes()."""
    return {pid: cast(int, row[0]) for pid, row in rows.items()}


def descendants(rows: Mapping[int, Sequence[object]], pid: object) -> list[int]:
    """`pid` and its live descendants, from a table of pid -> (ppid, ...); [] when `pid` is not live."""
    if type(pid) is not int or pid not in rows:
        return []
    return sorted(tree(children_of(parents_of(rows)), [pid], rows.__contains__))


def program(cmd: str) -> str:
    """The command's executable as ps shows it: its first word, or for an app bundle the path through its
    .app/Contents/ part, spaces and all."""
    found = re.match(r"(/(?:[^ ]| (?![-/]))*?\.app/Contents/\S*)", cmd)
    return found[1] if found else (cmd.split() or [""])[0]


def session_executable(cmd: str) -> bool:
    """Whether `cmd` is an agent session (Claude Code or Codex, via `fathers_common.agents`) or a terminal
    multiplexer, sshd, login or CI runner executable."""
    return _agents.is_agent(cmd) or bool(TERMINAL_SESSION.search(program(cmd)))


def session_command(cmd: str, tty: str = "") -> bool:
    """The one session rule (`procs` and the sweep): a process with a terminal, or whose command is an agent
    session or a terminal multiplexer, sshd, login or CI runner."""
    return bool(tty) or session_executable(cmd)
