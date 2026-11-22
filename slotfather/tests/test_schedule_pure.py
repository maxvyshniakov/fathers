"""The scheduler is pure policy (fathers#35): it reads no file, environment or clock, and the one "slots free by
expected ends" walk answers as each of the three loops it replaced did."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from slotfather import config as sf_config  # noqa: E402
from slotfather import schedule as sf_schedule  # noqa: E402

M = 1_800_000_000.0


def stamp(offset: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(M + offset))


def waiter(label: str, count: int, queued: float) -> dict[str, Any]:
    return {"label": label, "count": count, "cmd": label, "since": stamp(queued), "state": "waiting"}


def holder(label: str, count: int, granted: float) -> dict[str, Any]:
    return {"label": label, "count": count, "cmd": label, "since": stamp(granted), "granted": stamp(granted),
            "state": "held"}


class PurityTests(unittest.TestCase):
    def test_the_module_imports_no_reader_of_files_environment_or_clock(self) -> None:
        tree = ast.parse((ROOT / "src/slotfather/schedule.py").read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported |= {alias.name for alias in node.names} if node.level else {node.module or ""}
        self.assertEqual({"__future__", "math", "time", "typing", "config", "entry"}, imported)
        calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        readers = {"time.time", "time.monotonic", "_config.now", "_config.overtake_window", "_config.setting",
                   "_config.state_dir", "open", "os.environ.get"}
        self.assertEqual(set(), calls & readers)

    def test_plan_on_fixtures_reads_no_state_directory_environment_or_clock(self) -> None:
        queue = [waiter("gate", 6, -700), waiter("short", 1, -10)]
        held = [holder("one", 1, -60)]

        def run(**settings: Any) -> tuple[list[str], str | None, float | None]:
            found = sf_schedule.plan([dict(e) for e in queue], held, 6, M, None, lambda e: 3600.0 if e["label"] == "one" else 1.0,
                                     **settings)
            return [e["label"] for e in found.grants], found.reason, found.reserved

        expected = run(settings=dict(sf_config.ORDER_DEFAULTS), window=600.0)
        missing = Path(tempfile.gettempdir()) / "slotfather-missing-state-dir" / "never"
        environment = {"SLOTFATHER_DIR": str(missing), "SLOTFATHER_QUEUE": "window",
                       "SLOTFATHER_OVERTAKE_WINDOW": "0", "SLOTFATHER_BACKFILL_SAFETY": "99"}
        with mock.patch.dict(os.environ, environment), \
                mock.patch("time.time", side_effect=AssertionError("the policy read the clock")):
            self.assertEqual(expected, run())
        self.assertFalse(missing.exists())
        self.assertEqual(["short"], expected[0], "the fixture passes a later request that fits")


def reserve_loop(ends: list[tuple[float, int]], have: int, need: int, moment: float) -> tuple[int, float]:
    """reserve()'s walk before fathers#35."""
    start = moment
    for end, count in sorted(ends):
        if have >= need:
            break
        have, start = have + count, end
    return have, start


def soonest_loop(ends: list[tuple[float, int]], have: int, count: int, moment: float) -> float | None:
    """soonest_start()'s walk before fathers#35."""
    start = moment
    for end, size in sorted(ends):
        if have >= count:
            break
        have, start = have + size, end
    return start if have >= count else None


def yield_loop(ends: list[tuple[float, int]], free: int, count: int, moment: float) -> tuple[int, float]:
    """yield_asks()'s walk before fathers#35."""
    have, when = free, moment
    for end, size in sorted(ends):
        if have >= count:
            break
        have, when = have + size, end
    return have, when


class FreeAtTests(unittest.TestCase):
    CASES: list[tuple[str, list[tuple[float, int]], int, int]] = [
        ("fits now", [(M + 100, 2)], 3, 2),
        ("one end", [(M + 100, 2), (M + 50, 1)], 1, 3),
        ("several ends", [(M + 300, 1), (M + 100, 2), (M + 200, 3)], 0, 6),
        ("a shrunk capacity drains first", [(M + 100, 2), (M + 200, 2)], -2, 1),
        ("earlier queue: never enough", [(M + 100, 1)], 0, 4),
        ("no ends", [], 0, 1),
        ("ties", [(M + 100, 1), (M + 100, 1)], 0, 2),
    ]

    def test_free_at_answers_as_each_former_loop(self) -> None:
        for name, ends, have, count in self.CASES:
            with self.subTest(name):
                found = sf_schedule.free_at(count, list(ends), have, M)
                self.assertEqual(reserve_loop(list(ends), have, count, M), found)
                self.assertEqual(yield_loop(list(ends), have, count, M), found)
                self.assertEqual(soonest_loop(list(ends), have, count, M), found[1] if found[0] >= count else None)


class AdmissionLazinessTests(unittest.TestCase):
    """fathers#214: Admission keeps host and estimator lookups strictly lazy, never mutates caller waiter dicts,
    and wires p90 consistently through room()."""

    def test_idle_single_waiter_fast_path_never_calls_estimators_or_host_resources(self) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("should not be called on idle single-waiter fast path")

        one = waiter("one", 1, -10)
        outcome = sf_schedule.Admission(
            [one],
            [],
            4,
            M,
            host=sf_schedule.Host(ram_mb=boom, averages=boom, cores=boom),
            estimate=boom,
            p90=boom,
            memory_estimate=boom,
        ).plan_grants()
        self.assertEqual(1, len(outcome.started))
        self.assertIs(one, outcome.started[0].entry)
        self.assertEqual((1, "normal", False), (outcome.started[0].count, outcome.started[0].served, outcome.started[0].pressed))
        self.assertEqual([], outcome.gated)
        self.assertIsNone(outcome.reason)

    def test_no_grants_never_calls_host_memory(self) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("should not be called when there are no grants")

        host = sf_schedule.Host(memory=boom, ram_mb=boom, averages=boom, cores=boom)
        empty = sf_schedule.Admission([], [], 4, M, host=host, estimate=boom, p90=boom, memory_estimate=boom).plan_grants()
        self.assertEqual(([], [], None), (empty.started, empty.gated, empty.reason))

        full = sf_schedule.Admission(
            [waiter("blocked", 1, -10)],
            [holder("h", 4, -60)],
            4,
            M,
            host=host,
            estimate=boom,
            p90=boom,
            memory_estimate=boom,
        ).plan_grants()
        self.assertEqual(([], []), (full.started, full.gated))

    def test_host_memoizes_lazy_callables(self) -> None:
        counts = {"memory": 0, "ram_mb": 0, "averages": 0, "cores": 0}

        def read_memory() -> tuple[str, str | None]:
            counts["memory"] += 1
            return ("normal", None)

        def read_ram() -> float | None:
            counts["ram_mb"] += 1
            return 16384.0

        def read_avg() -> tuple[float, float, float] | None:
            counts["averages"] += 1
            return (1.0, 1.0, 1.0)

        def read_cores() -> int:
            counts["cores"] += 1
            return 8

        host = sf_schedule.Host(memory=read_memory, ram_mb=read_ram, averages=read_avg, cores=read_cores)
        self.assertEqual(("normal", None), host.memory)
        self.assertEqual(("normal", None), host.memory)
        self.assertEqual(16384.0, host.ram_mb)
        self.assertEqual(16384.0, host.ram_mb)
        self.assertEqual((1.0, 1.0, 1.0), host.averages)
        self.assertEqual((1.0, 1.0, 1.0), host.averages)
        self.assertEqual(8, host.cores)
        self.assertEqual(8, host.cores)
        self.assertEqual({"memory": 1, "ram_mb": 1, "averages": 1, "cores": 1}, counts)

    def test_admission_copies_waiters_and_preserves_caller_identity(self) -> None:
        big: dict[str, Any] = {**waiter("big", 8, -10), "all": True}
        adm = sf_schedule.Admission([big], [], 4, M)
        outcome = adm.plan_grants()
        self.assertEqual(8, big["count"], "caller waiter dict is never mutated")
        self.assertIs(big, outcome.started[0].entry)
        self.assertEqual(4, outcome.started[0].count)
        self.assertIn(id(big), adm.forecast())

    def test_room_wires_p90_for_blocked_head(self) -> None:
        # Unknown-duration holder has run 1000s (fallback 3600s -> projected end M + 2600s).
        # Head waits past its reserved start (M - 600). Probe has est 1000s (safety 3x = 3000s > 2600s),
        # so it can only pass via the blocked-head p90 rule when p90 <= 2600s.
        head: dict[str, Any] = {**waiter("gate", 4, -1200), "reserved": M - 600}
        held = [holder("unk", 1, -1000)]

        def est(entry: sf_schedule.Entry) -> float | None:
            return 1000.0 if entry.get("label") == "probe" else None

        short_p90 = sf_schedule.Admission([head], held, 4, M, estimate=est, p90=lambda _e: 1200.0)
        long_p90 = sf_schedule.Admission([head], held, 4, M, estimate=est, p90=lambda _e: 3000.0)
        self.assertEqual(3, short_p90.room(label="probe", command="probe"))
        self.assertEqual(0, long_p90.room(label="probe", command="probe"))


if __name__ == "__main__":
    unittest.main()
