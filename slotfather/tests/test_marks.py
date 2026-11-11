"""The QUEUE state column (#15): one emoji per row, two cells, before the label; no cursor glyph."""
from __future__ import annotations

import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import datetime
import importlib
import unittest
from typing import Any

from slotfather import theme

DARK = {"SLOTFATHER_TERMINAL_BG": "#282c34", "SLOTFATHER_TERMINAL_FG": "#ffffff"}


class MarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = importlib.reload(importlib.import_module("slotfather.dashboard"))
        self.tool.set_theme(theme.resolve("auto", environ=DARK))
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def model(self) -> dict[str, Any]:
        now, old = self.now, self.now - datetime.timedelta(minutes=30)

        def holder(label: str, pid: int, **extra: Any) -> dict[str, Any]:
            return {**dict(label=label, kind="held", count=1, pid=pid, style="h0", cpu=50, since=old, granted=old,
                           rss=1024, procs=1, queue="", cmd="work", tree=[]), **extra}

        holders = [holder("plain", 10), holder("everything", 11, idle_since=old, asked="next", cpu=900),
                   holder("sleepy", 12, idle_since=old, cpu=900), holder("hog", 13, cpu=900),
                   dict(holder("elder", 14, cpu=900), kind="legacy", style="legacy"), holder("veteran", 15)]
        holders[5]["kind"], holders[5]["style"] = "legacy", "legacy"
        waiters = [dict(label="next", kind="waiting", count=1, pid=20, style="text", cpu=0, since=now, granted=None),
                   dict(label="again", kind="waiting", count=1, pid=21, style="text", cpu=0, since=now, granted=None,
                        yields=1)]
        return {"holders": holders, "waiters": waiters, "slots": 4, "cores": 12}

    def test_one_state_per_row_the_first_that_applies(self) -> None:
        model = self.model()
        self.assertEqual(["running", "asked", "idle", "hot", "hot", "legacy", "waiting", "yielded"],
                         [self.tool.row_state(item, model) for item in model["holders"] + model["waiters"]],
                         "asked over idle over hot over legacy; waiting rows waiting or yielded")
        self.assertEqual({"asked": "❓", "idle": "💤", "hot": "🔥", "legacy": "👨‍🦳", "running": "▶️",
                          "yielded": "↩️", "waiting": "⏸️"}, self.tool.marks)
        self.assertEqual("⏸️", self.tool.marks["waiting"], "⏸️ with VS16")

    def test_every_emoji_is_one_two_cell_glyph(self) -> None:
        for emoji in [*self.tool.marks.values(), self.tool.OLD_FALLBACK]:
            self.assertEqual([emoji], self.tool.clusters(emoji), emoji)
            self.assertEqual(2, self.tool.text_width(emoji), emoji)
        self.assertEqual(["a", "👨‍🦳", "b"], self.tool.clusters("a👨‍🦳b"), "the ZWJ sequence stays one glyph")
        self.assertEqual("👨‍🦳", self.tool.dashboard_text("👨‍🦳"), "the joiner is not blanked as a control")

    def test_labels_and_the_columns_after_them_line_up_at_120_60_and_40(self) -> None:
        for width in (120, 60, 40):
            with self.subTest(width=width):
                canvas = self.tool.Canvas(width, 14)
                self.tool.draw_requests(canvas, 0, 0, 14, width, self.model(), 0, self.now)
                rows = [y for y in range(2, 13) if canvas.cells[y][2][0] not in (" ", "")]
                self.assertEqual(8, len(rows), "every row has a state")
                for y in rows:
                    cells = canvas.cells[y]
                    self.assertEqual("", cells[3][0], "the glyph takes two cells")
                    self.assertEqual(" ", cells[4][0], "then a gap")
                    self.assertNotEqual(" ", cells[5][0], "the label starts in the same cell on every row")
                    self.assertEqual("│", cells[width - 1][0], "the frame stays put")
                    self.assertNotIn("▶", [cell[0] for cell in cells], "no cursor glyph")
                    self.assertFalse({"◌", "↓"} & {cell[0] for cell in cells}, "no marker column")
                if width >= 60:  # the columns right of the label start in the same cell on every running row
                    starts = {next(x for x in range(5, width) if canvas.cells[y][x][0] in "■░") for y in rows[:6]}
                    self.assertEqual(1, len(starts), starts)
                line = canvas.lines(True)[rows[5]]  # 👨‍🦳 at cell 2: the cursor is then put on cell 4 (CHA 5)
                self.assertIn("👨‍🦳\x1b[0m\x1b[5G", line)

    def test_a_long_label_is_cut_with_an_ellipsis_after_the_state(self) -> None:
        model = self.model()
        model["holders"][0]["label"] = "lane-pin-ui-libs-0_42_2-0_3-and-a-good-deal-more-than-fits-here"
        canvas = self.tool.Canvas(60, 14)
        self.tool.draw_requests(canvas, 0, 0, 14, 60, model, 0, self.now)
        row = next(line for line in canvas.render(False).splitlines() if "lane-pin" in line)
        self.assertRegex(row, r"^┃ ▶️ lane-pin-ui\S*… ", "the state first, the name cut with a plain ellipsis")

    def test_the_selected_row_has_its_background_and_no_cursor(self) -> None:
        canvas = self.tool.Canvas(120, 14)
        self.tool.draw_requests(canvas, 0, 0, 14, 120, self.model(), 1, self.now)
        selected = [y for y in range(14) if any(cell[2] for cell in canvas.cells[y])]
        self.assertEqual(1, len(selected))
        self.assertTrue(all(cell[2] for cell in canvas.cells[selected[0]][1:119]), "the whole row, state included")
        self.assertIn("48;2;", canvas.lines(True)[selected[0]])

    def test_the_selected_row_is_marked_on_the_left_frame_in_its_colour(self) -> None:
        model = self.model()
        for index, style, sgr in ((0, "h0", "\x1b[0;38;2;%d;%d;%dm" % self.tool.current_theme.styles["h0"][0]),
                                  (6, "text", "\x1b[0m")):  # a running lease: its colour; a waiter: the text colour
            with self.subTest(index=index):
                canvas = self.tool.Canvas(120, 14)
                self.tool.draw_requests(canvas, 0, 0, 14, 120, model, index, self.now)
                [y] = [y for y in range(14) if any(cell[2] for cell in canvas.cells[y])]
                self.assertEqual(["┃", style], canvas.cells[y][0][:2])
                self.assertTrue(canvas.lines(True)[y].startswith(sgr + "┃"), canvas.lines(True)[y][:40])
                others = [canvas.cells[row][0][0] for row in range(1, 13) if row != y]
                self.assertEqual({"│"}, set(others), "only the selected row")
                self.assertEqual("#", canvas.lines(False, ascii_only=True)[y][0])
                self.assertEqual(" ", canvas.cells[y][1][0], "nothing after the frame moves")

    def test_ascii_terminals_get_one_character_tags(self) -> None:
        self.tool.use_marks(True)
        model = self.model()
        canvas = self.tool.Canvas(120, 14)
        self.tool.draw_requests(canvas, 0, 0, 14, 120, model, 0, self.now)
        lines = canvas.lines(False, ascii_only=True)
        self.assertTrue(all(line.isascii() for line in lines))
        found = {label: line[2] for line in lines for label in ("plain", "everything", "sleepy", "hog", "veteran",
                                                                "next", "again") if f" {label} " in line}
        self.assertEqual({"plain": ">", "everything": "?", "sleepy": "z", "hog": "!", "veteran": "o", "next": ".",
                          "again": "<"}, found)

    def test_a_terminal_that_does_not_join_the_sequence_gets_the_old_man(self) -> None:
        self.tool.use_marks(False, joined=False)
        self.assertEqual("👴", self.tool.marks["legacy"])
        answers = theme.Answers()
        answers.feed("\x1b[1;5R")  # 👨‍🦳 drawn as 👨 and 🦳: the cursor four cells on
        self.assertEqual(5, answers.column)


if __name__ == "__main__":
    unittest.main()
