"""All tool I/O: finding the tools, running their CLIs with an argument vector and a timeout, and fixture mode.

Godfather never imports a tool's modules and never reads a file in a tool's state directory; it only runs
`<tool> ARGS...` and reads what it prints. GODFATHER_FIXTURES=DIR replaces every such call with a recorded reply:

    DIR/<tool>/replies.json   {"capabilities --json": {"code": 0, "json": {...}}, "status --json --full": ...}
    DIR/host.json             {"cwd": {"PID": "/path"}}   (optional: a holder's working directory, for lsof)

A tool is present in fixture mode when DIR/<tool>/ exists. A call with no recorded reply exits 127. A call that
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
from typing import Any, NamedTuple, Optional

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
    found = replies.get(" ".join(argv)) if isinstance(replies, dict) else None
    if not isinstance(found, dict):
        return Reply(127, "", f"{tool}: no fixture for {' '.join(argv)}")
    stdout = json.dumps(found["json"]) if "json" in found else str(found.get("stdout", ""))
    return Reply(int(found.get("code", 0)), stdout, str(found.get("stderr", "")))


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


def run_json(tool: str, argv: list[str], timeout: float, executable: Optional[str] = None) -> tuple[int, Any, str]:
    """run() with its stdout read as JSON: (code, object or None, the first stderr line or why it is not JSON)."""
    reply = run(tool, argv, timeout, executable)
    problem = next((line for line in reply.stderr.splitlines() if line.strip()), "")
    try:
        value = json.loads(reply.stdout) if reply.stdout.strip() else None
    except ValueError:
        return reply.code, None, problem or f"{tool} {' '.join(argv)} printed no JSON"
    return reply.code, value, problem


LOG_TAIL = 256 * 1024  # bytes of an event log read from its end


def tail_log(tool: str, path: str, size: int = LOG_TAIL) -> tuple[str, ...]:
    """The last lines of a tool's event log (`capabilities --json` gives its path; docs/events.md), at most `size`
    bytes from its end, a line cut by that start dropped; () when it cannot be read. In fixture mode the log is
    DIR/<tool>/events.jsonl. The one file of a tool Godfather reads, and only by the path the tool gives."""
    root = fixtures()
    target = root / tool / "events.jsonl" if root is not None else Path(path)
    try:
        info = target.stat()
        stamp = (str(target), size, info.st_ino, info.st_size, info.st_mtime_ns)
        if stamp in _tails:  # polled every second: read again only when the log changed (the same tuple)
            return _tails[stamp]
        with open(target, "rb") as handle:
            start = max(0, handle.seek(0, os.SEEK_END) - size)
            handle.seek(max(0, start - 1))
            data = handle.read()
    except OSError:
        return ()
    if start:  # the byte before the part read says whether its first line is whole
        cut, data = data[:1] != b"\n", data[1:]
    else:
        cut = False
    lines = data.decode("utf-8", "replace").splitlines()
    found = tuple(line for line in lines[1 if cut else 0:] if line.strip())
    _tails.clear()
    _tails[stamp] = found
    return found


_tails: dict[tuple[Any, ...], tuple[str, ...]] = {}  # the last log read, by what its stat said


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
        self.snapshot: Optional[dict[str, Any]] = None
        self.snapshot_at: Optional[float] = None  # time.time() of the last good snapshot
        self.failed_at: Optional[float] = None  # time.time() of the first failure since it
        self.checked = -float("inf")
        self.looked = -float("inf")

    @property
    def tag(self) -> str:
        return str(self.adapter.TAG)

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
        self.version = str(value.get("version") or "?")
        why = self.adapter.unsupported(value)
        if why:
            self.capabilities, self.state, self.problem = None, "unsupported", f"{self.name} {self.version}: {why}"
            return
        self.capabilities = value
        if self.state in ("absent", "unsupported"):
            self.state, self.problem = "ok", None

    def snapshot_argv(self) -> list[str]:
        block = (self.capabilities or {}).get("dashboard") or {}
        argv = (block.get("snapshot") or {}).get("argv")
        return [str(part) for part in argv] if isinstance(argv, list) else []

    def poll(self) -> Any:
        """The snapshot command once, decoded: the adapter's model. Raises SnapshotError with why it failed."""
        code, value, problem = run_json(self.name, self.snapshot_argv(), SNAPSHOT_TIMEOUT, self.executable)
        if code != 0:
            raise SnapshotError(problem or f"{self.name} {' '.join(self.snapshot_argv())} exited {code}")
        if value is None:
            raise SnapshotError(problem or f"{self.name} {' '.join(self.snapshot_argv())} printed nothing")
        return value

    def took(self, value: Optional[dict[str, Any]], failure: Optional[str]) -> None:
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
