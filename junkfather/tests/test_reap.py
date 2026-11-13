"""Which processes a reap may signal, and how it signals them: identity first, a reused pid never."""
from __future__ import annotations

import json
import os
import signal
from typing import Any, Dict, List, Optional, Tuple
import unittest

from support import Case, as_entry, proc, table

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
        import subprocess
        record = entry(pid=None)
        record["state"] = "reaped"
        self.store.save(record)
        child = subprocess.Popen(["sleep", "600"], start_new_session=True)
        self.children.append(child)
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
