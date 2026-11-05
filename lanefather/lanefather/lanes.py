"""Registering lanes: start, next, adopt and list.

Statuses: planned (registered), active (has a worktree), landed (merged), released (merged with a green smoke while
its agent still locks the worktree), done (cleaned up). Only planned and active lanes count toward the lane cap and
hold their claims. start prints the brief header for the lane's prompt, ending with the rule snippets whose path
globs overlap the lane's claims.
"""
from __future__ import annotations

from collections.abc import Iterable
import json
import os
from pathlib import Path
import sys
from typing import Any

from . import claims, config, host, journal, promote, queue, review, state, worktrees
from .errors import LaneError
from .gitops import common_dir, current_branch, git, git_ok, main_checkout, worktree_list
from .state import Record


def header(record: Record, rules: list[dict[str, Any]] | tuple[()] = ()) -> str:
    prog = config.prog()
    ticket = f"#{record['ticket']}" if record["ticket"] != "none" else "none (no ticket)"
    targets = record.get("cargo_targets") or []
    return "\n".join([
        f"Lane {record['id']}: ticket {ticket}",
        f"Branch: {record['branch']} from {record['base']} ({record['base_sha'][:10]})",
        f"Claims: {' '.join(record['claims']) or f'none yet; {prog} check adds the paths the lane changes'}"
        + (f" (lands after {', '.join(record['after'])})" if record["after"] else ""),
        # The agent records its worktree at once, so the lane record names it before the first check (#577).
        "Worktree: " + (record["worktree"]
                        or f"yours; first run git checkout -B {record['branch']} {record['base']}")
        + ("" if record["worktree"] else f', then {prog} adopt {record["id"]} "$PWD" {record["branch"]}'),
        f"Scratch: {record['scratch'][0]} (evidence logs and probes; {prog} done removes it)",
        *([f"Cargo target, when the lane needs its own: {targets[0]}"] if targets else []),
        f"Before reporting: {prog} check {record['id']} in your worktree, on a clean committed tree.",
        f"Report: the block in {config.current().report_doc}, with "
        f"Receipt: {state.receipt_path(record['id'])}",
        *claims.rules_block(list(rules)),
    ])


def prepare(cwd: Any, lane_id: str, ticket: str, lane_claims: list[str], base: str | None, after: list[str],
            branch: str | None = None, scratch: Iterable[str] = ()) -> tuple[Record, list[dict[str, Any]]]:
    """The record start registers, and the rules its claims touch; nothing is written."""
    state.record_path(lane_id)
    for claim in lane_claims:
        if claim.startswith("/") or ".." in claim.split("/"):
            raise LaneError(f"claim {claim}: give a glob relative to the repository")
    common = common_dir(cwd)
    base = base or current_branch(cwd) or git(cwd, "rev-parse", "HEAD")
    base_sha = git(cwd, "rev-parse", "--verify", base + "^{commit}")
    integration = next((w["path"] for w in worktree_list(cwd) if w.get("branch") == base), None)
    rules = claims.rules_for(lane_claims, git(cwd, "rev-parse", "--show-toplevel"))
    record: Record = {
        "id": lane_id, "ticket": str(ticket).lstrip("#"), "branch": branch or f"lane/{lane_id}",
        "base": base, "base_sha": base_sha, "claims": lane_claims, "after": after, "common_dir": common,
        "integration": integration, "worktree": None, "status": "planned", "created": journal.now(),
        "scratch": [str(state.state_dir() / f"{lane_id}.scratch"), *(os.path.realpath(s) for s in scratch)],
        # The record key keeps the name older tools/lane checkouts read (#648).
        "cargo_targets": worktrees.lane_targets(lane_id), "overrides": [],
        "rules": [rule["name"] for rule in rules]}
    return record, rules


def register(record: Record, rules: list[dict[str, Any]], force_reason: str | None, create: bool) -> None:
    """Enforce the cap, disk, load and claims, save the record and print its brief header."""
    prog = config.prog()
    lane_id, lane_claims, after = record["id"], record["claims"], record["after"]
    cap, source = host.lane_cap()
    with state.locked("state"):
        existing = state.read_json(state.record_path(lane_id))
        if existing and existing["status"] != "done":
            raise LaneError(f"lane {lane_id} exists ({existing['status']}); finish it with {prog} done")
        others = [r for r in state.lanes() if r["id"] != lane_id]
        for name in after:
            if name not in {r["id"] for r in others if r["status"] != "done"}:
                raise LaneError(f"--after {name}: no lane that is not done by that name")
        active = state.occupying(others)
        refusals: list[str] = []
        info = host.host()
        if len(active) >= cap:
            refusals.append(f"{len(active)} lanes are planned or active ({', '.join(r['id'] for r in active)}); "
                            f"the cap is {cap} ({source}): land one to make room under the lane cap")
        if info["disk_free_gb"] < host.MIN_DISK_GB:
            refusals.append(f"free disk is {info['disk_free_gb']:.0f} GB, under {host.MIN_DISK_GB} GB: "
                            f"run {prog} done on landed lanes")
        if info["load15"] > host.load_limit(info["cores"]):
            refusals.append(f"load15 is {info['load15']:.1f}, above {host.LOAD_FACTOR}x{info['cores']} cores")
        if refusals and not force_reason:
            raise LaneError("refused:\n- " + "\n- ".join(refusals))
        for other in active:
            pairs = [] if other["id"] in after else claims.overlapping(lane_claims, other)
            if pairs:
                listed = ", ".join(f"{m} with {t}" for m, t in pairs)
                raise LaneError(f"claims overlap lane {other['id']} (#{other['ticket']}): {listed}. Run the lanes "
                                f"in sequence: start this one with --after {other['id']}")
        Path(record["scratch"][0]).mkdir(exist_ok=True)
        if refusals:
            record["overrides"].append({"at": journal.now(), "reason": force_reason, "refusals": refusals})
            journal.ledger(f"override start {lane_id}: {force_reason} (refusals: {'; '.join(refusals)})")
        if create:
            main = main_checkout(record["common_dir"])
            path = os.path.join(main, ".claude", "worktrees", f"lane-{lane_id}")
            git(main, "worktree", "add", "-q", "-b", record["branch"], path, record["base_sha"])
            record.update(worktree=os.path.realpath(path), status="active")
        state.save(record)
        journal.ledger(f"start {lane_id} ticket={record['ticket']} branch={record['branch']} "
                       f"base={record['base']}@{record['base_sha'][:10]} claims={' '.join(lane_claims)} "
                       f"after={','.join(after) or '-'} cap={cap} ({source}) {host.host_fields(info)}")
    print(header(record, rules))


def start(args: Any) -> int:
    lane_claims = [c for claim in args.claims for c in claim.split()]
    record, rules = prepare(os.getcwd(), args.id, args.ticket, lane_claims, args.base, args.after, args.branch,
                            args.scratch)
    register(record, rules, args.force_reason, args.create)
    return 0


def next_lane(args: Any) -> int:
    data, ready, states = queue.next_ready()
    for entry, reason in states:
        if reason and entry is not ready:
            print(f"  skip {entry['id']} (#{str(entry['ticket']).lstrip('#')}): {reason}", file=sys.stderr)
    if ready is None:
        print(f"No ready entry in {queue.queue_path()}", file=sys.stderr)
        return 3
    record, rules = prepare(os.getcwd(), ready["id"], ready["ticket"], list(ready["claims"]),
                            args.base or data.get("base"), [])
    if args.start:
        register(record, rules, args.force_reason, args.create)
    else:
        print(header(record, rules))
    # An entry may carry its brief (a queued pin lane, #588): it follows the header into the lane's prompt.
    if isinstance(ready.get("brief"), str) and ready["brief"].strip():
        print("Brief:\n" + ready["brief"].strip())
    return 0


def adopt_worktree(record: Record, worktree: str, branch: str) -> None:
    path = os.path.realpath(worktree)
    if not os.path.isdir(path) or common_dir(path) != record["common_dir"]:
        raise LaneError(f"adopt refused: {path} is not a worktree of {main_checkout(record['common_dir'])}")
    actual = current_branch(path)
    if actual != branch:
        raise LaneError(f"adopt refused: {path} is on {actual or 'a detached HEAD'}, not {branch}")
    for other in state.lanes():
        if other["id"] != record["id"] and other["status"] != "done" and other.get("worktree") == path:
            raise LaneError(f"adopt refused: lane {other['id']} owns {path}")
    record.update(worktree=path, branch=branch, status="active" if record["status"] == "planned" else record["status"],
                  adopted=journal.now())
    state.save(record)
    journal.ledger(f"adopt {record['id']} worktree={path} branch={branch}")


def adopt(args: Any) -> int:
    with state.locked("state"):
        record = state.load(args.id)
        if record["status"] not in ("planned", "active"):
            raise LaneError(f"adopt refused: lane {args.id} is {record['status']}")
        adopt_worktree(record, args.worktree, args.branch)
    print(f"Lane {args.id} adopted {record['worktree']} on {args.branch}")
    return 0


def receipt_state(record: Record) -> str:
    receipt = state.read_json(state.receipt_path(record["id"]))
    if not receipt:
        return "no receipt"
    text = receipt.get("status", "?")
    if record["status"] == "active":
        tip = git(main_checkout(record["common_dir"]), "rev-parse", "--verify", "--quiet",
                  record["branch"] + "^{tree}", check=False)
        if tip and tip != receipt.get("tree"):
            text += " (stale)"
    if record.get("post_merge"):
        text += f", post-merge {record['post_merge']['status']}"
    if record.get("landed") and record["status"] != "done":
        text += f", {review.review_state(record['id'])[1]}"
    return text


def list_lanes(args: Any) -> int:
    shown = [r for r in state.lanes() if args.all or r["status"] != "done"]
    cap, source = host.lane_cap()
    if args.json:
        # Scripts read lanes here (#577): every record with its worktree (null until adopt) and receipt state.
        print(json.dumps({"lanes": [dict(r, receipt=receipt_state(r)) for r in shown],
                          "active": len(state.occupying()), "cap": cap, "cap_source": source,
                          "host": host.host()}, indent=2))
        return 0
    for record in shown:
        print(f"{record['id']}  {record['status']}  #{record['ticket']}  {record['branch']}  "
              f"receipt: {receipt_state(record)}")
        print(f"    claims: {' '.join(record['claims'])}"
              + (f"  after: {', '.join(record['after'])}" if record["after"] else ""))
        if record.get("worktree"):
            print(f"    worktree: {record['worktree']}")
    print(f"{len(state.occupying())} of {cap} lanes planned or active (cap {source}); {host.host_line(host.host())}")
    # #666: the repository it runs in, as last fetched; outside a Git checkout there is nothing to compare.
    if git_ok(os.getcwd(), "rev-parse", "--git-dir"):
        for line in promote.ahead_lines(os.getcwd()):
            print(line)
    return 0
