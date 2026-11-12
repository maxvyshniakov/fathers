"""Git calls: one subprocess each, refusing with the command and its error."""
from __future__ import annotations

import os
import subprocess
from typing import Any

from .errors import LaneError


def git(cwd: Any, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode:
        raise LaneError(f"git {' '.join(args)} in {cwd}: {result.stderr.strip()}")
    return result.stdout.strip()


def git_ok(cwd: Any, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def common_dir(cwd: Any) -> str:
    return os.path.realpath(git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def main_checkout(common: str) -> str:
    return os.path.dirname(common) if os.path.basename(common) == ".git" else common


def current_branch(worktree: Any) -> str:
    return git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)


def worktree_list(cwd: Any) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    entry: dict[str, str] = {}
    for line in git(cwd, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if entry:
                entries.append(entry)
            entry = {}
        elif line.startswith("worktree "):
            entry["path"] = os.path.realpath(line[len("worktree "):])
        elif line.startswith("branch refs/heads/"):
            entry["branch"] = line[len("branch refs/heads/"):]
        elif line == "locked" or line.startswith("locked "):
            entry["locked"] = line[len("locked "):] or "no reason given"
    return entries


def worktree_lock(cwd: Any, worktree: str) -> str | None:
    """The lock reason of a locked worktree (the Agent harness locks its worktrees while it runs), else None."""
    path = os.path.realpath(worktree)
    return next((w.get("locked") for w in worktree_list(cwd) if w["path"] == path), None)


def dirty(worktree: Any) -> str:
    return git(worktree, "status", "--porcelain")


def resolve(cwd: Any, ref: str) -> str | None:
    """The commit `ref` names here, or None."""
    return git(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False) or None


def is_ancestor(cwd: Any, ancestor: str, descendant: str) -> bool:
    return git_ok(cwd, "merge-base", "--is-ancestor", ancestor, descendant)
