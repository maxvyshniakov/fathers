"""Git worktrees as resources (fathers#112): the `worktree` kind's one guarded removal (`plan`), its guards, and
what the sweep's pass over the repositories' worktrees reads.

A worktree is removed only by `git worktree remove` (never `rm -rf`, never `--force`), and only when every guard
passes, all read again by `plan` right before the removal (the git listing, the repository's identity, the process
table and the working directories). The guards follow Lanefather's gc (lanefather/gc.py), which keeps its own
remover until fathers#171 makes this the one. The process table and working directories come from the shared reader
(`fathers_common.proc`, vendored with every command). The guards:

- listed by `git worktree list` as a linked worktree of the repository whose common git dir the entry recorded (a
  repository replaced at the same path owns nothing of the old one);
- not the main worktree, not bare, not missing or a symbolic link (as git lists it), not locked, no `.keep` (of any
  kind, readable or not);
- not dev, main, master or an integrate/* branch checked out;
- no uncommitted or untracked change (`git status --porcelain`);
- no ignored file outside the disposable build output Lanefather names (node_modules, .venv, dist, ...);
- HEAD merged into the base: an ancestor of it, or, for a squash or a rebase, no merge commit beyond the base (a
  merge can carry its own conflict resolution, which `git cherry` does not see) and every commit's patch in it
  (`git cherry`). The base is origin's default branch (origin/HEAD, origin/main, origin/master) when the repository
  has an origin, else its local main or master: work merged only locally into another branch is not merged;
- no Git repository or other worktree inside it;
- no process inside: none whose working directory is in it or whose command names it, and none of this user's whose
  working directory cannot be read (an unreadable process table or working-directory listing keeps it). Not seen: a
  process that started inside and changed directory, or one that only holds files open in it;
- spawnfather itself is not running inside it;
- for the sweep's unowned worktrees only: idle for WORKTREE_IDLE, 24 h (its folder, HEAD, index and HEAD log
  unchanged), and no Lanefather lane that is not done names it (lanes from Lanefather's project registry; when that
  cannot be read, no worktree under a `.claude/worktrees/` folder is removed).

Its branch goes after it only when its tip, as checked, is merged the same way and no other worktree has it, and
only that tip: `git update-ref -d refs/heads/BRANCH SHA` fails when the branch moved meanwhile, and the worktree is
then removed with a warning.
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from . import procs as _procs
from .defaults import RESOURCE_TIMEOUT
from .resources import Refused, run
from .state import as_dict, as_list

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
    {path (resolved), listed (as git has it), head, branch, bare, detached, locked, prunable, main}; None when git
    cannot list it."""
    code, out = git(repo, "worktree", "list", "--porcelain")
    if code != 0:
        return None
    found: List[Dict[str, Any]] = []
    for block in out.strip().split("\n\n"):
        item: Dict[str, Any] = {"locked": None, "prunable": None, "branch": None, "bare": False, "detached": False}
        for line in block.splitlines():
            word, _, value = line.partition(" ")
            if word == "worktree":
                item["listed"] = value
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
    """Whether `commit`'s work is in one of `targets`: an ancestor of it, or (a squash or a rebase) no merge commit
    beyond it and every patch of it there (`git cherry` passes over merge commits, and so over a merge's own
    conflict resolution)."""
    for target in targets:
        code, _ = git(repo, "merge-base", "--is-ancestor", commit, target)
        if code == 0:
            return True
        code, out = git(repo, "rev-list", "--merges", f"{target}..{commit}")
        if code != 0 or out.strip():
            continue
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


def zombie(proc: _procs.Proc) -> bool:
    """A process that exited and waits for its parent (ps shows `<defunct>` or its name in parentheses)."""
    return "<defunct>" in proc.cmd or (proc.cmd.startswith("(") and proc.cmd.endswith(")"))


def inside(path: str, table: Optional[_procs.Table], cwds: Optional[Mapping[int, str]],
           exclude: Sequence[int] = ()) -> Optional[List[str]]:
    """The processes working in the worktree or naming it in their command, and this user's processes whose working
    directory is unknown (one that started after lsof read them, or one lsof could not read); None when that cannot
    be told at all. `exclude`: processes that do not count (an ending owner's own)."""
    if table is None or cwds is None:
        return None
    prefix = path.rstrip("/") + "/"
    me = os.getuid()
    found: List[str] = []
    unknown: List[_procs.Proc] = []
    for proc in table.procs.values():
        if proc.pid in exclude or proc.pid == os.getpid():
            continue
        cwd = cwds.get(proc.pid)
        named = any(word == path or word.startswith(prefix) for word in proc.cmd.split())
        if named or (cwd is not None and (cwd == path or cwd.startswith(prefix))):
            found.append(f"pid {proc.pid} ({proc.cmd[:80]})")
        elif cwd is None and proc.uid == me and not zombie(proc):
            unknown.append(proc)
    if unknown:  # those that ended between ps and lsof do not count; one that still runs unseen does
        try:
            running = _procs.identities([p.pid for p in unknown])
        except _procs.ProcError:
            return None
        found += [f"pid {p.pid} ({p.cmd[:80]}; its working directory is unknown)" for p in unknown
                  if running.get(p.pid) == p.start]
    return found


class Lanes(NamedTuple):
    """Worktrees of Lanefather lanes that are not done; `known` False when Lanefather's registry cannot be read."""
    paths: List[str]
    known: bool

    def holds(self, path: str) -> Optional[str]:
        if path in self.paths:
            return "a Lanefather lane that is not done names it"
        if not self.known and "/.claude/worktrees/" in path + "/":
            return "Lanefather's project registry cannot be read, and its lanes live under .claude/worktrees"
        return None


def lane_worktrees(root: Optional[str] = None) -> Lanes:
    """Worktrees of Lanefather lanes that are not done: the lane records (`*.json` with `worktree` and `status`) in
    every state directory Lanefather's project registry names (`$LANEFATHER_REGISTRY`, else projects.json in its
    state folder: `$SPAWNFATHER_LANEFATHER_STATE`, default ~/.local/state/lanefather), and in that folder's lanes/ and
    */lanes/. Without that folder Lanefather is not installed and there are none; with it but no readable registry,
    `known` is False, and Lanes.holds protects every .claude/worktrees folder."""
    configured = os.environ.get("SPAWNFATHER_LANEFATHER_STATE") or os.environ.get("JUNKFATHER_LANEFATHER_STATE")
    base = Path(root or configured or Path.home() / ".local/state/lanefather")
    if not base.is_dir():
        return Lanes([], True)
    registry = Path(os.path.expanduser(os.environ.get("LANEFATHER_REGISTRY") or str(base / "projects.json")))
    directories: List[Path] = [base / "lanes", *base.glob("*/lanes")]
    known = True
    try:
        data = as_dict(json.loads(registry.read_text()))
        projects = data.get("projects")
        if data.get("v") != 1 or not isinstance(projects, list):
            raise ValueError("not a version 1 registry")
        for project in as_list(projects):
            state = as_dict(project).get("state_dir")
            if isinstance(state, str):
                directories.append(Path(os.path.expanduser(state)))
    except (OSError, ValueError):
        known = False
    found: List[str] = []
    for directory in dict.fromkeys(directories):
        for record in directory.glob("*.json"):
            try:
                lane = as_dict(json.loads(record.read_text()))
            except (OSError, ValueError):
                continue
            worktree = lane.get("worktree")
            if isinstance(worktree, str) and lane.get("status") != "done":
                found.append(os.path.realpath(worktree))
    return Lanes(found, known)


def keep_file(path: str) -> Optional[str]:
    """What a `.keep` says, when there is one (of any kind, readable or not)."""
    keep = os.path.join(path, ".keep")
    if not os.path.lexists(keep):
        return None
    try:
        return ".keep: " + Path(keep).read_text(errors="replace").strip()[:120]
    except OSError:
        return ".keep (unreadable)"


def refusal(item: Mapping[str, Any], repo: str, table: Optional[_procs.Table], cwds: Optional[Mapping[int, str]],
            entries: Sequence[Mapping[str, Any]], idle: Optional[float] = None, now: Optional[float] = None,
            lanes: Optional[Lanes] = None, exclude: Sequence[int] = ()) -> Optional[str]:
    """Why this worktree (a `listing` item) must be kept, or None when it may be removed. `idle`: the sweep's
    unowned worktrees must also be idle that long; `lanes`: worktrees of live Lanefather lanes; `exclude`: processes
    that do not count as using it. The cheap checks come first."""
    path = str(item.get("path"))
    branch = str(item.get("branch") or "")
    if item.get("main") or item.get("bare"):
        return "the main worktree"
    if os.path.islink(str(item.get("listed") or path)) or os.path.islink(path) or not os.path.isdir(path):
        return "missing or a symbolic link"
    if item.get("locked"):
        return f"locked: {item['locked']}"
    here = os.path.realpath(os.getcwd())
    if here == path or here.startswith(path + "/"):
        return "spawnfather runs inside it"
    if branch in PROTECTED or branch.startswith("integrate/"):
        return f"it has {branch} checked out"
    keep = keep_file(path)
    if keep:
        return keep
    held_by = lanes.holds(path) if lanes is not None else None
    if held_by:
        return held_by
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
    targets = bases(repo)
    if not targets:
        return "no base to compare with (no origin/HEAD, origin/main, origin/master, main or master)"
    if not merged(path, "HEAD", targets):
        return f"commits not in {' or '.join(short(t) for t in targets)}"
    repository = nested(path)
    if repository:
        return f"it holds a Git repository at {repository}"
    users = inside(path, table, cwds, exclude)
    if users is None:
        return "whether a process works inside it cannot be told"
    if users:
        return "in use by " + "; ".join(users[:3])
    return None


def branch_tip(repo: str, branch: Optional[str], entries: Sequence[Mapping[str, Any]], path: str
               ) -> Optional[Tuple[str, str]]:
    """(the branch, its tip) when it may go with the worktree: not protected, no other worktree on it, and that tip
    merged into the base. The deletion names that tip, so a branch that moved since is left alone."""
    if not branch or branch in PROTECTED or branch.startswith("integrate/"):
        return None
    if any(e.get("branch") == branch and e.get("path") != path for e in entries):
        return None
    code, out = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    sha = out.strip() if code == 0 else ""
    targets = bases(repo)
    if not sha or not targets or not merged(repo, sha, targets):
        return None
    return branch, sha


def plan(resource: Mapping[str, Any], idle: Optional[float] = None, now: Optional[float] = None,
         lanes: Optional[Lanes] = None, exclude: Sequence[int] = ()) -> List[List[str]]:
    """The one guarded removal of a worktree, for an owned entry's stop (resources.Worktree) and for the sweep's
    unowned ones alike: everything read again now (the git listing, the repository's identity, the process table,
    the working directories), then the steps to run; Refused, saying which guard keeps it, otherwise."""
    path = str(resource.get("id"))
    items = listing(str(resource.get("repo") or path))
    item = next((i for i in items or [] if i.get("path") == path), None)
    if items is None or item is None:
        raise Refused("git does not list the worktree; nothing was removed")
    repo = str(items[0]["path"])
    recorded = resource.get("common")
    if recorded and common_dir(repo) != recorded:
        raise Refused(f"its repository is not the one recorded ({recorded}); nothing was removed")
    why = refusal(item, repo, _procs.read_or_none(), _procs.working_dirs_or_none(os.getuid()), items, idle, now,
                  lanes, exclude)
    if why:
        raise Refused(f"kept: {why}")
    steps = [["git", "-C", repo, "worktree", "remove", path]]
    tip = branch_tip(repo, item.get("branch"), items, path)
    if tip:
        steps.append(["git", "-C", repo, "update-ref", "-d", f"refs/heads/{tip[0]}", tip[1]])
    return steps


def size(path: str) -> Optional[int]:
    """Bytes the worktree takes (du), None when du cannot say in time."""
    ran = run(["du", "-sk", path], timeout=RESOURCE_TIMEOUT)
    words = ran.out.split()
    return int(words[0]) * 1024 if ran.code == 0 and words and words[0].isdigit() else None


def repositories() -> List[str]:
    """The repositories whose worktrees the sweep looks at: `$SPAWNFATHER_REPOS` (or legacy `$JUNKFATHER_REPOS`;
    colon-separated; empty: none), else every folder in the home folder that holds a `.git` directory (a main
    checkout)."""
    if "SPAWNFATHER_REPOS" in os.environ:
        given: Optional[str] = os.environ["SPAWNFATHER_REPOS"]
    else:
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
