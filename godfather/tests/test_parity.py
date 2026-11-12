"""Godfather draws what Slotfather's in-tree dashboard draws, from the same model, but the footer.

Slotfather renders its own demo scene (tests/parity_reference.py, in a subprocess with the checkout on sys.path);
the same model goes through `status --json --full` (tools/slotfather_reference.py, the SF-1 stand-in) into fixture
mode, Godfather's adapter decodes it, and Godfather renders it at the same moment. Every row but the footer must be
identical, in colour and without, emoji and ASCII, wide, one-column and narrow. Skipped without a Slotfather
checkout (SLOTFATHER_SRC, default ~/slotfather/src).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from helpers import ROOT, SLOTFATHER_SRC, frozen_at, slotfather_checkout, timezone

from godfather import app, sources, tui
from godfather.adapters import slotfather as adapter
from godfather.views import overview


@unittest.skipUnless(slotfather_checkout(), "needs a Slotfather checkout at SLOTFATHER_SRC")
class ParityTests(unittest.TestCase):
    out: Path
    frames: dict[str, str]
    scene: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        environ = {key: value for key, value in os.environ.items()
                   if not key.startswith(("SLOTFATHER_", "GODFATHER_", "COLORFGBG", "NO_COLOR"))}
        environ["SLOTFATHER_SRC"] = str(SLOTFATHER_SRC)
        subprocess.run([sys.executable, str(ROOT / "tests" / "parity_reference.py"), str(cls.out)], check=True,
                       env=environ, cwd=cls.tmp.name, capture_output=True, text=True)
        cls.frames = json.loads((cls.out / "frames.json").read_text())
        cls.scene = json.loads((cls.out / "scene.json").read_text())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def setUp(self) -> None:
        keep = {key: value for key, value in os.environ.items()
                if not key.startswith(("SLOTFATHER_", "GODFATHER_", "COLORFGBG", "NO_COLOR"))}
        patcher = mock.patch.dict(os.environ, dict(keep, GODFATHER_FIXTURES=str(self.out)), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        tui.set_theme(app._theme.from_environment(environ={}))
        history = mock.patch.object(overview, "HISTORY_WINDOW", self.scene["history_window"])
        history.start()
        self.addCleanup(history.stop)
        self.addCleanup(overview.use_marks, False)

    def model(self) -> dict[str, Any]:
        model, error = app.Screen().load()
        self.assertIsNone(error)
        assert model is not None
        overview.dashboard_colours(model, {})
        for item in model["holders"] + model["waiters"]:
            item["lane"] = self.scene["lanes"][json.dumps(list(item["key"]))]
            if item["lane"] is None:
                del item["lane"]
        return model

    def render(self, width: int, height: int, colour: bool, ascii_only: bool, selected: int,
               help_open: bool = False) -> str:
        model = self.model()
        overview.use_marks(ascii_only)
        with timezone("Etc/GMT-2"), frozen_at(adapter.parse_time(self.scene["moment"])):  # type: ignore[arg-type]
            canvas = tui.Canvas(width, height)
            overview.dashboard_draw(canvas, model, selected, help_open, cwd="~/straylight")
            return canvas.render(colour, ascii_only)

    def test_fixture_mode_reads_only_the_recorded_replies(self) -> None:
        self.assertEqual(str(self.out / "slotfather"), sources.which("slotfather"))
        self.assertIsNone(sources.which("junkfather"))

    def test_every_row_but_the_footer_is_slotfathers(self) -> None:
        compared = 0
        for name, expected in self.frames.items():
            parts = name.split()
            size, colour, ascii_only, selected = parts[0], parts[1] == "1", parts[2] == "1", int(parts[3])
            width, height = (int(value) for value in size.split("x"))
            with self.subTest(name):
                got = self.render(width, height, colour, ascii_only, selected, len(parts) > 4)
                old, new = expected.split("\n"), got.split("\n")
                self.assertEqual(len(old), len(new))
                self.assertEqual(old[:-1], new[:-1], f"{name}: rows above the footer differ")
                if not colour:
                    self.assertTrue(old[-1].lstrip().startswith("Slotfather"), old[-1])
                    self.assertTrue(new[-1].lstrip().startswith("Godfather"), new[-1])
                compared += 1
        self.assertGreaterEqual(compared, 50)

    def test_the_footer_differs_only_in_the_name_and_version(self) -> None:
        old = self.frames["156x43 0 0 0"].split("\n")[-1]
        new = self.render(156, 43, False, False, 0).split("\n")[-1]
        hints = "↑↓ select   tab panel   s settings   ? help   q quit"
        self.assertIn(hints, old)
        self.assertIn(hints, new)
        self.assertTrue(new.startswith(" Godfather 0.1.0"), new)
        self.assertEqual(old[old.index("updated"):], new[new.index("updated"):])


if __name__ == "__main__":
    unittest.main()
