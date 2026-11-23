"""Folders as resources (fathers#171): the `dir` kind's one guarded removal (`plan`) and its guards.

A requester (Lanefather) owns the folders a lane made, its scratch and its build directories, as `dir` resources;
a reap removes one with `rm -rf` only when every guard passes, all read again right before the removal. Integrity
guards are never lifted:

- not `/`, the home folder or one of its parents, nor Spawnfather's state folder, a folder holding it or one inside
  it;
- not a symbolic link, and a folder (not a file);
- spawnfather itself is not running inside it;
- no Git repository and no worktree inside it, the folder itself included (a lane's scratch can hold a base checkout
  a killed check left: its requester removes that first, through git);
- the process table and the working directories can be read, and no process of this user's has a working directory
  that cannot be read.

The one work guard, which `--override REASON` lifts (the stop event names it): no process works inside it (its
working directory is in it, or its command names it). `--stop-inside` stops the unowned ones first
(resources.stop_inside).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

from . import procs as _procs
from .resources import Refused
from .state import Store
from .worktrees import NONE, Evidence, users

__all__ = ["Evidence", "integrity_refusal", "plan"]


def _within(path: str, other: str) -> bool:
    return path == other or path.startswith(other.rstrip("/") + "/")


def holds_repository(path: str) -> Optional[str]:
    """The first Git repository or worktree (a `.git` file or folder) in the folder, the folder itself included."""
    for folder, directories, files in os.walk(path):
        if ".git" in directories or ".git" in files:
            return os.path.relpath(folder, path)
    return None


def integrity_refusal(path: str) -> Optional[str]:
    """Why this folder may never be removed, or None (the cheap guards first, then the walk for repositories)."""
    if os.path.islink(path):
        return "a symbolic link"
    if not os.path.isdir(path):
        return "not a folder"
    home = os.path.realpath(str(Path.home()))
    state = os.path.realpath(str(Store().root))
    if path == "/" or _within(home, path):
        return "the root, the home folder or one of its parents"
    if _within(state, path) or _within(path, state):
        return "Spawnfather's state folder"
    here = os.path.realpath(os.getcwd())
    if _within(here, path):
        return "spawnfather runs inside it"
    repository = holds_repository(path)
    if repository is not None:
        where = "itself" if repository == "." else f"at {repository}"
        return f"it holds a Git repository or worktree {where}"
    return None


def plan(resource: Mapping[str, Any], exclude: Sequence[int] = (), evidence: Evidence = NONE) -> List[List[str]]:
    """The one guarded removal of a folder: every guard read again now, then the step to run (`rm -rf -- PATH`);
    Refused, saying which guard keeps it, otherwise."""
    path = str(resource.get("id"))
    why = integrity_refusal(path)
    if why is not None:
        raise Refused(f"kept: {why}")
    split = users(path, _procs.read_or_none(), _procs.working_dirs_or_none(os.getuid()), exclude)
    if split is None:
        raise Refused("kept: whether a process works inside it cannot be told")
    seen, unknown = split
    if unknown:
        raise Refused(f"kept: in use by {'; '.join(unknown[:3])}")
    lifted: List[str] = []
    if seen:
        why = f"in use by {'; '.join(seen[:3])}"
        if not evidence.override:
            raise Refused(f"kept: {why}")
        lifted.append(why)
    evidence.lifted[:] = lifted
    return [["rm", "-rf", "--", path]]
