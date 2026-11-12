#!/usr/bin/env python3
"""Godfather's screens as pictures, with no terminal and no browser (fathers#114): a cell grid drawn to PNG or
animated WebP the way a terminal such as Ghostty draws it.

    python3 tools/raster.py FRAME.ansi [OUT.png] [--theme seti|prolight] [--font-size 18] [--scale 1]
    python3 tools/raster.py FRAME1.ansi FRAME2.ansi ... --webp OUT.webp [--fps 4]
    python3 tools/raster.py --goldens OUTDIR          every golden screen in tests/goldens/ as a PNG, for review
    godfather --once --color --width 100 --height 40 | python3 tools/raster.py - OUT.png    a live screen

A frame is what Godfather prints in colour (`Canvas.render(True)`, `godfather --once --color`, a `.ansi` golden): rows
of cells with SGR colours; hyperlinks and cursor moves are read and dropped. Each cell is drawn at integer pixel edges
(`round(column × cell width)`), so a fractional cell width leaves no seam between cells.

- Box drawing and block elements (U+2500–U+259F, and the eighths blocks of U+1FB82–U+1FB8B) are geometry snapped to
  the cell: straight lines are whole pixels, centred on the same pixels in every cell, so corners, joins and lines
  meet; rounded corners are quarter circles, anti-aliased by supersampling; blocks fill their exact fraction.
- Text uses JetBrains Mono (tools/fonts/, OFL), bold for SGR 1, with a system fallback for a glyph it lacks.
- A two-cell cluster (🆗, ⏸️) uses a colour emoji font (Apple Color Emoji, Noto Color Emoji, or
  GODFATHER_EMOJI_FONT), scaled into its two cells.

The same cells give the same pixels: nothing depends on the clock or on a renderer's state. A development tool; it
needs Pillow (requirements-dev.txt). Godfather itself does not.
"""
from __future__ import annotations

import argparse
import functools
import math
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import NamedTuple, Optional

from PIL import Image, ImageDraw, ImageFont, features

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from godfather.tui import cluster_width, clusters  # noqa: E402  (the dashboard's own idea of a cell's width)

FONTS = ROOT / "tools" / "fonts"
RGB = tuple[int, int, int]


class Theme(NamedTuple):
    background: RGB
    foreground: RGB
    palette: tuple[RGB, ...]  # the 16 ANSI colours


def hex_rgb(value: str) -> RGB:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def theme(background: str, foreground: str, palette: str) -> Theme:
    return Theme(hex_rgb(background), hex_rgb(foreground), tuple(hex_rgb(value) for value in palette.split()))


# The owner's terminal themes (cmux's bundled Ghostty themes): dark Seti, light Pro Light
THEMES = {
    "seti": theme("#111213", "#cacecd", "#323232 #c22832 #8ec43d #e0c64f #43a5d5 #8b57b5 #8ec43d #eeeeee "
                                        "#3f3f3f #c22832 #8ec43d #e0c64f #43a5d5 #8b57b5 #8ec43d #ffffff"),
    "prolight": theme("#ffffff", "#191919", "#000000 #e5492b #50d148 #c6c440 #3b75ff #ed66e8 #4ed2de #c2c2c2 "
                                            "#9f9f9f #ff6640 #48d53e #bfbe23 #0082ff #ff7eff #3bd1d2 #f2f2f2"),
}


# --- frames: SGR text to cells ---------------------------------------------------------------------------------------

class Cell(NamedTuple):
    text: str  # one cluster; "" for the second cell of a wide one
    fg: Optional[RGB]  # None: the theme's foreground
    bg: Optional[RGB]  # None: the theme's background
    bold: bool = False
    dim: bool = False
    width: int = 1


BLANK = Cell(" ", None, None)
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
CSI = re.compile(r"\x1b\[([0-9;:?<>=]*)[ -/]*([@-~])")


class Pen:
    def __init__(self, foreground: Optional[RGB] = None, background: Optional[RGB] = None) -> None:
        self.foreground, self.background = foreground, background  # what reverse video swaps an unset colour for
        self.reset()

    def reset(self) -> None:
        self.fg: Optional[RGB] = None
        self.bg: Optional[RGB] = None
        self.bold = self.dim = self.reverse = False

    def sgr(self, codes: list[int], palette: tuple[RGB, ...]) -> None:
        index = 0
        while index < len(codes):
            code = codes[index]
            if code == 0:
                self.reset()
            elif code == 1:
                self.bold = True
            elif code == 2:
                self.dim = True
            elif code == 22:
                self.bold = self.dim = False
            elif code == 7:
                self.reverse = True
            elif code == 27:
                self.reverse = False
            elif 30 <= code <= 37 or 90 <= code <= 97:
                self.fg = palette[code - 30 if code < 90 else code - 82]
            elif 40 <= code <= 47 or 100 <= code <= 107:
                self.bg = palette[code - 40 if code < 100 else code - 92]
            elif code == 39:
                self.fg = None
            elif code == 49:
                self.bg = None
            elif code in (38, 48) and index + 1 < len(codes):
                colour: Optional[RGB] = None
                if codes[index + 1] == 2 and index + 4 < len(codes):
                    colour = (codes[index + 2], codes[index + 3], codes[index + 4])
                    index += 4
                elif codes[index + 1] == 5 and index + 2 < len(codes):
                    colour = indexed(codes[index + 2], palette)
                    index += 2
                if code == 38:
                    self.fg = colour
                else:
                    self.bg = colour
            index += 1

    def cell(self, text: str, width: int) -> Cell:
        if self.reverse:  # a badge: the text in the background's colour on the foreground's
            return Cell(text, self.bg or self.background, self.fg or self.foreground, self.bold, self.dim, width)
        return Cell(text, self.fg, self.bg, self.bold, self.dim, width)


def codes(params: str) -> list[int]:
    """CSI parameters as numbers; a colon group (`38:2::r:g:b`, its colour space empty) as its `;` form."""
    found: list[int] = []
    for group in params.split(";") if params else []:
        parts = [int(part) if part.isdigit() else 0 for part in group.split(":")]
        if len(parts) > 2 and parts[0] in (38, 48) and parts[1] in (2, 5):
            parts = parts[:2] + parts[-3:] if parts[1] == 2 else parts[:2] + parts[-1:]
        found.extend(parts)
    return found


def indexed(number: int, palette: tuple[RGB, ...]) -> RGB:
    """An xterm 256-colour index."""
    if number < 16:
        return palette[number]
    if number < 232:
        number -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return steps[number // 36], steps[number // 6 % 6], steps[number % 6]
    grey = 8 + 10 * (number - 232)
    return grey, grey, grey


def parse(text: str, columns: int, rows: int, palette: tuple[RGB, ...], foreground: Optional[RGB] = None,
          background: Optional[RGB] = None) -> list[list[Cell]]:
    """A frame's cells, `columns` × `rows`: text after SGR colours, at the column CHA (`ESC[nG`) or CUP moves to;
    every other escape is dropped."""
    grid = [[BLANK] * columns for _ in range(rows)]
    pen = Pen(foreground, background)
    row = column = 0
    text = OSC.sub("", text)
    index = 0
    while index < len(text):
        found = CSI.match(text, index)
        if found:
            params, final = found.groups()
            numbers = codes(params)
            if final == "m":
                pen.sgr(numbers or [0], palette)
            elif final == "G":
                column = max(0, (numbers[0] if numbers else 1) - 1)
            elif final in "Hf":
                row = max(0, (numbers[0] if numbers else 1) - 1)
                column = max(0, (numbers[1] if len(numbers) > 1 else 1) - 1)
            index = found.end()
            continue
        if text[index] == "\x1b":
            index += 2
            continue
        end = text.find("\x1b", index)
        run = text[index:end if end >= 0 else len(text)]
        index += len(run)
        for part in re.split(r"(\r?\n|\r)", run):
            if part in ("\n", "\r\n"):
                row, column = row + 1, 0
                continue
            if part == "\r":
                column = 0
                continue
            for cluster in clusters(part):
                width = max(1, cluster_width(cluster))
                if 0 <= row < rows and column + width <= columns:
                    line = grid[row]
                    if line[column].width == 0 and column:  # over the second half of a wide cluster: blank its first
                        line[column - 1] = BLANK
                    end = column + width
                    if end < columns and line[end].width == 0:  # a wide cluster's first half replaced: blank its second
                        line[end] = BLANK
                    line[column] = pen.cell(cluster, width)
                    for extra in range(1, width):
                        line[column + extra] = pen.cell("", 0)
                column += width
    return grid


def size_of(text: str) -> tuple[int, int]:
    """The columns and rows a frame fills."""
    lines = OSC.sub("", text).replace("\r\n", "\n").rstrip("\n").split("\n")
    plain = [CSI.sub("", line) for line in lines]
    return max([1] + [sum(max(1, cluster_width(c)) for c in clusters(line)) for line in plain]), len(lines)


# --- geometry: box drawing and blocks --------------------------------------------------------------------------------

SAMPLES = 4  # supersampling per axis where an edge is not on the pixel grid (arcs, diagonals, shades excepted)
WEIGHTS = {"LIGHT": 1, "SINGLE": 1, "HEAVY": 2, "DOUBLE": 3}
DIRECTIONS = {"UP": ("up",), "DOWN": ("down",), "LEFT": ("left",), "RIGHT": ("right",),
              "HORIZONTAL": ("left", "right"), "VERTICAL": ("up", "down")}


def box_arms(char: str) -> Optional[dict[str, int]]:
    """A box-drawing character's arms from its Unicode name: {direction: 1 light, 2 heavy, 3 double}."""
    name = unicodedata.name(char, "")
    if not name.startswith("BOX DRAWINGS ") or any(word in name for word in ("DASH", "ARC", "DIAGONAL")):
        return None
    arms: dict[str, int] = {}
    weight = 1
    for segment in name[len("BOX DRAWINGS "):].split(" AND "):
        words = segment.split()
        named = [WEIGHTS[word] for word in words if word in WEIGHTS]
        weight = named[-1] if named else weight
        for word in words:
            for direction in DIRECTIONS.get(word, ()):
                arms[direction] = weight
    return arms or None


class Mask:
    """A cell's coverage, 0–255 per pixel, built from rectangles (exact) and shapes (supersampled)."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.data = bytearray(width * height)

    def rect(self, x0: float, y0: float, x1: float, y1: float, value: int = 255) -> None:
        for y in range(max(0, round(y0)), min(self.height, round(y1))):
            start, end = max(0, round(x0)), min(self.width, round(x1))
            if end > start:
                row = y * self.width
                self.data[row + start:row + end] = bytes([value]) * (end - start)

    def shape(self, inside: "Inside", box: tuple[float, float, float, float]) -> None:
        """Covers the pixels in `box` by the share of their samples `inside` holds; the larger coverage wins."""
        x0, y0, x1, y1 = box
        for y in range(max(0, math.floor(y0)), min(self.height, math.ceil(y1))):
            for x in range(max(0, math.floor(x0)), min(self.width, math.ceil(x1))):
                hits = sum(inside(x + (i + 0.5) / SAMPLES, y + (j + 0.5) / SAMPLES)
                           for i in range(SAMPLES) for j in range(SAMPLES))
                value = round(255 * hits / SAMPLES ** 2)
                if value > self.data[y * self.width + x]:
                    self.data[y * self.width + x] = value

    def image(self) -> Image.Image:
        return Image.frombytes("L", (self.width, self.height), bytes(self.data))


class Inside:
    def __call__(self, x: float, y: float) -> bool:
        raise NotImplementedError


class Ring(Inside):
    """A quarter of a circle's stroke: centre, radius, stroke width, and the quadrant's signs."""

    def __init__(self, cx: float, cy: float, radius: float, stroke: float, sx: int, sy: int) -> None:
        self.cx, self.cy, self.radius, self.half, self.sx, self.sy = cx, cy, radius, stroke / 2, sx, sy

    def __call__(self, x: float, y: float) -> bool:
        dx, dy = x - self.cx, y - self.cy
        return dx * self.sx >= 0 and dy * self.sy >= 0 and abs(math.hypot(dx, dy) - self.radius) <= self.half


class Segment(Inside):
    def __init__(self, ax: float, ay: float, bx: float, by: float, stroke: float) -> None:
        self.ax, self.ay, self.bx, self.by, self.half = ax, ay, bx, by, stroke / 2

    def __call__(self, x: float, y: float) -> bool:
        dx, dy = self.bx - self.ax, self.by - self.ay
        t = max(0.0, min(1.0, ((x - self.ax) * dx + (y - self.ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(x - self.ax - t * dx, y - self.ay - t * dy) <= self.half


class Strokes(NamedTuple):
    """Where a cell's lines go, the same in every cell of a size so lines meet: each weight's stroke, and the
    pixel column and row a centred stroke starts at."""
    light: int
    heavy: int

    def start(self, size: int, stroke: int) -> int:
        return (size - stroke) // 2


def strokes(font_px: float) -> Strokes:
    light = max(1, round(font_px / 15))
    return Strokes(light, 2 * light)


def box_mask(char: str, width: int, height: int, lines: Strokes) -> Optional[Mask]:
    """A box-drawing or block character as geometry in a width × height cell, or None for any other."""
    code = ord(char)
    mask = Mask(width, height)
    if 0x2580 <= code <= 0x259F or 0x1FB82 <= code <= 0x1FB8B:
        block(mask, code)
        return mask
    if not 0x2500 <= code <= 0x257F:
        return None
    name = unicodedata.name(char, "")
    t = lines.light if "HEAVY" not in name else lines.heavy
    x0, y0 = lines.start(width, t), lines.start(height, t)
    if "ARC" in name:  # ╭ ╮ ╯ ╰: a quarter circle from the middle of one edge to the middle of the next
        sx = 1 if "RIGHT" in name else -1
        sy = 1 if "DOWN" in name else -1
        cx, cy = x0 + t / 2, y0 + t / 2  # the centre of a straight line's stroke
        radius = min(width - cx if sx > 0 else cx, height - cy if sy > 0 else cy)
        ox, oy = cx + sx * radius, cy + sy * radius  # the circle's centre
        mask.shape(Ring(ox, oy, radius, t, -sx, -sy), (min(cx, ox) - t, min(cy, oy) - t, max(cx, ox) + t,
                                                       max(cy, oy) + t))
        if sy > 0:
            mask.rect(x0, oy, x0 + t, height)
        else:
            mask.rect(x0, 0, x0 + t, oy)
        if sx > 0:
            mask.rect(ox, y0, width, y0 + t)
        else:
            mask.rect(0, y0, ox, y0 + t)
        return mask
    if "DIAGONAL" in name:
        if "UPPER RIGHT TO LOWER LEFT" in name or "CROSS" in name:
            mask.shape(Segment(width, 0, 0, height, t), (0, 0, width, height))
        if "UPPER LEFT TO LOWER RIGHT" in name or "CROSS" in name:
            mask.shape(Segment(0, 0, width, height, t), (0, 0, width, height))
        return mask
    if "DASH" in name:
        count = {"DOUBLE": 2, "TRIPLE": 3, "QUADRUPLE": 4}[name.split()[3]]
        vertical = name.endswith("VERTICAL")
        size = height if vertical else width
        for n in range(count):
            a, b = round(size * n / count), round(size * (n + 0.5) / count + size / count / 4)
            if vertical:
                mask.rect(x0, a, x0 + t, b)
            else:
                mask.rect(a, y0, b, y0 + t)
        return mask
    arms = box_arms(char)
    if not arms or 3 in arms.values():  # double lines are left to the font; Godfather draws none
        return None
    for direction, weight in arms.items():
        stroke = lines.heavy if weight == 2 else lines.light
        sx, sy = lines.start(width, stroke), lines.start(height, stroke)
        for offset in (0,):
            # Each arm runs from its edge through the cell's middle stroke, so arms of any weight overlap there
            if direction == "up":
                mask.rect(sx + offset, 0, sx + offset + stroke, y0 + t)
            elif direction == "down":
                mask.rect(sx + offset, y0, sx + offset + stroke, height)
            elif direction == "left":
                mask.rect(0, sy + offset, x0 + t, sy + offset + stroke)
            else:
                mask.rect(x0, sy + offset, width, sy + offset + stroke)
    return mask


SHADES = {0x2591: 64, 0x2592: 128, 0x2593: 191}
QUADRANTS = {0x2596: "c", 0x2597: "d", 0x2598: "a", 0x2599: "acd", 0x259A: "ad", 0x259B: "abc", 0x259C: "abd",
             0x259D: "b", 0x259E: "bc", 0x259F: "bcd"}  # a upper left, b upper right, c lower left, d lower right


def block(mask: Mask, code: int) -> None:
    """A block element: its exact share of the cell, rounded to whole pixels."""
    w, h = mask.width, mask.height
    if code == 0x2580:
        mask.rect(0, 0, w, round(h / 2))
    elif 0x2581 <= code <= 0x2588:  # lower one eighth … full block
        mask.rect(0, h - round(h * (code - 0x2580) / 8), w, h)
    elif 0x2589 <= code <= 0x258F:  # left seven eighths … left one eighth
        mask.rect(0, 0, round(w * (0x2590 - code) / 8), h)
    elif code == 0x2590:
        mask.rect(round(w / 2), 0, w, h)
    elif code in SHADES:
        mask.rect(0, 0, w, h, SHADES[code])
    elif code == 0x2594:
        mask.rect(0, 0, w, round(h / 8))
    elif code == 0x2595:
        mask.rect(w - round(w / 8), 0, w, h)
    elif code in QUADRANTS:
        mx, my = round(w / 2), round(h / 2)
        boxes = {"a": (0, 0, mx, my), "b": (mx, 0, w, my), "c": (0, my, mx, h), "d": (mx, my, w, h)}
        for part in QUADRANTS[code]:
            mask.rect(*boxes[part])
    elif 0x1FB82 <= code <= 0x1FB86:  # upper one quarter, three eighths, five eighths, three quarters, seven eighths
        mask.rect(0, 0, w, round(h * (2, 3, 5, 6, 7)[code - 0x1FB82] / 8))
    elif 0x1FB87 <= code <= 0x1FB8B:  # right one quarter … right seven eighths
        mask.rect(w - round(w * (2, 3, 5, 6, 7)[code - 0x1FB87] / 8), 0, w, h)


# --- fonts -----------------------------------------------------------------------------------------------------------

FALLBACKS = ("/System/Library/Fonts/Apple Symbols.ttf", "/System/Library/Fonts/Menlo.ttc",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
EMOJI = (("/System/Library/Fonts/Apple Color Emoji.ttc", 160),
         ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 109),
         ("/usr/share/fonts/noto/NotoColorEmoji.ttf", 109))
FreeType = ImageFont.FreeTypeFont
ZWJ_FALLBACKS = {"👨\u200d🦳": "👴"}  # views/common.py OLD_FALLBACK


class Fonts:
    """The text fonts at one size, the fallbacks for a glyph they lack, and the colour emoji font."""

    def __init__(self, font_px: float) -> None:
        self.size = font_px
        self.regular = ImageFont.truetype(str(FONTS / "JetBrainsMono-Regular.ttf"), round(font_px))
        self.bold = ImageFont.truetype(str(FONTS / "JetBrainsMono-Bold.ttf"), round(font_px))
        self.fallbacks = [ImageFont.truetype(path, round(font_px)) for path in FALLBACKS if Path(path).is_file()]
        self.emoji: Optional[FreeType] = None
        chosen = os.environ.get("GODFATHER_EMOJI_FONT")
        for path, size in ((chosen, 0),) if chosen else EMOJI:
            if path and Path(path).is_file():
                for strike in ((size,) if size else (160, 137, 136, 109, 96, 64)):
                    try:
                        self.emoji = ImageFont.truetype(path, strike)
                        break
                    except OSError:
                        continue
            if self.emoji is not None:
                break

    def has(self, font: FreeType, text: str) -> bool:
        """Whether `font` draws `text` itself rather than its missing-glyph box."""
        return text.isspace() or all(drawn(font, char) != drawn(font, "\U0010fffd") for char in text
                                     if char not in "\ufe0e\ufe0f\u200d")

    def text_font(self, text: str, bold: bool) -> FreeType:
        first = self.bold if bold else self.regular
        if self.has(first, text):
            return first
        return next((font for font in self.fallbacks if self.has(font, text)), first)


@functools.lru_cache(maxsize=4096)
def drawn(font: FreeType, text: str) -> bytes:
    """One character in `font`, as pixels: two that match are the same glyph."""
    size = round(font.size * 2)
    image = Image.new("L", (size, size), 0)
    ImageDraw.Draw(image).text((0, 0), text, font=font, fill=255)
    return image.tobytes()


class Geometry(NamedTuple):
    """A frame's pixels: the cell size (fractional), the padding round the grid, and every cell's edges."""
    cell_width: float
    cell_height: float
    pad_x: int
    pad_y: int

    def x(self, column: int) -> int:
        return self.pad_x + round(column * self.cell_width)

    def y(self, row: int) -> int:
        return self.pad_y + round(row * self.cell_height)


class Raster:
    """Draws frames of one size and theme; cell images are cached, so a frame like the last costs little."""

    def __init__(self, columns: int, rows: int, palette: Theme, font_px: float = 18, line_height: float = 1.2,
                 scale: float = 1) -> None:
        self.columns, self.rows, self.theme = columns, rows, palette
        self.fonts = Fonts(font_px * scale)
        # The advance unhinted, from the font at a large size: FreeType rounds it to whole pixels at the drawn size
        width = ImageFont.truetype(str(FONTS / "JetBrainsMono-Regular.ttf"), 1000).getlength("M") / 1000 * font_px \
            * scale
        height = font_px * scale * line_height
        self.geometry = Geometry(width, height, round(width), round(height / 2))
        self.lines = strokes(font_px * scale)
        ascent, descent = self.fonts.regular.getmetrics()
        self.baseline = (height - (ascent + descent)) / 2 + ascent
        self.glyphs: dict[tuple[str, bool, int, int], Optional[Image.Image]] = {}

    @property
    def size(self) -> tuple[int, int]:
        g = self.geometry
        return g.x(self.columns) + g.pad_x, g.y(self.rows) + g.pad_y

    def frame(self, text: str) -> Image.Image:
        grid = parse(text, self.columns, self.rows, self.theme.palette, self.theme.foreground, self.theme.background)
        image = Image.new("RGB", self.size, self.theme.background)
        g = self.geometry
        for row, cells in enumerate(grid):
            top, bottom = g.y(row), g.y(row + 1)
            for column, cell in enumerate(cells):
                if not cell.width:
                    continue
                left, right = g.x(column), g.x(column + cell.width)
                if cell.bg is not None:
                    image.paste(cell.bg, (left, top, right, bottom))
                if cell.text.isspace():
                    continue
                glyph = self.glyph(cell.text, cell.bold, right - left, bottom - top)
                if glyph is None:
                    continue
                if glyph.mode == "RGBA":
                    image.paste(glyph, (left, top), glyph)
                else:
                    image.paste(self.ink(cell), (left, top), glyph)
        return image

    def ink(self, cell: Cell) -> RGB:
        colour = cell.fg if cell.fg is not None else self.theme.foreground
        if cell.dim:
            back = cell.bg if cell.bg is not None else self.theme.background
            colour = (round((colour[0] + back[0]) / 2), round((colour[1] + back[1]) / 2),
                      round((colour[2] + back[2]) / 2))
        return colour

    def glyph(self, text: str, bold: bool, width: int, height: int) -> Optional[Image.Image]:
        """A cell's ink: an L mask to paint in its colour, or an RGBA emoji; None for nothing. Kept per cell kind,
        so a frame like the last draws almost nothing new."""
        key = (text, bold, width, height)
        if key not in self.glyphs:
            self.glyphs[key] = self.draw_glyph(text, bold, width, height)
        return self.glyphs[key]

    def draw_glyph(self, text: str, bold: bool, width: int, height: int) -> Optional[Image.Image]:
        if len(text) == 1 or (len(text) == 2 and text[1] == "︎"):
            mask = box_mask(text[0], width, height, self.lines)
            if mask is not None:
                return mask.image()
        if cluster_width(text) == 2 and self.fonts.emoji is not None:
            found = self.emoji(text, width, height)
            if found is not None:
                return found
        font = self.fonts.text_font(text.replace("️", ""), bold)
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).text((0, self.baseline), text.replace("️", ""), font=font, fill=255, anchor="ls")
        return mask

    def emoji(self, text: str, width: int, height: int) -> Optional[Image.Image]:
        """A colour emoji drawn at its font's size and scaled to the font's height, centred in its two cells."""
        font = self.fonts.emoji
        assert font is not None
        if "\u200d" in text and not features.check("raqm"):
            # Without a shaping engine a joined sequence draws as its parts: the glyph a terminal falls back to
            # (Godfather's own 👴 for 👨‍🦳), else its first part
            text = ZWJ_FALLBACKS.get(text, text.split("\u200d")[0])
        left, top, right, bottom = (round(value) for value in font.getbbox(text))
        if right <= left or bottom <= top:
            return None
        drawn = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
        ImageDraw.Draw(drawn).text((-left, -top), text, font=font, embedded_color=True)
        if drawn.getbbox() is None:
            return None
        target = min(width, round(self.fonts.size * 1.15), height)
        factor = target / max(drawn.width, drawn.height)
        size = (max(1, round(drawn.width * factor)), max(1, round(drawn.height * factor)))
        scaled = drawn.resize(size, Image.Resampling.LANCZOS)  # pyright: ignore[reportUnknownMemberType]
        cell = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        cell.paste(scaled, ((width - scaled.width) // 2, (height - scaled.height) // 2))
        return cell


def save_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)


def save_webp(frames: list[Image.Image], path: Path, fps: float) -> None:
    """Frames at a fixed rate, lossless, looping."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(path, format="WEBP", save_all=True, append_images=frames[1:], duration=round(1000 / fps), loop=0,
                   lossless=True, quality=100, method=4)


def golden_theme(name: str) -> str:
    return "prolight" if ".prolight." in name else "seti"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("frames", nargs="*", help="frame files (- for stdin)")
    parser.add_argument("out", nargs="?", default=None)
    parser.add_argument("--webp", help="write the frames as an animated WebP")
    parser.add_argument("--fps", type=float, default=4)
    parser.add_argument("--goldens", metavar="OUTDIR", help="draw every .ansi golden in tests/goldens/")
    parser.add_argument("--theme", choices=sorted(THEMES), default="seti")
    parser.add_argument("--font-size", type=float, default=18)
    parser.add_argument("--line-height", type=float, default=1.2)
    parser.add_argument("--scale", type=float, default=1)
    parser.add_argument("--columns", type=int)
    parser.add_argument("--rows", type=int)
    args = parser.parse_args(argv)
    if args.goldens:
        for path in sorted((ROOT / "tests" / "goldens").glob("*.ansi")):
            text = path.read_text(encoding="utf-8")
            columns, rows = size_of(text)
            raster = Raster(columns, rows, THEMES[golden_theme(path.name)], args.font_size, args.line_height,
                            args.scale)
            target = Path(args.goldens) / (path.name[:-len(".ansi")] + ".png")
            save_png(raster.frame(text), target)
            print(target)
        return 0
    paths = list(args.frames)
    if not args.webp and args.out is None and len(paths) == 2:
        paths, args.out = paths[:1], paths[1]
    if not paths:
        parser.error("give a frame, or --goldens OUTDIR")
    if not args.webp and len(paths) > 1:
        parser.error("one frame makes a PNG (FRAME OUT.png); several need --webp OUT.webp")
    texts = [sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8") for path in paths]
    sizes = [size_of(text) for text in texts]
    columns = args.columns or max(size[0] for size in sizes)
    rows = args.rows or max(size[1] for size in sizes)
    raster = Raster(columns, rows, THEMES[args.theme], args.font_size, args.line_height, args.scale)
    images = [raster.frame(text) for text in texts]
    if args.webp:
        save_webp(images, Path(args.webp), args.fps)
        print(args.webp)
        return 0
    out = Path(args.out or "frame.png")
    save_png(images[0], out)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
