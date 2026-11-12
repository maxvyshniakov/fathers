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
LIGHT = {"SLOTFATHER_TERMINAL_BG": "#fdfcfa", "SLOTFATHER_TERMINAL_FG": "#2a2a2a"}


def sample(now: datetime.datetime) -> dict[str, Any]:
    case = MarkTests()
    case.now = now
    return case.model()


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

        holders = [holder("plain", 10), holder("everything", 11, idle_since=old, asked="next", cpu=900,
                                              priority="interactive"),
                   holder("sleepy", 12, idle_since=old, cpu=900, priority="batch"), holder("hog", 13, cpu=900),
                   dict(holder("elder", 14, cpu=900), kind="legacy", style="legacy"), holder("veteran", 15)]
        holders[5]["kind"], holders[5]["style"] = "legacy", "legacy"
        waiters = [dict(label="next", kind="waiting", count=1, pid=20, style="text", cpu=0, since=now, granted=None,
                        priority="interactive"),
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
        for emoji in [*self.tool.marks.values(), *self.tool.priority_marks.values(), *self.tool.health_marks.values(),
                      self.tool.OLD_FALLBACK]:
            self.assertEqual([emoji], self.tool.clusters(emoji), emoji)
            self.assertEqual(2, self.tool.text_width(emoji), emoji)
        self.assertEqual(["a", "👨‍🦳", "b"], self.tool.clusters("a👨‍🦳b"), "the ZWJ sequence stays one glyph")
        self.assertEqual("👨‍🦳", self.tool.dashboard_text("👨‍🦳"), "the joiner is not blanked as a control")

    def test_labels_and_the_columns_after_them_line_up_at_120_60_and_40(self) -> None:
        for width in (120, 60, 40):
            with self.subTest(width=width):
                canvas = self.tool.Canvas(width, 14)
                self.tool.draw_requests(canvas, 0, 0, 14, width, self.model(), 0, self.now)
                rows = [y for y in range(1, 13) if canvas.cells[y][3][0] not in (" ", "")]
                self.assertEqual(8, len(rows), "every row has a state")
                for y in rows:
                    cells = canvas.cells[y]
                    self.assertEqual("", cells[4][0], "the glyph takes two cells")
                    self.assertEqual(" ", cells[5][0], "then a gap")
                    self.assertIn((cells[6][0], cells[7][0]), (("⚡", ""), ("🐢", ""), (" ", " ")),
                                  "the priority: two cells, blank for normal")
                    self.assertEqual(" ", cells[8][0], "then a gap")
                    self.assertNotEqual(" ", cells[9][0], "the label starts in the same cell on every row")
                    self.assertEqual("┃" if y == rows[0] else "│", cells[width - 1][0], "the frame stays put")
                    self.assertNotIn("▶", [cell[0] for cell in cells], "no cursor glyph")
                    self.assertFalse({"◌", "↓"} & {cell[0] for cell in cells}, "no marker column")
                if width >= 60:  # the columns right of the label start in the same cell on every running row
                    starts = {next(x for x in range(9, width) if canvas.cells[y][x][0] in "■░") for y in rows[:6]}
                    self.assertEqual(1, len(starts), starts)
                line = canvas.lines(True, place=True)[rows[5]]  # 👨‍🦳 at cell 3, then the cursor on cell 5 (CHA 6)
                self.assertIn("👨‍🦳\x1b[0m\x1b[6G", line)

    def test_the_priority_column_marks_interactive_and_batch_and_leaves_normal_blank(self) -> None:
        model = self.model()
        canvas = self.tool.Canvas(120, 14)
        self.tool.draw_requests(canvas, 0, 0, 14, 120, model, 0, self.now)
        found = {}
        for y in range(1, 13):
            text = "".join(cell[0] for cell in canvas.cells[y][9:30])
            label = next((label for label in ("plain", "everything", "sleepy", "next", "again")
                          if text.startswith(label + " ")), None)
            if label:
                found[label] = tuple(cell[0] for cell in canvas.cells[y][6:9])
        self.assertEqual({"plain": (" ", " ", " "), "everything": ("⚡", "", " "), "sleepy": ("🐢", "", " "),
                          "next": ("⚡", "", " "), "again": (" ", " ", " ")}, found, "a waiting request has one too")
        self.assertEqual(2, self.tool.text_width("⚡"))
        self.assertEqual(2, self.tool.text_width("🐢"))
        self.tool.use_marks(True)
        ascii_lines = self.tool.Canvas(120, 14)
        self.tool.draw_requests(ascii_lines, 0, 0, 14, 120, model, 0, self.now)
        plain = ascii_lines.lines(False, ascii_only=True)
        tags = {label: line[6] for line in plain for label in ("plain", "everything", "sleepy") if f" {label} " in line}
        self.assertEqual({"plain": " ", "everything": "!", "sleepy": "~"}, tags)
        self.assertTrue(all(line.isascii() for line in plain))

    def test_the_details_put_the_mark_before_the_priority(self) -> None:
        now = self.now
        for priority, shown, ascii_shown in (("interactive", "⚡ interactive", "! interactive"),
                                             ("batch", "🐢 batch", "~ batch"), ("normal", "normal", "normal"),
                                             (None, "normal", "normal")):
            for ascii_only in (False, True):
                with self.subTest(priority=priority, ascii_only=ascii_only):
                    self.tool.use_marks(ascii_only)
                    item = dict(label="build", kind="held", count=1, pid=10, style="h0", cpu=50, since=now,
                                granted=now, rss=1024, procs=1, queue="", cmd="work", tree=[], priority=priority)
                    model = {"holders": [item], "waiters": [], "slots": 4, "cores": 12}
                    canvas = self.tool.Canvas(80, 20)
                    self.tool.draw_detail(canvas, 0, 0, 20, 80, model, 0, None, now)
                    line = next(line for line in canvas.render(False).splitlines() if "priority" in line)
                    self.assertRegex(line, r"priority\s+" + (ascii_shown if ascii_only else shown) + r"\s")
                    self.assertNotRegex(line, r"priority\s+\S+\s+normal", "normal has no mark")
        self.tool.use_marks(False)

    def test_a_long_label_is_cut_with_an_ellipsis_after_the_state(self) -> None:
        model = self.model()
        model["holders"][0]["label"] = "lane-pin-ui-libs-0_42_2-0_3-and-a-good-deal-more-than-fits-here"
        canvas = self.tool.Canvas(60, 14)
        self.tool.draw_requests(canvas, 0, 0, 14, 60, model, 0, self.now)
        row = next(line for line in canvas.render(False).splitlines() if "lane-pin" in line)
        self.assertRegex(row, r"^┃  ▶️    lane-pin-u\S*… ", "the state, a blank priority, the name cut with an ellipsis")

    def test_the_selected_row_has_its_background_and_no_cursor(self) -> None:
        canvas = self.tool.Canvas(120, 14)
        self.tool.draw_requests(canvas, 0, 0, 14, 120, self.model(), 1, self.now)
        selected = [y for y in range(14) if any(cell[2] for cell in canvas.cells[y])]
        self.assertEqual(1, len(selected))
        self.assertTrue(all(cell[2] for cell in canvas.cells[selected[0]][1:119]), "the whole row, state included")
        self.assertIn("48;2;", canvas.lines(True)[selected[0]])

    def test_the_selected_row_is_marked_on_the_left_frame_in_its_colour(self) -> None:
        model = self.model()
        for index, style, sgr in ((0, "f0", "\x1b[0;38;2;%d;%d;%dm" % self.tool.current_theme.styles["f0"][0]),
                                  (6, "text", "\x1b[0m")):  # a running lease: its colour; a waiter: the text colour
            with self.subTest(index=index):
                canvas = self.tool.Canvas(120, 14)
                self.tool.draw_requests(canvas, 0, 0, 14, 120, model, index, self.now)
                [y] = [y for y in range(14) if any(cell[2] for cell in canvas.cells[y])]
                self.assertEqual(["┃", style], canvas.cells[y][0][:2])
                self.assertTrue(canvas.lines(True)[y].startswith(sgr + "┃"), canvas.lines(True)[y][:40])
                others = [canvas.cells[row][0][0] for row in range(1, 13) if row != y]
                self.assertEqual({"│"}, set(others), "only the selected row")
                self.assertEqual({"accent"}, {canvas.cells[row][0][1] for row in range(1, 13) if row != y},
                                 "the rest of the frame is the focused blue")
                self.assertEqual("#", canvas.lines(False, ascii_only=True)[y][0])
                self.assertEqual(" ", canvas.cells[y][1][0], "nothing after the frame moves")

    def test_the_selected_row_is_marked_on_both_frames_never_in_the_thumbs_cell(self) -> None:
        model = self.model()
        for index, fill in ((0, "f0"), (6, "text")):  # a running lease in its fill; a waiter in the text colour
            for height in (14, 6):  # everything fits; it overflows, with a thumb
                with self.subTest(index=index, height=height):
                    canvas = self.tool.Canvas(120, height)
                    self.tool.draw_requests(canvas, 0, 0, height, 120, model, index, self.now)
                    [y] = [y for y in range(height) if any(cell[2] for cell in canvas.cells[y])]
                    self.assertEqual([["┃", fill], ["┃", fill]], [canvas.cells[y][0][:2], canvas.cells[y][119][:2]])
                    thumbs = {(row, column) for row in range(height) for column in range(120)
                              if canvas.cells[row][column][0] in ("┃", "╻", "╹") and column not in (0, 119)}
                    self.assertEqual(height == 6, bool(thumbs), "a thumb only when the rows overflow")
                    self.assertTrue(all(column == 118 for _, column in thumbs), "the thumb is in the padding cell")
                    self.assertNotIn((y, 119), thumbs)
                    plain = canvas.lines(False, ascii_only=True)[y]
                    self.assertEqual(("#", "#"), (plain[0], plain[-1]), "ASCII: # on both sides")

    def test_ascii_terminals_get_one_character_tags(self) -> None:
        self.tool.use_marks(True)
        model = self.model()
        canvas = self.tool.Canvas(120, 14)
        self.tool.draw_requests(canvas, 0, 0, 14, 120, model, 0, self.now)
        lines = canvas.lines(False, ascii_only=True)
        self.assertTrue(all(line.isascii() for line in lines))
        found = {label: line[3] for line in lines for label in ("plain", "everything", "sleepy", "hog", "veteran",
                                                                "next", "again") if f" {label} " in line}
        self.assertEqual({"plain": ">", "everything": "?", "sleepy": "z", "hog": "!", "veteran": "o", "next": ".",
                          "again": "<"}, found)

    def test_a_terminal_that_does_not_join_the_sequence_gets_the_old_man(self) -> None:
        self.tool.use_marks(False, joined=False)
        self.assertEqual("👴", self.tool.marks["legacy"])
        answers = theme.Answers()
        answers.feed("\x1b[1;5R")  # 👨‍🦳 drawn as 👨 and 🦳: the cursor four cells on
        self.assertEqual(5, answers.column)


class PlacementTests(unittest.TestCase):
    """#15 review: glyph placement apart from colour, overlays over half a wide cell, late width replies."""

    def setUp(self) -> None:
        self.tool: Any = importlib.reload(importlib.import_module("slotfather.dashboard"))
        self.tool.set_theme(theme.resolve("auto", environ=DARK))

    def test_the_live_screen_places_every_wide_glyph_with_or_without_colour(self) -> None:
        canvas = self.tool.Canvas(12, 1)
        canvas.put(0, 2, "💤 x")
        for colour in (True, False):
            line = canvas.lines(colour, place=True)[0]
            self.assertIn("\x1b[5G", line, f"the cursor is put after 💤, colour {colour}")
        self.assertIn("\x1b[5G", canvas.lines(True, place=True)[0], "a one-code-point emoji too, not only sequences")

    def test_a_wide_glyph_is_drawn_over_two_styled_spaces(self) -> None:
        canvas = self.tool.Canvas(12, 1)
        canvas.put(0, 2, "▶️ x", "h0")
        canvas.select(0, 0, 12)
        line = canvas.lines(True, place=True)[0]
        sgr = self.tool.sgr("h0", True)
        self.assertIn(sgr + "  \x1b[3G▶️", line, "the background covers both cells, then the glyph is drawn at its cell")
        self.assertIn("▶️\x1b[0m\x1b[5G", line)

    def test_once_output_has_no_cursor_moves(self) -> None:
        canvas = self.tool.Canvas(12, 1)
        canvas.put(0, 2, "👨‍🦳 x")
        self.assertNotRegex(canvas.render(True), r"\x1b\[\d+G", "files, pipes and less -R get plain text")

    def test_an_overlay_over_half_a_wide_cell_blanks_the_other_half(self) -> None:
        canvas = self.tool.Canvas(8, 3)
        for row in range(3):
            canvas.put(row, 2, "👨‍🦳")
        canvas.put(0, 3, "x")  # over its second half
        self.assertEqual([" ", "x"], [canvas.cells[0][2][0], canvas.cells[0][3][0]])
        canvas.put(1, 1, "ab")  # over its first half
        self.assertEqual(["a", "b", " "], [cell[0] for cell in canvas.cells[1][1:4]])
        canvas.fill(2, 3, 1, 3)  # a panel whose edge lands on its second half
        self.assertEqual(" ", canvas.cells[2][2][0])
        for row in range(3):
            self.assertEqual(8, sum(self.tool.cluster_width(cell[0]) or (cell[0] == "") for cell in canvas.cells[row])
                             - sum(cell[0] == "" for cell in canvas.cells[row]), "every row eight cells")

    def test_a_late_width_reply_switches_to_the_old_man_and_redraws(self) -> None:
        import os
        read, write = os.pipe()
        try:
            answers = theme.Answers()
            self.tool.use_marks(False)
            self.assertTrue(self.tool.theme_reports(["\x1b[1;5R"], answers, write, False), "a redraw")
            self.assertEqual("👴", self.tool.marks["legacy"])
        finally:
            os.close(read)
            os.close(write)


class TintTests(unittest.TestCase):
    """#15: a selected running row is tinted with its own colour; a waiting one keeps the neutral selection."""

    def setUp(self) -> None:
        self.tool: Any = importlib.reload(importlib.import_module("slotfather.dashboard"))
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def selected_sgr(self, index: int) -> tuple[str, Any]:
        canvas = self.tool.Canvas(120, 14)
        self.tool.draw_requests(canvas, 0, 0, 14, 120, sample(self.now), index, self.now)
        [y] = [y for y in range(14) if any(cell[2] for cell in canvas.cells[y])]
        label = next(x for x in range(6, 60) if canvas.cells[y][x][0] not in (" ", ""))
        return self.tool.sgr(canvas.cells[y][label][1], canvas.cells[y][label][2]), canvas.cells[y]

    def test_a_running_row_in_its_tint_and_a_waiting_row_neutral(self) -> None:
        for environ, bg, fg in ((LIGHT, (0xFD, 0xFC, 0xFA), (0x2A, 0x2A, 0x2A)), (DARK, (0x28, 0x2C, 0x34), (255, 255, 255))):
            self.tool.set_theme(theme.resolve("auto", environ=environ))
            code, cells = self.selected_sgr(0)
            unselected = self.tool.sgr("h0", False)
            self.assertTrue(code.startswith(unselected[:-1] + ";48;2;"), "the same text colour as unselected")
            self.assertEqual(unselected[:-1] + ";48;2;%d;%d;%dm" % self.tool.current_theme.tints["h0"], code)
            self.assertNotIn("38;2;%d;%d;%d" % fg, code)  # not the foreground
            self.assertEqual(["┃", "f0"], cells[0][:2], "the frame marker in its fill")
            self.assertEqual("f0", next(cell[1] for cell in cells if cell[0] == "▪"), "slot cells too")
            code, _ = self.selected_sgr(6)
            self.assertEqual("\x1b[0;48;2;%d;%d;%dm" % theme.mix(bg, fg, theme.SELECTION_MIX), code, "waiting: neutral")

    def test_every_holder_tint_mixes_its_fill_and_keeps_nine_tenths_of_its_inks_contrast(self) -> None:
        for environ in (LIGHT, {"SLOTFATHER_TERMINAL_BG": "#1e1e1e", "SLOTFATHER_TERMINAL_FG": "#e6e6e6"},
                        {"SLOTFATHER_TERMINAL_BG": "#282c34", "SLOTFATHER_TERMINAL_FG": "#ffffff"}):
            found = theme.resolve("auto", environ=environ)
            background = found.background
            assert background is not None
            self.assertEqual(8, len(found.tints))
            for name, tinted in found.tints.items():
                ink, fill = found.styles[name][0], found.styles["f" + name[1:]][0]
                assert isinstance(ink, tuple) and isinstance(fill, tuple)
                mixes = [theme.mix(background, fill, step / 100) for step in range(1, 19)]
                self.assertIn(tinted, mixes, f"{name}: its fill in the background, at most 18%")
                plain = theme.contrast(ink, background)
                self.assertGreaterEqual(plain, 4.6)
                self.assertGreaterEqual(theme.contrast(ink, tinted), 0.9 * plain - 1e-9, (name, tinted))
                step = mixes.index(tinted) + 1
                stronger = theme.mix(background, fill, (step + 1) / 100)
                self.assertTrue(step == round(theme.TINT_MIX * 100) or theme.contrast(ink, stronger) < 0.9 * plain,
                                f"{name}: as much fill as the rule allows ({step}%)")
                self.assertGreater(theme.distance(tinted, background), 0.02, f"{name}'s tint shows")
            if found.light:
                self.assertGreaterEqual(min(theme.distance(tinted, background) for tinted in found.tints.values()),
                                        0.035, "on a light background stronger than the single-colour tints")

    def test_the_selected_rows_text_colour_is_its_unselected_one_for_every_holder(self) -> None:
        self.tool.set_theme(theme.resolve("auto", environ=LIGHT))
        now = self.now
        for n in range(self.tool.current_theme.holders()):
            holder = dict(label=f"h{n}", kind="held", count=1, pid=10, style=f"h{n}", cpu=50, since=now, granted=now)
            model = {"holders": [holder], "waiters": [], "slots": 4, "cores": 12}
            canvas = self.tool.Canvas(80, 5)
            self.tool.draw_requests(canvas, 0, 0, 5, 80, model, 0, now)
            cell = next(cell for cell in canvas.cells[2] if cell[0] == "h")
            self.assertEqual(f"h{n}", cell[1])
            self.assertEqual(self.tool.sgr(cell[1], False)[:-1], self.tool.sgr(cell[1], cell[2]).split(";48;2")[0])

    def test_without_a_known_background_the_row_is_bold(self) -> None:
        self.tool.set_theme(theme.resolve("auto", environ={}))
        code, _ = self.selected_sgr(0)
        self.assertTrue(code.startswith("\x1b[0;1;"), code)
        self.assertNotIn("48;2", code)


class FrameTests(unittest.TestCase):
    """#15: one title offset for every panel, and the right frame as a scrollbar."""

    def setUp(self) -> None:
        self.tool: Any = importlib.reload(importlib.import_module("slotfather.dashboard"))
        self.tool.set_theme(theme.resolve("auto", environ=DARK))
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def thumb(self, canvas: Any, x: int, top: int, height: int) -> list[int]:
        return [row - top for row in range(top, top + height) if canvas.cells[row][x][0] == "┃"]

    def column(self, canvas: Any, x: int, top: int, height: int) -> str:
        return "".join(canvas.cells[row][x][0] for row in range(top, top + height))

    def test_the_thumb_moves_in_half_cells_with_no_track(self) -> None:
        # 10 rows are 20 half cells; 30 of 100 shown: a thumb of 6 halves, at 14 halves' travel
        for offset, drawn in ((0, "┃┃┃       "), (15, " ╻┃┃╹     "), (35, "   ╻┃┃╹   "), (50, "     ┃┃┃  "),
                              (70, "       ┃┃┃"), (99, "       ┃┃┃")):
            with self.subTest(offset=offset):
                canvas = self.tool.Canvas(20, 12)
                canvas.box(0, 0, 12, 20)
                canvas.scrollbar(1, 18, 10, offset, 30, 100)
                self.assertEqual(drawn, self.column(canvas, 18, 1, 10))
                self.assertEqual({"dim"}, {canvas.cells[row][18][1] for row in range(1, 11)
                                           if canvas.cells[row][18][0] != " "}, "unfocused: muted")
                self.assertEqual({"│"}, {canvas.cells[row][19][0] for row in range(1, 11)}, "the frame is unbroken")
        canvas = self.tool.Canvas(20, 12)
        canvas.scrollbar(1, 18, 10, 15, 30, 100, True)
        self.assertEqual({"text"}, {canvas.cells[row][18][1] for row in (2, 3, 4, 5)}, "focused: the foreground")
        self.assertEqual(" #### ", "".join(canvas.lines(False, ascii_only=True)[row][18] for row in range(1, 7)),
                         "ASCII: whole cells")
        for offset, drawn in ((0, "╹         "), (2970, "         ╻")):  # the shortest thumb: half a cell
            canvas = self.tool.Canvas(20, 12)
            canvas.scrollbar(1, 18, 10, offset, 1, 3000)
            self.assertEqual(drawn, self.column(canvas, 18, 1, 10), offset)

    def test_tables_put_it_in_the_outer_padding_cell_and_nothing_moves(self) -> None:
        model = sample(self.now)
        fits, overflows = self.tool.Canvas(120, 14), self.tool.Canvas(120, 7)
        self.tool.draw_requests(fits, 0, 0, 14, 120, model, 0, self.now)
        self.tool.draw_requests(overflows, 0, 0, 7, 120, model, 0, self.now)
        self.assertEqual("┃", overflows.cells[2][118][0], "the thumb, in the outer padding cell")
        self.assertEqual(" ", overflows.cells[2][117][0], "a blank between it and the content")
        self.assertEqual(" ", fits.cells[2][118][0], "nothing while everything fits")
        same = ["".join(cell[0] for cell in row[:116]) for row in (fits.cells[2], overflows.cells[2])]
        self.assertEqual(same[0], same[1], "the content does not move when it starts to overflow")

    def test_every_scrollable_panel_keeps_a_blank_row_over_and_under_its_content(self) -> None:
        now = self.now
        model = sample(now)
        model["events"] = [dict(when=now, verb="granted", label=f"job-{n}", pid=100 + n, count=1, detail="")
                           for n in range(30)]
        detail = dict(model["holders"][0], tree=[(200 + n, 1.0, 1024, f"worker-{n}") for n in range(30)],
                      rss=1024, procs=30, queue="", cmd="work")
        detail_model = {**model, "holders": [detail], "waiters": []}

        def panels() -> list[tuple[str, Any]]:
            def queue(canvas: Any) -> tuple[int, int, int, int]:
                self.tool.draw_requests(canvas, 0, 0, 8, 100, model, 0, now)
                return 0, 0, 8, 100

            def events(canvas: Any) -> tuple[int, int, int, int]:
                self.tool.draw_events(canvas, 0, 0, 10, 100, model)
                return 0, 0, 10, 100

            def details(canvas: Any) -> tuple[int, int, int, int]:
                self.tool.draw_detail(canvas, 0, 0, 12, 100, detail_model, 0, None, now)
                return 0, 0, 12, 100

            def overlay(draw: Any) -> Any:
                def drawn(canvas: Any) -> tuple[int, int, int, int]:
                    draw(canvas)
                    info = canvas.help_view
                    return info.y, info.x, info.height - 1, info.width  # as if the footer row were the border

                return drawn

            return [("requests", queue), ("events", events), ("details", details),
                    ("help", overlay(self.tool.draw_help)), ("changelog", overlay(self.tool.draw_changelog))]

        for name, draw in panels():
            for offset in (0, 3, 10_000):  # the top, scrolled, the end
                with self.subTest(panel=name, offset=offset):
                    canvas = self.tool.Canvas(100, 24)
                    canvas.scroll = {name: offset}
                    y, x, height, width = draw(canvas)
                    for padding in (y + 1, y + height - 2):
                        inside = "".join(cell[0] for cell in canvas.cells[padding][x + 1:x + width - 1])
                        self.assertEqual("", inside.strip(), f"row {padding} is padding: nothing on it")
                    thumb = [row for row in range(y, y + height)
                             if canvas.cells[row][x + width - 2][0] in "┃╻╹"]
                    self.assertTrue(thumb, "it overflows: a thumb")
                    self.assertGreaterEqual(thumb[0], y + 2, "the thumb runs between the padding rows")
                    self.assertLessEqual(thumb[-1], y + height - 3)
                    if offset == 0:
                        self.assertEqual(y + 2, thumb[0], "at the top: the first content row")
                    if offset == 10_000:
                        self.assertEqual(y + height - 3, thumb[-1], "at the end: the last content row")

    def test_no_thumb_when_everything_fits(self) -> None:
        canvas = self.tool.Canvas(120, 14)
        self.tool.draw_requests(canvas, 0, 0, 14, 120, sample(self.now), 0, self.now)  # 8 rows fit
        self.assertEqual(" " * 12, self.column(canvas, 118, 1, 12))

    def test_the_selection_marker_and_the_thumb_share_a_row(self) -> None:
        model = sample(self.now)
        canvas = self.tool.Canvas(120, 7)  # room for three of the nine rows
        self.tool.draw_requests(canvas, 0, 0, 7, 120, model, 0, self.now)
        selected = [y for y in range(7) if any(cell[2] for cell in canvas.cells[y])]
        self.assertEqual(["┃", "f0"], canvas.cells[selected[0]][0][:2], "left: the row's fill")
        self.assertEqual(["┃", "text"], canvas.cells[selected[0]][118][:2], "right: the thumb, same row (QUEUE has focus)")
        self.assertEqual(["┃", "f0"], canvas.cells[selected[0]][119][:2], "right: the row's fill on the frame too")
        self.assertEqual(["│", "accent"], canvas.cells[selected[0] + 1][119][:2], "the focused frame: light, in blue")
        self.assertNotIn("↓", canvas.render(False), "no row counts: the scrollbar shows it")

    def test_every_panel_title_sits_at_one_offset(self) -> None:
        sys_path = _path_sys.path
        if str(_Path(__file__).resolve().parent) not in sys_path:
            sys_path.insert(0, str(_Path(__file__).resolve().parent))
        from test_footer import model as footer_model
        state = footer_model()
        state["holders"] = sample(self.now)["holders"][:2]
        for holder in state["holders"]:
            holder.update(key=(holder["pid"], None), priority="normal", ticket=None, meta="", context={},
                          workdir="", yieldable=False, yields=0, asked_at=None, passable=False, all=False)
        canvas = self.tool.Canvas(160, 44)
        self.tool.dashboard_draw(canvas, state)
        offsets: dict[str, int] = {}
        for line in canvas.render(False).splitlines():
            for title in ("SLOTS", "QUEUE", "EVENTS", "LEASE DETAILS", "MACHINE"):
                at = line.find(f" {title} ")
                if at >= 0:
                    frame = max(line.rfind("╭", 0, at), 0)
                    offsets[title] = at + 1 - frame
        self.assertEqual(5, len(offsets), offsets)
        self.assertEqual({3}, set(offsets.values()), offsets)
        queue = next(y for y, line in enumerate(canvas.render(False).splitlines()) if " QUEUE " in line)
        row = canvas.cells[queue + 2]  # under the blank padding row
        self.assertEqual("▶️", row[3][0], "the state starts where every panel's content does")
        self.assertEqual("p", row[9][0], "the label after it and the priority")


if __name__ == "__main__":
    unittest.main()
