"""The batch gate, verified-tree stamps, and the perf suite (fathers#73)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any

from . import config, host, journal, plan, receipts, slots, state, verify, worktrees
from . import results as result_store
from .errors import LaneError
from .gitops import common_dir, git


def promote_lock_message(store: state.Store) -> str:
    return f"lane: waiting for another promote's gate worktree (lock {store.directory / '.promote.lock'})"


# The batch gate (#446): the full gate once per batch, at promote.

def stamped_trees(common: str, project: config.Config = config.DEFAULT) -> set[str]:
    """The trees a full verify run stamped, from the common Git directory."""
    try:
        rows = (Path(common) / project.stamp_file).read_text().splitlines()
    except OSError:
        return set()
    return {row.split()[0] for row in rows if row.split()}


def verified(cwd: str, common: str, sha: str, project: config.Config = config.DEFAULT) -> bool:
    """Whether pre-push would take the SHA's tree: stamped, or (with the hook) differing only in documentation."""
    if git(cwd, "rev-parse", sha + "^{tree}") in stamped_trees(common, project):
        return True
    if not project.stamp_tool:
        return False
    top = git(cwd, "rev-parse", "--show-toplevel")
    script = Path(top) / project.stamp_tool
    if not (Path(top) / project.pre_push_hook).is_file() or not script.is_file():
        return False
    return subprocess.run([str(script), "check", sha], cwd=top, capture_output=True).returncode == 0


def verified_why(store: state.Store, cwd: str, sha: str) -> str:
    """Why a verified tree needs no gate: the green full gate the results store holds for it (fathers#91), else its
    stamp."""
    tree = git(cwd, "rev-parse", sha + "^{tree}")
    top = git(cwd, "rev-parse", "--show-toplevel", check=False) or cwd
    entry = result_store.full_green(store, tree, plan.toolchain_state(top, store.project))
    if entry is None:
        return f"tree {tree[:10]} is stamped"
    source = f"the batch gate of {str(entry.get('sha'))[:10]}" if entry.get("source") == "gate" else entry.get("source")
    return (f"{source} passed tree {tree[:10]} at {entry.get('at')}"
            + (f"; receipt {entry['receipt']}" if entry.get("receipt") else ""))


def _format_bisect_lines(store: state.Store, merges: list[tuple[str, str]], direct: list[tuple[str, str]],
                         header: str, other_header: str) -> list[str]:
    by_merge = {r["landed"]["merge"]: r["id"] for r in store.lanes() if r.get("landed")}
    lines = [header]
    lines += [f"  {c[:10]} {s}" + (f"  lane {by_merge[c]}" if c in by_merge else "") for c, s in merges] or ["  none"]
    if direct:
        lines.append(other_header)
        lines += [f"  {c[:10]} {s}" for c, s in direct]
    return lines


def bisect_list(
    cwd: str,
    common: str,
    sha: str,
    depth: int = 500,
    *,
    project: config.Config = config.DEFAULT,
) -> tuple[str | None, list[tuple[str, str]], list[tuple[str, str]]]:
    """The newest first-parent ancestor of the SHA with a stamped tree, and the merges and other commits after it,
    oldest first."""
    stamped = stamped_trees(common, project)
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


def bisect_lines(store: state.Store, cwd: str, common: str, sha: str) -> list[str]:
    green, merges, direct = bisect_list(cwd, common, sha, project=store.project)
    since = (f"since the last green verified tree {green[:10]}" if green
             else f"among the last 500 first-parent commits of {sha[:10]} (none has a verified tree)")
    return _format_bisect_lines(
        store, merges, direct,
        f"Lane merges {since}, oldest first; bisect these:",
        "Other commits on the integration branch since then:",
    )


def batch_gate(store: state.Store, cwd: str, common: str, sha: str, jobs: int | None) -> None:
    """Run the full gate on the SHA in the gate worktree; return once its tree is stamped, else refuse."""
    with store.locked("promote", "lane: waiting for the batch gate of another promote "
                                 f"(lock {store.directory / '.promote.lock'})"):
        if verified(cwd, common, sha, store.project):
            print(f"lane: {sha[:10]}'s tree was verified while this promote waited; no batch gate needed")
            return
        run_batch_gate(store, cwd, common, sha, jobs)


def gate_meta(path: Any, project: config.Config = config.DEFAULT) -> dict[str, str]:
    """The batch gate's run type (fathers#107): the full plan, and how many checks the tree's plan module defines."""
    known = plan.known_checks(path, project)
    return verify.plan_meta(result_store.FULL, path, len(known) if known is not None else None, project=project)


def run_batch_gate(store: state.Store, cwd: str, common: str, sha: str, jobs: int | None) -> None:
    path = worktrees.gate_worktree(common, sha, store.project)
    label = f"promote-{sha[:10]}"
    load_wait = host.wait_for_load(store, label)
    # Sized once the wait is over (#599 review), so jobs_decision records the load the gate starts under.
    info = host.host(store.project)
    decision = host.given_jobs(jobs, "--jobs") if jobs else host.gate_jobs(info)
    job_count = int(decision["jobs"])
    slots.warn_outer_lease("promote", job_count, prog=store.prog)
    outcome = verify.Run(
        path, verify.Selection.full(), label, "gate", job_count,
        store=store, meta={"sha": sha[:10]}, ticket=None, run_type=gate_meta(path, store.project),
        record_history=False, wait_for_load=False, load_wait=load_wait,
        banner=f"lane: batch gate: the full gate on {sha[:10]} in {path} with {job_count} jobs (log {{log}}); "
               "queue: slotfather status",
    ).execute()
    jobs = job_count
    status, seconds, load, log = outcome.exit, outcome.seconds, outcome.load, outcome.log
    stamp, report = outcome.stamp, outcome.report
    failed, blocked, triaged = outcome.failed_checks, outcome.blocked_checks, outcome.triage
    green = status == 0 and stamp["stamped"] and not blocked
    module = plan.module(path, store.project)
    tree = git(cwd, "rev-parse", sha + "^{tree}")
    gate_path = receipts.write(
        store, label,
        {"version": 1, "lane": label, "sha": sha, "tree": tree, "full": True,
         "platform": result_store.host_platform(store.project),
         "suites": list(getattr(module, "SUITES", ())), "jobs": jobs, "jobs_decision": decision, "exit": status,
         "seconds": seconds,
         "status": "green" if green else "red", "failed_checks": failed,
         **({"blocked_checks": blocked} if blocked else {}),
         "triage": triaged, "verified_tree": stamp,
         "load_wait": load_wait, "load": load,
         "results": [{"suite": "full gate", "status": "passed" if green else "failed", "seconds": seconds,
                      "load_wait": load_wait, "load": load, "log": str(log), "failed_checks": failed,
                      **({"blocked_checks": blocked} if blocked else {})}],
         "log": str(log), "host": info, "started": outcome.started, "finished": journal.now()},
        "gate",
    )
    rows = {row["id"]: row.get("status") for row in (report or {}).get("checks", []) if row.get("id")}
    result_store.record(store, tree,
                        {c: v for c, v in rows.items() if v in ("passed", "failed")}, source="gate", full=True,
                        status="green" if green else "red", toolchain=plan.toolchain_state(path, store.project),
                        sha=sha, receipt=str(gate_path))
    journal.ledger(f"promote-gate {sha} {'green' if green else 'red'} jobs={jobs} seconds={seconds:.0f}"
                   f"{' stamped' if stamp['stamped'] else ''} {host.host_fields(info)}",
                   kind="gate.finished", level="info" if green else "error", subject={"type": "commit", "id": sha},
                   details={"status": "green" if green else "red", "jobs": jobs, "seconds": seconds,
                            "stamped": bool(stamp["stamped"]), "failed_checks": failed, "log": str(log)},
                   store=store)
    if green:
        print(f"lane: batch gate green in {seconds:.0f} s; verified tree {stamp['tree']} stamped")
        return
    if status:
        blocked_note = f"; blocked checks: {', '.join(blocked)}" if blocked else ""
        print(f"lane: batch gate red on {sha[:10]} after {seconds:.0f} s; failed checks: "
              f"{', '.join(failed) or '(see the log)'}{blocked_note}  {log}")
        if triaged:
            print(triaged)
    elif blocked:
        print(f"lane: batch gate red on {sha[:10]} after {seconds:.0f} s; blocked checks: "
              f"{', '.join(blocked)}  {log}")
    else:
        print(f"lane: the batch gate passed but did not stamp: {stamp.get('reason', 'unknown')}")
    print("\n".join(bisect_lines(store, cwd, common, sha)))
    raise LaneError(f"promote refused: the batch gate did not stamp {sha[:10]}; nothing moved")


# The perf suite (#579, owner decision 2026-10-27): the timing budgets run only before main, once, alone on the
# machine.

def main_base(cwd: str, remote: str = "origin", *, project: config.Config = config.DEFAULT) -> str | None:
    """What the final promoted branch holds: the remote's, else the local one."""
    branch = project.promoted[-1]
    return (git(cwd, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}^{{commit}}", check=False)
            or git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False) or None)


def perf_bisect_lines(store: state.Store, cwd: str, sha: str, base: str | None) -> list[str]:
    """The lane merges and other commits main has not taken: a red perf run's regression is among them."""
    if not base:
        return ["No main to bisect from: bisect the lane merges since the last release."]
    merges: list[tuple[str, str]] = []
    direct: list[tuple[str, str]] = []
    for row in git(cwd, "log", "--first-parent", "--reverse", "--format=%H %P%x1f%s", f"{base}..{sha}").splitlines():
        head, _, subject = row.partition("\x1f")
        commit, *parents = head.split()
        (merges if len(parents) > 1 else direct).append((commit, subject))
    return _format_bisect_lines(
        store, merges, direct,
        f"Lane merges since main {base[:10]}, oldest first; bisect these for the performance regression "
        f"(./{store.project.verify} --suites perf on each):",
        "Other commits since main:",
    )


def perf_suite(store: state.Store, cwd: str, common: str, sha: str, ship: bool,
               remote: str = "origin") -> dict[str, Any] | None:
    """Run the perf suite on the SHA in the gate worktree: verify --suites <perf>, which holds every verification
    slot while it measures. Returns its receipt, or None for a tree whose plan has no perf suite."""
    suite = plan.perf_suite_of(cwd, sha, store.project)
    if not suite:
        print(f"lane: {sha[:10]} has no perf suite in {store.project.plan_module or 'its plan'}; nothing to measure")
        return None
    tree = git(cwd, "rev-parse", sha + "^{tree}")
    if ship:
        previous = receipts.perf_reusable(store, sha, tree)
        if previous is not None:
            print(f"lane: perf suite already green on tree {tree[:10]} ({previous.get('log')})")
            return previous
    with store.locked("promote", promote_lock_message(store)):
        worktree = worktrees.gate_worktree(common, sha, store.project)
        label = f"perf-{sha[:10]}"
        outcome = verify.Run(
            worktree, verify.Selection.suites([suite]), label, "perf", 1,
            store=store, meta={"sha": sha[:10]}, ticket=None,
            run_type=verify.plan_meta([f"suite:{suite}"], worktree,
                                      verify.suite_count(worktree, [suite], store.project), project=store.project),
            record_history=False, wait_for_load=False,
            banner=f"lane: perf suite on {sha[:10]} in {worktree}, alone on the machine (log {{log}}); "
                   "queue: slotfather status",
        ).execute()
        status, seconds, load, log = outcome.exit, outcome.seconds, outcome.load, outcome.log
        failed, blocked = outcome.failed_checks, outcome.blocked_checks
        triaged, started = outcome.triage, outcome.started
    info = host.host(store.project)
    perf_status = "red" if status or blocked else "green"
    receipt: dict[str, Any] = {"version": 1, "sha": sha, "tree": tree,
                               "status": perf_status,
                               "exit": status, "seconds": seconds, "failed_checks": failed,
                               **({"blocked_checks": blocked} if blocked else {}),
                               "triage": triaged,
                               "log": str(log), "load": load, "host": info, "started": started,
                               "finished": journal.now()}
    receipts.write(store, sha, receipt, "perf")
    journal.ledger(f"perf {sha} {perf_status} seconds={seconds:.0f} {host.host_fields(info)}",
                   kind="perf.finished", level="info" if perf_status == "green" else "error",
                   subject={"type": "commit", "id": sha},
                   details={"status": perf_status, "seconds": seconds, "failed_checks": failed,
                            "log": str(log)},
                   store=store)
    if perf_status == "green":
        print(f"lane: perf suite green in {seconds:.0f} s on {sha[:10]}")
        return receipt
    parts: list[str] = []
    if failed:
        parts.append(f"failed checks: {', '.join(failed)}")
    if blocked:
        parts.append(f"blocked checks: {', '.join(blocked)}")
    detail = "; ".join(parts) or "failed checks: (see the log)"
    print(f"lane: perf suite red on {sha[:10]} after {seconds:.0f} s; {detail}  {log}")
    if triaged:
        print(triaged)
    print("A red perf suite is a performance regression, not a flake: no registry entry excuses it.")
    print("\n".join(perf_bisect_lines(store, cwd, sha, main_base(cwd, remote, project=store.project))))
    raise LaneError(f"{'promote --ship refused' if ship else 'perf'}: the perf suite is red on {sha[:10]}; "
                    "nothing moved")


def perf(args: Any, store: state.Store) -> int:
    """The perf suite on a SHA by hand; it moves nothing."""
    cwd = os.getcwd()
    sha = git(cwd, "rev-parse", "--verify", "--quiet", args.sha + "^{commit}", check=False)
    if not sha:
        raise LaneError(f"perf refused: {args.sha} is not a commit here")
    perf_suite(store, cwd, common_dir(cwd), sha, ship=False, remote=args.remote)
    return 0
