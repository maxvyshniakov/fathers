"""Finishing a landed lane: stop what it left running in its worktree (#661), then remove the worktree, its branches,
its build directories, its scratch and the shared cache partitions built from it.

A lane whose agent still locks its worktree is released instead: its slot and claims are free, and done finishes it
once the agent exits.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from . import config, journal, procs, review, state, worktrees
from .errors import LaneError
from .gitops import dirty, git, git_ok, main_checkout, worktree_lock
from .state import Record


def ticket_commented(record: Record) -> str | None:
    """None when the ticket has a comment since the lane started, else the refusal."""
    argv = [review.gh_command(), "issue", "view", record["ticket"], "--json", "comments",
            *review.repo_args(record["common_dir"])]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode:
        return f"gh could not read #{record['ticket']}: {result.stderr.strip()}"
    since = journal.parse_time(record["created"])
    comments: list[dict[str, Any]] = json.loads(result.stdout).get("comments", [])
    if any(journal.parse_time(c["createdAt"]) >= since for c in comments):
        return None
    return (f"#{record['ticket']} has no comment since the lane started; post the lane's RED->GREEN evidence with "
            f"gh issue comment {record['ticket']}")


def done(args: Any) -> int:
    finish(args.id, args.force_reason)
    return 0


def finish_landed(lane_id: str, keep: bool) -> None:
    """After a green post-merge smoke: finish the lane as done does. When its agent still locks the worktree, keep
    the worktree and release the lane; done finishes it later. A landed lane holds no slot, so a refused done only
    leaves cleanup behind."""
    prog = config.prog()
    if keep:
        print(f"Lane {lane_id} kept (--keep): it holds no slot; finish it with {prog} done {lane_id}")
        return
    record = state.load(lane_id)
    if record["status"] == "done":
        # land and the lane's review both finish a reviewed lane; whichever comes second has nothing left to do.
        return
    worktree = record.get("worktree")
    lock = (worktree_lock(main_checkout(record["common_dir"]), worktree)
            if worktree and os.path.isdir(worktree) else None)
    if lock is not None:
        with state.locked("state"):
            record = state.load(lane_id)
            record.update(status="released", released=journal.now())
            state.save(record)
            journal.ledger(f"release {lane_id}: worktree {worktree} is locked ({lock}); slot and claims freed")
        print(f"Lane {lane_id} released: its slot and claims are free. Its agent still holds {worktree} "
              f"(locked: {lock}); once the agent exits, remove it with {prog} done {lane_id}")
        return
    try:
        finish(lane_id, None)
    except LaneError as error:
        journal.ledger(f"auto-done {lane_id} refused: {str(error).replace(chr(10), ' ')}")
        print(f"{prog} done {lane_id} did not run after landing; the lane stays landed and holds no slot.\n"
              f"{error}\nFix that, then run {prog} done {lane_id}")


def finish(lane_id: str, force_reason: str | None) -> None:
    prog = config.prog()
    with state.locked("state"):
        record = state.load(lane_id)
        refusals: list[str] = []
        if record["status"] == "done":
            raise LaneError(f"lane {lane_id} is already done")
        if record["status"] not in ("landed", "released"):
            refusals.append(f"lane {lane_id} is {record['status']}, not landed")
        elif record.get("post_merge", {}).get("status") == "red":
            refusals.append("its post-merge check is red: fix forward (a follow-up commit on the lane) first")
        elif state.read_json(state.receipt_path(lane_id, "review")) is not None and not review.review_state(lane_id)[0]:
            # The fix round of the review runs in this worktree (#570); a lane landed without a review has none.
            refusals.append(f"its {review.review_state(lane_id)[1]}: mark each finding with "
                            + review.mark_help(lane_id))
        main = main_checkout(record["common_dir"])
        worktree = record.get("worktree")
        tip = git(main, "rev-parse", "--verify", "--quiet", record["branch"], check=False)
        integration = (record.get("landed") or {}).get("integration") or record.get("integration")
        if (tip and record["status"] in ("landed", "released")
                and not git_ok(integration, "merge-base", "--is-ancestor", tip, "HEAD")):
            refusals.append(f"{record['branch']} has commits that are not in {integration}")
        lock: str | None = None
        leftover: list[Any] = []
        scanner = procs.source()
        if worktree and os.path.isdir(worktree):
            if dirty(worktree):
                refusals.append(f"{worktree} has uncommitted changes")
            lock = worktree_lock(main, worktree)
            if lock is not None:
                refusals.append(f"an agent still holds {worktree} (locked: {lock}); run {prog} done {lane_id} "
                                "once it exits")
            # #661: what the lane left running there stops first; a session, a terminal or a lease holds it.
            leftover, held = scanner.sort_for_removal(worktree)
            refusals += [f"{reason}; it works in {worktree}" for reason in held]
        if record["ticket"] != "none" and not refusals:
            missing = ticket_commented(record)
            if missing:
                refusals.append(missing)
        if refusals and not force_reason:
            raise LaneError("done refused (not landed or not safe to remove):\n- " + "\n- ".join(refusals))
        if refusals:
            journal.ledger(f"override done {lane_id}: {force_reason} ({'; '.join(refusals)})")
        freed, removed = 0, list[str]()
        if leftover:
            survivors = {p.pid for p in scanner.stop(leftover)}
            for process in leftover:
                what = "did not stop" if process.pid in survivors else "stopped"
                journal.ledger(f"done {lane_id}: {what} {process.describe()} in {worktree}")
                removed.append(f"{what} {process.describe()}")
        if worktree and os.path.isdir(worktree):
            freed += worktrees.size(worktree)
            # A second --force removes a locked worktree, which only an override reaches.
            git(main, "worktree", "remove", "--force", *(["--force"] if lock is not None else []), worktree)
            removed.append(worktree)
        git(main, "worktree", "prune")
        harness = f"worktree-{os.path.basename(worktree)}" if worktree else None
        for branch in (record["branch"], harness):
            if not branch or not git_ok(main, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch):
                continue
            if branch == harness and integration and not git_ok(main, "merge-base", "--is-ancestor", branch,
                                                                 git(integration, "rev-parse", "HEAD")):
                continue
            git(main, "branch", "-D", branch)
            removed.append(f"branch {branch}")
        protected = [main, str(Path.home()), str(state.state_dir().resolve())]
        for path in [*record.get("cargo_targets", []), *record.get("scratch", [])]:
            if os.path.exists(path):
                freed += worktrees.remove_tree(path, protected)
                removed.append(path)
        # The lane's worktree, and the base checkouts a red check classified in, under its scratch (#568).
        checkouts = [path for path in (worktree, *record.get("scratch", [])[:1]) if path]
        for partition in worktrees.shared_partitions(*checkouts) if checkouts else []:
            with open(str(partition) + ".lock", "a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                freed += worktrees.remove_tree(str(partition), protected)
                removed.append(str(partition))
        record.update(status="done", done=journal.now(), freed_bytes=freed)
        state.save(record)
        journal.ledger(f"done {lane_id} freed={freed // 2**20} MiB removed={' '.join(removed) or '-'}")
    print(f"Lane {lane_id} done: claims released, {freed / 1e9:.1f} GB freed")
    for item in removed:
        print(f"  {item}" if item.startswith(("stopped ", "did not stop ")) else f"  removed {item}")
