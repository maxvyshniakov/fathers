"""Shared agent-session patterns (`fathers_common.agents`) and cross-tool agreement between Slotfather and Spawnfather
on one shared fixture of process tables (fathers#146).

Run from the repository root: python3 -m unittest discover -s common/tests
"""
from __future__ import annotations

from collections.abc import Iterator
import contextlib
import importlib
import os
from pathlib import Path
import sys
import time
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "common", ROOT / "slotfather" / "src", ROOT / "spawnfather"):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from fathers_common import agents, proc  # noqa: E402

sf_lineage: Any = importlib.import_module("slotfather.lineage")
sf_platform: Any = importlib.import_module("slotfather.platform")
sf_procs: Any = importlib.import_module("slotfather.procs")
sf_sweep_os: Any = importlib.import_module("slotfather.sweep_os")
sf_sweep_plan: Any = importlib.import_module("slotfather.sweep_plan")
sp_cpu: Any = importlib.import_module("spawnfather.cpu")
sp_owner: Any = importlib.import_module("spawnfather.owner")
sp_procs: Any = importlib.import_module("spawnfather.procs")
sp_safety: Any = importlib.import_module("spawnfather.safety")

NEW_YORK = "EST5EDT,M3.2.0,M11.1.0"

# One shared fixture of processes: (pid, ppid, uid, rss, tty, epoch, cmd, expected_agent_kind).
# Covers Claude Code, Codex, desktop GUI apps, non-agent scripts, and a reused PID scenario.
BASE_EPOCH = 1_794_480_000  # 2026-11-12 08:00:00 UTC

FIXTURE: tuple[tuple[int, int, int, int, str, int, str, str | None], ...] = (
    (1, 0, 0, 4096, "??", BASE_EPOCH, "/sbin/launchd", None),
    (101, 1, 501, 102400, "??", BASE_EPOCH + 10, "claude --resume abc", "claude"),
    (102, 1, 501, 102400, "??", BASE_EPOCH + 20, "/Users/m/.local/share/claude/versions/2.1.293 --print", "claude"),
    (103, 1, 501, 102400, "??", BASE_EPOCH + 30,
     "/Users/m/Library/Application Support/Claude/claude-code/2.1/x/claude.app/Contents/MacOS/claude --output-format x",
     "claude"),
    (104, 1, 501, 32768, "??", BASE_EPOCH + 40, "/usr/bin/python3 /tmp/test/bin/claude 600", "claude"),
    (201, 1, 501, 81920, "??", BASE_EPOCH + 50, "/usr/local/bin/codex exec", "codex"),
    (202, 1, 501, 81920, "??", BASE_EPOCH + 60, "/opt/bin/CodexCLI --flag", "codex"),
    (203, 1, 501, 81920, "??", BASE_EPOCH + 70,
     "/opt/homebrew/lib/node_modules/@openai/codex-darwin/vendor/aarch64-apple-darwin/codex --model o3", "codex"),
    (301, 1, 501, 65536, "??", BASE_EPOCH + 80, "/Applications/Claude.app/Contents/MacOS/Claude", None),
    (302, 1, 501, 65536, "??", BASE_EPOCH + 90, "/Applications/Codex.app/Contents/MacOS/Codex", None),
    (303, 102, 501, 16384, "??", BASE_EPOCH + 100, "node /w/claude/index.js", None),
    (304, 201, 501, 16384, "??", BASE_EPOCH + 110, "node codex-helper", None),
    (305, 1, 501, 8192, "??", BASE_EPOCH + 120, "tail -n +1 -f /Users/m/.claude/tasks/a.output", None),
    (306, 1, 501, 8192, "??", BASE_EPOCH + 130, "python3 worker.py", None),
)


@contextlib.contextmanager
def zone(name: str) -> Iterator[None]:
    """Run with TZ=`name`, restoring the caller's zone after."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def render_slotfather_sweep_ps(rows: tuple[tuple[int, int, int, int, str, int, str, str | None], ...]) -> str:
    """Format `FIXTURE` as Slotfather's `ps -o pid=,ppid=,uid=,rss=,tty=,lstart=,command=` (local clock)."""
    lines: list[str] = []
    for pid, ppid, uid, rss, tty, epoch, cmd, _ in rows:
        lstart = proc.render(epoch, "local", padded=True)
        lines.append(f"{pid:5d} {ppid:5d} {uid:5d} {rss:7d} {tty:8s} {lstart} {cmd}")
    return "\n".join(lines) + "\n"


def render_spawnfather_ps(rows: tuple[tuple[int, int, int, int, str, int, str, str | None], ...]) -> str:
    """Format `FIXTURE` as Spawnfather's `ps -o pid=,ppid=,pgid=,uid=,pcpu=,tty=,lstart=,command=` (UTC clock)."""
    lines: list[str] = []
    for pid, ppid, uid, _rss, tty, epoch, cmd, _ in rows:
        lstart = proc.render(epoch, "utc", padded=True)
        lines.append(f"{pid:5d} {ppid:5d} {pid:5d} {uid:5d}   0.0 {tty:8s} {lstart} {cmd}")
    return "\n".join(lines) + "\n"


class AgentPatternsTests(unittest.TestCase):
    def test_claude_and_codex_patterns(self) -> None:
        for _pid, _ppid, _uid, _rss, _tty, _epoch, cmd, expected in FIXTURE:
            with self.subTest(cmd=cmd):
                self.assertEqual(expected, agents.agent_kind(cmd))
                self.assertEqual(expected == "claude", agents.is_claude(cmd))
                self.assertEqual(expected == "codex", agents.is_codex(cmd))
                self.assertEqual(expected is not None, agents.is_agent(cmd))


class CrossToolAgreementTests(unittest.TestCase):
    """One shared process-table fixture gives the exact same agent-session and liveness answers from both
    Slotfather and Spawnfather (fathers#146)."""

    def test_slotfather_and_spawnfather_agree_on_agent_sessions(self) -> None:
        with zone(NEW_YORK):
            sf_table = sf_sweep_os.parse_table(render_slotfather_sweep_ps(FIXTURE))
            sp_table = sp_procs.Table(sp_procs.parse(render_spawnfather_ps(FIXTURE)))

        self.assertEqual(set(sf_table), set(sp_table.procs))
        for pid, _ppid, _uid, _rss, _tty, _epoch, cmd, expected_kind in FIXTURE:
            with self.subTest(pid=pid, cmd=cmd):
                sf_proc = sf_table[pid]
                sp_proc = sp_table.procs[pid]
                expected_agent = expected_kind is not None

                sf_is_session = sf_sweep_plan.is_session(sf_proc)
                sf_lineage_session = sf_lineage.session_command(sf_proc.cmd, sf_proc.tty)
                sf_procs_session = sf_procs.session(
                    sf_procs.Process(sf_proc.pid, sf_proc.ppid, sf_proc.uid, sf_proc.tty, sf_proc.start, sf_proc.cmd)
                )
                sp_is_agent = sp_safety.agent(sp_proc)

                self.assertEqual(expected_agent, sf_is_session)
                self.assertEqual(expected_agent, sf_lineage_session)
                self.assertEqual(expected_agent, sf_procs_session)
                self.assertEqual(expected_agent, sp_is_agent)
                self.assertEqual(sf_is_session, sp_is_agent)

    def test_slotfather_and_spawnfather_agree_on_liveness(self) -> None:
        with zone(NEW_YORK):
            sf_table = sf_sweep_os.parse_table(render_slotfather_sweep_ps(FIXTURE))
            sp_table = sp_procs.Table(sp_procs.parse(render_spawnfather_ps(FIXTURE)))
            sf_identity_rows: dict[int, tuple[int, str]] = {
                pid: (row.ppid, row.start) for pid, row in sf_table.items()
            }
            epoch_by_pid: dict[int, int] = {pid: epoch for pid, _pp, _u, _r, _t, epoch, _c, _k in FIXTURE}

            def lookup_epoch(p: int) -> int | None:
                return epoch_by_pid.get(p)

            with mock.patch.object(sf_platform, "start_epoch", side_effect=lookup_epoch):
                for pid, _ppid, _uid, _rss, _tty, epoch, _cmd, kind in FIXTURE:
                    with self.subTest(case="live", pid=pid):
                        sf_proc = sf_table[pid]
                        sp_proc = sp_table.procs[pid]
                        self.assertEqual(float(epoch), sf_proc.started)
                        self.assertEqual(float(epoch), sp_proc.started)
                        self.assertTrue(sp_cpu.same_start(sf_proc.start, sp_proc))

                        sf_alive = sf_platform.alive({"pid": pid, "start": sf_proc.start, "started": epoch},
                                                     sf_identity_rows)
                        owner: dict[str, Any] = {
                            "kind": kind or "shell", "session": "s1" if kind else None,
                            "pid": pid, "pid_start": sp_proc.start,
                        }
                        sp_alive = sp_owner.alive(owner, sp_table, sessions=[])
                        self.assertTrue(sf_alive)
                        self.assertTrue(sp_alive)
                        self.assertEqual(sf_alive, sp_alive)

                # Reused PID: same pid, different start time (shifted by 60 s) -> both tools report not alive.
                reused_pid = 101
                old_epoch = epoch_by_pid[reused_pid] - 60
                old_local = proc.render(old_epoch, "local")
                old_utc = proc.render(old_epoch, "utc")
                sf_reused = sf_platform.alive({"pid": reused_pid, "start": old_local, "started": old_epoch},
                                              sf_identity_rows)
                sp_reused = sp_owner.alive({"kind": "claude", "session": "s1", "pid": reused_pid, "pid_start": old_utc},
                                           sp_table, sessions=[])
                self.assertFalse(sf_reused)
                self.assertFalse(sp_reused)
                self.assertEqual(sf_reused, sp_reused)

                # Exited PID: absent from table -> both tools report not alive.
                gone_pid = 99999
                sf_gone = sf_platform.alive({"pid": gone_pid, "start": old_local, "started": old_epoch},
                                            sf_identity_rows)
                sp_gone = sp_owner.alive({"kind": "codex", "session": "t1", "pid": gone_pid, "pid_start": old_utc},
                                         sp_table, sessions=[])
                self.assertFalse(sf_gone)
                self.assertFalse(sp_gone)
                self.assertEqual(sf_gone, sp_gone)


if __name__ == "__main__":
    unittest.main()
