"""`slotfather lock PATH -- COMMAND` (fathers#159): a lease gives up its slots while its command waits on a lock,
takes them back before the command runs, shows the wait in status, and an older client reads such a lease."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any
import unittest
from unittest import mock

from test_entry_pins import Queue
from test_live_cutover import CutoverCase
from test_slotfather import TOOL, WORK, SlotCase

from slotfather import client as sf_client
from slotfather import config as sf_config
from slotfather import entry as sf_entry
from slotfather import idle as sf_idle
from slotfather import leases as sf_leases
from slotfather import lockwait as sf_lockwait
from slotfather import process as sf_process
from slotfather import queue as sf_queue
from slotfather import runner as sf_runner
from slotfather import schedule as sf_schedule
from slotfather import usage as sf_usage


class LockTests(SlotCase):
    def setUp(self) -> None:
        super().setUp()
        self.lock = self.tmp / "cache.lock"
        self.env.update(TOOL=TOOL, LOCK=str(self.lock), WORK=WORK)
        self.assertEqual(0, self.tool("slots", "2").returncode)

    def entries(self, *options: str) -> dict[str, dict[str, Any]]:
        found = json.loads(self.tool("status", "--json", *options).stdout)
        return {e["label"]: e for e in found["entries"]}

    def raw(self, label: str) -> dict[str, Any]:
        """The queue entry of `label` as written (its lock owner is in no output)."""
        for path in (self.state / "queue").glob("*.json"):
            entry = json.loads(path.read_text())
            if entry.get("label") == label:
                return entry
        raise AssertionError(f"no entry {label}")

    def queue_log(self) -> str:
        return (self.state / "queue.log").read_text()

    def locked(self, label: str, then: str = "") -> subprocess.Popen[str]:
        """`slotfather run -n 1 --label LABEL -- slotfather lock LOCK -- <work LABEL>`, then `<work THEN>` under
        the same lease without the lock."""
        script = '"$TOOL" lock "$LOCK" -- sh -c "$WORK" work "$1"' + ('; sh -c "$WORK" work "$2"' if then else "")
        return self.request(label, 1, "sh", "-c", script, "locked", label, then)

    def test_a_lease_waiting_on_a_lock_frees_its_slot_and_takes_it_back_before_it_runs(self) -> None:
        first = self.locked("a", then="a2")
        self.started("a")
        second = self.locked("b")
        self.wait_for(lambda: self.entries().get("b", {}).get("lock") == str(self.lock), "b to wait for the lock")
        waiting = self.entries()["b"]
        self.assertEqual(("waiting", "yielded"), (waiting["state"], waiting["phase"]))
        self.assertEqual(0, waiting["yields"], "a lock wait is no polite yield: YIELDS_MAX is not spent on it")
        self.assertIn("waiting: 1 slot(s) b ", self.status())
        self.assertIn(f"waiting-for-lock={self.lock}", self.status())
        self.assertIn("1 of 2 slots held", self.status())
        dashboard = json.loads(self.tool("status", "--json", "--full").stdout)["dashboard"]
        self.assertEqual([str(self.lock)], [w["lock"] for w in dashboard["waiters"] if w["label"] == "b"])
        self.assertNotIn("lock", self.entries()["a"], "status --json adds `lock` only where a lease waits on one")
        # Its slot is free for others meanwhile
        third = self.request("c", 1)
        self.started("c")
        # a releases the lock and runs on under its lease; b takes the lock but no slot is free: it waits for one
        self.release("a")
        self.started("a2")
        self.wait_for(lambda: "took lock" in (self.state / "queue.log").read_text(), "b to take the lock")
        self.assertNotIn("lock", self.entries()["b"])
        self.assertEqual("waiting", self.entries()["b"]["state"])
        self.assertNotIn("start b", self.events(), "b runs only once it holds its slot again")
        self.release("c")
        self.started("b")
        self.assertEqual("held", self.entries()["b"]["state"])
        log = (self.state / "queue.log").read_text()
        self.assertIn(f"while waiting for lock {self.lock}", log)
        events = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]
        yielded = [e for e in events if e["kind"] == "lease.yielded"]
        self.assertEqual([(str(self.lock), f"lock {self.lock}")],
                         [(e["details"]["lock"], e["details"]["to"]["label"]) for e in yielded])
        ended = [e["details"] for e in events if e["kind"] == "lease.lock_ended"]
        self.assertEqual([(str(self.lock), "took", True)], [(d["lock"], d["why"], d["yielded"]) for d in ended])
        for label in ("a2", "b"):
            self.release(label)
        for process in (first, second, third):
            self.finish(process)
        history = [json.loads(line) for line in (self.state / "history.jsonl").read_text().splitlines()]
        self.assertEqual("released", next(h for h in history if h["label"] == "b")["outcome"])

    def waiting_b(self, then: str = "b2") -> subprocess.Popen[str]:
        """a holds the lock (then runs a2); b waits for it, yielded: the queue entry names its waiter."""
        self.processes_a = self.locked("a", then="a2")
        self.started("a")
        second = self.locked("b", then=then)
        self.wait_for(lambda: self.entries().get("b", {}).get("state") == "waiting", "b to give up its slot")
        return second

    def test_a_waiter_killed_outright_leaves_no_mark_the_next_look_keeps(self) -> None:
        second = self.waiting_b()
        owner = self.raw("b")["lock_owner"]["pid"]
        os.kill(owner, signal.SIGKILL)
        self.assertEqual(0, self.tool("slots", "2").returncode)  # a look that grants, as any client's next one
        self.wait_for(lambda: self.entries()["b"]["state"] == "held", "b to be granted again")
        self.assertNotIn("lock", self.raw("b"))
        self.assertIn(f"the waiter for lock {self.lock} is gone (pid {owner})", self.queue_log())
        for label in ("a", "a2", "b2"):
            self.release(label)
        for process in (self.processes_a, second):
            process.communicate(timeout=20)

    def test_a_signal_ends_the_wait_only_once_the_lease_holds_its_slots_again(self) -> None:
        second = self.waiting_b()
        third = self.request("c", 1)
        self.started("c")  # b's slot is taken: after the signal b must wait for one before anything runs on
        os.kill(self.raw("b")["lock_owner"]["pid"], signal.SIGTERM)
        self.wait_for(lambda: f"stopped waiting for lock {self.lock}" in self.queue_log(), "the wait to end")
        self.assertNotIn("lock", self.raw("b"))
        self.assertEqual("waiting", self.entries()["b"]["state"])
        self.assertNotIn("start b2", self.events(), "nothing runs on under b before it holds its slot")
        self.release("c")
        self.started("b2")
        self.assertEqual("held", self.entries()["b"]["state"])
        for label in ("a", "a2", "b2"):
            self.release(label)
        for process in (self.processes_a, second, third):
            process.communicate(timeout=20)

    def test_a_lease_with_other_work_running_keeps_its_slots_and_says_what_it_waits_for(self) -> None:
        first = self.locked("a", then="a2")
        self.started("a")
        busy = ('sh -c \'while [ ! -e "$RELEASE/bg" ]; do sleep 0.05; done\' & '
                '"$TOOL" lock "$LOCK" -- sh -c "$WORK" work b; wait')
        second = self.request("b", 1, "sh", "-c", busy)
        self.wait_for(lambda: self.entries().get("b", {}).get("lock") == str(self.lock), "b to wait for the lock")
        self.assertEqual(("held", "held"), (self.entries()["b"]["state"], self.entries()["b"]["phase"]))
        self.assertIn("2 of 2 slots held", self.status())
        self.assertIn(f"waiting-for-lock={self.lock}", self.status())
        self.assertNotIn("while waiting for lock", self.queue_log(), "it yields nothing")
        self.release("a")
        self.started("b")
        self.assertNotIn("lock", self.raw("b"))
        for label in ("a2", "b", "bg"):
            self.release(label)
        for process in (first, second):
            self.finish(process)

    def test_a_free_lock_runs_at_once_and_outside_a_lease_it_is_a_plain_lock(self) -> None:
        result = subprocess.run([TOOL, "lock", str(self.lock), "--", "sh", "-c", "exit 3"], env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(3, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertFalse((self.state / "queue").exists() and any((self.state / "queue").iterdir()))
        for argv in (["lock"], ["lock", str(self.lock)], ["lock", str(self.lock), "true"], ["lock", str(self.lock), "--"]):
            with self.subTest(argv=argv):
                self.assertEqual(2, self.tool(*argv).returncode)


class OlderClientTests(CutoverCase):
    def test_an_older_client_reads_a_lease_waiting_on_a_lock_as_a_yielded_waiter(self) -> None:
        state = self.tmp / "state"
        (state / "queue").mkdir(parents=True)
        pid, start = self.sleeper()
        entry: dict[str, Any] = {"pid": pid, "start": start, "count": 1, "label": "b", "cmd": "make b",
                                 "since": "2026-11-11T00:00:00Z", "state": "held", "granted": "2026-11-11T00:01:00Z"}
        sf_entry.to_lock_waiting(entry, time.time(), "/tmp/cache.lock", (pid, start))
        (state / "queue" / f"0000000001-{pid}.json").write_text(json.dumps(entry))
        old = self.drive(self.old_root, state, "look")
        self.assertEqual([["b", "waiting", "normal", None, 1]], old["status"])
        self.assertTrue(any(line.startswith("waiting: 1 slot(s) b") for line in old["lines"]), old["lines"])
        new = self.drive(self.new_root, state, "look")
        self.assertEqual([], new["grants"], "this client never grants a lease while its command waits on the lock")


class RuleTests(unittest.TestCase):
    def test_plan_neither_grants_nor_heads_a_lease_waiting_on_a_lock(self) -> None:
        waiting: dict[str, Any] = {"pid": 1, "start": "Wed Nov 11 00:00:00 2026", "label": "b", "count": 1,
                                   "since": "2026-11-11T00:00:00Z", "state": "held", "granted": "2026-11-11T00:01:00Z"}
        sf_entry.to_lock_waiting(waiting, 1_800_000_000.0, "/tmp/x.lock", (1, "Wed Nov 11 00:00:00 2026"))
        found = sf_schedule.plan([waiting], [], 2, 1_800_000_000.0)
        self.assertEqual(([], None), (found.grants, found.head))
        sf_entry.lock_taken(waiting)
        self.assertEqual([waiting], sf_schedule.plan([waiting], [], 2, 1_800_000_000.0).grants)

    def test_only_what_runs_a_container_or_vm_counts_as_work(self) -> None:
        for command in ("docker run --rm alpine true", "/usr/local/bin/docker compose up", "podman exec -it x sh",
                        "docker-compose run web", "qemu-system-aarch64 -m 2048", "/opt/homebrew/bin/vfkit --cpus 2"):
            with self.subTest(command=command):
                self.assertTrue(sf_idle.container(command))
        for command in ("kubectl port-forward svc/x 8080", "docker ps", "limactl list", "colima status", "python3 x",
                        "dockerize -wait tcp://db", ""):
            with self.subTest(command=command):
                self.assertFalse(sf_idle.container(command))


class ResumeTests(unittest.TestCase):
    """Work that starts under a lease that gave up its slots for a lock takes them back (fathers#159 review): a
    nested run there must not wait for a grant the lock mark forbids."""

    def test_a_nested_run_clears_the_mark_before_it_waits_for_the_grant(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="slotfather-lock-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        queue = Queue(root)
        path = queue.entry(90001, "lane", 1, "held", since=-600)
        queue.table[90002] = (90001, "Wed Nov 12 11:00:00 2025")  # the waiter, alive
        seen: list[Any] = []
        with queue.running(), mock.patch.dict(os.environ, {"SLOTFATHER_LEASE": str(path)}), \
                contextlib.redirect_stderr(io.StringIO()):
            entry = json.loads(path.read_text())
            sf_entry.to_lock_waiting(entry, time.time(), "/tmp/x.lock", (90002, "Wed Nov 12 11:00:00 2025"))
            path.write_text(json.dumps(entry))
            def held_again(lease: Path) -> None:
                seen.append(json.loads(lease.read_text()))

            with mock.patch.object(sf_client, "held_again", held_again):
                lease = sf_client.inherited(dict(queue.table))
        self.assertIsNotNone(lease)
        self.assertEqual([None], [sf_entry.lock_of(e) for e in seen], "the mark is gone before it waits")
        self.assertIn("work resumed while waiting for lock /tmp/x.lock", (queue.directory / "queue.log").read_text())


class ContainerIdleTests(unittest.TestCase):
    """The holder's sampler (usage.Watch) never records a tree that runs a container as idle (fathers#159)."""

    def since(self, argv0: str) -> Any:
        root = Path(tempfile.mkdtemp(prefix="slotfather-idle-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        entry = root / "state" / "queue" / "0000000001-1.json"
        entry.parent.mkdir(parents=True)
        entry.write_text("{}")
        child = subprocess.Popen([argv0, "-c", "sleep 30; :", "run", "alpine"], executable="/bin/sh")
        self.addCleanup(lambda: (child.kill(), child.wait()))
        deadline = time.monotonic() + 30  # a functional bound: the child starts its sleep at once
        while not any(ppid == child.pid for ppid, _ in sf_process.processes().values()):
            self.assertLess(time.monotonic(), deadline, "the sleep to start")
        with mock.patch.dict(os.environ, {"SLOTFATHER_DIR": str(root / "state"), "SLOTFATHER_LEGACY_DIRS": ""}):
            watch = sf_usage.Watch(sf_usage.TreeUsage(child.pid, time.time() - 1), root / "state", entry=entry)
            for _ in range(2):
                watch.observe()
                watch.record_idle()
        return json.loads(sf_idle.path(entry).read_text())["since"]

    def test_a_tree_running_a_container_is_never_idle(self) -> None:
        self.assertIsNotNone(self.since("sh"), "the same tree as a plain shell is low")
        self.assertIsNone(self.since("docker"))


class LockwaitUnitTests(unittest.TestCase):
    def test_parse_and_stop(self) -> None:
        path, cmd = sf_lockwait.parse(["~/cache.lock", "--", "make", "test"])
        self.assertEqual((Path(os.path.expanduser("~/cache.lock")), ["make", "test"]), (path, cmd))
        for bad in ([], ["/tmp/x"], ["/tmp/x", "make"], ["/tmp/x", "--"], ["", "--", "true"]):
            with self.subTest(bad=bad), self.assertRaises(sf_config.SlotError):
                sf_lockwait.parse(bad)
        with self.assertRaises(sf_lockwait.Stopped) as ctx:
            sf_lockwait.stop(signal.SIGTERM, None)
        self.assertEqual(signal.SIGTERM, ctx.exception.number)

    def test_enclosing_and_resume(self) -> None:
        state_dir = Path("/tmp/sf-state")
        lease_path = state_dir / "queue" / "0000000001-50.json"
        me = os.getpid()
        start = "Wed Nov 11 00:00:00 2026"
        table = {me: (50, start), 50: (1, start)}
        base_entry: dict[str, Any] = {
            "pid": 50, "start": start, "count": 1, "label": "l", "cmd": "c",
            "since": "2026-11-11T00:00:00Z",
        }
        held_entry: dict[str, Any] = {**base_entry, "state": "held", "granted": "2026-11-11T00:00:00Z"}
        waiting_entry: dict[str, Any] = {**base_entry, "state": "waiting"}
        with mock.patch.object(sf_config, "state_dir", return_value=state_dir), \
                mock.patch.object(sf_process, "processes", return_value=table):
            with mock.patch.object(sf_queue, "inherited_lease", return_value=None):
                self.assertIsNone(sf_lockwait.enclosing())
            # Waiting non-session lease is not enclosing for a new wait.
            with mock.patch.object(sf_queue, "inherited_lease",
                                   return_value=(lease_path, dict(waiting_entry), 1)):
                self.assertIsNone(sf_lockwait.enclosing())
            # Idle held plain lease -> idle=True.
            with mock.patch.object(sf_queue, "inherited_lease",
                                   return_value=(lease_path, dict(held_entry), 1)), \
                    mock.patch.object(sf_process, "other_work", return_value=False):
                self.assertEqual((lease_path, (me, start), True), sf_lockwait.enclosing())
            # Session entry -> idle=False (verified in yield transaction).
            with mock.patch.object(sf_queue, "inherited_lease",
                                   return_value=(lease_path, {**waiting_entry, "session": {"name": "s"}}, 1)):
                self.assertEqual((lease_path, (me, start), False), sf_lockwait.enclosing())
            # resume() reclaims a lock-yielded lease before running COMMAND.
            yielded_entry = dict(held_entry)
            sf_entry.to_lock_waiting(yielded_entry, 1_800_000_000.0, "/tmp/a.lock", (50, start))
            with mock.patch.object(sf_queue, "inherited_lease", return_value=(lease_path, yielded_entry, 1)), \
                    mock.patch.object(sf_client, "resume_lock_yield") as resume_yield, \
                    mock.patch.object(sf_client, "held_again") as held_again:
                sf_lockwait.resume()
                resume_yield.assert_called_once_with(lease_path, yielded_entry)
                held_again.assert_called_once_with(lease_path)

    def test_wait_and_command_handle_open_errors_and_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(sf_config.SlotError, "cannot open lock"):
                sf_lockwait.command([tmp, "--", "true"])
            lock_path = Path(tmp) / "unit.lock"
            # Free lock runs resume() and execute().
            with mock.patch.object(sf_lockwait, "resume") as resume, \
                    mock.patch.object(sf_runner, "execute", return_value=(0, None)) as execute:
                self.assertEqual(0, sf_lockwait.command([str(lock_path), "--", "echo", "ok"]))
                resume.assert_called_once()
                execute.assert_called_once_with(["echo", "ok"], {})
            # Contended lock interrupted by signal exits 128 + signum and marks "stopped".
            lease_path = Path(tmp) / "queue" / "0000000001-1.json"
            owner = (os.getpid(), "Wed Nov 11 00:00:00 2026")
            handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
            self.addCleanup(os.close, handle)
            with mock.patch.object(sf_lockwait, "enclosing", return_value=(lease_path, owner, False)), \
                    mock.patch.object(sf_leases, "yield_for_lock", return_value="noted"), \
                    mock.patch.object(sf_lockwait.fcntl, "flock",
                                      side_effect=sf_lockwait.Stopped(signal.SIGTERM)), \
                    mock.patch.object(sf_leases, "lock_taken") as lock_taken, \
                    mock.patch.object(sf_client, "held_again") as held_again, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(sf_lockwait.Stopped):
                    sf_lockwait.wait(handle, lock_path)
            self.assertIn("other work runs under it", err.getvalue())
            lock_taken.assert_called_once_with(lease_path, "stopped", lock=str(lock_path), owner=owner)
            held_again.assert_called_once_with(lease_path)

            with mock.patch.object(sf_lockwait.fcntl, "flock", side_effect=BlockingIOError), \
                    mock.patch.object(sf_lockwait, "wait", side_effect=sf_lockwait.Stopped(signal.SIGINT)):
                with self.assertRaises(SystemExit) as exit_ctx:
                    sf_lockwait.command([str(lock_path), "--", "true"])
                self.assertEqual(128 + signal.SIGINT, exit_ctx.exception.code)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
