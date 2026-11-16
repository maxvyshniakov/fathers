"""A red post-merge smoke cleared without --force-reason (fathers#167).

A landed lane's smoke can go red for a reason outside the lane: the smoke checkout had the published dependency
instead of the integration build. Two kinds of evidence clear it, and `done` names the one it took in the ledger
(`lane.done`, `post_merge_cleared_by`) and the lane record (`post_merge.cleared_by`):

- a later green run on the same integration branch, on a commit that holds the lane's merge and that the
  integration branch holds: the batch gate there (`promote-<sha>.gate.json`, the full gate), or another lane's
  green post-merge smoke that ran each check the red smoke ran (its own run on its tree, not a result taken from
  another tree). done and done --finished look for it themselves; nothing runs.
- `recheck ID`: the checks the red smoke ran, again, on the integration tip when the tip holds the merge. Green, its
  receipt (`<id>.recheck.json`) replaces the red result in the record; the red receipt stays as it was.

Each receipt is matched by identity: its commit exists and has the tree the receipt names, the lane's merge is its
ancestor, the integration worktree's HEAD holds it, a smoke's lane landed into the same integration worktree, and a
smoke receipt naming a platform names the red smoke's.
"""
from __future__ import annotations

import os
from typing import Any, cast

from . import checks, config, journal, plan, state, tickets, worktrees
from . import results as result_store
from .errors import LaneError
from .gitops import current_branch, git, git_ok
from .host import given_jobs, host, post_merge_jobs
from .state import Record

Receipt = dict[str, Any]


def landed(record: Record) -> dict[str, Any]:
    value: Any = record.get("landed")
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def post_merge(record: Record) -> dict[str, Any]:
    value: Any = record.get("post_merge")
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def red_receipt(record: Record, store: state.Store) -> Receipt | None:
    """The lane's red post-merge receipt, when it is the smoke of the merge the lane landed as."""
    receipt = state.read_json(store.receipt_path(record["id"], "post-merge"))
    merge = landed(record).get("merge")
    if not receipt or receipt.get("status") != "red" or not merge or receipt.get("merge") != merge:
        return None
    return receipt


def integration_of(record: Record) -> str | None:
    """The integration worktree the lane landed into, when it still exists."""
    path = landed(record).get("integration") or record.get("integration")
    return os.path.realpath(path) if path and os.path.isdir(path) else None


def ran(red: Receipt) -> tuple[dict[str, list[str]] | None, list[str]]:
    """What the red smoke ran: its checks by suite, or None for whole suites; and its suites."""
    selected: Any = red.get("checks")
    named: list[Any] = red.get("suites") or []
    suites = [str(s) for s in named]
    if isinstance(selected, dict):
        return {str(k): [str(c) for c in v] for k, v in cast(dict[str, Any], selected).items()}, suites
    return None, suites


def uncovered(red: Receipt, green: Receipt) -> list[str]:
    """The checks (whole suites, for a smoke that ran them) of the red smoke that the green smoke did not pass on its
    own tree: fresh, or a pass recorded on that same tree. A result it took from another tree (the lane's receipt,
    the previous tip) stands for nothing here."""
    selected, suites = ran(red)
    if not suites:
        return ["(the red smoke names no suites)"]
    tree = green.get("tree")
    rows: list[dict[str, Any]] = green.get("results") or []
    whole = {str(r.get("suite")) for r in rows if r.get("status") == "passed" and "checks" not in r}
    results: dict[str, Any] = green.get("check_results") or {}

    def stands(check: str) -> bool:
        entry: dict[str, Any] = results.get(check) or {}
        return entry.get("status") == "passed" and entry.get("reused_from") in (None, tree)
    if selected is None:
        return [s for s in suites if s not in whole]
    return [c for suite, ids in selected.items() for c in ids if suite not in whole and not stands(c)]


def candidates(record: Record, store: state.Store, integration: str, red: Receipt) -> list[dict[str, Any]]:
    """Every receipt that clears the red smoke, oldest first."""
    merge = str(red["merge"])
    head = git(integration, "rev-parse", "HEAD")

    def holds(sha: str, tree: Any) -> bool:
        return (bool(sha) and git(integration, "rev-parse", "--verify", "--quiet", f"{sha}^{{tree}}", check=False)
                == tree and git_ok(integration, "merge-base", "--is-ancestor", merge, sha)
                and git_ok(integration, "merge-base", "--is-ancestor", sha, head))
    found: list[dict[str, Any]] = []
    for path in sorted(store.directory.glob("*.gate.json")):
        gate = state.read_json(path) or {}
        sha = str(gate.get("sha") or "")
        if gate.get("full") is True and gate.get("status") == "green" and holds(sha, gate.get("tree")):
            found.append({"kind": "gate", "receipt": str(path), "sha": sha, "at": str(gate.get("finished") or ""),
                          "red_receipt": str(store.receipt_path(record["id"], "post-merge"))})
    for other in store.lanes():
        if other["id"] == record["id"] or integration_of(other) != integration:
            continue
        path = store.receipt_path(other["id"], "post-merge")
        smoke: Receipt = state.read_json(path) or {}
        sha = str(smoke.get("merge") or "")
        platform = smoke.get("platform")
        if (smoke.get("status") != "green" or sha != landed(other).get("merge")
                or (platform and red.get("platform") and platform != red["platform"])
                or uncovered(red, smoke) or not holds(sha, smoke.get("tree"))):
            continue
        found.append({"kind": "post-merge", "lane": other["id"], "receipt": str(path), "sha": sha,
                      "at": str(smoke.get("finished") or ""),
                      "red_receipt": str(store.receipt_path(record["id"], "post-merge"))})
    return sorted(found, key=lambda entry: entry["at"])


def clearing(record: Record, store: state.Store, integration: str | None = None) -> dict[str, Any] | None:
    """What clears the lane's red post-merge smoke, the oldest such receipt; None when nothing does."""
    red = red_receipt(record, store)
    integration = os.path.realpath(integration) if integration else integration_of(record)
    if red is None or integration is None:
        return None
    found = candidates(record, store, integration, red)
    return found[0] if found else None


def words(cleared: dict[str, Any]) -> str:
    """The run that cleared a red smoke, for the ledger and the output."""
    sha = str(cleared.get("sha") or "")[:10]
    what = {"gate": f"the batch gate of {sha}", "recheck": f"the recheck of {sha}",
            "post-merge": f"the post-merge smoke of {cleared.get('lane')} {sha}"}.get(str(cleared.get("kind")),
                                                                                 f"{cleared.get('kind')} {sha}")
    return f"{what} ({cleared.get('receipt')})"


def recheck(args: Any) -> int:
    """`recheck ID`: the checks the lane's red post-merge smoke ran, on the integration tip when it holds the merge.
    Green, its receipt replaces the red result, and the lane is finished as land would have finished it."""
    store = state.Store(config.current())
    prog = store.project.command
    with store.locked("state"):
        record = store.load(args.id)
        if record["status"] not in ("landed", "released"):
            raise LaneError(f"recheck refused: lane {args.id} is {record['status']}, not landed")
        smoke = post_merge(record).get("status")
        if smoke != "red":
            raise LaneError(f"recheck refused: lane {args.id}'s post-merge smoke is {smoke or 'not run'}: "
                            "nothing to recheck")
        red = red_receipt(record, store)
        if red is None:
            raise LaneError(f"recheck refused: {store.receipt_path(args.id, 'post-merge')} is not the red smoke of "
                            f"the merge {str(landed(record).get('merge'))[:10]}")
        integration = integration_of(record)
        if integration is None:
            raise LaneError(f"recheck refused: lane {args.id}'s integration worktree is gone")
        merge = str(red["merge"])
        tip = git(integration, "rev-parse", "HEAD")
        if not git_ok(integration, "merge-base", "--is-ancestor", merge, tip):
            raise LaneError(f"recheck refused: the integration tip {tip[:10]} does not hold the merge {merge[:10]}")
        selected, suites = ran(red)
        if not suites:
            raise LaneError(f"recheck refused: the red smoke {store.receipt_path(args.id, 'post-merge')} names no "
                            "suites")
    with worktrees.smoke_checkout(record["common_dir"], tip) as checkout:
        toolchain = plan.toolchain_state(checkout)
        decision = given_jobs(args.slots, "--slots") if args.slots else post_merge_jobs(host())
        results, check_results, triaged, _stamp = checks.run_suites(
            checkout, suites, f"recheck-{args.id}", decision["jobs"], selected=selected, phase="post-merge",
            meta=checks.lane_meta(record), ticket=checks.lane_ticket(record), toolchain=toolchain)
        tree = git(checkout, "rev-parse", "HEAD^{tree}")
    status = "green" if results and all(r["status"] == "passed" for r in results) else "red"
    path = store.receipt_path(args.id, "recheck")
    red_path = str(store.receipt_path(args.id, "post-merge"))
    state.write_json(path, {"version": 1, "lane": args.id, "kind": "recheck", "merge": merge, "sha": tip,
                            "tree": tree, "checkout": checkout, "platform": result_store.host_platform(),
                            "red_receipt": red_path, "suites": suites, "checks": selected,
                            "check_results": check_results, "results": results, "triage": triaged, "status": status,
                            "jobs": decision["jobs"], "jobs_decision": decision,
                            "leases": checks.leases_of(results), "host": host(), "finished": journal.now()})
    result_store.record(tree, checks.recordable(check_results), source="post-merge", toolchain=toolchain,
                        lane=args.id, sha=tip, receipt=str(path), phase="post-merge")
    cleared = {"kind": "recheck", "receipt": str(path), "sha": tip, "red_receipt": red_path}
    with store.locked("state"):
        record = store.load(args.id)
        if status == "green" and post_merge(record).get("status") == "red":
            record["post_merge"] = {"status": "green", "receipt": str(path), "cleared_by": cleared}
            store.save(record)
    journal.ledger(f"recheck {args.id} {status} sha={tip} merge={merge} receipt={path}"
                   + (f" replaces red {red_path}" if status == "green" else ""),
                   kind="lane.rechecked", level="info" if status == "green" else "error", lane=record,
                   details={"status": status, "sha": tip, "merge": merge, "receipt": str(path),
                            "red_receipt": red_path, "suites": suites},
                   directory=store.directory, project=store.project)
    ids = [c for s in suites for c in (selected or {}).get(s, [])] if selected is not None else None
    what = ", ".join(ids) if ids is not None and len(ids) <= 8 else f"{len(ids)} checks" if ids else ", ".join(suites)
    if status != "green":
        print(f"Lane {args.id}: post-merge still red on {tip[:10]} (recheck: {what}); {path}")
        for result in results:
            if result["status"] != "passed":
                named = ", ".join(result.get("failed_checks") or []) or "(see log)"
                print(f"  {result['suite']}: failed checks {named}  {result['log']}")
        if triaged:
            print(triaged)
        return 1
    print(f"Lane {args.id}: post-merge green on {tip[:10]} (recheck: {what}); it replaces the red smoke of "
          f"{merge[:10]} ({path})")
    receipt = state.read_json(store.receipt_path(args.id))
    tickets.after_land(record, merge, integration, current_branch(integration) or integration, receipt, status,
                       f"recheck on {tip[:10]}: {what}")
    # done and review import this module.
    from . import done as done_module
    from . import review
    reviewed, review_words = review.review_state(args.id, store)
    keep = done_module.kept(record)
    if reviewed or keep:
        done_module.finish_landed(args.id, keep)
    else:
        print(f"Lane {args.id} kept for its review ({review_words}); marking its last finding finishes it ("
              + review.mark_help(args.id, prog) + ")")
    return 0

