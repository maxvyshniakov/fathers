"""Dashboard colours from the terminal's own theme (#14), like CSS tokens with color-mix() for what it lacks.

Tokens are palette indices or the terminal's default foreground, so they follow the theme live: text, bold, hotkeys,
cursor and status use the default foreground (SGR 39); muted text and every frame share bright black (8), one token;
ok, warn, bad and accent are ANSI green, yellow, red and blue. The surfaces the palette lacks (the selected row's
background, a bar's track and a free slot) are mixed in OKLab from the background and foreground the terminal reports
(OSC 10/11). Without a report COLORFGBG says light or dark; without that either, they are drawn with no background
colour. Holder colours are ours: pastels in OKLCH, hues a golden angle apart, at a lightness that reads on the
background. The load chart runs from the theme's blue to its red in OKLab.

Modes: auto (the above), terminal (tokens only: no mixed surfaces, the chart in palette steps) and builtin (the fixed
256-colour palette from before #14, for themes such as Solarized where bright black is close to the background)."""
from __future__ import annotations

import math
import os
import re
import select
import time
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple, Union

RGB = Tuple[int, int, int]
Lab = Tuple[float, float, float]
Colour = Union[None, int, RGB]  # None: the terminal's default foreground; int: a palette index; RGB: true colour
Style = Tuple[Colour, str]  # (colour, attributes: b bold, d dim, r reverse)

MODES = ("auto", "terminal", "builtin")
DEFAULT_MODE = "auto"
MODE_LABELS = {"auto": "Auto", "terminal": "Terminal", "builtin": "Built-in"}

# Surfaces: the share of the foreground mixed into the background.
SELECTION_MIX, TRACK_MIX, FREE_MIX = 0.10, 0.15, 0.20
HOLDERS = 12
GOLDEN_ANGLE = 180 * (3 - math.sqrt(5))  # 137.5°: each next hue lands in the widest gap the earlier ones left
HOLDER_START = 248.0  # OKLCH hue of the first holder colour, the blue of the palette before #14 (256-colour 75)
# (OKLCH lightness, chroma) of holder colours on a dark background, and deeper on a light one. The chroma is a
# target: a hue whose colour at it falls outside sRGB (blue and cyan at these lightnesses) gets the most that fits.
DARK_PASTEL = (0.80, 0.13)
LIGHT_PASTEL = (0.64, 0.14)  # 0.15 puts the coral (hue 25) within ΔE 0.1 of a light theme's over-share red
MIN_CONTRAST = 3.0  # holder text against the background it is drawn on
CHART_STEPS = 8
FIXED_LOW, FIXED_HIGH = (80, 140, 240), (225, 85, 80)  # the chart without the theme's own blue and red
QUERY_TIMEOUT = 0.1  # seconds the dashboard waits at startup for the terminal's colours
SCHEME_ON, SCHEME_OFF = b"\x1b[?2031h", b"\x1b[?2031l"  # colour-scheme change notifications (CSI ? 997 ; 1|2 n)
PALETTE_ASKED = (1, 4, 9, 12)  # red, blue and their bright forms: the chart's ends
QUERY = (b"\x1b]10;?\x1b\\\x1b]11;?\x1b\\"
         + b"".join(f"\x1b]4;{index};?\x1b\\".encode() for index in PALETTE_ASKED))
DEVICE_ATTRIBUTES = b"\x1b[c"  # every terminal answers it, after the answers before it: the end of the replies
# A report the terminal sends (never a key): an OSC string, a private DSR (?997;1n) or the device attributes (?…c).
# An OSC report starts with its number and is short, so a stray ESC ] (Alt+] without the kitty protocol) followed by
# keys is never taken for one, nor held back waiting for its end.
OSC_LONGEST = 80
REPORT = re.compile(r"\x1b\]\d[^\x07\x1b]{0,%d}(?:\x07|\x1b\\)|\x1b\[\?[0-9;]*[nc]" % OSC_LONGEST)
OSC_PARTIAL = r"\](?:\d[^\x07\x1b]{0,%d}\x1b?)?" % OSC_LONGEST
REPORT_PARTIAL = re.compile(r"\x1b(?:%s|\[\?[0-9;]*)?$" % OSC_PARTIAL)
COLOUR_REPLY = re.compile(r"\x1b\](10|11|4;(\d+));([^\x07\x1b]*)")
SCHEME_REPLY = re.compile(r"\x1b\[\?997;([12])n")
# xterm's default 16 colours, for COLORFGBG's indices
XTERM = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205), (0, 205, 205),
         (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255), (255, 0, 255),
         (0, 255, 255), (255, 255, 255)]

# The palette before #14, kept as the builtin mode. Frames share the muted token.
BUILTIN_HOLDERS: List[Colour] = [75, 79, 216, 141, 110, 186]
BUILTIN_CHART: List[Colour] = [(95, 175, 255), (105, 155, 255), (135, 145, 255), (170, 135, 240),
                               (200, 140, 210), (225, 150, 170), (245, 160, 130), (255, 175, 95)]
BUILTIN: Dict[str, Style] = {
    "text": (None, ""), "bold": (None, "b"), "dim": (245, "d"), "title": (None, "b"), "key": (255, "b"),
    "accent": (75, ""), "cursor": (255, "b"), "status-normal": (255, ""), "ok": (114, ""), "warn": (179, ""),
    "bad": (167, ""), "legacy": (176, ""), "badge-ok": (114, "br"), "badge-warn": (179, "br"),
    "badge-bad": (167, "br"), "track": (239, "d"), "free": (239, "d")}
TOKENS: Dict[str, Style] = {
    "text": (None, ""), "bold": (None, "b"), "dim": (8, ""), "title": (None, "b"), "key": (None, "b"),
    "accent": (4, ""), "cursor": (None, "b"), "status-normal": (None, ""), "ok": (2, ""), "warn": (3, ""),
    "bad": (1, ""), "legacy": (5, ""), "badge-ok": (2, "br"), "badge-warn": (3, "br"), "badge-bad": (1, "br"),
    "track": (8, ""), "free": (8, "")}


# Colour arithmetic: sRGB, OKLab (Björn Ottosson, 2020) and WCAG contrast

def _linear(channel: float) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _encoded(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return 255 * (12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055)


def oklab(rgb: RGB) -> Lab:
    red, green, blue = (_linear(channel) for channel in rgb)
    long = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    medium = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    short = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue
    long, medium, short = (math.copysign(abs(cone) ** (1 / 3), cone) for cone in (long, medium, short))
    return (0.2104542553 * long + 0.7936177850 * medium - 0.0040720468 * short,
            1.9779984951 * long - 2.4285922050 * medium + 0.4505937099 * short,
            0.0259040371 * long + 0.7827717662 * medium - 0.8086757660 * short)


def _linear_rgb(lab: Lab) -> Tuple[float, float, float]:
    lightness, a, b = lab
    long = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    medium = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    short = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short,
            -1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short,
            -0.0041960863 * long - 0.7034186147 * medium + 1.7076147010 * short)


def from_oklab(lab: Lab) -> RGB:
    red, green, blue = (round(_encoded(value)) for value in _linear_rgb(lab))
    return red, green, blue


def oklch(lightness: float, chroma: float, hue: float) -> RGB:
    """The sRGB colour at an OKLCH point; outside sRGB, chroma is lowered until it fits (hue and lightness kept)."""
    def lab(c: float) -> Lab:
        return lightness, c * math.cos(math.radians(hue)), c * math.sin(math.radians(hue))

    def fits(c: float) -> bool:
        return all(-1e-4 <= value <= 1 + 1e-4 for value in _linear_rgb(lab(c)))

    if not fits(chroma):
        low, high = 0.0, chroma
        for _ in range(24):
            middle = (low + high) / 2
            low, high = (middle, high) if fits(middle) else (low, middle)
        chroma = low
    return from_oklab(lab(chroma))


def hue(rgb: RGB) -> float:
    _, a, b = oklab(rgb)
    return math.degrees(math.atan2(b, a)) % 360


def mix(base: RGB, other: RGB, amount: float) -> RGB:
    """`amount` of `other` in `base`, interpolated in OKLab, like color-mix(in oklab, …)."""
    first, second = oklab(base), oklab(other)
    lightness, a, b = (x + (y - x) * amount for x, y in zip(first, second))
    return from_oklab((lightness, a, b))


def distance(first: RGB, second: RGB) -> float:
    """ΔE in OKLab: about 0.02 is a just-noticeable difference."""
    return math.dist(oklab(first), oklab(second))


def luminance(rgb: RGB) -> float:
    red, green, blue = (_linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: RGB, second: RGB) -> float:
    """The WCAG contrast ratio, 1 to 21."""
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def is_light(background: RGB) -> bool:
    """A background black text reads better on than white."""
    return contrast(background, (0, 0, 0)) > contrast(background, (255, 255, 255))


def holder_colours(background: Optional[RGB], light: bool, count: int = HOLDERS) -> List[RGB]:
    """`count` pastels at one lightness and chroma, hues a golden angle apart from today's blue so neighbours
    differ. On a known background each is moved away from it until it reaches MIN_CONTRAST."""
    lightness, chroma = LIGHT_PASTEL if light else DARK_PASTEL
    found: List[RGB] = []
    for index in range(count):
        angle = (HOLDER_START + index * GOLDEN_ANGLE) % 360
        level = lightness
        colour = oklch(level, chroma, angle)
        while background is not None and contrast(colour, background) < MIN_CONTRAST and 0.02 < level < 0.98:
            level += -0.01 if light else 0.01
            colour = oklch(level, chroma, angle)
        found.append(colour)
    return found


def gradient(low: RGB, high: RGB, steps: int = CHART_STEPS) -> List[RGB]:
    """`steps` colours from low to high in OKLab, ends included."""
    return [mix(low, high, step / (steps - 1)) for step in range(steps)]


# What the terminal says about its colours

def parse_colour(text: str) -> Optional[RGB]:
    """An X11 colour spec as terminals report and accept it: rgb:R/G/B (1-4 hex digits each), rgba:R/G/B/A, #rgb or
    #rrggbb; None for anything else."""
    text = text.strip().lower()
    parts: List[str] = []
    if text.startswith(("rgb:", "rgba:")):
        parts = text.split(":", 1)[1].split("/")[:3]
    elif text.startswith("#") and len(text) in (4, 7, 10, 13):
        size = (len(text) - 1) // 3
        parts = [text[1 + n * size:1 + (n + 1) * size] for n in range(3)]
    if len(parts) != 3 or not all(1 <= len(part) <= 4 and all(c in "0123456789abcdef" for c in part)
                                  for part in parts):
        return None
    red, green, blue = (round(int(part, 16) * 255 / (16 ** len(part) - 1)) for part in parts)
    return red, green, blue


class Answers:
    """The colours the terminal reported: foreground, background, palette entries and the colour-scheme hint of
    its last change notification ("light" or "dark")."""

    def __init__(self) -> None:
        self.foreground: Optional[RGB] = None
        self.background: Optional[RGB] = None
        self.palette: Dict[int, RGB] = {}
        self.scheme: Optional[str] = None
        self.finished = False  # the device attributes came back: nothing more answers this query

    def feed(self, report: str) -> bool:
        """Takes one report (see REPORT); True when it changed a colour or the scheme."""
        if report.startswith("\x1b[?") and report.endswith("c"):
            self.finished = True
            return False
        scheme = SCHEME_REPLY.match(report)
        if scheme:
            before, self.scheme = self.scheme, "dark" if scheme[1] == "1" else "light"
            return before != self.scheme
        found = COLOUR_REPLY.match(report)
        colour = parse_colour(found[3]) if found else None
        if found is None or colour is None:
            return False
        if found[1] == "10":
            before, self.foreground = self.foreground, colour
        elif found[1] == "11":
            before, self.background = self.background, colour
        else:
            before = self.palette.get(int(found[2]))
            self.palette[int(found[2])] = colour
        return before != colour


def split_reports(data: str) -> Tuple[List[str], str, str]:
    """(the reports in data, the rest of it, a report cut off at its end that the next read completes)."""
    reports: List[str] = []
    rest: List[str] = []
    index = 0
    while index < len(data):
        found = REPORT.match(data, index)
        if found:
            reports.append(found.group())
            index = found.end()
        else:
            rest.append(data[index])
            index += 1
    text = "".join(rest)
    partial = REPORT_PARTIAL.search(text)
    if partial and partial.group() != "\x1b":  # a lone Esc is a key
        return reports, text[:partial.start()], partial.group()
    return reports, text, ""


def ask(fd_in: int, fd_out: int, answers: Answers, timeout: float = QUERY_TIMEOUT,
        clock: Callable[[], float] = time.monotonic) -> str:
    """Asks the terminal for its colours and waits for the answers, at most `timeout` seconds in all; the device
    attributes asked last end the wait early on any terminal. Returns what arrived that was not a report: keys typed
    meanwhile. The terminal must be in non-canonical mode, or the answers wait for a newline and echo."""
    os.write(fd_out, QUERY + DEVICE_ATTRIBUTES)
    deadline, pending, typed = clock() + timeout, "", ""
    while not answers.finished:
        left = deadline - clock()
        if left <= 0 or not select.select([fd_in], [], [], left)[0]:
            break
        chunk = os.read(fd_in, 1024)
        if not chunk:
            break
        reports, rest, pending = split_reports(pending + chunk.decode("utf-8", "ignore"))
        typed += rest
        for report in reports:
            answers.feed(report)
    return typed + pending


# Themes

class Theme(NamedTuple):
    mode: str
    styles: Dict[str, Style]
    selection: Optional[RGB]  # the selected row's background; None: the row is bold instead
    selected_text: Colour  # the colour of default-foreground text on a selected row (builtin: 252), None to keep it
    track: str  # a bar's empty cell
    free: str  # "block": a free slot is a block in the free colour; "outline": it is outlined
    light: bool
    background: Optional[RGB]
    source: str  # where the background came from: terminal, environment, COLORFGBG or none

    def holders(self) -> int:
        return sum(name.startswith("h") and name[1:].isdigit() for name in self.styles)


def colorfgbg(value: Optional[str]) -> Optional[Tuple[RGB, RGB]]:
    """(foreground, background) by COLORFGBG's palette indices ("15;0", or "15;default;0"), as xterm draws them."""
    fields = (value or "").split(";")
    if len(fields) < 2 or not fields[0].isdigit() or not fields[-1].isdigit():
        return None
    fore, back = int(fields[0]), int(fields[-1])
    if fore > 15 or back > 15:
        return None
    return XTERM[fore], XTERM[back]


def checked_mode(value: Optional[str]) -> str:
    """SLOTFATHER_THEME: auto when unset; ValueError for a value that is not a mode."""
    if value is None or value == "":
        return DEFAULT_MODE
    if value not in MODES:
        raise ValueError(f"SLOTFATHER_THEME takes {', '.join(MODES[:-1])} or {MODES[-1]}, not {value!r}")
    return value


def environment_colour(name: str, environ: Mapping[str, str]) -> Optional[RGB]:
    """SLOTFATHER_TERMINAL_BG / _FG; ValueError for one that is not a colour."""
    value = environ.get("SLOTFATHER_TERMINAL_" + name)
    if not value:
        return None
    colour = parse_colour(value)
    if colour is None:
        raise ValueError(f"SLOTFATHER_TERMINAL_{name} takes a colour such as #fdfcfa, not {value!r}")
    return colour


def resolve(mode: str, answers: Optional[Answers] = None,
            environ: Optional[Mapping[str, str]] = None) -> Theme:
    """The theme for `mode` from what the terminal answered, overridden by SLOTFATHER_TERMINAL_BG/_FG, else
    COLORFGBG, else nothing known."""
    environ = os.environ if environ is None else environ
    answers = answers or Answers()
    background, foreground = environment_colour("BG", environ), environment_colour("FG", environ)
    source = "environment" if background else "none"
    if background is None and answers.background is not None:
        background, source = answers.background, "terminal"
    foreground = foreground or answers.foreground
    # COLORFGBG is fixed when the shell starts: a colour-scheme notification since then says more
    guessed = colorfgbg(environ.get("COLORFGBG")) if answers.scheme is None else None
    if background is None and guessed is not None:
        foreground, background, source = foreground or guessed[0], guessed[1], "COLORFGBG"
    light = is_light(background) if background else answers.scheme == "light"
    if mode == "builtin":
        styles = dict(BUILTIN)
        styles.update({f"h{n}": (colour, "") for n, colour in enumerate(BUILTIN_HOLDERS)})
        styles.update({f"chart{n}": (colour, "") for n, colour in enumerate(BUILTIN_CHART)})
        return Theme(mode, styles, (56, 62, 74), 252, "■", "block", light, background, source)
    styles = dict(TOKENS)
    styles.update({f"h{n}": (colour, "") for n, colour in enumerate(holder_colours(background, light))})
    mixing = mode == "auto" and background is not None
    if mixing:
        assert background is not None
        ink = foreground or ((0, 0, 0) if light else (255, 255, 255))
        styles["track"] = (mix(background, ink, TRACK_MIX), "")
        styles["free"] = (mix(background, ink, FREE_MIX), "")
        selection: Optional[RGB] = mix(background, ink, SELECTION_MIX)
    else:
        selection = None
    styles.update({f"chart{n}": (colour, "") for n, colour in enumerate(chart_colours(mode, answers, background))})
    return Theme(mode, styles, selection, None, "■" if mixing else "░", "block" if mixing else "outline",
                 light, background, source)


def chart_colours(mode: str, answers: Answers, background: Optional[RGB]) -> List[Colour]:
    """The load chart by height: the theme's blue to its red in OKLab (the pair, bright or normal, that reads better
    on the background), fixed RGB without them; in terminal mode palette steps, nothing mixed."""
    if mode == "terminal":
        return [4, 4, 4, 5, 5, 1, 1, 1]
    pairs = [(answers.palette[low], answers.palette[high]) for low, high in ((12, 9), (4, 1))
             if low in answers.palette and high in answers.palette]
    if not pairs:
        return list(gradient(FIXED_LOW, FIXED_HIGH))
    if background is not None:
        bg = background
        pairs.sort(key=lambda pair: -min(contrast(pair[0], bg), contrast(pair[1], bg)))
    return list(gradient(*pairs[0]))


def from_environment(environ: Optional[Mapping[str, str]] = None, answers: Optional[Answers] = None,
                     mode: Optional[str] = None) -> Theme:
    """The theme SLOTFATHER_THEME (or `mode`) names; raises ValueError for a bad setting."""
    environ = os.environ if environ is None else environ
    return resolve(mode or checked_mode(environ.get("SLOTFATHER_THEME")), answers, environ)


def sgr_colour(colour: Colour, layer: int = 3) -> List[str]:
    """SGR parameters for a colour: layer 3 foreground, 4 background. Palette 0-15 use the 16-colour codes."""
    if colour is None:
        return []
    if isinstance(colour, tuple):
        return [f"{layer}8;2;{colour[0]};{colour[1]};{colour[2]}"]
    if colour < 8:
        return [f"{layer}{colour}"]
    if colour < 16:
        return [f"{layer + 6}{colour - 8}"]
    return [f"{layer}8;5;{colour}"]
