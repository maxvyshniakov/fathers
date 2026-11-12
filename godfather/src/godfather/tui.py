"""The terminal framework, moved from Slotfather's dashboard unchanged in behaviour: a canvas of styled cells and
its rendering, frames with titles set into their borders, tables and column sharing, scrollbars, scrollable
overlays and their footers, footer hints as click targets, key and mouse reading (the kitty keyboard protocol and
SGR mouse), full-screen setup and restore, and the background loader. It knows nothing about any tool.

Plain escape sequences, not curses: the ncurses that Python links (6.0 here) repeats a multibyte character with REP
by sending only its last byte, and terminals with REP in their terminfo, such as Ghostty and cmux, then draw each
run of █ or ─ as garbage. Writing the canvas ourselves avoids every such quirk.
"""
from __future__ import annotations

import contextlib
import datetime
import os
import re
import select
import termios
import textwrap
import threading
import time
import tty
import unicodedata
from urllib.parse import urlsplit
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple, cast

from . import keys as _keys
from . import theme as _theme


# Styles come from the theme (#14, theme.py): style -> (colour, attributes: b bold, d dim, r reverse), where the
# colour is None for the terminal's default foreground, a palette index or RGB. Frames are drawn in the muted "dim"
# token, the same as muted text, so the two cannot drift apart.
current_theme = _theme.resolve(_theme.DEFAULT_MODE, environ={})


def set_theme(theme: _theme.Theme) -> None:
    global current_theme
    current_theme = theme


ASCII = str.maketrans({"▀": "#", "🆗": "+", "🔥": "!", "🮅": "#", "■": "#", "▪": "#", "◌": "o", "□": "o", "▫": "o", "▶": ">", "▉": "#", "▌": "+", "▐": "|", "█": "#", "▓": "%", "▒": "=", "░": ".", "·": ".", "●": "*", "…": "~", "─": "-", "│": "|",
                       "╭": "+", "╮": "+", "╰": "+", "╯": "+", "↑": "^", "↓": "v", "↗": "/", "↘": "\\", "→": ">",
                       "←": "<", "≈": "~", "×": "x", "—": "-", "⏸": "=", "▭": "o", "−": "-", "╻": "#", "╹": "#",  "┃": "#", "▁": "_", "▂": "_", "▃": "-", "▄": "-",
                       "▅": "=", "▆": "=", "▇": "#"})


JOINER, TEXT_STYLE, EMOJI_STYLE = "\u200d", "\ufe0e", "\ufe0f"  # zero-width joiner, variation selectors 15 and 16


def dashboard_text(value) -> str:
    """A label or command as plain text: any process writes queue entries, so control bytes are blanked. The
    zero-width joiner of an emoji sequence (👨‍🦳) stays."""
    return "".join(char if char.isprintable() or char == JOINER else " " for char in str(value))


def cell_width(char: str) -> int:
    if unicodedata.combining(char) or char in (JOINER, TEXT_STYLE, EMOJI_STYLE):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def clusters(text: str) -> list[str]:
    """Text as the glyphs a terminal draws (#15): each character with the combining marks, variation selectors, skin
    tones and zero-width-joined characters after it, so ▶️ and 👨‍🦳 are one glyph each."""
    found: list[str] = []
    joined = False
    for char in text:
        if found and (joined or char in (JOINER, TEXT_STYLE, EMOJI_STYLE) or unicodedata.combining(char)
                      or 0x1F3FB <= ord(char) <= 0x1F3FF):
            found[-1] += char
        else:
            found.append(char)
        joined = char == JOINER
    return found


def cluster_width(cluster: str) -> int:
    """Cells a glyph takes: an emoji sequence (VS16 or joined) two, else its first character's width."""
    if len(cluster) > 1 and (EMOJI_STYLE in cluster or JOINER in cluster):
        return 2
    return cell_width(cluster[0]) if cluster else 0


def text_width(value) -> int:
    return sum(cluster_width(cluster) for cluster in clusters(dashboard_text(value)))


def clip(value, width: int) -> str:
    """value cut to `width` cells, ending in … when cut; a glyph is never split."""
    text = dashboard_text(value)
    if text_width(text) <= width:
        return text
    kept, used = [], 0
    for cluster in clusters(text):
        if used + cluster_width(cluster) > width - 1:
            break
        kept.append(cluster)
        used += cluster_width(cluster)
    return "".join(kept) + "…" if width > 0 else ""


def span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds // 60 % 60:02}m"
    return f"{seconds // 86400}d{seconds // 3600 % 24:02}h"


def minutes(seconds: float) -> str:
    """A long span to the minute: 45s, 23m, 1h05m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m" if seconds < 3600 else f"{seconds // 3600}h{seconds // 60 % 60:02}m"


def today() -> datetime.date:
    return datetime.date.today()


def clock(moment: datetime.datetime | None) -> str:
    if moment is None:
        return "--:--:--"
    local = moment.astimezone()
    return local.strftime("%H:%M:%S" if local.date() == today() else "%a %H:%M")


def megabytes(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value:.0f} MB"


def short(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def parse_utc(value) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


class Canvas:
    """A grid of [character, style, selected] cells; a wide character takes its cell and an empty one after it."""

    def __init__(self, width: int, height: int):
        self.scroll: dict[str, int] = {}
        self.tables: dict[str, dict[str, Any]] = {}
        self.links: dict[tuple[int, int], str] = {}
        self.help_view: HelpViewport | None = None
        self.focus = "requests"  # the panel keys act on (#15): requests (QUEUE), details or events; its frame is bright
        self.loading: int | None = None  # the details' spinner frame while they load in the background, else None
        self.fresh_events = 0  # events that arrived while EVENTS was scrolled down
        self.version_at: tuple[int, int, int] | None = None  # the footer version's row and columns: the changelog
        self.hint_hits: list[tuple[int, int, int, str]] = []  # (row, from, to, key) of every footer hint drawn
        self.glow: set[str] = set()  # panels scrolled within SCROLL_GLOW seconds: their thumb in the foreground
        self.width, self.height = max(0, width), max(0, height)
        self.cells = [[[" ", "text", False] for _ in range(self.width)] for _ in range(self.height)]

    def put(self, y: int, x: int, value: Any, style: str = "text", width: int | None = None) -> int:
        """Writes value from column x, cut to `width` cells and to the edge; the column after it."""
        text = clip(value, width) if width is not None else dashboard_text(value)
        if not 0 <= y < self.height:
            return x
        start = x
        for char in clusters(text):
            size = cluster_width(char)
            if size == 0:
                continue
            if x + size > self.width:
                break
            if x >= 0:
                self.cells[y][x][:2] = [char, style]
                self.links.pop((y, x), None)
                if size == 2:
                    self.cells[y][x + 1][:2] = ["", style]
                    self.links.pop((y, x + 1), None)
            x += size
        if x > start:
            self.unsplit(y, start, x)
        return x

    def unsplit(self, y: int, start: int, end: int) -> None:
        """After cells start..end were overwritten: a wide glyph they cut in half loses its other half too, a blank,
        so no row is ever a cell short or long (#15 review)."""
        cells = self.cells[y]
        if 0 < start < self.width and cells[start][0] != "" and cells[start - 1][0] and \
                cluster_width(str(cells[start - 1][0])) == 2:
            cells[start - 1][0] = " "
        if 0 <= end < self.width and cells[end][0] == "":
            cells[end][0] = " "

    def hyperlink(self, y: int, x: int, text: str, url: str, width: int, style: str = "accent") -> None:
        end = self.put(y, x, text, style, width)
        if safe_link(url):
            for column in range(max(0, x), min(self.width, end)):
                self.links[y, column] = url

    def right(self, y: int, end: int, value: Any, style: str = "text") -> int:
        """Writes value ending just before column `end`; the column it starts at."""
        start = end - text_width(value)
        self.put(y, start, value, style)
        return start

    def segments(self, y: int, x: int, parts: Any, width: int | None = None) -> int:
        """Writes (text, style) parts one after another, all within `width` cells from x."""
        end = self.width if width is None else x + width
        for text, style in parts:
            if x >= end:
                break
            x = self.put(y, x, text, style, end - x)
        return x

    def fill(self, y: int, x: int, height: int, width: int) -> None:
        for row in range(max(0, y), min(self.height, y + height)):
            for column in range(max(0, x), min(self.width, x + width)):
                self.cells[row][column] = [" ", "text", False]
                self.links.pop((row, column), None)
            self.unsplit(row, max(0, x), min(self.width, x + width))

    def overlay_footer(self, y: int, x: int, width: int, hints: list[tuple[str, str]], right: str = "") -> None:
        """An overlay's key line, as the main footer (#15): the hints centred in key and muted tokens, the least
        needed dropped when narrow (closing last), and a muted position on the right."""
        inner = max(0, width - 4)
        room = inner - (text_width(right) + 2 if right else 0)
        for drop in ("↑↓", "q", "←→", "d", "enter"):
            if hints_width(hints) <= room:
                break
            hints = [hint for hint in hints if hint[0] != drop]
        start = x + 2 + max(0, (inner - hints_width(hints)) // 2)
        if right:
            self.right(y, x + width - 2, right, "dim")
        for index, (key, action) in enumerate(hints):
            start = self.hint(y, start, key, action, x + width - 2 - start)
            if index < len(hints) - 1:
                start += HINT_GAP

    def hint(self, y: int, x: int, key: str, action: str, width: int | None = None) -> int:
        """One footer hint, key and label, drawn and made a click target that does what its key does (HINT_KEYS):
        the one registry both drawing and clicks use. The column after it."""
        end = self.segments(y, x, [(key, "key"), (f" {action}", "dim")], width)
        presses = HINT_KEYS.get(key)
        if isinstance(presses, tuple):  # ↑↓, ←→: each arrow its own key; the label does nothing
            for offset, press in enumerate(presses):
                self.hint_hits.append((y, x + offset, x + offset + 1, press))
        elif presses:
            self.hint_hits.append((y, x, end, presses))
        return end

    def hint_at(self, mx: int, my: int) -> str | None:
        """The key a click on a footer hint presses, None between hints."""
        return next((press for y, start, end, press in reversed(self.hint_hits) if y == my and start <= mx < end),
                    None)

    def scrollbar(self, y: int, x: int, height: int, offset: int, visible: int, total: int, name: str = "") -> None:
        """A scrollable panel's scrollbar in column x, the outer of the two padding cells left of its right frame, so
        a blank cell stays between it and the content (a right-aligned pid touched the inner one) and nothing moves
        (#15): a thumb as long as the visible share, at the bottom on the last page, in the muted token at rest and in
        the foreground for a flash while panel `name` scrolls (owner). Nothing while everything fits."""
        if total <= visible or height <= 0 or visible <= 0:
            return
        halves = 2 * height  # in half cells, so the thumb moves in half-cell steps (owner)
        length = min(halves, max(1, round(halves * visible / total)))
        start = round((halves - length) * min(offset, total - visible) / (total - visible))
        for row in range(height):  # the thumb alone, no track (owner)
            upper, lower = start <= 2 * row < start + length, start <= 2 * row + 1 < start + length
            if upper or lower:
                glyph = SCROLL_THUMB if upper and lower else THUMB_LOWER if lower else THUMB_UPPER
                self.put(y + row, x, glyph, "text" if name in self.glow else "dim")  # fg only while scrolling

    def select(self, y: int, start: int, end: int, tint: bool | str = True) -> None:
        """Marks cells start..end of row y selected: True for the neutral background, a holder style for its tint."""
        if 0 <= y < self.height:
            for column in range(max(0, start), min(self.width, end)):
                self.cells[y][column][2] = tint

    def box(self, y: int, x: int, height: int, width: int, title: str = "", note: str = "", name: str = "") -> None:
        """A rounded light frame in the frame token (a little paler than muted text); the focused panel's (`name`,
        #15) in the accent blue, the same glyphs: focus shows by colour alone (owner)."""
        if height < 2 or width < 6:
            return
        frame = "accent" if name and name == self.focus else "frame"
        top_left, top_right, bottom_left, bottom_right, across, down = "╭", "╮", "╰", "╯", "─", "│"
        self.put(y, x, top_left + across * (width - 2) + top_right, frame)
        for row in range(y + 1, y + height - 1):
            self.put(row, x, down, frame)
            self.put(row, x + width - 1, down, frame)
        self.put(y + height - 1, x, bottom_left + across * (width - 2) + bottom_right, frame)
        after = self.put(y, x + 2, f" {title} ", "title", width - 4) if title else x + 2
        if note and x + width - 2 - text_width(note) - 2 > after:
            self.right(y, x + width - 2, f" {note} ", "dim")

    def runs(self, y: int) -> list[list]:
        """Row y as [text, style, selected] runs."""
        found = []
        for char, style, selected in self.cells[y]:
            if found and found[-1][1] == style and found[-1][2] == selected:
                found[-1][0] += char
            else:
                found.append([char, style, selected])
        return found

    def lines(self, colour: bool, ascii_only: bool = False, place: bool = False) -> list[str]:
        """Every row at full width, with SGR colour codes when `colour`. `place` (the live screen, never --once): a
        wide glyph is drawn over two spaces in its style, from its own cell, and the cursor is then put on the cell
        after it (CHA), so a terminal that draws it one cell or four wide shifts nothing and leaves no gap (#15)."""
        found = []
        for y in range(self.height):
            runs: list[list[Any]] = []
            for column, (char, style, selected) in enumerate(cast(list[tuple[str, str, bool]], self.cells[y])):
                url = self.links.get((y, column), "")
                wide = place and bool(char) and cluster_width(char) == 2
                if runs and runs[-1][1:4] == [style, selected, url] and runs[-1][4] is None and not wide:
                    runs[-1][0] += char
                else:
                    runs.append([char, style, selected, url, column if wide else None])
            parts: list[str] = []
            for text, style, selected, url, at in runs:
                if colour:
                    parts.append(sgr(style, selected))
                if at is not None:
                    parts.append(f"  \x1b[{at + 1}G")
                parts.append(f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\" if url and colour else text)
                if colour:
                    parts.append("\x1b[0m")
                if at is not None:
                    parts.append(f"\x1b[{at + 3}G")
            line = "".join(parts)
            found.append(line.translate(ASCII) if ascii_only else line)
        return found

    def render(self, colour: bool, ascii_only: bool = False) -> str:
        return "\n".join(line if colour else line.rstrip() for line in self.lines(colour, ascii_only))


def safe_link(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        local_file = parsed.scheme == "file" and parsed.netloc in ("", "localhost") and parsed.path.startswith("/")
        return ((local_file or (parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username))
                and all(char.isprintable() and not char.isspace() for char in url))
    except ValueError:
        return False


def sgr(style: str, selected: bool | str) -> str:
    """The SGR sequence for a cell. A selected cell has the theme's selection background, or is bold where the
    theme has none; its text keeps its colour (builtin: default-foreground text turns 252 over its dark band)."""
    fore, flags = current_theme.styles[style]
    if selected and fore is None:
        fore = current_theme.selected_text
    # `selected` is True, or a holder style: a running row's own tint (#15), the neutral selection where it has none
    back = (current_theme.tints.get(selected) if isinstance(selected, str) else None) or current_theme.selection \
        if selected else None
    bold = "b" in flags or (bool(selected) and back is None)
    codes = ["0"] + (["1"] if bold else []) + (["2"] if "d" in flags and fore is None else [])
    codes += _theme.sgr_colour(fore)
    if back is not None:
        codes += _theme.sgr_colour(back, 4)
    codes += ["7"] if "r" in flags else []
    return "\x1b[" + ";".join(codes) + "m"


def columns(width: int, spec: list[tuple]) -> list[tuple]:
    """spec: (key, title, width, right-aligned, priority) with width 0 for the one flexible column. Drops the
    columns of the highest priority number until the rest leave the flexible one 10 cells; (key, title, x, width,
    right-aligned)."""
    keep = list(spec)
    while sum(c[2] for c in keep) + 2 * (len(keep) - 1) + 10 > width and any(c[2] and c[4] for c in keep):
        keep.remove(max((c for c in keep if c[2] and c[4]), key=lambda c: c[4]))
    flexible = max(4, width - sum(c[2] for c in keep) - 2 * (len(keep) - 1))
    placed, x = [], 0
    for key, title, size, right, _ in keep:
        placed.append((key, title, x, size or flexible, right))
        x += (size or flexible) + 2
    return placed


def draw_row(canvas: Canvas, y: int, x: int, placed: list, values: dict) -> None:
    for key, _, column, size, right in placed:
        value = values.get(key, "")
        parts = value if isinstance(value, list) else [(value, "text")] if isinstance(value, str) else [value]
        start = x + column
        if right:
            start += max(0, size - sum(text_width(text) for text, _ in parts))
        canvas.segments(y, start, parts, x + column + size - start)


def table_view(canvas: Canvas, name: str, y: int, x: int, height: int, width: int,
               top: int, room: int, count: int, selected: int | None = None,
               base: int | None = None, row_items: list | None = None) -> tuple[int, int]:
    """One viewport shared by painting, hit testing and wheel scrolling: `room` rows from `top`, between a blank
    padding row under the top border and one over the bottom border; the scrollbar's thumb runs in them too."""
    room = max(0, room)
    visible = room
    automatic = max(0, (selected or 0) - visible + 1) if selected is not None else 0
    offset = max(0, min(canvas.scroll.get(name, automatic), max(0, count - visible)))
    canvas.tables[name] = dict(x=x, y=y, width=width, height=height, top=top,
                              visible=visible, count=count, offset=offset, base=base, row_items=row_items)
    if room:  # the scrollbar shows what overflows (no row counts)
        canvas.scrollbar(top, x + width - 2, visible, offset, visible, count, name)
        fresh = canvas.fresh_events if name == "events" and offset else 0
        if fresh:  # EVENTS scrolled down: what arrived since, in its bottom border (Home goes back to it)
            canvas.put(y + height - 1, x + 2, f" ↑ {fresh} new ", "text" if canvas.focus == name else "dim",
                       width - 4)
    return offset, visible


def mouse_action(canvas: Canvas, event: tuple[str, int, int, int, bool],
                 scroll: dict[str, int], selected: int) -> int:
    if canvas.help_view is not None:
        help_mouse(canvas, event, scroll)
        return selected
    _, button, mx, my, pressed = event
    if not pressed:
        return selected
    for name, view in reversed(list(canvas.tables.items())):
        if not (view["x"] < mx < view["x"] + view["width"] - 1
                and view["y"] < my < view["y"] + view["height"] - 1):
            continue
        code = button & ~28  # Shift / Alt / Ctrl do not change the wheel direction.
        if code in (64, 65):
            scroll[name] = max(0, min(scroll.get(name, cast(int, view["offset"])) + (3 if code == 65 else -3),
                                      max(0, view["count"] - view["visible"])))
        elif code == 0:
            row = my - view["top"]
            if 0 <= row < min(view["visible"], view["count"] - view["offset"]):
                position = view["offset"] + row
                if view["row_items"] is not None:
                    item = view["row_items"][position]
                    return selected if item is None else item
                if view["base"] is not None:
                    return view["base"] + position
        return selected
    return selected


def table_header(canvas: Canvas, y: int, x: int, height: int, width: int, title: str, placed: list[Any], gutter: int = 0,
                 name: str = "") -> None:
    """The panel's top border doubles as its table header, aligned with the data."""
    canvas.box(y, x, height, width, name=name)
    for index, (_key, label, column, size, right) in enumerate(cast(list[tuple[str, str, int, int, bool]], placed)):
        value = title if index == 0 else label.lower()
        # The title sits where every panel's does (x + 3); the other headers over their columns, `gutter` in
        start = x + 3 if index == 0 else x + 3 + gutter + column + (max(0, size - text_width(value)) if right else 0)
        canvas.put(y, start - 1, " ", "text")
        focused = bool(name) and canvas.focus == name  # a focused panel's labels in the foreground, its frame blue
        canvas.put(y, start, value, "title" if index == 0 else "text" if focused else "dim", size)
        canvas.put(y, min(x + width - 2, start + min(size, text_width(value))), " ", "text")


SELECTED_FRAME = "┃"  # the left frame beside a selected row, heavy in the row's colour (#15); # on ASCII
# A scrollbar's thumb (#15): a whole cell, a cell's lower half (where it starts), its upper half (where it ends);
# # on an ASCII terminal
SCROLL_THUMB, THUMB_LOWER, THUMB_UPPER = "┃", "╻", "╹"


HELP_INDENT, HELP_GAP = 2, 2  # before a term, and between the term column and its description


def wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, max(1, width), break_long_words=True, break_on_hyphens=False) or [""]




HEADING_BEFORE, HEADING_AFTER = 2, 1  # blank lines around an overlay's section or release title (owner)


def add_heading(lines: list[list[tuple[str, str]]], title: list[tuple[str, str]]) -> None:
    """Adds a section title: HEADING_BEFORE blank lines before it but the first, HEADING_AFTER after it."""
    blank: list[tuple[str, str]] = []
    lines.extend([blank[:] for _ in range(HEADING_BEFORE if lines else 0)] + [title]
                 + [blank[:] for _ in range(HEADING_AFTER)])



class HelpViewport(NamedTuple):
    x: int
    y: int
    width: int
    height: int
    offset: int
    visible: int
    total: int
    name: str = "help"  # the overlay: help, or changelog


def help_key(canvas: Canvas, key: str, scroll: dict[str, int]) -> str:
    """Help owns navigation while open; dashboard actions cannot leak through it."""
    if key in ("?", "v", "esc"):
        return "close"
    if key in ("q", "Q"):
        return "quit"
    info = canvas.help_view
    if info is None:
        return "ignored"
    maximum = max(0, info.total - info.visible)
    offset = scroll.get(info.name, info.offset)
    page = max(1, info.visible - 1)
    if key in ("j", "down"):
        offset += 1
    elif key in ("k", "up"):
        offset -= 1
    elif key in ("page_down", " "):
        offset += page
    elif key == "page_up":
        offset -= page
    elif key in ("g", "home"):
        offset = 0
    elif key in ("G", "end"):
        offset = maximum
    else:
        return "ignored"
    scroll[info.name] = max(0, min(offset, maximum))
    return "scroll"


def help_mouse(canvas: Canvas, event: tuple[str, int, int, int, bool], scroll: dict[str, int]) -> str:
    """The wheel inside the help scrolls it; a left click outside closes it ("close"); nothing reaches the dashboard."""
    info = canvas.help_view
    if info is None:
        return "ignored"
    _, button, x, y, pressed = event
    code = button & ~28
    if not (info.x <= x < info.x + info.width and info.y <= y < info.y + info.height):
        return "close" if pressed and code == 0 else "ignored"
    if not pressed:
        return "ignored"
    if code in (64, 65):
        offset = scroll.get(info.name, info.offset) + (3 if code == 65 else -3)
        scroll[info.name] = max(0, min(offset, max(0, info.total - info.visible)))
    return "ignored"


def draw_overlay(canvas: Canvas, title: str, layout: Any, name: str) -> None:
    """A centred, scrollable overlay (help, the changelog): `layout(width)` gives its lines as (text, style) parts;
    the frame, scrollbar, counts and centred footer are the same for every one."""
    width = min(max(0, canvas.width - 2), 76)
    # The text starts under the title's first letter (x + 3); on the right two padding cells, the scrollbar in one
    lines = cast(list[list[tuple[str, str]]], layout(width - 7))
    height = min(max(0, canvas.height - 2), len(lines) + 5)  # a blank row above the lines and one below them
    y, x = (canvas.height - height) // 2, (canvas.width - width) // 2
    visible = max(0, height - 5)
    offset = max(0, min(canvas.scroll.get(name, 0), max(0, len(lines) - visible)))
    canvas.scroll[name] = offset
    canvas.help_view = HelpViewport(x, y, width, height, offset, visible, len(lines), name)
    canvas.fill(y, x, height, width)
    canvas.box(y, x, height, width, title)
    inner = max(0, width - 4)
    if height < 6:
        canvas.put(y + 1, x + 2, "Enlarge terminal", "dim", inner)
        return
    canvas.scrollbar(y + 2, x + width - 2, visible, offset, visible, len(lines), name)
    for row, parts in enumerate(lines[offset:offset + visible], y + 2):
        canvas.segments(row, x + 3, parts, width - 7)
    shown = f"{round(100 * (offset + visible) / len(lines))}%" if len(lines) > visible else ""
    hints = [("↑↓", "scroll"), ("esc", "close")] + ([("q", "quit")] if name == "help" else [])
    canvas.overlay_footer(y + height - 2, x, width, hints, shown)



# The changelog: CHANGELOG.md shipped with the installed package, as titles and bullet rows

RELEASE = re.compile(r"^## \[?([^\]\s]+)\]?(?: - (\d{4}-\d\d-\d\d))?(?: \((\w+)\))?")
MARKUP = re.compile(r"\*\*|`|\[([^\]]*)\]\([^)]*\)")


class Release(NamedTuple):
    version: str
    date: str
    commit: str
    sections: list[tuple[str, list[str]]]  # (Added, [entries])


def plain(text: str) -> str:
    """Markdown taken out of a changelog entry: bold, code and links."""
    return MARKUP.sub(lambda found: found[1] or "", text)


def parse_changelog(text: str) -> list[Release]:
    """The releases of a Keep a Changelog file, newest first, with markdown taken out of the entries; an entry's
    continuation lines join it."""
    found: list[Release] = []
    for line in text.splitlines():
        heading = RELEASE.match(line)
        if heading:
            found.append(Release(heading[1], heading[2] or "", heading[3] or "", []))
        elif not found:
            continue
        elif line.startswith("### "):
            found[-1].sections.append((line[4:].strip(), []))
        elif line.startswith("- ") and found[-1].sections:
            found[-1].sections[-1][1].append(plain(line[2:].strip()))
        elif line.startswith("  ") and line.strip() and found[-1].sections and found[-1].sections[-1][1]:
            entries = found[-1].sections[-1][1]
            entries[-1] += " " + plain(line.strip())
    return found


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)) or (-1,)


def allocate(available: int, panels: list[tuple[str, int, int]]) -> dict[str, int]:
    """panels: (name, minimum, natural height) by priority. Each gets its minimum while room lasts, then grows
    towards its natural height; the last panel placed takes what remains."""
    heights = {}
    for name, minimum, _ in panels:
        if minimum <= available:
            heights[name], available = minimum, available - minimum
    for name, _, natural in panels:
        if name in heights:
            extra = min(available, max(0, natural - heights[name]))
            heights[name], available = heights[name] + extra, available - extra
    if heights:
        heights[list(heights)[-1]] += available
    return heights


HINT_GAP = 3
# What a click on a hint presses, by the hint's key: the same key; for a pair of arrows, each arrow its own
HINT_KEYS: dict[str, str | tuple[str, ...]] = {"s": "s", "?": "?", "q": "q", "tab": "\t", "esc": "esc", "enter": "\r",
                                              "d": "d", "↑↓": ("up", "down"), "←→": ("left", "right")}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def hints_width(hints: list[tuple[str, str]]) -> int:
    return sum(text_width(key) + 1 + text_width(action) for key, action in hints) + HINT_GAP * max(0, len(hints) - 1)


# Dashboard terminal: plain escape sequences, not curses. The ncurses that Python links (6.0 here) repeats a
# multibyte character with REP by sending only its last byte, and terminals with REP in their terminfo, such as
# Ghostty and cmux, then draw each run of █ or ─ as garbage. Writing the canvas ourselves avoids every such quirk.

KEYS = {"\x1b[A": "up", "\x1bOA": "up", "\x1b[B": "down", "\x1bOB": "down", "\x1b[H": "home", "\x1bOH": "home",
        "\x1b[1~": "home", "\x1b[F": "end", "\x1bOF": "end", "\x1b[4~": "end", "\x1b[5~": "page_up", "\x1b[6~": "page_down",
        "\x1b[C": "right", "\x1bOC": "right", "\x1b[D": "left", "\x1bOD": "left", "\x1b[Z": "back_tab"}
KEY_PENDING: dict[int, str] = {}
MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
SEQUENCE = re.compile(r"\x1b(\[[0-9;:?<]*[ -/]*[@-~]|O.)")
PARTIAL = re.compile(r"\x1b(\[(?:<[0-9;]*|[0-9;:?]*[ -/]*)|O|%s)?$" % _theme.OSC_PARTIAL)  # \x1b]: an OSC report


def read_keys(fd: int, reports: list[str] | None = None) -> list[str | tuple[str, int, int, int, bool]]:
    """Keys waiting on fd: printable characters, the names in KEYS, or esc. A sequence split across reads is
    completed first; one this view does not know is dropped rather than read as Esc and its letters. What the
    terminal reports (its colours, a colour-scheme change: theme.REPORT) goes to `reports`, never to the keys."""
    data = KEY_PENDING.pop(fd, "") + os.read(fd, 1024).decode("utf-8", "ignore")
    while PARTIAL.search(data) and select.select([fd], [], [], 0.03)[0]:
        chunk = os.read(fd, 1024)
        if not chunk:
            break
        data += chunk.decode("utf-8", "ignore")
    partial = PARTIAL.search(data)
    if partial and partial.group() != "\x1b":
        KEY_PENDING[fd] = partial.group()
        data = data[:partial.start()]
    found: list[str | tuple[str, int, int, int, bool]] = []
    index = 0
    while index < len(data):
        report = _theme.REPORT.match(data, index)
        if report:
            if reports is not None:
                reports.append(report.group())
            index = report.end()
            continue
        mouse = MOUSE.match(data, index)
        if mouse:
            button, x, y, action = mouse.groups()
            found.append(("mouse", int(button), int(x) - 1, int(y) - 1, action == "M"))
            index = mouse.end()
            continue
        event = _keys.KITTY_KEY.match(data, index)
        if event:  # the kitty keyboard protocol (#13): a key with its base-layout key, or one this view drops
            key = _keys.kitty(event)
            if key == "\x03":  # Ctrl-C reaches the dashboard as a key under the protocol, not as SIGINT
                raise KeyboardInterrupt
            if key is not None:
                found.append(key)
            index = event.end()
            continue
        name = next((KEYS[seq] for seq in KEYS if data.startswith(seq, index)), None)
        sequence = SEQUENCE.match(data, index)
        if name or sequence:
            if name:
                found.append(name)
            index = sequence.end() if sequence else index + 1
        else:
            found.append("esc" if data[index] == "\x1b" else data[index])
            index += 1
    return found


@contextlib.contextmanager
def full_screen(fd_in: int, fd_out: int, answers: _theme.Answers | None = None, probe: str = "👨‍🦳"):
    """The alternate screen with the cursor hidden, line wrap off and keys read one at a time without echo; Ctrl-C
    still interrupts. With `answers`, the terminal is first asked for its colours (#14), at most
    theme.QUERY_TIMEOUT, and colour-scheme change notifications are subscribed to. Everything is put back on the way
    out. `probe` is the glyph whose drawn width the terminal is asked for (the dashboard's 👨‍🦳)."""
    saved = termios.tcgetattr(fd_in)
    try:
        tty.setcbreak(fd_in)
        active = termios.tcgetattr(fd_in)
        active[0] &= ~(termios.IXON | termios.IXOFF)  # Ctrl-S must not freeze the screen by terminal flow control.
        termios.tcsetattr(fd_in, termios.TCSANOW, active)
        os.write(fd_out, b"\x1b[?1049h\x1b[?25l")
        if answers is not None:  # before the kitty push, so no reply can be taken for a key event
            # On the alternate screen, so the width probe of 👨‍🦳 (#15) never shows on the shell's screen
            typed = _theme.ask(fd_in, fd_out, answers, probe=probe)
            if typed:
                KEY_PENDING[fd_in] = typed  # keys typed meanwhile come with the next read
        # The kitty keyboard protocol (#13) reports each key's base-layout key, so hotkeys work on any layout; a
        # terminal without it ignores the request, and the layout table in keys.hotkey() stands in.
        os.write(fd_out, b"\x1b[?7l\x1b[?1000h\x1b[?1006h\x1b[2J"
                 + (_theme.SCHEME_ON if answers is not None else b"") + _keys.KITTY_ON)
        yield
    finally:
        KEY_PENDING.pop(fd_in, None)
        os.write(fd_out, _keys.KITTY_OFF + (_theme.SCHEME_OFF if answers is not None else b"")
                 + b"\x1b[?1006l\x1b[?1000l\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l")
        # TCSAFLUSH: unread input goes, so a late colour reply never lands on the shell's command line
        termios.tcsetattr(fd_in, termios.TCSAFLUSH if answers is not None else termios.TCSADRAIN, saved)


class Loader:
    """One background thread running `work` for the newest request only (#15): a request made while one runs
    replaces any that waits, so moving quickly through rows loads just the row it ends on. Results are taken on the
    main thread with take(); each one wakes the main loop through `wake`, a pipe its select watches."""

    def __init__(self, work: Any, wake: int) -> None:
        self.work, self.wake = work, wake
        self.lock = threading.Lock()
        self.pending: tuple[Any, Any] | None = None
        self.current: Any = None  # the key being worked on
        self.busy = False
        self.results: list[tuple[Any, Any, BaseException | None]] = []

    def request(self, key: Any, argument: Any = None) -> None:
        with self.lock:
            self.pending = (key, argument)
            if self.busy:
                return
            self.busy = True
        threading.Thread(target=self.run, daemon=True).start()

    def wants(self, key: Any) -> bool:
        with self.lock:
            return self.current == key or (self.pending is not None and self.pending[0] == key)

    def run(self) -> None:
        while True:
            with self.lock:
                if self.pending is None:
                    self.busy, self.current = False, None
                    return
                (key, argument), self.pending = self.pending, None
                self.current = key
            value: Any = None
            failure: BaseException | None = None
            try:
                value = self.work(argument)
            except Exception as error:  # noqa: BLE001 - a failed load is shown, never the end of the view
                failure = error
            with self.lock:
                self.results.append((key, value, failure))
            with contextlib.suppress(OSError):
                os.write(self.wake, b".")

    def take(self) -> list[tuple[Any, Any, BaseException | None]]:
        with self.lock:
            found, self.results = self.results, []
            return found



NAVIGATION = ("up", "k", "down", "j", "page_up", "page_down", "home", "g", "end", "G")



def panel_key(canvas: Canvas, scroll: dict[str, int], name: str, key: str) -> bool:
    """A navigation key on a focused DETAILS or EVENTS panel is that panel's, also with nothing to scroll (still
    loading, empty): it never falls through to QUEUE's selection. True when the key was the panel's."""
    if key not in NAVIGATION:
        return False
    scroll_panel(canvas, scroll, name, key)
    return True


def scroll_panel(canvas: Canvas, scroll: dict[str, int], name: str, key: str) -> bool:
    """A scroll key on the focused DETAILS or EVENTS panel: a line, a page, the top or the bottom (#15)."""
    view = canvas.tables.get(name)
    if not view:
        return False
    visible, count = int(view["visible"]), int(view["count"])
    last = max(0, count - visible)
    offset = scroll[name] if name in scroll else int(view["offset"])
    moves = {"up": -1, "k": -1, "down": 1, "j": 1, "page_up": -max(1, visible - 1), "page_down": max(1, visible - 1)}
    if key in moves:
        offset += moves[key]
    elif key in ("home", "g"):
        offset = 0
    elif key in ("end", "G"):
        offset = last
    else:
        return False
    scroll[name] = max(0, min(last, offset))
    return True


SCROLL_GLOW = 0.28  # seconds a scrolled panel's thumb stays in the foreground after its last scroll: a flash


class ScrollGlow:
    """When each panel last scrolled, by the wheel, keys or an overlay's own scrolling: its thumb is drawn in the
    foreground until SCROLL_GLOW after that, and the live loop wakes once at that moment to draw it back."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.last: dict[str, float] = {}

    def touch(self, names: Iterable[str]) -> None:
        now = self.clock()
        for name in names:
            self.last[name] = now

    def bright(self) -> set[str]:
        now = self.clock()
        self.last = {name: when for name, when in self.last.items() if now - when < SCROLL_GLOW}
        return set(self.last)

    def fade(self) -> float | None:
        """Seconds until the next thumb goes back to its colour, None while none glows: one repaint, then."""
        now = self.clock()
        due = [when + SCROLL_GLOW - now for when in self.last.values() if now - when < SCROLL_GLOW]
        return max(0.0, min(due)) if due else None


def scrolled(before: dict[str, int], after: dict[str, int]) -> set[str]:
    """The panels scrolled: their offset set to another value. One dropped (a new selection, an overlay opened)
    went back to its start without being scrolled."""
    return {name for name in after if before.get(name, 0) != after[name]}
