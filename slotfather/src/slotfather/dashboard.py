#!/usr/bin/env python3
"""slotfather-dashboard: a live queue view with explicit settings; `slotfather dashboard` runs it.

  slotfather dashboard [--interval S]                        live view: ? lists the keys, q quits
  slotfather dashboard --once [--width N] [--height N] [--color]   one snapshot as text

It lives apart from the queue so the lease code stays small: it reads what `slotfather status` reads, and a fault
here ends the view, never admission. It loads the slotfather executable beside it (symlinks resolved) as a module.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import termios
import textwrap
import threading
import time
import tty
import unicodedata
from urllib.parse import urlsplit
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple, cast


from . import config as _config
from . import dashboard_settings as _settings
from . import keys as _keys
from . import lane_context as _lane_context
from . import platform as _platform
from . import query as _query
from . import schedule as _schedule
from . import state as _state
from . import theme as _theme
from . import version as _version


# dashboard_model() gathers plain data and dashboard_draw() lays it out on a Canvas of styled cells; the live view
# and --once write that canvas as the same escape sequences, so they are the same picture.

SPARK = "▁▂▃▄▅▆▇█"
CHART_ROWS, MIN_CHART_ROWS = 4, 2  # the chart's rows when there is room; fewer than two, it goes (owner)
MACHINE_HEIGHT = 7 + CHART_ROWS  # stacked: frame, a blank row, 3 resource rows, the chart and its axis line
MACHINE_SIDE_HEIGHT = 7  # the chart beside the rows: frame, a blank row, 3 rows level with 3 chart rows, the axis
MACHINE_LABEL = 8  # a MACHINE row: its health mark, a gap, the label (load, mem, swap), a gap, then the bar
MACHINE_BAR_MIN, MACHINE_BAR_SIDE = 4, 12  # the shortest bar before values go short; beside the chart, its bars
MACHINE_GAP, MACHINE_CHART_MIN = 2, 24  # between the rows and a chart beside them, which needs this many columns
# The width from which the chart sits beside the rows: frame and padding (6), the rows with a 9-cell value
# column, the gap and the chart's least width (owner, 2026-11-12: picked by rendering)
MACHINE_SIDE_WIDTH = 6 + MACHINE_LABEL + MACHINE_BAR_SIDE + 1 + 9 + MACHINE_GAP + MACHINE_CHART_MIN
HISTORY_WINDOW = 3600  # seconds of queue.log the utilisation strip rebuilds
LOG_TAIL = 262144  # bytes of queue.log read per refresh
WIDE = 110  # columns from which the panels sit side by side
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
GRANTED = re.compile(r"granted (\d+) to (.*) \(pid (\d+)\)$")
RELEASED = re.compile(r"released (\d+) \(\d+-(\d+)\.json\)$")
REAPED = re.compile(r"reaped (\S+) lease of (\d+) for (.*) \(pid (\d+) exited\)$")
REPRIORITIZED = re.compile(r"reprioritized (.*) \(pid (\d+)\) from (\w+) to (\w+)$")
WAITING_LONG = re.compile(r"waiting long: (.*) \(pid (\d+), [^)]*\) waiting (\S+) behind .*: est\. start (\S+)")


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


# Dashboard data

def process_usage() -> tuple[dict, dict]:
    """pid -> (CPU percent, resident KB, command name) for every process, and ppid -> its child pids."""
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,%cpu=,rss=,comm="], capture_output=True, text=True)
    if result.returncode:
        raise OSError(f"CPU process sample failed: ps exited {result.returncode}")
    own, children = {}, {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
            with contextlib.suppress(ValueError):
                own[int(parts[0])] = (float(parts[2].replace(",", ".")), int(parts[3]), os.path.basename(parts[4]))
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return own, children


def process_tree(pid, own: dict, children: dict) -> list[tuple[int, float, int, str]]:
    """(pid, CPU percent, resident KB, name) for pid and all its descendants: a lease's whole command."""
    found, stack, seen = [], [pid], set()
    while stack:
        current = stack.pop()
        if current in seen or current not in own:
            continue
        seen.add(current)
        found.append((current, *own[current]))
        stack.extend(children.get(current, ()))
    return found


def process_cwd(pid) -> str | None:
    """The directory a holder was started in, which names its project better than most labels."""
    with contextlib.suppress(OSError):
        return os.readlink(f"/proc/{pid}/cwd")
    try:
        result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True,
                                timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return next((line[1:] for line in result.stdout.splitlines() if line.startswith("n")), None)


def macos_used_memory(text: str) -> float | None:
    """Physical MB in active, wired and compressor pages; inactive cache is excluded."""
    page = re.search(r"page size of (\d+) bytes", text)
    fields = dict(re.findall(r"^(Pages [^:]+):\s+(\d+)\.", text, re.MULTILINE))
    names = ("Pages active", "Pages wired down", "Pages occupied by compressor")
    if not page or any(name not in fields for name in names):
        return None
    return sum(int(fields[name]) for name in names) * int(page[1]) / (1024 * 1024)


def memory_facts() -> dict:
    """Free and total swap in MB and the kernel's memory-pressure level, as far as this host reports them."""
    seam = _config.setting("MEMORY_FILE")
    if seam:
        with contextlib.suppress(OSError, ValueError):
            free, level = (float(v) for v in Path(seam).read_text().split()[:2])
            return {"swap_free": free, "swap_total": None, "level": level}
        return {"swap_free": None, "swap_total": None, "level": None}
    if _platform.linux():  # no pressure level: MemAvailable and PSI, as the memory policy reads them
        facts = _platform.linux_memory()
        return {"swap_total": facts["swap_total"], "swap_free": facts["swap_free"], "level": None,
                "available": facts["available"], "total": facts["total"], "psi_some": facts["psi_some"],
                "psi_full": facts["psi_full"], "cgroup_available": facts.get("cgroup_available"),
                "cgroup": facts.get("cgroup")}
    total, free = _platform.swap_usage() or (None, None)
    facts = {"swap_total": total, "swap_free": free, "level": _platform.pressure_level()}
    with contextlib.suppress(OSError, ValueError, subprocess.TimeoutExpired):
        physical = _platform.sysctl("hw.memsize")
        stats = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2)
        facts.update(total=int(physical) / (1024 * 1024), used=macos_used_memory(stats.stdout))
    return facts


def queue_events(directory: Path) -> list[dict]:
    """The grants, releases and reaps at the end of queue.log, oldest first. Each event carries `delta`, the change
    it made to held slots. A release whose grant precedes the part read is taken to end a held lease; one whose
    pid was never granted in a fully read log is a waiter that gave up."""
    try:
        with open(directory / "queue.log", "rb") as handle:
            start = max(0, handle.seek(0, os.SEEK_END) - LOG_TAIL)
            handle.seek(start)
            lines = handle.read().decode("utf-8", "replace").splitlines()[1 if start else 0:]
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    granted: dict[int, str] = {}
    for line in lines:
        moment, _, message = line.partition(" ")
        when = parse_utc(moment)
        if when is None:
            continue
        if found := GRANTED.match(message):
            count, label, pid = int(found[1]), found[2], int(found[3])
            granted[pid] = label
            events.append({"when": when, "verb": "granted", "count": count, "label": label, "pid": pid,
                           "delta": count})
        elif found := RELEASED.match(message):
            count, pid = int(found[1]), int(found[2])
            held = pid in granted or start > 0
            events.append({"when": when, "verb": "released" if held else "left", "count": count,
                           "label": granted.pop(pid, ""), "pid": pid, "delta": -count if held else 0,
                           # granted before the part read, or never: describe_events() asks its history line
                           "unsure": held and pid not in granted})
        elif found := REAPED.match(message):
            state, count, label, pid = found[1], int(found[2]), found[3], int(found[4])
            granted.pop(pid, None)
            events.append({"when": when, "verb": "reaped", "count": count, "label": label, "pid": pid,
                           "delta": -count if state == "held" else 0, "state": state})
        elif found := REPRIORITIZED.match(message):  # slotfather reprioritize: a class, not slots
            events.append({"when": when, "verb": "priority", "count": 0, "label": found[1], "pid": int(found[2]),
                           "delta": 0, "detail": f"{found[3]} → {found[4]}"})
        elif found := WAITING_LONG.match(message):  # a long wait, said by the waiter itself
            events.append({"when": when, "verb": "waiting", "count": 0, "label": found[1], "pid": int(found[2]),
                           "delta": 0, "detail": f"{found[3]}, est. {found[4]}"})
    return events


EVENT_HISTORY = 600  # history lines read to describe the events shown
EVENT_MATCH = 2.0  # seconds between a queue.log line and the history stamp of the same moment


def describe_events(events: list[dict[str, Any]], directory: Path, live: list[dict[str, Any]]) -> None:
    """Adds each event's `detail` from the history line of its request (state.record() writes it as the request
    leaves): `waited 3m00s` for a grant (a running lease's from its queue entry), `exit 1, ran 4m00s` for a release,
    `gave up after 15m00s` for a waiter that left, `owner exited, ran 5m55s` for a reap. The history line also
    settles whether a release was a lease ending or a waiter giving up, which queue.log alone cannot say once it is
    longer than the part queue_events() reads; a waiter that gave up changed no held slots."""
    by_pid: dict[int, list[dict[str, Any]]] = {}
    history = cast(list[dict[str, Any]], cast(Any, _state).tail_objects(directory / "history.jsonl", EVENT_HISTORY))
    for item in history:
        pid = item.get("pid")
        if isinstance(pid, int):
            by_pid.setdefault(pid, []).append(item)
    running = {entry["pid"]: entry for entry in live if isinstance(entry.get("pid"), int)}

    def near(stamp: Any, when: datetime.datetime) -> bool:
        moment = parse_utc(stamp)
        return moment is not None and abs((moment - when).total_seconds()) <= EVENT_MATCH

    for event in events:
        if event["verb"] in ("priority", "waiting"):  # their detail is their own line's
            continue
        event["detail"] = ""
        verb, when = event["verb"], event["when"]
        if verb == "granted":
            item = next((i for i in by_pid.get(event["pid"], []) if near(i.get("granted"), when)), None)
            if item is None and event["pid"] in running and near(running[event["pid"]].get("granted"), when):
                since, granted = parse_utc(running[event["pid"]].get("since")), when
                waited = None if since is None else (granted - since).total_seconds()
            else:
                waited = item.get("waited") if item else None
            if isinstance(waited, (int, float)):
                event["detail"] = f"waited {span(waited)}" if waited >= 1 else "no wait"
            continue
        item = next((i for i in by_pid.get(event["pid"], []) if near(i.get("ended"), when)), None)
        if item is None and event.get("unsure"):
            # No grant in the part of queue.log read and no history line that it held slots: a request that left
            # without one (queue.log has its release, never a grant). Counting it as a lease that ended put slots
            # back into the chart's past that nothing held (`peak 9` of 8).
            event.update(verb="left", delta=0, detail="never granted")
            continue
        if item is None:
            event["detail"] = "owner exited" if verb == "reaped" else "gave up" if verb == "left" else ""
            continue
        ran = item.get("ran")
        ran_text = f"ran {span(ran)}" if isinstance(ran, (int, float)) else ""
        if verb == "reaped":
            event["detail"] = ", ".join(part for part in ("owner exited", ran_text) if part)
        elif item.get("outcome") == "left":
            event.update(verb="left", delta=0, label=event["label"] or str(item.get("label") or ""))
            waited = item.get("waited")
            event["detail"] = f"gave up after {span(waited)}" if isinstance(waited, (int, float)) else "gave up"
        else:
            status = item.get("status")
            event["detail"] = ", ".join(part for part in (
                f"exit {status}" if isinstance(status, int) else "", ran_text) if part)
            event["label"] = event["label"] or str(item.get("label") or "")


def held_history(events: list[dict], held_now: int, now_moment: datetime.datetime,
                 window: float = HISTORY_WINDOW * 2) -> list[tuple[datetime.datetime, datetime.datetime, int]]:
    """Held slots over the last `window` seconds as (start, end, slots) steps, walked back from held_now."""
    begin = now_moment - datetime.timedelta(seconds=window)
    steps, level, end = [], held_now, now_moment
    for event in reversed(events):
        if event["when"] <= begin:
            break
        if event["delta"] and event["when"] < end:
            steps.append((event["when"], end, max(0, level)))
            end = event["when"]
        level -= event["delta"]
    steps.append((begin, end, max(0, level)))
    return steps[::-1]


def history_buckets(steps: list, columns: int) -> list[int]:
    """Peak slots per fixed wall-clock bucket; only the newest bucket is incomplete.

    Anchoring buckets to the clock keeps old peaks stable between scroll steps.
    """
    if not steps or columns <= 0:
        return []
    begin, end = steps[0][0], steps[-1][1]
    seconds = max(1, math.ceil(HISTORY_WINDOW / columns))
    edge = math.ceil(end.timestamp() / seconds) * seconds
    end = datetime.datetime.fromtimestamp(edge, end.tzinfo)
    width = datetime.timedelta(seconds=seconds)
    begin = end - width * columns
    buckets = []
    for column in range(columns):
        low, high = begin + width * column, begin + width * (column + 1)
        buckets.append(max((level for start, stop, level in steps if start < high and stop > low), default=0))
    return buckets


def history_summary(steps: list, slots: int) -> dict:
    # Retain extra history for whole clock buckets, but summarise only the trailing hour.
    if steps:
        begin = steps[-1][1] - datetime.timedelta(seconds=HISTORY_WINDOW)
        steps = [(max(start, begin), stop, level) for start, stop, level in steps if stop > begin]
    total_seconds = sum((stop - start).total_seconds() for start, stop, _ in steps) or 1
    held = sum((stop - start).total_seconds() * min(level, slots) for start, stop, level in steps)
    full = sum((stop - start).total_seconds() for start, stop, level in steps if level >= slots)
    return {"average": held / total_seconds / slots, "full": full / total_seconds,
            "peak": max((level for _, _, level in steps), default=0)}


def dashboard_admission(waiters: list[dict[str, Any]], held: list[dict[str, Any]], slots: int, other: int,
                        memory_block: str | None, estimate: _schedule.Estimate | None, settings: dict[str, Any]) -> int:
    """Largest generic request grant() would admit, including earlier grants consuming the memory exemption."""
    used = sum(_schedule.holds(entry, slots) for entry in held) + other
    free = max(0, slots - used)
    if not free or memory_block and used:
        return 0
    if not waiters:
        return free
    if memory_block:
        # On an idle host only the first planned grant bypasses the memory gate.
        # A new generic request may rank ahead of a lower-priority existing waiter.
        probe: dict[str, Any] = dict(count=free, label="", cmd="", since=_config.now())
        found = _schedule.plan(waiters + [probe], held, slots, time.time(), estimate=estimate,
                        other=other, settings=settings)
        return free if found.grants and found.grants[0] is probe else 0
    return _schedule.room(waiters, free, held, estimate, other, settings=settings, slots=slots)


def dashboard_model() -> dict:
    directory = _config.state_dir()
    live, legacy, _, basis = _query.locked_snapshot(directory)  # the capacity under the queue's lock
    slots: int = basis["slots"]
    saved: bool = basis["saved"]
    own, children = process_usage()
    asks: dict[int, tuple[str, datetime.datetime | None]] = {}  # holder pid -> (waiter that asked it to yield, when)
    for place in (directory / "queue").glob("*.yield"):
        with contextlib.suppress(OSError, ValueError, AttributeError):
            request = json.loads(place.read_text())
            asks[int(place.stem.split("-")[-1])] = (str(request.get("label") or "?"), parse_utc(request.get("at")))

    def item(entry: dict[str, Any], kind: str, count: int, queue: str = "") -> dict[str, Any]:
        tree = process_tree(entry.get("pid"), own, children) if kind != "waiting" else []
        foreseen: dict[str, Any] = expected.get(id(entry)) or {}  # query.outlook(): its class and expected start
        return {"tree": sorted(tree, key=lambda process: -process[1]), "cpu": sum(p[1] for p in tree),
                "rss": sum(p[2] for p in tree), "procs": len(tree),"key": (entry.get("pid"), entry.get("since")), "kind": kind, "count": count, "queue": queue,
                "label": dashboard_text(entry.get("label", "")), "pid": entry.get("pid"),
                "cmd": dashboard_text(entry.get("cmd", "")), "since": parse_utc(entry.get("since")),
                "meta": dashboard_text(_config.meta_text(_config.meta_of(entry))),
                "context": _config.meta_of(entry), "workdir": entry.get("cwd") or "",
                "priority": _config.priority_of(entry), "ticket": _config.ticket_of(entry),
                "yieldable": bool(entry.get("yieldable")), "yields": int(entry.get("yields") or 0),
                "asked": dashboard_text(asks.get(int(entry.get("pid") or 0), ("", None))[0]),
                "asked_at": asks.get(int(entry.get("pid") or 0), ("", None))[1],
                # #12: since when the holder's record says the lease has been idle (query.locked_snapshot()).
                "idle_since": datetime.datetime.fromtimestamp(entry["idle_since"], datetime.timezone.utc)
                if isinstance(entry.get("idle_since"), (int, float)) else None,
                "served": foreseen.get("priority") or
                (entry.get("aged") if entry.get("aged") in _config.PRIORITIES else None),
                "reprioritized": entry.get("reprioritized") if entry.get("reprioritized") in _config.PRIORITIES else None,
                "est_start": datetime.datetime.fromtimestamp(foreseen["est_start"], datetime.timezone.utc)
                if foreseen.get("est_start") is not None else None,
                "granted": parse_utc(entry.get("granted")), "passable": kind == "waiting" and _schedule.overtakable(entry),
                "all": bool(entry.get("all")),
                "style": "text" if kind == "waiting" else "legacy"}

    # The class each waiter is served in and its expected start (query.outlook(); nothing when it cannot be read).
    expected: dict[int, dict[str, Any]] = {}
    with contextlib.suppress(Exception):
        expected = _query.outlook(directory, live, slots, sum(count for _, _, count in legacy))
    # An `-n all` lease holds the whole capacity, also one grown under it (#7).
    holders = [item(e, "held", _schedule.holds(e, slots)) for e in live if e["state"] == "held"]
    holders += [item(e, "legacy", count, str(path.parent.parent)) for path, e, count in legacy]
    settings, start, reserved = _schedule.order_settings(directory), None, None
    order = settings["order"]
    live_waiters = [e for e in live if e["state"] != "held"]
    _schedule.fit(live_waiters, slots)  # as the queue grants them when the capacity shrank under them
    if order == "backfill":
        live_waiters = _schedule.ranked(live_waiters, settings, time.time())  # the order the queue serves them in
    waiters = [item(e, "waiting", e["count"]) for e in live_waiters]
    used = sum(h["count"] for h in holders)
    if order == "backfill" and live_waiters:
        with contextlib.suppress(Exception):  # an estimate that cannot be read leaves the sentence without a time
            found = _schedule.plan(live_waiters, [e for e in live if e["state"] == "held"], slots, time.time(), order,
                            _schedule.estimator(directory), sum(count for _, _, count in legacy), reserve_head=True,
                            settings=settings)
            if found.head is not None and found.start is not None:
                start = datetime.datetime.fromtimestamp(found.start, datetime.timezone.utc)
            if found.head is not None and found.reserved is not None:
                reserved = datetime.datetime.fromtimestamp(found.reserved, datetime.timezone.utc)
    memory_block, machine, averages = _platform.memory_short(), _config.cores(), _platform.loads()
    events = queue_events(directory)
    describe_events(cast(list[dict[str, Any]], events), directory, live)
    moment = datetime.datetime.now(datetime.timezone.utc)
    free = max(0, slots - used)
    admission = dashboard_admission(live_waiters, [e for e in live if e["state"] == "held"], slots,
                    sum(count for _, _, count in legacy), memory_block,
                    _schedule.estimator(directory) if live_waiters else None, settings)
    own_held = sum(h["count"] for h in holders if h["kind"] == "held")
    return {"directory": str(directory), "slots": slots, "saved": saved, "basis": basis, "cores": machine,
            "averages": averages, "draining": _query.draining(used, slots),
            "advice": _query.size(slots, machine, slots, averages, min(used, slots),
                              bool(waiters) and admission == 0),
            "holders": holders, "waiters": waiters, "used": used, "free": free, "memory_block": memory_block,
            "admission": admission,
            "order": order, "start": start, "reserved": reserved, "limit": settings["limit"],
            "memory": memory_facts(), "history": held_history(events, own_held, moment), "events": events[-60:],
            "updated": moment}


def dashboard_colours(model: dict, assigned: dict) -> None:
    """Gives each running lease a colour it keeps until it ends, preferring colours no other lease shows, so the
    slot bar, the table swatch and the detail heading stay linked as the queue moves."""
    keys = {h["key"] for h in model["holders"]}
    for key in [key for key in assigned if key not in keys]:
        del assigned[key]
    count = current_theme.holders()  # 8 holder colours less any the theme reserves, or the builtin 6 (#14)
    for holder in model["holders"]:
        if holder["kind"] != "held":
            continue
        if holder["key"] not in assigned:
            taken = [index % count for index in cast("dict[Any, int]", assigned).values()]
            assigned[holder["key"]] = min(range(count), key=lambda n: (taken.count(n), n))
        holder["style"] = f"h{assigned[holder['key']] % count}"


def fill(style: str) -> str:
    """A holder's fill token (`f3`) for its ink token (`h3`): what paints blocks, cells, bars and the selection marker;
    any other style is its own fill."""
    token = "f" + style[1:]
    return token if style[:1] == "h" and style[1:].isdigit() and token in current_theme.styles else style


# Dashboard layout

def bar(fraction: float, width: int, style: str) -> list[tuple[str, str]]:
    """Compact squares leave vertical breathing room between adjacent bar rows."""
    width = max(0, width)
    full = max(0, min(width, math.floor(fraction * width + 0.5)))
    return [("■" * full, style), (current_theme.track * (width - full), "track")]


def ramp_bar(fraction: float, width: int, critical: bool = False) -> list[tuple[str, str]]:
    """A MACHINE resource bar: each filled cell in the load chart's indigo-to-red ramp at its place along the bar, so
    a nearly full bar reaches red at its tip; a critical one (🔥) entirely in that red, so full and critical match."""
    width = max(0, width)
    full = max(0, min(width, math.floor(fraction * width + 0.5)))
    parts: list[tuple[str, str]] = []
    for cell in range(full):
        step = round(cell / (width - 1) * (_theme.CHART_STEPS - 1)) if width > 1 else _theme.CHART_STEPS - 1
        style = f"chart{_theme.CHART_STEPS - 1}" if critical else f"chart{step}"  # all the ramp's red: full = critical
        if parts and parts[-1][1] == style:
            parts[-1] = (parts[-1][0] + "■", style)
        else:
            parts.append(("■", style))
    return parts + [(current_theme.track * (width - full), "track")]


def share(item: dict, model: dict) -> tuple[float, float]:
    """(cores the lease's commands use, cores its slots stand for)."""
    return item["cpu"] / 100, item["count"] * model["cores"] / model["slots"]


def share_style(item: dict, model: dict, moment: datetime.datetime) -> str:
    used, allotted = share(item, model)
    if item.get("idle_since") is not None:
        return "warn"  # an idle lease (#12; the holder's record, idle.idle_since()): holds slots doing nothing
    return "bad" if used > 1.5 * allotted else "accent"


def queue_bar_style(item: dict[str, Any], model: dict[str, Any], moment: datetime.datetime) -> str:
    """A QUEUE row's CPU bar fills in the holder's fill (its SLOTS colour), except when it warns: red over 1.5 times
    its share, yellow while the lease is idle (#12, share_style())."""
    style = share_style(item, model, moment)
    return fill(str(item["style"])) if style == "accent" else style


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
    for index, (key, label, column, size, right) in enumerate(cast(list[tuple[str, str, int, int, bool]], placed)):
        value = title if index == 0 else label.lower()
        # The title sits where every panel's does (x + 3); the other headers over their columns, `gutter` in
        start = x + 3 if index == 0 else x + 3 + gutter + column + (max(0, size - text_width(value)) if right else 0)
        canvas.put(y, start - 1, " ", "text")
        focused = bool(name) and canvas.focus == name  # a focused panel's labels in the foreground, its frame blue
        canvas.put(y, start, value, "title" if index == 0 else "text" if focused else "dim", size)
        canvas.put(y, min(x + width - 2, start + min(size, text_width(value))), " ", "text")


def draw_header(canvas: Canvas, model: dict | None, error: str | None) -> tuple[int, int]:
    """The name on the left, the last update and live or stale on the right; (first free column, last free one)."""
    y = canvas.height - 1
    x = canvas.put(y, 1, "Slotfather", "title", max(0, canvas.width - 2))
    status, style = ("● stale", "bad") if error else ("● live", "ok")
    room = canvas.width - 1 - x - 2
    right = canvas.width - 1
    if text_width(status) <= room:
        right = canvas.right(y, right, status, style)
        if model:
            stamp = clock(model["updated"])
            if text_width("updated " + stamp) + text_width(status) + 2 <= room:
                stamp = "updated " + stamp
            if text_width(stamp) + text_width(status) + 2 <= room:
                right = canvas.right(y, right - 2, stamp, "dim")
    return x + 3, right - 2


def draw_slots(canvas: Canvas, y: int, x: int, width: int, model: dict) -> None:
    """Solid slots spread across the widget, with two-cell gaps and right-aligned free label."""
    slots = model["slots"]
    area = max(0, width)
    gap = 2 if area >= slots + 2 * (slots - 1) else 1 if area >= 2 * slots - 1 else 0
    cell, remainder = divmod(max(0, area - gap * (slots - 1)), slots)
    widths = [cell + (index < remainder) for index in range(slots)]
    starts, cursor = [], x
    for size in widths:
        starts.append(cursor)
        cursor += size + gap
    labels, filled = [], 0
    for holder in model["holders"]:
        count = min(holder["count"], slots - filled)
        if count <= 0:
            break
        labels.append((starts[filled], holder["label"], holder["style"]))
        for index in range(filled, filled + count):
            canvas.put(y, starts[index], "🮅" * widths[index], fill(str(holder["style"])))
        filled += count
    for index in range(filled, slots):
        canvas.put(y, starts[index], free_slot(widths[index]), "free")
    label_end = x + area
    if filled < slots and area >= 4:
        free_start = x + area - 4
        canvas.put(y + 1, free_start, "free", "dim")
        label_end = free_start - 2
    for index, (label_x, label, style) in enumerate(labels):
        limit = labels[index + 1][0] - 2 if index + 1 < len(labels) else label_end
        canvas.put(y + 1, label_x, label, style, max(0, limit - label_x))


def free_slot(width: int) -> str:
    """A free slot `width` cells wide: a block in the theme's free colour, or outlined where the theme has no surface
    colours (#14)."""
    if current_theme.free == "block" or width < 1:
        return "🮅" * width
    return "▭" if width == 1 else "╭" + "─" * (width - 2) + "╮"


SLOT_CELLS = 8  # most cells the QUEUE table's slots column shows before it counts the rest
# How a slot cell is drawn: (glyph, gap between cells). Each cell must stay countable at a glance, never run into a
# bar: "spaced" puts a space between full squares, "small" uses a small square whose side bearing separates it.
SLOT_CELL_FORMS = {"spaced": ("■", " "), "small": ("▪", "")}
SLOT_CELL = "small"  # the owner's pick (2026-11-11)
HOLLOW = {"■": "□", "▪": "▫"}  # slots handed back or never held: the same size, empty (U+25AB is narrow like U+25AA)


def cells_text(count: int, hollow: bool = False) -> str:
    glyph, gap = SLOT_CELL_FORMS[SLOT_CELL]
    return gap.join((HOLLOW[glyph] if hollow else glyph) * count)


def cell_run(count: int, size: int, style: str, hollow: bool = False, whole: bool = False,
             text: str = "") -> list[tuple[str, str]]:
    """`count` countable cells in `style` within `size` columns and at most SLOT_CELLS cells. A count wider than
    that keeps the cells that fit
    and counts the rest (`▪▪▪▪ +16` for 20 in 8 columns); a `whole` (-n all) one that does not fit says `all`. The
    words are in `text` (a holder's ink), else in `style`."""
    text = text or style
    count = max(0, count)
    size = min(size, text_width(cells_text(SLOT_CELLS)))  # more than SLOT_CELLS cells cannot be counted at a glance
    if text_width(cells_text(count, hollow)) <= size:
        return [(cells_text(count, hollow), style)]
    if whole:
        return [("all", text)]
    shown = min(count, size)  # no more cells than the column has cells: at most `size` tries
    while shown and text_width(cells_text(shown, hollow)) + len(f" +{count - shown}") > size:
        shown -= 1
    return [(cells_text(shown, hollow), style), (f" +{count - shown}", text)]


def slot_cells(item: dict[str, Any], size: int) -> list[tuple[str, str]]:
    """A QUEUE row's slots as countable cells in the row's colour (the fill of its SLOTS blocks for a lease; the
    plain text colour for a waiting request, whose state the cpu column gives); see cell_run()."""
    style = "text" if item["kind"] == "waiting" else str(item["style"])
    return cell_run(int(item["count"]), size, fill(style), whole=bool(item.get("all")), text=style)


def event_cells(event: dict[str, Any], size: int, style: str) -> list[tuple[str, str]]:
    """An EVENTS row's slots in its event colour: filled for slots taken (granted), hollow for slots handed back
    (released, a held lease reaped) or never held (a waiter that left or was reaped)."""
    return cell_run(int(event["count"]), size, style, hollow=event["verb"] != "granted")


QUEUE_LABEL_MAX = 40  # the most cells a label asks for when the table is wide enough to share out its width
CPU_BAR = 5  # the CPU bar's base length; a wide table lets it grow to twice that


class QueueLayout(NamedTuple):
    placed: list[tuple[str, str, int, int, bool]]  # (key, title, x, width, right-aligned), as columns() gives
    natural: dict[str, int]  # each column's own width: its widest value (a label at most QUEUE_LABEL_MAX)
    use_width: int  # the CPU numbers: `use` right-aligned to this, then `/`, then `share` left-aligned to share_width
    share_width: int
    bar: int  # the CPU bar's length


def queue_layout(inner: int, model: dict[str, Any]) -> QueueLayout:
    """The QUEUE table's columns in `inner` cells. Each column has a natural width (its widest value). A table wider
    than the natural widths and their gaps shares the surplus out in proportion to them, so it fills the panel
    evenly, and the CPU bar grows with its column up to twice CPU_BAR; a narrower one keeps the label flexible: it
    shrinks first, then pid, time and cpu use/share drop in that order."""
    holders = cast(list[dict[str, Any]], model["holders"])
    waiters = cast(list[dict[str, Any]], model["waiters"])
    items = holders + waiters
    shares = [share(item, model) for item in holders]
    use_width = max([3] + [len(f"{used:.1f}") for used, _ in shares])
    share_width = max([1] + [len(short(allotted)) for _, allotted in shares])
    natural = {
        "label": max(10, min(QUEUE_LABEL_MAX, max([0] + [text_width(str(item["label"])) for item in items]))),
        "slots": max([5] + [text_width(cells_text(min(SLOT_CELLS, int(item["count"])))) for item in items]),
        "cpu": max(len("cpu use/share"), CPU_BAR + 1 + use_width + 1 + share_width,
                   max([0] + [len(f"waiting #{n}") for n in range(1, len(waiters) + 1)])),
        "time": 8, "pid": max([6] + [len(str(item["pid"])) for item in items])}
    spec = [("label", "label", False), ("slots", "slots", False), ("cpu", "cpu use/share", False),
            ("time", "time", True), ("pid", "pid", True)]
    gaps = 2 * (len(spec) - 1)
    surplus = inner - sum(natural.values()) - gaps
    if surplus <= 0:  # the label shrinks first, then columns drop: pid, time, cpu use/share
        placed = cast(list[tuple[str, str, int, int, bool]], columns(inner, [
            (key, title, 0 if key == "label" else natural[key], right, rank)
            for rank, (key, title, right) in enumerate(spec)]))
        return QueueLayout(placed, natural, use_width, share_width, CPU_BAR)
    total = sum(natural.values())
    sizes = {key: natural[key] + surplus * natural[key] // total for key in natural}
    left = inner - gaps - sum(sizes.values())  # what rounding down left over: to the largest remainders
    for key in sorted(natural, key=lambda key: -(surplus * natural[key] % total))[:left]:
        sizes[key] += 1
    placed: list[tuple[str, str, int, int, bool]] = []
    column = 0
    for key, title, right in spec:
        placed.append((key, title, column, sizes[key], right))
        column += sizes[key] + 2
    bar_length = min(2 * CPU_BAR, CPU_BAR + sizes["cpu"] - natural["cpu"])
    return QueueLayout(placed, natural, use_width, share_width, bar_length)


# A QUEUE row's state (#15), the first column of every row: (emoji, ASCII). A running row shows the first of asked,
# idle, hot and legacy that applies, else running; a waiting row yielded or waiting.
MARKS = {"asked": ("❓", "?"), "idle": ("💤", "z"), "hot": ("🔥", "!"), "legacy": ("👨‍🦳", "o"), "running": ("▶️", ">"),
         "yielded": ("↩️", "<"), "waiting": ("⏸️", ".")}
OLD_FALLBACK = "👴"  # for a terminal that draws 👨‍🦳 as two glyphs (theme.ask() measures it)
marks = {key: emoji for key, (emoji, _) in MARKS.items()}  # what this terminal is drawn with
# A request's priority (owner, 2026-11-11): its own column after the state; normal has none, so the column is blank
PRIORITY_MARKS = {"interactive": ("⚡", "!"), "batch": ("🐢", "~")}
priority_marks = {key: emoji for key, (emoji, _) in PRIORITY_MARKS.items()}
# A MACHINE resource's health (owner, 2026-11-12): ok, or critical by the rules that turn its bar red
HEALTH_MARKS = {"ok": ("🆗", "+"), "hot": ("🔥", "!")}
health_marks = {key: emoji for key, (emoji, _) in HEALTH_MARKS.items()}


def use_marks(ascii_only: bool, joined: bool = True) -> None:
    """The QUEUE state column for this terminal: ASCII where it has no UTF-8, 👴 for 👨‍🦳 where it does not join."""
    marks.update({key: plain if ascii_only else emoji for key, (emoji, plain) in MARKS.items()})
    priority_marks.update({key: plain if ascii_only else emoji for key, (emoji, plain) in PRIORITY_MARKS.items()})
    health_marks.update({key: plain if ascii_only else emoji for key, (emoji, plain) in HEALTH_MARKS.items()})
    if not ascii_only and not joined:
        marks["legacy"] = OLD_FALLBACK


def priority_mark(item: dict[str, Any]) -> str:
    """⚡ for an interactive request, 🐢 for a batch one (! and ~ in ASCII), nothing for normal: the class it is
    served in, aged (`served`), else the one it asked."""
    return priority_marks.get(_config.priority_of({"priority": item.get("served") or item.get("priority")}), "")


def row_state(item: dict[str, Any], model: dict[str, Any]) -> str:
    """The MARKS key of a QUEUE row: `asked` (a waiter asked this lease for its slots, the actionable state) over
    `idle` (#12) over `hot` (above 1.5 times its share, as its red bar) over `legacy` (an older client's lease)."""
    if item["kind"] == "waiting":
        return "yielded" if item.get("yields") else "waiting"
    if item.get("asked"):
        return "asked"
    if item.get("idle_since") is not None:
        return "idle"
    used, allotted = share(item, model)
    if used > 1.5 * allotted:
        return "hot"
    return "legacy" if item["kind"] == "legacy" else "running"


SELECTED_FRAME = "┃"  # the left frame beside a selected row, heavy in the row's colour (#15); # on ASCII
# A scrollbar's thumb (#15): a whole cell, a cell's lower half (where it starts), its upper half (where it ends);
# # on an ASCII terminal
SCROLL_THUMB, THUMB_LOWER, THUMB_UPPER = "┃", "╻", "╹"


def draw_requests(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict, selected: int,
               moment: datetime.datetime) -> None:
    holders = cast(list[dict[str, Any]], model["holders"])
    waiters = cast(list[dict[str, Any]], model["waiters"])
    items = holders + waiters
    row_items = list(range(len(holders)))
    if holders and waiters:
        row_items.append(None)  # visual separator, never a selectable request
    row_items.extend(range(len(holders), len(items)))
    # cpu use/share: the bar starts at the column's left edge on every row, then `use` right-aligned and `/share`
    # left-aligned around the slash, padded to the widest on screen; a waiting row's state starts where the bars do.
    layout = queue_layout(width - 12, cast(dict[str, Any], model))  # the state and priority: two cells and a gap each
    placed, cells = layout.placed, layout.natural["slots"]
    shares = {index: share(item, model) for index, item in enumerate(holders)}
    use_width, share_width = layout.use_width, layout.share_width
    size = next((column[3] for column in placed if column[0] == "slots"), cells)
    table_header(canvas, y, x, height, width, "QUEUE", placed, gutter=6, name="requests")
    if not items:
        canvas.put(y + 2, x + 3, f"No requests · all {model['slots']} slots free", "dim", width - 6)
    visual_selected = row_items.index(selected) if selected in row_items else None
    offset, rows = table_view(canvas, "requests", y, x, height, width, y + 2, height - 4,
                              len(row_items), visual_selected, row_items=row_items)
    for visual, index in enumerate(row_items[offset:offset + rows], offset):
        if index is None:
            continue
        item = items[index]
        row = y + 2 + visual - offset
        waiting = index >= len(holders)
        # One colour per row: a running lease's SLOTS colour (its CPU bar too, but for a warning), the text colour for a
        # request that holds no slots.
        style = "text" if waiting else str(item["style"])
        # A selected running row keeps its colour and gets its own tint behind it (#15)
        tinted = index == selected and style in current_theme.tints
        ink = style
        if waiting:
            place = index - len(holders) + 1
            state = f"yielded #{place}" if item.get("yields") else f"waiting #{place}"  # back in the queue to resume
            age = (moment - item["since"]).total_seconds() if item["since"] else None
            cpu: list[tuple[str, str]] = [(state, style)]
        else:
            age = (moment - item["granted"]).total_seconds() if item["granted"] else None
            used, allotted = shares[index]
            cpu = bar(used / allotted if allotted else 0, layout.bar, queue_bar_style(item, cast(dict[str, Any], model), moment)) + [
                (f" {used:.1f}".rjust(use_width + 1) + "/" + short(allotted).ljust(share_width), ink)]
        # The state is the row's first content, where every panel's starts (x + 3); the label after it (#15)
        canvas.put(row, x + 3, marks[row_state(item, cast(dict[str, Any], model))], style, 2)
        canvas.put(row, x + 6, priority_mark(item), style, 2)  # blank for normal: the labels stay in one column
        draw_row(canvas, row, x + 9, placed, {
            "label": (str(item["label"]), ink), "slots": slot_cells(item, size),
            "time": (span(age) if age is not None else "—", ink), "cpu": cpu, "pid": (str(item["pid"]), ink)})
        if index == selected:  # its background, and the left frame heavy in the row's colour: no cursor glyph
            canvas.select(row, x + 1, x + width - 1, style if tinted else True)
            for frame in (x, x + width - 1):  # both frames, at that row; the thumb is in the padding cell (#15)
                canvas.put(row, frame, SELECTED_FRAME, fill(style))


def queue_policy(model: dict) -> dict[str, str]:
    """Admission estimates and deadlines for the head, exposed as table fields."""
    if not model["waiters"]:
        return {}
    head = model["waiters"][0]
    if head["count"] <= model["free"]:
        return {}
    fields = {}
    if model["free"] and head.get("passable") and head.get("since"):
        fields["pass until"] = clock(head["since"] + datetime.timedelta(seconds=_config.overtake_window()))
    if model.get("order") == "backfill" and model.get("start") is not None and head.get("since"):
        fields["start est"] = clock(model["start"])
        promised: Any = cast(Any, model).get("reserved")  # the start it is promised: nothing that passes it ends later
        if promised is not None:
            fields["reserved"] = clock(promised)
        if model.get("limit"):
            fields["pass until"] = clock(head["since"] + datetime.timedelta(seconds=model["limit"]))
    return fields



def detail_ticket(item: dict[str, Any], lane: dict[str, Any], context: dict[str, str]) -> tuple[str, str]:
    source: dict[str, Any] = {}
    if item.get("ticket") is not None:
        source["ticket"] = item["ticket"]
    ticket: dict[str, Any] = lane.get("ticket") or {}
    value = _config.ticket_of(source) or _config.ticket_of({"ticket": ticket.get("number")})
    if not value:
        return "", ""
    url = value if value.startswith("https://") and safe_link(value) else ""
    repository = str(lane.get("repository") or "")
    if value.isdigit() and repository.startswith("https://") and safe_link(repository):
        url = repository.rstrip("/") + "/issues/" + value
    elif value == str(ticket.get("number")):
        candidate = str(ticket.get("url") or "")
        if candidate.startswith("https://") and safe_link(candidate):
            url = candidate
    return ("#" + value if value.isdigit() else value), url


def detail_title(item: dict[str, Any] | None) -> str:
    return "LEASE DETAILS" if item is not None and item["kind"] != "waiting" else "REQUEST DETAILS"


def draw_detail(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict, selected: int,
                cwd: str | None, moment: datetime.datetime) -> None:
    items = cast(list[dict[str, Any]], model["holders"] + model["waiters"])
    # The title follows the selection: a request whose lease holds slots has LEASE DETAILS; a waiting or yielded
    # one, holding none, REQUEST DETAILS.
    canvas.box(y, x, height, width, detail_title(items[selected] if items else None), name="details")
    inner = width - 6
    if not items or canvas.loading is not None:  # drawn, so it can hold the focus, with nothing to scroll yet
        table_view(canvas, "details", y, x, height, width, y + 2, max(0, height - 4), 0)
    if not items:
        canvas.put(y + 2, x + 3, "Nothing to select · the queue is idle", "dim", inner)
        return
    if canvas.loading is not None:  # loading in the background (#15): a spinner, centred, until they arrive
        middle = y + max(1, (height - 2) // 2)
        glyph = SPINNER[canvas.loading % len(SPINNER)]
        canvas.put(middle, x + (width - 1) // 2, glyph, "dim")
        if middle + 1 < y + height - 1:
            canvas.put(middle + 1, x + (width - len("loading")) // 2, "loading", "dim")
        return
    item = items[selected]
    lane: dict[str, Any] = item.get("lane") or {}
    context: dict[str, str] = item.get("context") or {}
    ticket_text, ticket_url = detail_ticket(item, lane, context)
    if item["kind"] == "waiting":
        position = selected - len(model["holders"])
        heading = f"waiting · #{position + 1} · wants {item['count']}"
    else:
        heading = ("earlier queue" if item["kind"] == "legacy" else "running") + f" · {item['count']} slot{'s' if item['count'] != 1 else ''}"
    facts: list[tuple[str, list[tuple[str, str]]]] = [("label", [(item["label"] or "(no label)", "bold")]), ("status", [(heading, "text")])]
    priority = _config.priority_of({"priority": item.get("served") or item.get("priority")})
    mark = priority_mark(item)
    asked = _config.priority_of({"priority": item.get("priority")})
    how = f" · aged from {asked}" if priority != asked else ""
    how += f" · reprioritized from {item['reprioritized']}" if item.get("reprioritized") else ""
    facts.append(("priority", ([(mark, "text"), (" ", "text")] if mark else []) + [(priority, "text"),
                                                                                 (how, "dim")]))
    if ticket_text:
        facts.append(("ticket", [(ticket_text, "accent" if ticket_url else "text")]))
    for key, label in (("phase", "phase"), ("kind", "kind"), ("check", "check"), ("sha", "revision"),
                       ("on", "run on"), ("session", "session")):
        if context.get(key):
            facts.append((label, [(dashboard_text(context[key]), "dim" if key in ("sha", "session") else "text")]))
    if item["kind"] == "waiting":
        facts.append(("queued", [(f"{clock(item['since'])} · waiting "
                                  f"{span((moment - item['since']).total_seconds()) if item['since'] else '?'}", "text")]))
        if position == 0:
            facts.append(("free", [(f"{model['free']} of {model['slots']} slots", "text")]))
            facts.extend((name, [(value, "text")]) for name, value in queue_policy(model).items())
            if model["used"] and model["memory_block"]:
                facts.append(("blocked", [(model["memory_block"], "warn")]))
        else:
            facts.append(("ahead", [(f"{position} requests · {sum(w['count'] for w in model['waiters'][:position])} slots", "text")]))
            if item.get("est_start") is not None:  # schedule.forecast(): if nothing more comes
                facts.append(("start est", [(clock(item["est_start"]), "text")]))
    else:
        used, allotted = share(item, model)
        style = share_style(item, model, moment)
        note = " · above its share" if style == "bad" else ""
        facts.append(("cpu", bar(used / allotted if allotted else 0, 10, style) +
                      [(f" {used:.1f} of {short(allotted)} cores{note}", "text")]))
        if item["granted"]:
            waited = f" · waited {span((item['granted'] - item['since']).total_seconds())}" if item["since"] else ""
            facts.append(("granted", [(f"{clock(item['granted'])}{waited} · running "
                                       f"{span((moment - item['granted']).total_seconds())}", "text")]))
        facts.append(("memory", [(f"{megabytes(item['rss'] / 1024)} in {item['procs']} processes", "text")]))
    if item["kind"] != "waiting" and item.get("idle_since") is not None:  # #12: one definition (idle.idle_since())
        since = item["idle_since"]
        facts.append(("idle", [(f"{minutes((moment - since).total_seconds())} since "
                                f"{since.astimezone().strftime('%H:%M')}", "warn")]))
    if item.get("asked"):
        at = item.get("asked_at")
        facts.append(("yield", [(f"asked at {at.astimezone().strftime('%H:%M')} for {item['asked']}"
                                 if at else f"asked to yield for {item['asked']}", "warn")]))
    elif item.get("yieldable") or item.get("yields"):
        facts.append(("yield", [("yields between steps when asked" + (f" · yielded {item['yields']}×"
                                                                       if item.get("yields") else ""), "dim")]))
    facts.append(("owner pid", [(str(item["pid"]), "text")]))
    where = tilde(item["queue"] or cwd or "")
    label_width = max(text_width(name) for name in [*(name for name, _ in facts), "command", "meta", "cwd"]) + 2
    value_width = max(1, inner - label_width)
    if text_width(where) > value_width:
        where = "…" + where[-max(0, value_width - 1):]
    worktree_rows: set[int] = set()
    worktree_url = ""
    if lane.get("worktree"):
        folder = Path(str(lane["worktree"]))
        if folder.is_absolute():
            worktree_url = folder.as_uri()
        for index, line in enumerate(textwrap.wrap(tilde(dashboard_text(lane["worktree"])), value_width,
                                                   break_on_hyphens=False)):
            worktree_rows.add(len(facts))
            facts.append(("worktree" if index == 0 else "", [(line, "dim")]))
    blocks = [("command", item["cmd"] or "—")]
    metadata: str = item.get("meta", "")
    if context:
        builtin = {"class", "priority", "ticket", "lane", "phase", "kind", "check", "sha", "on", "session", "lanefather.state"}
        metadata = dashboard_text(_config.meta_text({key: value for key, value in context.items() if key not in builtin}))
    if metadata:
        blocks.append(("meta", metadata))
    wrapped = [textwrap.wrap(text, value_width, break_on_hyphens=False) or ["—"] for _, text in blocks]
    for (name, _), lines in zip(blocks, wrapped):
        for index, line in enumerate(lines[:3]):
            if index == 2 and len(lines) > 3:
                line = clip(line + " …", value_width)
            facts.append((name if index == 0 else "", [(line, "dim")]))
    if where:
        facts.append(("cwd", [(where, "dim")]))
    if context.get("lane"):
        facts.extend([("", []), ("lane", [(dashboard_text(context["lane"]), "bold")])])
        if lane.get("error") or not lane:
            facts.append(("details", [("unavailable" + (f" · {lane['error']}" if lane.get("error") else ""), "dim")]))
        else:
            for name in ("project", "status", "branch", "base"):
                text = dashboard_text(lane.get(name) or "—")
                for index, line in enumerate(textwrap.wrap(text, value_width, break_on_hyphens=False)):
                    label = "stage" if name == "status" else name
                    facts.append((label if index == 0 else "", [(line, "text")]))
            checks: dict[str, Any] = lane.get("checks") or {}
            check_text = str(checks.get("status") or "unknown")
            if checks.get("current") is not None:
                check_text += " · current" if checks["current"] else " · stale"
            facts.append(("receipt", [(dashboard_text(check_text), "text")]))
            review: dict[str, Any] = lane.get("review") or {}
            review_text = str(review.get("status") or "unknown")
            if review.get("findings"):
                review_text += f" · {review.get('unresolved', '?')} unresolved of {review['findings']}"
            facts.append(("review", [(dashboard_text(review_text), "text")]))
            for name in ("claims", "after"):
                text = ", ".join(dashboard_text(value) for value in lane.get(name, [])) or "—"
                for index, line in enumerate(textwrap.wrap(text, value_width)):
                    facts.append((name if index == 0 else "", [(line, "dim")]))
    content_height = len(facts) + (3 + len(item["tree"]) if item["tree"] else 0)
    document = Canvas(width, content_height)
    for row, (name, parts) in enumerate(facts):
        document.put(row, 3, name, "dim")
        if row in worktree_rows and worktree_url:
            document.hyperlink(row, 3 + label_width, str(parts[0][0]), worktree_url, value_width, "dim")
        elif name == "ticket" and ticket_url:
            document.hyperlink(row, 3 + label_width, str(parts[0][0]), ticket_url, value_width)
        else:
            document.segments(row, 3 + label_width, parts, value_width)
    if item["tree"]:
        row = len(facts)
        placed = columns(inner, [("name", "process", 0, False, 0), ("cpu", "cpu", 6, True, 1),
                                 ("rss", "memory", 8, True, 2), ("pid", "pid", 6, True, 0)])
        draw_row(document, row + 1, 3, placed, {key: (title, "dim") for key, title, *_ in placed})
        for process_row, (pid, cpu, rss, name) in enumerate(item["tree"], row + 3):
            draw_row(document, process_row, 3, placed, {"pid": str(pid), "cpu": f"{cpu:.0f}%",
                     "rss": megabytes(rss / 1024), "name": name})
    offset, visible = table_view(canvas, "details", y, x, height, width, y + 2,
                                 max(0, height - 4), content_height)
    for row in range(min(visible, content_height - offset)):
        for column in range(3, max(3, width - 3)):
            target_y, target_x = y + 2 + row, x + column
            if 0 <= target_y < canvas.height and 0 <= target_x < canvas.width:
                canvas.cells[target_y][target_x] = document.cells[offset + row][column][:]
                url = document.links.get((offset + row, column))
                canvas.links.pop((target_y, target_x), None)
                if url:
                    canvas.links[target_y, target_x] = url


def enrich_details(item: dict[str, Any]) -> None:
    context: dict[str, str] = item.get("context") or {}
    if context.get("lane"):
        item["lane"] = _lane_context.fetch(context, item.get("workdir") or "")


def machine_memory_status(memory: dict) -> tuple[str, str]:
    """RAM/pressure health only; swap has a separate status and never colours RAM by itself."""
    level = memory.get("level")
    if level is not None:
        return ("memory low", "bad") if level > _config.MAX_PRESSURE_LEVEL else ("memory ok", "status-normal")
    if any(memory.get(key) is not None for key in ("available", "cgroup_available", "psi_some", "psi_full")):
        facts = dict(memory, swap_total=0, swap_free=None)
        for key in ("available", "psi_some", "psi_full"):
            facts.setdefault(key, None)
        return ("memory low", "bad") if _platform.linux_short(facts) else ("memory ok", "status-normal")
    return "memory unknown", "dim"


def machine_note(model: dict[str, Any]) -> list[tuple[str, str]]:
    """MACHINE's top border, on the right (owner, 2026-11-12): the load outside leases and whether the queue admits
    work, the admission red when it blocks."""
    admission = model.get("admission")
    permit = (("admission: ?", "dim") if admission is None else ("no admission", "bad") if not admission
              else (f"admission: {admission} slots", "status-normal"))
    return [(f"unleased {model['advice']['unleased_load']:.1f}", "status-normal"), (" · ", "dim"), permit]


class Resource(NamedTuple):
    """A MACHINE row: its short label, how full (None: unknown), its used/total value in full and short, and whether
    it is critical by the rules that turn its bar red."""
    label: str
    fraction: float | None
    value: str
    short: str
    critical: bool


def machine_resources(model: dict[str, Any]) -> list[Resource]:
    """load, mem and swap as used/total (owner, 2026-11-12): `10.1/12`, `23.0/32G`, `10.0/11G`; short `23/32G`."""
    memory = cast(dict[str, Any], model["memory"])
    cores, slots = int(model["cores"]), int(model["slots"])
    load1 = float(model["averages"][0])
    # Critical where the one-minute load leaves less than one slot's share of the cores free (#15 review): by load
    # alone, so a fully leased but quiet machine is not red
    load_value = f"{load1:.1f}/{cores}"
    found = [Resource("load", load1 / cores, load_value, load_value, cores - load1 < cores / slots)]
    used, total = memory.get("used"), memory.get("total")
    if used is None and total and memory.get("available") is not None:
        used = max(0, total - memory["available"])
    low = machine_memory_status(memory)[1] == "bad"
    if total and used is not None:
        found.append(Resource("mem", used / total, f"{gigabytes(used)}/{gigabytes(total, True)}G",
                              f"{round(used / 1024)}/{round(total / 1024)}G", low))
    else:
        found.append(Resource("mem", None, "?", "?", low))
    swap_total = float(memory.get("swap_total") or 0)
    if swap_total and memory.get("swap_free") is not None:
        swap_used = max(0.0, swap_total - float(memory["swap_free"]))
        swap_low = float(memory["swap_free"]) < _config.number_setting("MIN_FREE_SWAP_MB", _config.MIN_FREE_SWAP_MB)
        found.append(Resource("swap", swap_used / swap_total, f"{gigabytes(swap_used)}/{gigabytes(swap_total, True)}G",
                              f"{round(swap_used / 1024)}/{round(swap_total / 1024)}G", swap_low))
    else:
        found.append(Resource("swap", None, "off" if memory.get("swap_total") == 0 else "?",
                              "off" if memory.get("swap_total") == 0 else "?", False))
    return found


def machine_side_by_side(width: int) -> bool:
    """Whether MACHINE is wide enough for its chart beside the three rows rather than under them (owner)."""
    return width >= MACHINE_SIDE_WIDTH


def machine_height(width: int) -> int:
    """MACHINE's natural height at this width: shorter with the chart beside the rows."""
    return MACHINE_SIDE_HEIGHT if machine_side_by_side(width) else MACHINE_HEIGHT


def draw_resource_rows(canvas: Canvas, row: int, x: int, width: int, resources: list[Resource]) -> None:
    """The three rows in `width` cells from x: health mark, label, a bar, and the value right-aligned in one column
    as wide as the longest, so every bar has the same length; short values (`23/32G`) where full ones leave no bar."""
    values = [resource.value for resource in resources]
    column = max(text_width(value) for value in values)
    bar_width = width - MACHINE_LABEL - 1 - column
    if bar_width < MACHINE_BAR_MIN:
        values = [resource.short for resource in resources]
        column = max(text_width(value) for value in values)
        bar_width = width - MACHINE_LABEL - 1 - column
    bar_width = max(0, bar_width)
    for line, (resource, value) in enumerate(zip(resources, values)):
        canvas.put(row + line, x, health_marks["hot" if resource.critical else "ok"], "text", 2)
        canvas.put(row + line, x + 3, resource.label, "dim")
        if bar_width and resource.fraction is not None:
            canvas.segments(row + line, x + MACHINE_LABEL, ramp_bar(resource.fraction, bar_width, resource.critical),
                            bar_width)
        canvas.put(row + line, x + width - text_width(value), value, "text")


def draw_machine(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict) -> None:
    canvas.box(y, x, height, width, "MACHINE")
    note = machine_note(cast(dict[str, Any], model))
    for parts in (note, note[2:]):  # all of it, else the admission alone, else nothing
        size = sum(text_width(text) for text, _ in parts) + 2
        if x + 2 + text_width(" MACHINE ") + 2 + size <= x + width - 2:
            canvas.segments(y, x + width - 2 - size, [(" ", "text"), *parts, (" ", "text")], size)
            break
    inner, slots = width - 6, int(cast(int, model["slots"]))
    resources = machine_resources(cast(dict[str, Any], model))
    rows = len(resources)
    interior = height - 2
    # Wide (owner, 2026-11-12): the chart beside the rows, its rows level with theirs, its axis under it alone
    if machine_side_by_side(width) and interior >= 1 + rows + 1:
        metrics = MACHINE_LABEL + MACHINE_BAR_SIDE + 1 + max(text_width(resource.value) for resource in resources)
        chart_x, chart_width = x + 3 + metrics + MACHINE_GAP, inner - metrics - MACHINE_GAP
        chart_rows = min(CHART_ROWS, interior - 2)
        draw_resource_rows(canvas, y + 2, x + 3, metrics, resources)
        draw_chart(canvas, y + 2, chart_x, chart_width, chart_rows, cast(dict[str, Any], model), slots)
        return
    # Stacked: a blank row, the three rows, the chart and its axis line, which the bottom frame follows directly.
    # Short of rows (owner, 2026-11-12) the chart gives up height first, 4 → 3 → 2 rows and then entirely (never one
    # row); then the blank row; then the rows become one compact line; memory and swap never go.
    full = interior >= rows
    padded = interior >= 1 + rows
    chart_rows = min(CHART_ROWS, interior - 1 - rows - 1) if padded else 0
    chart_rows = chart_rows if chart_rows >= MIN_CHART_ROWS else 0
    row = y + 2 if padded else y + 1
    if full:
        draw_resource_rows(canvas, row, x + 3, inner, resources)
        row += rows
    elif interior >= 1:
        canvas.segments(row, x + 3, compact_resources(
            [(resource.label, resource.fraction, resource.value, resource.critical) for resource in resources],
            inner, [resource.short for resource in resources]), inner)
        row += 1
    if chart_rows:
        draw_chart(canvas, row, x + 3, inner, chart_rows, cast(dict[str, Any], model), slots)


def draw_chart(canvas: Canvas, row: int, x: int, width: int, rows: int, model: dict[str, Any], slots: int) -> None:
    """The history chart, `rows` high from `row`, with half a cell of air at each end (owner, 2026-11-12), and its
    axis line right under it."""
    chart_width = max(1, width)
    history = cast(list[tuple[datetime.datetime, datetime.datetime, int]], model["history"])
    buckets = history_buckets(history, chart_width)
    for line, column_glyphs in enumerate(chart_cells(buckets, slots, rows)):
        colour = round((rows - 1 - line) / (rows - 1) * (_theme.CHART_STEPS - 1))
        for column, glyph in enumerate(column_glyphs):
            canvas.put(row + line, x + column, glyph, f"chart{colour}" if glyph != " " else "dim")
    summary = history_summary(history, slots)
    canvas.segments(row + rows, x, machine_legend(cast(dict[str, Any], summary), slots, chart_width), width)


def chart_cells(buckets: list[int], slots: int, rows: int, half_gaps: bool = True) -> list[list[str]]:
    """The history chart's glyphs, top row first, one per bucket. With `half_gaps` (owner, 2026-11-12) the span
    runs from the bottom cell's mid-line to the top cell's: a column with any height shows `▀` in the bottom row,
    the rows above fill from their own bottoms in exact eighths, and a full column tops out at `▄` in the top row,
    so half a cell of air sits above the chart and half over its axis. Without them, the full rows."""
    span = 8 * (rows - 1) if half_gaps else 8 * rows
    base = 4 if half_gaps else 0  # eighths below the span's zero: the bottom cell's lower half
    grid: list[list[str]] = [[] for _ in range(rows)]
    for level in buckets:
        height = base + math.ceil(min(1.0, level / slots) * span) if level > 0 and slots else 0
        for line in range(rows):
            above = rows - 1 - line  # rows below this one
            if half_gaps and above == 0:
                glyph = "▀" if height else " "
            else:
                fill = max(0, min(8, height - 8 * above))
                glyph = SPARK[fill - 1] if fill else " "
            grid[line].append(glyph)
    return grid


def gigabytes(value_mb: float | None, total: bool = False) -> str:
    """Megabytes as a short GB number for the compact resource line: 24.1, or a total as 32 or 0.5."""
    if value_mb is None:
        return "?"
    value = value_mb / 1024
    return (f"{value:.1f}".rstrip("0").rstrip(".") or "0") if total else f"{value:.1f}"


def compact_resources(resources: list[tuple[str, float | None, str, bool]], width: int,
                      shorts: list[str] | None = None) -> list[tuple[str, str]]:
    """Load, memory and swap on one line (owner, 2026-11-12) when MACHINE is too short for their rows: short ramp
    bars (6 to 4 cells, red when critical), else the numbers alone, a critical one in red; at the narrowest the
    short values (`23/32G`), then no marks. Never drops one."""
    parts: list[tuple[str, str]] = []
    forms = [(6, " · ", True, False), (5, " · ", True, False), (4, " · ", True, False), (0, " · ", True, False),
             (0, "  ", True, False), (0, " ", True, False), (0, " ", True, True), (0, " ", False, False),
             (0, " ", False, True)]
    for bar_width, separator, marked, short in forms:
        parts = []
        for index, (label, fraction, value, critical) in enumerate(resources):
            if short and shorts:
                value = shorts[index]
            if index:
                parts.append((separator, "dim"))
            if marked:
                parts.append((health_marks["hot" if critical else "ok"] + " ", "text"))
            parts.append((label + " ", "dim"))
            if bar_width and fraction is not None:
                parts += ramp_bar(fraction, bar_width, critical) + [(" ", "text")]
            parts.append((value, "bad" if critical and not bar_width else "text"))
        if sum(text_width(text) for text, _ in parts) <= width:
            break
    return parts


def window_text(seconds: float) -> str:
    """How far back a chart reaches, in a friendly unit: −45s, −90s, −15m, −60m, −2h."""
    seconds = max(1, round(seconds))
    if seconds < 120:
        return f"−{seconds if seconds < 30 else 5 * round(seconds / 5)}s"
    minutes = seconds / 60
    if minutes < 120:
        return f"−{round(minutes) if minutes < 10 else 5 * round(minutes / 5)}m"
    return f"−{round(minutes / 60)}h"


def machine_legend(summary: dict[str, Any], slots: int, chart_width: int) -> list[tuple[str, str]]:
    """The history chart's legend, a time axis under it: the window it shows on the left, under its first column,
    `now` on the right under its last, and between them, centred, the mean and most held slots and the share of
    the time every slot was held. A narrow chart drops full, then peak, then avg; the axis stays."""
    left, right = window_text(chart_width * max(1, math.ceil(HISTORY_WINDOW / chart_width))), "now"
    stats = [[("avg", "dim"), (f" {summary['average'] * slots:.1f}/{slots}", "text")],
             [("peak", "dim"), (f" {summary['peak']}", "text")],
             [("full", "dim"), (f" {summary['full']:.0%}", "text")]]
    room = chart_width - text_width(left) - text_width(right) - 4  # two cells each side of the stats
    parts: list[tuple[str, str]] = []
    while stats:
        parts = [part for index, stat in enumerate(stats) for part in ([(" · ", "dim")] if index else []) + stat]
        if sum(text_width(text) for text, _ in parts) <= room:
            break
        stats.pop()
        parts = []
    middle = sum(text_width(text) for text, _ in parts)
    gap = max(0, chart_width - text_width(left) - text_width(right) - middle)
    before = gap // 2 if parts else gap
    return ([(left, "dim"), (" " * before, "text")] + parts
            + [(" " * (gap - before), "text"), (right, "dim")])


def draw_events(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict) -> None:
    events = cast(list[dict[str, Any]], model["events"])[::-1]
    cells = max([5] + [text_width(cells_text(min(SLOT_CELLS, int(event["count"])))) for event in events])
    placed = cast(list[tuple[str, str, int, int, bool]], columns(width - 6, [
        ("time", "time", 10, False, 0), ("event", "event", 8, False, 1), ("slots", "slots", cells, False, 2),
        ("detail", "detail", 22, False, 3), ("label", "label", 0, False, 0), ("pid", "pid", 6, True, 4)]))
    size = next((column[3] for column in placed if column[0] == "slots"), cells)
    table_header(canvas, y, x, height, width, "EVENTS", placed, name="events")
    styles = {"granted": "ok", "released": "accent", "left": "dim", "reaped": "bad", "priority": "accent",
              "waiting": "warn"}
    offset, visible = table_view(canvas, "events", y, x, height, width, y + 2, height - 4, len(events))
    if not events:
        canvas.put(y + 2, x + 3, "No grants, releases or reaps logged yet", "dim", width - 6)
    for row, event in enumerate(events[offset:offset + visible], y + 2):
        style = styles[event["verb"]]
        draw_row(canvas, row, x + 3, placed, {
            "time": (clock(event["when"]), "dim"), "event": (event["verb"], style),
            "label": str(event["label"] or "?"), "slots": event_cells(event, size, style),
            "detail": (str(event.get("detail") or ""), "dim"), "pid": str(event["pid"])})


# The help (#15): sections of term → description rows and short notes. A row is (term, description); a note is
# ("", text) in the muted token, or ("", text, "text") in the text colour.
HELP: list[tuple[str, list[tuple[str, ...]]]] = [
    ("KEYS", [
        ("↑ ↓", "select a row · scroll a panel"),
        ("pgup pgdn", "a page"),
        ("home end", "first · last row, top · bottom"),
        ("tab", "next panel, by columns (shift-tab: back)"),
        ("click", "a row, a panel, a footer hint (as its key)"),
        ("wheel", "scroll the panel under the pointer"),
        ("s", "settings"),
        ("?", "help"),
        ("v", "changelog (or click the version)"),
        ("esc", "close an overlay"),
        ("q", "quit"),
        ("shift+drag", "select text"),
        ("", "Hotkeys work on any keyboard layout."),
    ]),
    ("STATES", [
        ("▶️", "running"),
        ("⏸️", "waiting"),
        ("↩️", "yielded, back in the queue"),
        ("❓", "asked to yield its slots"),
        ("💤", "idle: under 0.1 core for 5 min"),
        ("🔥", "above 1.5× its CPU share"),
        ("👨‍🦳", "lease of an older client"),
    ]),
    ("PRIORITY", [
        ("⚡", "interactive: someone is waiting on it"),
        ("🐢", "batch: unattended work, after the rest"),
        ("", "Normal has no mark."),
    ]),
    ("SLOTS", [
        ("", "One block per slot; the colour is its owner.", "text"),
        ("", "Slots budget scheduling, not CPU or memory."),
        ("manual · auto", "saved or computed capacity"),
        ("draining N", "held above a lowered capacity"),
    ]),
    ("QUEUE", [
        ("slots", "cells held or asked for"),
        ("cpu", "cores used / cores its slots stand for (a soft limit)"),
        ("time", "running or waiting for"),
        ("pid", "lease owner"),
        ("", "The bar turns red above 1.5× its share and yellow when idle."),
        ("", "An idle lease that can yield is asked to when a request waits for its slots. Nothing is ever killed."),
    ]),
    ("DETAILS", [
        ("start est", "when the queue head should start"),
        ("pass until", "later work may overtake until then"),
        ("receipt", "the lane's check: green or red, current or stale"),
        ("review", "review findings still open"),
        ("", "Estimates, not promises."),
    ]),
    ("EVENTS", [
        ("granted ▪", "got slots"),
        ("released ▫", "gave them back"),
        ("left", "stopped waiting"),
        ("reaped", "owner exited; entry removed"),
    ]),
    ("MACHINE", [
        ("load", "the 1-minute load average / cores (not CPU %)"),
        ("mem · swap", "used / total, in GB"),
        ("🆗 · 🔥", "fine · critical"),
        ("unleased", "demand outside leased work"),
        ("admission", "slots a new request could get now"),
        ("", "Bars run indigo→red, and turn all red when the resource is critical."),
        ("", "The chart shows held slots over time, now on the right."),
    ]),
    ("SETTINGS", [
        ("← →", "change the value"),
        ("enter", "edit · save"),
        ("d · D D", "reset one · reset all"),
        ("esc", "close"),
        ("", "A lower capacity drains: running work keeps its slots."),
        ("", "Colours follow the terminal theme; Colour theme picks Auto, Terminal or Built-in."),
    ]),
]
HELP_INDENT, HELP_GAP = 2, 2  # before a term, and between the term column and its description


def wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, max(1, width), break_long_words=True, break_on_hyphens=False) or [""]




HEADING_BEFORE, HEADING_AFTER = 2, 1  # blank lines around an overlay's section or release title (owner)


def add_heading(lines: list[list[tuple[str, str]]], title: list[tuple[str, str]]) -> None:
    """Adds a section title: HEADING_BEFORE blank lines before it but the first, HEADING_AFTER after it."""
    blank: list[tuple[str, str]] = []
    lines.extend([blank[:] for _ in range(HEADING_BEFORE if lines else 0)] + [title]
                 + [blank[:] for _ in range(HEADING_AFTER)])


def help_lines(width: int) -> list[list[tuple[str, str]]]:
    """The help laid out at `width` cells: a section title, then its rows with the terms in one column as wide as the
    section's longest term and each description wrapped under itself (a hanging indent), then its notes. Two blank
    lines before a section title but the first, one after it (owner). A row too narrow for both columns puts its
    description under its term."""
    lines: list[list[tuple[str, str]]] = []
    for title, entries in HELP:
        add_heading(lines, [(title, "title")])
        terms = max([text_width(entry[0]) for entry in entries if entry[0]] or [0])
        column = HELP_INDENT + terms + HELP_GAP
        for entry in entries:
            term, text = entry[0], entry[1]
            pad = " " * HELP_INDENT
            if not term:
                lines.extend([[(pad + line, entry[2] if len(entry) > 2 else "dim")] for line in wrap(text, width - HELP_INDENT)])
            elif width - column >= 12:
                wrapped = wrap(text, width - column)
                lines.append([(pad, "text"), (term, "bold"), (" " * (column - HELP_INDENT - text_width(term)), "text"),
                              (wrapped[0], "text")])
                lines.extend([[(" " * column + line, "text")] for line in wrapped[1:]])
            else:
                lines.append([(pad, "text"), (term, "bold")])
                lines.extend([[(" " * (HELP_INDENT * 2) + line, "text")] for line in wrap(text, width - 2 * HELP_INDENT)])
    return lines


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


def draw_help(canvas: Canvas) -> None:
    draw_overlay(canvas, "HELP", help_lines, "help")


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


def installed_release() -> Path | None:
    """The package directory the installed `slotfather` command runs now, from its entry point."""
    with contextlib.suppress(OSError, UnicodeDecodeError):
        found = INSTALLED_RELEASE.search(Path(sys.argv[0]).read_text())
        if found:
            return Path(found[1]) / "slotfather"
    return None


def changelog_text() -> str:
    """The changelog bundled with the installed package; this package's own, or the checkout's, without one."""
    here = Path(__file__).resolve()
    for path in (installed_release(), here.parent, here.parents[2]):
        if path is not None and (path / "CHANGELOG.md").is_file():
            with contextlib.suppress(OSError, UnicodeDecodeError):
                return (path / "CHANGELOG.md").read_text()
    return ""


def changelog_lines(width: int, releases: list[Release] | None = None, running: str | None = None,
                    stale: bool = False) -> list[list[tuple[str, str]]]:
    """The changelog laid out at `width`: a release title (version · date · commit), the running one marked
    installed and, when a newer install waits for a restart, newer ones marked new; then each section's entries as
    bullets with a hanging indent. An empty release (Unreleased) goes."""
    releases = parse_changelog(changelog_text()) if releases is None else releases
    running = _version.VERSION if running is None else running
    lines: list[list[tuple[str, str]]] = []
    for release in releases:
        if not any(entries for _, entries in release.sections):
            continue
        title: list[tuple[str, str]] = [(release.version, "title")]
        title += [(f" · {part}", "dim") for part in (release.date, release.commit) if part]
        if release.version == running:
            title.append(("  installed", "ok"))
        elif stale and release.version[:1].isdigit() and version_key(release.version) > version_key(running):
            title.append(("  new — restart to get these", "warn"))
        add_heading(lines, title)
        for heading, entries in release.sections:
            if not entries:
                continue
            lines.append([("  " + heading, "bold")])
            for entry in entries:
                wrapped = wrap(entry, width - 6)
                lines.append([("    • ", "dim"), (wrapped[0], "text")])
                lines.extend([[(" " * 6 + more, "text")] for more in wrapped[1:]])
    return lines or [[("No changelog in this installation.", "dim")]]


def draw_changelog(canvas: Canvas, stale: bool = False) -> None:
    def layout(width: int) -> list[list[tuple[str, str]]]:
        return changelog_lines(width, stale=stale)

    draw_overlay(canvas, "CHANGELOG", layout, "changelog")


NARROW_MACHINE, NARROW_EVENTS = 44, 38  # the least width each takes side by side; below both, EVENTS goes


def narrow_machine_width(width: int) -> int:
    """MACHINE's width in the one-column layout: half, at least NARROW_MACHINE, with EVENTS beside it in the rest
    and a one-cell gap; the whole width where EVENTS would get under NARROW_EVENTS."""
    machine = max(NARROW_MACHINE, (width - 1) // 2)
    return machine if width - machine - 1 >= NARROW_EVENTS else width


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


# The footer's hints in their order: navigation, panels, configuration, help, quit last; a narrow footer drops them
# in DROP_ORDER, quit last to go. On a scrollable panel the arrows scroll.
FOOTER_HINTS: tuple[tuple[str, str], ...] = (("↑↓", "select"), ("tab", "panel"), ("s", "settings"), ("?", "help"),
                                             ("q", "quit"))
DROP_ORDER = ("?", "s", "tab", "↑↓", "q")
HINT_GAP = 3
# What a click on a hint presses, by the hint's key: the same key; for a pair of arrows, each arrow its own
HINT_KEYS: dict[str, str | tuple[str, ...]] = {"s": "s", "?": "?", "q": "q", "tab": "\t", "esc": "esc", "enter": "\r",
                                              "d": "d", "↑↓": ("up", "down"), "←→": ("left", "right")}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def footer_hints(focus: str) -> list[tuple[str, str]]:
    return [("↑↓", "select" if focus == "requests" else "scroll"), *FOOTER_HINTS[1:]]


_identity: dict[str, Any] = {}


def running_identity() -> dict[str, Any]:
    """The version metadata of the code this process runs, read once (version.info())."""
    if not _identity:
        try:
            _identity.update(_version.info())
        except (OSError, ValueError, RuntimeError):
            _identity["commit"] = None
    return _identity


def running_version() -> str:
    """The footer's version: a semver where the package has one, else the short commit (+ for a dirty checkout)."""
    found = running_identity()
    if found.get("version"):
        return str(found["version"])
    commit = str(found.get("commit") or "")[:7]
    return commit + ("+" if commit and found.get("dirty") else "")


def running_source() -> str | None:
    return running_identity().get("source_sha256")


INSTALLED_RELEASE = re.compile(r"sys\.path\.insert\(0, '([^']+)'\)")


def installed_source(entry: str | None = None) -> str | None:
    """The source digest of the release the installed `slotfather` command runs now, from its entry point; None where
    it cannot be told (a checkout, a missing file). A digest other than running_source() means a newer install."""
    try:
        found = INSTALLED_RELEASE.search(Path(entry or sys.argv[0]).read_text())
        if found is None:
            return None
        data: Any = json.loads((Path(found[1]) / "slotfather" / "version.json").read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    digest = cast(dict[str, Any], data).get("source_sha256") if isinstance(data, dict) else None
    return str(digest) if digest else None


def hints_width(hints: list[tuple[str, str]]) -> int:
    return sum(text_width(key) + 1 + text_width(action) for key, action in hints) + HINT_GAP * max(0, len(hints) - 1)


def draw_hints(canvas: Canvas, start: int, end: int) -> int:
    """The hints centred on the footer between columns start and end (the name and the status), never over them;
    the column after the last hint."""
    hints: list[tuple[str, str]] = footer_hints(canvas.focus)

    def width() -> int:
        return hints_width(hints)

    for key in DROP_ORDER:
        if not hints or width() <= end - start - 2:
            break
        hints = [hint for hint in hints if hint[0] != key]
    if not hints:
        return start
    x = max(start + 1, min((canvas.width - width()) // 2, end - 1 - width()))
    for index, (key, action) in enumerate(hints):
        x = canvas.hint(canvas.height - 1, x, key, action)
        if index < len(hints) - 1:
            x += HINT_GAP
    return x


def dashboard_draw(canvas: Canvas, model: dict[str, Any] | None, selected: int = 0, help_open: bool | str = False,
                   error: str | None = None, cwd: str | None = None, scroll: dict | None = None,
                   settings: _settings.Settings | None = None) -> None:
    canvas.scroll = scroll if scroll is not None else {}
    width, height = canvas.width, canvas.height
    x, status_end = draw_header(canvas, model, error)
    version = running_version() + (" ↻" if model and model.get("installed") not in (None, running_source()) else "")
    if version and status_end - x - text_width(version) - 1 >= hints_width(footer_hints(canvas.focus)) + 2:
        end = canvas.put(height - 1, x - 2, version, "dim")  # the version goes before any hint on a narrow footer
        canvas.version_at = (height - 1, x - 2, end)  # a click here opens the changelog
        x = end + 3
    x = draw_hints(canvas, x, status_end)
    if error:
        canvas.put(height - 1, x + 2, f"refresh failed: {error}", "bad", max(0, status_end - x - 2))
    if model is None:
        canvas.put(2, 3, f"Queue unavailable: {error}" if error else "Reading the queue…",
                   "bad" if error else "dim", width - 6)
        if help_open:
            draw_open_overlay(canvas, help_open, model)
        if settings is not None:
            settings.draw(canvas, cast(dict[str, Any] | None, model))
        return
    moment = datetime.datetime.now(datetime.timezone.utc)
    if width < 50 or height < 14:
        draw_slots(canvas, min(2, height - 4), 3, width - 6, model)
        if 6 < height - 1:
            canvas.put(6, 3, "Enlarge the terminal for leases and the queue", "dim", width - 6)
        if help_open:
            draw_open_overlay(canvas, help_open, model)
        if settings is not None:
            settings.draw(canvas, cast(dict[str, Any] | None, model))
        return
    left = width - max(52, width * 2 // 5) - 1 if width >= WIDE else width
    slot_height = 5
    canvas.box(0, 0, slot_height, left, "SLOTS", f"{model['cores']} cores · {model['slots']} slots "
               + ("(manual)" if model["saved"] else "(auto)")
               + (f" · draining {drain}" if (drain := model.get("draining")) else ""))
    draw_slots(canvas, 2, 3, left - 6, model)
    top, available = slot_height, height - 1 - slot_height
    holders, waiters = model["holders"], model["waiters"]
    requests = max(1, len(holders) + len(waiters)) + bool(holders and waiters) + 4  # the frame and a blank row each
    if width >= WIDE:
        heights = allocate(available, [("requests", min(requests, 10), requests), ("events", 5, available)])
        y = top
        for name in ("requests", "events"):
            if name in heights:
                draw_panel(canvas, name, y, 0, heights[name], left, model, selected, cwd, moment)
                y += heights[name]
        right_available = height - 1
        machine = min(machine_height(width - left - 1), right_available - 10) if right_available >= 16 else 0
        right = {"detail": right_available - machine, "machine": machine}
        y = 0
        for name in ("detail", "machine"):
            if right[name]:
                draw_panel(canvas, name, y, left + 1, right[name], width - left - 1, model, selected, cwd, moment)
                y += right[name]
    else:  # one column: QUEUE, the details, then MACHINE and EVENTS side by side, or MACHINE alone (owner)
        machine_width = narrow_machine_width(width)
        # The bottom row is MACHINE's own height (EVENTS beside it scrolls in the same); QUEUE gets its rows and the
        # details what is left (owner, 2026-11-12). Short of room, each gives way down to its minimum.
        heights = allocate(available, [("requests", min(requests, 10), requests), ("detail", 5, 5),
                                       ("bottom", 6, machine_height(machine_width))])
        extra = heights.get("bottom", 0) - machine_height(machine_width)
        if extra > 0:  # MACHINE never grows past its own height: the rest goes to the details
            heights["bottom"] = machine_height(machine_width)
            heights["detail" if "detail" in heights else "requests"] += extra
        y = top
        for name in ("requests", "detail", "bottom"):
            if name not in heights:
                continue
            if name != "bottom":
                draw_panel(canvas, name, y, 0, heights[name], width, model, selected, cwd, moment)
            elif machine_width < width:  # EVENTS on the left, MACHINE on the right (owner, 2026-11-12)
                events_width = width - machine_width - 1
                draw_panel(canvas, "events", y, 0, heights[name], events_width, model, selected, cwd, moment)
                draw_panel(canvas, "machine", y, events_width + 1, heights[name], machine_width, model, selected,
                           cwd, moment)
            else:
                draw_panel(canvas, "machine", y, 0, heights[name], width, model, selected, cwd, moment)
            y += heights[name]
    if help_open:
        draw_open_overlay(canvas, help_open, model)
    if settings is not None:
        settings.draw(canvas, cast(dict[str, Any] | None, model))


def draw_open_overlay(canvas: Canvas, which: bool | str, model: dict[str, Any] | None) -> None:
    if which == "changelog":
        draw_changelog(canvas, bool(model and model.get("installed") not in (None, running_source())))
    else:
        draw_help(canvas)


def draw_panel(canvas: Canvas, name: str, y: int, x: int, height: int, width: int, model: dict, selected: int,
               cwd: str | None, moment: datetime.datetime) -> None:
    if name == "requests":
        draw_requests(canvas, y, x, height, width, model, selected, moment)
    elif name == "detail":
        draw_detail(canvas, y, x, height, width, model, selected, cwd, moment)
    elif name == "machine":
        draw_machine(canvas, y, x, height, width, model)
    else:
        draw_events(canvas, y, x, height, width, model)


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
def full_screen(fd_in: int, fd_out: int, answers: _theme.Answers | None = None):
    """The alternate screen with the cursor hidden, line wrap off and keys read one at a time without echo; Ctrl-C
    still interrupts. With `answers`, the terminal is first asked for its colours (#14), at most
    theme.QUERY_TIMEOUT, and colour-scheme change notifications are subscribed to. Everything is put back on the way
    out."""
    saved = termios.tcgetattr(fd_in)
    try:
        tty.setcbreak(fd_in)
        active = termios.tcgetattr(fd_in)
        active[0] &= ~(termios.IXON | termios.IXOFF)  # Ctrl-S must not freeze the screen by terminal flow control.
        termios.tcsetattr(fd_in, termios.TCSANOW, active)
        os.write(fd_out, b"\x1b[?1049h\x1b[?25l")
        if answers is not None:  # before the kitty push, so no reply can be taken for a key event
            # On the alternate screen, so the width probe of 👨‍🦳 (#15) never shows on the shell's screen
            typed = _theme.ask(fd_in, fd_out, answers, probe=MARKS["legacy"][0])
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


DETAILS_FRESH = 5.0  # seconds a request's loaded details are shown as they are; older ones load again, quietly
PANELS = ("requests", "details", "events")  # Tab's order (#15)


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


def load_details(item: dict[str, Any]) -> tuple[Any, str | None]:
    """A request's slow facts, off the main thread (#15): its lane (a lanefather call) and its holder's directory (lsof
    or /proc). SLOTFATHER_DETAILS_DELAY, a test seam, adds seconds first."""
    delay = _config.number_setting("DETAILS_DELAY", 0.0)
    if delay > 0:
        time.sleep(delay)
    found = dict(item)
    enrich_details(found)  # the lane, through the same seam --once and the demo use
    cwd = process_cwd(item["pid"]) if item.get("kind") != "waiting" else None
    return found.get("lane"), cwd


def load_model(_: Any = None) -> tuple[dict[str, Any], str | None]:
    return cast(dict[str, Any], dashboard_model()), installed_source()


class DetailsCache:
    """Loaded details by request key, with when they arrived. A result is kept under the key it was loaded for, so a
    load that finishes after the selection moved on never paints over the row now selected (#15)."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self.clock = clock
        self.found: dict[Any, tuple[float, Any, str | None]] = {}

    def put(self, key: Any, lane: Any, cwd: str | None) -> None:
        self.found[key] = (self.clock(), lane, cwd)

    def get(self, key: Any) -> tuple[Any, str | None, bool] | None:
        """(lane, cwd, fresh) for a key loaded before, else None."""
        entry = self.found.get(key)
        if entry is None:
            return None
        return entry[1], entry[2], self.clock() - entry[0] < DETAILS_FRESH


def panel_at(canvas: Canvas, mx: int, my: int) -> str | None:
    """The panel (requests, details, events) under a click."""
    for name in PANELS:
        view = canvas.tables.get(name)
        if view and view["x"] <= mx < view["x"] + view["width"] and view["y"] <= my < view["y"] + view["height"]:
            return name
    return None


NAVIGATION = ("up", "k", "down", "j", "page_up", "page_down", "home", "g", "end", "G")


def next_panel(canvas: Canvas, focus: str, step: int) -> str:
    """Tab's next panel (Shift-Tab's with step -1) among those drawn in the last frame, by columns: left to right,
    each top to bottom, as the layout placed them (wide: QUEUE, EVENTS, DETAILS; narrow: top to bottom). A focus
    whose panel is no longer drawn starts again from QUEUE."""
    drawn = sorted((name for name in PANELS if name in canvas.tables),
                   key=lambda name: (canvas.tables[name].get("x", 0), canvas.tables[name].get("y", 0)))
    if "requests" not in drawn:
        drawn.insert(0, "requests")
    if focus not in drawn:
        return "requests"
    return drawn[(drawn.index(focus) + step) % len(drawn)]


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


def event_id(event: dict[str, Any]) -> tuple[Any, ...]:
    return (event.get("when"), event.get("verb"), event.get("pid"), event.get("count"))


def follow_events(scroll: dict[str, int], fresh: int, newest: Any, events: list[dict[str, Any]]) -> tuple[int, Any]:
    """EVENTS (newest first) after a refresh (#15): at the top it shows the newest; scrolled down, it keeps showing
    the same events as new ones arrive above them and counts those (fresh). (fresh, the newest event's id)."""
    ids = [event_id(event) for event in events[::-1]]
    arrived = ids.index(newest) if newest in ids else 0
    if scroll.get("events"):
        scroll["events"] += arrived
        fresh += arrived
    else:
        fresh = 0
    return fresh, ids[0] if ids else None


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


def dashboard_live(interval: float, theme: str = _theme.DEFAULT_MODE) -> None:
    fd_in, fd_out = sys.stdin.fileno(), sys.stdout.fileno()
    colour = not os.environ.get("NO_COLOR")
    # What the terminal says about its colours (#14) and how wide it draws 👨‍🦳 (#15): asked with colour off too
    answers = _theme.Answers()
    ascii_only = "utf" not in (sys.stdout.encoding or "").lower()
    model: dict[str, Any] | None = None
    error: str | None = None
    selected_key: Any = None
    index, colours, shown, drawn_size = 0, {}, [], None
    scroll: dict[str, int] = {}
    focus, fresh_events, newest = "requests", 0, None
    help_open: bool | str = False  # True: help; "changelog": the changelog
    settings: _settings.Settings | None = None
    drawn, whole, dirty = 0.0, 0.0, True
    glow, glowing = ScrollGlow(), set[str]()
    canvas = Canvas(0, 0)
    wake_read, wake_write = os.pipe()
    # Everything slow runs in the background, so a key moves the selection within one frame (#15)
    models, details, cache = Loader(load_model, wake_write), Loader(load_details, wake_write), DetailsCache()
    models.request("model")
    fetched = time.monotonic()
    try:
        with full_screen(fd_in, fd_out, answers):
            set_theme(_theme.from_environment(answers=answers, mode=theme))
            # 👨‍🦳 is one glyph where the cursor ended two cells on (column 3); without an answer, assume it is
            use_marks(ascii_only, joined=answers.column in (None, 3))
            while True:
                for _, value, failure in models.take():
                    if failure is not None:
                        error = dashboard_text(failure)
                    else:
                        fresh, installed = value
                        fresh["installed"] = installed
                        dashboard_colours(fresh, colours)
                        fresh_events, newest = follow_events(scroll, fresh_events, newest,
                                                             cast(list[dict[str, Any]], fresh["events"]))
                        model, error = fresh, None
                    dirty = True
                for key, value, failure in details.take():
                    lane, cwd = value if failure is None and value else (None, None)
                    cache.put(key, lane, cwd)
                    dirty = dirty or key == selected_key
                if settings is not None and settings.tick():  # an arrow change settled: saved, so show its effect now
                    interval, dirty = float(settings.interval), True
                    models.request("model")
                    fetched = time.monotonic()
                if settings is not None and settings.theme != theme:  # this dashboard's theme, applied at once
                    theme = str(settings.theme)
                    shown, dirty = [], True
                    set_theme(_theme.from_environment(answers=answers, mode=theme))
                if time.monotonic() - fetched >= interval and not models.wants("model"):
                    models.request("model")
                    fetched = time.monotonic()
                items = cast(list[dict[str, Any]], (model["holders"] + model["waiters"]) if model else [])
                keys = [item["key"] for item in items]
                index = keys.index(selected_key) if selected_key in keys else max(0, min(index, len(items) - 1))
                next_key = keys[index] if items else None
                if next_key != selected_key:
                    scroll.pop("details", None)
                selected_key = next_key
                loading, cwd = None, None
                if items:
                    item = items[index]
                    known = cache.get(selected_key)
                    if known is None:
                        loading = int(time.monotonic() * 10)  # a spinner at ten frames a second
                        if not details.wants(selected_key):
                            details.request(selected_key, dict(item))
                    else:
                        lane, cwd, current = known
                        if lane is not None:
                            item["lane"] = lane
                        if not current and not details.wants(selected_key):
                            details.request(selected_key, dict(item))  # quietly: the cached facts stay meanwhile
                if not scroll.get("events"):
                    fresh_events = 0
                size = os.get_terminal_size(fd_out)  # read each pass: at most 0.25 s behind a resize
                if size != drawn_size:
                    # A terminal reflows its own screen on resize: clear it and draw every row.
                    os.write(fd_out, b"\x1b[2J")
                    shown, drawn_size, dirty = [], size, True
                elif time.monotonic() - whole >= 5:
                    shown = []  # rewrite every row now and then, in place, so a stray line cannot stay wrong
                bright = glow.bright()
                if bright != glowing:  # a thumb lit up or faded back
                    glowing, dirty = bright, True
                if dirty or loading is not None or time.monotonic() - drawn >= 1 or not shown:
                    canvas = Canvas(size.columns, size.lines)
                    canvas.focus, canvas.loading, canvas.fresh_events = focus, loading, fresh_events
                    canvas.glow = glowing
                    dashboard_draw(canvas, model, index, help_open, error, cwd, scroll, settings)
                    if focus != "requests" and focus not in canvas.tables:  # its panel is not drawn now
                        focus = canvas.focus = "requests"
                    lines = canvas.lines(colour, ascii_only, place=True)
                    # Only rows that changed are written: a second's tick usually touches the clock and a few ages.
                    frame = "".join(f"\x1b[{row + 1};1H{line}" for row, line in enumerate(lines)
                                    if row >= len(shown) or shown[row] != line)
                    if frame:
                        os.write(fd_out, frame.encode("utf-8", "replace"))
                    whole = time.monotonic() if not shown else whole
                    shown, drawn, dirty = lines, time.monotonic(), False
                wait = 0.1 if loading is not None else 0.25
                fade = glow.fade()  # one wake at the moment a scrolled thumb goes back, not a poll
                ready = select.select([fd_in, wake_read], [], [], wait if fade is None else min(wait, fade))[0]
                if wake_read in ready:
                    os.read(wake_read, 4096)
                if fd_in not in ready:
                    continue
                reports: list[str] = []
                pressed = read_keys(fd_in, reports)
                if reports and theme_reports(reports, answers, fd_out, ascii_only):
                    set_theme(_theme.from_environment(answers=answers, mode=theme))
                    shown, dirty = [], True  # every row's colours changed
                offsets = dict(scroll)
                settings_offset = settings.offset if settings is not None else 0
                for key in pressed:
                    dirty = True
                    if isinstance(key, tuple) and key[4] and key[1] & ~28 == 0:  # a click on a footer hint is its key
                        key = canvas.hint_at(key[2], key[3]) or key
                    if settings is not None:
                        action = settings.mouse(key) if isinstance(key, tuple) else settings.key(key)
                        if action == "quit":
                            return
                        interval = float(settings.interval)  # this dashboard's own setting, changed at once
                        if settings.theme != theme:  # so is its colour theme
                            theme = str(settings.theme)
                            shown = []
                            set_theme(_theme.from_environment(answers=answers, mode=theme))
                        if action == "close":
                            settings = None
                            shown = []
                        if action == "changed":  # a saved setting applies at the next grant: show its effect now
                            models.request("model")
                            fetched = time.monotonic()
                        continue
                    if isinstance(key, tuple) and help_open:  # the help keeps the mouse; a click outside closes it
                        if help_mouse(canvas, key, scroll) == "close":
                            help_open, canvas.help_view = False, None
                        continue
                    if isinstance(key, tuple):  # a click or the wheel; Shift+drag stays the terminal's own selection
                        _, button, mx, my, pressed_now = key
                        at = canvas.version_at
                        if pressed_now and button & ~28 == 0 and at and my == at[0] and at[1] <= mx < at[2]:
                            help_open = "changelog"  # the footer's version opens what changed
                            scroll.pop("changelog", None)
                            continue
                        if pressed_now and button & ~28 == 0 and canvas.help_view is None:
                            focus = panel_at(canvas, mx, my) or focus  # a click focuses the panel it lands in
                        previous = index
                        index = mouse_action(canvas, key, scroll, index)
                        if index != previous:
                            scroll.pop("details", None)
                        selected_key = keys[index] if items else None
                        continue
                    key = _keys.hotkey(key)  # the hotkey at this key's US-QWERTY position (#13)
                    if help_open:
                        action = help_key(canvas, key, scroll)
                        if action == "quit":
                            return
                        if action == "close":
                            help_open = False
                            canvas.help_view = None
                        continue
                    if key in ("q", "Q"):
                        return
                    if key in ("\t", "back_tab"):  # Tab and Shift-Tab move between the panels drawn (#15)
                        focus = next_panel(canvas, focus, 1 if key == "\t" else -1)
                        continue
                    if focus != "requests" and panel_key(canvas, scroll, focus, key):
                        continue
                    if key in ("j", "down", "k", "up", "g", "home", "G", "end", "page_up", "page_down"):
                        scroll.pop("requests", None)
                        scroll.pop("details", None)
                    if key == "esc":
                        help_open = False
                    elif key == "?":
                        help_open = True
                        scroll.pop("help", None)
                        draw_help(canvas)
                    elif key == "v":  # the changelog, as a click on the footer's version
                        help_open = "changelog"
                        scroll.pop("changelog", None)
                    elif key == "s":
                        try:
                            settings = _settings.Settings(interval, theme)
                        except (_config.SlotError, OSError) as failure:
                            error = dashboard_text(failure)
                        if settings is not None:
                            settings.draw(canvas, model)
                    elif key in ("j", "down"):
                        index += 1
                    elif key in ("k", "up"):
                        index -= 1
                    elif key in ("g", "home", "page_up"):
                        index = 0
                    elif key in ("G", "end", "page_down"):
                        index = len(items) - 1
                    index = max(0, min(index, len(items) - 1))
                    selected_key = keys[index] if items else None
                moved = scrolled(offsets, scroll)
                if settings is not None and settings.offset != settings_offset:
                    moved.add("settings")
                glow.touch(moved)
    finally:
        for fd in (wake_read, wake_write):
            os.close(fd)


def theme_reports(reports: list[str], answers: _theme.Answers, fd_out: int, ascii_only: bool = False) -> bool:
    """Takes what the terminal reported; True when a colour or the width of 👨‍🦳 changed: everything is drawn
    again. A colour-scheme change notification (mode 2031) asks for the colours again: their answers arrive with
    later keys. A width reply later than ask() waited for (a slow link) still picks the glyph (#15)."""
    changed = False
    for report in reports:
        column = answers.column
        changed = answers.feed(report) or changed
        if answers.column != column:
            use_marks(ascii_only, joined=answers.column in (None, 3))
            changed = True
        if _theme.SCHEME_REPLY.match(report):
            os.write(fd_out, _theme.QUERY)
    return changed


def dashboard_command(argv: list[str]) -> int:
    interval, once, colour, width, height = 1.0, False, None, None, None
    while argv:
        if argv[0] == "--once":
            once, argv = True, argv[1:]
        elif argv[0] == "--color":
            colour, argv = True, argv[1:]
        elif argv[0] in ("--interval", "--width", "--height") and len(argv) > 1:
            try:
                value = float(argv[1])
            except ValueError:
                value = math.nan
            if argv[0] == "--interval":
                if not 0.1 <= value <= 60:
                    raise _config.SlotError("dashboard --interval takes seconds from 0.1 to 60")
                interval = value
            elif not (value.is_integer() and 5 <= value <= 1000):
                raise _config.SlotError(f"dashboard {argv[0]} takes a whole number of cells from 5 to 1000")
            elif argv[0] == "--width":
                width = int(value)
            else:
                height = int(value)
            argv = argv[2:]
        else:
            raise _config.SlotError("dashboard takes [--interval SECONDS] or --once [--width N] [--height N] [--color]")
    try:
        theme = _theme.checked_mode(os.environ.get("SLOTFATHER_THEME"))
        set_theme(_theme.from_environment())  # --once cannot ask the terminal: SLOTFATHER_TERMINAL_BG/_FG, COLORFGBG
    except ValueError as error:
        raise _config.SlotError(str(error)) from None
    if once:
        model = dashboard_model()
        dashboard_colours(model, {})
        items = model["holders"] + model["waiters"]
        if items:
            enrich_details(cast(dict[str, Any], items[0]))
        use_marks("utf" not in (sys.stdout.encoding or "").lower())
        size_now = shutil.get_terminal_size((120, 40))
        canvas = Canvas(width or size_now.columns, height or size_now.lines)
        dashboard_draw(canvas, cast(dict[str, Any], model), cwd=process_cwd(items[0]["pid"]) if items and items[0]["kind"] != "waiting"
                       else None)
        tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        print(canvas.render(colour if colour is not None else tty,
                            "utf" not in (sys.stdout.encoding or "").lower()))
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise _config.SlotError("dashboard needs an interactive terminal; dashboard --once prints one snapshot")
    try:
        dashboard_live(interval, theme)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return dashboard_command(list(sys.argv[1:] if argv is None else argv))
    except _config.SlotError as error:
        print(f"slotfather dashboard: {error}", file=sys.stderr)
        return 2
