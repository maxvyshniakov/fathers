"""Git worktrees as resources (fathers#112): the `worktree` kind, its guards, and the sweep's pass over the
repositories' worktrees.

A worktree is removed only by `git worktree remove` (never `rm -rf`, never `--force`), and only when every guard
passes, read again right before the removal. The guards match Lanefather's gc (lanefather/gc.py):

- not the main worktree, not bare, not missing or a symbolic link, not locked, no `.keep` file;
- not dev, main, master or an integrate/* branch checked out;
- no uncommitted or untracked change (`git status --porcelain`);
- no ignored file outside the disposable build output Lanefather names (node_modules, .venv, dist, ...);
- no Git repository or other worktree inside it;
- HEAD merged by content into the base: an ancestor of it, or every commit's patch in it (`git cherry`). The base
  is origin's default branch (origin/HEAD, origin/main, origin/master) when the repository has an origin, else its
  local main or master: work merged only locally into another branch is not merged;
- no process inside: none whose working directory is in it or whose command names it (an unreadable process table
  or working-directory listing keeps it);
- junkfather itself is not running inside it;
- for the sweep's unowned worktrees only: idle for WORKTREE_IDLE, 24 h (its folder, HEAD, index and HEAD log
  unchanged), and no Lanefather lane that is not done names it.

Its branch is deleted after it (`git branch -D`) only when that branch's content is in the base too, and it is not a
protected branch; a branch that cannot be deleted leaves the worktree removed with a warning.
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import procs as _procs
from .defaults import RESOURCE_TIMEOUT
from .resources import run
from .state import as_dict

DISPOSABLE = ("node_modules", "__pycache__", "target", ".build", "dist", "build", ".venv", ".turbo", ".next",
              "storybook-static", "coverage", ".pytest_cache", ".ruff_cache", ".DS_Store")  # Lanefather's gc
PROTECTED = ("dev", "main", "master")
BASES = ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/remotes/origin/master")
LOCAL_BASES = ("refs/heads/main", "refs/heads/master")


def git(where: str, *args: str, timeout: float = RESOURCE_TIMEOUT, both: bool = False) -> Tuple[Optional[int], str]:
    """`git -C where ARGS` without a shell: (exit code, stdout; with `both` stdout and stderr), code None when git
    could not run or timed out; on a failure its stderr."""
    ran = run(["git", "--no-optional-locks", "-C", where, *args], timeout=timeout)
    if ran.code != 0:
        return ran.code, ran.err or ran.error
    return ran.code, ran.out + ran.err if both else ran.out


def common_dir(path: str) -> Optional[str]:
    code, out = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.realpath(out.strip()) if code == 0 and out.strip() else None


def listing(repo: str) -> Optional[List[Dict[str, Any]]]:
    """`git worktree list --porcelain` of the repository at `repo` (any of its worktrees or its common dir), each
    {path, head, branch, bare, detached, locked, prunable, main}; None when git cannot list it."""
    code, out = git(repo, "worktree", "list", "--porcelain")
    if code != 0:
        return None
    found: List[Dict[str, Any]] = []
    for block in out.strip().split("\n\n"):
        item: Dict[str, Any] = {"locked": None, "prunable": None, "branch": None, "bare": False, "detached": False}
        for line in block.splitlines():
            word, _, value = line.partition(" ")
            if word == "worktree":
                item["path"] = os.path.realpath(value) if os.path.exists(value) else value
            elif word == "HEAD":
                item["head"] = value
            elif word == "branch":
                item["branch"] = value.removeprefix("refs/heads/")
            elif word in ("bare", "detached"):
                item[word] = True
            elif word in ("locked", "prunable"):
                item[word] = value or word
        if item.get("path"):
            item["main"] = not found
            found.append(item)
    return found


def bases(repo: str) -> List[str]:
    """The refs a worktree's work must be in: origin's default branch, or without an origin the local main."""
    code, out = git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/", "refs/heads/")
    refs: Set[str] = set(out.split()) if code == 0 else set()
    if not any(ref.startswith("refs/remotes/origin/") for ref in refs):
        return [ref for ref in LOCAL_BASES if ref in refs]
    found: List[str] = []
    for name in BASES:
        target = name
        if name.endswith("/HEAD"):  # origin's default branch, by the ref it points to
            code, out = git(repo, "symbolic-ref", "-q", name)
            target = out.strip() if code == 0 else ""
        if target in refs and target not in found:
            found.append(target)
    return found


def short(ref: str) -> str:
    return ref.removeprefix("refs/remotes/").removeprefix("refs/heads/")


def merged(repo: str, commit: str, targets: Sequence[str]) -> bool:
    """Whether `commit`'s work is in one of `targets`: an ancestor of it, or every patch of it there (git cherry)."""
    for target in targets:
        code, _ = git(repo, "merge-base", "--is-ancestor", commit, target)
        if code == 0:
            return True
        code, out = git(repo, "cherry", target, commit)
        if code == 0 and not any(line.startswith("+") for line in out.splitlines()):
            return True
    return False


def activity(path: str) -> float:
    """The last change of the worktree's folder, HEAD, index and HEAD log (Lanefather's gc)."""
    code, out = git(path, "rev-parse", "--absolute-git-dir")
    gitdir = out.strip() if code == 0 else ""
    candidates = [path] + ([os.path.join(gitdir, n) for n in ("HEAD", "index", "logs/HEAD")] if gitdir else [])
    times: List[float] = []
    for candidate in candidates:
        with contextlib.suppress(OSError):
            times.append(os.stat(candidate).st_mtime)
    return max(times, default=time.time())


def nested(path: str) -> Optional[str]:
    """The first Git repository or worktree below the worktree's top (ignored folders included)."""
    for folder, directories, files in os.walk(path):
        if folder == path:
            directories[:] = [d for d in directories if d != ".git"]
            continue
        if ".git" in directories or ".git" in files:
            return os.path.relpath(folder, path)
    return None


def ignored(path: str) -> Optional[List[str]]:
    """Ignored paths that are not disposable build output; None when git cannot say."""
    code, out = git(path, "status", "--porcelain", "--ignored")
    if code != 0:
        return None
    return [line[3:] for line in out.splitlines() if line.startswith("!! ")
            and not any(fnmatch.fnmatchcase(part, p) for part in line[3:].rstrip("/").split("/") for p in DISPOSABLE)]


def inside(path: str, table: Optional[_procs.Table], cwds: Optional[Mapping[int, str]]) -> Optional[List[str]]:
    """The processes working in the worktree or naming it in their command; None when that cannot be told."""
    if table is None or cwds is None:
        return None
    prefix = path.rstrip("/") + "/"
    found: List[str] = []
    for proc in table.procs.values():
        cwd = cwds.get(proc.pid) or ""
        named = any(word == path or word.startswith(prefix) for word in proc.cmd.split())
        if cwd == path or cwd.startswith(prefix) or named:
            found.append(f"pid {proc.pid} ({proc.cmd[:80]})")
    return found


def lane_worktrees(root: Optional[str] = None) -> List[str]:
    """Worktrees of Lanefather lanes that are not done, from its lane records (`$JUNKFATHER_LANEFATHER_STATE`,
    default ~/.local/state/lanefather): Lanefather removes those itself."""
    base = Path(root or os.environ.get("JUNKFATHER_LANEFATHER_STATE") or Path.home() / ".local/state/lanefather")
    found: List[str] = []
    for record in list(base.glob("lanes/*.json")) + list(base.glob("*/lanes/*.json")):
        try:
            data = as_dict(json.loads(record.read_text()))
        except (OSError, ValueError):
            continue
        worktree = data.get("worktree")
        if isinstance(worktree, str) and data.get("status") != "done":
            found.append(os.path.realpath(worktree))
    return found


def refusal(item: Mapping[str, Any], repo: str, table: Optional[_procs.Table], cwds: Optional[Mapping[int, str]],
            entries: Sequence[Mapping[str, Any]], idle: Optional[float] = None, now: Optional[float] = None,
            lanes: Sequence[str] = ()) -> Optional[str]:
    """Why this worktree (a `listing` item) must be kept, or None when it may be removed. `idle`: the sweep's
    unowned worktrees must also be idle that long; `lanes`: worktrees of live Lanefather lanes."""
    path = str(item.get("path"))
    branch = str(item.get("branch") or "")
    if item.get("main") or item.get("bare"):
        return "the main worktree"
    if not os.path.isdir(path) or os.path.islink(path):
        return "missing or a symbolic link"
    if item.get("locked"):
        return f"locked: {item['locked']}"
    here = os.path.realpath(os.getcwd())
    if here == path or here.startswith(path + "/"):
        return "junkfather runs inside it"
    if branch in PROTECTED or branch.startswith("integrate/"):
        return f"it has {branch} checked out"
    if Path(path, ".keep").exists():
        return ".keep: " + Path(path, ".keep").read_text(errors="replace").strip()[:120]
    if path in lanes:
        return "a Lanefather lane that is not done names it"
    other = next((str(e.get("path")) for e in entries if str(e.get("path")).startswith(path + "/")), None)
    if other:
        return f"it holds the worktree {other}"
    if idle is not None:
        quiet = max(0.0, (time.time() if now is None else now) - activity(path))
        if quiet < idle:
            return f"active {quiet / 3600:.1f} h ago (under {idle / 3600:.0f} h)"
    code, out = git(path, "status", "--porcelain")
    if code != 0:
        return f"git status failed: {out.strip()[:200]}"
    if out.strip():
        return "uncommitted or untracked changes"
    held = ignored(path)
    if held is None:
        return "its ignored files cannot be listed"
    if held:
        return f"ignored files: {', '.join(held[:3])}{' ...' if len(held) > 3 else ''} (not disposable build output)"
    repository = nested(path)
    if repository:
        return f"it holds a Git repository at {repository}"
    targets = bases(repo)
    if not targets:
        return "no base to compare with (no origin/HEAD, origin/main, origin/master, main or master)"
    if not merged(path, "HEAD", targets):
        return f"commits not in {' or '.join(short(t) for t in targets)}"
    users = inside(path, table, cwds)
    if users is None:
        return "whether a process works inside it cannot be told"
    if users:
        return "in use by " + "; ".join(users[:3])
    return None


def branch_to_delete(repo: str, branch: Optional[str], entries: Sequence[Mapping[str, Any]], path: str
                     ) -> Optional[str]:
    """The worktree's branch when it may go with it: its content in the base, not protected, no other worktree on
    it."""
    if not branch or branch in PROTECTED or branch.startswith("integrate/"):
        return None
    if any(e.get("branch") == branch and e.get("path") != path for e in entries):
        return None
    targets = bases(repo)
    return branch if targets and merged(repo, f"refs/heads/{branch}", targets) else None


def size(path: str) -> Optional[int]:
    """Bytes the worktree takes (du), None when du cannot say in time."""
    ran = run(["du", "-sk", path], timeout=RESOURCE_TIMEOUT)
    words = ran.out.split()
    return int(words[0]) * 1024 if ran.code == 0 and words and words[0].isdigit() else None


def repositories() -> List[str]:
    """The repositories whose worktrees the sweep looks at: `$JUNKFATHER_REPOS` (colon-separated; empty: none),
    else every folder in the home folder that holds a `.git` directory (a main checkout)."""
    given = os.environ.get("JUNKFATHER_REPOS")
    if given is not None:
        return [os.path.realpath(os.path.expanduser(p)) for p in given.split(":") if p]
    home = Path.home()
    found: List[str] = []
    with contextlib.suppress(OSError):
        for child in sorted(home.iterdir()):
            if not child.name.startswith(".") and (child / ".git").is_dir():
                found.append(str(child.resolve()))
    return found
