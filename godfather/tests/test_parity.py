"""Godfather draws what Slotfather's in-tree dashboard drew, from the same model, but the footer and EVENTS.

Slotfather's dashboard is gone upstream (slotfather#28, 81abd86); its last commit with one, 37c4df7, rendered its own
demo scene once into tests/fixtures/parity/ (tests/parity_reference.py; README there), frozen: the frames and cells
it drew, its help, and the same model as a `status --json --full` reply (tools/slotfather_reference.py, the SF-1
stand-in). Godfather reads that reply in fixture mode, its adapter decodes it, and Godfather renders it at the same
moment. Every cell, character and style, must be identical, emoji and ASCII, wide, one-column and narrow, but the
footer (its name and version) and the EVENTS panel, whose columns Godfather changed on purpose after the move (time ·
slots · label · result, owner 2026-11-12); help likewise in every section but EVENTS. A missing or unreadable fixture
fails the test; it never skips.
"""
from __future__ import annotations

import gzip
import json
import os
import unittest
import zlib
from pathlib import Path
from typing import Any
from unittest import mock

from helpers import ROOT, frozen_at, timezone

from godfather import app, sources, state, tui
from godfather.adapters import slotfather as adapter
from godfather.views import common, machine, overlays, overview


FIXTURE = ROOT / "tests" / "fixtures" / "parity"


def read(name: str) -> Any:
    path = FIXTURE / name
    try:
        if name.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, EOFError, zlib.error) as error:
        raise AssertionError(f"parity fixture {path} unreadable ({error}); it is frozen from Slotfather 37c4df7, "
                             f"see {FIXTURE / 'README.md'}") from error


class ParityTests(unittest.TestCase):
    out: Path
    frames: dict[str, str]
    scene: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.out = FIXTURE
        cls.frames = read("frames.json.gz")
        cls.cells = read("cells.json.gz")
        cls.help = read("help.json")
        cls.scene = read("scene.json")
        read("slotfather/replies.json")

    def setUp(self) -> None:
        keep = {key: value for key, value in os.environ.items()
                if not key.startswith(("SLOTFATHER_", "GODFATHER_", "COLORFGBG", "NO_COLOR"))}
        patcher = mock.patch.dict(os.environ, dict(keep, GODFATHER_FIXTURES=str(self.out)), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        tui.set_theme(app._theme.from_environment(environ={}))
        history = mock.patch.object(machine, "HISTORY_WINDOW", self.scene["history_window"])
        history.start()
        self.addCleanup(history.stop)
        self.addCleanup(common.use_marks, False)

    def model(self) -> dict[str, Any]:
        model, error = app.Screen().load()
        self.assertIsNone(error)
        assert model is not None
        common.dashboard_colours(model, {})
        for item in model["holders"] + model["waiters"]:
            item["lane"] = self.scene["lanes"][json.dumps(list(item["key"]))]
            if item["lane"] is None:
                del item["lane"]
        return model

    def canvas(self, width: int, height: int, ascii_only: bool, selected: int, help_open: bool = False) -> tui.Canvas:
        model = self.model()
        common.use_marks(ascii_only)
        with timezone("Etc/GMT-2"), frozen_at(adapter.parse_time(self.scene["moment"])):  # type: ignore[arg-type]
            canvas = tui.Canvas(width, height)
            overview.dashboard_draw(canvas, model, selected, state.Help() if help_open else None, cwd="~/straylight")
            return canvas

    def render(self, width: int, height: int, colour: bool, ascii_only: bool, selected: int,
               help_open: bool = False) -> str:
        return self.canvas(width, height, ascii_only, selected, help_open).render(colour, ascii_only)

    def test_fixture_mode_reads_only_the_recorded_replies(self) -> None:
        self.assertEqual(str(self.out / "slotfather"), sources.which("slotfather"))
        self.assertIsNone(sources.which("junkfather"))

    def test_every_cell_but_the_footer_and_events_is_slotfathers(self) -> None:
        compared = 0
        for name, expected in self.cells.items():
            size, ascii_only, selected = name.split()
            width, height = (int(value) for value in size.split("x"))
            with self.subTest(name):
                canvas = self.canvas(width, height, ascii_only == "1", int(selected))
                events = canvas.tables.get("events")
                masked: set[tuple[int, int]] = set()
                if events:
                    masked = {(row, column) for row in range(events["y"], events["y"] + events["height"])
                              for column in range(events["x"], events["x"] + events["width"])}
                self.assertEqual(len(expected), canvas.height)
                for row in range(height - 1):
                    for column in range(width):
                        if (row, column) not in masked:
                            self.assertEqual(expected[row][column], canvas.cells[row][column],
                                             f"{name}: row {row}, column {column}")
                if events:  # the same frame and place, other columns
                    for row, column in ((events["y"], events["x"]), (events["y"] + events["height"] - 1,
                                                                     events["x"] + events["width"] - 1)):
                        self.assertEqual(expected[row][column], canvas.cells[row][column], f"{name}: EVENTS' frame")
                compared += 1
        self.assertGreaterEqual(compared, 25)

    def test_help_is_slotfathers_in_every_section_but_events(self) -> None:
        ours = {title: [list(entry) for entry in entries] for title, entries in overlays.HELP}
        theirs = {title: entries for title, entries in self.help}
        self.assertEqual(list(theirs), list(ours), "the same sections in the same order")
        for title in theirs:
            if title != "EVENTS":
                self.assertEqual(theirs[title], ours[title], title)
        self.assertNotEqual(theirs["EVENTS"], ours["EVENTS"])

    def test_plain_text_outside_events_is_slotfathers_too(self) -> None:
        old = self.frames["156x43 0 0 0"].split("\n")
        new = self.render(156, 43, False, False, 0).split("\n")
        self.assertEqual(old[:5], new[:5], "SLOTS")
        self.assertTrue(old[-1].lstrip().startswith("Slotfather"), old[-1])
        self.assertTrue(new[-1].lstrip().startswith("Godfather"), new[-1])

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
