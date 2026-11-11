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


class HintClickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = importlib.reload(importlib.import_module("slotfather.dashboard"))

    def targets(self, canvas: Any, row: int, hints: list[str]) -> dict[str, list[str | None]]:
        """For each hint text on the row: what a click on each of its cells presses."""
        line = "".join(cell[0] for cell in canvas.cells[row])
        found: dict[str, list[str | None]] = {}
        for hint in hints:
            start = line.index(hint)
            found[hint] = [canvas.hint_at(x, row) for x in range(start, start + len(hint))]
        return found

    def test_every_footer_hint_presses_its_key_and_the_gaps_nothing(self) -> None:
        canvas = self.tool.Canvas(140, 30)
        self.tool.dashboard_draw(canvas, model())
        row = canvas.height - 1
        found = self.targets(canvas, row, HINTS)
        self.assertEqual(["up", "down"], found["↑↓ select"][:2], "each arrow its own key")
        for hint, key in (("tab panel", "\t"), ("s settings", "s"), ("? help", "?"), ("q quit", "q")):
            self.assertEqual({key}, set(found[hint]), f"the whole of {hint}, key and label")
        line = "".join(cell[0] for cell in canvas.cells[row])
        gap = line.index("tab panel") - 2
        self.assertIsNone(canvas.hint_at(gap, row), "between hints: nothing")

    def test_overlay_footers_are_click_targets_too(self) -> None:
        for which, hints in ((True, {"esc close": "esc", "q quit": "q"}), ("changelog", {"esc close": "esc"})):
            canvas = self.tool.Canvas(100, 30)
            self.tool.dashboard_draw(canvas, model(), help_open=which)
            info = canvas.help_view
            row = info.y + info.height - 2
            found = self.targets(canvas, row, ["↑↓ scroll", *hints])
            self.assertEqual(["up", "down"], found["↑↓ scroll"][:2])
            for hint, key in hints.items():
                self.assertEqual({key}, set(found[hint]), f"{which}: {hint}")

    def test_the_settings_footer_is_a_click_target_too(self) -> None:
        import os
        import tempfile
        from unittest import mock
        from slotfather import dashboard_settings
        directory = tempfile.mkdtemp(prefix="slotfather-hints-")
        with mock.patch.dict(os.environ, {"SLOTFATHER_DIR": directory, "SLOTFATHER_CORES": "12",
                                          "SLOTFATHER_MEMORY_GIB": "64"}):
            dialog = dashboard_settings.Settings(1.0)
            canvas = self.tool.Canvas(100, 44)
            dialog.draw(canvas, None)
        _, y, _, height = dialog.bounds
        found = self.targets(canvas, y + height - 2, ["↑↓ select", "←→ change", "enter edit", "d reset", "esc close"])
        self.assertEqual(["up", "down"], found["↑↓ select"][:2])
        self.assertEqual(["left", "right"], found["←→ change"][:2])
        for hint, key in (("enter edit", "\r"), ("d reset", "d"), ("esc close", "esc")):
            self.assertEqual({key}, set(found[hint]), hint)


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


class HintClickLiveTests(SlotCase):
    def test_clicks_on_footer_hints_act_like_their_keys(self) -> None:
        master, slave = pty.openpty()
        process: subprocess.Popen[bytes] | None = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 140, 0, 0))
            process = subprocess.Popen([str(ROOT / "slotfather"), "dashboard", "--interval", "60"],
                                       stdin=slave, stdout=slave, stderr=slave, env=self.env, start_new_session=True)
            os.close(slave)
            slave = -1
            screen = bytearray()

            def receive(fragment: str, since: int = 0) -> int:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.05)[0]:
                        try:
                            screen.extend(os.read(master, 65536))
                        except OSError:
                            break
                    plain = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", screen.decode("utf-8", "replace"))
                    if fragment in plain[since:]:
                        return len(plain)
                self.fail(f"terminal did not show {fragment!r}")

            def click(text: str, row: int) -> None:
                plain_rows = self.rows(bytes(screen))
                column = plain_rows[row].index(text) + 1  # 1-based for SGR
                os.write(master, f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode())

            mark = receive("q quit")
            click("? help", 30)
            mark = receive("esc close", mark)
            help_row = next(r for r, line in self.rows(bytes(screen)).items() if "esc close" in line)
            click("esc close", help_row)
            mark = receive("QUEUE", mark)
            click("tab panel", 30)
            mark = receive("↑↓ scroll", mark)
            click("s settings", 30)
            mark = receive("enter edit", mark)
            settings_row = next(r for r, line in self.rows(bytes(screen)).items() if "esc close" in line and "enter" in line)
            click("esc close", settings_row)
            mark = receive("QUEUE", mark)
            click("q quit", 30)
            deadline = time.monotonic() + 8
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        break
            self.assertEqual(0, process.wait(timeout=1), "a click on q quit quits")
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            os.close(master)
            if slave >= 0:
                os.close(slave)

    @staticmethod
    def rows(raw: bytes) -> dict[int, str]:
        """The screen's rows, 1-based, as last written (the dashboard writes whole rows after CSI row;1H)."""
        found: dict[int, str] = {}
        for match in re.finditer(r"\x1b\[(\d+);1H(.*?)(?=\x1b\[\d+;1H|$)", raw.decode("utf-8", "replace"), re.S):
            found[int(match[1])] = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", match[2])
        return found


if __name__ == "__main__":
    unittest.main()
