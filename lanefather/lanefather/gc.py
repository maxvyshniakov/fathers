"""Conservative collection of merged idle agent worktrees and their unused branches.

The candidates are the worktrees under the main checkout's .claude/worktrees whose name, path or branch matches the
project's [gc] dirs and branches (agent, lane, Claude Code and Codex session worktrees by default, fathers#44); the
branches deleted are the merged ones the branch patterns name that no worktree or lane uses.

Agent and lane worktrees (AGENT_DIRS, AGENT_BRANCHES) are throwaway: merged into origin or a local integrate/*
branch, clean, idle and unused is enough. Any other candidate is a session worktree an owner may come back to, so
it is kept unless its HEAD reached origin/dev or origin/main, it has a commit of its own, and it holds no ignored
file outside [gc] disposable. No candidate that holds another worktree or Git repository is removed.

One Project value owns the configuration, state snapshot and process source for this pass. Git and slotfather's
project adapter supply observations; cleanup policy lives here, rather than in a project's janitor. Project build
caches and scratchpads are outside this module: [gc] after_remove runs only after a worktree was actually removed.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, cast

from . import config, procs, state, worktrees
from .errors import LaneError
from .gitops import common_dir, git, git_ok, main_checkout, worktree_list

IDLE_SECONDS = 2 * 3600
# The worktrees and branches gc collected before fathers#44, under the rules it had then.
AGENT_DIRS = ("agent-*", "lane-*")
AGENT_BRANCHES = ("worktree-agent-*", "lane/*")
# A HEAD reflog entry that records work done in the worktree, not a checkout or reset.
OWN_WORK = ("commit", "merge", "cherry-pick", "rebase", "pull", "revert", "am")


@dataclass
class Result:
    removed: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    kept: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    errors: list[str] = field(default_factory=list[str])
    freed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"v": 1, "removed": self.removed, "kept": self.kept, "errors": self.errors, "freed": self.freed}


class Project:
    """Collect one repository under one explicit configuration; run is the caller's only operation."""

    def __init__(self, root: str, settings: config.Config):
        self._root = main_checkout(common_dir(root))
        self._invoked_from = os.path.realpath(os.getcwd())
        self._settings = settings
        self._store = state.Store(settings)
        self._processes = procs.source(settings)

    def run(self, dry_run: bool = False) -> Result:
        result = Result()
        with self._store.locked("state"):
            targets = git(self._root, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/dev",
                          "refs/remotes/origin/main", "refs/heads/integrate/").splitlines()
            if not targets:
                result.kept.append({"path": self._root, "reason": "no merge target refs"})
                return result
            records = [r for r in self._store.lanes(strict=True) if r.get("status") != "done"]
            entries = worktree_list(self._root)
            removed: set[str] = set()
            for entry in entries:
                path = entry["path"]
                if not path.startswith(os.path.realpath(Path(self._root) / ".claude/worktrees") + "/"):
                    continue
                reason = self._keep(entry, targets, records)
                if reason:
                    result.kept.append({"path": path, "reason": reason})
                    continue
                # Read again immediately before removal: don't act on an earlier state snapshot if Git changed.
                current = next((w for w in worktree_list(self._root) if w["path"] == path), None)
                reason = self._keep(current, targets, records) if current else "worktree disappeared"
                if reason:
                    result.kept.append({"path": path, "reason": reason})
                    continue
                assert current is not None
                try:
                    self._remove(current, targets, dry_run, result)
                except (LaneError, OSError) as error:
                    result.errors.append(f"{path}: {error}")
                    continue
                removed.add(path)
            in_use = {e.get("branch") for e in entries if e["path"] not in removed}
            in_use.update(f"worktree-{os.path.basename(e['path'])}" for e in entries if e["path"] not in removed)
            in_use.update(r.get("branch") for r in records)
            for branch in git(self._root, "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines():
                if not self._branch_collected(branch) or branch in in_use:
                    continue
                agent = any(fnmatch.fnmatchcase(branch, p) for p in AGENT_BRANCHES)
                if not any(git_ok(self._root, "merge-base", "--is-ancestor", f"refs/heads/{branch}", t)
                           for t in (targets if agent else upstream(targets))):
                    continue
                try:
                    if not dry_run:
                        git(self._root, "branch", "-D", branch)
                    result.removed.append({"kind": "branch", "path": branch})
                except LaneError as error:
                    result.errors.append(str(error))
        return result

    def _keep(self, entry: dict[str, str], targets: list[str], records: list[state.Record]) -> str | None:
        path, branch = entry["path"], entry.get("branch", "")
        if self._invoked_from == path or self._invoked_from.startswith(path + "/"):
            return "the collector is running inside this worktree"
        if not (self._dir_collected(path) or self._branch_collected(branch)):
            return "not a collected worktree: neither [gc] dirs nor [gc] branches names it"
        if branch in ("dev", "main") or branch.startswith("integrate/"):
            return f"it has {branch} checked out"
        inside = next((w["path"] for w in worktree_list(self._root) if w["path"].startswith(path + "/")), None)
        if inside:
            return f"it holds the worktree {inside}"
        if any(os.path.realpath(r.get("worktree") or "/") == path or r.get("branch") == branch for r in records):
            return "a lane record that is not done owns it"
        lock = entry.get("locked")
        if lock is not None and not lock.startswith("claude agent"):
            return f"locked: {lock}"
        holder = re.search(r"\(pid (\d+)\)", lock or "")
        if holder and _alive(int(holder[1])):
            return f"locked by a live claude agent (pid {holder[1]})"
        if not os.path.isdir(path) or os.path.islink(path):
            return "missing or symbolic-link worktree"
        if Path(path, ".keep").exists():
            return ".keep: " + Path(path, ".keep").read_text(errors="replace").strip()[:120]
        activity = _last_activity(path)
        if time.time() - activity < IDLE_SECONDS:
            return f"active {(time.time() - activity) / 3600:.1f} h ago (under 2 h)"
        session = not (any(fnmatch.fnmatchcase(os.path.basename(path), p) for p in AGENT_DIRS)
                       or any(fnmatch.fnmatchcase(branch, p) for p in AGENT_BRANCHES))
        if session:
            if not any(git_ok(path, "merge-base", "--is-ancestor", "HEAD", t) for t in upstream(targets)):
                return "HEAD is not merged into origin/dev or origin/main"
        elif not any(git_ok(path, "merge-base", "--is-ancestor", "HEAD", t) for t in targets):
            return "HEAD is not merged"
        if git(path, "--no-optional-locks", "status", "--porcelain"):
            return "uncommitted or untracked changes"
        if session and not self._settings.gc_collect_uncommitted_sessions and not _own_work(path):
            return ("no commit of its own: the session may still be open "
                    "([gc] collect_uncommitted_sessions = true collects it)")
        if session:
            held = self._ignored(path)
            if held:
                return f"ignored files: {', '.join(held[:3])}{' ...' if len(held) > 3 else ''} (not [gc] disposable)"
        nested = _nested_repository(path)
        if nested:
            return f"it holds a Git repository at {nested}"
        if isinstance(self._processes, procs.NoSource):
            return "no process source; worktree use is unknown"
        _, held = self._processes.sort_for_removal(path)
        return "in use by " + "; ".join(held[:3]) if held else None

    def _ignored(self, path: str) -> list[str]:
        """The ignored paths in the worktree that no [gc] disposable pattern names a component of."""
        found = git(path, "--no-optional-locks", "status", "--porcelain", "--ignored")
        return [line[3:] for line in found.splitlines() if line.startswith("!! ")
                and not any(fnmatch.fnmatchcase(part, p) for part in line[3:].rstrip("/").split("/")
                            for p in self._settings.gc_disposable)]

    def _dir_collected(self, path: str) -> bool:
        """A pattern with a '/' matches the path from the main checkout, one without the directory's name."""
        relative = os.path.relpath(path, os.path.realpath(self._root))
        return any(fnmatch.fnmatchcase(relative if "/" in p else os.path.basename(path), p)
                   for p in self._settings.gc_dirs)

    def _branch_collected(self, branch: str) -> bool:
        if not branch or branch in ("dev", "main") or branch.startswith("integrate/"):
            return False
        return any(fnmatch.fnmatchcase(branch, p) for p in self._settings.gc_branches)

    def _remove(self, entry: dict[str, str], targets: list[str], dry_run: bool, result: Result) -> None:
        path = entry["path"]
        left, held = self._processes.sort_for_removal(path)
        if held:
            raise LaneError("in use by " + "; ".join(held))
        size = worktrees.size(path)
        if not dry_run:
            if left:
                remaining = self._processes.stop(left, path)
                if remaining:
                    raise LaneError("still in use after process cleanup: "
                                    + "; ".join(p.describe() for p in remaining))
            # Termination handlers can change HEAD, ownership or the retention lock. Re-run the entire policy
            # after stopping, rather than duplicating selected predicates from _keep.
            current = next((w for w in worktree_list(self._root) if w["path"] == path), None)
            records = [r for r in self._store.lanes(strict=True) if r.get("status") != "done"]
            reason = self._keep(current, targets, records) if current else "worktree disappeared"
            if reason:
                raise LaneError(f"retained after process cleanup: {reason}")
            further, held = self._processes.sort_for_removal(path)
            if further or held:
                raise LaneError("still in use after process cleanup: "
                                + "; ".join([p.describe() for p in further] + held))
            assert current is not None
            entry = current
            if entry.get("locked") is not None:
                git(self._root, "worktree", "unlock", path)
            git(self._root, "worktree", "remove", path)
        result.removed.append({"kind": "worktree", "path": path, "branch": entry.get("branch"), "bytes": size,
                               "stopped": [p.describe() for p in left]})
        result.freed += size
        hook = self._settings.gc_after_remove
        if not dry_run and hook:
            project = str(self._settings.path.parent) if self._settings.path else self._root
            argv = [s.replace("{worktree}", path).replace("{name}", os.path.basename(path))
                    .replace("{project}", project) for s in hook]
            run = subprocess.run(argv, cwd=self._root, capture_output=True, text=True)
            if run.returncode:
                result.errors.append(f"after_remove {path}: exit {run.returncode}: {run.stderr.strip()}")
            elif self._settings.gc_after_remove_report:
                try:
                    decoded: Any = json.loads(run.stdout)
                    if not isinstance(decoded, dict):
                        raise ValueError("expected an object")
                    report = cast(dict[str, Any], decoded)
                    if (type(report.get("v")) is not int or report["v"] != 1
                            or type(report.get("freed")) is not int or report["freed"] < 0):
                        raise ValueError("expected {v: 1, freed: non-negative integer}")
                    result.freed += report["freed"]
                except ValueError as error:
                    result.errors.append(f"after_remove {path}: invalid cleanup report: {error}")


def upstream(targets: list[str]) -> list[str]:
    """The targets that are origin's: work merged only into a local integrate/* branch is not yet upstream."""
    return [t for t in targets if t.startswith("refs/remotes/")]


def _own_work(path: str) -> bool:
    """Whether the worktree's HEAD reflog records a commit, merge or rebase made in it; without a reflog, no."""
    found = subprocess.run(["git", "-C", path, "reflog", "show", "--format=%gs", "HEAD", "--"],
                           capture_output=True, text=True)
    return found.returncode == 0 and any(line.startswith(OWN_WORK) for line in found.stdout.splitlines())


def _nested_repository(path: str) -> str | None:
    """The first Git repository or worktree (a .git file or directory) below the worktree's top, ignored
    directories included: removing the worktree would delete it."""
    for folder, directories, files in os.walk(path):
        if folder == path:
            directories[:] = [d for d in directories if d != ".git"]
            continue
        if ".git" in directories or ".git" in files:
            return os.path.relpath(folder, path)
    return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _last_activity(path: str) -> float:
    gitdir = git(path, "rev-parse", "--absolute-git-dir")
    times: list[float] = []
    for candidate in (path, *(os.path.join(gitdir, n) for n in ("HEAD", "index", "logs/HEAD"))):
        with contextlib.suppress(OSError):
            times.append(os.stat(candidate).st_mtime)
    return max(times, default=time.time())


def collect(args: Any) -> int:
    result = Project(os.getcwd(), config.current()).run(args.dry_run)
    if args.json:
        print(json.dumps(result.as_dict()))
    else:
        for item in result.removed:
            print(f"{'would remove' if args.dry_run else 'removed'} {item['kind']} {item['path']}")
        for item in result.kept:
            print(f"keep {item['path']}: {item['reason']}")
        for error in result.errors:
            print(f"error: {error}")
    return 0
