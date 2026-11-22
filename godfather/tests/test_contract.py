"""Godfather against the real Slotfather CLI in an isolated SLOTFATHER_DIR: its capabilities, snapshot and settings
decode as docs/contracts/slotfather.md says. It runs the sibling checkout's ../slotfather/slotfather in the fathers
repository, so both packages are tested at the same commit; SLOTFATHER_BIN names another executable. Outside the
repository it falls back to the installed `slotfather`, and is skipped where that lacks SF-1 / SF-2 (slotfather#26, #27)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from helpers import ROOT

import fake_slotfather
import fake_tool
from godfather import app, sources
from godfather.adapters import slotfather as adapter

SIBLING = ROOT.parent / "slotfather" / "slotfather"
EXECUTABLE = os.environ.get("SLOTFATHER_BIN") or (str(SIBLING) if SIBLING.is_file() else shutil.which("slotfather"))


def capabilities(environ: dict[str, str]) -> Any:
    if not EXECUTABLE:
        return None
    try:
        result = subprocess.run([EXECUTABLE, "capabilities", "--json"], capture_output=True, text=True, timeout=10,
                                env=environ)
        return json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="godfather-contract-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        environ = {key: value for key, value in os.environ.items()
                   if not key.startswith(("SLOTFATHER_", "GODFATHER_"))}
        environ.update(SLOTFATHER_DIR=str(self.tmp / "state"), SLOTFATHER_LEGACY_DIRS="", SLOTFATHER_CORES="12",
                       SLOTFATHER_MEMORY_GIB="64", XDG_CONFIG_HOME=str(self.tmp / "config"))
        if EXECUTABLE:
            bin_dir = self.tmp / "bin"
            bin_dir.mkdir()
            (bin_dir / "slotfather").symlink_to(EXECUTABLE)
            environ["PATH"] = f"{bin_dir}:{environ.get('PATH', '')}"
        found = capabilities(environ)
        found_dict = cast(dict[str, Any], found) if isinstance(found, dict) else None
        if found_dict is None or adapter.unsupported(found_dict):
            # The sibling package is this commit's Slotfather: a broken one fails here instead of skipping.
            self.assertNotEqual(EXECUTABLE, str(SIBLING), f"../slotfather/slotfather capabilities --json: {found!r}")
            self.skipTest("slotfather with capabilities --json and a dashboard block (SF-1) is not installed")
        self.capabilities: dict[str, Any] = found_dict
        patcher = mock.patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_snapshot_decodes_into_the_overview_model(self) -> None:
        with app.Screen() as screen:
            model, error = screen.load()
            slot_state = screen.clients["slotfather"].state()
            self.assertIsNone(error, slot_state.problem)
            assert model is not None
            self.assertEqual("ok", slot_state.state)
        self.assertEqual(4, model["slots"], "12 cores, 64 GiB: Slotfather's automatic capacity")
        for key in ("holders", "waiters", "events", "history", "memory", "advice", "overtake_window"):
            self.assertIn(key, model)
        self.assertIn("low", model["memory"])

    def test_settings_read_check_set_and_conflict(self) -> None:
        if not adapter.settings_supported(self.capabilities):
            self.assertNotEqual(EXECUTABLE, str(SIBLING), "../slotfather/slotfather lacks settings (SF-2)")
            self.skipTest("slotfather settings (SF-2) is not installed")
        client = adapter.SettingsClient(sources.which("slotfather"))
        reply = client.read()
        self.assertEqual("automatic", rows(reply)["capacity.mode"]["value"])
        self.assertEqual({"ok": False}, {"ok": client.check("capacity.slots", "0")["ok"]})
        self.assertEqual("Enter a whole number of slots, at least 1.", client.check("capacity.slots", "0")["error"])
        self.assertEqual(6, client.check("capacity.slots", "6").get("slots"))
        revision = rows(reply)["capacity.slots"]["revision"]
        client.set("capacity.slots", "6", revision)
        with self.assertRaises(adapter.Conflict):
            client.set("capacity.slots", "5", revision)
        self.assertEqual(("fixed", 6), (rows(client.read())["capacity.mode"]["value"], client.read()["basis"]["slots"]))
        client.reset(None)
        self.assertEqual("automatic", rows(client.read())["capacity.mode"]["value"])

    def test_settings_draw_the_live_reservation_timeout(self) -> None:
        if not adapter.settings_supported(self.capabilities):
            self.assertNotEqual(EXECUTABLE, str(SIBLING), "the sibling Slotfather must support settings")
            self.skipTest("installed Slotfather has no supported settings contract")

        from godfather import settings_model, tui
        from godfather.views import settings

        client = adapter.SettingsClient(sources.which("slotfather"))
        key = "order.reservation_timeout"
        if key not in rows(client.read()):
            self.assertNotEqual(EXECUTABLE, str(SIBLING), "the sibling Slotfather must publish the setting")
            self.skipTest("installed Slotfather has no reservation timeout setting")
        with app.Screen() as screen:
            context = settings_model.Context(width=90, height=45)
            for value, shown in (("12m", "12m"), ("0", "never")):
                with self.subTest(value=value):
                    client.set(key, value)
                    panel = settings_model.opened(screen.settings_sections(), context, 0.0)
                    canvas = tui.Canvas(90, 45)
                    settings.draw(canvas, panel, context)
                    self.assertRegex(canvas.render(False), rf"Reservation timeout +{shown}")

    def test_the_fake_settings_reply_is_the_real_one(self) -> None:
        """Exact parity applies to the sibling checkout. External clients use the v1 behavioral checks above."""
        if EXECUTABLE != str(SIBLING):
            self.skipTest("the fixture describes the sibling Slotfather settings")
        if not adapter.settings_supported(self.capabilities):
            self.skipTest("slotfather settings (SF-2) is not installed")
        real = adapter.SettingsClient(sources.which("slotfather")).read()
        fake = fake_slotfather.settings({"cores": 12, "memory_gib": 64, "capacity": None, "order": {},
                                         "environment": ["SLOTFATHER_CORES", "SLOTFATHER_MEMORY_GIB"],
                                         "writable": True})
        assert_same_shape(self, real, fake)


    def test_reservation_timeout_warning_from_real_provider_reaches_selected_row(self) -> None:
        from godfather import settings_model, tui
        from godfather.views import settings

        client = adapter.SettingsClient(sources.which("slotfather"))
        key = "order.reservation_timeout"
        if key not in rows(client.read()):
            self.assertNotEqual(EXECUTABLE, str(SIBLING), "the sibling must publish the setting")
            self.skipTest("the installed provider predates this setting")
        self.assertEqual(150, rows(client.set(key, "2m30s"))[key]["value"])
        fake, _ = fake_slotfather.validate({"cores": 12, "memory_gib": 64, "capacity": None}, key, "2m30s")
        self.assertEqual(150, fake)
        reply = client.set(key, "0")
        self.assertIn("Older Slotfather clients may use Arrival order", rows(reply)[key]["warning"])
        context = settings_model.Context(width=90, height=32)
        panel = settings_model.opened([settings_model.Section("slotfather", reply)], context, 0.0)
        panel, _, _ = settings_model.move_to(panel, context, ("slotfather", key), 0.0)
        canvas = tui.Canvas(90, 32)
        settings.draw(canvas, panel, context)
        lines = canvas.render(False).splitlines()
        selected = next(index for index, line in enumerate(lines) if "▶ Reservation timeout" in line)
        self.assertIn("Older Slotfather clients may use Arrival order", lines[selected + 1])


def rows(reply: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["key"]: row for section in reply["sections"] for row in section["settings"]}


SHOWN = ("key", "label", "type", "default", "choices", "min", "max", "step", "unit", "zero", "when", "writable",
         "min_exclusive", "description")


def assert_same_shape(case: unittest.TestCase, real: dict[str, Any], fake: dict[str, Any]) -> None:
    """The fields Godfather reads, equal in the fake and the real reply; values only by type."""
    case.assertEqual(sorted(set(real) - {"revision"}), sorted(set(fake) - {"revision"}), "top-level fields")
    case.assertEqual([section["id"] for section in real["sections"]], [section["id"] for section in fake["sections"]])
    case.assertEqual(list(rows(real)), list(rows(fake)), "the rows, in order")
    for key, row in rows(real).items():
        other = rows(fake)[key]
        with case.subTest(key=key):
            case.assertEqual(sorted(row), sorted(other), "the row's fields")
            for name in SHOWN:
                if name in row and not (name == "description" and key.startswith("env.")):
                    case.assertEqual(row[name], other[name], name)
            case.assertEqual(type(row["value"]), type(other["value"]), "value's type")
    case.assertEqual([item["label"] for item in real["status"]], [item["label"] for item in fake["status"]])


SIBLINGS = Path(__file__).resolve().parents[2]


class ToolContractTests(unittest.TestCase):
    """Lanefather's and Junkfather's real `settings` CLIs, from the sibling checkouts in isolated state: what
    Godfather's client reads and writes, and that tests/fake_tool.py prints the same rows. Skipped where a sibling
    is missing or cannot run (Lanefather needs Python 3.11)."""

    def tool(self, name: str) -> tuple[Path, dict[str, str]]:
        package = SIBLINGS / name
        if not (package / name / "settings.py").is_file():
            self.skipTest(f"no {name} checkout beside godfather")
        tmp = Path(tempfile.mkdtemp(prefix=f"godfather-{name}-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        python = shutil.which("python3") or sys.executable
        (bin_dir / name).write_text(f"#!/bin/sh\nPYTHONPATH={package} exec {python} -m {name} \"$@\"\n")
        (bin_dir / name).chmod(0o755)
        environ = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp / "home"),
                   "LANEFATHER_SETTINGS": str(tmp / "lanefather" / "settings.json"),
                   "JUNKFATHER_STATE": str(tmp / "junkfather"), "XDG_CONFIG_HOME": str(tmp / "config")}
        (tmp / "home").mkdir()
        probe = subprocess.run([str(bin_dir / name), "settings", "--json"], capture_output=True, text=True,
                               env=environ, cwd=tmp, timeout=30)
        if probe.returncode != 0:
            self.skipTest(f"{name} settings cannot run here: {probe.stderr.strip().splitlines()[-1:]}")
        patcher = mock.patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp)
        return bin_dir / name, environ

    def check_tool(self, name: str, key: str, value: str, read_only: tuple[str, str]) -> None:
        executable, _ = self.tool(name)
        client = adapter.SettingsClient(str(executable), name)
        reply = client.read()
        fake = fake_tool.document({"tool": name, "values": {}, "environment": {}, "writable": True})
        real = [row for row in rows(reply) if not row.startswith("project.")]  # the project the command runs in
        self.assertEqual(real, list(rows(fake)), "the fake's rows are the real ones, every one, in order")
        for row_key, row in rows(fake).items():
            self.assertEqual(sorted(rows(reply)[row_key]), sorted(row), f"{row_key}: the same fields")
            for field in ("label", "type", "default", "choices", "min", "max", "step", "unit", "writable"):
                if field in rows(reply)[row_key]:
                    self.assertEqual(rows(reply)[row_key][field], row.get(field), f"{row_key} {field}")
        self.assertTrue(client.check(key, value)["ok"])
        self.assertFalse(client.check(key, "nonsense")["ok"])
        revision = rows(reply)[key]["revision"]
        after = client.set(key, value, revision)
        self.assertEqual("saved", rows(after)[key]["source"])
        with self.assertRaises(adapter.Conflict):
            client.set(key, value, revision)
        client.reset(None)
        self.assertEqual("default", rows(client.read())[key]["source"])
        variable, changed = read_only
        environment = dict(os.environ, **{variable: "60"})
        refused = subprocess.run([str(executable), "settings", "set", changed, "120", "--json"], capture_output=True,
                                 text=True, env=environment, cwd=os.getcwd(), timeout=30)
        self.assertEqual(self.READ_ONLY[name], refused.returncode, f"{name}: a row its environment sets is read-only")
        with mock.patch.dict(os.environ, {variable: "60"}):
            with self.assertRaises(adapter.ReadOnly):
                client.set(changed, "120")

    READ_ONLY = {"lanefather": 4, "junkfather": 5}  # what each tool exits with for a read-only row (fathers#118)

    def test_lanefather(self) -> None:
        self.check_tool("lanefather", "rerun.wait", "600", ("LANEFATHER_RERUN_WAIT", "rerun.wait"))

    def test_junkfather(self) -> None:
        self.check_tool("junkfather", "reap.grace", "30", ("JUNKFATHER_STATE", "env.JUNKFATHER_STATE"))

    def test_junkfather_read_only_is_exit_5_and_the_client_says_so(self) -> None:
        executable, _ = self.tool("junkfather")
        client = adapter.SettingsClient(str(executable), "junkfather")
        with self.assertRaises(adapter.ReadOnly):
            client.set("env.JUNKFATHER_STATE", "/elsewhere")


if __name__ == "__main__":
    unittest.main()
