"""All tool I/O: finding the tools, running their CLIs with an argument vector and a timeout, and fixture mode.

Godfather never imports a tool's modules and never reads a file in a tool's state directory; it only runs
`<tool> ARGS...` and reads what it prints. GODFATHER_FIXTURES=DIR replaces every such call with a recorded reply:

    DIR/<tool>/replies.json   {"capabilities --json": {"code": 0, "json": {...}}, "status --json --full": ...}
    DIR/host.json             {"cwd": {"PID": "/path"}, "host": "mbp"}   (optional: a holder's working directory,
                              for lsof; the host name the event logs are read for)

A tool is present in fixture mode when DIR/<tool>/ exists; its event log is DIR/<tool>/events.jsonl. A call with no recorded reply exits 127. A call that
would change something (`settings set`, `settings reset`, an action) is recorded in `recorded` (and appended to
GODFATHER_FIXTURES_RECORD, a JSON-lines file, when set) and answered from the replies like any other, never run.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional, cast

TOOLS = ("slotfather", "lanefather", "junkfather")  # the names Godfather looks for (SPEC: built in)
CAPABILITIES_TIMEOUT = 2.0
SNAPSHOT_TIMEOUT = 5.0
SETTINGS_TIMEOUT = 5.0
MAX_REPLY = 4 * 1024 * 1024  # a snapshot stays under 4 MiB
WRITES = (("settings", "set"), ("settings", "reset"))

recorded: list[list[str]] = []  # fixture mode: the argv of every write, in order
_lock = threading.Lock()


class Reply(NamedTuple):
    code: int
    stdout: str
    stderr: str


def fixtures() -> Optional[Path]:
    value = os.environ.get("GODFATHER_FIXTURES")
    return Path(value) if value else None


def which(tool: str) -> Optional[str]:
    """The executable Godfather runs for `tool`: on PATH, or the fixture directory standing in for it."""
    root = fixtures()
    if root is not None:
        folder = root / tool
        return str(folder) if folder.is_dir() else None
    return shutil.which(tool)


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


def is_write(argv: list[str]) -> bool:
    return any(tuple(argv[:len(prefix)]) == prefix for prefix in WRITES)


def run(tool: str, argv: list[str], timeout: float, executable: Optional[str] = None) -> Reply:
    """`tool ARGV...` with no shell, no terminal and a timeout: its exit code and output. A missing tool, a timeout
    and an oversized reply come back as a non-zero code with the reason on stderr, never as an exception."""
    root = fixtures()
    if root is not None:
        if is_write(argv):
            with _lock:
                recorded.append([tool, *argv])
                record = os.environ.get("GODFATHER_FIXTURES_RECORD")
                if record:
                    with contextlib.suppress(OSError), open(record, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps([tool, *argv]) + "\n")
        return _fixture_reply(root, tool, argv)
    command = executable or shutil.which(tool)
    if not command:
        return Reply(127, "", f"{tool} not found")
    try:
        result = subprocess.run([command, *argv], capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL, env=dict(os.environ, NO_COLOR="1"))
    except subprocess.TimeoutExpired:
        return Reply(124, "", f"{tool} {' '.join(argv)} took longer than {timeout:g} s")
    except OSError as error:
        return Reply(126, "", f"cannot run {tool}: {error.strerror or error}")
    if len(result.stdout) > MAX_REPLY:
        return Reply(125, "", f"{tool} {' '.join(argv)} printed more than 4 MiB")
    return Reply(result.returncode, result.stdout, result.stderr)


ACTION_TIMEOUT = 30.0  # seconds an action may take (SPEC 24)


def act(tool: str, argv: list[str], actor: str, executable: Optional[str] = None, detach: bool = False) -> Reply:
    """A row action (fathers#88): `tool ARGV...` (or, with no tool, ARGV as it is: the platform opener) with no
    shell, a 30 s timeout and GODFATHER_ACTOR in its environment; `detach` starts it in a session of its own and
    does not wait (an opener that waits for what it opened is no failure). Under GODFATHER_FIXTURES it is recorded with its
    actor, never run, and answered from DIR/<tool>/replies.json where a reply is recorded (else it succeeds)."""
    root = fixtures()
    if root is not None:
        with _lock:
            line = [tool, *argv] if tool else list(argv)
            recorded.append(line)
            record = os.environ.get("GODFATHER_FIXTURES_RECORD")
            if record:
                with contextlib.suppress(OSError), open(record, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"argv": line, "env": {"GODFATHER_ACTOR": actor}}) + "\n")
        found = _fixture_reply(root, tool, argv) if tool else Reply(127, "", "")
        return found if found.code != 127 else Reply(0, f"recorded: {' '.join(line)}", "")
    command = [executable or shutil.which(tool) or tool, *argv] if tool else list(argv)
    if detach:
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, env=dict(os.environ, GODFATHER_ACTOR=actor))
        except OSError as error:
            return Reply(126, "", f"cannot run {command[0]}: {error.strerror or error}")
        return Reply(0, f"opened {argv[-1] if argv else command[0]}", "")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=ACTION_TIMEOUT,
                                stdin=subprocess.DEVNULL, env=dict(os.environ, NO_COLOR="1", GODFATHER_ACTOR=actor))
    except subprocess.TimeoutExpired:
        return Reply(124, "", f"{' '.join(command[:2])} took longer than {ACTION_TIMEOUT:g} s")
    except OSError as error:
        return Reply(126, "", f"cannot run {command[0]}: {error.strerror or error}")
    return Reply(result.returncode, result.stdout, result.stderr)


def run_json(tool: str, argv: list[str], timeout: float, executable: Optional[str] = None) -> tuple[int, Any, str]:
    """run() with its stdout read as JSON: (code, object or None, the first stderr line or why it is not JSON)."""
    reply = run(tool, argv, timeout, executable)
    problem = next((line for line in reply.stderr.splitlines() if line.strip()), "")
    try:
        value = json.loads(reply.stdout) if reply.stdout.strip() else None
    except ValueError:
        return reply.code, None, problem or f"{tool} {' '.join(argv)} printed no JSON"
    return reply.code, value, problem


def event_log(tool: str, path: str) -> Path:
    """Where a tool's event log is read (docs/events.md): the path its capabilities give, or in fixture mode
    DIR/<tool>/events.jsonl. The one file of a tool Godfather reads, and only by the path the tool gives."""
    root = fixtures()
    return root / tool / "events.jsonl" if root is not None else Path(path)


def process_cwd(pid: Any) -> Optional[str]:
    """The directory a holder was started in, which names its project better than most labels (moved from
    Slotfather: /proc where there is one, else lsof)."""
    root = fixtures()
    if root is not None:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            found = json.loads((root / "host.json").read_text(encoding="utf-8")).get("cwd", {}).get(str(pid))
            return str(found) if found else None
        return None
    with contextlib.suppress(OSError):
        return os.readlink(f"/proc/{pid}/cwd")
    try:
        result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True,
                                timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return next((line[1:] for line in result.stdout.splitlines() if line.startswith("n")), None)


DISCOVERY_EVERY = 60.0  # seconds between capability checks of a tool
RESOLVE_EVERY = 10.0  # seconds between looks at where a tool's executable resolves


class Source:
    """One tool as Godfather sees it: absent, unsupported (with why) or ok, its capabilities, and its last good
    snapshot. State changes come from discover() and from the snapshot worker's results (took())."""

    def __init__(self, name: str, adapter: Any, clock: Any = time.monotonic) -> None:
        self.name = name
        self.adapter = adapter  # adapters.<tool>: tag, supported(capabilities), decode(snapshot)
        self.clock = clock
        self.state = "absent"  # absent | unsupported | ok | stale
        self.problem: Optional[str] = None
        self.capabilities: Optional[dict[str, Any]] = None
        self.executable: Optional[str] = None
        self.resolved: Optional[str] = None
        self.version: Optional[str] = None
        self.snapshot: Optional[Mapping[str, Any]] = None  # its adapter's model (model.py)
        self.snapshot_at: Optional[float] = None  # time.time() of the last good snapshot
        self.failed_at: Optional[float] = None  # time.time() of the first failure since it
        self.checked = -float("inf")
        self.looked = -float("inf")

    @property
    def tag(self) -> str:
        """Its EVENTS tag: the one its capabilities give, else the adapter's."""
        from . import events as _events

        return _events.tag_of(self.capabilities, self.name) if self.capabilities else str(self.adapter.TAG)

    def due(self) -> bool:
        """Whether discovery should run again: every 60 s, and when the executable resolves elsewhere (10 s)."""
        now = self.clock()
        if now - self.checked >= DISCOVERY_EVERY:
            return True
        if now - self.looked >= RESOLVE_EVERY:
            self.looked = now
            found = which(self.name)
            resolved = os.path.realpath(found) if found else None
            return resolved != self.resolved
        return False

    def discover(self) -> None:
        """`<tool> capabilities --json` (2 s): absent when not found, unsupported when it fails or names no
        dashboard contract this Godfather knows, else ok (stale stays stale until a good snapshot)."""
        self.checked = self.looked = self.clock()
        found = which(self.name)
        self.executable, self.resolved = found, (os.path.realpath(found) if found else None)
        if not found:
            self.state, self.problem, self.capabilities, self.version = "absent", None, None, None
            return
        code, value, problem = run_json(self.name, ["capabilities", "--json"], CAPABILITIES_TIMEOUT, found)
        if (code != 0 or not isinstance(value, dict)) and self.state in ("ok", "stale") \
                and self.capabilities is not None:
            # It answered before: a slow or failed answer now (a loaded machine) leaves the screen as it was, stale,
            # rather than replacing it with "update slotfather"; the next discovery asks again.
            self.took(None, problem or f"{self.name} capabilities exited {code}")
            return
        if code != 0 or not isinstance(value, dict):
            self.capabilities = None
            self.state = "unsupported"
            # A tool without the command prints its usage: the exit code says enough; a timeout says why itself
            why = problem if code in (124, 125, 126) or (code == 0 and problem) else f"capabilities --json exited {code}"
            self.problem = f"{self.name}: no dashboard contract ({why}); " \
                           f"update {self.name}"
            return
        self.version = str(cast(dict[str, Any], value).get("version") or "?")
        why = self.adapter.unsupported(value)
        if why:
            self.capabilities, self.state, self.problem = None, "unsupported", f"{self.name} {self.version}: {why}"
            return
        self.capabilities = value
        if self.state in ("absent", "unsupported"):
            self.state, self.problem = "ok", None

    def snapshot_argv(self) -> list[str]:
        block: Any = (self.capabilities or {}).get("dashboard")
        snapshot: Any = cast(dict[str, Any], block).get("snapshot") if isinstance(block, dict) else None
        argv: Any = cast(dict[str, Any], snapshot).get("argv") if isinstance(snapshot, dict) else None
        return [str(part) for part in cast(list[Any], argv)] if isinstance(argv, list) else []

    def poll(self) -> Any:
        """The snapshot command once, decoded: the adapter's model. Raises SnapshotError with why it failed."""
        code, value, problem = run_json(self.name, self.snapshot_argv(), SNAPSHOT_TIMEOUT, self.executable)
        if code != 0:
            raise SnapshotError(problem or f"{self.name} {' '.join(self.snapshot_argv())} exited {code}")
        if value is None:
            raise SnapshotError(problem or f"{self.name} {' '.join(self.snapshot_argv())} printed nothing")
        return value

    def took(self, value: Optional[Mapping[str, Any]], failure: Optional[str]) -> None:
        """A snapshot result: a good one replaces the last, a failure leaves it and marks the source stale."""
        if failure is None and value is not None:
            self.snapshot, self.snapshot_at, self.failed_at = value, time.time(), None
            if self.state in ("ok", "stale"):
                self.state, self.problem = "ok", None
            return
        if self.state in ("ok", "stale"):
            self.state, self.problem = "stale", failure
            self.failed_at = self.failed_at or time.time()


class SnapshotError(Exception):
    pass
