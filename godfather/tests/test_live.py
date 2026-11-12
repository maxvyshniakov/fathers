"""The live view (#15 batch): details load in the background, panels take keyboard focus and scroll, EVENTS stays
put while scrolled, the footer's version opens the changelog."""
from __future__ import annotations

import helpers  # noqa: F401 - src/ on sys.path
import view as _view
from helpers import ROOT

import dataclasses
import datetime
import os
import re
import select
import threading
import time
import unittest
from typing import Any, Callable, Protocol, cast

from godfather import theme
from helpers import drawn_state
from godfather.layout import Rect

def judged(memory: dict[str, Any]) -> dict[str, Any]:
    """Memory facts with Slotfather's judgement of them, as its snapshot carries it (docs/contracts/slotfather.md):
    low above pressure level 1, swap low under 512 MB free."""
    level, free = memory.get("level"), memory.get("swap_free")
    return dict(memory, low=None if level is None else level > 1,
                swap_low=bool(memory.get("swap_total")) and free is not None and free < 512)



DARK = {"SLOTFATHER_TERMINAL_BG": "#282c34", "SLOTFATHER_TERMINAL_FG": "#ffffff"}
PLAIN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class Receive(Protocol):
    def __call__(self, check: Callable[[str], object], seconds: float = 8, what: str = "") -> str: ...


def tool() -> Any:
    found: Any = _view.fresh()
    found.set_theme(theme.resolve("auto", environ=DARK))
    return found


def event(n: int) -> dict[str, Any]:
    when = datetime.datetime(2026, 11, 11, 12, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=n)
    return {"when": when, "verb": "granted", "count": 1, "label": f"job{n}", "pid": 1000 + n, "delta": 1}


def frames_of(view: Any, canvas: Any) -> None:
    """QUEUE and DETAILS side by side over EVENTS, each in a shared Panel frame."""
    view.Panel(canvas, Rect(0, 0, 10, 40), "requests").frame("QUEUE")
    view.Panel(canvas, Rect(0, 40, 10, 40), "details").frame("DETAILS")
    view.Panel(canvas, Rect(10, 0, 10, 80), "events").frame("EVENTS")


def tabs(canvas: Any, key: str = "\t", presses: int = 3) -> list[str]:
    """The focus from QUEUE after each Tab (or Shift-Tab) through the reducer, over the frame `canvas` drew."""
    view = _view.current()
    state = drawn_state(canvas)
    order = [state.focus]
    for _ in range(presses):
        state, _ = view.reduce(state, view.Key(key), 0.0)
        order.append(state.focus)
    return order


class LoaderTests(unittest.TestCase):
    def test_the_newest_request_supersedes_one_that_waits(self) -> None:
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        gate, seen = threading.Event(), cast(list[str], [])

        def work(argument: str) -> str:
            seen.append(argument)
            if argument == "a":
                gate.wait(5)
            return argument.upper()

        tool_ = tool()
        loader = tool_.Loader(work, write)
        loader.request("a", "a")
        time.sleep(0.05)
        loader.request("b", "b")  # waits behind a
        loader.request("c", "c")  # replaces b: quick moves load only the row they end on
        self.assertTrue(loader.wants("c"))
        self.assertFalse(loader.wants("b"))
        gate.set()
        deadline = time.monotonic() + 5
        results: list[Any] = []
        while len(results) < 2 and time.monotonic() < deadline:
            results += loader.take()
            time.sleep(0.01)
        self.assertEqual(["a", "c"], seen)
        self.assertEqual([("a", "A"), ("c", "C")], [(key, value) for key, value, _ in results])
        self.assertTrue(select.select([read], [], [], 0)[0], "each result wakes the main loop")

    def test_a_result_is_kept_under_its_own_key_never_the_selected_one(self) -> None:
        view = tool()
        model = {"holders": [dict(key=(n, None), kind="held", pid=n) for n in (1, 2)], "waiters": []}
        state, effects = view.reduce(view.AppState(model=model), view.ModelLoaded(model), 100.0)
        a = view.details_key(model["holders"][0], ((1, None), 0))
        b = view.details_key(model["holders"][1], ((2, None), 0))
        self.assertEqual([a], [effect.key for effect in effects if effect.kind == "load-details"], "row a loads")
        state, _ = view.reduce(state, view.Key("j"), 100.0)  # the selection moves to b before a's load lands
        state, effects = view.reduce(state, view.DetailsLoaded(a, {"lane": "a"}, "/a"), 100.0)
        self.assertNotIn(b, state.details, "row b still loads: nothing of a's is painted on it")
        self.assertEqual([b], [effect.key for effect in effects if effect.kind == "load-details"])
        self.assertEqual(({"lane": "a"}, "/a"), (state.details[a].lane, state.details[a].cwd))
        state, effects = view.reduce(state, view.Key("k"), 100.0)
        self.assertEqual([], [effect for effect in effects if effect.kind == "load-details"], "fresh: shown as it is")
        state, effects = view.reduce(state, view.Tick(), 100.0 + view.DETAILS_FRESH + 1)
        self.assertEqual([a], [effect.key for effect in effects if effect.kind == "load-details"],
                         "older: shown at once and refreshed quietly")
        self.assertIn(a, state.details)


class DetailsPanelTests(unittest.TestCase):
    def model(self) -> dict[str, Any]:
        now = datetime.datetime.now(datetime.timezone.utc)
        holder = dict(label="build", kind="held", count=1, pid=10, style="h0", cpu=50, since=now, granted=now,
                      rss=1024, procs=1, queue="", cmd="work", tree=[], key=(10, None), priority="normal", ticket=None,
                      meta="", context={}, workdir="", yieldable=False, yields=0, asked="", asked_at=None)
        return {"holders": [holder], "waiters": [], "slots": 4, "cores": 12, "free": 3, "used": 1}

    def test_a_spinner_while_loading_and_the_facts_once_they_are_there(self) -> None:
        view = tool()
        now = datetime.datetime.now(datetime.timezone.utc)
        canvas = view.Canvas(60, 20)
        canvas.loading = 3
        view.draw_detail(canvas, Rect(0, 0, 20, 60), self.model(), 0, None, now)
        text = canvas.render(False)
        self.assertIn(view.SPINNER[3], text)
        self.assertIn("loading", text)
        self.assertNotIn("build", text.split("LEASE DETAILS")[1])
        spinner = next((y, line.index(view.SPINNER[3])) for y, line in enumerate(text.splitlines())
                       if view.SPINNER[3] in line)
        self.assertLessEqual(abs(spinner[1] - 29), 1, "centred")
        self.assertEqual("dim", canvas.cells[spinner[0]][spinner[1]][1])
        canvas = view.Canvas(60, 20)  # a cache hit: no spinner
        view.draw_detail(canvas, Rect(0, 0, 20, 60), self.model(), 0, None, now)
        text = canvas.render(False)
        self.assertNotIn("loading", text)
        self.assertRegex(text, r"label\s+build")


class FocusTests(unittest.TestCase):
    def test_the_focused_frame_is_the_accent_and_the_rest_muted(self) -> None:
        view = tool()
        for focus in view.PANELS:
            with self.subTest(focus=focus):
                canvas = view.Canvas(80, 20)
                canvas.focus = focus
                frames_of(view, canvas)
                frames = {"requests": canvas.cells[5][0], "details": canvas.cells[5][40], "events": canvas.cells[15][0]}
                for name, cell in frames.items():
                    self.assertEqual("accent" if name == focus else "frame", cell[1], name)
                self.assertEqual("\x1b[0;38;2;%d;%d;%dm" % view.current_theme.styles["accent"][0], view.sgr("accent", False),
                                 "our accent blue")

    def test_every_frame_is_light_and_rounded_and_focus_shows_by_colour(self) -> None:
        view = tool()
        canvas = view.Canvas(80, 20)
        canvas.focus = "details"
        frames_of(view, canvas)
        rows = ["".join(cell[0] for cell in row) for row in canvas.cells]
        for x, name in ((0, "QUEUE"), (40, "DETAILS")):
            self.assertEqual(("╭", "╮", "╰", "╯"), (rows[0][x], rows[0][x + 39], rows[9][x], rows[9][x + 39]), name)
            self.assertEqual(("│", "─"), (rows[5][x], rows[9][x + 20]), name)
        self.assertEqual(("╭", "╮"), (rows[10][0], rows[10][79]))
        self.assertFalse(set("┏┓┗┛━┃") & set("".join(rows)), "no heavy glyphs")
        self.assertEqual({"accent"}, {cell[1] for cell in (canvas.cells[0][40], canvas.cells[0][79],
                                                            canvas.cells[5][40], canvas.cells[9][60])})
        self.assertEqual({"frame"}, {cell[1] for cell in (canvas.cells[0][0], canvas.cells[5][39], canvas.cells[15][0])})
        lines = canvas.lines(True)
        blue = view.sgr("accent", False)
        self.assertIn(blue + "╭", lines[0], "the focused corner in our blue")
        self.assertIn(blue + "│", lines[5], "and its sides")
        self.assertEqual("DETAILS", "".join(cell[0] for cell in canvas.cells[0][43:50]))
        self.assertEqual("title", canvas.cells[0][43][1], "the title in the foreground (bold), as on every panel")

    def test_the_focused_panels_labels_are_in_the_foreground_and_the_others_muted(self) -> None:
        view = tool()
        placed = [("label", "label", 0, 10, False), ("pid", "pid", 12, 5, True)]
        for focus in ("requests", "events"):
            with self.subTest(focus=focus):
                canvas = view.Canvas(60, 6)
                canvas.focus = focus
                view.Table(placed).header(view.Panel(canvas, Rect(0, 0, 6, 60), "requests"), "QUEUE")
                x = "".join(cell[0] for cell in canvas.cells[0]).index("pid")
                self.assertEqual("text" if focus == "requests" else "dim", canvas.cells[0][x][1])
                self.assertEqual("title", canvas.cells[0][3][1])
                self.assertEqual("accent" if focus == "requests" else "frame", canvas.cells[0][0][1], "the frame")
        self.assertEqual("\x1b[0m", view.sgr("text", False), "the foreground token: no colour code")

    def test_a_click_focuses_the_panel_it_lands_in(self) -> None:
        view = tool()
        canvas = view.Canvas(80, 20)
        canvas.tables = {"requests": dict(x=0, y=0, width=40, height=10), "details": dict(x=40, y=0, width=40,
                         height=10), "events": dict(x=0, y=10, width=80, height=10)}
        state = drawn_state(canvas)
        self.assertEqual(("requests", "details", "events", None),
                         (view.panel_at(state, 5, 5), view.panel_at(state, 50, 2), view.panel_at(state, 70, 15),
                          view.panel_at(state, 90, 25)))
        canvas.tables = {name: dict(view, x=view["x"], top=view["y"] + 2, visible=6, count=0, offset=0)
                         for name, view in canvas.tables.items()}
        for x, y, focus in ((50, 2, "details"), (70, 15, "events"), (5, 5, "requests")):
            self.assertEqual(focus, tool().reduce(drawn_state(canvas), tool().Mouse(0, x, y, True), 0.0)[0].focus)

    def test_scroll_keys_move_a_panel_and_its_thumb(self) -> None:
        view = tool()
        model = {"events": [event(n) for n in range(40)]}
        scroll: dict[str, int] = {}

        def draw() -> Any:
            canvas = view.Canvas(80, 14)
            canvas.scroll = scroll
            canvas.focus = "events"
            view.draw_events(canvas, Rect(0, 0, 14, 80), model)
            return canvas

        def thumb(canvas: Any) -> list[int]:
            return [y for y in range(1, 13) if canvas.cells[y][78][0] in "┃╻╹"]

        canvas = draw()
        top = thumb(canvas)
        self.assertEqual(2, top[0], "the first content row, under the padding row")
        for key, expected in (("down", 1), ("j", 2), ("page_down", 11), ("up", 10), ("end", 30), ("home", 0)):
            state, _ = view.reduce(drawn_state(canvas, focus="events", scroll=dict(scroll)), view.Key(key), 0.0)
            scroll.update(state.scroll)
            self.assertEqual(expected, scroll["events"], key)
            canvas = draw()
        scroll["events"] = 30
        last = draw()
        self.assertEqual(11, thumb(last)[-1], "the last page puts the thumb at the bottom of the rows")
        for padding in (1, 12):  # nothing draws on the padding rows, scrolled or not
            self.assertEqual(" " * 77, "".join(cell[0] for cell in last.cells[padding][1:78]), padding)
        state = drawn_state(canvas, focus="events", scroll=dict(scroll))
        self.assertIs(state, view.reduce(state, view.Key("x"), 0.0)[0], "x is no scroll key")

    def test_events_follow_the_newest_at_the_top_and_stay_put_when_scrolled(self) -> None:
        view = tool()
        state = view.follow_events(view.AppState(), [event(n) for n in range(10)])
        state = view.follow_events(state, [event(n) for n in range(12)])
        self.assertEqual((0, None), (state.fresh_events, state.scroll.get("events")), "pinned to the newest at the top")
        state = dataclasses.replace(state, scroll={"events": 3})
        state = view.follow_events(state, [event(n) for n in range(15)])
        self.assertEqual((3, 6), (state.fresh_events, state.scroll["events"]),
                         "three new ones above: the same rows stay in view")
        canvas = view.Canvas(80, 14)
        canvas.scroll, canvas.fresh_events = state.scroll, state.fresh_events
        view.draw_events(canvas, Rect(0, 0, 14, 80), {"events": [event(n) for n in range(15)]})
        self.assertIn("↑ 3 new", canvas.render(False))
        state = dataclasses.replace(state, scroll={"events": 0})  # Home
        self.assertEqual(0, view.follow_events(state, [event(n) for n in range(15)]).fresh_events)
        self.assertEqual(0, view.settle(dataclasses.replace(state, fresh_events=3), 0.0)[0].fresh_events,
                         "at the top the count goes")


def sample_model() -> dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    holder = dict(label="build", kind="held", count=1, pid=10, style="h0", cpu=50, since=now, granted=now,
                  rss=1024, procs=1, queue="", cmd="work", tree=[], key=(10, None), priority="normal", ticket=None,
                  meta="", context={}, workdir="", yieldable=False, yields=0, asked="", asked_at=None)
    return dict(holders=[holder], waiters=[], slots=4, cores=12, saved=False, averages=(1.0, 1.0, 1.0),
                advice=dict(unleased_load=0, jobs=4, capacity=4), admission=3, memory_block=None,
                memory=judged(dict(level=1, used=512, total=1024, swap_total=0, swap_free=None)), history=[],
                events=[event(n) for n in range(5)], updated=now, order="backfill", start=None, limit=2700, used=1,
                free=3)


class FocusRestsOnDrawnPanelsTests(unittest.TestCase):
    """The audit's probe: with DETAILS focused while loading (or not drawn), j moved the QUEUE selection."""

    def model(self) -> dict[str, Any]:
        now = datetime.datetime.now(datetime.timezone.utc)

        def holder(n: int) -> dict[str, Any]:
            return dict(label=f"row{n}", kind="held", count=1, pid=10 + n, style="h0", cpu=50, since=now,
                        granted=now, rss=1024, procs=1, queue="", cmd="work", tree=[], key=(10 + n, None),
                        priority="normal", ticket=None, meta="", context={}, workdir="", yieldable=False, yields=0,
                        asked="", asked_at=None, idle_since=None)

        return {"holders": [holder(n) for n in range(3)], "waiters": [], "slots": 4, "cores": 12, "free": 1,
                "used": 3}

    def test_details_loading_or_empty_still_take_their_keys(self) -> None:
        view = tool()
        now = datetime.datetime.now(datetime.timezone.utc)
        empty: dict[str, Any] = {"holders": [], "waiters": [], "slots": 4, "cores": 12}
        for loading, model in ((1, self.model()), (None, empty)):
            canvas = view.Canvas(60, 20)
            canvas.focus, canvas.loading = "details", loading
            view.draw_detail(canvas, Rect(0, 0, 20, 60), model, 1, None, now)
            self.assertIn("details", canvas.tables, "drawn, so it can hold the focus")
            state = drawn_state(canvas, model if model["holders"] else None, 1, focus="details")
            for key in ("j", "k", "down", "page_down", "home"):
                after, _ = view.reduce(state, view.Key(key), 0.0)
                self.assertEqual((state.selection, "details"), (after.selection, after.focus),
                                 f"{key} is the panel's: QUEUE's selection stays")

    def test_tab_goes_by_columns_from_the_layout(self) -> None:
        view = tool()
        canvas = view.Canvas(160, 40)
        view.dashboard_draw(canvas, sample_model())
        self.assertEqual(["requests", "events", "details", "requests"], tabs(canvas), "left column down, then the right")
        self.assertEqual("details", tabs(canvas, "back_tab")[1], "Shift-Tab backwards")
        narrow = view.Canvas(100, 60)
        view.dashboard_draw(narrow, sample_model())
        self.assertEqual(["requests", "details", "events"], tabs(narrow)[:3], "one column: top to bottom")

    def test_one_column_puts_machine_and_events_side_by_side_or_drops_events(self) -> None:
        view = tool()
        for width, beside in ((109, True), (100, True), (83, True), (82, False), (60, False)):
            with self.subTest(width=width):
                canvas = view.Canvas(width, 60)
                view.dashboard_draw(canvas, sample_model())
                lines = canvas.render(False).splitlines()
                machine = next(y for y, line in enumerate(lines) if " MACHINE " in line)
                self.assertEqual(beside, " EVENTS " in lines[machine], "on MACHINE's row")
                self.assertEqual(beside, "events" in canvas.tables, "a hidden EVENTS is no Tab stop")
                self.assertTrue(lines[machine].startswith("╭─ EVENTS " if beside else "╭─ MACHINE "),
                                "EVENTS on the left, MACHINE on the right (alone: the whole width)")
                order = tabs(canvas)
                expected = ["requests", "details", "events", "requests"] if beside else ["requests", "details",
                                                                                         "requests", "details"]
                self.assertEqual(expected, order, "by columns, top to bottom")
                if beside:
                    events = canvas.tables["events"]
                    self.assertEqual(0, events["x"])
                    self.assertGreaterEqual(events["width"], view.NARROW_EVENTS)
                    machine_x = lines[machine].index("╭─ MACHINE")
                    self.assertEqual(events["width"] + 1, machine_x, "one column between them")
                    self.assertGreaterEqual(width - machine_x, view.NARROW_MACHINE, "MACHINE keeps its least width")
                    self.assertTrue(lines[machine].rstrip().endswith("╮"))

    def test_side_by_side_machine_keeps_its_own_height_and_the_details_get_the_rest(self) -> None:
        view = tool()
        for height in (60, 44, 40):  # 34 and less: the details keep their floor, MACHINE gives way
            with self.subTest(height=height):
                canvas = view.Canvas(100, height)
                view.dashboard_draw(canvas, sample_model())
                lines = canvas.render(False).splitlines()
                top = next(y for y, line in enumerate(lines) if "╭─ MACHINE " in line)
                left = lines[top].index("╭─ MACHINE ")
                bottom = next(y for y in range(top + 1, height) if lines[y][left:].startswith("╰"))
                self.assertEqual(view.MACHINE_HEIGHT, bottom - top + 1, "MACHINE at its own height, chart and all")
                self.assertIn("now", lines[bottom - 1], "its axis right over the bottom border: no blank rows")
                events = canvas.tables["events"]
                self.assertEqual((top, view.MACHINE_HEIGHT), (events["y"], events["height"]), "EVENTS beside it")
                details, queue = canvas.tables["details"], canvas.tables["requests"]
                self.assertEqual(top, details["y"] + details["height"], "the details right above")
                self.assertEqual(height - 1 - 5 - queue["height"] - view.MACHINE_HEIGHT, details["height"],
                                 "the details take what QUEUE and MACHINE leave")

    def test_a_scrolled_thumb_flashes_and_fades_with_one_wake(self) -> None:
        view = tool()
        self.assertAlmostEqual(0.28, view.SCROLL_GLOW)
        canvas = view.Canvas(80, 14)
        canvas.focus = "events"
        view.draw_events(canvas, Rect(0, 0, 14, 80), {"events": [event(n) for n in range(40)]})
        state = drawn_state(canvas, focus="events")
        self.assertEqual((set(), None), (view.glowing(state, 100.0), view.fade(state, 100.0)),
                         "nothing scrolled: no wake")
        state, _ = view.reduce(state, view.Key("j"), 100.0)
        self.assertEqual({"events"}, view.glowing(state, 100.0))
        state, _ = view.reduce(state, view.Key("j"), 100.2)  # still scrolling: the flash runs on from the last scroll
        self.assertEqual({"events"}, view.glowing(state, 100.4))
        self.assertAlmostEqual(0.08, cast(float, view.fade(state, 100.4)), places=6, msg="one wake, at the fade")
        self.assertEqual(set(), view.glowing(state, 100.48), "0.28 s after the last scroll: back to muted")
        self.assertIsNone(view.fade(state, 100.48), "and no more wakes")
        self.assertEqual({"events"}, view.scrolled({"events": 0, "details": 4}, {"events": 3, "details": 4}))
        self.assertEqual(set(), view.scrolled({"details": 5}, {}), "an offset dropped is no scroll")
        model = {"events": [event(n) for n in range(40)]}
        cases: list[tuple[set[str], bool, str]] = [(set(), False, "dim"), (set(), True, "dim"),
                                                   ({"events"}, False, "text"), ({"events"}, True, "text")]
        for lit, focused, style in cases:
            with self.subTest(lit=lit, focused=focused):
                canvas = view.Canvas(80, 14)
                canvas.scroll, canvas.glow = {"events": 3}, lit
                canvas.focus = "events" if focused else "requests"
                view.draw_events(canvas, Rect(0, 0, 14, 80), model)
                thumb = {canvas.cells[y][78][1] for y in range(14) if canvas.cells[y][78][0] in ("┃", "╻", "╹")}
                self.assertEqual({style}, thumb, "muted at rest, focused or not; the foreground while scrolling")
        for draw, name in ((view.draw_help, "help"), (view.draw_changelog, "changelog")):
            for lit, style in ((set[str](), "dim"), ({name}, "text")):
                with self.subTest(overlay=name, lit=lit):
                    canvas = view.Canvas(80, 20)
                    canvas.glow = lit
                    draw(canvas)
                    info = canvas.help_view
                    column = info.x + info.width - 2
                    thumb = {canvas.cells[y][column][1] for y in range(20)
                             if canvas.cells[y][column][0] in ("┃", "╻", "╹")}
                    self.assertEqual({style}, thumb, f"{name}: muted at rest, the foreground while scrolling")

    def test_tab_skips_panels_that_are_not_drawn(self) -> None:
        view = tool()
        canvas = view.Canvas(80, 20)
        canvas.tables = {"requests": {"x": 0, "y": 0}, "events": {"x": 0, "y": 10}}
        self.assertEqual(["requests", "events", "requests", "events"], tabs(canvas))
        self.assertEqual("events", tabs(canvas, "back_tab")[1])
        state = drawn_state(canvas, focus="details")
        self.assertEqual("requests", view.reduce(state, view.Key("\t"), 0.0)[0].focus, "a focus that lost its panel")
        self.assertEqual("requests", view.reduce(state, view.Drawn(state.frame), 0.0)[0].focus,
                         "and a frame that no longer draws it gives the focus back to QUEUE")


class ChangelogTests(unittest.TestCase):
    TEXT = """# Godfather Changelog

## Unreleased

## 0.2.0 - 2026-12-01

### Added

- A **new** thing with `code` and a [link](https://example.com),
  continued here.

## 0.1.0 - 2026-11-11

### Removed

- An old key.
"""

    def test_parsing_takes_markdown_out_and_joins_continuations(self) -> None:
        releases = tool().parse_changelog(self.TEXT)
        self.assertEqual([("Unreleased", "", ""), ("0.2.0", "2026-12-01", ""), ("0.1.0", "2026-11-11", "")],
                         [(r.version, r.date, r.commit) for r in releases])
        self.assertEqual([("Added", ["A new thing with code and a link, continued here."])], releases[1].sections)
        self.assertTrue((ROOT / "CHANGELOG.md").is_file())
        view = tool()
        shipped = view.parse_changelog((ROOT / "CHANGELOG.md").read_text())
        self.assertIn(view._version.VERSION, [r.version for r in shipped], "the running release has its section")

    def test_the_running_release_is_installed_and_newer_ones_new_when_stale(self) -> None:
        view = tool()
        releases = view.parse_changelog(self.TEXT)
        lines = ["".join(text for text, _ in parts) for parts in view.changelog_lines(60, releases, "0.1.0", True)]
        self.assertIn("0.2.0 · 2026-12-01  new — restart to get these", lines)
        self.assertIn("0.1.0 · 2026-11-11  installed", lines)
        self.assertFalse(any("Unreleased" in line for line in lines), "an empty release goes")
        self.assertIn("    • A new thing with code and a link, continued here.", lines)
        self.assertFalse(any("**" in line or "`" in line or "](" in line for line in lines), "no raw markdown")
        calm = ["".join(t for t, _ in p) for p in view.changelog_lines(60, releases, "0.1.0", False)]
        self.assertFalse(any("restart" in line for line in calm))
        narrow = view.changelog_lines(30, releases, "0.1.0", False)
        bullet = next(i for i, parts in enumerate(narrow) if parts and parts[0][0] == "    • ")
        self.assertEqual(" " * 6, narrow[bullet + 1][0][0][:6], "a wrapped entry hangs under its text")

    def test_the_footer_version_is_the_click_target_and_the_dialog_closes(self) -> None:
        view = tool()
        canvas = view.Canvas(140, 30)
        view.dashboard_draw(canvas, None)
        row, start, end = canvas.version_at
        footer = "".join(cell[0] for cell in canvas.cells[row])
        self.assertEqual(view.running_version(), footer[start:end].strip())
        self.assertTrue(view.running_version().startswith(view._version.VERSION))
        state, _ = view.reduce(drawn_state(canvas), view.Mouse(0, start, row, True), 0.0)
        self.assertEqual(view.Changelog(), state.overlay, "a click on the version opens the changelog")
        canvas = view.Canvas(100, 30)
        view.dashboard_draw(canvas, None, overlay=view.Changelog())
        self.assertIn("CHANGELOG", canvas.render(False))
        self.assertIn("esc close", canvas.render(False))
        info = canvas.help_view
        state = drawn_state(canvas, overlay=view.Changelog())

        def after(event: Any) -> Any:
            return view.reduce(state, event, 0.0)[0].overlay

        self.assertIsNone(after(view.Mouse(0, 0, 0, True)), "a click outside closes it")
        self.assertEqual(view.Changelog(), after(view.Mouse(0, info.x + 5, info.y + 3, True)), "one inside does not")
        self.assertEqual(view.Changelog(), after(view.Mouse(65, 0, 0, True)), "nor the wheel outside")
        self.assertIsNone(after(view.Key("esc")))
        self.assertEqual(view.Changelog(scroll=1), after(view.Key("down")))






if __name__ == "__main__":
    unittest.main()
