"""`launch.ChildDriver` / `Supervisor` tests: `gone` revival, foreground signal handling, and transient `ProcError`
recovery on attached leader exit. Shared TTL/backoff loop mechanics are tested in `test_supervise.py`."""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import unittest

from support import Case

from spawnfather import launch, procs
from spawnfather.state import Store


class Quiet(launch.Catcher):
    def __init__(self) -> None:  # no signal.signal: the test process keeps its own handlers
        self.signum = None


class SupervisorDriverTests(Case):
    def supervised(self, *argv: str, ttl: float = 3600.0) -> Tuple[subprocess.Popen[Any], str]:
        command = list(argv) if argv else ["sleep", "600"]
        child = self.spawn(*command, start_new_session=True)
        record = launch.new_entry(self.store, command, ttl, "owner", None, {"kind": "process"}, "attached",
                                  str(self.tmp))
        self.store.save(record)
        self.assertIsNotNone(launch.record_child(self.store, record["id"], child, os.getpid()))
        return child, record["id"]

    def test_a_gone_entry_whose_child_still_runs_is_revived(self) -> None:
        child, entry_id = self.supervised()
        self.store.update(entry_id, lambda e: e.update(state="gone", ended=1.0))
        seen: List[Optional[str]] = []

        def pause(_seconds: float) -> None:
            seen.append(self.state_of(entry_id))
            child.kill()
            child.wait()

        launch.Supervisor(self.store, entry_id, child, quiet=True, catcher=Quiet(), clock=lambda: 0.0,
                          pause=pause).run()
        self.assertEqual("running", seen[0])
        self.assertEqual("exited", self.state_of(entry_id))
        kinds = [json.loads(line)["kind"] for line in (self.state / "events.jsonl").read_text().splitlines()]
        self.assertEqual(["entry.owned", "entry.exited"], kinds)

    def test_sigterm_stops_through_reaper_and_exits_143(self) -> None:
        child, entry_id = self.supervised()
        catcher = Quiet()
        catcher.signum = signal.SIGTERM
        reap_calls: List[Tuple[str, str, Dict[str, Any]]] = []

        def fake_reaper(store: Store, ident: str, reason: str, **options: Any) -> Dict[str, Any]:
            reap_calls.append((ident, reason, options))
            child.kill()
            child.wait()
            store.update(ident, lambda e: e.update(state="reaped"))
            return {"id": ident, "result": "stopped", "reason": reason, "pids": [child.pid], "how": "leader"}

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = launch.Supervisor(self.store, entry_id, child, quiet=False, catcher=catcher,
                                     reaper=fake_reaper, clock=lambda: 0.0, pause=lambda _s: None).run()
        self.assertEqual(128 + signal.SIGTERM, code)
        self.assertEqual(143, code)
        self.assertEqual(1, len(reap_calls))
        ident, reason, options = reap_calls[0]
        self.assertEqual((entry_id, "spawnfather got SIGTERM"), (ident, reason))
        self.assertEqual(launch.SIGNAL_GRACE, options["grace"])
        self.assertIn(f"{entry_id} stopped on SIGTERM: stopped", err.getvalue())

    def test_transient_proc_error_on_attached_leader_exit_retries_cleanly_to_exited(self) -> None:
        child, entry_id = self.supervised()
        child.terminate()
        child.wait(5)
        clock = [0.0]
        pauses: List[float] = []
        reads = [0]

        def flaky_reader() -> procs.Table:
            reads[0] += 1
            if reads[0] == 1:
                raise procs.ProcError("ps failed")
            return procs.Table.read()

        def pause(seconds: float) -> None:
            pauses.append(seconds)
            clock[0] += seconds

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = launch.Supervisor(self.store, entry_id, child, quiet=False, attached=True, catcher=Quiet(),
                                     clock=lambda: clock[0], pause=pause, reader=flaky_reader).run()
        self.assertEqual(launch.status(child.returncode), code)
        self.assertEqual(2, reads[0])
        self.assertEqual([launch.POLL], pauses)
        self.assertEqual("exited", self.state_of(entry_id))
        self.assertNotIn("0 process(es) it left", err.getvalue())
        kinds = [json.loads(line)["kind"] for line in (self.state / "events.jsonl").read_text().splitlines()]
        self.assertEqual(["entry.owned", "entry.exited"], kinds)


if __name__ == "__main__":
    unittest.main()
