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

HINTS = ["↑↓ select", "tab panel", "s settings", "? help", "q quit"]
DROPS = ["? help", "s settings", "tab panel", "↑↓ select"]  # the order a narrow footer drops them in


def model() -> dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    return dict(holders=[], waiters=[], slots=4, cores=12, saved=False, averages=(1.0, 1.0, 1.0),
                advice=dict(unleased_load=0, jobs=4, capacity=4), admission=4, memory_block=None,
                memory=dict(level=1, used=512, total=1024, swap_total=0, swap_free=None), history=[], events=[],
                updated=now, order="backfill", start=None, limit=2700, used=0, free=4)


class FooterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = importlib.reload(importlib.import_module("slotfather.dashboard"))

    def footer(self, width: int, **extra: Any) -> str:
        canvas = self.tool.Canvas(width, 30)
        self.tool.dashboard_draw(canvas, dict(model(), **extra))
        return canvas.render(False).splitlines()[-1].ljust(width)

    def test_the_hints_are_centred_in_order_between_the_name_and_the_status(self) -> None:
        for width in (120, 80, 60):
            with self.subTest(width=width):
                line = self.footer(width)
                self.assertTrue(line.startswith(" Slotfather"), line)
                self.assertRegex(line.rstrip(), r"updated \d\d:\d\d:\d\d  ● live$")
                status = line.index("updated")
                shown = [hint for hint in HINTS if hint in line]
                self.assertIn("q quit", shown, "quit is the last to go")
                self.assertEqual(set(HINTS) - set(shown), set(DROPS[:len(HINTS) - len(shown)]), "help goes first")
                hints = "   ".join(shown)
                self.assertIn(hints, line, "navigation, panels, settings, help, quit last")
                start = line.index(hints)
                self.assertLess(start + len(hints), status - 1, "never over the status")
                if width == 120:
                    self.assertEqual(HINTS, shown)
                    self.assertLessEqual(abs(start + len(hints) / 2 - width / 2), 1, "centred on the footer")

    def test_the_version_follows_the_name_and_goes_before_any_hint(self) -> None:
        version = self.tool.running_version()
        self.assertTrue(version, "a commit or semver")
        self.assertIn(f" Slotfather {version} ", self.footer(120))
        self.assertIn(f" Slotfather {version} ↻ ", self.footer(120, installed="another release"),
                      "installed after this dashboard started: it runs older code until it restarts")
        self.assertNotIn("↻", self.footer(120, installed=self.tool.running_source()))
        narrow = self.footer(80)
        self.assertNotIn(version, narrow, "the version goes first")
        self.assertIn("↑↓ select", narrow)

    def test_a_scrollable_panel_has_the_arrows_scroll(self) -> None:
        canvas = self.tool.Canvas(120, 30)
        canvas.focus = "details"
        self.tool.dashboard_draw(canvas, model())
        self.assertIn("↑↓ scroll   tab panel", canvas.render(False).splitlines()[-1])

    def test_the_removed_keys_and_states_are_gone(self) -> None:
        line = self.footer(120)
        for gone in ("pause", "refresh", "select text", "text selection", "paused"):
            self.assertNotIn(gone, line)
        keys = dict(self.tool.HELP)["KEYS"]
        terms = [entry[0] for entry in keys if entry[0]]
        for gone in ("p", "r", "m", "space"):
            self.assertNotIn(gone, terms)
        self.assertIn("shift+drag", terms, "text is selected with Shift+drag")


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
