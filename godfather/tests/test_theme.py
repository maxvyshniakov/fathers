"""Dashboard colours from the terminal's theme (#14): tokens, mixed surfaces, holder colours and the chart gradient."""
from __future__ import annotations

import helpers  # noqa: F401 - src/ on sys.path
import view as _view

import itertools
import os
import re
import threading
import time
import unittest
from typing import Any
from unittest import mock

from godfather.views import settings as sf_settings
from godfather import theme


LIGHT_BG, LIGHT_FG = (0xFD, 0xFC, 0xFA), (0x2A, 0x2A, 0x2A)
DARK_BG, DARK_FG = (0x28, 0x2C, 0x34), (0xFF, 0xFF, 0xFF)
LIGHT_ENV = {"SLOTFATHER_TERMINAL_BG": "#fdfcfa", "SLOTFATHER_TERMINAL_FG": "#2a2a2a"}
DARK_ENV = {"SLOTFATHER_TERMINAL_BG": "#282c34", "SLOTFATHER_TERMINAL_FG": "#ffffff"}
# The owner's own themes (cmux's bundled Ghostty files) among them: Pro Light #ffffff, Seti #111213
BACKGROUNDS = {"light": [LIGHT_BG, (255, 255, 255), (238, 232, 213)],
               "dark": [DARK_BG, (0, 0, 0), (30, 30, 46), (0x11, 0x12, 0x13)]}
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


REFERENCES = {"light": (0xFD, 0xFC, 0xFA), "dark": (0x1E, 0x1E, 0x1E)}
# Terminal palettes' (blue, red, yellow): focus, over share and idle
RESERVED = {"light": [hexes("#0969da #cf222e #4d2d00"), hexes("#0000ee #cd0000 #cdcd00"),
                      hexes("#4078f2 #e45649 #c18401"), hexes("#3b75ff #e5492b #c6c440")],  # GitHub Light, xterm,
            # One Light, Pro Light
            "dark": [hexes("#81a2be #cc6666 #f0c674"), hexes("#61afef #e06c75 #e5c07b"), hexes("#0000ee #cd0000 #cdcd00"),
                     hexes("#bd93f9 #ff5555 #f1fa8c"), hexes("#89b4fa #f38ba8 #f9e2af"),
                     hexes("#43a5d5 #c22832 #e0c64f")]}  # Ghostty, One Dark, xterm, Dracula, Catppuccin Mocha, Seti
# The owner's pick (2026-11-11), "Neon": OKLCH hues in the order holders get them
ORDER = ["hot pink", "orange", "acid green", "spring teal", "cyan", "electric violet", "magenta", "green"]
NEON_HUES = [350, 68, 138, 172, 205, 288, 318, 152]


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
                    else:  # from the fill's lightness up: a fill that already reads keeps it
                        self.assertGreaterEqual(ink[0], fill[0] - 0.005, f"{holder}: never darker on a dark one")
                    self.assertLessEqual(ink[1], theme.INK_CHROMA + 0.005, f"{holder}: the ink's chroma is capped")
                    # sRGB's violet is narrow up there: a nudge lighter still keeps a clear tint
                    self.assertGreater(fill[1], 0.07 if fill[0] > 0.8 else 0.1, f"{holder}: a vivid fill, not a muddy one")
                    self.assertTrue(0.66 <= fill[0] <= 0.87, f"{holder}: the fill's lightness, or a nudge from it")

    def test_every_ink_reads_at_four_point_six_to_one(self) -> None:
        for kind, background, reserved in every_theme():
            with self.subTest(kind=kind, background=background, reserved=reserved):
                for holder in theme.holder_pairs(background, kind == "light", reserved):
                    self.assertGreaterEqual(theme.contrast(holder.ink, background), 4.6, holder)
        for light, reference in ((True, REFERENCES["light"]), (False, REFERENCES["dark"])):
            for holder in theme.holder_pairs(None, light):  # an unknown background: measured on the reference
                self.assertGreaterEqual(theme.contrast(holder.ink, reference), 4.6, holder)

    def test_no_fill_sits_near_the_focus_blue_the_over_share_red_or_the_idle_yellow(self) -> None:
        for kind, background, reserved in every_theme():
            with self.subTest(kind=kind, background=background, reserved=reserved):
                holders = theme.holder_pairs(background, kind == "light", reserved)
                blue, red, yellow = reserved or theme.reserved_for(background, kind == "light")
                for holder in holders:
                    self.assertGreaterEqual(theme.distance(holder.fill, blue), theme.RESERVED_APART, f"{holder}: focus")
                    self.assertGreaterEqual(theme.distance(holder.fill, red), theme.RESERVED_APART, f"{holder}: over")
                    self.assertGreaterEqual(theme.distance(holder.fill, yellow), theme.RESERVED_APART, f"{holder}: idle")
                for first, second in itertools.combinations(holders, 2):
                    self.assertGreaterEqual(theme.distance(first.fill, second.fill), theme.HOLDER_APART - 1e-9)
                self.assertEqual(8, len(holders), "these palettes leave room for every hue")
                for holder in holders:  # no blue, sky or indigo, whichever way a hue turned
                    self.assertFalse(225 < theme.hue(holder.fill) < 280, holder)

    def test_the_hues_come_in_the_owners_order_and_a_reserved_one_drops_out(self) -> None:
        self.assertEqual(ORDER, [name for name, *_ in theme.HOLDER_HUES])
        for light in (True, False):
            self.assertEqual(ORDER, [holder.name for holder in theme.holder_pairs(None, light)])
        cyan = theme.oklch(0.73, 0.13, 205)  # a theme whose blue, red and yellow all sit on the cyan
        self.assertEqual([name for name in ORDER if name != "cyan"],
                         [holder.name for holder in theme.holder_pairs(LIGHT_BG, True, (cyan, cyan, cyan))],
                         "cyan has no room left: dropped, the rest keep their order")

    def test_the_sets_are_neon(self) -> None:
        for kind, level in (("light", 0.73), ("dark", 0.80)):
            reserved = RESERVED[kind][0]  # GitHub Light, Ghostty
            holders = theme.holder_pairs(REFERENCES[kind], kind == "light", reserved)
            for holder, angle in zip(holders, NEON_HUES):
                with self.subTest(kind=kind, holder=holder):
                    nudged = theme.lch(holder.fill)[0] != level and kind == "dark" and holder.name == "orange"
                    if not nudged:  # Ghostty's yellow moves the dark orange down in lightness
                        self.assertEqual(theme.oklch(level, 0.24, angle), holder.fill, "chroma 0.24, gamut-clamped")
                    ink_hue = theme.lch(holder.ink)[2]
                    self.assertLess(abs((ink_hue - angle + 180) % 360 - 180), 3)
                    self.assertGreaterEqual(theme.contrast(holder.ink, REFERENCES[kind]), 4.6)
        light = theme.holder_pairs(REFERENCES["light"], True, RESERVED["light"][0])
        self.assertGreater(min(theme.distance(first.fill, second.fill)
                               for first, second in itertools.combinations(light, 2)), 0.06, "well separated")

    def test_holders_keep_apart_from_our_fixed_tokens_whatever_the_palette(self) -> None:
        plain = theme.resolve("auto", answered(bg=LIGHT_BG), {})
        teal = theme.oklch(0.73, 0.15, 172)  # a palette whose blue is a teal changes nothing now
        found = theme.resolve("auto", answered(bg=LIGHT_BG, p4=teal, p1=teal, p3=teal), {})
        fills = [found.styles[f"f{n}"][0] for n in range(found.holders())]
        self.assertEqual([plain.styles[f"f{n}"][0] for n in range(plain.holders())], fills)
        for background in (REFERENCES["light"], REFERENCES["dark"]):
            tokens = theme.resolve("auto", None, {"SLOTFATHER_TERMINAL_BG": "#%02x%02x%02x" % background})
            for fill in (tokens.styles[f"f{n}"][0] for n in range(tokens.holders())):
                for name in ("accent", "bad", "warn"):
                    self.assertGreaterEqual(theme.distance(cast_rgb(fill), cast_rgb(tokens.styles[name][0])),
                                            theme.RESERVED_APART, f"{fill} near {name}")

    def test_a_dark_background_known_or_not_gives_the_dark_set(self) -> None:
        self.assertEqual(theme.holder_pairs(None, False), theme.holder_pairs(REFERENCES["dark"], False))


def cast_rgb(colour: Any) -> tuple[int, int, int]:
    assert isinstance(colour, tuple)
    return colour  # type: ignore[return-value]


# Real themes' (foreground, background, bright black): Seti and Pro Light (iTerm2), One Dark, Solarized, Dracula,
# GitHub Light Default
MUTED_THEMES = {"Seti": hexes("#cacecd #111213 #3f3f3f"), "Pro Light": hexes("#191919 #ffffff #9f9f9f"),
                "One Dark": hexes("#abb2bf #282c34 #5c6370"), "Solarized Dark": hexes("#839496 #002b36 #002b36"),
                "Solarized Light": hexes("#657b83 #fdf6e3 #002b36"), "Dracula": hexes("#f8f8f2 #282a36 #6272a4"),
                "GitHub Light": hexes("#1f2328 #ffffff #57606a")}


class MutedTests(unittest.TestCase):
    def test_muted_text_reads_on_every_theme_and_stays_dimmer_than_its_text(self) -> None:
        for name, (fg, bg, bright_black) in MUTED_THEMES.items():
            with self.subTest(name=name):
                found = theme.resolve("auto", answered(fg=fg, bg=bg, p8=bright_black), {})
                dim = cast_rgb(found.styles["dim"][0])
                plain, muted = theme.contrast(fg, bg), theme.contrast(dim, bg)
                self.assertLess(muted, plain - 0.1, "dimmer than the text")
                if plain >= 4.5:
                    self.assertGreaterEqual(muted, 4.5, f"{theme.contrast(bright_black, bg):.2f}:1 with bright black")
                else:  # Solarized Light: its own text reads at 4.13:1; muted keeps about 85% of that, never more
                    self.assertGreaterEqual(muted, 0.85 * plain)
                if 0.85 * plain >= 4.5:
                    self.assertLessEqual(muted, 0.85 * plain + 0.05, "visibly dimmer")
                self.assertAlmostEqual(theme.distance(fg, bg), theme.distance(fg, dim) + theme.distance(dim, bg),
                                       delta=0.01, msg="on the way from the theme's text to its background")

    def test_bright_black_stays_where_a_colour_is_unknown_and_in_terminal_mode(self) -> None:
        fg, bg, _ = MUTED_THEMES["Seti"]
        self.assertEqual(8, theme.resolve("auto", answered(bg=bg), {}).styles["dim"][0], "no foreground")
        self.assertEqual(8, theme.resolve("auto", answered(fg=fg), {}).styles["dim"][0], "no background")
        self.assertEqual(8, theme.resolve("auto", theme.Answers(), {}).styles["dim"][0])
        self.assertEqual(8, theme.resolve("terminal", answered(fg=fg, bg=bg), {}).styles["dim"][0])
        self.assertEqual((245, "d"), theme.resolve("builtin", answered(fg=fg, bg=bg), {}).styles["dim"])
        explicit = theme.resolve("auto", None, {"SLOTFATHER_TERMINAL_BG": "#111213", "SLOTFATHER_TERMINAL_FG": "#cacecd"})
        self.assertEqual(theme.muted(fg, bg), explicit.styles["dim"][0], "the environment's colours too")

    def test_a_scheme_switch_recomputes_it(self) -> None:
        answers = answered(fg=MUTED_THEMES["Seti"][0], bg=MUTED_THEMES["Seti"][1])
        dark = theme.resolve("auto", answers, {}).styles["dim"][0]
        answers.feed("\x1b[?997;2n")  # the light scheme: the dashboard asks again and the answers replace these
        for name, colour in (("10", MUTED_THEMES["Pro Light"][0]), ("11", MUTED_THEMES["Pro Light"][1])):
            answers.feed(f"\x1b]{name};rgb:{colour[0]:02x}/{colour[1]:02x}/{colour[2]:02x}\x1b\\")
        light = theme.resolve("auto", answers, {}).styles["dim"][0]
        self.assertEqual(theme.muted(*MUTED_THEMES["Pro Light"][:2]), light)
        self.assertNotEqual(dark, light)

    def test_frames_are_a_little_paler_than_muted_text(self) -> None:
        tool = _view.current()
        before = tool.current_theme
        try:
            fg, bg, _ = MUTED_THEMES["Seti"]
            tool.set_theme(theme.resolve("auto", answered(fg=fg, bg=bg), {}))
            canvas = tool.Canvas(40, 6)
            tool.Panel(canvas, tool.Rect(0, 0, 6, 40), "requests").frame("QUEUE")  # unfocused: QUEUE is not the focus
            canvas.focus = "details"
            tool.Panel(canvas, tool.Rect(0, 0, 6, 40), "requests").frame("QUEUE")
            canvas.put(2, 3, "label", "dim")
            self.assertEqual("frame", canvas.cells[3][0][1], "the frame's own token")
            line = canvas.lines(True)[2]
            dim = cast_rgb(tool.current_theme.styles["dim"][0])
            drawn = cast_rgb(tool.current_theme.styles["frame"][0])
            self.assertTrue(line.startswith("\x1b[0;38;2;%d;%d;%dm" % drawn + "│"), line[:40])
            self.assertIn("\x1b[0;38;2;%d;%d;%dm" % dim + "label", line, "muted text in its own colour")
            self.assertLess(theme.contrast(drawn, bg), theme.contrast(dim, bg), "the frame paler than muted text")
            self.assertGreaterEqual(theme.contrast(drawn, bg), 2.5, "but at least 2.5:1")
        finally:
            tool.set_theme(before)


class GradientTests(unittest.TestCase):
    def test_gradient_mixes_in_oklab(self) -> None:
        blue, red = (0x7A, 0xA6, 0xDA), (0xD5, 0x4E, 0x53)
        steps = theme.gradient(blue, red)
        self.assertEqual((8, blue, red), (len(steps), steps[0], steps[-1]))
        for index, colour in enumerate(steps):
            self.assertEqual(theme.mix(blue, red, index / 7), colour)


RAMP_DARK = hexes("#5b5cf0 #8b5cf6 #c44fe0 #ee4a8c #ff3b3b")  # indigo, violet, orchid, crimson-pink, red


class RampTests(unittest.TestCase):
    """The MACHINE chart and bars (owner, 2026-11-12): our ramp, whatever the theme's palette."""

    def test_the_ramp_runs_through_our_stops_and_deepens_on_a_light_background(self) -> None:
        dark = theme.chart_ramp(False, 5)
        self.assertEqual(RAMP_DARK, dark, "five steps are the stops themselves")
        light = theme.chart_ramp(True, 5)
        for stop, deep in zip(RAMP_DARK, light):
            (level, _, angle), (deeper, _, turned) = theme.lch(stop), theme.lch(deep)
            self.assertAlmostEqual(level - 0.06, deeper, delta=0.01, msg=f"{stop}: 0.06 deeper")
            self.assertLess(abs((angle - turned + 180) % 360 - 180), 4, f"{stop} keeps its hue")
        eight = theme.chart_ramp(False)
        self.assertEqual((RAMP_DARK[0], RAMP_DARK[-1]), (eight[0], eight[-1]))
        hues = [theme.lch(colour)[2] for colour in theme.chart_ramp(False, 25)]
        turns = [(after - before + 180) % 360 - 180 for before, after in zip(hues, hues[1:])]
        self.assertTrue(all(0 <= turn < 12 for turn in turns), "one way round: indigo, purple, magenta, red")
        self.assertFalse(any(150 < hue < 260 or 40 < hue < 120 for hue in hues), "no cyan, no orange")
        levels = [theme.lch(colour)[0] for colour in theme.chart_ramp(False, 25)]
        self.assertLess(max(levels) - min(levels), 0.12, "lightness roughly even: no muddy or washed-out band")
        palette = answered(bg=DARK_BG, p4=(0, 0, 238), p1=(205, 0, 0), p12=(92, 92, 255), p9=(255, 0, 0))
        for mode, environ in (("auto", {}), ("auto", DARK_ENV), ("builtin", {})):
            found = theme.resolve(mode, palette, environ)
            self.assertEqual(eight, [found.styles[f"chart{n}"][0] for n in range(8)], f"{mode}: never the palette's")
        lit = theme.resolve("auto", palette, LIGHT_ENV)
        self.assertEqual(theme.chart_ramp(True), [lit.styles[f"chart{n}"][0] for n in range(8)])
        self.assertEqual(theme.chart_ramp(False), [theme.resolve("auto", None, {}).styles[f"chart{n}"][0]
                                                   for n in range(8)], "unknown: dark")
        self.assertEqual(theme.chart_ramp(True), [theme.resolve("auto", None, {"COLORFGBG": "0;15"}).styles[
            f"chart{n}"][0] for n in range(8)], "COLORFGBG says light")

    def test_our_tokens_read_on_both_references_and_on_a_theme_that_needs_a_nudge(self) -> None:
        for light, reference in ((True, REFERENCES["light"]), (False, REFERENCES["dark"])):
            for name, (dark, lit) in theme.SEMANTIC.items():
                colour = theme.semantic(name, reference, light)
                self.assertEqual(theme.hex_colour(lit if light else dark), colour, f"{name}: as chosen")
                self.assertGreaterEqual(theme.contrast(colour, reference), 4.5, name)
        for name in theme.SEMANTIC:  # One Dark's background is lighter than the reference: bad gets lighter
            self.assertGreaterEqual(theme.contrast(theme.semantic(name, DARK_BG, False), DARK_BG), 4.5, name)
        for light, background in ((True, (255, 255, 255)), (False, (0x11, 0x12, 0x13))):  # Pro Light, Seti
            for name in theme.SEMANTIC:
                self.assertGreaterEqual(theme.contrast(theme.semantic(name, background, light), background), 4.5,
                                        f"{name} on {background}")
        found = theme.resolve("auto", None, DARK_ENV)
        self.assertEqual(theme.semantic("bad", DARK_BG, False), found.styles["bad"][0])
        self.assertEqual((found.styles["ok"][0], "br"), found.styles["badge-ok"])

    def test_the_critical_red_is_ours_and_terminal_mode_keeps_the_palette(self) -> None:
        found = theme.resolve("auto", None, {})
        self.assertEqual(theme.hex_colour("#ff3b3b"), found.styles["bad"][0], "the chart's red on dark")
        self.assertEqual(theme.hex_colour("#ff3b3b"), found.styles["chart7"][0], "the chart's top: full")
        self.assertEqual(theme.hex_colour("#d1203a"), theme.resolve("auto", None, LIGHT_ENV).styles["bad"][0])
        terminal = theme.resolve("terminal", answered(bg=DARK_BG), {})
        self.assertEqual([4, 4, 4, 5, 5, 1, 1, 1], [terminal.styles[f"chart{n}"][0] for n in range(8)])
        self.assertEqual(1, terminal.styles["bad"][0], "terminal: the theme's own red")


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
        tool = _view.current()
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
        tool = _view.current()
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
                         [(found.styles[f"f{n}"][0], found.styles[f"h{n}"][0]) for n in range(8)])

    def test_tokens_follow_the_default_foreground_and_terminal_mode_the_palette(self) -> None:
        for mode in ("auto", "terminal"):
            styles = theme.resolve(mode, answered(bg=DARK_BG), {}).styles
            for name in ("text", "bold", "title", "key", "cursor", "status-normal"):
                self.assertIsNone(styles[name][0], f"{name} is the terminal's own foreground, never white")
            self.assertEqual((8, 8), (styles["dim"][0], styles["frame"][0]), "no foreground: bright black for both")
        terminal = theme.resolve("terminal", answered(bg=DARK_BG), {}).styles
        self.assertEqual({"accent": 4, "ok": 2, "warn": 3, "bad": 1, "legacy": 5},
                         {name: terminal[name][0] for name in ("accent", "ok", "warn", "bad", "legacy")},
                         "terminal mode: the theme's own ANSI colours, the opt-out")

    def test_the_environment_overrides_the_terminal_and_colorfgbg_stands_in(self) -> None:
        found = theme.resolve("auto", answered(bg=DARK_BG, fg=DARK_FG), LIGHT_ENV)
        self.assertEqual(("environment", LIGHT_BG, theme.mix(LIGHT_BG, LIGHT_FG, 0.10)),
                         (found.source, found.background, found.selection))
        light = theme.resolve("auto", None, {"COLORFGBG": "0;15"})
        self.assertEqual(("COLORFGBG", True, theme.mix((255, 255, 255), (0, 0, 0), 0.10)),
                         (light.source, light.light, light.selection))
        self.assertEqual([h.ink for h in theme.holder_pairs((255, 255, 255), True)],
                         [light.styles[f"h{n}"][0] for n in range(8)])
        dark = theme.resolve("auto", None, {"COLORFGBG": "15;default;0"})
        self.assertEqual(("COLORFGBG", False), (dark.source, dark.light))

    def test_nothing_known_draws_surfaces_without_a_background_colour(self) -> None:
        for environ in ({}, {"COLORFGBG": "nonsense"}):
            found = theme.resolve("auto", theme.Answers(), environ)
            self.assertEqual((None, "░", "outline", "none", False),
                             (found.selection, found.track, found.free, found.source, found.light))
            self.assertEqual((8, 8), (found.styles["track"][0], found.styles["free"][0]))
            self.assertEqual([h.ink for h in theme.holder_pairs(None, False)], [found.styles[f"h{n}"][0] for n in range(8)])
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
        self.assertEqual(theme.chart_ramp(True)[-1], builtin.styles["chart7"][0], "our ramp")
        self.assertEqual(theme.semantic("accent", LIGHT_BG, True), builtin.styles["accent"][0], "our tokens")

    def test_modes_and_colours_from_the_environment_are_checked(self) -> None:
        self.assertEqual("auto", theme.checked_mode(None))
        self.assertEqual("builtin", theme.checked_mode("builtin"))
        with self.assertRaisesRegex(ValueError, "auto, terminal or builtin"):
            theme.checked_mode("dark")
        with self.assertRaisesRegex(ValueError, "SLOTFATHER_TERMINAL_BG"):
            theme.resolve("auto", None, {"SLOTFATHER_TERMINAL_BG": "beige"})


class DrawingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _view.fresh()

    def sgr_of(self, canvas: Any, row: int, text: str) -> str:
        line = canvas.lines(True)[row]
        found = re.search(r"(\x1b\[[0-9;]*m)" + re.escape(text), line)
        assert found, (text, line)
        return found[1]

    def test_frames_have_their_own_token_paler_than_muted_text(self) -> None:
        for name, theme_now in (("light", theme.resolve("auto", None, LIGHT_ENV)),
                                ("dark", theme.resolve("auto", None, DARK_ENV)),
                                ("unknown", theme.resolve("auto", None, {})),
                                ("terminal", theme.resolve("terminal", None, LIGHT_ENV)),
                                ("builtin", theme.resolve("builtin", None, {}))):
            with self.subTest(name):
                self.tool.set_theme(theme_now)
                canvas = self.tool.Canvas(40, 4)
                self.tool.Panel(canvas, self.tool.Rect(0, 0, 4, 40)).frame("TITLE")
                canvas.put(1, 2, "muted words", "dim")
                frame = self.sgr_of(canvas, 0, "╭")
                self.assertEqual(frame, self.sgr_of(canvas, 1, "│"))
                self.assertEqual(frame, self.sgr_of(canvas, 3, "╰"))
                self.assertEqual(frame, self.tool.sgr("frame", False))
                self.assertEqual(self.sgr_of(canvas, 1, "muted words"), self.tool.sgr("dim", False))
                self.assertNotIn("239", frame, "never the near-black of the palette before")
                dim, drawn = theme_now.styles["dim"][0], theme_now.styles["frame"][0]
                if isinstance(dim, tuple) and theme_now.background is not None:
                    background = theme_now.background
                    paler = theme.contrast(cast_rgb(drawn), background)
                    self.assertLess(paler, theme.contrast(dim, background), "frames a little paler than muted text")
                    self.assertGreaterEqual(paler, 2.5, "and still clearly visible")
                else:
                    self.assertEqual(dim, drawn, "without our muted colour: the same token")

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
        plain = theme.resolve("auto", None, {})
        self.assertEqual([("■■", "accent"), ("░░░", "track")], self.tool.bar(0.4, 5, "accent", plain.track))
        self.assertEqual(("╭──╮", "▭"), (self.tool.free_slot(4, plain.free), self.tool.free_slot(1, plain.free)))
        dark = theme.resolve("auto", None, DARK_ENV)
        self.assertEqual([("■■", "accent"), ("■■■", "track")], self.tool.bar(0.4, 5, "accent", dark.track))
        self.assertEqual("🮅🮅🮅🮅", self.tool.free_slot(4, dark.free))
        self.assertEqual("o", "▭".translate(self.tool.ASCII))

    def test_builtin_keeps_six_colours_and_gives_a_freed_one_first(self) -> None:
        def holders(count: int) -> list[dict[str, Any]]:
            return [dict(key=n, kind="held") for n in range(count)]

        builtin = theme.resolve("builtin", None, {}).holders()
        assigned: dict[Any, int] = {}
        model = {"holders": holders(7)}
        self.tool.dashboard_colours(model, assigned, builtin)
        self.assertEqual([f"h{n}" for n in (0, 1, 2, 3, 4, 5, 0)], [h["style"] for h in model["holders"]])
        model = {"holders": [h for h in holders(7) if h["key"] != 1] + [dict(key=7, kind="held")]}
        self.tool.dashboard_colours(model, assigned, builtin)
        self.assertEqual("h1", model["holders"][-1]["style"], "the colour the ended lease freed")
        model = {"holders": holders(9)}
        self.tool.dashboard_colours(model, {}, theme.resolve("auto", None, {}).holders())
        self.assertEqual([f"h{n}" for n in range(8)] + ["h0"], [h["style"] for h in model["holders"]])

    def test_the_holder_colour_count_always_comes_from_the_theme(self) -> None:
        # fathers#138 review: a default of eight gave a builtin-theme lease h6, a style that theme lacks
        import dataclasses
        import inspect

        self.assertIs(inspect.Parameter.empty, inspect.signature(self.tool.dashboard_colours).parameters["count"].default)
        self.assertIs(dataclasses.MISSING, {f.name: f for f in dataclasses.fields(self.tool.ModelLoaded)}["colours"].default)
        builtin = theme.resolve("builtin", None, {})
        found = {"holders": [dict(key=n, kind="held") for n in range(8)], "waiters": [], "events": []}
        state, _ = self.tool.reduce(self.tool.AppState(), self.tool.ModelLoaded(found, builtin.holders()), 0.0)
        self.assertTrue(all(h["style"] in builtin.styles for h in state.model["holders"]))


class SettingTests(unittest.TestCase):
    def setUp(self) -> None:
        from helpers import FakeSlotfather
        self.fake = FakeSlotfather()
        self.addCleanup(self.fake.close)
        patcher = mock.patch.dict(os.environ, self.fake.environ(), clear=True)
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
        self.assertFalse([argv for argv in self.fake.argv() if argv[1:2] in (["set"], ["reset"])],
                         "never written to the shared queue")
        self.assertEqual("handled", dialog.key("5"), "a choice takes no typed value")
        self.assertFalse(dialog.editing)
        self.assertEqual((None, "This setting takes no typed value."), dialog.check("theme", "auto"))
        self.assertEqual("changed", dialog.key("d"))
        self.assertEqual("auto", dialog.theme, "d: back to the default")
        dialog.theme = "terminal"
        dialog.key("D")
        dialog.key("D")
        self.assertEqual("auto", dialog.theme, "D twice: every setting at its default")
        self.assertIn(["settings", "reset", "--all", "--json"], self.fake.argv(), "Slotfather's too")
        with self.assertRaises(ValueError):
            sf_settings.Settings(1.0, "solarized")

    def test_the_panel_shows_the_theme_last_after_the_refresh(self) -> None:
        tool = _view.current()
        dialog = sf_settings.Settings(1.0, "builtin", clock=self.clock)
        canvas = tool.Canvas(100, 44)
        dialog.draw(canvas, None)
        text = canvas.render(False)
        self.assertRegex(text, r"Colour theme +Built-in")
        self.assertLess(text.index("Refresh every"), text.index("Colour theme"))






if __name__ == "__main__":
    unittest.main()
