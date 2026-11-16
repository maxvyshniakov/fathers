"""One home for the shared settings (fathers#34): each duration grammar accepts exactly what its surface accepted, the
validators accept and refuse what they did, configure() saves capacity.json, order.json and sweep.json with the bytes
older clients read, and the persistence layer imports no admission code."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from slotfather import config as sf_config
from slotfather import durations as sf_durations
from slotfather import settings as sf_settings

SOURCE = Path(__file__).resolve().parents[1] / "src"


class DurationTests(unittest.TestCase):
    def test_each_grammar_accepts_what_its_surface_accepts(self) -> None:
        clock = {"90": 90.0, "90s": 90.0, "30m": 1800.0, "1.5h": 5400.0, "1h30m": 5400.0, "1h 30m 5s": 5405.0, "1h30": 3630.0}
        span = {"90": 90.0, "90s": 90.0, "30m": 1800.0, "2h": 7200.0, "1d": 86400.0, "1.5h": 5400.0, " 7d ": 604800.0}
        since = {"90m": 1000.0 - 5400, "36h": 1000.0 - 129600, "7d": 1000.0 - 604800, "10s": 990.0}
        self.assertEqual(clock, {text: sf_durations.clock(text) for text in clock})
        self.assertEqual(span, {text: sf_durations.span(text) for text in span})
        self.assertEqual(since, {text: sf_durations.since(text, 1000.0) for text in since})
        self.assertEqual(1793664000.0, sf_durations.since("2026-11-03T00:00:00Z"))
        for text in ("", "-1", "1d", "m", "2 h x", "30m1h"):
            with self.subTest(clock=text), self.assertRaises(ValueError):
                sf_durations.clock(text)
        for text in ("", "-1", "1h30m", "m", "1w"):
            with self.subTest(span=text), self.assertRaises(ValueError):
                sf_durations.span(text)
        for text in ("90", "1h30m", "yesterday", "-3d"):
            with self.subTest(since=text), self.assertRaises(ValueError):
                sf_durations.since(text, 1000.0)


class ValidatorTests(unittest.TestCase):
    def test_order_settings_accepted_and_refused(self) -> None:
        self.assertEqual({"order": "window", "safety": 1, "warning_admits": 0},
                         sf_settings.checked_order({"order": "window", "safety": 1, "warning_admits": 0}))
        bads: list[Any] = [[], {"order": "fifo"}, {"nope": 1}, {"safety": 0.5}, {"safety": "3"}, {"limit": -1},
                {"warning_admits": 0.5}, {"safety": float("inf")}]
        for bad in bads:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sf_settings.checked_order(bad)

    def test_sweep_settings_accepted_and_refused(self) -> None:
        self.assertEqual({"classes": ["removed", "memory"], "grace": 0.0, "memory_top": 1.0},
                         sf_settings.checked_sweep({"classes": ["memory", "removed"], "grace": 0, "memory_top": 1}))
        bads: list[Any] = [[], {"nope": 1}, {"classes": "removed"}, {"classes": ["everything"]}, {"grace": -1},
                           {"memory_top": 0.5}, {"grace": "1"}]
        for bad in bads:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sf_settings.checked_sweep(bad)

    def test_a_sweep_file_that_cannot_be_read_gives_the_defaults_with_an_error(self) -> None:
        for text in ("{not json", "[]", '{"grace": -1}'):
            with self.subTest(text=text):
                found = sf_settings.sweep_from(text)
                self.assertIn("sweep.json ignored", found["error"])
                self.assertEqual(sf_settings.SWEEP_DEFAULTS["grace"], found["grace"])
        found = sf_settings.sweep_from('{"orphan_age": 60, "from_the_future": 1}')
        self.assertEqual((60.0, True), (found["orphan_age"], found["saved"]))
        self.assertEqual(["sweep.json: unknown setting(s) from_the_future ignored"], found["warnings"])


class SavedBytesTests(unittest.TestCase):
    """The bytes the files are saved with: those a client before this home wrote (taken from main @ aa8db1d)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="slotfather-settings-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"SLOTFATHER_DIR": str(self.dir), "SLOTFATHER_LEGACY_DIRS": "",
                                               "SLOTFATHER_CORES": "12", "SLOTFATHER_MEMORY_GIB": "32"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def saved(self, name: str) -> str:
        return (self.dir / name).read_text()

    def test_capacity_order_and_sweep_files(self) -> None:
        granted: list[Path] = []
        sf_settings.configure({"capacity.json": sf_settings.capacity_change(["6"])}, granted=granted.append)
        self.assertEqual('{"slots": 6}\n', self.saved("capacity.json"))
        self.assertEqual([self.dir], granted, "the caller's grant runs once the change is saved")
        sf_settings.configure({"capacity.json": sf_settings.capacity_change(["lower", "4"])})
        self.assertEqual('{"slots": 4, "mode": "lower", "gib_per_slot": 4}\n', self.saved("capacity.json"))
        sf_settings.configure({"order.json": {"safety": 4, "batch": 600}})
        # the keys before classes always; a class knob only when it is not its default, for older clients
        self.assertEqual('{"order": "backfill", "safety": 4.0, "fallback": 3600.0, "overrun": 0.5, "batch": 600.0}\n',
                         self.saved("order.json"))
        sf_settings.configure({"sweep.json": {"orphan_age": 10800.0, "classes": ["removed", "orphan"]}})
        self.assertEqual(
            '{"classes": ["removed", "orphan"], "removed_age": 600.0, "orphan_age": 10800.0, "dev_server_age": 28800.0, '
            '"session_removed_age": 21600.0, "grace": 10.0, "container_minutes": 30.0, "container_restarts": 3.0, '
            '"swap_free_mb": 1024.0, "swap_free_pct": 20.0, "memory_top": 8.0}\n', self.saved("sweep.json"))
        kinds = [json.loads(line)["kind"] for line in (self.dir / "events.jsonl").read_text().splitlines()]
        self.assertEqual(["capacity.changed", "capacity.changed", "order.changed"], kinds, "no event for sweep.json")
        (self.dir / "sweep.json").write_text("{broken")
        sf_settings.configure({"sweep.json": {"grace": 5.0}})
        self.assertEqual((5.0, 7200.0), (json.loads(self.saved("sweep.json"))["grace"],
                                         json.loads(self.saved("sweep.json"))["orphan_age"]),
                         "a broken file is replaced by the defaults and the change")
        sf_settings.configure({"order.json": None, "sweep.json": None, "capacity.json": None})
        self.assertEqual([], sorted(p.name for p in self.dir.iterdir() if p.suffix == ".json" and p.name != "schema.json"))

    def test_a_refused_change_saves_nothing(self) -> None:
        for change in ({"order.json": {"safety": 0.1}}, {"sweep.json": {"grace": -1}}):
            with self.subTest(change=change), self.assertRaises(sf_config.SlotError):
                sf_settings.configure(change)
        self.assertFalse((self.dir / "order.json").exists() or (self.dir / "sweep.json").exists())


def imports(path: Path) -> set[str]:
    """Every slotfather module a source file imports, at its top or inside a function."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            found.update([node.module] if node.module else [alias.name for alias in node.names])
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("slotfather"):
            found.update(alias.name for alias in node.names)
    return found


class ImportTests(unittest.TestCase):
    def test_persistence_imports_no_admission_and_settings_no_cli(self) -> None:
        package = SOURCE / "slotfather"
        self.assertEqual(set(), imports(package / "state.py") & {"leases", "schedule", "runtype", "events", "platform"})
        self.assertNotIn("platform", imports(package / "config.py"))
        for path in sorted(package.glob("settings*.py")):
            with self.subTest(path.name):
                self.assertNotIn("cli", imports(path))


if __name__ == "__main__":
    unittest.main()
