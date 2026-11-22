"""Spawnfather's timing budgets (fathers#99): elapsed-time asserts live here, not in the default suite, because a
loaded machine makes them fail for no fault of the code. The default discovery (`unittest discover -s tests`) does not
enter this folder (it is not a package); run it before a release, on a quiet machine:

  python3 -m unittest discover -s tests/perf
"""
from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # support.py, beside this folder

from support import Case  # noqa: E402

from spawnfather import resources  # noqa: E402

BUDGET = 10.0  # seconds: either of these takes well under one when nothing else runs


def perf_budget(test: unittest.TestCase, started: float, budget: float, what: str) -> None:
    """The perf helper repo-kit's test-hygiene knows: fails `test` when `what` took longer than `budget` seconds
    since `started` (time.monotonic). Only this suite calls it."""
    took = time.monotonic() - started
    test.assertLess(took, budget, f"{what} took {took:.2f} s; its budget is {budget:.0f} s")


class TimingBudgets(Case):
    def test_detach_returns_within_its_budget(self):
        started = time.monotonic()
        entry = self.own_detached(self.env, "--ttl", "1h", argv=("sleep", "600"))
        perf_budget(self, started, BUDGET, "own --detach")
        self.assertEqual(0, self.cli("reap", "--id", entry["id"]).returncode)

    def test_a_hanging_stop_command_times_out_within_its_budget(self):
        original = resources.STOP_TIMEOUT
        resources.STOP_TIMEOUT = 0.5
        try:
            started = time.monotonic()
            outcome = resources.stop({"kind": "command", "id": "x", "stop": ["sleep", "30"]}, 0.5, lambda: True,
                                     pause=lambda _s: None)
        finally:
            resources.STOP_TIMEOUT = original
        perf_budget(self, started, BUDGET, "a stop of `sleep 30` with a 0.5 s timeout")
        self.assertEqual("failed", outcome["result"])


if __name__ == "__main__":
    unittest.main()
