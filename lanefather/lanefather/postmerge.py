"""A red post-merge smoke cleared without --force-reason (fathers#167).

A landed lane's smoke can go red for a reason outside the lane: the smoke checkout had the published dependency
instead of the integration build. Two kinds of evidence clear it, and `done` names the one it took in the ledger
(`lane.done`, `post_merge_cleared_by`) and the lane record (`post_merge.cleared_by`):

- a later green run on the same integration branch, on a commit that holds the lane's merge and that the
  integration branch holds: the batch gate there (`promote-<sha>.gate.json`, the full gate), or another lane's
  green post-merge smoke that ran each check the red smoke ran (its own run on its tree, not a result taken from
  another tree). done and done --finished look for it themselves; nothing runs.
- `recheck ID` (lanefather.land.recheck, which runs and completes it as land does its smoke): the checks the red
  smoke ran, again, on the integration tip when the tip holds the merge. Green, its receipt (`<id>.recheck.json`)
  replaces the red result in the record; the red receipt stays as it was.

Each receipt is matched by identity: its commit exists and has the tree the receipt names, the lane's merge is its
ancestor, the integration worktree's HEAD holds it, and a smoke's lane landed into the same integration worktree.
It ran on the red smoke's platform (a gate's from its receipt or its results-store entry) and finished after the
red smoke did.
"""
from __future__ import annotations

import os
from typing import Any, cast

from . import results as result_store
from . import state
from .errors import LaneError
from .gitops import git, git_ok
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


def gate_platform(gate: Receipt, path: Any) -> str | None:
    """The platform a batch gate ran on: its receipt's, else that of the full-gate result the results store holds
    for this receipt (promote records both); None when neither names one."""
    if gate.get("platform"):
        return str(gate["platform"])
    tree = gate.get("tree")
    if not isinstance(tree, str) or not result_store.TREE.match(tree):
        return None
    found = [e for e in result_store.entries(tree) if e.get("full") and e.get("receipt") == str(path)]
    return str(found[-1]["platform"]) if found and found[-1].get("platform") else None


def candidates(record: Record, store: state.Store, integration: str, red: Receipt) -> list[dict[str, Any]]:
    """Every receipt that clears the red smoke, oldest first."""
    merge = str(red["merge"])
    head = git(integration, "rev-parse", "HEAD")
    # The evidence must stand for the platform the red smoke ran on, and come after it: a run that passed before
    # the smoke failed says nothing about that failure.
    platform = red.get("platform") or result_store.host_platform()
    after = str(red.get("finished") or "")

    def later(at: str) -> bool:
        return bool(after) and at > after

    def holds(sha: str, tree: Any) -> bool:
        return (bool(sha) and git(integration, "rev-parse", "--verify", "--quiet", f"{sha}^{{tree}}", check=False)
                == tree and git_ok(integration, "merge-base", "--is-ancestor", merge, sha)
                and git_ok(integration, "merge-base", "--is-ancestor", sha, head))
    found: list[dict[str, Any]] = []
    for path in sorted(store.directory.glob("*.gate.json")):
        gate = state.read_json(path) or {}
        sha = str(gate.get("sha") or "")
        if (gate.get("full") is True and gate.get("status") == "green" and later(str(gate.get("finished") or ""))
                and gate_platform(gate, path) == platform and holds(sha, gate.get("tree"))):
            found.append({"kind": "gate", "receipt": str(path), "sha": sha, "at": str(gate.get("finished") or ""),
                          "red_receipt": str(store.receipt_path(record["id"], "post-merge"))})
    for other in store.lanes():
        if other["id"] == record["id"] or integration_of(other) != integration:
            continue
        path = store.receipt_path(other["id"], "post-merge")
        smoke: Receipt = state.read_json(path) or {}
        sha = str(smoke.get("merge") or "")
        if (smoke.get("status") != "green" or sha != landed(other).get("merge")
                or smoke.get("platform") != platform or not later(str(smoke.get("finished") or ""))
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


def recheckable(record: Record, store: state.Store) -> tuple[Receipt, str, str]:
    """What `recheck` reruns (lanefather.land.recheck), under the state lock: the red smoke receipt, the integration
    worktree and its tip, which holds the merge; else why not."""
    lane_id = record["id"]
    if record["status"] not in ("landed", "released"):
        raise LaneError(f"recheck refused: lane {lane_id} is {record['status']}, not landed")
    smoke = post_merge(record).get("status")
    if smoke != "red":
        raise LaneError(f"recheck refused: lane {lane_id}'s post-merge smoke is {smoke or 'not run'}: "
                        "nothing to recheck")
    red = red_receipt(record, store)
    if red is None:
        raise LaneError(f"recheck refused: {store.receipt_path(lane_id, 'post-merge')} is not the red smoke of "
                        f"the merge {str(landed(record).get('merge'))[:10]}")
    if not ran(red)[1]:
        raise LaneError(f"recheck refused: the red smoke {store.receipt_path(lane_id, 'post-merge')} names no suites")
    integration = integration_of(record)
    if integration is None:
        raise LaneError(f"recheck refused: lane {lane_id}'s integration worktree is gone")
    merge, tip = str(red["merge"]), git(integration, "rev-parse", "HEAD")
    if not git_ok(integration, "merge-base", "--is-ancestor", merge, tip):
        raise LaneError(f"recheck refused: the integration tip {tip[:10]} does not hold the merge {merge[:10]}")
    return red, integration, tip
