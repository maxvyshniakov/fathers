"""The dashboard footer: the name on the left, the hints centred, the status on the right; no pause, refresh or
select-text keys."""
from __future__ import annotations

import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import datetime
import fcntl
import importlib
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time
import unittest
from typing import Any

from test_slotfather import ROOT, SlotCase

HINTS = ["↑↓ select", "s settings", "? help", "q quit"]


def model() -> dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    return dict(holders=[], waiters=[], slots=4, cores=12, saved=False, averages=(1.0, 1.0, 1.0),
                advice=dict(unleased_load=0, jobs=4, capacity=4), admission=4, memory_block=None,
                memory=dict(level=1, used=512, total=1024, swap_total=0, swap_free=None), history=[], events=[],
                updated=now, order="backfill", start=None, limit=2700, used=0, free=4)


class FooterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = importlib.reload(importlib.import_module("slotfather.dashboard"))

    def footer(self, width: int) -> str:
        canvas = self.tool.Canvas(width, 30)
        self.tool.dashboard_draw(canvas, model())
        return canvas.render(False).splitlines()[-1].ljust(width)

    def test_the_hints_are_centred_in_order_between_the_name_and_the_status(self) -> None:
        for width, shown in ((120, HINTS), (80, [hint for hint in HINTS if hint != "? help"])):
            with self.subTest(width=width):
                line = self.footer(width)
                self.assertTrue(line.startswith(" slotfather "), line)
                self.assertRegex(line.rstrip(), r"updated \d\d:\d\d:\d\d  ● live$")
                hints = "   ".join(shown)
                self.assertIn(hints, line, "navigation, settings, help, quit last; at 80 cells help gives way")
                start, status = line.index(hints), line.index("updated")
                self.assertEqual(min((width - len(hints)) // 2, status - 3 - len(hints)), start,
                                 "centred on the footer, or as near as the status leaves room for")
                if width == 120:
                    self.assertLessEqual(abs(start + len(hints) / 2 - width / 2), 1)

    def test_a_narrow_footer_drops_hints_from_the_right_but_quit_and_never_overlaps(self) -> None:
        line = self.footer(60)
        status = line.index("updated") if "updated" in line else line.index("● live")
        shown = [hint for hint in HINTS if hint in line]
        self.assertIn("q quit", shown, "quit is the last to go")
        self.assertEqual(shown, [hint for hint in HINTS if hint in shown], "the order stays")
        dropped = [hint for hint in HINTS if hint not in shown]
        self.assertTrue(dropped, "60 cells cannot hold every hint beside the name and status")
        self.assertEqual(set(dropped), set(["? help", "s settings", "↑↓ select"][:len(dropped)]),
                         "help goes first, then settings, then select")
        self.assertLess(max(line.index(hint) + len(hint) for hint in shown), status - 1, "never over the status")

    def test_the_removed_keys_and_states_are_gone(self) -> None:
        line = self.footer(120)
        for gone in ("pause", "refresh", "select text", "text selection", "paused"):
            self.assertNotIn(gone, line)
        help_text = " ".join(text for text, _ in self.tool.HELP_CONTENT)
        self.assertNotRegex(help_text, r"\bp or space\b|\br: refresh|\bm: ")
        self.assertIn("Shift", help_text, "text is selected with Shift+drag")


class RemovedKeyTests(SlotCase):
    def test_p_r_and_m_do_nothing_in_the_live_view(self) -> None:
        master, slave = pty.openpty()
        process: subprocess.Popen[bytes] | None = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            process = subprocess.Popen([str(ROOT / "slotfather"), "dashboard", "--interval", "60"],
                                       stdin=slave, stdout=slave, stderr=slave, env=self.env, start_new_session=True)
            os.close(slave)
            slave = -1
            screen = b""

            def read(seconds: float) -> None:
                nonlocal screen
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.05)[0]:
                        try:
                            screen += os.read(master, 65536)
                        except OSError:
                            return

            deadline = time.monotonic() + 8
            while b"QUEUE" not in screen and time.monotonic() < deadline:
                read(0.1)
            before = len(screen)
            for key in (b"p", b" ", b"r", b"m", b"M", b"R"):
                os.write(master, key)
            read(1.5)
            after = re.sub(rb"\x1b\[[0-9;?]*[ -/]*[@-~]", b"", screen[before:])
            self.assertNotIn(b"paused", after)
            self.assertNotIn(b"text selection", after)
            self.assertNotIn(b"\x1b[?1006l", screen[before:], "mouse reporting stays on")
            self.assertNotIn(b"\x1b[2J", screen[before:], "no refresh clears the screen")
            os.write(master, b"q")
            deadline = time.monotonic() + 8
            while process.poll() is None and time.monotonic() < deadline:
                read(0.1)
            self.assertEqual(0, process.wait(timeout=1))
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            os.close(master)
            if slave >= 0:
                os.close(slave)


if __name__ == "__main__":
    unittest.main()
