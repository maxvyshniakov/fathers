"""Unowned processes of a Claude Code session that is gone (fathers#168): the sweep stops one only when its
environment ties it to a session and that session is certainly gone, never one a Slotfather lease or an active entry
holds; everything uncertain stays report-only. Fake process tables and environments, an injected clock and stopper;
one test reads a real child's environment and stops it, and the Linux reader runs against a fake /proc on every
system, so a macOS run checks what CI's Linux runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from unittest import mock

from support import REAL_ENVIRONMENTS, Case, as_entry

from spawnfather import leftovers, procs, reap, resources, sweep
from spawnfather.defaults import SESSION_END_KEEP

NOW = 1_900_000_000.0
EARLY = "Thu Nov 12 08:00:00 2026"  # the session's own process started
LEFT = "Thu Nov 12 09:00:00 2026"  # the leftover started
LATER = "Thu Nov 12 11:00:00 2026"  # a process that started after the leftover
SCRATCH = "/scratch-168/"
Leases = Tuple[Optional[List[Dict[str, Any]]], str]


class Leftovers(Case):
    def setUp(self) -> None:
        super().setUp()
        self.stopped: List[List[int]] = []
        os.environ["SPAWNFATHER_SCRATCH_ROOTS"] = SCRATCH
        self.addCleanup(os.environ.pop, "SPAWNFATHER_SCRATCH_ROOTS", None)
        patcher = mock.patch.object(resources, "unowned", return_value=([], []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def leftover(self, pid: int = 700, session: Optional[str] = "s-gone", claude_pid: Optional[str] = None,
                 cmd: str = "node server.js", **fields: Any) -> procs.Proc:
        """A process launchd adopted (ppid 1) in a scratchpad, whose environment names `session`."""
        fields.setdefault("start", LEFT)
        made = self.fake_proc(pid, cwd=f"{SCRATCH}s1", cmd=cmd, **fields)
        env: Dict[str, List[str]] = {}
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = [session]
        if claude_pid:
            env["CLAUDE_PID"] = [claude_pid]
        self.fake_envs[pid] = env
        return made

    def stopper(self, targets: List[procs.Proc], grace: float) -> Dict[str, Any]:
        self.stopped.append(sorted(p.pid for p in targets))
        return {"result": "stopped", "signalled": [p.pid for p in targets], "left": []}

    def sweep(self, sessions: Optional[Sequence[Dict[str, Any]]] = (), leases: Leases = ([], ""),
              dry_run: bool = False) -> Dict[str, Any]:
        return sweep.sweep(self.store, dry_run=dry_run, table=procs.Table(dict(self.fake_procs), moment=NOW),
                           cwds=dict(self.fake_cwds), sessions=None if sessions is None else list(sessions),
                           now=NOW, grace=1, leases=lambda: leases, stopper=self.stopper)

    def ended(self, session: str = "s-gone", reason: str = "prompt_input_exit") -> None:
        """What the SessionEnd hook records when the session ends (HookTests), at the sweeps' clock."""
        leftovers.record_end(self.store, session, reason, now=NOW - 60)

    def events(self, kind: str) -> List[Dict[str, Any]]:
        path = self.state / "events.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [e for e in map(json.loads, lines) if e["kind"] == kind]

    def kept(self, report: Dict[str, Any], pid: int = 700) -> Dict[str, Any]:
        """The report item of a process the sweep kept, and nothing stopped."""
        self.assertEqual([], self.stopped)
        self.assertEqual([], report["stopped_unowned"])
        self.assertEqual([], self.events("unowned.reaped"))
        item = next((i for i in report["unowned"] if i["pid"] == pid), None)
        self.assertIsNotNone(item, report["unowned"])
        assert item is not None
        return item


class StopTests(Leftovers):
    def test_a_reparented_process_of_a_session_that_ended_is_stopped_with_its_tree(self):
        self.leftover()
        self.fake_proc(701, ppid=700, pgid=700, cwd=f"{SCRATCH}s1", start=LEFT, cmd="node worker.js")
        self.ended()
        report = self.sweep()
        self.assertEqual([[700, 701]], self.stopped)
        (row,) = report["stopped_unowned"]
        self.assertEqual(("stopped", 700, "s-gone", [700, 701]), (row["result"], row["pid"], row["session"],
                                                                  row["pids"]))
        self.assertEqual("its Claude Code session s-gone is gone: SessionEnd recorded (prompt_input_exit)",
                         row["reason"])
        self.assertEqual([], report["unowned"], "a stopped process is no longer reported")
        (event,) = self.events("unowned.reaped")
        self.assertEqual((700, "sweep", "s-gone", "stopped", [700, 701]),
                         (event["subject"]["pid"], event["actor"]["via"], event["details"]["session"],
                          event["details"]["result"], event["details"]["pids"]))
        self.assertEqual(row["reason"], event["details"]["reason"])
        self.assertEqual([], self.events("unowned.reported"))
        self.assertEqual([], json.loads((self.state / "unowned.json").read_text())["items"])
        self.assertEqual([700], json.loads((self.state / "sweep.json").read_text())["result"]["stopped_unowned"])
        self.assertIn("stopped unowned pid 700 (its Claude Code session s-gone is gone: SessionEnd recorded "
                      "(prompt_input_exit)) -> stopped (pids 700, 701): node server.js", sweep.text(report))

    def test_its_session_process_gone_is_proof_enough(self):
        self.leftover(claude_pid="650")
        report = self.sweep()
        self.assertEqual([[700]], self.stopped)
        self.assertEqual("its Claude Code session s-gone is gone: its process (pid 650) is gone",
                         report["stopped_unowned"][0]["reason"])

    def test_a_session_pid_reused_by_a_later_process_counts_as_gone(self):
        self.leftover(claude_pid="650")
        self.fake_proc(650, start=LATER, cmd="/x/.local/share/claude/versions/2.1.0")
        report = self.sweep()
        self.assertEqual([[700]], self.stopped)
        self.assertIn("its process (pid 650) is gone", report["stopped_unowned"][0]["reason"])

    def test_a_registry_record_whose_process_is_gone_is_proof_enough(self):
        self.leftover()
        self.fake_proc(650, start=LATER, cmd="node other")
        record = {"pid": 650, "sessionId": "s-gone", "procStart": EARLY}
        report = self.sweep(sessions=[record, {"pid": 651, "sessionId": "s-gone", "procStart": EARLY}])
        self.assertEqual([[700]], self.stopped)
        self.assertIn("the process Claude Code's registry names for it is gone", report["stopped_unowned"][0]["reason"])

    def test_every_top_of_a_group_is_stopped_when_all_name_the_session(self):
        self.leftover(700, pgid=700)
        self.leftover(705, pgid=700, cmd="node helper.js")
        self.ended()
        report = self.sweep()
        self.assertEqual([[700, 705]], self.stopped)
        self.assertEqual([], report["unowned"])

    def test_a_dry_run_says_what_it_would_stop_and_stops_nothing(self):
        self.leftover()
        self.ended()
        report = self.sweep(dry_run=True)
        self.assertEqual([], self.stopped)
        self.assertEqual(["would-stop"], [r["result"] for r in report["stopped_unowned"]])
        self.assertEqual([700], [i["pid"] for i in report["unowned"]], "it still runs")
        self.assertIn("would stop unowned pid 700", sweep.text(report))
        self.assertEqual([], self.events("unowned.reaped"))

    def test_a_stop_that_fails_keeps_reporting_it(self):
        self.leftover()
        self.ended()

        def failing(targets: List[procs.Proc], grace: float) -> Dict[str, Any]:
            return {"result": "failed", "signalled": [700], "left": [700]}

        report = sweep.sweep(self.store, table=procs.Table(dict(self.fake_procs), moment=NOW),
                             cwds=dict(self.fake_cwds), sessions=[], now=NOW, grace=1, leases=lambda: ([], ""),
                             stopper=failing)
        (item,) = report["unowned"]
        self.assertEqual("the stop failed", item["session"]["kept"])
        self.assertEqual(["failed"], [r["result"] for r in report["stopped_unowned"]])
        self.assertEqual("error", self.events("unowned.reaped")[0]["level"])
        self.assertIn("failed to stop unowned pid 700", sweep.text(report))


class ReportOnlyTests(Leftovers):
    def test_a_process_whose_session_runs_is_kept(self):
        self.leftover()
        self.ended()  # a resumed session: it ended once and runs again under a new process
        self.fake_proc(660, start=EARLY, cmd="/x/.local/share/claude/versions/2.1.0")
        item = self.kept(self.sweep(sessions=[{"pid": 660, "sessionId": "s-gone", "procStart": EARLY}]))
        self.assertEqual({"id": "s-gone", "gone": False, "why": "the session runs"}, item["session"])

    def test_a_session_process_that_may_still_be_it_keeps_it(self):
        self.leftover(claude_pid="650")
        self.ended()  # SessionEnd fires on /clear too, while the process goes on
        self.fake_proc(650, start=EARLY, cmd="node cli.js")
        item = self.kept(self.sweep())
        self.assertIsNone(item["session"]["gone"])
        self.assertIn("pid 650, the session's process when pid 700 started, may still be it", item["session"]["why"])

    def test_a_session_process_started_the_same_second_may_still_be_it(self):
        self.leftover(claude_pid="650")
        self.fake_proc(650, start=LEFT, cmd="node cli.js")
        self.assertIsNone(self.kept(self.sweep())["session"]["gone"])

    def test_nothing_that_says_the_session_ended_keeps_it(self):
        self.leftover()
        item = self.kept(self.sweep())
        self.assertEqual((None, "nothing says it ended: no SessionEnd recorded and no record of its process"),
                         (item["session"]["gone"], item["session"]["why"]))
        self.assertIn("its Claude Code session s-gone cannot be told", sweep.text(self.sweep()))

    def test_an_unreadable_registry_keeps_it(self):
        self.leftover(claude_pid="650")
        self.ended()
        os.environ["SPAWNFATHER_CLAUDE_SESSIONS"] = str(self.tmp / "no-registry")
        item = self.kept(self.sweep(sessions=None))
        self.assertEqual("Claude Code's session registry cannot be read", item["session"]["why"])

    def test_a_registry_record_without_a_start_proves_nothing_while_its_pid_runs(self):
        self.leftover()
        self.fake_proc(650, start=LATER, cmd="node other")
        self.assertIsNone(self.kept(self.sweep(sessions=[{"pid": 650, "sessionId": "s-gone"}]))["session"]["gone"])

    def test_a_process_whose_environment_names_no_session_is_only_reported(self):
        self.leftover(session=None, claude_pid="650")
        item = self.kept(self.sweep())
        self.assertNotIn("session", item)

    def test_an_unreadable_environment_is_no_link(self):
        self.leftover()
        self.ended()
        with mock.patch.object(procs, "environments", return_value={700: None}):
            item = self.kept(self.sweep())
        self.assertNotIn("session", item)

    def test_a_group_whose_other_top_cannot_be_read_or_names_another_session_is_kept(self):
        self.leftover(700, pgid=700)
        self.leftover(705, pgid=700, session="s-other")
        self.ended()
        self.ended("s-other")
        self.assertEqual("its processes name 2 sessions", self.kept(self.sweep())["session"]["why"])
        self.fake_envs[705] = {}
        self.assertEqual("pid 705 names no session", self.kept(self.sweep())["session"]["why"])

    def test_a_child_that_names_another_session_keeps_the_tree(self):
        self.leftover()
        self.fake_proc(701, ppid=700, cwd="/", start=LEFT, cmd="node worker.js")
        self.fake_envs[701] = {"CLAUDE_CODE_SESSION_ID": ["s-live"]}
        self.ended()
        self.assertEqual("pid 701 in its tree names another session", self.kept(self.sweep())["session"]["why"])

    def test_the_session_end_hook_records_a_safe_id_and_the_sweep_prunes_it_after_a_week(self):
        for session in ("s-gone", "../escape"):
            result = self.hook("SessionEnd", session, reason="prompt_input_exit")
            self.assertEqual(("", 0), (result.stdout, result.returncode), result.stderr)
        self.assertEqual(["s-gone.json"], os.listdir(self.state / "ended"))
        record = json.loads((self.state / "ended" / "s-gone.json").read_text())
        self.assertEqual(("s-gone", "prompt_input_exit"), (record["session"], record["reason"]))
        leftovers.prune(self.store, record["at"] + SESSION_END_KEEP - 1)
        self.assertEqual(["s-gone.json"], os.listdir(self.state / "ended"))
        leftovers.prune(self.store, record["at"] + SESSION_END_KEEP + 1)
        self.assertEqual([], os.listdir(self.state / "ended"))


class HolderTests(Leftovers):
    """Never touched, whatever the session: a lease's, an active entry's, the sweep's own line."""

    def setUp(self) -> None:
        super().setUp()
        self.ended()

    def test_a_process_inside_a_slotfather_lease_is_never_stopped(self):
        self.leftover()
        self.fake_proc(701, ppid=700, cwd="/", start=LEFT, cmd="cc1")
        lease = {"id": "q-1", "label": "build", "pid": 990, "start": EARLY, "tree": [990, 701]}
        item = self.kept(self.sweep(leases=([lease], "")))
        self.assertEqual("a Slotfather lease holds it or runs above it", item["session"]["kept"])
        self.assertTrue(item["session"]["gone"], "its session is gone, and still it stays")

    def test_a_process_below_a_lease_holder_is_never_stopped(self):
        self.fake_proc(990, cwd="/", start=EARLY, cmd="slotfather run -- make")
        self.leftover(ppid=990)
        lease = {"id": "q-1", "pid": 990, "start": EARLY, "tree": [990]}
        self.assertEqual("a Slotfather lease holds it or runs above it",
                         self.kept(self.sweep(leases=([lease], "")))["session"]["kept"])

    def test_unreadable_leases_stop_nothing(self):
        self.leftover()
        item = self.kept(self.sweep(leases=(None, "slotfather status --json exited 3")))
        self.assertEqual("Slotfather's leases cannot be read (slotfather status --json exited 3)",
                         item["session"]["kept"])

    def test_a_process_an_active_entry_holds_in_its_tree_is_never_stopped(self):
        self.leftover()
        self.fake_proc(701, ppid=700, pgid=701, cwd="/", start=LEFT, cmd="sleep 600")
        for state in ("running", "starting", "reaping"):
            self.store.save(as_entry({"v": 1, "id": "jheld01", "state": state, "pid": 701, "pgid": 701, "start": LEFT,
                                      "seen": "sleep 600", "group": True, "known": [[701, LEFT]], "mode": "detached",
                                      "owner": {}, "created": NOW - 60, "expires": NOW + 3600, "ttl": 3600.0,
                                      "reaping": {"at": NOW, "by": os.getpid(), "by_start": LEFT}}))
            self.stopped.clear()
            item = self.kept(self.sweep())
            self.assertEqual("an active Spawnfather entry holds part of its tree", item["session"]["kept"], state)

    def test_an_entry_that_adopts_it_during_the_sweep_keeps_it(self):
        self.leftover()

        def adopting(cmds: Mapping[int, str]) -> Dict[int, Any]:
            self.store.save(as_entry({"v": 1, "id": "jlate01", "state": "running", "pid": 700, "pgid": 700,
                                      "start": LEFT, "seen": "node server.js", "group": True, "known": [[700, LEFT]],
                                      "mode": "adopted", "owner": {}, "created": NOW, "expires": NOW + 3600,
                                      "ttl": 3600.0}))
            return {pid: dict(self.fake_envs.get(pid) or {}) for pid in cmds}

        report = sweep.sweep(self.store, table=procs.Table(dict(self.fake_procs), moment=NOW),
                             cwds=dict(self.fake_cwds), sessions=[], now=NOW, grace=1, leases=lambda: ([], ""),
                             stopper=self.stopper, environ=adopting)
        self.assertEqual("an active Spawnfather entry holds part of its tree", self.kept(report)["session"]["kept"])

    def test_an_agent_browser_daemon_is_never_stopped(self):
        self.leftover(cmd="/x/bin/agent-browser-darwin-arm64 --session s1")
        self.assertEqual("an agent-browser daemon, whose browser session another agent may use",
                         self.kept(self.sweep())["session"]["kept"])

    def test_the_sweep_and_its_ancestors_are_never_stopped(self):
        me = os.getpid()
        self.leftover(600)  # the sweep's parent: protected, never reported
        self.fake_procs[me] = self.fake_procs[me]._replace(ppid=600)
        self.fake_envs[me] = {"CLAUDE_CODE_SESSION_ID": ["s-gone"]}
        self.leftover(700)
        self.sweep()
        self.assertEqual([[700]], self.stopped)

    def test_a_terminal_or_an_agent_session_below_it_is_never_stopped(self):
        self.leftover()
        self.fake_proc(701, ppid=700, tty="ttys009", cwd="/", start=LEFT, cmd="-zsh")
        report = self.sweep()
        self.assertEqual([], self.stopped)
        self.assertEqual([], report["unowned"], "its tree holds a terminal: not even reported")


class EnvironmentTests(Case):
    def test_a_real_child_is_tied_to_its_session_through_its_environment_and_stopped(self):
        self.use_real_procs()
        ended = subprocess.Popen(["true"])  # the session's process: it ran, and is gone
        ended.wait()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CLAUDE_CODE_SESSION_ID": "s-real-168",
               "CLAUDE_PID": str(ended.pid)}
        child = self.spawn(sys.executable, "-c", "import sys; sys.stdin.read()", stdin=subprocess.PIPE, env=env)

        def seen() -> Optional[procs.Proc]:
            found = procs.Table.read().get(child.pid)
            return found if found is not None and "sys.stdin.read()" in found.cmd else None

        self.wait_for(seen, what="the child to run its command")
        mine = seen()
        assert mine is not None
        read = REAL_ENVIRONMENTS({mine.pid: mine.cmd})[mine.pid]
        if sys.platform == "darwin" and read == {}:
            self.skipTest(f"{sys.executable} hides its environment from ps (an Apple program)")
        self.assertEqual({"CLAUDE_CODE_SESSION_ID": ["s-real-168"], "CLAUDE_PID": [str(ended.pid)]}, read)
        self.assertIsNone(REAL_ENVIRONMENTS({mine.pid: "another command"})[mine.pid], "a changed command: unread")
        item = {"pid": mine.pid, "start": mine.start, "cmd": mine.cmd, "cwd": "/", "also": [], "tree": [mine.pid]}
        leftovers.record_end(self.store, "s-real-168", "prompt_input_exit", now=time.time())
        kept, rows = leftovers.stop_gone(self.store, [item], procs.Table.read(), [],
                                         leftovers.Holders(set(), set(), set()), 5.0, False, time.time(),
                                         environ=REAL_ENVIRONMENTS, stopper=reap.stop)
        self.assertEqual(([], ["stopped"]), (kept, [r["result"] for r in rows]))
        self.assertIsNotNone(child.wait(30), "the child ended")


class ProcEnvironTests(unittest.TestCase):
    """procs.environments' Linux branch, /proc/<pid>/cmdline and /proc/<pid>/environ, on a fake /proc."""

    CMD = "node server.js --port 3000"
    ENV = {"CLAUDE_CODE_SESSION_ID": ["s-linux-168"], "CLAUDE_PID": ["4242"]}

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        patcher = mock.patch.object(procs.sys, "platform", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)

    def process(self, pid: int, argv: Sequence[str], env: Mapping[str, str]) -> Path:
        here = self.root / str(pid)
        here.mkdir()
        (here / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in argv))
        (here / "environ").write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items()))
        return here

    def read(self, cmds: Mapping[int, str]) -> Dict[int, Optional[Dict[str, List[str]]]]:
        return REAL_ENVIRONMENTS(cmds, proc_root=str(self.root))

    def test_the_command_the_table_read_has_its_session_read_from_environ(self) -> None:
        self.process(700, self.CMD.split(" "), {"PATH": "/usr/bin", "CLAUDE_CODE_SESSION_ID": "s-linux-168",
                                                "CLAUDE_PID": "4242", "OTHER": "a=b"})
        self.assertEqual({700: self.ENV}, self.read({700: self.CMD}))

    def test_a_reused_pid_whose_cmdline_differs_is_never_read(self) -> None:
        self.process(700, ["python3", "-m", "http.server"], {"CLAUDE_CODE_SESSION_ID": "s-someone-else",
                                                             "CLAUDE_PID": "1"})
        self.assertEqual({700: None}, self.read({700: self.CMD}))
        self.assertEqual({700: None}, self.read({700: "python3 -m http.server --more"}),
                         "a longer command is another one")
        self.assertEqual({700: None}, self.read({700: ""}), "no command read: nothing to match")

    def test_missing_or_unreadable_files_fail_closed(self) -> None:
        self.assertEqual({701: None}, self.read({701: self.CMD}), "the process is gone")
        (self.process(702, self.CMD.split(" "), {}) / "environ").unlink()
        environ = self.process(703, self.CMD.split(" "), {}) / "environ"
        environ.unlink()
        environ.mkdir()  # unreadable as a file whoever runs the test
        (self.process(704, self.CMD.split(" "), {"CLAUDE_PID": "4242"}) / "cmdline").unlink()
        self.assertEqual({702: None, 703: None, 704: None}, self.read({pid: self.CMD for pid in (702, 703, 704)}))

    def test_an_environment_without_the_names_reads_as_empty_not_unknown(self) -> None:
        self.process(705, self.CMD.split(" "), {"PATH": "/usr/bin", "XCLAUDE_PID": "1"})
        self.assertEqual({705: {}}, self.read({705: self.CMD}))
