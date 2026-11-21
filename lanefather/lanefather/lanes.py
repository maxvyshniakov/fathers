"""Registering lanes: start, next, adopt and list.

Statuses: planned (registered), active (has a worktree), landed (merged), released (merged with a green smoke while
its agent still locks the worktree), done (cleaned up). Only planned and active lanes count toward the lane cap and
hold their claims, each among the lanes of its own repository (fathers#149). start prints the brief header for the
lane's prompt, ending with the rule snippets whose path globs overlap the lane's claims.
"""
from __future__ import annotations

from collections.abc import Iterable
import json
import os
from pathlib import Path
import sys
from typing import Any

from . import (
    admission,
    claims,
    config,
    done,
    events,
    host,
    journal,
    promote,
    queue,
    review,
    slots,
    state,
    tickets,
    worktrees,
)
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
        *claims.rules_block(list(rules)),
        *next_lines(record),
    ])


def next_lines(record: Record) -> list[str]:
    """The lane's next commands, as start and adopt print them (fathers#175): the agent goes on without a complete
    brief or a --help turn."""
    prog, lane_id = config.prog(), record["id"]
    return [
        "Next, in the worktree:",
        *([f'  git checkout -B {record["branch"]} {record["base"]} && {prog} adopt {lane_id} "$PWD" '
           f'{record["branch"]}'] if not record["worktree"] else []),
        f"  {prog} check {lane_id}    (on a clean committed tree; its receipt is the evidence)",
        f"  Hand back: the block in {config.current().report_doc}, with Receipt: {state.receipt_path(lane_id)}",
        f"  Then the coordinator lands it ({prog} land {lane_id}); {prog} wait {lane_id} --for landed blocks until "
        "it does, no loop over list.",
    ]


def prepare(cwd: Any, lane_id: str, ticket: str, lane_claims: list[str], base: str | None, after: list[str],
            branch: str | None = None, scratch: Iterable[str] = (), *, parents: list[str] | None = None,
            batch_fix: bool = False) -> tuple[Record, list[dict[str, Any]]]:
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
        "cargo_targets": worktrees.lane_targets(lane_id), "overrides": [], "agents": [],
        "rules": [rule["name"] for rule in rules]}
    state.set_fix_parents(record, parents or [], batch=batch_fix)
    return record, rules


def fix_parent(record: Record) -> str | None:
    """The landed lane whose kept worktree a fix lane (review --fix-lane) takes over, else None."""
    parent_id = state.hand_over_parent(record)
    if parent_id is None:
        return None
    parent = state.read_json(state.record_path(parent_id))
    if (parent and parent.get("status") in ("landed", "released") and parent.get("worktree")
            and parent.get("worktree") == record.get("worktree")):
        return parent_id
    return None


def admit(record: Record, force_reason: str | None, repaired: str | None) -> tuple[admission.Decision, list[str]]:
    """Under the state lock: the existing-lane, dependency, cap, disk and claim rules for registration."""
    prog = config.prog()
    lane_id, lane_claims, after = record["id"], record["claims"], record["after"]
    existing = state.read_json(state.record_path(lane_id))
    if existing and existing["status"] != "done":
        raise LaneError(f"lane {lane_id} exists ({existing['status']}); finish it with {prog} done")
    others = [r for r in state.Store(config.current()).lanes(strict=True) if r["id"] != lane_id]
    for name in after:
        if name not in {r["id"] for r in others if r["status"] != "done"}:
            raise LaneError(f"--after {name}: no lane that is not done by that name")
    for fix_id in state.fix_parents(record):
        prefix = f"--fix-of {fix_id}" if state.batch_fix(record) else "hand-over refused"
        review.fix_parent_review(fix_id, record["common_dir"], prefix)
    parent = fix_parent(record)
    decision = admission.evaluate(config.current(), others, record["common_dir"], cap_exempt=parent is not None,
                                  repaired=repaired)
    refusals = list(decision.refusals)
    if refusals and not force_reason and not (decision.disk_short and len(refusals) == 1):
        raise LaneError("refused:\n- " + "\n- ".join(refusals))
    for other in decision.occupying:
        pairs = [] if other["id"] in after else claims.overlapping(lane_claims, other)
        if pairs:
            listed = ", ".join(f"{m} with {t}" for m, t in pairs)
            raise LaneError(f"claims overlap lane {other['id']} (#{other['ticket']}): {listed}. Run the lanes "
                            f"in sequence: start this one with --after {other['id']}")
    return decision, refusals


def register(record: Record, rules: list[dict[str, Any]], force_reason: str | None, create: bool,
             *, agent: str | None = None) -> None:
    """Enforce the cap, disk and claims, save the record and print its brief header. When the disk floor is
    the only refusal left, start's disk repair runs first (fathers#157), outside the state lock each done takes,
    and the decision is taken again; `review --fix-lane` registers through here, so it repairs too."""
    repaired: str | None = None
    while True:
        with state.locked("state"):
            decision, refusals = admit(record, force_reason, repaired)
            if not refusals or force_reason or repaired is not None:
                if refusals and not force_reason:
                    raise LaneError("refused:\n- " + "\n- ".join(refusals))
                register_admitted(record, decision, refusals, force_reason, create, agent=agent)
                break
        repaired = done.repair_disk(decision.machine["disk_free_gb"], decision.floor)
    print(header(record, rules))
    machine = decision.machine
    if machine["load15"] > machine["load_limit"]:
        print(f"lane: load15 is {machine['load15']:.1f}, above the advisory limit of {machine['load_limit']:.1f}. "
              "Checks may wait for a Slotfather slot.", file=sys.stderr)
    warning = tickets.no_ticket_warning(record)
    if warning:
        print(warning, file=sys.stderr)


def register_admitted(record: Record, decision: admission.Decision, refusals: list[str], force_reason: str | None,
                      create: bool, *, agent: str | None = None) -> None:
    """Under the state lock, once admitted: save the record and log the start."""
    lane_id, lane_claims, after = record["id"], record["claims"], record["after"]
    cap, source, info = decision.cap, decision.source, decision.machine
    parent = fix_parent(record)
    if parent is not None:
        # fathers#45 (a): the fix lane takes its landed parent's place, outside the cap; the record says why.
        source = record["cap_source"] = f"fix-lane of {parent}"
    Path(record["scratch"][0]).mkdir(exist_ok=True)
    if refusals:
        record["overrides"].append({"at": journal.now(), "reason": force_reason, "refusals": refusals})
        journal.ledger(f"override start {lane_id}: {force_reason} (refusals: {'; '.join(refusals)})",
                       kind="override", level="warn", lane=record,
                       details={"command": "start", "reason": force_reason, "refusals": refusals})
    if create:
        main = main_checkout(record["common_dir"])
        path = os.path.join(main, ".claude", "worktrees", f"lane-{lane_id}")
        git(main, "worktree", "add", "-q", "-b", record["branch"], path, record["base_sha"])
        record.update(worktree=os.path.realpath(path), status="active")
    state.record_agent(record, "start", events.agent_context(agent), now=journal.now())
    state.save(record)
    journal.ledger(f"start {lane_id} ticket={record['ticket']} branch={record['branch']} "
                   f"base={record['base']}@{record['base_sha'][:10]} claims={' '.join(lane_claims)} "
                   f"after={','.join(after) or '-'} cap={cap} ({source}) {host.host_fields(info)}",
                   kind="lane.started", lane=record,
                   details={"status": record["status"], "branch": record["branch"], "base": record["base"],
                            "base_sha": record["base_sha"], "claims": lane_claims, "after": after,
                            "worktree": record["worktree"], "cap": cap, "cap_source": source})


def start(args: Any) -> int:
    lane_claims = [c for claim in args.claims for c in claim.split()]
    given = getattr(args, "fix_of", None) or ()
    parents = list(dict.fromkeys(name for value in given for name in value.replace(",", " ").split()))
    record, rules = prepare(os.getcwd(), args.id, args.ticket, lane_claims, args.base, args.after, args.branch,
                            args.scratch, parents=parents, batch_fix=True)
    register(record, rules, args.force_reason, args.create, agent=getattr(args, "agent", None))
    if not parents and "review-fixes" in args.id:
        print(f"{config.prog()}: warning: {args.id} looks like a fix lane but names no parent, so its land starts "
              f"a review of the fix; to exempt it, {config.prog()} drop {args.id} and start it again with "
              "--fix-of <lane>,<lane>", file=sys.stderr)
    return 0


def next_lane(args: Any) -> int:
    data, ready, states = queue.next_ready(common_dir(os.getcwd()))
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


def adopt_worktree(record: Record, worktree: str, branch: str, *, agent: str | None = None) -> None:
    path = os.path.realpath(worktree)
    if not os.path.isdir(path) or common_dir(path) != record["common_dir"]:
        raise LaneError(f"adopt refused: {path} is not a worktree of {main_checkout(record['common_dir'])}")
    actual = current_branch(path)
    if actual != branch:
        raise LaneError(f"adopt refused: {path} is on {actual or 'a detached HEAD'}, not {branch}")
    for other in state.lanes():
        if other["id"] != record["id"] and other["status"] != "done" and other.get("worktree") == path:
            raise LaneError(f"adopt refused: lane {other['id']} owns {path}")
    now = journal.now()
    record.update(worktree=path, branch=branch, status="active" if record["status"] == "planned" else record["status"],
                  adopted=now)
    state.record_agent(record, "adopt", events.agent_context(agent), now=now)
    state.save(record)
    journal.ledger(f"adopt {record['id']} worktree={path} branch={branch}", kind="lane.adopted", lane=record,
                   details={"worktree": path, "branch": branch, "status": record["status"]})


def adopt(args: Any) -> int:
    with state.locked("state"):
        record = state.load(args.id)
        if record["status"] not in ("planned", "active"):
            raise LaneError(f"adopt refused: lane {args.id} is {record['status']}")
        adopt_worktree(record, args.worktree, args.branch, agent=getattr(args, "agent", None))
    print(f"Lane {args.id} adopted {record['worktree']} on {args.branch}")
    print("\n".join(next_lines(record)))
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
    store = state.Store(config.current())
    records = store.lanes(strict=True)
    shown = [r for r in records if args.all or r["status"] != "done"]
    decision = admission.evaluate(config.current(), records, admission.running_common(config.current()))
    cap, source, machine = decision.cap, decision.source, decision.machine
    ceiling = decision.ceiling
    # Each run's effective slot priority and its source (lanefather#24).
    priorities = slots.priorities()
    if args.json:
        # Scripts read lanes here (#577): every record with its worktree (null until adopt) and receipt state.
        print(json.dumps({"lanes": [dict(r, receipt=receipt_state(r)) for r in shown],
                          "active": len(decision.occupying), "cap": cap, "cap_source": source,
                          "global": {"active": len(decision.everywhere), "cap": ceiling[0] if ceiling else None,
                                     "cap_source": ceiling[1] if ceiling else None},
                          "host": machine, "priorities": priorities}, indent=2))
        return 0
    for record in shown:
        print(f"{record['id']}  {record['status']}  #{record['ticket']}  {record['branch']}  "
              f"receipt: {receipt_state(record)}")
        print(f"    claims: {' '.join(record['claims'])}"
              + (f"  after: {', '.join(record['after'])}" if record["after"] else ""))
        if record.get("worktree"):
            print(f"    worktree: {record['worktree']}")
    across = f"{len(decision.everywhere)} of {ceiling[0]} across projects ({ceiling[1]}); " if ceiling else ""
    print(f"{len(decision.occupying)} of {cap} lanes planned or active (cap {source}); {across}"
          f"{host.host_line(machine, decision.floor)}")
    print("slot priorities: " + ", ".join(
        f"{phase} {found['priority']}" + ("" if found["source"] == "built-in" else f" ({found['source']})")
        for phase, found in priorities.items()))
    # #666: the repository it runs in, as last fetched; outside a Git checkout there is nothing to compare.
    if git_ok(os.getcwd(), "rev-parse", "--git-dir"):
        for line in promote.ahead_lines(os.getcwd()):
            print(line)
    pending = list(review.unmarked_reviews(records, store))
    label = "review" if len(pending) == 1 else "reviews"
    references = [f"{lane['id']}#{finding['n']}" for lane, findings in pending for finding in findings]
    summary = f"{len(pending)} finished {label} with unmarked findings"
    print(f"{summary}: {', '.join(references)}" if references else summary)
    return 0
