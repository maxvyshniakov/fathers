"""Godfather against the real Slotfather CLI in an isolated SLOTFATHER_DIR: its capabilities, snapshot and settings
decode as docs/contracts/slotfather.md says. It runs the sibling checkout's ../slotfather/slotfather in the fathers
repository, so both packages are tested at the same commit; SLOTFATHER_BIN names another executable. Outside the
repository it falls back to the installed `slotfather`, and is skipped where that lacks SF-1 / SF-2 (slotfather#26, #27)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401 - src/ on sys.path

from godfather import app, sources
from godfather.adapters import slotfather as adapter
from godfather.views import settings as settings_view

SIBLING = Path(__file__).resolve().parents[2] / "slotfather" / "slotfather"
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
        if not isinstance(found, dict) or adapter.unsupported(found):
            self.skipTest("slotfather with capabilities --json and a dashboard block (SF-1) is not installed")
        self.capabilities = found
        patcher = mock.patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_snapshot_decodes_into_the_overview_model(self) -> None:
        screen = app.Screen()
        model, error = screen.load()
        self.assertIsNone(error, screen.source.problem)
        assert model is not None
        self.assertEqual("ok", screen.source.state)
        self.assertEqual(4, model["slots"], "12 cores, 64 GiB: Slotfather's automatic capacity")
        for key in ("holders", "waiters", "events", "history", "memory", "advice", "overtake_window"):
            self.assertIn(key, model)
        self.assertIn("low", model["memory"])

    def test_settings_read_check_set_and_conflict(self) -> None:
        if not adapter.settings_supported(self.capabilities):
            self.skipTest("slotfather settings (SF-2) is not installed")
        client = adapter.SettingsClient(sources.which("slotfather"))
        dialog = settings_view.Settings(1.0, client=client)
        self.assertEqual("automatic", dialog.mode())
        self.assertEqual({"ok": False}, {"ok": client.check("capacity.slots", "0")["ok"]})
        self.assertEqual("Enter a whole number of slots, at least 1.", client.check("capacity.slots", "0")["error"])
        self.assertEqual(6, client.check("capacity.slots", "6").get("slots"))
        revision = dialog.capacity_bytes
        client.set("capacity.slots", "6", revision)
        with self.assertRaises(adapter.Conflict):
            client.set("capacity.slots", "5", revision)
        dialog.read()
        self.assertEqual(("fixed", 6), (dialog.mode(), dialog.basis["slots"]))
        client.reset(None)
        dialog.read()
        self.assertEqual("automatic", dialog.mode())


if __name__ == "__main__":
    unittest.main()
