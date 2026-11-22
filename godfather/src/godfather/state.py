"""The live view's state in one value (fathers#90, fathers#225). Everything the screen shows but the tools' own data
is here: the UI state (`UiState`: the active screen and its valid focus, at most one open modal or overlay, and the
committed EVENTS filter), the selection by entry identity (fathers#21), each panel's scroll and its thumb's flash, the
details' load states (fathers#23), and a notice kept apart from the refresh state (fathers#24). Keys, the mouse, the
loaders' results, the clock and each drawn frame go through `reducer.reduce()`, which returns the new state and the
effects for the app to carry out (quit, load, apply a theme, save choices, a tool's settings call) and does no I/O of
its own. Settings is a value too (`settings_model.Panel`): its reads, checks and writes are effects, and their answers
events."""
from __future__ import annotations

import dataclasses
import datetime
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Optional, Sequence, Union, cast

from . import actions as _actions
from . import entries as _entries
from . import events as _events_view
from . import layout as _layout
from . import news as _news
from . import overlay_text as _overlays
from . import settings_layout as _settings_layout
from . import settings_model as _settings
from .adapters import lanefather as _lanefather
from .adapters import spawnfather as _spawnfather
from .model import Dashboard as Model
from .overlay_text import OverlayView
from .text import hint_at

EntryId = tuple[Any, int]
DetailsKey = tuple[EntryId, str]

DETAILS_FRESH = 5.0  # seconds a request's loaded details are shown as they are; older ones load again, quietly
DETAILS_RETRY = DETAILS_FRESH  # seconds before a failed load is tried again, its error shown meanwhile
NOTICE_SECONDS = 4.0  # how long a notice (a Settings panel that would not open) stays in the footer
SCROLL_GLOW = 0.28  # seconds a scrolled panel's thumb stays in the foreground after its last scroll: a flash
PANELS = ("requests", "details", "events")  # the overview's focusable panels
# The pages (fathers#85, fathers#86): `1` the overview, `2` Lanes, `3` Spawns; each page's tool and its table
PAGES = ("overview", "lanes", "spawns")
PAGE_TOOLS = {"lanes": "lanefather", "spawns": "spawnfather"}
PAGE_TABLES = {"overview": "requests", "lanes": "lanes", "spawns": "spawns"}
PAGE_SCROLLS = ("details", "events", "lanes", "spawns")  # what a page shows: scrolled again from the top on a switch
FILTER_MAX = 40  # characters EVENTS' text filter takes
REASON_MAX = 120  # characters a park reason takes


class Mode(StrEnum):
    """The 14 input modes of the dashboard (fathers#22, fathers#225)."""
    MAIN = "main"
    OVERVIEW_EVENTS = "overview-events"
    OVERVIEW_DETAILS = "overview-details"
    PAGE = "page"
    PAGE_PANEL = "page-panel"
    FILTER = "filter"
    ACTIONS = "actions"
    REASON = "reason"
    CONFIRM = "confirm"
    HELP = "help"
    NEWS = "news"
    SETTINGS = "settings"
    SETTINGS_EDIT = "settings-edit"
    SETTINGS_SMALL = "settings-small"


# Screens and modals: illegal states are unrepresentable (fathers#225)

@dataclass(frozen=True)
class OverviewScreen:
    """The overview screen: `focus` is one of QUEUE, DETAILS or EVENTS; `shows` is whichever of QUEUE or EVENTS
    DETAILS currently describes (always matching `focus` while QUEUE or EVENTS is focused)."""
    focus: str = "requests"
    shows: str = ""

    def __post_init__(self) -> None:
        if self.focus not in PANELS:
            raise ValueError(f"invalid overview focus: {self.focus!r}")
        if not self.shows:
            object.__setattr__(self, "shows", self.focus if self.focus in ("requests", "events") else "requests")
        if self.shows not in ("requests", "events"):
            raise ValueError(f"invalid overview details target: {self.shows!r}")
        if self.focus in ("requests", "events") and self.shows != self.focus:
            raise ValueError(f"overview details cannot show {self.shows!r} while {self.focus!r} is focused")

    @property
    def page(self) -> str:
        return "overview"

    @property
    def came_from(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class PageScreen:
    """A tool's page (`lanes` or `spawns`): `focus` is its table, DETAILS or EVENTS; `came_from` the pages links
    left on the way here."""
    page: str
    focus: str = ""
    came_from: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.page not in PAGE_TOOLS:
            raise ValueError(f"invalid page screen: {self.page!r}")
        table = PAGE_TABLES[self.page]
        if not self.focus:
            object.__setattr__(self, "focus", table)
        if self.focus not in (table, "details", "events"):
            raise ValueError(f"invalid focus {self.focus!r} for page {self.page!r}")
        if not isinstance(cast(object, self.came_from), tuple) or any(origin not in PAGES for origin in self.came_from):
            raise ValueError(f"invalid came_from history: {self.came_from!r}")

    @property
    def shows(self) -> str:
        return "requests"


ScreenState = Union[OverviewScreen, PageScreen]


@dataclass(frozen=True)
class FilterEdit:
    """EVENTS' text filter field open in its bottom border (fathers#84)."""
    kind: str = "filter"

    def __post_init__(self) -> None:
        if self.kind != "filter":
            raise ValueError(f"invalid FilterEdit kind: {self.kind!r}")


@dataclass(frozen=True)
class Help:
    scroll: int = 0
    kind: str = "help"

    def __post_init__(self) -> None:
        if self.scroll < 0 or self.kind != "help":
            raise ValueError(f"invalid Help state: scroll={self.scroll!r}, kind={self.kind!r}")


@dataclass(frozen=True)
class News:
    """What's new (fathers#102): `tab` is its filter, 0 for every tool, then each tool with entries
    (overlays.filters()); closing it marks everything read."""
    tab: int = 0
    scroll: int = 0
    kind: str = "changelog"

    def __post_init__(self) -> None:
        if self.tab < 0 or self.scroll < 0 or self.kind not in ("changelog", "news"):
            raise ValueError(f"invalid News state: tab={self.tab!r}, scroll={self.scroll!r}, kind={self.kind!r}")


Changelog = News
SettingsPanel = _settings.Panel  # Settings (fathers#87): its sections, cursor, pending change and answers
SettingsOverlay = SettingsPanel


@dataclass(frozen=True)
class ActionMenu:
    """A row's actions (fathers#88): only those its tool advertises and that apply to it now; `cursor` the one Enter
    picks, none until one is chosen, so a second Enter never runs the first."""
    actions: tuple[_actions.Action, ...]
    cursor: Optional[int] = None
    kind: str = "actions"

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.actions), tuple) or not self.actions or any(
            not isinstance(cast(object, action), _actions.Action) for action in self.actions
        ):
            raise ValueError("ActionMenu requires a non-empty tuple of actions")
        if self.cursor is not None and not 0 <= self.cursor < len(self.actions):
            raise ValueError(f"ActionMenu cursor {self.cursor!r} out of range")
        pages = {action.identity[0] for action in self.actions if action.identity}
        if len(pages) > 1 or any(page not in PAGES for page in pages):
            raise ValueError(f"ActionMenu actions have inconsistent pages: {pages!r}")
        if self.kind != "actions":
            raise ValueError(f"invalid ActionMenu kind: {self.kind!r}")


@dataclass(frozen=True)
class Reason:
    """The reason a park asks for first, typed; an empty one cancels."""
    action: _actions.Action
    text: str = ""
    kind: str = "reason"

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.action), _actions.Action) or not self.action.reason:
            raise ValueError("Reason requires an action with reason=True")
        if len(self.text) > REASON_MAX or self.kind != "reason":
            raise ValueError(f"invalid Reason state: len(text)={len(self.text)}, kind={self.kind!r}")


@dataclass(frozen=True)
class Confirm:
    """An irreversible action asked first: the action, its target and the exact command; `y` runs it, `n`, Esc or a
    click outside cancel, nothing is preselected."""
    action: _actions.Action
    reason: str = ""
    kind: str = "confirm"

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.action), _actions.Action) or not self.action.confirm:
            raise ValueError("Confirm requires an action with confirm=True")
        if self.action.reason and not self.reason.strip():
            raise ValueError("Confirm for an action with reason=True requires a non-empty reason")
        if not self.action.reason and self.reason:
            raise ValueError("Confirm for an action without reason cannot carry a reason")
        if self.kind != "confirm":
            raise ValueError(f"invalid Confirm kind: {self.kind!r}")


Overlay = Union[Help, News, SettingsPanel, ActionMenu, Reason, Confirm]
Modal = Union[FilterEdit, Overlay]

_UNSET: Any = object()
_DEFAULT_SCREEN = OverviewScreen()


@dataclass(frozen=True, init=False)
class UiState:
    """The consolidated UI state (fathers#225): the active screen (`OverviewScreen` or `PageScreen`), at most one
    active modal (`FilterEdit`, `ActionMenu`, `Reason`, `Confirm`, `Help`, `News`, `SettingsPanel`), and EVENTS'
    filter criteria."""
    screen: ScreenState
    modal: Optional[Modal]
    filter: _events_view.Filter

    def __init__(
        self,
        screen: ScreenState = _DEFAULT_SCREEN,
        modal: Any = _UNSET,
        filter: Any = _UNSET,
        *,
        overlay: Any = _UNSET,
        events_filter: Any = _UNSET,
    ) -> None:
        if not isinstance(cast(object, screen), (OverviewScreen, PageScreen)):
            raise ValueError(f"invalid screen: {screen!r}")
        if modal is not _UNSET and overlay is not _UNSET and modal != overlay:
            raise ValueError("cannot specify two simultaneous modals")
        chosen = overlay if modal is _UNSET else modal
        if chosen is _UNSET:
            chosen = None
        if chosen is not None and not isinstance(
            chosen, (FilterEdit, ActionMenu, Reason, Confirm, Help, News, SettingsPanel)
        ):
            raise ValueError(f"invalid modal: {chosen!r}")
        if filter is not _UNSET and events_filter is not _UNSET and filter != events_filter:
            raise ValueError("cannot specify conflicting filter and events_filter")
        chosen_filter = events_filter if filter is _UNSET else filter
        if chosen_filter is _UNSET:
            chosen_filter = _events_view.Filter()
        if not isinstance(chosen_filter, _events_view.Filter):
            raise ValueError(f"invalid events filter: {chosen_filter!r}")
        if chosen_filter.editing:
            if chosen is not None and not isinstance(chosen, FilterEdit):
                raise ValueError("cannot combine filter editing with another modal")
            if chosen is None:
                raise ValueError("events_filter.editing=True requires modal=FilterEdit()")
            chosen_filter = dataclasses.replace(chosen_filter, editing=False)
        if isinstance(chosen, FilterEdit) and not isinstance(screen, OverviewScreen):
            raise ValueError("FilterEdit is only valid on OverviewScreen")
        if isinstance(chosen, (ActionMenu, Reason, Confirm)):
            if screen.focus != PAGE_TABLES[screen.page]:
                raise ValueError(
                    f"action modal requires focus on {PAGE_TABLES[screen.page]!r}, not {screen.focus!r}"
                )
            actions = chosen.actions if isinstance(chosen, ActionMenu) else (chosen.action,)
            for action in actions:
                if action.identity and action.identity[0] != screen.page:
                    raise ValueError(f"action for {action.identity[0]!r} cannot open on {screen.page!r}")
        object.__setattr__(self, "screen", screen)
        object.__setattr__(self, "modal", chosen)
        object.__setattr__(self, "filter", chosen_filter)

    @property
    def page(self) -> str:
        return self.screen.page

    @property
    def focus(self) -> str:
        return self.screen.focus

    @property
    def shows(self) -> str:
        return self.screen.shows

    @property
    def came_from(self) -> tuple[str, ...]:
        return self.screen.came_from

    @property
    def overlay(self) -> Optional[Overlay]:
        return None if self.modal is None or isinstance(self.modal, FilterEdit) else self.modal

    @property
    def editing_filter(self) -> bool:
        return isinstance(self.modal, FilterEdit)

    @property
    def events_filter(self) -> _events_view.Filter:
        return dataclasses.replace(self.filter, editing=True) if isinstance(self.modal, FilterEdit) else self.filter


@dataclass(frozen=True)
class Refresh:
    """The model refresh (fathers#24): initial until the first reply, then live, or stale with why."""
    kind: str = "initial"
    error: Optional[str] = None


@dataclass(frozen=True)
class Notice:
    """A passing note in the footer, never the refresh state: a Settings panel that would not open, what an action
    did (`style` ok, or bad for a failure, fathers#88), `no actions`."""
    text: str
    at: float
    style: str = "bad"


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
class Tool:
    """Another tool as a source (fathers#85, fathers#86): absent, unsupported (with why), ok or stale (with why and
    since when), its last good snapshot as its adapter decoded it, the actions its capabilities advertise, and for
    Lanefather whether it answers `show --json`."""
    name: str
    state: str = "absent"
    problem: Optional[str] = None
    snapshot: Optional[Mapping[str, Any]] = None  # model.Lanes or model.Processes
    stale_since: Optional[datetime.datetime] = None
    actions: tuple[str, ...] = ()
    show: bool = False

    @property
    def present(self) -> bool:
        return self.state != "absent"

    @property
    def readable(self) -> bool:
        """It has a snapshot to show (dimmed while stale)."""
        return self.state in ("ok", "stale") and self.snapshot is not None


Target = tuple[Any, ...]  # where a link goes: (page, row key); ("overview", entry id) selects a request


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
    filters: tuple[tuple[int, int, int, int], ...] = ()  # what's new's tool filters: (row, from, to, tab)
    switch: Optional[tuple[int, int, int]] = None  # EVENTS' title, which cycles its log (fathers#152): (row, from, to)
    targets: tuple[tuple[int, int, int, Target], ...] = ()  # links to a page's row: (row, from, to, target)

    def hint_at(self, x: int, y: int) -> Optional[str]:
        return hint_at(self.hints, x, y)

    def target_at(self, x: int, y: int) -> Optional[Target]:
        """Where a click on a link (a summary, DETAILS' lane or process row, a page's link back) goes."""
        return next((target for row, start, end, target in reversed(self.targets) if row == y and start <= x < end),
                    None)

    def filter_at(self, x: int, y: int) -> Optional[int]:
        """The tool filter a click in what's new's top border lands on, None between them."""
        return next((tab for row, start, end, tab in self.filters if row == y and start <= x < end), None)


@dataclass(frozen=True)
class AppState:
    model: Optional[Model] = None
    refresh: Refresh = Refresh()
    notice: Optional[Notice] = None
    ui: UiState = field(default_factory=UiState)
    selection: Selection = Selection()
    scroll: dict[str, int] = field(default_factory=lambda: cast(dict[str, int], {}))  # QUEUE, DETAILS, EVENTS
    glow: dict[str, float] = field(default_factory=lambda: cast(dict[str, float], {}))  # when each last scrolled or flashed
    details: dict[DetailsKey, Loaded] = field(default_factory=lambda: cast(dict[DetailsKey, Loaded], {}))
    colours: dict[EntryId, int] = field(default_factory=lambda: cast(dict[EntryId, int], {}))
    fresh_events: int = 0  # events the filter lets through that arrived while EVENTS was scrolled down
    newest: Any = None  # the newest shown event's identity
    interval: float = 1.0  # Refresh every
    theme: str = "auto"  # Colour theme
    news: Any = None  # news.News: what's new, once the tools answered
    read: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))  # news.Read: newest read, by tool
    frame: Frame = Frame()
    moment: Optional[datetime.datetime] = None  # the wall-clock second the screen is drawn at (Tick; fathers#138)
    # A table's selected row other than QUEUE's, by the row's identity: EVENTS' cursor (fathers#152; without one it
    # is on its newest row, and stays there as new ones arrive) and each page's row (fathers#85)
    picked: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))
    tools: dict[str, Tool] = field(default_factory=lambda: cast(dict[str, Tool], {}))  # Lanefather, Spawnfather
    acting: frozenset[tuple[Any, ...]] = frozenset()  # the actions in flight (action_key()), each started once
    slotfather_notice: Optional[tuple[str, ...]] = None  # notice lines when Slotfather is absent or unsupported

    @property
    def focus(self) -> str:
        return self.ui.focus

    @property
    def shows(self) -> str:
        return self.ui.shows

    @property
    def page(self) -> str:
        return self.ui.page

    @property
    def came_from(self) -> tuple[str, ...]:
        return self.ui.came_from

    @property
    def overlay(self) -> Optional[Overlay]:
        return self.ui.overlay

    @property
    def modal(self) -> Optional[Modal]:
        return self.ui.modal

    @property
    def events_filter(self) -> _events_view.Filter:
        return self.ui.events_filter


def with_modal(state: AppState, modal: Optional[Modal]) -> AppState:
    if modal == state.ui.modal:
        return state
    return dataclasses.replace(state, ui=dataclasses.replace(state.ui, modal=modal))


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
    colours: int  # the drawing theme's holder colours (theme.Theme.holders()) leases get: required, as a builtin
    # theme has six and a lease given a seventh would name a style it lacks
    problem: Optional[str] = None  # why the model shown is stale
    notice: Optional[tuple[str, ...]] = None  # what stands in for the overview when Slotfather is absent/unsupported


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
    """Settings opened: each present tool's section as first read (settings_model.Section), in any order; or, with
    `loading`, none yet: they are read on Settings' worker and come as SettingsSections."""
    sections: tuple[_settings.Section, ...]
    loading: bool = False


@dataclass(frozen=True)
class SettingsSections:
    """The tools' sections, read after Settings opened; `problem` why none could be."""
    sections: tuple[_settings.Section, ...]
    problem: Optional[str] = None


@dataclass(frozen=True)
class SettingsRead:
    """A tool's `settings --json` answered: its reply, or why it could not be read; `generation` is the section's
    when the read began."""
    tool: str
    generation: int
    reply: Optional[dict[str, Any]] = None
    problem: Optional[str] = None


@dataclass(frozen=True)
class SettingsChecked:
    """`settings check` answered for a value under a row's revision."""
    tool: str
    key: str
    value: str
    revision: Optional[str]
    reply: dict[str, Any]


@dataclass(frozen=True)
class SettingsWritten:
    """`settings set` or `reset` answered: ok, conflict, read-only, invalid or failed; the settings when the tool
    printed them, and its message."""
    request: _settings.Request
    outcome: str
    reply: Optional[dict[str, Any]] = None
    message: str = ""


@dataclass(frozen=True)
class SettingsFailed:
    error: str


@dataclass(frozen=True)
class NewsLoaded:
    news: Any  # news.News


@dataclass(frozen=True)
class ActionDone:
    """An action ran (or was dropped): the tool's first stdout line on success, its first stderr line on failure;
    `key` the action's (action_key()), no longer in flight."""
    tool: str
    ok: bool
    line: str
    key: tuple[Any, ...] = ()


@dataclass(frozen=True)
class HandOff:
    """The latest log, checked: the screen is to hand the terminal to the pager (fathers#88)."""
    action: _actions.Action
    key: tuple[Any, ...] = ()


def action_key(action: _actions.Action) -> tuple[Any, ...]:
    """What makes an action the same one: its row and its command; one in flight is not started again."""
    return (*action.identity, action.tool, *action.argv)


@dataclass(frozen=True)
class ToolLoaded:
    """Another tool's snapshot polled (or why not): its Tool."""
    tool: Tool


@dataclass(frozen=True)
class Drawn:
    frame: Frame


@dataclass(frozen=True)
class Tick:
    """The loop's pass: a Settings change settles and is saved, a notice expires; `moment` is the wall clock, which
    the state keeps to the second, so ages on screen move once a second and a pass within one changes nothing."""
    moment: Optional[datetime.datetime] = None


Event = Union[Key, Mouse, ModelLoaded, ModelFailed, DetailsLoaded, SettingsOpened, SettingsSections, SettingsRead,
              SettingsChecked, SettingsWritten, SettingsFailed, NewsLoaded, ToolLoaded, ActionDone, HandOff, Drawn, Tick]


# Effects, for the app to carry out

@dataclass(frozen=True)
class Effect:
    """quit · load-model · load-details (key, item) · open-settings · settings (item: a settings_model.Request to
    read, check, set or reset) · apply-theme · remember · repaint (every row) · load-news (what's new, from every
    tool) · save-read (the read versions, state.read) · flash (key: what lights up as a scrolled thumb does; the
    reducer takes it into `glow`, the app never sees it)."""
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


def overlay_box(state: AppState, overlay: Union[Help, News]) -> OverlayView:
    """Where an overlay is on this screen and how much of it shows: the geometry its keys and the wheel use."""
    tab = overlay.tab if isinstance(overlay, News) else 0
    return _overlays.overlay_view(overlay.kind, state.frame.width, state.frame.height, overlay.scroll, tab,
                                  state.news, state.read)[0]


def unread(state: AppState) -> bool:
    """Any tool has entries not read yet (what's new marks them; the footer looks the same either way)."""
    return bool(_news.unread(state.news, state.read))


def news_loaded(state: AppState, news: Any) -> AppState:
    """New news: an open what's new keeps its tool filter by name (the filters are the tools with entries)."""
    modal = state.ui.modal
    if isinstance(modal, News):
        before = _overlays.filters(_overlays.shown_news(state.news))
        tool_name = before[modal.tab % len(before)]
        after = _overlays.filters(_overlays.shown_news(news))
        tab = after.index(tool_name) if tool_name in after else 0
        if tab != modal.tab:
            modal = News(tab)
    return dataclasses.replace(state, news=news, ui=dataclasses.replace(state.ui, modal=modal))


def closed_news(before: AppState, state: AppState, quitting: bool = False) -> Result:
    """What's new closed, however (Esc, v, ?, a click outside, q while it is open): everything it showed is read,
    and saved."""
    if isinstance(before.ui.modal, News) and (quitting or not isinstance(state.ui.modal, News)):
        read = _news.mark_read(state.news, state.read)
        if read != state.read:
            return dataclasses.replace(state, read=read), [SAVE_READ]
    return state, []


# Queries and transitions shared by the modes

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
    return found, _entries.entry_ids(found)


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
    shown = state.ui.overlay
    if overlay and isinstance(shown, (Help, News)):
        found[shown.kind] = shown.scroll
    elif overlay and isinstance(shown, SettingsPanel):
        found["settings"] = shown.offset
    return found


def overlay_identity(state: AppState) -> tuple[Any, ...]:
    """Which overlay is open, and which tab or panel: one that opens or switches starts at its top unscrolled."""
    shown = state.ui.overlay
    if shown is None:
        return ()
    if isinstance(shown, SettingsPanel):
        return shown.kind, 0
    return shown.kind, shown.tab if isinstance(shown, News) else 0


def glowing(state: AppState, now: float) -> set[str]:
    """The panels whose thumb is in the foreground: scrolled within SCROLL_GLOW seconds."""
    return {name for name, when in state.glow.items() if now - when < SCROLL_GLOW}


def fade(state: AppState, now: float) -> Optional[float]:
    """Seconds until the next thumb goes back to its colour, None while none glows: one repaint, then."""
    due = [when + SCROLL_GLOW - now for when in state.glow.values() if now - when < SCROLL_GLOW]
    return max(0.0, min(due)) if due else None


def follow_events(state: AppState, events: list[dict[str, Any]]) -> AppState:
    """EVENTS (newest first) after a refresh (#15): at the top it shows the newest; scrolled down, it keeps showing
    the same events as new ones arrive above them and counts those. Only the rows the filter lets through count
    (fathers#84)."""
    model = {"events": events, "sources": (state.model or {}).get("sources") or []}
    ids = [event_id(event) for event in _events_view.shown_rows(model, state.ui.filter)[::-1]]
    arrived = ids.index(state.newest) if state.newest in ids else 0
    scroll, fresh = dict(state.scroll), 0
    if scroll.get("events") and state.ui.page == "overview":  # a page's strip is another list
        scroll["events"] += arrived
        fresh = state.fresh_events + arrived
    return dataclasses.replace(state, scroll=scroll, fresh_events=fresh, newest=ids[0] if ids else None)


_KEEP: Any = object()


def filtered(state: AppState, events_filter: _events_view.Filter, modal: Any = _KEEP) -> AppState:
    """A filter changed: what EVENTS shows is another list, so the newest shown is found again and nothing counts as
    new; one pinned to the newest stays pinned (fathers#84). Leaving FilterEdit without changing the filter criteria
    preserves scroll, picked, and fresh_events (fathers#225)."""
    clean = dataclasses.replace(events_filter, editing=False)
    next_modal = state.ui.modal if modal is _KEEP else modal
    current = state.ui.filter
    ui = dataclasses.replace(state.ui, modal=next_modal, filter=clean)
    if (clean.source, clean.text, clean.raw) == (current.source, current.text, current.raw):
        return state if ui == state.ui else dataclasses.replace(state, ui=ui)
    rows = _events_view.shown_rows(state.model, clean)
    newest = event_id(rows[-1]) if rows else None
    picked = {name: key for name, key in state.picked.items() if name != "events"}  # another list: its newest
    scroll = {name: offset for name, offset in state.scroll.items() if name != "events"}  # and the view with it
    return dataclasses.replace(state, ui=ui, newest=newest, fresh_events=0, picked=picked, scroll=scroll)


def event_rows(state: AppState) -> list[dict[str, Any]]:
    """EVENTS' rows as drawn, newest first."""
    return _events_view.newest_first(state.model, state.ui.filter)


def event_index(state: AppState, rows: Optional[list[dict[str, Any]]] = None) -> int:
    """EVENTS' cursor (fathers#152): the row picked while it is shown, else the newest."""
    rows = event_rows(state) if rows is None else rows
    picked = state.picked.get("events")
    if picked is not None:
        for index, row in enumerate(rows):
            if event_id(row) == picked:
                return index
    return 0


def event_move(state: AppState, target: int) -> AppState:
    """EVENTS' cursor to the row at `target` (newest first), kept in range and in view: the scroll follows it, so a
    scrolled list keeps counting what arrives above (`↑ N new`); the newest row picks nothing, so the cursor stays on
    the newest as new rows arrive, as the list does at its top."""
    rows = event_rows(state)
    if not rows:
        return state
    index = min(max(target, 0), len(rows) - 1)
    picked = {name: key for name, key in state.picked.items() if name != "events"}
    if index:
        picked["events"] = event_id(rows[index])
    view = state.frame.tables.get("events")
    scroll = dict(state.scroll)
    if view:
        visible = max(1, int(view["visible"]))
        offset = scroll.get("events", int(view["offset"]))
        offset = index if index < offset else index - visible + 1 if index >= offset + visible else offset
        scroll["events"] = _layout.clamp_scroll(offset, len(rows), visible)
    if index == 0:
        scroll.pop("events", None)
    if picked == state.picked and scroll == state.scroll:
        return state
    return dataclasses.replace(state, picked=picked, scroll=scroll)


def event_id(event: dict[str, Any]) -> Any:
    """A row's identity: its place in its source's log (events.row()), else what a row without one says."""
    return event.get("key") or (event.get("when"), event.get("kind"), event.get("pid"), event.get("count"))


# Pages and row actions (fathers#85, fathers#86, fathers#88)

def tool(state: AppState, name: str) -> Tool:
    """Another tool as the state last heard of it; absent until its first poll."""
    return state.tools.get(name) or Tool(name)


def selected_item(state: AppState) -> Optional[dict[str, Any]]:
    found = items(state)
    return found[min(state.selection.index, len(found) - 1)] if found else None


def selected_cwd(state: AppState) -> Optional[str]:
    """The selected request's holder directory, once its details loaded: the worktree join's fallback."""
    item = selected_item(state)
    if item is None or state.selection.key is None:
        return None
    known = state.details.get(details_key(item, state.selection.key))
    return known.cwd if known is not None else None


def page_rows(state: AppState, page: Optional[str] = None) -> Sequence[Mapping[str, Any]]:
    """A page's table rows, from its tool's last good snapshot; a row without a `key` is not selectable."""
    which = state.ui.page if page is None else page
    if which == "lanes":
        return _lanefather.page_rows(tool(state, "lanefather").snapshot)
    if which == "spawns":
        return _spawnfather.page_rows(tool(state, "spawnfather").snapshot)
    return []


def page_index(state: AppState, page: Optional[str] = None,
               rows: Optional[Sequence[Mapping[str, Any]]] = None) -> Optional[int]:
    """The selected row of a page: the one picked while it is there, else the first selectable one."""
    which = state.ui.page if page is None else page
    rows = page_rows(state, which) if rows is None else rows
    keys = [row.get("key") for row in rows]
    picked = state.picked.get(which)
    if picked is not None and picked in keys:
        return keys.index(picked)
    return next((index for index, key in enumerate(keys) if key is not None), None)


def linked(state: AppState, page: str) -> Any:
    """The row of `page` the selected request links to: its lane (fathers#85), its first owned spawn
    (fathers#86); None where it has none."""
    item = selected_item(state)
    if item is None:
        return None
    lane = _lanefather.lane_of(item, tool(state, "lanefather").snapshot, selected_cwd(state))
    if page == "lanes":
        return lane["key"] if lane is not None else None
    if page == "spawns":  # its first owned entry (fathers#86)
        owned = _spawnfather.of_request(item, tool(state, "spawnfather").snapshot, lane)
        return owned[0]["key"] if owned else None
    return None


def applicable(state: AppState) -> list[_actions.Action]:
    """The actions of the row the keys act on (fathers#88): QUEUE's selected request on the overview, a page's
    selected row on a page; those its tool advertises and that apply to it now."""
    page = state.ui.page
    if page == "overview":
        item = selected_item(state)
        if item is None or state.model is None:
            return []
        uid = state.model.get("uid")
        return _actions.for_request(item, cast(list[dict[str, Any]], state.model["waiters"]),
                                    state.model.get("actions") or (), uid if isinstance(uid, int) else None)
    index = page_index(state, page)
    if index is None:
        return []
    if page == "spawns":
        return _actions.for_process(_spawnfather.page_rows(tool(state, "spawnfather").snapshot)[index],
                                    tool(state, "spawnfather").actions)
    return _actions.for_lane(page_rows(state, page)[index], tool(state, "lanefather").actions)


def settings_context(state: AppState) -> _settings.Context:
    return _settings.Context(state.interval, state.theme, state.model, state.refresh.error, state.frame.width,
                             state.frame.height)


def settings_result(state: AppState, panel: _settings.Panel, context: _settings.Context,
                    requests: list[_settings.Request]) -> Result:
    """A Settings transition carried into the app: the panel kept (its checks asked for, its scroll placed), this
    dashboard's own settings applied at once and remembered, each tool call an effect, a save reloading the model
    so its effect shows now, close and quit."""
    effects: list[Effect] = []
    if context.theme != state.theme:
        effects += [APPLY_THEME, REPAINT]
    if (context.interval, context.theme) != (state.interval, state.theme):
        state = dataclasses.replace(state, interval=context.interval, theme=context.theme)
        effects.append(REMEMBER)
    context = settings_context(state)
    modal: Optional[Modal] = panel
    for request in requests:
        if request.kind in ("open", "read", "check", "set", "reset"):
            effects.append(Effect("settings", item=request))
        elif request.kind == "changed" and LOAD_MODEL not in effects:
            effects.append(LOAD_MODEL)
        elif request.kind == "close":
            modal = None
            effects.append(REPAINT)
        elif request.kind == "quit":
            effects.append(QUIT)
    if modal is not None:
        panel, checks = _settings.wanted_checks(_settings.settle_focus(panel, context), context)
        modal = _settings_layout.place(panel, context)
        effects += [Effect("settings", item=request) for request in checks]
    return with_modal(state, modal), effects


def settings_event(state: AppState, event: Event, now: float) -> Result:
    """An answer from a tool's settings CLI, or the loop's tick, while Settings is open."""
    panel = state.ui.modal
    if not isinstance(panel, SettingsPanel):
        return state, []
    context = settings_context(state)
    requests: list[_settings.Request] = []
    if isinstance(event, SettingsSections):
        panel = _settings.sections_read(panel, context, event.sections, now)
        if event.problem:
            panel = _settings.say(panel, event.problem, "bad", now)
    elif isinstance(event, SettingsRead):
        panel = _settings.read_done(panel, context, event.tool, event.generation, event.reply, event.problem, now)
    elif isinstance(event, SettingsChecked):
        panel, context, requests = _settings.check_done(panel, context, event.tool, event.key, event.value,
                                                        event.revision, event.reply, now)
    elif isinstance(event, SettingsWritten):
        panel, requests = _settings.write_done(panel, context, event.request, event.outcome, event.reply,
                                               event.message, now)
    else:
        panel, context, requests = _settings.tick(panel, context, now)
    return settings_result(state, panel, context, requests)


def settle(state: AppState, now: float) -> Result:
    """After every event: the selection found again in the model, its details loaded when missing, stale or due a
    retry, a notice gone once it is old, the count of new events gone at EVENTS' top."""
    effects: list[Effect] = []
    if state.notice is not None and now - state.notice.at >= NOTICE_SECONDS:
        state = dataclasses.replace(state, notice=None)
    if not state.scroll.get("events") and state.fresh_events:
        state = dataclasses.replace(state, fresh_events=0)
    # DETAILS follows the focused panel (fathers#152): QUEUE's entry or EVENTS' row, whichever was focused last; with
    # EVENTS not drawn (one column under 83 columns or short), QUEUE's, and any open filter edit closes (fathers#225).
    screen = state.ui.screen
    modal = state.ui.modal
    if isinstance(screen, OverviewScreen):
        focus = screen.focus
        shows = screen.focus if screen.focus in ("requests", "events") else screen.shows
        if state.frame.width and "events" not in state.frame.tables:
            if focus == "events":
                focus = "requests"
            shows = "requests"
            if isinstance(modal, FilterEdit):
                modal = None
        if focus != screen.focus or shows != screen.shows or modal is not state.ui.modal:
            state = dataclasses.replace(
                state,
                ui=dataclasses.replace(state.ui, screen=OverviewScreen(focus, shows), modal=modal),
            )
    known = entries(state)
    found = known[0]
    state = move(state, resolve(state, known), known=known)
    if found:
        index = state.selection.index
        key = details_key(found[index], cast(EntryId, state.selection.key))
        loaded = state.details.get(key)
        if loaded is None or now - loaded.at >= (DETAILS_RETRY if loaded.error else DETAILS_FRESH):
            effects.append(Effect("load-details", key, dict(found[index])))
        if loaded is not None and loaded.lane is not None and found[index].get("lane") != loaded.lane:
            state = join_lane(state, index, loaded.lane)
    return state, effects


def join_lane(state: AppState, index: int, lane: Any) -> AppState:
    """The model with the selected entry's loaded lane in it (fathers#138: the reducer joins it, the views only read
    it): a new model, its entry copied, so the model a view drew never changes."""
    model = cast(Model, state.model)
    holders = len(model["holders"])
    group, position = ("holders", index) if index < holders else ("waiters", index - holders)
    joined = model.copy()
    entries_now = list(model["holders"] if group == "holders" else model["waiters"])
    entry = entries_now[position].copy()
    entry["lane"] = lane
    entries_now[position] = entry
    if group == "holders":
        joined["holders"] = entries_now
    else:
        joined["waiters"] = entries_now
    return dataclasses.replace(state, model=joined)


def model_loaded(state: AppState, event: ModelLoaded) -> AppState:
    fresh = event.model
    if fresh is not None and fresh is not state.model:
        colours = dict(state.colours)
        _entries.dashboard_colours(fresh, colours, event.colours)
        state = dataclasses.replace(state, colours=colours, model=fresh)
        state = follow_events(state, fresh["events"])
        ids = _entries.entry_ids(items(state))
        keep = {(identity, kind) for identity in ids for kind in ("lease", "request")}
        state = dataclasses.replace(state, details={key: value for key, value in state.details.items() if key in keep})
    refresh = Refresh("live") if event.problem is None else Refresh("stale", event.problem)
    notice = tuple(event.notice) if event.notice is not None else None
    return dataclasses.replace(state, model=fresh, refresh=refresh, slotfather_notice=notice)
