"""godfather: one terminal dashboard for the tools that share a development machine.

  godfather [--interval S]                                         live view: ? lists the keys, q quits
  godfather --once [--page overview] [--width N] [--height N] [--color]   one frame as text
  godfather --version

The main screen is Slotfather's dashboard, moved here unchanged: it is drawn from `slotfather status --json --full`
and Settings goes through `slotfather settings` (docs/contracts/slotfather.md). Everything slow runs in the
background, so a key moves the selection within one frame. A fault here ends the view, never a tool's work.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import math
import os
import select
import shutil
import sys
import time
from pathlib import Path
from typing import Any, cast

from . import keys as _keys
from . import sources as _sources
from . import theme as _theme
from . import version as _version
from .adapters import slotfather as _slotfather
from .adapters.slotfather import SettingsClient, SettingsError, Unsupported
from .sources import SnapshotError, Source
from .tui import (Canvas, Loader, ScrollGlow, dashboard_text, full_screen, help_key, help_mouse, mouse_action,
                  panel_key, read_keys, scrolled, set_theme)
from .views import overview as _overview
from .views import settings as _settings

USAGE = ("usage: godfather [--interval SECONDS]\n"
         "       godfather --once [--page overview] [--width N] [--height N] [--color]\n"
         "       godfather --version")


class UsageError(Exception):
    pass


# Godfather's own settings (SPEC 22): Refresh every and Colour theme, kept between runs

def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "godfather" / "settings.json"


def load_choices() -> dict[str, Any]:
    """The saved Refresh every and Colour theme; {} when there are none or they cannot be read."""
    with contextlib.suppress(OSError, ValueError):
        found = json.loads(config_path().read_text(encoding="utf-8"))
        if isinstance(found, dict):
            return cast(dict[str, Any], found)
    return {}


def save_choices(interval: float, theme: str) -> None:
    """Writes them atomically (a temporary file renamed over the old one); a failure leaves the old file."""
    path = config_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(f".settings-{os.getpid()}.json")
        pending.write_text(json.dumps({"v": 1, "interval": interval, "theme": theme}, indent=1) + "\n",
                           encoding="utf-8")
        os.replace(pending, path)


def remember(saved: tuple[float, str], settings: Any) -> tuple[float, str]:
    current = (float(settings.interval), str(settings.theme))
    if current != saved:
        save_choices(*current)
    return current


def starting_choices(interval: float | None) -> tuple[float, str]:
    """Refresh every: --interval, else the saved value, else 1 s. Colour theme: GODFATHER_THEME (SLOTFATHER_THEME
    where unset), else the saved one, else auto."""
    saved = load_choices()
    if interval is None:
        value = saved.get("interval")
        interval = float(value) if isinstance(value, (int, float)) and 0.1 <= value <= 60 else 1.0
    variable, named = _theme.setting("THEME", os.environ)
    if named:
        theme = _theme.checked_mode(named, variable)
    else:
        try:
            theme = _theme.checked_mode(saved.get("theme") if isinstance(saved.get("theme"), str) else None)
        except ValueError:
            theme = _theme.DEFAULT_MODE
    return interval, theme


class Screen:
    """The overview's data: the Slotfather source, rediscovered as SPEC's discovery rules say, polled one call at
    a time on the model loader's thread; the last good model stays, marked stale, after a failed poll."""

    def __init__(self, source: Source | None = None) -> None:
        self.source = source or Source("slotfather", _slotfather)

    def load(self, _: Any = None) -> tuple[dict[str, Any] | None, str | None]:
        """(the model to draw, the refresh error for the footer); discovery first when it is due."""
        source = self.source
        if source.due():
            source.discover()
            self.tabs()
        if source.state in ("absent", "unsupported"):
            return None, None
        try:
            source.took(_slotfather.decode(source.poll()), None)
        except (SnapshotError, Unsupported) as error:
            source.took(None, str(error))
        if source.snapshot is None:
            return None, source.problem
        model = dict(source.snapshot, installed=_version.installed_source())
        if source.state == "stale" and source.failed_at is not None:
            model["stale_since"] = datetime.datetime.fromtimestamp(source.failed_at, datetime.timezone.utc)
        return model, source.problem if source.state == "stale" else None

    def tabs(self) -> None:
        """The changelog overlay's Slotfather tab, from the changelog its capabilities name."""
        path = _slotfather.changelog(self.source.capabilities)
        tabs: list[_overview.ChangelogTab] = []
        if path:
            with contextlib.suppress(OSError, UnicodeDecodeError):
                tabs.append(_overview.ChangelogTab("Slotfather", Path(path).read_text(encoding="utf-8"),
                                                   str(self.source.version or "")))
        _overview.changelog_tabs[:] = tabs

    def notice(self) -> list[str] | None:
        """What the screen says instead of the overview: the tools looked for, or why Slotfather is unsupported."""
        source = self.source
        if source.state == "absent":
            where = "GODFATHER_FIXTURES" if _sources.fixtures() is not None else "PATH"
            return ["No tool found. Godfather looked for these on " + where + ":"] + [
                f"{tool}  {'found' if _sources.which(tool) else 'not found'}" for tool in _sources.TOOLS]
        if source.state == "unsupported":
            return [str(source.problem)]
        return None

    def settings(self, interval: float, theme: str) -> _settings.Settings:
        client = None
        if self.source.state in ("ok", "stale") and _slotfather.settings_supported(self.source.capabilities):
            client = SettingsClient(self.source.executable)
        return _settings.Settings(interval, theme, client=client, problem=lambda: self.source.problem,
                                  local_only=client is None)


DETAILS_FRESH = 5.0  # seconds a request's loaded details are shown as they are; older ones load again, quietly
PANELS = ("requests", "details", "events")  # Tab's order (#15)




def load_details(item: dict[str, Any]) -> tuple[Any, str | None]:
    """A request's slow facts, off the main thread (#15): its lane (a lanefather call) and its holder's directory (lsof
    or /proc). GODFATHER_DETAILS_DELAY, a test seam, adds seconds first."""
    with contextlib.suppress(ValueError):
        delay = float(os.environ.get("GODFATHER_DETAILS_DELAY") or 0)
        if delay > 0:
            time.sleep(delay)
    found = dict(item)
    _overview.enrich_details(found)  # the lane, through the same seam --once and the demo use
    cwd = _sources.process_cwd(item["pid"]) if item.get("kind") != "waiting" else None
    return found.get("lane"), cwd


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



def dashboard_live(interval: float, theme: str = _theme.DEFAULT_MODE, screen: Screen | None = None) -> None:
    fd_in, fd_out = sys.stdin.fileno(), sys.stdout.fileno()
    colour = not os.environ.get("NO_COLOR")
    # What the terminal says about its colours (#14) and how wide it draws 👨‍🦳 (#15): asked with colour off too
    answers = _theme.Answers()
    ascii_only = "utf" not in (sys.stdout.encoding or "").lower()
    screen = screen or Screen()
    model: dict[str, Any] | None = None
    error: str | None = None
    changelog_tab = 0
    selected_key: Any = None
    index, colours, shown, drawn_size = 0, {}, [], None
    scroll: dict[str, int] = {}
    focus, fresh_events, newest = "requests", 0, None
    help_open: bool | str = False  # True: help; "changelog": the changelog
    settings: _settings.Settings | None = None
    saved_choices = (interval, theme)  # Refresh every and Colour theme as saved; a change is written back
    drawn, whole, dirty = 0.0, 0.0, True
    glow, glowing = ScrollGlow(), set[str]()
    canvas = Canvas(0, 0)
    wake_read, wake_write = os.pipe()
    # Everything slow runs in the background, so a key moves the selection within one frame (#15)
    models, details, cache = Loader(screen.load, wake_write), Loader(load_details, wake_write), DetailsCache()
    models.request("model")
    fetched = time.monotonic()
    try:
        with full_screen(fd_in, fd_out, answers, _overview.MARKS["legacy"][0]):
            set_theme(_theme.from_environment(answers=answers, mode=theme))
            # 👨‍🦳 is one glyph where the cursor ended two cells on (column 3); without an answer, assume it is
            _overview.use_marks(ascii_only, joined=answers.column in (None, 3))
            while True:
                for _, value, failure in models.take():
                    if failure is not None:
                        error = dashboard_text(failure)
                    else:
                        fresh, error = value
                        if fresh is not model and fresh is not None:
                            _overview.dashboard_colours(fresh, colours)
                            fresh_events, newest = follow_events(scroll, fresh_events, newest,
                                                                 cast(list[dict[str, Any]], fresh["events"]))
                        model = fresh
                    dirty = True
                for key, value, failure in details.take():
                    lane, cwd = value if failure is None and value else (None, None)
                    cache.put(key, lane, cwd)
                    dirty = dirty or key == selected_key
                if settings is not None and settings.tick():  # an arrow change settled: saved, so show its effect now
                    interval, dirty = float(settings.interval), True
                    saved_choices = remember(saved_choices, settings)
                    models.request("model")
                    fetched = time.monotonic()
                if settings is not None and settings.theme != theme:  # this dashboard's theme, applied at once
                    theme = str(settings.theme)
                    shown, dirty = [], True
                    set_theme(_theme.from_environment(answers=answers, mode=theme))
                    saved_choices = remember(saved_choices, settings)
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
                    _overview.dashboard_draw(canvas, model, index, help_open, error, cwd, scroll, settings,
                                             changelog_tab, screen.notice())
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
                        saved_choices = remember(saved_choices, settings)
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
                            help_open, changelog_tab = "changelog", 0  # the footer's version opens what changed
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
                    if help_open == "changelog" and key in ("left", "right", "h", "l", "\t", "back_tab"):
                        step = -1 if key in ("left", "h", "back_tab") else 1  # the next tool's changelog
                        changelog_tab = (changelog_tab + step) % len(_overview.tabs())
                        scroll.pop("changelog", None)
                        continue
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
                        _overview.draw_help(canvas)
                    elif key == "v":  # the changelog, as a click on the footer's version
                        help_open, changelog_tab = "changelog", 0
                        scroll.pop("changelog", None)
                    elif key == "s":
                        try:
                            settings = screen.settings(interval, theme)
                        except SettingsError as failure:
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
            _overview.use_marks(ascii_only, joined=answers.column in (None, 3))
            changed = True
        if _theme.SCHEME_REPLY.match(report):
            os.write(fd_out, _theme.QUERY)
    return changed



def once(width: int | None, height: int | None, colour: bool | None, interval: float) -> int:
    """One frame of the overview, as `slotfather dashboard --once` printed it."""
    screen = Screen()
    model, error = screen.load()
    items = cast(list[dict[str, Any]], (model["holders"] + model["waiters"]) if model else [])
    cwd = None
    if model:
        _overview.dashboard_colours(model, {})
        if items:
            _overview.enrich_details(items[0])
            cwd = _sources.process_cwd(items[0]["pid"]) if items[0]["kind"] != "waiting" else None
    ascii_only = "utf" not in (sys.stdout.encoding or "").lower()
    _overview.use_marks(ascii_only)
    size_now = shutil.get_terminal_size((120, 40))
    canvas = Canvas(width or size_now.columns, height or size_now.lines)
    _overview.dashboard_draw(canvas, model, error=error, cwd=cwd, notice=screen.notice())
    tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    print(canvas.render(colour if colour is not None else tty, ascii_only))
    return 0


def command(argv: list[str]) -> int:
    interval, run_once, colour, width, height = None, False, None, None, None
    while argv:
        if argv[0] == "--once":
            run_once, argv = True, argv[1:]
        elif argv[0] == "--color":
            colour, argv = True, argv[1:]
        elif argv[0] in ("--version", "-V") and len(argv) == 1:
            print(_version.text(_version.running_identity()) if _version.running_source() else _version.VERSION)
            return 0
        elif argv[0] in ("--help", "-h"):
            print(USAGE)
            return 0
        elif argv[0] == "--page" and len(argv) > 1:
            if argv[1] != "overview":
                raise UsageError(f"--page {argv[1]}: only the overview has a page in this godfather")
            argv = argv[2:]
        elif argv[0] in ("--interval", "--width", "--height") and len(argv) > 1:
            try:
                value = float(argv[1])
            except ValueError:
                value = math.nan
            if argv[0] == "--interval":
                if not 0.1 <= value <= 60:
                    raise UsageError("--interval takes seconds from 0.1 to 60")
                interval = value
            elif not (value.is_integer() and 5 <= value <= 1000):
                raise UsageError(f"{argv[0]} takes a whole number of cells from 5 to 1000")
            elif argv[0] == "--width":
                width = int(value)
            else:
                height = int(value)
            argv = argv[2:]
        else:
            raise UsageError(f"unknown argument {argv[0]!r}\n{USAGE}")
    try:
        interval, theme = starting_choices(interval)
        set_theme(_theme.from_environment(mode=theme))  # --once cannot ask the terminal: GODFATHER_TERMINAL_BG/_FG
    except ValueError as error:
        raise UsageError(str(error)) from None
    if run_once:
        return once(width, height, colour, interval)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(USAGE, file=sys.stderr)
        print("godfather: the live view needs an interactive terminal; --once prints one frame", file=sys.stderr)
        return 2
    try:
        dashboard_live(interval, theme)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return command(list(sys.argv[1:] if argv is None else argv))
    except UsageError as error:
        print(f"godfather: {error}", file=sys.stderr)
        return 2
