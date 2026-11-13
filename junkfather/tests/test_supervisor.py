"""The supervisor's loop, driven by an injected clock: a failing TTL reap backs off, a `gone` entry is revived."""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import unittest

from support import Case

from junkfather import launch
from junkfather.state import Store


class Quiet(launch.Catcher):
    def __init__(self) -> None:  # no signal.signal: the test process keeps its own handlers
        self.signum = None


class SupervisorLoopTests(Case):
    def supervised(self, ttl: float = 3600.0) -> Tuple[subprocess.Popen[Any], str]:
        child = self.spawn("sleep", "600", start_new_session=True)
        record = launch.new_entry(self.store, ["sleep", "600"], ttl, "owner", None, {"kind": "process"}, "attached",
                                  str(self.tmp))
        self.store.save(record)
        self.assertIsNotNone(launch.record_child(self.store, record["id"], child, os.getpid()))
        return child, record["id"]

    def test_a_failing_ttl_reap_backs_off_and_reports_once_per_streak(self):
        child, entry_id = self.supervised(ttl=0)
        clock = [100.0]
        attempts: List[Tuple[float, bool]] = []

        def reaper(store: Store, ident: str, reason: str, **options: Any) -> Dict[str, Any]:
            attempts.append((clock[0], options.get("report_failure", True)))
            if len(attempts) < 4:
                return {"id": ident, "result": "failed", "reason": reason, "pids": [], "how": "leader"}
            child.kill()
            child.wait()
            store.update(ident, lambda e: e.update(state="reaped"))
            return {"id": ident, "result": "killed", "reason": reason, "pids": [child.pid], "how": "leader"}

        supervisor = launch.Supervisor(self.store, entry_id, child, quiet=True, catcher=Quiet(), reaper=reaper,
                                       clock=lambda: clock[0], pause=lambda s: clock.__setitem__(0, clock[0] + s))
        supervisor.run()
        self.assertEqual(4, len(attempts), "one attempt per backoff step, not one per loop")
        gaps = [later[0] - earlier[0] for earlier, later in zip(attempts, attempts[1:])]
        self.assertEqual(gaps, sorted(gaps))
        self.assertLess(gaps[0], gaps[-1])
        for gap, delay in zip(gaps, [launch.RETRY_FIRST * 2 ** n for n in range(3)]):
            self.assertGreaterEqual(gap, delay)
        self.assertEqual([True, False, False, False], [report for _, report in attempts])
        self.assertLessEqual(launch.retry_delay(50), launch.RETRY_MAX)

    def test_a_gone_entry_whose_child_still_runs_is_revived(self):
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


if __name__ == "__main__":
    unittest.main()
