"""Godfather draws what Slotfather's in-tree dashboard drew, from the same model, but the footer and EVENTS.

Slotfather's dashboard is gone upstream (slotfather#28, 81abd86); its last commit with one, 37c4df7, rendered its own
demo scene once into tests/fixtures/parity/ (tests/parity_reference.py; README there), frozen: the frames and cells
it drew, its help, and the same model as a `status --json --full` reply (tools/slotfather_reference.py, the SF-1
stand-in). Godfather reads that reply in fixture mode, its adapter decodes it, and Godfather renders it at the same
moment. Every cell, character and style, must be identical, emoji and ASCII, wide, one-column and narrow, but the
footer (its name and version) and the EVENTS panel, whose columns Godfather changed on purpose after the move (time ·
slots · label · result, owner 2026-11-12), and the focused panel's frame, heavy since fathers#139 (all four sides since fathers#165) with a selected row's markers leaning toward it, and what says the history chart shows slots held
(fathers#140; chart_title()), and from 83 to 109 columns everything under QUEUE (fathers#169: EVENTS full width,
MACHINE and DETAILS beside each other under it, at heights of their own), which the other sizes compare;
help likewise in every section but EVENTS. A missing or unreadable fixture
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

from helpers import ROOT, timezone

from godfather import app, entries, layout, overlay_text, sources, state, tui
from godfather.adapters import slotfather as adapter
from godfather.layout import Rect
from godfather.model import Dashboard
from godfather.views import common, overview


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


def admission_note(canvas: Any) -> set[tuple[int, int]]:
    """MACHINE's top border after its title: it says why admission is closed (fathers#156), where Slotfather's said
    `no admission` alone."""
    found: set[tuple[int, int]] = set()
    for row in range(canvas.height):
        text = "".join(str(cell[0]) or "\0" for cell in canvas.cells[row])
        at = text.find(" MACHINE ")
        if at >= 0:
            end = text.find("╮", at)
            found |= {(row, column) for column in range(at + len(" MACHINE "), end if end > 0 else canvas.width)}
    return found


def chart_title(canvas: tui.Canvas) -> set[tuple[int, int]]:
    """What MACHINE's chart shows since fathers#140, which Slotfather's dashboard never drew: beside the gauges, the
    title row above the chart and the axis line between its window and `now`, whose figures moved up into that title;
    stacked, only the ` slots held ` set into the bottom border (the axis line is compared as it was)."""
    rows = ["".join(str(cell[0]) or "\0" for cell in cells) for cells in canvas.cells]
    top = next((row for row, text in enumerate(rows) if "─ MACHINE " in text), None)
    if top is None:
        return set()
    x = rows[top].index("─ MACHINE ") - 1
    width = rows[top].index("╮", x) - x + 1
    height = next(row for row in range(top + 1, len(rows)) if rows[row][x] == "╰") - top + 1
    inside = layout.machine(Rect(top, x, height, width), 9)  # the rows' place does not depend on the values
    chart = inside.chart
    if chart is None:
        return set()
    if inside.side:  # both rows are otherwise blank left of the chart: their whole inside
        return {(row, column) for row in (chart.y - 1, chart.y + chart.height) for column in range(x + 1, x + width - 1)}
    return {(top + height - 1, column) for column in range(chart.x - 1, chart.x + 11)}


def swapped(canvas: tui.Canvas) -> set[tuple[int, int]]:
    """fathers#169: in one column from 83 columns EVENTS has the band Slotfather's DETAILS had, and the bottom row is
    MACHINE then DETAILS, as tall as DETAILS' key fields, where Slotfather's was EVENTS then MACHINE at MACHINE's
    height: every cell from the band down, masked (DETAILS and MACHINE are compared at the other sizes); none in the
    other layouts."""
    details, events = canvas.tables.get("details"), canvas.tables.get("events")
    if not details or not events or details["x"] == 0 or events["width"] != canvas.width:
        return set()
    return {(row, column) for row in range(events["y"], canvas.height) for column in range(canvas.width)}


RESTED = {"┏": "╭", "┓": "╮", "┗": "╰", "┛": "╯", "━": "─", "┃": "│"}  # a focused frame as a resting one
MARKED = {"▌": "┃", "▐": "┃"}  # a focused panel's selected row's markers since fathers#165, as Slotfather's


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
        self.theme = app._theme.from_environment(environ={})

    def model(self) -> Dashboard:
        model, error = app.Screen().load()
        self.assertIsNone(error)
        assert model is not None
        entries.dashboard_colours(model, {}, self.theme.holders())
        for item in model["holders"] + model["waiters"]:
            item["lane"] = self.scene["lanes"][json.dumps(list(item["key"]))]
            if item["lane"] is None:
                del item["lane"]
        model["history_window"] = self.scene["history_window"]
        return model

    def canvas(self, width: int, height: int, ascii_only: bool, selected: int, help_open: bool = False) -> tui.Canvas:
        model = self.model()
        with timezone("Etc/GMT-2"):
            canvas = tui.Canvas(width, height, self.theme, common.marks_for(ascii_only))
            overview.dashboard_draw(canvas, model, selected, state.Help() if help_open else None, cwd="~/straylight",
                                    moment=adapter.parse_time(self.scene["moment"]))
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
                # QUEUE holds the focus where it is drawn: its frame is heavy (fathers#139, fathers#165); a selected
                # row's heavy sides are in the row's colour, the frame's in the accent
                focused = canvas.tables.get("requests", dict(x=0, y=-1, width=0, height=0))
                rules = {focused["y"], focused["y"] + focused["height"] - 1}
                sides = {focused["x"], focused["x"] + focused["width"] - 1}
                moved = swapped(canvas)
                masked |= chart_title(canvas) | admission_note(canvas) | moved
                for row in range(height - 1):
                    for column in range(width):
                        if (row, column) not in masked:
                            cell = canvas.cells[row][column]
                            framed = row in rules and focused["x"] <= column < focused["x"] + focused["width"] or (
                                column in sides and focused["y"] < row < focused["y"] + focused["height"] - 1
                                and cell[1] == "accent")
                            if framed:
                                cell = [RESTED.get(str(cell[0]), cell[0]), *cell[1:]]
                            elif column in sides:
                                cell = [MARKED.get(str(cell[0]), cell[0]), *cell[1:]]
                            self.assertEqual(expected[row][column], cell, f"{name}: row {row}, column {column}")
                if events and not moved:  # the same frame and place, other columns
                    for row, column in ((events["y"], events["x"]), (events["y"] + events["height"] - 1,
                                                                     events["x"] + events["width"] - 1)):
                        self.assertEqual(expected[row][column], canvas.cells[row][column], f"{name}: EVENTS' frame")
                compared += 1
        self.assertGreaterEqual(compared, 25)

    def test_help_is_slotfathers_in_every_section_but_events(self) -> None:
        ours = {title: [list(entry) for entry in entries] for title, entries in overlay_text.HELP
                if title != "PAGES"}  # fathers#85: the pages are Godfather's own
        theirs = {title: entries for title, entries in self.help}
        self.assertEqual(list(theirs), list(ours), "the same sections in the same order")
        what = ["v", "what's new (or click Godfather what's new?)"]  # fathers#102: the changelog became what's new
        self.assertIn(what, ours["KEYS"])
        ours["KEYS"][ours["KEYS"].index(what)] = ["v", "changelog (or click the version)"]
        esc = ["esc", "close an overlay · leave a page"]  # fathers#85: Esc leaves a page too
        self.assertIn(esc, ours["KEYS"])
        ours["KEYS"][ours["KEYS"].index(esc)] = ["esc", "close an overlay"]
        redraw = ["q · ctrl+l", "quit · redraw"]  # fathers#143: Ctrl-L writes every row again
        self.assertIn(redraw, ours["KEYS"])
        ours["KEYS"][ours["KEYS"].index(redraw)] = ["q", "quit"]
        # fathers#84, #152: EVENTS' log, raw, text filter; #85: pages; #88: row actions
        for key in ("← → · f", "r", "/", "1 2 3", "enter · a"):
            ours["KEYS"] = [entry for entry in ours["KEYS"] if entry[0] != key]
        snail = ["🐌", "batch: unattended work, after the rest"]  # owner, 2026-11-12: batch is a snail, not a turtle
        self.assertIn(snail, ours["PRIORITY"])
        ours["PRIORITY"][ours["PRIORITY"].index(snail)] = ["🐢", snail[1]]
        for title in theirs:
            if title not in ("EVENTS", "SETTINGS"):  # fathers#87: Settings has a section per tool
                self.assertEqual(theirs[title], ours[title], title)
        self.assertNotEqual(theirs["EVENTS"], ours["EVENTS"])
        self.assertIn(["d · D D", "reset one · reset its section"], ours["SETTINGS"])

    def test_plain_text_outside_events_is_slotfathers_too(self) -> None:
        old = self.frames["156x43 0 0 0"].split("\n")
        new = self.render(156, 43, False, False, 0).split("\n")
        self.assertEqual(old[:5], new[:5], "SLOTS")
        self.assertTrue(old[-1].lstrip().startswith("Slotfather"), old[-1])
        self.assertTrue(new[-1].lstrip().startswith("Godfather"), new[-1])

    def test_the_footer_differs_only_in_the_name_and_where_the_hints_centre(self) -> None:
        old = self.frames["156x43 0 0 0"].split("\n")[-1]
        new = self.render(156, 43, False, False, 0).split("\n")[-1]
        hints = "↑↓ select   tab panel   s settings   ? help   q quit"
        self.assertIn(hints, old)
        self.assertIn(hints, new)
        self.assertTrue(new.startswith(" Godfather what's new? "), new)  # fathers#102
        self.assertEqual(old[old.index("updated"):], new[new.index("updated"):])


if __name__ == "__main__":
    unittest.main()
