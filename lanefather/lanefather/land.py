"""land: merge --no-ff after a current green receipt, start the lane review in the background, run the post-merge
smoke, and after a green one finish the lane as done does once its review is clean or marked.

land holds its lock only for the merge, so another land waits for that alone (#573). After the merge, land runs the
smoke the merged tree's plan module defines on its own checkout detached at the merge, the first free
<main checkout>/.claude/worktrees/smoke-<n>, so smokes of several lands overlap while the integration worktree moves
on; a merge whose plan selects only the structure checks runs that plan instead. The lane's green receipt stands for
what it verified (#640): a merge whose tree is the receipt's runs nothing more, and otherwise only the checks the
diff from the lane tip to the merge selects, plus every smoke check the receipt does not show passed.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

from . import checks, config, done, journal, plan, review, state, tickets, worktrees
from . import results as result_store
from .errors import LaneError
from .gitops import current_branch, dirty, git, git_ok, main_checkout
from .host import given_jobs, host, host_fields, post_merge_jobs
from .landing import landing

# The branches only promote moves; promote's review gate covers lanes that are not in dev yet (#570).
PROMOTED = ("dev", "main")


def failed_items(failed: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Each failed check of the failed results with its suite, once each; a suite whose failure no check accounts
    for stands as its own item."""
    items = [(check, result["suite"]) for result in failed for check in result.get("failed_checks", [])]
    return list(dict.fromkeys(items)) or [(result["suite"], result["suite"]) for result in failed]


def touched_lines(failed: list[tuple[str, str]], integration: str, merge: str) -> list[str]:
    """The lanes in the merge, newest first, whose checks ran each failed (check, suite) item: the check itself, or
    its whole suite (review of #422, finding 1); for a suite item, any check of the suite. A lane that landed after
    the merge, while its smoke ran (#573), is not in it."""
    landed = [r for r in sorted(state.lanes(), key=lambda r: r.get("landed", {}).get("at", ""), reverse=True)
              if r.get("landed", {}).get("integration") == integration
              and git_ok(integration, "merge-base", "--is-ancestor", r["landed"]["merge"], merge)]
    ran = {r["id"]: checks.lane_ran(r["id"]) for r in landed}

    def covers(lane_id: str, item: str, suite: str) -> bool:
        lane_checks, suites, whole = ran[lane_id]
        return item in lane_checks or suite in whole or (item == suite and suite in suites)

    lines: list[str] = []
    for item, suite in failed:
        names = [f"{r['id']} (#{r['ticket']})" for r in landed if covers(r["id"], item, suite)]
        lines.append(f"  {item}: {', '.join(names) or 'no recorded lane'}")
    return lines


def smoke_reuse(kind: str, rows: list[dict[str, Any]] | None, merge_tree: str, toolchain: Any,
                ) -> tuple[str, list[dict[str, Any]] | None, dict[str, dict[str, Any]], dict[str, str]]:
    """The smoke rows left once each check that already passed on the merge tree is taken out (fathers#91): (kind,
    rows, the reused results, why each was skipped). Nothing left is kind "skip"."""
    if not rows:
        return kind, rows, {}, {}
    wanted = {row["id"]: row["suite"] for row in rows}
    reused = checks.same_tree(wanted, merge_tree, toolchain)
    skips = {c: "passed on this merge tree before (" + str(e.get("reused_source") or "an earlier run")
             + (f", {e['reused_receipt']}" if e.get("reused_receipt") else "") + ")" for c, e in reused.items()}
    kept = [row for row in rows if row["id"] not in reused]
    return ("skip" if reused and not kept else kind), kept, reused, skips


def merge_problem(args: Any, tip: str, tree: str, receipt: dict[str, Any] | None) -> str | None:
    """Why the lane may not land: no receipt, a stale one, or a red one."""
    prog = config.prog()
    if not receipt:
        return f"no receipt for lane {args.id}: run {prog} check {args.id} in its worktree"
    if receipt.get("tree") != tree:
        return (f"the receipt is stale: it covers tree {receipt.get('tree')}, the lane tip {tip[:10]} has tree "
                f"{tree}; rerun {prog} check {args.id}")
    if receipt.get("status") == "green":
        return None
    # A suite whose checks did not start behind a red first check (#639) is named apart from the red ones.
    results = receipt.get("results", [])
    failed = [r["suite"] for r in results if r["status"] not in ("passed", "not_run")]
    unstarted = [r["suite"] for r in results if r["status"] == "not_run"]
    problem = (f"the receipt for tree {tree} is red ({', '.join(failed)}"
               + (f"; not run: {', '.join(unstarted)}" if unstarted else "") + ")")
    entries = [e for e in (receipt.get("classification") or {}).get("checks", []) if e["class"] == "regression"]
    if entries:
        # The receipt key stays "alone": the lane's rerun of the check by itself, at its own lease size.
        rerun = all((e.get("alone") or {}).get("status") != "not rerun" for e in entries)
        problem += (f"; regression: {', '.join(e['check'] for e in entries)} fails on the lane"
                    f"{' again when rerun by itself' if rerun else ''} and passes on its base "
                    f"{receipt['classification']['base'][:10]}")
    return problem


def land(args: Any) -> int:
    prog = config.prog()
    with state.locked("land", f"lane: waiting for another land's merge (lock {state.state_dir() / '.land.lock'})"):
        record = state.load(args.id)
        if record["status"] != "active":
            raise LaneError(f"land refused: lane {args.id} is {record['status']}" +
                            ("; it has no worktree yet" if record["status"] == "planned" else ""))
        for name in record["after"]:
            if state.load(name)["status"] not in state.LANDED:
                raise LaneError(f"land refused: lane {args.id} is declared to land after {name}, which has not landed")
        main = main_checkout(record["common_dir"])
        tip = git(main, "rev-parse", "--verify", record["branch"] + "^{commit}")
        tree = git(main, "rev-parse", tip + "^{tree}")
        receipt = state.read_json(state.receipt_path(args.id))
        problem = merge_problem(args, tip, tree, receipt)
        if problem and not args.force_reason:
            raise LaneError("land refused: " + problem)
        if problem:
            record.setdefault("overrides", []).append({"at": journal.now(), "reason": args.force_reason,
                                                       "refusals": [problem]})
            journal.ledger(f"override land {args.id}: {args.force_reason} ({problem})", kind="override",
                           level="warn", lane=record,
                           details={"command": "land", "reason": args.force_reason, "refusals": [problem]})
        if not args.into and not record.get("integration"):
            raise LaneError(f"no worktree has {record['base']} checked out; give --into <integration worktree>")
        integration = os.path.realpath(args.into or record["integration"])
        if dirty(integration) or os.path.exists(git(integration, "rev-parse", "--git-path", "MERGE_HEAD")):
            raise LaneError(f"land refused: {integration} has uncommitted changes or a merge in progress")
        onto = current_branch(integration)
        if onto in PROMOTED:
            # promote waits only for lanes not yet in dev (#570): a lane landed on dev or main is reviewed first.
            reviewed, words = review.review_state(args.id)
            if not reviewed and not review.review_exemption(record):
                if not args.force_reason:
                    raise LaneError(f"land refused: {integration} is on {onto}, which promote would not hold for "
                                    f"this lane's review ({words}); run {prog} review {args.id} and mark its "
                                    "findings first, or land into the integration branch")
                record.setdefault("overrides", []).append({"at": journal.now(), "reason": args.force_reason,
                                                           "refusals": [f"unreviewed land onto {onto}"]})
                journal.ledger(f"override land {args.id} onto {onto} unreviewed: {args.force_reason} ({words})",
                               kind="override", level="warn", lane=record,
                               details={"command": "land", "reason": args.force_reason,
                                        "refusals": [f"unreviewed land onto {onto}"], "review": words})
            else:
                journal.ledger(f"land {args.id} onto {onto}: {words}", kind="landing.reviewed", lane=record,
                               details={"onto": onto, "review": words})
        previous = git(integration, "rev-parse", "HEAD")
        if git_ok(integration, "merge-base", "--is-ancestor", tip, previous):
            raise LaneError(f"land refused: {record['branch']} ({tip[:10]}) is already in {integration}")
        ticket = f" (#{record['ticket']})" if record["ticket"] != "none" else ""
        message = tickets.merge_message(args.message or f"Merge {record['branch']}{ticket}", record["ticket"])
        merge_config = [part for setting in config.current().merge_config for part in ("-c", setting)]
        # The landing lock lets the merge move an integration worktree that is on dev or main (B2).
        conflicts = ""
        with landing(record["common_dir"], f"{prog} land {args.id} (pid {os.getpid()})", args.wait) as env:
            merged = subprocess.run(["git", *merge_config, "merge", "--no-ff", "-m", message, record["branch"]],
                                    cwd=integration, capture_output=True, text=True, env=env)
            if merged.returncode:
                conflicts = git(integration, "diff", "--name-only", "--diff-filter=U", check=False)
                subprocess.run(["git", "merge", "--abort"], cwd=integration, capture_output=True, env=env)
        if merged.returncode:
            journal.ledger(f"land {args.id} conflict: {conflicts.replace(chr(10), ' ')}", kind="lane.conflict",
                           level="error", lane=record,
                           details={"paths": conflicts.split(), "integration": integration, "previous": previous})
            raise LaneError(f"merge conflict, integration left at {previous[:10]}; send it back to the lane:\n"
                            f"{conflicts or merged.stdout + merged.stderr}")
        merge = git(integration, "rev-parse", "HEAD")
        record.update(status="landed", landed={"merge": merge, "previous": previous, "tip": tip, "tree": tree,
                                               "integration": integration, "at": journal.now(),
                                               "keep": bool(args.keep)})
        state.save(record)
        journal.ledger(f"land {args.id} merge={merge} previous={previous}", kind="lane.landed", lane=record,
                       details={"merge": merge, "previous": previous, "tip": tip, "onto": onto,
                                "integration": integration, "keep": bool(args.keep)})
    # The land lock is free from here on (#573): the next land merges while this merge's smoke runs on its own
    # checkout of the merge, since the integration worktree moves on.
    with worktrees.smoke_checkout(record["common_dir"], merge) as checkout:
        merged_plan = plan.plan_for(checkout, previous)
        # The independent review (#570) runs beside the smoke and never holds the land; promote waits for it.
        with state.locked("state"):
            record = state.load(args.id)
            record["landed"].update(docs_only=review.documentation_only(merged_plan["paths"]))
            state.save(record)
        print(f"lane: {review.start_lane_review(record)}", flush=True)
        kind, rows = plan.post_merge_selection(checkout, merged_plan)
        merge_tree = git(checkout, "rev-parse", "HEAD^{tree}")
        # #640: what the lane's green receipt already verified does not run again.
        kind, rows, narrowing = plan.narrow_smoke(checkout, receipt, record["landed"], merge_tree, kind, rows)
        lane_verified = narrowing.pop("lane_verified", {})
        # fathers#91: a smoke check that already passed on the merge tree, under this toolchain, does not run again.
        toolchain = plan.toolchain_state(checkout)
        kind, rows, tree_verified, skips = smoke_reuse(kind, rows, merge_tree, toolchain)
        suites, selected = ((merged_plan["selected"], None) if rows is None
                            else (plan.suites_of(rows), plan.by_suite(rows)))
        info = host()
        if not suites:
            decision: dict[str, Any] = {"jobs": 0, "reason": "nothing to run"}
        else:
            decision = given_jobs(args.slots, "--slots") if args.slots else post_merge_jobs(info)
        jobs = decision["jobs"]
        results, check_results, triaged, stamp = checks.run_suites(checkout, suites, f"post-merge-{args.id}", jobs,
                                                                   selected=selected, phase="post-merge",
                                                                   meta=checks.lane_meta(record),
                                                                   ticket=checks.lane_ticket(record),
                                                                   toolchain=toolchain)
    status = "green" if all(r["status"] == "passed" for r in results) else "red"
    post = state.receipt_path(args.id, "post-merge")
    info = host()
    state.write_json(post, {"version": 1, "lane": args.id, "merge": merge, "base": previous,
                            "tree": merge_tree, "checkout": checkout, "kind": kind,
                            "jobs": jobs, "jobs_decision": decision, "suites": suites, "full": False,
                            "reasons": merged_plan["reasons"], "checks": selected, "selection": rows,
                            "narrowing": narrowing, "skipped": skips,
                            "check_results": {**lane_verified, **tree_verified, **check_results},
                            "results": results, "triage": triaged, "status": status, "verified_tree": stamp,
                            "leases": checks.leases_of(results),
                            "host": info, "finished": journal.now()})
    result_store.record(merge_tree, checks.recordable({**lane_verified, **tree_verified, **check_results}),
                   source="post-merge", toolchain=toolchain, lane=args.id, sha=merge, receipt=str(post),
                   reused=[*lane_verified, *tree_verified],
                   origins={**checks.origins(lane_verified, str(state.receipt_path(args.id))),
                            **checks.origins(tree_verified, "")}, phase="post-merge")
    # Reloaded: a review hand-over may have changed the record during the smoke.
    with state.locked("state"):
        record = state.load(args.id)
        record["post_merge"] = {"status": status, "receipt": str(post)}
        state.save(record)
    verified = len(lane_verified)
    journal.ledger(f"post-merge {args.id} {status} {kind} jobs={jobs} suites={','.join(suites) or '-'} "
                   + (f"lane_verified={verified} " if narrowing.get("narrowed") else "")
                   + (f"not_rerun={len(skips)} " if skips else "")
                   + f"{host_fields(info)} merge={merge}",
                   kind="lane.post_merge", level="info" if status == "green" else "error", lane=record,
                   details={"status": status, "scope": kind, "jobs": jobs, "suites": suites,
                            "lane_verified": verified, "skipped": skips, "merge": merge, "receipt": str(post)})
    ids = [c for s in suites for c in selected[s]] if selected is not None else None
    what = (", ".join(ids) if ids is not None and len(ids) <= 8 else f"{len(ids)} checks" if ids is not None
            else ", ".join(suites)) or "no suites affected"
    prefix = {"smoke": "smoke: ", "narrowed": "narrowed smoke: "}.get(kind, "")
    if kind == "skip" and skips:
        # Every check left was taken out because it passed on this merge tree before (review of fathers#91).
        what = "skipped: " + "; ".join(f"{c}: {why}" for c, why in skips.items())
        if narrowing.get("narrowed"):
            what += f"; the lane's green receipt stands for {verified}"
    elif kind == "skip" and merge_tree == narrowing["receipt_tree"]:
        what = f"skipped: the lane's green receipt verified the merge tree {merge_tree[:10]}"
    elif kind == "skip":
        brought = ", ".join(narrowing["integration_paths"][:3]) + (", ..." if len(narrowing["integration_paths"]) > 3
                                                                    else "")
        what = (f"skipped: what the integration brought in ({brought}) selects no smoke check; the lane's green "
                f"receipt stands for {verified}")
    elif narrowing.get("narrowed"):
        what += f"; the lane's receipt stands for {verified} more"
    elif narrowing.get("reason") and rows:
        what += f"; whole, since {narrowing['reason']}"
    if skips and kind != "skip":
        what += f"; {len(skips)} not run again ({', '.join(f'{c}: {why}' for c, why in skips.items())})"
    print(f"Landed {record['branch']} as {merge[:10]} on {current_branch(integration) or integration}; "
          f"post-merge {status} ({prefix}{what})")
    # #25: the lane's issue shows the land once its smoke is green; a gh failure only warns.
    tickets.after_land(record, merge, integration, onto or integration, receipt, status, prefix + what)
    if receipt and receipt.get("changed_red_tests"):
        print(checks.changed_red_tests_line(receipt["changed_red_tests"]))
    if status == "green":
        reviewed, words = review.review_state(args.id)
        if reviewed or args.keep:
            done.finish_landed(args.id, args.keep)
        else:
            # The fix round runs in the lane's worktree, where a resumed agent keeps its context (#570).
            print(f"Lane {args.id} kept for its review ({words}): it does not count toward the lane cap; "
                  "marking its last finding "
                  f"finishes it (" + review.mark_help(args.id) + ")")
        return 0
    # A suite whose checks did not start because the first checks were red (#639) is not one of the failures.
    failed = [r for r in results if r["status"] == "failed"]
    for result in failed:
        named = ', '.join(result['failed_checks']) or '(see log)'
        print(f"  {result['suite']}: failed checks {named}  {result['log']}")
    if triaged:
        print(triaged)
    head = git(integration, "rev-parse", "HEAD")
    if head != merge:
        print(f"{integration} has moved on to {head[:10]} since; this smoke checked the merge {merge[:10]}.")
    print(f"The merge {merge[:10]} stays in place, marked red. Lanes in it whose checks ran the failed checks:")
    print("\n".join(touched_lines(failed_items(failed), integration, merge)))
    return 1
