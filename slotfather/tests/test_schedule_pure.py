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


if __name__ == "__main__":
    unittest.main()
