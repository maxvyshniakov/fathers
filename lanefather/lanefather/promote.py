"""Promote and sync (fathers#73).

promote <sha> runs the ordered promotion steps on <sha> (refusals, batch gate via gate.py, review gate, perf suite,
atomic push + local branch moves under the landing lock, after-promote, release comments, overall review, and
configured install/deploy steps).

sync fetches, then fast-forwards local dev and main to the remote under the landing lock; a branch with commits the
remote lacks is refused, and --drop-local resets it after keeping them on backup/<branch>-<date>[-N] (#666).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import datetime
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from . import (
    config,
    events,
    gate,
    journal,
    plan,
    review,
    review_gate,
    review_record,
    slots,
    state,
    tickets,
    verify,
    worktrees,
)
from .errors import LaneError
from .gate import (
    batch_gate,
    main_base,
    perf,
    perf_suite,
    promote_lock_message,
    run_batch_gate,
    stamped_trees,
    verified,
    verified_why,
)
from .gitops import ahead_lines, commits, common_dir, git, git_ok, move_branch, tracking, worktree_list
from .landing import landing

__all__ = [
    "PromoteRun",
    "after_promote",
    "batch_gate",
    "main_base",
    "perf",
    "perf_suite",
    "plan",
    "promote",
    "promote_lock_message",
    "promote_sha",
    "promote_steps",
    "run_batch_gate",
    "stamped_trees",
    "step_priority",
    "sync",
    "sync_plan",
    "verified",
    "verified_why",
]


@dataclass
class PromoteRun:
    cwd: str
    common: str
    sha: str
    branches: list[str]
    remote: str
    ship: bool = False
    no_push: bool = False
    jobs: int | None = None
    wait: float = 120.0
    force_reason: str | None = None
    install: bool = False
    deploy: str | None = None
    subject: str = ""
    ship_base: str | None = None
    failed_moves: list[str] = field(default_factory=list[str])
    store: state.Store = field(default_factory=lambda: state.Store(config.DEFAULT))


def substitute(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def step_priority(step: config.Step, project: config.Config = config.DEFAULT) -> tuple[str, str]:
    """A leased install or deploy step's priority and its source: the step's own, else the gate's as
    slots.phase_priority resolves it, normal when nothing sets it (lanefather#24)."""
    assert step.lease is not None
    if step.lease.priority:
        return step.lease.priority, f"step {step.name} lease"
    return slots.phase_priority("gate", fallback="normal", project=project)


def _run_config_step(store: state.Store, common: str, sha: str, step: config.Step, skipped: list[str],
                     deploy: str | None) -> None:
    cfg = store.project
    values = {"sha": sha, "short": sha[:10], "host": deploy or "", "package": str(store.directory / "link-package")}
    environment = {k: v for k, v in os.environ.items() if k not in cfg.install_env_drop}
    with store.locked("promote", promote_lock_message(store)):
        path = worktrees.gate_worktree(common, sha, cfg)
        argv = [substitute(part, values) for part in step.argv]
        extra = {key: substitute(value, values) for key, value in step.env}
        print(f"lane: {step.name}: {' '.join(argv)} in {path}", flush=True)
        if step.lease:
            priority, source = step_priority(step, cfg)
            try:
                argv = slots.wrap(step.lease.slots, f"promote-{sha[:10]}-{step.name}", argv,
                                  priority=priority, ticket=step.lease.ticket, required=True,
                                  meta={"phase": step.name, "kind": "promote", "sha": sha[:10],
                                        **verify.plan_meta([f"step:{step.name}"], None, 0, project=cfg),
                                        **slots.priority_meta(priority, source),
                                        "lanefather.state": str(store.directory)})
            except slots.Unavailable as error:
                raise LaneError(f"promote of {sha[:10]} went through, but {step.name} cannot run: "
                                f"{error}") from None
        status = subprocess.run(argv, cwd=path, env={**environment, **extra}).returncode
        journal.ledger(f"promote-{step.name} {sha} exit={status}", kind="promote.step",
                       level="error" if status else "info", subject={"type": "commit", "id": sha},
                       details={"step": step.name, "exit": status, "command": events.command(argv)},
                       store=store)
        if status:
            skipped_text = ", ".join(skipped) or "none"
            raise LaneError(f"promote of {sha[:10]} went through, but {step.name} failed (exit {status}); "
                            f"later steps not run: {skipped_text}")


# Local dev and main follow origin (#666): promote --no-push once left them ahead of it, and only the owner could
# drop the commits. sync catches them up under the landing lock; every refusal names the command that does it right.

def publish_hint(sha: str, ship: bool = False, *, prog: str = "lanefather") -> str:
    return (f"unpushed work lives on its own branch (agent/<name>); to publish it: {prog} promote {sha}"
            + (" --ship" if ship else ""))


def has_remote(cwd: str, remote: str) -> bool:
    return bool(git(cwd, "remote", "get-url", remote, check=False))


def fetch(cwd: str, remote: str, verb: str) -> None:
    fetched = subprocess.run(["git", "fetch", "-q", remote], cwd=cwd, capture_output=True, text=True)
    if fetched.returncode:
        raise LaneError(f"{verb} refused: git fetch {remote} failed: {fetched.stderr.strip()}")


def no_push_refusal(cwd: str, sha: str, branches: list[str], remote: str, *, ship: bool | None = None,
                    project: config.Config = config.DEFAULT) -> None:
    """--no-push moves a local branch only to what <remote> already has, so it never runs ahead of it. A repository
    without the remote has nothing to run ahead of."""
    if not has_remote(cwd, remote):
        return
    fetch(cwd, remote, "promote --no-push")
    promoted = project.promoted
    need_ship = (len(promoted) > 1 and promoted[-1] in branches) if ship is None else ship
    for branch in branches:
        _, upstream = tracking(cwd, remote, branch)
        if not upstream or not git_ok(cwd, "merge-base", "--is-ancestor", sha, upstream):
            raise LaneError(f"promote --no-push refused: {sha[:10]} is not on {remote}/{branch}, and --no-push moves "
                            f"local {branch} only to what {remote} has; "
                            + publish_hint(sha[:10], ship=need_ship, prog=project.command))


def backup_name(cwd: str, branch: str) -> str:
    """backup/<branch>-<date>, or -<date>-N for the next free N."""
    base = f"backup/{branch}-{datetime.date.today().isoformat()}"
    name, number = base, 1
    while git_ok(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
        number += 1
        name = f"{base}-{number}"
    return name


Move = tuple[str, str, str, list[str] | None]


def sync_plan(cwd: str, args: Any, verb: str, *,
              project: config.Config = config.DEFAULT) -> tuple[list[Move], list[str], dict[str, str]]:
    """The moves, notes and checked-out worktrees of a sync, read after the fetch; raises the refusal that moves
    nothing."""
    prog = project.command
    fetch(cwd, args.remote, verb)
    checked_out = {w["branch"]: w["path"] for w in worktree_list(cwd) if w.get("branch")}
    moves: list[Move] = []
    notes: list[str] = []
    diverged: list[Move] = []
    for branch in project.promoted:
        local, upstream = tracking(cwd, args.remote, branch)
        if not local or not upstream:
            missing = f"local {branch}" if not local else f"{args.remote}/{branch}"
            notes.append(f"{branch}: no {missing}; left alone")
        elif local == upstream:
            notes.append(f"{branch}: up to date at {local[:10]}")
        elif git_ok(cwd, "merge-base", "--is-ancestor", local, upstream):
            moves.append((branch, local, upstream, None))
        else:
            dropped = git(cwd, "log", "--oneline", "--no-decorate", f"{upstream}..{local}").splitlines()
            diverged.append((branch, local, upstream, dropped))
    if diverged and not args.drop_local:
        lines: list[str] = []
        for branch, _, _, kept in diverged:
            dropped = kept or []
            verb_form = "is" if len(dropped) == 1 else "are"
            lines.append(f"local {branch} has {commits(len(dropped))} that {verb_form} not on {args.remote}/{branch}:")
            lines += [f"  {line}" for line in dropped]
        raise LaneError("sync refused: " + "\n".join(lines) + "\nWork to keep goes on its own branch (git branch "
                        f"agent/<name> <commit>) and lands through a lane; to publish a landed SHA: {prog} promote "
                        f"<sha>. If they are stale: {prog} sync --drop-local resets to {args.remote} and keeps "
                        "them on a backup branch; nothing moved")
    for branch, _, _, _ in diverged:
        if branch in checked_out:
            changed = git(checked_out[branch], "status", "--porcelain", "--untracked-files=no")
            if changed:
                raise LaneError(f"{verb} refused: {checked_out[branch]} has {branch} checked out with uncommitted "
                                f"changes:\n{changed}\nCommit them on their own branch or move them away; "
                                "nothing moved")
    return moves + diverged, notes, checked_out


def sync(args: Any, store: state.Store) -> int:
    """Under the landing lock: fetch, then fast-forward local dev and main to <remote>; with --drop-local reset them
    to it, keeping the commits <remote> lacks on a backup branch first. A refusal moves neither branch."""
    prog = store.prog
    cwd = os.getcwd()
    common = common_dir(cwd)
    verb = "sync --drop-local" if args.drop_local else "sync"
    if not has_remote(cwd, args.remote):
        raise LaneError(f"{verb} refused: no remote {args.remote} here; nothing moved")
    holder = f"{prog} {verb} (pid {os.getpid()}, {cwd})"
    finished: list[str] = []
    failed: list[str] = []
    kept: list[str] = []
    with landing(common, holder, args.wait, store=store) as env:
        moves, notes, checked_out = sync_plan(cwd, args, verb, project=store.project)
        for branch, old, new, dropped in moves:
            backup = None
            if dropped is not None:
                backup = backup_name(cwd, branch)
                git(cwd, "branch", backup, old)
                reason = f"lane sync --drop-local ({backup})"
            else:
                reason = "lane sync"
            error = move_branch(cwd, branch, old, new, env, reason, checked_out=checked_out,
                                reset=dropped is not None)
            if error:
                failed.append(error)
                if backup:
                    git(cwd, "branch", "-D", backup, check=False)
            elif backup and dropped is not None:
                finished.append(f"{branch} {old[:10]}->{new[:10]}, {commits(len(dropped))} dropped, kept on {backup}")
                kept.append(f"{backup} keeps the {commits(len(dropped))} dropped from local {branch}:")
                kept += [f"  {line}" for line in dropped]
            else:
                finished.append(f"{branch} {old[:10]}->{new[:10]} fast-forwarded")
    if not moves:
        promoted = store.project.promoted
        verb_word = "is" if len(promoted) == 1 else "are"
        print(f"lane: local {' and '.join(promoted)} {verb_word} up to date with {args.remote} ({'; '.join(notes)})")
        return 0
    journal.ledger(f"sync {args.remote}: {'; '.join(finished) or '-'}" + (f" failed: {'; '.join(failed)}" if failed
                                                                            else ""),
                   kind="sync.finished", level="warn" if failed else "info",
                   subject={"type": "remote", "id": args.remote}, details={"moved": finished, "failed": failed},
                   store=store)
    for line in kept:
        print(line if line.startswith("  ") else f"lane: {line}")
    for line in finished + notes:
        print(f"lane: {line}")
    if failed:
        print("lane: these local branches did not move:\n  " + "\n  ".join(failed), file=sys.stderr)
        return 1
    return 0


def after_promote(cwd: str, sha: str, project: config.Config = config.DEFAULT) -> None:
    """After dev moved: the project's after-promote command (Straylight's Sid-Kit and Data Grid adoption check,
    #588), with `{sha}` and `{short}` (its first 10) replaced. It never fails the promote: a failure only warns."""
    argv = [sys.executable if part == "python3" else substitute(part, {"sha": sha, "short": sha[:10]})
            for part in project.after_promote]
    if not argv:
        return
    script = next((part for part in argv[1:] if part.endswith(".py")), None)
    if script and not (Path(cwd) / script).is_file():
        return
    sys.stdout.flush()
    try:
        status = subprocess.run(argv, cwd=cwd).returncode
    except OSError as error:
        status, argv = 1, [*argv, f"({error})"]
    if status:
        print(f"lane: warning: the after-promote command failed (exit {status}): {' '.join(argv)}", file=sys.stderr)


def step_no_push_refusal(run: PromoteRun) -> None:
    if run.no_push:
        no_push_refusal(run.cwd, run.sha, run.branches, run.remote, ship=run.ship, project=run.store.project)


def step_ahead_warnings(run: PromoteRun) -> None:
    for line in ahead_lines(run.cwd, run.remote, project=run.store.project):
        print(f"lane: {line}", flush=True)


def step_ship_refusal(run: PromoteRun) -> None:
    cfg = run.store.project
    if run.ship and cfg.ship_refuses and git(run.cwd, "ls-tree", "--name-only", f"{run.sha}:{cfg.ship_refuses}",
                                             check=False):
        raise LaneError(f"promote --ship refused: {run.sha[:10]} still has {cfg.ship_refuses}/; {cfg.ship_refusal}")


def step_review_gate(run: PromoteRun) -> None:
    prog = run.store.prog
    report = review_gate.gate_report(run.store, run.cwd, run.common, run.sha)
    for entry in report["reported"]:
        print(f"lane: reported, does not block: {entry['text']}", flush=True)
    blocking = [e["text"] for e in report["blocking"]]
    if blocking and not run.force_reason:
        raise LaneError(f"promote refused: {'/'.join(report['blocks'])} review findings unmarked (or marked fixed "
                        "outside the SHA) hold the batch "
                        "gate and dev:\n- " + "\n- ".join(blocking) + f"\nThen rerun {prog} promote {run.sha[:10]}. "
                        "--force-reason overrides, logged")


def step_batch_gate(run: PromoteRun) -> None:
    cfg = run.store.project
    if (cfg.stamp_tool and git_ok(run.cwd, "cat-file", "-e", f"{run.sha}:{cfg.verify}")
            and git_ok(run.cwd, "cat-file", "-e", f"{run.sha}:{cfg.stamp_tool}")):
        if gate.verified(run.cwd, run.common, run.sha, cfg):
            why = gate.verified_why(run.store, run.cwd, run.sha)
            print(f"lane: {run.sha[:10]}'s tree is already verified ({why}); no batch gate needed")
        else:
            gate.batch_gate(run.store, run.cwd, run.common, run.sha, run.jobs)


def step_review_refusals(run: PromoteRun) -> None:
    prog = run.store.prog
    refusals = review_gate.review_refusals(run.store, run.cwd, run.common, run.sha)
    if refusals and not run.force_reason:
        raise LaneError("promote refused: dev waits for the review of every lane landed since dev:\n- "
                        + "\n- ".join(refusals) + "\nMark each finding with "
                        + review_record.mark_help("<id>", prog=prog)
                        + f", then rerun {prog} promote {run.sha[:10]}; a stamped tree is not gated again. "
                        "--force-reason overrides, logged")
    if refusals:
        journal.ledger(f"override promote {run.sha}: {run.force_reason} ({'; '.join(refusals)})", kind="override",
                       level="warn", subject={"type": "commit", "id": run.sha},
                       details={"command": "promote", "reason": run.force_reason, "refusals": refusals},
                       store=run.store)


def step_perf_suite(run: PromoteRun) -> None:
    cfg = run.store.project
    if run.ship and git_ok(run.cwd, "cat-file", "-e", f"{run.sha}:{cfg.verify}"):
        gate.perf_suite(run.store, run.cwd, run.common, run.sha, ship=True, remote=run.remote)


def step_publish_refs(run: PromoteRun) -> int:
    prog = run.store.prog
    run.subject = git(run.cwd, "log", "-1", "--format=%s", run.sha)
    holder = f"{prog} promote {run.sha[:10]}{' --ship' if run.ship else ''} (pid {os.getpid()}, {run.cwd})"
    run.failed_moves = []
    with landing(run.common, holder, run.wait, store=run.store) as env:
        if not run.no_push:
            fetch(run.cwd, run.remote, "promote")
        if run.ship:
            run.ship_base = gate.main_base(run.cwd, run.remote, project=run.store.project)
        moves: list[tuple[str, str]] = []
        for branch in run.branches:
            old = git(run.cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False)
            if old and not git_ok(run.cwd, "merge-base", "--is-ancestor", old, run.sha):
                foreign = git(run.cwd, "log", "--oneline", "--no-decorate", "-10", f"{run.sha}..{old}")
                upstream = f"{run.remote}/{branch}"
                on_remote = git_ok(run.cwd, "rev-parse", "--verify", "--quiet", f"refs/remotes/{upstream}") and not git(
                    run.cwd, "rev-list", "-1", old, f"^{run.sha}", f"^refs/remotes/{upstream}", check=False)
                way_out = (f"they are on {upstream}: merge {upstream} into the lane or integration branch and promote "
                           "the new tip" if on_remote else
                           f"land them through a lane into the integration branch; if they are stale, {prog} sync "
                           "--drop-local drops them and keeps them on a backup branch")
                raise LaneError(f"promote refused: local {branch} has commits that are not in {run.sha[:10]}:\n"
                                f"{foreign}\n{way_out[0].upper() + way_out[1:]}. Nothing was pushed")
            moves.append((branch, old))
        if not run.no_push:
            refspecs = [f"{run.sha}:refs/heads/{branch}" for branch in run.branches]
            print(f"lane: pushing {run.sha[:10]} {run.subject!r} to {run.remote} {' and '.join(run.branches)}",
                  flush=True)
            if subprocess.run(["git", "push", "--atomic", run.remote, *refspecs], cwd=run.cwd).returncode:
                raise LaneError(f"promote refused: git push {run.remote} {' '.join(refspecs)} failed; "
                                f"local {' and '.join(run.branches)} unchanged")
        checked_out = {w["branch"]: w["path"] for w in worktree_list(run.cwd) if w.get("branch")}
        for branch, old in moves:
            if old == run.sha:
                continue
            error = move_branch(run.cwd, branch, old, run.sha, env, f"lane promote {run.sha[:10]}",
                                checked_out=checked_out)
            if error:
                run.failed_moves.append(error)
    pushed = "local only" if run.no_push else f"pushed to {run.remote}"
    journal.ledger(f"promote {run.sha} {'+'.join(run.branches)} {pushed}"
                   + (f" failed: {'; '.join(run.failed_moves)}" if run.failed_moves else ""),
                   kind="promote.finished", level="warn" if run.failed_moves else "info",
                   subject={"type": "commit", "id": run.sha},
                   details={"branches": run.branches, "pushed": not run.no_push, "remote": run.remote,
                            "failed": run.failed_moves},
                   store=run.store)
    if run.failed_moves:
        print("lane: " + ("the push went through, but " if not run.no_push else "") + "these local branches did "
              "not move:\n  " + "\n  ".join(run.failed_moves) + f"\nFix that, then rerun {prog} promote {run.sha[:10]}"
              + (" --ship" if run.ship else ""), file=sys.stderr)
        return 1
    print(f"Promoted {run.sha[:10]} {run.subject!r}: {' and '.join(run.branches)}, {pushed}")
    return 0


def step_after_promote(run: PromoteRun) -> None:
    after_promote(run.cwd, run.sha, run.store.project)


def step_release_comments(run: PromoteRun) -> None:
    if run.ship and run.ship_base:
        tickets.release_comments(run.store, run.cwd, run.common, run.ship_base, run.sha, None)


def step_overall_review(run: PromoteRun) -> None:
    if run.ship and run.ship_base:
        print(f"lane: {review.start_overall_review(run.store, run.cwd, run.sha, run.ship_base)}")


def _install_summary_step(run: PromoteRun) -> None:
    print(f"Installed{' and deployed' if run.deploy else ''} {run.sha[:10]}"
          + (f" to {run.deploy}" if run.deploy else ""))


def _require_install_steps(run: PromoteRun) -> None:
    if not run.store.project.install:
        raise LaneError("promote --install: the project names no install steps ([[promote.install]] in "
                        ".lanefather.toml)")


PromoteStep = Callable[[PromoteRun], int | None]


def promote_steps(run: PromoteRun) -> list[tuple[str, PromoteStep]]:
    steps: list[tuple[str, PromoteStep]] = [
        ("no_push_refusal", step_no_push_refusal),
        ("ahead_warnings", step_ahead_warnings),
        ("ship_refusal", step_ship_refusal),
        ("review_gate", step_review_gate),
        ("batch_gate", step_batch_gate),
        ("review_refusals", step_review_refusals),
        ("perf_suite", step_perf_suite),
        ("publish_refs", step_publish_refs),
        ("after_promote", step_after_promote),
        ("release_comments", step_release_comments),
        ("overall_review", step_overall_review),
    ]
    if run.install:
        cfg = run.store.project
        if not cfg.install:
            steps.append(("install_check", _require_install_steps))
        else:
            configured = [*cfg.install, *(cfg.deploy if run.deploy else ())]
            for index, cfg_step in enumerate(configured):
                skipped = [later.name for later in configured[index + 1:]]

                def _run_step(r: PromoteRun, s: config.Step = cfg_step, sk: list[str] = skipped) -> None:
                    _run_config_step(r.store, r.common, r.sha, s, sk, r.deploy)

                steps.append((f"step:{cfg_step.name}", _run_step))
            steps.append(("install_summary", _install_summary_step))
    return steps


def promote_sha(run: PromoteRun) -> int:
    for _name, step in promote_steps(run):
        status = step(run)
        if status:
            return status
    return 0


def promote(args: Any, store: state.Store) -> int:
    """The batch gate on a SHA unless its tree is verified; then fast-forward local dev (and main with --ship) to
    it and push it, under the landing lock; with --install (and --deploy) install and deploy it."""
    if args.deploy and not args.install:
        raise LaneError("promote refused: --deploy needs --install (install the Mac first, as deploy.sh says)")
    if (args.install or args.deploy) and args.no_push:
        raise LaneError("promote refused: --install and --deploy need the push: the install and deploy guards take "
                        "only a SHA on origin/dev")
    cfg = store.project
    steps = (*cfg.install, *(cfg.deploy if args.deploy else ())) if args.install else ()
    if any(step.lease for step in steps) and slots.command() is None:
        raise LaneError("promote refused: configured execution leases require slotfather on PATH; nothing published")
    for step in steps:
        if step.lease:
            step_priority(step, cfg)
    cwd = os.getcwd()
    sha = git(cwd, "rev-parse", "--verify", "--quiet", args.sha + "^{commit}", check=False)
    if not sha:
        raise LaneError(f"promote refused: {args.sha} is not a commit here")
    promoted = cfg.promoted
    branches = list(promoted) if args.ship or len(promoted) == 1 else list(promoted[:-1])
    run = PromoteRun(
        cwd=cwd,
        common=common_dir(cwd),
        sha=sha,
        branches=branches,
        remote=args.remote,
        ship=bool(args.ship),
        no_push=bool(args.no_push),
        jobs=args.jobs,
        wait=args.wait,
        force_reason=args.force_reason,
        install=bool(args.install),
        deploy=args.deploy,
        store=store,
    )
    return promote_sha(run)
