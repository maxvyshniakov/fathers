"""One verify runner for every Lanefather verify invocation (fathers#70).

Check, post-merge smoke, red-check reruns (alone and on base), the batch gate and the perf suite all run the
project's verify command through `Run` and read its `Outcome`. `Run` builds the verify argv and `<slot_env>_*`
environment, optionally wraps verify in `slotfather run` (for reruns), waits for host load when asked, captures the
log, parses the report and verified-tree stamp, records run history, and runs gate triage on failure.
"""
from __future__ import annotations

from collections.abc import Generator, Iterable
import contextlib
from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import Any, Literal, TextIO, cast
import uuid

from . import history, host, journal, plan, slots, state
from . import results as result_store
from .gitops import git

# verify report statuses that are not a failure (a skipped row with blocked_by is dependency-blocked, fathers#195).
PASSING = ("passed", "not_run", "skipped")
STAMPED = re.compile(r"^Verified tree ([0-9a-f]+) stamped\b", re.M)
NOT_STAMPED = re.compile(r"^Verified tree not stamped: (.+?)\.?$", re.M)
REPORT = re.compile(r"^Verification report: (.+)$", re.M)
UNRESOLVED: Any = object()


class LeaseTimedOut(Exception):
    """A wrapped `slotfather run --wait` timed out before starting verify."""


def _terminated(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def terminable() -> Generator[None]:
    """SIGTERM exits through the finally blocks, which stop the running child and clean up its checkout."""
    previous = signal.signal(signal.SIGTERM, _terminated)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def is_dependency_blocked(row: dict[str, Any]) -> bool:
    """Whether a verify report row was skipped or blocked because a dependency failed (fathers#195)."""
    status = row.get("status")
    if status == "blocked":
        return True
    return status == "skipped" and bool(row.get("blocked_by"))


def is_executed_failure(row: dict[str, Any]) -> bool:
    """Whether a verify report row actually ran and failed (neither passing nor dependency-blocked, fathers#195)."""
    return row.get("status") not in PASSING and not is_dependency_blocked(row)


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


_ORIGIN_REPORT_OF = report_of
_ORIGIN_STAMP_RESULT = stamp_result


def _read_report(text: str) -> tuple[list[str], dict[str, Any] | None]:
    checks_mod = sys.modules.get("lanefather.checks")
    override = getattr(checks_mod, "report_of", None) if checks_mod is not None else None
    if callable(override) and override is not _ORIGIN_REPORT_OF:
        return cast(tuple[list[str], dict[str, Any] | None], override(text))
    return report_of(text)


def _read_stamp(text: str, full: bool) -> dict[str, Any]:
    checks_mod = sys.modules.get("lanefather.checks")
    override = getattr(checks_mod, "stamp_result", None) if checks_mod is not None else None
    if callable(override) and override is not _ORIGIN_STAMP_RESULT:
        return cast(dict[str, Any], override(text, full))
    return stamp_result(text, full)


def verify_env(label: str, phase: str, *, ticket: str | None = None, **meta: Any) -> dict[str, str]:
    """The environment of a verify run lanefather starts: its lease's label, the phase's priority (lanefather#24), and
    metadata naming the phase, the priority's source and what the caller adds (lane, check, sha)."""
    priority, source = slots.phase_priority(phase)
    tags: dict[str, Any] = {"phase": phase, **slots.priority_meta(priority, source), **meta}
    return dict(os.environ, **slots.queue_environment(label, priority, ticket=ticket, **tags))


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


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def tree_of(root: Any) -> str | None:
    return git(root, "rev-parse", "HEAD^{tree}", check=False) or None


@dataclass(frozen=True)
class Selection:
    """What a verify run executes: the full gate, whole suites, or named check ids."""
    kind: Literal["full", "suites", "checks"]
    items: tuple[str, ...] = ()

    @classmethod
    def full(cls) -> Selection:
        return cls("full")

    @classmethod
    def suites(cls, names: Iterable[str]) -> Selection:
        return cls("suites", tuple(names))

    @classmethod
    def checks(cls, ids: Iterable[str]) -> Selection:
        return cls("checks", tuple(ids))

    def argv(self) -> tuple[str, ...]:
        if self.kind == "full":
            return ()
        if self.kind == "checks":
            return ("--checks", ",".join(self.items))
        return ("--suites", ",".join(self.items))

    def describe(self) -> str:
        if self.kind == "full":
            return "the full gate"
        return ", ".join(self.items)


@dataclass(frozen=True)
class Lease:
    """Optional outer `slotfather run` lease wrapping a verify invocation (used by reruns)."""
    wait: float | None = None
    kind: str | None = None
    required: bool = False


@dataclass(frozen=True)
class Outcome:
    """Everything one verify run produced."""
    exit: int
    seconds: float
    load: dict[str, Any]
    load_wait: dict[str, Any] | None
    log: Path
    report_path: Path | None
    report: dict[str, Any] | None
    failed_checks: list[str]
    blocked_checks: list[str]
    blocked_by_id: dict[str, list[str]]
    environment: str | None
    stopped: dict[str, Any] | None
    triage: str | None
    stamp: dict[str, Any]
    run_id: str
    leases: list[dict[str, Any]]
    started: str
    finished: str
    tree: str | None = None

    @property
    def run_dir(self) -> Path | None:
        return self.report_path.parent if self.report_path is not None else None

    @property
    def lease(self) -> str | None:
        return self.report.get("lease") if self.report and self.report.get("lease") else None

    @property
    def lease_fields(self) -> dict[str, Any]:
        return {"run": self.run_id, **({"lease": self.lease} if self.lease else {})}


class Run:
    """One invocation of the project's verify command, returning an `Outcome`."""

    def __init__(
        self,
        root: Any,
        selection: Selection,
        label: str,
        phase: str,
        jobs: int,
        *,
        meta: dict[str, Any] | None = None,
        ticket: str | None = None,
        run_type: dict[str, str] | None = None,
        lease: Lease | None = None,
        record_history: bool = True,
        wait_for_load: bool = True,
        load_wait: dict[str, Any] | None = None,
        banner: str | None = None,
        stoppable: bool = False,
    ) -> None:
        self.root = root
        self.selection = selection
        self.label = label
        self.phase = phase
        self.jobs = jobs
        self.ticket = ticket
        self.lease = lease
        self.record_history = record_history
        self.wait_for_load = wait_for_load
        self._given_load_wait = load_wait
        self._banner = banner
        self.stoppable = stoppable
        self.log = journal.log_path(label)
        self.run_id = new_run_id()
        self.tree = tree_of(root)
        self.meta: dict[str, Any] = {
            **(meta or {}),
            **(run_type or {}),
            "run": self.run_id,
            **({"tree": self.tree} if self.tree else {}),
        }
        self.verify_argv = [plan.verify_path(root), *selection.argv(), "--keep-going", "--jobs", str(jobs)]
        if lease is not None:
            priority, source = slots.phase_priority(phase)
            wrap_meta: dict[str, Any] = {
                "phase": phase,
                **({"kind": lease.kind} if lease.kind else {}),
                **slots.priority_meta(priority, source),
                **self.meta,
            }
            self.argv = slots.wrap(jobs, label, self.verify_argv, wait=lease.wait, meta=wrap_meta,
                                   priority=priority, ticket=ticket, required=lease.required)
        else:
            self.argv = list(self.verify_argv)
        self.env = verify_env(label, phase, ticket=ticket, **self.meta)
        self._child: subprocess.Popen[bytes] | None = None
        self._handle: TextIO | None = None
        self._started_at: str = ""
        self._clock: float = 0.0
        self._load: dict[str, Any] = {}
        self._load_wait: dict[str, Any] | None = None

    def _print_banner(self) -> None:
        line = (self._banner.format(log=self.log) if self._banner is not None
                else f"lane: running {self.selection.describe()} in {self.root} (log {self.log}); "
                     "queue: slotfather status")
        print(line, flush=True)

    def _build_outcome(self, status: int) -> Outcome:
        seconds = round(time.monotonic() - self._clock, 1)
        self._load["end"] = host.load_averages()
        finished = journal.now()
        text = self.log.read_text(errors="replace")
        reports, report = _read_report(text)
        if self.lease is not None and self.argv != self.verify_argv and status == slots.TIMED_OUT and not reports:
            wait = f"{self.lease.wait:g} s" if self.lease.wait is not None else "its wait"
            raise LeaseTimedOut(f"no lease of {self.jobs} slot(s) within {wait} (slotfather status)")
        report_path = Path(reports[-1].strip()) if reports else None
        ids = list(self.selection.items) if self.selection.kind == "checks" else None
        if self.record_history:
            history.record_run(report, lane=self.meta.get("lane"), phase=self.phase, tree=self.tree,
                               run=self.run_id, started=self._started_at, finished=finished,
                               seconds=seconds, checks=ids)
        failed_checks: list[str] = []
        blocked_by_id: dict[str, list[str]] = {}
        if report:
            failed_checks = [row["id"] for row in report.get("checks", []) if is_executed_failure(row)]
            blocked_by_id = {row["id"]: list(row.get("blocked_by") or []) for row in report.get("checks", [])
                             if is_dependency_blocked(row)}
        env_info: dict[str, Any] = (report or {}).get("environment") or {}
        environment: str | None = env_info.get("class")
        stopped = (report or {}).get("stopped")
        triage = plan.triage(self.root, report_path.parent) if status and report_path is not None else None
        stamp = ({"stamped": False, "reason": "the run failed"} if status or environment
                 else _read_stamp(text, self.selection.kind == "full"))
        lease_entry: dict[str, Any] = {"run": self.run_id,
                                       **({"lease": report["lease"]} if report and report.get("lease") else {})}
        return Outcome(
            exit=status,
            seconds=seconds,
            load=self._load,
            load_wait=self._load_wait,
            log=self.log,
            report_path=report_path,
            report=report,
            failed_checks=failed_checks,
            blocked_checks=list(blocked_by_id),
            blocked_by_id=blocked_by_id,
            environment=environment,
            stopped=stopped,
            triage=triage,
            stamp=stamp,
            run_id=self.run_id,
            leases=[lease_entry],
            started=self._started_at,
            finished=finished,
            tree=self.tree,
        )

    def start(self) -> None:
        self._load_wait = (self._given_load_wait if self._given_load_wait is not None
                           else host.wait_for_load(self.label) if self.wait_for_load else None)
        self._print_banner()
        self._started_at = journal.now()
        self._clock = time.monotonic()
        self._load = {"start": host.load_averages()}
        self._handle = open(self.log, "w")  # noqa: SIM115 - closed in finish/stop, after the child exits
        self._child = subprocess.Popen(self.argv, cwd=self.root, env=self.env,
                                       stdout=self._handle, stderr=subprocess.STDOUT)

    def finish(self) -> Outcome:
        assert self._child is not None and self._handle is not None
        status = self._child.wait()
        self._handle.close()
        self._handle = None
        return self._build_outcome(status)

    def stop(self) -> None:
        """SIGTERM to a child still running (verify stops its jobs on it), SIGKILL 30 s later."""
        if self._child is not None and self._child.poll() is None:
            self._child.terminate()
            try:
                self._child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._child.kill()
                self._child.wait()
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def execute(self) -> Outcome:
        """Run verify once and return its `Outcome`."""
        if self.stoppable:
            try:
                self.start()
                return self.finish()
            finally:
                self.stop()
        self._load_wait = (self._given_load_wait if self._given_load_wait is not None
                           else host.wait_for_load(self.label) if self.wait_for_load else None)
        self._print_banner()
        self._started_at = journal.now()
        self._clock = time.monotonic()
        self._load = {"start": host.load_averages()}
        with open(self.log, "w") as handle:
            status = subprocess.run(self.argv, cwd=self.root, env=self.env,
                                    stdout=handle, stderr=subprocess.STDOUT).returncode
        return self._build_outcome(status)
