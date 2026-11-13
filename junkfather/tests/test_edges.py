"""fathers#136: the edges the suite skipped. Real child processes, short grace periods, injected clocks."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Tuple
import unittest
from unittest import mock

from support import ROOT, Case, as_entry, proc, table

from junkfather import procs, reap, resources, sweep
from junkfather.entry import Entry

IGNORES_TERM = ("import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(600)")


def running(pid: int, start: str, cmd: str, entry_id: str = "jedge") -> Entry:
    return as_entry({"v": 1, "id": entry_id, "state": "running", "pid": pid, "pgid": pid, "start": start, "seen": cmd,
            "group": True, "known": [[pid, start]], "argv": cmd.split(), "expires": 0, "keep": "owner", "owner": {},
            "created": 1.0})


class EdgeTests(Case):
    def no_stop(self, targets: List[procs.Proc], grace: float) -> Dict[str, Any]:
        self.fail("the second reaper signalled")

    def quiet_sweep(self, **options: Any) -> Dict[str, Any]:
        """A sweep on this state directory that lists no resource kind (no docker, simctl or tailscale call)."""
        with mock.patch.object(resources, "unowned", return_value=([], [])):
            return sweep.sweep(self.store, cwds={}, sessions=[], **options)

    def test_a_process_that_ignores_sigterm_is_killed_after_the_grace(self):
        child = self.spawn(sys.executable, "-c", IGNORES_TERM, stdout=subprocess.PIPE, text=True,
                           start_new_session=True)
        assert child.stdout is not None
        self.assertEqual("ready", child.stdout.readline().strip())
        mine = procs.Table.read().get(child.pid)
        assert mine is not None
        self.store.save(running(child.pid, mine.start, mine.cmd))
        sent: List[Tuple[int, float]] = []

        def kill(pid: int, sig: int) -> None:
            sent.append((sig, time.monotonic()))
            os.kill(pid, sig)
        result = reap.reap(self.store, "jedge", "test", grace=0.3,
                           stopper=lambda targets, grace: reap.stop(targets, grace, kill=kill))
        self.assertEqual(("killed", [child.pid]), (result["result"], result["pids"]))
        self.assertEqual([signal.SIGTERM, signal.SIGKILL], [sig for sig, _ in sent])
        self.assertGreaterEqual(sent[1][1] - sent[0][1], 0.3, "SIGKILL only after the grace period")
        self.assertEqual(-signal.SIGKILL, child.wait(5))
        self.assertEqual("reaped", self.state_of("jedge"))
        event = json.loads((self.state / "events.jsonl").read_text().splitlines()[-1])
        self.assertEqual(["TERM", "KILL"], event["details"]["signals"])

    def test_an_entry_stuck_in_reaping_is_released_once_its_reaper_is_gone(self):
        me = os.getpid()
        now = 10_000.0
        here = table(proc(me, ppid=1, start="Thu Nov 12 09:00:00 2026", cmd="python3"))
        stuck = {"at": now - sweep.REAPING_STUCK - 1, "by": 4242, "by_start": "Thu Nov 12 08:00:00 2026"}
        for ident, reaping in (("jstuck", stuck), ("jheld", {**stuck, "by": me, "by_start": here.procs[me].start}),
                               ("jfresh", {**stuck, "at": now - 1})):
            record = running(4243, "Thu Nov 12 08:30:00 2026", "node server.js", ident)
            record.update(state="reaping", reaping=reaping)
            self.store.save(record)
        report = self.quiet_sweep(table=here, now=now)
        self.assertEqual("gone", self.state_of("jstuck"), "released to running, then found with nothing left")
        self.assertEqual(["jstuck"], [g["id"] for g in report["gone"]])
        self.assertEqual("reaping", self.state_of("jheld"), "its reaper still runs")
        self.assertEqual("reaping", self.state_of("jfresh"), "not stuck long enough")

    def test_corrupt_and_partial_entry_files_are_skipped(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        mine = procs.Table.read().get(child.pid)
        assert mine is not None
        self.store.save(running(child.pid, mine.start, mine.cmd, "jgood"))
        good = self.store.path("jgood").read_text()
        for name, text in (("jcut", good[: len(good) // 2]), ("jlist", "[1, 2]"), ("jnoid", '{"state": "running"}'),
                           ("jempty", "")):
            (self.store.entries_dir / f"{name}.json").write_text(text)
        self.assertEqual(["jgood"], [e["id"] for e in self.store.entries()])
        self.assertIsNone(self.store.load("jcut"))
        self.assertEqual("unknown", reap.reap(self.store, "jcut", "test")["result"])
        report = self.quiet_sweep(dry_run=True)
        self.assertEqual(([], 1), (report["errors"], report["running"]))
        listed = self.cli("list", "--json")
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertEqual(["jgood"], [e["id"] for e in json.loads(listed.stdout)["entries"]])
        self.assertTrue(self.runs(child.pid))

    def test_two_reaper_processes_at_once_signal_once(self):
        child = self.spawn(sys.executable, "-c", IGNORES_TERM, stdout=subprocess.PIPE, text=True,
                           start_new_session=True)
        assert child.stdout is not None
        self.assertEqual("ready", child.stdout.readline().strip())
        mine = procs.Table.read().get(child.pid)
        assert mine is not None
        self.store.save(running(child.pid, mine.start, mine.cmd))
        go = self.tmp / "go"
        # Each reaper writes its entry 0.2 s after reading it, so without the store's lock both would read
        # `running` and both would claim the entry.
        script = (f"import json, os, sys, time; sys.path.insert(0, {str(ROOT)!r})\n"
                  "from junkfather import reap, state\n"
                  "save = state.Store.save\n"
                  "state.Store.save = lambda self, e: (time.sleep(0.2), save(self, e))[1]\n"
                  f"while not os.path.exists({str(go)!r}): time.sleep(0.01)\n"
                  f"print(json.dumps(reap.reap(state.Store(), 'jedge', 'race', grace=0.3)))\n")
        reapers = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=self.env)
                   for _ in range(2)]
        go.touch()
        results = [json.loads(r.communicate(timeout=30)[0]) for r in reapers]
        self.assertEqual([[], [child.pid]], sorted(r["pids"] for r in results), results)
        self.assertEqual(["killed", "skipped"], sorted(r["result"] for r in results))
        self.assertEqual(-signal.SIGKILL, child.wait(5))

    def test_two_reapers_at_once_signal_once(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        mine = procs.Table.read().get(child.pid)
        assert mine is not None
        self.store.save(running(child.pid, mine.start, mine.cmd))

        def other_reaper_first() -> procs.Table:
            # Between this reaper's load (running) and its claim, another reaper's claim lands.
            self.store.update("jedge", lambda e: e.update(
                state="reaping", reaping={"reason": "other", "at": 1.0, "by": 4242, "by_start": "x"}))
            return procs.Table.read()

        result = reap.reap(self.store, "jedge", "test", grace=0.2, reader=other_reaper_first,
                           stopper=self.no_stop)
        self.assertEqual(("skipped", "another reaper has it"), (result["result"], result["reason"]))
        self.assertEqual(4242, self.loaded("jedge")["reaping"]["by"])
        self.assertTrue(self.runs(child.pid))


if __name__ == "__main__":
    unittest.main()
