"""godfather: one terminal dashboard for the tools that share a development machine.

  godfather [--interval S]                                         live view: ? lists the keys, q quits
  godfather --once [--page overview|lanes|processes] [--width N] [--height N] [--color]   one frame as text
  godfather events [--source NAME]... [--follow] [--json]          the tools' events, newest last
  godfather --version

The main screen is Slotfather's dashboard, moved here unchanged: it is drawn from `slotfather status --json --full`
and Settings goes through each present tool's `settings` CLI (SPEC.md, Settings contract). Everything slow runs in the
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
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

from . import actions as _actions
from . import entries as _entries
from . import events as _events
from . import settings_model as _settings_model
from . import news as _news
from . import sources as _sources
from . import theme as _theme
from . import version as _version
from .adapters import junkfather as _junkfather
from .adapters import lanefather as _lanefather
from .adapters import settings as _settings_adapter
from .adapters import slotfather as _slotfather
from .adapters.settings import Conflict, ReadOnly, SettingsClient, SettingsError
from .adapters.slotfather import Unsupported
from .model import Dashboard
from .sources import SnapshotError, Source
from . import pages as _pages
from . import reducer as _reducer
from . import state as _state
from .tui import Canvas, Jobs, Loader, full_screen, read_keys, suspended
from .views import common as _common
from .views import details as _details
from .views import events as _events_view
from .views import overview as _overview

USAGE = ("usage: godfather [--interval SECONDS]\n"
         "       godfather --once [--page overview|lanes|processes] [--width N] [--height N] [--color]\n"
         "       godfather events [--source NAME]... [--follow] [--json]\n"
         "       godfather --version")


class UsageError(Exception):
    """A command line Godfather does not take (exit 2, docs/cli.md): the reason, then one line pointing to the help of
    `command` (None: Godfather's own)."""

    def __init__(self, reason: str, command: str | None = None) -> None:
        super().__init__(reason)
        self.command = command


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

    def __init__(self, source: Source | None = None, others: list[Source] | None = None) -> None:
        self.source = source or Source("slotfather", _slotfather)
        # The other tools EVENTS reads (fathers#84): only their event logs, until their pages come
        self.others = others if others is not None else [Source(name, _events.EventsContract(name))
                                                         for name in _sources.TOOLS if name != "slotfather"]
        self.feed = _events.Feed()
        # Settings' own view of the tools (fathers#87 review): discovered and read on Settings' worker alone, never on
        # the screen's thread nor on the Source objects the model loader uses
        self.settings_found = [Source(name, _settings_adapter.Contract(name)) for name in _sources.TOOLS]
        self.rows: list[dict[str, Any]] = []
        self.followed: tuple[tuple[str, str], ...] = ()
        # The other tools as sources (fathers#85): each polled on its own worker, on Source objects of its own
        self.tool_sources = {name: Source(name, adapter) for name, adapter in TOOL_ADAPTERS.items()}
        # The action worker's own view of every tool (fathers#88 review): its re-checks never touch the loaders'
        self.action_sources = {"slotfather": Source("slotfather", _slotfather),
                               **{name: Source(name, adapter) for name, adapter in TOOL_ADAPTERS.items()}}
        self.fresh: set[str] = set()  # the sources whose log gained a line in the last load: polled again soon

    def follow(self) -> tuple[tuple[str, str], ...]:
        """Every present source with an event log followed, the others not: (name, tag) each, in TOOLS order."""
        found: list[tuple[str, str]] = []
        for source in [self.source, *self.others]:
            if source is not self.source and source.due():
                source.discover()
            log = _events.log_path(source.capabilities) if source.state in ("ok", "stale") else None
            self.feed.follow(source.name, str(_sources.event_log(source.name, log)) if log else None)
            if log:
                found.append((source.name, source.tag))
        return tuple(found)

    def events(self) -> list[dict[str, Any]]:
        """EVENTS' rows from every followed log, built again only when a log gained a line or a source came or
        went."""
        followed = self.follow()
        polled = self.feed.poll()
        self.fresh = {event.source for event in polled}
        if polled or followed != self.followed:
            self.followed = followed
            self.rows = _events.rows(self.feed.events(), dict(followed))
        return self.rows

    def load(self, _: Any = None) -> tuple[Dashboard | None, str | None]:
        """(the model to draw, the refresh error for the footer); discovery first when it is due."""
        source = self.source
        if source.due():
            source.discover()
        if source.state in ("absent", "unsupported"):
            return None, None
        try:
            source.took(_slotfather.decode(source.poll()), None)
        except (SnapshotError, Unsupported) as error:
            source.took(None, str(error))
        if source.snapshot is None:
            return None, source.problem
        model = cast(Dashboard, dict(source.snapshot))  # decode() made it (fathers#142); a copy to add this load's
        model["installed"] = _version.installed_source()
        # EVENTS (fathers#84): every source's log, never the snapshot's described events; a Slotfather whose
        # capabilities name no log shows none and says why
        model["events"] = self.events()
        model["sources"] = list(self.followed)
        model["events_skipped"] = self.feed.skipped()
        model["fresh_sources"] = sorted(self.fresh)  # an event polls its source at once, debounced (SPEC 9)
        model["uid"] = os.geteuid()  # whose requests can be reprioritized (fathers#88)
        model["actions"] = list(_actions.offered(_lanefather.dashboard_block(source.capabilities).get("actions")))
        if not _events.log_path(source.capabilities):  # no event log of a version this Godfather reads: why
            named = isinstance(_events.dashboard_block(source.capabilities).get("events"), dict)
            model["events_note"] = _events_view.TOO_NEW if named else _events_view.TOO_OLD
        # This frame's colours and lanes stay off the cached snapshot
        model["holders"] = [item.copy() for item in model["holders"]]
        model["waiters"] = [item.copy() for item in model["waiters"]]
        if source.state == "stale" and source.failed_at is not None:
            model["stale_since"] = datetime.datetime.fromtimestamp(source.failed_at, datetime.timezone.utc)
        return model, source.problem if source.state == "stale" else None

    def load_tool(self, name: str) -> _state.Tool:
        """Another tool's snapshot, polled once (discovery first when it is due): its Tool for the reducer."""
        source = self.tool_sources[name]
        if source.due():
            source.discover()
        adapter = TOOL_ADAPTERS[name]
        if source.state == "absent":
            return _state.Tool(name)
        if source.state == "unsupported":
            return _state.Tool(name, "unsupported", source.problem)
        try:
            source.took(adapter.decode(source.poll()), None)
        except SnapshotError as error:
            source.took(None, str(error))
        since = datetime.datetime.fromtimestamp(source.failed_at, datetime.timezone.utc) \
            if source.state == "stale" and source.failed_at is not None else None
        return _state.Tool(name, source.state, source.problem, source.snapshot, since,
                           adapter.actions(source.capabilities), adapter.show_supported(source.capabilities))

    def revalidated(self, action: _actions.Action) -> _actions.Action | None:
        """The action as its row offers it in a fresh snapshot of its source (SPEC 23, step 3), read on the action
        worker's own Source objects, never the loaders' (fathers#88 review); None where the row is gone, changed or
        no longer in the state the action needs."""
        page = action.identity[0] if action.identity else ""
        name = {"overview": "slotfather", "processes": "junkfather", "lanes": "lanefather"}.get(page)
        if name is None:
            return None
        source = self.action_sources[name]
        if source.due():
            source.discover()
        if source.state in ("absent", "unsupported"):
            return None
        try:
            adapter = _slotfather if name == "slotfather" else TOOL_ADAPTERS[name]
            return _actions.revalidate(action, adapter.decode(source.poll()), os.geteuid())
        except (SnapshotError, Unsupported):
            return None

    def act(self, action: _actions.Action, reason: str) -> _state.ActionDone | _state.HandOff:
        """An action: the row checked again, then the command run (30 s, GODFATHER_ACTOR); the tool's first stdout
        line on success, its first stderr line on failure, never a retry. An opener is started and left to run
        (`xdg-open` may wait for the program it opens); the pager is handed back to the screen to run."""
        key = _state.action_key(action)
        fresh = self.revalidated(action)
        if fresh is None:
            return _state.ActionDone(action.tool, False, f"{action.name}: the row changed or is gone; nothing run",
                                     key)
        if fresh.kind == "pager":
            return _state.HandOff(fresh, key)
        executable = self.action_sources[fresh.tool].executable if fresh.tool in self.action_sources else None
        argv = list(fresh.command(reason)[1:] if fresh.tool else fresh.command(reason))
        reply = _sources.act(fresh.tool, argv, ACTOR, executable, detach=fresh.kind == "open")
        first = next((line for line in (reply.stdout if reply.code == 0 else reply.stderr).splitlines()
                      if line.strip()), "")
        if reply.code != 0:
            return _state.ActionDone(fresh.tool, False, first or f"{fresh.shown(reason)} exited {reply.code}", key)
        return _state.ActionDone(fresh.tool, True, first or f"{fresh.name}: done", key)

    def poll_every(self, name: str) -> float:
        """Seconds between a tool's polls: its capabilities' hint clamped to 5–10 (SPEC 9)."""
        snapshot: Any = _lanefather.dashboard_block(self.tool_sources[name].capabilities).get("snapshot")
        hint: Any = cast(dict[str, Any], snapshot).get("poll") if isinstance(snapshot, dict) else None
        return float(min(10, max(5, hint))) if isinstance(hint, (int, float)) and not isinstance(hint, bool) else 5.0

    def load_details(self, item: dict[str, Any]) -> tuple[Any, str | None]:
        """A request's slow facts, off the main thread (#15): its holder's directory (lsof or /proc) and, where
        Lanefather's status snapshot does not join it to a lane, its lane from `lanefather show` when Lanefather
        advertises it (fathers#85). GODFATHER_DETAILS_DELAY, a test seam, adds seconds first."""
        return load_details(item, self.tool_sources["lanefather"])

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

    def sources(self) -> list[Source]:
        return [self.source, *self.others]

    def settings_client(self, tool: str) -> SettingsClient:
        source = next((found for found in self.settings_found if found.name == tool), None)
        return SettingsClient(source.executable if source else None, tool)

    def settings_sections(self) -> list[_settings_model.Section]:
        """Settings as it opens (fathers#87): a section for each present tool, read once now; one without a settings
        contract this Godfather reads says why in its section, an absent one has none."""
        found: list[_settings_model.Section] = []
        for source in self.settings_found:
            if source.due():
                source.discover()
            name = source.name
            if source.state == "absent":
                continue
            if source.state == "unsupported" or not _settings_adapter.supported(source.capabilities):
                why = source.problem if source.state == "unsupported" else \
                    f"{name} {source.version or ''}: no settings contract; update {name}".replace(" :", ":")
                found.append(_settings_model.Section(name, problem=why, readable=False))
                continue
            try:
                found.append(_settings_model.Section(name, reply=self.settings_client(name).read()))
            except SettingsError as error:
                found.append(_settings_model.Section(name, problem=str(error)))
        return found

    def settings_call(self, request: _settings_model.Request) -> _state.Event:
        """One settings call for the panel, answered as the event the reducer takes."""
        if request.kind == "open":
            return _state.SettingsSections(tuple(self.settings_sections()))
        client = self.settings_client(request.tool)
        if request.kind == "read":
            try:
                return _state.SettingsRead(request.tool, request.generation, client.read())
            except SettingsError as error:
                return _state.SettingsRead(request.tool, request.generation, None, str(error))
        if request.kind == "check":
            try:
                reply = client.check(str(request.key), str(request.value))
            except SettingsError as error:
                reply = {"ok": False, "error": str(error), "preview": None}
            return _state.SettingsChecked(request.tool, str(request.key), str(request.value), request.revision, reply)
        try:
            if request.kind == "set":
                reply = client.set(str(request.key), str(request.value), request.revision)
            else:
                reply = client.reset(request.key, request.revision)
        except Conflict as conflict:
            return _state.SettingsWritten(request, "conflict", conflict.reply, str(conflict))
        except ReadOnly as refusal:
            return _state.SettingsWritten(request, "read-only", None, str(refusal))
        except SettingsError as error:
            return _state.SettingsWritten(request, "refused", None, str(error))
        return _state.SettingsWritten(request, "ok", reply)


def settings_failed(request: _settings_model.Request, failure: BaseException) -> _state.Event:
    """A settings call that raised what settings_call() does not expect: its answer, a failure."""
    why = str(failure) or type(failure).__name__
    if request.kind == "open":
        return _state.SettingsSections((), why)
    if request.kind == "read":
        return _state.SettingsRead(request.tool, request.generation, None, why)
    if request.kind == "check":
        return _state.SettingsChecked(request.tool, str(request.key), str(request.value), request.revision,
                                      {"ok": False, "error": why, "preview": None})
    return _state.SettingsWritten(request, "failed", None, why)


TOOL_ADAPTERS: dict[str, Any] = {"lanefather": _lanefather, "junkfather": _junkfather}  # the other tools, by name


def load_details(item: dict[str, Any], lanes: Source | None = None) -> tuple[Any, str | None]:
    """A request's slow facts, off the main thread (#15): its holder's directory (lsof or /proc) and its lane from
    `lanefather show` where Lanefather's status snapshot (`lanes`, its source) does not join it already.
    GODFATHER_DETAILS_DELAY, a test seam, adds seconds first."""
    with contextlib.suppress(ValueError):
        delay = float(os.environ.get("GODFATHER_DETAILS_DELAY") or 0)
        if delay > 0:
            time.sleep(delay)
    found = dict(item)
    cwd = _sources.process_cwd(item["pid"]) if item.get("kind") != "waiting" else None
    snapshot = lanes.snapshot if lanes is not None else None
    if _lanefather.lane_of(found, snapshot, cwd) is None:
        _details.enrich_details(found, lanes.capabilities if lanes is not None else None)
    return found.get("lane"), cwd


def load_news(_: Any = None) -> _news.News:
    """What's new from every tool, off the main thread."""
    return _news.load()


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
        self.theme = _theme.from_environment(answers=self.answers, mode=theme)  # each canvas is drawn in it
        self.state = _state.AppState(interval=interval, theme=theme, read=_news.load_read())
        self.saved = (interval, theme)  # Refresh every and Colour theme as saved; a change is written back
        self.shown: list[str] = []
        self.drawn_size: Any = None
        self.resized = True  # SIGWINCH came (fathers#143): the size is read again at the next pass
        self.drawn = 0.0
        self.glowing: set[str] = set()  # the thumbs drawn in the foreground
        self.dirty, self.done = True, False
        self.wake_read, self.wake_write = os.pipe()
        # Everything slow runs in the background, so a key moves the selection within one frame (#15)
        self.models = Loader(screen.load, self.wake_write)
        self.details = Loader(screen.load_details, self.wake_write)
        # Lanefather's snapshot (fathers#85) on a worker of its own, every 5–10 s and soon after one of its events
        self.tools = {name: Loader(screen.load_tool, self.wake_write) for name in TOOL_ADAPTERS}
        self.tool_fetched = {name: -math.inf for name in TOOL_ADAPTERS}
        self.tool_due: dict[str, float] = {}  # an event's poll, debounced: when it runs
        self.news = Loader(load_news, self.wake_write)  # what's new: on start and each time it opens
        self.settings = Jobs(self.settings_work, self.wake_write)  # Settings' calls, in order (fathers#87)
        self.acting = Jobs(self.action_work, self.wake_write)  # row actions, in order, in the background (fathers#88)
        self.asked = 0
        self.fetched = 0.0

    def close(self) -> None:
        for fd in (self.wake_read, self.wake_write):
            os.close(fd)

    def apply_theme(self) -> None:
        self.theme = _theme.from_environment(answers=self.answers, mode=self.state.theme)

    def marks(self) -> _common.Marks:
        return terminal_marks(self.ascii_only, self.answers)

    def canvas(self, width: int, height: int) -> Canvas:
        return Canvas(width, height, self.theme, self.marks())

    def send(self, event: _state.Event) -> None:
        """One event through the reducer, and its effects carried out; any change is drawn at the next pass."""
        before = self.state
        self.state, effects = _reducer.reduce(self.state, event, time.monotonic())
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
        elif kind == "load-news":
            self.news.request("news")
        elif kind == "save-read":
            _news.save_read(self.state.read)
        elif kind == "apply-theme":
            self.apply_theme()
        elif kind == "repaint":
            self.shown, self.dirty = [], True
        elif kind == "remember":
            current = (self.state.interval, self.state.theme)
            if current != self.saved:
                save_choices(*current)
                self.saved = current
        elif kind == "settings":
            self.asked += 1
            self.settings.request(self.asked, effect.item)
        elif kind == "action":
            action, reason = effect.item
            self.asked += 1  # checked on the worker first; the pager comes back as a hand-off
            self.acting.request(self.asked, (action, reason))
        elif kind == "hand-off":  # the terminal is the pager's until it quits
            self.send(self.page_file(*effect.item))
        elif kind == "poll":
            if effect.key == "slotfather":
                self.load_model()
            elif effect.key in self.tools:
                self.tool_due[effect.key] = time.monotonic()
        elif kind == "open-settings":  # at once; the tools' sections come from Settings' worker (fathers#87 review)
            self.send(_state.SettingsOpened((), loading=True))

    def action_work(self, job: tuple[_actions.Action, str]) -> _state.Event:
        try:
            return self.screen.act(*job)
        except Exception as failure:  # noqa: BLE001 - a failed action is a footer line, never the end of the view
            return _state.ActionDone(job[0].tool, False, str(failure) or type(failure).__name__,
                                     _state.action_key(job[0]))

    def page_file(self, action: _actions.Action, key: tuple[Any, ...]) -> _state.ActionDone:
        """The latest log in $PAGER, already checked by the action worker: the screen handed over and taken back
        (kitty keyboard mode too), GODFATHER_ACTOR in its environment, no time limit (it is read for as long as the
        reader wants). Under GODFATHER_FIXTURES its argv is recorded, never run."""
        try:
            command = list(action.command())
        except ValueError as error:  # a malformed $PAGER
            return _state.ActionDone("", False, f"PAGER: {error}", key)
        if _sources.fixtures() is not None:
            _sources.act("", command, ACTOR)
            return _state.ActionDone("", True, f"recorded: {' '.join(command)}", key)
        with suspended(self.fd_in, self.fd_out, self.answers):
            try:
                code = subprocess.call(command, env=dict(os.environ, GODFATHER_ACTOR=ACTOR))
            except OSError as error:
                code, why = -1, f"cannot run {command[0]}: {error.strerror or error}"
            else:
                why = "" if code == 0 else f"{command[0]} exited {code}"
        self.shown, self.dirty = [], True
        return _state.ActionDone("", code == 0, why, key)

    def settings_work(self, request: _settings_model.Request) -> _state.Event:
        try:
            return self.screen.settings_call(request)
        except Exception as failure:  # noqa: BLE001 - a failed call is shown in Settings, never the end of the view
            return settings_failed(request, failure)

    def load_model(self) -> None:
        self.models.request("model")
        self.fetched = time.monotonic()

    def take(self) -> None:
        """What the loaders finished, and the clock: a model polled every Refresh every."""
        now = time.monotonic()
        for _, value, failure in self.models.take():
            if failure is not None:
                self.send(_state.ModelFailed(str(failure)))
            else:
                model, problem = value
                fresh = cast(dict[str, Any], model or {}).get("fresh_sources")
                for name in cast(list[str], fresh or []):
                    if name in self.tools:
                        self.tool_due[name] = now + TOOL_DEBOUNCE
                self.send(_state.ModelLoaded(model, self.theme.holders(), problem))
        for name, loader in self.tools.items():
            for _, tool, failure in loader.take():
                self.send(_state.ToolLoaded(tool if failure is None else _state.Tool(name, "stale", str(failure))))
            due = self.tool_due.get(name)
            if (now - self.tool_fetched[name] >= self.screen.poll_every(name) or (due is not None and now >= due)) \
                    and not loader.wants(name):
                self.tool_due.pop(name, None)
                self.tool_fetched[name] = now
                loader.request(name, name)
        for _, answer, _ in self.acting.take():
            if answer is not None:
                self.send(answer)
        for _, answer, _ in self.settings.take():
            if answer is not None:
                self.send(answer)
        for _, value, failure in self.news.take():
            if failure is None:  # a failed load leaves what's new as it was
                self.send(_state.NewsLoaded(value))
        for key, value, failure in self.details.take():
            lane, cwd = value if failure is None and value else (None, None)
            self.send(_state.DetailsLoaded(key, lane, cwd, None if failure is None else str(failure) or
                                           type(failure).__name__))
        self.send(_state.Tick(datetime.datetime.now(datetime.timezone.utc)))
        if time.monotonic() - self.fetched >= self.state.interval and not self.models.wants("model"):
            self.load_model()

    def on_resize(self, *_: Any) -> None:
        """SIGWINCH: the size is read again, and the loop woken to draw it now."""
        self.resized = True
        with contextlib.suppress(OSError):  # a full pipe already wakes the loop
            os.write(self.wake_write, b"w")

    @contextlib.contextmanager
    def resizes(self) -> Any:
        """SIGWINCH handled by on_resize() while inside; the previous handler back on the way out."""
        previous = signal.signal(signal.SIGWINCH, self.on_resize)
        try:
            yield
        finally:
            signal.signal(signal.SIGWINCH, previous)

    def draw(self) -> bool:
        """The screen when something changed, a second passed or the details' spinner turns; only the rows that
        changed are written, all of them in one synchronized update (fathers#143). Every row is written again only
        after a resize, a theme change or Ctrl-L. True while the spinner turns."""
        clear = b""
        if self.resized:
            self.resized = False  # before the read: a resize meanwhile is read at the next pass
            size = terminal_size(self.fd_out)
            if size != self.drawn_size:
                # A terminal reflows its own screen on resize: clear it and draw every row.
                clear = b"\x1b[2J"
                self.shown, self.drawn_size, self.dirty = [], size, True
        size = self.drawn_size
        now = time.monotonic()
        glowing = _state.glowing(self.state, now)
        if glowing != self.glowing:  # a thumb lit up or faded back
            self.glowing, self.dirty = glowing, True
        loading = self.loading()
        if not (self.dirty or loading or now - self.drawn >= 1 or not self.shown):
            return loading
        canvas = self.canvas(size.columns, size.lines)
        _overview.render(canvas, self.state, now, self.screen.notice())
        self.send(_state.Drawn(_state.Frame(canvas.width, canvas.height, canvas.tables, canvas.version_at,
                                            tuple(canvas.hint_hits), canvas.order, tuple(canvas.filter_hits),
                                            canvas.switch_at, tuple(canvas.targets))))
        lines = canvas.lines(self.colour, self.ascii_only, place=True)
        # Only rows that changed are written: a second's tick usually touches the clock and a few ages.
        frame = "".join(f"\x1b[{row + 1};1H{line}" for row, line in enumerate(lines)
                        if row >= len(self.shown) or self.shown[row] != line)
        if frame or clear:
            os.write(self.fd_out, SYNC_ON + clear + frame.encode("utf-8", "replace") + SYNC_OFF)
        self.shown, self.drawn, self.dirty = lines, time.monotonic(), False
        return loading

    def loading(self) -> bool:
        """The selected request's details are loading: the spinner turns at ten frames a second."""
        found = _state.items(self.state)
        if not found or self.state.shows == "events":  # DETAILS shows an event (fathers#152): nothing loads
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
        if reports and theme_reports(reports, self.answers, self.fd_out):
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
            self.load_model()
            self.news.request("news")
            with self.resizes():
                self.loop()

    def loop(self) -> None:
        while not self.done:
            self.take()
            loading = self.draw()
            self.read(0.1 if loading else 0.25)


# A frame as one synchronized update (fathers#143): the terminal paints it whole; one without mode 2026 ignores both
SYNC_ON, SYNC_OFF = b"\x1b[?2026h", b"\x1b[?2026l"


def terminal_size(fd: int) -> os.terminal_size:
    return os.get_terminal_size(fd)


ACTOR = f"godfather/{_version.VERSION}"  # GODFATHER_ACTOR for every action (SPEC 24)
TOOL_DEBOUNCE = 0.25  # seconds after a tool's event before its snapshot is polled (SPEC 9)


def dashboard_live(interval: float, theme: str = _theme.DEFAULT_MODE, screen: Screen | None = None) -> None:
    live = Live(sys.stdin.fileno(), sys.stdout.fileno(), screen or Screen(), interval, theme)
    try:
        live.run()
    finally:
        live.close()


def terminal_marks(ascii_only: bool, answers: _theme.Answers) -> _common.Marks:
    """The marks this terminal draws: ASCII without UTF-8; 👨‍🦳 is one glyph where the cursor ended two cells on
    (column 3), and without an answer it is assumed to be."""
    return _common.marks_for(ascii_only, joined=answers.column in (None, 3))


def theme_reports(reports: list[str], answers: _theme.Answers, fd_out: int) -> bool:
    """Takes what the terminal reported; True when a colour or the width of 👨‍🦳 changed: everything is drawn
    again. A colour-scheme change notification (mode 2031) asks for the colours again: their answers arrive with
    later keys. A width reply later than ask() waited for (a slow link) still picks the glyph (#15): the marks
    follow `answers` (Live.marks())."""
    changed = False
    for report in reports:
        column = answers.column
        changed = answers.feed(report) or changed
        if answers.column != column:
            changed = True
        if _theme.SCHEME_REPLY.match(report):
            os.write(fd_out, _theme.QUERY)
    return changed



def once(width: int | None, height: int | None, colour: bool | None, interval: float,
         theme: _theme.Theme | None = None, page: str = "overview") -> int:
    """One frame of the overview, as `slotfather dashboard --once` printed it, in `theme`; or of a page (fathers#85),
    with the row the first request links to selected, else its first row. A page whose tool is absent is the
    overview, as its key would leave it."""
    theme = theme if theme is not None else _theme.from_environment()
    screen = Screen()
    model, error = screen.load()
    items = cast(list[dict[str, Any]], (model["holders"] + model["waiters"]) if model else [])
    tools = {name: screen.load_tool(name) for name in TOOL_ADAPTERS}
    cwd = None
    if model:
        _entries.dashboard_colours(model, {}, theme.holders())
        if items:
            cwd = _sources.process_cwd(items[0]["pid"]) if items[0]["kind"] != "waiting" else None
            lanes = screen.tool_sources["lanefather"]
            if _lanefather.lane_of(items[0], lanes.snapshot, cwd) is None:
                _details.enrich_details(items[0], lanes.capabilities)
    state = _pages.open_page(_state.settle(_state.AppState(model=model, tools=tools), 0.0)[0], page)
    ascii_only = "utf" not in (sys.stdout.encoding or "").lower()
    size_now = shutil.get_terminal_size((120, 40))
    canvas = Canvas(width or size_now.columns, height or size_now.lines, theme, _common.marks_for(ascii_only))
    canvas.focus = state.focus
    _overview.dashboard_draw(canvas, model, error=error, cwd=cwd, notice=screen.notice(),
                             moment=datetime.datetime.now(datetime.timezone.utc), tools=tools, page=state.page,
                             page_row=_pages.page_index(state, state.page) if state.page != "overview" else None)
    tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    print(canvas.render(colour if colour is not None else tty, ascii_only))
    return 1 if model is None else 0  # the frame says why: no tool, no contract, or a failed snapshot


def event_text(event: _events.Event, tag: str) -> str:
    """One `godfather events` line: the local time, the source tag, the kind, the subject's label or id and the
    one-line detail EVENTS shows. The validator let no control character through; none is printed whatever a line held."""
    shown = _events.shown(event)  # the detail EVENTS shows (fathers#84), from the event's fields
    detail = "".join(text for text, _ in shown.detail)
    fields = [event.when.astimezone().strftime("%Y-%m-%d %H:%M:%S"), f"{tag:<4}", f"{event.kind:<22}",
              shown.label or _events.subject_label(event), detail]
    return _events.CONTROL.sub(" ", "  ".join(field for field in fields if field).rstrip())


def split_options(argv: list[str]) -> list[str]:
    """`--opt=value` as `--opt value` (docs/cli.md): every long option that takes a value takes both forms."""
    found: list[str] = []
    for arg in argv:
        if arg.startswith("--") and "=" in arg:
            found += arg.split("=", 1)
        else:
            found.append(arg)
    return found


def events_command(argv: list[str]) -> int:
    """`godfather events [--source NAME]... [--follow] [--json]`: every present source's events merged, newest last;
    --follow keeps printing what the logs gain (checked every 250 ms), --json prints the lines as read. An absent
    source is left out; a present one that is unsupported is named on stderr. Never in colour."""
    argv = split_options(argv)
    names: list[str] = []
    follow = as_json = False
    while argv:
        if argv[0] == "--source" and len(argv) > 1:
            if argv[1] not in _sources.TOOLS:
                raise UsageError(f"--source takes {', '.join(_sources.TOOLS)}, not {argv[1]!r}", "events")
            names, argv = names + [argv[1]], argv[2:]
        elif argv[0] == "--follow":
            follow, argv = True, argv[1:]
        elif argv[0] == "--json":
            as_json, argv = True, argv[1:]
        elif argv[0] in ("--help", "-h"):
            print(USAGE)
            return 0
        else:
            raise UsageError(f"events: unknown argument {argv[0]!r}", "events")
    feed: _events.Feed = _events.Feed()
    tags: dict[str, str] = {}
    for name in [tool for tool in _sources.TOOLS if not names or tool in names]:
        source = Source(name, _events.EventsContract(name))
        source.discover()
        if source.state == "absent":
            continue
        log = _events.log_path(source.capabilities)
        if source.state == "unsupported" or not log:
            print(f"godfather: {source.problem or name + ': no event log'}", file=sys.stderr)
            continue
        tags[name] = source.tag
        feed.follow(name, str(_sources.event_log(name, log)))

    def show(found: list[_events.Event]) -> None:
        for event in found:
            print(event.line if as_json else event_text(event, tags[event.source]), flush=follow)

    try:
        show(feed.poll())
        while follow:
            time.sleep(_events.POLL)
            show(feed.poll())
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:  # `godfather events | head`
        with contextlib.suppress(OSError):  # the interpreter's own flush at exit then writes nowhere
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
    finally:
        feed.close()
    return 0


def command(argv: list[str]) -> int:
    if argv[:1] == ["events"]:
        return events_command(argv[1:])
    argv = split_options(argv)
    interval, run_once, colour, width, height, page = None, False, None, None, None, "overview"
    while argv:
        if argv[0] == "--once":
            run_once, argv = True, argv[1:]
        elif argv[0] == "--color":
            colour, argv = True, argv[1:]
        elif argv[0] in ("--version", "-V") and len(argv) == 1:
            print(_version.text(_version.running_identity()) if _version.running_source() else f"godfather {_version.VERSION}")
            return 0
        elif argv[0] in ("--help", "-h"):
            print(USAGE)
            return 0
        elif argv[0] == "--page" and len(argv) > 1:
            if argv[1] not in _state.PAGES:
                raise UsageError(f"--page takes {', '.join(_state.PAGES)}, not {argv[1]!r}")
            page, argv = argv[1], argv[2:]
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
            raise UsageError(f"unknown argument {argv[0]!r}")
    try:
        interval, theme = starting_choices(interval)
        resolved = _theme.from_environment(mode=theme)  # --once cannot ask the terminal: GODFATHER_TERMINAL_BG/_FG
    except ValueError as error:
        raise UsageError(str(error)) from None
    if run_once:
        return once(width, height, colour, interval, resolved, page)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("godfather: the live view needs an interactive terminal; --once prints one frame", file=sys.stderr)
        print("Try 'godfather --help'.", file=sys.stderr)
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
        print(f"Try 'godfather {error.command + ' ' if error.command else ''}--help'.", file=sys.stderr)
        return 2
