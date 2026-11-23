"""`reap --pid PID --started EPOCH --cmd CMD [--tree] --reason TEXT` (fathers#171): an unowned process stopped by
the identity its caller read (Slotfather's live sweep), with no sweep of Spawnfather's having listed it, under the
same policy as `reap --pid --force`: the start (within EPOCH_SLACK) and the command both checked."""
from __future__ import annotations

import json
from typing import Any, Dict, List

from support import FAKE_START, Case, proc, table

from spawnfather import procs, reap

STARTED: float = procs.started_at(FAKE_START) or 0.0  # the fake table's processes started then


class IdentityRuleTests(Case):
    """The rule on a fake table: nothing here signals a real process."""

    def stopped(self) -> List[List[int]]:
        calls: List[List[int]] = []

        def stopper(targets: List[procs.Proc], grace: float) -> Dict[str, Any]:
            calls.append([p.pid for p in targets])
            return {"result": "stopped", "signalled": [p.pid for p in targets], "left": []}
        self.stopper = stopper
        return calls

    def test_the_named_process_and_with_tree_its_descendants_go(self):
        here = table(proc(1, ppid=0), proc(500, cmd="node vitest"), proc(501, ppid=500, cmd="node worker"))
        calls = self.stopped()
        alone = reap.reap_identity(self.store, 500, STARTED, "node vitest", here, "slotfather sweep: orphan",
                                   stopper=self.stopper)
        tree = reap.reap_identity(self.store, 500, STARTED, "node vitest", here, "slotfather sweep: orphan",
                                  tree=True, stopper=self.stopper)
        self.assertEqual([[500], [500, 501]], calls)
        self.assertEqual(("stopped", "unowned, by identity"), (alone[0]["result"], alone[0]["how"]))
        self.assertEqual([500, 501], tree[0]["pids"])
        events = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]
        self.assertEqual(["unowned.reaped"] * 2, [e["kind"] for e in events])
        self.assertEqual((int(STARTED), "slotfather sweep: orphan"),
                         (events[0]["details"]["started"], events[0]["details"]["reason"]))

    def test_a_start_within_the_slack_is_the_same_process_and_one_beyond_it_is_gone(self):
        here = table(proc(1, ppid=0), proc(500, cmd="node vitest"))
        calls = self.stopped()
        near = reap.reap_identity(self.store, 500, STARTED + procs.EPOCH_SLACK, "node vitest", here, "sweep",
                                  stopper=self.stopper)
        far = reap.reap_identity(self.store, 500, STARTED + procs.EPOCH_SLACK + 1, "node vitest", here, "sweep",
                                 stopper=self.stopper)
        self.assertEqual(("stopped", "gone"), (near[0]["result"], far[0]["result"]))
        self.assertEqual("sweep; the pid now belongs to another process", far[0]["reason"])
        self.assertEqual([[500]], calls, "only the process with the start the caller saw is signalled")

    def test_a_pid_taken_again_in_the_same_second_is_told_apart_by_its_command(self):
        here = table(proc(1, ppid=0), proc(500, cmd="python3 server.py"))
        calls = self.stopped()
        found = reap.reap_identity(self.store, 500, STARTED, "node vitest", here, "sweep", stopper=self.stopper)
        self.assertEqual("gone", found[0]["result"])
        self.assertEqual([], calls)

    def test_a_pid_that_no_longer_runs_is_gone(self):
        found = reap.reap_identity(self.store, 500, STARTED, "node vitest", table(proc(1, ppid=0)), "sweep")
        self.assertEqual(("gone", "sweep; it no longer runs", []),
                         (found[0]["result"], found[0]["reason"], found[0]["pids"]))


class IdentityCliTests(Case):
    def identity(self, pid: int) -> List[str]:
        here = procs.Table.read().get(pid)
        assert here is not None and here.started is not None
        return ["--pid", str(pid), "--started", str(int(here.started)), "--cmd", here.cmd]

    def test_an_unlisted_unowned_process_is_stopped_by_its_identity(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        self.runs_600(child)
        self.assertFalse((self.state / "unowned.json").exists(), "no sweep listed it")
        dry = self.cli("reap", *self.identity(child.pid), "--reason", "slotfather sweep: orphan", "--dry-run",
                       "--json")
        self.assertEqual(0, dry.returncode, dry.stderr)
        self.assertEqual(["would-stop"], [r["result"] for r in json.loads(dry.stdout)["results"]])
        self.assertTrue(self.runs(child.pid))
        done = self.cli("reap", *self.identity(child.pid), "--reason", "slotfather sweep: orphan", "--grace", "2",
                        "--json")
        self.assertEqual(0, done.returncode, done.stderr)
        (row,) = json.loads(done.stdout)["results"]
        self.assertEqual(("stopped", [child.pid]), (row["result"], row["pids"]))
        self.assertIsNotNone(child.wait(10))

    def test_tree_stops_the_descendants_too(self):
        shell = self.spawn("sh", "-c", "sleep 600 & wait", start_new_session=True)
        below = self.runs_600(shell, below=True)
        done = self.cli("reap", *self.identity(shell.pid), "--tree", "--reason", "slotfather sweep: removed",
                        "--grace", "2", "--json")
        self.assertEqual(0, done.returncode, done.stderr)
        (row,) = json.loads(done.stdout)["results"]
        self.assertEqual({shell.pid, below}, set(row["pids"]))
        self.assertIsNotNone(shell.wait(10))
        self.wait_for(lambda: not self.runs(below), what="the shell's child to end")

    def test_an_owned_or_agent_process_is_refused(self):
        child = self.fake_proc(4200, cmd="sleep 600")
        self.assertEqual(0, self.cli("adopt", str(child.pid), "--ttl", "1h").returncode)
        owned = self.cli("reap", "--pid", "4200", "--started", str(int(STARTED)), "--cmd", "sleep 600",
                         "--reason", "sweep")
        self.assertEqual((3, "spawnfather: refusing pid 4200: an entry owns it now (reap that entry)\n"),
                         (owned.returncode, owned.stderr))
        self.fake_proc(4300, cmd="/usr/local/bin/claude --resume")
        agent = self.cli("reap", "--pid", "4300", "--started", str(int(STARTED)), "--cmd",
                         "/usr/local/bin/claude --resume", "--reason", "sweep")
        self.assertEqual((3, "spawnfather: refusing pid 4300: not a process spawnfather may stop\n"),
                         (agent.returncode, agent.stderr))
        self.assertTrue(self.runs(4200) and self.runs(4300))

    def test_a_gone_identity_exits_0(self):
        self.fake_proc(4200, cmd="sleep 600")
        moved = self.cli("reap", "--pid", "4200", "--started", str(int(STARTED) - 60), "--cmd", "sleep 600",
                         "--reason", "sweep", "--json")
        self.assertEqual(0, moved.returncode, moved.stderr)
        self.assertEqual(["gone"], [r["result"] for r in json.loads(moved.stdout)["results"]])

    def test_usage(self):
        for args in (("--pid", "1", "--started", "5", "--reason", "x"),
                     ("--pid", "1", "--cmd", "sleep", "--reason", "x"),
                     ("--started", "5", "--cmd", "sleep", "--reason", "x", "--id", "j1"),
                     ("--pid", "1", "--tree", "--reason", "x"),
                     ("--pid", "1", "--started", "5", "--cmd", "sleep"),
                     ("--pid", "1", "--started", "5", "--cmd", "sleep", "--reason", " "),
                     ("--pid", "1", "--started", "5", "--cmd", "sleep", "--reason", "x", "--force")):
            found = self.cli("reap", *args)
            self.assertEqual(2, found.returncode, (args, found.stderr))
