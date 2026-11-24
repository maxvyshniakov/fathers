"""One verify runner for every Lanefather verify invocation (fathers#70).

Check, post-merge smoke, red-check reruns (alone and on base), the batch gate and the perf suite all run the
project's verify command through `Run` and read its `Outcome`. `Run` builds the verify argv and `<slot_env>_*`
environment, optionally wraps verify in `slotfather run` (for reruns), waits for host load when asked, captures the
log, parses the report and verified-tree stamp, records run history, and runs gate triage on failure.
"""
from __future__ import annotations

from collections.abc import Generator, Iterable, Sequence
import contextlib
from dataclasses import dataclass
from enum import StrEnum
import fnmatch
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
from types import FrameType
from typing import Any, Literal, TextIO, cast
import uuid

from . import config, history, host, journal, plan, settings, slots, state, worktrees
from . import results as result_store
from .errors import LaneError
from .gitops import git
from .state import Record

Rows = list[dict[str, Any]]
SmokeTuple = tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]], str | None, dict[str, Any], str]

# verify report statuses that are not a failure (a skipped row with blocked_by is dependency-blocked, fathers#195).
PASSING = ("passed", "not_run", "skipped")
STAMPED = re.compile(r"^Verified tree ([0-9a-f]+) stamped\b", re.M)
NOT_STAMPED = re.compile(r"^Verified tree not stamped: (.+?)\.?$", re.M)
REPORT = re.compile(r"^Verification report: (.+)$", re.M)
DEFAULT_HEARTBEAT = 30.0
MAX_HEARTBEATS = 240
_SETUP_LINE = re.compile(r"^(?:setup|preparing|installing|bootstrapping)\b[:\s-]", re.I)
_UNITTEST_ACTIVE = re.compile(r"^(test_\S+\s+\([^)]+\))\s+\.\.\.\s*$")
_RUNNING_TEST = re.compile(r"^Running test:\s+(\S+)\s*$")
UNRESOLVED: Any = object()

DEFAULT_RERUNS = 5
DEFAULT_RERUN_WAIT = float(settings.DEFAULT_RERUN_WAIT)
RERUN_MAX_JOBS = 2


class CheckClass(StrEnum):
    FLAKY_UNDER_LOAD = "flaky-under-load"
    PRE_EXISTING = "pre-existing"
    REGRESSION = "regression"
    DEPENDENCY_BLOCKED = "dependency-blocked"
    UNCLASSIFIED = "unclassified"


LANDABLE: tuple[CheckClass, ...] = (CheckClass.FLAKY_UNDER_LOAD, CheckClass.PRE_EXISTING)


def _heartbeat_interval(project: config.Config = config.DEFAULT) -> float | None:
    """LANEFATHER_VERIFY_HEARTBEAT: seconds of quiet before emitting a progress line (0/off disables)."""
    raw = project.env("VERIFY_HEARTBEAT")
    if not raw or not raw.strip():
        return DEFAULT_HEARTBEAT
    text = raw.strip().lower()
    if text in ("0", "off", "false", "no"):
        return None
    try:
        value = float(text)
    except ValueError:
        return DEFAULT_HEARTBEAT
    return max(0.05, value) if value > 0 else None


def _parse_ps_cpu(text: str) -> float | None:
    """Parse `ps -o time=` ([DD-]HH:MM:SS[.cc] or MM:SS[.cc]) into CPU seconds."""
    token = text.strip()
    if not token:
        return None
    days = 0
    if "-" in token:
        day_part, _, token = token.partition("-")
        if not day_part.isdigit():
            return None
        days = int(day_part)
    parts = token.split(":")
    try:
        if len(parts) == 2:
            return days * 86400 + int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return days * 86400 + int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None
    return None


def _sample_process_tree(root_pid: int) -> dict[str, Any] | None:
    """Best-effort snapshot of `root_pid` and its live descendants (CPU time, count, slotfather pids)."""
    if root_pid <= 0:
        return None
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,time=,command="],
            capture_output=True, text=True, timeout=2,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode:
        return None
    children: dict[int, list[int]] = {}
    info: dict[int, tuple[float, str]] = {}
    for raw_line in out.stdout.splitlines():
        parts = raw_line.strip().split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        cpu = _parse_ps_cpu(parts[2])
        children.setdefault(ppid, []).append(pid)
        info[pid] = (cpu or 0.0, parts[3])
    if root_pid not in info:
        return None
    visited: set[int] = set()
    stack = [root_pid]
    while stack:
        current = stack.pop()
        if current in visited or current not in info:
            continue
        visited.add(current)
        stack.extend(children.get(current, ()))
    total_cpu = sum(info[pid][0] for pid in visited)
    slotfather_pids = [pid for pid in sorted(visited) if "slotfather" in info[pid][1].lower()]
    return {
        "pids": frozenset(visited),
        "procs": len(visited),
        "children": max(0, len(visited) - 1),
        "cpu_seconds": round(total_cpu, 2),
        "slotfather_pids": slotfather_pids,
    }


@dataclass(frozen=True)
class ProgressSample:
    """One bounded status observation of a running `verify.Run`."""
    state: Literal["queued", "setup", "active"]
    target: str
    output_age: float
    line_age: float
    bytes_delta: int
    procs: int | None = None
    children: int | None = None
    cpu_seconds: float | None = None
    cpu_delta: float | None = None
    children_changed: bool = False
    test: str | None = None
    queue_detail: str | None = None

    def format_line(self, label: str) -> str:
        queue_note = f" ({self.queue_detail})" if self.queue_detail else ""
        if self.bytes_delta > 0:
            silence = (f"last output {self.output_age:.1f}s ago, "
                       f"no newline for {self.line_age:.1f}s (+{self.bytes_delta} B)")
        else:
            silence = f"last output {self.output_age:.1f}s ago"
        if self.procs is not None and self.cpu_seconds is not None:
            proc_word = "proc" if self.procs == 1 else "procs"
            child_note = (f" ({self.children} {'child' if self.children == 1 else 'children'})"
                          if self.children else "")
            delta_note = f" (+{self.cpu_delta:.2f}s)" if self.cpu_delta is not None else ""
            changed_note = ", children changed" if self.children_changed else ""
            activity = f"cpu {self.cpu_seconds:.2f}s{delta_note}, {self.procs} {proc_word}{child_note}{changed_note}"
        else:
            activity = "activity unavailable"
        test_note = f"; test {self.test}" if self.test else ""
        return f"lane: {label} [{self.state}] {self.target}{queue_note}; {silence}; {activity}{test_note}"


class _ProgressTracker:
    """Track log silence, observed check/suite/setup markers, and reliably emitted test names."""

    def __init__(self, log: Path, selection: Selection) -> None:
        self.log = log
        self.selection = selection
        self.offset = 0
        self.line_buf = b""
        self.last_output_at = 0.0
        self.last_line_at = 0.0
        self.bytes_since_heartbeat = 0
        self.in_setup = False
        self.active_target: str | None = (
            selection.items[0] if selection.kind in ("checks", "suites") and len(selection.items) == 1 else None
        )
        self.observed_test: str | None = None
        self._item_patterns = [
            (item, re.compile(rf"(?:^|[\s:=\[\(]){re.escape(item)}(?:$|[\s:=\]\)])"))
            for item in selection.items
        ]

    def reset(self, now: float) -> None:
        self.offset = 0
        self.line_buf = b""
        self.last_output_at = now
        self.last_line_at = now
        self.bytes_since_heartbeat = 0
        self.in_setup = False
        self.observed_test = None

    def poll(self, now: float) -> None:
        try:
            with open(self.log, "rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except OSError:
            return
        if not chunk:
            return
        self.offset += len(chunk)
        self.last_output_at = now
        self.bytes_since_heartbeat += len(chunk)
        self.line_buf += chunk
        if b"\n" in self.line_buf:
            self.last_line_at = now
            parts = self.line_buf.split(b"\n")
            self.line_buf = parts[-1]
            for raw_line in parts[:-1]:
                self._observe_line(raw_line.decode("utf-8", errors="replace").strip())
        self._observe_partial(self.line_buf.decode("utf-8", errors="replace").strip())

    def _observe_line(self, line: str) -> None:
        if not line:
            return
        self.observed_test = None
        if line.startswith("Verification report:"):
            return
        for item, pattern in self._item_patterns:
            if pattern.search(line):
                self.active_target = item
                self.in_setup = False
                return
        if _SETUP_LINE.match(line):
            self.in_setup = True

    def _observe_partial(self, partial: str) -> None:
        if not partial:
            self.observed_test = None
            return
        match = _UNITTEST_ACTIVE.match(partial) or _RUNNING_TEST.match(partial)
        self.observed_test = match.group(1) if match else None


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


_VALID_ROW_STATUSES = (*PASSING, "failed", "blocked", "unrun", "void", "cancelled")


def _string_list(value: Any) -> list[str] | None:
    """Return a list of strings when `value` is None or list[str], else None on malformed types."""
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    items: list[str] = []
    for entry in cast(list[Any], value):
        if not isinstance(entry, str):
            return None
        items.append(entry)
    return items


def _report_rows(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Safely extract dict rows with non-empty string `id` from a possibly malformed report."""
    if not isinstance(report, dict):
        return []
    raw_checks: Any = report.get("checks")
    if not isinstance(raw_checks, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in cast(list[Any], raw_checks):
        if isinstance(item, dict):
            row = cast(dict[str, Any], item)
            if isinstance(row.get("id"), str) and row["id"]:
                rows.append(row)
    return rows


def is_dependency_blocked(row: dict[str, Any]) -> bool:
    """Whether a verify report row was skipped or blocked because a dependency failed (fathers#195)."""
    status = row.get("status")
    if status == "blocked":
        return True
    blockers = _string_list(row.get("blocked_by"))
    return status == "skipped" and bool(blockers)


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


def verify_env(label: str, phase: str, *, project: config.Config = config.DEFAULT,
               ticket: str | None = None, min_jobs: int | None = None,
               **meta: Any) -> dict[str, str]:
    """The environment of a verify run lanefather starts: its lease's label, the phase's priority (lanefather#24), and
    metadata naming the phase, the priority's source and what the caller adds (lane, check, sha)."""
    priority, source = slots.phase_priority(phase, project=project)
    tags: dict[str, Any] = {"phase": phase, **slots.priority_meta(priority, source), **meta}
    return dict(os.environ, **slots.queue_environment(label, priority, project=project,
                                                      ticket=ticket, min_jobs=min_jobs, **tags))


def report_jobs(report: dict[str, Any] | None) -> int | None:
    """Granted worker count reported by the verify report (fathers#43), or None."""
    if not report:
        return None
    for key in ("jobs", "slots", "granted_jobs"):
        value = report.get(key)
        if type(value) is int and value > 0:
            return value
    return None


def plan_meta(selection: Any, root: Any = None, count: int | None = None, toolchain: Any = UNRESOLVED,
              *, project: config.Config = config.DEFAULT) -> dict[str, str]:
    """The run type a slot request carries (fathers#107): `plan`, the plan's key of the results store (fathers#91:
    its sorted check ids, or FULL, with the toolchain), and `checks`, how many checks it runs. A selection of check
    ids counts them; the full gate, whole suites (`suite:<name>`) and a promote step (`step:<name>`) take `count`,
    and leave `checks` out without one. The toolchain is the caller's when it resolved it, else `root`'s; a refused
    one is one run type, whatever its message. Slotfather keeps expected durations per run type."""
    if toolchain is UNRESOLVED:
        toolchain = plan.toolchain_state(root, project) if root is not None else None
    if isinstance(toolchain, dict) and "refused" in toolchain:
        toolchain = {"refused": True}
    named = selection == result_store.FULL or any(str(item).startswith(("suite:", "step:")) for item in selection)
    number = count if named else len(set(selection))
    return {"plan": result_store.plan_hash(selection, toolchain, project=project),
            **({"checks": str(number)} if number is not None else {})}


def suite_count(root: Any, suites: list[str], project: config.Config = config.DEFAULT) -> int | None:
    """How many checks whole suites hold, by the tree's plan module; None for a tree without it."""
    owners = plan.suite_owners(root, suites, project)
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
    only: tuple[str, ...] = ()

    @classmethod
    def full(cls) -> Selection:
        return cls("full")

    @classmethod
    def suites(cls, names: Iterable[str]) -> Selection:
        return cls("suites", tuple(names))

    @classmethod
    def checks(cls, ids: Iterable[str], *, only: Iterable[str] = ()) -> Selection:
        return cls("checks", tuple(ids), only=tuple(only))

    def argv(self) -> tuple[str, ...]:
        if self.kind == "full":
            return ()
        if self.kind == "checks":
            base: tuple[str, ...] = ("--checks", ",".join(self.items))
            return (*base, "--only", ",".join(self.only)) if self.only else base
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
    granted_jobs: int | None = None

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

    project: config.Config = config.DEFAULT

    def __init__(
        self,
        root: Any,
        selection: Selection,
        label: str,
        phase: str,
        jobs: int,
        *,
        store: state.Store | None = None,
        project: config.Config | None = None,
        meta: dict[str, Any] | None = None,
        ticket: str | None = None,
        run_type: dict[str, str] | None = None,
        lease: Lease | None = None,
        record_history: bool = True,
        wait_for_load: bool = True,
        load_wait: dict[str, Any] | None = None,
        banner: str | None = None,
        stoppable: bool = False,
        heartbeat: float | None = UNRESOLVED,
        min_jobs: int | None = None,
    ) -> None:
        self.store = store or state.Store(project or config.DEFAULT)
        self.project = self.store.project
        self.root = root
        self.selection = selection
        self.label = label
        self.phase = phase
        self.jobs = jobs
        self.min_jobs = min_jobs
        self.ticket = ticket
        self.lease = lease
        self.record_history = record_history
        self.wait_for_load = wait_for_load
        self._given_load_wait = load_wait
        self._banner = banner
        self.stoppable = stoppable
        self.heartbeat = _heartbeat_interval(self.project) if heartbeat is UNRESOLVED else heartbeat
        self.log = self.store.log_path(label)
        self.run_id = new_run_id()
        self.tree = tree_of(root)
        self.meta: dict[str, Any] = {
            **(meta or {}),
            **(run_type or {}),
            "run": self.run_id,
            **({"tree": self.tree} if self.tree else {}),
        }
        self.verify_argv = [
            plan.verify_path(root, self.project), *selection.argv(), "--keep-going", "--jobs", str(jobs)
        ]
        if lease is not None:
            priority, source = slots.phase_priority(phase, project=self.project)
            wrap_meta: dict[str, Any] = {
                "phase": phase,
                **({"kind": lease.kind} if lease.kind else {}),
                **slots.priority_meta(priority, source),
                **self.meta,
            }
            self.argv = slots.wrap(jobs, label, self.verify_argv, wait=lease.wait, meta=wrap_meta,
                                   priority=priority, ticket=ticket, required=lease.required, min_count=min_jobs)
        else:
            self.argv = list(self.verify_argv)
        self.env = verify_env(label, phase, project=self.project, ticket=ticket, min_jobs=min_jobs, **self.meta)
        self._child: subprocess.Popen[bytes] | None = None
        self._handle: TextIO | None = None
        self._started_at: str = ""
        self._clock: float = 0.0
        self._load: dict[str, Any] = {}
        self._load_wait: dict[str, Any] | None = None
        self._progress = _ProgressTracker(self.log, self.selection)

    def _print_banner(self) -> None:
        line = (self._banner.format(log=self.log) if self._banner is not None
                else f"lane: running {self.selection.describe()} in {self.root} (log {self.log}); "
                     "queue: slotfather status")
        print(line, flush=True)

    def _build_outcome(self, status: int) -> Outcome:
        seconds = round(time.monotonic() - self._clock, 1)
        self._load["end"] = host.load_averages(self.project)
        finished = journal.now()
        text = self.log.read_text(errors="replace")
        reports, report = report_of(text)
        if self.lease is not None and self.argv != self.verify_argv and status == slots.TIMED_OUT and not reports:
            wait = f"{self.lease.wait:g} s" if self.lease.wait is not None else "its wait"
            raise LeaseTimedOut(f"no lease of {self.jobs} slot(s) within {wait} (slotfather status)")
        if isinstance(report, dict):
            # Transient process observation; preserve the verifier's on-disk report unchanged.
            report = dict(report, _lanefather_process_exit=status)
        report_path = Path(reports[-1].strip()) if reports else None
        ids = list(self.selection.items) if self.selection.kind == "checks" else None
        granted_jobs = report_jobs(report)
        if self.record_history:
            history.record_run(self.store, report, lane=self.meta.get("lane"), phase=self.phase, tree=self.tree,
                               run=self.run_id, started=self._started_at, finished=finished,
                               seconds=seconds, checks=ids, jobs=granted_jobs)
        failed_checks: list[str] = []
        blocked_by_id: dict[str, list[str]] = {}
        for row in _report_rows(report):
            if is_executed_failure(row):
                failed_checks.append(row["id"])
            elif is_dependency_blocked(row):
                blocked_by_id[row["id"]] = _string_list(row.get("blocked_by")) or []
        raw_env: Any = report.get("environment") if isinstance(report, dict) else None
        if isinstance(raw_env, dict):
            env_class = cast(dict[str, Any], raw_env).get("class")
            environment: str | None = env_class if isinstance(env_class, str) and env_class else None
        elif isinstance(raw_env, str) and raw_env:
            environment = raw_env
        else:
            environment = None
        raw_stopped: Any = report.get("stopped") if isinstance(report, dict) else None
        stopped: dict[str, Any] | None = (
            cast(dict[str, Any], raw_stopped) if isinstance(raw_stopped, dict) else None
        )
        triage = (
            plan.triage(self.root, report_path.parent, self.project)
            if status and report_path is not None
            else None
        )
        stamp = ({"stamped": False, "reason": "the run failed"} if status or environment
                 else stamp_result(text, self.selection.kind == "full"))
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
            granted_jobs=granted_jobs,
        )

    def status_sample(self, now: float | None = None,
                      prev_tree: dict[str, Any] | None = None) -> tuple[ProgressSample, dict[str, Any] | None]:
        """Collect one bounded progress sample separating log silence from process/queue activity."""
        moment = time.monotonic() if now is None else now
        self._progress.poll(moment)
        pid = self._child.pid if self._child is not None else 0
        tree = _sample_process_tree(pid)
        queue_detail: str | None = None
        queued = False
        if tree and tree["slotfather_pids"]:
            sf_pid = int(tree["slotfather_pids"][0])
        elif self.lease is not None and self.argv != self.verify_argv and pid > 0:
            sf_pid = pid
        else:
            sf_pid = 0
        if sf_pid:
            status = slots.request_status(sf_pid)
            raw_entries: Any = (status or {}).get("entries") or []
            for raw_entry in cast(list[Any], raw_entries) if isinstance(raw_entries, list) else []:
                if not isinstance(raw_entry, dict):
                    continue
                entry = cast(dict[str, Any], raw_entry)
                if entry.get("state") == "waiting":
                    queued = True
                    parts = ["slotfather waiting"]
                    if entry.get("position") is not None:
                        parts.append(f"pos {entry['position']}")
                    if entry.get("waited") is not None:
                        parts.append(f"waited {entry['waited']}s")
                    queue_detail = ", ".join(parts)
                    break
        state_name: Literal["queued", "setup", "active"] = (
            "queued" if queued else "setup" if self._progress.in_setup else "active"
        )
        cpu_delta = (max(0.0, round(tree["cpu_seconds"] - prev_tree["cpu_seconds"], 2))
                     if tree and prev_tree else None)
        children_changed = bool(tree and prev_tree and tree["pids"] != prev_tree["pids"])
        sample = ProgressSample(
            state=state_name,
            target=self._progress.active_target or self.selection.describe(),
            output_age=max(0.0, moment - self._progress.last_output_at),
            line_age=max(0.0, moment - self._progress.last_line_at),
            bytes_delta=self._progress.bytes_since_heartbeat,
            procs=tree["procs"] if tree else None,
            children=tree["children"] if tree else None,
            cpu_seconds=tree["cpu_seconds"] if tree else None,
            cpu_delta=cpu_delta,
            children_changed=children_changed,
            test=self._progress.observed_test,
            queue_detail=queue_detail,
        )
        return sample, tree

    def _wait_child(self) -> int:
        assert self._child is not None
        interval = self.heartbeat
        last_heartbeat = self._clock
        prev_tree: dict[str, Any] | None = None
        emitted = 0
        poll_pause = min(0.1, interval / 3.0) if interval is not None else 0.1
        while True:
            status = self._child.poll()
            if status is not None:
                return status
            now = time.monotonic()
            if interval is not None and emitted < MAX_HEARTBEATS:
                self._progress.poll(now)
                if now - self._progress.last_line_at >= interval and now - last_heartbeat >= interval:
                    sample, prev_tree = self.status_sample(now, prev_tree)
                    print(sample.format_line(self.label), flush=True)
                    self._progress.bytes_since_heartbeat = 0
                    last_heartbeat = now
                    emitted += 1
            time.sleep(poll_pause)

    def start(self) -> None:
        self._load_wait = (self._given_load_wait if self._given_load_wait is not None
                           else host.wait_for_load(self.store, self.label) if self.wait_for_load else None)
        self._print_banner()
        self._started_at = journal.now()
        self._clock = time.monotonic()
        self._progress.reset(self._clock)
        self._load = {"start": host.load_averages(self.project)}
        handle = open(self.log, "w")  # noqa: SIM115 - closed in finish/stop, after the child exits
        try:
            self._child = subprocess.Popen(self.argv, cwd=self.root, env=self.env,
                                           stdout=handle, stderr=subprocess.STDOUT)
        except (FileNotFoundError, PermissionError, IsADirectoryError) as error:
            handle.close()
            verify_argv = getattr(self, "verify_argv", None)
            if verify_argv is not None and self.argv == verify_argv:
                raise plan.verifier_error(verify_argv[0], error) from error
            raise LaneError(f"cannot start {self.argv[0]}: {error.strerror or error}") from error
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def finish(self) -> Outcome:
        assert self._child is not None and self._handle is not None
        status = self._wait_child()
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
        try:
            self.start()
            return self.finish()
        finally:
            if self._handle is not None:
                self._handle.close()
                self._handle = None


# ---------------------------------------------------------------------------
# Outcome projection, result combination & post-merge smoke execution
# ---------------------------------------------------------------------------


def lane_meta(store: state.Store, record: Record) -> dict[str, str]:
    """Caller tags for the lane; ticket is passed as a separate request field."""
    return {"lane": str(record["id"]), "lanefather.state": str(store.directory.resolve())}


def lane_ticket(record: Record) -> str | None:
    return slots.ticket_value(str(record.get("ticket") or ""))


def suite_outcomes(
    root: Any,
    suites: list[str],
    selected: dict[str, list[str]] | None,
    outcome: Outcome,
    *,
    project: config.Config = config.DEFAULT,
) -> tuple[Rows, dict[str, dict[str, Any]]]:
    """Project one `verify.Outcome` onto per-suite results and per-check results."""
    status = outcome.exit
    environment = outcome.environment
    failed_checks = outcome.failed_checks
    blocked_by_id = outcome.blocked_by_id
    owners = plan.suite_owners(root, suites, project) if status or environment or blocked_by_id else None
    failing = {s: [c for c in failed_checks if plan.owned(owners, s, c)] for s in suites}
    blocking = {s: [c for c in blocked_by_id if plan.owned(owners, s, c)] for s in suites}
    # A failure no check accounts for (setup, slot limiter) fails every suite of the run; dependency-blocked checks
    # keep their own suite status rather than becoming executed failures (fathers#195).
    failed = (list(suites) if environment
              else ([s for s in suites if failing[s]] or ([] if any(blocking.values()) else suites)) if status else [])
    blocked_suites = [s for s in suites if s not in failed and blocking[s]]
    rows = {row["id"]: row for row in _report_rows(outcome.report)}
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
        blockers = _string_list(row.get("blocked_by"))
        if blocked and blockers:
            entry["blocked_by"] = blockers
        if isinstance(row.get("seconds"), (int, float)):
            entry["seconds"] = row["seconds"]
        return entry

    checks: dict[str, dict[str, Any]] = {} if ids is None or selected is None else {
        check: check_entry(suite, check) for suite in suites for check in selected[suite]}
    return results, checks


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


def _suite_runs(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw_runs: Any = row.get("runs")
    if isinstance(raw_runs, list) and raw_runs:
        return [
            dict(cast(dict[str, Any], item))
            for item in cast(list[Any], raw_runs)
            if isinstance(item, dict) and cast(dict[str, Any], item).get("run")
        ]
    if row.get("run"):
        return [{"run": row["run"], **({"lease": row["lease"]} if row.get("lease") else {})}]
    return []


def joined_results(first: Rows, second: Rows, *, attempted: Iterable[str] = ()) -> Rows:
    """Each suite's result over two runs of a lane check (#639, fathers#240): the second run's, red when either
    run's was, with the failed checks of both, any blocked checks that did not run in the second pass, every wave's
    run/lease and log, and coherent failure exit/log."""
    later = {r["suite"]: r for r in second}
    ran_second = set(attempted)
    joined: Rows = []
    for result in first:
        other = later.pop(result["suite"], None)
        if other is None:
            joined.append(result)
            continue
        failed = result["failed_checks"] + [c for c in other["failed_checks"] if c not in result["failed_checks"]]
        second_ids = (
            ran_second
            | set(other.get("checks") or ())
            | set(other.get("failed_checks") or ())
            | set(other.get("blocked_checks") or ())
        )
        still_blocked = [c for c in result.get("blocked_checks", []) if c not in second_ids]
        for check_id in other.get("blocked_checks", []):
            if check_id not in still_blocked:
                still_blocked.append(check_id)
        status = ("failed" if "failed" in (result["status"], other["status"])
                  else "blocked" if still_blocked
                  else other["status"])
        prior_logs: list[str] = list(result.get("logs") or ([result["log"]] if result.get("log") else []))
        all_logs = list(dict.fromkeys([*prior_logs, *([other["log"]] if other.get("log") else [])]))
        combined_runs: dict[str, dict[str, Any]] = {}
        for run_entry in (*_suite_runs(result), *_suite_runs(other)):
            combined_runs.setdefault(str(run_entry["run"]), run_entry)
        runs_list = list(combined_runs.values())
        merged = dict(
            other,
            status=status,
            failed_checks=failed,
            first_log=result.get("first_log") or result["log"],
        )
        if status == "failed" and other["status"] != "failed" and result["status"] == "failed":
            merged["exit"] = result["exit"]
            merged["log"] = result["log"]
            if result.get("report") is not None:
                merged["report"] = result["report"]
        if len(all_logs) > 1:
            merged["logs"] = all_logs
        if len(runs_list) > 1:
            merged["runs"] = runs_list
        if still_blocked:
            merged["blocked_checks"] = still_blocked
        else:
            merged.pop("blocked_checks", None)
        if result.get("checks") is None:
            merged.pop("checks", None)
        elif other.get("checks") is not None:
            merged["checks"] = list(dict.fromkeys([*result["checks"], *other["checks"]]))
        joined.append(merged)
    return joined + list(later.values())


def leases_of(fresh: Rows, classification: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The verify runs of a receipt: each one's run id and, when its report named one, its slot lease, then its
    reruns' (fathers#92)."""
    found: dict[str, dict[str, Any]] = {}
    for result in fresh:
        for entry in _suite_runs(result):
            found.setdefault(str(entry["run"]), dict(entry))
    for run in (classification or {}).get("runs", []):
        found.setdefault(run["run"], dict(run))
    return list(found.values())


def run_smoke(
    store: state.Store,
    record: dict[str, Any],
    checkout: str,
    suites: list[str],
    selected: dict[str, list[str]] | None,
    label: str,
    slots_override: int | None,
    toolchain: Any,
) -> SmokeTuple:
    """Run a post-merge smoke's suites (land's, or recheck's) in its checkout: (the jobs decision, suite results,
    check results, triage, the verified-tree stamp, green or red)."""
    if not suites:
        decision: dict[str, Any] = {"jobs": 0, "reason": "nothing to run"}
        return decision, [], {}, None, stamp_result("", False), "green"
    decision = host.given_jobs(slots_override, "--slots") if slots_override else host.post_merge_jobs(
        host.host(store.project)
    )
    ids = None if selected is None else [c for s in suites for c in selected[s]]
    selection = Selection.checks(ids) if ids is not None else Selection.suites(suites)
    run_type = (
        plan_meta(ids, checkout, toolchain=toolchain, project=store.project)
        if ids is not None
        else plan_meta([f"suite:{s}" for s in suites], checkout, suite_count(checkout, suites, store.project),
                       toolchain, project=store.project)
    )
    what = ", ".join(suites) + (f" ({len(ids)} checks)" if ids is not None else "")
    outcome = Run(
        checkout, selection, label, "post-merge", decision["jobs"],
        store=store, meta=lane_meta(store, record), ticket=lane_ticket(record), run_type=run_type,
        banner=f"lane: running {what} in {checkout} (log {{log}}); queue: slotfather status",
    ).execute()
    results, check_results = suite_outcomes(checkout, suites, selected, outcome, project=store.project)
    status = "green" if all(r["status"] == "passed" for r in results) else "red"
    return decision, results, check_results, outcome.triage, outcome.stamp, status


# ---------------------------------------------------------------------------
# Red check classification (#568, #639, #147, #195)
# ---------------------------------------------------------------------------


RerunUnavailable = LeaseTimedOut


def rerun_cap(project: config.Config = config.DEFAULT) -> int:
    """LANEFATHER_RERUNS: how many red checks a lane check reruns; the rest stay unclassified."""
    value = project.env("RERUNS")
    return int(value) if value and value.strip().isdigit() and int(value) > 0 else DEFAULT_RERUNS


def rerun_wait(project: config.Config = config.DEFAULT) -> float:
    """LANEFATHER_RERUN_WAIT, else this machine's `rerun.wait` setting: seconds a rerun waits for its lease before
    its checks stay unclassified."""
    value = project.env("RERUN_WAIT") or str(settings.value("rerun.wait") or "")
    try:
        seconds = float(value) if value else DEFAULT_RERUN_WAIT
    except ValueError:
        return DEFAULT_RERUN_WAIT
    return seconds if seconds > 0 else DEFAULT_RERUN_WAIT


def rerun_min_slots(
    check_ids: list[str],
    lane_plan: dict[str, Any] | None,
    fresh: Rows,
    *,
    project: config.Config = config.DEFAULT,
) -> int:
    """Minimum slots required for a rerun of `check_ids` (fathers#173, fathers#247)."""
    if lane_plan is not None and lane_plan.get("selection") is not None:
        return plan.required_min_slots(lane_plan, {"_": check_ids}, [], project=project)[0]
    floor = 1
    if lane_plan is not None:
        suites = [r["suite"] for r in fresh if r["status"] != "passed"]
        floor = max(floor, plan.required_min_slots(lane_plan, None, suites, project=project)[0])
    default_floor: int | None = None
    patterns: list[tuple[str, int]] = []
    for pattern, count in project.min_slots:
        if pattern == "default":
            default_floor = count
        else:
            patterns.append((pattern, count))
    check_suites: dict[str, str] = {}
    for row in fresh:
        suite = str(row.get("suite") or "")
        if not suite:
            continue
        for failed_id in row.get("failed_checks") or ():
            check_suites[str(failed_id)] = suite
    plan_declared = lane_plan is not None and plan.declared_slots(lane_plan) is not None
    for cid in check_ids:
        suite = check_suites.get(cid, "")
        matched = plan_declared
        for pattern, count in patterns:
            if fnmatch.fnmatchcase(cid, pattern) or (suite and fnmatch.fnmatchcase(suite, pattern)):
                floor = max(floor, count)
                matched = True
        if not matched and default_floor is not None:
            floor = max(floor, default_floor)
    return floor


def rerun_jobs(jobs: int, minimum: int = 1) -> int:
    """A rerun's lease size (lanefather#4): the lane check's own, 1 to RERUN_MAX_JOBS slots (or the step's declared
    minimum slots, fathers#173)."""
    return max(1, min(max(RERUN_MAX_JOBS, minimum), jobs))


def red_checks(results: Rows) -> list[str]:
    """The failed checks of the failed results, in order, once each."""
    found: list[str] = []
    for result in results:
        if result["status"] != "passed":
            found += [c for c in result.get("failed_checks", []) if c not in found]
    return found


@dataclass(frozen=True)
class ComparisonRun:
    """One attempt passed to `comparison_problem`, with its phase label, log path, selected ids, and process exit."""
    label: str
    report: dict[str, Any] | None
    log: Path | str | None = None
    selected: Sequence[str] | None = None
    process_exit: int | None = None


def rerun_problem(
    report: object,
    log: Path | str | None,
    *,
    label: str = "the rerun",
    require_exit: bool = False,
    process_exit: int | None = None,
) -> str | None:
    """Why a rerun says nothing about its checks, or None."""
    suffix = f" ({log})" if log is not None else ""
    if report is None:
        return f"{label} wrote no verification report{suffix}"
    if not isinstance(report, dict):
        return f"{label} wrote a malformed verification report{suffix}"
    report = cast(dict[str, Any], report)
    raw_rows = report.get("checks")
    malformed = f"{label} wrote a malformed verification report{suffix}"
    has_exit = "exit" in report and report.get("exit") is not None
    if (require_exit and not has_exit) or (has_exit and type(report["exit"]) is not int):
        return malformed
    if not isinstance(raw_rows, list):
        return malformed
    rows: Rows = []
    seen: set[str] = set()
    for value in cast(list[Any], raw_rows):
        if not isinstance(value, dict):
            return malformed
        row = cast(dict[str, Any], value)
        check_id = row.get("id")
        if (not isinstance(check_id, str) or not check_id or check_id in seen
                or row.get("status") not in _VALID_ROW_STATUSES):
            return malformed
        if "blocked_by" in row and _string_list(row.get("blocked_by")) is None:
            return malformed
        seen.add(check_id)
        rows.append(row)
    observed_exit: Any = process_exit if process_exit is not None else report.get("_lanefather_process_exit")
    if observed_exit is not None and (type(observed_exit) is not int or (has_exit and observed_exit != report["exit"])):
        return f"{label} process exit differs from its report exit{suffix}"
    if report.get("environment"):
        env_val: Any = report["environment"]
        if isinstance(env_val, dict):
            env_map = cast(dict[str, Any], env_val)
            env_desc: Any = env_map.get("class") or env_map
        else:
            env_desc = env_val
        return f"{label} failed on its environment: {env_desc}{suffix}"
    if report.get("interrupted") or report.get("cleanup_error"):
        return f"{label} was interrupted or its cleanup failed{suffix}"
    effective_exit: Any = report["exit"] if has_exit else observed_exit
    if effective_exit == 0 and any(row["status"] == "failed" for row in rows):
        return f"{label} report contradicts its failed check status{suffix}"
    if (
        effective_exit
        and not any(row["status"] == "failed" or is_dependency_blocked(row) for row in rows)
        and (require_exit or not rows or any(row["status"] in PASSING for row in rows))
    ):
        return f"{label} failed outside any check{suffix}"
    return None


def failure_evidence(row: dict[str, Any]) -> dict[str, Any] | None:
    """Verifier-owned v1 failure evidence, never inferred from a shared check name or arbitrary log text."""
    evidence = row.get("failure_evidence")
    if row.get("status") != "failed" or not isinstance(evidence, dict):
        return None
    evidence = cast(dict[str, Any], evidence)
    if (set(evidence) != {"version", "comparable", "context", "cases"}
            or type(evidence["version"]) is not int or evidence["version"] != 1
            or evidence["comparable"] is not True
            or not isinstance(evidence["context"], str) or not evidence["context"].strip()
            or not isinstance(evidence["cases"], list) or not evidence["cases"]):
        return None
    cases: dict[str, str] = {}
    for raw_case in cast(list[Any], evidence["cases"]):
        if not isinstance(raw_case, dict):
            return None
        case = cast(dict[str, Any], raw_case)
        if (set(case) != {"id", "signature"}
                or not isinstance(case["id"], str) or not case["id"].strip()
                or not isinstance(case["signature"], str) or not case["signature"].strip()
                or case["id"] in cases):
            return None
        cases[case["id"]] = case["signature"]
    return {"context": evidence["context"], "cases": cases}


def comparison_problem(
    reports: Sequence[ComparisonRun | dict[str, Any] | None],
    check_id: str,
    selected: Sequence[str] | None = None,
) -> str | None:
    """A failed base is usable only with valid, comparable failure evidence from every attempt where every
    failing case on the candidate is also present (matching id + signature and context) on the base."""
    if len(reports) < 2:
        return "candidate and base evidence are both required"
    default_selected = list(selected) if selected is not None else [check_id]
    runs: list[ComparisonRun] = []
    last_index = len(reports) - 1
    for idx, item in enumerate(reports):
        if isinstance(item, ComparisonRun):
            runs.append(item)
        else:
            default_label = (
                "the initial run" if idx == 0 else "the base rerun" if idx == last_index else "the alone rerun"
            )
            runs.append(ComparisonRun(default_label, item, selected=default_selected))
    compared: list[dict[str, Any]] = []
    for run in runs:
        problem = rerun_problem(
            run.report, run.log, label=run.label, require_exit=True, process_exit=run.process_exit,
        )
        if problem:
            return problem
        assert run.report is not None
        run_selected = set(run.selected if run.selected is not None else default_selected)
        suffix = f" ({run.log})" if run.log is not None else ""
        if any(row["id"] not in run_selected and is_executed_failure(row) for row in run.report["checks"]):
            return f"{run.label} failed outside the selected check{suffix}"
        evidence = failure_evidence(report_row(run.report, check_id))
        if evidence is None:
            return f"{run.label} has missing or invalid comparable failure_evidence (v1){suffix}"
        compared.append(evidence)
    first_ev = compared[0]
    base_ev = compared[-1]
    candidate_reruns = compared[1:-1]
    if any(ev["context"] != first_ev["context"] for ev in compared[1:]):
        return "the runtime/input context differs across candidate and base runs"
    if any(ev["cases"] != first_ev["cases"] for ev in candidate_reruns):
        return "the alone rerun failing cases/signatures differ from the initial run"
    base_cases: dict[str, str] = base_ev["cases"]
    unmatched = [cid for cid, sig in first_ev["cases"].items() if base_cases.get(cid) != sig]
    if unmatched:
        return (
            "the candidate failing cases/signatures differ from the base "
            f"(not matched on base: {', '.join(unmatched)})"
        )
    return None


def not_run(row: dict[str, Any]) -> str:
    blocked = f" by {', '.join(row['blocked_by'])}" if row.get("blocked_by") else ""
    return f"{row['status']}{blocked}"


def report_row(report: dict[str, Any] | None, check_id: str) -> dict[str, Any]:
    rows = report.get("checks") if isinstance(report, dict) else None
    if not isinstance(rows, list):
        return {"status": "not_run"}
    for value in cast(list[Any], rows):
        if isinstance(value, dict):
            row = cast(dict[str, Any], value)
            if row.get("id") == check_id:
                return row
    return {"status": "not_run"}


def failed_prerequisites(check_id: str, blocked: dict[str, list[str]]) -> list[str]:
    """The checks behind a dependency-blocked check that did not themselves wait on another (fathers#195): its
    `blocked_by`, followed through the checks that were blocked too, in order, once each."""
    found: list[str] = []
    seen = {check_id}
    pending = list(reversed(blocked.get(check_id, [])))
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in blocked:
            pending.extend(reversed(blocked[current]))
        else:
            found.append(current)
    return found


def dependency_blocked_entry(check_id: str, blockers: list[str], prerequisites: list[str] | None = None,
                             classes: dict[str, str] | None = None, **evidence: Any) -> dict[str, Any]:
    """The classification of a check that did not run because a check it depends on failed (fathers#195): never a
    regression, never landable, and unresolved until it runs. It names its `blocked_by` and, once known, the
    `prerequisites` that actually ran and failed with their class."""
    reason = (f"skipped because dependency failed ({', '.join(blockers)})" if blockers
              else "skipped because dependency failed (the report names none)")
    if prerequisites:
        named = ", ".join(f"{p} ({classes[p]})" if classes and p in classes else p for p in prerequisites)
        reason += f"; failed prerequisite{'s' if len(prerequisites) > 1 else ''}: {named}"
    if prerequisites is not None:
        reason += "; not run, so unresolved"
    return {"check": check_id, "class": str(CheckClass.DEPENDENCY_BLOCKED), "blocked_by": list(blockers),
            **({"prerequisites": list(prerequisites)} if prerequisites is not None else {}),
            "reason": reason, **evidence}


def _dedup_strings(value: Any) -> list[str]:
    if type(value) not in (list, tuple, set):
        return []
    found: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in found:
            found.append(item)
    return found


def _ordered_subset_tests(check_ids: list[str], failed_tests_by_check: dict[str, list[str]]) -> list[str]:
    if not check_ids or not all(c in failed_tests_by_check for c in check_ids):
        return []
    merged: list[str] = []
    for check_id in check_ids:
        for test_name in failed_tests_by_check[check_id]:
            if test_name not in merged:
                merged.append(test_name)
    return merged


def _outside_failed_tests(row: dict[str, Any], expected_tests: set[str]) -> list[str]:
    outside = _dedup_strings(row.get("outside_failed_tests") or row.get("unreached_tests"))
    if expected_tests:
        for name in _dedup_strings(row.get("failed_tests")):
            if name not in expected_tests and name not in outside:
                outside.append(name)
    return outside


def keep_run(store: state.Store, run: Path | None, label: str) -> Path | None:
    """Copy a run directory out of a checkout about to be removed, into the lane logs."""
    if run is None or not run.is_dir():
        return None
    kept = store.logs_dir() / f"{label}-{run.name}"
    shutil.copytree(run, kept, dirs_exist_ok=True)
    return kept


def run_rerun(
    store: state.Store,
    root: Any,
    ids: list[str],
    label: str,
    jobs: int,
    *,
    queued: bool = True,
    meta: dict[str, Any] | None = None,
    ticket: str | None = None,
    toolchain: Any = UNRESOLVED,
    only: Iterable[str] = (),
) -> tuple[str, dict[str, Any] | None, Path | None, Path | None, str | None]:
    """Execute one rerun via `verify.Run` and return `(run_id, report, log, run_dir, problem)`."""
    only_tuple = tuple(only)
    lease = Lease(wait=rerun_wait(store.project), kind="rerun") if queued else None
    run = Run(
        root,
        Selection.checks(ids, only=only_tuple),
        label,
        "rerun",
        jobs,
        store=store,
        meta={"check": ",".join(ids), **(meta or {})},
        ticket=ticket,
        run_type=plan_meta(ids, root, toolchain=toolchain, project=store.project),
        lease=lease,
        wait_for_load=False,
        banner=f"lane: rerunning {', '.join(ids)} in {root} on {jobs} slot(s) (log {{log}})",
        stoppable=True,
    )
    try:
        outcome = run.execute()
        return run.run_id, outcome.report, outcome.log, outcome.run_dir, rerun_problem(outcome.report, outcome.log)
    except LeaseTimedOut as error:
        return run.run_id, None, None, None, str(error)


@dataclass(frozen=True)
class ClassificationPlan:
    """Pure pre-rerun classification decision computed from `fresh` suite rows and the initial `report`."""
    first: bool
    candidates: list[str]
    blocked_rows: dict[str, list[str]]
    unaccounted: list[str]
    active_rerun: list[str]
    still_red: list[str]
    alone_tests: list[str]
    failed_tests_by_check: dict[str, list[str]]
    entries: dict[str, dict[str, Any]]


def plan_classification(
    fresh: Rows,
    report: dict[str, Any] | None,
    *,
    cap: int = DEFAULT_RERUNS,
    budget: int | None = None,
    only_supported: bool = False,
) -> ClassificationPlan | dict[str, Any] | None:
    """Pure decision: return `None` when nothing is red/blocked, a skipped classification dict on environment failure,
    or a `ClassificationPlan` naming what reruns alone vs on base."""
    failed = [r for r in fresh if r["status"] == "failed"]
    blocked_suites = [r for r in fresh if r["status"] == "blocked"]
    if not failed and not blocked_suites:
        return None
    stopped_first = bool((report or {}).get("stopped"))
    environment = next((r["environment"] for r in failed if r.get("environment")), None)
    if environment:
        return {"raw_status": "red",
                "skipped": f"environment: {environment}: the run's results do not count, so nothing reran",
                "checks": [], "green": False}
    if isinstance(report, dict):
        if report.get("interrupted") or report.get("cleanup_error"):
            return {
                "raw_status": "red",
                "skipped": (
                    "the initial run was interrupted or its cleanup failed: "
                    "the run's results do not count, so nothing reran"
                ),
                "checks": [],
                "green": False,
            }
        raw_report_exit: Any = report.get("exit")
        raw_proc_exit: Any = report.get("_lanefather_process_exit")
        if raw_proc_exit is None and fresh and "exit" in fresh[0]:
            raw_proc_exit = fresh[0].get("exit")
        if (
            (raw_report_exit is not None and type(raw_report_exit) is not int)
            or (raw_proc_exit is not None and type(raw_proc_exit) is not int)
            or (type(raw_report_exit) is int and type(raw_proc_exit) is int and raw_proc_exit != raw_report_exit)
        ):
            return {
                "raw_status": "red",
                "skipped": (
                    "the initial run process exit differs from its report exit: "
                    "the run's results do not count, so nothing reran"
                ),
                "checks": [],
                "green": False,
            }
    candidates = red_checks(failed)
    safe_rows = _report_rows(report)
    blocked_rows = {row["id"]: _string_list(row.get("blocked_by")) or [] for row in safe_rows
                    if is_dependency_blocked(row)}
    unaccounted = sorted({row["id"] for row in safe_rows if is_executed_failure(row)}
                         - set(candidates)) + [r["suite"] for r in failed if not r.get("failed_checks")]
    effective_budget = cap if budget is None else max(0, budget)
    rerun = candidates[:effective_budget]
    failed_tests_by_check: dict[str, list[str]] = {}
    outside_by_check: dict[str, list[str]] = {}
    if only_supported:
        for check_id in rerun:
            initial_row = report_row(report, check_id)
            tests = _dedup_strings(initial_row.get("failed_tests"))
            if tests:
                failed_tests_by_check[check_id] = tests
            outside = _dedup_strings(initial_row.get("outside_failed_tests") or initial_row.get("unreached_tests"))
            if outside:
                outside_by_check[check_id] = outside
    over_budget_reason = (
        f"the rerun budget ({cap}) was used by earlier checks"
        if effective_budget < cap
        else f"only the first {cap} red checks rerun"
    )
    entries: dict[str, dict[str, Any]] = {
        c: {"check": c, "class": str(CheckClass.UNCLASSIFIED), "reason": over_budget_reason}
        for c in candidates[effective_budget:]
    }
    for check_id, outside in outside_by_check.items():
        entries[check_id] = {
            "check": check_id,
            "class": str(CheckClass.UNCLASSIFIED),
            "reason": f"red test(s) outside failed_tests: {', '.join(outside)}",
            "outside_failed_tests": outside,
            **({"tests": failed_tests_by_check[check_id]} if check_id in failed_tests_by_check else {}),
        }
    active_rerun = [c for c in rerun if c not in outside_by_check]
    if active_rerun and stopped_first:
        for check_id in active_rerun:
            entries[check_id] = {"check": check_id, "alone": {
                "status": "not rerun", "reason": "a first check (#639): static, so load does not make it red"}}
    still_red = list(active_rerun) if stopped_first else []
    alone_tests = (
        _ordered_subset_tests(active_rerun, failed_tests_by_check)
        if (only_supported and not stopped_first)
        else []
    )
    return ClassificationPlan(
        first=stopped_first,
        candidates=candidates,
        blocked_rows=blocked_rows,
        unaccounted=unaccounted,
        active_rerun=active_rerun,
        still_red=still_red,
        alone_tests=alone_tests,
        failed_tests_by_check=failed_tests_by_check,
        entries=entries,
    )


def evaluate_alone_row(
    check_id: str,
    row: dict[str, Any],
    *,
    first_row: dict[str, Any],
    log: Path | str | None,
    problem: str | None,
    load: dict[str, Any],
    check_tests: list[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Pure transition for one check's `alone` rerun row: returns `(entry, needs_base_rerun)`."""
    outside = _outside_failed_tests(row, set(check_tests)) if check_tests else []
    if problem:
        return (
            {
                "check": check_id,
                "class": str(CheckClass.UNCLASSIFIED),
                "reason": problem,
                **({"tests": check_tests} if check_tests else {}),
            },
            False,
        )
    if outside:
        return (
            {
                "check": check_id,
                "class": str(CheckClass.UNCLASSIFIED),
                "reason": f"red test(s) outside failed_tests: {', '.join(outside)}",
                "outside_failed_tests": outside,
                "tests": check_tests,
                "alone": {"status": row["status"], "log": str(log)},
            },
            False,
        )
    if row["status"] == "passed":
        return (
            {
                "check": check_id,
                "class": str(CheckClass.FLAKY_UNDER_LOAD),
                "load": load,
                "alone": {"status": "passed", "log": str(log)},
                **({"junit": first_row["junit"]} if first_row.get("junit") else {}),
                **({"tests": check_tests} if check_tests else {}),
            },
            False,
        )
    if is_dependency_blocked(row):
        blockers = list(row.get("blocked_by") or [])
        return (
            dependency_blocked_entry(check_id, blockers, alone={
                "status": row["status"], "log": str(log), **({"blocked_by": blockers} if blockers else {})}),
            False,
        )
    if row["status"] == "failed":
        return (
            {
                "check": check_id,
                "alone": {"status": row["status"], "log": str(log)},
                **({"tests": check_tests} if check_tests else {}),
            },
            True,
        )
    return (
        {
            "check": check_id,
            "class": str(CheckClass.UNCLASSIFIED),
            "reason": f"the lane's rerun did not run it ({not_run(row)}): {log}",
            **({"tests": check_tests} if check_tests else {}),
        },
        False,
    )


def evaluate_base_row(
    check_id: str,
    prior_entry: dict[str, Any],
    *,
    base: str,
    present: bool,
    row: dict[str, Any] | None = None,
    base_log: Path | str | None = None,
    kept_dir: Path | None = None,
    problem: str | None = None,
    check_tests: list[str] | None = None,
    comparison_reports: Sequence[ComparisonRun | dict[str, Any] | None] | None = None,
    selected: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Pure transition for one check's `base` rerun row: returns the completed classification entry."""
    entry = dict(prior_entry)
    if not present:
        entry.update({
            "class": str(CheckClass.REGRESSION),
            "base": {"sha": base, "status": "absent", "reason": "the base defines no such check"},
        })
        return entry
    actual_row = row or {"status": "not_run"}
    own = kept_dir / Path(actual_row["log"]).name if kept_dir and actual_row.get("log") else None
    evidence = {
        "sha": base,
        "status": actual_row["status"],
        "log": str(own) if own and own.is_file() else str(base_log),
        **({"blocked_by": actual_row["blocked_by"]} if actual_row.get("blocked_by") else {}),
    }
    outside = _outside_failed_tests(actual_row, set(check_tests)) if check_tests else []
    if check_tests:
        entry["tests"] = check_tests
    if problem:
        entry.update({"class": str(CheckClass.UNCLASSIFIED), "reason": problem, "base": evidence})
    elif outside:
        entry.update({
            "class": str(CheckClass.UNCLASSIFIED),
            "reason": f"red test(s) outside failed_tests: {', '.join(outside)}",
            "outside_failed_tests": outside,
            "base": evidence,
        })
    elif actual_row["status"] == "passed":
        entry.update({"class": str(CheckClass.REGRESSION), "base": evidence})
    elif actual_row["status"] == "failed":
        comparison = (
            comparison_problem(comparison_reports, check_id, selected if selected is not None else [check_id])
            if comparison_reports is not None
            else "candidate and base evidence are both required"
        )
        if comparison:
            entry.update({"class": str(CheckClass.UNCLASSIFIED), "base": evidence, "reason": comparison})
        else:
            assert comparison_reports is not None
            first_item = comparison_reports[0]
            first_report = first_item.report if isinstance(first_item, ComparisonRun) else first_item
            matched_evidence = failure_evidence(report_row(first_report, check_id))
            comparison_payload: dict[str, Any] = {
                "version": 1,
                **(
                    {
                        "context": matched_evidence["context"],
                        "cases": [{"id": cid, "signature": sig} for cid, sig in matched_evidence["cases"].items()],
                    }
                    if matched_evidence
                    else {}
                ),
            }
            entry.update({
                "class": str(CheckClass.PRE_EXISTING),
                "base": evidence,
                "failure_comparison": comparison_payload,
            })
    else:
        entry.update({
            "class": str(CheckClass.UNCLASSIFIED),
            "base": evidence,
            "reason": f"the base did not run it ({not_run(actual_row)})",
        })
    return entry


def finalize_classification(
    base: str,
    cplan: ClassificationPlan,
    entries: dict[str, dict[str, Any]],
    *,
    load: dict[str, Any],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pure finalizer assembling the classification dict and `green` verdict. Each dependency-blocked check names
    the checks that actually ran and failed behind it (`prerequisites`) and how they were classified (fathers#195)."""
    final_entries = dict(entries)
    for check_id, blockers in cplan.blocked_rows.items():
        if check_id not in final_entries:
            final_entries[check_id] = dependency_blocked_entry(check_id, blockers)
    blocked = {c: list(e.get("blocked_by") or []) for c, e in final_entries.items()
               if e.get("class") == CheckClass.DEPENDENCY_BLOCKED}
    graph = {**cplan.blocked_rows, **blocked}
    for check_id, blockers in blocked.items():
        roots = failed_prerequisites(check_id, graph)
        final_entries[check_id] = dependency_blocked_entry(
            check_id, blockers, roots, {r: str(final_entries[r]["class"]) for r in roots
                                                 if r in final_entries and final_entries[r].get("class")},
            **{k: v for k, v in final_entries[check_id].items()
               if k not in ("check", "class", "blocked_by", "reason", "prerequisites")})
    ordered = [*cplan.candidates, *[c for c in cplan.blocked_rows if c not in cplan.candidates]]
    checks = [final_entries[c] for c in ordered]
    green = bool(checks) and not cplan.unaccounted and all(e["class"] in LANDABLE for e in checks)
    return {
        "base": base,
        "raw_status": "red",
        "checks": checks,
        "load": load,
        "unaccounted": cplan.unaccounted,
        "green": green,
        "runs": runs,
        **({"first": True} if cplan.first else {}),
    }


def classify_failures(
    store: state.Store,
    record: Record,
    worktree: str,
    base: str,
    fresh: Rows,
    report: dict[str, Any] | None,
    jobs: int = 1,
    toolchain: Any = UNRESOLVED,
    lane_plan: dict[str, Any] | None = None,
    *,
    rerun_budget: int | None = None,
) -> dict[str, Any] | None:
    """Rerun the red checks of a lane check (at most LANEFATHER_RERUNS) and classify each; None when nothing is red."""
    only_supported = plan.supports_only(worktree, lane_plan=lane_plan, report=report, project=store.project)
    planned = plan_classification(
        fresh, report, cap=rerun_cap(store.project), budget=rerun_budget, only_supported=only_supported,
    )
    if planned is None or isinstance(planned, dict):
        return planned
    entries = dict(planned.entries)
    still_red = list(planned.still_red)
    initial_log = next((r.get("log") for r in fresh if r.get("log")), None)
    candidate_runs: list[ComparisonRun] = [
        ComparisonRun("the initial run", report, log=initial_log, selected=planned.candidates),
    ]
    load: dict[str, Any] = {"first_run": host.load_averages(store.project)}
    runs: list[dict[str, Any]] = []
    if planned.active_rerun and not planned.first:
        alone_jobs = rerun_jobs(jobs, minimum=rerun_min_slots(planned.active_rerun, lane_plan, fresh,
                                                              project=store.project))
        alone_run_id, alone_report, log, _, problem = run_rerun(
            store, worktree, planned.active_rerun, f"lane-{record['id']}-alone", alone_jobs,
            meta=lane_meta(store, record), ticket=lane_ticket(record), toolchain=toolchain,
            only=planned.alone_tests,
        )
        candidate_runs.append(
            ComparisonRun("the alone rerun", alone_report, log=log, selected=planned.active_rerun),
        )
        runs.append({"run": alone_run_id, "rerun": "alone",
                     **({"tests": planned.alone_tests} if planned.alone_tests else {})})
        load["alone"] = host.load_averages(store.project)
        for check_id in planned.active_rerun:
            row = report_row(alone_report, check_id)
            check_tests = planned.failed_tests_by_check.get(check_id) if planned.alone_tests else None
            entry, needs_base = evaluate_alone_row(
                check_id, row, first_row=report_row(report, check_id), log=log, problem=problem, load=load,
                check_tests=check_tests,
            )
            entries[check_id] = entry
            if needs_base:
                still_red.append(check_id)
    if still_red:
        # Only after the lane's rerun is red, never beside it (review of lanefather#4).
        present: list[str] = []
        base_report: dict[str, Any] | None = None
        base_log: Path | None = None
        base_problem: str | None = None
        kept: Path | None = None
        base_tests: list[str] = []
        with worktrees.base_checkout(record, worktree, base) as path:
            known = plan.known_checks(path, store.project)
            present = [c for c in still_red if known is None or c in known]
            if present:
                base_label = f"lane-{record['id']}-base"
                base_jobs = (
                    1 if planned.first
                    else rerun_jobs(jobs, minimum=rerun_min_slots(present, lane_plan, fresh, project=store.project))
                )
                base_tests = _ordered_subset_tests(present, planned.failed_tests_by_check) if only_supported else []
                base_run_id, base_report, base_log, base_dir, base_problem = run_rerun(
                    store, path, present, base_label, base_jobs, queued=not planned.first,
                    meta={**lane_meta(store, record), "on": "base"}, ticket=lane_ticket(record),
                    only=base_tests,
                )
                runs.append({"run": base_run_id, "rerun": "base",
                             **({"tests": base_tests} if base_tests else {})})
                kept = keep_run(store, base_dir, base_label)
        base_run = ComparisonRun("the base rerun", base_report, log=base_log, selected=present)
        for check_id in still_red:
            is_present = check_id in present
            row = report_row(base_report, check_id) if is_present else None
            check_tests = planned.failed_tests_by_check.get(check_id) if base_tests else None
            entries[check_id] = evaluate_base_row(
                check_id, entries[check_id], base=base, present=is_present, row=row, base_log=base_log,
                kept_dir=kept, problem=base_problem, check_tests=check_tests,
                comparison_reports=[*candidate_runs, base_run], selected=planned.candidates,
            )
    return finalize_classification(base, planned, entries, load=load, runs=runs)


def classification_lines(classification: dict[str, Any]) -> list[str]:
    base = str(classification.get("base") or "")[:10]
    stopped_first = classification.get("first")
    if classification.get("skipped"):
        lines = [f"lane: red checks not classified: {classification['skipped']}"]
        if not classification.get("checks"):
            return lines
    else:
        lines = [f"lane: red first checks rerun on the base {base} (#639):" if stopped_first
                 else f"lane: red checks rerun by themselves on the lane, then on its base {base} (#568):"]
    for entry in classification["checks"]:
        kind, detail = entry["class"], entry.get("reason", "")
        if kind == CheckClass.REGRESSION:
            again = "" if entry.get("alone", {}).get("status") == "not rerun" else " again when rerun by itself"
            detail = ("the base defines no such check" if entry["base"]["status"] == "absent"
                      else f"fails on the lane{again}, the base {base} passes it")
        elif kind == CheckClass.PRE_EXISTING:
            detail = f"the base {base} fails it too, with equivalent failure evidence ({entry['base']['log']})"
        elif kind == CheckClass.FLAKY_UNDER_LOAD:
            detail = (f"passes rerun by itself; load averages {'/'.join(map(str, entry['load']['first_run']))} "
                      f"after the lane run, {'/'.join(map(str, entry['load']['alone']))} after its rerun")
        lines.append(f"  {entry['check']}: {kind}: {detail}")
    if classification["unaccounted"]:
        lines.append(f"  not rerun (no check of a suite): {', '.join(classification['unaccounted'])}")
    waiting = [e for e in classification["checks"] if e["class"] == CheckClass.DEPENDENCY_BLOCKED]
    if waiting:
        behind: list[str] = []
        for entry in waiting:
            named: list[str] = entry.get("prerequisites") or entry.get("blocked_by") or []
            for name in named:
                if name not in behind:
                    behind.append(name)
        lines.append(f"lane: {len(waiting)} dependency-blocked check(s) did not run and stay unresolved"
                     f"{' behind ' + ', '.join(behind) if behind else ''}; they used no rerun and count as "
                     "neither passed nor regressed")
    if classification["green"]:
        lines.append("lane: raw verification RED; every red check is flaky-under-load or pre-existing: "
                     "the classified receipt is green; a flake ticket cites these lines")
    return lines


def reruns_used(classification: dict[str, Any] | None) -> int:
    """How many solo-rerun budget slots `classification` consumed (fathers#240)."""
    if not classification or classification.get("skipped") or classification.get("first"):
        return 0
    checks: list[dict[str, Any]] = classification.get("checks") or []
    used = 0
    for entry in checks:
        alone: Any = entry.get("alone")
        if isinstance(alone, dict):
            if cast(dict[str, Any], alone).get("status") != "not rerun":
                used += 1
        elif (
            entry.get("class") == CheckClass.UNCLASSIFIED
            and not str(entry.get("reason", "")).startswith(("only the first ", "the rerun budget ("))
            and not entry.get("outside_failed_tests")
        ):
            used += 1
    return used


def unblocked_checks(classification: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """Blocked checks whose failed prerequisites all passed their own rerun (`flaky-under-load`), in report order,
    together with the prerequisites that unblocked them (fathers#240)."""
    if not classification or classification.get("skipped"):
        return [], []
    checks: list[dict[str, Any]] = classification.get("checks") or []
    passed_order = [str(e["check"]) for e in checks if e.get("class") == CheckClass.FLAKY_UNDER_LOAD]
    passed_alone = set(passed_order)
    if not passed_alone:
        return [], []
    blocked_graph = {
        str(e["check"]): list(e.get("blocked_by") or [])
        for e in checks
        if e.get("class") == CheckClass.DEPENDENCY_BLOCKED
    }
    unblocked: list[str] = []
    prereqs_used: set[str] = set()
    for entry in checks:
        if entry.get("class") != CheckClass.DEPENDENCY_BLOCKED:
            continue
        check_id = str(entry["check"])
        raw_prereqs: Any = entry.get("prerequisites")
        prereqs = (
            [str(p) for p in cast(list[Any], raw_prereqs)]
            if isinstance(raw_prereqs, list)
            else failed_prerequisites(check_id, blocked_graph)
        )
        if prereqs and all(p in passed_alone for p in prereqs):
            unblocked.append(check_id)
            prereqs_used.update(prereqs)
    resolved_prereqs = [p for p in passed_order if p in prereqs_used]
    return unblocked, resolved_prereqs


def joined_classification(
    first_cls: dict[str, Any],
    second_cls: dict[str, Any] | None,
    *,
    resolved: Iterable[str] = (),
) -> dict[str, Any]:
    """The classification over two runs of a lane check (#639, fathers#240): drops `dependency-blocked` entries for
    checks in `resolved` that ran in the second pass, recomputes prerequisites on still-blocked checks, combines
    `checks`, `unaccounted` and `runs`, and is green when no unaccounted failure or non-landable check remains."""
    resolved_set = set(resolved)
    first_checks: list[dict[str, Any]] = list(first_cls.get("checks", []))
    first_runs: list[dict[str, Any]] = list(first_cls.get("runs", []))
    first_unaccounted: list[str] = list(first_cls.get("unaccounted", []))
    if second_cls is not None and second_cls.get("skipped"):
        runs = first_runs + list(second_cls.get("runs", []))
        return dict(
            first_cls, raw_status="red", checks=first_checks, skipped=second_cls["skipped"], runs=runs, green=False,
        )
    kept: list[dict[str, Any]] = [
        e for e in first_checks
        if not (e.get("class") == CheckClass.DEPENDENCY_BLOCKED and e.get("check") in resolved_set)
    ]
    second_checks: list[dict[str, Any]] = [] if second_cls is None else list(second_cls.get("checks", []))
    replaced = {str(e["check"]) for e in second_checks if e.get("check")}
    resolved_passed = resolved_set - replaced
    raw_combined = [e for e in kept if str(e.get("check")) not in replaced] + second_checks
    known_classes = {
        str(e["check"]): str(e["class"])
        for e in raw_combined
        if e.get("check") and e.get("class")
    }
    still_blocked_graph = {
        str(e["check"]): [str(b) for b in cast(list[Any], e.get("blocked_by") or [])]
        for e in raw_combined
        if e.get("class") == CheckClass.DEPENDENCY_BLOCKED and isinstance(e.get("blocked_by") or [], list)
    }
    combined_checks: list[dict[str, Any]] = []
    for entry in raw_combined:
        if entry.get("class") == CheckClass.DEPENDENCY_BLOCKED:
            check_id = str(entry["check"])
            raw_blockers: Any = entry.get("blocked_by") or []
            blockers = [str(b) for b in cast(list[Any], raw_blockers)] if isinstance(raw_blockers, list) else []
            roots = [p for p in failed_prerequisites(check_id, still_blocked_graph) if p not in resolved_passed]
            extra = {
                k: v for k, v in entry.items()
                if k not in ("check", "class", "blocked_by", "reason", "prerequisites")
            }
            combined_checks.append(
                dependency_blocked_entry(check_id, blockers, roots, known_classes, **extra)
            )
        else:
            combined_checks.append(entry)
    second_unaccounted: list[str] = [] if second_cls is None else list(second_cls.get("unaccounted", []))
    unaccounted = first_unaccounted + [u for u in second_unaccounted if u not in first_unaccounted]
    runs = first_runs + ([] if second_cls is None else list(second_cls.get("runs", [])))
    skipped = first_cls.get("skipped")
    green = (
        not skipped
        and bool(combined_checks)
        and not unaccounted
        and all(e["class"] in LANDABLE for e in combined_checks)
    )
    base_dict = first_cls if second_cls is None else second_cls
    return dict(
        base_dict,
        raw_status="red",
        checks=combined_checks,
        unaccounted=unaccounted,
        runs=runs,
        green=green,
        **({"skipped": skipped} if skipped else {}),
    )
