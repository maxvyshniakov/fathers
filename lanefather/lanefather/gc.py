"""Conservative collection of merged idle agent worktrees, unused branches, orphaned scratch dirs and old logs.

The candidates are the worktrees under the main checkout's .claude/worktrees whose name, path or branch matches the
project's [gc] dirs and branches (agent, lane, Claude Code and Codex session worktrees by default, fathers#44); the
branches deleted are the merged ones the branch patterns name that no worktree or lane uses.

Agent and lane worktrees (AGENT_DIRS, AGENT_BRANCHES) are throwaway: merged into origin or a local integrate/*
branch, clean, idle and unused is enough. Any other candidate is a session worktree an owner may come back to, so
it is kept unless its HEAD reached origin/dev or origin/main, it has a commit of its own, and it holds no ignored
file outside [gc] disposable. No candidate that holds another worktree or Git repository is removed.

One Project value owns the configuration and state snapshot for this pass. The policy lives here; worktree removal is
Spawnfather's (fathers#171): `spawnfather reap --worktree PATH`, with the merge targets as --base and [gc]
disposable as --disposable, checks its own guards (processes inside, a lock, ignored files, work not merged) again
and removes the worktree, unlocking a dead agent's lock first. Without Spawnfather every worktree candidate is kept.
Project build caches stay outside this module: [gc] after_remove runs only after a worktree was removed.

In the lane state directory (fathers#259), `gc` also sweeps orphaned or finished-lane `*.scratch` directories
(sparing any non-done lane's scratch and never following symlinks or deleting outside the state directory) and
prunes `logs/` by `[gc]` `log_max_age_days` and `log_max_mb` (oldest first, sparing live lanes' logs).
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import fnmatch
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Any, cast

from . import admission, config, done, lifecycle, lifetimes, state, worktrees
from .errors import LaneError
from .gitops import common_dir, git, git_ok, main_checkout, worktree_list

IDLE_SECONDS = 2 * 3600
# The worktrees and branches gc collected before fathers#44, under the rules it had then.
AGENT_DIRS = ("agent-*", "lane-*")
AGENT_BRANCHES = ("worktree-agent-*", "lane/*")
# A HEAD reflog entry that records work done in the worktree, not a checkout or reset.
OWN_WORK = ("commit", "merge", "cherry-pick", "rebase", "pull", "revert", "am")
_LOG_FILE = re.compile(r"(?P<label>.+)-\d{8}T\d{6}\.log")
_SECONDS_PER_DAY = 86400
_BYTES_PER_MIB = 1024 * 1024


@dataclass(frozen=True)
class _LogEntry:
    path: Path
    real: str
    name: str
    is_dir: bool
    size: int
    mtime: float
    protected: bool


class Kept(Exception):
    """Spawnfather keeps a candidate: its guard's reason."""


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
        self._spawnfather = lifetimes.client()
        self._proof: lifetimes.Evidence | None = None

    def run(self, dry_run: bool = False) -> Result:
        result = Result()
        with self._store.locked("state"):
            all_records = self._store.lanes(strict=True)
            promoted_refs = [f"refs/remotes/origin/{b}" for b in self._settings.promoted]
            targets = git(self._root, "for-each-ref", "--format=%(refname)", *promoted_refs,
                          "refs/heads/integrate/").splitlines()
            if not targets:
                result.kept.append({"path": self._root, "reason": "no merge target refs"})
            else:
                self._proof = lifetimes.Evidence(bases=tuple(targets), disposable=self._settings.gc_disposable,
                                                 stop_inside=lifetimes.stop_inside(self._settings))
                records = [r for r in all_records if not lifecycle.finished(r)]
                entries = worktree_list(self._root)
                removed: set[str] = set()
                wt_root = os.path.realpath(worktrees.worktrees_root_from_main(self._root, self._settings))
                for entry in entries:
                    path = entry["path"]
                    if not path.startswith(wt_root + "/"):
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
                    except Kept as kept:
                        result.kept.append({"path": path, "reason": f"Spawnfather keeps it: {kept}"})
                        continue
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
        self._drop_gone(dry_run, result)
        with self._store.locked("state"):
            current_records = self._store.lanes(strict=True)
            self._sweep_scratch(current_records, dry_run, result)
            self._prune_logs(current_records, dry_run, result)
        return result

    def _sweep_scratch(self, all_records: list[state.Record], dry_run: bool, result: Result) -> None:
        """Sweep orphaned or finished-lane `*.scratch` directories under `store.directory` (fathers#259)."""
        store_dir = self._store.directory
        if store_dir.is_symlink() or not store_dir.is_dir():
            return
        live_records = [r for r in all_records if not lifecycle.finished(r)]
        live_ids = {str(r["id"]) for r in live_records}
        live_scratch_reals = _record_scratch_reals(live_records)
        records_by_id = {str(r["id"]): r for r in all_records}
        try:
            with os.scandir(store_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except FileNotFoundError:
            return
        except OSError as error:
            result.errors.append(f"{store_dir}: {error}")
            return
        for entry in entries:
            if not entry.name.endswith(".scratch") or entry.name == ".scratch":
                continue
            entry_path = Path(entry.path)
            try:
                if entry.is_symlink():
                    result.kept.append({"path": str(entry_path), "reason": "symbolic link"})
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except FileNotFoundError:
                continue
            except OSError as error:
                result.errors.append(f"{entry_path}: {error}")
                continue
            if not lifetimes.within_dir(entry_path, store_dir):
                result.kept.append({"path": str(entry_path), "reason": "outside state directory"})
                continue
            target_real = os.path.realpath(entry_path)
            if self._invoked_from == target_real or self._invoked_from.startswith(target_real + os.sep):
                result.kept.append({
                    "path": target_real,
                    "reason": "the collector is running inside this scratch directory",
                })
                continue
            lane_id = entry.name.removesuffix(".scratch")
            if lane_id in live_ids or target_real in live_scratch_reals:
                continue
            size = worktrees.dir_bytes(entry_path)
            if dry_run:
                result.removed.append({"kind": "scratch", "path": target_real, "bytes": size})
                result.freed += size
                continue
            try:
                _clean_scratch_base_worktrees(entry_path, records_by_id.get(lane_id), self._root)
                removed_ok = worktrees.safe_rmtree(entry_path)
            except (LaneError, OSError) as error:
                result.errors.append(f"{target_real}: {error}")
                continue
            if removed_ok:
                result.removed.append({"kind": "scratch", "path": target_real, "bytes": size})
                result.freed += size

    def _prune_logs(self, all_records: list[state.Record], dry_run: bool, result: Result,
                    *, now: float | None = None) -> None:
        """Prune old or oversized entries in `<store.directory>/logs`, sparing live lanes' logs (fathers#259)."""
        max_age_days = self._settings.gc_log_max_age_days
        max_mb = self._settings.gc_log_max_mb
        if max_age_days == 0 and max_mb == 0:
            return
        store_dir = self._store.directory
        logs_dir = store_dir / "logs"
        if logs_dir.is_symlink():
            result.kept.append({"path": str(logs_dir), "reason": "symbolic link"})
            return
        if not logs_dir.is_dir():
            return
        if not lifetimes.within_dir(logs_dir, store_dir):
            result.kept.append({"path": str(logs_dir), "reason": "outside state directory"})
            return
        live_ids = {str(r["id"]) for r in all_records if not lifecycle.finished(r)}
        current_time = time.time() if now is None else now
        try:
            with os.scandir(logs_dir) as it:
                raw_entries = sorted(it, key=lambda e: e.name)
        except FileNotFoundError:
            return
        except OSError as error:
            result.errors.append(f"{logs_dir}: {error}")
            return
        candidates: list[_LogEntry] = []
        for entry in raw_entries:
            entry_path = Path(entry.path)
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as error:
                result.errors.append(f"{entry_path}: {error}")
                continue
            if stat.S_ISLNK(st.st_mode):
                result.kept.append({"path": str(entry_path), "reason": "symbolic link"})
                continue
            if not lifetimes.within_dir(entry_path, logs_dir):
                result.kept.append({"path": str(entry_path), "reason": "outside logs directory"})
                continue
            entry_real = os.path.realpath(entry_path)
            if stat.S_ISREG(st.st_mode):
                is_dir = False
                size = st.st_size
                mtime = st.st_mtime
            elif stat.S_ISDIR(st.st_mode):
                is_dir = True
                size = worktrees.dir_bytes(entry_path)
                mtime = _dir_mtime(entry_path, st.st_mtime)
            else:
                continue
            protected = _log_belongs_to_live_lane(entry.name, is_dir, live_ids)
            candidates.append(_LogEntry(
                path=entry_path, real=entry_real, name=entry.name,
                is_dir=is_dir, size=size, mtime=mtime, protected=protected,
            ))
        cutoff = current_time - max_age_days * _SECONDS_PER_DAY if max_age_days > 0 else None
        to_remove: list[_LogEntry] = []
        remaining: list[_LogEntry] = []
        for item in candidates:
            if cutoff is not None and not item.protected and item.mtime < cutoff:
                to_remove.append(item)
            else:
                remaining.append(item)
        if max_mb > 0:
            max_bytes = max_mb * _BYTES_PER_MIB
            total_bytes = sum(item.size for item in remaining)
            if total_bytes > max_bytes:
                evictable = sorted((item for item in remaining if not item.protected),
                                   key=lambda i: (i.mtime, i.name))
                for item in evictable:
                    if total_bytes <= max_bytes:
                        break
                    to_remove.append(item)
                    total_bytes -= item.size
        for item in sorted(to_remove, key=lambda i: (i.mtime, i.name)):
            if dry_run:
                result.removed.append({"kind": "log", "path": item.real, "bytes": item.size})
                result.freed += item.size
                continue
            try:
                if item.is_dir:
                    removed_ok = worktrees.safe_rmtree(item.path)
                else:
                    item.path.unlink()
                    removed_ok = True
            except FileNotFoundError:
                continue
            except OSError as error:
                result.errors.append(f"{item.real}: {error}")
                continue
            if removed_ok:
                result.removed.append({"kind": "log", "path": item.real, "bytes": item.size})
                result.freed += item.size

    def _drop_gone(self, dry_run: bool, result: Result) -> None:
        """Drop this repository's gone lanes (fathers#237): drop's own proof holds for them, so nothing can be lost.
        Outside the state lock, which drop takes for each lane; a lane drop refuses is an error, the others go on."""
        here = common_dir(self._root)
        for record in state.of_project(self._store.lanes(strict=True), here):
            if not admission.gone(record):
                continue
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    done.drop_lane(record["id"], self._store, dry_run=dry_run)
            except LaneError as error:
                result.errors.append(f"lane {record['id']}: {error}")
                continue
            result.removed.append({"kind": "lane", "path": record["id"]})

    def _keep(self, entry: dict[str, str], targets: list[str], records: list[state.Record]) -> str | None:
        path, branch = entry["path"], entry.get("branch", "")
        if self._invoked_from == path or self._invoked_from.startswith(path + "/"):
            return "the collector is running inside this worktree"
        if not (self._dir_collected(path) or self._branch_collected(branch)):
            return "not a collected worktree: neither [gc] dirs nor [gc] branches names it"
        if branch in self._settings.promoted or branch.startswith("integrate/"):
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
                return f"HEAD is not merged into {' or '.join(f'origin/{b}' for b in self._settings.promoted)}"
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
        if self._spawnfather is None:
            return f"{lifetimes.INSTALL_HINT}; worktree use is unknown"
        return None

    def _ignored(self, path: str) -> list[str]:
        """The ignored paths in the worktree that no [gc] disposable pattern names a component of."""
        found = git(path, "--no-optional-locks", "status", "--porcelain", "--ignored=matching")
        return [line[3:] for line in found.splitlines() if line.startswith("!! ")
                and not any(fnmatch.fnmatchcase(part, p) for part in line[3:].rstrip("/").split("/")
                            for p in self._settings.gc_disposable)]

    def _dir_collected(self, path: str) -> bool:
        """A pattern with a '/' matches the path from the main checkout, one without the directory's name."""
        relative = os.path.relpath(path, os.path.realpath(self._root))
        patterns = self._settings.gc_dirs
        default_cfg = config.Config()
        if self._settings.worktrees_root != default_cfg.worktrees_root and patterns == default_cfg.gc_dirs:
            patterns = (*patterns, f"{self._settings.worktrees_root.strip('/')}/*")
        return any(fnmatch.fnmatchcase(relative if "/" in p else os.path.basename(path), p) for p in patterns)

    def _branch_collected(self, branch: str) -> bool:
        if not branch or branch in self._settings.promoted or branch.startswith("integrate/"):
            return False
        return any(fnmatch.fnmatchcase(branch, p) for p in self._settings.gc_branches)

    def _remove(self, entry: dict[str, str], targets: list[str], dry_run: bool, result: Result) -> None:
        """Spawnfather's guarded removal (or its dry run) of one candidate _keep let through; what it keeps is kept,
        with its reason."""
        del targets
        path = entry["path"]
        assert self._spawnfather is not None and self._proof is not None
        size = worktrees.size(path)
        reaped = self._spawnfather.remove_worktree(path, self._proof, dry_run=dry_run, reason="lanefather gc")
        if reaped.kept:
            raise Kept("; ".join(k.removeprefix(f"{path}: ").removeprefix("kept: ") for k in reaped.kept))
        left = reaped.stopped
        result.removed.append({"kind": "worktree", "path": path, "branch": entry.get("branch"), "bytes": size,
                               "stopped": left})
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


def _record_scratch_reals(records: list[state.Record]) -> set[str]:
    found: set[str] = set()
    for record in records:
        for path in lifetimes.record_scratch_paths(record):
            with contextlib.suppress(OSError):
                found.add(os.path.realpath(path))
    return found


def _clean_scratch_base_worktrees(scratch: Path, record: state.Record | None, fallback_root: str) -> None:
    """Remove any stale `base-*` worktrees inside `scratch` through git before removing the scratch directory."""
    record_main: str | None = None
    if record is not None:
        common = record.get("common_dir")
        if isinstance(common, str) and common and os.path.isdir(common):
            record_main = main_checkout(common)
    for stale in sorted(scratch.glob("base-*")):
        if stale.is_symlink() or not stale.is_dir():
            continue
        repo_root = record_main
        if repo_root is None and (stale / ".git").exists():
            try:
                repo_root = main_checkout(common_dir(str(stale)))
            except LaneError:
                repo_root = fallback_root
        worktrees.remove_checkout(repo_root or fallback_root, stale)


def _dir_mtime(path: Path, default: float) -> float:
    newest = default
    stack: list[Path] = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        newest = max(newest, st.st_mtime)
                        if not entry.is_symlink() and entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return newest


def _log_belongs_to_live_lane(name: str, is_dir: bool, live_ids: set[str]) -> bool:
    """Whether a file or kept-run directory in `<state>/logs/` belongs to a lane in `live_ids`."""
    if not live_ids:
        return False
    if not is_dir:
        match = _LOG_FILE.fullmatch(name)
        if not match:
            return False
        label = match["label"]
        if label.startswith("post-merge-"):
            return label.removeprefix("post-merge-") in live_ids
        if label.startswith("recheck-"):
            return label.removeprefix("recheck-") in live_ids
        if label.startswith("review-"):
            rest = label.removeprefix("review-")
            return rest in live_ids or (rest.endswith("-delta") and rest.removesuffix("-delta") in live_ids)
        if label.startswith("lane-"):
            rest = label.removeprefix("lane-")
            return (
                rest in live_ids
                or (rest.endswith("-alone") and rest.removesuffix("-alone") in live_ids)
                or (rest.endswith("-base") and rest.removesuffix("-base") in live_ids)
                or any(rest.startswith(f"{lid}-base-") for lid in live_ids)
            )
        return False
    if name.startswith("lane-"):
        rest = name.removeprefix("lane-")
        return rest in live_ids or any(rest.startswith(f"{lid}-base-") for lid in live_ids)
    for prefix in ("post-merge-", "recheck-", "review-"):
        if name.startswith(prefix):
            rest = name.removeprefix(prefix)
            return rest in live_ids or any(rest.startswith(f"{lid}-") for lid in live_ids)
    return False


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


def collect(args: Any, store: state.Store) -> int:
    result = Project(os.getcwd(), store.project).run(args.dry_run)
    if args.json:
        print(json.dumps(result.as_dict()))
    else:
        for item in result.removed:
            if item["kind"] == "lane":
                print(f"{'would drop' if args.dry_run else 'dropped'} lane {item['path']}: its branch and worktree "
                      "are gone")
                continue
            print(f"{'would remove' if args.dry_run else 'removed'} {item['kind']} {item['path']}")
        for item in result.kept:
            print(f"keep {item['path']}: {item['reason']}")
        for error in result.errors:
            print(f"error: {error}")
    return 0
