"""Verify runs and the lane check's receipt.

check runs the checks the tree's plan selects for the lane's diff and writes the receipt for the committed tree,
reusing each check that passed for an earlier tree of the lane and that no change since selects. Each red check
reruns by itself on the lane at the lane check's own lease size (lanefather#4), then, red again, on its base, and is
classified (#568): lane red again and base green, `regression`; base red too, `pre-existing`; lane green on its
rerun, `flaky-under-load`. Only the last two leave the receipt landable. A red cheap check stops the run before any
heavier check starts and reruns on the base only; the heavier checks run when it is pre-existing there (#639).
"""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import os
from pathlib import Path
import re
import shutil
from typing import Any

from . import (
    claims,
    config,
    events,
    history,
    host,
    journal,
    plan,
    queue,
    receipts,
    settings,
    slots,
    state,
    verify,
    worktrees,
)
from . import results as result_store
from .errors import LaneError
from .gitops import current_branch, dirty, git, git_ok
from .state import Record
from .verify import (
    NOT_STAMPED,
    PASSING,
    REPORT,
    STAMPED,
    UNRESOLVED,
    is_dependency_blocked,
    is_executed_failure,
    new_run_id,
    plan_meta,
    report_of,
    stamp_result,
    suite_count,
    terminable,
    tree_of,
    verify_env,
)

Rows = list[dict[str, Any]]

DEFAULT_RERUNS = 5
LANDABLE = ("flaky-under-load", "pre-existing")
# Test files by name: a tests/ directory, test-*, test_*, *.test.*, *_test.*, *-test.*.
TEST_PATH = re.compile(r"(?:^|/)(?:tests?|__tests__)/|(?:^|/)test[-_][^/]*$|[._-]tests?\.[^/]+$")
# Longer than slotfather's head limit (2700 s) plus a margin: an all-slot rerun that becomes the queue head starts
# by then (#650).
DEFAULT_RERUN_WAIT = float(settings.DEFAULT_RERUN_WAIT)
# A rerun's most slots (lanefather#4): the lane check's own lease size, never the whole machine; only the perf suite
# measures alone.
RERUN_MAX_JOBS = 2

__all__ = [
    "NOT_STAMPED", "PASSING", "REPORT", "STAMPED", "UNRESOLVED",
    "is_dependency_blocked", "is_executed_failure", "new_run_id", "plan_meta",
    "report_of", "stamp_result", "suite_count", "terminable", "tree_of", "verify_env",
]


def lane_meta(record: Record) -> dict[str, str]:
    """Caller tags for the lane; ticket is passed as a separate request field."""
    return {"lane": record["id"], "lanefather.state": str(state.state_dir().resolve())}


def lane_ticket(record: Record) -> str | None:
    return slots.ticket_value(str(record.get("ticket") or ""))


def suite_outcomes(root: Any, suites: list[str], selected: dict[str, list[str]] | None,
                   outcome: verify.Outcome) -> tuple[Rows, dict[str, dict[str, Any]]]:
    """Project one `verify.Outcome` onto per-suite results and per-check results."""
    status = outcome.exit
    environment = outcome.environment
    failed_checks = outcome.failed_checks
    blocked_by_id = outcome.blocked_by_id
    owners = plan.suite_owners(root, suites) if status or environment or blocked_by_id else None
    failing = {s: [c for c in failed_checks if plan.owned(owners, s, c)] for s in suites}
    blocking = {s: [c for c in blocked_by_id if plan.owned(owners, s, c)] for s in suites}
    # A failure no check accounts for (setup, slot limiter) fails every suite of the run; dependency-blocked checks
    # keep their own suite status rather than becoming executed failures (fathers#195).
    failed = (list(suites) if environment
              else ([s for s in suites if failing[s]] or ([] if any(blocking.values()) else suites)) if status else [])
    blocked_suites = [s for s in suites if s not in failed and blocking[s]]
    rows = {row.get("id"): row for row in (outcome.report or {}).get("checks", [])}
    # A run that stopped at its first checks (#639) did not start the others: a suite with one of them not run and
    # none red neither passed nor failed.
    stopped = outcome.stopped
    ids = None if selected is None else [c for s in suites for c in selected[s]]
    unstarted = [s for s in suites if stopped and s not in failed and s not in blocked_suites and (
        selected is None or ids is None
        or any(rows.get(c, {}).get("status", "not_run") == "not_run" for c in selected[s]))]
    if stopped:
        print(f"lane: verify stopped at its first checks (#639): {', '.join(stopped['failed'])} red; "
              f"{stopped['not_run']} heavier checks did not start ({outcome.log})", flush=True)
    lease = outcome.lease_fields
    results: Rows = [{"suite": suite,
                      "status": ("failed" if suite in failed else "blocked" if suite in blocked_suites
                                 else "not_run" if suite in unstarted else "passed"),
                      "exit": status,
                      "seconds": outcome.seconds, "load_wait": outcome.load_wait, "load": outcome.load,
                      "log": str(outcome.log), **lease,
                      "failed_checks": failing[suite] if suite in failed else [],
                      **({"blocked_checks": blocking[suite]} if blocking[suite] else {}),
                      **({"report": str(outcome.report_path)} if outcome.report_path is not None else {}),
                      **({"environment": environment} if environment else {}),
                      **({"checks": selected[suite]} if selected is not None else {})}
                     for suite in suites]
    void = bool(environment) or (bool(status) and not any(failing.values()) and not any(blocking.values()))

    def check_entry(suite: str, check: str) -> dict[str, Any]:
        row = rows.get(check, {})
        blocked = not void and is_dependency_blocked(row)
        entry: dict[str, Any] = {
            "suite": suite,
            "status": "void" if void else "blocked" if blocked else row.get("status", "not_run"),
            "log": str(outcome.log),
            **lease,
        }
        if blocked and row.get("blocked_by"):
            entry["blocked_by"] = list(row["blocked_by"])
        if isinstance(row.get("seconds"), (int, float)):
            entry["seconds"] = row["seconds"]
        return entry

    checks: dict[str, dict[str, Any]] = {} if ids is None or selected is None else {
        check: check_entry(suite, check) for suite in suites for check in selected[suite]}
    return results, checks


def reusable(previous: Record, delta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The check results of an earlier receipt that a later check of the lane reuses: each passed there, and no
    change since its head (the `delta` plan) selects it. A full delta reuses nothing; nor does a receipt from
    before per-check results."""
    return receipts.reusable_checks(previous, delta)


def same_tree(wanted: dict[str, str], tree: str, toolchain: Any) -> dict[str, dict[str, Any]]:
    """The wanted checks ({id: suite}) whose newest result on the tree, under the toolchain, passed (fathers#91), as
    reused check results naming the run that stands for them."""
    if not wanted:
        return {}
    stored = result_store.passed(tree, toolchain)

    def standing(result: dict[str, Any]) -> dict[str, Any]:
        # An inferred result points at the run it was taken from (review of fathers#91).
        origin: dict[str, Any] = result.get("origin") or {}
        return {**result, **origin}
    return {c: {"suite": suite, "status": "passed", "log": standing(stored[c]).get("receipt", ""), "reused_from": tree,
                "reused_by": "tree", **{f"reused_{k}": standing(stored[c])[k]
                                        for k in ("source", "receipt", "lane", "at") if standing(stored[c]).get(k)}}
            for c, suite in wanted.items() if c in stored}


def origins(reused: dict[str, dict[str, Any]], receipt: str) -> dict[str, dict[str, Any]]:
    """Where each reused result came from, for the results store: the receipt and tree that ran it."""
    return {c: {k: v for k, v in (("tree", e.get("reused_from")),
                                  ("receipt", e.get("reused_receipt") or receipt),
                                  ("source", e.get("reused_source") or "check"),
                                  ("lane", e.get("reused_lane")), ("at", e.get("reused_at"))) if v}
            for c, e in reused.items()}


def recordable(check_results: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The statuses the results store keeps: a pass or a failure of the check itself."""
    return {c: e["status"] for c, e in check_results.items() if e.get("status") in ("passed", "failed")}


def combined_results(suites: list[str], selected: dict[str, list[str]] | None, fresh: Rows,
                     reused: dict[str, dict[str, Any]]) -> Rows:
    """Each suite's result for a receipt: the fresh run's, naming every check the plan selects there and those it
    reused; a suite whose every check was reused passes, reused from the tree its first check ran on."""
    ran = {r["suite"]: r for r in fresh}
    results: Rows = []
    for suite in suites:
        ids = (selected or {}).get(suite, [])
        kept = [c for c in ids if c in reused]
        if suite in ran:
            result = dict(ran[suite], **({"checks": ids} if selected is not None else {}))
            results.append(dict(result, reused=kept) if kept else result)
        else:
            entries = [reused[c] for c in kept]
            results.append({"suite": suite, "status": "passed", "checks": ids, "failed_checks": [], "reused": kept,
                            "reused_from": entries[0]["reused_from"], "log": entries[0].get("log", "")})
    return results


def lane_ran(lane_id: str) -> tuple[set[str], set[str], set[str]]:
    """What the lane's check and post-merge receipts ran: the checks, the suites, and the suites they ran whole. A
    result without per-check results ran every check of its suite."""
    return receipts.lane_ran(state.Store(config.current()), lane_id)


# Red check classification (#568)

def rerun_cap() -> int:
    """LANEFATHER_RERUNS: how many red checks a lane check reruns; the rest stay unclassified."""
    value = config.current().env("RERUNS")
    return int(value) if value and value.strip().isdigit() and int(value) > 0 else DEFAULT_RERUNS


def red_checks(results: Rows) -> list[str]:
    """The failed checks of the failed results, in order, once each."""
    found: list[str] = []
    for result in results:
        if result["status"] != "passed":
            found += [c for c in result.get("failed_checks", []) if c not in found]
    return found


RerunUnavailable = verify.LeaseTimedOut


def rerun_wait() -> float:
    """LANEFATHER_RERUN_WAIT, else this machine's `rerun.wait` setting: seconds a rerun waits for its lease before
    its checks stay unclassified."""
    value = config.current().env("RERUN_WAIT") or str(settings.value("rerun.wait") or "")
    try:
        seconds = float(value) if value else DEFAULT_RERUN_WAIT
    except ValueError:
        return DEFAULT_RERUN_WAIT
    return seconds if seconds > 0 else DEFAULT_RERUN_WAIT


def required_min_slots(lane_plan: dict[str, Any], remaining: dict[str, list[str]] | None,
                       suites: list[str]) -> tuple[int, list[str]]:
    """The minimum slot floor declared by the checks/suites that will actually run (fathers#173), from the plan's
    per-check `min_slots`/`slots` fields or `.lanefather.toml` `[checks.min_slots]` patterns."""
    needs: dict[str, int] = {}
    patterns = config.current().min_slots
    if remaining is None:
        top = plan.declared_slots(lane_plan)
        if top is not None and suites:
            needs["plan"] = top
        for suite in suites:
            for pattern, count in patterns:
                if fnmatch.fnmatchcase(suite, pattern):
                    needs[suite] = max(needs.get(suite, 0), count)
    else:
        running = {c for ids in remaining.values() for c in ids}
        for row in lane_plan.get("selection") or ():
            cid = row["id"]
            if cid not in running:
                continue
            declared = plan.declared_slots(row)
            if declared is not None:
                needs[cid] = max(needs.get(cid, 0), declared)
            for pattern, count in patterns:
                if fnmatch.fnmatchcase(cid, pattern) or fnmatch.fnmatchcase(row["suite"], pattern):
                    needs[cid] = max(needs.get(cid, 0), count)
    if not needs:
        return 1, []
    floor = max(needs.values())
    return floor, [name for name, count in needs.items() if count == floor]


def rerun_min_slots(check_ids: list[str], lane_plan: dict[str, Any] | None, fresh: Rows) -> int:
    """Minimum slots required for a rerun of `check_ids` (fathers#173)."""
    if lane_plan is not None and lane_plan.get("selection") is not None:
        return required_min_slots(lane_plan, {"_": check_ids}, [])[0]
    floor = 1
    if lane_plan is not None:
        suites = [r["suite"] for r in fresh if r["status"] != "passed"]
        floor = max(floor, required_min_slots(lane_plan, None, suites)[0])
    patterns = config.current().min_slots
    for cid in check_ids:
        for pattern, count in patterns:
            if fnmatch.fnmatchcase(cid, pattern):
                floor = max(floor, count)
    return floor


def rerun_jobs(jobs: int, minimum: int = 1) -> int:
    """A rerun's lease size (lanefather#4): the lane check's own, 1 to RERUN_MAX_JOBS slots (or the step's declared
    minimum slots, fathers#173). A lease of the whole machine never came within its wait while one-slot lane checks
    ran, and as the queue head held them back."""
    return max(1, min(max(RERUN_MAX_JOBS, minimum), jobs))


class Rerun:
    """One verify run of red checks in a checkout, delegated to `verify.Run`."""

    def __init__(self, root: Any, ids: list[str], label: str, jobs: int, queued: bool = True,
                 meta: dict[str, Any] | None = None, ticket: str | None = None, toolchain: Any = UNRESOLVED):
        self.root, self.ids, self.label, self.jobs = root, ids, label, jobs
        lease = verify.Lease(wait=rerun_wait(), kind="rerun") if queued else None
        self._run = verify.Run(
            root,
            verify.Selection.checks(ids),
            label,
            "rerun",
            jobs,
            meta={"check": ",".join(ids), **(meta or {})},
            ticket=ticket,
            run_type=plan_meta(ids, root, toolchain=toolchain),
            lease=lease,
            wait_for_load=False,
            banner=f"lane: rerunning {', '.join(ids)} in {root} on {jobs} slot(s) (log {{log}})",
            stoppable=True,
        )
        self.log = self._run.log
        self.run_id = self._run.run_id
        self.tree = self._run.tree
        self.verify = self._run.verify_argv
        self.argv = self._run.argv

    def execute(self) -> verify.Outcome:
        return self._run.execute()


Outcome = tuple[dict[str, Any] | None, Path | None, Path | None, str | None]


def run_rerun(rerun: Rerun) -> Outcome:
    """The rerun's (report, log, run directory, problem); when this process exits first, the child stops."""
    try:
        outcome = rerun.execute()
        return outcome.report, outcome.log, outcome.run_dir, rerun_problem(outcome.report, outcome.log)
    except RerunUnavailable as error:
        return None, None, None, str(error)


def rerun_problem(report: dict[str, Any] | None, log: Path | None) -> str | None:
    """Why a rerun says nothing about its checks, or None."""
    if report is None:
        return f"the rerun wrote no verification report ({log})"
    if report.get("environment"):
        return f"the rerun failed on its environment: {report['environment'].get('class')} ({log})"
    return None


def not_run(row: dict[str, Any]) -> str:
    blocked = f" by {', '.join(row['blocked_by'])}" if row.get("blocked_by") else ""
    return f"{row['status']}{blocked}"


def report_row(report: dict[str, Any] | None, check_id: str) -> dict[str, Any]:
    return next((r for r in (report or {}).get("checks", []) if r.get("id") == check_id), {"status": "not_run"})


def keep_run(run: Path | None, label: str) -> Path | None:
    """Copy a run directory out of a checkout about to be removed, into the lane logs."""
    if run is None or not run.is_dir():
        return None
    kept = journal.logs() / f"{label}-{run.name}"
    shutil.copytree(run, kept, dirs_exist_ok=True)
    return kept


def classify(record: Record, worktree: str, base: str, fresh: Rows,
             report: dict[str, Any] | None, jobs: int = 1, toolchain: Any = UNRESOLVED,
             lane_plan: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Rerun the red checks of a lane check (at most LANEFATHER_RERUNS) and classify each; None when nothing is red.
    `green` is true when every red check is flaky-under-load or pre-existing.

    A run that stopped at its first checks (#639, the report's `stopped`) has only those red: static checks of
    seconds, which load does not make red, so they rerun only on the base, as verify queues them."""
    failed = [r for r in fresh if r["status"] == "failed"]
    blocked_suites = [r for r in fresh if r["status"] == "blocked"]
    if not failed and not blocked_suites:
        return None
    first = bool((report or {}).get("stopped"))
    environment = next((r["environment"] for r in failed if r.get("environment")), None)
    if environment:
        return {"skipped": f"environment: {environment}: the run's results do not count, so nothing reran",
                "checks": [], "green": False}
    candidates = red_checks(failed)
    # fathers#195: dependency-blocked checks stay separate from executed failures and never consume rerun budget.
    blocked_rows = {row["id"]: list(row.get("blocked_by") or []) for row in (report or {}).get("checks", [])
                    if is_dependency_blocked(row)}
    # A red row no suite owns (setup, cleanup) or a failed suite without a red check cannot be rerun.
    unaccounted = sorted({row["id"] for row in (report or {}).get("checks", []) if is_executed_failure(row)}
                         - set(candidates)) + [r["suite"] for r in failed if not r.get("failed_checks")]
    cap = rerun_cap()
    rerun = candidates[:cap]
    load: dict[str, Any] = {"first_run": host.load_averages()}
    entries: dict[str, dict[str, Any]] = {
        c: {"check": c, "class": "unclassified", "reason": f"only the first {cap} red checks rerun"}
        for c in candidates[cap:]}
    present: list[str] = []
    runs: list[dict[str, str]] = []  # each rerun's run id, which its slot lease names (fathers#92)
    if rerun and first:
        for check_id in rerun:
            entries[check_id] = {"check": check_id, "alone": {
                "status": "not rerun", "reason": "a first check (#639): static, so load does not make it red"}}
    still_red: list[str] = list(rerun) if first else []
    if rerun and not first:
        alone_jobs = rerun_jobs(jobs, minimum=rerun_min_slots(rerun, lane_plan, fresh))
        alone = Rerun(worktree, rerun, f"lane-{record['id']}-alone", alone_jobs, meta=lane_meta(record),
                      ticket=lane_ticket(record), toolchain=toolchain)
        runs.append({"run": alone.run_id, "rerun": "alone"})
        alone_report, log, _, problem = run_rerun(alone)
        load["alone"] = host.load_averages()
        for check_id in rerun:
            row = report_row(alone_report, check_id)
            if problem:
                entries[check_id] = {"check": check_id, "class": "unclassified", "reason": problem}
            elif row["status"] == "passed":
                first_row = report_row(report, check_id)
                entries[check_id] = {"check": check_id, "class": "flaky-under-load", "load": load,
                                     "alone": {"status": "passed", "log": str(log)},
                                     **({"junit": first_row["junit"]} if first_row.get("junit") else {})}
            elif is_dependency_blocked(row):
                blockers = list(row.get("blocked_by") or [])
                entries[check_id] = {
                    "check": check_id, "class": "dependency-blocked", "blocked_by": blockers,
                    "reason": (f"skipped because dependency failed ({', '.join(blockers)})"
                               if blockers else "skipped because dependency failed"),
                    "alone": {"status": row["status"], "log": str(log),
                              **({"blocked_by": blockers} if blockers else {})}}
            elif row["status"] == "failed":
                still_red.append(check_id)
                entries[check_id] = {"check": check_id, "alone": {"status": row["status"], "log": str(log)}}
            else:
                entries[check_id] = {"check": check_id, "class": "unclassified",
                                     "reason": f"the lane's rerun did not run it ({not_run(row)}): {log}"}
    base_side: Outcome | None = None
    kept: Path | None = None
    if still_red:
        # Only after the lane's rerun is red, never beside it (review of lanefather#4): a check with a fixed port,
        # /tmp path or launchd label fails when two copies run at once, and a base red from that collision would
        # call the lane's regression pre-existing.
        with worktrees.base_checkout(record, worktree, base) as path:
            known = plan.known_checks(path)
            present = [c for c in still_red if known is None or c in known]
            if present:
                base_label = f"lane-{record['id']}-base"
                # The cheap first checks rerun as verify queues them; others under a lease like the lane's rerun.
                base_jobs = 1 if first else rerun_jobs(jobs, minimum=rerun_min_slots(present, lane_plan, fresh))
                on_base = Rerun(path, present, base_label, base_jobs,
                                queued=not first, meta={**lane_meta(record), "on": "base"},
                                ticket=lane_ticket(record))
                runs.append({"run": on_base.run_id, "rerun": "base"})
                base_side = run_rerun(on_base)
                kept = keep_run(base_side[2], base_label)
        for check_id in still_red:
            if check_id not in present:
                entries[check_id].update({"class": "regression", "base": {
                    "sha": base, "status": "absent", "reason": "the base defines no such check"}})
                continue
            report_base, base_log, _, problem = base_side or (None, None, None, None)
            row = report_row(report_base, check_id)
            own = kept / Path(row["log"]).name if kept and row.get("log") else None
            evidence = {"sha": base, "status": row["status"],
                        "log": str(own) if own and own.is_file() else str(base_log),
                        **({"blocked_by": row["blocked_by"]} if row.get("blocked_by") else {})}
            if problem:
                entries[check_id].update({"class": "unclassified", "reason": problem, "base": evidence})
            elif row["status"] == "passed":
                entries[check_id].update({"class": "regression", "base": evidence})
            elif row["status"] == "failed":
                entries[check_id].update({"class": "pre-existing", "base": evidence})
            else:
                # Blocked behind a failed producer, not run or unrun: the base says nothing about the check.
                entries[check_id].update({"class": "unclassified", "base": evidence,
                                          "reason": f"the base did not run it ({not_run(row)})"})
    for check_id, blockers in blocked_rows.items():
        if check_id not in entries:
            entries[check_id] = {
                "check": check_id, "class": "dependency-blocked", "blocked_by": blockers,
                "reason": (f"skipped because dependency failed ({', '.join(blockers)})"
                           if blockers else "skipped because dependency failed"),
            }
    ordered = [*candidates, *[c for c in blocked_rows if c not in candidates]]
    checks = [entries[c] for c in ordered]
    green = bool(checks) and not unaccounted and all(e["class"] in LANDABLE for e in checks)
    return {"base": base, "checks": checks, "load": load, "unaccounted": unaccounted, "green": green, "runs": runs,
            **({"first": True} if first else {})}


def classification_lines(classification: dict[str, Any]) -> list[str]:
    if classification.get("skipped"):
        return [f"lane: red checks not classified: {classification['skipped']}"]
    base = classification["base"][:10]
    first = classification.get("first")
    lines = [f"lane: red first checks rerun on the base {base} (#639):" if first
             else f"lane: red checks rerun by themselves on the lane, then on its base {base} (#568):"]
    for entry in classification["checks"]:
        kind, detail = entry["class"], entry.get("reason", "")
        if kind == "regression":
            # The receipt key stays "alone": the lane's rerun of the check by itself, at its own lease size.
            again = "" if entry.get("alone", {}).get("status") == "not rerun" else " again when rerun by itself"
            detail = ("the base defines no such check" if entry["base"]["status"] == "absent"
                      else f"fails on the lane{again}, the base {base} passes it")
        elif kind == "pre-existing":
            detail = f"the base {base} fails it too ({entry['base']['log']})"
        elif kind == "flaky-under-load":
            detail = (f"passes rerun by itself; load averages {'/'.join(map(str, entry['load']['first_run']))} "
                      f"after the lane run, {'/'.join(map(str, entry['load']['alone']))} after its rerun")
        lines.append(f"  {entry['check']}: {kind}: {detail}")
    if classification["unaccounted"]:
        lines.append(f"  not rerun (no check of a suite): {', '.join(classification['unaccounted'])}")
    if classification["green"]:
        lines.append("lane: every red check is flaky-under-load or pre-existing: the receipt is green; a flake "
                     "ticket cites these lines")
    return lines


def first_receipt(record: Record, previous: Record | None, head: str, tree: str, results: Rows) -> dict[str, Any]:
    """The lane's first receipt, kept per lane in <id>.first.json, whatever the branch's history does later (an
    amend or rebase). A file from an earlier lane of the same id, registered at another time, does not count."""
    store = state.Store(config.current())
    return receipts.ensure_first(store, record, previous, head, tree, red_checks(results))


def changed_red_tests(lane_plan: dict[str, Any], first: dict[str, Any]) -> list[dict[str, Any]]:
    """Test files the lane changed against its base (the check's plan) whose check was red in its first receipt."""
    failed = first.get("failed_checks") or []
    if not failed:
        return []
    rows = lane_plan.get("checks")
    if rows is None:
        return [{"path": p, "check": None} for p in lane_plan["paths"] if TEST_PATH.search(p)]
    return [{"path": p, "check": row["id"]} for row in rows if row["id"] in failed for p in row["paths"]
            if TEST_PATH.search(p)]


def changed_red_tests_line(tests: list[dict[str, Any]]) -> str:
    named = ", ".join(f"{t['path']} ({t['check']})" if t["check"] else t["path"] for t in tests)
    return (f"lane: test files the lane changed whose check was red in its first receipt: {named}. The reviewer "
            "confirms each change fixes the test rather than hides a regression (#568)")


# The base a check plans against

def integration_refs(record: Record, worktree: str) -> list[str]:
    """The branches the lane lands into, whose merge-base may be newer than its base: the branch checked out in
    the worktree it lands (or landed) into. Only a lane registered at a literal commit, whose branch is unknown
    until it lands, also takes the one its parent landed into for a fix lane and the ranked queue's base. A lane
    based on a branch lands there: another branch it merged (a dev-based lane merging the queue's integration
    branch) never stands in for it, or that branch's commits would land unchecked (review of lanefather#3)."""
    def landed_into(lane: dict[str, Any]) -> Any:
        landed: dict[str, Any] = lane.get("landed") or {}
        return landed.get("integration")

    places: list[Any] = [record.get("integration"), landed_into(record)]
    literal = not git_ok(worktree, "show-ref", "--verify", "--quiet", f"refs/heads/{record['base']}")
    if literal:
        places += [landed_into(state.read_json(state.record_path(parent)) or {})
                   for parent in state.fix_parents(record) if state.ID.match(parent)]
    refs = [branch for path in places if isinstance(path, str) and os.path.isdir(path)
            for branch in [current_branch(path)] if branch]
    if literal:
        try:
            settings = queue.read_settings()
        except LaneError as error:
            print(f"lane: warning: the queue's base is unknown, so check plans without it: {error}")
            settings = {}
        if isinstance(settings.get("base"), str) and settings["base"]:
            refs.append(settings["base"])
    return list(dict.fromkeys(refs))


def fix_parent(record: Record) -> dict[str, Any] | None:
    """The original lane's per-check receipt for a hand-over. A lane from start --fix-of runs its own plan."""
    parent = state.hand_over_parent(record)
    if parent is None:
        return None
    receipt = receipts.read(state.Store(config.current()), parent)
    if not receipt or receipt.get("selection") is None or receipt.get("check_results") is None:
        return None
    return dict(receipt, lane=parent)


def receipt_paths(receipt: dict[str, Any]) -> set[str]:
    """The paths a receipt's plan covered: its `paths`, or for a receipt from before them, the paths that chose its
    checks."""
    listed: Any = receipt.get("paths")
    if isinstance(listed, list):
        return {str(p) for p in listed}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    rows: list[dict[str, Any]] = receipt.get("selection") or []
    reasons: dict[str, list[str]] = receipt.get("reasons") or {}
    return {p for row in rows for p in row.get("paths", [])} | {p for paths in reasons.values() for p in paths}


def fix_plan(lane_plan: dict[str, Any], parent: dict[str, Any], known: set[str] | None = None) -> dict[str, Any]:
    """A review-fix lane's plan (fathers#93), changed in place: its own diff's plan when every path it changes is in
    the parent lane's diff; otherwise the plan of both diffs, the union of the two selections. Returns what the
    receipt records about it."""
    parent_paths = receipt_paths(parent)
    outside = sorted(p for p in lane_plan["paths"] if p not in parent_paths)
    # A tree whose plan names whole suites has no checks to join: None, neither inside nor the union.
    union = bool(outside) if lane_plan.get("selection") is not None else (None if outside else False)
    about: dict[str, Any] = {"lane": parent["lane"], "tree": receipts.tree_of(parent), "outside_parent": outside,
                             "union": union}
    if not union:
        return about
    rows = {row["id"]: row for row in lane_plan["selection"]}
    # A check the fix lane's tree no longer defines (a lane since renamed or removed it) stays out (review).
    added = [row for row in parent["selection"] if row["id"] not in rows and (known is None or row["id"] in known)]
    dropped = [row["id"] for row in parent["selection"] if known is not None and row["id"] not in known]
    lane_plan["selection"] = [*lane_plan["selection"],
                              *({"id": r["id"], "suite": r["suite"], "paths": r.get("paths", []),
                                 **({"min_slots": need} if (need := plan.declared_slots(r)) is not None else {})}
                                for r in added)]
    lane_plan["selected"] = plan.suites_of(lane_plan["selection"])
    lane_plan["by_suite"] = plan.by_suite(lane_plan["selection"])
    about["added"] = [r["id"] for r in added]
    if dropped:
        about["dropped"] = dropped
    return about


def plan_base(record: Record, worktree: str) -> str:
    """The newest merge-base of the lane's HEAD (lanefather#3): with its base, or with an integration branch tip
    when that merge-base descends from the former. A lane registered at a literal commit keeps it after it merges
    the integration branch; planning against it would count all the integration gained since as the lane's diff."""
    base_ref = git(worktree, "rev-parse", "--verify", "--quiet", record["base"] + "^{commit}", check=False)
    base = git(worktree, "merge-base", "HEAD", base_ref or record["base_sha"])
    for ref in integration_refs(record, worktree):
        tip = git(worktree, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
        newer = git(worktree, "merge-base", "HEAD", tip, check=False) if tip else ""
        if newer and newer != base and git_ok(worktree, "merge-base", "--is-ancestor", base, newer):
            base = newer
    return base


# The command

def implementer(args: Any) -> str | None:
    """The agent that built the lane, for the review's implementer family (fathers#150, fathers#200): `check --agent`,
    else the agent session this check runs in (events.agent_context), else None."""
    context = events.agent_context(getattr(args, "agent", None))
    return context["kind"] if context else None


def _record_check_agent(lane_id: str, agent_info: dict[str, Any] | None) -> None:
    if not agent_info:
        return
    store = state.Store(config.current())
    store.update(lane_id, lambda current: state.record_agent(current, "check", agent_info, now=journal.now()))


@dataclass(frozen=True)
class Prepared:
    record: Record
    worktree: str
    head: str
    tree: str
    receipt_path: Path
    previous: Record | None
    agent_info: dict[str, Any] | None


@dataclass(frozen=True)
class Selected:
    toolchain: Any
    platform: str
    base: str
    lane_plan: dict[str, Any]
    fix_of: dict[str, Any] | None
    suites: list[str]
    selected: dict[str, list[str]] | None
    reused: dict[str, dict[str, Any]]
    remaining: dict[str, list[str]] | None
    outside: list[str]
    added: list[str]
    overlaps: list[dict[str, Any]]
    decision: dict[str, Any]


@dataclass(frozen=True)
class CheckRun:
    started: str
    fresh: Rows
    ran: dict[str, dict[str, Any]]
    triage: str | None
    classification: dict[str, Any] | None
    stopped: Any


def prepare(lane_id: str, *, agent: str | None = None) -> Prepared:
    """Adopt a planned lane's worktree when run there, refuse non-active/wrong-branch/dirty worktrees."""
    from . import lanes, lifecycle  # adopt_worktree; lanes imports this module for its commands
    prog = config.prog()
    agent_info = events.agent_context(agent)
    record = state.load(lane_id)
    if lifecycle.planned(record):
        cwd = git(os.getcwd(), "rev-parse", "--show-toplevel", check=False)
        if not cwd or current_branch(cwd) != record["branch"]:
            raise LaneError(f"lane {lane_id} has no worktree yet: run it from a worktree on {record['branch']}, "
                            f"or register one with {prog} adopt")
        lanes.adopt_worktree(record, cwd, record["branch"], agent=agent)
    if not lifecycle.active(record):
        raise LaneError(f"lane {lane_id} is {record['status']}; check runs before landing")
    worktree = record["worktree"]
    if current_branch(worktree) != record["branch"]:
        raise LaneError(f"{worktree} is on {current_branch(worktree)}, not the lane branch {record['branch']}")
    if dirty(worktree):
        raise LaneError("commit your work (or set it aside in a WIP commit) first: the receipt covers a committed "
                        "tree\n" + dirty(worktree))
    head, tree = git(worktree, "rev-parse", "HEAD"), git(worktree, "rev-parse", "HEAD^{tree}")
    store = state.Store(config.current())
    path = receipts.path(store, lane_id)
    previous = receipts.read(store, lane_id)
    return Prepared(record=record, worktree=worktree, head=head, tree=tree, receipt_path=path,
                    previous=previous, agent_info=agent_info)


def select(prepared: Prepared, *, rerun: bool = False, slots_override: int | None = None) -> Selected | None:
    """Resolve toolchain/platform, short-circuit a current green receipt, plan, reuse passes, widen claims and size
    jobs. Returns None when the existing green receipt already certifies the tree."""
    record, worktree, tree = prepared.record, prepared.worktree, prepared.tree
    previous, path = prepared.previous, prepared.receipt_path
    store = state.Store(config.current())
    # A receipt and its results certify the toolchain they ran with (#571): another one, or none now, voids them.
    toolchain = plan.toolchain_state(worktree)
    # ... and the platform they ran on (owner decision 2026-11-13): a receipt naming another, or none, voids them.
    platform = result_store.host_platform()
    verdict = receipts.check_verdict(record, previous, tree, toolchain, platform=platform)
    if not rerun:
        if isinstance(verdict, receipts.Green):
            _record_check_agent(record["id"], prepared.agent_info)
            print(f"Receipt current: green for tree {tree} on {platform} ({path})")
            return None
        if isinstance(verdict, receipts.StaleToolchain):
            print("lane: the Rust toolchain changed since the receipt (#571); running the checks again"
                  if verdict.toolchain_changed else
                  f"lane: the receipt was not made on {platform}; running the checks again")
    same_rust = receipts.same_environment(previous, toolchain, worktree=worktree, platform=platform)
    base = plan_base(record, worktree)
    lane_plan = plan.plan_for(worktree, base)
    # fathers#93: a review-fix lane runs the plan of its fix, widened to both diffs when the fix leaves the parent's.
    parent = fix_parent(record)
    fix_of = fix_plan(lane_plan, parent, plan.known_checks(worktree)) if parent else None
    if fix_of:
        outside_list = ", ".join(fix_of["outside_parent"][:3]) + (", ..." if len(fix_of["outside_parent"]) > 3
                                                                  else "")
        print(f"lane: review-fix lane of {fix_of['lane']}: " + (
            f"the plan of both diffs, since the fix changes {outside_list} outside {fix_of['lane']}'s diff"
            if fix_of["union"] else "the plan of its fix, inside the parent's diff" if fix_of["union"] is False
            else f"its own plan: the fix changes {outside_list} outside the parent's diff, but this tree's plan "
                 "names whole suites"), flush=True)
    suites: list[str] = lane_plan["selected"]
    selected: dict[str, list[str]] | None = lane_plan["by_suite"]
    reused: dict[str, dict[str, Any]] = {}
    if (selected is not None and previous and same_rust and previous.get("head") and not rerun
            and git_ok(worktree, "cat-file", "-e", previous["head"])):
        # Per check: a check that passed for an earlier tree of the lane and that no change since selects.
        wanted = {c for ids in selected.values() for c in ids}
        reused = {c: e for c, e in reusable(previous, plan.plan_for(worktree, previous["head"])).items()
                  if c in wanted}
    if (selected is not None and parent and not rerun
            and receipts.same_environment(parent, toolchain, platform=platform)
            and parent.get("head") and git_ok(worktree, "cat-file", "-e", parent["head"])):
        # The parent's passes stand for each check no change since the parent's head selects, as an earlier
        # receipt of the lane's own would.
        wanted = {c for ids in selected.values() for c in ids}
        parent_receipt = str(receipts.path(store, parent["lane"]))
        reused.update({c: dict(e, reused_by="parent", reused_lane=e.get("reused_lane") or parent["lane"],
                               reused_receipt=e.get("reused_receipt") or parent_receipt) for c, e in
                       reusable(parent, plan.plan_for(worktree, parent["head"])).items()
                       if c in wanted and c not in reused})
    if selected is not None and not rerun:
        # fathers#91: a check that passed on this very tree under this toolchain, in any lane or run, stands.
        reused.update(same_tree({c: s for s, ids in selected.items() for c in ids if c not in reused}, tree,
                                toolchain))
    outside = [p for p in lane_plan["paths"] if not claims.unclaimed(p)
               and not any(claims.matches(c, p) for c in record["claims"])]
    added, overlaps = claims.widen_claims(record["id"], outside)
    if added:
        print(f"lane: claims widened: {', '.join(added)}")
    for overlap in overlaps:
        print(f"lane: warning: {overlap['path']} overlaps the claims of lane {overlap['lane']} (#{overlap['ticket']}): "
              f"{overlap['claim']}; tell the orchestrator, the two lanes may conflict when they land")
    for unowned in lane_plan.get("unowned", []):
        print(f"lane: no check reads {unowned}, so the plan is the full gate: declare its readers in "
              f"{config.current().plan_module or 'the plan module'}")
    # A lane check runs its selection even for a full plan (every check of the gate): no stamp; only promote's
    # batch gate stamps the tree it promotes.
    remaining = None if selected is None else {s: [c for c in ids if c not in reused] for s, ids in selected.items()}
    if slots_override:
        decision = host.given_jobs(slots_override, "--slots")
    elif remaining is None or any(remaining.values()):
        min_slots, min_for = required_min_slots(lane_plan, remaining, suites)
        decision = host.check_jobs(host.host(), minimum=min_slots, min_for=min_for)
    else:
        decision = {"jobs": 1, "reason": "nothing to run"}
    return Selected(toolchain=toolchain, platform=platform, base=base, lane_plan=lane_plan, fix_of=fix_of,
                    suites=suites, selected=selected, reused=reused, remaining=remaining, outside=outside,
                    added=added, overlaps=overlaps, decision=decision)


def run(prepared: Prepared, selection: Selected) -> CheckRun:
    """Execute the remaining checks through `verify.Run` and classify any red checks."""
    started = journal.now()
    fresh, ran, triaged, classification, stopped = lane_run(
        prepared.record, prepared.worktree, selection.base, selection.suites, selection.remaining,
        selection.decision["jobs"], selection.toolchain, lane_plan=selection.lane_plan)
    return CheckRun(started=started, fresh=fresh, ran=ran, triage=triaged,
                    classification=classification, stopped=stopped)


def record(prepared: Prepared, selection: Selected, executed: CheckRun) -> int:
    """Write the receipt, update the results store, history and ledger, print the summary, and return exit code."""
    prog = config.prog()
    rec = prepared.record
    lane_id, head, tree, path = rec["id"], prepared.head, prepared.tree, prepared.receipt_path
    suites, selected, reused = selection.suites, selection.selected, selection.reused
    decision, lane_plan = selection.decision, selection.lane_plan
    fresh, ran, triaged = executed.fresh, executed.ran, executed.triage
    classification, stopped = executed.classification, executed.stopped
    results = combined_results(suites, selected, fresh, reused)
    check_results = {} if selected is None else {
        c: reused.get(c) or ran.get(c) or {"suite": s, "status": "not_run"} for s, ids in selected.items() for c in ids}
    status = "green" if all(r["status"] == "passed" for r in results) else "red"
    # The lane's first receipt: a test file the lane changes whose check was red there is named.
    first = first_receipt(rec, prepared.previous, head, tree, results)
    # A run that stopped at its first checks stays red: its heavier checks did not run.
    if status == "red" and classification and classification["green"] and not stopped:
        status = "green"
    changed_tests = changed_red_tests(lane_plan, first)
    info = host.host()
    agent_kind = prepared.agent_info["kind"] if prepared.agent_info else None
    store = state.Store(config.current(), directory=path.parent)
    receipts.write(
        store, lane_id,
        {"version": 1, "lane": lane_id, "ticket": rec["ticket"], "branch": rec["branch"],
         "head": head, "tree": tree, "base": selection.base, "suites": suites,
         "reasons": lane_plan["reasons"], "checks": selected, "selection": lane_plan["selection"],
         "check_results": check_results, "unowned": lane_plan.get("unowned", []),
         "full": lane_plan["full"], "outside_claims": selection.outside,
         "claims_added": selection.added, "claim_overlaps": selection.overlaps,
         "toolchain": selection.toolchain, "platform": selection.platform,
         "jobs": decision["jobs"], "jobs_decision": decision, "results": results,
         "triage": triaged, "classification": classification, "first": first,
         "changed_red_tests": changed_tests, "stopped": stopped, "status": status, "host": info,
         "started": executed.started, "paths": lane_plan["paths"],
         **({"fix_of": selection.fix_of} if selection.fix_of else {}),
         "leases": leases_of(fresh, classification),
         # Who ran the check, which a review takes for the implementer (fathers#150, fathers#200).
         **({"agent": agent_kind} if agent_kind else {}),
         "finished": journal.now()},
    )
    _record_check_agent(lane_id, prepared.agent_info)
    # fathers#91: what ran, and what was reused for this tree, is on record for any later run of the tree. A check
    # that failed and passed rerun by itself stays failed here: the merge runs it again (#640); the history marks it
    # a flake (fathers#92).
    result_store.record(tree, recordable(check_results), source="check", toolchain=selection.toolchain,
                        platform=selection.platform, lane=lane_id, sha=head, receipt=str(path),
                        reused=list(reused), origins=origins(reused, str(path)), phase="check")
    for entry in (classification or {}).get("checks", []):
        if entry.get("class") == "flaky-under-load":
            history.flake(entry["check"], lane=lane_id, tree=tree, failed_at=executed.started, how="rerun-alone",
                          junit=entry.get("junit"), phase="check")
    classes = ",".join(f"{e['check']}={e['class']}" for e in (classification or {}).get("checks", []))
    journal.ledger(f"check {lane_id} tree={tree} {status} suites={','.join(suites) or '-'} "
                   f"checks={len(check_results) - len(reused)} reused={len(reused)} jobs={decision['jobs']} "
                   f"classified={classes or '-'} {host.host_fields(info)}",
                   kind="lane.checked", lane=rec, level="info" if status == "green" else "warn",
                   details={"status": status, "tree": tree, "suites": suites,
                            "checks": len(check_results) - len(reused), "reused": len(reused),
                            "jobs": decision["jobs"], "receipt": str(path)})
    for result in results:
        note = (" (passed on this tree before: " + str(result.get("log") or "an earlier run") + ")"
                if result.get("reused_from") == tree
                else f" (reused from tree {result['reused_from'][:10]})" if result.get("reused_from")
                else f" ({len(result['reused'])} of {len(result['checks'])} checks reused)" if result.get("reused")
                else "")
        failed = f" failed checks: {', '.join(result['failed_checks'])}" if result.get("failed_checks") else ""
        blocked = f" blocked checks: {', '.join(result['blocked_checks'])}" if result.get("blocked_checks") else ""
        print(f"  {result['suite']}: {result['status']}{note}{failed}{blocked}  {result['log']}")
    if triaged:
        print(triaged)
    if classification:
        print("\n".join(classification_lines(classification)))
    if changed_tests:
        print(changed_red_tests_line(changed_tests))
    if stopped:
        print(f"lane: stopped at the first checks (#639): {', '.join(stopped['failed'])} red, so the "
              f"{stopped['not_run']} heavier checks did not start. Fix them, commit and run {prog} check again.")
    print(f"Receipt {status} for tree {tree}: {path}")
    return 0 if status == "green" else 1


def check(args: Any) -> int:
    prepared = prepare(args.id, agent=getattr(args, "agent", None))
    selection = select(prepared, rerun=bool(args.rerun), slots_override=args.slots)
    if selection is None:
        return 0
    executed = run(prepared, selection)
    return record(prepared, selection, executed)


def leases_of(fresh: Rows, classification: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The verify runs of a receipt: each one's run id and, when its report named one, its slot lease, then its
    reruns' (fathers#92)."""
    found: dict[str, dict[str, Any]] = {}
    for result in fresh:
        if result.get("run"):
            found.setdefault(result["run"], {"run": result["run"], **({"lease": result["lease"]}
                                                                      if result.get("lease") else {})})
    for run in (classification or {}).get("runs", []):
        found.setdefault(run["run"], dict(run))
    return list(found.values())


def lane_run(record: Record, worktree: str, base: str, suites: list[str], remaining: dict[str, list[str]] | None,
             jobs: int, toolchain: Any = UNRESOLVED, lane_plan: dict[str, Any] | None = None,
             ) -> tuple[Rows, dict[str, dict[str, Any]], str | None, dict[str, Any] | None, Any]:
    """Run the lane's checks that are left and classify the red ones (#568): (results, check results, triage,
    classification, stopped).

    verify stops at its first checks (the preflight and the cheap tier, #639) when one is red, before any heavier
    check starts: `stopped`, from its report, and the lane check is red. Only when every red first check is red on
    the base too (pre-existing) do the heavier checks run, once, in a second run whose results join the first's."""
    def attempt(selection: dict[str, list[str]] | None) -> tuple[Rows, dict[str, dict[str, Any]], str | None,
                                                                 dict[str, Any] | None, Any]:
        active_suites = [s for s in suites if selection is None or selection.get(s)]
        if not active_suites:
            return [], {}, None, None, None
        ids = None if selection is None else [c for s in active_suites for c in selection[s]]
        choice = verify.Selection.checks(ids) if ids is not None else verify.Selection.suites(active_suites)
        run_type = (plan_meta(ids, worktree, toolchain=toolchain) if ids is not None
                    else plan_meta([f"suite:{s}" for s in active_suites], worktree,
                                   suite_count(worktree, active_suites), toolchain))
        what = ", ".join(active_suites) + (f" ({len(ids)} checks)" if ids is not None else "")
        outcome = verify.Run(
            worktree, choice, f"lane-{record['id']}", "check", jobs,
            meta=lane_meta(record), ticket=lane_ticket(record), run_type=run_type,
            banner=f"lane: running {what} in {worktree} (log {{log}}); queue: slotfather status",
        ).execute()
        fresh, ran = suite_outcomes(worktree, active_suites, selection, outcome)
        triaged = outcome.triage
        classification, report = None, outcome.report
        if any(r["status"] != "passed" for r in fresh):
            run_dir = outcome.run_dir
            with terminable():
                classification = classify(record, worktree, base, fresh, report, jobs, toolchain,
                                          lane_plan=lane_plan)
            if classification and run_dir is not None:
                # The project's gate triage reads the classification beside the run's report (rerun.json).
                state.write_json(run_dir / "rerun.json", classification)
                triaged = plan.triage(worktree, run_dir) or triaged
        return fresh, ran, triaged, classification, outcome.stopped

    fresh, ran, triaged, classification, stopped = attempt(remaining)
    if not (stopped and classification and classification["green"] and remaining is not None):
        return fresh, ran, triaged, classification, stopped
    rest = {s: [c for c in ids if (ran.get(c) or {}).get("status") == "not_run"] for s, ids in remaining.items()}
    if not any(rest.values()):
        return fresh, ran, triaged, classification, None
    print("lane: every red first check is red on the base too (pre-existing); running the heavier checks", flush=True)
    more, ran_more, triaged_more, classified_more, stopped = attempt(rest)
    return (joined_results(fresh, more), {**ran, **ran_more},
            "\n".join(t for t in (triaged, triaged_more) if t) or None,
            joined_classification(classification, classified_more), stopped)


def joined_results(first: Rows, second: Rows) -> Rows:
    """Each suite's result over two runs of a lane check (#639): the second run's, red when either run's was, with
    the failed checks of both."""
    later = {r["suite"]: r for r in second}
    joined: Rows = []
    for result in first:
        other = later.pop(result["suite"], None)
        if other is None:
            joined.append(result)
            continue
        failed = result["failed_checks"] + [c for c in other["failed_checks"] if c not in result["failed_checks"]]
        joined.append(dict(other, status="failed" if "failed" in (result["status"], other["status"])
                           else other["status"], failed_checks=failed, first_log=result["log"]))
    return joined + list(later.values())


def joined_classification(first: dict[str, Any], second: dict[str, Any] | None) -> dict[str, Any]:
    """The classification of two runs of a lane check (#639): green when both runs' red checks are landable."""
    if second is None:
        return first
    return dict(second, checks=first["checks"] + second.get("checks", []),
                unaccounted=first["unaccounted"] + second.get("unaccounted", []),
                green=first["green"] and second["green"])
