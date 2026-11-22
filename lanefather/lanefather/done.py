"""Finishing a landed lane: stop what it left running in its worktree (#661), then remove the worktree, its branches,
its build directories, its scratch and the shared cache partitions built from it.

A lane whose agent still locks its worktree is released instead. It no longer counts toward the lane cap or holds
claims; done finishes it once the agent exits.

Auto-done (fathers#157, fathers#227): `settle_lane` and `after_smoke` drive every completion path (`done`, `done
--finished`, post-smoke auto-done, and post-review auto-done) through `postmerge.decide_completion` and re-evaluate
under `store.locked("state")` before releasing or cleaning up. Every function acts on the project of the Store it is
given (`store.project`), never on a module-wide current project.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, cast

from . import (
    config,
    github,
    host,
    journal,
    lifecycle,
    postmerge,
    procs,
    registry,
    review_record,
    state,
    tickets,
    worktrees,
)
from .errors import LaneError
from .gitops import dirty, git, git_ok, main_checkout, worktree_lock
from .state import Record

REPAIR_WINDOW = 600.0


def ticket_commented(record: Record, project: config.Config) -> str | None:
    """None when the ticket has a comment since the lane started, else the refusal."""
    data, error = github.issue_view(record["ticket"], json_fields="comments", common=record["common_dir"],
                                    project=project)
    if data is None:
        return f"gh could not read #{record['ticket']}: {error}"
    since = journal.parse_time(record["created"])
    comments: list[dict[str, Any]] = data.get("comments", [])
    # The comment land posts (#25) is not the lane's evidence.
    if any(journal.parse_time(c["createdAt"]) >= since and not c.get("body", "").startswith(tickets.LAND_MARKER)
           for c in comments):
        return None
    return (f"#{record['ticket']} has no comment since the lane started; post the lane's RED->GREEN evidence with "
            f"gh issue comment {record['ticket']}")


def done(args: Any) -> int:
    if args.finished:
        if args.id or args.force_reason:
            raise LaneError("done --finished takes no lane id and no --force-reason: it runs done, with every "
                            "refusal, on each finished lane of the registered projects")
        finish_finished(args.dry_run, skip_auto_off=False)
        return 0
    if args.dry_run:
        raise LaneError("--dry-run goes with --finished: done --finished --dry-run lists the lanes it would finish")
    if not args.id:
        raise LaneError("done needs a lane id, or --finished for every finished lane")
    finish(args.id, args.force_reason)
    return 0


def field(record: Record, name: str) -> dict[str, Any]:
    """One of the record's objects (`landed`, `post_merge`), empty when it has none."""
    value: Any = record.get(name)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def kept(record: Record) -> bool:
    """Whether auto-done and done --finished leave the lane alone (fathers#157): `keep ID` or `land --keep`. The
    landed lane's flag is the one older installs read."""
    return lifecycle.kept(record)


def keep(args: Any) -> int:
    """`keep ID [--off]`: auto-done, done --finished and start's disk repair skip the lane until --off; done ID
    still finishes it. A landed lane's `landed.keep` is set too, which older installs read; before the land there
    is no `landed` to set (an older promote reads every `landed` as a merge), and land carries the flag."""
    store = state.Store(config.current())
    prog = store.project.command
    lifecycle.set_keep(store, args.id, not args.off)
    if args.off:
        print(f"Lane {args.id} no longer kept: once it is finished, done runs by itself "
              f"(or {prog} done --finished)")
    else:
        print(f"Lane {args.id} kept: auto-done and {prog} done --finished leave it; {prog} done {args.id} "
              f"finishes it, {prog} keep {args.id} --off releases it")
    return 0


@dataclass(frozen=True)
class DoneReadiness:
    """Whether a lane record in `store` is ready for `done`, and if not, why."""

    ready: bool
    reason: str | None = None
    refusal: str | None = None
    cleared: dict[str, Any] | None = None


def _worktree_lock_for(record: Record) -> str | None:
    """Agent lock reason on `record`'s worktree when both `common_dir` and `worktree` exist, else `None`."""
    common = record.get("common_dir")
    worktree = record.get("worktree")
    if (
        not isinstance(common, str)
        or not common
        or not os.path.isdir(common)
        or not isinstance(worktree, str)
        or not worktree
        or not os.path.isdir(worktree)
    ):
        return None
    return worktree_lock(main_checkout(common), worktree)


def evaluate_completion(
    store: state.Store,
    record: Record,
    *,
    mode: postmerge.CompletionMode,
    keep_override: bool = False,
    integration: str | None = None,
    check_lock: bool = True,
) -> postmerge.CompletionDecision:
    """Load post-merge and review state from `store`, guard `common_dir`, and run pure `decide_completion`."""
    lane_id = str(record["id"])
    prog = store.project.command
    pm = postmerge.PostMergeRecord.load(store, record, integration=integration)
    rev = review_record.ReviewRecord.load(store, lane_id)
    is_kept = bool(keep_override or kept(record))
    done_auto = store.project.done_auto
    needs_lock_probe = (
        check_lock
        and lifecycle.awaiting_done(record)
        and pm.phase in (postmerge.PostMergePhase.GREEN, postmerge.PostMergePhase.CLEARED)
        and isinstance(
            rev.verdict,
            (review_record.Absent, review_record.Exempt, review_record.Complete)
            if mode is postmerge.CompletionMode.INTERACTIVE
            else (review_record.Exempt, review_record.Complete),
        )
        and (not is_kept or mode is postmerge.CompletionMode.INTERACTIVE)
        and (done_auto or mode in (postmerge.CompletionMode.INTERACTIVE, postmerge.CompletionMode.FINISHED))
    )
    lock = _worktree_lock_for(record) if needs_lock_probe else None
    stage = str(record["status"]) if record.get("status") is not None else None
    return postmerge.decide_completion(
        stage,
        pm.phase,
        rev.verdict,
        kept=is_kept,
        done_auto=done_auto,
        worktree_lock=lock,
        mode=mode,
        lane_id=lane_id,
        prog=prog,
        raw_smoke=pm.raw_status,
        cleared_by=pm.cleared_by,
    )


def done_readiness(
    record: Record,
    store: state.Store,
    *,
    integration: str | None = None,
    allow_absent_review: bool = False,
    allow_kept: bool = False,
) -> DoneReadiness:
    """Evaluate status, post-merge smoke, review verdict and keep flag for `done` via `decide_completion`."""
    lane_id = str(record["id"])
    prog = store.project.command
    pm = postmerge.PostMergeRecord.load(store, record, integration=integration)
    rev = review_record.ReviewRecord.load(store, lane_id)
    is_kept = False if allow_kept else kept(record)
    mode = (
        postmerge.CompletionMode.INTERACTIVE
        if allow_absent_review and isinstance(rev.verdict, review_record.Absent)
        else postmerge.CompletionMode.FINISHED
    )
    stage = str(record["status"]) if record.get("status") is not None else None
    decision = postmerge.decide_completion(
        stage,
        pm.phase,
        rev.verdict,
        kept=is_kept,
        done_auto=True,
        worktree_lock=None,
        mode=mode,
        lane_id=lane_id,
        prog=prog,
        raw_smoke=pm.raw_status,
        cleared_by=pm.cleared_by,
    )
    if decision.already_done:
        return DoneReadiness(
            ready=False,
            reason="it is done",
            refusal=f"lane {lane_id} is done, not landed",
        )
    if allow_absent_review and not allow_kept and kept(record) and decision.ready:
        return DoneReadiness(
            ready=False,
            reason="kept",
            refusal=f"lane {lane_id} is kept ({prog} keep {lane_id} --off releases it)",
            cleared=decision.cleared_by,
        )
    return DoneReadiness(
        ready=decision.ready,
        reason=decision.reason,
        refusal=decision.refusal,
        cleared=decision.cleared_by,
    )


def unfinished(record: Record, store: state.Store) -> str | None:
    """None when the lane is finished (fathers#157): landed, its smoke green (or red and cleared by a later green
    run, fathers#167), its review finished or exempt with every finding (each panel member's) marked, and not kept;
    else why not."""
    return done_readiness(record, store, allow_absent_review=False, allow_kept=False).reason


def refused(lane_id: str, error: Exception, store: state.Store) -> str:
    """Log an unattended done that did not run (lane.done_refused); its reason on one line."""
    text = str(error).replace("\n", " ") or type(error).__name__
    journal.ledger(f"auto-done {lane_id} refused: {text}", kind="lane.done_refused", level="warn", lane=lane_id,
                   details={"error": text}, directory=store.directory, project=store.project)
    return text


def _print_hold_notice(
    store: state.Store,
    record: Record,
    decision: postmerge.CompletionDecision,
) -> None:
    lane_id = str(record["id"])
    prog = store.project.command
    if decision.action is postmerge.CompletionAction.HOLD_KEPT:
        how = "--keep" if field(record, "landed").get("keep") and not record.get("keep") else f"{prog} keep"
        print(f"Lane {lane_id} kept ({how}): it does not count toward the lane cap; "
              f"finish it with {prog} done {lane_id}")
    elif decision.action is postmerge.CompletionAction.HOLD_REVIEW:
        print(
            f"Lane {lane_id} kept for its review ({decision.reason}): it does not count toward the lane cap; "
            f"marking its last finding finishes it ({review_record.mark_help(lane_id, prog)})"
        )
    elif decision.action is postmerge.CompletionAction.HOLD_AUTO_OFF:
        print(f"Lane {lane_id} is finished; auto-done is off ([done] auto = false): "
              f"finish it with {prog} done {lane_id} or {prog} done --finished")


def settle_lane(
    store: state.Store,
    lane_id: str,
    *,
    mode: postmerge.CompletionMode,
    keep_override: bool = False,
) -> postmerge.CompletionDecision:
    """Unify lane completion across post-smoke auto-done, post-review auto-done, `done`, and `done --finished`.

    Re-evaluates `evaluate_completion` under `store.locked("state")` before releasing or holding, guards `common_dir`
    existence, serializes per-lane cleanup without holding `state` across `gh`/`du`/`rmtree`, and suppresses duplicate
    output on idempotent repeats.
    """
    prog = store.project.command
    decision = evaluate_completion(store, store.load(lane_id), mode=mode, keep_override=keep_override)
    if decision.action is postmerge.CompletionAction.NOOP:
        return decision

    if decision.action is not postmerge.CompletionAction.CLEANUP:
        released_notice: str | None = None
        hold_record: Record | None = None
        with store.locked("state"):
            record = store.load(lane_id)
            decision = evaluate_completion(store, record, mode=mode, keep_override=keep_override)
            if decision.action is postmerge.CompletionAction.NOOP:
                return decision
            if decision.action is postmerge.CompletionAction.REFUSE:
                raise LaneError(decision.refusal or decision.reason or f"done {lane_id} refused")
            if decision.action is postmerge.CompletionAction.RELEASE:
                if not lifecycle.released(record):
                    worktree = record.get("worktree")
                    lock = decision.worktree_lock
                    lifecycle.transition(
                        store,
                        lane_id,
                        "release",
                        released=journal.now(),
                        post_merge_cleared_by=decision.cleared_by,
                        ledger_message=(
                            f"release {lane_id}: worktree {worktree} is locked ({lock}); "
                            "lane cap space and claims freed"
                        ),
                        ledger_details={
                            "worktree": worktree,
                            "lock": lock,
                            **({"post_merge_cleared_by": decision.cleared_by} if decision.cleared_by else {}),
                        },
                    )
                    released_notice = (
                        f"Lane {lane_id} released: it no longer counts toward the lane cap or holds claims. "
                        f"Its agent still holds {worktree} (locked: {lock}); once the agent exits, remove it "
                        f"with {prog} done {lane_id}"
                    )
            elif decision.action in (
                postmerge.CompletionAction.HOLD_KEPT,
                postmerge.CompletionAction.HOLD_REVIEW,
                postmerge.CompletionAction.HOLD_AUTO_OFF,
            ):
                hold_record = record
        if released_notice is not None:
            print(released_notice)
            return decision
        if hold_record is not None:
            _print_hold_notice(store, hold_record, decision)
            return decision
        if decision.action is not postmerge.CompletionAction.CLEANUP:
            return decision

    if mode is postmerge.CompletionMode.INTERACTIVE:
        with store.locked(f"done-{lane_id}"):
            finish(lane_id, None, store, unattended=False)
        return decision

    try:
        with store.locked(f"done-{lane_id}"):
            finish(lane_id, None, store, unattended=True)
    except LaneError as error:
        if mode is postmerge.CompletionMode.FINISHED:
            raise
        refused(lane_id, error, store)
        print(
            f"{prog} done {lane_id} did not run after landing; "
            "the lane stays landed and does not count toward the lane cap.\n"
            f"{error}\nFix that, then run {prog} done {lane_id}"
        )
    return decision


def after_smoke(
    store: state.Store,
    record: Record,
    merge: str,
    integration: str,
    onto: str,
    receipt: dict[str, Any] | None,
    smoke_status: str,
    smoke_summary: str,
    *,
    keep_override: bool = False,
) -> postmerge.CompletionDecision | None:
    """After a post-merge smoke or recheck: update the lane's ticket, then settle the lane when green."""
    tickets.after_land(store, record, merge, integration, onto, receipt, smoke_status, smoke_summary)
    if smoke_status != "green":
        return None
    return settle_lane(
        store,
        str(record["id"]),
        mode=postmerge.CompletionMode.AUTO_AFTER_SMOKE,
        keep_override=keep_override,
    )


def removal_paths(record: Record) -> list[str]:
    """What done removes of the lane besides its branches: the worktree, its build directories and its scratch."""
    worktree = record.get("worktree")
    return [p for p in (worktree, *record.get("cargo_targets", []), *record.get("scratch", []))
            if p and os.path.exists(p)]


def holders(record: Record, store: state.Store, scanner: Any, main: str, *, unattended: bool, verb: str = "done",
            ) -> tuple[list[str], list[Any], str | None]:
    """What keeps done or drop from removing the lane: another lane in its worktree (a fix lane), no process source
    while unattended, an agent's lock, and processes the source does not take for left behind in the worktree, the
    build directories or the scratch. The refusals, what the source would stop in the worktree, and the lock."""
    lane_id, prog = record["id"], store.project.command
    worktree = record.get("worktree")
    refusals: list[str] = []
    # A fix lane works in its parent's worktree (review --fix-lane): it keeps the worktree while not done.
    sharing = [r["id"] for r in store.lanes() if r["id"] != lane_id and not lifecycle.finished(r)
               and worktree and r.get("worktree") == worktree]
    if sharing:
        refusals.append(f"lane {', '.join(sharing)} works in {worktree} too")
    if unattended and isinstance(scanner, procs.NoSource):
        refusals.append("no process source answers (slotfather procs is missing or too old, and no [hooks] "
                        f"process_scan): unattended done needs one; {prog} done {lane_id} removes it as before")
    lock: str | None = None
    leftover: list[Any] = []
    if worktree and os.path.isdir(worktree):
        lock = worktree_lock(main, worktree)
        if lock is not None:
            refusals.append(f"an agent still holds {worktree} (locked: {lock}); run {prog} {verb} {lane_id} "
                            "once it exits")
        # #661: what the process source takes for left behind there stops first; any other process, or no
        # answer, holds the worktree.
        leftover, held = scanner.sort_for_removal(worktree)
        refusals += [f"{reason}; it works in {worktree}" for reason in held]
    # The build directories and scratch go too: whatever works there holds them (fathers#157 review).
    for path in removal_paths(record):
        if path == worktree:
            continue
        stopping, held = scanner.sort_for_removal(path)
        refusals += [f"{reason}; it works in {path}" for reason in held]
        refusals += [f"{process.describe()} works in {path}" for process in stopping]
    return refusals, leftover, lock


def remove(record: Record, store: state.Store, scanner: Any, main: str, leftover: list[Any], lock: str | None, *,
           verb: str, holding: str | None, overridden: bool = False) -> tuple[int, list[str]]:
    """Stop what the lane left behind, then remove its worktree, branches, build directories, scratch and the shared
    cache partitions built from it: the bytes freed and what went. The agent's harness branch goes only when
    `holding` (the integration tip for done, the base for drop) holds it. `overridden`: a --force-reason took
    the lane, so a process still in the worktree does not keep it."""
    lane_id, project = record["id"], store.project
    worktree = record.get("worktree")
    freed, removed = 0, list[str]()
    if leftover and worktree:
        # The source stops only what it took for left behind, then reads the worktree again: whatever still works
        # there keeps the worktree, as tools/lane done and the janitor do since #661's review (fail closed).
        left = scanner.stop(leftover, worktree)
        for process in leftover:
            journal.ledger(f"{verb} {lane_id}: {procs.source_name(scanner)} stopped {process.describe()} in "
                           f"{worktree}", kind="process.stopped", lane=record,
                           details={"source": procs.source_name(scanner), "process": process.describe(),
                                    "worktree": worktree}, directory=store.directory, project=project)
            removed.append(f"stopped {process.describe()}")
        if left and not overridden:
            raise LaneError(f"{verb} refused: {procs.source_name(scanner)} stopped what it took for left behind in "
                            f"{worktree}, but it is still in use:\n- " + "\n- ".join(p.describe() for p in left))
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
        if branch == harness and (not holding or not git_ok(main, "merge-base", "--is-ancestor", branch, holding)):
            continue
        git(main, "branch", "-D", branch)
        removed.append(f"branch {branch}")
    protected = [main, str(Path.home()), str(store.directory.resolve())]
    for path in [*record.get("cargo_targets", []), *record.get("scratch", [])]:
        if os.path.exists(path):
            freed += worktrees.remove_tree(path, protected)
            removed.append(path)
    # The lane's worktree, and the base checkouts a red check classified in, under its scratch (#568).
    checkouts = [path for path in (worktree, *record.get("scratch", [])[:1]) if path]
    for partition in worktrees.shared_partitions(*checkouts, project=project) if checkouts else []:
        with open(str(partition) + ".lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            freed += worktrees.remove_tree(str(partition), protected)
            removed.append(str(partition))
    return freed, removed


def print_removed(removed: list[str]) -> None:
    for item in removed:
        print(f"  {item}" if item.startswith(("stopped ", "did not stop ")) else f"  removed {item}")


@dataclass(frozen=True)
class FinishPlan:
    """What `finish_refusals` gathers under the `state` lock for `cleanup` to act on outside it."""

    record: Record
    main: str
    scanner: Any
    leftover: list[Any]
    lock: str | None
    integration: str | None
    cleared: dict[str, Any] | None
    refusals: list[str]


def finish_refusals(
    lane_id: str, force_reason: str | None, store: state.Store, *, unattended: bool = False
) -> FinishPlan | None:
    """Check status, smoke, review, integration and holders under the `state` lock.

    Returns None when `unattended` sees the lane is already done; raises `LaneError` on refusals unless
    `force_reason` overrides them; otherwise returns a `FinishPlan` for `cleanup` to execute outside the lock.
    """
    project = store.project
    prog = project.command
    with store.locked("state"):
        record = store.load(lane_id)
        refusals: list[str] = []
        if lifecycle.finished(record):
            if unattended:
                return None
            raise LaneError(f"lane {lane_id} is already done")
        if unattended and kept(record):
            raise LaneError(f"lane {lane_id} is kept ({prog} keep {lane_id} --off releases it)")
        common = record.get("common_dir")
        if not common or not os.path.isdir(common):
            raise LaneError(f"lane {lane_id} names no repository that exists ({common or 'none'})")
        main = main_checkout(common)
        worktree = record.get("worktree")
        integration = field(record, "landed").get("integration") or record.get("integration")
        if not integration or not os.path.isdir(integration):
            integration = None
        readiness = done_readiness(
            record,
            store,
            integration=integration,
            allow_absent_review=not unattended,
            allow_kept=not unattended,
        )
        if not readiness.ready and readiness.refusal is not None:
            refusals.append(readiness.refusal)
        cleared = readiness.cleared
        if integration is None:
            named = field(record, "landed").get("integration") or record.get("integration")
            refusals.append(f"its integration worktree {named or '(none)'} is gone: nothing to check its "
                            "commits against")
        tip = git(main, "rev-parse", "--verify", "--quiet", record["branch"], check=False)
        if (integration and tip and lifecycle.awaiting_done(record)
                and not git_ok(integration, "merge-base", "--is-ancestor", tip, "HEAD")):
            refusals.append(f"{record['branch']} has commits that are not in {integration}")
        scanner = procs.source(project)
        if worktree and os.path.isdir(worktree):
            if dirty(worktree):
                refusals.append(f"{worktree} has uncommitted changes")
            head = git(worktree, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
            if integration and head and not git_ok(integration, "merge-base", "--is-ancestor", head, "HEAD"):
                refusals.append(f"{worktree} is at {head[:10]}, with commits that are not in {integration}")
        held, leftover, lock = holders(record, store, scanner, main, unattended=unattended)
        refusals += held
        if refusals and not force_reason:
            raise LaneError("done refused (not landed or not safe to remove):\n- " + "\n- ".join(refusals))
        return FinishPlan(
            record=record, main=main, scanner=scanner, leftover=leftover,
            lock=lock, integration=integration, cleared=cleared, refusals=refusals,
        )


def cleanup(plan: FinishPlan, force_reason: str | None, store: state.Store) -> tuple[int, list[str]]:
    """Run the slow cleanup outside the `state` lock: ticket comment check (`gh`), `du`, worktree/branch/scratch
    removal (`rmtree`)."""
    record = plan.record
    refusals = list(plan.refusals)
    if record["ticket"] != "none" and not refusals:
        missing = ticket_commented(record, store.project)
        if missing:
            refusals.append(missing)
    if refusals and not force_reason:
        raise LaneError("done refused (not landed or not safe to remove):\n- " + "\n- ".join(refusals))
    if refusals:
        journal.ledger(f"override done {record['id']}: {force_reason} ({'; '.join(refusals)})", kind="override",
                       level="warn", lane=record,
                       details={"command": "done", "reason": force_reason, "refusals": refusals},
                       directory=store.directory, project=store.project)
    holding = git(plan.integration, "rev-parse", "HEAD") if plan.integration else None
    return remove(record, store, plan.scanner, plan.main, plan.leftover, plan.lock, verb="done", holding=holding,
                  overridden=bool(force_reason))


def finish(lane_id: str, force_reason: str | None, store: state.Store | None = None, *,
           unattended: bool = False) -> int:
    """done ID in the project `store` holds (by default the current one): refusals under the lock, cleanup outside
    it, then the `done` transition under the lock; returns the bytes freed."""
    store = store or state.Store(config.current())
    plan = finish_refusals(lane_id, force_reason, store, unattended=unattended)
    if plan is None:
        return 0
    freed, removed = cleanup(plan, force_reason, store)
    lifecycle.transition(
        store, lane_id, "finish",
        force=bool(force_reason),
        done=journal.now(), freed_bytes=freed,
        post_merge_cleared_by=plan.cleared,
        ledger_message=(f"done {lane_id} freed={freed // 2**20} MiB removed={' '.join(removed) or '-'}"
                        + (f" post-merge red cleared by {postmerge.words(plan.cleared)}" if plan.cleared else "")),
        ledger_details={"freed_bytes": freed, "removed": removed,
                        **({"post_merge_cleared_by": plan.cleared} if plan.cleared else {})},
    )
    print(f"Lane {lane_id} done: claims released, {freed / 1e9:.1f} GB freed"
          + (f"; its red post-merge smoke was cleared by {postmerge.words(plan.cleared)}" if plan.cleared else ""))
    print_removed(removed)
    return freed


def nothing_added(cwd: str, commit: str, bases: list[tuple[str, str]]) -> tuple[bool, str]:
    """Whether `commit` adds nothing to one of `bases` ((name, commit), the base branch's tip first): the base holds
    it, its tree is the base's, or its commits since its merge-base with the base change nothing. Then why; else
    what it changes beyond the first base."""
    tree = git(cwd, "rev-parse", commit + "^{tree}")
    for name, base in bases:
        if git_ok(cwd, "merge-base", "--is-ancestor", commit, base):
            return True, f"no commits beyond {name}"
        if git(cwd, "rev-parse", base + "^{tree}") == tree:
            return True, f"its tree equals {name} ({base[:10]})"
        fork = git(cwd, "merge-base", commit, base, check=False)
        if fork and git(cwd, "rev-parse", fork + "^{tree}") == tree:
            return True, f"its commits since {name} at {fork[:10]} change nothing"
    name, base = bases[0]
    fork = git(cwd, "merge-base", commit, base, check=False) or base
    paths = git(cwd, "diff", "--name-only", fork, commit).splitlines()
    shown = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
    return False, f"changes {len(paths)} path{'s' if len(paths) != 1 else ''} beyond {name}: {shown}"


def drop(args: Any) -> int:
    """`drop ID` (fathers#167): remove a planned or active lane that adds nothing to its base, with done's cleanup
    and refusals for what still works there. Nothing it removes can be lost, so it takes no --force-reason: a
    lane with commits beyond its base, a worktree HEAD that has some, or uncommitted or untracked changes refuse."""
    store = state.Store(config.current())
    project, prog, lane_id = store.project, store.project.command, args.id
    with store.locked("state"):
        record = store.load(lane_id)
        if lifecycle.awaiting_done(record):
            raise LaneError(f"drop refused: lane {lane_id} is {record['status']}: {prog} done {lane_id} finishes it")
        if not lifecycle.occupying(record):
            raise LaneError(f"drop refused: lane {lane_id} is already {record['status']}")
        common = record.get("common_dir")
        if not common or not os.path.isdir(common):
            raise LaneError(f"lane {lane_id} names no repository that exists ({common or 'none'})")
        main = main_checkout(common)
        base_tip = git(main, "rev-parse", "--verify", "--quiet", str(record.get("base")) + "^{commit}", check=False)
        bases = [(str(record.get("base")), base_tip)] if base_tip else []
        if record.get("base_sha") and record["base_sha"] != base_tip:
            bases.append((f"its start {str(record['base_sha'])[:10]}", str(record["base_sha"])))
        if not bases:
            raise LaneError(f"drop refused: lane {lane_id}'s base {record.get('base')} is not in {main}")
        refusals: list[str] = []
        whys: list[str] = []
        worktree = record.get("worktree")
        tip = git(main, "rev-parse", "--verify", "--quiet", "refs/heads/" + record["branch"], check=False)
        if tip:
            empty, why = nothing_added(main, tip, bases)
            (whys if empty else refusals).append(why if empty else f"{record['branch']} {why}")
        if worktree and os.path.isdir(worktree):
            changes = git(worktree, "status", "--porcelain", "--untracked-files=all").splitlines()
            if changes:
                shown = ", ".join(line.split(None, 1)[-1] for line in changes[:5]) + (
                    f" and {len(changes) - 5} more" if len(changes) > 5 else "")
                refusals.append(f"{worktree} has uncommitted or untracked changes: {shown}")
            head = git(worktree, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
            if head and head != tip:
                empty, why = nothing_added(main, head, bases)
                if not empty:
                    refusals.append(f"{worktree} is at {head[:10]}, which {why}")
        scanner = procs.source(project)
        held, leftover, lock = holders(record, store, scanner, main, unattended=False, verb="drop")
        refusals += held
        if refusals:
            raise LaneError("drop refused (the lane holds work, or something still uses it):\n- "
                            + "\n- ".join(refusals) + f"\nLand it with {prog} land {lane_id}, or revert its "
                            "commits on the lane first; drop removes only a lane that adds nothing")
        why = whys[0] if whys else "no branch and no commits"
        freed, removed = remove(record, store, scanner, main, leftover, lock, verb="drop", holding=bases[0][1])
        now = journal.now()
        lifecycle.transition(
            store, lane_id, "drop",
            done=now, freed_bytes=freed,
            dropped={"at": now, "why": why, "tip": tip, "base": bases[0][1]},
            ledger_message=f"drop {lane_id}: {why} freed={freed // 2**20} MiB removed={' '.join(removed) or '-'}",
            ledger_details={"why": why, "tip": tip, "base": bases[0][1], "freed_bytes": freed,
                            "removed": removed},
        )
    print(f"Lane {lane_id} dropped ({why}): claims released, {freed / 1e9:.1f} GB freed")
    print_removed(removed)
    return 0


def footprint(record: Record) -> int:
    """The bytes done would free at least: the worktree, the lane's build directories and its scratch."""
    return sum(worktrees.size(path) for path in removal_paths(record))


def finish_finished(dry_run: bool, *, skip_auto_off: bool) -> tuple[int, int, list[str]]:
    """Run done, unattended and with every refusal it has, on each finished lane of the registered projects
    (fathers#157); a dry run lists them with their sizes and removes nothing. `skip_auto_off` (start's disk repair)
    leaves the projects whose `[done] auto` is false. One project or lane that fails is reported and the rest go
    on. Prints what it removed and freed; returns the bytes freed, the lanes finished and one line per lane that
    was refused."""
    seen: set[tuple[str, str]] = set()
    freed, finished, failed, listed = 0, 0, list[str](), 0
    if dry_run:
        print("Finished lanes of the registered projects (dry run):")
    for entry, where, unreadable in registry.projects():
        if where is None:
            print(f"skipped {entry.get('project') or entry.get('state_dir')}: {unreadable}")
            continue
        if skip_auto_off and not where.config.done_auto:
            continue
        store = state.Store(where.config, directory=where.directory)
        try:
            records = state.of_project(store.lanes(), where.common)
        except Exception as error:  # one unreadable project never stops the others
            print(f"skipped {where.name}: {error}")
            continue
        for record in records:
            key = (str(where.directory), str(record.get("id")))
            if key in seen or not lifecycle.awaiting_done(record):
                continue
            seen.add(key)
            try:
                why = unfinished(record, store)
                if why is not None:
                    if dry_run:
                        print(f"  {where.name} {record['id']}: not finished ({why})")
                    continue
                if dry_run:
                    size = footprint(record)
                    freed += size
                    listed += 1
                    print(f"  {where.name} {record['id']}: {size / 1e9:.1f} GB  {record.get('worktree') or '-'}")
                    continue
                print(f"{where.name}: done {record['id']}", flush=True)
                with store.locked(f"done-{record['id']}"):
                    freed += finish(record["id"], None, store, unattended=True)
                finished += 1
            except Exception as error:  # a lane that cannot be finished is refused; the next goes on
                text = refused(str(record.get("id")), error, store)
                failed.append(f"{where.name} {record.get('id')}: {text}")
                print(f"  refused: {text}")
    if dry_run:
        print(f"{listed} finished lane{'s' if listed != 1 else ''}, {freed / 1e9:.1f} GB; dry run: nothing removed")
    else:
        print(f"done --finished: {finished} lane{'s' if finished != 1 else ''} finished, {freed / 1e9:.1f} GB freed"
              + (f"; {len(failed)} refused" if failed else ""))
    return freed, finished, failed


def repair_path() -> Path:
    """The disk repair's marker, beside the project registry it acts on: when it last ran and what it did."""
    return registry.path().with_name("disk-repair.json")


def repair_disk(free_gb: float, floor: host.Floor) -> str:
    """start below the disk floor (fathers#157): done on the machine's finished lanes, at most once per
    REPAIR_WINDOW seconds machine-wide (a start within it reuses the marker's result; one waits for another's
    repair). Never fails start. The words for the refusal should the disk still be short."""
    marker = repair_path()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        with open(marker.with_name("disk-repair.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            last = state.read_json(marker) or {}
            if isinstance(last.get("at"), (int, float)) and time.time() - last["at"] < REPAIR_WINDOW:
                words = str(last.get("words") or "")
                print(f"lane: free disk is {free_gb:.0f} GB (10^9), under the floor of {floor.words()}; done ran "
                      f"on the finished lanes {time.time() - last['at']:.0f} s ago: {words}", flush=True)
                return words
            print(f"lane: free disk is {free_gb:.0f} GB (10^9), under the floor of {floor.words()}: running done "
                  "on the registered projects' finished lanes first", flush=True)
            freed, finished, failed = finish_finished(False, skip_auto_off=True)
            words = f"done on finished lanes freed {freed / 1e9:.1f} GB (10^9) on {finished}"
            words += f", {len(failed)} refused ({'; '.join(failed[:3])})" if failed else ""
            state.atomic_write(marker, json.dumps({"v": 1, "at": time.time(), "freed_bytes": freed,
                                                   "finished": finished, "refused": failed, "words": words}) + "\n")
            return words
    except Exception as error:  # the repair only helps start; it never fails it
        print(f"lane: warning: the disk repair failed: {error}", file=sys.stderr)
        return f"the disk repair failed ({error})"
