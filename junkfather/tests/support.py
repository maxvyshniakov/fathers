"""Shared helpers: an isolated state directory and session registry, the CLI as a child process, and fake tables.

Nothing here reads or changes the real ~/.local/state/junkfather or ~/.claude/sessions; the CLI runs with the agent
variables of the session running the tests removed and fakes put in their place.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "common"))  # eventcheck: common/eventcheck.py, the shared validator

from junkfather import procs  # noqa: E402
from junkfather.entry import Entry  # noqa: E402
from junkfather.state import Store  # noqa: E402

AGENT_VARIABLES = ("CLAUDE", "CODEX", "LANEFATHER_", "JUNKFATHER_")


def proc(pid: int, ppid: int = 1, pgid: Optional[int] = None, uid: Optional[int] = None, tty: str = "",
         start: str = "Thu Nov 12 10:00:00 2026", cmd: str = "sleep 300") -> procs.Proc:
    return procs.Proc(pid, ppid, pid if pgid is None else pgid, os.getuid() if uid is None else uid, tty, start, cmd)


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


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="junkfather-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        self.sessions = self.tmp / "sessions"
        self.sessions.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(AGENT_VARIABLES)}
        self.env.update(JUNKFATHER_STATE=str(self.state), JUNKFATHER_CLAUDE_SESSIONS=str(self.sessions),
                        JUNKFATHER_LANE_RECORDS=str(self.tmp / "no-lanes"), PYTHONDONTWRITEBYTECODE="1")
        os.environ["JUNKFATHER_STATE"] = str(self.state)
        os.environ["JUNKFATHER_CLAUDE_SESSIONS"] = str(self.sessions)
        self.addCleanup(os.environ.pop, "JUNKFATHER_STATE", None)
        self.addCleanup(os.environ.pop, "JUNKFATHER_CLAUDE_SESSIONS", None)
        self.store = Store(self.state)
        self.children: List[subprocess.Popen[Any]] = []
        self.addCleanup(self.kill_children)

    def kill_children(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait()
        for entry in self.store.entries():
            for pid, start in entry.get("known") or []:
                if procs.identities([pid]).get(pid) == procs.norm(start):
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass

    def cli(self, *args: str, env: Optional[Dict[str, str]] = None, input: Optional[str] = None,
            timeout: float = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "junkfather", *args], cwd=ROOT, env=env or self.env,
                              capture_output=True, text=True, input=input, timeout=timeout)

    def spawn(self, *argv: str, **options: Any) -> subprocess.Popen[Any]:
        options.setdefault("stdout", subprocess.DEVNULL)
        options.setdefault("stderr", subprocess.DEVNULL)
        child: subprocess.Popen[Any] = subprocess.Popen(list(argv), **options)
        self.children.append(child)
        return child

    def fake_claude(self, session: str, register: bool = True) -> subprocess.Popen[Any]:
        """A process standing in for a Claude Code session, and its registry record."""
        child = self.spawn("sleep", "600")
        if register:
            start = procs.Table.read().get(child.pid)
            assert start is not None
            utc = time.strftime(procs.LSTART, time.gmtime(procs.started_at(start.start)))
            (self.sessions / f"{child.pid}.json").write_text(json.dumps(
                {"pid": child.pid, "sessionId": session, "procStart": utc, "startedAt": int(time.time() * 1000)}))
        return child

    def claude_env(self, session: str, claude: subprocess.Popen[Any]) -> Dict[str, str]:
        return {**self.env, "CLAUDE_CODE_SESSION_ID": session, "CLAUDE_PID": str(claude.pid)}

    def own_detached(self, env: Dict[str, str], *options: str, argv: Tuple[str, ...] = ("sleep", "600")
                     ) -> Dict[str, Any]:
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
