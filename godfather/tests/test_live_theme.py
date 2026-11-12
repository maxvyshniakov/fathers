"""Colours from the terminal's own theme (#14) in the live dashboard and in --once, moved from Slotfather's
FakeTerminalTests and OnceTests: the PTY answers colour queries as a real terminal does, a mode 2031 notification
asks again, and --once takes GODFATHER_THEME and GODFATHER_TERMINAL_BG/_FG (the SLOTFATHER_ names where unset)."""
from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import sys
import unittest

from helpers import ROOT
from terminal import LiveCase, environ, item

from godfather import keys as sf_keys
from godfather import theme
from godfather import tui

LIGHT_BG, LIGHT_FG = (0xFD, 0xFC, 0xFA), (0x2A, 0x2A, 0x2A)
DARK_BG, DARK_FG = (0x28, 0x2C, 0x34), (0xFF, 0xFF, 0xFF)
LIGHT_ENV = {"GODFATHER_TERMINAL_BG": "#fdfcfa", "GODFATHER_TERMINAL_FG": "#2a2a2a"}
DARK_ENV = {"GODFATHER_TERMINAL_BG": "#282c34", "GODFATHER_TERMINAL_FG": "#ffffff"}


class FakeTerminalTests(LiveCase):
    """The live dashboard against a PTY whose 'terminal' answers colour queries as a real one does."""

    def test_a_light_background_reported_by_the_terminal_colours_the_selection_and_2031_asks_again(self) -> None:
        screen = self.start()

        def answer(background: tuple[int, int, int], foreground: tuple[int, int, int]) -> None:
            screen.write(("\x1b]11;rgb:%02x%02x/%02x%02x/%02x%02x\x1b\\" % (
                background[0], background[0], background[1], background[1], background[2], background[2])
                + "\x1b]10;rgb:%02x/%02x/%02x\x07" % foreground).encode())

        screen.raw_until(lambda data: b"\x1b]11;?" in data and theme.DEVICE_ATTRIBUTES in data, what="the query")
        self.assertNotIn(sf_keys.KITTY_ON, screen.raw, "asked before the kitty push")
        answer(LIGHT_BG, LIGHT_FG)
        screen.write(b"\x1b[?62;22c")
        screen.raw_until(lambda data: b"QUEUE" in data, what="the dashboard")
        self.assertIn(theme.SCHEME_ON, screen.raw, "colour-scheme changes are subscribed to")
        screen.write(b"s")  # Settings: its focused row is selected
        light = ("48;2;%d;%d;%dm" % theme.mix(LIGHT_BG, LIGHT_FG, 0.10)).encode()
        screen.raw_until(lambda data: b"STATUS" in data and light in data, what="the light selection")
        self.assertNotIn(b"56;62;74", screen.raw)
        self.assertNotIn(b"38;5;255", screen.raw)
        answered = screen.mark()
        screen.write(b"\x1b[?997;1n")  # the terminal switched to dark
        screen.raw_until(lambda data: b"\x1b]11;?" in data[answered:], what="a second query after the notification")
        answer(DARK_BG, DARK_FG)
        dark = ("48;2;%d;%d;%dm" % theme.mix(DARK_BG, DARK_FG, 0.10)).encode()
        screen.raw_until(lambda data: dark in data[answered:], what="the dark selection after the answer")
        screen.write(b"\x1b")
        self.assertEqual(0, screen.quit())
        self.assertIn(sf_keys.KITTY_OFF + theme.SCHEME_OFF, screen.raw, "both modes reset in one place")

    def test_without_colour_the_width_probe_still_runs_and_glyphs_are_placed(self) -> None:
        self.queue(holders=[item("probe-owner", 500)])
        screen = self.start(NO_COLOR="1")
        screen.raw_until(lambda data: re.search(rb"\xe2\x96\xb6\xef\xb8\x8f\x1b\[\d+G", data), what="▶️ placed")
        self.assertIn(b"\x1b[6n", screen.raw, "the width of the joined glyph is measured with colour off too")
        self.assertEqual(0, screen.quit())

    def test_the_scheme_mode_is_reset_when_the_dashboard_fails(self) -> None:
        master, slave = pty.openpty()
        read, write = os.pipe()
        try:
            with self.assertRaises(RuntimeError):
                with tui.full_screen(slave, write, theme.Answers()):
                    raise RuntimeError("boom")
            out = b""
            while select.select([read], [], [], 0.1)[0]:
                out += os.read(read, 65536)
            self.assertLess(out.index(theme.QUERY), out.index(sf_keys.KITTY_ON))
            self.assertLess(out.index(theme.SCHEME_ON), out.index(theme.SCHEME_OFF))
        finally:
            for fd in (master, slave, read, write):
                os.close(fd)


class OnceTests(LiveCase):
    def once(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ROOT / "godfather"), "--once", "--color", "--width", "140",
                               "--height", "40"], capture_output=True, text=True, env=environ(self.fake, **extra))

    def test_once_honours_the_theme_and_an_explicit_background(self) -> None:
        self.queue(holders=[item("painted", 600)])
        light = self.once(**LIGHT_ENV)
        self.assertEqual(0, light.returncode, light.stderr)
        self.assertIn("48;2;%d;%d;%dm" % theme.resolve("auto", None, LIGHT_ENV).tints["h0"], light.stdout,
                      "the selected lease on its own tint (#15)")
        self.assertNotIn("56;62;74", light.stdout)
        self.assertNotIn("38;5;255", light.stdout, "no white text on a light background")
        builtin = self.once(GODFATHER_THEME="builtin", **LIGHT_ENV)
        self.assertIn("48;2;56;62;74m", builtin.stdout)
        for environment, bg, fg in ((LIGHT_ENV, LIGHT_BG, LIGHT_FG), (DARK_ENV, DARK_BG, DARK_FG)):
            drawn = self.once(**environment).stdout
            for share in (theme.TRACK_MIX, theme.FREE_MIX):  # the bar tracks and the free slots
                self.assertIn("\x1b[0;38;2;%d;%d;%dm" % theme.mix(bg, fg, share), drawn)
            dim = theme.muted(fg, bg)  # ours, from the theme's own fg and bg
            self.assertIn("\x1b[0;38;2;%d;%d;%dm" % theme.frame(dim, bg) + "╭", drawn, "frames, a little paler")
            self.assertIn("\x1b[0;38;2;%d;%d;%dm" % dim + "status", drawn, "muted text")
        unknown = self.once()
        self.assertNotIn("48;2;", unknown.stdout, "nothing known: no background colour at all")
        self.assertIn("\x1b[0;1m", unknown.stdout)
        for name, value, message in (("GODFATHER_THEME", "dark", "auto, terminal or builtin"),
                                     ("GODFATHER_TERMINAL_BG", "beige", "GODFATHER_TERMINAL_BG"),
                                     ("SLOTFATHER_THEME", "dark", "SLOTFATHER_THEME takes"),
                                     ("SLOTFATHER_TERMINAL_BG", "beige", "SLOTFATHER_TERMINAL_BG")):
            refused = self.once(**{name: value})
            self.assertEqual(2, refused.returncode, name)
            self.assertIn(message, refused.stderr)

    def test_the_slotfather_names_still_set_the_theme_where_the_new_ones_are_unset(self) -> None:
        self.queue(holders=[item("painted", 600)])
        old = {"SLOTFATHER_TERMINAL_BG": "#fdfcfa", "SLOTFATHER_TERMINAL_FG": "#2a2a2a"}
        tint = "48;2;%d;%d;%dm" % theme.resolve("auto", None, LIGHT_ENV).tints["h0"]
        self.assertIn(tint, self.once(**old).stdout, "SLOTFATHER_TERMINAL_BG/_FG as GODFATHER_ ones")
        builtin = self.once(SLOTFATHER_THEME="builtin", **LIGHT_ENV)
        self.assertIn("48;2;56;62;74m", builtin.stdout)
        overridden = self.once(SLOTFATHER_THEME="builtin", GODFATHER_THEME="auto", **LIGHT_ENV)
        self.assertNotIn("48;2;56;62;74m", overridden.stdout, "GODFATHER_THEME wins")


if __name__ == "__main__":
    unittest.main()
