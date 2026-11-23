"""Git worktrees as resources (fathers#112): the `worktree` kind's one guarded removal (`plan`), its guards, and
what the sweep's pass over the repositories' worktrees reads.

A worktree is removed only by `git worktree remove` (never `rm -rf`), and only when every guard passes, all read
again by `plan` right before the removal (the git listing, the repository's identity, the process table and the
working directories). This is the one worktree remover: Lanefather's done, drop and gc ask for it (fathers#171). The
process table and working directories come from the shared reader (`fathers_common.proc`, vendored with every
command). The guards come in two tiers. Integrity guards are never lifted:

- listed by `git worktree list` as a linked worktree of the repository whose common git dir the entry recorded (a
  repository replaced at the same path owns nothing of the old one);
- not the main worktree, not bare, not missing or a symbolic link (as git lists it);
- spawnfather itself is not running inside it;
- not dev, main, master or an integrate/* branch checked out;
- no other worktree and no Git repository inside it, and no submodule in its index (`git worktree remove` refuses
  those without --force, which would lose their work);
- the process table and the working directories can be read, and no process of this user's has a working directory
  that cannot be read (one that started after lsof read them, or one lsof could not read).

Work guards are where the requester's evidence counts (`Evidence`: `--base`, `--disposable`); `--override REASON`
lifts every one of them (the removal then uses `git worktree remove --force`, twice for a locked one) and the stop
event names the reason and what it lifted:

- not locked (a `claude agent ... (pid N)` lock whose process no longer runs is unlocked first, as Lanefather's gc
  did), no `.keep` (of any kind, readable or not);
- no uncommitted or untracked change (`git status --porcelain`);
- no ignored file outside the disposable build output (DISPOSABLE, and the requester's `--disposable` patterns);
- HEAD merged into a base: an ancestor of it, or, for a squash or a rebase, no merge commit beyond the base (a
  merge can carry its own conflict resolution, which `git cherry` does not see) and every commit's patch in it
  (`git cherry`). The bases are origin's default branch (origin/HEAD, origin/main, origin/master) when the
  repository has an origin, else its local main or master, and the requester's `--base` refs (a lane landed on an
  integration branch that is not promoted yet): work merged only locally into another branch is not merged;
- no process inside: none whose working directory is in it or whose command names it. Not seen: a process that
  started inside and changed directory, or one that only holds files open in it;
- for the sweep's unowned worktrees only: idle for WORKTREE_IDLE, 24 h (its folder, HEAD, index and HEAD log
  unchanged), and no Lanefather lane that is not done names it (lanes from Lanefather's project registry; when that
  cannot be read, no worktree under a `.claude/worktrees/` folder is removed). That read of Lanefather's files stays
  one release for lanes started before Lanefather registered its worktrees with Spawnfather (fathers#171); a
  registered lane's worktree is an owned entry, which the sweep leaves alone.

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
import re
import time
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from . import procs as _procs
from . import safety as _safety
from .defaults import RESOURCE_TIMEOUT
from .resources import Refused, run
from .state import as_dict, as_list

DISPOSABLE = ("node_modules", "__pycache__", "target", ".build", "dist", "build", ".venv", ".turbo", ".next",
              "storybook-static", "coverage", ".pytest_cache", ".ruff_cache", ".DS_Store")  # Lanefather's gc
PROTECTED = ("dev", "main", "master")
BASES = ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/remotes/origin/master")
LOCAL_BASES = ("refs/heads/main", "refs/heads/master")


class Evidence:
    """What the requester of a removal vouches for (fathers#171): `bases`, refs (or commits) its work may be merged
    into besides origin's default branch; `disposable`, ignored-file patterns that may go besides DISPOSABLE;
    `override`, a reason that lifts every work guard. `lifted` collects the work guards an override lifted, for the
    stop event."""

    def __init__(self, bases: Sequence[str] = (), disposable: Sequence[str] = (), override: Optional[str] = None):
        self.bases = tuple(str(b) for b in bases if b)
        self.disposable = tuple(str(d) for d in disposable if d)
        self.override = override or None
        self.lifted: List[str] = []

    @classmethod
    def of(cls, given: Any) -> "Evidence":
        if isinstance(given, Evidence):
            return given
        data = as_dict(given)
        return cls([str(b) for b in as_list(data.get("bases"))], [str(d) for d in as_list(data.get("disposable"))],
                   str(data["override"]) if data.get("override") else None)

    def as_dict(self) -> Dict[str, Any]:
        return {"bases": list(self.bases), "disposable": list(self.disposable), "override": self.override}


NONE = Evidence()


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


def bases(repo: str, extra: Sequence[str] = ()) -> List[str]:
    """The refs a worktree's work must be in: origin's default branch, or without an origin the local main, and
    the requester's `extra` refs or commits that name a commit here."""
    found = default_bases(repo)
    for given in extra:
        code, out = git(repo, "rev-parse", "--verify", "-q", f"{given}^{{commit}}")
        if code == 0 and out.strip() and given not in found:
            found.append(given)
    return found


def default_bases(repo: str) -> List[str]:
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
    if re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", ref):
        return ref[:10]
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


def ignored(path: str, extra: Sequence[str] = ()) -> Optional[List[str]]:
    """Ignored paths that are not disposable build output (DISPOSABLE and the requester's `extra` patterns, each
    matched against every path component); None when git cannot say."""
    # --ignored=matching: the paths an ignore pattern names (desktop/node_modules/), not the folder that holds only
    # ignored files (desktop/), whose name says nothing of what is in it.
    code, out = git(path, "status", "--porcelain", "--ignored=matching")
    if code != 0:
        return None
    patterns = (*DISPOSABLE, *extra)
    return [line[3:] for line in out.splitlines() if line.startswith("!! ")
            and not any(fnmatch.fnmatchcase(part, p) for part in line[3:].rstrip("/").split("/") for p in patterns)]


def submodules(path: str) -> Optional[List[str]]:
    """The submodules (gitlinks) in the worktree's index: `git worktree remove` refuses a worktree with any, and its
    --force would delete their checkouts. None when git cannot say."""
    code, out = git(path, "ls-files", "--stage")
    if code != 0:
        return None
    return [line.split("\t", 1)[-1] for line in out.splitlines() if line.startswith("160000 ")]


def dead_agent_lock(lock: Optional[str]) -> bool:
    """A `claude agent ... (pid N)` lock whose process no longer runs (Lanefather's gc rule): it holds nothing."""
    pid = _safety.agent_lock_pid(lock or "")
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OverflowError, ValueError):
        return False
    return False


def zombie(proc: _procs.Proc) -> bool:
    """A process that exited and waits for its parent (ps shows `<defunct>` or its name in parentheses)."""
    return "<defunct>" in proc.cmd or (proc.cmd.startswith("(") and proc.cmd.endswith(")"))


def inside(path: str, table: Optional[_procs.Table], cwds: Optional[Mapping[int, str]],
           exclude: Sequence[int] = ()) -> Optional[List[str]]:
    """The processes working in the worktree or naming it in their command, and this user's processes whose working
    directory is unknown (one that started after lsof read them, or one lsof could not read); None when that cannot
    be told at all. `exclude`: processes that do not count (an ending owner's own)."""
    split = users(path, table, cwds, exclude)
    return None if split is None else split[0] + split[1]


def users(path: str, table: Optional[_procs.Table], cwds: Optional[Mapping[int, str]],
          exclude: Sequence[int] = ()) -> Optional[Tuple[List[str], List[str]]]:
    """`inside` in two parts: (the processes seen working inside, this user's processes whose working directory is
    unknown); None when that cannot be told at all."""
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
    unseen: List[str] = []
    if unknown:  # those that ended between ps and lsof do not count; one that still runs unseen does
        try:
            running = _procs.identities([p.pid for p in unknown])
        except _procs.ProcError:
            return None
        unseen = [f"pid {p.pid} ({p.cmd[:80]}; its working directory is unknown)" for p in unknown
                  if running.get(p.pid) == p.start]
    return found, unseen


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
            lanes: Optional[Lanes] = None, exclude: Sequence[int] = (), evidence: Evidence = NONE) -> Optional[str]:
    """Why this worktree (a `listing` item) must be kept, or None when it may be removed. `idle`: the sweep's
    unowned worktrees must also be idle that long; `lanes`: worktrees of live Lanefather lanes; `exclude`: processes
    that do not count as using it; `evidence`: the requester's bases, disposable patterns and override. The cheap
    checks come first; with an override every work guard that holds is recorded in `evidence.lifted` instead."""
    path = str(item.get("path"))
    branch = str(item.get("branch") or "")
    lifted: List[str] = []
    found: Dict[str, Tuple[List[str], List[str]]] = {}

    def git_status() -> Optional[str]:
        code, out = git(path, "status", "--porcelain")
        if code != 0:
            return f"git status failed: {out.strip()[:200]}"
        return "uncommitted or untracked changes" if out.strip() else None

    def ignored_files() -> Optional[str]:
        held = ignored(path, evidence.disposable)
        if held is None:
            return "its ignored files cannot be listed"
        if held:
            more = " ..." if len(held) > 3 else ""
            return f"ignored files: {', '.join(held[:3])}{more} (not disposable build output)"
        return None

    def merged_into_base() -> Optional[str]:
        targets = bases(repo, evidence.bases)
        if not targets:
            return "no base to compare with (no origin/HEAD, origin/main, origin/master, main or master)"
        if not merged(path, "HEAD", targets):
            return f"commits not in {' or '.join(short(t) for t in targets)}"
        return None

    def quiet() -> Optional[str]:
        if idle is None:
            return None
        since = max(0.0, (time.time() if now is None else now) - activity(path))
        return f"active {since / 3600:.1f} h ago (under {idle / 3600:.0f} h)" if since < idle else None

    def lock() -> Optional[str]:
        held = item.get("locked")
        if not held or dead_agent_lock(str(held)):
            return None
        return f"locked: {held}"

    def other_worktree() -> Optional[str]:
        other = next((str(e.get("path")) for e in entries if str(e.get("path")).startswith(path + "/")), None)
        return f"it holds the worktree {other}" if other else None

    def repository() -> Optional[str]:
        nested_at = nested(path)
        return f"it holds a Git repository at {nested_at}" if nested_at else None

    def submodule() -> Optional[str]:
        held = submodules(path)
        if held is None:
            return "its submodules cannot be listed"
        if held:
            return (f"it has submodules ({', '.join(held[:3])}{' ...' if len(held) > 3 else ''}): git worktree remove "
                    "refuses them, and forcing it would lose their checkouts; remove it by hand")
        return None

    def processes() -> Optional[str]:
        split = users(path, table, cwds, exclude)
        if split is None:
            return "whether a process works inside it cannot be told"
        found["users"] = split
        return f"in use by {'; '.join(split[1][:3])}" if split[1] else None

    def in_use() -> Optional[str]:
        split = found.get("users", ([], []))
        return f"in use by {'; '.join(split[0][:3])}" if split[0] else None

    here = os.path.realpath(os.getcwd())
    checks: List[Tuple[bool, Callable[[], Optional[str]]]] = [
        (True, lambda: "the main worktree" if item.get("main") or item.get("bare") else None),
        (True, lambda: "missing or a symbolic link" if os.path.islink(str(item.get("listed") or path))
         or os.path.islink(path) or not os.path.isdir(path) else None),
        (False, lock),
        (True, lambda: "spawnfather runs inside it" if here == path or here.startswith(path + "/") else None),
        (True, lambda: f"it has {branch} checked out" if branch in PROTECTED or branch.startswith("integrate/")
         else None),
        (False, lambda: keep_file(path)),
        (False, lambda: lanes.holds(path) if lanes is not None else None),
        (True, other_worktree),
        (False, quiet),
        (False, git_status),
        (False, ignored_files),
        (False, merged_into_base),
        (True, submodule),
        (True, repository),
        (True, processes),
        (False, in_use),
    ]
    for integrity, check in checks:
        why = check()
        if why is None:
            continue
        if integrity or not evidence.override:
            return why
        lifted.append(why)
    evidence.lifted[:] = lifted
    return None


def branch_tip(repo: str, branch: Optional[str], entries: Sequence[Mapping[str, Any]], path: str,
               extra: Sequence[str] = ()) -> Optional[Tuple[str, str]]:
    """(the branch, its tip) when it may go with the worktree: not protected, no other worktree on it, and that tip
    merged into the base. The deletion names that tip, so a branch that moved since is left alone."""
    if not branch or branch in PROTECTED or branch.startswith("integrate/"):
        return None
    if any(e.get("branch") == branch and e.get("path") != path for e in entries):
        return None
    code, out = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    sha = out.strip() if code == 0 else ""
    targets = bases(repo, extra)
    if not sha or not targets or not merged(repo, sha, targets):
        return None
    return branch, sha


def plan(resource: Mapping[str, Any], idle: Optional[float] = None, now: Optional[float] = None,
         lanes: Optional[Lanes] = None, exclude: Sequence[int] = (), evidence: Evidence = NONE) -> List[List[str]]:
    """The one guarded removal of a worktree, for an owned entry's stop (resources.Worktree), the sweep's unowned
    ones and a requester's one-shot removal (`reap --worktree`) alike: everything read again now (the git listing,
    the repository's identity, the process table, the working directories), then the steps to run; Refused, saying
    which guard keeps it, otherwise. A dead agent's lock is unlocked first; an override that lifted a dirty tree or
    a lock removes with --force (twice for the lock), and its branch then goes only when merged."""
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
                  lanes, exclude, evidence)
    if why:
        raise Refused(f"kept: {why}")
    steps: List[List[str]] = []
    locked = item.get("locked")
    if locked and dead_agent_lock(str(locked)):
        steps.append(["git", "-C", repo, "worktree", "unlock", path])
        locked = None
    force: List[str] = []
    forced = ("uncommitted", "locked:", "in use by", "git status")
    if evidence.override and any(w.startswith(forced) for w in evidence.lifted):
        force = ["--force", "--force"] if locked else ["--force"]
    steps.append(["git", "-C", repo, "worktree", "remove", *force, path])
    tip = branch_tip(repo, item.get("branch"), items, path, evidence.bases)
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
