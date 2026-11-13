#!/usr/bin/env python3
"""The dashboard against real queues: `dashboard --once` prints the same canvas the live view draws, so these tests
read its text. Log history is checked on slotfather-dashboard itself."""
from __future__ import annotations
import helpers  # noqa: F401 - src/ on sys.path
from helpers import mouse
import view as _view

def judged(memory: dict[str, Any]) -> dict[str, Any]:
    """Memory facts with Slotfather's judgement of them, as its snapshot carries it (docs/contracts/slotfather.md):
    low above pressure level 1, swap low under 512 MB free."""
    level, free = memory.get("level"), memory.get("swap_free")
    psi_low = (memory.get("psi_some") or 0) >= 10 or (memory.get("psi_full") or 0) >= 5
    low = level > 1 if level is not None else (
        psi_low or (memory.get("available") is not None and memory["available"] < 1024)
        if any(memory.get(key) is not None for key in ("available", "psi_some", "psi_full")) else None)
    return dict(memory, low=low, psi_low=psi_low,
                swap_low=bool(memory.get("swap_total")) and free is not None and free < 512)




import datetime
import os
import re
import unittest
from unittest import mock
from typing import Any, cast
from godfather.layout import Rect



def load_dashboard():
    return _view.fresh()


DARK = {"SLOTFATHER_TERMINAL_BG": "#282c34", "SLOTFATHER_TERMINAL_FG": "#ffffff"}


def surfaces(tool: Any) -> Any:
    """The dashboard drawing with mixed surfaces (#14: a known background), as layout tests read its glyphs."""
    tool.set_theme(tool._theme.resolve("auto", environ=DARK))
    return tool






class UnifiedLaneTests(unittest.TestCase):
    def test_running_and_waiting_share_a_viewport_with_an_unselectable_gap(self):
        tool=load_dashboard()
        now=datetime.datetime.now(datetime.timezone.utc)
        holder=dict(label="active",kind="held",count=1,pid=10,style="h0",cpu=150,since=now,granted=now,key=(10,None))
        waiter=dict(label="pending",kind="waiting",count=2,pid=11,style="warn",cpu=0,since=now,granted=None,
                    key=(11,None))
        model=dict(holders=[holder],waiters=[waiter],slots=6,cores=12)
        canvas=tool.Canvas(130,8)
        tool.draw_requests(canvas, Rect(0, 0, 8, 130),model,1,now)
        lines=canvas.render(False).splitlines()
        self.assertIn("QUEUE",lines[0])
        self.assertFalse(lines[1][1:-1].strip(),"a blank padding row")
        self.assertIn("active",lines[2])
        self.assertFalse(lines[3][1:-1].strip())
        self.assertIn("┃  ⏸️    pending",lines[4])
        column=lines[0].index("cpu use/share")+len("cpu use/share")
        self.assertTrue(lines[4][:column].rstrip().endswith("waiting #1"),lines[4])
        self.assertEqual(1,mouse(canvas,("mouse",0,20,4,True),{},0,model))
        self.assertEqual(0,mouse(canvas,("mouse",0,20,3,True),{},0,model))
        canvas=tool.Canvas(130,6)
        tool.draw_requests(canvas, Rect(0, 0, 6, 130),model,1,now)
        lines=canvas.render(False).splitlines()
        self.assertIn("pending",lines[3],"selection must remain visible beyond the gap")
        self.assertNotIn("above",canvas.render(False),"the scrollbar shows overflow, no row counts")


class QueueTableTests(unittest.TestCase):
    """The QUEUE table (owner request): no status column; a request that holds no slots says so in the cpu column;
    slot counts are drawn as cells in the row's colour."""

    def setUp(self) -> None:
        self.tool = surfaces(load_dashboard())
        self.marks = self.tool.marks_for(True)  # one-cell ASCII states: these tests read columns as positions (#15)
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def model(self, **extra: Any) -> dict[str, Any]:
        now = self.now
        holders = [dict(label="build", kind="held", count=2, pid=10, style="h0", cpu=150, since=now, granted=now),
                   dict(label="older", kind="legacy", count=1, pid=11, style="legacy", cpu=0, since=now, granted=now)]
        waiters = [dict(label="gate", kind="waiting", count=3, pid=12, style="text", cpu=0, since=now, granted=None),
                   dict(label="back", kind="waiting", count=1, pid=13, style="text", cpu=0, since=now, granted=None,
                        yields=1)]
        return {"holders": holders, "waiters": waiters, "slots": 6, "cores": 12, **extra}

    def draw(self, model: dict[str, Any], width: int = 120, height: int = 10) -> Any:
        canvas = self.tool.Canvas(width, height, marks=self.marks)
        self.tool.draw_requests(canvas, Rect(0, 0, height, width), model, 0, self.now)
        return canvas

    def row(self, canvas: Any, label: str) -> int:
        return next(n for n, line in enumerate(canvas.render(False).splitlines()) if f" {label} " in line + " ")

    def styles(self, canvas: Any, y: int, text: str) -> set[str]:
        line = "".join(cell[0] for cell in canvas.cells[y])
        start = line.index(text)
        return {canvas.cells[y][x][1] for x in range(start, start + len(text))}

    def test_there_is_no_status_column_and_time_follows_the_cpu_column(self) -> None:
        canvas = self.draw(self.model())
        lines = canvas.render(False).splitlines()
        header = lines[0]
        self.assertNotIn("status", header)
        places = [header.index(title) for title in ("QUEUE", "slots", "cpu use/share", "time", "pid")]
        self.assertEqual(sorted(places), places)
        self.assertRegex(lines[self.row(canvas, "gate")], r"waiting #1 +0s +12 ")
        self.assertRegex(lines[self.row(canvas, "build")], r"■ 1\.5/4 +0s +10 ")

    def test_a_request_holding_no_slots_says_why_in_the_cpu_column_in_a_neutral_colour(self) -> None:
        canvas = self.draw(self.model())
        lines = canvas.render(False).splitlines()
        end = lines[0].index("cpu use/share") + len("cpu use/share")
        for label, state in (("gate", "waiting #1"), ("back", "yielded #2")):
            y = self.row(canvas, label)
            self.assertTrue(lines[y][:end].rstrip().endswith(state), lines[y])
            self.assertEqual({"text"}, self.styles(canvas, y, state), "white like the rest of the row")
            self.assertEqual({"text"}, self.styles(canvas, y, label), "a waiting label is not yellow")
            self.assertNotIn("warn", {cell[1] for cell in canvas.cells[y]})
        build = self.row(canvas, "build")
        self.assertRegex(lines[build], r"■+ 1\.5/4")

    def test_a_row_is_one_colour_the_slots_colour_running_and_white_waiting_but_for_the_bar(self) -> None:
        canvas = self.draw(self.model())
        for label, style in (("build", "h0"), ("older", "legacy"), ("gate", "text"), ("back", "text")):
            y = self.row(canvas, label)
            cells = canvas.cells[y][3:-3]
            # build is selected and tinted with its colour (#15): its text in its ink, its cells in its fill
            text = {cell[1] for cell in cells if cell[0] not in (" ", "■", "", "▪", ">")}
            self.assertEqual({style}, text, f"{label}: the selected row keeps its colour too")
            self.assertTrue(all(cell[1] == self.tool.fill(style, self.tool.current_theme) for cell in cells if cell[0] == "▪"), label)
        build = self.row(canvas, "build")
        self.assertEqual({"f0", "track"}, {cell[1] for cell in canvas.cells[build] if cell[0] == "■"},
                         "the CPU bar fills in the row's fill; its empty cells keep the track colour")
        self.assertTrue(all(cell[2] for cell in canvas.cells[build][3:-3]), "the selection highlight stays on top")

    def test_bars_and_states_start_at_one_column_and_the_numbers_line_up(self) -> None:
        now = self.now
        rows = [(1, 0.0, 12), (2, 12.3, 12), (8, 5.1, 24), (1, 0.5, 12), (2, 1.3, 12)]  # shares 1.5, 3, 12 ... 0.5
        holders = [dict(label=f"run{n}", kind="held", count=count, pid=20 + n, style=f"h{n}", cpu=cpu * 100,
                        since=now, granted=now) for n, (count, cpu, _) in enumerate(rows)]
        for slots, cores in ((8, 12), (2, 12), (24, 12), (8, 4)):
            with self.subTest(slots=slots, cores=cores):
                model = self.model(slots=slots, cores=cores)
                model["holders"] = holders
                canvas = self.draw(model, height=14)
                lines = canvas.render(False).splitlines()
                labels = [f"run{n}" for n in range(len(rows))]
                starts = {lines[self.row(canvas, label)].index("■", lines[0].index("cpu use/share")) for label in labels}
                self.assertEqual({lines[0].index("cpu use/share")}, starts, "every bar starts where the header does")
                slashes = {lines[self.row(canvas, label)].index("/", min(starts)) for label in labels}
                self.assertEqual(1, len(slashes), "use and share line up around the slash")
                states = {lines[self.row(canvas, label)].index(state) for label, state in (("gate", "waiting #1"),
                                                                                          ("back", "yielded #2"))}
                self.assertEqual(starts, states, "a waiting row's state starts where the bars do")

    def test_a_cpu_bar_fills_in_the_rows_colour_unless_it_warns(self) -> None:
        now, old = self.now, self.now - datetime.timedelta(minutes=10)
        model = self.model()
        model["holders"] = [
            dict(label="fair", kind="held", count=2, pid=10, style="h0", cpu=200, since=now, granted=now),
            dict(label="hog", kind="held", count=1, pid=11, style="h1", cpu=900, since=now, granted=now),
            dict(label="idle", kind="held", count=1, pid=12, style="h2", cpu=5, since=old, granted=old,
                 idle_since=old)]  # #12: idle is the holder's record (idle.idle_since()), not a guess from one sample
        styles = [self.tool.queue_bar_style(item, model, self.tool.current_theme) for item in model["holders"]]
        self.assertEqual(["f0", "bad", "warn"], styles, "over its share stays red, an idle lease stays yellow")
        canvas = self.draw(model)
        cpu = canvas.render(False).splitlines()[0].index("cpu use/share")
        for label, filled in (("fair", {"f0", "track"}), ("hog", {"bad"})):
            y = self.row(canvas, label)
            self.assertEqual(filled, {cell[1] for cell in canvas.cells[y][cpu:cpu + 10] if cell[0] == "■"}, label)

    def test_slots_are_countable_cells_in_the_rows_colour_alike_for_running_and_waiting(self) -> None:
        for form, two in (("spaced", "■ ■"), ("small", "▪▪")):
            with self.subTest(form=form), mock.patch.object(self.tool, "SLOT_CELL", form):
                one, three = two[0], self.tool.cells_text(3)
                self.assertEqual(3, three.count(one))
                canvas = self.draw(self.model())
                lines = canvas.render(False).splitlines()
                column = lines[0].index("slots")
                for label, cells, style in (("build", two, "f0"), ("older", one, "legacy"), ("gate", three, "text"),
                                            ("back", one, "text")):
                    y = self.row(canvas, label)
                    self.assertEqual(cells, lines[y][column:].split("  ")[0], label)
                    self.assertEqual({style}, self.styles(canvas, y, cells), label)
                self.assertNotRegex(lines[self.row(canvas, "gate")], r"\b3\b", "no digit for the count")

    def test_large_counts_are_capped_with_the_rest_counted_and_all_says_so(self) -> None:
        model = self.model(slots=24)
        model["holders"][0]["count"] = 20
        model["waiters"][0].update(count=24, all=True)
        canvas = self.draw(model)
        lines = canvas.render(False).splitlines()
        column = lines[0].index("slots")
        build = lines[self.row(canvas, "build")][column:].split("  ")[0]
        self.assertRegex(build, r"^▪+ \+\d+$")
        self.assertEqual(20, build.count("▪") + int(build.split("+")[1]))
        self.assertEqual("all", lines[self.row(canvas, "gate")][column:].split("  ")[0])

    def test_a_narrow_table_drops_columns_and_keeps_every_row_inside_its_frame(self) -> None:
        titles = ("slots", "cpu use/share", "time", "pid")
        for width, kept in ((110, titles), (62, titles), (55, titles[:3]), (47, titles[:2]), (33, titles[:1])):
            with self.subTest(width=width):
                lines = self.draw(self.model(), width=width).render(False).splitlines()
                self.assertEqual(kept, tuple(title for title in titles if title in lines[0]),
                                 "pid goes first, then time, then cpu use/share")
                for line in lines[:8]:
                    self.assertLessEqual(len(line.rstrip()), width)
                gate = next(line for line in lines if "gate" in line)
                self.assertIn("▪▪▪", gate, "the slot cells stay longest after the label")
                if "cpu use/share" in kept:
                    self.assertIn("waiting #1", gate, "the state outlasts the time and pid")
                self.assertIn(gate.rstrip()[-1], "│┃")

    def test_a_wide_table_shares_its_width_out_in_proportion(self) -> None:
        model = self.model()
        model["holders"].append(dict(label="wide", kind="held", count=1, pid=14, style="h1", cpu=1230, since=self.now,
                                     granted=self.now))
        for width in (120, 160, 220):
            with self.subTest(width=width):
                layout = self.tool.queue_layout(width - 6, model)
                sizes = {key: size for key, _, _, size, _ in layout.table.placed}
                self.assertEqual(width - 6, sum(sizes.values()) + 2 * (len(sizes) - 1), "the table fills the panel")
                ratios = [sizes[key] / layout.natural[key] for key in sizes]
                self.assertLess(max(ratios) - min(ratios), 0.25, f"in proportion: {sizes} of {layout.natural}")
                self.assertTrue(all(sizes[key] >= layout.natural[key] for key in sizes))
                self.assertTrue(5 <= layout.bar <= 10, "the bar grows with its column, at most twice")
                canvas = self.draw(model, width=width)
                lines = canvas.render(False).splitlines()
                for line in lines[:9]:
                    self.assertLessEqual(len(line.rstrip()), width)
                cpu = lines[0].index("cpu use/share")
                starts = {lines[self.row(canvas, label)].index("■", cpu) for label in ("build", "older", "wide")}
                self.assertEqual({cpu}, starts, "bars start under the header, on every row")
                self.assertEqual({cpu}, {lines[self.row(canvas, "gate")].index("waiting #1")})
                slashes = {lines[self.row(canvas, label)].index("/", cpu) for label in ("build", "older", "wide")}
                self.assertEqual(1, len(slashes))
                pid_end = lines[0].index("pid") + len("pid")
                self.assertTrue(lines[self.row(canvas, "build")][:pid_end].endswith("10"), "pid under its title")

    def test_an_ascii_terminal_gets_pure_ascii_rows(self) -> None:
        model = self.model(slots=24)
        model["holders"][0]["count"] = 20
        for line in self.draw(model).lines(False, ascii_only=True):
            self.assertTrue(line.isascii(), line)

    def test_a_large_count_is_cut_without_trying_every_count_below_it(self) -> None:
        item = dict(kind="held", style="h0", count=100000)
        with mock.patch.object(self.tool, "cells_text", wraps=self.tool.cells_text) as tries:
            self.assertEqual([("▪", "f0"), (" +99999", "h0")], self.tool.slot_cells(item, 11, self.tool.current_theme),
                             "at most 8 columns")
        self.assertLess(tries.call_count, 25, "a bounded number of tries, not one per count")
        self.assertEqual([("▪▪▪▪", "f0"), (" +16", "h0")], self.tool.slot_cells(dict(item, count=20), 8,
                                                                                self.tool.current_theme),
                         "cells in the fill, the count in the ink")


class EventDetailTests(unittest.TestCase):
    """EVENTS' detail column, as Slotfather describes each event from its request's history line (#queue-table
    review); Slotfather keeps that description and its tests, Godfather draws it."""



    def events(self, padding: int = 0) -> list[dict[str, Any]]:
        """The events Slotfather describes for its test log (its queue_events() and describe_events(), which stay
        there), as its snapshot's dashboard block carries them."""
        def at(clock: str) -> datetime.datetime:
            return datetime.datetime.fromisoformat(f"2026-11-11T{clock}+00:00")
        return [dict(when=at("02:00:00"), verb="granted", count=1, label="build", pid=101, delta=1,
                     detail="waited 3m00s"),
                dict(when=at("02:00:05"), verb="granted", count=2, label="verify", pid=102, delta=2, detail="no wait"),
                dict(when=at("02:04:00"), verb="released", count=1, label="build", pid=101, delta=-1,
                     detail="exit 1, ran 4m00s"),
                dict(when=at("02:05:00"), verb="left", count=1, label="gate", pid=103, delta=0,
                     detail="gave up after 15m00s"),
                dict(when=at("02:06:00"), verb="reaped", count=2, label="verify", pid=102, delta=-2, state="held",
                     detail="owner exited, ran 5m55s"),
                dict(when=at("02:06:30"), verb="granted", count=1, label="live", pid=104, delta=1,
                     detail="waited 1m00s")]

    def draw(self, width: int, events: list[dict[str, Any]] | None = None) -> tuple[Any, list[str]]:
        tool = load_dashboard()
        found = self.events() if events is None else events
        when = found[-1]["when"]  # drawn on the log's day: times read as times on any day
        canvas = tool.Canvas(width, 12)
        tool.draw_events(canvas, Rect(0, 0, 12, width), {"events": found}, when)
        return canvas, canvas.render(False).splitlines()

    def test_the_columns_are_time_slots_label_result(self) -> None:
        for width in (156, 120, 92, 64):  # wide, the one-column layout's EVENTS beside MACHINE, narrow
            with self.subTest(width=width):
                _, lines = self.draw(width)
                header = lines[0]
                places = [header.index(title) for title in ("EVENTS", "slots", "label", "result")]
                self.assertEqual(sorted(places), places)
                for gone in (" event ", " detail ", " pid "):
                    self.assertNotIn(gone, header, "no event, detail or pid column")
                row = next(line for line in lines if " gate " in line)
                self.assertRegex(row, r"^│  \d\d:\d\d:\d\d +▫ +gate +gave up 15m  │$")
                self.assertNotIn("103", row, "no pid")
                self.assertEqual(header.index("result") + len("result"), row.index("15m") + 3, "right-aligned")

    def test_a_very_narrow_panel_drops_the_result_and_keeps_the_cells(self) -> None:
        _, lines = self.draw(44)
        self.assertEqual(["slots", "label"], [t for t in ("slots", "label", "result") if t in lines[0]])
        self.assertRegex(next(line for line in lines if " gate " in line), r"▫ +gate")

    def test_the_label_takes_the_width_the_event_and_pid_columns_left(self) -> None:
        long = dict(self.events()[0], label="lane-710-speech-paste-phase-review-fixes-and-the-follow-up-checks")
        _, lines = self.draw(92, [long])
        label = lines[0].index("label")
        self.assertEqual(label, lines[2].index("lane-710"))
        shown = lines[2][label:].split("  ")[0]
        self.assertGreaterEqual(len(shown), 92 - 6 - 10 - 5 - len("wait 3m00s") - 6, "every freed cell")
        # Before: time 10, event 8, slots 5, detail 22, pid 6 and five gaps left the label 25 of these 86 cells.
        self.assertGreater(len(shown), 25 + 8)

    def test_slot_cells_are_filled_when_taken_hollow_when_handed_back_and_never_coloured(self) -> None:
        events = self.events()
        events[1]["count"] = 20  # verify's grant: wider than the column
        canvas, lines = self.draw(120, events)
        column = lines[0].index("slots")
        expected = {("granted", "build"): "▪", ("released", "build"): "▫", ("left", "gate"): "▫",
                    ("reaped", "verify"): "▫▫", ("granted", "verify"): "▪▪▪▪ +16"}
        rows = {(event["verb"], event["label"]): 2 + index for index, event in enumerate(events[::-1])}
        for (verb, label), cells in expected.items():
            row = rows[verb, label]
            self.assertEqual(cells, lines[row][column:].split("  ")[0], (verb, label))
            styles = {canvas.cells[row][column + n][1] for n in range(len(cells))}
            self.assertEqual({"text"}, styles, f"{verb} {label}: cells in the text colour")
        for line in canvas.lines(False, ascii_only=True):
            self.assertTrue(line.isascii(), line)

    def test_each_kind_has_a_short_result(self) -> None:
        tool = load_dashboard()

        def result(verb: str, detail: str, **extra: Any) -> tuple[str, str]:
            return tool.event_result(dict(dict(verb=verb, detail=detail, count=1, delta=0), **extra))

        self.assertEqual(("wait 32s", "text"), result("granted", "waited 32s"))
        self.assertEqual(("no wait", "dim"), result("granted", "no wait"))
        self.assertEqual(("ok 3m25s", "dim"), result("released", "exit 0, ran 3m25s"))
        self.assertEqual(("exit 1 · 26s", "bad"), result("released", "exit 1, ran 26s"))
        self.assertEqual(("exit 137", "bad"), result("released", "exit 137"))
        self.assertEqual(("gave up 5m", "dim"), result("left", "gave up after 5m12s"))
        self.assertEqual(("gave up", "dim"), result("left", "never granted"))
        self.assertEqual(("owner died · 12m", "bad"), result("reaped", "owner exited, ran 12m40s", state="held"))
        self.assertEqual(("owner died", "warn"), result("reaped", "owner exited", state="waiting"))
        self.assertEqual(("waiting 30m", "warn"), result("waiting", "30m00s, est. 10:30:00"))
        self.assertEqual(("batch → interactive", "dim"), result("priority", "batch → interactive"))
        self.assertEqual(("owner died · 1h05m", "bad"), result("reaped", "owner exited, ran 1h05m", delta=-2))
        self.assertEqual(3725.0, tool.span_seconds("1h02m05s"))
        self.assertIsNone(tool.span_seconds("soon"))

    def test_results_are_in_their_colours(self) -> None:
        canvas, lines = self.draw(120)
        for text, style in (("wait 3m00s", "text"), ("exit 1 · 4m00s", "bad"), ("gave up 15m", "dim"),
                            ("owner died · 5m", "bad"), ("no wait", "dim")):
            row = next(n for n, line in enumerate(lines) if text in line)
            self.assertEqual(style, canvas.cells[row][lines[row].index(text)][1], text)


class MouseInputTests(unittest.TestCase):
    def test_click_and_wheel_follow_the_visible_rows_of_an_overflowing_table(self):
        tool = load_dashboard()
        now = datetime.datetime.now(datetime.timezone.utc)
        holders = [dict(label=f"job-{i}", kind="held", count=1, pid=100+i, style="h0",
                        cpu=0, since=now, granted=now, key=(100+i, None)) for i in range(8)]
        model = dict(holders=holders, waiters=[], slots=8, cores=8)
        scroll: dict[str, int] = {}
        def render():
            canvas = tool.Canvas(100, 7)
            canvas.scroll = scroll
            tool.draw_requests(canvas, Rect(0, 0, 7, 100), model, 0, now)
            return canvas
        canvas = render()
        self.assertEqual(1, mouse(canvas, ("mouse", 0, 20, 3, True), scroll, 0, model))
        self.assertEqual(0, mouse(canvas, ("mouse", 65, 20, 3, True), scroll, 0, model),
                         "wheel scrolls the viewport without changing selection")
        canvas = render()
        lines = canvas.render(False).splitlines()
        self.assertIn("job-3", lines[2], "rows start under the blank padding row")
        self.assertIn("job-5", lines[4], "three rows between the padding rows")
        self.assertFalse(lines[1][1:-1].strip(), "the top padding row stays blank while scrolled")
        self.assertFalse(lines[5][1:-2].strip(), "and the bottom one")
        self.assertNotIn("above", canvas.render(False))
        self.assertNotIn("more", canvas.render(False))
        self.assertEqual(3, mouse(canvas, ("mouse", 0, 20, 2, True), scroll, 0, model))
        for padding in (1, 5):
            self.assertEqual(0, mouse(canvas, ("mouse", 0, 20, padding, True), scroll, 0, model),
                             "clicking a padding row selects nothing")
        self.assertEqual(0, mouse(canvas, ("mouse", 0, 20, 0, True), scroll, 0, model),
                         "clicking the border must not select a hidden row")
        self.assertEqual(0, mouse(canvas, ("mouse", 0, 20, 1, False), scroll, 0, model))

    def test_a_delayed_mouse_release_cannot_become_the_copy_shortcut(self):
        tool = load_dashboard()
        read, write = os.pipe()
        try:
            os.write(write, b"\x1b[<0;14;10")
            self.assertEqual([], tool.read_keys(read))
            os.write(write, b"m")
            self.assertEqual([("mouse", 0, 13, 9, False)], tool.read_keys(read))
        finally:
            os.close(read)
            os.close(write)

    def test_sgr_mouse_reports_do_not_leak_into_keyboard_shortcuts(self):
        tool = load_dashboard()
        read, write = os.pipe()
        try:
            os.write(write, b"\x1b[<0;14;10M\x1b[<0;14;10m\x1b[<65;22;18Mq")
            self.assertEqual([("mouse", 0, 13, 9, True), ("mouse", 0, 13, 9, False),
                              ("mouse", 65, 21, 17, True), "q"], tool.read_keys(read))
        finally:
            os.close(read)
            os.close(write)





class DetailLayoutTests(unittest.TestCase):
    def test_waiting_facts_share_one_value_column_with_two_spaces_after_the_longest_label(self) -> None:
        tool = load_dashboard()
        moment = datetime.datetime(2026, 11, 5, 10).astimezone()
        item: dict[str, Any] = dict(label="first", kind="waiting", count=6, pid=12345,
                                   style="warn", since=moment, queue="", cmd="python -m work",
                                   tree=[], passable=True)
        model: dict[str, Any] = dict(holders=[], waiters=[item], free=4, used=2, slots=6,
                                    memory_block=None, order="window", start=None, limit=0, overtake_window=600)
        canvas = tool.Canvas(90, 24)
        tool.draw_detail(canvas, Rect(0, 0, 24, 90), model, 0, None, moment)
        lines = canvas.render(False).splitlines()
        for label, value in (("label", "first"), ("status", "waiting"), ("queued", "10:00:00"),
                             ("free", "4 of 6 slots"), ("pass until", "10:10:00"),
                             ("owner pid", "12345"), ("command", "python -m work")):
            line = next(row for row in lines if re.search(r"│  " + re.escape(label) + r"\s", row))
            self.assertEqual(15, line.index(value), line)
            self.assertTrue(line[line.index(label) + len(label):line.index(value)].startswith("  "), line)

    def test_process_table_shares_the_details_scroll_after_long_text(self):
        tool = load_dashboard()
        now = datetime.datetime.now(datetime.timezone.utc)
        item = dict(label="build", kind="held", count=2, pid=12345, style="h0", cpu=150,
                    rss=102400, procs=1, granted=now, since=now, queue="",
                    cmd="verify.sh " + "a-very-long-check-name," * 80,
                    meta="lane=build,ticket=42,phase=check," * 12,
                    tree=[(12345, 150, 102400, "worker")])
        canvas = tool.Canvas(70, 20)
        tool.draw_detail(canvas, Rect(0, 0, 20, 70), {"holders": [item], "waiters": [], "cores": 12, "slots": 6},
                         0, "/project", now)
        lines = canvas.render(False).splitlines()
        self.assertIn("LEASE DETAILS", lines[0], "a running request's lease holds slots")
        lane = next(i for i,line in enumerate(lines) if re.search(r"label\s+build", cast(str, line)))
        self.assertRegex(lines[lane + 1], r"status\s+running")
        command = next(i for i,line in enumerate(lines) if "command" in line)
        meta = next(i for i,line in enumerate(lines) if "meta" in line)
        cwd = next(i for i,line in enumerate(lines) if "cwd" in line)
        self.assertLessEqual(meta-command, 3)
        self.assertLessEqual(cwd-meta, 3)
        self.assertIn("\x1b[0m 1.5 of 4 cores", canvas.lines(True)[next(i for i, line in enumerate(lines) if re.search(r"cpu\s", line))])
        self.assertNotIn("processes", canvas.tables)
        canvas = tool.Canvas(70, 20)
        canvas.scroll = {"details": 1000}
        tool.draw_detail(canvas, Rect(0, 0, 20, 70), {"holders": [item], "waiters": [], "cores": 12, "slots": 6},
                         0, "/project", now)
        lines = canvas.render(False).splitlines()
        header = next(line for line in lines if "process" in line and "cpu" in line)
        self.assertEqual(3, header.index("process"))
        self.assertNotIn("processes", header)
        self.assertLess(header.index("process"), header.index("cpu"))
        self.assertLess(header.index("memory"), header.index("pid"))
        row = next(line for line in lines if "worker" in line)
        self.assertEqual(3, row.index("worker"))
        self.assertTrue(row.rstrip(" │┃").endswith("12345"))
        item["tree"] = [(1000+i, 0, 1024, f"worker{i}") for i in range(10)]
        canvas = tool.Canvas(70, 20)
        canvas.scroll = {"details": 1000}
        tool.draw_detail(canvas, Rect(0, 0, 20, 70), {"holders": [item], "waiters": [], "cores": 12, "slots": 6},
                         0, "/project", now)
        text = canvas.render(False)
        self.assertIn("worker9", text)
        self.assertNotIn("above", text, "the scrollbar shows overflow, no row counts")
        self.assertNotIn("more", text)



class QueuePolicyTests(unittest.TestCase):
    def test_backfill_policy_is_structured_as_deadlines_not_a_sentence(self):
        tool = load_dashboard()
        since = datetime.datetime(2026, 11, 5, 10).astimezone()
        model = dict(holders=[], waiters=[dict(count=3, since=since, passable=False, label="head", pid=100,
                     kind="waiting",style="warn",cmd="work",queue="",tree=[])], free=1,used=1,slots=4,memory_block=None,
                     order="backfill", start=since+datetime.timedelta(minutes=30), limit=1200)
        self.assertEqual({"start est": "10:30:00", "pass until": "10:20:00"},
                         tool.queue_policy(model, since))
        canvas = tool.Canvas(100,24)
        tool.draw_detail(canvas, Rect(0, 0, 24, 100),model,0,None,since)
        lines = canvas.render(False).splitlines()
        for title, value in (("start est", "10:30:00"), ("pass until", "10:20:00")):
            line=next(line for line in lines if title in line)
            self.assertRegex(line,re.escape(title)+r"\s+"+re.escape(value))
        model["waiters"] = []
        self.assertEqual({}, tool.queue_policy(model, since))


class SlotBarTests(unittest.TestCase):
    def test_free_label_is_right_aligned_and_leaves_room_for_the_last_owner(self):
        tool = surfaces(load_dashboard())
        canvas = tool.Canvas(120, 3)
        tool.draw_slots(canvas, Rect(0, 2, 2, 116), dict(slots=6, used=1, waiters=[],
                        holders=[dict(count=1, kind="held", style="h0", label="lane-624-flows-are-graphs")]))
        lines = canvas.render(False).splitlines()
        self.assertIn("lane-624-flows-are-graphs", lines[1])
        self.assertEqual(lines[0].rindex("🮅") + 1, lines[1].index("free") + 4)

    def test_each_slot_is_solid_with_only_a_gap_between_slots(self):
        tool = surfaces(load_dashboard())
        canvas = tool.Canvas(120, 3)
        tool.draw_slots(canvas, Rect(0, 2, 2, 116), dict(slots=3, used=2, waiters=[dict(count=3)],
                        holders=[dict(count=2, kind="held", style="h0", label="build")]))
        line = canvas.render(False).splitlines()[0]
        self.assertEqual("🮅" * 38 + "  " + "🮅" * 37 + "  " + "🮅" * 37, line[2:118])
        self.assertNotIn("▉", line)

    def test_blocks_are_in_the_holders_fill_and_its_label_in_its_ink(self):
        tool = surfaces(load_dashboard())
        canvas = tool.Canvas(120, 3)
        tool.draw_slots(canvas, Rect(0, 2, 2, 116), dict(slots=3, used=2, waiters=[],
                        holders=[dict(count=2, kind="held", style="h3", label="build")]))
        self.assertEqual({"f3"}, {cell[1] for cell in canvas.cells[0] if cell[0] == "🮅"} - {"free"})
        self.assertEqual({"h3"}, {cell[1] for cell in canvas.cells[1][2:7]})
        self.assertIn("f3", tool.current_theme.styles, "two tokens (on a dark background a fill that reads is its ink)")


class MachineStatusTests(unittest.TestCase):


    def test_the_status_moves_into_the_top_border_and_each_row_has_its_health_mark(self):
        tool=load_dashboard()
        now=datetime.datetime.now(datetime.timezone.utc)
        for level,free,load,admission,marks in ((1,800,4,2,("🆗","🆗","🆗")),
                                               (2,800,4,0,("🆗","🔥","🆗")),
                                               (1,128,11.5,0,("🔥","🆗","🔥"))):
            with self.subTest(level=level,free=free,load=load):
                canvas=tool.Canvas(110,24)
                model=dict(slots=6,cores=12,averages=(load,4,4),admission=admission,
                           advice=dict(unleased_load=4.5,jobs=1),memory_block=None,
                           memory=judged(dict(level=level,used=768,total=1024,swap_total=1024,swap_free=free)),
                           history=[(now-datetime.timedelta(hours=1),now,1)])
                tool.draw_machine(canvas, Rect(0, 0, 24, 110),model)
                lines=canvas.lines(False)
                permit=f"admission: {admission} slots" if admission else "no admission"
                self.assertTrue(lines[0].rstrip().endswith(f" unleased 4.5 · {permit} ─╮"),lines[0])
                self.assertTrue("".join(cell[0] for cell in canvas.cells[1][1:-1]).strip().startswith("slots held"),
                                "a blank row first but for the chart's title")
                for row,(name,mark) in enumerate(zip(("load","mem","swap"),marks),2):
                    self.assertEqual(mark,canvas.cells[row][3][0],f"{name}: under the blank row")
                    self.assertEqual(name,"".join(cell[0] for cell in canvas.cells[row][6:6+len(name)]))
                self.assertNotIn("memory ok",canvas.render(False),"no status row")
                coloured=canvas.lines(True)[0]
                self.assertIn("\x1b[0munleased 4.5",coloured,"in the text colour")
                self.assertIn(tool.sgr("bad" if not admission else "status-normal",False)+permit,coloured,
                              "red when it blocks")
                ascii_canvas=tool.Canvas(110,24,marks=tool.marks_for(True))
                tool.draw_machine(ascii_canvas, Rect(0, 0, 24, 110),model)
                plain=ascii_canvas.lines(False,ascii_only=True)
                self.assertEqual("".join("!" if mark=="🔥" else "+" for mark in marks),
                                 "".join(plain[row][3] for row in (2,3,4)),"ASCII: + and !")

    def test_a_narrow_border_keeps_the_admission_and_drops_the_unleased_load(self):
        tool=load_dashboard()
        model=dict(slots=6,cores=12,averages=(4,4,4),admission=0,advice=dict(unleased_load=4.5,jobs=1),
                   memory_block=None,memory=judged(dict(level=1,used=768,total=1024,swap_total=1024,swap_free=800)),history=[])
        for width,shown in ((60,"unleased 4.5 · no admission"),(34,"no admission"),(24,None)):
            with self.subTest(width=width):
                canvas=tool.Canvas(width,13)
                tool.draw_machine(canvas, Rect(0, 0, 13, width),model)
                top=canvas.lines(False)[0]
                self.assertTrue(top.startswith("╭─ MACHINE "))
                if shown:
                    self.assertTrue(top.endswith(f" {shown} ─╮"),top)
                    if shown=="no admission":
                        self.assertNotIn("unleased",top)
                else:
                    self.assertNotIn("admission",top)


class MemoryBarTests(unittest.TestCase):
    def test_machine_has_usage_bars_and_a_chart_spanning_the_inner_width(self):
        tool = load_dashboard()
        now = datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.timezone.utc)
        self.assertFalse(tool.machine_side_by_side(60), "60 columns: the chart under the rows")
        canvas = tool.Canvas(60, tool.MACHINE_HEIGHT)
        tool.draw_machine(canvas, Rect(0, 0, tool.MACHINE_HEIGHT, 60), dict(slots=4, cores=12, averages=(1,2,3),
                          advice=dict(unleased_load=0, jobs=2), memory_block=None,
                          memory=judged(dict(level=1, used=768, total=1024, swap_total=512, swap_free=128)),
                          history=[(now-datetime.timedelta(hours=1), now, 4)]))
        lines = canvas.render(False).splitlines()
        self.assertEqual(11, len(lines))
        self.assertFalse(lines[1][1:-1].strip(), "a blank row under the top border")
        self.assertIn("load", lines[2], "then the rows")
        self.assertIn("swap", lines[4])
        text = "\n".join(lines)
        self.assertRegex(lines[2], r"🆗 load ■+░* +1\.0/12  │")
        self.assertRegex(lines[3], r"🆗 mem  ■+░* +normal  │", "macOS: the pressure level, a third of the gauge")
        self.assertRegex(lines[4], r"swap ■+░* +0\.4/0\.5G  │")
        self.assertNotIn("used ", text)
        self.assertNotIn("cores", text)
        self.assertNotIn("pressure", text)
        self.assertEqual(["▄" * 54, "█" * 54, "█" * 54, "▀" * 54], [line[3:57] for line in lines[5:9]],
                         "four rows right under swap: a full column tops out at ▄, half a cell of air each end")
        chart = lines[5]
        self.assertEqual("  ", chart[1:3])
        self.assertEqual("  ", chart[57:59])
        self.assertRegex(lines[-2], r"^│  −\d+[smh] +avg 4\.0/4 · peak 4 · full 100% +now  │$")
        self.assertEqual(11, tool.machine_height(60))
        self.assertIn("╰", lines[-1], "the bottom frame right after the legend")


    def test_the_chart_says_it_shows_slots_held_without_a_row_of_its_own(self):
        # fathers#140: beside the gauges its title and figures sit in the blank row above it and its axis line keeps
        # only the window and now, so neither its rows nor its figures line up with the gauges'; stacked, the bottom
        # border under the axis names it and the axis keeps every figure
        tool=surfaces(load_dashboard())
        for width,height,kept in ((62,7,("avg",)),(90,7,("avg","peak","full"))):
            with self.subTest(width=width):
                lines=self.machine(tool,height,width=width)
                start=lines[-2].index("−")  # the chart's first column, where its axis starts
                self.assertTrue(lines[1][start:].startswith("slots held  avg 4.0/4"),lines[1])
                for word in ("avg","peak","full"):
                    self.assertEqual(word in kept,f"{word} " in lines[1],lines[1])
                    self.assertNotIn(f"{word} ",lines[-2],"no figure under the gauges' values")
                self.assertRegex(lines[-2],r"^│ +−\d+[smh] +now  │$")
        lines=self.machine(tool,11,width=60)
        self.assertNotIn("slots held","".join(lines[:-1]),"stacked: no row for a title")
        self.assertRegex(lines[-2],r"^│  −\d+[smh] +avg [\d.]+/4 · peak 4 · full \d+% +now  │$","every figure kept")
        self.assertTrue(lines[-1].startswith("╰─ slots held ─"),lines[-1])
        self.assertEqual(lines[-2].index("−"),lines[-1].index("slots"),"under the axis' first column")
        bare="".join(text for text,_ in tool.machine_legend(dict(average=0.65,peak=8,full=0.2),8,30,stats=False))
        self.assertRegex(bare,r"^−\S+ +now$")

    def test_the_legend_is_a_time_axis_with_the_stats_in_slots_between(self):
        tool=surfaces(load_dashboard())
        summary=dict(average=0.65,peak=8,full=0.2)
        for width,kept in ((120,("avg","peak","full")),(80,("avg","peak","full")),(60,("avg","peak","full")),
                           (34,("avg","peak")),(22,("avg",)),(15,())):
            with self.subTest(width=width):
                parts=tool.machine_legend(summary,8,width)
                line="".join(text for text,_ in parts)
                self.assertEqual(width,tool.text_width(line),"exactly under the chart")
                self.assertTrue(line.startswith("−") and line.endswith("now"),line)
                for word in ("avg","peak","full"):
                    self.assertEqual(word in kept,f"{word} " in line,line)
                if kept:
                    self.assertIn("avg 5.2/8",line,"slots, one decimal, of the total")
                    stats=line.index("avg")
                    end=line.rindex("%" if "full" in kept else "8" if "peak" in kept else "/8")+1
                    left,right=stats-line.index(" "),line.index("now")-end
                    self.assertLessEqual(abs(left-right),1,"centred between the axis labels")
                styles={text.strip():style for text,style in parts if text.strip()}
                self.assertEqual("dim",styles["now"])
                if kept:
                    self.assertEqual(("dim","text"),(styles["avg"],styles["5.2/8"]),"labels muted, values in the text colour")
        for seconds,text in ((45,"−45s"),(61,"−60s"),(900,"−15m"),(3540,"−60m"),(3600,"−60m"),(7300,"−2h"),(400,"−7m")):
            self.assertEqual(text,tool.window_text(seconds),seconds)
        self.assertTrue(tool.machine_legend(summary,8,84)[0][0].startswith("−60m"),"84 columns of 43 s: an hour")

    def test_memory_bar_colour_reflects_pressure_without_a_separate_pressure_row(self):
        tool = load_dashboard()
        now = datetime.datetime.now(datetime.timezone.utc)
        for level, colour in ((1,"38;2;%d;%d;%d"%tool.current_theme.styles["chart0"][0]), (2,tool.sgr("bad",False)[4:-1]), (4,tool.sgr("bad",False)[4:-1])):
            # the ramp's blue at the bar's start; under pressure (memory low) the theme's red, the whole bar
            with self.subTest(level=level):
                canvas = tool.Canvas(90,24)
                tool.draw_machine(canvas, Rect(0, 0, 24, 90),dict(slots=4,cores=12,averages=(1,2,3),
                                  advice=dict(unleased_load=0,jobs=2),memory_block=None,
                                  memory=judged(dict(level=level,used=768,total=1024,swap_total=512,swap_free=128)),
                                  history=[(now-datetime.timedelta(hours=1),now,4)]))
                row = next(i for i,line in enumerate(canvas.lines(False)) if " mem " in line)
                self.assertIn(f"\x1b[0;{colour}m■", canvas.lines(True)[row])
                self.assertNotIn("pressure", canvas.render(False))

    def test_full_capacity_is_the_ramps_red_end_in_the_shortest_chart(self):
        tool=load_dashboard()
        now=datetime.datetime.now(datetime.timezone.utc)
        canvas=tool.Canvas(60,9)
        tool.draw_machine(canvas, Rect(0, 0, 9, 60),dict(slots=4,cores=12,averages=(1,2,3),
                          advice=dict(unleased_load=0,jobs=2),memory_block=None,
                          memory=judged(dict(level=1,used=768,total=1024,swap_total=512,swap_free=128)),
                          history=[(now-datetime.timedelta(hours=1),now,4)]))
        rows=[i for i,line in enumerate(canvas.lines(False)) if "▄" in line or "▀" in line]
        self.assertEqual(2,len(rows),"two rows: never fewer, one cell of span")
        red=tool.current_theme.styles["chart7"][0]  # the ramp's red end: a full column's top, not the warning red
        self.assertIn("38;2;%d;%d;%dm▄"%red,canvas.lines(True)[rows[0]])

    def machine(self, tool: Any, height: int, width: int = 60, memory: dict[str, Any] | None = None,
                load: float = 3, marks: Any = None) -> list[str]:
        now=datetime.datetime.now(datetime.timezone.utc)
        canvas=tool.Canvas(width,height,marks=marks)
        tool.draw_machine(canvas, Rect(0, 0, height, width),dict(slots=4,cores=12,averages=(load,2,3),
                          advice=dict(unleased_load=0,jobs=2),memory_block=None,admission=1,
                          memory=judged(memory or dict(level=1,used=24678,total=32768,swap_total=9216,swap_free=1434)),
                          history=[(now-datetime.timedelta(hours=1),now,4)]))
        self.canvas=canvas
        return canvas.lines(False)

    def test_the_chart_gives_up_rows_first_four_three_two_then_none(self):
        tool=load_dashboard()
        for height,chart in ((11,4),(10,3),(9,2),(8,0),(6,0),(5,0)):
            with self.subTest(height=height):
                lines=self.machine(tool,height)
                rows=sum(1 for line in lines if any(glyph in line for glyph in tool.SPARK+"▀"))
                self.assertEqual(chart,rows)
                self.assertEqual(chart>0,any("avg" in line for line in lines),"the axis line goes with the chart")
                for name in ("load","mem","swap"):
                    self.assertTrue(any(f" {name} " in line for line in lines),f"{name}: rows before the chart goes")
                padded=height>=7
                self.assertEqual(padded,not lines[1][1:-1].strip(),"the blank rows go after the chart, before the rows")
                self.assertIn("🆗 load ",lines[2 if padded else 1])
                swap=next(i for i,line in enumerate(lines) if " swap " in line)
                if chart==0 and height>=6:  # no chart: a blank row under swap too, never swap on the border
                    self.assertFalse(lines[swap+1][1:-1].strip(),f"height {height}: a blank row under swap")
                    self.assertTrue(lines[height-1].startswith("╰"))

    def test_short_labels_values_in_one_column_and_bars_of_one_length(self):
        tool=load_dashboard()
        memory=judged(dict(level=None,available=9216,total=32768,swap_total=11264,swap_free=1024))  # Linux: MemAvailable
        for width in (60,90):
            with self.subTest(width=width):
                lines=self.machine(tool,13,width=width,memory=memory,load=10.1)
                rows=lines[2:5]
                self.assertEqual(["load","mem","swap"],[row.split()[2] for row in rows])
                values=("10.1/12","23.0/32G","10.0/11G")
                for row,value in zip(rows,values):
                    self.assertIn(f" {value}",row)
                ends={row.index(value)+len(value) for row,value in zip(rows,values)}
                self.assertEqual(1,len(ends),"the values right-aligned in one column")
                bars={sum(1 for char in row if char in "■░") for row in rows}
                self.assertEqual(1,len(bars),"every bar the same length")
                self.assertNotIn("cores","\n".join(lines))
        resources=tool.machine_resources(dict(cores=12,slots=4,averages=(10.1,1,1),memory=judged(memory)))
        tight=tool.Canvas(19,3)
        tool.draw_resource_rows(tight, Rect(0, 0, 3, 19),resources)
        self.assertEqual(["10.1/12","23/32G","10/11G"],[line.split()[-1] for line in tight.render(False).splitlines()],
                         "tight: memory and swap without decimals")

    def test_wide_machine_puts_the_chart_beside_the_rows(self):
        tool=load_dashboard()
        self.assertEqual(62,tool.MACHINE_SIDE_WIDTH)
        for width,side in ((tool.MACHINE_SIDE_WIDTH,True),(90,True),(tool.MACHINE_SIDE_WIDTH-1,False)):
            with self.subTest(width=width):
                height=tool.machine_height(width)
                self.assertEqual(7 if side else 11,height)
                lines=self.machine(tool,height,width=width)
                self.assertEqual(height,len(lines))
                if side:
                    rows=[lines[row] for row in (2,3,4)]
                    self.assertTrue(all(any(glyph in row for glyph in "▄█▀") for row in rows),"the chart level with the rows")
                    start=min(row.index(glyph) for row in rows for glyph in "▄█▀" if glyph in row)
                    self.assertTrue(all(row[start-2:start]=="  " for row in rows),"a gap of two cells")
                    axis=lines[5]
                    self.assertTrue(axis[3:start].strip()=="","the axis under the chart alone")
                    self.assertTrue(axis[start:].lstrip().startswith("−") and "now" in axis)
                else:
                    self.assertTrue(any("now" in line for line in lines[5:]))
                    self.assertFalse(any(glyph in lines[3] for glyph in "▄█▀"),"stacked: the chart under the rows")
        tight=self.machine(tool,6,width=90)  # no room for the axis beside: the stacked shrink order
        self.assertFalse(any("now" in line for line in tight))

    def test_the_layouts_give_the_shorter_machine_height_to_the_panels_above(self):
        tool=load_dashboard()
        for width,height in ((156,43),(176,50),(100,50)):
            with self.subTest(width=width):
                canvas=tool.Canvas(width,height)
                tool.dashboard_draw(canvas,self.layout_model())
                lines=canvas.render(False).splitlines()
                top=next(y for y,line in enumerate(lines) if "╭─ MACHINE " in line)
                left=lines[top].index("╭─ MACHINE ")
                right=lines[top].index("╮",left)
                bottom=next(y for y in range(top+1,height) if lines[y][left:].startswith("╰"))
                self.assertEqual(tool.machine_height(right-left+1),bottom-top+1,"MACHINE at its own height")
                details=canvas.tables["details"]
                self.assertEqual(top,details["y"]+details["height"],"the details take everything above it")

    def test_mem_shows_what_admission_reads(self):
        tool=load_dashboard()
        for level,word,third,mark in ((1,"normal",1,"🆗"),(2,"warning",2,"🔥"),(4,"critical",3,"🔥")):
            with self.subTest(level=level):
                lines=self.machine(tool,11,width=60,memory=judged(dict(level=level,used=32000,total=32768,swap_total=0,
                                                                swap_free=None)))
                row=lines[3]
                self.assertRegex(row,rf"{mark} mem  ■+░* +{word}  │","the level's word, not used RAM")
                bar=sum(1 for char in row if char=="■")+sum(1 for char in row if char=="░")
                self.assertEqual(round(bar*third/3),sum(1 for char in row if char=="■"),"a three-step gauge")
                self.assertNotIn("/32G",row)
        linux=self.machine(tool,11,width=60,memory=judged(dict(level=None,available=8192,total=32768,psi_some=1.0,
                                                         psi_full=0.0,swap_total=0,swap_free=None)))
        self.assertRegex(linux[3],r"🆗 mem  ■+░* +24\.0/32G  │","Linux: MemAvailable-based used/total")
        held=self.machine(tool,11,width=60,memory=judged(dict(level=None,available=8192,total=32768,psi_some=15.0,
                                                        psi_full=0.0,swap_total=0,swap_free=None)))
        self.assertRegex(held[3],r"🔥 mem  ■+░* +psi 15%  │","the PSI that holds admission back")
        self.assertTrue(any("not used RAM" in "".join(text for text,_ in line) for line in tool.help_lines(70)))

    def test_a_long_queue_scrolls_and_the_details_keep_their_floor(self):
        tool=load_dashboard()
        model=self.layout_model()
        model["holders"]=[dict(model["holders"][0],label=f"job-{n}",pid=100+n,key=(100+n,None)) for n in range(17)]
        model["slots"],model["used"],model["free"]=17,17,0
        for width,height in ((100,50),(100,40),(156,43),(176,50)):
            with self.subTest(width=width,height=height):
                canvas=tool.Canvas(width,height)
                tool.dashboard_draw(canvas,model)
                details,queue=canvas.tables["details"],canvas.tables["requests"]
                self.assertGreaterEqual(details["height"],tool.DETAILS_FLOOR,"the details keep 9 rows and more")
                self.assertGreaterEqual(queue["height"],tool.QUEUE_FLOOR)
                if width<tool.WIDE:  # one column: QUEUE scrolls instead of growing into the details
                    self.assertLess(queue["visible"],queue["count"])
                else:  # wide: the details have their own column, the whole right side above MACHINE
                    self.assertGreater(details["height"],20)
        self.assertEqual(dict(requests=13,details=5,machine=6),{name:rect.height for name,rect in
                         tool.overview(100,30,17,tool.machine_height).panels.items() if name!="slots" and name!="events"},
                         "too short for the floors: the earlier order")

    def layout_model(self) -> dict[str, Any]:
        now=datetime.datetime.now(datetime.timezone.utc)
        holders=[dict(label="build",kind="held",count=1,pid=10,style="h0",cpu=50,since=now,granted=now,rss=1024,procs=1,
                      queue="",cmd="work",tree=[],key=(10,None),priority="normal",ticket=None,meta="",context={},
                      workdir="",yieldable=False,yields=0,asked_at=None,passable=False,all=False)]
        return dict(holders=holders,waiters=[],slots=4,cores=12,saved=False,averages=(1.0,1.0,1.0),
                    advice=dict(unleased_load=0,jobs=4,capacity=4),admission=4,memory_block=None,
                    memory=judged(dict(level=1,used=512,total=1024,swap_total=0,swap_free=None)),history=[],events=[],
                    updated=now,order="backfill",start=None,limit=2700,used=1,free=3)

    def test_the_chart_keeps_half_a_cell_of_air_at_both_ends(self):
        tool=load_dashboard()
        # four rows, eight slots: the span is 3 cells (24 eighths) from the bottom cell's mid-line to the top's
        # level 2 is 6 eighths above the mid-line: the bottom cell's upper half (4) and 2 of the next row; level 4
        # is 12, the next row full; level 8 is 24, half of the top row (▄); more than capacity caps there
        for level,glyphs in ((0,"    "),(1,"   ▀"),(2,"  ▂▀"),(3,"  ▅▀"),(4,"  █▀"),(5," ▃█▀"),(6," ▆█▀"),
                             (7,"▁██▀"),(8,"▄██▀"),(12,"▄██▀")):
            with self.subTest(level=level):
                self.assertEqual(list(glyphs),[row[0] for row in tool.chart_cells([level],8,4)])
        two=[row[0] for row in tool.chart_cells([4],4,2)]
        self.assertEqual(["▄","▀"],two,"two rows: one cell of span, a full column at ▄ over ▀")
        self.assertEqual([" ","▀"],[row[0] for row in tool.chart_cells([2],4,2)],"half: the bottom half cell")
        self.assertEqual([" ","▄"],[row[0] for row in tool.chart_cells([1],4,2,half_gaps=False)],
                         "without the half gaps, full rows as before")

    def test_short_of_rows_the_resources_share_one_line_and_never_drop_memory_or_swap(self):
        tool=load_dashboard()
        for height in (4,3):
            with self.subTest(height=height):
                lines=self.machine(tool,height,width=90)
                compact=next(line for line in lines if "load " in line)
                self.assertRegex(compact,r"🆗 load ■\S* 3\.0/12 · 🆗 mem ■\S* normal · 🆗 swap ■\S* 7\.6/9G")
                self.assertTrue(" mem " in compact and " swap " in compact,"one line, not rows")
        self.machine(tool,3,width=90,marks=tool.marks_for(True))
        self.assertRegex(next(line for line in self.canvas.lines(False,ascii_only=True) if "load " in line),
                         r"\+ load #\S* 3\.0/12 \. \+ mem #\S* normal \. \+ swap #\S* 7\.6/9G")
        narrow=self.machine(tool,3,width=56)
        line=next(line for line in narrow if "load " in line)
        self.assertRegex(line,r"🆗 load 3\.0/12 (· )?🆗 mem normal (· )?🆗 swap 7\.6/9G","too narrow for bars: the numbers")
        self.assertNotIn("■",line)
        narrowest=next(line for line in self.machine(tool,3,width=tool.NARROW_MACHINE) if "load " in line)
        self.assertRegex(narrowest,r"load 3\.0/12 mem normal swap 7\.6/9G","the narrowest drops the marks")
        for width in (90,60,tool.NARROW_MACHINE):  # never narrower in any layout
            for height in range(3,14):
                with self.subTest(width=width,height=height):
                    text="\n".join(self.machine(tool,height,width=width))
                    self.assertRegex(text,r"mem(ory)?\b","memory shown")
                    self.assertIn("swap",text)

    def test_critical_values_are_red_in_the_compact_forms(self):
        tool=load_dashboard()
        hot=dict(level=4,used=32000,total=32768,swap_total=9216,swap_free=10)  # memory low, swap low
        for width,red in ((90,"bar"),(56,"value")):
            with self.subTest(width=width):
                lines=self.machine(tool,3,width=width,memory=hot,load=12)
                y=next(i for i,line in enumerate(lines) if "load " in line)
                cells=self.canvas.cells[y]
                text="".join(cell[0] or "\0" for cell in cells)  # one character a cell: placeholders kept
                for label in ("load","mem","swap"):
                    at=text.index(label+" ")
                    styles={cell[1] for cell in cells[at+len(label)+1:at+len(label)+7] if cell[0] not in (" ","")}
                    self.assertIn("chart7" if red=="bar" else "bad",styles,f"{label}: critical in red ({red})")
                    self.assertEqual("dim",cells[at][1],"labels muted")
                    self.assertEqual("🔥",cells[at-3][0],"the critical mark before it")

    def test_resource_bars_follow_the_chart_ramp_and_turn_red_when_critical(self):
        tool=surfaces(load_dashboard())
        styles=[style for text,style in tool.ramp_bar(1,12,False,"■") for _ in text if style!="track"]
        self.assertEqual(12,len(styles))
        self.assertEqual(("chart0","chart7"),(styles[0],styles[-1]),"blue at the start, red at the tip of a full bar")
        self.assertEqual([f"chart{round(k/11*7)}" for k in range(12)],styles,"each cell at its place on the ramp")
        half=[style for text,style in tool.ramp_bar(0.5,12,False,"■") if style!="track" for _ in text]
        self.assertEqual(["chart0","chart1","chart1","chart2","chart3","chart3"],half,"half full runs blue to mid")
        self.assertEqual({"chart7"},{style for _,style in tool.ramp_bar(0.5,12,True,"■") if style!="track"},
                         "critical: all the ramp's red, the red a full bar ends on")
        now=datetime.datetime.now(datetime.timezone.utc)
        def machine(**extra: Any) -> Any:
            model=dict(slots=4,cores=12,averages=(20.3,20,20),admission=0,advice=dict(unleased_load=20,jobs=1,capacity=0),
                       memory_block=None,memory=judged(dict(level=1,used=512,total=1024,swap_total=1024,swap_free=100)),
                       history=[(now-datetime.timedelta(minutes=30),now-datetime.timedelta(minutes=10),2),
                                (now-datetime.timedelta(minutes=10),now,4)])
            model.update(extra)
            canvas=tool.Canvas(90,24)
            tool.draw_machine(canvas, Rect(0, 0, 24, 90),model)
            return canvas
        canvas=machine()
        def bar_styles(name: str) -> set[str]:
            row=next(i for i,line in enumerate(canvas.lines(False)) if f" {name} " in line)
            return {cell[1] for cell in canvas.cells[row][11:23] if cell[0]=="■" and cell[1]!="track"}
        self.assertEqual({"chart7"},bar_styles("load"),"20.3 on 12 cores leaves no room: all red")
        self.assertEqual({"chart7"},bar_styles("swap"),"swap low: all red")
        self.assertTrue(all(style.startswith("chart") for style in bar_styles("mem")),"memory ok: the ramp")
        chart=[i for i,line in enumerate(canvas.lines(False)) if any(glyph in line for glyph in "▄█▀")]
        columns=[x for x in range(90) if canvas.cells[chart[0]][x][0]=="▄"]  # the columns that reached every slot
        self.assertTrue(columns)
        self.assertEqual(["▄","█","█","▀"],[canvas.cells[row][columns[0]][0] for row in chart],"half cells at the ends")
        for step,row in enumerate(chart):  # a full column keeps the height ramp: red only at its top (owner)
            self.assertEqual({f"chart{round((len(chart)-1-step)/(len(chart)-1)*7)}"},
                             {canvas.cells[row][x][1] for x in columns})
        calm=machine(averages=(3,3,3),advice=dict(unleased_load=0,jobs=2,capacity=4),
                     memory=judged(dict(level=1,used=512,total=1024,swap_total=1024,swap_free=900)))
        canvas=calm
        self.assertTrue(all(style.startswith("chart") for style in bar_styles("load")))
        self.assertTrue(all(line.isascii() for line in calm.lines(False,ascii_only=True)),"ASCII: # and the rest")

    def test_the_load_bar_is_critical_by_load_alone_not_by_held_slots(self):
        tool=surfaces(load_dashboard())
        now=datetime.datetime.now(datetime.timezone.utc)
        for load,critical in ((0.5,False),(6.5,False),(7.5,True),(20.3,True)):
            with self.subTest(load=load):
                canvas=tool.Canvas(90,24)  # every slot held: the advice has no capacity, whatever the load
                tool.draw_machine(canvas, Rect(0, 0, 24, 90),dict(slots=8,cores=8,averages=(load,0.1,0.1),admission=0,
                                  advice=dict(unleased_load=0,jobs=1,capacity=0),memory_block=None,
                                  memory=judged(dict(level=1,used=512,total=1024,swap_total=0,swap_free=None)),
                                  history=[(now-datetime.timedelta(hours=1),now,8)]))
                row=next(i for i,line in enumerate(canvas.lines(False)) if " load " in line)
                styles={cell[1] for cell in canvas.cells[row][11:23] if cell[0]=="■" and cell[1]!="track"}
                self.assertEqual(critical,styles=={"chart7"},f"load {load} on 8 cores, 8 slots: {styles}")



class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tool = load_dashboard()

    def test_oldest_clock_bucket_retains_its_peak_until_the_next_scroll(self):
        seconds = int(datetime.datetime(2026,1,1,12,tzinfo=datetime.timezone.utc).timestamp())
        edge = datetime.datetime.fromtimestamp(seconds // 43 * 43, datetime.timezone.utc)
        oldest = edge-datetime.timedelta(seconds=3612)
        events = [dict(when=oldest+datetime.timedelta(seconds=5),delta=4),
                  dict(when=oldest+datetime.timedelta(seconds=6),delta=-4)]
        earlier = self.tool.held_history(events,0,edge-datetime.timedelta(seconds=10))
        later = self.tool.held_history(events,0,edge-datetime.timedelta(seconds=5))
        self.assertEqual(self.tool.history_buckets(earlier,84),self.tool.history_buckets(later,84))
        self.assertEqual(4,self.tool.history_buckets(later,84)[0])
        self.assertEqual(0,self.tool.history_summary(later,4)["peak"],
                         "the trailing-hour summary must exclude the older retained chart peak")

    def test_a_historical_peak_keeps_its_bucket_between_clock_boundaries(self):
        base = datetime.datetime(2026, 1, 1, 10, tzinfo=datetime.timezone.utc)
        rise = base + datetime.timedelta(seconds=601.2)
        fall = base + datetime.timedelta(seconds=601.4)
        def buckets(seconds):
            now = base + datetime.timedelta(seconds=seconds)
            steps = [(now - datetime.timedelta(hours=1), rise, 0),
                     (rise, fall, 4), (fall, now, 0)]
            return self.tool.history_buckets(steps, 6)
        self.assertEqual(buckets(3601), buckets(3602),
                         "refreshing within one time bucket must not move or widen a past peak")
        self.assertEqual([4, 0, 0, 0, 0, 0], buckets(3601))



if __name__ == "__main__":
    unittest.main()
