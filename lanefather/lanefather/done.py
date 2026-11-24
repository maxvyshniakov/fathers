"""Finishing a landed lane: Spawnfather stops what it left running in its worktree (#661) and removes the worktree,
its build directories and its scratch (fathers#171: the lane owns them through Spawnfather, lifetimes.py); done
then removes its branches and the shared cache partitions built from it. Lanefather keeps its refusals (not landed,
commits not in the integration branch, a fix lane sharing the worktree, the agent's lock, kept lanes); without
Spawnfather nothing is removed and done refuses (exit 3).

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
    admission,
    config,
    github,
    host,
    journal,
    lifecycle,
    lifetimes,
    postmerge,
    registry,
    review_class,
    review_gate,
    review_record,
    scope,
    state,
    tickets,
    worktrees,
)
from .errors import LaneError, LaneRefused
from .gitops import current_branch, dirty, git, git_ok, main_checkout, worktree_lock
from .state import Record

REPAIR_WINDOW = 600.0


def ticket_commented(record: Record, project: config.Config) -> str | None:
    """None when the ticket has a comment since the lane started, else the refusal."""
    issue = scope.Issue(record["ticket"])
    rule = project.tickets
    data, error = github.issue_view(
        issue.local,
        json_fields="comments",
        common=None if issue.repo or rule.repo else record["common_dir"],
        repo=issue.repo or rule.repo or None,
        project=project,
    )
    if data is None:
        return f"gh could not read {issue.ref}: {error}"
    since = journal.parse_time(record["created"])
    comments: list[dict[str, Any]] = data.get("comments", [])
    # The comment land posts (#25) is not the lane's evidence.
    if any(journal.parse_time(c["createdAt"]) >= since and not c.get("body", "").startswith(tickets.LAND_MARKER)
           for c in comments):
        return None
    repo_flag = f" -R {issue.repo}" if issue.repo else ""
    return (f"{issue.ref} has no comment since the lane started; post the lane's RED->GREEN evidence with "
            f"gh issue comment {issue.local}{repo_flag}")


def done(args: Any, store: state.Store) -> int:
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
    finish(args.id, args.force_reason, store)
    return 0


def field(record: Record, name: str) -> dict[str, Any]:
    """One of the record's objects (`landed`, `post_merge`), empty when it has none."""
    value: Any = record.get(name)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def kept(record: Record) -> bool:
    """Whether auto-done and done --finished leave the lane alone (fathers#157): `keep ID` or `land --keep`. The
    landed lane's flag is the one older installs read."""
    return lifecycle.kept(record)


def keep(args: Any, store: state.Store) -> int:
    """`keep ID [--off]`: auto-done, done --finished and start's disk repair skip the lane until --off; done ID
    still finishes it. A landed lane's `landed.keep` is set too, which older installs read; before the land there
    is no `landed` to set (an older promote reads every `landed` as a merge), and land carries the flag."""
    prog = store.prog
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
    verdict = _effective_review_verdict(store, record)
    is_kept = bool(keep_override or kept(record))
    done_auto = store.project.done_auto
    needs_lock_probe = (
        check_lock
        and lifecycle.awaiting_done(record)
        and pm.phase in postmerge.SMOKE_OK_PHASES
        and isinstance(
            verdict,
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
        verdict,
        kept=is_kept,
        done_auto=done_auto,
        worktree_lock=lock,
        mode=mode,
        lane_id=lane_id,
        prog=prog,
        raw_smoke=pm.raw_status,
        cleared_by=pm.cleared_by,
    )


def _effective_review_verdict(store: state.Store, record: Record) -> review_record.ReviewVerdict:
    """Resolve a lane's review verdict, including batch coverage and `double` 2-reviewer enforcement."""
    lane_id = str(record["id"])
    rev = review_record.ReviewRecord.load(store, lane_id, record=record)
    if isinstance(rev.verdict, (review_record.Absent, review_record.Failed, review_record.Died)):
        batch_rev = review_gate.covering_batch_review(store, record)
        if batch_rev is not None:
            rev = batch_rev
    raw_landed: Any = record.get("landed")
    eff_cls = (
        cast(dict[str, Any], raw_landed).get("review_class")
        if isinstance(raw_landed, dict)
        else None
    )
    if not isinstance(eff_cls, str) or eff_cls not in review_class.CLASS_RANK:
        eff_cls = review_class.declared_class(record)
    if eff_cls == "double" and rev.has_finished_receipt:
        double_err = review_class.double_review_problem(rev.raw, label=lane_id, prog=store.project.command)
        if double_err is not None:
            return review_record.Failed(error=double_err, raw_status="incomplete")
    return rev.verdict


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
    verdict = _effective_review_verdict(store, record)
    is_kept = False if allow_kept else kept(record)
    mode = (
        postmerge.CompletionMode.INTERACTIVE
        if allow_absent_review and isinstance(verdict, review_record.Absent)
        else postmerge.CompletionMode.FINISHED
    )
    stage = str(record["status"]) if record.get("status") is not None else None
    decision = postmerge.decide_completion(
        stage,
        pm.phase,
        verdict,
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


def evidence(store: state.Store, bases: list[str], override: str | None) -> lifetimes.Evidence:
    """What done and drop vouch for to Spawnfather: the commits the lane's work is in (`bases`: the integration tip
    for done, the base for drop), the ignored files that may go (`[gc] disposable`), a --force-reason, and whether
    to stop what still works inside (`[done] stop_inside`)."""
    return lifetimes.Evidence(bases=tuple(b for b in bases if b), disposable=store.project.gc_disposable,
                              override=override, stop_inside=lifetimes.stop_inside(store.project))


def on_demand(spawnfather: lifetimes.Spawnfather, record: Record, *, clean_stale_base: bool = True) -> list[str]:
    """Register what the lane record names before a removal (a lane from before the upgrade, a registration that
    failed): a fix lane first takes its parent's worktree over. One line per path Spawnfather refused."""
    if clean_stale_base:
        parent = state.hand_over_parent(record) if lifecycle.is_fix_lane(record) else None
        if parent is not None:
            spawnfather.hand(lifetimes.owner({**record, "id": parent}), lifetimes.owner(record), "worktree")
        # The base checkouts a killed red check left in the scratch (#568) are Lanefather's own throwaway worktrees:
        # they go through git first, as base_checkout removes them (a folder holding a worktree is never Spawnfather's).
        scratch = [str(p) for p in record.get("scratch", []) if p]
        common = record.get("common_dir")
        if scratch and os.path.isdir(scratch[0]) and isinstance(common, str) and os.path.isdir(common):
            for stale in sorted(Path(scratch[0]).glob("base-*")):
                worktrees.remove_checkout(main_checkout(common), stale)
    return lifetimes.register(spawnfather, record)


def holders(record: Record, store: state.Store, spawnfather: lifetimes.Spawnfather, main: str,
            proof: lifetimes.Evidence, *, verb: str = "done",
            clean_stale_base: bool = True) -> tuple[list[str], list[str], str | None]:
    """What keeps done or drop from removing the lane: another lane in its worktree (a fix lane), an agent's lock,
    and whatever Spawnfather would keep (a process it does not stop, ignored files, work not in the bases), asked
    as a dry run after registering what the record names. Returns (overridable_refusals, hard_refusals, lock)."""
    lane_id, prog = record["id"], store.project.command
    worktree = record.get("worktree")
    refusals: list[str] = []
    hard_refusals: list[str] = []
    # A fix lane works in its parent's worktree (review --fix-lane): it keeps the worktree while not done.
    sharing = [r["id"] for r in store.lanes() if r["id"] != lane_id and not lifecycle.finished(r)
               and worktree and r.get("worktree") == worktree]
    if sharing:
        refusals.append(f"lane {', '.join(sharing)} works in {worktree} too")
    lock: str | None = None
    if worktree and os.path.isdir(worktree):
        lock = worktree_lock(main, worktree)
        if lock is not None:
            refusals.append(f"an agent still holds {worktree} (locked: {lock}); run {prog} {verb} {lane_id} "
                            "once it exits")
    unowned = [f"Spawnfather does not own {problem}"
               for problem in on_demand(spawnfather, record, clean_stale_base=clean_stale_base)]
    hard_refusals += unowned
    planned = spawnfather.reap(lifetimes.owner(record), proof, dry_run=True, reason=f"lanefather {verb} {lane_id}")
    kept = [f"Spawnfather keeps {k}" for k in planned.kept
            if not (lock is not None and f"locked: {lock}" in k)]
    # When proof already carries an override, anything Spawnfather still keeps is an un-overridable integrity guard.
    if proof.override:
        hard_refusals += kept
    else:
        refusals += kept
    return refusals, hard_refusals, lock


def remove(record: Record, store: state.Store, spawnfather: lifetimes.Spawnfather, main: str, proof: lifetimes.Evidence,
           *, verb: str, holding: str | None, keep_branches: tuple[str, ...] = ()) -> tuple[int, list[str]]:
    """Have Spawnfather stop what the lane left behind and remove its worktree, build directories and scratch, then
    remove its branches and the shared cache partitions built from it: the bytes freed and what went. The agent's
    harness branch goes only when `holding` (the integration tip for done, the base for drop) holds it. A reap
    Spawnfather refuses leaves the lane as it was (done again retries: what went stays gone)."""
    lane_id, project = record["id"], store.project
    worktree = record.get("worktree")
    freed, removed = 0, list[str]()
    scratch = [str(p) for p in record.get("scratch", []) if p]
    harness = f"worktree-{os.path.basename(worktree)}" if worktree else None
    kept_set = set(keep_branches)
    branches = [b for b in (record["branch"], harness)
                if b and b not in kept_set and git_ok(main, "rev-parse", "--verify", "--quiet", "refs/heads/" + b)]
    unowned = on_demand(spawnfather, record, clean_stale_base=True)
    if unowned:
        raise LaneError(f"{verb} refused: Spawnfather does not own what the lane holds:\n- "
                        + "\n- ".join(unowned))
    sizes = {path: worktrees.size(path) for _, path in lifetimes.lane_paths(record)}
    reaped = spawnfather.reap(lifetimes.owner(record), proof, dry_run=False, reason=f"lanefather {verb} {lane_id}")
    for process in reaped.stopped:
        journal.ledger(f"{verb} {lane_id}: Spawnfather stopped {process} in {worktree}", kind="process.stopped",
                       lane=record, details={"source": "spawnfather", "process": process, "worktree": worktree},
                       directory=store.directory, project=project)
        removed.append(f"stopped {process}")
    if reaped.kept:
        raise LaneError(f"{verb} refused: Spawnfather kept what the lane holds:\n- " + "\n- ".join(reaped.kept))
    for path in reaped.removed:
        freed += sizes.get(path, 0)
        removed.append(path)
    git(main, "worktree", "prune")
    for branch in branches:
        if not git_ok(main, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch):
            removed.append(f"branch {branch}")  # Spawnfather took it with its worktree: merged
            continue
        if branch == harness and (not holding or not git_ok(main, "merge-base", "--is-ancestor", branch, holding)):
            continue
        git(main, "branch", "-D", branch)
        removed.append(f"branch {branch}")
    protected = [main, str(Path.home()), str(store.directory.resolve())]
    # The lane's worktree, and the base checkouts a red check classified in, under its scratch (#568).
    checkouts = [path for path in (worktree, *scratch[:1]) if path]
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
    spawnfather: lifetimes.Spawnfather
    proof: lifetimes.Evidence
    lock: str | None
    integration: str | None
    cleared: dict[str, Any] | None
    refusals: list[str]


def finish_refusals(
    lane_id: str, force_reason: str | None, store: state.Store, *, unattended: bool = False
) -> FinishPlan | None:
    """Check status, smoke, review, integration and holders under the `state` lock.

    Returns None when `unattended` sees the lane is already done; raises `LaneError` on refusals unless
    `force_reason` overrides them, and `LaneRefused` (exit 3, no override) without Spawnfather; otherwise returns a
    `FinishPlan` for `cleanup` to execute outside the lock.
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
        if not isinstance(common, str) or not common or not os.path.isdir(common):
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
        spawnfather = lifetimes.client()
        if spawnfather is None:
            raise LaneRefused(f"done {lane_id} refused: {lifetimes.INSTALL_HINT}; nothing was removed")
        if worktree and os.path.isdir(worktree):
            if dirty(worktree):
                refusals.append(f"{worktree} has uncommitted changes")
            head = git(worktree, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
            if integration and head and not git_ok(integration, "merge-base", "--is-ancestor", head, "HEAD"):
                refusals.append(f"{worktree} is at {head[:10]}, with commits that are not in {integration}")
        holding = git(integration, "rev-parse", "HEAD", check=False) if integration else None
        proof = evidence(store, [holding] if holding else [], force_reason)
        held, hard_refusals, lock = holders(
            record, store, spawnfather, main, proof, clean_stale_base=not refusals or bool(force_reason)
        )
        refusals += held
        if hard_refusals:
            raise LaneError("done refused (not landed or not safe to remove):\n- "
                            + "\n- ".join(refusals + hard_refusals))
        if refusals and not force_reason:
            raise LaneError("done refused (not landed or not safe to remove):\n- " + "\n- ".join(refusals))
        return FinishPlan(
            record=record, main=main, spawnfather=spawnfather, proof=proof,
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
        journal.override("done", record, force_reason, refusals, audited=False, directory=store.directory,
                         project=store.project)
    holding = git(plan.integration, "rev-parse", "HEAD") if plan.integration else None
    return remove(record, store, plan.spawnfather, plan.main, plan.proof, verb="done", holding=holding)


def finish(lane_id: str, force_reason: str | None, store: state.Store, *,
           unattended: bool = False) -> int:
    """done ID in the project `store` holds: refusals under the lock, cleanup outside it, then the `done`
    transition under the lock; returns the bytes freed."""
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
    paths = git(cwd, "-c", "core.quotePath=false", "diff", "--name-only", fork, commit).splitlines()
    shown = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
    return False, f"changes {len(paths)} path{'s' if len(paths) != 1 else ''} beyond {name}: {shown}"


def drop(args: Any, store: state.Store) -> int:
    """`drop ID [ID ...]` (fathers#167, fathers#237): drop_lane on each id in turn; one that refuses is reported and
    the others go on. Exits with the first refusal's code."""
    ids = list(dict.fromkeys([args.id] if isinstance(args.id, str) else args.id))
    failed = 0
    for lane_id in ids:
        try:
            admission.refuse_foreign(store, "drop", lane_id)
            drop_lane(lane_id, store)
        except LaneError as error:
            if len(ids) == 1:
                raise
            print(f"{store.prog}: drop {lane_id}: {error}", file=sys.stderr)
            failed = failed or error.code
    return failed


def drop_lane(lane_id: str, store: state.Store, *, dry_run: bool = False) -> str:
    """Remove a planned or active lane that adds nothing to its base, with done's cleanup and refusals for what
    still works there; returns why it adds nothing. Nothing it removes can be lost, so it takes no --force-reason:
    a lane with commits beyond its base, a worktree HEAD that has some, or uncommitted or untracked changes
    refuse."""
    prog = store.prog
    with store.locked("state"):
        record = store.load(lane_id)
        if lifecycle.awaiting_done(record):
            raise LaneError(f"drop refused: lane {lane_id} is {record['status']}: {prog} done {lane_id} finishes it")
        if not lifecycle.occupying(record):
            raise LaneError(f"drop refused: lane {lane_id} is already {record['status']}")
        _refuse_unmarked_findings(store, lane_id, record, "drop")
        common = record.get("common_dir")
        if not isinstance(common, str) or not common or not os.path.isdir(common):
            raise LaneError(f"lane {lane_id} names no repository that exists ({common or 'none'})")
        main = main_checkout(common)
        base_tip = git(main, "rev-parse", "--verify", "--quiet", str(record.get("base")) + "^{commit}", check=False)
        bases = [(str(record.get("base")), base_tip)] if base_tip else []
        if record.get("base_sha") and record["base_sha"] != base_tip:
            bases.append((f"its start {str(record['base_sha'])[:10]}", str(record["base_sha"])))
        if not bases:
            raise LaneError(f"drop refused: lane {lane_id}'s base {record.get('base')} is not in {main}")
        spawnfather = lifetimes.client()
        if spawnfather is None:
            raise LaneRefused(f"drop {lane_id} refused: {lifetimes.INSTALL_HINT}; nothing was removed")
        refusals: list[str] = []
        whys: list[str] = []
        # What Spawnfather may take as merged: the base, and a commit that adds nothing to it (a revert, an empty
        # tree change), which drop vouches for.
        vouched = [base for _, base in bases]
        worktree = record.get("worktree")
        tip = git(main, "rev-parse", "--verify", "--quiet", "refs/heads/" + record["branch"], check=False)
        if tip:
            empty, why = nothing_added(main, tip, bases)
            (whys if empty else refusals).append(why if empty else f"{record['branch']} {why}")
            if empty:
                vouched.append(tip)
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
                else:
                    vouched.append(head)
        proof = evidence(store, vouched, None)
        held, hard_refusals, _ = holders(
            record, store, spawnfather, main, proof, verb="drop", clean_stale_base=not refusals and not dry_run
        )
        refusals += held + hard_refusals
        if refusals:
            raise LaneError("drop refused (the lane holds work, or something still uses it):\n- "
                            + "\n- ".join(refusals) + f"\nLand it with {prog} land {lane_id}, or revert its "
                            "commits on the lane first; drop removes only a lane that adds nothing")
        why = whys[0] if whys else "no branch and no commits"
        if dry_run:
            return why
        freed, removed = remove(record, store, spawnfather, main, proof, verb="drop", holding=bases[0][1])
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
    return why


def _refuse_unmarked_findings(store: state.Store, lane_id: str, record: Record, verb: str) -> None:
    """Refuse `drop`, `park`, or `abandon` when the lane's review has unmarked findings."""
    unmarked = review_record.ReviewRecord.load(store, lane_id, record=record).open_findings
    if unmarked:
        prog = store.prog
        blocks = store.project.review_blocks
        holds = (
            ", which hold lands"
            if any(review_class.blocks_gate(f.get("severity", "P2"), blocks) for f in unmarked)
            else ""
        )
        noun = "dropped" if verb == "drop" else "parked" if verb == "park" else "abandoned"
        raise LaneError(
            f"{verb} refused: lane {lane_id}'s review has {len(unmarked)} unmarked "
            f"finding{'s' if len(unmarked) != 1 else ''}{holds}: mark each first "
            f"({prog} review {lane_id}); a {noun} lane gets no fix round"
        )


def _merged_anywhere(main: str, commit: str, *, exclude: set[str], base_shas: list[str]) -> bool:
    """Whether `commit` is reachable from `base_shas` or any local/remote branch outside `exclude`."""
    for base in base_shas:
        if git_ok(main, "merge-base", "--is-ancestor", commit, base):
            return True
    refs = git(main, "for-each-ref", "--contains", commit, "--format=%(refname)",
               "refs/heads/", "refs/remotes/", check=False).splitlines()
    for ref in refs:
        short = ref.removeprefix("refs/heads/")
        if short not in exclude:
            return True
    return False


def _validate_keep_branch(main: str, kept_ref: str, record: Record, store: state.Store) -> None:
    """Refuse invalid, reflog-syntax, or protected branch names for `abandon --keep-branch`."""
    if ("@" in kept_ref or kept_ref.startswith("-")
            or not git_ok(main, "check-ref-format", f"refs/heads/{kept_ref}")):
        raise LaneError(f"abandon refused: invalid branch name {kept_ref!r} for --keep-branch")
    raw_branch = record.get("branch")
    if isinstance(raw_branch, str) and kept_ref == raw_branch:
        raise LaneError(f"abandon refused: --keep-branch {kept_ref!r} cannot be the lane's own branch")
    raw_base = record.get("base")
    if isinstance(raw_base, str) and kept_ref == raw_base:
        raise LaneError(f"abandon refused: --keep-branch {kept_ref!r} cannot be the lane's base branch")
    if kept_ref in store.project.promoted:
        raise LaneError(f"abandon refused: --keep-branch {kept_ref!r} cannot be a promoted branch")
    integration = record.get("integration")
    if isinstance(integration, str) and integration and os.path.isdir(integration):
        integ_branch = current_branch(integration)
        if integ_branch and kept_ref == integ_branch:
            raise LaneError(f"abandon refused: --keep-branch {kept_ref!r} cannot be the integration branch")


def park_lane(lane_id: str, reason: str, store: state.Store) -> dict[str, Any]:
    """Save `lane_id`'s current branch tip on `park/<id>`, remove its worktree and lane branch, keep receipts,
    and transition `planned|active -> done` with `outcome='parked'` (fathers#57)."""
    prog = store.prog
    cleaned_reason = reason.strip()
    if not cleaned_reason:
        raise LaneError("park refused: --reason must not be empty")
    park_ref = f"park/{lane_id}"
    with store.locked("state"):
        record = store.load(lane_id)
        if lifecycle.awaiting_done(record):
            raise LaneError(f"park refused: lane {lane_id} is {record['status']}: {prog} done {lane_id} finishes it")
        if not lifecycle.occupying(record):
            raise LaneError(f"park refused: lane {lane_id} is already {lifecycle.display_state(record)}")
        _refuse_unmarked_findings(store, lane_id, record, "park")
        common = record.get("common_dir")
        if not isinstance(common, str) or not common or not os.path.isdir(common):
            raise LaneError(f"lane {lane_id} names no repository that exists ({common or 'none'})")
        main = main_checkout(common)
        spawnfather = lifetimes.client()
        if spawnfather is None:
            raise LaneRefused(f"park {lane_id} refused: {lifetimes.INSTALL_HINT}; nothing was removed")
        refusals: list[str] = []
        worktree = record.get("worktree")
        raw_branch = record.get("branch")
        branch_name = raw_branch if isinstance(raw_branch, str) and raw_branch else ""
        tip = (
            git(main, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch_name, check=False)
            if branch_name
            else ""
        )
        head = ""
        if isinstance(worktree, str) and worktree and os.path.isdir(worktree):
            changes = git(worktree, "status", "--porcelain", "--untracked-files=all").splitlines()
            if changes:
                shown = ", ".join(line.split(None, 1)[-1] for line in changes[:5]) + (
                    f" and {len(changes) - 5} more" if len(changes) > 5 else "")
                refusals.append(f"{worktree} has uncommitted or untracked changes: {shown}; commit or discard first")
            head = git(worktree, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
            if tip and head and head != tip:
                refusals.append(f"{worktree} is at {head[:10]}, not {branch_name} ({tip[:10]})")
        if not tip:
            tip = head
        if not tip:
            raw_base_sha = record.get("base_sha")
            raw_base = record.get("base")
            if isinstance(raw_base_sha, str) and raw_base_sha and git_ok(
                main, "rev-parse", "--verify", "--quiet", raw_base_sha + "^{commit}"
            ):
                tip = raw_base_sha
            elif isinstance(raw_base, str) and raw_base:
                tip = git(main, "rev-parse", "--verify", "--quiet", raw_base + "^{commit}", check=False)
        if not tip:
            raise LaneError(f"park refused: lane {lane_id} has no branch tip or base commit to park")
        existing_park = git(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{park_ref}^{{commit}}", check=False)
        if existing_park and not git_ok(main, "merge-base", "--is-ancestor", existing_park, tip):
            raise LaneError(
                f"park refused: branch {park_ref} already exists at {existing_park[:10]} and is not an ancestor of "
                f"{tip[:10]}; inspect or remove {park_ref} first"
            )
        proof = evidence(store, [tip], None)
        held, hard_refusals, _ = holders(
            record, store, spawnfather, main, proof, verb="park", clean_stale_base=not refusals
        )
        refusals += held + hard_refusals
        if refusals:
            raise LaneError("park refused:\n- " + "\n- ".join(refusals))
        if existing_park:
            if existing_park != tip:
                git(main, "update-ref", f"refs/heads/{park_ref}", tip, existing_park)
        else:
            git(main, "branch", park_ref, tip)
        freed, removed = remove(
            record, store, spawnfather, main, proof, verb="park", holding=tip, keep_branches=(park_ref,)
        )
        now = journal.now()
        parked_info: dict[str, Any] = {"ref": park_ref, "tip": tip, "reason": cleaned_reason, "at": now}
        lifecycle.transition(
            store, lane_id, "park",
            done=now, outcome="parked", parked=parked_info, freed_bytes=freed,
            ledger_message=(f"park {lane_id} ref={park_ref} tip={tip[:10]}: {cleaned_reason} "
                            f"freed={freed // 2**20} MiB removed={' '.join(removed) or '-'}"),
            ledger_details={"ref": park_ref, "tip": tip, "reason": cleaned_reason,
                            "freed_bytes": freed, "removed": removed},
        )
    print(f"Lane {lane_id} parked at {park_ref} ({tip[:10]}): claims released, {freed / 1e9:.1f} GB freed; "
          f"resume with {prog} unpark {lane_id}")
    print_removed(removed)
    return parked_info


def abandon_lane(lane_id: str, reason: str, store: state.Store, *,
                 keep_branch: str | None = None, force_reason: str | None = None) -> dict[str, Any]:
    """Transition `planned|active -> done` with `outcome='abandoned'`, removing worktree, branch, scratch and claims.
    Refuses when the branch has commits not merged anywhere unless `keep_branch` or `force_reason` is given."""
    prog = store.prog
    cleaned_reason = reason.strip()
    if not cleaned_reason:
        raise LaneError("abandon refused: --reason must not be empty")
    kept_ref = keep_branch.strip() if isinstance(keep_branch, str) else None
    if keep_branch is not None and not kept_ref:
        raise LaneError("abandon refused: --keep-branch must not be empty")
    with store.locked("state"):
        record = store.load(lane_id)
        if lifecycle.awaiting_done(record):
            raise LaneError(
                f"abandon refused: lane {lane_id} is {record['status']}: {prog} done {lane_id} finishes it"
            )
        if not lifecycle.occupying(record):
            raise LaneError(f"abandon refused: lane {lane_id} is already {lifecycle.display_state(record)}")
        _refuse_unmarked_findings(store, lane_id, record, "abandon")
        common = record.get("common_dir")
        if not isinstance(common, str) or not common or not os.path.isdir(common):
            raise LaneError(f"lane {lane_id} names no repository that exists ({common or 'none'})")
        main = main_checkout(common)
        if kept_ref is not None:
            _validate_keep_branch(main, kept_ref, record, store)
        spawnfather = lifetimes.client()
        if spawnfather is None:
            raise LaneRefused(f"abandon {lane_id} refused: {lifetimes.INSTALL_HINT}; nothing was removed")
        base_shas: list[str] = []
        raw_base = record.get("base")
        base_tip = (
            git(main, "rev-parse", "--verify", "--quiet", raw_base + "^{commit}", check=False)
            if isinstance(raw_base, str) and raw_base
            else ""
        )
        if base_tip:
            base_shas.append(base_tip)
        raw_base_sha = record.get("base_sha")
        if (isinstance(raw_base_sha, str) and raw_base_sha and raw_base_sha not in base_shas
                and git_ok(main, "rev-parse", "--verify", "--quiet", raw_base_sha + "^{commit}")):
            base_shas.append(raw_base_sha)
        worktree = record.get("worktree")
        harness = f"worktree-{os.path.basename(worktree)}" if isinstance(worktree, str) and worktree else None
        raw_branch = record.get("branch")
        branch_name = raw_branch if isinstance(raw_branch, str) and raw_branch else ""
        own_branches = {b for b in (branch_name, harness) if b}
        tip = (
            git(main, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch_name, check=False)
            if branch_name
            else ""
        )
        head = ""
        refusals: list[str] = []
        vouched: list[str] = list(base_shas)
        if isinstance(worktree, str) and worktree and os.path.isdir(worktree):
            changes = git(worktree, "status", "--porcelain", "--untracked-files=all").splitlines()
            if changes:
                shown = ", ".join(line.split(None, 1)[-1] for line in changes[:5]) + (
                    f" and {len(changes) - 5} more" if len(changes) > 5 else "")
                refusals.append(f"{worktree} has uncommitted or untracked changes: {shown}")
            head = git(worktree, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
        if tip:
            tip_merged = _merged_anywhere(main, tip, exclude=own_branches, base_shas=base_shas)
            if tip_merged or kept_ref is not None:
                vouched.append(tip)
            else:
                refusals.append(
                    f"{branch_name} ({tip[:10]}) has commits not merged anywhere; "
                    f"pass --keep-branch park/{lane_id} or --force-reason"
                )
        if head and head != tip:
            head_merged = _merged_anywhere(main, head, exclude=own_branches, base_shas=base_shas)
            if head_merged or (not tip and kept_ref is not None):
                vouched.append(head)
            else:
                refusals.append(
                    f"{worktree} is at {head[:10]}, with commits not merged anywhere"
                    + (f" (differs from {branch_name} {tip[:10]})" if tip else "")
                    + "; commit on the lane branch or pass --force-reason"
                )
        save_tip = tip or head or (base_shas[0] if base_shas else "")
        existing_kept = ""
        if kept_ref is not None:
            existing_kept = git(
                main, "rev-parse", "--verify", "--quiet", f"refs/heads/{kept_ref}^{{commit}}", check=False
            )
            if existing_kept and existing_kept != save_tip:
                raise LaneError(
                    f"abandon refused: branch {kept_ref} already exists at {existing_kept[:10]}; choose another ref"
                )
        proof = evidence(store, vouched, force_reason)
        held, hard_refusals, _ = holders(
            record, store, spawnfather, main, proof, verb="abandon",
            clean_stale_base=not refusals or bool(force_reason),
        )
        refusals += held
        if hard_refusals:
            raise LaneError("abandon refused:\n- " + "\n- ".join(refusals + hard_refusals))
        if refusals and not force_reason:
            raise LaneError("abandon refused:\n- " + "\n- ".join(refusals))
        override_entry: dict[str, Any] | None = None
        if refusals:
            override_entry = journal.override(
                "abandon", record, force_reason, refusals, audited=False, store=store,
            )
        if kept_ref is not None and save_tip and not existing_kept:
            git(main, "branch", kept_ref, save_tip)
        freed, removed = remove(
            record, store, spawnfather, main, proof, verb="abandon",
            holding=save_tip or None, keep_branches=(kept_ref,) if kept_ref is not None else (),
        )
        now = journal.now()
        abandoned_info: dict[str, Any] = {
            "reason": cleaned_reason, "at": now, "tip": tip or None, "kept_branch": kept_ref,
        }
        lifecycle.transition(
            store, lane_id, "abandon",
            done=now, outcome="abandoned", abandoned=abandoned_info, freed_bytes=freed,
            overrides=[override_entry] if override_entry is not None else None,
            ledger_message=(f"abandon {lane_id}: {cleaned_reason}"
                            + (f" kept={kept_ref}" if kept_ref else "")
                            + f" freed={freed // 2**20} MiB removed={' '.join(removed) or '-'}"),
            ledger_details={"reason": cleaned_reason, "tip": tip or None, "kept_branch": kept_ref,
                            "freed_bytes": freed, "removed": removed},
        )
    print(f"Lane {lane_id} abandoned ({cleaned_reason}): claims released, {freed / 1e9:.1f} GB freed"
          + (f"; branch kept at {kept_ref}" if kept_ref else ""))
    print_removed(removed)
    return abandoned_info


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
