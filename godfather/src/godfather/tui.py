"""The terminal framework, moved from Slotfather's dashboard unchanged in behaviour: a canvas of styled cells and
its rendering, text measured as the glyphs a terminal draws, footer hints as click targets, key and mouse reading
(the kitty keyboard protocol and SGR mouse), full-screen setup and restore, and the background loader. Frames,
tables and scrollbars are widgets.py; where things go is layout.py. It knows nothing about any tool.

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
import threading
import tty
import unicodedata
from urllib.parse import urlsplit
from typing import Any, cast

from . import keys as _keys
from . import theme as _theme


# Styles come from the theme (#14, theme.py): style -> (colour, attributes: b bold, d dim, r reverse), where the
# colour is None for the terminal's default foreground, a palette index or RGB. Frames are drawn in the muted "dim"
# token, the same as muted text, so the two cannot drift apart. Each canvas carries the theme it is drawn and written
# in (fathers#138); `current_theme` is only the one a canvas made without a theme takes, for tests and tools.
current_theme = _theme.resolve(_theme.DEFAULT_MODE, environ={})


def set_theme(theme: _theme.Theme) -> None:
    """The theme a Canvas made without one takes; the app gives each canvas its own."""
    global current_theme
    current_theme = theme


ASCII = str.maketrans({"▀": "#", "🆗": "+", "🔥": "!", "🮅": "#", "■": "#", "▪": "#", "◌": "o", "□": "o", "▫": "o", "▶": ">", "▉": "#", "▌": "+", "▐": "|", "█": "#", "▓": "%", "▒": "=", "░": ".", "·": ".", "●": "*", "…": "~", "─": "-", "│": "|",
                       "╭": "+", "╮": "+", "╰": "+", "╯": "+", "↑": "^", "↓": "v", "↗": "/", "↘": "\\", "→": ">",
                       "←": "<", "≈": "~", "×": "x", "—": "-", "⏸": "=", "▭": "o", "−": "-", "╻": "#", "╹": "#",  "┃": "#", "━": "=", "┍": "+", "┑": "+", "┕": "+", "┙": "+", "▁": "_", "▂": "_", "▃": "-", "▄": "-",
                       "▅": "=", "▆": "=", "▇": "#"})


JOINER, TEXT_STYLE, EMOJI_STYLE = "\u200d", "\ufe0e", "\ufe0f"  # zero-width joiner, variation selectors 15 and 16


def dashboard_text(value: Any) -> str:
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


def text_width(value: Any) -> int:
    return sum(cluster_width(cluster) for cluster in clusters(dashboard_text(value)))


def clip(value: Any, width: int) -> str:
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


def clock(moment: datetime.datetime | None, now: datetime.datetime | None = None) -> str:
    """A time of day, its weekday too when it is not on the day of `now` (the moment the screen is drawn at; None:
    the same day). It never reads the clock (fathers#138)."""
    if moment is None:
        return "--:--:--"
    local = moment.astimezone()
    same = now is None or local.date() == now.astimezone().date()
    return local.strftime("%H:%M:%S" if same else "%a %H:%M")


def megabytes(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value:.0f} MB"


def short(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def parse_utc(value: Any) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


class Canvas:
    """A grid of [character, style, selected] cells; a wide character takes its cell and an empty one after it."""

    def __init__(self, width: int, height: int, theme: _theme.Theme | None = None, marks: Any = None):
        # What it is drawn with (fathers#138): the theme its styles resolve in, and the marks (views.common.Marks) its
        # views draw, emoji where None
        self.theme = theme if theme is not None else current_theme
        self.marks = marks
        self.scroll: dict[str, int] = {}  # each panel's scroll offset this frame is drawn at (state.AppState.scroll)
        self.tables: dict[str, dict[str, Any]] = {}
        self.links: dict[tuple[int, int], str] = {}
        self.help_view: Any = None  # the overlay drawn (help or the changelog): a widgets.OverlayView
        self.focus = "requests"  # the panel keys act on (#15): requests (QUEUE), details or events; its frame is bright
        self.loading: int | None = None  # the details' spinner frame while they load in the background, else None
        self.fresh_events = 0  # events that arrived while EVENTS was scrolled down
        self.version_at: tuple[int, int, int] | None = None  # the footer version's row and columns: the changelog
        self.hint_hits: list[tuple[int, int, int, str]] = []  # (row, from, to, key) of every footer hint drawn
        self.filter_hits: list[tuple[int, int, int, int]] = []  # (row, from, to, tab) of what's new's tool filters
        self.order: tuple[str, ...] = ("requests",)  # Tab's order among the panels drawn (layout.tab_order())
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

    def select(self, y: int, start: int, end: int, tint: bool | str = True) -> None:
        """Marks cells start..end of row y selected: True for the neutral background, a holder style for its tint."""
        if 0 <= y < self.height:
            for column in range(max(0, start), min(self.width, end)):
                self.cells[y][column][2] = tint

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
                    parts.append(sgr(style, selected, self.theme))
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


def sgr(style: str, selected: bool | str, theme: _theme.Theme | None = None) -> str:
    """The SGR sequence for a cell in `theme` (current_theme where None). A selected cell has the theme's selection
    background, or is bold where the theme has none; its text keeps its colour (builtin: default-foreground text
    turns 252 over its dark band)."""
    theme = theme if theme is not None else current_theme
    fore, flags = theme.styles[style]
    if selected and fore is None:
        fore = theme.selected_text
    # `selected` is True, or a holder style: a running row's own tint (#15), the neutral selection where it has none
    back = (theme.tints.get(selected) if isinstance(selected, str) else None) or theme.selection \
        if selected else None
    bold = "b" in flags or (bool(selected) and back is None)
    codes = ["0"] + (["1"] if bold else []) + (["2"] if "d" in flags and fore is None else [])
    codes += _theme.sgr_colour(fore)
    if back is not None:
        codes += _theme.sgr_colour(back, 4)
    codes += ["7"] if "r" in flags else []
    return "\x1b[" + ";".join(codes) + "m"


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
