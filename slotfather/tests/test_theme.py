"""Dashboard colours from the terminal's theme (#14): tokens, mixed surfaces, holder colours and the chart gradient."""
from __future__ import annotations

import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import fcntl
import importlib
import itertools
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from typing import Any, Callable
from unittest import mock

from slotfather import dashboard_settings as sf_settings
from slotfather import keys as sf_keys
from slotfather import theme

from test_slotfather import ROOT, SlotCase

LIGHT_BG, LIGHT_FG = (0xFD, 0xFC, 0xFA), (0x2A, 0x2A, 0x2A)
DARK_BG, DARK_FG = (0x28, 0x2C, 0x34), (0xFF, 0xFF, 0xFF)
LIGHT_ENV = {"SLOTFATHER_TERMINAL_BG": "#fdfcfa", "SLOTFATHER_TERMINAL_FG": "#2a2a2a"}
DARK_ENV = {"SLOTFATHER_TERMINAL_BG": "#282c34", "SLOTFATHER_TERMINAL_FG": "#ffffff"}
BACKGROUNDS = {"light": [LIGHT_BG, (255, 255, 255), (238, 232, 213)], "dark": [DARK_BG, (0, 0, 0), (30, 30, 46)]}
# The palette's red the "over share" bar uses: Ghostty's default and a light theme's (GitHub Light)
RED = {"light": (0xCF, 0x22, 0x2E), "dark": (0xCC, 0x66, 0x66)}


def answered(**replies: Any) -> theme.Answers:
    answers = theme.Answers()
    for name, colour in replies.items():
        code = {"fg": "10", "bg": "11"}.get(name) or "4;" + name.lstrip("p")
        answers.feed(f"\x1b]{code};rgb:{colour[0]:02x}/{colour[1]:02x}/{colour[2]:02x}\x1b\\")
    return answers


class ColourTests(unittest.TestCase):
    def test_oklab_round_trips_and_mixes_like_color_mix(self) -> None:
        for rgb in ((0, 0, 0), (255, 255, 255), (253, 252, 250), (40, 44, 52), (95, 175, 255), (204, 102, 102)):
            self.assertEqual(rgb, theme.from_oklab(theme.oklab(rgb)))
        self.assertEqual((0, 0, 0), theme.mix((0, 0, 0), (255, 255, 255), 0))
        self.assertEqual((255, 255, 255), theme.mix((0, 0, 0), (255, 255, 255), 1))
        middle = theme.mix((0, 0, 0), (255, 255, 255), 0.5)
        self.assertAlmostEqual(0.5, theme.oklab(middle)[0], places=2, msg="halfway in perceived lightness")
        self.assertEqual(middle[0], middle[1])
        self.assertEqual(middle[1], middle[2])
        light = theme.mix(LIGHT_BG, LIGHT_FG, 0.10)
        self.assertTrue(theme.oklab(LIGHT_FG)[0] < theme.oklab(light)[0] < theme.oklab(LIGHT_BG)[0])
        self.assertLess(theme.distance(light, LIGHT_BG), theme.distance(light, LIGHT_FG))

    def test_contrast_is_wcag(self) -> None:
        self.assertAlmostEqual(21, theme.contrast((0, 0, 0), (255, 255, 255)))
        self.assertAlmostEqual(1, theme.contrast(DARK_BG, DARK_BG))
        self.assertEqual(theme.contrast(LIGHT_BG, DARK_BG), theme.contrast(DARK_BG, LIGHT_BG))
        self.assertAlmostEqual(4.54, theme.contrast((118, 118, 118), (255, 255, 255)), places=2)  # #767676, the WCAG AA grey
        self.assertTrue(theme.is_light(LIGHT_BG))
        self.assertFalse(theme.is_light(DARK_BG))

    def test_oklch_stays_inside_srgb(self) -> None:
        for hue in range(0, 360, 15):
            colour = theme.oklch(0.7, 0.4, hue)  # far outside sRGB at any hue: chroma comes down, hue stays
            self.assertTrue(all(0 <= channel <= 255 for channel in colour))
            self.assertAlmostEqual(0.7, theme.oklab(colour)[0], places=1)


def hexes(text: str) -> list[tuple[int, int, int]]:
    return [(int(code[1:3], 16), int(code[3:5], 16), int(code[5:7], 16)) for code in text.split()]


# The owner's mock (2026-11-11): fill and ink on a light background, in the order holders get them
MOCK_LIGHT = list(zip(hexes("#f2668b #2ec4a0 #9b7bf4 #ff9248 #2bb5b0 #d77ae6 #9ad94a"),
                      hexes("#c8264f #0b7f63 #6a45d6 #c2530a #0f7f7a #9d3bb0 #4a7a12")))
REFERENCES = {"light": (0xFD, 0xFC, 0xFA), "dark": (0x1E, 0x1E, 0x1E)}
# Terminal palettes' (blue, red, yellow): focus, over share and idle
RESERVED = {"light": [hexes("#0969da #cf222e #4d2d00"), hexes("#0000ee #cd0000 #cdcd00"),
                      hexes("#4078f2 #e45649 #c18401")],  # GitHub Light, xterm, One Light
            "dark": [hexes("#81a2be #cc6666 #f0c674"), hexes("#61afef #e06c75 #e5c07b"), hexes("#0000ee #cd0000 #cdcd00"),
                     hexes("#bd93f9 #ff5555 #f1fa8c"), hexes("#89b4fa #f38ba8 #f9e2af")]}  # Ghostty, One Dark, xterm,
# Dracula, Catppuccin Mocha
ORDER = ["rose", "emerald", "violet", "tangerine", "teal", "orchid", "lime", "pink", "cyan", "grape"]


def every_theme() -> list[tuple[str, tuple[int, int, int], Any]]:
    return [(kind, background, reserved) for kind, backgrounds in BACKGROUNDS.items()
            for background in [*backgrounds, REFERENCES[kind]] for reserved in [None, *RESERVED[kind]]]


class HolderColourTests(unittest.TestCase):
    def test_every_holder_has_a_fill_and_an_ink_of_its_hue(self) -> None:
        for kind, background, reserved in every_theme():
            with self.subTest(kind=kind, background=background, reserved=reserved):
                for holder in theme.holder_pairs(background, kind == "light", reserved):
                    fill, ink = theme.lch(holder.fill), theme.lch(holder.ink)
                    self.assertLess(abs((fill[2] - ink[2] + 180) % 360 - 180), 12, f"{holder} keeps its hue")
                    if kind == "light":
                        self.assertLess(ink[0], fill[0] - 0.08, f"{holder}: the ink is darker on a light background")
                    else:
                        self.assertGreater(ink[0], fill[0] + 0.04, f"{holder}: and lighter on a dark one")
                    self.assertGreater(fill[1], 0.1, f"{holder}: a juicy fill, not a muddy one")
                    self.assertTrue(0.65 <= fill[0] <= 0.86, f"{holder}: a candy lightness")

    def test_every_ink_reads_at_four_and_a_half_to_one(self) -> None:
        for kind, background, reserved in every_theme():
            with self.subTest(kind=kind, background=background, reserved=reserved):
                for holder in theme.holder_pairs(background, kind == "light", reserved):
                    self.assertGreaterEqual(theme.contrast(holder.ink, background), 4.5, holder)
        for light, reference in ((True, REFERENCES["light"]), (False, REFERENCES["dark"])):
            for holder in theme.holder_pairs(None, light):  # an unknown background: measured on the reference
                self.assertGreaterEqual(theme.contrast(holder.ink, reference), 4.5, holder)

    def test_no_fill_sits_near_the_focus_blue_the_over_share_red_or_the_idle_yellow(self) -> None:
        for kind, background, reserved in every_theme():
            with self.subTest(kind=kind, background=background, reserved=reserved):
                holders = theme.holder_pairs(background, kind == "light", reserved)
                blue, red, yellow = reserved or (theme.LIGHT_RESERVED if kind == "light" else theme.DARK_RESERVED)
                for holder in holders:
                    self.assertGreaterEqual(theme.distance(holder.fill, blue), theme.RESERVED_APART, f"{holder}: focus")
                    self.assertGreaterEqual(theme.distance(holder.fill, red), theme.RESERVED_APART, f"{holder}: over")
                    self.assertGreaterEqual(theme.distance(holder.fill, yellow), theme.RESERVED_APART, f"{holder}: idle")
                for first, second in itertools.combinations(holders, 2):
                    self.assertGreaterEqual(theme.distance(first.fill, second.fill), theme.HOLDER_APART - 1e-9)
                self.assertGreaterEqual(len(holders), 7, "a palette takes a few hues at most")
                for holder in holders:  # no blue, sky or indigo, whichever way a hue turned
                    self.assertFalse(225 < theme.hue(holder.fill) < 280, holder)

    def test_the_hues_come_in_the_owners_order_and_a_reserved_one_drops_out(self) -> None:
        self.assertEqual(ORDER, [name for name, *_ in theme.HOLDER_HUES])
        for light in (True, False):
            self.assertEqual(ORDER, [holder.name for holder in theme.holder_pairs(None, light)])
        dracula = theme.holder_pairs(DARK_BG, False, RESERVED["dark"][3])  # its blue is a violet
        self.assertEqual([name for name in ORDER if name != "violet"], [holder.name for holder in dracula],
                         "violet has no room left that is not blue: dropped, the rest keep their order")

    def test_the_light_set_is_the_owners_mock(self) -> None:
        holders = theme.holder_pairs(REFERENCES["light"], True)
        for holder, (fill, ink) in zip(holders, MOCK_LIGHT):
            self.assertLess(theme.distance(holder.fill, fill), 0.02, holder)
            self.assertLess(theme.distance(holder.ink, ink), 0.03, holder)

    def test_the_queried_palette_sets_what_is_reserved(self) -> None:
        blue = theme.hex_colour("#2bb5b0")  # a teal accent: the teal holder must move or go
        found = theme.resolve("auto", answered(bg=LIGHT_BG, p4=blue), {})
        fills = [found.styles[f"f{n}"][0] for n in range(found.holders())]
        for fill in fills:
            self.assertGreaterEqual(theme.distance(cast_rgb(fill), blue), theme.RESERVED_APART)
        self.assertEqual([holder.fill for holder in theme.holder_pairs(LIGHT_BG, True, (blue,) + theme.LIGHT_RESERVED[1:])],
                         fills)

    def test_a_dark_background_known_or_not_gives_the_dark_set(self) -> None:
        self.assertEqual(theme.holder_pairs(None, False), theme.holder_pairs(REFERENCES["dark"], False))


def cast_rgb(colour: Any) -> tuple[int, int, int]:
    assert isinstance(colour, tuple)
    return colour  # type: ignore[return-value]


class GradientTests(unittest.TestCase):
    def test_the_chart_runs_from_the_themes_blue_to_its_red_in_oklab(self) -> None:
        blue, red = (0x7A, 0xA6, 0xDA), (0xD5, 0x4E, 0x53)
        steps = theme.gradient(blue, red)
        self.assertEqual(8, len(steps))
        self.assertEqual((blue, red), (steps[0], steps[-1]))
        for index, colour in enumerate(steps):
            self.assertEqual(theme.mix(blue, red, index / 7), colour)
        for colour in steps[1:-1]:  # no rainbow: the path never passes through green or yellow
            self.assertFalse(70 < theme.hue(colour) < 200, colour)
        found = theme.resolve("auto", answered(bg=DARK_BG, p12=blue, p9=red, p4=(0x81, 0xA2, 0xBE),
                                               p1=(0xCC, 0x66, 0x66)), {})
        self.assertIn((found.styles["chart0"][0], found.styles["chart7"][0]),
                      [(blue, red), ((0x81, 0xA2, 0xBE), (0xCC, 0x66, 0x66))])
        only = theme.resolve("auto", answered(bg=DARK_BG, p4=(0, 0, 238), p1=(205, 0, 0)), {})
        self.assertEqual(((0, 0, 238), (205, 0, 0)), (only.styles["chart0"][0], only.styles["chart7"][0]))

    def test_without_the_palette_the_chart_is_fixed_blue_to_red(self) -> None:
        found = theme.resolve("auto", None, {})
        self.assertEqual((theme.FIXED_LOW, theme.FIXED_HIGH), (found.styles["chart0"][0], found.styles["chart7"][0]))
        self.assertEqual([4, 4, 4, 5, 5, 1, 1, 1], [theme.resolve("terminal", None, {}).styles[f"chart{n}"][0]
                                                    for n in range(8)], "terminal: palette steps, nothing mixed")


class ReplyTests(unittest.TestCase):
    def test_colour_specs(self) -> None:
        self.assertEqual((253, 252, 250), theme.parse_colour("rgb:fdfd/fcfc/fafa"))
        self.assertEqual((255, 0, 136), theme.parse_colour("rgb:f/0/88"))
        self.assertEqual((255, 0, 136), theme.parse_colour("rgba:ffff/0000/8888/ffff"))
        self.assertEqual((253, 252, 250), theme.parse_colour("#fdfcfa"))
        self.assertEqual((255, 255, 255), theme.parse_colour("#fff"))
        for bad in ("", "rgb:", "rgb:zz/00/00", "#ff", "red", "rgb:12345/0/0"):
            self.assertIsNone(theme.parse_colour(bad), bad)

    def test_osc_10_11_and_4_replies_with_either_terminator(self) -> None:
        answers = theme.Answers()
        self.assertTrue(answers.feed("\x1b]11;rgb:fdfd/fcfc/fafa\x1b\\"))
        self.assertTrue(answers.feed("\x1b]10;rgb:2a2a/2a2a/2a2a\x07"))
        self.assertTrue(answers.feed("\x1b]4;12;rgb:0909/6969/dada\x1b\\"))
        self.assertFalse(answers.feed("\x1b]4;12;rgb:0909/6969/dada\x1b\\"), "the same colour is no change")
        self.assertFalse(answers.feed("\x1b]11;?\x1b\\"), "an echoed query is not a colour")
        self.assertEqual((LIGHT_BG, LIGHT_FG, {12: (9, 105, 218)}),
                         (answers.background, answers.foreground, answers.palette))
        self.assertTrue(answers.feed("\x1b[?997;2n"))
        self.assertEqual("light", answers.scheme)
        self.assertFalse(answers.finished)
        answers.feed("\x1b[?62;22c")
        self.assertTrue(answers.finished)

    def test_reports_are_split_from_keys_and_a_cut_one_waits(self) -> None:
        reports, rest, partial = theme.split_reports("j\x1b]11;rgb:0/0/0\x1b\\k\x1b[?997;1nq\x1b]10;rgb:ff")
        self.assertEqual(["\x1b]11;rgb:0/0/0\x1b\\", "\x1b[?997;1n"], reports)
        self.assertEqual(("jkq", "\x1b]10;rgb:ff"), (rest, partial))
        self.assertEqual(([], "", "\x1b]10;rgb:ff\x1b"), theme.split_reports("\x1b]10;rgb:ff\x1b"))
        self.assertEqual(([], "\x1b", ""), theme.split_reports("\x1b"), "a lone Esc stays a key")

    def pipes(self) -> tuple[int, int, int, int]:
        terminal_in, app_in = os.pipe()  # what the terminal answers, the application reads
        app_out, terminal_out = os.pipe()  # what the application writes, the terminal reads
        for fd in (terminal_in, app_in, app_out, terminal_out):
            self.addCleanup(os.close, fd)
        return terminal_in, app_in, app_out, terminal_out

    def test_ask_reads_replies_split_across_reads_until_the_device_attributes(self) -> None:
        reader, writer, query_out, query_in = self.pipes()
        pieces = [b"\x1b]11;rgb:fd", b"fd/fcfc/fafa\x1b", b"\\x\x1b]10;rgb:2a2a/2a2a/2a2a\x07", b"\x1b[?62;", b"22c"]

        def terminal() -> None:
            for piece in pieces:
                time.sleep(0.02)
                os.write(writer, piece)

        thread = threading.Thread(target=terminal)
        thread.start()
        answers, started = theme.Answers(), time.monotonic()
        typed = theme.ask(reader, query_in, answers, timeout=3)
        thread.join()
        self.assertLess(time.monotonic() - started, 2, "the device attributes end the wait, not the timeout")
        self.assertEqual((LIGHT_BG, LIGHT_FG, "x", True), (answers.background, answers.foreground, typed,
                                                            answers.finished))
        asked = os.read(query_out, 4096)
        self.assertTrue(asked.startswith(theme.QUERY) and asked.endswith(theme.DEVICE_ATTRIBUTES))
        for code in (b"\x1b]10;?", b"\x1b]11;?", b"\x1b]4;1;?", b"\x1b]4;4;?", b"\x1b]4;9;?", b"\x1b]4;12;?"):
            self.assertIn(code, asked)

    def test_ask_gives_up_after_the_timeout_without_an_answer(self) -> None:
        reader, _writer, _query_out, query_in = self.pipes()
        answers, started = theme.Answers(), time.monotonic()
        self.assertEqual("", theme.ask(reader, query_in, answers))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, theme.QUERY_TIMEOUT * 0.9)
        self.assertLess(elapsed, 1.0, "about 100 ms in all, with room for a loaded machine")
        self.assertEqual((None, None, {}), (answers.background, answers.foreground, answers.palette))

    def test_a_stray_osc_start_never_holds_the_keys_after_it(self) -> None:
        tool = importlib.import_module("slotfather.dashboard")
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        os.write(write, b"\x1b]")  # Alt+] where the kitty protocol is off
        self.assertEqual([], tool.read_keys(read))
        os.write(write, b"jk")
        self.assertEqual(["esc", "]", "j", "k"], tool.read_keys(read), "the next keys come through at once")
        os.write(write, b"\x1b]1" + b"x" * 100)  # longer than any colour report
        self.assertIn("x", tool.read_keys(read))
        self.assertEqual(([], "\x1b]j", ""), theme.split_reports("\x1b]j"))

    def test_a_reply_split_across_key_reads_is_never_read_as_keys(self) -> None:
        tool = importlib.import_module("slotfather.dashboard")
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        reports: list[str] = []
        os.write(write, b"j\x1b]11;rgb:fdfd/fc")
        first = tool.read_keys(read, reports)
        os.write(write, b"fc/fafa\x1b\\k\x1b[?997;2n")
        second = tool.read_keys(read, reports)
        self.assertEqual((["j"], ["k"]), (first, second), "no r, g, b or digits leak into the keys")
        self.assertEqual(["\x1b]11;rgb:fdfd/fcfc/fafa\x1b\\", "\x1b[?997;2n"], reports)


class FallbackTests(unittest.TestCase):
    def test_a_reported_background_mixes_the_surfaces(self) -> None:
        found = theme.resolve("auto", answered(bg=LIGHT_BG, fg=LIGHT_FG), {})
        self.assertEqual(theme.mix(LIGHT_BG, LIGHT_FG, theme.SELECTION_MIX), found.selection)
        self.assertEqual(theme.mix(LIGHT_BG, LIGHT_FG, theme.TRACK_MIX), found.styles["track"][0])
        self.assertEqual(theme.mix(LIGHT_BG, LIGHT_FG, theme.FREE_MIX), found.styles["free"][0])
        self.assertEqual(("■", "block", True, "terminal", None), (found.track, found.free, found.light, found.source,
                                                                  found.selected_text))
        self.assertEqual([(h.fill, h.ink) for h in theme.holder_pairs(LIGHT_BG, True)],
                         [(found.styles[f"f{n}"][0], found.styles[f"h{n}"][0]) for n in range(10)])

    def test_tokens_follow_the_palette_and_the_default_foreground(self) -> None:
        for mode in ("auto", "terminal"):
            styles = theme.resolve(mode, answered(bg=DARK_BG), {}).styles
            for name in ("text", "bold", "title", "key", "cursor", "status-normal"):
                self.assertIsNone(styles[name][0], f"{name} is the terminal's own foreground, never white")
            self.assertEqual({"dim": 8, "accent": 4, "ok": 2, "warn": 3, "bad": 1},
                             {name: styles[name][0] for name in ("dim", "accent", "ok", "warn", "bad")})
            self.assertNotIn("frame", styles, "frames are drawn in the muted token")

    def test_the_environment_overrides_the_terminal_and_colorfgbg_stands_in(self) -> None:
        found = theme.resolve("auto", answered(bg=DARK_BG, fg=DARK_FG), LIGHT_ENV)
        self.assertEqual(("environment", LIGHT_BG, theme.mix(LIGHT_BG, LIGHT_FG, 0.10)),
                         (found.source, found.background, found.selection))
        light = theme.resolve("auto", None, {"COLORFGBG": "0;15"})
        self.assertEqual(("COLORFGBG", True, theme.mix((255, 255, 255), (0, 0, 0), 0.10)),
                         (light.source, light.light, light.selection))
        self.assertEqual([h.ink for h in theme.holder_pairs((255, 255, 255), True)],
                         [light.styles[f"h{n}"][0] for n in range(10)])
        dark = theme.resolve("auto", None, {"COLORFGBG": "15;default;0"})
        self.assertEqual(("COLORFGBG", False), (dark.source, dark.light))

    def test_nothing_known_draws_surfaces_without_a_background_colour(self) -> None:
        for environ in ({}, {"COLORFGBG": "nonsense"}):
            found = theme.resolve("auto", theme.Answers(), environ)
            self.assertEqual((None, "░", "outline", "none", False),
                             (found.selection, found.track, found.free, found.source, found.light))
            self.assertEqual((8, 8), (found.styles["track"][0], found.styles["free"][0]))
            self.assertEqual([h.ink for h in theme.holder_pairs(None, False)], [found.styles[f"h{n}"][0] for n in range(10)])
        hinted = theme.Answers()
        hinted.feed("\x1b[?997;2n")
        self.assertTrue(theme.resolve("auto", hinted, {}).light, "a light-scheme notification picks the light set")
        switched = theme.resolve("auto", hinted, {"COLORFGBG": "15;0"})
        self.assertEqual((True, None, "none"), (switched.light, switched.selection, switched.source),
                         "a notification since the shell started outranks COLORFGBG")

    def test_terminal_mixes_nothing_and_builtin_is_the_palette_before(self) -> None:
        terminal = theme.resolve("terminal", answered(bg=LIGHT_BG, fg=LIGHT_FG), {})
        self.assertEqual((None, "░", "outline"), (terminal.selection, terminal.track, terminal.free))
        self.assertEqual(theme.holder_pairs(LIGHT_BG, True)[0].ink, terminal.styles["h0"][0], "holders still read")
        builtin = theme.resolve("builtin", answered(bg=LIGHT_BG), {})
        self.assertEqual(((56, 62, 74), 252, "■", "block"),
                         (builtin.selection, builtin.selected_text, builtin.track, builtin.free))
        self.assertEqual([75, 79, 216, 141, 110, 186], [builtin.styles[f"h{n}"][0] for n in range(6)])
        self.assertEqual([75, 79, 216, 141, 110, 186], [builtin.styles[f"f{n}"][0] for n in range(6)], "one token")
        self.assertEqual((255, 175, 95), builtin.styles["chart7"][0])

    def test_modes_and_colours_from_the_environment_are_checked(self) -> None:
        self.assertEqual("auto", theme.checked_mode(None))
        self.assertEqual("builtin", theme.checked_mode("builtin"))
        with self.assertRaisesRegex(ValueError, "auto, terminal or builtin"):
            theme.checked_mode("dark")
        with self.assertRaisesRegex(ValueError, "SLOTFATHER_TERMINAL_BG"):
            theme.resolve("auto", None, {"SLOTFATHER_TERMINAL_BG": "beige"})


class DrawingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = importlib.reload(importlib.import_module("slotfather.dashboard"))

    def sgr_of(self, canvas: Any, row: int, text: str) -> str:
        line = canvas.lines(True)[row]
        found = re.search(r"(\x1b\[[0-9;]*m)" + re.escape(text), line)
        assert found, (text, line)
        return found[1]

    def test_frames_and_muted_text_share_one_colour_in_light_and_dark(self) -> None:
        for name, theme_now in (("light", theme.resolve("auto", None, LIGHT_ENV)),
                                ("dark", theme.resolve("auto", None, DARK_ENV)),
                                ("unknown", theme.resolve("auto", None, {})),
                                ("terminal", theme.resolve("terminal", None, LIGHT_ENV)),
                                ("builtin", theme.resolve("builtin", None, {}))):
            with self.subTest(name):
                self.tool.set_theme(theme_now)
                canvas = self.tool.Canvas(40, 4)
                canvas.box(0, 0, 4, 40, "TITLE")
                canvas.put(1, 2, "muted words", "dim")
                frame = self.sgr_of(canvas, 0, "╭")
                self.assertEqual(frame, self.sgr_of(canvas, 1, "│"))
                self.assertEqual(frame, self.sgr_of(canvas, 1, "muted words"))
                self.assertEqual(frame, self.sgr_of(canvas, 3, "╰"))
                self.assertNotIn("239", frame, "never the near-black of the palette before")

    def test_the_selection_is_the_mixed_colour_or_bold(self) -> None:
        for environ, expect in ((LIGHT_ENV, "48;2;%d;%d;%d" % theme.mix(LIGHT_BG, LIGHT_FG, 0.10)),
                                (DARK_ENV, "48;2;%d;%d;%d" % theme.mix(DARK_BG, DARK_FG, 0.10))):
            self.tool.set_theme(theme.resolve("auto", None, environ))
            self.assertEqual(f"\x1b[0;{expect}m", self.tool.sgr("text", True), "selected text keeps its colour")
            self.assertNotIn("56;62;74", self.tool.sgr("h0", True))
        self.tool.set_theme(theme.resolve("auto", None, {}))
        self.assertEqual("\x1b[0;1m", self.tool.sgr("text", True), "no surface colour: bold beside the cursor")
        self.tool.set_theme(theme.resolve("builtin", None, {}))
        self.assertEqual("\x1b[0;38;5;252;48;2;56;62;74m", self.tool.sgr("text", True))

    def test_tracks_and_free_slots_without_surface_colours(self) -> None:
        self.tool.set_theme(theme.resolve("auto", None, {}))
        self.assertEqual([("■■", "accent"), ("░░░", "track")], self.tool.bar(0.4, 5, "accent"))
        self.assertEqual(("╭──╮", "▭"), (self.tool.free_slot(4), self.tool.free_slot(1)))
        self.tool.set_theme(theme.resolve("auto", None, DARK_ENV))
        self.assertEqual([("■■", "accent"), ("■■■", "track")], self.tool.bar(0.4, 5, "accent"))
        self.assertEqual("🮅🮅🮅🮅", self.tool.free_slot(4))
        self.assertEqual("o", "▭".translate(self.tool.ASCII))

    def test_builtin_keeps_six_colours_and_gives_a_freed_one_first(self) -> None:
        def holders(count: int) -> list[dict[str, Any]]:
            return [dict(key=n, kind="held") for n in range(count)]

        self.tool.set_theme(theme.resolve("builtin", None, {}))
        assigned: dict[Any, int] = {}
        model = {"holders": holders(7)}
        self.tool.dashboard_colours(model, assigned)
        self.assertEqual([f"h{n}" for n in (0, 1, 2, 3, 4, 5, 0)], [h["style"] for h in model["holders"]])
        model = {"holders": [h for h in holders(7) if h["key"] != 1] + [dict(key=7, kind="held")]}
        self.tool.dashboard_colours(model, assigned)
        self.assertEqual("h1", model["holders"][-1]["style"], "the colour the ended lease freed")
        self.tool.set_theme(theme.resolve("auto", None, {}))
        model = {"holders": holders(11)}
        self.tool.dashboard_colours(model, {})
        self.assertEqual([f"h{n}" for n in range(10)] + ["h0"], [h["style"] for h in model["holders"]])


class SettingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = _Path(tempfile.mkdtemp(prefix="slotfather-theme-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        environ = {key: value for key, value in os.environ.items() if not key.startswith("SLOTFATHER")}
        environ.update(SLOTFATHER_DIR=str(self.directory / "state"), SLOTFATHER_CORES="12", SLOTFATHER_MEMORY_GIB="64")
        patcher = mock.patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def test_the_theme_is_a_choice_of_this_dashboard_saved_once_settled(self) -> None:
        dialog = sf_settings.Settings(1.0, "auto", clock=self.clock)
        self.assertIn("theme", dialog.fields())
        self.assertEqual("Colour theme", dialog.label("theme"))
        dialog.select("theme")
        for direction, expected in ((1, "terminal"), (1, "builtin"), (1, "auto"), (-1, "builtin")):
            dialog.step(direction)
            self.assertEqual(expected, dialog.shown_value("theme"))
        self.assertEqual("auto", dialog.theme, "nothing saved while the keys move")
        self.now += sf_settings.SETTLE_SECONDS
        self.assertTrue(dialog.tick())
        self.assertEqual("builtin", dialog.theme)
        self.assertIn("this dashboard only", (dialog.flash or ("",))[0])
        self.assertFalse((self.directory / "state" / "order.json").exists(), "never written to the shared queue")
        self.assertEqual("handled", dialog.key("5"), "a choice takes no typed value")
        self.assertFalse(dialog.editing)
        self.assertEqual((None, "This setting takes no typed value."), dialog.check("theme", "auto"))
        self.assertEqual("changed", dialog.key("d"))
        self.assertEqual("auto", dialog.theme, "d: back to the default")
        dialog.theme = "terminal"
        dialog.key("D")
        dialog.key("D")
        self.assertEqual("auto", dialog.theme, "D twice: every setting at its default")
        with self.assertRaises(ValueError):
            sf_settings.Settings(1.0, "solarized")

    def test_the_panel_shows_the_theme_last_after_the_refresh(self) -> None:
        tool = importlib.import_module("slotfather.dashboard")
        dialog = sf_settings.Settings(1.0, "builtin", clock=self.clock)
        canvas = tool.Canvas(100, 44)
        dialog.draw(canvas, None)
        text = canvas.render(False)
        self.assertRegex(text, r"Colour theme +Built-in")
        self.assertLess(text.index("Refresh every"), text.index("Colour theme"))


class OnceTests(SlotCase):
    def test_once_honours_the_theme_and_an_explicit_background(self) -> None:
        holder = self.request("painted", 1)
        self.started("painted")
        light = self.tool("dashboard", "--once", "--color", "--width", "140", "--height", "40", **LIGHT_ENV)
        self.assertEqual(0, light.returncode, light.stderr)
        self.assertIn("48;2;%d;%d;%dm" % theme.resolve("auto", None, LIGHT_ENV).tints["h0"], light.stdout,
                      "the selected lease on its own tint (#15)")
        self.assertNotIn("56;62;74", light.stdout)
        self.assertNotIn("38;5;255", light.stdout, "no white text on a light background")
        builtin = self.tool("dashboard", "--once", "--color", "--width", "140", "--height", "40",
                            SLOTFATHER_THEME="builtin", **LIGHT_ENV)
        self.assertIn("48;2;56;62;74m", builtin.stdout)
        for environ, bg, fg in ((LIGHT_ENV, LIGHT_BG, LIGHT_FG), (DARK_ENV, DARK_BG, DARK_FG)):
            drawn = self.tool("dashboard", "--once", "--color", "--width", "140", "--height", "40", **environ).stdout
            for share in (theme.TRACK_MIX, theme.FREE_MIX):  # the bar tracks and the free slots
                self.assertIn("\x1b[0;38;2;%d;%d;%dm" % theme.mix(bg, fg, share), drawn)
            self.assertIn("\x1b[0;90m╭", drawn, "frames in the muted token")
            self.assertIn("\x1b[0;90mstatus", drawn, "muted text in the same token")
        unknown = self.tool("dashboard", "--once", "--color", "--width", "140", "--height", "40")
        self.assertNotIn("48;2;", unknown.stdout, "nothing known: no background colour at all")
        self.assertIn("\x1b[0;1m", unknown.stdout)
        for name, value, message in (("SLOTFATHER_THEME", "dark", "auto, terminal or builtin"),
                                     ("SLOTFATHER_TERMINAL_BG", "beige", "SLOTFATHER_TERMINAL_BG")):
            refused = self.tool("dashboard", "--once", **{name: value})
            self.assertEqual(2, refused.returncode)
            self.assertIn(message, refused.stderr)
        self.release("painted")
        self.finish(holder)


class FakeTerminalTests(SlotCase):
    """The live dashboard against a PTY whose 'terminal' answers colour queries as a real one does."""

    def test_a_light_background_reported_by_the_terminal_colours_the_selection_and_2031_asks_again(self) -> None:
        env = {key: value for key, value in self.env.items() if key != "COLORFGBG"}
        master, slave = pty.openpty()
        process: subprocess.Popen[bytes] | None = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            process = subprocess.Popen([str(ROOT / "slotfather"), "dashboard", "--interval", "60"],
                                       stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
            os.close(slave)
            slave = -1
            screen = bytearray()
            answered_at = [0]

            def answer(background: tuple[int, int, int], foreground: tuple[int, int, int]) -> None:
                os.write(master, ("\x1b]11;rgb:%02x%02x/%02x%02x/%02x%02x\x1b\\" % (
                    background[0], background[0], background[1], background[1], background[2], background[2])
                    + "\x1b]10;rgb:%02x/%02x/%02x\x07" % foreground).encode())

            def receive(until: Callable[[bytes], bool], what: str) -> None:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            screen.extend(os.read(master, 65536))
                        except OSError:
                            break
                    if until(bytes(screen)):
                        return
                self.fail(f"terminal did not show {what}: {bytes(screen)[-2000:]!r}")

            receive(lambda data: b"\x1b]11;?" in data and theme.DEVICE_ATTRIBUTES in data, "the colour query")
            self.assertNotIn(sf_keys.KITTY_ON, screen, "asked before the kitty push")
            answer(LIGHT_BG, LIGHT_FG)
            os.write(master, b"\x1b[?62;22c")
            receive(lambda data: b"QUEUE" in data, "the dashboard")
            self.assertIn(theme.SCHEME_ON, screen, "colour-scheme changes are subscribed to")
            os.write(master, b"s")  # Settings: its focused row is selected
            light = ("48;2;%d;%d;%dm" % theme.mix(LIGHT_BG, LIGHT_FG, 0.10)).encode()
            receive(lambda data: b"STATUS" in data and light in data, "the light selection")
            self.assertNotIn(b"56;62;74", screen)
            self.assertNotIn(b"38;5;255", screen)
            answered_at[0] = len(screen)
            os.write(master, b"\x1b[?997;1n")  # the terminal switched to dark
            receive(lambda data: b"\x1b]11;?" in data[answered_at[0]:], "a second query after the notification")
            answer(DARK_BG, DARK_FG)
            dark = ("48;2;%d;%d;%dm" % theme.mix(DARK_BG, DARK_FG, 0.10)).encode()
            receive(lambda data: dark in data[answered_at[0]:], "the dark selection after the answer")
            os.write(master, b"\x1b")
            os.write(master, b"q")
            deadline = time.monotonic() + 8
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        screen.extend(os.read(master, 65536))
                    except OSError:
                        break
            self.assertEqual(0, process.wait(timeout=1))
            self.assertIn(sf_keys.KITTY_OFF + theme.SCHEME_OFF, screen, "both modes reset in one place")
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            os.close(master)
            if slave >= 0:
                os.close(slave)

    def test_without_colour_the_width_probe_still_runs_and_glyphs_are_placed(self) -> None:
        env = {key: value for key, value in self.env.items() if key != "COLORFGBG"}
        env["NO_COLOR"] = "1"
        holder = self.request("probe-owner", 1)
        self.started("probe-owner")
        master, slave = pty.openpty()
        process: subprocess.Popen[bytes] | None = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            process = subprocess.Popen([str(ROOT / "slotfather"), "dashboard", "--interval", "60"],
                                       stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
            os.close(slave)
            slave = -1
            screen = b""
            deadline = time.monotonic() + 8
            while not re.search(rb"\xe2\x96\xb6\xef\xb8\x8f\x1b\[\d+G", screen) and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    screen += os.read(master, 65536)
            self.assertIn(b"\x1b[6n", screen, "the width of the joined glyph is measured with colour off too")
            self.assertRegex(screen, rb"\xe2\x96\xb6\xef\xb8\x8f\x1b\[\d+G", "▶️ is followed by a cursor move")
            os.write(master, b"q")
            deadline = time.monotonic() + 8
            while process.poll() is None and time.monotonic() < deadline:  # read on, so its output never blocks
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        break
            self.assertEqual(0, process.wait(timeout=1))
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            os.close(master)
            if slave >= 0:
                os.close(slave)
            self.release("probe-owner")
            self.finish(holder)

    def test_the_scheme_mode_is_reset_when_the_dashboard_fails(self) -> None:
        tool = importlib.import_module("slotfather.dashboard")
        master, slave = pty.openpty()
        read, write = os.pipe()
        try:
            with self.assertRaises(RuntimeError):
                with tool.full_screen(slave, write, theme.Answers()):
                    raise RuntimeError("boom")
            out = b""
            while select.select([read], [], [], 0.1)[0]:
                out += os.read(read, 65536)
            self.assertLess(out.index(theme.QUERY), out.index(sf_keys.KITTY_ON))
            self.assertLess(out.index(theme.SCHEME_ON), out.index(theme.SCHEME_OFF))
        finally:
            for fd in (master, slave, read, write):
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
