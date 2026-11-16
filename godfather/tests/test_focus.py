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


if __name__ == "__main__":
    unittest.main()
