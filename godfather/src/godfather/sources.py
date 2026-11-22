"""All tool I/O: finding the tools, running their CLIs with an argument vector and a timeout, fixture mode,
and the pure discovery/snapshot state machine wrapped by the thread-safe ToolClient shell.

Godfather never imports a tool's modules and never reads a file in a tool's state directory; it only runs
`<tool> ARGS...` and reads what it prints. GODFATHER_FIXTURES=DIR replaces every such call with a recorded reply:

    DIR/<tool>/replies.json   {"capabilities --json": {"code": 0, "json": {...}}, "status --json --full": ...}
    DIR/host.json             {"cwd": {"PID": "/path"}, "host": "mbp"}   (optional: a holder's working directory,
                              for lsof; the host name the event logs are read for; `processes`, the rows
                              process_table() reads from ps)

A tool is present in fixture mode when DIR/<tool>/ exists; its event log is DIR/<tool>/events.jsonl. A call with no
recorded reply exits 127. A call that would change something (`settings set`, `settings reset`, an action) is recorded
in `recorded` (and appended to GODFATHER_FIXTURES_RECORD, a JSON-lines file, when set) and answered from the replies
like any other, never run.
"""
from __future__ import annotations

import contextlib
import dataclasses
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, NamedTuple, Optional, Protocol, Union, cast

from .model import PsRow

TOOLS = ("slotfather", "lanefather", "spawnfather")  # the names Godfather looks for (SPEC: built in)
DEFAULT_TAGS = {"slotfather": "slot", "lanefather": "lane", "spawnfather": "spwn"}
TOOL_ALIASES: dict[str, tuple[str, ...]] = {"spawnfather": ("spawnfather", "junkfather")}
CAPABILITIES_TIMEOUT: Optional[float] = 2.0
SNAPSHOT_TIMEOUT: Optional[float] = 5.0
SETTINGS_TIMEOUT: Optional[float] = 5.0
ACTION_TIMEOUT = 30.0  # seconds an action may take (SPEC 24)
PROCESS_TIMEOUT = 2.0  # seconds `ps` may take for the whole process table
DISCOVERY_EVERY = 60.0  # seconds between capability checks of a tool
RESOLVE_EVERY = 10.0  # seconds between looks at where a tool's executable resolves
MAX_REPLY = 4 * 1024 * 1024  # a snapshot stays under 4 MiB
WRITES = (("settings", "set"), ("settings", "reset"))
_TAG_RE = re.compile(r"[a-z][a-z0-9]{0,3}")
_EVENT_SCHEMA = 1
_SETTINGS_VERSIONS = (1,)
_SHOW_VERSIONS = (1,)
_UNSET = object()

recorded: list[list[str]] = []  # fixture mode: the argv of every write, in order
_lock = threading.Lock()


class Reply(NamedTuple):
    code: int
    stdout: str
    stderr: str


class SnapshotError(Exception):
    pass


def fixtures() -> Optional[Path]:
    value = os.environ.get("GODFATHER_FIXTURES")
    return Path(value) if value else None


def is_write(argv: list[str]) -> bool:
    return any(tuple(argv[:len(prefix)]) == prefix for prefix in WRITES)


def _fixture_reply(root: Path, tool: str, argv: list[str]) -> Reply:
    try:
        replies = json.loads((root / tool / "replies.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return Reply(127, "", f"{tool}: no fixture replies ({error})")
    recorded_replies: dict[str, Any] = cast(dict[str, Any], replies) if isinstance(replies, dict) else {}
    found: Any = recorded_replies.get(" ".join(argv))
    if not isinstance(found, dict):
        return Reply(127, "", f"{tool}: no fixture for {' '.join(argv)}")
    reply = cast(dict[str, Any], found)
    stdout = json.dumps(reply["json"]) if "json" in reply else str(reply.get("stdout", ""))
    return Reply(int(reply.get("code", 0)), stdout, str(reply.get("stderr", "")))


def _decode_json_reply(tool: str, argv: list[str], reply: Reply) -> tuple[int, Any, str]:
    problem = next((line for line in reply.stderr.splitlines() if line.strip()), "")
    try:
        value = json.loads(reply.stdout) if reply.stdout.strip() else None
    except ValueError:
        return reply.code, None, problem or f"{tool} {' '.join(argv)} printed no JSON"
    return reply.code, value, problem


class Runner(Protocol):
    """I/O boundary for finding and running the tools, reading host process facts, and fixture replay."""

    @property
    def is_fixture(self) -> bool: ...

    def which(self, tool: str) -> Optional[str]: ...

    def realpath(self, path: str) -> str: ...

    def run(self, tool: str, argv: list[str], timeout: Optional[float],
            executable: Optional[str] = None) -> Reply: ...

    def run_json(self, tool: str, argv: list[str], timeout: Optional[float],
                 executable: Optional[str] = None) -> tuple[int, Any, str]: ...

    def act(self, tool: str, argv: list[str], actor: str, executable: Optional[str] = None,
            detach: bool = False) -> Reply: ...

    def event_log(self, tool: str, path: str) -> Path: ...

    def process_cwd(self, pid: Any) -> Optional[str]: ...

    def process_table(self) -> Optional[list[PsRow]]: ...

    def local_host(self) -> Optional[str]: ...


class SystemRunner:
    """Runs real tool CLIs and host queries (`lsof`, `ps`) against the local operating system."""

    @property
    def is_fixture(self) -> bool:
        return False

    def which(self, tool: str) -> Optional[str]:
        for candidate in TOOL_ALIASES.get(tool, (tool,)):
            found = shutil.which(candidate)
            if found:
                return found
        return None

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def run(self, tool: str, argv: list[str], timeout: Optional[float],
            executable: Optional[str] = None) -> Reply:
        command = executable or self.which(tool)
        if not command:
            return Reply(127, "", f"{tool} not found")
        try:
            result = subprocess.run([command, *argv], capture_output=True, text=True, timeout=timeout,
                                    stdin=subprocess.DEVNULL, env=dict(os.environ, NO_COLOR="1"))
        except subprocess.TimeoutExpired:
            limit = timeout if timeout is not None else 0.0
            return Reply(124, "", f"{tool} {' '.join(argv)} took longer than {limit:g} s")
        except OSError as error:
            return Reply(126, "", f"cannot run {tool}: {error.strerror or error}")
        if len(result.stdout) > MAX_REPLY:
            return Reply(125, "", f"{tool} {' '.join(argv)} printed more than 4 MiB")
        return Reply(result.returncode, result.stdout, result.stderr)

    def run_json(self, tool: str, argv: list[str], timeout: Optional[float],
                 executable: Optional[str] = None) -> tuple[int, Any, str]:
        return _decode_json_reply(tool, argv, self.run(tool, argv, timeout, executable))

    def act(self, tool: str, argv: list[str], actor: str, executable: Optional[str] = None,
            detach: bool = False) -> Reply:
        command = [executable or self.which(tool) or tool, *argv] if tool else list(argv)
        if detach:
            try:
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True,
                                 env=dict(os.environ, GODFATHER_ACTOR=actor))
            except OSError as error:
                return Reply(126, "", f"cannot run {command[0]}: {error.strerror or error}")
            return Reply(0, f"opened {argv[-1] if argv else command[0]}", "")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=ACTION_TIMEOUT,
                                    stdin=subprocess.DEVNULL,
                                    env=dict(os.environ, NO_COLOR="1", GODFATHER_ACTOR=actor))
        except subprocess.TimeoutExpired:
            return Reply(124, "", f"{' '.join(command[:2])} took longer than {ACTION_TIMEOUT:g} s")
        except OSError as error:
            return Reply(126, "", f"cannot run {command[0]}: {error.strerror or error}")
        return Reply(result.returncode, result.stdout, result.stderr)

    def event_log(self, tool: str, path: str) -> Path:
        return Path(path)

    def process_cwd(self, pid: Any) -> Optional[str]:
        with contextlib.suppress(OSError):
            return os.readlink(f"/proc/{pid}/cwd")
        try:
            result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True,
                                    text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return next((line[1:] for line in result.stdout.splitlines() if line.startswith("n")), None)

    def process_table(self) -> Optional[list[PsRow]]:
        try:
            result = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,pcpu=,rss=,comm="], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=PROCESS_TIMEOUT,
                                    stdin=subprocess.DEVNULL, env=dict(os.environ, LC_ALL="C"))
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        rows: list[PsRow] = []
        own = os.getpid()
        for line in result.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            try:
                pid, ppid, cpu, rss = int(parts[0]), int(parts[1]), float(parts[2].replace(",", ".")), int(parts[3])
            except ValueError:
                continue
            if not math.isfinite(cpu):
                continue
            raw = parts[4].strip()
            name = (os.path.basename(raw) or raw) if raw.startswith(("/", "-/")) else raw
            if ppid == own and name == "ps":  # this very look
                continue
            rows.append((pid, ppid, cpu, rss, name))
        return rows

    def local_host(self) -> Optional[str]:
        return None


class FixtureRunner:
    """Replays recorded replies from `root/<tool>/replies.json` and records mutating calls."""

    def __init__(self, root: Union[str, Path], record_path: Union[str, Path, None] = None,
                 sink: Optional[list[list[str]]] = None) -> None:
        self.root = Path(root)
        self.record_path = Path(record_path) if record_path is not None else None
        self.sink = sink

    @property
    def is_fixture(self) -> bool:
        return True

    def _folder_name(self, tool: str) -> str:
        for candidate in TOOL_ALIASES.get(tool, (tool,)):
            if (self.root / candidate).is_dir():
                return candidate
        return tool

    def which(self, tool: str) -> Optional[str]:
        folder = self.root / self._folder_name(tool)
        return str(folder) if folder.is_dir() else None

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def _record_target(self) -> tuple[list[list[str]], Optional[Path]]:
        target = self.sink if self.sink is not None else recorded
        if self.record_path is not None:
            return target, self.record_path
        env_record = os.environ.get("GODFATHER_FIXTURES_RECORD")
        return target, Path(env_record) if env_record else None

    def run(self, tool: str, argv: list[str], timeout: Optional[float],
            executable: Optional[str] = None) -> Reply:
        del timeout, executable
        if is_write(argv):
            with _lock:
                target, record = self._record_target()
                target.append([tool, *argv])
                if record is not None:
                    with contextlib.suppress(OSError), open(record, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps([tool, *argv]) + "\n")
        return _fixture_reply(self.root, self._folder_name(tool), argv)

    def run_json(self, tool: str, argv: list[str], timeout: Optional[float],
                 executable: Optional[str] = None) -> tuple[int, Any, str]:
        return _decode_json_reply(tool, argv, self.run(tool, argv, timeout, executable))

    def act(self, tool: str, argv: list[str], actor: str, executable: Optional[str] = None,
            detach: bool = False) -> Reply:
        del executable, detach
        with _lock:
            line = [tool, *argv] if tool else list(argv)
            target, record = self._record_target()
            target.append(line)
            if record is not None:
                with contextlib.suppress(OSError), open(record, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"argv": line, "env": {"GODFATHER_ACTOR": actor}}) + "\n")
        found = _fixture_reply(self.root, self._folder_name(tool), argv) if tool else Reply(127, "", "")
        return found if found.code != 127 else Reply(0, f"recorded: {' '.join(line)}", "")

    def event_log(self, tool: str, path: str) -> Path:
        del path
        return self.root / self._folder_name(tool) / "events.jsonl"

    def process_cwd(self, pid: Any) -> Optional[str]:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            found = json.loads((self.root / "host.json").read_text(encoding="utf-8")).get("cwd", {}).get(str(pid))
            return str(found) if found else None
        return None

    def process_table(self) -> Optional[list[PsRow]]:
        try:
            found: Any = json.loads((self.root / "host.json").read_text(encoding="utf-8")).get("processes")
        except (OSError, ValueError, AttributeError):
            return None
        if not isinstance(found, list):
            return None
        rows: list[PsRow] = []
        for row in cast(list[Any], found):
            with contextlib.suppress(TypeError, ValueError):
                pid, ppid, cpu, rss, name = cast(list[Any], row)
                cpu_val = float(str(cpu).replace(",", "."))
                if math.isfinite(cpu_val):
                    rows.append((int(pid), int(ppid), cpu_val, int(rss), str(name)))
        return rows

    def local_host(self) -> Optional[str]:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            named = json.loads((self.root / "host.json").read_text(encoding="utf-8")).get("host")
            if isinstance(named, str) and named:
                return named
        return None


_SYSTEM_RUNNER = SystemRunner()


def default_runner() -> Runner:
    root = fixtures()
    if root is not None:
        record = os.environ.get("GODFATHER_FIXTURES_RECORD")
        return FixtureRunner(root, Path(record) if record else None)
    return _SYSTEM_RUNNER


def which(tool: str) -> Optional[str]:
    """The executable Godfather runs for `tool`: on PATH, or the fixture directory standing in for it."""
    return default_runner().which(tool)


def run(tool: str, argv: list[str], timeout: Optional[float], executable: Optional[str] = None) -> Reply:
    """`tool ARGV...` with no shell, no terminal and a timeout: its exit code and output. A missing tool, a timeout
    and an oversized reply come back as a non-zero code with the reason on stderr, never as an exception."""
    return default_runner().run(tool, argv, timeout, executable)


def act(tool: str, argv: list[str], actor: str, executable: Optional[str] = None, detach: bool = False) -> Reply:
    """A row action (fathers#88): `tool ARGV...` (or, with no tool, ARGV as it is: the platform opener) with no
    shell, a 30 s timeout and GODFATHER_ACTOR in its environment; `detach` starts it in a session of its own and
    does not wait (an opener that waits for what it opened is no failure). Under GODFATHER_FIXTURES it is recorded with its
    actor, never run, and answered from DIR/<tool>/replies.json where a reply is recorded (else it succeeds)."""
    return default_runner().act(tool, argv, actor, executable, detach)


def run_json(tool: str, argv: list[str], timeout: Optional[float],
             executable: Optional[str] = None) -> tuple[int, Any, str]:
    """run() with its stdout read as JSON: (code, object or None, the first stderr line or why it is not JSON)."""
    return default_runner().run_json(tool, argv, timeout, executable)


def event_log(tool: str, path: str) -> Path:
    """Where a tool's event log is read (docs/events.md): the path its capabilities give, or in fixture mode
    DIR/<tool>/events.jsonl. The one file of a tool Godfather reads, and only by the path the tool gives."""
    return default_runner().event_log(tool, path)


def process_cwd(pid: Any) -> Optional[str]:
    """The directory a holder was started in, which names its project better than most labels (moved from
    Slotfather: /proc where there is one, else lsof)."""
    return default_runner().process_cwd(pid)


def process_table() -> Optional[list[PsRow]]:
    """Every process on the machine as `ps -A` gives it, for MACHINE's top unleased processes (fathers#203): pid,
    parent pid, cpu %, rss KB and the command's base name; None when `ps` fails. Presentation, like a holder's
    directory: no tool owns the machine's process table. In fixture mode DIR/host.json's `processes`
    (`[[pid, ppid, cpu, rss_kb, name], ...]`), None without it; a malformed row is skipped."""
    return default_runner().process_table()


# Pure capability helpers (kept acyclic in `sources`: no imports from `events` or `adapters.*`)

def _dashboard_block(capabilities: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    block = (capabilities or {}).get("dashboard")
    return cast(dict[str, Any], block) if isinstance(block, dict) else {}


def _tag_of(capabilities: Optional[Mapping[str, Any]], fallback: str) -> str:
    tag = _dashboard_block(capabilities).get("tag")
    if tag == "junk" and fallback == "spwn":
        return "spwn"
    return tag if isinstance(tag, str) and _TAG_RE.fullmatch(tag) else fallback


def _log_path(capabilities: Optional[Mapping[str, Any]]) -> Optional[str]:
    log: Any = _dashboard_block(capabilities).get("events")
    if not isinstance(log, dict):
        return None
    versions: Any = cast(dict[str, Any], log).get("versions")
    if not isinstance(versions, list) or _EVENT_SCHEMA not in cast(list[Any], versions):
        return None
    path: Any = cast(dict[str, Any], log).get("path")
    return path if isinstance(path, str) and path.startswith("/") else None


def _settings_supported(capabilities: Optional[Mapping[str, Any]]) -> bool:
    found: Any = _dashboard_block(capabilities).get("settings")
    versions: Any = cast(dict[str, Any], found).get("versions") if isinstance(found, dict) else None
    return isinstance(versions, list) and any(v in _SETTINGS_VERSIONS for v in cast(list[Any], versions))


def _show_supported(capabilities: Optional[Mapping[str, Any]]) -> bool:
    show: Any = _dashboard_block(capabilities).get("show")
    versions: Any = cast(dict[str, Any], show).get("versions") if isinstance(show, dict) else None
    return isinstance(versions, list) and any(v in _SHOW_VERSIONS for v in cast(list[Any], versions))


# Pure discovery & snapshot state machine

@dataclass(frozen=True)
class Discovery:
    """Pure value representing what `<tool> capabilities --json` and PATH lookup found."""
    state: str = "absent"  # absent | unsupported | ok
    problem: Optional[str] = None
    executable: Optional[str] = None
    resolved: Optional[str] = None
    version: Optional[str] = None
    capabilities: Optional[dict[str, Any]] = None
    checked_at: float = -math.inf
    looked_at: float = -math.inf
    generation: int = 0
    transient_problem: Optional[str] = None

    @property
    def checked(self) -> float:
        return self.checked_at

    @property
    def looked(self) -> float:
        return self.looked_at


@dataclass(frozen=True)
class SnapshotState:
    """Pure value representing the tool's last good decoded snapshot and any active poll/transient failure."""
    snapshot: Optional[Mapping[str, Any]] = None
    snapshot_at: Optional[float] = None
    failed_at: Optional[float] = None
    problem: Optional[str] = None


@dataclass(frozen=True)
class ToolState:
    """Immutable view of a tool combining its Discovery, SnapshotState, and derived capability facts."""
    name: str
    tag: str
    discovery: Discovery = field(default_factory=Discovery)
    snapshot_state: SnapshotState = field(default_factory=SnapshotState)
    poll_interval: float = 5.0
    event_log: Optional[str] = None
    event_problem: Optional[str] = None
    settings_supported: bool = False
    settings_problem: Optional[str] = None
    actions: tuple[str, ...] = ()
    show_supported: bool = False

    @property
    def state(self) -> str:
        if self.discovery.state in ("absent", "unsupported"):
            return self.discovery.state
        if self.snapshot_state.failed_at is not None or self.snapshot_state.problem is not None:
            return "stale"
        return "ok"

    @property
    def problem(self) -> Optional[str]:
        if self.discovery.state == "absent":
            return None
        if self.discovery.state == "unsupported":
            return self.discovery.problem
        return self.snapshot_state.problem

    @property
    def executable(self) -> Optional[str]:
        return self.discovery.executable

    @property
    def resolved(self) -> Optional[str]:
        return self.discovery.resolved

    @property
    def version(self) -> Optional[str]:
        return self.discovery.version

    @property
    def capabilities(self) -> Optional[dict[str, Any]]:
        return self.discovery.capabilities

    @property
    def snapshot(self) -> Optional[Mapping[str, Any]]:
        return self.snapshot_state.snapshot

    @property
    def snapshot_at(self) -> Optional[float]:
        return self.snapshot_state.snapshot_at

    @property
    def failed_at(self) -> Optional[float]:
        return self.snapshot_state.failed_at

    @property
    def generation(self) -> int:
        return self.discovery.generation


def snapshot_argv(capabilities: Optional[Mapping[str, Any]]) -> list[str]:
    snapshot: Any = _dashboard_block(capabilities).get("snapshot")
    argv: Any = cast(dict[str, Any], snapshot).get("argv") if isinstance(snapshot, dict) else None
    return [str(part) for part in cast(list[Any], argv)] if isinstance(argv, list) else []


def poll_interval(capabilities: Optional[Mapping[str, Any]], default: float = 5.0,
                  minimum: float = 5.0, maximum: float = 10.0) -> float:
    """Seconds between a tool's polls: its capabilities' `dashboard.snapshot.poll` hint clamped to 5–10 (SPEC 9)."""
    snapshot: Any = _dashboard_block(capabilities).get("snapshot")
    hint: Any = cast(dict[str, Any], snapshot).get("poll") if isinstance(snapshot, dict) else None
    if isinstance(hint, (int, float)) and not isinstance(hint, bool):
        return float(min(maximum, max(minimum, hint)))
    return float(default)


def discovery_due(disc: Discovery, now: float, resolved: Any = _UNSET) -> bool:
    """Whether discovery should run at `now`: every 60 s, or after 10 s when the resolved executable changed."""
    if now - disc.checked_at >= DISCOVERY_EVERY:
        return True
    if now - disc.looked_at < RESOLVE_EVERY:
        return False
    if resolved is _UNSET:
        return True
    return bool(resolved != disc.resolved)


def on_path_checked(disc: Discovery, now_or_resolved: Any, maybe_now: Optional[float] = None) -> Discovery:
    """Record a 10 s path check that found the same resolved executable."""
    if maybe_now is not None:
        now = float(maybe_now)
    elif isinstance(now_or_resolved, (int, float)) and not isinstance(now_or_resolved, bool):
        now = float(now_or_resolved)
    else:
        now = disc.looked_at
    return dataclasses.replace(disc, looked_at=now)


def on_discovered(
    prev: Discovery,
    name: str,
    adapter: Any,
    executable: Optional[str],
    resolved: Optional[str] = None,
    code: int = 0,
    value: Any = None,
    problem: str = "",
    now: float = 0.0,
) -> Discovery:
    """Pure transition for `<tool> capabilities --json`: retains raw `capabilities` and `version` whenever a JSON
    object is returned, and preserves the last good capabilities on a transient failure while `prev.state == 'ok'`."""
    gen = prev.generation + 1
    if not executable:
        return Discovery(
            state="absent",
            problem=None,
            executable=None,
            resolved=None,
            version=None,
            capabilities=None,
            checked_at=now,
            looked_at=now,
            generation=gen,
            transient_problem=None,
        )
    resolved_path = resolved if resolved is not None else executable
    if code != 0 or not isinstance(value, dict):
        if prev.state == "ok" and prev.capabilities is not None:
            # It answered before: a slow or failed answer now (a loaded machine) leaves the contract as it was
            # and records the transient failure for reconcile_discovery() to mark the snapshot stale.
            failure = problem or f"{name} capabilities exited {code}"
            return Discovery(
                state="ok",
                problem=None,
                executable=executable,
                resolved=resolved_path,
                version=prev.version,
                capabilities=prev.capabilities,
                checked_at=now,
                looked_at=now,
                generation=gen,
                transient_problem=failure,
            )
        why = problem if code in (124, 125, 126) or (code == 0 and problem) else f"capabilities --json exited {code}"
        return Discovery(
            state="unsupported",
            problem=f"{name}: no dashboard contract ({why}); update {name}",
            executable=executable,
            resolved=resolved_path,
            version=None,
            capabilities=None,
            checked_at=now,
            looked_at=now,
            generation=gen,
            transient_problem=None,
        )
    caps = dict(cast(dict[str, Any], value))
    version = str(caps.get("version") or "?")
    unsupported_fn = getattr(adapter, "unsupported", None)
    why = unsupported_fn(caps) if callable(unsupported_fn) else None
    if why:
        return Discovery(
            state="unsupported",
            problem=f"{name} {version}: {why}",
            executable=executable,
            resolved=resolved_path,
            version=version,
            capabilities=caps,
            checked_at=now,
            looked_at=now,
            generation=gen,
            transient_problem=None,
        )
    return Discovery(
        state="ok",
        problem=None,
        executable=executable,
        resolved=resolved_path,
        version=version,
        capabilities=caps,
        checked_at=now,
        looked_at=now,
        generation=gen,
        transient_problem=None,
    )


def reconcile_discovery(
    first: Discovery,
    second: Union[Discovery, SnapshotState],
    third: Union[SnapshotState, float, None] = None,
    fourth: Optional[float] = None,
) -> SnapshotState:
    """Reconcile `SnapshotState` after discovery: clear `snapshot`/`snapshot_at`/`failed_at` when transitioning to
    `absent` or `unsupported`; preserve last good `snapshot` and mark stale on transient capabilities failure."""
    if isinstance(second, Discovery):
        disc = second
        snap = third if isinstance(third, SnapshotState) else SnapshotState()
        wall_now = float(fourth) if fourth is not None else time.time()
    else:
        disc = first
        snap = second
        wall_now = float(third) if isinstance(third, (int, float)) and not isinstance(third, bool) else time.time()
    if disc.state in ("absent", "unsupported"):
        return SnapshotState()
    if disc.transient_problem is not None:
        return SnapshotState(
            snapshot=snap.snapshot,
            snapshot_at=snap.snapshot_at,
            failed_at=snap.failed_at if snap.failed_at is not None else wall_now,
            problem=disc.transient_problem,
        )
    return snap


def on_snapshot(
    disc: Discovery,
    snap: SnapshotState,
    third: Any = _UNSET,
    fourth: Any = _UNSET,
    fifth: Any = _UNSET,
    sixth: Any = _UNSET,
    *,
    value: Any = _UNSET,
    failure: Any = _UNSET,
    now: Optional[float] = None,
    expected_generation: Optional[int] = None,
) -> SnapshotState:
    """Pure transition for a snapshot poll result, guarded by `expected_generation` so a stale poll cannot clobber
    newer discovery state."""
    if isinstance(third, int) and not isinstance(third, bool):
        gen = third if expected_generation is None else expected_generation
        raw_value: Any = fourth if value is _UNSET else value
        snap_failure: Any = (None if fifth is _UNSET else fifth) if failure is _UNSET else failure
        wall_arg: Any = sixth if now is None else now
    else:
        raw_value = (None if third is _UNSET else third) if value is _UNSET else value
        snap_failure = (None if fourth is _UNSET else fourth) if failure is _UNSET else failure
        if isinstance(fifth, int) and not isinstance(fifth, bool) and sixth is _UNSET and expected_generation is None and now is not None:
            gen = fifth
            wall_arg = now
        else:
            wall_arg = fifth if now is None else now
            gen = sixth if expected_generation is None and isinstance(sixth, int) and not isinstance(sixth, bool) else expected_generation
    if disc.state in ("absent", "unsupported"):
        return SnapshotState()
    if gen is not None and gen != disc.generation:
        return snap
    wall_now = float(wall_arg) if isinstance(wall_arg, (int, float)) and not isinstance(wall_arg, bool) else time.time()
    snap_value = cast(Optional[Mapping[str, Any]], raw_value) if isinstance(raw_value, Mapping) else None
    if snap_failure is None and snap_value is not None:
        return SnapshotState(snapshot=snap_value, snapshot_at=wall_now, failed_at=None, problem=None)
    return SnapshotState(
        snapshot=snap.snapshot,
        snapshot_at=snap.snapshot_at,
        failed_at=snap.failed_at if snap.failed_at is not None else wall_now,
        problem=str(snap_failure) if snap_failure is not None else "snapshot failed",
    )


def build_tool_state(name: str, adapter: Any, disc: Discovery, snap: SnapshotState) -> ToolState:
    """Build the immutable `ToolState` from `disc` and `snap`, deriving event, settings, show and action support
    from raw `disc.capabilities` even when `dashboard.snapshot` is unsupported."""
    default_tag = str(getattr(adapter, "TAG", DEFAULT_TAGS.get(name, name[:4])))
    tag = _tag_of(disc.capabilities, default_tag) if disc.capabilities else default_tag
    interval = poll_interval(disc.capabilities)
    if disc.state == "absent":
        return ToolState(name=name, tag=tag, discovery=disc, snapshot_state=snap, poll_interval=interval)
    if disc.capabilities is None:
        return ToolState(
            name=name,
            tag=tag,
            discovery=disc,
            snapshot_state=snap,
            poll_interval=interval,
            event_log=None,
            event_problem=disc.problem,
            settings_supported=False,
            settings_problem=disc.problem,
            actions=(),
            show_supported=False,
        )
    caps = disc.capabilities
    ver = disc.version or "?"
    block = _dashboard_block(caps)
    has_dashboard = caps.get("v") == 1 and isinstance(caps.get("dashboard"), dict)
    if not has_dashboard or not block:
        ev_log: Optional[str] = None
        ev_prob: Optional[str] = f"{name} {ver}: no dashboard contract; update {name}"
    elif not isinstance(block.get("events"), dict):
        ev_log = None
        ev_prob = f"{name} {ver}: no event log; update {name}"
    else:
        ev_log = _log_path(caps)
        ev_prob = None if ev_log is not None else f"{name} {ver}: its event log needs a newer godfather"
    if not has_dashboard:
        set_ok = False
        set_prob: Optional[str] = f"{name} {ver}: no dashboard contract; update {name}"
    elif not _settings_supported(caps):
        set_ok = False
        set_prob = f"{name} {disc.version or ''}: no settings contract; update {name}".replace(" :", ":")
    else:
        set_ok = True
        set_prob = None
    raw_actions = block.get("actions")
    actions = tuple(str(item) for item in cast(list[Any], raw_actions)) if isinstance(raw_actions, list) else ()
    show_fn = getattr(adapter, "show_supported", None)
    show_ok = bool(show_fn(caps)) if callable(show_fn) else _show_supported(caps)
    return ToolState(
        name=name,
        tag=tag,
        discovery=disc,
        snapshot_state=snap,
        poll_interval=interval,
        event_log=ev_log,
        event_problem=ev_prob,
        settings_supported=set_ok,
        settings_problem=set_prob,
        actions=actions,
        show_supported=show_ok,
    )


def poll_snapshot(name: str, adapter: Any, disc: Discovery,
                  runner: Optional[Runner] = None) -> Mapping[str, Any]:
    """Run `<tool> <snapshot_argv>` once and decode it through `adapter`. Raises SnapshotError on any failure."""
    if disc.state in ("absent", "unsupported"):
        raise SnapshotError(disc.problem or f"{name} is {disc.state}")
    argv = snapshot_argv(disc.capabilities)
    if not argv:
        raise SnapshotError(f"{name}: no snapshot command")
    active_runner = runner if runner is not None else default_runner()
    code, value, problem = active_runner.run_json(name, argv, SNAPSHOT_TIMEOUT, disc.executable)
    if code != 0:
        raise SnapshotError(problem or f"{name} {' '.join(argv)} exited {code}")
    if value is None:
        raise SnapshotError(problem or f"{name} {' '.join(argv)} printed nothing")
    decode = getattr(adapter, "decode", None)
    try:
        decoded = decode(value) if callable(decode) else value
    except ValueError as error:
        raise SnapshotError(str(error)) from error
    if not isinstance(decoded, Mapping):
        raise SnapshotError(f"{name} {' '.join(argv)} printed no object")
    return cast(Mapping[str, Any], decoded)


class ToolClient:
    """Thread-safe shell around a tool's `Discovery`, `SnapshotState` and `ToolState`.

    - `_state_lock` protects only `_disc`, `_snap` and `_view`, and is never held across I/O.
    - `_discover_lock` ensures single-flight discovery and re-checks `discovery_due` after acquiring.
    - `_poll_lock` serializes `refresh()` snapshot updates while allowing `poll_fresh()` and `state()` concurrently.
    """

    def __init__(
        self,
        name: str,
        adapter: Any,
        *,
        runner: Optional[Runner] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.name = name
        self.adapter = adapter
        self._runner = runner
        self._clock = clock
        self._wall = wall
        self._state_lock = threading.Lock()
        self._discover_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._disc = Discovery()
        self._snap = SnapshotState()
        self._view = build_tool_state(name, adapter, self._disc, self._snap)

    @property
    def runner(self) -> Runner:
        return self._runner if self._runner is not None else default_runner()

    def state(self) -> ToolState:
        with self._state_lock:
            return self._view

    def discover(self, *, force: bool = False) -> ToolState:
        if not force:
            with self._state_lock:
                if not discovery_due(self._disc, self._clock()):
                    return self._view
        with self._discover_lock:
            with self._state_lock:
                disc = self._disc
                now = self._clock()
                if not force and not discovery_due(disc, now):
                    return self._view
                need_full = force or (now - disc.checked_at >= DISCOVERY_EVERY)
            active_runner = self.runner
            found = active_runner.which(self.name)
            resolved = active_runner.realpath(found) if found else None
            if not need_full and not discovery_due(disc, now, resolved):
                with self._state_lock:
                    if self._disc.generation == disc.generation:
                        self._disc = on_path_checked(self._disc, resolved, now)
                        self._view = build_tool_state(self.name, self.adapter, self._disc, self._snap)
                    return self._view
            if found:
                code, value, problem = active_runner.run_json(
                    self.name, ["capabilities", "--json"], CAPABILITIES_TIMEOUT, found
                )
            else:
                code, value, problem = 127, None, ""
            with self._state_lock:
                done_now = self._clock()
                wall_now = self._wall()
                new_disc = on_discovered(
                    self._disc, self.name, self.adapter, found, resolved, code, value, problem, done_now
                )
                self._snap = reconcile_discovery(self._disc, new_disc, self._snap, wall_now)
                self._disc = new_disc
                self._view = build_tool_state(self.name, self.adapter, self._disc, self._snap)
                return self._view

    def refresh(self) -> ToolState:
        current = self.discover()
        if current.state in ("absent", "unsupported"):
            return current
        with self._poll_lock:
            with self._state_lock:
                disc = self._disc
                if disc.state in ("absent", "unsupported"):
                    return self._view
                gen = disc.generation
            try:
                decoded: Optional[Mapping[str, Any]] = poll_snapshot(self.name, self.adapter, disc, self.runner)
                failure: Optional[str] = None
            except (SnapshotError, ValueError) as error:
                decoded = None
                failure = str(error)
            with self._state_lock:
                wall_now = self._wall()
                self._snap = on_snapshot(
                    self._disc, self._snap, decoded, failure, wall_now, expected_generation=gen
                )
                self._view = build_tool_state(self.name, self.adapter, self._disc, self._snap)
                return self._view

    def poll_fresh(self) -> Mapping[str, Any]:
        """Poll and decode a fresh snapshot without mutating `_snap` (used for action revalidation)."""
        current = self.discover()
        if current.state in ("absent", "unsupported"):
            raise SnapshotError(current.problem or f"{self.name} is {current.state}")
        return poll_snapshot(self.name, self.adapter, current.discovery, self.runner)
