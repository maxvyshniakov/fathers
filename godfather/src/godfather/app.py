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

from . import sources as _sources
from . import theme as _theme
from . import version as _version
from .adapters import slotfather as _slotfather
from .adapters.slotfather import SettingsClient, SettingsError, Unsupported
from .sources import SnapshotError, Source
from . import state as _state
from .tui import Canvas, Loader, full_screen, read_keys, set_theme
from .views import common as _common
from .views import details as _details
from .views import overlays as _overlays
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
        for name in ("holders", "waiters"):  # this frame's colours and lanes stay off the cached snapshot
            model[name] = [dict(item) for item in source.snapshot[name]]
        if source.state == "stale" and source.failed_at is not None:
            model["stale_since"] = datetime.datetime.fromtimestamp(source.failed_at, datetime.timezone.utc)
        return model, source.problem if source.state == "stale" else None

    def tabs(self) -> None:
        """The changelog overlay's Slotfather tab, from the changelog its capabilities name."""
        path = _slotfather.changelog(self.source.capabilities)
        tabs: list[_overlays.ChangelogTab] = []
        if path:
            with contextlib.suppress(OSError, UnicodeDecodeError):
                tabs.append(_overlays.ChangelogTab("Slotfather", Path(path).read_text(encoding="utf-8"),
                                                   str(self.source.version or "")))
        _overlays.changelog_tabs[:] = tabs

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
                                  local_only=client is None, background=True)


def load_details(item: dict[str, Any]) -> tuple[Any, str | None]:
    """A request's slow facts, off the main thread (#15): its lane (a lanefather call) and its holder's directory (lsof
    or /proc). GODFATHER_DETAILS_DELAY, a test seam, adds seconds first."""
    with contextlib.suppress(ValueError):
        delay = float(os.environ.get("GODFATHER_DETAILS_DELAY") or 0)
        if delay > 0:
            time.sleep(delay)
    found = dict(item)
    _details.enrich_details(found)  # the lane, through the same seam --once and the demo use
    cwd = _sources.process_cwd(item["pid"]) if item.get("kind") != "waiting" else None
    return found.get("lane"), cwd


class Live:
    """The live view's loop around the one state and its reducer (state.py): what the terminal, the loaders and the
    clock say goes in as events; what reduce() asks for is carried out here; the screen is drawn from the state,
    only the rows that changed written."""

    def __init__(self, fd_in: int, fd_out: int, screen: Screen, interval: float, theme: str) -> None:
        self.fd_in, self.fd_out, self.screen = fd_in, fd_out, screen
        self.colour = not os.environ.get("NO_COLOR")
        # What the terminal says about its colours (#14) and how wide it draws 👨‍🦳 (#15): asked with colour off too
        self.answers = _theme.Answers()
        self.ascii_only = "utf" not in (sys.stdout.encoding or "").lower()
        self.state = _state.AppState(interval=interval, theme=theme)
        self.saved = (interval, theme)  # Refresh every and Colour theme as saved; a change is written back
        self.shown: list[str] = []
        self.drawn_size: Any = None
        self.drawn = self.whole = 0.0
        self.glowing: set[str] = set()  # the thumbs drawn in the foreground
        self.dirty, self.done = True, False
        self.wake_read, self.wake_write = os.pipe()
        # Everything slow runs in the background, so a key moves the selection within one frame (#15)
        self.models = Loader(screen.load, self.wake_write)
        self.details = Loader(load_details, self.wake_write)
        self.fetched = 0.0

    def close(self) -> None:
        for fd in (self.wake_read, self.wake_write):
            os.close(fd)

    def apply_theme(self) -> None:
        set_theme(_theme.from_environment(answers=self.answers, mode=self.state.theme))

    def send(self, event: _state.Event) -> None:
        """One event through the reducer, and its effects carried out; any change is drawn at the next pass."""
        before = self.state
        self.state, effects = _state.reduce(self.state, event, time.monotonic())
        self.dirty = self.dirty or self.state is not before
        for effect in effects:
            self.carry_out(effect)

    def carry_out(self, effect: _state.Effect) -> None:
        kind = effect.kind
        if kind == "quit":
            self.done = True
        elif kind == "load-model":
            self.load_model()
        elif kind == "load-details":
            if not self.details.wants(effect.key):
                self.details.request(effect.key, effect.item)
        elif kind == "apply-theme":
            self.apply_theme()
        elif kind == "repaint":
            self.shown, self.dirty = [], True
        elif kind == "remember":
            current = (self.state.interval, self.state.theme)
            if current != self.saved:
                save_choices(*current)
                self.saved = current
        elif kind == "open-settings":
            try:
                panel = self.screen.settings(self.state.interval, self.state.theme)
            except SettingsError as failure:
                self.send(_state.SettingsFailed(str(failure)))
                return
            size = os.get_terminal_size(self.fd_out)
            panel.draw(Canvas(size.columns, size.lines), self.state.model)  # its hit-test geometry, before a click
            self.send(_state.SettingsOpened(panel))

    def load_model(self) -> None:
        self.models.request("model")
        self.fetched = time.monotonic()

    def take(self) -> None:
        """What the loaders finished, and the clock: a model polled every Refresh every."""
        for _, value, failure in self.models.take():
            if failure is not None:
                self.send(_state.ModelFailed(str(failure)))
            else:
                model, problem = value
                self.send(_state.ModelLoaded(model, problem))
        for key, value, failure in self.details.take():
            lane, cwd = value if failure is None and value else (None, None)
            self.send(_state.DetailsLoaded(key, lane, cwd, None if failure is None else str(failure) or
                                           type(failure).__name__))
        self.send(_state.Tick())
        if time.monotonic() - self.fetched >= self.state.interval and not self.models.wants("model"):
            self.load_model()

    def draw(self) -> bool:
        """The screen when something changed, a second passed or the details' spinner turns; only the rows that
        changed are written. True while the spinner turns."""
        size = os.get_terminal_size(self.fd_out)  # read each pass: at most 0.25 s behind a resize
        if size != self.drawn_size:
            # A terminal reflows its own screen on resize: clear it and draw every row.
            os.write(self.fd_out, b"\x1b[2J")
            self.shown, self.drawn_size, self.dirty = [], size, True
        elif time.monotonic() - self.whole >= 5:
            self.shown = []  # rewrite every row now and then, in place, so a stray line cannot stay wrong
        now = time.monotonic()
        glowing = _state.glowing(self.state, now)
        if glowing != self.glowing:  # a thumb lit up or faded back
            self.glowing, self.dirty = glowing, True
        loading = self.loading()
        if not (self.dirty or loading or now - self.drawn >= 1 or not self.shown):
            return loading
        canvas = Canvas(size.columns, size.lines)
        _overview.render(canvas, self.state, now, self.screen.notice())
        self.send(_state.Drawn(_state.Frame(canvas.width, canvas.height, canvas.tables, canvas.version_at,
                                            tuple(canvas.hint_hits))))
        lines = canvas.lines(self.colour, self.ascii_only, place=True)
        # Only rows that changed are written: a second's tick usually touches the clock and a few ages.
        frame = "".join(f"\x1b[{row + 1};1H{line}" for row, line in enumerate(lines)
                        if row >= len(self.shown) or self.shown[row] != line)
        if frame:
            os.write(self.fd_out, frame.encode("utf-8", "replace"))
        self.whole = time.monotonic() if not self.shown else self.whole
        self.shown, self.drawn, self.dirty = lines, time.monotonic(), False
        return loading

    def loading(self) -> bool:
        """The selected request's details are loading: the spinner turns at ten frames a second."""
        found = _state.items(self.state)
        if not found:
            return False
        item = found[self.state.selection.index]
        return _state.details_key(item, cast(Any, self.state.selection.key)) not in self.state.details

    def read(self, wait: float) -> None:
        """Waits for a key, a loader or the moment a thumb fades, then takes the keys and what the terminal said."""
        fade = _state.fade(self.state, time.monotonic())  # one wake when a scrolled thumb goes back, not a poll
        ready = select.select([self.fd_in, self.wake_read], [], [], wait if fade is None else min(wait, fade))[0]
        if self.wake_read in ready:
            os.read(self.wake_read, 4096)
        if self.fd_in not in ready:
            return
        reports: list[str] = []
        pressed = read_keys(self.fd_in, reports)
        if reports and theme_reports(reports, self.answers, self.fd_out, self.ascii_only):
            self.apply_theme()
            self.shown, self.dirty = [], True  # every row's colours changed
        for key in pressed:
            self.dirty = True
            self.send(_state.Mouse(key[1], key[2], key[3], key[4]) if isinstance(key, tuple) else _state.Key(key))
            if self.done:
                return

    def run(self) -> None:
        with full_screen(self.fd_in, self.fd_out, self.answers, _common.MARKS["legacy"][0]):
            self.apply_theme()
            # 👨‍🦳 is one glyph where the cursor ended two cells on (column 3); without an answer, assume it is
            _common.use_marks(self.ascii_only, joined=self.answers.column in (None, 3))
            self.load_model()
            while not self.done:
                self.take()
                loading = self.draw()
                self.read(0.1 if loading else 0.25)


def dashboard_live(interval: float, theme: str = _theme.DEFAULT_MODE, screen: Screen | None = None) -> None:
    live = Live(sys.stdin.fileno(), sys.stdout.fileno(), screen or Screen(), interval, theme)
    try:
        live.run()
    finally:
        live.close()


def theme_reports(reports: list[str], answers: _theme.Answers, fd_out: int, ascii_only: bool = False) -> bool:
    """Takes what the terminal reported; True when a colour or the width of 👨‍🦳 changed: everything is drawn
    again. A colour-scheme change notification (mode 2031) asks for the colours again: their answers arrive with
    later keys. A width reply later than ask() waited for (a slow link) still picks the glyph (#15)."""
    changed = False
    for report in reports:
        column = answers.column
        changed = answers.feed(report) or changed
        if answers.column != column:
            _common.use_marks(ascii_only, joined=answers.column in (None, 3))
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
        _common.dashboard_colours(model, {})
        if items:
            _details.enrich_details(items[0])
            cwd = _sources.process_cwd(items[0]["pid"]) if items[0]["kind"] != "waiting" else None
    ascii_only = "utf" not in (sys.stdout.encoding or "").lower()
    _common.use_marks(ascii_only)
    size_now = shutil.get_terminal_size((120, 40))
    canvas = Canvas(width or size_now.columns, height or size_now.lines)
    _overview.dashboard_draw(canvas, model, error=error, cwd=cwd, notice=screen.notice())
    tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    print(canvas.render(colour if colour is not None else tty, ascii_only))
    return 1 if model is None else 0  # the frame says why: no tool, no contract, or a failed snapshot


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
