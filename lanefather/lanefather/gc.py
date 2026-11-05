"""Conservative collection of merged idle agent worktrees and their unused branches.

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
import subprocess
import time
from typing import Any

from . import config, procs, state, worktrees
from .errors import LaneError
from .gitops import common_dir, git, git_ok, main_checkout, worktree_list

IDLE_SECONDS = 2 * 3600
DIRS = ("agent-*", "lane-*")
BRANCHES = ("worktree-agent-*", "lane/*")


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
        self.root = main_checkout(common_dir(root))
        self.settings = settings
        self.store = state.Store(settings)
        self.processes = procs.source(settings)

    def run(self, dry_run: bool = False) -> Result:
        result = Result()
        with self.store.locked("state"):
            targets = git(self.root, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/dev",
                          "refs/remotes/origin/main", "refs/heads/integrate/").splitlines()
            if not targets:
                result.kept.append({"path": self.root, "reason": "no merge target refs"})
                return result
            records = [r for r in self.store.lanes(strict=True) if r.get("status") != "done"]
            entries = worktree_list(self.root)
            removed: set[str] = set()
            for entry in entries:
                path = entry["path"]
                if not path.startswith(os.path.realpath(Path(self.root) / ".claude/worktrees") + "/"):
                    continue
                reason = self._keep(entry, targets, records)
                if reason:
                    result.kept.append({"path": path, "reason": reason})
                    continue
                # Read again immediately before removal: don't act on an earlier state snapshot if Git changed.
                current = next((w for w in worktree_list(self.root) if w["path"] == path), None)
                reason = self._keep(current, targets, records) if current else "worktree disappeared"
                if reason:
                    result.kept.append({"path": path, "reason": reason})
                    continue
                try:
                    self._remove(entry, dry_run, result)
                except (LaneError, OSError) as error:
                    result.errors.append(f"{path}: {error}")
                    continue
                removed.add(path)
            in_use = {e.get("branch") for e in entries if e["path"] not in removed}
            in_use.update(f"worktree-{os.path.basename(e['path'])}" for e in entries if e["path"] not in removed)
            in_use.update(r.get("branch") for r in records)
            for branch in git(self.root, "for-each-ref", "--format=%(refname:short)",
                              "refs/heads/worktree-agent-*", "refs/heads/lane/").splitlines():
                if branch in in_use or not any(git_ok(self.root, "merge-base", "--is-ancestor",
                                                     f"refs/heads/{branch}", t) for t in targets):
                    continue
                try:
                    if not dry_run:
                        git(self.root, "branch", "-D", branch)
                    result.removed.append({"kind": "branch", "path": branch})
                except LaneError as error:
                    result.errors.append(str(error))
        return result

    def _keep(self, entry: dict[str, str], targets: list[str], records: list[state.Record]) -> str | None:
        path, branch = entry["path"], entry.get("branch", "")
        if not (any(fnmatch.fnmatchcase(os.path.basename(path), p) for p in DIRS)
                or any(fnmatch.fnmatchcase(branch, p) for p in BRANCHES)):
            return "not an agent or lane worktree"
        if branch in ("dev", "main") or branch.startswith("integrate/"):
            return f"it has {branch} checked out"
        if any(os.path.realpath(r.get("worktree") or "/") == path or r.get("branch") == branch for r in records):
            return "a lane record that is not done owns it"
        lock = entry.get("locked")
        if lock is not None and not lock.startswith("claude agent"):
            return f"locked: {lock}"
        if not os.path.isdir(path) or os.path.islink(path):
            return "missing or symbolic-link worktree"
        if Path(path, ".keep").exists():
            return ".keep: " + Path(path, ".keep").read_text(errors="replace").strip()[:120]
        activity = _last_activity(path)
        if time.time() - activity < IDLE_SECONDS:
            return f"active {(time.time() - activity) / 3600:.1f} h ago (under 2 h)"
        if not any(git_ok(path, "merge-base", "--is-ancestor", "HEAD", t) for t in targets):
            return "HEAD is not merged"
        if git(path, "--no-optional-locks", "status", "--porcelain"):
            return "uncommitted or untracked changes"
        if isinstance(self.processes, procs.NoSource):
            return "no process source; worktree use is unknown"
        _, held = self.processes.sort_for_removal(path)
        return "in use by " + "; ".join(held[:3]) if held else None

    def _remove(self, entry: dict[str, str], dry_run: bool, result: Result) -> None:
        path = entry["path"]
        left, held = self.processes.sort_for_removal(path)
        if held:
            raise LaneError("in use by " + "; ".join(held))
        size = worktrees.size(path)
        if not dry_run:
            if left:
                remaining = self.processes.stop(left, path)
                if remaining:
                    raise LaneError("still in use after process cleanup: "
                                    + "; ".join(p.describe() for p in remaining))
            # Changes can arrive while processes stop. Every retention check must still pass.
            if git(path, "--no-optional-locks", "status", "--porcelain") or Path(path, ".keep").exists():
                raise LaneError("changed during process cleanup; left in place")
            if entry.get("locked") is not None:
                git(self.root, "worktree", "unlock", path)
            git(self.root, "worktree", "remove", path)
        result.removed.append({"kind": "worktree", "path": path, "branch": entry.get("branch"), "bytes": size,
                               "stopped": [p.describe() for p in left]})
        result.freed += size
        hook = self.settings.gc_after_remove
        if not dry_run and hook:
            project = str(self.settings.path.parent) if self.settings.path else self.root
            argv = [s.replace("{worktree}", path).replace("{name}", os.path.basename(path))
                    .replace("{project}", project) for s in hook]
            run = subprocess.run(argv, cwd=self.root, capture_output=True, text=True)
            if run.returncode:
                result.errors.append(f"after_remove {path}: exit {run.returncode}: {run.stderr.strip()}")


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
