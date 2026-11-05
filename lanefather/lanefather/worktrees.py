"""The worktrees lanefather makes and removes: a base checkout for a red check's rerun, the post-merge smoke's
checkouts, the batch gate's worktree, a review's checkout; and the lane's own build directories under the cache.

  <main checkout>/.claude/worktrees/smoke-<n>     one per running smoke, kept warm between smokes (#573)
  <main checkout>/.claude/worktrees/batch-gate    promote's, one promote at a time (#454)
  <lane scratch>/base-<sha>                       a red check's base rerun (#568), removed on every exit path
  <state>/reviews/<label>-checkout-<pid>          a review's checkout at the lane tip
"""
from __future__ import annotations

from collections.abc import Generator
import contextlib
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from . import config, state
from .errors import LaneError
from .gitops import git, main_checkout, worktree_list

GATE_WORKTREE = "batch-gate"
SMOKE_WORKTREE = "smoke"


def worktrees_root(common: str) -> Path:
    return Path(main_checkout(common)) / ".claude" / "worktrees"


def remove_checkout(worktree: Any, path: Path) -> None:
    git(worktree, "worktree", "remove", "--force", "--force", str(path), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    git(worktree, "worktree", "prune", check=False)


@contextlib.contextmanager
def base_checkout(record: state.Record, worktree: Any, sha: str) -> Generator[str]:
    """A disposable worktree detached at the lane's base, in the lane's scratch directory, removed on every exit
    path; one that a killed check left behind goes first. done drops the cache partitions that name it."""
    scratch = Path(record["scratch"][0])
    scratch.mkdir(parents=True, exist_ok=True)
    for stale in scratch.glob("base-*"):
        remove_checkout(worktree, stale)
    path = scratch / f"base-{sha[:12]}"
    try:
        git(worktree, "worktree", "add", "-q", "--detach", str(path), sha)
        yield str(path)
    finally:
        remove_checkout(worktree, path)


@contextlib.contextmanager
def smoke_checkout(common: str, merge: str) -> Generator[str]:
    """A worktree detached at the merge for its post-merge smoke (#573): the first free one of
    <main checkout>/.claude/worktrees/smoke-<n>, which its lock file beside it holds while a smoke runs. Smokes
    overlap, each on its own merge, while the integration worktree moves on; the checkouts stay and keep their builds
    warm for the next smokes, as the batch gate's does."""
    main = main_checkout(common)
    root = worktrees_root(common)
    root.mkdir(parents=True, exist_ok=True)
    git(main, "worktree", "prune", check=False)
    listed = {w["path"] for w in worktree_list(main)}
    index = 0
    while True:
        path = os.path.realpath(root / f"{SMOKE_WORKTREE}-{index}")
        # Held past this loop for the smoke's whole run; the finally below closes it.
        handle = open(root / f".{SMOKE_WORKTREE}-{index}.lock", "a")  # noqa: SIM115
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            index += 1
            continue
        if path in listed or not os.path.exists(path):
            break
        # A directory that is not a worktree is someone else's: leave it.
        handle.close()
        index += 1
    try:
        if path in listed:
            git(path, "checkout", "-q", "--force", "--detach", merge)
            # Untracked files an earlier smoke left; ignored build output stays warm.
            git(path, "clean", "-fdq")
        else:
            git(main, "worktree", "add", "-q", "--detach", path, merge)
        yield path
    finally:
        handle.close()


def gate_worktree(common: str, sha: str) -> str:
    """The batch gate's own worktree, detached at the SHA, so lanes keep landing on the integration worktree
    while the gate runs and its builds stay warm from batch to batch."""
    main = main_checkout(common)
    path = os.path.realpath(worktrees_root(common) / GATE_WORKTREE)
    if path in {w["path"] for w in worktree_list(main)}:
        # The gate worktree belongs to promote: reset whatever a previous run left in tracked files.
        git(path, "checkout", "-q", "--force", "--detach", sha)
    elif os.path.exists(path):
        raise LaneError(f"{path} exists but is not a worktree of {main}; move it aside")
    else:
        git(main, "worktree", "add", "-q", "--detach", path, sha)
    return path


@contextlib.contextmanager
def review_checkout(main: str, label: str, tip: str) -> Generator[str]:
    """A worktree detached at the reviewed tip, removed on every exit path, so landing and done go on meanwhile."""
    root = state.state_dir() / "reviews"
    root.mkdir(exist_ok=True)
    for stale in root.glob(f"{label}-checkout-*"):
        remove_checkout(main, stale)
    path = root / f"{label}-checkout-{os.getpid()}"
    try:
        git(main, "worktree", "add", "-q", "--detach", str(path), tip)
        yield str(path)
    finally:
        remove_checkout(main, path)


# The lane's build directories under the project's cache

def cache_root(project: config.Config | None = None) -> Path:
    current = project or config.current()
    configured = current.env("BUILD_CACHE")
    if configured:
        return Path(configured)
    base = Path.home() / "Library/Caches" if sys.platform == "darwin" else Path(
        os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / current.cache_name


def lane_targets(lane_id: str) -> list[str]:
    """The lane's own build directories (`[lane] targets`, `{id}` replaced) under the cache root."""
    return [str(cache_root() / pattern.replace("{id}", lane_id)) for pattern in config.current().lane_targets]


def size(path: str) -> int:
    result = subprocess.run(["du", "-sk", path], capture_output=True, text=True)
    return int(result.stdout.split()[0]) * 1024 if result.returncode == 0 and result.stdout else 0


def remove_tree(path: str, protected: list[str]) -> int:
    path = os.path.realpath(path)
    if not os.path.exists(path):
        return 0
    if any(path == p or p.startswith(path + "/") for p in ["/", *protected]):
        raise LaneError(f"refusing to remove {path}: it holds {', '.join(protected)}")
    freed = size(path)
    shutil.rmtree(path)
    return freed


def shared_partitions(*checkouts: str, project: config.Config | None = None) -> list[Path]:
    """Shared cache partitions (`[lane] cache_partitions` under the cache root, 64-hex directories of a build cache
    such as Straylight's cargo-v2) whose dependency files name a path under the checkouts."""
    name = (project or config.current()).cache_partitions
    if not name:
        return []
    found: list[Path] = []
    for partition in (cache_root(project) / name).glob("[0-9a-f]" * 64):
        if not partition.is_dir() or partition.is_symlink():
            continue
        for depfile in [*partition.glob("debug/*.d"), *partition.glob("release/*.d")]:
            try:
                with open(depfile, errors="replace") as handle:
                    text = handle.read(1 << 16)
                    if any(checkout + "/" in text for checkout in checkouts):
                        found.append(partition)
                        break
            except OSError:
                pass
    return found
