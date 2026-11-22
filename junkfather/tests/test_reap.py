"""Which processes a reap may signal, and how it signals them: identity first, a reused pid never."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
import unittest

from support import ROOT, Case, as_entry, proc, table

from junkfather import procs, reap
from junkfather.entry import Entry

A = "Thu Nov 12 10:00:00 2026"
B = "Thu Nov 12 11:30:00 2026"


def entry(pid: Optional[int] = 100, start: str = A, cmd: str = "node server.js", group: bool = True,
          known: Optional[List[List[Any]]] = None) -> Entry:
    return as_entry({"id": "jtest", "state": "running", "pid": pid, "pgid": pid, "start": start, "seen": cmd,
                     "group": group, "known": known or [], "argv": cmd.split(), "expires": 0, "keep": "owner",
                     "owner": {}})


def no_signal(pid: int, sig: int) -> None:
    raise AssertionError(f"signalled {pid} with {sig}")


class MembersTests(unittest.TestCase):
    def test_a_verified_leader_brings_its_group_and_descendants(self):
        found, how = reap.members(entry(), table(
            proc(100, cmd="node server.js"), proc(101, ppid=100, pgid=100), proc(102, ppid=1, pgid=100),
            proc(103, ppid=101, pgid=103, cmd="chrome --headless"), proc(200, cmd="unrelated")))
        self.assertEqual("leader", how)
        self.assertEqual([100, 101, 102, 103], [p.pid for p in found])

    def test_a_reused_pid_is_never_a_member(self):
        found, how = reap.members(entry(), table(proc(100, start=B, cmd="node server.js"), proc(101, ppid=100)))
        self.assertEqual("pid reused", how)
        self.assertEqual([], found)

    def test_a_changed_command_needs_force(self):
        reused = table(proc(100, cmd="/usr/bin/python3 other.py"))
        self.assertEqual(([], "command changed"), reap.members(entry(), reused))
        self.assertEqual([100], [p.pid for p in reap.members(entry(), reused, force=True)[0]])

    def test_a_renamed_process_of_the_same_program_still_matches(self):
        found, how = reap.members(entry(cmd="node server.js --port 1"), table(proc(100, cmd="node vite worker")))
        self.assertEqual(("leader", [100]), (how, [p.pid for p in found]))

    def test_without_its_leader_only_known_members_count_not_the_group_id(self):
        # pid 100 is gone; 102 was seen as a member, 104 joined a group that reused the id later.
        known = [[102, A]]
        found, how = reap.members(entry(known=known), table(
            proc(102, pgid=100), proc(105, ppid=102, pgid=100), proc(104, pgid=100, start=B)))
        self.assertEqual("known", how)
        self.assertEqual([102, 105], [p.pid for p in found])

    def test_a_known_member_whose_pid_was_reused_is_not_a_member(self):
        found, how = reap.members(entry(known=[[102, A]]), table(proc(102, pgid=100, start=B)))
        self.assertEqual(([], "gone"), (found, how))

    def test_other_users_and_junkfather_itself_are_never_members(self):
        me = os.getpid()
        found, _ = reap.members(entry(), table(proc(100, cmd="node server.js"), proc(101, ppid=100, uid=0),
                                                proc(me, ppid=1, pgid=100, cmd="python3")))
        self.assertEqual([100], [p.pid for p in found])

    def test_a_leader_that_does_not_lead_a_group_brings_only_its_tree(self):
        adopted = entry(pid=100, group=False)
        adopted["pgid"] = 50
        found, _ = reap.members(adopted, table(proc(50, cmd="zsh"), proc(100, ppid=50, pgid=50, cmd="node server.js"),
                                               proc(101, ppid=100, pgid=50), proc(102, ppid=50, pgid=50)))
        self.assertEqual([100, 101], [p.pid for p in found])


class StopTests(unittest.TestCase):
    def test_term_then_kill_only_to_unchanged_identities(self):
        alive = {100: A, 101: A, 102: A}
        sent: List[Tuple[int, int]] = []

        def kill(pid: int, sig: int) -> None:
            sent.append((pid, sig))
            if sig == signal.SIGTERM and pid == 100:
                alive.pop(100)
            if sig == signal.SIGTERM and pid == 102:
                alive[102] = B  # it exited and its pid was taken by another process meanwhile
            if sig == signal.SIGKILL:
                alive.pop(pid, None)

        clock = iter(range(1000))
        result = reap.stop([proc(100), proc(101), proc(102)], grace=3, kill=kill,
                           look=lambda pids: {p: (alive[p], "sleep 300") for p in pids if p in alive},
                           now=lambda: next(clock), pause=lambda _s: None)
        self.assertEqual("killed", result["result"])
        self.assertIn((101, signal.SIGKILL), sent)
        self.assertNotIn((102, signal.SIGKILL), sent)

    def test_nothing_alive_is_gone(self):
        result = reap.stop([proc(100)], 1.0, look=lambda pids: {}, kill=no_signal)
        self.assertEqual("gone", result["result"])


class ReapEntryTests(Case):
    def test_a_reused_pid_leaves_the_process_alone_and_the_entry_gone(self):
        child = self.spawn("sleep", "600")
        live = procs.Table.read()
        mine = live.get(child.pid)
        assert mine is not None
        record = entry(pid=child.pid, start=B, cmd=mine.cmd)
        self.store.save(record)
        result = reap.reap(self.store, "jtest", "test", grace=1)
        self.assertEqual(("gone", "pid reused"), (result["result"], result["how"]))
        self.assertTrue(self.runs(child.pid))
        self.assertEqual("reaped", self.state_of("jtest"))

    def test_a_dry_run_signals_nothing(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        mine = procs.Table.read().get(child.pid)
        assert mine is not None
        self.store.save(entry(pid=child.pid, start=mine.start, cmd=mine.cmd))
        result = reap.reap(self.store, "jtest", "test", dry_run=True)
        self.assertEqual(("would-stop", [child.pid]), (result["result"], result["pids"]))
        self.assertTrue(self.runs(child.pid))
        self.assertEqual("running", self.state_of("jtest"))


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(Case):
    """The independent review's findings, each pinned by a test."""

    def test_terminals_agent_sessions_and_what_runs_below_them_are_never_members(self):
        found, _ = reap.members(entry(), table(
            proc(100, cmd="node server.js"), proc(101, ppid=100, pgid=100, cmd="tmux new-session"),
            proc(102, ppid=101, pgid=102, tty="ttys004", cmd="-zsh"),
            proc(103, ppid=102, pgid=103, cmd="/Users/x/.local/share/claude/versions/2.1.322"),
            proc(104, ppid=100, pgid=100, cmd="/opt/bin/codex exec"), proc(105, ppid=104, pgid=100),
            proc(106, ppid=100, pgid=100, cmd="chrome --headless")))
        self.assertEqual([100, 101, 106], [p.pid for p in found])

    def test_an_unreadable_process_table_fails_the_stop_and_keeps_the_entry(self):
        def look(_pids: List[int]) -> Dict[int, Tuple[str, str]]:
            raise procs.ProcError("ps did not answer")
        result = reap.stop([proc(100)], 1.0, look=look, kill=no_signal)
        self.assertEqual("failed", result["result"])
        child = self.spawn("sleep", "600", start_new_session=True)
        mine = procs.Table.read().get(child.pid)
        assert mine is not None
        self.store.save(entry(pid=child.pid, start=mine.start, cmd=mine.cmd))
        outcome = reap.reap(self.store, "jtest", "test", stopper=lambda targets, grace: reap.stop(targets, grace,
                                                                                              look=look))
        self.assertEqual("failed", outcome["result"])
        self.assertEqual("running", self.state_of("jtest"))
        self.assertTrue(self.runs(child.pid))

    def test_a_starting_entry_is_skipped_not_marked_reaped(self):
        record = entry(pid=None)
        record["state"] = "starting"
        self.store.save(record)
        self.assertEqual("skipped", reap.reap(self.store, "jtest", "session-end")["result"])
        self.assertEqual("starting", self.state_of("jtest"))

    def test_a_child_whose_entry_stopped_starting_is_stopped_not_revived(self):
        from junkfather import launch
        record = entry(pid=None)
        record["state"] = "reaped"
        self.store.save(record)
        child = self.spawn("sleep", "600", start_new_session=True)
        self.assertIsNone(launch.record_child(self.store, "jtest", child, os.getpid()))
        self.assertIsNotNone(child.poll())
        self.assertEqual("reaped", self.state_of("jtest"))

    def test_identities_do_not_depend_on_the_callers_time_zone(self):
        child = self.spawn("sleep", "600")
        first = procs.identities([child.pid])
        os.environ["TZ"] = "Asia/Tokyo"
        self.addCleanup(os.environ.pop, "TZ", None)
        self.assertEqual(first, procs.identities([child.pid]))
        self.assertEqual(first[child.pid], (procs.Table.read().get(child.pid) or proc(0)).start)

    def test_unowned_reports_skip_live_sessions_and_trees_holding_terminals(self):
        from junkfather import sweep
        root = "/scratch/"
        t = table(proc(500, cmd="/Users/x/.local/share/claude/versions/2.1.322"),
                  proc(510, ppid=500, cmd="node mcp-server.js"),
                  proc(520, ppid=1, cmd="tmux new -d"), proc(521, ppid=520, pgid=521, tty="ttys009", cmd="-zsh"),
                  proc(530, ppid=1, cmd="firefox -marionette -profile /scratch/s/profile"))
        cwds = {510: "/scratch/s", 520: "/scratch/s"}
        items = sweep.unowned(t, cwds, set(), [root], sweep.agent_sessions(t))
        self.assertEqual([530], [i["pid"] for i in items])
        self.assertEqual("/scratch/", items[0]["root"])


class ChangedLeaderTests(Case):
    """fathers#134: a leader refused for its command is not found again through `known`, which always holds it."""

    def test_a_changed_leader_is_not_signalled_through_known_but_its_children_are(self):
        known = [[100, A], [101, A]]  # what record_child, adopt and refresh write: the leader comes first
        found, how = reap.members(entry(known=known), table(
            proc(100, cmd="/usr/bin/python3 other.py"), proc(101, ppid=100, pgid=100, cmd="node worker.js"),
            proc(102, ppid=101, pgid=100, cmd="esbuild --service"), proc(103, ppid=100, pgid=100)))
        self.assertEqual("known", how)
        self.assertEqual([101, 102], [p.pid for p in found], "the reused leader and its own new child stay")

    def test_a_reused_leader_is_not_signalled_through_known(self):
        found, how = reap.members(entry(known=[[100, A], [101, A]]), table(
            proc(100, start=B, cmd="node server.js"), proc(101, ppid=1, pgid=100)))
        self.assertEqual(("known", [101]), (how, [p.pid for p in found]))

    def test_force_takes_the_changed_leader_with_its_group(self):
        found, how = reap.members(entry(known=[[100, A]]), table(
            proc(100, cmd="/usr/bin/python3 other.py"), proc(103, ppid=100, pgid=100)), force=True)
        self.assertEqual(("leader", [100, 103]), (how, [p.pid for p in found]))

    def test_a_real_changed_leader_survives_a_reap_and_force_stops_it(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        mine = procs.Table.read().get(child.pid)
        assert mine is not None
        self.store.save(entry(pid=child.pid, start=mine.start, cmd="node server.js", known=[[child.pid, mine.start]]))
        result = reap.reap(self.store, "jtest", "test", grace=0.2)
        self.assertEqual(("gone", []), (result["result"], result["pids"]))
        self.assertTrue(self.runs(child.pid))
        self.store.update("jtest", lambda e: e.update(state="running", end=None))
        forced = reap.reap(self.store, "jtest", "test", grace=0.2, force=True)
        self.assertEqual(("stopped", [child.pid], "leader"), (forced["result"], forced["pids"], forced["how"]))
        child.wait(5)


class CommandFactorTests(Case):
    """fathers#135: a start time has one-second granularity, so the command is the second factor after it."""

    def test_a_known_member_whose_command_changed_is_not_a_member_unless_forced(self):
        record = entry(pid=100, known=[[102, A], [103, A]])
        record["known_seen"] = [[102, A, "node worker.js"], [103, A, "esbuild --service"]]
        reused = table(proc(102, pgid=100, cmd="node worker.js --inspect"), proc(103, pgid=100, cmd="/bin/zsh -l"))
        self.assertEqual(("known", [102]), (lambda f: (f[1], [p.pid for p in f[0]]))(reap.members(record, reused)))
        self.assertEqual([102, 103], [p.pid for p in reap.members(record, reused, force=True)[0]])

    def test_an_entry_without_known_seen_keeps_pid_and_start_only(self):
        found, how = reap.members(entry(known=[[102, A]]), table(proc(102, pgid=100, cmd="anything at all")))
        self.assertEqual(("known", [102]), (how, [p.pid for p in found]))

    def test_a_leader_matches_the_command_its_supervisor_last_recorded(self):
        record = entry(cmd="/usr/bin/python3 -m http.server", known=[[100, A]])
        record["known_seen"] = [[100, A, "/Library/Frameworks/Python.app/Contents/MacOS/Python -m http.server"]]
        found, how = reap.members(record, table(
            proc(100, cmd="/Library/Frameworks/Python.app/Contents/MacOS/Python -m http.server")))
        self.assertEqual(("leader", [100]), (how, [p.pid for p in found]))

    def test_stop_signals_no_pid_whose_command_changed_since_it_was_read(self):
        sent: List[Tuple[int, int]] = []
        result = reap.stop([proc(100, cmd="node server.js"), proc(101, cmd="node worker.js")], grace=0,
                           look=lambda pids: {100: (A, "/bin/zsh"), 101: (A, "node worker.js --x")},
                           kill=lambda pid, sig: sent.append((pid, sig)), now=iter(range(100)).__next__,
                           pause=lambda _s: None)
        self.assertEqual([(101, signal.SIGTERM), (101, signal.SIGKILL)], sent)
        self.assertEqual("failed", result["result"])

    def test_identities_with_their_commands(self):
        child = self.spawn("sleep", "600")
        here = procs.identities([child.pid], command=True)
        self.assertEqual((procs.identities([child.pid])[child.pid], "sleep 600"), here[child.pid])

    def test_the_supervisor_records_the_command_its_own_child_shows_now(self):
        from junkfather import launch
        child = self.spawn("sleep", "600", start_new_session=True)
        record = entry(pid=None)
        record["state"] = "starting"
        self.store.save(record)
        self.assertIsNotNone(launch.record_child(self.store, "jtest", child, os.getpid()))
        shim = "/usr/bin/python3 shim.py"  # what a launcher showed before it exec'd the real program
        self.store.update("jtest", lambda e: e.update(seen=shim, known_seen=[[child.pid, e["start"], shim]]))
        self.assertEqual([], reap.members(as_entry(self.loaded("jtest")), procs.Table.read())[0])
        from test_supervisor import Quiet
        launch.Supervisor(self.store, "jtest", child, quiet=True, catcher=Quiet()).refresh(None)
        now = self.loaded("jtest")
        self.assertEqual([[child.pid, now["start"], "sleep 600"]], now["known_seen"])
        self.assertEqual(([child.pid], "leader"),
                         (lambda f: ([p.pid for p in f[0]], f[1]))(reap.members(as_entry(now), procs.Table.read())))

    def test_reap_pid_force_refuses_a_listed_pid_whose_command_changed(self):
        import json
        child = self.spawn("sleep", "600", start_new_session=True)
        mine = procs.Table.read().get(child.pid)
        assert mine is not None
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "unowned.json").write_text(json.dumps(
            {"items": [{"pid": child.pid, "start": mine.start, "cmd": "node server.js"}]}))
        result = self.cli("reap", "--pid", str(child.pid), "--force", "--grace", "0.2")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("command changed", result.stderr)
        self.assertTrue(self.runs(child.pid))


class LifecycleHandoffRaceTests(Case):
    """fathers#189: a handoff that completes after selection and before begin_reap skips the lifecycle reap."""

    def make_process(self, entry_id: str = "jtest") -> Entry:
        record = entry(pid=100, known=[[100, A]])
        record.update({"id": entry_id, "owner": {"kind": "claude", "session": "s1", "agent": "a1"}, "keep": "owner"})
        self.store.save(record)
        return record

    def make_resource(self, entry_id: str = "jres") -> Entry:
        record: Entry = as_entry({"v": 1, "id": entry_id, "state": "running", "mode": "resource", "group": False,
                                  "keep": "owner", "owner": {"kind": "claude", "session": "s1", "agent": "a1"},
                                  "resource": {"kind": "command", "id": "x", "alive": ["true"], "stop": ["false"]},
                                  "argv": [], "cwd": "/", "created": 1.0, "ttl": 3600.0, "expires": 3601.0,
                                  "pid": None, "pgid": None, "start": None, "seen": None, "known": [], "handed": []})
        self.store.save(record)
        return record

    def test_a_handoff_before_the_mark_skips_process_reap_for_both_lifecycle_events(self):
        from junkfather import entry as entry_module
        for lifecycle, agent in ((reap.Lifecycle.SUBAGENT_STOP, "a1"), (reap.Lifecycle.SESSION_END, None)):
            with self.subTest(lifecycle=lifecycle.value):
                self.make_process()
                selected, _ = reap.select(self.store, [], "s1", agent, None, lifecycle=lifecycle)
                self.assertEqual(["jtest"], [e["id"] for e in selected])
                stopped: List[Any] = []

                def hand_before_mark() -> procs.Table:
                    self.assertIsNotNone(entry_module.handed(self.store, "jtest", {"kind": "claude", "session": "s2"}))
                    return table(proc(100, cmd="node server.js"))

                def stopper(targets: List[procs.Proc], _grace: float,
                            calls: List[Any] = stopped) -> Dict[str, Any]:
                    calls.append(targets)
                    return {"result": "stopped"}

                result = reap.reap(self.store, "jtest", lifecycle.value, reader=hand_before_mark,
                                   stopper=stopper, lifecycle=lifecycle, owner="s1", agent=agent)
                self.assertEqual(("skipped", "owner changed", []),
                                 (result["result"], result["reason"], stopped))
                loaded = self.loaded("jtest")
                self.assertEqual(("running", "s2"), (loaded["state"], loaded["owner"]["session"]))

    def test_a_handoff_before_the_mark_skips_resource_reap_for_both_lifecycle_events(self):
        from unittest import mock
        from junkfather import entry as entry_module, resources
        real = entry_module.begin_reap

        def hand_then_mark(store: Any, entry_id: str, reason: str, *args: Any, **kwargs: Any) -> Any:
            self.assertIsNotNone(entry_module.handed(store, entry_id, {"kind": "claude", "session": "s2"}))
            return real(store, entry_id, reason, *args, **kwargs)

        for lifecycle, agent in ((reap.Lifecycle.SUBAGENT_STOP, "a1"), (reap.Lifecycle.SESSION_END, None)):
            with self.subTest(lifecycle=lifecycle.value):
                self.make_resource()
                selected, _ = reap.select(self.store, [], "s1", agent, None, lifecycle=lifecycle)
                self.assertEqual(["jres"], [e["id"] for e in selected])
                with mock.patch.object(entry_module, "begin_reap", side_effect=hand_then_mark), \
                        mock.patch.object(resources, "stop", side_effect=AssertionError("resource stop ran")):
                    result = reap.reap(self.store, "jres", lifecycle.value, lifecycle=lifecycle,
                                       owner="s1", agent=agent)
                self.assertEqual(("skipped", "owner changed"), (result["result"], result["reason"]))
                loaded = self.loaded("jres")
                self.assertEqual(("running", "s2"), (loaded["state"], loaded["owner"]["session"]))

    def test_a_matching_owner_still_reaps_and_a_mark_before_handoff_refuses_the_handoff(self):
        from junkfather import entry as entry_module
        for lifecycle, agent in ((reap.Lifecycle.SUBAGENT_STOP, "a1"), (reap.Lifecycle.SESSION_END, None)):
            with self.subTest(lifecycle=lifecycle.value):
                self.make_process()
                handed_while_reaping: List[Any] = []

                def stopper(targets: List[procs.Proc], _grace: float,
                            calls: List[Any] = handed_while_reaping) -> Dict[str, Any]:
                    calls.append(entry_module.handed(self.store, "jtest", {"kind": "claude", "session": "s2"}))
                    return {"result": "stopped", "signalled": [p.pid for p in targets], "left": []}

                result = reap.reap(self.store, "jtest", lifecycle.value,
                                   table=table(proc(100, cmd="node server.js")), stopper=stopper,
                                   lifecycle=lifecycle, owner="s1", agent=agent)
                self.assertEqual(("stopped", [100], [None]),
                                 (result["result"], result["pids"], handed_while_reaping))
                self.assertEqual("reaped", self.state_of("jtest"))

    def test_a_matching_resource_owner_still_reaps_and_a_mark_before_handoff_refuses_the_handoff(self):
        from unittest import mock
        from junkfather import entry as entry_module, resources
        for lifecycle, agent in ((reap.Lifecycle.SUBAGENT_STOP, "a1"), (reap.Lifecycle.SESSION_END, None)):
            with self.subTest(lifecycle=lifecycle.value):
                self.make_resource()
                handed_while_reaping: List[Any] = []

                def stop(_resource: Any, _grace: float, _alive: Any,
                         calls: List[Any] = handed_while_reaping) -> Dict[str, Any]:
                    calls.append(entry_module.handed(self.store, "jres", {"kind": "claude", "session": "s2"}))
                    return {"result": "stopped", "steps": []}

                with mock.patch.object(resources, "stop", side_effect=stop):
                    result = reap.reap(self.store, "jres", lifecycle.value, lifecycle=lifecycle,
                                       owner="s1", agent=agent)
                self.assertEqual(("stopped", [None]), (result["result"], handed_while_reaping))
                loaded = self.loaded("jres")
                self.assertEqual(("reaped", "s1"), (loaded["state"], loaded["owner"]["session"]))

    def test_an_extra_guard_keeps_the_lifecycle_owner_check(self):
        from junkfather import entry as entry_module
        for lifecycle, agent in ((reap.Lifecycle.SUBAGENT_STOP, "a1"), (reap.Lifecycle.SESSION_END, None)):
            with self.subTest(lifecycle=lifecycle.value):
                self.make_process()  # expires 0: due, so `allowed` alone would still hold after the handoff
                stopped: List[Any] = []
                reads: List[int] = []

                def hand_before_mark(calls: List[int] = reads) -> procs.Table:
                    if not calls:  # the first read, between selection and the mark
                        self.assertIsNotNone(entry_module.handed(self.store, "jtest",
                                                                 {"kind": "claude", "session": "s2"}))
                    calls.append(1)
                    return table(proc(100, cmd="node server.js"))

                def stopper(targets: List[procs.Proc], _grace: float,
                            calls: List[Any] = stopped) -> Dict[str, Any]:
                    calls.append(targets)
                    return {"result": "stopped"}

                result = reap.reap(self.store, "jtest", lifecycle.value, reader=hand_before_mark, stopper=stopper,
                                   lifecycle=lifecycle, owner="s1", agent=agent, allowed=entry_module.due)
                self.assertEqual(("skipped", "owner changed", []), (result["result"], result["reason"], stopped))
                loaded = self.loaded("jtest")
                self.assertEqual(("running", "s2"), (loaded["state"], loaded["owner"]["session"]))

    def test_cli_lifecycle_reap_paused_at_barrier_skips_after_real_store_handoff(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        claude = self.fake_claude("s1")
        other = self.fake_claude("s2")
        env = self.claude_env("s1", claude)
        mine = procs.Table.read().get(child.pid)
        assert mine is not None
        record = entry(pid=child.pid, start=mine.start, cmd=mine.cmd, known=[[child.pid, mine.start]])
        record.update({"owner": {"kind": "claude", "session": "s1", "agent": "a1", "pid": claude.pid}, "keep": "owner"})
        self.store.save(record)
        selected = self.tmp / "selected"
        handed = self.tmp / "handed"
        script = (f"import os, sys, time; sys.path.insert(0, {str(ROOT)!r})\n"
                  "from junkfather import cli, reap\n"
                  "real = reap.select\n"
                  "def paused(*args, **kwargs):\n"
                  "    found = real(*args, **kwargs)\n"
                  f"    open({str(selected)!r}, 'w').close()\n"
                  "    deadline = time.monotonic() + 20\n"
                  f"    while not os.path.exists({str(handed)!r}) and time.monotonic() < deadline:\n"
                  "        time.sleep(0.01)\n"
                  "    return found\n"
                  "reap.select = paused\n"
                  "sys.exit(cli.main(['reap', '--owner', 's1', '--agent', 'a1', '--lifecycle', 'SubagentStop', "
                  "'--reason', 'subagent-stop', '--via', 'hook', '--json']))\n")
        reaper = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=env)
        self.children.append(reaper)
        self.wait_for(selected.exists, what="reap selection")
        hand = self.cli("hand", "jtest", "--to", "s2", env=env)
        self.assertEqual(0, hand.returncode, hand.stderr)
        handed.touch()
        out, err = reaper.communicate(timeout=30)
        self.assertEqual(0, reaper.returncode, err)
        self.assertEqual([{"how": "leader", "id": "jtest", "pids": [], "reason": "owner changed",
                           "result": "skipped"}], json.loads(out)["results"])
        self.assertTrue(self.runs(child.pid))
        self.assertEqual(("running", "s2", other.pid),
                         (self.state_of("jtest"), self.loaded("jtest")["owner"]["session"],
                          self.loaded("jtest")["owner"]["pid"]))


class SelectorGuardTests(unittest.TestCase):
    def make(self, *, session: str = "s1", agent: Optional[str] = None, lane: Optional[str] = None,
             keep: str = "owner", expires: float = 100.0, auto: Optional[str] = None) -> Entry:
        owner: Dict[str, Any] = {"kind": "claude", "session": session}
        if agent is not None:
            owner["agent"] = agent
        if lane is not None:
            owner["lane"] = lane
        record: Dict[str, Any] = {"id": "jguard", "state": "running", "owner": owner, "keep": keep,
                                  "expires": expires}
        if auto is not None:
            record["auto"] = auto
        return as_entry(record)

    def test_selector_guard_checks_owner_lane_expiry_and_extra_left_to_right(self):
        self.assertIsNone(reap.selector_guard())

        owner_only = reap.selector_guard(owner="s1")
        assert owner_only is not None
        self.assertIsNone(owner_only(self.make(session="s1")))
        self.assertEqual("owner changed", owner_only(self.make(session="s2")))

        subagent = reap.selector_guard(owner="s1", agent="a1", lifecycle=reap.Lifecycle.SUBAGENT_STOP)
        assert subagent is not None
        self.assertIsNone(subagent(self.make(session="s1", agent="a1")))
        self.assertEqual("owner changed", subagent(self.make(session="s1", agent="a1", keep="session")))
        self.assertEqual("owner changed", subagent(self.make(session="s1", agent="a1", auto="background")))

        lane_only = reap.selector_guard(lane="lane-a")
        assert lane_only is not None
        self.assertIsNone(lane_only(self.make(lane="lane-a")))
        self.assertEqual("owner changed", lane_only(self.make(lane="lane-b")))
        self.assertEqual("owner changed", lane_only(self.make(lane=None)))

        clock = [50.0]
        owner_and_due = reap.selector_guard(owner="s1", expired=True, now=lambda: clock[0],
                                            allowed=lambda _e: "custom refusal")
        assert owner_and_due is not None
        self.assertEqual("owner changed", owner_and_due(self.make(session="s2", expires=100.0)))
        self.assertEqual("not expired", owner_and_due(self.make(session="s1", expires=100.0)))
        clock[0] = 100.0
        self.assertEqual("custom refusal", owner_and_due(self.make(session="s1", expires=100.0)))

        lane_and_due = reap.selector_guard(lane="lane-a", expired=True, now=50.0)
        assert lane_and_due is not None
        self.assertEqual("owner changed", lane_and_due(self.make(lane="lane-b", expires=100.0)))
        self.assertEqual("not expired", lane_and_due(self.make(lane="lane-a", expires=100.0)))
        self.assertIsNone(lane_and_due(self.make(lane="lane-a", expires=50.0)))


class FailureStreakTests(Case):
    def test_a_retry_in_a_failure_streak_records_last_error_but_writes_no_event(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        mine = procs.Table.read().get(child.pid)
        assert mine is not None
        self.store.save(entry(pid=child.pid, start=mine.start, cmd=mine.cmd))
        def failing(targets: List[procs.Proc], grace: float) -> Dict[str, Any]:
            return {"result": "failed", "signalled": [], "left": [t.pid for t in targets], "error": "ps did not answer"}

        first = reap.reap(self.store, "jtest", "ttl expired", stopper=failing)
        again = reap.reap(self.store, "jtest", "ttl expired", stopper=failing, report_failure=False)
        self.assertEqual(("failed", "failed"), (first["result"], again["result"]))
        lines = (self.state / "events.jsonl").read_text().splitlines()
        self.assertEqual(1, len(lines), "only the first failure of the streak is an event")
        self.assertEqual("running", self.state_of("jtest"))
        self.assertIn("left", self.loaded("jtest")["last_error"])

    def test_resource_reap_honors_report_failure_parity(self):
        from unittest import mock
        from junkfather import resources
        self.store.save(as_entry({
            "v": 1, "id": "jres", "state": "running", "mode": "resource", "group": False,
            "keep": "owner", "owner": {"kind": "claude", "session": "s1"},
            "resource": {"kind": "command", "id": "x", "alive": ["true"], "stop": ["false"]},
            "argv": [], "cwd": "/", "created": 1.0, "ttl": 60.0, "expires": 50.0,
            "pid": None, "pgid": None, "start": None, "seen": None, "known": [], "handed": []}))
        failing: Dict[str, Any] = {"result": "failed", "steps": [], "error": "stop refused"}
        with mock.patch.object(resources, "stop", return_value=failing):
            first = reap.reap(self.store, "jres", "ttl expired", table=table(proc(os.getpid())))
            again = reap.reap(self.store, "jres", "ttl expired", table=table(proc(os.getpid())),
                              report_failure=False)
        self.assertEqual(("failed", "failed"), (first["result"], again["result"]))
        events = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]
        self.assertEqual(["resource.stop_failed"], [e["kind"] for e in events],
                         "report_failure=False suppresses resource.stop_failed just like entry.reap_failed")
        recorded = self.loaded("jres")
        self.assertEqual(("running", "stop refused"), (recorded["state"], recorded["last_error"]["error"]))

    def test_an_unreadable_first_table_records_last_error_and_starts_the_streak_with_an_event(self):
        self.store.save(entry(pid=4242, known=[[4242, A]]))

        def unreadable():
            raise procs.ProcError("ps did not answer")
        first = reap.reap(self.store, "jtest", "ttl expired", reader=unreadable)
        again = reap.reap(self.store, "jtest", "ttl expired", reader=unreadable, report_failure=False)
        self.assertEqual(("failed", "failed"), (first["result"], again["result"]))
        events = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]
        self.assertEqual(["entry.reap_failed"], [e["kind"] for e in events])
        self.assertEqual("ps did not answer", events[0]["details"]["error"])
        recorded = self.loaded("jtest")
        self.assertEqual(("running", "ps did not answer"), (recorded["state"], recorded["last_error"]["error"]))


class ForcedUnownedBypassTests(Case):
    """fathers#210: `reap --pid PID --force` goes straight to `reap_unowned` and never reaps a live owned entry."""

    def list_unowned(self, pid: int) -> None:
        mine = procs.Table.read().get(pid)
        assert mine is not None
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "unowned.json").write_text(json.dumps(
            {"v": 1, "items": [{"pid": pid, "start": mine.start, "cmd": mine.cmd}]}))

    def test_reap_pid_force_refuses_when_a_running_entry_adopted_the_listed_pid(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        self.runs_600(child)
        self.list_unowned(child.pid)
        self.assertEqual(0, self.cli("adopt", str(child.pid), "--ttl", "1h").returncode)
        (owned,) = self.store.entries()
        for extra in ((), ("--dry-run",)):
            forced = self.cli("reap", "--pid", str(child.pid), "--force", *extra)
            self.assertEqual(3, forced.returncode, forced.stderr)
            self.assertEqual("", forced.stdout)
            self.assertEqual(f"junkfather: refusing pid {child.pid}: an entry owns it now (reap that entry)\n",
                             forced.stderr)
            self.assertEqual("running", self.state_of(owned["id"]))
            self.assertTrue(self.runs(child.pid))
        by_entry = self.cli("reap", "--pid", str(child.pid), "--grace", "1")
        self.assertEqual(0, by_entry.returncode, by_entry.stderr)
        self.assertEqual("reaped", self.state_of(owned["id"]))
        self.assertIsNotNone(child.wait(10))

    def test_reap_pid_force_refuses_a_child_spawned_under_a_running_entry(self):
        owned = self.own_detached(self.env, "--ttl", "1h", argv=("sh", "-c", "sleep 600 & wait"))
        sub = self.runs_600(type("P", (), {"pid": owned["pid"]})(), below=True)  # type: ignore[arg-type]
        self.list_unowned(sub)
        forced = self.cli("reap", "--pid", str(sub), "--force")
        self.assertEqual(3, forced.returncode, forced.stderr)
        self.assertEqual(f"junkfather: refusing pid {sub}: an entry owns it now (reap that entry)\n", forced.stderr)
        self.assertEqual("running", self.state_of(owned["id"]))
        self.assertTrue(self.runs(sub) and self.runs(owned["pid"]))
        self.assertEqual(0, self.cli("reap", "--id", owned["id"], "--grace", "1").returncode)


class BeginReapSkipReasonTests(Case):
    """fathers#210, fathers#217: `reap.reap` reports the exact lock-time reason from `begin_reap` without a TOCTOU
    reload, uniformly for process and resource entries, and passes the already-read table into `begin_reap`."""

    def test_lock_time_skip_reasons_distinguish_exit_reaper_expiry_owner_and_guard_without_reload(self):
        from typing import Callable
        from unittest import mock
        from junkfather import entry as entry_module, resources
        from junkfather.state import Store

        cases: List[Tuple[str, Callable[[Store, str], Any], Optional[entry_module.Guard], Optional[reap.Lifecycle],
                          Optional[str], str]] = [
            ("already exited", lambda s, i: s.update(i, lambda e: e.update(state="exited", exit=0)),
             None, None, None, "reap"),
            ("already gone", lambda s, i: entry_module.gone(s, i),
             None, None, None, "reap"),
            ("another reaper has it", lambda s, i: s.update(i, lambda e: e.update(
                state="reaping", reaping={"reason": "other", "at": 1.0, "by": 4242, "by_start": A})),
             None, None, None, "reap"),
            ("not expired", lambda s, i: entry_module.extended(s, i, 3600.0, now=100.0),
             entry_module.require_due(100.0), None, None, "reap"),
            ("owner changed", lambda s, i: entry_module.handed(s, i, {"kind": "claude", "session": "s2"}),
             None, reap.Lifecycle.SESSION_END, "s1", "session-end"),
            ("owner changed", lambda s, i: entry_module.handed(s, i, {"kind": "claude", "session": "s2"}),
             None, None, "s1", "reap"),
            ("lane moved", lambda _s, _i: None, lambda _e: "lane moved", None, None, "reap"),
            ("guard refused", lambda _s, _i: None, lambda _e: False, None, None, "reap"),
        ]
        real_begin = entry_module.begin_reap
        real_load = self.store.load

        for mode in ("process", "resource"):
            for expected, change, guard, lifecycle, owner_id, reason in cases:
                with self.subTest(mode=mode, expected=expected, lifecycle=lifecycle, owner=owner_id):
                    entry_id = "jtest" if mode == "process" else "jres"
                    if mode == "process":
                        record = entry(pid=100, known=[[100, A]])
                        record["owner"] = {"kind": "claude", "session": "s1"}
                    else:
                        record = as_entry({
                            "v": 1, "id": entry_id, "state": "running", "mode": "resource", "group": False,
                            "keep": "owner", "owner": {"kind": "claude", "session": "s1"},
                            "resource": {"kind": "command", "id": "x", "alive": ["true"], "stop": ["false"]},
                            "argv": [], "cwd": "/", "created": 1.0, "ttl": 60.0, "expires": 50.0,
                            "pid": None, "pgid": None, "start": None, "seen": None, "known": [], "handed": []})
                    self.store.save(record)
                    here = table(proc(100, cmd="node server.js"), proc(os.getpid(), cmd="python3"))
                    after_begin = [False]
                    post_loads: List[str] = []
                    seen_tables: List[Optional[procs.Table]] = []

                    def load_spy(ident: str, flag: List[bool] = after_begin,
                                 seen: List[str] = post_loads) -> Optional[Entry]:
                        if flag[0]:
                            seen.append(ident)
                        return real_load(ident)

                    def mutate_then_begin(store: Any, ident: str, why_arg: str,
                                          passed_table: Optional[procs.Table] = None, *args: Any,
                                          mutate: Any = change, flag: List[bool] = after_begin,
                                          tables: List[Optional[procs.Table]] = seen_tables, **kwargs: Any) -> Any:
                        tables.append(passed_table)
                        mutate(store, ident)
                        outcome = real_begin(store, ident, why_arg, passed_table, *args, **kwargs)
                        # Mutate the stored entry again right after the lock releases: any post-lock reload would
                        # misclassify the reason instead of keeping what `begin_reap` saw under the lock.
                        real_load_entry = real_load(ident)
                        if real_load_entry is not None:
                            store.save(as_entry({**real_load_entry, "state": "reaped"}))
                        flag[0] = True
                        return outcome

                    with mock.patch.object(self.store, "load", side_effect=load_spy), \
                            mock.patch.object(entry_module, "begin_reap", side_effect=mutate_then_begin), \
                            mock.patch.object(procs.Table, "read", side_effect=AssertionError("unexpected ps read")), \
                            mock.patch.object(resources, "stop", side_effect=AssertionError("resource stop ran")):
                        result = reap.reap(self.store, entry_id, reason,
                                           reader=lambda t=here: t,
                                           stopper=lambda _t, _g: self.fail("stopped a skipped entry"),
                                           lifecycle=lifecycle, owner=owner_id, allowed=guard)
                    self.assertEqual(("skipped", expected, [], [here]),
                                     (result["result"], result["reason"], post_loads, seen_tables))


class ReapingSelectTests(Case):
    """fathers#231: a PID belonging to a `reaping` entry is reported as already reaping instead of unowned."""

    def test_select_pid_reports_reaping_entry_as_skipped(self):
        record = entry(pid=100, known=[[100, A]])
        record["state"] = "reaping"
        self.store.save(record)
        here = table(proc(100, cmd="node server.js"), proc(101, ppid=100, pgid=100, cmd="node worker.js"))
        for target_pid in (100, 101):
            with self.subTest(pid=target_pid):
                selected, skipped = reap.select(self.store, [], None, None, None, here, target_pid)
                self.assertEqual([], selected)
                self.assertEqual([{"id": "jtest", "result": "skipped", "reason": "already reaping",
                                   "pids": [], "how": ""}], skipped)

    def test_cli_reap_pid_on_reaping_entry_skips_instead_of_falling_through_to_reap_unowned(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        self.runs_600(child)
        self.assertEqual(0, self.cli("adopt", str(child.pid), "--ttl", "1h").returncode)
        (owned,) = self.store.entries()
        self.store.update(owned["id"], lambda e: e.update(
            state="reaping", reaping={"reason": "other", "at": 1.0, "by": os.getpid(), "by_start": A}))
        for extra in ((), ("--dry-run",), ("--expired",)):
            with self.subTest(extra=extra):
                result = self.cli("reap", "--pid", str(child.pid), "--json", *extra)
                self.assertEqual((0, ""), (result.returncode, result.stderr))
                self.assertEqual([{"id": owned["id"], "result": "skipped", "reason": "already reaping",
                                   "pids": [], "how": ""}], json.loads(result.stdout)["results"])
                self.assertEqual("reaping", self.state_of(owned["id"]))
                self.assertTrue(self.runs(child.pid))
