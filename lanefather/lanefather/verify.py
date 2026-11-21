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

from . import config, history, host, journal, plan, slots, state
from . import results as result_store
from .gitops import git

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


def _heartbeat_interval() -> float | None:
    """LANEFATHER_VERIFY_HEARTBEAT: seconds of quiet before emitting a progress line (0/off disables)."""
    raw = config.current().env("VERIFY_HEARTBEAT")
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
        heartbeat: float | None = UNRESOLVED,
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
        self.heartbeat = _heartbeat_interval() if heartbeat is UNRESOLVED else heartbeat
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
        self._progress = _ProgressTracker(self.log, self.selection)

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
                           else host.wait_for_load(self.label) if self.wait_for_load else None)
        self._print_banner()
        self._started_at = journal.now()
        self._clock = time.monotonic()
        self._progress.reset(self._clock)
        self._load = {"start": host.load_averages()}
        self._handle = open(self.log, "w")  # noqa: SIM115 - closed in finish/stop, after the child exits
        self._child = subprocess.Popen(self.argv, cwd=self.root, env=self.env,
                                       stdout=self._handle, stderr=subprocess.STDOUT)

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
