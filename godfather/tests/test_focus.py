"""The focused panel shows by shape as well as colour (fathers#139): its frame is heavy on all four sides
(fathers#165: `┏━┓ ┃ ┗━┛`, `+=+ # +=+` on ASCII), so Tab changes what a terminal without colour shows. The glyphs on a live screen are tests/test_live.py's frame test; these are the
ASCII fallback and a panel without a name."""
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

    def test_ascii_draws_the_focused_rules_with_equals_signs(self) -> None:
        focused = self.frame("events").lines(False, ascii_only=True)
        self.assertEqual("+= EVENTS " + "=" * 19 + "+", focused[0])
        self.assertEqual("+" + "=" * 28 + "+", focused[-1])
        self.assertEqual(["#", "#"], [focused[2][0], focused[2][-1]], "and its sides")
        rested = self.frame("requests").lines(False, ascii_only=True)
        self.assertEqual("+- EVENTS " + "-" * 19 + "+", rested[0])
        self.assertEqual(["|", "|"], [rested[2][0], rested[2][-1]])

    def test_an_unnamed_panel_never_looks_focused(self) -> None:
        self.assertTrue(self.frame("", name="").render(False).startswith("╭"))


class HeaderTickTests(unittest.TestCase):
    """fathers#165 (owner, question 3): column labels set into a top border always have a tick of rule between them.
    Two cells apart, half segments meet in the middle of the gap (`waited╶╴outcome`, heavy `╺╸` on a focused
    frame); one cell apart, one rule cell; three or more, a space each side of the rule as before; `-` on ASCII."""

    def setUp(self) -> None:
        self.tool: Any = _view.fresh()
        self.addCleanup(_view.fresh)

    def header(self, gap: int, focus: str) -> Any:
        canvas = self.tool.Canvas(40, 4)
        canvas.focus = focus
        # waited right-aligned in 7..13 of the table, outcome left-aligned `gap` cells after it
        table = self.tool.Table([("time", "time", 0, 4, False), ("waited", "waited", 7, 6, True),
                                 ("outcome", "outcome", 13 + gap, 7, False)])
        table.header(self.tool.Panel(canvas, Rect(0, 0, 4, 40), "events"), "")
        return canvas

    def test_two_and_one_cell_gaps_keep_a_tick_light_and_heavy(self) -> None:
        cases = [(2, "requests", "waited╶╴outcome", "frame"), (2, "events", "waited╺╸outcome", "accent"),
                 (1, "requests", "waited─outcome", "frame"), (1, "events", "waited━outcome", "accent"),
                 (3, "requests", "waited ─ outcome", "frame"), (4, "events", "waited ━━ outcome", "accent")]
        for gap, focus, text, style in cases:
            with self.subTest(gap=gap, focus=focus):
                canvas = self.header(gap, focus)
                line = canvas.render(False).splitlines()[0]
                self.assertIn(text, line)
                ticks = range(line.index("waited") + 6, line.index("outcome"))
                self.assertEqual({style}, {canvas.cells[0][x][1] for x in ticks if canvas.cells[0][x][0] != " "})
                self.assertIn(text.replace("╶╴", "--").replace("╺╸", "--").replace("─", "-").replace("━", "="),
                              canvas.lines(False, ascii_only=True)[0])


if __name__ == "__main__":
    unittest.main()
