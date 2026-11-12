"""Which processes a reap may signal, and how it signals them: identity first, a reused pid never."""
from __future__ import annotations

import os
import signal
import unittest

from support import Case, proc, table

from junkfather import procs, reap

A = "Thu Nov 12 10:00:00 2026"
B = "Thu Nov 12 11:30:00 2026"


def entry(pid: int = 100, start: str = A, cmd: str = "node server.js", group: bool = True, known=None):
    return {"id": "jtest", "state": "running", "pid": pid, "pgid": pid, "start": start, "seen": cmd, "group": group,
            "known": known or [], "argv": cmd.split(), "expires": 0, "keep": "owner", "owner": {}}


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
        sent = []

        def kill(pid, sig):
            sent.append((pid, sig))
            if sig == signal.SIGTERM and pid == 100:
                alive.pop(100)
            if sig == signal.SIGTERM and pid == 102:
                alive[102] = B  # it exited and its pid was taken by another process meanwhile
            if sig == signal.SIGKILL:
                alive.pop(pid, None)

        clock = iter(range(1000))
        result = reap.stop([proc(100), proc(101), proc(102)], grace=3, kill=kill,
                           look=lambda pids: {p: alive[p] for p in pids if p in alive},
                           now=lambda: next(clock), pause=lambda _s: None)
        self.assertEqual("killed", result["result"])
        self.assertIn((101, signal.SIGKILL), sent)
        self.assertNotIn((102, signal.SIGKILL), sent)

    def test_nothing_alive_is_gone(self):
        result = reap.stop([proc(100)], look=lambda pids: {}, kill=lambda *_: self.fail("signalled"))
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
