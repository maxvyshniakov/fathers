"""Which processes Junkfather may adopt, signal or report: one policy for `adopt`, a reap, `reap --pid --force`, the
sweep's unowned report and its unowned resources (docs/reference.md, Ending an entry and Sweep).

Never touched, nor anything below them: a process with a terminal and an agent session (Claude Code, Codex) are
`unsafe`; pid 0 and 1, Junkfather itself and its ancestors are `protected`; another user's process is not ours.
An unowned process is only reported, and stopped only by `reap --pid --force` when the last sweep listed it and it
is still that process; an unowned resource is only ever reported.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set

from . import procs as _procs
from .state import Store, as_dict, as_list, read_json

CLAUDE = re.compile(r"(^|/)claude(\s|$)|/claude/versions/[^/\s]+(\s|$)|/claude\.app/Contents/MacOS/claude(\s|$)")
CODEX = re.compile(r"(^|/)(codex|CodexCLI)(\s|$)|/codex-[\w-]+/vendor/[^\s]*/codex(\s|$)")


def claude(cmd: str) -> bool:
    """A Claude Code session's process, by its command."""
    return bool(CLAUDE.search(cmd))


def codex(cmd: str) -> bool:
    """A Codex session's process, by its command."""
    return bool(CODEX.search(cmd))


def agent(proc: _procs.Proc) -> bool:
    """An agent session's process (Claude Code or Codex)."""
    return claude(proc.cmd) or codex(proc.cmd)


def protected(table: _procs.Table) -> Set[int]:
    """pid 0 and 1, this process and its ancestors."""
    me = os.getpid()
    return {0, 1, me, *table.ancestors(me)}


def unsafe(proc: _procs.Proc) -> bool:
    """A process no reap may touch, nor anything below it: a terminal's or an agent session."""
    return bool(proc.tty) or agent(proc)


def untouchable_tree(pid: int, table: _procs.Table) -> bool:
    """Whether anything below `pid` is unsafe (a tmux server and its panes, an agent started from it)."""
    return any(unsafe(p) for p in table.descendants(pid))


def below_session(pid: int, table: _procs.Table, sessions_alive: Mapping[int, bool]) -> bool:
    """Whether `pid` runs below a live agent session (its shells, MCP and language servers at work)."""
    return any(sessions_alive.get(ancestor) for ancestor in table.ancestors(pid))


def signallable(found: Iterable[_procs.Proc], table: _procs.Table, uid: Optional[int] = None) -> List[_procs.Proc]:
    """Those of `found` a reap may signal: this user's, not protected, neither unsafe nor below an unsafe one of
    them."""
    uid = os.getuid() if uid is None else uid
    found = list(found)
    skip = protected(table)
    excluded = {p.pid for p in found if unsafe(p)}
    for pid in excluded:
        excluded = excluded | {p.pid for p in table.descendants(pid)}
    return [p for p in found if p.uid == uid and p.pid not in skip and p.pid not in excluded]


def listed_unowned(store: Store, pid: int) -> Optional[Dict[str, Any]]:
    """The last sweep's report of this unowned pid (unowned.json), or None: what `reap --pid --force` may stop."""
    for item in as_list(as_dict(read_json(store.root / "unowned.json")).get("items")):
        if isinstance(item, dict) and as_dict(item).get("pid") == pid:
            return as_dict(item)
    return None


def adopt_refusal(pid: int, table: _procs.Table, owned: Callable[[int], bool]) -> Optional[str]:
    """Why `adopt` may not own this running pid, or None. `owned` tells whether an entry holds it already."""
    proc = table.get(pid)
    if proc is None:
        return "no such process"
    if proc.uid != os.getuid():
        return "another user's process"
    if pid in protected(table):
        return "junkfather itself or one of its ancestors"
    if agent(proc):
        return "an agent session"
    if proc.tty:
        return f"a process with a terminal ({proc.tty})"
    if untouchable_tree(pid, table):
        return "its tree holds a terminal's process or an agent session"
    if owned(pid):
        return "already owned"
    return None


def force_refusal(pid: int, table: _procs.Table, store: Store, owned: Callable[[int], bool]) -> Optional[str]:
    """Why `reap --pid --force` may not stop this unowned pid, or None: the last sweep must have listed it, and it
    must still be that process and still unowned. `owned` tells whether an entry holds it now."""
    proc = table.get(pid)
    listed = listed_unowned(store, pid)
    if listed is None:
        return "the last sweep did not list it (run junkfather sweep)"
    if proc is None:
        return "it no longer runs"
    if proc.start != _procs.norm(listed.get("start") or ""):
        return "the pid now belongs to another process (its start time changed)"
    if listed.get("cmd") is not None and not _procs.command_matches(proc.cmd, str(listed["cmd"])):
        return "the pid now belongs to another process (its command changed)"
    if proc.uid != os.getuid() or unsafe(proc) or pid in protected(table):
        return "not a process junkfather may stop"
    if untouchable_tree(pid, table):
        return "its tree holds a terminal's process or an agent session"
    if owned(pid):
        return "an entry owns it now (reap that entry)"
    return None


def forced_targets(pid: int, table: _procs.Table) -> List[_procs.Proc]:
    """What `reap --pid --force` stops once `force_refusal` let it: the process and its descendants that are this user's
    and not protected, leaving out unsafe ones with their subtrees."""
    proc = table.get(pid)
    if proc is None:
        return []
    skip = protected(table)
    return [proc] + [p for p in table.descendants(pid, prune=unsafe) if p.uid == os.getuid() and p.pid not in skip]


def reportable(proc: _procs.Proc, uid: int, skip: Set[int], owned: Set[int]) -> bool:
    """Whether the sweep may report this process as unowned at all: this user's, not protected, not owned, and
    neither a terminal's process nor an agent session (its tree is checked separately)."""
    return proc.uid == uid and proc.pid not in skip and proc.pid not in owned and not unsafe(proc)


def unowned_resource(resource: Mapping[str, Any], key: str, owned: Mapping[str, str]) -> Optional[Dict[str, Any]]:
    """The report item for a running resource no entry owns, or None when one does. It is only ever reported: no
    command stops a resource that no entry owns."""
    if key in owned:
        return None
    return {**resource, "key": key, "why": f"a running {resource.get('kind')} no entry owns"}
