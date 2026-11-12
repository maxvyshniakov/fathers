"""The live view in a PTY (#15 batch), moved from Slotfather's LiveTests and VersionTests, against the fake
Slotfather: details load in the background (GODFATHER_DETAILS_DELAY) while the selection moves at once, Tab focuses
the details and arrows scroll them, EVENTS counts what arrives while scrolled, a resize redraws, and a newer install
shows ↻ in the footer."""
from __future__ import annotations

import fcntl
import importlib.util
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path
from typing import Any

from helpers import ROOT
from terminal import PLAIN, LiveCase, event, item


class LiveTests(LiveCase):
    def test_moving_is_instant_and_ends_on_the_right_row_and_details(self) -> None:
        self.queue(holders=[item(f"row{n}", 100 + n) for n in range(3)])
        screen = self.start(rows=40, columns=140, GODFATHER_DETAILS_DELAY="1.5")
        text = screen.until(lambda text: re.search(r"label\s+row\d", text)
                            and len(re.findall(r"▶️\s+row\d", text)) >= 3, 10, "the queue and the first row's details")
        order = re.findall(r"▶️\s+(row\d)", text)[:3]
        self.assertEqual(["row0", "row1", "row2"], order, "the snapshot's order")
        self.assertRegex(text, rf"label\s+{order[0]}")
        mark = screen.mark()
        screen.write(b"\x1b[B")
        started = time.monotonic()
        screen.until(lambda text: "loading" in text, 2, "the spinner", since=mark)
        self.assertLess(time.monotonic() - started, 1.0, "the selection moves without waiting for the details")
        mark = screen.mark()
        screen.write(b"\x1b[B\x1b[B\x1b[B")  # rapidly to the end
        screen.until(lambda text: re.search(rf"label\s+{order[2]}", text), 8, "the last row's details", since=mark)
        mark = screen.mark()
        screen.write(b"\x1b[A")  # back to the middle row, whose load was superseded: it loads now
        screen.until(lambda text: re.search(rf"label\s+{order[1]}", text), 8, "the middle row's details", since=mark)
        mark = screen.mark()
        screen.write(b"\x1b[B")  # the last row again: cached, at once, no spinner
        before = time.monotonic()
        screen.until(lambda text: re.search(rf"label\s+{order[2]}", text), 1, "the last row from the cache",
                     since=mark)
        self.assertLess(time.monotonic() - before, 1.0)
        self.assertEqual(0, screen.quit())

    def test_tab_focuses_the_details_and_arrows_scroll_them(self) -> None:
        self.queue(holders=[item("scroller", 200)])
        screen = self.start(rows=20, columns=140)
        screen.until(lambda text: re.search(r"label\s+scroller", text), 10, "the details")
        mark = screen.mark()
        screen.write(b"\t\t")  # by columns: QUEUE, EVENTS, then DETAILS
        screen.receive("↑↓ scroll", seconds=3, since=mark)
        screen.write(b"\x1b[B\x1b[B")

        def top_row(_: str) -> bool:  # the details' first row, as last drawn, is now their third fact
            cells = re.split("[│┃]", screen.rows().get(3, ""))  # row 2 is the blank padding row
            return len(cells) >= 3 and "priority" in cells[-2]

        screen.until(top_row, 3, "the details scrolled two lines")
        mark = screen.mark()
        screen.write(b"\t")  # Tab: from DETAILS, the last column, back to QUEUE
        screen.receive("↑↓ select", seconds=3, since=mark)
        self.assertEqual(0, screen.quit())

    def test_events_scrolled_down_stay_put_and_count_what_arrives(self) -> None:
        events = [event(n) for n in range(40)]
        self.queue(holders=[item("busy", 300)], events=events)
        screen = self.start(rows=30, columns=140, argv=[sys.executable, str(ROOT / "godfather"), "--interval", "0.2"])
        screen.receive("job39")
        mark = screen.mark()
        screen.write(b"\t")  # EVENTS
        screen.receive("↑↓ scroll", seconds=3, since=mark)
        mark = screen.mark()
        screen.write(b"\x1b[B\x1b[B")
        screen.until(lambda _: not any("job39" in line for line in screen.rows().values()), 3,
                     "EVENTS scrolled past the newest")
        self.queue(holders=[item("busy", 300)], events=events + [event(n) for n in range(40, 43)])
        mark = screen.mark()
        screen.receive("↑ 3 new", since=mark)
        self.assertFalse(any("job42" in line for line in screen.rows().values()), "it stays where it was scrolled")
        mark = screen.mark()
        screen.write(b"\x1b[H")  # Home: back to the newest
        screen.until(lambda _: any("job42" in line for line in screen.rows().values()), 3, "the newest again")
        self.assertEqual(0, screen.quit())

    def test_a_resize_redraws_the_whole_screen_in_the_new_layout(self) -> None:
        self.queue(holders=[item("resized", 400)])
        screen = self.start(rows=40, columns=140)
        screen.until(lambda _: any("LEASE DETAILS" in line and "SLOTS" not in line for line in screen.rows().values())
                     or any("SLOTS" in line and "LEASE DETAILS" in line for line in screen.rows().values()), 8,
                     "the wide layout: DETAILS beside SLOTS")
        mark = screen.mark()
        fcntl.ioctl(screen.master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 80, 0, 0))
        screen.raw_until(lambda data: b"\x1b[2J" in data[mark:], 3, "the screen cleared after the resize")
        screen.until(lambda _: (rows := screen.rows(mark)) and len(rows) >= 40
                     and len(rows[1]) == 80 and rows[1].endswith("╮")  # SLOTS' border: the new width, all narrow glyphs
                     and not any("SLOTS" in line and "DETAILS" in line for line in rows.values()), 3,
                     "every row again, in one column at 80")
        self.assertEqual(0, screen.quit())


def installer() -> Any:
    spec = importlib.util.spec_from_file_location("godfather_installer_for_test", ROOT / "install.py")
    assert spec and spec.loader
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VersionTests(LiveCase):
    def test_version_prints_the_semver_and_the_commit(self) -> None:
        result = subprocess.run([sys.executable, str(ROOT / "godfather"), "--version"], capture_output=True,
                                text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertRegex(result.stdout, r"^godfather \d+\.\d+\.\d+ \([0-9a-f]{7}(\+dirty)?\) source [0-9a-f]{64}\n$")

    def test_install_ships_the_changelog(self) -> None:
        module = installer()
        self.assertIn(ROOT / "CHANGELOG.md", module.source_files(ROOT))
        with tempfile.TemporaryDirectory() as home:
            release = module.install_package(Path(home), ROOT)
            self.assertEqual((ROOT / "CHANGELOG.md").read_bytes(),
                             (release / "godfather" / "CHANGELOG.md").read_bytes())

    def test_a_newer_install_shows_in_the_footer_of_the_one_running(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="godfather-reinstall-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)

        def release(name: str, digest: str) -> Path:
            folder = root / name
            shutil.copytree(ROOT / "src" / "godfather", folder / "godfather",
                            ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copyfile(ROOT / "CHANGELOG.md", folder / "godfather" / "CHANGELOG.md")
            (folder / "godfather" / "version.json").write_text(json.dumps(
                {"v": 1, "commit": "0" * 40, "dirty": False, "source_sha256": digest}))
            return folder

        def entry(folder: Path) -> None:
            (root / "godfather").write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(folder)!r})\n"
                                            "from godfather.app import main\nif __name__ == '__main__':\n"
                                            "    sys.exit(main())\n")

        first, second = release("first", "a" * 64), release("second", "b" * 64)
        entry(first)
        command = [sys.executable, str(root / "godfather"), "--interval", "0.3"]
        screen = self.start(rows=30, columns=140, argv=command)
        screen.until(lambda _: "Godfather  what's new? " in screen.rows().get(30, ""), 8, "the footer's what's new")
        self.assertNotIn("↻", screen.rows().get(30, ""))
        entry(second)  # a reinstall replaces the entry point; this process keeps its release
        screen.until(lambda _: "what's new? ↻" in screen.rows().get(30, ""), 8, "↻ after the reinstall")
        mark = screen.mark()
        screen.write(b"v")
        text = screen.receive("WHAT'S NEW", "installed", since=mark)
        self.assertNotIn("restart to update", text, "fathers#102: the footer's ↻ says it")
        self.assertEqual(0, screen.quit())


class ChangelogPlainTests(unittest.TestCase):
    def test_plain_strips_terminal_sequences_the_tests_read_through(self) -> None:
        self.assertEqual("ab", PLAIN.sub("", "\x1b[0;1ma\x1b]8;;https://x\x1b\\b"))


if __name__ == "__main__":
    unittest.main()
