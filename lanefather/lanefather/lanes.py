"""Registering lanes: start, next, adopt and list.

Statuses: planned (registered), active (has a worktree), landed (merged), released (merged with a green smoke while
its agent still locks the worktree), done (cleaned up). Only planned and active lanes count toward the lane cap and
hold their claims, each among the lanes of its own repository (fathers#149). start prints the brief header for the
lane's prompt, ending with the rule snippets whose path globs overlap the lane's claims.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
import json
import os
from pathlib import Path
import sys
from typing import Any, cast

from . import (
    admission,
    claims,
    done,
    events,
    host,
    journal,
    lifecycle,
    lifetimes,
    queue,
    receipts,
    review_record,
    scope,
    slots,
    state,
    tickets,
    worktrees,
)
from .errors import LaneError
from .gitops import ahead_lines, common_dir, current_branch, git, git_ok, main_checkout, worktree_list
from .state import Record


def header(store: state.Store, record: Record, rules: list[dict[str, Any]] | tuple[()] = ()) -> str:
    prog = store.prog
    ticket = f"#{record['ticket']}" if record["ticket"] != "none" else "none (no ticket)"
    raw_targets: Any = record.get("cargo_targets") or []
    targets = [str(t) for t in cast(list[Any], raw_targets)] if isinstance(raw_targets, list) else []
    target_label = store.project.target_label or "Build target"
    items = scope.of(record)
    return "\n".join([
        f"Lane {record['id']}: ticket {ticket}",
        *([f"Scope: {scope.describe(items)}"] if scope.explicit(items) else []),
        f"Branch: {record['branch']} from {record['base']} ({record['base_sha'][:10]})",
        f"Claims: {' '.join(record['claims']) or f'none yet; {prog} check adds the paths the lane changes'}"
        + (f" (lands after {', '.join(record['after'])})" if record["after"] else ""),
        "Worktree: " + (record["worktree"]
                        or f"yours; first run git checkout -B {record['branch']} {record['base']}")
        + ("" if record["worktree"] else f', then {prog} adopt {record["id"]} "$PWD" {record["branch"]}'),
        f"Scratch: {record['scratch'][0]} (evidence logs and probes; {prog} done removes it)",
        *([f"{target_label}, when the lane needs its own: {targets[0]}"] if targets else []),
        *claims.rules_block(list(rules), store.project),
        *next_lines(store, record),
    ])


def next_lines(store: state.Store, record: Record) -> list[str]:
    """The lane's next commands, as start and adopt print them (fathers#175)."""
    prog, lane_id = store.prog, record["id"]
    return [
        "Next, in the worktree:",
        *([f'  git checkout -B {record["branch"]} {record["base"]} && {prog} adopt {lane_id} "$PWD" '
           f'{record["branch"]}'] if not record["worktree"] else []),
        f"  {prog} check {lane_id}    (on a clean committed tree; its receipt is the evidence)",
        f"  Hand back: the block in {store.project.report_doc}, with Receipt: {receipts.path(store, lane_id)}",
        f"  Then the coordinator lands it ({prog} land {lane_id}); {prog} wait {lane_id} --for landed blocks until "
        "it does, no loop over list.",
    ]


def prepare(store: state.Store, cwd: Any, lane_id: str, ticket: str, lane_claims: list[str], base: str | None,
            after: list[str], branch: str | None = None, scratch: Iterable[str] = (), *,
            parents: list[str] | None = None, batch_fix: bool = False,
            items: Sequence[scope.Item] | None = None) -> tuple[Record, list[dict[str, Any]]]:
    """The record start registers, and the rules its claims touch; nothing is written. `items` is the lane's
    accepted scope (lanefather.scope); without it the scope is `ticket` alone."""
    chosen = tuple(items) if items is not None else scope.from_ticket(str(ticket))
    store.record_path(lane_id)
    for claim in lane_claims:
        if claim.startswith("/") or ".." in claim.split("/"):
            raise LaneError(f"claim {claim}: give a glob relative to the repository")
    common = common_dir(cwd)
    base = base or current_branch(cwd) or git(cwd, "rev-parse", "HEAD")
    base_sha = git(cwd, "rev-parse", "--verify", base + "^{commit}")
    integration = next((w["path"] for w in worktree_list(cwd) if w.get("branch") == base), None)
    rules = claims.rules_for(lane_claims, git(cwd, "rev-parse", "--show-toplevel"), store.project)
    record: Record = {
        "id": lane_id, "ticket": scope.primary(chosen), "branch": branch or f"lane/{lane_id}",
        "base": base, "base_sha": base_sha, "claims": lane_claims, "after": after, "common_dir": common,
        "integration": integration, "worktree": None, "status": "planned", "created": journal.now(),
        "scratch": [str(store.directory / f"{lane_id}.scratch"), *(os.path.realpath(s) for s in scratch)],
        "cargo_targets": worktrees.lane_targets(lane_id, store.project), "overrides": [], "agents": [],
        "rules": [rule["name"] for rule in rules]}
    scope.write(record, chosen)
    state.set_fix_parents(record, parents or [], batch=batch_fix)
    return record, rules


def fix_parent(store: state.Store, record: Record) -> str | None:
    """The landed lane whose kept worktree a fix lane (review --fix-lane) takes over, else None."""
    parent_id = state.hand_over_parent(record)
    if parent_id is None:
        return None
    parent = state.read_json(store.record_path(parent_id))
    if (parent and lifecycle.awaiting_done(parent) and parent.get("worktree")
            and parent.get("worktree") == record.get("worktree")):
        return parent_id
    return None


def admit(store: state.Store, record: Record, force_reason: str | None,
          repaired: str | None) -> tuple[admission.Decision, list[str]]:
    """Under the state lock: the existing-lane, dependency, cap, disk and claim rules for registration."""
    prog = store.prog
    lane_id, lane_claims, after = record["id"], record["claims"], record["after"]
    existing = state.read_json(store.record_path(lane_id))
    if existing and not lifecycle.finished(existing):
        raise LaneError(f"lane {lane_id} exists ({existing['status']}); finish it with {prog} done")
    others = [r for r in store.lanes(strict=True) if r["id"] != lane_id]
    for name in after:
        if name not in {r["id"] for r in others if not lifecycle.finished(r)}:
            raise LaneError(f"--after {name}: no lane that is not done by that name")
    for fix_id in state.fix_parents(record):
        prefix = f"--fix-of {fix_id}" if state.batch_fix(record) else "hand-over refused"
        review_record.ReviewRecord.fix_parent(store, fix_id, record["common_dir"], prefix)
    parent = fix_parent(store, record)
    decision = admission.evaluate(store.project, others, record["common_dir"], cap_exempt=parent is not None,
                                  repaired=repaired)
    refusals = list(decision.refusals)
    if refusals and not force_reason and not (decision.disk_short and len(refusals) == 1):
        raise LaneError("refused:\n- " + "\n- ".join(refusals))
    for other in decision.occupying:
        pairs = [] if other["id"] in after else claims.overlapping(lane_claims, other, project=store.project)
        if pairs:
            listed = ", ".join(f"{m} with {t}" for m, t in pairs)
            raise LaneError(f"claims overlap lane {other['id']} (#{other['ticket']}): {listed}. Run the lanes "
                            f"in sequence: start this one with --after {other['id']}")
    return decision, refusals


def register(store: state.Store, record: Record, rules: list[dict[str, Any]], force_reason: str | None,
             create: bool, *, agent: str | None = None) -> None:
    """Enforce the cap, disk and claims, save the record and print its brief header."""
    repaired: str | None = None
    while True:
        with store.locked("state"):
            decision, refusals = admit(store, record, force_reason, repaired)
            if not refusals or force_reason or repaired is not None:
                if refusals and not force_reason:
                    raise LaneError("refused:\n- " + "\n- ".join(refusals))
                register_admitted(store, record, decision, refusals, force_reason, create, agent=agent)
                break
        repaired = done.repair_disk(decision.machine["disk_free_gb"], decision.floor)
    print(header(store, record, rules))
    machine = decision.machine
    if machine["load15"] > machine["load_limit"]:
        print(f"lane: load15 is {machine['load15']:.1f}, above the advisory limit of {machine['load_limit']:.1f}. "
              "Checks may wait for a Slotfather slot.", file=sys.stderr)
    warning = tickets.no_ticket_warning(record, store.project)
    if warning:
        print(warning, file=sys.stderr)


def register_admitted(store: state.Store, record: Record, decision: admission.Decision, refusals: list[str],
                      force_reason: str | None, create: bool, *, agent: str | None = None) -> None:
    """Under the state lock, once admitted: save the record and log the start."""
    lane_id, lane_claims, after = record["id"], record["claims"], record["after"]
    cap, source, info = decision.cap, decision.source, decision.machine
    parent = fix_parent(store, record)
    if parent is not None:
        source = record["cap_source"] = f"fix-lane of {parent}"
    store.directory.mkdir(parents=True, exist_ok=True)
    Path(record["scratch"][0]).mkdir(parents=True, exist_ok=True)
    if refusals:
        record["overrides"].append({"at": journal.now(), "reason": force_reason, "refusals": refusals})
        journal.ledger(f"override start {lane_id}: {force_reason} (refusals: {'; '.join(refusals)})",
                       kind="override", level="warn", lane=record,
                       details={"command": "start", "reason": force_reason, "refusals": refusals},
                       store=store)
    worktree: str | None = record.get("worktree")
    status = "planned"
    if create:
        main = main_checkout(record["common_dir"])
        path = str(worktrees.worktrees_root(record["common_dir"], store.project) / f"lane-{lane_id}")
        git(main, "worktree", "add", "-q", "-b", record["branch"], path, record["base_sha"])
        worktree = os.path.realpath(path)
        status = "active"
    lifecycle.register(
        store, record, status=status, worktree=worktree,
        agent=events.agent_context(agent), now=journal.now(),
        ledger_message=(f"start {lane_id} ticket={record['ticket']} branch={record['branch']} "
                        f"base={record['base']}@{record['base_sha'][:10]} claims={' '.join(lane_claims)} "
                        f"after={','.join(after) or '-'} cap={cap} ({source}) {host.host_fields(info)}"),
        ledger_details={"status": status, "branch": record["branch"], "base": record["base"],
                        "base_sha": record["base_sha"], "claims": lane_claims, "after": after,
                        "worktree": worktree, "cap": cap, "cap_source": source,
                        **({"scope": record["scope"]} if "scope" in record else {})},
    )
    # fathers#171: the lane owns its worktree, scratch and build directories through Spawnfather; a fix lane takes
    # its parent's worktree by hand-over (hand_over_fix_lane).
    lifetimes.register_quietly(store, store.load(lane_id), "start", skip=("worktree",) if parent is not None else ())


def start(args: Any, store: state.Store) -> int:
    lane_claims = [c for claim in args.claims for c in claim.split()]
    given = getattr(args, "fix_of", None) or ()
    parents = list(dict.fromkeys(name for value in given for name in value.replace(",", " ").split()))
    items = scope.from_options(cast(list[tuple[str, bool]], getattr(args, "issues", None) or []),
                               option_list(getattr(args, "decision", None)), option_list(getattr(args, "spec", None)))
    record, rules = prepare(store, os.getcwd(), args.id, scope.primary(items), lane_claims, args.base, args.after,
                            args.branch, args.scratch, parents=parents, batch_fix=True, items=items)
    register(store, record, rules, args.force_reason, args.create, agent=getattr(args, "agent", None))
    if not parents and "review-fixes" in args.id:
        print(f"{store.prog}: warning: {args.id} looks like a fix lane but names no parent, so its land starts "
              f"a review of the fix; to exempt it, {store.prog} drop {args.id} and start it again with "
              "--fix-of <lane>,<lane>", file=sys.stderr)
    return 0


def next_lane(args: Any, store: state.Store) -> int:
    data, ready, states = queue.next_ready(store, common_dir(os.getcwd()))
    for entry, reason in states:
        if reason and entry is not ready:
            print(f"  skip {entry['id']} (#{str(entry['ticket']).lstrip('#')}): {reason}", file=sys.stderr)
    if ready is None:
        print(f"No ready entry in {queue.queue_path(store)}", file=sys.stderr)
        return 3
    record, rules = prepare(store, os.getcwd(), ready["id"], ready["ticket"], list(ready["claims"]),
                            args.base or data.get("base"), [])
    if args.start:
        register(store, record, rules, args.force_reason, args.create)
    else:
        print(header(store, record, rules))
    if isinstance(ready.get("brief"), str) and ready["brief"].strip():
        print("Brief:\n" + ready["brief"].strip())
    return 0


def adopt_worktree(store: state.Store, record: Record, worktree: str, branch: str, *,
                   agent: str | None = None) -> Record:
    path = os.path.realpath(worktree)
    if not os.path.isdir(path) or common_dir(path) != record["common_dir"]:
        raise LaneError(f"adopt refused: {path} is not a worktree of {main_checkout(record['common_dir'])}")
    actual = current_branch(path)
    if actual != branch:
        raise LaneError(f"adopt refused: {path} is on {actual or 'a detached HEAD'}, not {branch}")
    with store.locked("state"):
        for other in store.lanes():
            if other["id"] != record["id"] and not lifecycle.finished(other) and other.get("worktree") == path:
                raise LaneError(f"adopt refused: lane {other['id']} owns {path}")
        now = journal.now()
        updated = lifecycle.transition(
            store, record["id"], "adopt",
            worktree=path, branch=branch, adopted=now,
            agent=events.agent_context(agent), agent_now=now,
            ledger_message=f"adopt {record['id']} worktree={path} branch={branch}",
            ledger_details={"worktree": path, "branch": branch},
        )
    record.clear()
    record.update(updated)
    lifetimes.register_quietly(store, updated, "adopt")
    return updated


def hand_over_fix_lane(
    store: state.Store,
    record: Record,
    fix_id: str,
    fix_paths: list[str],
    base: str,
    force_reason: str | None,
) -> tuple[Record, Record]:
    """Register a follow-up fix lane taking over `record`'s landed worktree and transition `record` via `hand_over`."""
    main = main_checkout(record["common_dir"])
    # The fix lane does part of its parent's tickets: its merge says Refs, never a second Fixes (#64).
    fix, rules = prepare(store, main, fix_id, record["ticket"], fix_paths, base, [], parents=[record["id"]],
                         items=scope.as_partial(scope.of(record)))
    fix["worktree"] = record["worktree"]
    register(store, fix, rules, force_reason, False)
    updated_parent = lifecycle.transition(
        store,
        record["id"],
        "hand_over",
        worktree=None,
        handed_over={"to": fix_id, "worktree": fix["worktree"], "at": journal.now()},
    )
    lifetimes.hand_worktree(store, record, store.load(fix_id))
    return fix, updated_parent


def adopt(args: Any, store: state.Store) -> int:
    with store.locked("state"):
        record = store.load(args.id)
        if not lifecycle.occupying(record):
            raise LaneError(f"adopt refused: lane {args.id} is {record['status']}")
        adopt_worktree(store, record, args.worktree, args.branch, agent=getattr(args, "agent", None))
    print(f"Lane {args.id} adopted {record['worktree']} on {args.branch}")
    print("\n".join(next_lines(store, record)))
    return 0


def scope_lines(record: Record) -> list[str]:
    items = scope.of(record)
    return [f"Lane {record['id']}: scope {scope.describe(items)} (primary ticket "
            f"{'#' + record['ticket'] if record['ticket'] != scope.NONE else 'none'})",
            *(f"  {n}. {scope.entry_line(item)}" for n, item in enumerate(items, 1))]


def change_scope(store: state.Store, lane_id: str, marks: list[tuple[str, bool]],
                 additions: list[scope.Item]) -> tuple[tuple[scope.Item, ...], tuple[scope.Item, ...], Record]:
    """Under the record's lock: mark tickets partial or complete, then append `additions`. A lane that landed has
    already named its tickets in its merge, so only a planned or active lane's scope changes."""
    seen: dict[str, tuple[scope.Item, ...]] = {}

    def change(record: Record) -> bool:
        if not lifecycle.occupying(record):
            raise LaneError(f"scope refused: lane {lane_id} is {record['status']}; its merge already named its "
                            "tickets")
        before = scope.of(record)
        current = list(before)
        for number, partial in marks:
            index = next((i for i, item in enumerate(current)
                          if isinstance(item, scope.Issue) and item.number == number), None)
            if index is None:
                raise LaneError(f"scope refused: lane {lane_id} has no ticket #{number}; add it with --add-ticket "
                                "or --add-partial")
            current[index] = scope.Issue(number, partial)
        after = (*current, *additions)
        scope.validate(after)
        seen.update(before=before, after=after)
        if after == before:
            return False
        scope.write(record, after)
        return True

    record = store.update(lane_id, change)
    return seen["before"], seen["after"], record


def option_list(value: Any) -> list[str]:
    """An `append` option's values as strings, none when it was not given."""
    return [str(item) for item in cast(list[Any], value)] if isinstance(value, list) else []


def scope_command(args: Any, store: state.Store) -> int:
    """Print a lane's accepted scope, or change it (lane.scope_changed): owner answers arrive mid-lane."""
    given = cast(list[tuple[str, bool]], getattr(args, "add_issues", None) or [])
    additions: list[scope.Item] = [scope.Issue(scope.ticket_number(value), partial) for value, partial in given]
    additions += [scope.parse_decision(url) for url in option_list(args.add_decision)]
    additions += [scope.parse_spec(path) for path in option_list(args.add_spec)]
    partial_marks = [scope.ticket_number(n) for n in option_list(args.mark_partial)]
    complete_marks = [scope.ticket_number(n) for n in option_list(args.mark_complete)]
    both = sorted(set(partial_marks) & set(complete_marks))
    if both:
        raise LaneError(f"cannot pass both --mark-partial and --mark-complete for #{both[0]}")
    marks = [(n, True) for n in partial_marks] + [(n, False) for n in complete_marks]
    if additions or marks:
        before, after, record = change_scope(store, args.id, marks, additions)
        if after != before:
            journal.ledger(f"scope {args.id}: {scope.describe(before)} -> {scope.describe(after)} "
                           f"ticket={record['ticket']}", kind="lane.scope_changed", lane=record,
                           details={"before": [scope.to_json(i) for i in before],
                                    "after": [scope.to_json(i) for i in after], "ticket": record["ticket"]},
                           store=store)
    else:
        record = store.load(args.id)
    if args.json:
        print(json.dumps({"v": 1, "lane": record["id"], "ticket": record["ticket"],
                          "scope": [scope.to_json(item) for item in scope.of(record)]}, indent=2))
    else:
        print("\n".join(scope_lines(record)))
    return 0


def list_lanes(args: Any, store: state.Store) -> int:
    records = store.lanes(strict=True)
    shown = [r for r in records if args.all or not lifecycle.finished(r)]
    decision = admission.evaluate(store.project, records, admission.running_common(store.project))
    cap, source, machine = decision.cap, decision.source, decision.machine
    ceiling = decision.ceiling
    priorities = slots.priorities(store.project)
    views = [(r, receipts.lane_view(store, r, repository=None, prog=store.prog)) for r in shown]
    if args.json:
        print(json.dumps({"lanes": [view.for_list(r) for r, view in views],
                          "active": len(decision.occupying), "cap": cap, "cap_source": source,
                          "global": {"active": len(decision.everywhere), "cap": ceiling[0] if ceiling else None,
                                     "cap_source": ceiling[1] if ceiling else None},
                          "host": machine, "priorities": priorities}, indent=2))
        return 0
    for record, view in views:
        print(f"{record['id']}  {record['status']}  #{record['ticket']}  {record['branch']}  "
              f"receipt: {view.list_receipt_text()}")
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
    if git_ok(os.getcwd(), "rev-parse", "--git-dir"):
        for line in ahead_lines(os.getcwd(), project=store.project):
            print(line)
    pending = list(review_record.ReviewRecord.unmarked(store, records))
    label = "review" if len(pending) == 1 else "reviews"
    references = [f"{lane['id']}#{finding['n']}" for lane, findings in pending for finding in findings]
    summary = f"{len(pending)} finished {label} with unmarked findings"
    print(f"{summary}: {', '.join(references)}" if references else summary)
    return 0
