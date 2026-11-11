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
    def test_the_focused_frame_is_the_foreground_and_the_rest_muted(self) -> None:
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
                    self.assertEqual("text" if name == focus else "dim", cell[1], name)
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
            return [y for y in range(1, 13) if canvas.cells[y][79][0] == "┃"]

        canvas = draw()
        top = thumb(canvas)
        self.assertEqual(1, top[0])
        for key, expected in (("down", 1), ("j", 2), ("page_down", 11), ("up", 10), ("end", 30), ("home", 0)):
            self.assertTrue(view.scroll_panel(canvas, scroll, "events", key))
            self.assertEqual(expected, scroll["events"], key)
            canvas = draw()
        scroll["events"] = 30
        self.assertEqual(12, thumb(draw())[-1], "the last page puts the thumb at the bottom")
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
        text = receive(lambda text: re.search(r"label\s+row\d", text) and len(re.findall(r"▶️ row\d", text)) >= 3,
                       10, "the queue and the first row's details")
        order = re.findall(r"▶️ (row\d)", text)[:3]  # the queue's order, by grant
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
        os.write(master, b"\t")
        receive(lambda text: "↑↓ scroll" in text, 3, "the scroll hint")
        os.write(master, b"\x1b[B\x1b[B")
        receive(lambda text: "↑ 2 above" in text, 3, "the details scrolled two lines")
        os.write(master, b"\x1b[Z\x1b[Z")  # back-tab twice: back to QUEUE
        receive(lambda text: "↑↓ select" in text[-4000:], 3, "QUEUE focused again")
        os.write(master, b"q")
        self.release("scroller")
        self.finish(holder)


if __name__ == "__main__":
    unittest.main()
