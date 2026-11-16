"""agent-browser's session leftovers and its detached daemons (fathers#115): a fake ~/.agent-browser and fake process
tables, and one real daemon that launchd adopted, reaped with its entry."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
import unittest
from unittest import mock

from support import Case, as_entry, proc, table

from junkfather import browser, cpu, procs, reap, sweep

DAY = 86400.0
OLD = "Thu Nov 12 10:00:00 2026"  # every fake process started then
NEWER = "Thu Nov 12 10:05:00 2026"
DAEMON = "/opt/homebrew/lib/node_modules/agent-browser/bin/agent-browser-darwin-arm64"


def interpreter() -> str:
    """The Python binary that keeps the argv[0] it is given. A macOS framework build's bin/python3 (python.org's,
    GitHub's setup-python, Xcode's) is a launcher that runs Resources/Python.app/Contents/MacOS/Python with argv[0]
    replaced, so a renamed argv[0] needs that binary itself."""
    app = os.path.join(sys.base_prefix, "Resources", "Python.app", "Contents", "MacOS", "Python")
    return app if sys.platform == "darwin" and os.path.isfile(app) else sys.executable


class BrowserCase(Case):
    def setUp(self) -> None:
        super().setUp()
        self.dir = self.tmp / "agent-browser"
        self.dir.mkdir()
        os.environ["JUNKFATHER_AGENT_BROWSER"] = str(self.dir)
        self.now = time.time()

    def session(self, name: str, age: float = 2 * DAY, daemon: Optional[int] = None,
                suffixes: Tuple[str, ...] = (".config", ".target", ".engine")) -> List[Path]:
        made: List[Path] = []
        for suffix in suffixes + ((".pid", ".stream", ".version") if daemon is not None else ()):
            path = self.dir / f"{name}{suffix}"
            path.write_text(str(daemon) if suffix == ".pid" else "x" * 10)
            os.utime(path, (self.now - age, self.now - age))
            made.append(path)
        return made

    def survey(self, here: procs.Table, held: Optional[Dict[int, List[str]]] = None,
               lsof_fails: bool = False) -> browser.Survey:
        def reader(pids: Sequence[int]) -> Optional[Dict[int, List[str]]]:
            return None if lsof_fails else (held or {})
        return browser.Survey(here, self.dir, socket_reader=reader)


class LeftoverTests(BrowserCase):
    def clean(self, survey: browser.Survey, fresh: Optional[procs.Table] = None) -> Dict[str, Any]:
        """browser.clean, its second read of the process table being `fresh` (else the survey's own table)."""
        return browser.clean(survey, self.now, dry_run=False, reader=lambda: fresh or survey.table)

    def test_a_daemon_that_starts_during_the_sweep_keeps_its_session(self):
        self.session("racing-100")
        self.session("pid-only-101")
        survey = self.survey(table(proc(5)))
        self.assertEqual(["pid-only-101", "racing-100"], [s.name for s in survey.leftovers(self.now)])
        (self.dir / "racing-100.sock").write_text("")  # the daemon binds its socket after the survey
        pid_file = self.dir / "pid-only-101.pid"
        pid_file.write_text("777")
        os.utime(pid_file, (self.now - 2 * DAY, self.now - 2 * DAY))
        report = self.clean(survey, fresh=table(proc(5), proc(777, cmd=DAEMON)))
        self.assertEqual(0, report["removed"], report)
        self.assertEqual(["pid-only-101 kept: its daemon 777 runs", "racing-100 kept: racing-100.sock changed within "
                          "the day"], sorted(report["notes"]))
        self.assertEqual(8, len(os.listdir(self.dir)))

    def test_a_starter_that_runs_again_by_the_removal_keeps_it(self):
        self.session("late-300")
        report = self.clean(self.survey(table(proc(5))), fresh=table(proc(300, cmd="bun check.mjs")))
        self.assertEqual((0, ["late-300 kept: its starting pid 300 runs"]), (report["removed"], report["notes"]))

    def test_a_day_old_session_whose_daemon_and_starter_are_gone_is_removed_and_nothing_else(self):
        gone = self.session("menu-dismissal-10752")
        (self.dir / "browsers" / "chrome-149").mkdir(parents=True)
        (self.dir / "sessions").mkdir()
        (self.dir / "sessions" / "saved-default.json").write_text("{}")
        (self.dir / ".encryption-key").write_text("k")
        (self.dir / "notes.txt").write_text("not a session file")
        os.symlink(self.tmp / "elsewhere", self.dir / "linked.config")
        report = self.clean(self.survey(table(proc(5, cmd="sleep 1"))))
        self.assertEqual((1, 3, 30, ["menu-dismissal-10752"]),
                         (report["removed"], report["files"], report["bytes"], report["names"]))
        self.assertFalse(any(p.exists() for p in gone))
        self.assertEqual({".encryption-key", "browsers", "sessions", "notes.txt", "linked.config"},
                         set(os.listdir(self.dir)))

    def test_a_live_daemon_a_running_starter_or_a_recent_change_keeps_the_session(self):
        self.session("with-daemon-100", daemon=200)
        self.session("starter-runs-300")
        self.session("fresh-400", age=3600)
        here = table(proc(200, cmd=DAEMON), proc(300, cmd="bun scripts/check.mjs"))
        report = self.clean(self.survey(here))
        self.assertEqual(0, report["removed"])
        self.assertEqual(12, len(os.listdir(self.dir)))

    def test_a_pid_file_naming_another_program_does_not_keep_it(self):
        self.session("reused-100", daemon=200)
        report = self.clean(self.survey(table(proc(200, cmd="/usr/sbin/cupsd"))))
        self.assertEqual((1, 6), (report["removed"], report["files"]))

    def test_the_dry_run_lists_and_removes_nothing(self):
        self.session("old-1")
        self.session("old-2")
        here = table(proc(5))
        # The second read before the removals is the fake table too: on Linux pid 2 (kthreadd) runs, and a session
        # named for a starting pid that runs is kept.
        with mock.patch.object(procs, "read_or_none", return_value=here):
            found = sweep.sweep(self.store, dry_run=True, table=here, cwds={}, sessions=[], now=self.now)
        report = found["agent_browser"]["leftovers"]
        self.assertEqual((2, ["old-1", "old-2"]), (report["removed"], report["names"]), report)
        self.assertEqual(6, len(os.listdir(self.dir)))
        self.assertIn("agent-browser: would remove 2 leftover session(s), 6 files, 60 bytes: old-1, old-2",
                      sweep.text(found))

    def test_a_daemon_whose_session_cannot_be_told_stops_every_removal(self):
        self.session("old-1")
        here = table(proc(900, cmd=DAEMON))  # adopted by launchd, named by no .pid file
        survey = self.survey(here, lsof_fails=True)
        report = self.clean(survey)
        self.assertEqual(0, report["removed"])
        self.assertEqual(["an agent-browser daemon's session cannot be told (lsof failed): nothing removed"],
                         report["notes"])
        self.assertEqual(3, len(os.listdir(self.dir)))

    def test_a_daemon_holding_a_session_socket_keeps_that_session(self):
        self.session("by-socket-77", suffixes=(".config", ".sock"))
        here = table(proc(900, cmd=DAEMON))
        report = self.clean(self.survey(here, {900: [str(self.dir / "by-socket-77.sock")]}))
        self.assertEqual(0, report["removed"])


class DaemonTests(BrowserCase):
    def fixture(self) -> procs.Table:
        return table(proc(100, cmd="bun scripts/check.mjs"), proc(101, ppid=100, cmd="node child"),
                     proc(200, cmd=DAEMON, start=NEWER, cpu=40.0),
                     proc(201, ppid=200, cmd="Google Chrome for Testing --headless", start=NEWER, cpu=60.0),
                     moment=self.now)

    def test_a_daemon_is_matched_by_the_session_name_and_joins_the_entry_of_its_starter(self):
        self.session("shadow-catalog-100", daemon=200)
        here = self.fixture()
        survey = self.survey(here)
        self.assertEqual([200], [p.pid for p in survey.daemons_below([here.procs[100], here.procs[101]])])
        entry = as_entry({"id": "j000001", "pid": 100, "start": OLD, "seen": "bun scripts/check.mjs",
                          "group": False, "daemons": [[200, NEWER, DAEMON]]})
        members, how = reap.members(entry, here)
        self.assertEqual(([100, 101, 200, 201], "leader"), ([p.pid for p in members], how))

    def test_a_daemon_is_matched_by_its_socket_when_no_pid_file_names_it(self):
        self.session("by-socket-100", suffixes=(".config", ".sock"))
        here = self.fixture()
        survey = self.survey(here, {200: [str(self.dir / "by-socket-100.sock")]})
        self.assertEqual([("socket", "by-socket-100")],
                         [(d.how, d.session.name if d.session else None) for d in survey.daemons])
        self.assertEqual([200], [p.pid for p in survey.daemons_below([here.procs[100]])])

    def test_a_starter_that_started_after_the_daemon_is_another_process(self):
        self.session("reused-100", daemon=200)
        here = table(proc(100, cmd="bash", start="Fri Nov 13 10:00:00 2026"), proc(200, cmd=DAEMON, start=NEWER))
        self.assertEqual([], self.survey(here).daemons_below([here.procs[100]]))

    def test_the_sweep_joins_a_daemon_to_its_entry(self):
        self.session("check-100", daemon=200)
        here = self.fixture()
        entry = self.adopted_entry()
        sweep.sweep(self.store, table=here, cwds={}, sessions=[], now=self.now)
        saved = self.loaded(entry)
        self.assertEqual([[200, NEWER, DAEMON]], saved["daemons"])
        self.assertIn([201, NEWER, "Google Chrome for Testing --headless"], saved["known_seen"])

    def adopted_entry(self) -> str:
        from junkfather import entry as _entry
        made = _entry.new(self.store, ["bun", "scripts/check.mjs"], 3600, "owner", None,
                          {"kind": "shell", "pid": None}, "adopted", "/", now=self.now)
        leader = proc(100, cmd="bun scripts/check.mjs")
        _entry.adopted(self.store, made, leader, [leader])
        return made["id"]

    def test_a_daemon_a_lease_started_is_that_leases_in_the_cpu_report(self):
        self.session("check-100", daemon=200)
        here = self.fixture()
        leases = cpu.lease_rule([{"id": "q-1", "label": "build", "pid": 100, "start": OLD, "tree": [100, 101]}], here)
        survey = self.survey(here)
        found = cpu.report(here, [leases, survey.rule(leases)], {})
        self.assertEqual({"lease": 1.0}, found["attributed"])
        self.assertEqual(0.0, found["unattributed"])
        self.assertEqual([], survey.idle(set(), leases, self.now + 2 * DAY))

    def test_an_unowned_daemon_idle_for_an_hour_is_reported_never_stopped(self):
        self.session("left-behind-555", daemon=200, age=2 * 3600)
        here = table(proc(200, cmd=DAEMON, start=OLD, cpu=0.2), proc(201, ppid=200, cmd="chrome", cpu=0.3),
                     moment=self.now)
        self.session("busy-556", daemon=300, age=2 * 3600)
        here.procs[300] = proc(300, cmd=DAEMON, cpu=30.0)
        here = table(*here.procs.values(), moment=self.now)
        report = sweep.sweep(self.store, table=here, cwds={}, sessions=[], now=self.now)
        (item,) = report["agent_browser"]["idle"]
        self.assertEqual((200, "left-behind-555", "pid file", 555, 0.5, 2),
                         (item["pid"], item["session"], item["matched"], item["starter"], item["cpu"],
                          item["processes"]))
        self.assertEqual([], report["reaped"])
        self.assertIn("idle agent-browser daemon pid 200 (session left-behind-555", sweep.text(report))


class RealDaemonTests(BrowserCase):
    def detached_daemon(self) -> int:
        """A program that shows as agent-browser-fake and double-forked, so launchd adopted it, as agent-browser's
        daemon does (Python itself, its argv[0] renamed)."""
        name = str(self.tmp / "bin" / "agent-browser-fake")
        python = interpreter()
        script = ("import os, sys\npid = os.fork()\nif pid:\n    print(pid, flush=True)\n    os._exit(0)\n"
                  "os.setsid()\nnull = os.open(os.devnull, os.O_RDWR)\nfor fd in (0, 1, 2):\n    os.dup2(null, fd)\n"
                  f"os.execv({python!r}, [{name!r}, '-c', 'import time; time.sleep(600)'])\n")
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)
        pid = int(done.stdout.strip())
        self.addCleanup(self.kill_quietly, pid)
        self.wait_for(lambda: (procs.Table.read().get(pid) or proc(0, cmd="")).program == "agent-browser-fake",
                      what=f"pid {pid} to show as agent-browser-fake (run as {python})",
                      seen=lambda: f"pid {pid}: {(procs.Table.read().get(pid) or proc(0, cmd='(not running)')).cmd}")
        return pid

    def kill_quietly(self, pid: int) -> None:
        try:
            os.kill(pid, 9)
        except OSError:
            pass

    def test_a_detached_daemon_is_reaped_with_the_entry_whose_process_started_it(self):
        entry = self.own_detached(self.env, "--ttl", "1h")
        daemon = self.detached_daemon()
        (self.dir / f"check-{entry['pid']}.pid").write_text(str(daemon))
        result = reap.reap(self.store, entry["id"], "test", grace=2)
        self.assertEqual("stopped", result["result"], result)
        self.assertIn(daemon, result["pids"])
        self.wait_for(lambda: not self.runs(daemon), what="the daemon to end")
        self.assertEqual(daemon, self.loaded(entry["id"])["daemons"][0][0])


if __name__ == "__main__":
    unittest.main()
