"""Shared helpers: an isolated state directory and session registry, the CLI as a child process, and fake tables.

Nothing here reads or changes the real ~/.local/state/junkfather or ~/.claude/sessions; the CLI runs with the agent
variables of the session running the tests removed and fakes put in their place.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from junkfather import procs  # noqa: E402
from junkfather.state import Store  # noqa: E402

AGENT_VARIABLES = ("CLAUDE", "CODEX", "LANEFATHER_", "JUNKFATHER_")


def proc(pid: int, ppid: int = 1, pgid: Optional[int] = None, uid: Optional[int] = None, tty: str = "",
         start: str = "Thu Nov 12 10:00:00 2026", cmd: str = "sleep 300") -> procs.Proc:
    return procs.Proc(pid, ppid, pid if pgid is None else pgid, os.getuid() if uid is None else uid, tty, start, cmd)


def table(*items: procs.Proc, moment: Optional[float] = None) -> procs.Table:
    return procs.Table({p.pid: p for p in items}, moment=moment)


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
        self.children: List[subprocess.Popen] = []
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
            timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "junkfather", *args], cwd=ROOT, env=env or self.env,
                              capture_output=True, text=True, input=input, timeout=timeout)

    def spawn(self, *argv: str, **options: Any) -> subprocess.Popen:
        options.setdefault("stdout", subprocess.DEVNULL)
        options.setdefault("stderr", subprocess.DEVNULL)
        child = subprocess.Popen(list(argv), **options)
        self.children.append(child)
        return child

    def fake_claude(self, session: str, register: bool = True) -> subprocess.Popen:
        """A process standing in for a Claude Code session, and its registry record."""
        child = self.spawn("sleep", "600")
        if register:
            start = procs.Table.read().get(child.pid)
            assert start is not None
            utc = time.strftime(procs.LSTART, time.gmtime(procs.started_at(start.start)))
            (self.sessions / f"{child.pid}.json").write_text(json.dumps(
                {"pid": child.pid, "sessionId": session, "procStart": utc, "startedAt": int(time.time() * 1000)}))
        return child

    def claude_env(self, session: str, claude: subprocess.Popen) -> Dict[str, str]:
        return {**self.env, "CLAUDE_CODE_SESSION_ID": session, "CLAUDE_PID": str(claude.pid)}

    def own_detached(self, env: Dict[str, str], *options: str, argv: tuple = ("sleep", "600")) -> Dict[str, Any]:
        result = self.cli("own", "--detach", *options, "--", *argv, env=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        entry_id = result.stdout.split()[1]
        entry = self.store.load(entry_id)
        assert entry is not None
        return entry

    def wait_for(self, predicate, timeout: float = 20.0, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def state_of(self, entry_id: str) -> Optional[str]:
        entry = self.store.load(entry_id)
        return entry.get("state") if entry else None

    def runs(self, pid: int) -> bool:
        return pid in procs.identities([pid])

    def hook(self, event: str, session: str, env: Optional[Dict[str, str]] = None, **fields: Any
             ) -> subprocess.CompletedProcess:
        data = {"hook_event_name": event, "session_id": session, **fields}
        return self.cli("hook", env=env, input=json.dumps(data))
