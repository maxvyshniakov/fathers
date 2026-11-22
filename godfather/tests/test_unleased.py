"""MACHINE's top unleased processes (fathers#203): while the load outside leases is high, the chart's place lists the
busiest processes no running lease owns; at a low load, without the list (an older model, a failed `ps`, a fake that
gives none) or with every process leased, the chart stays."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

import helpers  # noqa: F401  # pyright: ignore[reportUnusedImport] - src/ on sys.path

from godfather import app, sources
from godfather.layout import Rect
from godfather.model import Dashboard
from godfather.tui import Canvas
from godfather.views import machine

NOW = datetime.datetime(2026, 11, 22, 12, 0, tzinfo=datetime.UTC)
TABLE = [(1, 0, 0.5, 20_000, "launchd"),
         (812, 1, 312.4, 2_516_582, "com.apple.Virtualization.VirtualMachine"),
         (9120, 1, 148.0, 1_153_434, "Google Chrome Helper (Renderer)"),
         (433, 1, 61.2, 655_360, "Code Helper (Plugin)"),
         (4100, 1, 400.0, 900_000, "bun"),  # a lease's holder
         (4101, 4100, 350.0, 500_000, "node"),  # in its process rows
         (4102, 4101, 90.0, 100_000, "esbuild"),  # its child, not in the rows Slotfather gave
         (5200, 1, 30.0, 40_000, "python3"),  # a waiting request: no lease
         (77, 1, 12.5, 92_160, "WindowServer")]
HELD: dict[str, Any] = {"kind": "held", "pid": 4100, "count": 2, "label": "build", "tree": [[4100, 400.0, 900_000, "bun"],
                                                                            [4101, 350.0, 500_000, "node"]]}
WAITING: dict[str, Any] = {"kind": "waiting", "pid": 5200, "count": 1, "label": "check", "tree": []}


def model(unleased: float = 11.8, **extra: Any) -> Dashboard:
    found: dict[str, Any] = dict(
        slots=8, cores=12, averages=(14.2, 13.0, 12.1), admission=2, advice={"unleased_load": unleased, "jobs": 1},
        memory_block=None, used=2, free=6, holders=[HELD], waiters=[WAITING], draining=0,
        memory={"level": 1, "used": 512, "total": 1024, "swap_total": 1024, "swap_free": 800, "low": False,
                "swap_low": False},
        history=[(NOW - datetime.timedelta(minutes=1), NOW, 2)])
    found.update(extra)
    return cast(Dashboard, found)


def drawn(found: Dashboard, width: int = 72, height: int = 7) -> str:
    canvas = Canvas(width, height)
    machine.draw_machine(canvas, Rect(0, 0, height, width), found)
    return "\n".join(canvas.lines(False))


class HighTests(unittest.TestCase):
    def test_half_the_cores_outside_leases_is_high(self) -> None:
        for unleased, high in ((0.0, False), (5.9, False), (6.0, True), (31.9, True)):
            with self.subTest(unleased=unleased):
                self.assertEqual(high, machine.unleased_high(model(unleased)))

    def test_a_model_without_a_number_or_cores_is_never_high(self) -> None:
        extras: list[dict[str, Any]] = [{"advice": {}}, {"advice": {"unleased_load": True}}, {"advice": None},
                                        {"cores": 0}]
        for extra in extras:
            with self.subTest(extra=extra):
                self.assertFalse(machine.unleased_high(model(**extra)))


class TopTests(unittest.TestCase):
    def test_a_running_lease_and_its_descendants_are_left_out(self) -> None:
        top = machine.unleased_top(TABLE, [HELD, WAITING], limit=10)
        pids = [row[0] for row in top]
        self.assertNotIn(4100, pids, "the holder")
        self.assertNotIn(4101, pids, "in its process rows")
        self.assertNotIn(4102, pids, "a descendant the rows did not name")
        self.assertIn(5200, pids, "a waiting request holds no lease")
        self.assertEqual([812, 9120, 433, 5200, 77, 1], pids, "the highest cpu first")
        self.assertEqual((812, 312.4, 2_516_582, "com.apple.Virtualization.VirtualMachine"), top[0])

    def test_the_list_keeps_the_busiest_and_breaks_ties_by_memory(self) -> None:
        table = [(10, 1, 5.0, 100, "a"), (11, 1, 5.0, 900, "b"), (12, 1, 50.0, 10, "c"), (13, 1, 1.0, 10, "d")]
        self.assertEqual([12, 11], [row[0] for row in machine.unleased_top(table, [], limit=2)])

    def test_a_holder_without_a_usable_pid_hides_nothing(self) -> None:
        holders: list[dict[str, Any]] = [{"kind": "held", "pid": 1, "tree": []},
                                         {"kind": "held", "pid": 4100},
                                         {"kind": "legacy", "pid": None, "tree": [[], [0, 0, 0, "x"]]}]
        cyclic = [*TABLE, (7001, 7002, 10.0, 100, "cyc-a"), (7002, 7001, 10.0, 100, "cyc-b")]
        pids = [row[0] for row in machine.unleased_top(cyclic, holders, limit=10)]
        self.assertNotIn(4100, pids, "a holder without a tree key still excludes itself")
        self.assertIn(7001, pids, "a ppid cycle does not loop")
        self.assertIn(7002, pids)


class DrawTests(unittest.TestCase):
    TOP = machine.unleased_top(TABLE, [HELD, WAITING])

    def test_high_unleased_load_lists_the_processes_in_the_charts_place(self) -> None:
        text = drawn(model(unleased_top=self.TOP))
        self.assertIn("top unleased", text)
        self.assertNotIn("slots held", text)
        lines = text.splitlines()
        row = next(line for line in lines if "com.apple.Virtual" in line)
        for value in ("812", "312%", "2.4G"):
            self.assertIn(value, row)
        self.assertIn("640M", next(line for line in lines if "Code Helper" in line))
        self.assertNotIn(" bun ", text)

    def test_a_stacked_machine_lists_them_under_the_gauges(self) -> None:
        lines = drawn(model(unleased_top=self.TOP), 50, 11).splitlines()
        header = next(index for index, line in enumerate(lines) if "top unleased" in line)
        self.assertGreater(header, next(index for index, line in enumerate(lines) if " swap " in line))
        self.assertIn("pid", lines[header])
        self.assertIn("812", lines[header + 1])

    def test_the_chart_stays_at_a_low_load_without_the_list_or_with_none(self) -> None:
        for name, found in (("low load", model(2.0, unleased_top=self.TOP)), ("no list", model()),
                            ("empty list", model(unleased_top=[]))):
            with self.subTest(name):
                text = drawn(found)
                self.assertIn("slots held", text)
                self.assertNotIn("top unleased", text)

    def test_a_low_load_screen_is_the_same_with_or_without_the_list(self) -> None:
        self.assertEqual(drawn(model(2.0)), drawn(model(2.0, unleased_top=self.TOP)))

    def test_a_narrow_list_closes_its_gaps_then_drops_the_pid(self) -> None:
        rows = machine.unleased_top(TABLE, [])
        wide = machine.unleased_lines(rows, 40)
        self.assertEqual(["  ", "  ", "  "], [text[:2] for text, _ in wide[1][1:]], "two-cell gaps")
        narrow = machine.unleased_lines(rows, 18)
        self.assertEqual(3, len(narrow[1]), "name, cpu and mem: the pid went")
        self.assertEqual("top unleased", narrow[0][0][0])
        for line in wide:
            self.assertLessEqual(sum(len(text) for text, _ in line), 40)
        for line in narrow:
            self.assertLessEqual(sum(len(text) for text, _ in line), 18)
        self.assertEqual("warn", wide[1][2][1], "a cpu over one core")
        self.assertEqual("text", wide[5][2][1], "90%: under one core")


class TableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="godfather-unleased-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_fixture_mode_reads_host_jsons_processes(self) -> None:
        (self.root / "host.json").write_text(json.dumps(
            {"host": "mbp", "processes": [[812, 1, 312.4, 2516582, "VM"], ["x"], [77, 1, "1.5", 900, "WindowServer"]]}))
        with mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(self.root)}):
            self.assertEqual([(812, 1, 312.4, 2516582, "VM"), (77, 1, 1.5, 900, "WindowServer")],
                             sources.process_table())

    def test_fixture_mode_without_processes_reads_none(self) -> None:
        with mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(self.root)}):
            self.assertIsNone(sources.process_table(), "no host.json")
            (self.root / "host.json").write_text(json.dumps({"host": "mbp"}))
            self.assertIsNone(sources.process_table(), "an older fixture")

    def test_ps_is_read_by_columns_with_the_commands_base_name(self) -> None:
        output = (f"    1     0   0.0  20000 /sbin/launchd\n"
                  f"  812     1 312,4 2516582 /System/Library/Frameworks/Virtualization.framework/VirtualMachine\n"
                  f" 9120     1 148.0 1153434 /Applications/Google Chrome.app/Contents/Google Chrome Helper (Renderer)\n"
                  f" 9200     2   5.0       0 kworker/u16:2\n"
                  f" 9201     1   nan    1000 bad-float\n"
                  f"  bad\n"
                  f"99999 {os.getpid()}   0.0   900 ps\n")
        done = subprocess.CompletedProcess([], 0, output, "")
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(sources.subprocess, "run",
                                                                             return_value=done) as run:
            os.environ.pop("GODFATHER_FIXTURES", None)
            found = sources.process_table()
        self.assertEqual(["ps", "-A", "-ww", "-o", "pid=,ppid=,pcpu=,rss=,comm="], run.call_args.args[0])
        self.assertEqual([(1, 0, 0.0, 20000, "launchd"), (812, 1, 312.4, 2516582, "VirtualMachine"),
                          (9120, 1, 148.0, 1153434, "Google Chrome Helper (Renderer)"),
                          (9200, 2, 5.0, 0, "kworker/u16:2")], found)

    def test_a_failed_ps_reads_none(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GODFATHER_FIXTURES", None)
            effects: list[Any] = [OSError("no ps"), subprocess.TimeoutExpired("ps", 2.0),
                                  [subprocess.CompletedProcess([], 1, "", "ps: bad")]]
            for effect in effects:
                with self.subTest(effect=effect), mock.patch.object(sources.subprocess, "run", side_effect=effect):
                    self.assertIsNone(sources.process_table())


class ScreenTests(unittest.TestCase):
    def screen(self) -> app.Screen:
        found = app.Screen()
        self.addCleanup(found.close)
        return found

    def test_ps_is_read_only_while_the_load_is_high_and_at_most_every_few_seconds(self) -> None:
        screen = self.screen()
        clock = [100.0]
        with mock.patch.object(sources, "process_table", return_value=list(TABLE)) as table, \
                mock.patch.object(app.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertIsNone(screen.unleased(model(2.0)))
            table.assert_not_called()
            top = screen.unleased(model())
            self.assertEqual([812, 9120, 433, 5200, 77], [row[0] for row in top or []])
            clock[0] += 1
            screen.unleased(model(holders=[]))
            self.assertEqual(1, table.call_count, "the table is read again only after UNLEASED_EVERY")
            self.assertIn(4100, [row[0] for row in screen.unleased(model(holders=[])) or []],
                          "the leases are left out each load, from the latest holders")
            clock[0] += app.UNLEASED_EVERY
            screen.unleased(model())
            self.assertEqual(2, table.call_count)
            self.assertIsNone(screen.unleased(model(2.0)), "a load that falls back forgets the table")
            screen.unleased(model())
            self.assertEqual(3, table.call_count)

    def test_a_failed_ps_leaves_the_chart(self) -> None:
        with mock.patch.object(sources, "process_table", return_value=None):
            self.assertIsNone(self.screen().unleased(model()))


if __name__ == "__main__":
    unittest.main()
