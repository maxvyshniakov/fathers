"""Verify runs and the lane check's receipt.

check runs the checks the tree's plan selects for the lane's diff and writes the receipt for the committed tree,
reusing each check that passed for an earlier tree of the lane and that no change since selects. Each red check
reruns by itself on the lane at the lane check's own lease size (lanefather#4), then, red again, on its base, and is
classified (#568): lane red again and base green, `regression`; base red too, `pre-existing`; lane green on its
rerun, `flaky-under-load`. Only the last two leave the receipt landable. A red cheap check stops the run before any
heavier check starts and reruns on the base only; the heavier checks run when it is pre-existing there (#639).
"""
from __future__ import annotations

from collections.abc import Generator
import contextlib
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
from types import FrameType
from typing import Any, TextIO
import uuid

from . import claims, config, history, host, journal, plan, queue, settings, slots, state, worktrees
from . import results as result_store
from .errors import LaneError
from .gitops import current_branch, dirty, git, git_ok
from .state import Record

Rows = list[dict[str, Any]]

# verify report statuses that are not a failure.
PASSING = ("passed", "not_run", "skipped")
STAMPED = re.compile(r"^Verified tree ([0-9a-f]+) stamped\b", re.M)
NOT_STAMPED = re.compile(r"^Verified tree not stamped: (.+?)\.?$", re.M)
REPORT = re.compile(r"^Verification report: (.+)$", re.M)
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
# A check is red alone when it failed, or its producer failed alone (blocked). On the base only a failure of the
# check itself is pre-existing.
RED_ALONE = ("failed", "blocked")


def stamp_result(text: str, full: bool) -> dict[str, Any]:
    """What verify's own verified-tree stamp printed for this run."""
    if not full:
        return {"stamped": False, "reason": "selected suites; only the full plan stamps"}
    stamped = STAMPED.findall(text)
    if stamped:
        return {"stamped": True, "tree": stamped[-1]}
    reason = NOT_STAMPED.findall(text)
    return {"stamped": False, "reason": reason[-1] if reason else "the full run did not pass"}


def report_of(text: str) -> tuple[list[str], dict[str, Any] | None]:
    """The report paths a verify log names, and the last report."""
    reports = REPORT.findall(text)
    return reports, state.read_json(Path(reports[-1].strip())) if reports else None


def verify_env(label: str, phase: str, *, ticket: str | None = None, **meta: Any) -> dict[str, str]:
    """The environment of a verify run lanefather starts: its lease's label, the phase's priority (lanefather#24), and
    metadata naming the phase, the priority's source and what the caller adds (lane, check, sha)."""
    priority, source = slots.phase_priority(phase)
    tags: dict[str, Any] = {"phase": phase, **slots.priority_meta(priority, source), **meta}
    return dict(os.environ, **slots.queue_environment(label, priority, ticket=ticket, **tags))


UNRESOLVED: Any = object()


def plan_meta(selection: Any, root: Any = None, count: int | None = None, toolchain: Any = UNRESOLVED,
              ) -> dict[str, str]:
    """The run type a slot request carries (fathers#107): `plan`, the plan's key of the results store (fathers#91:
    its sorted check ids, or FULL, with the toolchain), and `checks`, how many checks it runs. A selection of check
    ids counts them; the full gate, whole suites (`suite:<name>`) and a promote step (`step:<name>`) take `count`,
    and leave `checks` out without one. The toolchain is the caller's when it resolved it, else `root`'s; a refused
    one is one run type, whatever its message. Slotfather keeps expected durations per run type."""
    if toolchain is UNRESOLVED:
        toolchain = plan.toolchain_state(root) if root is not None else None
    if isinstance(toolchain, dict) and "refused" in toolchain:
        toolchain = {"refused": True}
    named = selection == result_store.FULL or any(str(item).startswith(("suite:", "step:")) for item in selection)
    number = count if named else len(set(selection))
    return {"plan": result_store.plan_hash(selection, toolchain), **({"checks": str(number)} if number is not None
                                                                    else {})}


def suite_count(root: Any, suites: list[str]) -> int | None:
    """How many checks whole suites hold, by the tree's plan module; None for a tree without it."""
    owners = plan.suite_owners(root, suites)
    return None if owners is None else sum(len(ids) for ids in owners.values())


def lane_meta(record: Record) -> dict[str, str]:
    """Caller tags for the lane; ticket is passed as a separate request field."""
    return {"lane": record["id"], "lanefather.state": str(state.state_dir().resolve())}


def lane_ticket(record: Record) -> str | None:
    return slots.ticket_value(str(record.get("ticket") or ""))


def run_suites(root: Any, suites: list[str], label: str, jobs: int, full: bool = False,
               selected: dict[str, list[str]] | None = None, phase: str = "check",
               meta: dict[str, Any] | None = None, ticket: str | None = None, toolchain: Any = UNRESOLVED,
               ) -> tuple[Rows, dict[str, dict[str, Any]], str | None, dict[str, Any]]:
    """Each suite's result, each selected check's result, the gate triage when the run is red, and the verified-tree
    stamp.

    A full plan runs verify without a selection: that is the full gate, which stamps the tree when every check
    passed on a clean, unchanged tree. With `selected` (the checks the plan selected in each suite) it runs those
    checks, otherwise whole suites. The suites share one verify run, which starts once the load allows (#572); each
    result records that wait and the load averages at the run's start and end. A check's result is {suite, status,
    log}, its status as the verify report gives it, or "void" when the run failed outside any check (environment,
    setup, slot limiter): nothing of such a run counts as passed."""
    if not suites:
        return [], {}, None, stamp_result("", False)
    load_wait = host.wait_for_load(label)
    log = journal.log_path(label)
    ids = None if full or selected is None else [c for s in suites for c in selected[s]]
    selection = () if full else ("--checks", ",".join(ids)) if ids is not None else ("--suites", ",".join(suites))
    argv = [plan.verify_path(root), *selection, "--keep-going", "--jobs", str(jobs)]
    # verify queues for its own --jobs-slot lease; label, priority, ticket and tags name the run (#650).
    if full:
        known = plan.known_checks(root)
        kind = plan_meta(result_store.FULL, root, len(known) if known is not None else None, toolchain)
    elif ids is not None:
        kind = plan_meta(ids, root, toolchain=toolchain)
    else:
        kind = plan_meta([f"suite:{s}" for s in suites], root, suite_count(root, suites), toolchain)
    # verify queues for its own --jobs-slot lease; label, priority, ticket and tags name the run (#650). The run id
    # and tree in the lease's meta tie the lease to the receipt and the history (fathers#92).
    run_id, tree = new_run_id(), tree_of(root)
    env = verify_env(label, phase, ticket=ticket, **(meta or {}), **kind, run=run_id,
                     **({"tree": tree} if tree else {}))
    what = "the full gate" if full else ", ".join(suites) + (f" ({len(ids)} checks)" if ids is not None else "")
    print(f"lane: running {what} in {root} (log {log}); queue: slotfather status", flush=True)
    started, load, started_at = time.monotonic(), {"start": host.load_averages()}, journal.now()
    with open(log, "w") as handle:
        status = subprocess.run(argv, cwd=root, env=env, stdout=handle, stderr=subprocess.STDOUT).returncode
    seconds = round(time.monotonic() - started, 1)
    load["end"] = host.load_averages()
    failed_checks: list[str] = []
    text = log.read_text(errors="replace")
    reports, report = report_of(text)
    history.record_run(report, lane=(meta or {}).get("lane"), phase=phase, tree=tree, run=run_id, started=started_at,
                       finished=journal.now(), seconds=seconds, checks=ids)
    lease = {"run": run_id, **({"lease": report["lease"]} if report and report.get("lease") else {})}
    if report:
        failed_checks = [row["id"] for row in report.get("checks", []) if row.get("status") not in PASSING]
    # verify voids a run whose environment failed (the toolchain refused or changed, #571): no suite of it passed,
    # so a later check reuses none of its results.
    environment = ((report or {}).get("environment") or {}).get("class")
    owners = plan.suite_owners(root, suites) if status or environment else None
    failing = {s: [c for c in failed_checks if plan.owned(owners, s, c)] for s in suites}
    # A failure no check accounts for (setup, slot limiter) fails every suite of the run.
    failed = list(suites) if environment else [s for s in suites if failing[s]] or suites if status else []
    rows = {row.get("id"): row for row in (report or {}).get("checks", [])}
    # A run that stopped at its first checks (#639) did not start the others: a suite with one of them not run and
    # none red neither passed nor failed.
    stopped = (report or {}).get("stopped")
    unstarted = [s for s in suites if stopped and s not in failed and (
        selected is None or ids is None
        or any(rows.get(c, {}).get("status", "not_run") == "not_run" for c in selected[s]))]
    if stopped:
        print(f"lane: verify stopped at its first checks (#639): {', '.join(stopped['failed'])} red; "
              f"{stopped['not_run']} heavier checks did not start ({log})", flush=True)
    results: Rows = [{"suite": suite, "status": "failed" if suite in failed else "not_run" if suite in unstarted
                      else "passed", "exit": status,
                      "seconds": seconds, "load_wait": load_wait, "load": load, "log": str(log), **lease,
                      "failed_checks": failing[suite] if suite in failed else [],
                      **({"report": reports[-1].strip()} if reports else {}),
                      **({"environment": environment} if environment else {}),
                      **({"checks": selected[suite]} if selected is not None else {})}
                     for suite in suites]
    void = bool(environment) or (bool(status) and not any(failing.values()))
    checks: dict[str, dict[str, Any]] = {} if ids is None or selected is None else {
        check: {"suite": suite, "status": "void" if void else rows.get(check, {}).get("status", "not_run"),
                "log": str(log), **lease,
                **({"seconds": rows[check]["seconds"]} if isinstance(rows.get(check, {}).get("seconds"), (int, float))
                   else {})}
        for suite in suites for check in selected[suite]}
    stamp = {"stamped": False, "reason": "the run failed"} if status or environment else stamp_result(text, full)
    return results, checks, plan.triage(root, Path(reports[-1].strip()).parent) if status and reports else None, stamp


def reusable(previous: Record, delta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The check results of an earlier receipt that a later check of the lane reuses: each passed there, and no
    change since its head (the `delta` plan) selects it. A full delta reuses nothing; nor does a receipt from
    before per-check results."""
    if delta.get("full") or delta.get("selection") is None:
        return {}
    changed = {row["id"] for row in delta["selection"]}
    return {check: dict(entry, reused_from=entry.get("reused_from") or previous["tree"])
            for check, entry in (previous.get("check_results") or {}).items()
            if entry.get("status") == "passed" and check not in changed}


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def tree_of(root: Any) -> str | None:
    return git(root, "rev-parse", "HEAD^{tree}", check=False) or None


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
    checks: set[str] = set()
    suites: set[str] = set()
    whole: set[str] = set()
    for kind in ("receipt", "post-merge"):
        receipt = state.read_json(state.receipt_path(lane_id, kind)) or {}
        for result in receipt.get("results", []):
            suites.add(result["suite"])
            if "checks" in result:
                checks |= set(result["checks"])
            else:
                whole.add(result["suite"])
    return checks, suites, whole


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


def _terminated(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def terminable() -> Generator[None]:
    """SIGTERM exits through the finally blocks, which stop the rerun and remove the base checkout."""
    previous = signal.signal(signal.SIGTERM, _terminated)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class RerunUnavailable(Exception):
    """A rerun could not run; its checks stay unclassified."""


def rerun_wait() -> float:
    """LANEFATHER_RERUN_WAIT, else this machine's `rerun.wait` setting: seconds a rerun waits for its lease before
    its checks stay unclassified."""
    value = config.current().env("RERUN_WAIT") or str(settings.value("rerun.wait") or "")
    try:
        seconds = float(value) if value else DEFAULT_RERUN_WAIT
    except ValueError:
        return DEFAULT_RERUN_WAIT
    return seconds if seconds > 0 else DEFAULT_RERUN_WAIT


def rerun_jobs(jobs: int) -> int:
    """A rerun's lease size (lanefather#4): the lane check's own, 1 to RERUN_MAX_JOBS slots. A lease of the whole
    machine never came within its wait while one-slot lane checks ran, and as the queue head held them back."""
    return max(1, min(RERUN_MAX_JOBS, jobs))


class Rerun:
    """One verify run of red checks in a checkout: `verify --checks <ids> --keep-going --jobs <jobs>`, under a
    lease of `jobs` slots at the rerun's priority (interactive unless set otherwise) that waits at most the rerun
    wait (`slotfather run -n <jobs> --wait`, which exits 75 without running verify when the lease does not come).
    Without slotfather, or under a lease this process holds, verify runs as the lane check ran. `queued=False` runs
    verify as it queues itself: the cheap first checks (#639), which on a clean checkout run before the queue and
    need no slot."""

    def __init__(self, root: Any, ids: list[str], label: str, jobs: int, queued: bool = True,
                 meta: dict[str, Any] | None = None, ticket: str | None = None, toolchain: Any = UNRESOLVED):
        self.root, self.ids, self.label, self.jobs = root, ids, label, jobs
        self.log = journal.log_path(label)
        self.run_id, self.tree = new_run_id(), tree_of(root)
        self.meta = {"check": ",".join(ids), **(meta or {}), **plan_meta(ids, root, toolchain=toolchain),
                     "run": self.run_id, **({"tree": self.tree} if self.tree else {})}
        self.ticket = ticket
        self.verify = [plan.verify_path(root), "--checks", ",".join(ids), "--keep-going", "--jobs", str(jobs)]
        priority, source = slots.phase_priority("rerun")
        self.argv = slots.wrap(jobs, label, self.verify, wait=rerun_wait(),
                               meta={"phase": "rerun", "kind": "rerun", **slots.priority_meta(priority, source),
                                     **self.meta},
                               priority=priority, ticket=ticket) if queued else self.verify
        self.child: subprocess.Popen[bytes] | None = None
        self.started, self.clock = journal.now(), time.monotonic()
        self.handle: TextIO | None = None

    def start(self) -> None:
        print(f"lane: rerunning {', '.join(self.ids)} in {self.root} on {self.jobs} slot(s) (log {self.log})",
              flush=True)
        self.started, self.clock = journal.now(), time.monotonic()
        self.handle = open(self.log, "w")  # noqa: SIM115 - closed in finish, after the child exits
        self.child = subprocess.Popen(self.argv, cwd=self.root, stdout=self.handle, stderr=subprocess.STDOUT,
                                      env=verify_env(self.label, "rerun", ticket=self.ticket, **self.meta))

    def finish(self) -> tuple[dict[str, Any] | None, Path, Path | None]:
        """The report (None when there is none), the log and the run directory, once the child exited."""
        assert self.child is not None and self.handle is not None
        status = self.child.wait()
        self.handle.close()
        reports = REPORT.findall(self.log.read_text(errors="replace"))
        if self.argv != self.verify and status == slots.TIMED_OUT and not reports:
            raise RerunUnavailable(f"no lease of {self.jobs} slot(s) within {rerun_wait():g} s (slotfather status)")
        run = Path(reports[-1].strip()).parent if reports else None
        report = state.read_json(run / "report.json") if run else None
        history.record_run(report, lane=self.meta.get("lane"), phase="rerun", tree=self.tree, run=self.run_id,
                           started=self.started, finished=journal.now(),
                           seconds=round(time.monotonic() - self.clock, 1), checks=self.ids)
        return report, self.log, run

    def stop(self) -> None:
        """SIGTERM to a child still running (verify stops its jobs on it), SIGKILL 30 s later."""
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()
        if self.handle is not None:
            self.handle.close()


Outcome = tuple[dict[str, Any] | None, Path | None, Path | None, str | None]


def run_rerun(rerun: Rerun) -> Outcome:
    """The rerun's (report, log, run directory, problem); when this process exits first, the child stops."""
    try:
        rerun.start()
        report, log, run = rerun.finish()
        return report, log, run, rerun_problem(report, log)
    except RerunUnavailable as error:
        return None, None, None, str(error)
    finally:
        rerun.stop()


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
             report: dict[str, Any] | None, jobs: int = 1, toolchain: Any = UNRESOLVED) -> dict[str, Any] | None:
    """Rerun the red checks of a lane check (at most LANEFATHER_RERUNS) and classify each; None when nothing is red.
    `green` is true when every red check is flaky-under-load or pre-existing.

    A run that stopped at its first checks (#639, the report's `stopped`) has only those red: static checks of
    seconds, which load does not make red, so they rerun only on the base, as verify queues them."""
    failed = [r for r in fresh if r["status"] == "failed"]
    if not failed:
        return None
    first = bool((report or {}).get("stopped"))
    environment = next((r["environment"] for r in failed if r.get("environment")), None)
    if environment:
        return {"skipped": f"environment: {environment}: the run's results do not count, so nothing reran",
                "checks": [], "green": False}
    candidates = red_checks(failed)
    # A red row no suite owns (setup, cleanup) or a failed suite without a red check cannot be rerun.
    unaccounted = sorted({row["id"] for row in (report or {}).get("checks", []) if row.get("status") not in PASSING}
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
        alone = Rerun(worktree, rerun, f"lane-{record['id']}-alone", rerun_jobs(jobs), meta=lane_meta(record),
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
            elif row["status"] in RED_ALONE:
                still_red.append(check_id)
                entries[check_id] = {"check": check_id, "alone": {"status": row["status"], "log": str(log),
                                                                  **({"blocked_by": row["blocked_by"]}
                                                                     if row.get("blocked_by") else {})}}
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
                on_base = Rerun(path, present, base_label, 1 if first else rerun_jobs(jobs),
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
    checks = [entries[c] for c in candidates]
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
    path = state.receipt_path(record["id"], "first")
    first = state.read_json(path)
    if first and first.get("lane_created") == record.get("created"):
        return first
    # A receipt from before <id>.first.json carries its first receipt, or is the first one itself.
    first = (previous or {}).get("first") or ({"head": previous.get("head"), "tree": previous.get("tree"),
                                               "failed_checks": red_checks(previous.get("results", []))}
                                              if previous and previous.get("tree") else None)
    first = dict(first or {"head": head, "tree": tree, "failed_checks": red_checks(results)},
                 lane_created=record.get("created"))
    state.write_json(path, first)
    return first


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
    if literal and record.get("review_fix_of"):
        places.append(landed_into(state.read_json(state.record_path(record["review_fix_of"])) or {}))
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
    """The receipt of the lane a review-fix lane fixes, when it has per-check results; None otherwise."""
    parent = record.get("review_fix_of")
    if not parent:
        return None
    receipt = state.read_json(state.receipt_path(parent))
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


def fix_plan(lane_plan: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    """A review-fix lane's plan (fathers#93), changed in place: its own diff's plan when every path it changes is in
    the parent lane's diff; otherwise the plan of both diffs, the union of the two selections. Returns what the
    receipt records about it."""
    parent_paths = receipt_paths(parent)
    outside = sorted(p for p in lane_plan["paths"] if p not in parent_paths)
    about: dict[str, Any] = {"lane": parent["lane"], "tree": parent.get("tree"), "outside_parent": outside,
                             "union": bool(outside) and lane_plan.get("selection") is not None}
    if not about["union"]:
        return about
    rows = {row["id"]: row for row in lane_plan["selection"]}
    added = [row for row in parent["selection"] if row["id"] not in rows]
    lane_plan["full"] = bool(lane_plan.get("full") or parent.get("full"))
    lane_plan["selection"] = [*lane_plan["selection"],
                              *({"id": r["id"], "suite": r["suite"], "paths": r.get("paths", [])} for r in added)]
    lane_plan["selected"] = plan.suites_of(lane_plan["selection"])
    lane_plan["by_suite"] = plan.by_suite(lane_plan["selection"])
    about["added"] = [r["id"] for r in added]
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

def check(args: Any) -> int:
    from . import lanes  # adopt_worktree; lanes imports this module for its commands
    prog = config.prog()
    record = state.load(args.id)
    if record["status"] == "planned":
        cwd = git(os.getcwd(), "rev-parse", "--show-toplevel", check=False)
        if not cwd or current_branch(cwd) != record["branch"]:
            raise LaneError(f"lane {args.id} has no worktree yet: run it from a worktree on {record['branch']}, "
                            f"or register one with {prog} adopt")
        lanes.adopt_worktree(record, cwd, record["branch"])
    if record["status"] != "active":
        raise LaneError(f"lane {args.id} is {record['status']}; check runs before landing")
    worktree = record["worktree"]
    if current_branch(worktree) != record["branch"]:
        raise LaneError(f"{worktree} is on {current_branch(worktree)}, not the lane branch {record['branch']}")
    if dirty(worktree):
        raise LaneError("commit your work (or set it aside in a WIP commit) first: the receipt covers a committed "
                        "tree\n" + dirty(worktree))
    head, tree = git(worktree, "rev-parse", "HEAD"), git(worktree, "rev-parse", "HEAD^{tree}")
    path = state.receipt_path(args.id)
    previous = state.read_json(path)
    # A receipt and its results certify the toolchain they ran with (#571): another one, or none now, voids them.
    toolchain = plan.toolchain_state(worktree)
    same_rust = previous is not None and previous.get("toolchain") == toolchain
    if previous and previous.get("tree") == tree and previous.get("status") == "green" and not args.rerun:
        if same_rust:
            print(f"Receipt current: green for tree {tree} ({path})")
            return 0
        print("lane: the Rust toolchain changed since the receipt (#571); running the checks again")
    base = plan_base(record, worktree)
    lane_plan = plan.plan_for(worktree, base)
    # fathers#93: a review-fix lane runs the plan of its fix, widened to both diffs when the fix leaves the parent's.
    parent = fix_parent(record)
    fix_of = fix_plan(lane_plan, parent) if parent else None
    if fix_of:
        print(f"lane: review-fix lane of {fix_of['lane']}: " + (
            f"the plan of both diffs, since the fix changes {', '.join(fix_of['outside_parent'][:3])}"
            + (", ..." if len(fix_of["outside_parent"]) > 3 else "") + f" outside {fix_of['lane']}'s diff"
            if fix_of["union"] else "the plan of its fix, inside the parent's diff"), flush=True)
    suites: list[str] = lane_plan["selected"]
    selected: dict[str, list[str]] | None = lane_plan["by_suite"]
    reused: dict[str, dict[str, Any]] = {}
    if (selected is not None and previous and same_rust and previous.get("head") and not args.rerun
            and git_ok(worktree, "cat-file", "-e", previous["head"])):
        # Per check: a check that passed for an earlier tree of the lane and that no change since selects.
        wanted = {c for ids in selected.values() for c in ids}
        reused = {c: e for c, e in reusable(previous, plan.plan_for(worktree, previous["head"])).items()
                  if c in wanted}
    if (selected is not None and parent and not args.rerun and parent.get("toolchain") == toolchain
            and parent.get("head") and git_ok(worktree, "cat-file", "-e", parent["head"])):
        # The parent's passes stand for each check no change since the parent's head selects, as an earlier
        # receipt of the lane's own would.
        wanted = {c for ids in selected.values() for c in ids}
        reused.update({c: dict(e, reused_by="parent") for c, e in
                       reusable(parent, plan.plan_for(worktree, parent["head"])).items()
                       if c in wanted and c not in reused})
    if selected is not None and not args.rerun:
        # fathers#91: a check that passed on this very tree under this toolchain, in any lane or run, stands.
        reused.update(same_tree({c: s for s, ids in selected.items() for c in ids if c not in reused}, tree,
                                toolchain))
    outside = [p for p in lane_plan["paths"] if not claims.unclaimed(p)
               and not any(claims.matches(c, p) for c in record["claims"])]
    added, overlaps = claims.widen_claims(args.id, outside)
    if added:
        print(f"lane: claims widened: {', '.join(added)}")
    for overlap in overlaps:
        print(f"lane: warning: {overlap['path']} overlaps the claims of lane {overlap['lane']} (#{overlap['ticket']}): "
              f"{overlap['claim']}; tell the orchestrator, the two lanes may conflict when they land")
    for unowned in lane_plan.get("unowned", []):
        print(f"lane: no check reads {unowned}, so the plan is the full gate: declare its readers in "
              f"{config.current().plan_module or 'the plan module'}")
    started = journal.now()
    # A lane check runs its selection even for a full plan (every check of the gate): no stamp; only promote's
    # batch gate stamps the tree it promotes.
    remaining = None if selected is None else {s: [c for c in ids if c not in reused] for s, ids in selected.items()}
    if args.slots:
        decision = host.given_jobs(args.slots, "--slots")
    elif remaining is None or any(remaining.values()):
        decision = host.check_jobs(host.host())
    else:
        decision: dict[str, Any] = {"jobs": 1, "reason": "nothing to run"}
    fresh, ran, triaged, classification, stopped = lane_run(record, worktree, base, suites, remaining,
                                                            decision["jobs"], toolchain)
    results = combined_results(suites, selected, fresh, reused)
    check_results = {} if selected is None else {
        c: reused.get(c) or ran.get(c) or {"suite": s, "status": "not_run"} for s, ids in selected.items() for c in ids}
    status = "green" if all(r["status"] == "passed" for r in results) else "red"
    # The lane's first receipt: a test file the lane changes whose check was red there is named.
    first = first_receipt(record, previous, head, tree, results)
    # A run that stopped at its first checks stays red: its heavier checks did not run.
    if status == "red" and classification and classification["green"] and not stopped:
        status = "green"
    changed_tests = changed_red_tests(lane_plan, first)
    info = host.host()
    state.write_json(path, {"version": 1, "lane": args.id, "ticket": record["ticket"], "branch": record["branch"],
                            "head": head, "tree": tree, "base": base, "suites": suites,
                            "reasons": lane_plan["reasons"], "checks": selected, "selection": lane_plan["selection"],
                            "check_results": check_results, "unowned": lane_plan.get("unowned", []),
                            "full": lane_plan["full"], "outside_claims": outside, "claims_added": added,
                            "claim_overlaps": overlaps, "toolchain": toolchain, "jobs": decision["jobs"],
                            "jobs_decision": decision, "results": results, "triage": triaged,
                            "classification": classification, "first": first, "changed_red_tests": changed_tests,
                            "stopped": stopped, "status": status, "host": info, "started": started,
                            "paths": lane_plan["paths"], **({"fix_of": fix_of} if fix_of else {}),
                            "leases": leases_of(fresh, classification),
                            "finished": journal.now()})
    # fathers#91: what ran, and what was reused for this tree, is on record for any later run of the tree. A check
    # that failed and passed rerun by itself stays failed here: the merge runs it again (#640); the history marks it
    # a flake (fathers#92).
    result_store.record(tree, recordable(check_results), source="check", toolchain=toolchain, lane=args.id,
                        sha=head, receipt=str(path), reused=list(reused), origins=origins(reused, str(path)),
                        phase="check")
    for entry in (classification or {}).get("checks", []):
        if entry.get("class") == "flaky-under-load":
            history.flake(entry["check"], lane=args.id, tree=tree, failed_at=started, how="rerun-alone",
                          junit=entry.get("junit"), phase="check")
    classes = ",".join(f"{e['check']}={e['class']}" for e in (classification or {}).get("checks", []))
    journal.ledger(f"check {args.id} tree={tree} {status} suites={','.join(suites) or '-'} "
                   f"checks={len(check_results) - len(reused)} reused={len(reused)} jobs={decision['jobs']} "
                   f"classified={classes or '-'} {host.host_fields(info)}",
                   kind="lane.checked", lane=record, level="info" if status == "green" else "warn",
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
        print(f"  {result['suite']}: {result['status']}{note}{failed}  {result['log']}")
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
             jobs: int, toolchain: Any = UNRESOLVED,
             ) -> tuple[Rows, dict[str, dict[str, Any]], str | None, dict[str, Any] | None, Any]:
    """Run the lane's checks that are left and classify the red ones (#568): (results, check results, triage,
    classification, stopped).

    verify stops at its first checks (the preflight and the cheap tier, #639) when one is red, before any heavier
    check starts: `stopped`, from its report, and the lane check is red. Only when every red first check is red on
    the base too (pre-existing) do the heavier checks run, once, in a second run whose results join the first's."""
    def attempt(selection: dict[str, list[str]] | None) -> tuple[Rows, dict[str, dict[str, Any]], str | None,
                                                                 dict[str, Any] | None, Any]:
        fresh, ran, triaged, _ = run_suites(worktree, [s for s in suites if selection is None or selection.get(s)],
                                            f"lane-{record['id']}", jobs, selected=selection,
                                            meta=lane_meta(record), ticket=lane_ticket(record), toolchain=toolchain)
        classification, report = None, None
        if any(r["status"] != "passed" for r in fresh):
            run = next((Path(r["report"]).parent for r in fresh if r.get("report")), None)
            report = state.read_json(run / "report.json") if run else None
            with terminable():
                classification = classify(record, worktree, base, fresh, report, jobs, toolchain)
            if classification and run:
                # The project's gate triage reads the classification beside the run's report (rerun.json).
                state.write_json(run / "rerun.json", classification)
                triaged = plan.triage(worktree, run) or triaged
        return fresh, ran, triaged, classification, (report or {}).get("stopped")

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
