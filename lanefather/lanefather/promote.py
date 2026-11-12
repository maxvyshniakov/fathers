"""The batch gate, the perf suite, promote and sync.

promote <sha> runs the full gate on <sha> in the gate worktree unless its tree is stamped; then, once every lane
landed since dev has a finished review with each finding marked, it fast-forwards local dev (and main) to it and
pushes <sha>:dev (and <sha>:main, in one atomic push) through the pre-push gate, then runs the project's after-promote
command. --ship refuses a tree that still holds the configured unreleased directory, runs the perf suite once after
the gate, and starts one overall review of origin/main..<sha>. --no-push moves the local branches only to a SHA the
remote already has (#666). --install runs the configured install (and with --deploy the deploy) steps in the gate
worktree, each only after the previous succeeded (#569).

sync fetches, then fast-forwards local dev and main to the remote under the landing lock; a branch with commits the
remote lacks is refused, and --drop-local resets it after keeping them on backup/<branch>-<date>[-N] (#666).

promote and sync work in any repository they run in; a tree without the configured verify and stamp tool gets no
batch gate from promote.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from . import checks, config, events, host, journal, plan, review, slots, state, tickets, worktrees
from . import results as result_store
from .errors import LaneError
from .gitops import common_dir, git, git_ok, worktree_list
from .landing import landing


def promote_lock_message() -> str:
    return f"lane: waiting for another promote's gate worktree (lock {state.state_dir() / '.promote.lock'})"


# The batch gate (#446): the full gate once per batch, at promote.

def stamped_trees(common: str) -> set[str]:
    """The trees a full verify run stamped, from the common Git directory."""
    try:
        rows = (Path(common) / config.current().stamp_file).read_text().splitlines()
    except OSError:
        return set()
    return {row.split()[0] for row in rows if row.split()}


def verified(cwd: str, common: str, sha: str) -> bool:
    """Whether pre-push would take the SHA's tree: stamped, or (with the hook) differing only in documentation."""
    if git(cwd, "rev-parse", sha + "^{tree}") in stamped_trees(common):
        return True
    cfg = config.current()
    if not cfg.stamp_tool:
        return False
    top = git(cwd, "rev-parse", "--show-toplevel")
    script = Path(top) / cfg.stamp_tool
    if not (Path(top) / cfg.pre_push_hook).is_file() or not script.is_file():
        return False
    return subprocess.run([str(script), "check", sha], cwd=top, capture_output=True).returncode == 0


def verified_why(cwd: str, sha: str) -> str:
    """Why a verified tree needs no gate: the green full gate the results store holds for it (fathers#91), else its
    stamp."""
    tree = git(cwd, "rev-parse", sha + "^{tree}")
    top = git(cwd, "rev-parse", "--show-toplevel", check=False) or cwd
    entry = result_store.full_green(tree, plan.toolchain_state(top))
    if entry is None:
        return f"tree {tree[:10]} is stamped"
    source = f"the batch gate of {str(entry.get('sha'))[:10]}" if entry.get("source") == "gate" else entry.get("source")
    return (f"{source} passed tree {tree[:10]} at {entry.get('at')}"
            + (f"; receipt {entry['receipt']}" if entry.get("receipt") else ""))


def bisect_list(cwd: str, common: str, sha: str,
                depth: int = 500) -> tuple[str | None, list[tuple[str, str]], list[tuple[str, str]]]:
    """The newest first-parent ancestor of the SHA with a stamped tree, and the merges and other commits after it,
    oldest first."""
    stamped = stamped_trees(common)
    green: str | None = None
    merges: list[tuple[str, str]] = []
    direct: list[tuple[str, str]] = []
    for row in git(cwd, "log", "--first-parent", f"-n{depth}", "--format=%H %T %P%x1f%s", sha).splitlines():
        head, _, subject = row.partition("\x1f")
        commit, tree, *parents = head.split()
        if tree in stamped:
            green = commit
            break
        (merges if len(parents) > 1 else direct).append((commit, subject))
    return green, merges[::-1], direct[::-1]


def bisect_lines(cwd: str, common: str, sha: str) -> list[str]:
    green, merges, direct = bisect_list(cwd, common, sha)
    by_merge = {r["landed"]["merge"]: r["id"] for r in state.lanes() if r.get("landed")}
    since = (f"since the last green verified tree {green[:10]}" if green
             else f"among the last 500 first-parent commits of {sha[:10]} (none has a verified tree)")
    lines = [f"Lane merges {since}, oldest first; bisect these:"]
    lines += [f"  {c[:10]} {s}" + (f"  lane {by_merge[c]}" if c in by_merge else "") for c, s in merges] or ["  none"]
    if direct:
        lines.append("Other commits on the integration branch since then:")
        lines += [f"  {c[:10]} {s}" for c, s in direct]
    return lines


def batch_gate(cwd: str, common: str, sha: str, jobs: int | None) -> None:
    """Run the full gate on the SHA in the gate worktree; return once its tree is stamped, else refuse.

    One promote at a time holds the gate worktree, from its checkout to the end of verify: a second checkout under a
    running gate changed the tree that gate was verifying (#454)."""
    with state.locked("promote", "lane: waiting for the batch gate of another promote "
                                 f"(lock {state.state_dir() / '.promote.lock'})"):
        if verified(cwd, common, sha):
            print(f"lane: {sha[:10]}'s tree was verified while this promote waited; no batch gate needed")
            return
        run_batch_gate(cwd, common, sha, jobs)


def gate_meta(path: Any) -> dict[str, str]:
    """The batch gate's run type (fathers#107): the full plan, and how many checks the tree's plan module defines."""
    known = plan.known_checks(path)
    return checks.plan_meta(result_store.FULL, path, len(known) if known is not None else None)


def run_batch_gate(cwd: str, common: str, sha: str, jobs: int | None) -> None:
    path = worktrees.gate_worktree(common, sha)
    log = journal.log_path(f"promote-{sha[:10]}")
    label = f"promote-{sha[:10]}"
    load_wait = host.wait_for_load(label)
    # Sized once the wait is over (#599 review), so jobs_decision records the load the gate starts under.
    info = host.host()
    decision = host.given_jobs(jobs, "--jobs") if jobs else host.gate_jobs(info)
    jobs = decision["jobs"]
    print(f"lane: batch gate: the full gate on {sha[:10]} in {path} with {jobs} jobs (log {log}); "
          "queue: slotfather status", flush=True)
    started, clock, load = journal.now(), time.monotonic(), {"start": host.load_averages()}
    with open(log, "w") as handle:
        status = subprocess.run([plan.verify_path(path), "--keep-going", "--jobs", str(jobs)], cwd=path,
                                env=checks.verify_env(label, "gate", sha=sha[:10], **gate_meta(path)), stdout=handle,
                                stderr=subprocess.STDOUT).returncode
    seconds = round(time.monotonic() - clock, 1)
    load["end"] = host.load_averages()
    text = log.read_text(errors="replace")
    stamp = checks.stamp_result(text, True) if status == 0 else {"stamped": False, "reason": "the run failed"}
    reports, report = checks.report_of(text)
    failed = [row["id"] for row in (report or {}).get("checks", []) if row.get("status") not in checks.PASSING]
    triaged = plan.triage(path, Path(reports[-1].strip()).parent) if status and reports else None
    green = status == 0 and stamp["stamped"]
    module = plan.module(path)
    state.write_json(state.state_dir() / f"{label}.gate.json", {
        "version": 1, "lane": label, "sha": sha, "tree": git(cwd, "rev-parse", sha + "^{tree}"), "full": True,
        "suites": list(getattr(module, "SUITES", ())), "jobs": jobs, "jobs_decision": decision, "exit": status,
        "seconds": seconds,
        "status": "green" if green else "red", "failed_checks": failed, "triage": triaged, "verified_tree": stamp,
        "load_wait": load_wait, "load": load,
        "results": [{"suite": "full gate", "status": "passed" if green else "failed", "seconds": seconds,
                     "load_wait": load_wait, "load": load, "log": str(log), "failed_checks": failed}],
        "log": str(log), "host": info, "started": started, "finished": journal.now()})
    # fathers#91: the gate's result belongs to the tree; a later promote of any commit with it reads why it is verified.
    rows = {row["id"]: row.get("status") for row in (report or {}).get("checks", []) if row.get("id")}
    result_store.record(git(cwd, "rev-parse", sha + "^{tree}"),
                   {c: v for c, v in rows.items() if v in ("passed", "failed")}, source="gate", full=True,
                   status="green" if green else "red", toolchain=plan.toolchain_state(path), sha=sha,
                   receipt=str(state.state_dir() / f"{label}.gate.json"))
    journal.ledger(f"promote-gate {sha} {'green' if green else 'red'} jobs={jobs} seconds={seconds:.0f}"
                   f"{' stamped' if stamp['stamped'] else ''} {host.host_fields(info)}",
                   kind="gate.finished", level="info" if green else "error", subject={"type": "commit", "id": sha},
                   details={"status": "green" if green else "red", "jobs": jobs, "seconds": seconds,
                            "stamped": bool(stamp["stamped"]), "failed_checks": failed, "log": str(log)})
    if green:
        print(f"lane: batch gate green in {seconds:.0f} s; verified tree {stamp['tree']} stamped")
        return
    if status:
        print(f"lane: batch gate red on {sha[:10]} after {seconds:.0f} s; failed checks: "
              f"{', '.join(failed) or '(see the log)'}  {log}")
        if triaged:
            print(triaged)
    else:
        print(f"lane: the batch gate passed but did not stamp: {stamp['reason']}")
    print("\n".join(bisect_lines(cwd, common, sha)))
    raise LaneError(f"promote refused: the batch gate did not stamp {sha[:10]}; nothing moved")


# The perf suite (#579, owner decision 2026-10-27): the timing budgets run only before main, once, alone on the
# machine.

def main_base(cwd: str, remote: str = "origin") -> str | None:
    """What main holds: the remote's main, else the local one."""
    return (git(cwd, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/main^{{commit}}", check=False)
            or git(cwd, "rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}", check=False) or None)


def perf_bisect_lines(cwd: str, sha: str, base: str | None) -> list[str]:
    """The lane merges and other commits main has not taken: a red perf run's regression is among them."""
    if not base:
        return ["No main to bisect from: bisect the lane merges since the last release."]
    by_merge = {r["landed"]["merge"]: r["id"] for r in state.lanes() if r.get("landed")}
    merges: list[tuple[str, str]] = []
    direct: list[tuple[str, str]] = []
    for row in git(cwd, "log", "--first-parent", "--reverse", "--format=%H %P%x1f%s", f"{base}..{sha}").splitlines():
        head, _, subject = row.partition("\x1f")
        commit, *parents = head.split()
        (merges if len(parents) > 1 else direct).append((commit, subject))
    lines = [f"Lane merges since main {base[:10]}, oldest first; bisect these for the performance regression "
             f"(./{config.current().verify} --suites perf on each):"]
    lines += [f"  {c[:10]} {s}" + (f"  lane {by_merge[c]}" if c in by_merge else "") for c, s in merges] or ["  none"]
    if direct:
        lines.append("Other commits since main:")
        lines += [f"  {c[:10]} {s}" for c, s in direct]
    return lines


def perf_suite(cwd: str, common: str, sha: str, ship: bool, remote: str = "origin") -> dict[str, Any] | None:
    """Run the perf suite on the SHA in the gate worktree: verify --suites <perf>, which holds every verification
    slot while it measures. Returns its receipt, or None for a tree whose plan has no perf suite. For a ship, a green
    receipt of the same tree stands; a red run raises.

    One promote at a time holds the gate worktree, so the perf run never shares the machine with a batch gate."""
    suite = plan.perf_suite_of(cwd, sha)
    if not suite:
        print(f"lane: {sha[:10]} has no perf suite in {config.current().plan_module or 'its plan'}; nothing to measure")
        return None
    tree = git(cwd, "rev-parse", sha + "^{tree}")
    path = state.state_dir() / f"perf-{sha[:10]}.perf.json"
    previous = state.read_json(path)
    if ship and previous and previous.get("status") == "green" and previous.get("tree") == tree:
        print(f"lane: perf suite already green on tree {tree[:10]} ({previous.get('log')})")
        return previous
    with state.locked("promote", promote_lock_message()):
        worktree = worktrees.gate_worktree(common, sha)
        log = journal.log_path(f"perf-{sha[:10]}")
        label = f"perf-{sha[:10]}"
        print(f"lane: perf suite on {sha[:10]} in {worktree}, alone on the machine (log {log}); "
              "queue: slotfather status", flush=True)
        started, clock, load = journal.now(), time.monotonic(), {"start": host.load_averages()}
        with open(log, "w") as handle:
            status = subprocess.run([plan.verify_path(worktree), "--suites", suite, "--keep-going", "--jobs", "1"],
                                    cwd=worktree, env=checks.verify_env(
                                        label, "perf", sha=sha[:10],
                                        **checks.plan_meta([f"suite:{suite}"], worktree,
                                                           checks.suite_count(worktree, [suite]))),
                                    stdout=handle, stderr=subprocess.STDOUT).returncode
        seconds = round(time.monotonic() - clock, 1)
        load["end"] = host.load_averages()
        text = log.read_text(errors="replace")
        reports, report = checks.report_of(text)
        failed = [row["id"] for row in (report or {}).get("checks", []) if row.get("status") not in checks.PASSING]
        triaged = plan.triage(worktree, Path(reports[-1].strip()).parent) if status and reports else None
    info = host.host()
    receipt: dict[str, Any] = {"version": 1, "sha": sha, "tree": tree, "status": "red" if status else "green",
                               "exit": status, "seconds": seconds, "failed_checks": failed, "triage": triaged,
                               "log": str(log), "load": load, "host": info, "started": started,
                               "finished": journal.now()}
    state.write_json(path, receipt)
    journal.ledger(f"perf {sha} {receipt['status']} seconds={seconds:.0f} {host.host_fields(info)}",
                   kind="perf.finished", level="info" if receipt["status"] == "green" else "error",
                   subject={"type": "commit", "id": sha},
                   details={"status": receipt["status"], "seconds": seconds, "failed_checks": failed,
                            "log": str(log)})
    if not status:
        print(f"lane: perf suite green in {seconds:.0f} s on {sha[:10]}")
        return receipt
    print(f"lane: perf suite red on {sha[:10]} after {seconds:.0f} s; failed checks: "
          f"{', '.join(failed) or '(see the log)'}  {log}")
    if triaged:
        print(triaged)
    print("A red perf suite is a performance regression, not a flake: no registry entry excuses it.")
    print("\n".join(perf_bisect_lines(cwd, sha, main_base(cwd, remote))))
    raise LaneError(f"{'promote --ship refused' if ship else 'perf'}: the perf suite is red on {sha[:10]}; "
                    "nothing moved")


def perf(args: Any) -> int:
    """The perf suite on a SHA by hand; it moves nothing."""
    cwd = os.getcwd()
    sha = git(cwd, "rev-parse", "--verify", "--quiet", args.sha + "^{commit}", check=False)
    if not sha:
        raise LaneError(f"perf refused: {args.sha} is not a commit here")
    perf_suite(cwd, common_dir(cwd), sha, ship=False, remote=args.remote)
    return 0


def substitute(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def step_priority(step: config.Step) -> tuple[str, str]:
    """A leased install or deploy step's priority and its source: the step's own, else the gate's as
    slots.phase_priority resolves it, normal when nothing sets it (lanefather#24)."""
    assert step.lease is not None
    if step.lease.priority:
        return step.lease.priority, f"step {step.name} lease"
    return slots.phase_priority("gate", fallback="normal")


def install_and_deploy(common: str, sha: str, deploy: str | None) -> None:
    """After a promotion (#569): the configured install steps from the gate worktree, and with a host the deploy
    steps, each only after the previous succeeded. The steps run without the variables `install_env_drop` names
    (the landing token, a project's unpromoted marker), so each guard sees the promoted tree. The gate worktree is
    promote's, so the promote lock holds it at the SHA throughout."""
    cfg = config.current()
    values = {"sha": sha, "short": sha[:10], "host": deploy or "", "package": str(state.state_dir() / "link-package")}
    steps = [*cfg.install, *(cfg.deploy if deploy else ())]
    if not cfg.install:
        raise LaneError("promote --install: the project names no install steps ([[promote.install]] in "
                        ".lanefather.toml)")
    environment = {k: v for k, v in os.environ.items() if k not in cfg.install_env_drop}
    with state.locked("promote", promote_lock_message()):
        path = worktrees.gate_worktree(common, sha)
        for index, step in enumerate(steps):
            argv = [substitute(part, values) for part in step.argv]
            extra = {key: substitute(value, values) for key, value in step.env}
            print(f"lane: {step.name}: {' '.join(argv)} in {path}", flush=True)
            if step.lease:
                priority, source = step_priority(step)
                try:
                    argv = slots.wrap(step.lease.slots, f"promote-{sha[:10]}-{step.name}", argv,
                                      priority=priority, ticket=step.lease.ticket, required=True,
                                      meta={"phase": step.name, "kind": "promote", "sha": sha[:10],
                                            **checks.plan_meta([f"step:{step.name}"], path, 0),
                                            **slots.priority_meta(priority, source),
                                            "lanefather.state": str(state.state_dir())})
                except slots.Unavailable as error:
                    raise LaneError(f"promote of {sha[:10]} went through, but {step.name} cannot run: "
                                    f"{error}") from None
            status = subprocess.run(argv, cwd=path, env={**environment, **extra}).returncode
            journal.ledger(f"promote-{step.name} {sha} exit={status}", kind="promote.step",
                           level="error" if status else "info", subject={"type": "commit", "id": sha},
                           details={"step": step.name, "exit": status, "command": events.command(argv)})
            if status:
                skipped = ", ".join(later.name for later in steps[index + 1:]) or "none"
                raise LaneError(f"promote of {sha[:10]} went through, but {step.name} failed (exit {status}); "
                                f"later steps not run: {skipped}")
    print(f"Installed{' and deployed' if deploy else ''} {sha[:10]}" + (f" to {deploy}" if deploy else ""))


def promote(args: Any) -> int:
    """The batch gate on a SHA unless its tree is verified; then fast-forward local dev (and main with --ship) to
    it and push it, under the landing lock; with --install (and --deploy) install and deploy it."""
    if args.deploy and not args.install:
        raise LaneError("promote refused: --deploy needs --install (install the Mac first, as deploy.sh says)")
    if (args.install or args.deploy) and args.no_push:
        raise LaneError("promote refused: --install and --deploy need the push: the install and deploy guards take "
                        "only a SHA on origin/dev")
    cfg = config.current()
    steps = (*cfg.install, *(cfg.deploy if args.deploy else ())) if args.install else ()
    if any(step.lease for step in steps) and slots.command() is None:
        raise LaneError("promote refused: configured execution leases require slotfather on PATH; nothing published")
    # An invalid priority refuses before anything moves, not after the promotion (lanefather#24).
    for step in steps:
        if step.lease:
            step_priority(step)
    status = promote_sha(args)
    if status == 0 and args.install:
        cwd = os.getcwd()
        install_and_deploy(common_dir(cwd), git(cwd, "rev-parse", args.sha + "^{commit}"), args.deploy)
    return status


# Local dev and main follow origin (#666): promote --no-push once left them ahead of it, and only the owner could
# drop the commits. sync catches them up under the landing lock; every refusal names the command that does it right.

def publish_hint(sha: str, ship: bool = False) -> str:
    return (f"unpushed work lives on its own branch (agent/<name>); to publish it: {config.prog()} promote {sha}"
            + (" --ship" if ship else ""))


def commits(count: int) -> str:
    return f"{count} commit" + ("" if count == 1 else "s")


def has_remote(cwd: str, remote: str) -> bool:
    return bool(git(cwd, "remote", "get-url", remote, check=False))


def fetch(cwd: str, remote: str, verb: str) -> None:
    fetched = subprocess.run(["git", "fetch", "-q", remote], cwd=cwd, capture_output=True, text=True)
    if fetched.returncode:
        raise LaneError(f"{verb} refused: git fetch {remote} failed: {fetched.stderr.strip()}")


def tracking(cwd: str, remote: str, branch: str) -> tuple[str | None, str | None]:
    """Local <branch> and <remote>/<branch>, each None when absent."""
    local = git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False) or None
    upstream = git(cwd, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}^{{commit}}",
                   check=False) or None
    return local, upstream


def ahead_lines(cwd: str, remote: str = "origin") -> list[str]:
    """A warning for each of local dev and main that has commits <remote> lacks, as last fetched."""
    lines: list[str] = []
    for branch in ("dev", "main"):
        local, upstream = tracking(cwd, remote, branch)
        if local and upstream and local != upstream:
            count = int(git(cwd, "rev-list", "--count", f"{upstream}..{local}", check=False) or 0)
            if count:
                lines.append(f"local {branch} is {commits(count)} ahead of {remote}; `{config.prog()} sync "
                             "--drop-local` drops them with a backup")
    return lines


def no_push_refusal(cwd: str, sha: str, branches: list[str], remote: str) -> None:
    """--no-push moves a local branch only to what <remote> already has, so it never runs ahead of it. A repository
    without the remote has nothing to run ahead of."""
    if not has_remote(cwd, remote):
        return
    fetch(cwd, remote, "promote --no-push")
    for branch in branches:
        _, upstream = tracking(cwd, remote, branch)
        if not upstream or not git_ok(cwd, "merge-base", "--is-ancestor", sha, upstream):
            raise LaneError(f"promote --no-push refused: {sha[:10]} is not on {remote}/{branch}, and --no-push moves "
                            f"local {branch} only to what {remote} has; "
                            + publish_hint(sha[:10], ship="main" in branches))


def backup_name(cwd: str, branch: str) -> str:
    """backup/<branch>-<date>, or -<date>-N for the next free N."""
    base = f"backup/{branch}-{datetime.date.today().isoformat()}"
    name, number = base, 1
    while git_ok(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
        number += 1
        name = f"{base}-{number}"
    return name


Move = tuple[str, str, str, list[str] | None]


def sync_plan(cwd: str, args: Any, verb: str) -> tuple[list[Move], list[str], dict[str, str]]:
    """The moves, notes and checked-out worktrees of a sync, read after the fetch; raises the refusal that moves
    nothing. sync calls it under the landing lock, so no land or promote moves dev or main between this read and the
    moves (review of #666: a land during the lock wait was lost by a reset to an older read)."""
    prog = config.prog()
    fetch(cwd, args.remote, verb)
    checked_out = {w["branch"]: w["path"] for w in worktree_list(cwd) if w.get("branch")}
    moves: list[Move] = []
    notes: list[str] = []
    diverged: list[Move] = []
    for branch in ("dev", "main"):
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


def sync(args: Any) -> int:
    """Under the landing lock: fetch, then fast-forward local dev and main to <remote>; with --drop-local reset them
    to it, keeping the commits <remote> lacks on a backup branch first. A refusal moves neither branch."""
    prog = config.prog()
    cwd = os.getcwd()
    common = common_dir(cwd)
    verb = "sync --drop-local" if args.drop_local else "sync"
    if not has_remote(cwd, args.remote):
        raise LaneError(f"{verb} refused: no remote {args.remote} here; nothing moved")
    holder = f"{prog} {verb} (pid {os.getpid()}, {cwd})"
    finished: list[str] = []
    failed: list[str] = []
    kept: list[str] = []
    with landing(common, holder, args.wait) as env:
        moves, notes, checked_out = sync_plan(cwd, args, verb)
        for branch, old, new, dropped in moves:
            where = checked_out.get(branch)
            backup = None
            if dropped is not None:
                backup = backup_name(cwd, branch)
                git(cwd, "branch", backup, old)
                # reset --keep refuses to overwrite local changes, as a checked-out branch's worktree may have.
                argv = (["git", "reset", "-q", "--keep", new] if where else
                        ["git", "update-ref", "-m", f"lane sync --drop-local ({backup})", f"refs/heads/{branch}", new,
                         old])
            else:
                argv = (["git", "merge", "--ff-only", "-q", new] if where else
                        ["git", "update-ref", "-m", "lane sync", f"refs/heads/{branch}", new, old])
            moved = subprocess.run(argv, cwd=where or cwd, env=env, capture_output=True, text=True)
            if moved.returncode:
                failed.append(f"local {branch} in {where or cwd}: {(moved.stdout + moved.stderr).strip()}")
                if backup:
                    # Nothing was dropped, so nothing needs keeping: a rerun takes the same name.
                    git(cwd, "branch", "-D", backup, check=False)
            elif backup and dropped is not None:
                finished.append(f"{branch} {old[:10]}->{new[:10]}, {commits(len(dropped))} dropped, kept on {backup}")
                kept.append(f"{backup} keeps the {commits(len(dropped))} dropped from local {branch}:")
                kept += [f"  {line}" for line in dropped]
            else:
                finished.append(f"{branch} {old[:10]}->{new[:10]} fast-forwarded")
    if not moves:
        print(f"lane: local dev and main are up to date with {args.remote} ({'; '.join(notes)})")
        return 0
    journal.ledger(f"sync {args.remote}: {'; '.join(finished) or '-'}" + (f" failed: {'; '.join(failed)}" if failed
                                                                            else ""),
                   kind="sync.finished", level="warn" if failed else "info",
                   subject={"type": "remote", "id": args.remote}, details={"moved": finished, "failed": failed})
    for line in kept:
        print(line if line.startswith("  ") else f"lane: {line}")
    for line in finished + notes:
        print(f"lane: {line}")
    if failed:
        print("lane: these local branches did not move:\n  " + "\n  ".join(failed), file=sys.stderr)
        return 1
    return 0


def after_promote(cwd: str, sha: str) -> None:
    """After dev moved: the project's after-promote command (Straylight's Sid-Kit and Data Grid adoption check,
    #588), with `{sha}` and `{short}` (its first 10) replaced. It never fails the promote: a failure only warns."""
    argv = [sys.executable if part == "python3" else substitute(part, {"sha": sha, "short": sha[:10]})
            for part in config.current().after_promote]
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


def promote_sha(args: Any) -> int:
    prog = config.prog()
    cfg = config.current()
    cwd = os.getcwd()
    common = common_dir(cwd)
    sha = git(cwd, "rev-parse", "--verify", "--quiet", args.sha + "^{commit}", check=False)
    if not sha:
        raise LaneError(f"promote refused: {args.sha} is not a commit here")
    if args.no_push:
        no_push_refusal(cwd, sha, ["dev", "main"] if args.ship else ["dev"], args.remote)
    for line in ahead_lines(cwd, args.remote):
        print(f"lane: {line}", flush=True)
    # main takes only released notes: fragments wait on dev until a release commit folds them (#447).
    if args.ship and cfg.ship_refuses and git(cwd, "ls-tree", "--name-only", f"{sha}:{cfg.ship_refuses}",
                                              check=False):
        raise LaneError(f"promote --ship refused: {sha[:10]} still has {cfg.ship_refuses}/; {cfg.ship_refusal}")
    # Only fully gated trees are promoted. Repositories without the stamp tooling have their own pre-push gate.
    if (cfg.stamp_tool and git_ok(cwd, "cat-file", "-e", f"{sha}:{cfg.verify}")
            and git_ok(cwd, "cat-file", "-e", f"{sha}:{cfg.stamp_tool}")):
        if verified(cwd, common, sha):
            print(f"lane: {sha[:10]}'s tree is already verified ({verified_why(cwd, sha)}); no batch gate needed")
        else:
            batch_gate(cwd, common, sha, args.jobs)
    # The gate runs while reviews finish; only the move of dev waits for them (#570).
    refusals = review.review_refusals(cwd, common, sha)
    if refusals and not args.force_reason:
        raise LaneError("promote refused: dev waits for the review of every lane landed since dev:\n- "
                        + "\n- ".join(refusals) + "\nMark each finding with " + review.mark_help("<id>")
                        + f", then rerun {prog} promote {sha[:10]}; a stamped tree is not gated again. "
                        "--force-reason overrides, logged")
    if refusals:
        journal.ledger(f"override promote {sha}: {args.force_reason} ({'; '.join(refusals)})", kind="override",
                       level="warn", subject={"type": "commit", "id": sha},
                       details={"command": "promote", "reason": args.force_reason, "refusals": refusals})
    # main takes only a tree whose timing budgets held, measured alone after the gate (#579); dev never waits for it.
    if args.ship and git_ok(cwd, "cat-file", "-e", f"{sha}:{cfg.verify}"):
        perf_suite(cwd, common, sha, ship=True, remote=args.remote)
    branches = ["dev", "main"] if args.ship else ["dev"]
    subject = git(cwd, "log", "-1", "--format=%s", sha)
    holder = f"{prog} promote {sha[:10]}{' --ship' if args.ship else ''} (pid {os.getpid()}, {cwd})"
    ship_base = None
    failed: list[str] = []
    with landing(common, holder, args.wait) as env:
        if not args.no_push:
            fetch(cwd, args.remote, "promote")
        if args.ship:
            # The overall review covers what main gains: origin/main (local main without one) to the SHA.
            ship_base = (git(cwd, "rev-parse", "--verify", "--quiet", f"refs/remotes/{args.remote}/main^{{commit}}",
                             check=False)
                         or git(cwd, "rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}", check=False))
        moves: list[tuple[str, str]] = []
        for branch in branches:
            old = git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False)
            if old and not git_ok(cwd, "merge-base", "--is-ancestor", old, sha):
                foreign = git(cwd, "log", "--oneline", "--no-decorate", "-10", f"{sha}..{old}")
                upstream = f"{args.remote}/{branch}"
                on_remote = git_ok(cwd, "rev-parse", "--verify", "--quiet", f"refs/remotes/{upstream}") and not git(
                    cwd, "rev-list", "-1", old, f"^{sha}", f"^refs/remotes/{upstream}", check=False)
                way_out = (f"they are on {upstream}: merge {upstream} into the lane or integration branch and promote "
                           "the new tip" if on_remote else
                           f"land them through a lane into the integration branch; if they are stale, {prog} sync "
                           "--drop-local drops them and keeps them on a backup branch")
                raise LaneError(f"promote refused: local {branch} has commits that are not in {sha[:10]}:\n{foreign}\n"
                                f"{way_out[0].upper() + way_out[1:]}. Nothing was pushed")
            moves.append((branch, old))
        if not args.no_push:
            refspecs = [f"{sha}:refs/heads/{branch}" for branch in branches]
            print(f"lane: pushing {sha[:10]} {subject!r} to {args.remote} {' and '.join(branches)}", flush=True)
            # The pre-push hook gates the push: a verified tree, and main only as a dev tip. The push moves
            # remote-tracking refs only, so the token stays out of the verify runs a pre-push hook may start.
            # --atomic: the remote takes dev and main together or neither, so a refused main leaves dev in place.
            if subprocess.run(["git", "push", "--atomic", args.remote, *refspecs], cwd=cwd).returncode:
                raise LaneError(f"promote refused: git push {args.remote} {' '.join(refspecs)} failed; "
                                f"local {' and '.join(branches)} unchanged")
        checked_out = {w["branch"]: w["path"] for w in worktree_list(cwd) if w.get("branch")}
        for branch, old in moves:
            if old == sha:
                continue
            if branch in checked_out:
                argv, where = ["git", "merge", "--ff-only", "-q", sha], checked_out[branch]
            else:
                argv, where = ["git", "update-ref", "-m", f"lane promote {sha[:10]}", f"refs/heads/{branch}", sha,
                               old or ""], cwd
            moved = subprocess.run(argv, cwd=where, env=env, capture_output=True, text=True)
            if moved.returncode:
                failed.append(f"local {branch} in {where}: {(moved.stdout + moved.stderr).strip()}")
    pushed = "local only" if args.no_push else f"pushed to {args.remote}"
    journal.ledger(f"promote {sha} {'+'.join(branches)} {pushed}" + (f" failed: {'; '.join(failed)}" if failed else ""),
                   kind="promote.finished", level="warn" if failed else "info", subject={"type": "commit", "id": sha},
                   details={"branches": branches, "pushed": not args.no_push, "remote": args.remote,
                            "failed": failed})
    if failed:
        print("lane: " + ("the push went through, but " if not args.no_push else "") + "these local branches did "
              "not move:\n  " + "\n  ".join(failed) + f"\nFix that, then rerun {prog} promote {sha[:10]}"
              + (" --ship" if args.ship else ""), file=sys.stderr)
        return 1
    print(f"Promoted {sha[:10]} {subject!r}: {' and '.join(branches)}, {pushed}")
    after_promote(cwd, sha)
    if args.ship and ship_base:
        tickets.release_comments(cwd, common, ship_base, sha, None)
        print(f"lane: {review.start_overall_review(cwd, sha, ship_base)}")
    return 0
