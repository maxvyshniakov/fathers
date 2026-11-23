"""Shared helpers: an isolated state directory and session registry, the CLI in process or as a child, and fake tables.

Nothing here reads or changes the real ~/.local/state/spawnfather or ~/.claude/sessions; the CLI runs with the agent
variables of the session running the tests removed and fakes put in their place. By default `Case` stubs host-wide
`ps` and `lsof` reads with an in-memory table (`self.fake_procs`, `self.fake_cwds`) so unit and resource tests never
scan the host; tests that spawn or inspect real OS processes opt in via `self.use_real_procs()` (or `self.spawn()`).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "common"))  # eventcheck: common/eventcheck.py, the shared validator

from spawnfather import browser, cli, procs, settings  # noqa: E402
from spawnfather.entry import Entry  # noqa: E402
from spawnfather.state import Store  # noqa: E402

AGENT_VARIABLES = ("CLAUDE", "CODEX", "LANEFATHER_", "SPAWNFATHER_", "JUNKFATHER_")
FAKE_START = "Thu Nov 12 10:00:00 2026"
_REAL_TABLE_READ = procs.Table.read
_REAL_IDENTITIES = procs.identities
_REAL_WORKING_DIRS_OR_NONE = procs.working_dirs_or_none
_REAL_SOCKETS = browser.sockets
REAL_ENVIRONMENTS = procs.environments


def proc(pid: int, ppid: int = 1, pgid: Optional[int] = None, uid: Optional[int] = None, tty: str = "",
         start: str = FAKE_START, cmd: str = "sleep 300", cpu: float = 0.0) -> procs.Proc:
    return procs.Proc(pid, ppid, pid if pgid is None else pgid, os.getuid() if uid is None else uid, tty, start, cmd,
                      cpu)


def table(*items: procs.Proc, moment: Optional[float] = None) -> procs.Table:
    return procs.Table({p.pid: p for p in items}, moment=moment)


def plain(entry: Entry) -> Dict[str, Any]:
    """An entry as a plain mapping a test can index freely (a copy)."""
    return dict(cast(Mapping[str, Any], entry))


def got(entry: Optional[Entry]) -> Dict[str, Any]:
    """What a transition or a load returned, as a plain mapping; {} for None (indexing it then fails the test)."""
    return plain(entry) if entry is not None else {}


def as_entry(data: Mapping[str, Any]) -> Entry:
    """A test's hand-written entry (often only the keys the code under test reads) as the Entry type."""
    return cast(Entry, data)


class _FakeCwds(Dict[int, str]):
    """In-memory stand-in for `lsof -d cwd`: maps recorded PIDs to their cwd and any other PID to `/`."""

    def get(self, key: int, default: Any = "/") -> Any:  # type: ignore[override]
        return super().get(key, "/" if default is None else default)


class FakeProc:
    """A synthetic process in `Case.fake_procs` standing in for a session or child when no OS process is needed."""

    def __init__(self, pid: int, remove: Callable[[], None]):
        self.pid = pid
        self._remove = remove
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self._remove()

    def terminate(self) -> None:
        self.returncode = -15
        self._remove()

    def wait(self, timeout: Optional[float] = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="spawnfather-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        self.sessions = self.tmp / "sessions"
        self.sessions.mkdir()
        self.env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(AGENT_VARIABLES) or k.endswith("_GOLDEN_WRITE")
        }
        for existing in [
            k for k in os.environ
            if k.startswith(("SPAWNFATHER_", "JUNKFATHER_")) and not k.endswith("_GOLDEN_WRITE")
        ]:
            saved_val = os.environ.pop(existing)
            self.addCleanup(os.environ.__setitem__, existing, saved_val)
        # The machine's Slotfather, agent-browser sessions and repositories stay out of every test: each test that
        # needs one points these at its own fake.
        isolated = {"SPAWNFATHER_STATE": str(self.state), "SPAWNFATHER_CLAUDE_SESSIONS": str(self.sessions),
                    "SPAWNFATHER_SLOTFATHER": str(self.tmp / "no-slotfather"),
                    "SPAWNFATHER_AGENT_BROWSER": str(self.tmp / "no-agent-browser"), "SPAWNFATHER_REPOS": "",
                    "SPAWNFATHER_LANEFATHER_STATE": str(self.tmp / "no-lanefather"),
                    "SPAWNFATHER_LANE_RECORDS": str(self.tmp / "no-lanes")}
        self.env.update(isolated, PYTHONDONTWRITEBYTECODE="1")
        for name, value in isolated.items():
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)
        self.store = Store(self.state)
        self.children: List[subprocess.Popen[Any]] = []
        self.addCleanup(self.kill_children)

        self._real_procs = False
        self._real_lsof = False
        self._next_fake_pid = 500_000
        me = os.getpid()
        self.fake_procs: Dict[int, procs.Proc] = {
            1: proc(1, ppid=0, pgid=1, start=FAKE_START, cmd="launchd"),
            me: proc(me, ppid=1, pgid=me, start=FAKE_START, cmd="python3"),
        }
        self.fake_cwds: Dict[int, str] = {1: "/", me: "/"}
        self.fake_envs: Dict[int, Dict[str, List[str]]] = {}  # pid -> the session variables its environment holds
        for patcher in (
            mock.patch.object(procs.Table, "read", side_effect=self._table_read),
            mock.patch.object(procs, "identities", side_effect=self._identities),
            mock.patch.object(procs, "working_dirs_or_none", side_effect=self._working_dirs_or_none),
            mock.patch.object(browser, "sockets", side_effect=self._sockets),
            mock.patch.object(procs, "environments", side_effect=self._environments),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_real_procs(self, *, lsof: bool = False) -> None:
        """Opt in to real OS `ps` queries (and with `lsof=True`, real `lsof` working-directory and socket scans)."""
        self._real_procs = True
        if lsof:
            self._real_lsof = True

    def fake_proc(self, pid: Optional[int] = None, *, cwd: str = "/", **fields: Any) -> procs.Proc:
        """Register a synthetic process in `self.fake_procs` (and `self.fake_cwds`) for tests that need no OS child."""
        if pid is None:
            pid = self._next_fake_pid
            self._next_fake_pid += 1
        created = proc(pid, **fields)
        self.fake_procs[pid] = created
        self.fake_cwds[pid] = cwd
        return created

    def _table_read(self) -> procs.Table:
        if not self._real_procs:
            return procs.Table(dict(self.fake_procs))
        real = _REAL_TABLE_READ()
        extra = {pid: p for pid, p in self.fake_procs.items() if pid not in real.procs}
        return procs.Table({**real.procs, **extra}, moment=real.moment) if extra else real

    def _identities(self, pids: List[int], command: bool = False) -> Dict[int, Any]:
        if not self._real_procs:
            return {pid: (p.start, p.cmd) if command else p.start
                    for pid in pids if (p := self.fake_procs.get(pid)) is not None}
        found: Dict[int, Any] = {}
        real_pids: List[int] = []
        me = os.getpid()
        for pid in pids:
            if pid in self.fake_procs and pid not in (1, me):
                p = self.fake_procs[pid]
                found[pid] = (p.start, p.cmd) if command else p.start
            else:
                real_pids.append(pid)
        if real_pids:
            found.update(_REAL_IDENTITIES(real_pids, command=command))
        return found

    def _working_dirs_or_none(self, uid: int) -> Optional[Dict[int, str]]:
        if self._real_lsof:
            return _REAL_WORKING_DIRS_OR_NONE(uid)
        return _FakeCwds(self.fake_cwds)

    def _sockets(self, pids: Sequence[int]) -> Optional[Dict[int, List[str]]]:
        if self._real_lsof:
            return _REAL_SOCKETS(pids)
        return {}

    def _environments(self, cmds: Mapping[int, str]) -> Dict[int, Optional[Dict[str, List[str]]]]:
        """A fake process's session variables from `self.fake_envs` (none by default); with real processes, the
        real read for the others."""
        found: Dict[int, Optional[Dict[str, List[str]]]] = {
            pid: dict(self.fake_envs.get(pid) or {}) for pid in cmds if not self._real_procs or pid in self.fake_procs}
        rest = {pid: cmd for pid, cmd in cmds.items() if pid not in found}
        if rest:
            found.update(REAL_ENVIRONMENTS(rest))
        return found

    def kill_children(self) -> None:
        for child in self.children:
            try:
                if child.poll() is None:
                    child.kill()
                child.wait()
            finally:
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()
        if not self._real_procs:
            return
        wanted: Dict[int, str] = {}
        for entry in self.store.entries():
            for pid, start in entry.get("known") or []:
                if isinstance(pid, int) and pid not in self.fake_procs:
                    wanted[pid] = procs.norm(start)
        if not wanted:
            return
        try:
            running = _REAL_IDENTITIES(list(wanted))
        except procs.ProcError:
            return
        for pid, start in wanted.items():
            if running.get(pid) == start:
                with contextlib.suppress(OSError):
                    os.kill(pid, 9)

    def cli_in_process(self, *args: str, env: Optional[Dict[str, str]] = None,
                       input: Optional[str] = None) -> subprocess.CompletedProcess[str]:
        """Run `cli.main(list(args))` in-process with isolated environment, cwd, stdio and signal handlers."""
        import warnings
        saved_env, saved_cwd, saved_stdin = dict(os.environ), os.getcwd(), sys.stdin
        saved_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        out_buf, err_buf = io.StringIO(), io.StringIO()
        code = 1
        try:
            os.environ.clear()
            os.environ.update(self.env if env is None else env)
            os.chdir(ROOT)
            sys.stdin = io.StringIO(input or "")
            settings.forget_warnings()
            with warnings.catch_warnings(), contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                warnings.simplefilter("ignore", ResourceWarning)
                try:
                    code = cli.main(list(args))
                except SystemExit as exc:
                    if isinstance(exc.code, str):
                        print(exc.code, file=sys.stderr)
                    code = exc.code if isinstance(exc.code, int) else 0 if exc.code is None else 1
        finally:
            sys.stdin = saved_stdin
            for sig, handler in saved_signals.items():
                signal.signal(sig, handler)
            os.environ.clear()
            os.environ.update(saved_env)
            os.chdir(saved_cwd)
        return subprocess.CompletedProcess(["spawnfather", *args], code, out_buf.getvalue(), err_buf.getvalue())

    def cli(self, *args: str, env: Optional[Dict[str, str]] = None, input: Optional[str] = None,
            timeout: float = 60, in_process: bool = True) -> subprocess.CompletedProcess[str]:
        if not in_process:
            self.use_real_procs()
            return subprocess.run(
                [sys.executable, "-m", "spawnfather", *args],
                cwd=ROOT,
                env=self.env if env is None else env,
                capture_output=True,
                text=True,
                input=input,
                timeout=timeout,
            )
        if args[:1] == ("own",) and "--kind" not in args:
            self.use_real_procs()
        return self.cli_in_process(*args, env=env, input=input)

    def spawn(self, *argv: str, **options: Any) -> subprocess.Popen[Any]:
        self.use_real_procs()
        options.setdefault("stdout", subprocess.DEVNULL)
        options.setdefault("stderr", subprocess.DEVNULL)
        child: subprocess.Popen[Any] = subprocess.Popen(list(argv), **options)
        if options.get("cwd") is not None:
            self.fake_cwds[child.pid] = os.path.realpath(str(options["cwd"]))
        self.children.append(child)
        return child

    def fake_claude(self, session: str, register: bool = True) -> Any:
        """A process standing in for a Claude Code session, and its registry record."""
        if self._real_procs:
            child = self.spawn("sleep", "600")
            pid = child.pid
            lstart = procs.identities([pid]).get(pid)
            assert isinstance(lstart, str)
            handle: Any = child
        else:
            pid = self._next_fake_pid
            self._next_fake_pid += 1
            lstart = FAKE_START
            self.fake_procs[pid] = proc(pid, start=lstart, cmd="sleep 600")
            self.fake_cwds[pid] = "/"
            handle = FakeProc(pid, lambda p=pid: self.fake_procs.pop(p, None) and None)
        if register:
            utc = time.strftime(procs.LSTART, time.gmtime(procs.started_at(lstart)))
            (self.sessions / f"{pid}.json").write_text(json.dumps(
                {"pid": pid, "sessionId": session, "procStart": utc, "startedAt": int(time.time() * 1000)}))
        return handle

    def claude_env(self, session: str, claude: Any) -> Dict[str, str]:
        return {**self.env, "CLAUDE_CODE_SESSION_ID": session, "CLAUDE_PID": str(claude.pid)}

    def own_detached(self, env: Dict[str, str], *options: str, argv: Tuple[str, ...] = ("sleep", "600")
                     ) -> Dict[str, Any]:
        self.use_real_procs()
        result = self.cli("own", "--detach", *options, "--", *argv, env=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        entry_id = result.stdout.split()[1]
        return self.loaded(entry_id)

    def wait_for(self, predicate: Callable[[], object], timeout: float = 20.0, what: str = "condition",
                 seen: Optional[Callable[[], str]] = None) -> None:
        """Poll `predicate`; on timeout fail with `seen()`, what the test observed, so a failure explains itself."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}" + (f"; saw:\n{seen()}" if seen is not None else ""))

    def fake_agent(self) -> Path:
        """An executable named `claude` that sleeps for its argument: its command line (`<python> .../claude 600`)
        reads as a Claude Code session. A script, not a symlink to sleep: a multicall coreutils (uutils, Ubuntu
        26.04) picks its applet by argv[0] and exits at once for `claude`."""
        path = self.tmp / "bin" / "claude"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"#!{sys.executable}\nimport sys, time\ntime.sleep(float(sys.argv[1]))\n")
        path.chmod(0o755)
        return path

    def under_terminal(self, *argv: str) -> subprocess.Popen[Any]:
        """`argv` run by `script`, which gives it a pseudo-terminal of its own. BSD script (macOS) takes the command
        after the file; util-linux script (Linux) takes it with -c. Skips the test where there is no `script`."""
        found = shutil.which("script")
        if found is None:
            self.skipTest("no `script` here to give a command a terminal")
        command = [found, "-q", "/dev/null", *argv] if sys.platform == "darwin" else \
            [found, "-q", "-e", "-c", shlex.join(argv), "/dev/null"]
        return self.spawn(*command, stdin=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                          env={**os.environ, "SHELL": "/bin/sh"})

    def tree_of(self, child: subprocess.Popen[Any]) -> str:
        """What ps shows of `child` and its descendants, and whether it exited, for a timeout's message."""
        try:
            here = procs.Table.read()
        except procs.ProcError as error:
            return f"ps: {error}"
        rows = [here.get(child.pid), *here.descendants(child.pid)]
        lines = [f"  pid {p.pid} ppid {p.ppid} tty {p.tty or '-'}: {p.cmd}" for p in rows if p is not None]
        code = child.poll()
        if code is not None:
            err = child.stderr.read() if child.stderr is not None else b""
            lines.append(f"  pid {child.pid} exited {code}: {err.decode(errors='replace').strip()[:300]!r}")
        return "\n".join(lines) or f"  pid {child.pid}: not in ps"

    def runs_600(self, child: subprocess.Popen[Any], below: bool = False) -> int:
        """The pid of `child`, or with `below` of a process below it, once ps shows it running its `600` command
        (a shell that runs it, `sh -c ... 600`, is passed over for the command itself)."""
        found: List[int] = []

        def ready() -> bool:
            here = procs.Table.read()
            candidates = here.descendants(child.pid) if below else [here.get(child.pid)]
            found[:] = [p.pid for p in candidates if p is not None and p.cmd.endswith(" 600")
                        and p.program not in ("sh", "bash", "dash", "zsh")]
            return bool(found)
        self.wait_for(ready, what="the child", seen=lambda: self.tree_of(child))
        return found[0]

    def loaded(self, entry_id: str) -> Dict[str, Any]:
        """The saved entry, as a plain mapping a test can index freely; fails the test when there is none."""
        entry = self.store.load(entry_id)
        if entry is None:
            self.fail(f"no entry {entry_id}")
        return plain(entry)

    def state_of(self, entry_id: str) -> Optional[str]:
        entry = self.store.load(entry_id)
        return entry.get("state") if entry else None

    def runs(self, pid: Any) -> bool:
        return pid in procs.identities([pid])

    def hook(self, event: str, session: str, env: Optional[Dict[str, str]] = None, **fields: Any
             ) -> subprocess.CompletedProcess[str]:
        data = {"hook_event_name": event, "session_id": session, **fields}
        return self.cli("hook", env=env, input=json.dumps(data))
