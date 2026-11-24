"""The pre-push gate (.githooks/pre-push; fathers#235): each pushed commit's own checks, in a clean worktree.

Git hands the hook `<local ref> <local sha> <remote ref> <remote sha>` lines. Each pushed branch tip (not a deletion,
not a tag) is checked out detached in a temporary worktree and runs that commit's own `tools/checks.py verify`, with
the base pinned to its merge-base with origin/main, so uncommitted files cannot make it pass and the commit's own
selection rules apply. A check that passed on a tree is recorded in the Git common directory (fathers-verified, see
tools/verify.py) and not run again: a push whose checks all passed before takes no lease and runs nothing. That
shortcut is taken only when the commit's tools/checks.py is this hook's own, so a commit that changes the rules is
always asked. A commit the hook passed is recorded too, so pushing it again skips even once origin/main holds it
(where its base has no change, which selects every check). Skip once with `git push --no-verify`.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checks  # tools/checks.py: which checks a change selects
import verify  # tools/verify.py: the lease, the runner and the verified-tree cache

TOOLS = Path(__file__).resolve().parent

ZERO = re.compile(r"^0+$")


def tips(stdin: str) -> list[str]:
    """The pushed commits to check: each ref's tip once, without deletions and tags."""
    found: list[str] = []
    for line in stdin.splitlines():
        fields = line.split()
        if len(fields) < 2 or ZERO.match(fields[1]):
            continue
        if fields[0].startswith("refs/tags/") or (len(fields) > 2 and fields[2].startswith("refs/tags/")):
            continue
        if fields[1] not in found:
            found.append(fields[1])
    return found


def git(root: Path, env: Mapping[str, str], *arguments: str) -> str | None:
    """Git's output, or None when it fails."""
    result = subprocess.run(["git", *arguments], cwd=root, env=dict(env), capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def base_of(root: Path, env: Mapping[str, str], sha: str) -> str | None:
    return git(root, env, "merge-base", sha, checks.DEFAULT_BASE)


def verified(root: Path, env: Mapping[str, str]) -> verify.Verified | None:
    common = git(root, env, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return verify.Verified(Path(common)) if common else None


def pending(root: Path, env: Mapping[str, str], sha: str) -> set[str] | None:
    """The checks `sha` selects that have not passed on its input trees (none for a commit this hook passed); None when
    this hook cannot tell, because the commit's tools/checks.py or tools/verify.py is not this hook's own."""
    cache = verified(root, env)
    if cache is None:
        return None
    if cache.commit_passed(sha):
        return set()
    for tool_file in ("checks.py", "verify.py"):
        ours = git(root, env, "hash-object", str(TOOLS / tool_file))
        if ours is None or ours != git(root, env, "rev-parse", "--verify", "--quiet", f"{sha}:tools/{tool_file}"):
            return None
    base = base_of(root, env, sha)
    listed = git(root, env, "diff", "--name-only", "-z", base, sha, "--") if base else None
    paths = [path for path in (listed or "").split("\0") if path]
    selection = checks.select(paths) if paths else None
    names = list(selection.checks) if selection is not None else list(checks.NAMES)
    tree = git(root, env, "rev-parse", f"{sha}^{{tree}}")
    if tree is None:
        return None
    if hasattr(checks, "cache_keys"):
        keys = checks.cache_keys(root, tree, names, full_tree=bool(getattr(selection, "unowned", None)), env=env)
        return set(names) - cache.passed_keys(keys)
    return set(names) - cache.passed(tree)


def check_commit(root: Path, env: Mapping[str, str], sha: str) -> tuple[int, int | None]:
    """Runs `sha`'s own verify in a temporary detached worktree, removed afterwards: (exit code, signal received)."""
    subprocess.run(["git", "worktree", "prune"], cwd=root, env=dict(env), capture_output=True)
    directory = tempfile.mkdtemp(prefix="fathers-pre-push-")
    print(f"pre-push: tools/checks.py verify on {sha[:10]} in {directory}", flush=True)
    print("pre-push: skip once with git push --no-verify", flush=True)
    try:
        added = subprocess.run(["git", "worktree", "add", "--quiet", "--detach", directory, sha], cwd=root,
                               env=dict(env), capture_output=True, text=True)
        if added.returncode:
            print(f"pre-push: git worktree add failed: {added.stderr.strip()}", file=sys.stderr)
            return 1, None
        base = base_of(root, env, sha)
        run_env = dict(env)
        run_env.pop("FATHERS_CHECKS", None)
        if base:
            run_env["FATHERS_CHECK_BASE"] = base
        else:
            run_env.pop("FATHERS_CHECK_BASE", None)
        return verify.run_forwarding([sys.executable, "tools/checks.py", "verify", "--keep-going"],
                                     cwd=Path(directory), env=run_env)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", directory], cwd=root, env=dict(env),
                       capture_output=True)
        shutil.rmtree(directory, ignore_errors=True)


def signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def main(stdin: str, argv: Sequence[str], environment: Mapping[str, str] | None = None) -> int:
    """The hook: `argv` is its own (the hook's path, the remote's name and URL), `stdin` what Git wrote to it."""
    env = verify.clean_environment(os.environ if environment is None else environment)
    top = git(Path.cwd(), env, "rev-parse", "--show-toplevel")
    if top is None:
        print("pre-push: not in a Git checkout; nothing was pushed.", file=sys.stderr)
        return 1
    root = Path(top)
    left: list[str] = []
    for sha in tips(stdin):
        if pending(root, env, sha) == set():
            print(f"pre-push: {sha[:10]} already passed its checks", flush=True)
        else:
            left.append(sha)
    if not left:
        return 0
    slots = env.get("FATHERS_VERIFY_SLOTS") or "2"
    leased = verify.lease_self(slots, "pre-push", [sys.executable, *argv], root=root, environment=env,
                               input=stdin.encode())
    if leased is not None:
        return leased
    for sha in left:
        code, received = check_commit(root, env, sha)
        if received is not None or code > 128:
            number = received if received is not None else code - 128
            print(f"\npre-push: {signal_name(number)}; nothing was pushed.", file=sys.stderr)
            return 128 + number
        if code:
            print(f"\npre-push: tools/checks.py verify failed on {sha[:10]}; nothing was pushed.", file=sys.stderr)
            return 1
        cache = verified(root, env)
        if cache:
            cache.record_commit(sha)
    return 0
