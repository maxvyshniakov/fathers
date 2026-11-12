"""The live view (#15 batch): details load in the background, panels take keyboard focus and scroll, EVENTS stays
put while scrolled, the footer's version opens the changelog."""
from __future__ import annotations

import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import datetime
import fcntl
import importlib
import importlib.util
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time
import unittest
from typing import Any, Callable, Protocol, cast

from slotfather import theme

from test_slotfather import ROOT, SlotCase

DARK = {"SLOTFATHER_TERMINAL_BG": "#282c34", "SLOTFATHER_TERMINAL_FG": "#ffffff"}
PLAIN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class Receive(Protocol):
    def __call__(self, check: Callable[[str], object], seconds: float = 8, what: str = "") -> str: ...


def tool() -> Any:
    found: Any = importlib.reload(importlib.import_module("slotfather.dashboard"))
    found.set_theme(theme.resolve("auto", environ=DARK))
    return found


def event(n: int) -> dict[str, Any]:
    when = datetime.datetime(2026, 11, 11, 12, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=n)
    return {"when": when, "verb": "granted", "count": 1, "label": f"job{n}", "pid": 1000 + n, "delta": 1}


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
        now = [100.0]
        cache = tool().DetailsCache(lambda: now[0])
        cache.put("a", {"lane": "a"}, "/a")  # a load for row a, finished after the selection moved to b
        self.assertIsNone(cache.get("b"), "row b still loads: nothing of a's is painted on it")
        self.assertEqual(({"lane": "a"}, "/a", True), cache.get("a"))
        now[0] += tool().DETAILS_FRESH + 1
        self.assertEqual(({"lane": "a"}, "/a", False), cache.get("a"), "shown at once, refreshed quietly")


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
        view.draw_detail(canvas, 0, 0, 20, 60, self.model(), 0, None, now)
        text = canvas.render(False)
        self.assertIn(view.SPINNER[3], text)
        self.assertIn("loading", text)
        self.assertNotIn("build", text.split("LEASE DETAILS")[1])
        spinner = next((y, line.index(view.SPINNER[3])) for y, line in enumerate(text.splitlines())
                       if view.SPINNER[3] in line)
        self.assertLessEqual(abs(spinner[1] - 29), 1, "centred")
        self.assertEqual("dim", canvas.cells[spinner[0]][spinner[1]][1])
        canvas = view.Canvas(60, 20)  # a cache hit: no spinner
        view.draw_detail(canvas, 0, 0, 20, 60, self.model(), 0, None, now)
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
                canvas.box(0, 0, 10, 40, "QUEUE", name="requests")
                canvas.box(0, 40, 10, 40, "DETAILS", name="details")
                canvas.box(10, 0, 10, 80, "EVENTS", name="events")
                frames = {"requests": canvas.cells[5][0], "details": canvas.cells[5][40], "events": canvas.cells[15][0]}
                for name, cell in frames.items():
                    self.assertEqual("accent" if name == focus else "frame", cell[1], name)
                self.assertEqual("\x1b[0;38;2;%d;%d;%dm" % view.current_theme.styles["accent"][0], view.sgr("accent", False),
                                 "our accent blue")

    def test_every_frame_is_light_and_rounded_and_focus_shows_by_colour(self) -> None:
        view = tool()
        canvas = view.Canvas(80, 20)
        canvas.focus = "details"
        canvas.box(0, 0, 10, 40, "QUEUE", name="requests")
        canvas.box(0, 40, 10, 40, "DETAILS", name="details")
        canvas.box(10, 0, 10, 80, "EVENTS", name="events")
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
                view.table_header(canvas, 0, 0, 6, 60, "QUEUE", placed, name="requests")
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
        self.assertEqual(("requests", "details", "events", None),
                         (view.panel_at(canvas, 5, 5), view.panel_at(canvas, 50, 2), view.panel_at(canvas, 70, 15),
                          view.panel_at(canvas, 90, 25)))

    def test_scroll_keys_move_a_panel_and_its_thumb(self) -> None:
        view = tool()
        model = {"events": [event(n) for n in range(40)]}
        scroll: dict[str, int] = {}

        def draw() -> Any:
            canvas = view.Canvas(80, 14)
            canvas.scroll = scroll
            canvas.focus = "events"
            view.draw_events(canvas, 0, 0, 14, 80, model)
            return canvas

        def thumb(canvas: Any) -> list[int]:
            return [y for y in range(1, 13) if canvas.cells[y][78][0] in "┃╻╹"]

        canvas = draw()
        top = thumb(canvas)
        self.assertEqual(2, top[0], "the first content row, under the padding row")
        for key, expected in (("down", 1), ("j", 2), ("page_down", 11), ("up", 10), ("end", 30), ("home", 0)):
            self.assertTrue(view.scroll_panel(canvas, scroll, "events", key))
            self.assertEqual(expected, scroll["events"], key)
            canvas = draw()
        scroll["events"] = 30
        last = draw()
        self.assertEqual(11, thumb(last)[-1], "the last page puts the thumb at the bottom of the rows")
        for padding in (1, 12):  # nothing draws on the padding rows, scrolled or not
            self.assertEqual(" " * 77, "".join(cell[0] for cell in last.cells[padding][1:78]), padding)
        self.assertFalse(view.scroll_panel(canvas, scroll, "events", "x"))

    def test_events_follow_the_newest_at_the_top_and_stay_put_when_scrolled(self) -> None:
        view = tool()
        scroll: dict[str, int] = {}
        fresh, newest = view.follow_events(scroll, 0, None, [event(n) for n in range(10)])
        fresh, newest = view.follow_events(scroll, fresh, newest, [event(n) for n in range(12)])
        self.assertEqual((0, None), (fresh, scroll.get("events")), "pinned to the newest at the top")
        scroll["events"] = 3
        fresh, newest = view.follow_events(scroll, fresh, newest, [event(n) for n in range(15)])
        self.assertEqual((3, 6), (fresh, scroll["events"]), "three new ones above: the same rows stay in view")
        canvas = view.Canvas(80, 14)
        canvas.scroll, canvas.fresh_events = scroll, fresh
        view.draw_events(canvas, 0, 0, 14, 80, {"events": [event(n) for n in range(15)]})
        self.assertIn("↑ 3 new", canvas.render(False))
        scroll["events"] = 0  # Home
        self.assertEqual(0, view.follow_events(scroll, fresh, newest, [event(n) for n in range(15)])[0])


def sample_model() -> dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    holder = dict(label="build", kind="held", count=1, pid=10, style="h0", cpu=50, since=now, granted=now,
                  rss=1024, procs=1, queue="", cmd="work", tree=[], key=(10, None), priority="normal", ticket=None,
                  meta="", context={}, workdir="", yieldable=False, yields=0, asked="", asked_at=None)
    return dict(holders=[holder], waiters=[], slots=4, cores=12, saved=False, averages=(1.0, 1.0, 1.0),
                advice=dict(unleased_load=0, jobs=4, capacity=4), admission=3, memory_block=None,
                memory=dict(level=1, used=512, total=1024, swap_total=0, swap_free=None), history=[],
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
            view.draw_detail(canvas, 0, 0, 20, 60, model, 1, None, now)
            self.assertIn("details", canvas.tables, "drawn, so it can hold the focus")
            for key in ("j", "k", "down", "page_down", "home"):
                self.assertTrue(view.panel_key(canvas, {}, "details", key), f"{key} is the panel's")

    def test_tab_goes_by_columns_from_the_layout(self) -> None:
        view = tool()
        canvas = view.Canvas(160, 40)
        view.dashboard_draw(canvas, sample_model())
        order = ["requests"]
        for _ in range(3):
            order.append(view.next_panel(canvas, order[-1], 1))
        self.assertEqual(["requests", "events", "details", "requests"], order, "left column down, then the right")
        self.assertEqual("details", view.next_panel(canvas, "requests", -1), "Shift-Tab backwards")
        narrow = view.Canvas(100, 60)
        view.dashboard_draw(narrow, sample_model())
        self.assertEqual("details", view.next_panel(narrow, "requests", 1), "one column: top to bottom")
        self.assertEqual("events", view.next_panel(narrow, "details", 1))

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
                order = ["requests"]
                for _ in range(3):
                    order.append(view.next_panel(canvas, order[-1], 1))
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
        for height in (60, 44, 34):
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
        now = [100.0]
        glow = view.ScrollGlow(lambda: now[0])
        self.assertEqual((set(), None), (glow.bright(), glow.fade()), "nothing scrolled: no wake")
        glow.touch(view.scrolled({"events": 0, "details": 4}, {"events": 3, "details": 4}))
        self.assertEqual({"events"}, glow.bright())
        now[0] = 100.2
        glow.touch({"events"})  # still scrolling: the flash runs on from the last scroll
        now[0] = 100.4
        self.assertEqual({"events"}, glow.bright())
        self.assertAlmostEqual(0.08, cast(float, glow.fade()), places=6, msg="one wake, at the fade")
        now[0] = 100.48
        self.assertEqual(set(), glow.bright(), "0.28 s after the last scroll: back to muted")
        self.assertIsNone(glow.fade(), "and no more wakes")
        self.assertEqual(set(), view.scrolled({"details": 5}, {}), "an offset dropped is no scroll")
        model = {"events": [event(n) for n in range(40)]}
        cases: list[tuple[set[str], bool, str]] = [(set(), False, "dim"), (set(), True, "dim"),
                                                   ({"events"}, False, "text"), ({"events"}, True, "text")]
        for lit, focused, style in cases:
            with self.subTest(lit=lit, focused=focused):
                canvas = view.Canvas(80, 14)
                canvas.scroll, canvas.glow = {"events": 3}, lit
                canvas.focus = "events" if focused else "requests"
                view.draw_events(canvas, 0, 0, 14, 80, model)
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
        self.assertEqual("events", view.next_panel(canvas, "requests", 1))
        self.assertEqual("requests", view.next_panel(canvas, "events", 1))
        self.assertEqual("events", view.next_panel(canvas, "requests", -1))
        self.assertEqual("requests", view.next_panel(canvas, "details", 1), "a focus that lost its panel")


class ChangelogTests(unittest.TestCase):
    TEXT = """# Slotfather Changelog

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
        canvas = view.Canvas(100, 30)
        view.dashboard_draw(canvas, None, help_open="changelog")
        self.assertIn("CHANGELOG", canvas.render(False))
        self.assertIn("esc close", canvas.render(False))
        info = canvas.help_view
        self.assertEqual("close", view.help_mouse(canvas, ("mouse", 0, 0, 0, True), {}), "a click outside")
        self.assertEqual("ignored", view.help_mouse(canvas, ("mouse", 0, info.x + 5, info.y + 3, True), {}))
        self.assertEqual("ignored", view.help_mouse(canvas, ("mouse", 65, 0, 0, True), {}), "the wheel outside")
        self.assertEqual("close", view.help_key(canvas, "esc", {}))
        self.assertEqual("scroll", view.help_key(canvas, "down", scroll := {}))
        self.assertEqual({"changelog": 1}, scroll)


class VersionTests(SlotCase):
    def test_version_prints_the_semver_and_the_commit(self) -> None:
        result = self.tool("version")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertRegex(result.stdout, r"^slotfather \d+\.\d+\.\d+ \([0-9a-f]{7}(\+dirty)?\) source [0-9a-f]{64}$")

    def test_install_ships_the_changelog(self) -> None:
        spec = importlib.util.spec_from_file_location("installer_for_test", ROOT / "install.py")
        assert spec and spec.loader
        installer: Any = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        self.assertIn(ROOT / "CHANGELOG.md", installer.source_files(ROOT))
        home = self.tmp / "home"
        release = installer.install_package(home, ROOT)
        self.assertEqual((ROOT / "CHANGELOG.md").read_bytes(), (release / "slotfather" / "CHANGELOG.md").read_bytes())


class LiveTests(SlotCase):
    """The live dashboard in a PTY with slow details (SLOTFATHER_DETAILS_DELAY)."""

    def start(self, env: dict[str, str], rows: int = 40) -> tuple[int, subprocess.Popen[bytes], Receive]:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, 140, 0, 0))
        process = subprocess.Popen([str(ROOT / "slotfather"), "dashboard", "--interval", "60"], stdin=slave,
                                   stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        screen = bytearray()
        self.raw = screen

        def receive(check: Callable[[str], object], seconds: float = 8, what: str = "") -> str:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        screen.extend(os.read(master, 65536))
                    except OSError:
                        break
                plain = PLAIN.sub("", screen.decode("utf-8", "replace"))
                if check(plain):
                    return plain
            self.fail(f"terminal did not show {what}: {PLAIN.sub('', screen.decode('utf-8', 'replace'))[-3000:]!r}")

        def stop() -> None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            os.close(master)

        self.addCleanup(stop)
        return master, process, receive

    def test_moving_is_instant_and_ends_on_the_right_row_and_details(self) -> None:
        holders = [self.request(f"row{n}", 1) for n in range(3)]
        for n in range(3):
            self.started(f"row{n}")
        env = dict(self.env, SLOTFATHER_DETAILS_DELAY="1.5")
        master, _, receive = self.start(env)
        text = receive(lambda text: re.search(r"label\s+row\d", text) and len(re.findall(r"▶️\s+row\d", text)) >= 3,
                       10, "the queue and the first row's details")
        order = re.findall(r"▶️\s+(row\d)", text)[:3]  # the queue's order, by grant
        self.assertRegex(text, rf"label\s+{order[0]}")
        os.write(master, b"\x1b[B")
        started = time.monotonic()
        receive(lambda text: "loading" in text, 2, "the spinner")
        self.assertLess(time.monotonic() - started, 1.0, "the selection moves without waiting for the details")
        os.write(master, b"\x1b[B\x1b[B\x1b[B")  # rapidly to the end
        receive(lambda text: re.search(rf"label\s+{order[2]}", text), 8, "the last row's details")
        os.write(master, b"\x1b[A")  # back to the middle row, whose load was superseded: it loads now
        receive(lambda text: re.search(rf"label\s+{order[1]}", text), 8, "the middle row's details")
        mark = len(text)
        os.write(master, b"\x1b[B")  # the last row again: cached, at once, no spinner
        before = time.monotonic()
        receive(lambda text: re.search(rf"label\s+{order[2]}", text[mark:]), 1, "the last row from the cache")
        self.assertLess(time.monotonic() - before, 1.0)
        os.write(master, b"q")
        for n, holder in enumerate(holders):
            self.release(f"row{n}")
            self.finish(holder)

    def test_tab_focuses_the_details_and_arrows_scroll_them(self) -> None:
        holder = self.request("scroller", 1)
        self.started("scroller")
        master, _, receive = self.start(dict(self.env), rows=20)
        receive(lambda text: re.search(r"label\s+scroller", text), 10, "the details")
        os.write(master, b"\t\t")  # by columns: QUEUE, EVENTS, then DETAILS
        receive(lambda text: "↑↓ scroll" in text, 3, "the scroll hint")
        os.write(master, b"\x1b[B\x1b[B")
        def top_row(_: str) -> bool:  # the details' first row, as last drawn, is now their third fact
            rows: dict[int, str] = {}
            for found in re.finditer(r"\x1b\[(\d+);1H([^\n]*?)(?=\x1b\[\d+;1H|$)",
                                     self.raw.decode("utf-8", "replace"), re.S):
                rows[int(found[1])] = PLAIN.sub("", found[2])
            cells = re.split("[│┃]", rows.get(3, ""))  # row 2 is the blank padding row
            return len(cells) >= 3 and "priority" in cells[-2]

        mark = len(receive(top_row, 3, "the details scrolled two lines"))
        os.write(master, b"\t")  # Tab: from DETAILS, the last column, back to QUEUE
        receive(lambda text: "↑↓ select" in text[mark:], 3, "QUEUE focused again")
        os.write(master, b"q")
        self.release("scroller")
        self.finish(holder)


if __name__ == "__main__":
    unittest.main()
