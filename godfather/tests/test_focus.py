"""The focused panel shows by shape as well as colour (fathers#139): its top and bottom borders are heavy rules
(`━`, `=` on ASCII), its sides stay light, so the heavy `┃` of a selected row keeps its own meaning, and Tab changes
what a terminal without colour shows."""
from __future__ import annotations

import unittest
from typing import Any

import view as _view

from godfather.layout import Rect


class FocusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = _view.fresh()
        self.addCleanup(_view.fresh)

    def frame(self, focus: str, name: str = "events") -> Any:
        canvas = self.tool.Canvas(30, 6)
        canvas.focus = focus
        self.tool.Panel(canvas, Rect(0, 0, 6, 30), name).frame("EVENTS")
        return canvas

    def test_the_focused_frame_has_heavy_rules_top_and_bottom_and_light_sides(self) -> None:
        focused = self.frame("events").render(False).splitlines()
        self.assertEqual("┍━ EVENTS " + "━" * 19 + "┑", focused[0])
        self.assertEqual("┕" + "━" * 28 + "┙", focused[-1])
        self.assertEqual({"│"}, {line[0] for line in focused[1:-1]} | {line[-1] for line in focused[1:-1]},
                         "light sides: the heavy ┃ stays the selected row's")
        rested = self.frame("requests").render(False).splitlines()
        self.assertEqual("╭─ EVENTS " + "─" * 19 + "╮", rested[0])
        self.assertEqual("╰" + "─" * 28 + "╯", rested[-1])

    def test_ascii_draws_the_focused_rules_with_equals_signs(self) -> None:
        focused = self.frame("events").lines(False, ascii_only=True)
        self.assertEqual("+= EVENTS " + "=" * 19 + "+", focused[0])
        self.assertEqual("+" + "=" * 28 + "+", focused[-1])
        rested = self.frame("requests").lines(False, ascii_only=True)
        self.assertEqual("+- EVENTS " + "-" * 19 + "+", rested[0])

    def test_an_unnamed_panel_never_looks_focused(self) -> None:
        self.assertTrue(self.frame("", name="").render(False).startswith("╭"))


if __name__ == "__main__":
    unittest.main()
