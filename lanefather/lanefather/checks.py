"""Verify runs and the lane check's receipt.

check runs the checks the tree's plan selects for the lane's diff and writes the receipt for the committed tree,
reusing each check that passed for an earlier tree of the lane and that no change since selects. Each red check
reruns by itself on the lane at the lane check's own lease size (lanefather#4), then, red again, on its base, and is
classified (#568): lane red again and base green, `regression`; equivalent comparable base failure, `pre-existing`;
lane green on its rerun, `flaky-under-load`. Only the last two leave the receipt landable. A red cheap check stops
the run before any heavier check starts and reruns on the base only; the heavier checks run when it is pre-existing
there (#639).
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, cast

from . import (
    claims,
    events,
    history,
    host,
    journal,
    lanes,
    lifecycle,
    plan,
    queue,
    receipts,
    scope,
    state,
    verify,
)
from . import results as result_store
from .errors import LaneError
from .gitops import current_branch, dirty, git, git_ok
from .state import Record

Rows = list[dict[str, Any]]


def integration_refs(store: state.Store, record: Record, worktree: str) -> list[str]:
    """The branches the lane lands into, whose merge-base may be newer than its base: the branch checked out in
    the worktree it lands (or landed) into. Only a lane registered at a literal commit, whose branch is unknown
    until it lands, also takes the one its parent landed into for a fix lane and the ranked queue's base. A lane
    based on a branch lands there: another branch it merged (a dev-based lane merging the queue's integration
    branch) never stands in for it, or that branch's commits would land unchecked (review of lanefather#3)."""
    def landed_into(lane: dict[str, Any]) -> Any:
        landed: dict[str, Any] = lane.get("landed") or {}
        return landed.get("integration")

    places: list[Any] = [record.get("integration"), landed_into(record)]
    literal = not git_ok(worktree, "show-ref", "--verify", "--quiet", f"refs/heads/{record['base']}")
    if literal and lifecycle.is_fix_lane(record):
        places += [landed_into(state.read_json(store.record_path(parent)) or {})
                   for parent in state.fix_parents(record) if state.ID.match(parent)]
    refs = [branch for path in places if isinstance(path, str) and os.path.isdir(path)
            for branch in [current_branch(path)] if branch]
    if literal:
        try:
            q_settings = queue.read_settings(store)
        except LaneError as error:
            print(f"lane: warning: the queue's base is unknown, so check plans without it: {error}")
            q_settings = {}
        if isinstance(q_settings.get("base"), str) and q_settings["base"]:
            refs.append(q_settings["base"])
    return list(dict.fromkeys(refs))


def fix_parent(store: state.Store, record: Record) -> dict[str, Any] | None:
    """The original lane's per-check receipt for a hand-over. A lane from start --fix-of runs its own plan."""
    if not lifecycle.is_fix_lane(record):
        return None
    parent = state.hand_over_parent(record)
    if parent is None:
        return None
    parent_record = state.read_json(store.record_path(parent))
    receipt = receipts.read(store, parent, record=parent_record)
    if not receipt or receipt.get("selection") is None or receipt.get("check_results") is None:
        return None
    return dict(receipt, lane=parent)


def plan_base(store: state.Store, record: Record, worktree: str) -> str:
    """The newest merge-base of the lane's HEAD (lanefather#3): with its base, or with an integration branch tip
    when that merge-base descends from the former."""
    base_ref = git(worktree, "rev-parse", "--verify", "--quiet", record["base"] + "^{commit}", check=False)
    base = git(worktree, "merge-base", "HEAD", base_ref or record["base_sha"])
    for ref in integration_refs(store, record, worktree):
        tip = git(worktree, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
        newer = git(worktree, "merge-base", "HEAD", tip, check=False) if tip else ""
        if newer and newer != base and git_ok(worktree, "merge-base", "--is-ancestor", base, newer):
            base = newer
    return base


def _record_check_agent(store: state.Store, lane_id: str, agent_info: dict[str, Any] | None) -> None:
    if not agent_info:
        return
    store.update(lane_id, lambda current: state.record_agent(current, "check", agent_info, now=journal.now()))


@dataclass(frozen=True)
class Prepared:
    record: Record
    worktree: str
    head: str
    tree: str
    receipt_path: Path
    previous: Record | None
    agent_info: dict[str, Any] | None


@dataclass(frozen=True)
class Selected:
    toolchain: Any
    platform: str
    base: str
    lane_plan: dict[str, Any]
    fix_of: dict[str, Any] | None
    suites: list[str]
    selected: dict[str, list[str]] | None
    reused: dict[str, dict[str, Any]]
    remaining: dict[str, list[str]] | None
    outside: list[str]
    added: list[str]
    overlaps: list[dict[str, Any]]
    decision: dict[str, Any]


@dataclass(frozen=True)
class CheckRun:
    started: str
    fresh: Rows
    ran: dict[str, dict[str, Any]]
    triage: str | None
    classification: dict[str, Any] | None
    stopped: Any
    granted_jobs: int | None = None


def prepare(store: state.Store, lane_id: str, *, agent: str | None = None) -> Prepared:
    """Adopt a planned lane's worktree when run there, refuse non-active/wrong-branch/dirty worktrees."""
    prog = store.prog
    agent_info = events.agent_context(agent)
    record = store.load(lane_id)
    if lifecycle.planned(record):
        cwd = git(os.getcwd(), "rev-parse", "--show-toplevel", check=False)
        if not cwd or current_branch(cwd) != record["branch"]:
            raise LaneError(f"lane {lane_id} has no worktree yet: run it from a worktree on {record['branch']}, "
                            f"or register one with {prog} adopt")
        lanes.adopt_worktree(store, record, cwd, record["branch"], agent=agent)
    if not lifecycle.active(record):
        raise LaneError(f"lane {lane_id} is {record['status']}; check runs before landing")
    worktree = record["worktree"]
    if current_branch(worktree) != record["branch"]:
        raise LaneError(f"{worktree} is on {current_branch(worktree)}, not the lane branch {record['branch']}")
    if dirty(worktree):
        raise LaneError("commit your work (or set it aside in a WIP commit) first: the receipt covers a committed "
                        "tree\n" + dirty(worktree))
    head, tree = git(worktree, "rev-parse", "HEAD"), git(worktree, "rev-parse", "HEAD^{tree}")
    path = receipts.path(store, lane_id)
    previous = receipts.read(store, lane_id, record=record)
    return Prepared(record=record, worktree=worktree, head=head, tree=tree, receipt_path=path,
                    previous=previous, agent_info=agent_info)


def select(
    store: state.Store,
    prepared: Prepared,
    *,
    rerun: bool = False,
    slots_override: int | None = None,
) -> Selected | None:
    """Resolve toolchain/platform, short-circuit a current green receipt, plan, reuse passes, widen claims and size
    jobs. Returns None when the existing green receipt already certifies the tree."""
    record, worktree, tree = prepared.record, prepared.worktree, prepared.tree
    previous, path = prepared.previous, prepared.receipt_path
    inspector = plan.Checkout(store.project, worktree)
    toolchain = inspector.toolchain()
    platform = result_store.host_platform(store.project)
    verdict = receipts.check_verdict(record, previous, tree, toolchain, platform=platform, project=store.project)
    if not rerun:
        if isinstance(verdict, receipts.Green):
            _record_check_agent(store, record["id"], prepared.agent_info)
            print(f"Receipt current: green for tree {tree} on {platform} ({path})")
            return None
        if isinstance(verdict, receipts.StaleToolchain):
            print(f"lane: the {store.project.toolchain_name} changed since the receipt (#571); "
                  "running the checks again"
                  if verdict.toolchain_changed else
                  f"lane: the receipt was not made on {platform}; running the checks again")
    base = plan_base(store, record, worktree)
    lane_plan = inspector.plan_for(base)
    parent = fix_parent(store, record)
    fix_of = plan.fix_plan(lane_plan, parent, inspector.known_checks()) if parent else None
    if fix_of:
        outside_list = ", ".join(fix_of["outside_parent"][:3]) + (", ..." if len(fix_of["outside_parent"]) > 3
                                                                  else "")
        print(f"lane: review-fix lane of {fix_of['lane']}: " + (
            f"the plan of both diffs, since the fix changes {outside_list} outside {fix_of['lane']}'s diff"
            if fix_of["union"] else "the plan of its fix, inside the parent's diff" if fix_of["union"] is False
            else f"its own plan: the fix changes {outside_list} outside the parent's diff, but this tree's plan "
                 "names whole suites"), flush=True)
    suites: list[str] = lane_plan["selected"]
    selected: dict[str, list[str]] | None = lane_plan["by_suite"]
    reused: dict[str, dict[str, Any]] = {}
    if selected is not None and not rerun:
        wanted = {c for ids in selected.values() for c in ids}
        reused = plan.reuse_from_receipt(
            inspector, previous, wanted, toolchain=toolchain, platform=platform, worktree=worktree
        )
        if parent:
            parent_receipt = str(receipts.path(store, parent["lane"]))
            reused.update({
                c: e
                for c, e in plan.reuse_from_receipt(
                    inspector, parent, wanted - set(reused), toolchain=toolchain, platform=platform,
                    worktree=worktree, reused_by="parent", lane=parent["lane"], receipt_path=parent_receipt,
                ).items()
                if c not in reused
            })
        reused.update(
            plan.reuse_from_tree(
                store, {c: s for s, ids in selected.items() for c in ids if c not in reused}, tree, toolchain,
                platform=platform,
            )
        )
    where = claims.project_root(store.project)
    outside = [p for p in lane_plan["paths"] if not claims.unclaimed(p, store.project)
               and not any(claims.matches(c, p, root=where) for c in record["claims"])]
    added, overlaps = claims.widen_claims(store, record["id"], outside)
    if added:
        print(f"lane: claims widened: {', '.join(added)}")
    for overlap in overlaps:
        print(f"lane: warning: {overlap['path']} overlaps the claims of lane {overlap['lane']} "
              f"({scope.display_ticket(overlap['ticket'])}): "
              f"{overlap['claim']}; tell the orchestrator, the two lanes may conflict when they land")
    for unowned in lane_plan.get("unowned", []):
        print(f"lane: no check reads {unowned}, so the plan is the full gate: declare its readers in "
              f"{store.project.plan_module or 'the plan module'}")
    remaining = None if selected is None else {s: [c for c in ids if c not in reused] for s, ids in selected.items()}
    if slots_override:
        decision = host.given_jobs(slots_override, "--slots")
    elif remaining is None or any(remaining.values()):
        min_slots, min_for = plan.required_min_slots(lane_plan, remaining, suites, project=store.project)
        decision = host.check_jobs(host.host(store.project), minimum=min_slots, min_for=min_for)
    else:
        decision = {"jobs": 1, "reason": "nothing to run"}
    return Selected(toolchain=toolchain, platform=platform, base=base, lane_plan=lane_plan, fix_of=fix_of,
                    suites=suites, selected=selected, reused=reused, remaining=remaining, outside=outside,
                    added=added, overlaps=overlaps, decision=decision)


def run(store: state.Store, prepared: Prepared, selection: Selected) -> CheckRun:
    """Execute the remaining checks through `verify.Run` and classify any red checks."""
    started = journal.now()
    fresh, ran, triaged, classification, stopped, granted_jobs = _execute_lane_run(
        store, prepared.record, prepared.worktree, selection.base, selection.suites, selection.remaining,
        selection.decision["jobs"], selection.toolchain, lane_plan=selection.lane_plan,
        min_jobs=selection.decision.get("min_jobs"))
    return CheckRun(started=started, fresh=fresh, ran=ran, triage=triaged,
                    classification=classification, stopped=stopped, granted_jobs=granted_jobs)


def record(store: state.Store, prepared: Prepared, selection: Selected, executed: CheckRun) -> int:
    """Write the receipt, update the results store, history and ledger, print the summary, and return exit code."""
    prog = store.prog
    rec = prepared.record
    lane_id, head, tree, path = rec["id"], prepared.head, prepared.tree, prepared.receipt_path
    suites, selected, reused = selection.suites, selection.selected, selection.reused
    decision, lane_plan = selection.decision, selection.lane_plan
    effective_jobs = executed.granted_jobs if executed.granted_jobs is not None else decision["jobs"]
    recorded_decision = (
        {**decision, "jobs": effective_jobs, "max_jobs": decision["jobs"], "granted": executed.granted_jobs}
        if executed.granted_jobs is not None else decision
    )
    fresh, ran, triaged = executed.fresh, executed.ran, executed.triage
    classification, stopped = executed.classification, executed.stopped
    results = verify.combined_results(suites, selected, fresh, reused)
    check_results = {} if selected is None else {
        c: reused.get(c) or ran.get(c) or {"suite": s, "status": "not_run"} for s, ids in selected.items() for c in ids}
    raw_status = "green" if all(r["status"] == "passed" for r in results) else "red"
    status = raw_status
    first = receipts.ensure_first(store, rec, prepared.previous, head, tree, verify.red_checks(results))
    if status == "red" and classification and classification["green"] and not stopped:
        status = "green"
    changed_tests = receipts.changed_red_tests(lane_plan, first)
    info = host.host(store.project)
    agent_kind = prepared.agent_info["kind"] if prepared.agent_info else None
    lane_created = state.lane_created_of(rec)
    receipts.write(
        store, lane_id,
        {"version": 1, "lane": lane_id,
          **({"lane_created": lane_created} if lane_created else {}),
          "ticket": rec["ticket"], "branch": rec["branch"],
          "head": head, "tree": tree, "base": selection.base, "suites": suites,
          "reasons": lane_plan["reasons"], "checks": selected, "selection": lane_plan["selection"],
          "check_results": check_results, "unowned": lane_plan.get("unowned", []),
          "full": lane_plan["full"], "outside_claims": selection.outside,
          "claims_added": selection.added, "claim_overlaps": selection.overlaps,
          "toolchain": selection.toolchain, "platform": selection.platform,
          "jobs": effective_jobs, "jobs_decision": recorded_decision, "results": results,
          "triage": triaged, "classification": classification, "first": first,
          "changed_red_tests": changed_tests, "stopped": stopped, "raw_status": raw_status,
          "status": status, "host": info,
          "started": executed.started, "paths": lane_plan["paths"],
          **({"fix_of": selection.fix_of} if selection.fix_of else {}),
          "leases": verify.leases_of(fresh, classification),
          **({"agent": agent_kind} if agent_kind else {}),
          "finished": journal.now()},
    )
    _record_check_agent(store, lane_id, prepared.agent_info)
    result_store.record(store, tree, verify.recordable(check_results), source="check", toolchain=selection.toolchain,
                        platform=selection.platform, lane=lane_id, sha=head, receipt=str(path),
                        reused=list(reused), origins=verify.origins(reused, str(path)), phase="check")
    for entry in (classification or {}).get("checks", []):
        if entry.get("class") == verify.CheckClass.FLAKY_UNDER_LOAD:
            history.flake(store, entry["check"], lane=lane_id, tree=tree, failed_at=executed.started,
                          how="rerun-alone", junit=entry.get("junit"), phase="check")
    classes = ",".join(f"{e['check']}={e['class']}" for e in (classification or {}).get("checks", []))
    journal.ledger(f"check {lane_id} tree={tree} {status} suites={','.join(suites) or '-'} "
                   f"checks={len(check_results) - len(reused)} reused={len(reused)} jobs={effective_jobs} "
                   f"classified={classes or '-'} {host.host_fields(info)}",
                   kind="lane.checked", lane=rec, level="info" if status == "green" else "warn",
                   details={"status": status, "tree": tree, "suites": suites,
                            "checks": len(check_results) - len(reused), "reused": len(reused),
                            "jobs": effective_jobs, "receipt": str(path)},
                   store=store)
    for result in results:
        note = (" (passed on this tree before: " + str(result.get("log") or "an earlier run") + ")"
                if result.get("reused_from") == tree
                else f" (reused from tree {result['reused_from'][:10]})" if result.get("reused_from")
                else f" ({len(result['reused'])} of {len(result['checks'])} checks reused)" if result.get("reused")
                else "")
        failed = f" failed checks: {', '.join(result['failed_checks'])}" if result.get("failed_checks") else ""
        blocked = f" blocked checks: {', '.join(result['blocked_checks'])}" if result.get("blocked_checks") else ""
        print(f"  {result['suite']}: {result['status']}{note}{failed}{blocked}  {result['log']}")
    if triaged:
        print(triaged)
    if classification:
        print("\n".join(verify.classification_lines(classification)))
    if changed_tests:
        print(receipts.changed_red_tests_line(changed_tests))
    if stopped:
        print(f"lane: stopped at the first checks (#639): {', '.join(stopped['failed'])} red, so the "
              f"{stopped['not_run']} heavier checks did not start. Fix them, commit and run {prog} check again.")
    if raw_status == "red" and status == "green":
        print(f"Receipt {status} (raw verification RED, classified green) for tree {tree}: {path}")
    else:
        print(f"Receipt {status} for tree {tree}: {path}")
    return 0 if status == "green" else 1


def check(args: Any, store: state.Store) -> int:
    prepared = prepare(store, args.id, agent=getattr(args, "agent", None))
    selection = select(store, prepared, rerun=bool(args.rerun), slots_override=args.slots)
    if selection is None:
        return 0
    executed = run(store, prepared, selection)
    return record(store, prepared, selection, executed)


def _execute_lane_run(
    store: state.Store,
    record: Record,
    worktree: str,
    base: str,
    suites: list[str],
    remaining: dict[str, list[str]] | None,
    jobs: int,
    toolchain: Any = verify.UNRESOLVED,
    lane_plan: dict[str, Any] | None = None,
    min_jobs: int | None = None,
) -> tuple[Rows, dict[str, dict[str, Any]], str | None, dict[str, Any] | None, Any, int | None]:
    """Run the lane's checks that are left, classify the red ones (#568), and run any checks unblocked by a
    prerequisite passing its own rerun (fathers#240)."""
    def attempt(
        selection: dict[str, list[str]] | None,
        *,
        budget: int,
    ) -> tuple[Rows, dict[str, dict[str, Any]], str | None, dict[str, Any] | None, Any, int | None, set[str]]:
        active_suites = [s for s in suites if selection is None or selection.get(s)]
        if not active_suites:
            return [], {}, None, None, None, None, set()
        ids = None if selection is None else [c for s in active_suites for c in selection[s]]
        choice = verify.Selection.checks(ids) if ids is not None else verify.Selection.suites(active_suites)
        run_type = (
            verify.plan_meta(ids, worktree, toolchain=toolchain, project=store.project)
            if ids is not None
            else verify.plan_meta([f"suite:{s}" for s in active_suites], worktree,
                                  verify.suite_count(worktree, active_suites, store.project), toolchain,
                                  project=store.project)
        )
        what = ", ".join(active_suites) + (f" ({len(ids)} checks)" if ids is not None else "")
        outcome = verify.Run(
            worktree, choice, f"lane-{record['id']}", "check", jobs,
            store=store, meta=verify.lane_meta(store, record), ticket=verify.lane_ticket(record), run_type=run_type,
            min_jobs=min_jobs,
            banner=f"lane: running {what} in {worktree} (log {{log}}); queue: slotfather status",
        ).execute()
        fresh, ran = verify.suite_outcomes(worktree, active_suites, selection, outcome, project=store.project)
        triaged = outcome.triage
        classification, report = None, outcome.report
        raw_report_checks: Any = (report or {}).get("checks") or []
        passed_ids: set[str] = set()
        if not outcome.environment and isinstance(raw_report_checks, list):
            for raw_row in cast(list[Any], raw_report_checks):
                if not isinstance(raw_row, dict):
                    continue
                row = cast(dict[str, Any], raw_row)
                if (
                    row.get("id")
                    and row.get("status") in ("passed", "skipped")
                    and not verify.is_dependency_blocked(row)
                ):
                    passed_ids.add(str(row["id"]))
        if any(r["status"] != "passed" for r in fresh):
            run_dir = outcome.run_dir
            effective_jobs = outcome.granted_jobs if outcome.granted_jobs is not None else jobs
            with verify.terminable():
                classification = verify.classify_failures(
                    store, record, worktree, base, fresh, report, effective_jobs, toolchain,
                    lane_plan=lane_plan, rerun_budget=budget,
                )
            if classification and run_dir is not None:
                state.write_json(run_dir / "rerun.json", classification)
                triaged = plan.triage(worktree, run_dir, store.project) or triaged
        return fresh, ran, triaged, classification, outcome.stopped, outcome.granted_jobs, passed_ids

    def run_with_unblocking(
        initial_selection: dict[str, list[str]] | None,
    ) -> tuple[Rows, dict[str, dict[str, Any]], str | None, dict[str, Any] | None, Any, int | None]:
        rerun_budget = verify.rerun_cap(store.project)
        fresh, ran, triaged, classification, stopped, granted_jobs, _ = attempt(initial_selection, budget=rerun_budget)
        rerun_budget = max(0, rerun_budget - verify.reruns_used(classification))
        if stopped:
            return fresh, ran, triaged, classification, stopped, granted_jobs
        wave_fresh, wave_cls = fresh, classification
        while wave_cls is not None:
            unblocked_ids, resolved_prereqs = verify.unblocked_checks(wave_cls)
            if not unblocked_ids:
                break
            unblocked_set = set(unblocked_ids)
            if initial_selection is not None:
                next_selection = {s: [c for c in ids if c in unblocked_set] for s, ids in initial_selection.items()}
            else:
                next_selection = {
                    r["suite"]: [c for c in r.get("blocked_checks", []) if c in unblocked_set]
                    for r in wave_fresh
                }
            ran_ids = [c for s in suites if next_selection.get(s) for c in next_selection[s]]
            if not ran_ids:
                break
            print(
                f"lane: {', '.join(resolved_prereqs)} passed rerun by itself; running {len(ran_ids)} "
                f"unblocked check(s): {', '.join(ran_ids)}",
                flush=True,
            )
            more, ran_more, triaged_more, classified_more, stopped_more, more_jobs, more_passed = attempt(
                next_selection, budget=rerun_budget,
            )
            rerun_budget = max(0, rerun_budget - verify.reruns_used(classified_more))
            resolved_ids = [*ran_ids, *(c for c in unblocked_ids if c in more_passed and c not in ran_ids)]
            fresh = verify.joined_results(fresh, more, attempted=resolved_ids)
            ran = {**ran, **ran_more}
            triaged = "\n".join(t for t in (triaged, triaged_more) if t) or None
            assert classification is not None
            classification = verify.joined_classification(classification, classified_more, resolved=resolved_ids)
            stopped = stopped_more
            if more_jobs is not None:
                granted_jobs = max(granted_jobs, more_jobs) if granted_jobs is not None else more_jobs
            wave_fresh, wave_cls = more, classified_more
            if stopped:
                break
        return fresh, ran, triaged, classification, stopped, granted_jobs

    fresh, ran, triaged, classification, stopped, granted_jobs = run_with_unblocking(remaining)
    if not (stopped and classification and classification["green"] and remaining is not None):
        return fresh, ran, triaged, classification, stopped, granted_jobs
    rest = {s: [c for c in ids if (ran.get(c) or {}).get("status") == "not_run"] for s, ids in remaining.items()}
    if not any(rest.values()):
        return fresh, ran, triaged, classification, None, granted_jobs
    print("lane: every red first check is red on the base too (pre-existing); running the heavier checks", flush=True)
    more, ran_more, triaged_more, classified_more, stopped, more_jobs = run_with_unblocking(rest)
    return (verify.joined_results(fresh, more), {**ran, **ran_more},
            "\n".join(t for t in (triaged, triaged_more) if t) or None,
            verify.joined_classification(classification, classified_more), stopped,
            more_jobs if more_jobs is not None else granted_jobs)
