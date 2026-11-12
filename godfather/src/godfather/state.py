"""The live view's state in one value, and the one reducer that changes it (fathers#90). Everything the screen shows
but the tools' own data is here: the focus, the one overlay open (fathers#17), the selection by entry identity
(fathers#21), each panel's scroll and its thumb's flash, the details' load states (fathers#23), and a notice kept
apart from the refresh state (fathers#24). Keys and the mouse go through the mode table in modes.py; the loaders'
results, the clock and each drawn frame come in as events too. reduce() returns the new state and the effects for
the app to carry out (quit, load, apply a theme, save choices); it does no I/O of its own but through the Settings
panel, which owns its own state and its calls to `slotfather settings`."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Optional, Union, cast

from . import news as _news
from .tui import dashboard_text
from .views import common as _common
from .views import overlays as _overlays
from .widgets import OverlayView

Model = dict[str, Any]
EntryId = tuple[Any, int]
DetailsKey = tuple[EntryId, str]

DETAILS_FRESH = 5.0  # seconds a request's loaded details are shown as they are; older ones load again, quietly
DETAILS_RETRY = DETAILS_FRESH  # seconds before a failed load is tried again, its error shown meanwhile
NOTICE_SECONDS = 4.0  # how long a notice (a Settings panel that would not open) stays in the footer
SCROLL_GLOW = 0.28  # seconds a scrolled panel's thumb stays in the foreground after its last scroll: a flash
PANELS = ("requests", "details", "events")  # the focusable panels


# The overlays: at most one is open (fathers#17), and each owns its scroll

@dataclass(frozen=True)
class Help:
    scroll: int = 0
    kind: str = "help"


@dataclass(frozen=True)
class Changelog:
    """What's new (fathers#102): `tab` is its filter, 0 for every tool, then each tool with entries
    (overlays.filters()); closing it marks everything read."""
    tab: int = 0
    scroll: int = 0
    kind: str = "changelog"


@dataclass(frozen=True)
class SettingsOverlay:
    panel: Any  # views.settings.Settings: its own state machine, with its own tests
    kind: str = "settings"


Overlay = Union[Help, Changelog, SettingsOverlay]


@dataclass(frozen=True)
class Refresh:
    """The model refresh (fathers#24): initial until the first reply, then live, or stale with why."""
    kind: str = "initial"
    error: Optional[str] = None


@dataclass(frozen=True)
class Notice:
    """A passing note in the footer, never the refresh state: a Settings panel that would not open."""
    text: str
    at: float


@dataclass(frozen=True)
class Selection:
    """The selected entry by identity (fathers#21), and where it last was, so the selection moves to its neighbour
    in the same group when it ends."""
    key: Optional[EntryId] = None
    index: int = 0
    group: str = "holders"
    position: int = 0


@dataclass(frozen=True)
class Loaded:
    """A details load that finished: its facts, or why it failed (fathers#23), and when."""
    at: float
    lane: Any = None
    cwd: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class Frame:
    """What the last drawn frame placed, for hit testing and the panel keys: each panel's viewport, the footer's
    version and hints, and the screen's size."""
    width: int = 0
    height: int = 0
    tables: dict[str, dict[str, Any]] = field(default_factory=lambda: cast(dict[str, dict[str, Any]], {}))
    version_at: Optional[tuple[int, int, int]] = None
    hints: tuple[tuple[int, int, int, str], ...] = ()
    order: tuple[str, ...] = ("requests",)  # Tab's order, as layout.overview() placed the panels

    def hint_at(self, x: int, y: int) -> Optional[str]:
        return next((press for row, start, end, press in reversed(self.hints) if row == y and start <= x < end), None)


@dataclass(frozen=True)
class AppState:
    model: Optional[Model] = None
    refresh: Refresh = Refresh()
    notice: Optional[Notice] = None
    focus: str = "requests"
    overlay: Optional[Overlay] = None
    selection: Selection = Selection()
    scroll: dict[str, int] = field(default_factory=lambda: cast(dict[str, int], {}))  # QUEUE, DETAILS, EVENTS
    glow: dict[str, float] = field(default_factory=lambda: cast(dict[str, float], {}))  # when each last scrolled
    details: dict[DetailsKey, Loaded] = field(default_factory=lambda: cast(dict[DetailsKey, Loaded], {}))
    colours: dict[EntryId, int] = field(default_factory=lambda: cast(dict[EntryId, int], {}))
    fresh_events: int = 0  # events that arrived while EVENTS was scrolled down
    newest: Any = None  # the newest event's identity
    interval: float = 1.0  # Refresh every
    theme: str = "auto"  # Colour theme
    news: Any = None  # news.News: what's new, once the tools answered
    read: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))  # news.Read: newest read, by tool
    frame: Frame = Frame()


# Events

@dataclass(frozen=True)
class Key:
    key: str


@dataclass(frozen=True)
class Mouse:
    button: int
    x: int
    y: int
    pressed: bool


@dataclass(frozen=True)
class ModelLoaded:
    model: Optional[Model]
    problem: Optional[str] = None  # why the model shown is stale


@dataclass(frozen=True)
class ModelFailed:
    error: str


@dataclass(frozen=True)
class DetailsLoaded:
    key: DetailsKey
    lane: Any = None
    cwd: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class SettingsOpened:
    panel: Any


@dataclass(frozen=True)
class SettingsFailed:
    error: str


@dataclass(frozen=True)
class NewsLoaded:
    news: Any  # news.News


@dataclass(frozen=True)
class Drawn:
    frame: Frame


@dataclass(frozen=True)
class Tick:
    """The loop's pass: a Settings change settles and is saved, a notice expires."""


Event = Union[Key, Mouse, ModelLoaded, ModelFailed, DetailsLoaded, SettingsOpened, SettingsFailed, NewsLoaded, Drawn,
              Tick]


# Effects, for the app to carry out

@dataclass(frozen=True)
class Effect:
    """quit · load-model · load-details (key, item) · open-settings · apply-theme · remember · repaint (every row) ·
    load-news (what's new, from every tool) · save-read (the read versions, state.read)."""
    kind: str
    key: Any = None
    item: Any = None


QUIT, LOAD_MODEL, OPEN_SETTINGS, APPLY_THEME, REMEMBER, REPAINT, LOAD_NEWS, SAVE_READ = (
    Effect("quit"), Effect("load-model"), Effect("open-settings"), Effect("apply-theme"), Effect("remember"),
    Effect("repaint"), Effect("load-news"), Effect("save-read"))
Result = tuple[AppState, list[Effect]]


def items(state: AppState) -> list[dict[str, Any]]:
    model = state.model
    return cast(list[dict[str, Any]], model["holders"] + model["waiters"]) if model else []


def details_key(item: dict[str, Any], identity: EntryId) -> DetailsKey:
    """What a load is kept under (fathers#23): the entry and whether it is a lease or a request, so a grant or a
    yield loads its facts again at once."""
    return identity, "request" if item.get("kind") == "waiting" else "lease"


def overlay_box(state: AppState, overlay: Union[Help, Changelog]) -> OverlayView:
    """Where an overlay is on this screen and how much of it shows: the geometry its keys and the wheel use."""
    tab = overlay.tab if isinstance(overlay, Changelog) else 0
    return _overlays.overlay_view(overlay.kind, state.frame.width, state.frame.height, overlay.scroll, tab,
                                  state.news, state.read)[0]


def unread(state: AppState) -> bool:
    """Any tool has entries not read yet: the footer's what's new is in the accent colour."""
    return bool(_news.unread(state.news, state.read))


def closed_news(before: AppState, state: AppState) -> Result:
    """What's new closed, however (Esc, v, ?, a click outside): everything it showed is read, and saved."""
    if isinstance(before.overlay, Changelog) and not isinstance(state.overlay, Changelog):
        read = _news.mark_read(state.news, state.read)
        if read != state.read:
            return dataclasses.replace(state, read=read), [SAVE_READ]
    return state, []


# Transitions shared by the modes

def move(state: AppState, index: int, by_key: bool = False,
         known: Optional[tuple[list[dict[str, Any]], list[EntryId]]] = None) -> AppState:
    """The one way the selection moves (fathers#21): to the entry at `index`, kept in range. A key move starts QUEUE's
    and DETAILS' scroll over; any move to another entry starts DETAILS' over. `known`: the entries and their ids,
    where the caller has them."""
    found, ids = known or entries(state)
    holders = len(state.model["holders"]) if state.model else 0
    index = max(0, min(index, len(found) - 1))
    key = ids[index] if found else None
    scroll = dict(state.scroll)
    if by_key:
        scroll.pop("requests", None)
    if by_key or key != state.selection.key:
        scroll.pop("details", None)
    group, position = ("holders", index) if index < holders or not found else ("waiters", index - holders)
    selection = Selection(key, index, group, position)
    if selection == state.selection and scroll == state.scroll:
        return state  # nothing moved: the same state, so the loop has nothing to draw again
    return dataclasses.replace(state, selection=selection, scroll=scroll)


def entries(state: AppState) -> tuple[list[dict[str, Any]], list[EntryId]]:
    found = items(state)
    return found, _common.entry_ids(found)


def resolve(state: AppState, known: Optional[tuple[list[dict[str, Any]], list[EntryId]]] = None) -> int:
    """The selected entry's index now: where its identity is, else its neighbour in the same group (the one that
    took its place), else the nearest row."""
    found, ids = known or entries(state)
    selection = state.selection
    if selection.key in ids:
        return ids.index(selection.key)
    if not found or state.model is None:
        return 0
    holders = len(state.model["holders"])
    group = (0, holders) if selection.group == "holders" else (holders, len(found))
    if group[1] > group[0]:
        return group[0] + min(selection.position, group[1] - group[0] - 1)
    return max(0, min(selection.index, len(found) - 1))


def scrolled(before: dict[str, int], after: dict[str, int]) -> set[str]:
    """The panels scrolled: their offset set to another value. One dropped (a new selection, an overlay opened)
    went back to its start without being scrolled."""
    return {name for name in after if before.get(name, 0) != after[name]}


def offsets(state: AppState, overlay: bool = True) -> dict[str, int]:
    """Every scroll offset by panel name, the open overlay's too unless `overlay` is False: what the thumb's flash
    watches."""
    found = dict(state.scroll)
    shown = state.overlay
    if overlay and isinstance(shown, (Help, Changelog)):
        found[shown.kind] = shown.scroll
    elif overlay and isinstance(shown, SettingsOverlay):
        found["settings"] = int(shown.panel.offset)
    return found


def overlay_identity(state: AppState) -> tuple[Any, ...]:
    """Which overlay is open, and which tab or panel: one that opens or switches starts at its top unscrolled."""
    shown = state.overlay
    if shown is None:
        return ()
    if isinstance(shown, SettingsOverlay):
        return shown.kind, id(shown.panel)
    return shown.kind, shown.tab if isinstance(shown, Changelog) else 0


def glowing(state: AppState, now: float) -> set[str]:
    """The panels whose thumb is in the foreground: scrolled within SCROLL_GLOW seconds."""
    return {name for name, when in state.glow.items() if now - when < SCROLL_GLOW}


def fade(state: AppState, now: float) -> Optional[float]:
    """Seconds until the next thumb goes back to its colour, None while none glows: one repaint, then."""
    due = [when + SCROLL_GLOW - now for when in state.glow.values() if now - when < SCROLL_GLOW]
    return max(0.0, min(due)) if due else None


def follow_events(state: AppState, events: list[dict[str, Any]]) -> AppState:
    """EVENTS (newest first) after a refresh (#15): at the top it shows the newest; scrolled down, it keeps showing
    the same events as new ones arrive above them and counts those."""
    ids = [event_id(event) for event in events[::-1]]
    arrived = ids.index(state.newest) if state.newest in ids else 0
    scroll, fresh = dict(state.scroll), 0
    if scroll.get("events"):
        scroll["events"] += arrived
        fresh = state.fresh_events + arrived
    return dataclasses.replace(state, scroll=scroll, fresh_events=fresh, newest=ids[0] if ids else None)


def event_id(event: dict[str, Any]) -> tuple[Any, ...]:
    return (event.get("when"), event.get("verb"), event.get("pid"), event.get("count"))


def settings_effects(state: AppState, action: str) -> Result:
    """What a Settings action does to the app, one function for keys, clicks and a change that settled: Refresh every
    and Colour theme apply at once and are remembered; a closed panel repaints; a saved setting reloads the model so
    its effect shows now."""
    if action == "quit":
        return state, [QUIT]
    overlay = state.overlay
    if not isinstance(overlay, SettingsOverlay):
        return state, []
    panel = overlay.panel
    effects: list[Effect] = []
    theme = str(panel.theme)
    if theme != state.theme:
        effects += [APPLY_THEME, REPAINT]
    state = dataclasses.replace(state, interval=float(panel.interval), theme=theme)
    effects.append(REMEMBER)
    if action == "close":
        state = dataclasses.replace(state, overlay=None)
        effects.append(REPAINT)
    if action == "changed":
        effects.append(LOAD_MODEL)
    return state, effects


def settle(state: AppState, now: float) -> Result:
    """After every event: the selection found again in the model, its details loaded when missing, stale or due a
    retry, a notice gone once it is old, the count of new events gone at EVENTS' top."""
    effects: list[Effect] = []
    if state.notice is not None and now - state.notice.at >= NOTICE_SECONDS:
        state = dataclasses.replace(state, notice=None)
    if not state.scroll.get("events") and state.fresh_events:
        state = dataclasses.replace(state, fresh_events=0)
    known = entries(state)
    found = known[0]
    state = move(state, resolve(state, known), known=known)
    if found:
        index = state.selection.index
        key = details_key(found[index], cast(EntryId, state.selection.key))
        known = state.details.get(key)
        if known is None or now - known.at >= (DETAILS_RETRY if known.error else DETAILS_FRESH):
            effects.append(Effect("load-details", key, dict(found[index])))
    return state, effects


def model_loaded(state: AppState, event: ModelLoaded) -> AppState:
    fresh = event.model
    if fresh is not None and fresh is not state.model:
        colours = dict(state.colours)
        _common.dashboard_colours(fresh, colours)
        state = dataclasses.replace(state, colours=colours, model=fresh)
        state = follow_events(state, cast(list[dict[str, Any]], fresh["events"]))
        ids = _common.entry_ids(items(state))
        keep = {(identity, kind) for identity in ids for kind in ("lease", "request")}
        state = dataclasses.replace(state, details={key: value for key, value in state.details.items() if key in keep})
    refresh = Refresh("live") if event.problem is None else Refresh("stale", event.problem)
    return dataclasses.replace(state, model=fresh, refresh=refresh)


def reduce(state: AppState, event: Event, now: float) -> Result:
    """The one reducer: the state after `event` at `now`, and what the app must do."""
    from . import modes as _modes  # the mode table builds on the transitions above

    effects: list[Effect] = []
    if isinstance(event, (Key, Mouse)):  # only these scroll: a refresh that moves EVENTS down is no flash
        old, before, panels = state, offsets(state), offsets(state, False)  # Settings' offset before it moves
        state, effects = _modes.dispatch(state, event)
        state, marked = closed_news(old, state)
        effects += marked
        same = overlay_identity(state) == overlay_identity(old)  # an overlay opened or switched is not scrolled
        moved = scrolled(before if same else panels, offsets(state, same))
        if moved:
            state = dataclasses.replace(state, glow={**state.glow, **{name: now for name in moved}})
    elif isinstance(event, ModelLoaded):
        state = model_loaded(state, event)
    elif isinstance(event, ModelFailed):
        state = dataclasses.replace(state, refresh=Refresh("stale", dashboard_text(event.error)))
    elif isinstance(event, DetailsLoaded):
        state = dataclasses.replace(state, details={**state.details, event.key: Loaded(
            now, event.lane, event.cwd, None if event.error is None else dashboard_text(event.error))})
    elif isinstance(event, SettingsOpened):
        state = dataclasses.replace(state, overlay=SettingsOverlay(event.panel))
    elif isinstance(event, NewsLoaded):
        state = dataclasses.replace(state, news=event.news)
    elif isinstance(event, SettingsFailed):
        state = dataclasses.replace(state, notice=Notice(dashboard_text(event.error), now))
    elif isinstance(event, Drawn):
        focus = state.focus if state.focus == "requests" or state.focus in event.frame.tables else "requests"
        state = dataclasses.replace(state, frame=event.frame, focus=focus)
    elif isinstance(state.overlay, SettingsOverlay):  # Tick: a change that settled is saved, and applies at once
        panel = state.overlay.panel
        if panel.tick():
            state, effects = settings_effects(state, "changed")
        elif str(panel.theme) != state.theme:
            state, effects = settings_effects(state, "handled")
    state, more = settle(state, now)
    return state, effects + more
