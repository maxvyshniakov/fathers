"""Registering lanes: start, next, adopt and list.

Statuses: planned (registered), active (has a worktree), landed (merged), released (merged with a green smoke while
its agent still locks the worktree), done (cleaned up). Only planned and active lanes count toward the lane cap and
hold their claims, each among the lanes of its own repository (fathers#149). start prints the brief header for the
lane's prompt, ending with the rule snippets whose path globs overlap the lane's claims.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
import datetime
import json
import os
from pathlib import Path
import shlex
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
    review_class,
    review_record,
    scope,
    slots,
    state,
    tickets,
    worktrees,
)
from .errors import LaneError, LaneUsageError
from .gitops import ahead_lines, common_dir, current_branch, git, git_ok, main_checkout, worktree_list
from .state import Record


def header(store: state.Store, record: Record, rules: list[dict[str, Any]] | tuple[()] = ()) -> str:
    prog = store.prog
    ticket = scope.display_ticket(record["ticket"]) if record["ticket"] != "none" else "none (no ticket)"
    raw_targets: Any = record.get("cargo_targets") or []
    targets = [str(t) for t in cast(list[Any], raw_targets)] if isinstance(raw_targets, list) else []
    target_label = store.project.target_label or "Build target"
    items = scope.of(record)
    declared_review = review_class.declared_class(record)
    return "\n".join([
        f"Lane {record['id']}: ticket {ticket}",
        *([f"Scope: {scope.describe(items)}"] if scope.explicit(items) else []),
        *([f"Review: {declared_review}"] if declared_review else []),
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
            items: Sequence[scope.Item] | None = None,
            declared_review: str | None = None,
            review_decision: str | None = None,
            review_gate: str | None = None,
            review_reason: str | None = None) -> tuple[Record, list[dict[str, Any]]]:
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
    if declared_review is not None:
        if declared_review not in review_class.CLASS_RANK:
            raise LaneError(f"--review {declared_review!r}: choose one of {', '.join(review_class.REVIEW_CLASSES)}")
        record["review_class"] = declared_review
    evidence: dict[str, str] = {}
    if isinstance(review_decision, str) and review_decision.strip():
        evidence["decision"] = review_decision.strip()
    if isinstance(review_gate, str) and review_gate.strip():
        evidence["gate"] = review_gate.strip()
    if evidence:
        record["review_evidence"] = evidence
    if isinstance(review_reason, str) and review_reason.strip():
        record["review_reason"] = review_reason.strip()
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


def existing_lane_error(prog: str, lane_id: str, existing: Record) -> LaneError:
    """Refusal when registering an id that already exists and is not done (or is parked), with a fitting hint."""
    if lifecycle.parked(existing):
        return LaneError(f"lane {lane_id} exists (parked); resume it with {prog} unpark {lane_id}")
    if lifecycle.planned(existing):
        return LaneError(
            f"lane {lane_id} exists (planned); activate it with {prog} start {lane_id} --create "
            f"or remove it with {prog} drop {lane_id}"
        )
    if lifecycle.active(existing):
        return LaneError(
            f"lane {lane_id} exists (active); land it with {prog} land {lane_id} "
            f"or remove it with {prog} drop {lane_id}"
        )
    return LaneError(f"lane {lane_id} exists ({existing['status']}); finish it with {prog} done {lane_id}")


def admit(store: state.Store, record: Record, force_reason: str | None,
          repaired: str | None, *, allow_parked: bool = False) -> tuple[admission.Decision, list[str]]:
    """Under the state lock: the existing-lane, dependency, cap, disk and claim rules for registration."""
    prog = store.prog
    lane_id, lane_claims, after = record["id"], record["claims"], record["after"]
    existing = state.read_json(store.record_path(lane_id))
    if existing and (not lifecycle.finished(existing) or (lifecycle.parked(existing) and not allow_parked)):
        raise existing_lane_error(prog, lane_id, existing)
    others = [r for r in store.lanes(strict=True) if r["id"] != lane_id]
    others_by_id = {r["id"]: r for r in others}
    for name in after:
        dep = others_by_id.get(name)
        if dep is None or lifecycle.finished(dep):
            if lifecycle.parked(dep):
                raise LaneError(f"--after {name}: lane {name} is parked (unpark it with {prog} unpark {name} first)")
            if lifecycle.abandoned(dep):
                raise LaneError(f"--after {name}: lane {name} is abandoned and will not land")
            raise LaneError(f"--after {name}: no lane that is not done by that name")
    for fix_id in state.fix_parents(record):
        prefix = f"--fix-of {fix_id}" if state.batch_fix(record) else "hand-over refused"
        review_record.ReviewRecord.fix_parent(store, fix_id, record["common_dir"], prefix)
    parent = fix_parent(store, record)
    decision = admission.evaluate(store.project, others, record["common_dir"], cap_exempt=parent is not None,
                                  repaired=repaired)
    refusals = list(decision.refusals)
    if not state.fix_parents(record) and "review-fixes" in lane_id:
        refusals.append(
            f"lane {lane_id} looks like a fix lane but names no parent; "
            f"start it with --fix-of <lane> (or use {prog} review <lane> --fix-lane)"
        )
    if refusals and not force_reason and not (decision.disk_short and len(refusals) == 1):
        raise LaneError("refused:\n- " + "\n- ".join(refusals))
    for other in decision.occupying:
        pairs = [] if other["id"] in after else claims.overlapping(lane_claims, other, project=store.project)
        if pairs:
            listed = ", ".join(f"{m} with {t}" for m, t in pairs)
            if admission.gone(other):
                # fathers#237: --after a lane that can never land would wait forever.
                raise LaneError(f"claims overlap lane {other['id']} ({scope.display_ticket(other['ticket'])}): "
                                f"{listed}. " + admission.drop_advice(prog, [other["id"]], "its claims"))
            raise LaneError(f"claims overlap lane {other['id']} ({scope.display_ticket(other['ticket'])}): {listed}. "
                            f"Run the lanes in sequence: start this one with --after {other['id']}")
    return decision, refusals


def register(store: state.Store, record: Record, rules: list[dict[str, Any]], force_reason: str | None,
             create: bool, *, agent: str | None = None, start_point: str | None = None,
             extra_ledger_details: dict[str, Any] | None = None, allow_parked: bool = False) -> None:
    """Enforce the cap, disk and claims, save the record and print its brief header."""
    repaired: str | None = None
    while True:
        with store.locked("state"):
            decision, refusals = admit(store, record, force_reason, repaired, allow_parked=allow_parked)
            if not refusals or force_reason or repaired is not None:
                if refusals and not force_reason:
                    raise LaneError("refused:\n- " + "\n- ".join(refusals))
                register_admitted(
                    store, record, decision, refusals, force_reason, create, agent=agent,
                    start_point=start_point, extra_ledger_details=extra_ledger_details,
                )
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
                      force_reason: str | None, create: bool, *, agent: str | None = None,
                      start_point: str | None = None,
                      extra_ledger_details: dict[str, Any] | None = None) -> None:
    """Under the state lock, once admitted: save the record and log the start."""
    lane_id, lane_claims, after = record["id"], record["claims"], record["after"]
    prev_record = state.read_json(store.record_path(lane_id))
    prev_created = state.lane_created_of(prev_record)
    if prev_created is not None and isinstance(record.get("created"), str) and record["created"] <= prev_created:
        try:
            bumped = journal.parse_time(prev_created) + datetime.timedelta(seconds=1)
            record["created"] = bumped.strftime(journal.LEDGER_TIME)
        except ValueError:
            pass
    cap, source, info = decision.cap, decision.source, decision.machine
    parent = fix_parent(store, record)
    if parent is not None:
        source = record["cap_source"] = f"fix-lane of {parent}"
    store.directory.mkdir(parents=True, exist_ok=True)
    Path(record["scratch"][0]).mkdir(parents=True, exist_ok=True)
    if refusals:
        journal.override("start", record, force_reason, refusals, described=f"refusals: {'; '.join(refusals)}",
                         store=store)
    worktree: str | None = record.get("worktree")
    status = "planned"
    created_main: str | None = None
    created_path: Path | None = None
    if create:
        created_main = main_checkout(record["common_dir"])
        created_path = worktrees.worktrees_root(record["common_dir"], store.project) / f"lane-{lane_id}"
        git(created_main, "worktree", "add", "-q", "-b", record["branch"], str(created_path),
            start_point or record["base_sha"])
        worktree = os.path.realpath(str(created_path))
        status = "active"
    try:
        lifecycle.register(
            store, record, status=status, worktree=worktree,
            agent=events.agent_context(agent), now=journal.now(),
            ledger_message=(f"start {lane_id} ticket={record['ticket']} branch={record['branch']} "
                            f"base={record['base']}@{record['base_sha'][:10]} claims={' '.join(lane_claims)} "
                            f"after={','.join(after) or '-'} cap={cap} ({source}) {host.host_fields(info)}"),
            ledger_details={"status": status, "branch": record["branch"], "base": record["base"],
                            "base_sha": record["base_sha"], "claims": lane_claims, "after": after,
                            "worktree": worktree, "cap": cap, "cap_source": source,
                            **({"scope": record["scope"]} if "scope" in record else {}),
                            **(extra_ledger_details or {})},
        )
    except BaseException:
        if created_main is not None and created_path is not None:
            worktrees.remove_checkout(created_main, created_path)
            git(created_main, "branch", "-D", record["branch"], check=False)
        raise
    # fathers#171: the lane owns its worktree, scratch and build directories through Spawnfather; a fix lane takes
    # its parent's worktree by hand-over (hand_over_fix_lane).
    lifetimes.register_quietly(store, store.load(lane_id), "start", skip=("worktree",) if parent is not None else ())


def check_activation_options(prog: str, lane_id: str, record: Record, args: Any) -> None:
    """Refuse registration-only options on `start <id> --create` when they differ from the planned lane's record."""
    raw_issues = cast(list[tuple[str, bool]], getattr(args, "issues", None) or [])
    raw_decisions = option_list(getattr(args, "decision", None))
    raw_specs = option_list(getattr(args, "spec", None))
    stored_scope = scope.of(record)
    if raw_issues:
        given_scope = scope.from_options(raw_issues, raw_decisions, raw_specs)
        if given_scope != stored_scope:
            raise LaneError(
                f"start {lane_id} --create: scope ({scope.describe(given_scope)}) differs from planned lane's scope "
                f"({scope.describe(stored_scope)}); omit scope options or update with {prog} scope {lane_id}"
            )
    elif raw_decisions or raw_specs:
        extras: list[scope.Item] = [*(scope.parse_decision(u) for u in raw_decisions),
                                    *(scope.parse_spec(p) for p in raw_specs)]
        if any(item not in stored_scope for item in extras):
            which = (
                "--decision" if raw_decisions and not raw_specs
                else "--spec" if raw_specs and not raw_decisions
                else "--decision/--spec"
            )
            raise LaneError(
                f"start {lane_id} --create: {which} differs from planned lane's scope; "
                f"update it with {prog} scope {lane_id}"
            )
    raw_claims = option_list(getattr(args, "claims", None))
    if raw_claims:
        given_claims = [c for claim in raw_claims for c in claim.split()]
        raw_stored_claims: Any = record.get("claims")
        stored_claims = (
            [c for c in cast(list[Any], raw_stored_claims) if isinstance(c, str)]
            if isinstance(raw_stored_claims, list)
            else []
        )
        if given_claims != stored_claims:
            raise LaneError(
                f"start {lane_id} --create: --claims ({' '.join(given_claims)}) differs from planned lane's claims "
                f"({' '.join(stored_claims)}); omit --claims to keep the planned lane's claims"
            )
    given_after = list(dict.fromkeys(option_list(getattr(args, "after", None))))
    if given_after:
        raw_stored_after: Any = record.get("after")
        stored_after = (
            [name for name in cast(list[Any], raw_stored_after) if isinstance(name, str)]
            if isinstance(raw_stored_after, list)
            else []
        )
        if given_after != stored_after:
            raise LaneError(
                f"start {lane_id} --create: --after ({', '.join(given_after)}) differs from planned lane's --after "
                f"({', '.join(stored_after) or 'none'}); omit --after to keep the planned lane's dependencies"
            )
    given_fix = option_list(getattr(args, "fix_of", None))
    if given_fix:
        parents = list(dict.fromkeys(name for value in given_fix for name in value.replace(",", " ").split()))
        stored_parents = state.fix_parents(record)
        if parents != stored_parents:
            raise LaneError(
                f"start {lane_id} --create: --fix-of ({', '.join(parents)}) differs from planned lane's fix parents "
                f"({', '.join(stored_parents) or 'none'})"
            )
    given_scratch = option_list(getattr(args, "scratch", None))
    if given_scratch:
        raw_stored_scratch: Any = record.get("scratch")
        stored_scratch = (
            [os.path.realpath(s) for s in cast(list[Any], raw_stored_scratch)[1:] if isinstance(s, str)]
            if isinstance(raw_stored_scratch, list)
            else []
        )
        if [os.path.realpath(s) for s in given_scratch] != stored_scratch:
            raise LaneError(
                f"start {lane_id} --create: --scratch differs from planned lane's scratch directories; "
                "omit --scratch to keep them"
            )
    given_review = getattr(args, "review", None)
    stored_review = review_class.declared_class(record)
    if given_review is not None and stored_review is not None and given_review != stored_review:
        raise LaneError(
            f"start {lane_id} --create: --review ({given_review}) differs from planned lane's review class "
            f"({stored_review})"
        )
    stored_dec, stored_gate = review_class.evidence_of(record)
    given_dec = getattr(args, "review_decision", None)
    if isinstance(given_dec, str) and given_dec.strip() and stored_dec is not None and given_dec.strip() != stored_dec:
        raise LaneError(
            f"start {lane_id} --create: --review-decision differs from planned lane's decision; "
            f"update it with {prog} scope {lane_id}"
        )
    given_gate = getattr(args, "gate", None)
    if (
        isinstance(given_gate, str)
        and given_gate.strip()
        and stored_gate is not None
        and given_gate.strip() != stored_gate
    ):
        raise LaneError(
            f"start {lane_id} --create: --gate ({given_gate.strip()}) differs from planned lane's gate "
            f"({stored_gate}); update it with {prog} scope {lane_id}"
        )


def _apply_activation_review_opts(
    rec: Record,
    new_review: Any,
    new_dec: Any,
    new_gate: Any,
    new_reason: Any,
) -> bool:
    changed = False
    if isinstance(new_review, str) and new_review in review_class.CLASS_RANK and "review_class" not in rec:
        rec["review_class"] = new_review
        changed = True
    raw_ev: Any = rec.get("review_evidence")
    ev = dict(cast(dict[str, Any], raw_ev)) if isinstance(raw_ev, dict) else {}
    if isinstance(new_dec, str) and new_dec.strip() and "decision" not in ev:
        ev["decision"] = new_dec.strip()
        changed = True
    if isinstance(new_gate, str) and new_gate.strip() and "gate" not in ev:
        ev["gate"] = new_gate.strip()
        changed = True
    if ev and ev != raw_ev:
        rec["review_evidence"] = ev
    if isinstance(new_reason, str) and new_reason.strip() and "review_reason" not in rec:
        rec["review_reason"] = new_reason.strip()
        changed = True
    return changed


def activate_planned(store: state.Store, lane_id: str, *, base: str | None = None,
                     branch: str | None = None, force_reason: str | None = None,
                     agent: str | None = None, args: Any = None) -> Record:
    """Create the worktree for an existing planned lane whose `--after` lanes have landed, transition it to
    `active` while keeping its existing claims and scope, and print its brief header (fathers#241)."""
    prog = store.prog
    repaired: str | None = None
    updated: Record | None = None
    main = ""
    while True:
        with store.locked("state"):
            record = store.load(lane_id)
            if not lifecycle.planned(record):
                raise existing_lane_error(prog, lane_id, record)
            if args is not None:
                check_activation_options(prog, lane_id, record, args)
            theirs = record.get("common_dir")
            here = admission.running_common(store.project)
            if isinstance(theirs, str) and theirs and here and os.path.realpath(theirs) != os.path.realpath(here):
                checkout = main_checkout(theirs)
                raise LaneError(
                    f"lane {lane_id} belongs to {state.project_name(theirs)}, not {state.project_name(here)}; "
                    f"run it there ({checkout}): cd {shlex.quote(checkout)} && {prog} start {lane_id} --create"
                )
            raw_after: Any = record.get("after")
            after_ids = (
                [name for name in cast(list[Any], raw_after) if isinstance(name, str)]
                if isinstance(raw_after, list)
                else []
            )
            all_lanes = store.lanes(strict=True)
            by_id = {r["id"]: r for r in all_lanes}
            waiting = [
                f"{name} ({lifecycle.display_state(by_id.get(name), missing='missing')})"
                for name in after_ids
                if not lifecycle.after_satisfied(by_id.get(name))
            ]
            if waiting:
                has_abandoned = any(lifecycle.abandoned(by_id.get(name)) for name in after_ids)
                if has_abandoned:
                    raise LaneError(
                        f"lane {lane_id} is planned after {', '.join(waiting)}; abandoned dependencies will not land "
                        f"(drop with {prog} drop {lane_id} or abandon with {prog} abandon {lane_id} --reason REASON)"
                    )
                raise LaneError(
                    f"lane {lane_id} is planned after {', '.join(waiting)}; "
                    f"wait for them to land, then run {prog} start {lane_id} --create "
                    f"(or {prog} drop {lane_id} to remove it)"
                )
            common = record.get("common_dir")
            if not isinstance(common, str) or not common or not os.path.isdir(common):
                raise LaneError(f"lane {lane_id} names no repository that exists ({common or 'none'})")
            # The lane already holds a cap slot and its claims; re-check only the free-disk floor before worktree add.
            decision = admission.evaluate(store.project, all_lanes, common, cap_exempt=True, repaired=repaired)
            refusals = list(decision.refusals)
            if refusals and not force_reason and repaired is None and decision.disk_short:
                pass
            else:
                if refusals and not force_reason:
                    raise LaneError("refused:\n- " + "\n- ".join(refusals))
                main = main_checkout(common)
                raw_base = record.get("base")
                chosen_base = base or (raw_base if isinstance(raw_base, str) and raw_base else None)
                if not chosen_base:
                    chosen_base = current_branch(main) or git(main, "rev-parse", "HEAD")
                base_sha = git(main, "rev-parse", "--verify", chosen_base + "^{commit}")
                integration = next((w["path"] for w in worktree_list(main) if w.get("branch") == chosen_base),
                                   record.get("integration"))
                raw_branch = record.get("branch")
                chosen_branch = branch or (raw_branch if isinstance(raw_branch, str) and raw_branch
                                           else f"lane/{lane_id}")
                raw_scratch: Any = record.get("scratch")
                if isinstance(raw_scratch, list) and raw_scratch and isinstance(raw_scratch[0], str) and raw_scratch[0]:
                    Path(raw_scratch[0]).mkdir(parents=True, exist_ok=True)
                path = str(worktrees.worktrees_root(common, store.project) / f"lane-{lane_id}")
                git(main, "worktree", "add", "-q", "-b", chosen_branch, path, base_sha)
                worktree = os.path.realpath(path)
                new_overrides: list[dict[str, Any]] = []
                if refusals:
                    new_overrides.append(
                        journal.override(
                            "start", record, force_reason, refusals,
                            described=f"refusals: {'; '.join(refusals)}", audited=False, store=store,
                        )
                    )
                now = journal.now()
                updated = lifecycle.transition(
                    store, lane_id, "adopt",
                    worktree=worktree,
                    branch=chosen_branch,
                    base=chosen_base,
                    base_sha=base_sha,
                    integration=integration,
                    adopted=now,
                    agent=events.agent_context(agent),
                    agent_now=now,
                    overrides=new_overrides or None,
                    ledger_message=(
                        f"start {lane_id} --create worktree={worktree} branch={chosen_branch} "
                        f"base={chosen_base}@{base_sha[:10]}"
                    ),
                    ledger_details={
                        "worktree": worktree,
                        "branch": chosen_branch,
                        "base": chosen_base,
                        "base_sha": base_sha,
                    },
                )
                if args is not None:
                    new_review = getattr(args, "review", None)
                    new_dec = getattr(args, "review_decision", None)
                    new_gate = getattr(args, "gate", None)
                    new_reason = getattr(args, "review_reason", None)
                    if any(v is not None for v in (new_review, new_dec, new_gate, new_reason)):
                        updated = store.update(
                            lane_id,
                            lambda rec, r=new_review, d=new_dec, g=new_gate, w=new_reason: (
                                _apply_activation_review_opts(rec, r, d, g, w)
                            ),
                        )
                break
        repaired = done.repair_disk(decision.machine["disk_free_gb"], decision.floor)
    assert updated is not None
    lifetimes.register_quietly(store, updated, "adopt")
    raw_claims: Any = updated.get("claims")
    lane_claims = (
        [c for c in cast(list[Any], raw_claims) if isinstance(c, str)]
        if isinstance(raw_claims, list)
        else []
    )
    rules = claims.rules_for(lane_claims, main, store.project)
    print(header(store, updated, rules))
    return updated


def _inherit_partial_scope(store: state.Store, parents: Sequence[str],
                           items: Sequence[scope.Item]) -> tuple[tuple[scope.Item, ...], list[scope.Issue]]:
    """Any ticket of `items` that a fix parent landed partially (via `land --partial` or a partial scope entry)
    stays partial on the follow-up fix lane, so its merge says `Refs` instead of `Fixes` (fathers#64)."""
    if not parents:
        return tuple(items), []
    default_repo = store.project.tickets.repo
    partial_keys: set[str] = set()
    for parent_id in parents:
        parent = state.read_json(store.record_path(parent_id))
        if not isinstance(parent, dict):
            continue
        if not default_repo and isinstance(parent.get("common_dir"), str) and parent["common_dir"]:
            default_repo = tickets.repository(parent["common_dir"], store.project.tickets) or ""
        raw_landed: Any = parent.get("landed")
        landed_partial = isinstance(raw_landed, dict) and cast(dict[str, Any], raw_landed).get("partial") is True
        for issue in scope.issues(parent):
            if landed_partial or issue.partial:
                partial_keys.add(tickets.norm_key(issue.number, default_repo))
    if not partial_keys:
        return tuple(items), []
    updated: list[scope.Item] = []
    inherited: list[scope.Issue] = []
    for item in items:
        if (isinstance(item, scope.Issue) and not item.partial
                and tickets.norm_key(item.number, default_repo) in partial_keys):
            partial_issue = scope.Issue(item.number, True)
            updated.append(partial_issue)
            inherited.append(partial_issue)
        else:
            updated.append(item)
    return tuple(updated), inherited


def start(args: Any, store: state.Store) -> int:
    existing = state.read_json(store.record_path(args.id))
    if existing and (not lifecycle.finished(existing) or lifecycle.parked(existing)):
        if args.create and lifecycle.planned(existing):
            activate_planned(store, args.id, base=args.base, branch=args.branch,
                             force_reason=args.force_reason, agent=getattr(args, "agent", None), args=args)
            return 0
        raise existing_lane_error(store.prog, args.id, existing)
    raw_issues = cast(list[tuple[str, bool]], getattr(args, "issues", None) or [])
    raw_claims = option_list(getattr(args, "claims", None))
    missing = [opt for opt, val in (("--ticket", raw_issues), ("--claims", raw_claims)) if not val]
    if missing:
        raise LaneUsageError(f"the following arguments are required: {', '.join(missing)}")
    lane_claims = [c for claim in raw_claims for c in claim.split()]
    after = list(dict.fromkeys(option_list(getattr(args, "after", None))))
    given = getattr(args, "fix_of", None) or ()
    parents = list(dict.fromkeys(name for value in given for name in value.replace(",", " ").split()))
    items, inherited_partial = _inherit_partial_scope(
        store, parents,
        scope.from_options(raw_issues, option_list(getattr(args, "decision", None)),
                           option_list(getattr(args, "spec", None))),
    )
    record, rules = prepare(
        store, os.getcwd(), args.id, scope.primary(items), lane_claims, args.base, after,
        args.branch, args.scratch, parents=parents, batch_fix=True, items=items,
        declared_review=getattr(args, "review", None),
        review_decision=getattr(args, "review_decision", None),
        review_gate=getattr(args, "gate", None),
        review_reason=getattr(args, "review_reason", None),
    )
    register(store, record, rules, args.force_reason, args.create, agent=getattr(args, "agent", None))
    if inherited_partial:
        refs = ", ".join(issue.ref for issue in inherited_partial)
        print(f"lane: inherited partial scope from {', '.join(parents)} for {refs} (merge will say Refs)",
              file=sys.stderr)
    return 0


def unpark(args: Any, store: state.Store) -> int:
    """`unpark ID [--base REF] [--force-reason REASON] [--agent KIND]` (fathers#57): re-register a parked lane
    from `park/<id>`, preserving its scope, claims and base and creating a fresh worktree."""
    unpark_lane(
        store, args.id, base=args.base, force_reason=args.force_reason, agent=getattr(args, "agent", None),
    )
    return 0


def unpark_lane(store: state.Store, lane_id: str, *, base: str | None = None,
                force_reason: str | None = None, agent: str | None = None) -> Record:
    """Re-register a parked lane from `park/<id>`, preserving scope, claims and base, with a fresh `created`
    timestamp so older receipts are not inherited (fathers#54, fathers#57)."""
    old = store.load(lane_id)
    if not lifecycle.parked(old):
        raise LaneError(f"unpark refused: lane {lane_id} is {lifecycle.display_state(old)}, not parked")
    common = old.get("common_dir")
    if not isinstance(common, str) or not common or not os.path.isdir(common):
        raise LaneError(f"lane {lane_id} names no repository that exists ({common or 'none'})")
    main = main_checkout(common)
    raw_parked: Any = old.get("parked")
    parked_info = cast(dict[str, Any], raw_parked) if isinstance(raw_parked, dict) else {}
    raw_ref = parked_info.get("ref")
    park_ref = raw_ref if isinstance(raw_ref, str) and raw_ref else f"park/{lane_id}"
    park_tip = git(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{park_ref}^{{commit}}", check=False)
    if not park_tip:
        raise LaneError(f"unpark refused: branch {park_ref} does not exist in {main}")
    raw_claims: Any = old.get("claims")
    lane_claims = (
        [c for c in cast(list[Any], raw_claims) if isinstance(c, str) and c]
        if isinstance(raw_claims, list)
        else []
    )
    raw_base = old.get("base")
    chosen_base = base or (raw_base if isinstance(raw_base, str) and raw_base else None)
    raw_branch = old.get("branch")
    chosen_branch = raw_branch if isinstance(raw_branch, str) and raw_branch else f"lane/{lane_id}"
    if git_ok(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{chosen_branch}"):
        raise LaneError(f"unpark refused: branch {chosen_branch} already exists in {main}; remove or rename it first")
    raw_scratch: Any = old.get("scratch")
    extra_scratch = (
        [s for s in cast(list[Any], raw_scratch)[1:] if isinstance(s, str) and s]
        if isinstance(raw_scratch, list)
        else []
    )
    raw_after: Any = old.get("after")
    old_after = (
        [name for name in cast(list[Any], raw_after) if isinstance(name, str) and name]
        if isinstance(raw_after, list)
        else []
    )
    by_id = {r["id"]: r for r in store.lanes(strict=True)}
    remaining_after: list[str] = []
    abandoned_after: list[str] = []
    for name in old_after:
        dep = by_id.get(name)
        if lifecycle.after_satisfied(dep):
            continue
        if lifecycle.abandoned(dep):
            abandoned_after.append(name)
        else:
            remaining_after.append(name)
    if abandoned_after and not force_reason:
        joined = ", ".join(abandoned_after)
        raise LaneError(
            f"unpark refused: lane {lane_id} had --after {joined}, which is abandoned and will not land; "
            "pass --force-reason to unpark without that dependency"
        )
    items = scope.of(old)
    record, rules = prepare(
        store, main, lane_id, scope.primary(items), lane_claims, chosen_base, remaining_after,
        branch=chosen_branch, scratch=extra_scratch,
        parents=state.fix_parents(old), batch_fix=state.batch_fix(old), items=items,
    )
    mb = git(main, "merge-base", park_tip, record["base_sha"], check=False)
    if mb:
        record["base_sha"] = mb
    elif isinstance(old.get("base_sha"), str) and old["base_sha"] and git_ok(
        main, "rev-parse", "--verify", "--quiet", old["base_sha"] + "^{commit}"
    ):
        record["base_sha"] = old["base_sha"]
    if abandoned_after:
        journal.override(
            "unpark", record, force_reason,
            [f"dropped abandoned --after: {', '.join(abandoned_after)}"],
            store=store,
        )
    register(
        store, record, rules, force_reason, create=True, agent=agent,
        start_point=park_tip, extra_ledger_details={"unparked_from": park_ref, "park_tip": park_tip},
        allow_parked=True,
    )
    git(main, "branch", "-D", park_ref, check=False)
    return store.load(lane_id)


def next_lane(args: Any, store: state.Store) -> int:
    data, ready, states = queue.next_ready(store, common_dir(os.getcwd()))
    for entry, reason in states:
        if reason and entry is not ready:
            print(f"  skip {entry['id']} ({scope.display_ticket(entry['ticket'])}): {reason}", file=sys.stderr)
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
            f"{scope.display_ticket(record['ticket'])})",
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
            wanted = scope.Issue(number).key
            index = next((i for i, item in enumerate(current)
                          if isinstance(item, scope.Issue) and item.key == wanted), None)
            if index is None:
                raise LaneError(f"scope refused: lane {lane_id} has no ticket {scope.display_ticket(number)}; "
                                "add it with --add-ticket or --add-partial")
            existing = current[index]
            assert isinstance(existing, scope.Issue)
            current[index] = scope.Issue(existing.number, partial)
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
    complete_keys = {scope.Issue(n).key for n in complete_marks}
    both = [n for n in partial_marks if scope.Issue(n).key in complete_keys]
    if both:
        raise LaneError(f"cannot pass both --mark-partial and --mark-complete for {scope.display_ticket(both[0])}")
    marks = [(n, True) for n in partial_marks] + [(n, False) for n in complete_marks]
    add_gate = getattr(args, "add_gate", None)
    add_review_decision = getattr(args, "add_review_decision", None)
    gate_val = add_gate.strip() if isinstance(add_gate, str) and add_gate.strip() else None
    dec_val = (
        add_review_decision.strip()
        if isinstance(add_review_decision, str) and add_review_decision.strip()
        else None
    )
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
    if gate_val is not None or dec_val is not None:
        def _update_evidence(rec: Record) -> bool:
            if not lifecycle.occupying(rec):
                raise LaneError(f"scope refused: lane {args.id} is {rec['status']}; its merge already landed")
            raw_ev: Any = rec.get("review_evidence")
            ev = dict(cast(dict[str, Any], raw_ev)) if isinstance(raw_ev, dict) else {}
            changed = False
            if gate_val is not None and ev.get("gate") != gate_val:
                ev["gate"] = gate_val
                changed = True
            if dec_val is not None and ev.get("decision") != dec_val:
                ev["decision"] = dec_val
                changed = True
            if changed:
                rec["review_evidence"] = ev
            return changed

        record = store.update(args.id, _update_evidence)
    if args.json:
        print(json.dumps({"v": 1, "lane": record["id"], "ticket": record["ticket"],
                          "scope": [scope.to_json(item) for item in scope.of(record)]}, indent=2))
    else:
        print("\n".join(scope_lines(record)))
    return 0


def _common_dir(record: Record) -> str | None:
    value = record.get("common_dir")
    return value if isinstance(value, str) else None


def list_lanes(args: Any, store: state.Store) -> int:
    """The lanes of the repository the command runs in, as the cap counts them (fathers#236); --all-projects every
    lane of the Store, each naming its project."""
    records = store.lanes(strict=True)
    here = admission.running_common(store.project)
    every = getattr(args, "all_projects", False)
    mine = records if every else state.of_project(records, here)
    shown = [r for r in mine if args.all or not lifecycle.finished(r)]
    decision = admission.evaluate(store.project, records, here)
    cap, source, machine = decision.cap, decision.source, decision.machine
    ceiling = decision.ceiling
    priorities = slots.priorities(store.project)
    views = [(r, receipts.lane_view(store, r, repository=None, prog=store.prog), admission.gone(r)) for r in shown]
    stale = [r["id"] for r in decision.occupying if admission.gone(r)]
    pending = list(review_record.ReviewRecord.unmarked(store, mine))
    if args.json:
        print(json.dumps({"lanes": [dict(view.for_list(r), project=state.project_name(_common_dir(r)),
                                         gone=is_gone) for r, view, is_gone in views],
                          "active": len(decision.occupying), "cap": cap, "cap_source": source,
                          "global": {"active": len(decision.everywhere), "cap": ceiling[0] if ceiling else None,
                                     "cap_source": ceiling[1] if ceiling else None},
                          "host": machine, "priorities": priorities,
                          "pending_reviews": [{"lane": lane["id"], "findings": [finding["n"] for finding in findings]}
                                              for lane, findings in pending]}, indent=2))
        return 0
    for record, view, is_gone in views:
        outcome = lifecycle.outcome_of(record)
        state_label = (
            f"{record['status']} ({outcome})"
            if lifecycle.finished(record) and outcome in ("parked", "abandoned")
            else str(record["status"])
        )
        print(f"{record['id']}  {state_label}  {scope.display_ticket(record['ticket'])}  {record['branch']}  "
              f"receipt: {view.list_receipt_text()}"
              + (f"  project: {state.project_name(_common_dir(record))}" if every else "")
              + (f"  gone: {admission.GONE}; {store.prog} drop {record['id']}" if is_gone else ""))
        print(f"    claims: {' '.join(record['claims'])}"
              + (f"  after: {', '.join(record['after'])}" if record["after"] else ""))
        if record.get("worktree"):
            print(f"    worktree: {record['worktree']}")
    across = f"{len(decision.everywhere)} of {ceiling[0]} across projects ({ceiling[1]}); " if ceiling else ""
    print(f"{len(decision.occupying)} of {cap} lanes planned or active (cap {source}); {across}"
          f"{host.host_line(machine, decision.floor)}")
    if stale:
        held = "lane holds the cap and its claims" if len(stale) == 1 else "lanes hold the cap and their claims"
        print(f"{len(stale)} gone {held}: {store.prog} drop {' '.join(stale)} (or {store.prog} gc)")
    print("slot priorities: " + ", ".join(
        f"{phase} {found['priority']}" + ("" if found["source"] == "built-in" else f" ({found['source']})")
        for phase, found in priorities.items()))
    if git_ok(os.getcwd(), "rev-parse", "--git-dir"):
        for line in ahead_lines(os.getcwd(), project=store.project):
            print(line)
    label = "review" if len(pending) == 1 else "reviews"
    references = [f"{lane['id']}#{finding['n']}" for lane, findings in pending for finding in findings]
    summary = f"{len(pending)} finished {label} with unmarked findings"
    print(f"{summary}: {', '.join(references)}" if references else summary)
    return 0
