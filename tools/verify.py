"""The repository's gate runner (fathers#235): steps in, results out; it knows nothing about which checks exist.

tools/checks.py names the checks and what a change selects; this module runs them. Its callers:

  python3 tools/checks.py verify    `gate`: the selected checks, skipping those this tree passed already
  .githooks/pre-push                tools/prepush.py, which runs each pushed commit's own `checks.py verify`
  tools/lane-check                  `lane`: Lanefather's check contract (lanefather/docs/reference.md)

A run takes a machine lease first: `slotfather run -n <slots>` when Slotfather is installed and no lease is held
(SLOTFATHER_LEASE), else it runs directly, as on CI. The lease's label, priority, ticket, metadata and yield come from
FATHERS_SLOT_{LABEL,PRIORITY,TICKET,META,YIELDABLE} (.lanefather.toml [checks] slot_env), FATHERS_SLOT_WAIT gives up
waiting after that many seconds, and FATHERS_VERIFY_SLOTS overrides the slot count.

A step starts as soon as its declared memory fits beside the running steps (FATHERS_VERIFY_MEMORY_GIB, the job's
cgroup and available memory on Linux, the leased slots' memory where Slotfather gives one, all memory elsewhere), in
its own session, with its output streamed live as `[name] line`. The first failure stops the others (SIGTERM) unless
keep_going. A step past its timeout fails: SIGTERM to its group, SIGKILL 5 s later. A signal stops every step; a
repeat within a second is the same keypress, a later one kills them and exits 128+n. A reaper process stops the step
groups if the runner itself is killed outright.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, TextIO

SLOT_ENV = "FATHERS_SLOT"  # .lanefather.toml [checks] slot_env: Lanefather names a lane's lease with these
VERIFIED_RECORD = "fathers-verified"  # in the Git common directory: one line per check that passed on a tree
VERIFICATION_DIR = "fathers-verification"  # git rev-parse --git-path: each lane run's logs and report.json
PRIORITIES = ("interactive", "normal", "batch")
WAIT_EXPIRED = 75  # slotfather's exit status when --wait ran out before a lease was granted
REPEAT_SIGNAL = 1.0  # seconds: a second signal this soon is the same keypress (a terminal and a wrapper both send it)
KILL_GRACE = 5.0  # seconds between SIGTERM and SIGKILL for a step that times out or is stopped
CLOSE_GRACE = 2.0  # seconds a step's output may stay open after it exited or was killed before the run stops waiting
FAILURE_TAIL_LINES = 80
POLL = 0.1
GIB = 1024 ** 3
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
GIT_ENVIRONMENT = re.compile(
    r"^GIT_(DIR|WORK_TREE|INDEX_FILE|PREFIX|OBJECT_DIRECTORY|ALTERNATE_OBJECT_DIRECTORIES|QUARANTINE_PATH)$")
SUITE = "verify"
USAGE = ("Usage: tools/lane-check --changed --base REF --plan --json\n"
         "       tools/lane-check [--checks IDS | --suites SUITES] [--keep-going] [--jobs N]")


@dataclass(frozen=True)
class Step:
    name: str
    argv: list[str]
    memory: float = 0.0  # GiB at its peak: what the scheduler fits beside the running steps
    timeout: float = 15  # minutes; 0 for no limit
    cwd: Path | None = None
    inputs: tuple[str, ...] = ()  # repo-relative paths/prefixes (ending in '/'); empty => full tree


@dataclass(frozen=True)
class Result:
    name: str
    status: str  # ok | fail | stopped
    seconds: float
    timed_out: bool = False


@dataclass
class Outcome:
    results: list[Result]
    exit: int
    interrupted: str | None = None  # the signal's name
    forced: bool = False  # a repeat signal killed the steps and the run ended without waiting for them


def clean_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """`environment` without the GIT_* variables a hook runs with: commands elsewhere resolve their own."""
    return {key: value for key, value in environment.items() if not GIT_ENVIRONMENT.match(key)}


def git(root: Path, *arguments: str, env: Mapping[str, str] | None = None) -> str:
    return subprocess.run(["git", *arguments], cwd=root, env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


# ---- leases --------------------------------------------------------------------------------------------------------

def has_slotfather(environment: Mapping[str, str]) -> bool:
    return shutil.which("slotfather", path=environment.get("PATH")) is not None


def leased_slots(environment: Mapping[str, str]) -> int:
    """The slots of the lease this process runs under, or 0 without one."""
    try:
        count = int(environment.get("SLOTFATHER_COUNT", ""))
    except ValueError:
        return 0
    return count if environment.get("SLOTFATHER_LEASE") and count > 0 else 0


def slot_wait(environment: Mapping[str, str]) -> int:
    """Seconds to wait for a lease before giving up (FATHERS_SLOT_WAIT), or 0: as long as it takes."""
    try:
        seconds = int(environment.get(f"{SLOT_ENV}_WAIT", ""))
    except ValueError:
        return 0
    return max(seconds, 0)


def project_name(root: Path) -> str:
    """The main checkout's directory name, the same in every worktree; the checkout's own name elsewhere."""
    try:
        common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    except (OSError, subprocess.CalledProcessError):
        return root.name
    return common.parent.name if common.name == ".git" else root.name


def slot_label(purpose: str, environment: Mapping[str, str], project: str) -> str:
    """Who and what: FATHERS_SLOT_LABEL (a lane's) with the purpose unless it has one already, else the project's."""
    given = environment.get(f"{SLOT_ENV}_LABEL", "")
    if given:
        return given if " " in given or not purpose else f"{given} {purpose}"
    return f"{project} {purpose}"


def leased_argv(slots: str | int, argv: Sequence[str], *, environment: Mapping[str, str], purpose: str,
                project: str, slotfather: bool) -> list[str]:
    """The argv that runs `argv` under a lease, or `argv` itself when one is held already or there is no Slotfather."""
    if environment.get("SLOTFATHER_LEASE") or not slotfather:
        return list(argv)
    context: list[str] = []
    priority = environment.get(f"{SLOT_ENV}_PRIORITY", "")
    if priority:
        if priority not in PRIORITIES:
            raise ValueError(f"{SLOT_ENV}_PRIORITY must be one of {', '.join(PRIORITIES)}, not {priority!r}")
        context += ["--priority", priority]
    ticket = environment.get(f"{SLOT_ENV}_TICKET", "")
    if ticket:
        context += ["--ticket", ticket]
    meta = environment.get(f"{SLOT_ENV}_META", "")
    if meta:
        try:
            fields = json.loads(meta)
        except json.JSONDecodeError:
            raise ValueError(f"{SLOT_ENV}_META is not JSON") from None
        if not isinstance(fields, dict):
            raise ValueError(f"{SLOT_ENV}_META must be a JSON object")
        for key, value in fields.items():
            if key in ("priority", "ticket") or not isinstance(value, str):
                raise ValueError(f"{SLOT_ENV}_META: {key!r} is not a string field slotfather run --meta takes")
            context += ["--meta", f"{key}={value}"]
    if environment.get(f"{SLOT_ENV}_YIELDABLE") == "1":
        context.append("--yieldable")
    wait = slot_wait(environment)
    return ["slotfather", "run", "-n", str(slots), "--label", slot_label(purpose, environment, project), *context,
            *(["--wait", str(wait)] if wait else []), "--", *argv]


def run_forwarding(argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None,
                   input: bytes | None = None) -> tuple[int, int | None]:
    """Runs `argv` in this process group and waits for it: (its exit code, the signal this process got meanwhile).

    SIGTERM and SIGHUP are forwarded; SIGINT is not, because Ctrl-C reaches the child from the terminal already."""
    received: list[int] = []
    try:
        child = subprocess.Popen(list(argv), cwd=cwd, env=None if env is None else dict(env),
                                 stdin=subprocess.PIPE if input is not None else None)
    except OSError as error:
        print(f"{argv[0]}: {error}", file=sys.stderr)
        return 127, None

    def forward(signum: int, _frame: object) -> None:
        received.append(signum)
        if signum != signal.SIGINT:
            with _quiet():
                child.send_signal(signum)

    previous = _install(forward)
    try:
        if input is not None:
            assert child.stdin is not None
            with _quiet():
                child.stdin.write(input)
            with _quiet():
                child.stdin.close()
        code = child.wait()
    finally:
        _restore(previous)
    return (128 - code if code < 0 else code), (received[0] if received else None)


def lease_self(slots: str | int, purpose: str, argv: Sequence[str], *, root: Path,
               environment: Mapping[str, str] | None = None, input: bytes | None = None) -> int | None:
    """Re-runs `argv` (this command) under a lease and returns its exit code; None when no lease applies."""
    env = dict(os.environ) if environment is None else dict(environment)
    if env.get("SLOTFATHER_LEASE") or not has_slotfather(env):
        return None
    command = leased_argv(slots, argv, environment=env, purpose=purpose, project=project_name(root),
                          slotfather=True)
    code, _ = run_forwarding(command, cwd=root, env=env, input=input)
    if code == WAIT_EXPIRED and slot_wait(env):
        print(f"verify: gave up waiting for {slots} slot(s) after {slot_wait(env)}s (slotfather status shows the "
              "queue)", file=sys.stderr)
    return code


# ---- memory --------------------------------------------------------------------------------------------------------

def memory_budget(environment: Mapping[str, str], *, linux: bool, total: float, available: float | None = None,
                  constrained: float | None = None,
                  capacity: Callable[[], Mapping[str, Any]] = lambda: {}) -> float:
    """The GiB the run's steps may hold together: FATHERS_VERIFY_MEMORY_GIB, else the smallest of the job's cgroup
    limit, Linux's available memory and the leased slots' memory (where Slotfather gives a slot one); without a lease
    off Linux, all memory (macOS counts its cache as used). `total`, `available` and `constrained` are bytes."""
    try:
        override = float(environment.get("FATHERS_VERIFY_MEMORY_GIB", ""))
    except ValueError:
        override = 0.0
    if override > 0:
        return override
    limits: list[float] = []
    if constrained and constrained < total:
        limits.append(constrained / GIB)
    if linux and available:
        limits.append(available / GIB)
    leased = leased_slots(environment)
    if leased:
        found = capacity()
        per_slot, slots = found.get("gib_per_slot"), found.get("total")
        if isinstance(per_slot, int | float) and per_slot > 0:
            limits.append(min(leased, slots if isinstance(slots, int) and slots > 0 else leased) * per_slot)
    elif not linux:
        limits.append(total / GIB)
    return min(limits) if limits else math.inf


def slotfather_capacity() -> dict[str, Any]:
    try:
        found = json.loads(subprocess.run(["slotfather", "advise"], capture_output=True, text=True, timeout=30,
                                          check=True).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return found if isinstance(found, dict) else {}


def machine_budget(environment: Mapping[str, str]) -> float:
    total = float(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    linux = sys.platform.startswith("linux")
    available = constrained = None
    if linux:
        with _quiet():
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    available = float(line.split()[1]) * 1024
        with _quiet():
            group = next(line[3:].strip() for line in Path("/proc/self/cgroup").read_text().splitlines()
                         if line.startswith("0::"))
            limit = (Path("/sys/fs/cgroup") / group.lstrip("/") / "memory.max").read_text().strip()
            constrained = float(limit) if limit.isdigit() else None
    return memory_budget(environment, linux=linux, total=total, available=available, constrained=constrained,
                         capacity=slotfather_capacity)


# ---- the runner ----------------------------------------------------------------------------------------------------

def startable(steps: Sequence[Step], finished: Iterable[str], running: Mapping[str, float],
              memory: float) -> list[Step]:
    """The steps to start now, in order: each whose memory fits beside the running ones (`running`: name -> GiB);
    one that never fits starts once nothing runs."""
    done = set(finished)
    held = sum(running.values())
    count = len(running)
    chosen: list[Step] = []
    for step in steps:
        if step.name in done or step.name in running:
            continue
        if count and held + step.memory > memory:
            continue
        chosen.append(step)
        held += step.memory
        count += 1
    return chosen


REAPER = r"""
import os, signal, sys, time
groups, done = set(), False
for line in sys.stdin:
    line = line.strip()
    if line == "done":
        done = True
    elif line[:1] == "+":
        groups.add(int(line[1:]))
    elif line[:1] == "-":
        groups.discard(int(line[1:]))
def send(number):
    for group in groups:
        try:
            os.killpg(group, number)
        except OSError:
            pass
if not done and groups:
    send(signal.SIGTERM)
    time.sleep(5)
    send(signal.SIGKILL)
"""


class Reaper:
    """A process in its own session that stops the step groups still listed when the runner's pipe closes without
    "done": the runner was killed outright, which no handler of its own sees."""

    def __init__(self) -> None:
        self.process = subprocess.Popen([sys.executable, "-c", REAPER], stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def _send(self, line: str) -> None:
        assert self.process.stdin is not None
        with _quiet():
            self.process.stdin.write(f"{line}\n".encode())
            self.process.stdin.flush()

    def add(self, group: int) -> None:
        self._send(f"+{group}")

    def remove(self, group: int) -> None:
        self._send(f"-{group}")

    def done(self) -> None:
        self._send("done")
        assert self.process.stdin is not None
        with _quiet():
            self.process.stdin.close()
        with _quiet():
            self.process.wait(timeout=1)


@dataclass
class _Running:
    step: Step
    process: subprocess.Popen[bytes]
    started: float
    deadline: float
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=FAILURE_TAIL_LINES))
    stopped: bool = False
    timed_out: bool = False
    code: int | None = None
    exited: float | None = None
    closed: bool = False
    kill_at: float | None = None
    abandon_at: float | None = None

    def signal(self, number: int) -> None:
        with _quiet():
            os.killpg(self.process.pid, number)


def run(steps: Sequence[Step], *, memory: float = math.inf, keep_going: bool = False,
        env: Mapping[str, str] | None = None, on_line: Callable[[Step, str], None] | None = None,
        out: TextIO | None = None, reaper: bool = True, clock: Callable[[], float] = time.monotonic) -> Outcome:
    """Runs `steps` side by side as their memory allows and prints the summary; see the module's docstring."""
    stream = sys.stdout if out is None else out
    environment = dict(os.environ if env is None else env)

    def say(text: str = "") -> None:
        stream.write(f"{text}\n")
        stream.flush()

    events: queue.Queue[tuple[str, str, Any]] = queue.Queue()
    running: dict[str, _Running] = {}
    results: list[Result] = []
    tails: dict[str, list[str]] = {}
    pending: list[int] = []
    halted = False
    interrupted: tuple[int, float] | None = None
    groups = Reaper() if reaper else None
    began = clock()

    def write(handle: _Running, text: str) -> None:
        say(f"[{handle.step.name}] {text}")
        if on_line:
            on_line(handle.step, text)
        handle.tail.append(text)

    def read(name: str, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        for raw in iter(process.stdout.readline, b""):
            events.put(("line", name, raw.decode("utf-8", "replace").rstrip("\n")))
        process.stdout.close()
        events.put(("closed", name, None))

    def wait(name: str, process: subprocess.Popen[bytes]) -> None:
        events.put(("exit", name, process.wait()))

    def start(step: Step, index: int) -> None:
        say(f"  start {step.name} ({clock() - began:.1f}s)")
        now = clock()
        try:
            process = subprocess.Popen(step.argv, cwd=step.cwd, env={**environment, "FATHERS_STEP": f"{index}"},
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        except OSError as error:
            say(f"[{step.name}] {error}")
            results.append(Result(step.name, "fail", 0.0))
            if not halted and not keep_going:
                halt(signal.SIGTERM)
            return
        deadline = now + step.timeout * 60 if step.timeout > 0 else math.inf
        running[step.name] = _Running(step, process, now, deadline)
        if groups:
            groups.add(process.pid)
        threading.Thread(target=read, args=(step.name, process), daemon=True).start()
        threading.Thread(target=wait, args=(step.name, process), daemon=True).start()

    def halt(number: int) -> None:
        nonlocal halted
        halted = True
        now = clock()
        for handle in running.values():
            if not handle.stopped:
                handle.stopped = True
                handle.signal(number)
                handle.kill_at = handle.kill_at or now + KILL_GRACE

    def finish(handle: _Running) -> None:
        del running[handle.step.name]
        if groups:
            groups.remove(handle.process.pid)
        passed = handle.code == 0 and not handle.timed_out and not handle.stopped
        status = "ok" if passed else "stopped" if handle.stopped else "fail"
        tails[handle.step.name] = list(handle.tail)
        results.append(Result(handle.step.name, status, clock() - handle.started, handle.timed_out))
        if status == "fail" and not halted and not keep_going:
            halt(signal.SIGTERM)

    def launch() -> None:
        if halted:
            return
        done = [result.name for result in results]
        for step in startable(steps, done, {name: h.step.memory for name, h in running.items()}, memory):
            if halted:
                break
            start(step, steps.index(step))

    def on_signal(number: int, _frame: object) -> None:
        pending.append(number)

    previous = _install(on_signal)
    try:
        say(f"verify: {len(steps)} steps" + (f", {memory:.1f} GiB" if math.isfinite(memory) else ""))
        launch()
        while running:
            while pending:
                number = pending.pop(0)
                now = clock()
                if interrupted is None:
                    say(f"\nverify: {signal.Signals(number).name}, stopping the running steps")
                    interrupted = (number, now)
                    halt(number)
                elif now - interrupted[1] >= REPEAT_SIGNAL:
                    for handle in running.values():
                        handle.signal(signal.SIGKILL)
                    if groups:
                        groups.done()
                    return Outcome(results, 128 + number, signal.Signals(interrupted[0]).name, forced=True)
            with _quiet(queue.Empty):
                kind, name, value = events.get(timeout=POLL)
                handle = running.get(name)
                if handle and kind == "line":
                    write(handle, value)
                elif handle and kind == "closed":
                    handle.closed = True
                elif handle and kind == "exit":
                    handle.code, handle.exited = value, clock()
            now = clock()
            for handle in list(running.values()):
                if now >= handle.deadline and handle.kill_at is None:
                    if not handle.stopped:
                        handle.timed_out = True
                        write(handle, f"timed out after {now - handle.started:.1f}s (timeout: "
                                      f"{handle.step.timeout:g} min); stopping its processes")
                    handle.signal(signal.SIGTERM)
                    handle.kill_at = now + KILL_GRACE
                if handle.kill_at is not None and now >= handle.kill_at and handle.abandon_at is None:
                    handle.signal(signal.SIGKILL)
                    handle.abandon_at = now + CLOSE_GRACE
                ended = handle.exited is not None and (handle.closed or now >= handle.exited + CLOSE_GRACE)
                if ended or (handle.abandon_at is not None and now >= handle.abandon_at):
                    finish(handle)
                    launch()
    finally:
        _restore(previous)
    if groups:
        groups.done()
    for result in results:
        if result.status == "fail":
            how = f"timed out after {result.seconds:.1f}s" if result.timed_out else "failed"
            say(f"\n=== {result.name} {how}; its last {FAILURE_TAIL_LINES} lines:")
            for line in tails.get(result.name, []):
                say(line)
    say("\nverify:")
    symbol = {"ok": "ok  ", "fail": "FAIL", "stopped": "stop"}
    for result in results:
        late = ", timed out" if result.timed_out else ""
        say(f"  {symbol[result.status]} {result.name} ({result.seconds:.1f}s{late})")
    ran = {result.name for result in results}
    for step in steps:
        if step.name not in ran:
            say(f"  skip {step.name}")
    failed = len(results) < len(steps) or any(result.status != "ok" for result in results)
    say(f"{'Failed' if failed else 'Passed'} in {clock() - began:.1f}s")
    if interrupted:
        return Outcome(results, 128 + interrupted[0], signal.Signals(interrupted[0]).name)
    return Outcome(results, 1 if failed else 0)


# ---- the verified-tree cache ---------------------------------------------------------------------------------------

def toolchain() -> tuple[str, str]:
    """What a check's result depends on besides the tree: the Python that ran it and the machine it ran on."""
    return f"py{sys.version_info.major}.{sys.version_info.minor}", f"{sys.platform}-{platform.machine()}"


class Verified:
    """The checks that passed on a tree, per check: `v1 <tree> <python> <platform> <check>` (tab-separated) lines
    in the Git common directory's fathers-verified, shared by every worktree. A check's result depends on the tree
    alone, while which checks a commit selects depends on its base: so the cache is per check, not per commit. The
    pre-push gate also records each commit it passed (`v1 commit:<sha> <python> <platform> pre-push`): once that commit
    is on origin/main, its base there has no change, which selects every check."""

    def __init__(self, common_dir: Path, key: tuple[str, str] | None = None) -> None:
        self.path = common_dir / VERIFIED_RECORD
        self.key = key or toolchain()

    def passed(self, tree: str) -> set[str]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        found: set[str] = set()
        for line in text.splitlines():
            fields = line.split("\t")
            if len(fields) == 5 and fields[0] == "v1" and fields[1] == tree and tuple(fields[2:4]) == self.key:
                found.add(fields[4])
        return found

    def passed_keys(self, keys: Mapping[str, str]) -> set[str]:
        """The names in `keys` (name -> cache key) that have already passed under this toolchain."""
        if not keys:
            return set()
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        by_key: dict[str, set[str]] = {}
        for line in text.splitlines():
            fields = line.split("\t")
            if len(fields) == 5 and fields[0] == "v1" and tuple(fields[2:4]) == self.key:
                by_key.setdefault(fields[1], set()).add(fields[4])
        return {name for name, key in keys.items() if name in by_key.get(key, ())}

    def commit_passed(self, sha: str) -> bool:
        return "pre-push" in self.passed(f"commit:{sha}")

    def record_commit(self, sha: str) -> None:
        self.record(f"commit:{sha}", ["pre-push"])

    def record(self, tree: str, names: Iterable[str]) -> None:
        """Appends one line per check, each in one write: concurrent runs never interleave inside a line."""
        lines = [f"v1\t{tree}\t{self.key[0]}\t{self.key[1]}\t{name}\n" for name in names]
        if not lines:
            return
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            for line in lines:
                os.write(descriptor, line.encode())
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class Checkout:
    head: str
    tree: str
    clean: bool  # `git status --porcelain` is empty: no change and no untracked file outside .gitignore


def checkout(root: Path, env: Mapping[str, str] | None = None) -> Checkout:
    return Checkout(git(root, "rev-parse", "HEAD", env=env), git(root, "rev-parse", "HEAD^{tree}", env=env),
                    not git(root, "status", "--porcelain", env=env))


def common_dir(root: Path, env: Mapping[str, str] | None = None) -> Path:
    return Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", env=env))


def tree_keys(root: Path, tree: str, steps: Sequence[Step], *,
              env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Per-step cache key for `tree`: full `tree` when a step declares no `inputs`, else `inputs:<sha256>` over the
    git object type and hash of each declared input path in `tree`."""
    normalized = {step.name: sorted({spec.rstrip("/") for spec in step.inputs if spec.rstrip("/")}) for step in steps}
    all_paths = sorted({path for paths in normalized.values() for path in paths})
    if not all_paths:
        return {step.name: tree for step in steps}
    payload = "".join(f"{tree}:{path}\n" for path in all_paths)
    result = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objecttype) %(objectname)"],
        cwd=root, env=None if env is None else dict(env), input=payload, capture_output=True, text=True, check=True,
    )
    lines = result.stdout.splitlines()
    if len(lines) != len(all_paths):
        raise ValueError(f"git cat-file returned {len(lines)} lines for {len(all_paths)} paths")
    resolved = {path: ("missing" if line.endswith(" missing") else line)
                for path, line in zip(all_paths, lines, strict=True)}
    keys: dict[str, str] = {}
    for step in steps:
        paths = normalized[step.name]
        if not paths:
            keys[step.name] = tree
            continue
        digest = hashlib.sha256("\n".join(f"{path}\t{resolved[path]}" for path in paths).encode("utf-8")).hexdigest()
        keys[step.name] = f"inputs:{digest}"
    return keys


def gate(steps: Sequence[Step], *, root: Path, keep_going: bool = False, use_cache: bool = True,
         full_tree: bool = False, env: Mapping[str, str] | None = None, out: TextIO | None = None) -> int:
    """Runs `steps` in `root`, skipping those that passed on their input tree before; records the ones that pass. The
    cache is read and written only for a clean checkout whose HEAD and status are the same after the run."""
    environment = clean_environment(os.environ if env is None else env)
    stream = sys.stdout if out is None else out
    before = checkout(root, environment)
    cache = Verified(common_dir(root, environment)) if use_cache and before.clean else None
    keys = ({step.name: before.tree for step in steps} if full_tree
            else tree_keys(root, before.tree, steps, env=environment)) if cache else {}
    done = cache.passed_keys(keys) if cache else set()
    todo = [step for step in steps if step.name not in done]
    for step in steps:
        if step.name in done:
            stream.write(f"verify: {step.name} already passed on tree {before.tree[:10]}\n")
    if not todo:
        stream.write(f"verify: every selected check already passed on tree {before.tree[:10]}\n")
        stream.flush()
        return 0
    stream.flush()
    outcome = run(todo, memory=machine_budget(environment), keep_going=keep_going, env=environment, out=stream)
    if cache and not outcome.forced:
        if checkout(root, environment) == before:
            by_key: dict[str, list[str]] = {}
            for result in outcome.results:
                if result.status == "ok":
                    by_key.setdefault(keys[result.name], []).append(result.name)
            for key, names in by_key.items():
                cache.record(key, names)
        else:
            stream.write("verify: the checkout changed during the run; no check is recorded as passed\n")
            stream.flush()
    return outcome.exit


# ---- Lanefather's check contract -----------------------------------------------------------------------------------

class LaneError(Exception):
    pass


FLAGS = ("--help", "--changed", "--plan", "--json", "--keep-going")
VALUES = ("--base", "--checks", "--suites", "--jobs")


def lane_arguments(argv: Sequence[str]) -> dict[str, str | bool]:
    """`argv` as {option: value or True}; refuses an unknown option, a missing value and a positional argument."""
    found: dict[str, str | bool] = {}
    index = 0
    while index < len(argv):
        name, equals, value = argv[index].partition("=")
        if name in FLAGS and not equals:
            found[name] = True
        elif name in VALUES and equals:
            found[name] = value
        elif name in VALUES and index + 1 < len(argv):
            index += 1
            found[name] = argv[index]
        else:
            raise LaneError(f"unknown or incomplete argument {argv[index]!r}")
        index += 1
    return found


def lane_selection(arguments: Mapping[str, str | bool],
                   catalog: Sequence[tuple[str, Step]]) -> tuple[list[tuple[str, Step]], int | None]:
    """The checks a run selects (`catalog`: id -> step, in order) and its --jobs, or LaneError for a refusal."""
    if "--plan" in arguments:
        raise LaneError("a plan takes --changed --base REF --plan --json")
    if any(name in arguments for name in ("--changed", "--json")) or (
        "--base" in arguments and "--checks" not in arguments
    ):
        raise LaneError("plan flags require --plan")
    if "--checks" in arguments and "--suites" in arguments:
        raise LaneError("choose --checks or --suites")
    jobs: int | None = None
    if "--jobs" in arguments:
        text = str(arguments["--jobs"])
        if not text.isdigit() or int(text) < 1:
            raise LaneError("--jobs must be a positive integer")
        jobs = int(text)
    selected = list(catalog)
    if "--checks" in arguments:
        ids = str(arguments["--checks"]).split(",")
        known = {check_id for check_id, _ in catalog}
        if any(check_id not in known for check_id in ids):
            raise LaneError("unknown check selection")
        selected = [(check_id, step) for check_id, step in catalog if check_id in ids]
    elif "--suites" in arguments:
        suites = str(arguments["--suites"]).split(",")
        if any(suite != SUITE for suite in suites):
            raise LaneError("unknown suite selection")
    if not selected:
        raise LaneError("no configured checks selected")
    return selected, jobs


def lane_phase(environment: Mapping[str, str]) -> str:
    """What the lane's lease is for: the phase Lanefather names in FATHERS_SLOT_META, else `check`."""
    try:
        meta = json.loads(environment.get(f"{SLOT_ENV}_META") or "{}")
    except json.JSONDecodeError:
        return "check"
    phase = meta.get("phase") if isinstance(meta, dict) else None
    return phase if isinstance(phase, str) and phase else "check"


def lane(argv: Sequence[str], catalog: Sequence[tuple[str, Step]], *, root: Path, command: Sequence[str],
         env: Mapping[str, str] | None = None, out: TextIO | None = None) -> int:
    """Lanefather's verify run: `[--checks IDS | --suites verify] [--keep-going] [--jobs N]` over `catalog` (id ->
    step), in a clean committed checkout; writes report.json and prints `Verification report: <path>`. `command`
    re-runs this under a lease."""
    stream = sys.stdout if out is None else out
    environment = clean_environment(os.environ if env is None else env)
    try:
        arguments = lane_arguments(argv)
        if arguments.get("--help"):
            stream.write(USAGE + "\n")
            return 0
        selected, jobs = lane_selection(arguments, catalog)
        slots = environment.get("FATHERS_VERIFY_SLOTS") or str(min(2, jobs) if jobs else 2)
        leased = lease_self(slots, lane_phase(environment), [*command, *argv], root=root, environment=environment)
        if leased is not None:
            return leased
        before = checkout(root, environment)
        if not before.clean:
            raise LaneError("verification requires a clean committed checkout")
    except (LaneError, ValueError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", None) or error
        print(f"lane-check: {detail}", file=sys.stderr)
        return 1
    directory = root / git(root, "rev-parse", "--git-path", VERIFICATION_DIR, env=environment)
    directory.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="run-", dir=directory))
    logs = {check_id: folder / f"{index}.log" for index, (check_id, _) in enumerate(selected)}
    for log in logs.values():
        log.write_text("")
    steps = [Step(check_id, step.argv, step.memory, step.timeout, step.cwd) for check_id, step in selected]

    def on_line(step: Step, text: str) -> None:
        with logs[step.name].open("a", encoding="utf-8") as log:
            log.write(text + "\n")

    outcome = run(steps, memory=machine_budget(environment), keep_going=bool(arguments.get("--keep-going")),
                  env=environment, on_line=on_line, out=stream)
    if outcome.forced:
        return outcome.exit
    changed = checkout(root, environment) != before
    environment_class = ({"class": "inputs-changed", "message": "verification changed committed inputs"}
                         if changed else None)
    by_name = {result.name: result for result in outcome.results}
    status = {"ok": "passed", "fail": "failed", "stopped": "cancelled"}
    report = {
        "v": 1, "head": before.head, "tree": before.tree, "exit": 1 if changed else outcome.exit,
        "environment": environment_class, "interrupted": outcome.interrupted,
        "checks": [{"id": step.name, "suite": SUITE,
                    "status": "void" if changed else status[by_name[step.name].status] if step.name in by_name
                    else "cancelled",
                    "seconds": by_name[step.name].seconds if step.name in by_name else 0,
                    "timed_out": by_name[step.name].timed_out if step.name in by_name else False,
                    "log": str(logs[step.name]), "blocked_by": []} for step in steps],
    }
    file = folder / "report.json"
    file.write_text(json.dumps(report, indent=2) + "\n")
    stream.write(f"Verification report: {file}\n")
    stream.flush()
    return int(report["exit"])


class _quiet:
    """Ignores the errors of a best-effort call (a process already gone, a closed pipe)."""

    def __init__(self, *kinds: type[BaseException]) -> None:
        self.kinds = kinds or (OSError, ValueError)

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind: type[BaseException] | None, *_: object) -> bool:
        return kind is not None and issubclass(kind, self.kinds)


def _install(handler: Callable[[int, Any], None]) -> dict[int, Any] | None:
    if threading.current_thread() is not threading.main_thread():
        return None
    return {number: signal.signal(number, handler) for number in SIGNALS}


def _restore(previous: dict[int, Any] | None) -> None:
    for number, handler in (previous or {}).items():
        signal.signal(number, handler)
