"""land: merge --no-ff after a current green receipt, start the lane review in the background, run the post-merge
smoke, and after a green one finish the lane as done does once its review is clean or marked.

land holds its lock only for the merge, so another land waits for that alone (#573). After the merge, land runs the
smoke the merged tree's plan module defines on its own checkout detached at the merge, the first free
<main checkout>/.claude/worktrees/smoke-<n>, so smokes of several lands overlap while the integration worktree moves
on; a merge whose plan selects only the structure checks runs that plan instead. The lane's green receipt stands for
what it verified (#640): a merge whose tree is the receipt's runs nothing more, and otherwise only the checks the
diff from the lane tip to the merge selects, plus every smoke check the receipt does not show passed. Of those, a
check that already passed on the merge tree (fathers#91), or that the merged diff does not select and that passed on
the previous tip's tree (fathers#94), does not run again, and the project's `[checks] gate_only` checks are left to
the batch gate; each skip's reason is in the output, the receipt (`skipped`) and the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import sys
from typing import Any, cast

from . import (
    config,
    done,
    journal,
    lifecycle,
    plan,
    postmerge,
    receipts,
    review,
    review_class,
    review_gate,
    review_record,
    scope,
    state,
    tickets,
    verify,
    worktrees,
)
from . import results as result_store
from .errors import LaneError, LaneUsageError
from .gitops import current_branch, dirty, git, git_ok, main_checkout
from .host import host, host_fields
from .landing import landing

SmokeTuple = verify.SmokeTuple
run_smoke = verify.run_smoke
smoke_reuse = plan.smoke_reuse


def failed_items(failed: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Each failed check of the failed results with its suite, once each; a suite whose failure no check accounts
    for stands as its own item."""
    items = [(check, result["suite"]) for result in failed for check in result.get("failed_checks", [])]
    return list(dict.fromkeys(items)) or [(result["suite"], result["suite"]) for result in failed]


def touched_lines(failed: list[tuple[str, str]], integration: str, merge: str,
                  *, store: state.Store) -> list[str]:
    """The lanes in the merge, newest first, whose checks ran each failed (check, suite) item."""
    landed = [r for r in sorted(store.lanes(), key=lambda r: r.get("landed", {}).get("at", ""), reverse=True)
              if r.get("landed", {}).get("integration") == integration
              and git_ok(integration, "merge-base", "--is-ancestor", r["landed"]["merge"], merge)]
    ran = {r["id"]: receipts.lane_ran(store, r["id"], record=r) for r in landed}

    def covers(lane_id: str, item: str, suite: str) -> bool:
        lane_checks, suites, whole = ran[lane_id]
        return item in lane_checks or suite in whole or (item == suite and suite in suites)

    lines: list[str] = []
    for item, suite in failed:
        names = [f"{r['id']} ({scope.display_ticket(r['ticket'])})" for r in landed if covers(r["id"], item, suite)]
        lines.append(f"  {item}: {', '.join(names) or 'no recorded lane'}")
    return lines


def _print_smoke_failures(results: list[dict[str, Any]], triaged: str | None) -> list[dict[str, Any]]:
    """Print failed and blocked suite lines plus triage; return the failed suite rows (excluding `not_run`, #639)."""
    failed = [r for r in results if r["status"] == "failed"]
    for result in failed:
        named = ", ".join(result.get("failed_checks") or []) or "(see log)"
        blocked = f" blocked checks: {', '.join(result['blocked_checks'])}" if result.get("blocked_checks") else ""
        print(f"  {result['suite']}: failed checks {named}{blocked}  {result['log']}")
    for result in results:
        if result["status"] == "blocked":
            blocked = ", ".join(result.get("blocked_checks") or []) or "(see log)"
            print(f"  {result['suite']}: blocked checks: {blocked}  {result['log']}")
    if triaged:
        print(triaged)
    return failed


def merge_problem(lane_id: str, tip: str, tree: str, receipt: dict[str, Any] | None, *,
                  record: state.Record | None = None, prog: str = "lanefather") -> str | None:
    """Why the lane may not land: no receipt, a stale one, or a red one."""
    # Behaviour preserved in Tidy 3 (#71): land checks tree currency only, ignoring toolchain until #59.
    verdict = receipts.check_verdict(record, receipt, tree)
    if isinstance(verdict, receipts.NoReceipt):
        return f"no receipt for lane {lane_id}: run {prog} check {lane_id} in its worktree"
    if isinstance(verdict, receipts.StaleTree):
        return (f"the receipt is stale: it covers tree {verdict.receipt_tree}, the lane tip {tip[:10]} has tree "
                f"{tree}; rerun {prog} check {lane_id}")
    if isinstance(verdict, receipts.Green):
        return None
    assert isinstance(verdict, receipts.Red)
    problem = (f"the receipt for tree {tree} is red ({', '.join(verdict.failed)}"
               + (f"; not run: {', '.join(verdict.unstarted)}" if verdict.unstarted else "") + ")")
    if verdict.regressions:
        base = (verdict.classification_base or "")[:10]
        problem += (f"; regression: {', '.join(verdict.regressions)} fails on the lane"
                    f"{' again when rerun by itself' if verdict.rerun_alone else ''} and passes on its base "
                    f"{base}")
    if verdict.blocked:
        behind = f" behind {', '.join(verdict.prerequisites)}" if verdict.prerequisites else ""
        problem += f"; dependency-blocked, not run: {', '.join(verdict.blocked)}{behind}"
    return problem


@dataclass(frozen=True)
class Landed:
    """The result of `merge`: the landed lane record and merge metadata passed to `post_merge` and `after_land`."""

    record: state.Record
    merge: str
    previous: str
    integration: str
    onto: str | None
    keep: bool
    receipt: dict[str, Any] | None
    pre_land_overridden: bool = False


@dataclass(frozen=True)
class Smoke:
    """The result of `post_merge`: the updated record, smoke status, summary line and failure details."""

    record: state.Record
    status: str
    summary: str
    results: list[dict[str, Any]]
    triaged: str | None


def recheck(args: Any, store: state.Store) -> int:
    """`recheck ID` (fathers#167): the checks the lane's red post-merge smoke ran, again, on the integration tip when
    it holds the merge; green, its receipt replaces the red result and the lane is completed as land completes it.
    Rechecks of one lane run one at a time, so a later one never rewrites the receipt a green one left."""
    with store.locked(f"recheck-{args.id}", f"lane: waiting for another recheck of {args.id}"):
        with store.locked("state"):
            record = store.load(args.id)
            rplan = postmerge.PostMergeRecord.recheck_plan(store, record)
        suites, selected = rplan.suites, rplan.selected
        merge_sha, integration = rplan.merge, rplan.integration
        red_path = str(rplan.red_path)
        if rplan.recovered is not None:
            pm, path = postmerge.PostMergeRecord.recover_crashed_recheck(store, args.id, rplan)
            record = pm.record
            tip = rplan.recovered.sha
            status = "green"
            results: list[dict[str, Any]] = []
            triaged: str | None = None
            journal.ledger(
                f"recheck {args.id} green sha={tip} merge={merge_sha} receipt={path} replaces red {red_path}",
                kind="lane.rechecked",
                level="info",
                lane=record,
                details={
                    "status": "green", "sha": tip, "merge": merge_sha, "receipt": str(path),
                    "red_receipt": red_path, "suites": suites, "recovered": True,
                },
                store=store,
            )
        else:
            tip = rplan.tip
            smoke_recorded = False
            try:
                with worktrees.smoke_checkout(record["common_dir"], tip, store.project) as checkout:
                    postmerge.PostMergeRecord.start_smoke(
                        store, args.id, merge_sha=merge_sha, checkout=checkout, write_receipt=False,
                    )
                    toolchain = plan.toolchain_state(checkout, store.project)
                    decision, results, check_results, triaged, _stamp, status = run_smoke(
                        store, record, checkout, suites, selected, f"recheck-{args.id}", args.slots, toolchain
                    )
                    tree = git(checkout, "rev-parse", "HEAD^{tree}")
                status = status if results else "red"
                payload: dict[str, Any] = {
                    "version": 1, "lane": args.id, "kind": "recheck", "merge": merge_sha, "sha": tip,
                    "tree": tree, "checkout": checkout, "platform": result_store.host_platform(store.project),
                    "red_receipt": red_path, "suites": suites, "checks": selected,
                    "check_results": check_results, "results": results, "triage": triaged,
                    "status": status, "jobs": decision["jobs"], "jobs_decision": decision,
                    "leases": verify.leases_of(results), "host": host(store.project), "finished": journal.now(),
                }
                pm, path = postmerge.PostMergeRecord.record_recheck(
                    store, args.id, merge_sha=merge_sha, tip=tip, status=status, payload=payload,
                    red_receipt_path=red_path,
                )
                smoke_recorded = True
            finally:
                if not smoke_recorded:
                    postmerge.PostMergeRecord.abort_smoke(store, args.id, merge_sha=merge_sha)
            record = pm.record
            result_store.record(store, tree, verify.recordable(check_results), source="post-merge",
                                toolchain=toolchain, lane=args.id, sha=tip, receipt=str(path), phase="post-merge")
            journal.ledger(
                f"recheck {args.id} {status} sha={tip} merge={merge_sha} receipt={path}"
                + (f" replaces red {red_path}" if status == "green" else ""),
                kind="lane.rechecked",
                level="info" if status == "green" else "error",
                lane=record,
                details={
                    "status": status, "sha": tip, "merge": merge_sha, "receipt": str(path),
                    "red_receipt": red_path, "suites": suites,
                },
                store=store,
            )
    ids = [c for s in suites for c in (selected or {}).get(s, [])] if selected is not None else None
    what = ", ".join(ids) if ids is not None and len(ids) <= 8 else f"{len(ids)} checks" if ids else ", ".join(suites)
    if status != "green":
        print(f"Lane {args.id}: post-merge still red on {tip[:10]} (recheck: {what}); {path}")
        _print_smoke_failures(results, triaged)
        return 1
    print(f"Lane {args.id}: post-merge green on {tip[:10]} (recheck: {what}); it replaces the red smoke of "
          f"{merge_sha[:10]} ({path})")
    done.after_smoke(
        store,
        record,
        merge_sha,
        integration,
        current_branch(integration) or integration,
        receipts.read(store, args.id, record=record),
        status,
        f"recheck on {tip[:10]}: {what}",
        keep_override=lifecycle.kept(record),
    )
    return 0


def check_other_reviews(store: state.Store, record: state.Record,
                        force_reason: str | None) -> dict[str, Any] | None:
    """Under the landing lock, before moving the integration branch (fathers#177)."""
    held, reported = review.landing_findings(record, store)
    for lane, finding in reported:
        severity = finding.get("severity", "P2")
        print(f"lane: reported ({severity}, does not block): {review_gate.finding_line(lane, finding)}",
              file=sys.stderr)
    if not held:
        return None
    refusals = [review_gate.finding_line(lane, finding) for lane, finding in held]
    if not force_reason:
        lines = [f"- {text}" for text in refusals]
        blocking_lanes = {lane["id"]: lane for lane, _ in held}
        fixes = review.fix_round_commands(blocking_lanes.values(), review.valid_fix_parents(record, store),
                                          prog=store.prog)
        blocks = "/".join(store.project.review_blocks or config.DEFAULT.review_blocks)
        raise LaneError("\n".join([f"land refused: other finished reviews have unmarked {blocks} findings", *lines,
                                  "For findings that need a fix:", *(f"  {line}" for line in fixes),
                                  "--force-reason overrides and is logged"]))
    for text in refusals:
        print(f"lane: overridden: {text}", file=sys.stderr)
    return journal.override("land", record, force_reason, refusals, store=store)


def _find_merge_commit(integration: str, tip: str, since: str | None = None) -> tuple[str, str] | None:
    """Find the `--no-ff` merge commit in `integration` that merged `tip`, returning `(merge, first_parent)`."""
    if since is not None:
        rev_range = f"{since}..HEAD"
    elif git_ok(integration, "rev-parse", "--verify", "--quiet", f"{tip}^"):
        rev_range = f"{tip}^..HEAD"
    else:
        rev_range = "HEAD"
    log = git(integration, "log", "--merges", "--format=%H %P", "-n", "200", rev_range, check=False)
    for line in log.splitlines():
        parts = line.split()
        if len(parts) >= 3 and tip in parts[1:]:
            return parts[0], parts[1]
    return None


def _finish_land_transition(
    store: state.Store, lane_id: str, *, merge_sha: str, previous: str, tip: str, tree: str,
    integration: str, onto: str | None, keep: bool, new_overrides: list[dict[str, Any]],
    receipt: dict[str, Any] | None, pre_land_overridden: bool = False,
    partial: bool = False, remaining: str | None = None,
) -> Landed:
    landed_meta: dict[str, Any] = {
        "merge": merge_sha, "previous": previous, "tip": tip, "tree": tree,
        "integration": integration, "at": journal.now(), "keep": keep,
    }
    ledger_details: dict[str, Any] = {
        "merge": merge_sha, "previous": previous, "tip": tip, "onto": onto,
        "integration": integration, "keep": keep,
    }
    if partial:
        landed_meta["partial"] = True
        ledger_details["partial"] = True
        if remaining:
            landed_meta["remaining"] = remaining
            ledger_details["remaining"] = remaining
    record = lifecycle.transition(
        store, lane_id, "land",
        landed=landed_meta,
        overrides=new_overrides or None,
        ledger_message=f"land {lane_id} merge={merge_sha} previous={previous}",
        ledger_details=ledger_details,
    )
    return Landed(
        record=record, merge=merge_sha, previous=previous, integration=integration,
        onto=onto, keep=bool(record["landed"]["keep"]), receipt=receipt,
        pre_land_overridden=pre_land_overridden,
    )


def merge(
    store: state.Store, lane_id: str, into: str | None, message: str | None,
    keep: bool, force_reason: str | None, wait: float, *, repair: bool = False,
    partial: bool = False, remaining: str | None = None,
) -> Landed:
    """Phase 1 of land: validate, record landing intent, merge `--no-ff`, and transition to `landed`."""
    prog = store.prog
    new_overrides: list[dict[str, Any]] = []
    with store.locked("land", f"lane: waiting for another land's merge (lock {store.directory / '.land.lock'})"):
        record = store.load(lane_id)
        if not lifecycle.active(record):
            raise LaneError(f"land refused: lane {lane_id} is {record['status']}" +
                            ("; it has no worktree yet" if lifecycle.planned(record) else ""))
        unmet_after: list[str] = []
        for name in record["after"]:
            dep = store.load(name)
            if not lifecycle.after_satisfied(dep):
                unmet_after.append(f"{name} ({lifecycle.display_state(dep)})")
        if unmet_after:
            refusal = f"lane {lane_id} is declared to land after {', '.join(unmet_after)}, which has not landed"
            if not force_reason:
                raise LaneError(f"land refused: {refusal}")
            new_overrides.append(journal.override("land", record, force_reason, [refusal], store=store))
        main = main_checkout(record["common_dir"])
        tip = git(main, "rev-parse", "--verify", record["branch"] + "^{commit}")
        tree = git(main, "rev-parse", tip + "^{tree}")
        receipt = receipts.read(store, lane_id, record=record)
        if not into and not record.get("integration"):
            raise LaneError(f"no worktree has {record['base']} checked out; give --into <integration worktree>")
        integration = os.path.realpath(into or record["integration"])
        onto = current_branch(integration)
        intent = record.get("landing") if isinstance(record.get("landing"), dict) else None
        if intent and git_ok(integration, "merge-base", "--is-ancestor", str(intent.get("tip") or tip), "HEAD"):
            intent_tip = str(intent.get("tip") or tip)
            intent_prev = str(intent.get("previous") or "")
            found = _find_merge_commit(integration, intent_tip, intent_prev or None)
            merge_sha = found[0] if found else git(integration, "rev-parse", "HEAD")
            previous = found[1] if found else (intent_prev or merge_sha)
            keep_flag = bool(keep or record.get("keep"))
            intent_partial = partial or intent.get("partial") is True
            raw_intent_rem = intent.get("remaining")
            intent_remaining = remaining if remaining is not None else (
                raw_intent_rem if isinstance(raw_intent_rem, str) and raw_intent_rem else None
            )
            return _finish_land_transition(
                store, lane_id, merge_sha=merge_sha, previous=previous, tip=tip, tree=tree,
                integration=integration, onto=onto, keep=keep_flag, new_overrides=new_overrides, receipt=receipt,
                partial=intent_partial, remaining=intent_remaining,
            )
        problems: list[str] = []
        problem = merge_problem(lane_id, tip, tree, receipt, record=record, prog=prog)
        if problem:
            problems.append(problem)
        pre_land_problem = review.pre_land_review_problem(store, record, main, tip, tree, integration, prog=prog)
        if pre_land_problem:
            problems.append(pre_land_problem)
        int_head = git(integration, "rev-parse", "HEAD")
        if repair and git_ok(integration, "merge-base", "--is-ancestor", tip, int_head):
            repaired_merge = _find_merge_commit(integration, tip)
            if repaired_merge:
                lane_stats = review_class.diff_stats(integration, repaired_merge[1], repaired_merge[0])
            else:
                diff_base = git(main, "merge-base", int_head, tip, check=False) or int_head
                lane_stats = review_class.diff_stats(main, diff_base, tip)
        else:
            diff_base = git(main, "merge-base", int_head, tip, check=False) or int_head
            lane_stats = review_class.diff_stats(main, diff_base, tip)
        is_fix_lane = bool(list(review.valid_fix_parents(record, store)))
        class_problems = review_class.land_problems(
            record, lane_stats, store.project, root=main, is_fix_lane=is_fix_lane, receipt=receipt,
        )
        problems.extend(class_problems)
        if problems and not force_reason:
            raise LaneError("land refused: " + "; ".join(problems))
        if problems:
            new_overrides.append(journal.override("land", record, force_reason, problems, store=store))
        if dirty(integration) or os.path.exists(git(integration, "rev-parse", "--git-path", "MERGE_HEAD")):
            raise LaneError(f"land refused: {integration} has uncommitted changes or a merge in progress")
        if onto in store.project.promoted:
            reviewed, words = review_record.ReviewRecord.load(store, lane_id, record=record).summary(prog=prog)
            if pre_land_problem is not None:
                reviewed = False
                words = pre_land_problem
            eff_cls_onto, _ = review_class.effective_class(
                record, lane_stats, store.project, root=main, is_fix_lane=is_fix_lane, receipt=receipt,
            )
            class_exempt = eff_cls_onto in ("none", "evidence") and not class_problems
            if not reviewed and not class_exempt and not review.review_exemption(record, store):
                if not force_reason:
                    raise LaneError(f"land refused: {integration} is on {onto}, which promote would not hold for "
                                    f"this lane's review ({words}); run {prog} review {lane_id} and mark its "
                                    "findings first, or land into the integration branch")
                new_overrides.append(journal.override(
                    "land", record, force_reason, [f"unreviewed land onto {onto}"],
                    qualifier=f"onto {onto} unreviewed", described=words, details={"review": words}, store=store))
            else:
                journal.ledger(f"land {lane_id} onto {onto}: {words}", kind="landing.reviewed", lane=record,
                               details={"onto": onto, "review": words}, store=store)
        previous = git(integration, "rev-parse", "HEAD")
        if git_ok(integration, "merge-base", "--is-ancestor", tip, previous):
            if not repair:
                raise LaneError(f"land refused: {record['branch']} ({tip[:10]}) is already in {integration}; "
                                f"if a previous land merged it before saving, run {prog} land {lane_id} --repair")
            found = _find_merge_commit(integration, tip)
            if not found:
                raise LaneError(f"land --repair refused: no merge commit for {record['branch']} ({tip[:10]}) "
                                f"found in {integration}")
            merge_sha, prev_sha = found
            merge_body = git(integration, "log", "-1", "--format=%B", merge_sha, check=False)
            repo = tickets.repository(record["common_dir"], store.project.tickets) or ""
            repair_partial = tickets.check_repair_partial(
                merge_sha, merge_body, scope.issues(record), store.project.tickets, repo, partial
            )
            keep_flag = bool(keep or record.get("keep"))
            return _finish_land_transition(
                store, lane_id, merge_sha=merge_sha, previous=prev_sha, tip=tip, tree=tree,
                integration=integration, onto=onto, keep=keep_flag, new_overrides=new_overrides, receipt=receipt,
                partial=repair_partial, remaining=remaining if repair_partial else None,
            )
        issues = scope.issues(record)
        effective_issues = [scope.Issue(issue.number, True) for issue in issues] if partial else issues
        ticket = f" ({', '.join(issue.ref for issue in effective_issues)})" if effective_issues else ""
        repo = tickets.repository(record["common_dir"], store.project.tickets) or ""
        merge_msg = tickets.merge_message(
            message or f"Merge {record['branch']}{ticket}", effective_issues, store.project.tickets, repo=repo
        )
        tickets.warn_trailer_conflicts(
            store, record, integration, previous, tip, message or "", issues=effective_issues, repo=repo
        )
        merge_config = [part for setting in store.project.merge_config for part in ("-c", setting)]
        conflicts = ""
        with landing(record["common_dir"], f"{prog} land {lane_id} (pid {os.getpid()})", wait, store=store) as env:
            review_override = check_other_reviews(store, record, force_reason)
            if review_override is not None:
                new_overrides.append(review_override)
            landing_intent: dict[str, Any] = {
                "tip": tip, "integration": integration, "previous": previous, "at": journal.now(),
            }
            if partial:
                landing_intent["partial"] = True
                if remaining:
                    landing_intent["remaining"] = remaining
            store.update(lane_id, lambda rec: rec.__setitem__("landing", landing_intent))
            merged = subprocess.run(["git", *merge_config, "merge", "--no-ff", "-m", merge_msg, record["branch"]],
                                    cwd=integration, capture_output=True, text=True, env=env)
            if merged.returncode:
                conflicts = git(integration, "-c", "core.quotePath=false", "diff", "--name-only", "--diff-filter=U",
                                check=False)
                subprocess.run(["git", "merge", "--abort"], cwd=integration, capture_output=True, env=env)
        if merged.returncode:
            journal.ledger(f"land {lane_id} conflict: {conflicts.replace(chr(10), ' ')}", kind="lane.conflict",
                           level="error", lane=record,
                           details={"paths": conflicts.split(), "integration": integration, "previous": previous},
                           store=store)
            raise LaneError(f"merge conflict, integration left at {previous[:10]}; send it back to the lane:\n"
                            f"{conflicts or merged.stdout + merged.stderr}")
        merge_sha = git(integration, "rev-parse", "HEAD")
        keep_flag = bool(keep or record.get("keep"))
        return _finish_land_transition(
            store, lane_id, merge_sha=merge_sha, previous=previous, tip=tip, tree=tree,
            integration=integration, onto=onto, keep=keep_flag, new_overrides=new_overrides, receipt=receipt,
            pre_land_overridden=bool(pre_land_problem), partial=partial, remaining=remaining,
        )


def post_merge(store: state.Store, landed: Landed, slots: int | None) -> Smoke:
    """Phase 2 of land: run the post-merge smoke (with review started beside it), write the receipt and ledger."""
    record = landed.record
    lane_id = record["id"]
    merge_sha = landed.merge
    previous = landed.previous
    receipt = landed.receipt
    smoke_recorded = False
    try:
        with worktrees.smoke_checkout(record["common_dir"], merge_sha, store.project) as checkout:
            postmerge.PostMergeRecord.start_smoke(store, lane_id, merge_sha=merge_sha, checkout=checkout)
            inspector = plan.Checkout(store.project, checkout)
            merge_tree = git(checkout, "rev-parse", "HEAD^{tree}")
            toolchain = inspector.toolchain()
            exact_verified, exact_narrowing, reasons, prep_reason = plan.exact_tree_reuse(
                inspector, receipt, record["landed"], merge_tree, previous, toolchain, record.get("worktree")
            )
            if exact_verified is not None and exact_narrowing is not None:
                merged_stats = review_class.diff_stats(checkout, previous, merge_sha)
                docs_only = review.documentation_only(list(merged_stats.paths), store.project)
                eff_cls, eff_why = review_class.effective_class(
                    record, merged_stats, store.project, root=checkout,
                    is_fix_lane=bool(list(review.valid_fix_parents(record, store))),
                    receipt=receipt,
                )
                record = store.update(
                    lane_id,
                    lambda rec: rec["landed"].update(
                        docs_only=docs_only, review_class=eff_cls, review_class_reason=eff_why,
                    ),
                )
                print(
                    f"lane: {review.start_lane_review(store, record, force_fresh=landed.pre_land_overridden)}",
                    flush=True,
                )
                raw_exact_dropped: Any = exact_narrowing.get("dropped_suites")
                exact_dropped_suites: list[Any] = (
                    cast(list[Any], raw_exact_dropped) if isinstance(raw_exact_dropped, list) else []
                )
                is_whole_suite_skip = "dropped_suites" in exact_narrowing and not exact_verified
                verified_desc = (
                    f"{len(exact_dropped_suites)} suite{'s' if len(exact_dropped_suites) != 1 else ''}"
                    if is_whole_suite_skip
                    else f"{len(exact_verified)} checks"
                )
                print(f"lane: post-merge smoke skipped: exact merge tree {merge_tree[:10]} already verified by lane "
                      f"receipt ({verified_desc}); no plan bootstrap needed", flush=True)
                kind = "skip"
                rows: list[dict[str, Any]] | None = None if is_whole_suite_skip else []
                lane_verified, narrowing = exact_verified, exact_narrowing
                tree_verified: dict[str, dict[str, Any]] = {}
                skips: dict[str, str] = {}
                suites: list[str] = []
                selected: dict[str, list[str]] | None = None if is_whole_suite_skip else {}
                decision, results, check_results, triaged, stamp, status = run_smoke(
                    store, record, checkout, suites, selected, f"post-merge-{lane_id}", slots, toolchain
                )
                jobs = decision["jobs"]
            else:
                print(f"lane: preparing post-merge plan for {lane_id} in {checkout} ({prep_reason}); "
                      "queue: slotfather status", flush=True)
                merged_plan = inspector.plan_for(previous)
                merged_stats = review_class.diff_stats(checkout, previous, merge_sha)
                plan_docs_only = review.documentation_only(merged_plan["paths"], store.project)
                eff_cls, eff_why = review_class.effective_class(
                    record, merged_stats, store.project, root=checkout,
                    is_fix_lane=bool(list(review.valid_fix_parents(record, store))),
                    receipt=receipt,
                )
                record = store.update(
                    lane_id,
                    lambda rec: rec["landed"].update(
                        docs_only=plan_docs_only, review_class=eff_cls, review_class_reason=eff_why,
                    ),
                )
                print(
                    f"lane: {review.start_lane_review(store, record, force_fresh=landed.pre_land_overridden)}",
                    flush=True,
                )
                kind, rows = inspector.post_merge_selection(merged_plan)
                if rows is None and kind == "plan":
                    kind, suites, narrowing = plan.narrow_suites(
                        inspector, receipt, record["landed"], merge_tree, kind, list(merged_plan["selected"]),
                        record.get("worktree"), toolchain=toolchain,
                    )
                    lane_verified = {}
                    tree_verified = {}
                    skips = {}
                    selected = None
                else:
                    kind, rows, narrowing, lane_verified = plan.narrow_smoke(
                        inspector, receipt, record["landed"], merge_tree, kind, rows, record.get("worktree"),
                        toolchain=toolchain,
                    )
                    kind, rows, tree_verified, skips = smoke_reuse(
                        store, kind, rows, merge_tree, toolchain, merged_plan,
                        git(checkout, "rev-parse", previous + "^{tree}"),
                    )
                    suites, selected = ((merged_plan["selected"], None) if rows is None
                                        else (plan.suites_of(rows), plan.by_suite(rows)))
                reasons = merged_plan["reasons"]
                left = (f"{len(rows)} check{'s' if len(rows) != 1 else ''} to run" if rows
                        else f"suites {', '.join(suites)} to run" if rows is None and suites else "nothing to run")
                print(f"lane: post-merge plan for {lane_id} prepared ({kind}): {left}", flush=True)
                decision, results, check_results, triaged, stamp, status = run_smoke(
                    store, record, checkout, suites, selected, f"post-merge-{lane_id}", slots, toolchain
                )
                jobs = decision["jobs"]
        info = host(store.project)
        payload: dict[str, Any] = {
            "version": 1, "lane": lane_id, "merge": merge_sha, "base": previous,
            "tree": merge_tree, "checkout": checkout, "kind": kind,
            "platform": result_store.host_platform(store.project),
            "jobs": jobs, "jobs_decision": decision, "suites": suites, "full": False,
            "reasons": reasons, "checks": selected, "selection": rows,
            "narrowing": narrowing, "skipped": skips,
            "check_results": {**lane_verified, **tree_verified, **check_results},
            "results": results, "triage": triaged, "status": status, "verified_tree": stamp,
            "leases": verify.leases_of(results),
            "host": info, "finished": journal.now(),
        }
        pm, post = postmerge.PostMergeRecord.record_smoke(
            store, lane_id, merge_sha=merge_sha, status=status, payload=payload,
        )
        smoke_recorded = True
    finally:
        if not smoke_recorded:
            postmerge.PostMergeRecord.abort_smoke(store, lane_id, merge_sha=merge_sha)
    record = pm.record
    result_store.record(store, merge_tree, verify.recordable({**lane_verified, **tree_verified, **check_results}),
                        source="post-merge", toolchain=toolchain, lane=lane_id, sha=merge_sha, receipt=str(post),
                        reused=[*lane_verified, *tree_verified],
                        origins={**verify.origins(lane_verified, str(receipts.path(store, lane_id))),
                                 **verify.origins(tree_verified, "")}, phase="post-merge")
    raw_dropped_suites: Any = narrowing.get("dropped_suites")
    dropped_suites: list[Any] = cast(list[Any], raw_dropped_suites) if isinstance(raw_dropped_suites, list) else []
    is_whole_suite_narrowed = "dropped_suites" in narrowing and not lane_verified
    verified_checks = len(lane_verified)
    verified_suites = len(dropped_suites)
    verified = verified_suites if is_whole_suite_narrowed else verified_checks
    verified_tag = (
        f"lane_verified_suites={verified_suites} "
        if is_whole_suite_narrowed
        else f"lane_verified={verified_checks} "
    )
    journal.ledger(f"post-merge {lane_id} {status} {kind} jobs={jobs} suites={','.join(suites) or '-'} "
                   + (verified_tag if narrowing.get("narrowed") else "")
                   + (f"not_rerun={len(tree_verified)} " if tree_verified else "")
                   + (f"gate_only={len(skips) - len(tree_verified)} " if len(skips) > len(tree_verified) else "")
                   + f"{host_fields(info)} merge={merge_sha}",
                   kind="lane.post_merge", level="info" if status == "green" else "error", lane=record,
                   details={"status": status, "scope": kind, "jobs": jobs, "suites": suites,
                            "lane_verified": verified_checks,
                            **({"lane_verified_suites": verified_suites} if is_whole_suite_narrowed else {}),
                            "skipped": skips, "merge": merge_sha, "receipt": str(post)},
                   store=store)
    ids = [c for s in suites for c in selected[s]] if selected is not None else None
    what = (", ".join(ids) if ids is not None and len(ids) <= 8 else f"{len(ids)} checks" if ids is not None
            else ", ".join(suites)) or "no suites affected"
    prefix = {"smoke": "smoke: ", "narrowed": "narrowed plan: " if rows is None else "narrowed smoke: "}.get(kind, "")
    if kind == "skip" and skips:
        what = "skipped: " + "; ".join(f"{c}: {why}" for c, why in skips.items())
        if narrowing.get("narrowed"):
            what += f"; the lane's green receipt stands for {verified}"
    elif kind == "skip" and merge_tree == narrowing.get("receipt_tree"):
        what = f"skipped: the lane's green receipt verified the merge tree {merge_tree[:10]}"
    elif kind == "skip":
        brought = ", ".join(narrowing["integration_paths"][:3]) + (", ..." if len(narrowing["integration_paths"]) > 3
                                                                    else "")
        target_word = "planned suite" if rows is None else "smoke check"
        what = (f"skipped: what the integration brought in ({brought}) selects no {target_word}; the lane's green "
                f"receipt stands for {verified}")
    elif narrowing.get("narrowed") and verified:
        what += f"; the lane's receipt stands for {verified} more"
    elif narrowing.get("reason") and (rows or suites):
        what += f"; whole, since {narrowing['reason']}"
    if skips and kind != "skip":
        what += f"; {len(skips)} not run again ({', '.join(f'{c}: {why}' for c, why in skips.items())})"
    onto_label = current_branch(landed.integration) or landed.integration
    print(f"Landed {record['branch']} as {merge_sha[:10]} on {onto_label}; "
          f"post-merge {status} ({prefix}{what})")
    return Smoke(record=record, status=status, summary=prefix + what, results=results, triaged=triaged)


def after_land(store: state.Store, landed: Landed, smoke: Smoke) -> int:
    """Phase 3 of land: ticket comments, auto-done via `done.after_smoke`, and red-smoke failure report."""
    receipt = landed.receipt
    if receipt and receipt.get("changed_red_tests"):
        print(receipts.changed_red_tests_line(receipt["changed_red_tests"]))
    done.after_smoke(
        store,
        smoke.record,
        landed.merge,
        landed.integration,
        landed.onto or landed.integration,
        receipt,
        smoke.status,
        smoke.summary,
        keep_override=landed.keep,
    )
    if smoke.status == "green":
        return 0
    failed = _print_smoke_failures(smoke.results, smoke.triaged)
    head = git(landed.integration, "rev-parse", "HEAD")
    if head != landed.merge:
        print(f"{landed.integration} has moved on to {head[:10]} since; this smoke checked the merge "
              f"{landed.merge[:10]}.")
    print(f"The merge {landed.merge[:10]} stays in place, marked red. Lanes in it whose checks ran the failed checks:")
    print("\n".join(touched_lines(failed_items(failed), landed.integration, landed.merge, store=store)))
    return 1


def land(args: Any, store: state.Store) -> int:
    """Land a green lane in three phases: merge, post-merge smoke, and after-land."""
    partial = getattr(args, "partial", False) is True
    remaining_raw = getattr(args, "remaining", None)
    remaining: str | None = None
    if isinstance(remaining_raw, str):
        if not partial:
            raise LaneUsageError("--remaining goes with --partial")
        remaining = remaining_raw.strip()
        if not remaining:
            raise LaneUsageError("--remaining must be non-empty")
    landed = merge(
        store, args.id, args.into, args.message, bool(args.keep), args.force_reason, args.wait,
        repair=getattr(args, "repair", False) is True, partial=partial, remaining=remaining,
    )
    smoke = post_merge(store, landed, args.slots)
    return after_land(store, landed, smoke)
