"""Git calls: one subprocess each, refusing with the command and its error."""
from __future__ import annotations

import os
import subprocess
from typing import Any

from . import config
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


def commits(count: int) -> str:
    return f"{count} commit" + ("" if count == 1 else "s")


def tracking(cwd: str, remote: str, branch: str) -> tuple[str | None, str | None]:
    """Local <branch> and <remote>/<branch>, each None when absent."""
    local = git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False) or None
    upstream = git(cwd, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}^{{commit}}",
                   check=False) or None
    return local, upstream


def ahead_lines(cwd: str, remote: str = "origin") -> list[str]:
    """A warning for each promoted branch that has commits <remote> lacks, as last fetched."""
    lines: list[str] = []
    for branch in config.current().promoted:
        local, upstream = tracking(cwd, remote, branch)
        if local and upstream and local != upstream:
            count = int(git(cwd, "rev-list", "--count", f"{upstream}..{local}", check=False) or 0)
            if count:
                lines.append(f"local {branch} is {commits(count)} ahead of {remote}; `{config.prog()} sync "
                             "--drop-local` drops them with a backup")
    return lines


def move_branch(
    cwd: str,
    branch: str,
    old: str,
    new: str,
    env: dict[str, str] | None,
    reason: str,
    *,
    checked_out: dict[str, str] | None = None,
    reset: bool = False,
) -> str | None:
    """Move local `branch` from `old` to `new` (ff-only merge or reset --keep in its worktree, else update-ref).

    Returns None on success or an error line on failure."""
    if checked_out is None:
        checked_out = {w["branch"]: w["path"] for w in worktree_list(cwd) if w.get("branch")}
    where = checked_out.get(branch)
    if where:
        argv = ["git", "reset", "-q", "--keep", new] if reset else ["git", "merge", "--ff-only", "-q", new]
    else:
        argv = ["git", "update-ref", "-m", reason, f"refs/heads/{branch}", new, old or ""]
    moved = subprocess.run(argv, cwd=where or cwd, env=env, capture_output=True, text=True)
    if moved.returncode:
        return f"local {branch} in {where or cwd}: {(moved.stdout + moved.stderr).strip()}"
    return None
