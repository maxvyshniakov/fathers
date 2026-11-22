"""The one reducer and mode table (fathers#22, fathers#90, fathers#225): the app state after an event, and the
effects for the app to carry out. Every one of the 14 `Mode` values is derived from `state.ui` (`mode_of`) and
dispatched through `MODES` without scattered boolean flags."""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Union, cast

from . import events as _events_view
from . import keys as _keys
from . import layout as _layout
from . import overlay_text as _overlays
from . import settings_model as _settings
from .state import (
    FILTER_MAX,
    LOAD_NEWS,
    OPEN_SETTINGS,
    PAGE_SCROLLS,
    PAGE_TABLES,
    PAGE_TOOLS,
    PAGES,
    QUIT,
    REASON_MAX,
    REPAINT,
    ActionDone,
    ActionMenu,
    AppState,
    Confirm,
    DetailsLoaded,
    Drawn,
    Effect,
    Event,
    FilterEdit,
    HandOff,
    Help,
    Key,
    Loaded,
    Mode,
    ModelFailed,
    ModelLoaded,
    Mouse,
    News,
    NewsLoaded,
    Notice,
    OverviewScreen,
    PageScreen,
    Reason,
    Refresh,
    Result,
    ScreenState,
    SettingsChecked,
    SettingsFailed,
    SettingsOpened,
    SettingsPanel,
    SettingsRead,
    SettingsSections,
    SettingsWritten,
    Target,
    ToolLoaded,
    action_key,
    applicable,
    closed_news,
    entries,
    event_index,
    event_move,
    filtered,
    items,
    linked,
    model_loaded,
    move,
    news_loaded,
    offsets,
    overlay_box,
    overlay_identity,
    page_index,
    page_rows,
    scrolled,
    settings_context,
    settings_event,
    settings_result,
    settle,
    tool,
    with_modal,
)
from .text import dashboard_text

Handler = Callable[[AppState, str, float], Result]
NAVIGATION = ("up", "k", "down", "j", "page_up", "page_down", "home", "g", "end", "G")
WHEEL = 3  # rows a wheel step scrolls
LEFT, RIGHT, WHEEL_UP, WHEEL_DOWN = 0, 2, 64, 65
RAW_MODES = frozenset({Mode.FILTER, Mode.REASON, Mode.SETTINGS_EDIT})


def mode_of(state: AppState) -> Mode:
    """The mode keys are read in: the open modal/overlay (Settings by whether it is too small to show or a value is
    being typed), else the active screen by its focus."""
    modal = state.ui.modal
    if isinstance(modal, SettingsPanel):
        if _settings.small(modal, settings_context(state)):
            return Mode.SETTINGS_SMALL
        return Mode.SETTINGS_EDIT if modal.editing else Mode.SETTINGS
    if isinstance(modal, FilterEdit):
        return Mode.FILTER
    if isinstance(modal, ActionMenu):
        return Mode.ACTIONS
    if isinstance(modal, Reason):
        return Mode.REASON
    if isinstance(modal, Confirm):
        return Mode.CONFIRM
    if isinstance(modal, Help):
        return Mode.HELP
    if isinstance(modal, News):
        return Mode.NEWS
    screen = state.ui.screen
    if isinstance(screen, PageScreen):
        return Mode.PAGE if screen.focus == PAGE_TABLES[screen.page] else Mode.PAGE_PANEL
    return (
        Mode.MAIN if screen.focus == "requests"
        else Mode.OVERVIEW_EVENTS if screen.focus == "events"
        else Mode.OVERVIEW_DETAILS
    )


mode = mode_of


# Pages and focus transitions

def _frame_matches_page(state: AppState) -> bool:
    """False when `state.frame` still holds table geometry from a departed page before the next Drawn event."""
    return not any(table in state.frame.tables for page, table in PAGE_TABLES.items() if page != state.ui.page)


def focus_panel(state: AppState, target: str) -> AppState:
    """Focuses `target` on the current screen, updating what DETAILS shows on the overview."""
    screen = state.ui.screen
    if target == screen.focus:
        return state
    if isinstance(screen, OverviewScreen):
        if target not in ("requests", "events", "details"):
            return state
        if target == "events" and state.frame.width and "events" not in state.frame.tables:
            return state
        shows = target if target in ("requests", "events") else screen.shows
        if shows == "events" and state.frame.width and "events" not in state.frame.tables:
            shows = "requests"
        next_screen: ScreenState = OverviewScreen(target, shows)
    else:
        if target not in (PAGE_TABLES[screen.page], "details", "events"):
            return state
        next_screen = dataclasses.replace(screen, focus=target)
    modal = (
        None
        if isinstance(state.ui.modal, (ActionMenu, Reason, Confirm)) and target != PAGE_TABLES[screen.page]
        else state.ui.modal
    )
    return dataclasses.replace(state, ui=dataclasses.replace(state.ui, screen=next_screen, modal=modal))


def open_page(state: AppState, page: str, key: Any = None, link: bool = False) -> AppState:
    """`page` shown (fathers#85): from the overview with the row the selected request links to selected, else `key`,
    else the row picked before; a link (`link`) remembers where it came from for Esc. A page whose tool is absent
    does not open. Any open filter edit or row-action modal for the departed page is dismissed."""
    if page not in PAGES:
        raise ValueError(f"invalid page: {page!r}")
    if page != "overview" and not tool(state, PAGE_TOOLS[page]).present:
        return state
    current = state.ui.page
    if page == current and key is None:
        return state
    picked = dict(state.picked)
    if page != "overview":
        found = key if key is not None else linked(state, page) if current == "overview" else None
        if found is not None:
            picked[page] = tuple(cast(list[Any], found)) if isinstance(found, list) else found
    if page == "overview":
        screen: ScreenState = OverviewScreen("requests", "requests")
    else:
        if not link:
            came: tuple[str, ...] = ()
        else:
            came = (*state.ui.came_from, current) if page != current else state.ui.came_from
        screen = PageScreen(page, PAGE_TABLES[page], came)
    modal = None if isinstance(state.ui.modal, (FilterEdit, ActionMenu, Reason, Confirm)) else state.ui.modal
    scroll = {name: offset for name, offset in state.scroll.items() if name not in PAGE_SCROLLS}
    return dataclasses.replace(
        state,
        ui=dataclasses.replace(state.ui, screen=screen, modal=modal),
        picked=picked,
        scroll=scroll,
    )


def follow_link(state: AppState, target: Target) -> AppState:
    """A click on a link: to a page's row, or back to the overview with a request selected."""
    page, key = target[0], target[1] if len(target) > 1 else None
    if page == "overview":
        state = open_page(state, "overview")
        known = entries(state)
        return move(state, known[1].index(key), known=known) if key in known[1] else state
    return open_page(state, page, key, link=True)


def go_back(state: AppState) -> AppState:
    """Esc on a page: the nearest still-present page a link came from, skipping absent intermediate pages, else the
    overview."""
    if state.ui.page == "overview":
        return state
    came = state.ui.came_from
    while came:
        back, came = came[-1], came[:-1]
        if back == "overview":
            return open_page(state, "overview")
        if back != state.ui.page and tool(state, PAGE_TOOLS[back]).present:
            opened = open_page(state, back, state.picked.get(back))
            return dataclasses.replace(
                opened,
                ui=dataclasses.replace(
                    opened.ui,
                    screen=dataclasses.replace(cast(PageScreen, opened.ui.screen), came_from=came),
                ),
            )
    return open_page(state, "overview")


def page_move(state: AppState, page: str, target: Callable[[int, int], int], by_key: bool = True) -> AppState:
    """The page's selection moved to the selectable row `target(position, count)` picks among them (project rows and
    separators are skipped); a key move scrolls the table to it again, any move starts the details over."""
    rows = page_rows(state, page)
    selectable = [index for index, row in enumerate(rows) if row.get("key") is not None]
    if not selectable:
        return state
    current = page_index(state, page, rows)
    position = selectable.index(current) if current in selectable else 0
    chosen = selectable[min(max(int(target(position, len(selectable))), 0), len(selectable) - 1)]
    key = rows[chosen]["key"]
    scroll = dict(state.scroll)
    if by_key:
        scroll.pop(PAGE_TABLES[page], None)
    if key != state.picked.get(page):
        scroll.pop("details", None)
        scroll.pop("events", None)
    picked = {**state.picked, page: key}
    if picked == state.picked and scroll == state.scroll:
        return state
    return dataclasses.replace(state, picked=picked, scroll=scroll)


def page_select(state: AppState, page: str, index: int) -> AppState:
    """A click on a page's row `index`: selected where it is selectable."""
    rows = page_rows(state, page)
    if not 0 <= index < len(rows) or rows[index].get("key") is None:
        return state
    selectable = [position for position, row in enumerate(rows) if row.get("key") is not None]
    return page_move(state, page, lambda _position, _count: selectable.index(index), by_key=False)


def leave_absent(state: AppState) -> AppState:
    """A page whose tool went away: the overview, with any action modal for the departed page dismissed."""
    if state.ui.page != "overview" and not tool(state, PAGE_TOOLS[state.ui.page]).present:
        return open_page(state, "overview")
    return state


def open_actions(state: AppState, now: float) -> AppState:
    """Enter, `a` or a right-click on a row: its action menu, or `no actions` in the footer."""
    found = applicable(state)
    if not found:
        return dataclasses.replace(state, notice=Notice("no actions", now, "dim"))
    return with_modal(state, ActionMenu(tuple(found)))


# Mode handlers

def same(state: AppState, _: str, __: float = 0.0) -> Result:
    return state, []


def quit_(state: AppState, _: str, __: float = 0.0) -> Result:
    return state, [QUIT]


def focus_step(step: int) -> Handler:
    """Tab and Shift-Tab: the next panel drawn, by columns, in the order the layout placed them (layout.tab_order());
    a focus whose panel is no longer drawn starts again from QUEUE (on a page, from its table)."""
    def handler(state: AppState, _: str, __: float) -> Result:
        if not _frame_matches_page(state):
            return state, []
        order = state.frame.order
        current = state.ui.focus
        if current not in order:
            return focus_panel(state, PAGE_TABLES[state.ui.page]), []
        return focus_panel(state, order[(order.index(current) + step) % len(order)]), []
    return handler


def select(target: Callable[[int, int], int]) -> Handler:
    """↑↓ and the page keys on QUEUE: the selection moves (state.move()), from the row it is on."""
    def handler(state: AppState, _: str, __: float) -> Result:
        return move(state, target(state.selection.index, len(items(state))), by_key=True), []
    return handler


def open_help(state: AppState, _: str, __: float = 0.0) -> Result:
    return with_modal(state, Help()), []


def open_news(state: AppState, _: str, __: float = 0.0) -> Result:
    """What's new, all tools, at its top; the tools are asked again for their news."""
    return with_modal(state, News()), [LOAD_NEWS]


open_changelog = open_news


def open_settings(state: AppState, _: str, __: float = 0.0) -> Result:
    return state, [OPEN_SETTINGS]


def open_actions_key(state: AppState, _: str, now: float) -> Result:
    return open_actions(state, now), []


def scroll_events(state: AppState, key: str, _: float = 0.0) -> Result:
    """A navigation key on the overview's focused EVENTS panel: moves the row cursor (fathers#152), which the scroll
    follows; Home goes back to the newest."""
    view = state.frame.tables.get("events")
    if not view:
        return state, []
    index, page = event_index(state), max(1, int(view["visible"]) - 1)
    moves = {"up": -1, "k": -1, "down": 1, "j": 1, "page_up": -page, "page_down": page}
    target = 0 if key in ("home", "g") else int(view["count"]) - 1 if key in ("end", "G") else index + moves[key]
    return event_move(state, target), []


def scroll_panel(state: AppState, key: str, _: float = 0.0) -> Result:
    """A navigation key on a focused scrollable panel (DETAILS or a page's EVENTS strip): a line, a page, the top or
    the bottom (#15), from the viewport the last frame drew; nothing to scroll takes the key all the same."""
    focus = state.ui.focus
    view = state.frame.tables.get(focus)
    if not view:
        return state, []
    visible, count = int(view["visible"]), int(view["count"])
    last = _layout.last_offset(count, visible)
    offset = state.scroll.get(focus, int(view["offset"]))
    moves = {"up": -1, "k": -1, "down": 1, "j": 1, "page_up": -max(1, visible - 1), "page_down": max(1, visible - 1)}
    offset = 0 if key in ("home", "g") else last if key in ("end", "G") else offset + moves[key]
    return dataclasses.replace(state, scroll={**state.scroll, focus: _layout.clamp_scroll(offset, count, visible)}), []


def events_shown(state: AppState) -> bool:
    return "events" in state.frame.tables


def switch_log(step: int) -> Handler:
    """EVENTS' log (fathers#152): the next one the title names, SLOTS LOG, LANES LOG, PROCS LOG, then FULL LOG, of
    the tools present. `f` anywhere EVENTS is drawn, ← → while it has the focus. The arrow on that side flashes
    (fathers#165): the reducer lights it as it lights a scrolled thumb."""
    def handler(state: AppState, _: str, __: float = 0.0) -> Result:
        if not events_shown(state):
            return state, []
        after = _events_view.cycled(state.model, state.ui.filter, step)
        if after is None:
            return state, []
        flash = Effect("flash", key=_events_view.SWITCH_FLASH[1 if step > 0 else -1])
        return filtered(state, dataclasses.replace(state.ui.filter, source=after)), [flash]
    return handler


def cycle_source(state: AppState, key: str, now: float = 0.0) -> Result:
    return switch_log(1)(state, key, now)


def toggle_raw(state: AppState, _: str, __: float = 0.0) -> Result:
    """`r`: every event as logged, a run's grant and end apart; again, one row per run."""
    if not events_shown(state):
        return state, []
    current = state.ui.filter
    return filtered(state, dataclasses.replace(current, raw=not current.raw)), []


def open_filter(state: AppState, _: str, __: float = 0.0) -> Result:
    """`/`: EVENTS' text field, in its bottom border, with the filter as it is."""
    if not events_shown(state):
        return state, []
    return with_modal(state, FilterEdit()), []


def filter_escape(state: AppState, _: str, __: float = 0.0) -> Result:
    current = state.ui.filter
    return filtered(state, _events_view.Filter(current.source, raw=current.raw), modal=None), []


def filter_commit(state: AppState, _: str, __: float = 0.0) -> Result:
    return with_modal(state, None), []


def filter_backspace(state: AppState, _: str, __: float = 0.0) -> Result:
    current = state.ui.filter
    return filtered(state, dataclasses.replace(current, text=current.text[:-1])), []


def filter_type(state: AppState, key: str, _: float = 0.0) -> Result:
    current = state.ui.filter
    if len(key) == 1 and key.isprintable() and len(current.text) < FILTER_MAX:
        return filtered(state, dataclasses.replace(current, text=current.text + key)), []
    return state, []


def clear_filter(state: AppState, _: str, __: float = 0.0) -> Result:
    """Esc on the overview screen: a text filter Enter kept is cleared (the source filter stays, as Esc in the field
    leaves it); nothing else happens."""
    if not events_shown(state) or not state.ui.filter.text:
        return state, []
    current = state.ui.filter
    return filtered(state, _events_view.Filter(current.source, raw=current.raw)), []


def go_page(page: str) -> Handler:
    """`1` `2` `3`: the overview, Lanes, Processes; a page whose tool is absent does nothing (fathers#85)."""
    def handler(state: AppState, _: str, __: float = 0.0) -> Result:
        return open_page(state, page), []
    return handler


def back(state: AppState, _: str, __: float = 0.0) -> Result:
    return go_back(state), []


def page_select_key(target: Callable[[int, int], int]) -> Handler:
    """↑↓ and the page keys on a page's table: its selection moves over the rows that can be selected."""
    def handler(state: AppState, _: str, __: float = 0.0) -> Result:
        return page_move(state, state.ui.page, target), []
    return handler


# Help and what's new

def scroll_overlay(state: AppState, key: str, _: float = 0.0) -> Result:
    overlay = cast(Union[Help, News], state.ui.modal)
    view = overlay_box(state, overlay)
    page, last = max(1, view.visible - 1), _layout.last_offset(view.total, view.visible)
    moves = {"j": 1, "down": 1, "k": -1, "up": -1, "page_down": page, " ": page, "page_up": -page}
    offset = 0 if key in ("g", "home") else last if key in ("G", "end") else view.offset + moves[key]
    return with_modal(
        state,
        dataclasses.replace(overlay, scroll=_layout.clamp_scroll(offset, view.total, view.visible)),
    ), []


def close(state: AppState, _: str, __: float = 0.0) -> Result:
    return with_modal(state, None), []


def news_tab(step: int) -> Handler:
    """←→, h l, Tab and Shift-Tab in what's new: the next tool filter, from its top."""
    def handler(state: AppState, _: str, __: float = 0.0) -> Result:
        overlay = cast(News, state.ui.modal)
        tab = (overlay.tab + step) % len(_overlays.filters(_overlays.shown_news(state.news)))
        return with_modal(state, News(tab)), []
    return handler


# Row actions: ActionMenu, Reason, Confirm

def pick(state: AppState, menu: ActionMenu, now: float) -> Result:
    """The menu's action picked: a park asks its reason first, a confirmed one asks, any other runs now; with none
    chosen yet, nothing."""
    if menu.cursor is None:
        return state, []
    action = menu.actions[menu.cursor]
    if action.reason:
        return with_modal(state, Reason(action)), []
    if action.confirm:
        return with_modal(state, Confirm(action)), []
    return run_action(state, action, "", now)


def run_action(state: AppState, action: Any, reason: str, now: float) -> Result:
    """The action handed to the app, which checks the row in a fresh snapshot and runs it in the background; the
    same one already in flight is not started twice."""
    key = action_key(action)
    if key in state.acting:
        return dataclasses.replace(
            with_modal(state, None),
            notice=Notice(f"{action.name}: already running", now, "dim"),
        ), []
    return dataclasses.replace(
        with_modal(state, None),
        notice=Notice(f"{action.name}…", now, "dim"),
        acting=state.acting | {key},
    ), [Effect("action", item=(action, reason))]


def cancelled(state: AppState, now: float, why: str = "nothing run") -> Result:
    return dataclasses.replace(with_modal(state, None), notice=Notice(why, now, "dim")), []


def cancel_action(state: AppState, _: str, now: float) -> Result:
    return cancelled(state, now)


def move_action_cursor(state: AppState, key: str, _: float) -> Result:
    overlay = cast(ActionMenu, state.ui.modal)
    last = len(overlay.actions) - 1
    at = overlay.cursor
    cursor = {
        "up": last if at is None else at - 1,
        "k": last if at is None else at - 1,
        "down": 0 if at is None else at + 1,
        "j": 0 if at is None else at + 1,
        "home": 0,
        "g": 0,
        "end": last,
        "G": last,
    }[key]
    return with_modal(state, dataclasses.replace(overlay, cursor=min(max(cursor, 0), last))), []


def pick_action(state: AppState, _: str, now: float) -> Result:
    return pick(state, cast(ActionMenu, state.ui.modal), now)


def reason_commit(state: AppState, _: str, now: float) -> Result:
    overlay = cast(Reason, state.ui.modal)
    if not overlay.text.strip():
        return cancelled(state, now, "no reason: nothing run")
    return with_modal(state, Confirm(overlay.action, overlay.text.strip())), []


def reason_backspace(state: AppState, _: str, __: float) -> Result:
    overlay = cast(Reason, state.ui.modal)
    return with_modal(state, dataclasses.replace(overlay, text=overlay.text[:-1])), []


def reason_type(state: AppState, key: str, _: float) -> Result:
    overlay = cast(Reason, state.ui.modal)
    if len(key) == 1 and key.isprintable() and len(overlay.text) < REASON_MAX:
        return with_modal(state, dataclasses.replace(overlay, text=overlay.text + key)), []
    return state, []


def confirm_action(state: AppState, _: str, now: float) -> Result:
    overlay = cast(Confirm, state.ui.modal)
    return run_action(state, overlay.action, overlay.reason, now)


# Settings (fathers#87): its keys act on the panel value through settings_model

Transition = Callable[[_settings.Panel, _settings.Context, str, float], _settings.Result]


def on_panel(transition: Transition) -> Handler:
    def handler(state: AppState, key: str, now: float) -> Result:
        panel, context, requests = transition(cast(SettingsPanel, state.ui.modal), settings_context(state), key, now)
        return settings_result(state, panel, context, requests)
    return handler


def keep(change: Callable[[_settings.Panel, _settings.Context, str, float], _settings.Panel]) -> Transition:
    def transition(panel: _settings.Panel, context: _settings.Context, key: str, now: float) -> _settings.Result:
        return change(panel, context, key, now), context, []
    return transition


def settings_escape(panel: _settings.Panel, context: _settings.Context, _: str, now: float) -> _settings.Result:
    """Esc or s: a pending change is cancelled first, and said; then Settings closes."""
    if panel.pending is not None:
        return _settings.cancel_pending(panel, context, now), context, []
    return panel, context, [_settings.Request("close")]


def settings_quit(panel: _settings.Panel, context: _settings.Context, _: str, __: float) -> _settings.Result:
    return panel, context, [_settings.Request("quit")]


def settings_enter(panel: _settings.Panel, context: _settings.Context, key: str, now: float) -> _settings.Result:
    """Enter saves a pending change now; on a choice it (and Space) picks the next value, on a number it edits."""
    row = _settings.focused(panel, context)
    if panel.pending is not None and key != " ":
        return _settings.commit_pending(panel, context, now)
    if row is not None and row.type in ("choice", "theme", "boolean"):
        return _settings.step(panel, context, 1, now)
    return _settings.begin(panel, context), context, []


def settings_digit(panel: _settings.Panel, context: _settings.Context, key: str, _: float) -> _settings.Result:
    return _settings.begin(panel, context, key), context, []


def settings_reset_all(panel: _settings.Panel, context: _settings.Context, _: str, now: float) -> _settings.Result:
    """D: once, it asks; again within the flash, the selected row's section is reset."""
    if panel.armed is not None and panel.armed_tool == panel.focus[0] and now - panel.armed < _settings.FLASH_SECONDS:
        return _settings.reset_section(panel, context, now)
    return _settings.arm(panel, now), context, []


def settings_step(direction: int) -> Transition:
    return lambda panel, context, _, now: _settings.step(panel, context, direction, now)


def settings_move(count: int) -> Transition:
    return lambda panel, context, _, now: _settings.move(panel, context, count, now)


def settings_edge(last: bool) -> Transition:
    return lambda panel, context, _, now: _settings.edge(panel, context, last, now)


def settings_page(direction: int) -> Transition:
    return keep(lambda panel, context, _, __: _settings.page(panel, context, direction))


def edit_cancel(panel: _settings.Panel, context: _settings.Context, _: str, now: float) -> _settings.Result:
    return _settings.say(_settings.replace(panel, editing=False), "Edit cancelled · nothing was saved", "dim",
                         now), context, []


def edit_commit(panel: _settings.Panel, context: _settings.Context, key: str, now: float) -> _settings.Result:
    """Enter saves the typed value; Tab saves it and moves on. One refused stays in the field."""
    panel, context, requests, saved = _settings.commit_typed(panel, context, now)
    if saved and key == "\t":
        panel, context, more = _settings.move(panel, context, 1, now)
        requests += more
    return panel, context, requests


def edit_type(panel: _settings.Panel, _: _settings.Context, key: str, __: float) -> _settings.Panel:
    if key in ("\x7f", "\b"):
        text = "" if panel.replace else panel.buffer[:-1]
    elif key == "\x15":  # Ctrl-U clears the field
        text = ""
    elif len(panel.buffer) < 16 or panel.replace:
        text = key if panel.replace else panel.buffer + key
    else:
        return panel
    return _settings.replace(panel, buffer=text, replace=False)


# The 14-mode table (fathers#22, fathers#225)

PAGE_KEYS: dict[str, Handler] = {"1": go_page("overview"), "2": go_page("lanes"), "3": go_page("processes")}
OVERVIEW_BASE: dict[str, Handler] = {
    "q": quit_, "Q": quit_, "f": cycle_source, "r": toggle_raw, "/": open_filter, "esc": clear_filter,
    "\t": focus_step(1), "back_tab": focus_step(-1),
    "?": open_help, "v": open_news, "s": open_settings,
    **PAGE_KEYS,
}
MAIN: dict[str, Handler] = {
    **OVERVIEW_BASE,
    "\r": open_actions_key, "\n": open_actions_key, "a": open_actions_key,
    "j": select(lambda index, _: index + 1), "down": select(lambda index, _: index + 1),
    "k": select(lambda index, _: index - 1), "up": select(lambda index, _: index - 1),
    "g": select(lambda _, __: 0), "home": select(lambda _, __: 0), "page_up": select(lambda _, __: 0),
    "G": select(lambda _, count: count - 1), "end": select(lambda _, count: count - 1),
    "page_down": select(lambda _, count: count - 1),
}
OVERVIEW_EVENTS: dict[str, Handler] = {
    **OVERVIEW_BASE,
    **{key: scroll_events for key in NAVIGATION},
    "left": switch_log(-1), "h": switch_log(-1), "right": switch_log(1), "l": switch_log(1),
}
OVERVIEW_DETAILS: dict[str, Handler] = {
    **OVERVIEW_BASE,
    **{key: scroll_panel for key in NAVIGATION},
}
PAGE_BASE: dict[str, Handler] = {
    "q": quit_, "Q": quit_, "esc": back, "\t": focus_step(1), "back_tab": focus_step(-1),
    "?": open_help, "v": open_news, "s": open_settings, **PAGE_KEYS,
}
PAGE: dict[str, Handler] = {
    **PAGE_BASE,
    "\r": open_actions_key, "\n": open_actions_key, "a": open_actions_key,
    "j": page_select_key(lambda index, _: index + 1), "down": page_select_key(lambda index, _: index + 1),
    "k": page_select_key(lambda index, _: index - 1), "up": page_select_key(lambda index, _: index - 1),
    "g": page_select_key(lambda _, __: 0), "home": page_select_key(lambda _, __: 0),
    "page_up": page_select_key(lambda _, __: 0), "G": page_select_key(lambda _, count: count - 1),
    "end": page_select_key(lambda _, count: count - 1), "page_down": page_select_key(lambda _, count: count - 1),
}
PAGE_PANEL: dict[str, Handler] = {
    **PAGE_BASE,
    **{key: scroll_panel for key in NAVIGATION},
}
FILTER: dict[str, Handler] = {
    "esc": filter_escape,
    "\r": filter_commit, "\n": filter_commit,
    "\x7f": filter_backspace, "\x08": filter_backspace,
}
ACTIONS: dict[str, Handler] = {
    "q": quit_, "Q": quit_, "esc": cancel_action,
    **{key: move_action_cursor for key in ("up", "k", "down", "j", "home", "g", "end", "G")},
    "\r": pick_action, "\n": pick_action, " ": pick_action,
}
REASON: dict[str, Handler] = {
    "esc": cancel_action,
    "\r": reason_commit, "\n": reason_commit,
    "\x7f": reason_backspace, "\x08": reason_backspace,
}
CONFIRM: dict[str, Handler] = {
    "y": confirm_action, "Y": confirm_action,
    "n": cancel_action, "N": cancel_action, "esc": cancel_action,
}
OVERLAY_SCROLL: dict[str, Handler] = {key: scroll_overlay for key in (*NAVIGATION, " ")}
HELP: dict[str, Handler] = {
    **OVERLAY_SCROLL, "?": close, "esc": close, "v": open_news, "q": quit_, "Q": quit_,
}
NEWS: dict[str, Handler] = {
    **OVERLAY_SCROLL, "v": close, "esc": close, "?": open_help, "q": quit_, "Q": quit_,
    "left": news_tab(-1), "h": news_tab(-1), "back_tab": news_tab(-1),
    "right": news_tab(1), "l": news_tab(1), "\t": news_tab(1),
}
CHANGELOG = NEWS
DIGITS = tuple("0123456789.")
SETTINGS: dict[str, Handler] = {
    "esc": on_panel(settings_escape), "s": on_panel(settings_escape),
    "q": on_panel(settings_quit), "Q": on_panel(settings_quit),
    **{key: on_panel(settings_move(1)) for key in ("down", "j", "\t")},
    **{key: on_panel(settings_move(-1)) for key in ("up", "k", "back_tab")},
    "home": on_panel(settings_edge(False)), "g": on_panel(settings_edge(False)),
    "end": on_panel(settings_edge(True)), "G": on_panel(settings_edge(True)),
    **{key: on_panel(settings_step(-1)) for key in ("left", "h", "-")},
    **{key: on_panel(settings_step(1)) for key in ("right", "l", "+")},
    "\r": on_panel(settings_enter), "\n": on_panel(settings_enter), " ": on_panel(settings_enter),
    **{key: on_panel(settings_digit) for key in DIGITS},
    "d": on_panel(lambda panel, context, _, now: _settings.reset_row(panel, context, now)),
    "D": on_panel(settings_reset_all),
    "page_up": on_panel(settings_page(-1)), "page_down": on_panel(settings_page(1)),
}
SETTINGS_EDIT: dict[str, Handler] = {
    "esc": on_panel(edit_cancel),
    **{key: on_panel(edit_commit) for key in ("\r", "\n", "\t")},
    **{key: on_panel(keep(edit_type)) for key in ("\x7f", "\b", "\x15", *DIGITS)},
}
SETTINGS_SMALL: dict[str, Handler] = {
    "esc": on_panel(settings_escape), "s": on_panel(settings_escape),
    "q": on_panel(settings_quit), "Q": on_panel(settings_quit),
}

MODES: dict[Mode, dict[str, Handler]] = {
    Mode.MAIN: MAIN,
    Mode.OVERVIEW_EVENTS: OVERVIEW_EVENTS,
    Mode.OVERVIEW_DETAILS: OVERVIEW_DETAILS,
    Mode.PAGE: PAGE,
    Mode.PAGE_PANEL: PAGE_PANEL,
    Mode.FILTER: FILTER,
    Mode.ACTIONS: ACTIONS,
    Mode.REASON: REASON,
    Mode.CONFIRM: CONFIRM,
    Mode.HELP: HELP,
    Mode.NEWS: NEWS,
    Mode.SETTINGS: SETTINGS,
    Mode.SETTINGS_EDIT: SETTINGS_EDIT,
    Mode.SETTINGS_SMALL: SETTINGS_SMALL,
}
TABLE = MODES
SETTINGS_TABLE: dict[Mode, dict[str, Handler]] = {
    Mode.SETTINGS: SETTINGS,
    Mode.SETTINGS_EDIT: SETTINGS_EDIT,
    Mode.SETTINGS_SMALL: SETTINGS_SMALL,
}


def press(state: AppState, key: str, now: float) -> Result:
    """A key in the current mode: a hotkey by its US-QWERTY position (#13) unless text or a Settings number is being
    typed (`RAW_MODES`; `SETTINGS_SMALL` still translates hotkeys even if `editing=True`)."""
    if key == "\x0c":  # Ctrl-L, in every mode: every row written again (fathers#143)
        return state, [REPAINT]
    current = mode_of(state)
    if current not in RAW_MODES:
        key = _keys.hotkey(key)
    if current == Mode.SETTINGS:
        panel = cast(SettingsPanel, state.ui.modal)
        if panel.armed is not None and key != "D":
            state = with_modal(state, _settings.replace(panel, armed=None, armed_tool="", flash=None))
    handler = MODES[current].get(key)
    if handler is not None:
        return handler(state, key, now)
    if current == Mode.FILTER:
        return filter_type(state, key, now)
    if current == Mode.REASON:
        return reason_type(state, key, now)
    return state, []


# The mouse

def settings_click(state: AppState, event: Mouse, now: float) -> Result:
    """A click selects a row, a click on the selected one changes or edits it, a click on a Settings footer hint
    presses its key, and a click outside closes (as Esc does) unless a value is typed or pending; the wheel scrolls."""
    panel = cast(SettingsPanel, state.ui.modal)
    context = settings_context(state)
    view = _settings.geometry(panel, context)
    y, x, height, width = view.rect
    code = event.button & ~28
    if not (x <= event.x < x + width and y <= event.y < y + height):
        if event.pressed and code == LEFT and not panel.editing and panel.pending is None:
            return settings_result(state, panel, context, [_settings.Request("close")])
        return state, []
    if event.pressed and code == LEFT:
        hint = state.frame.hint_at(event.x, event.y)
        if hint is not None:
            return press(state, hint, now)
    if _settings.small(panel, context) or not event.pressed or not (x < event.x < x + width - 1
                                                             and y < event.y < y + height - 1):
        return state, []
    if code in (WHEEL_UP, WHEEL_DOWN):
        panel = _settings.scroll(panel, context, WHEEL if code == WHEEL_DOWN else -WHEEL)
        return settings_result(state, panel, context, [])
    if code != LEFT:
        return state, []
    target = next((focus for focus, row in view.hits() if row == event.y), None)
    if target is None:
        return state, []
    if target != panel.focus:
        return settings_result(state, *_settings.move_to(panel, context, target, now, cancel_edit=True))
    if panel.editing:
        return state, []
    row = _settings.focused(panel, context)
    return press(state, " " if row is not None and row.type in ("choice", "theme", "boolean") else "\r", now)


def click(state: AppState, event: Mouse, now: float) -> Result:
    """The mouse in the current mode. In an overlay the wheel scrolls it, a click on one of what's new's tool filters
    shows that tool, a click on an overlay's own footer hint presses its key, and a click outside closes or cancels
    it (clicking the main footer `q` while `Reason` is open is a click outside and cancels `Reason`); on the screen
    the footer's `Godfather what's new?` opens what's new, a click focuses the panel it lands in and selects a row,
    and the wheel scrolls the panel under the pointer. Shift+drag stays the terminal's own selection."""
    modal, code = state.ui.modal, event.button & ~28  # Shift / Alt / Ctrl do not change the button
    if isinstance(modal, FilterEdit) and event.pressed and code in (LEFT, RIGHT):
        state, modal = with_modal(state, None), None
    if isinstance(modal, SettingsPanel):
        return settings_click(state, event, now)
    if isinstance(modal, (ActionMenu, Reason, Confirm)):
        if not event.pressed or code != LEFT:
            return state, []
        box = _overlays.action_box(modal, state.frame.width, state.frame.height)
        if not (box.x <= event.x < box.x + box.width and box.y <= event.y < box.y + box.height):
            return cancelled(state, now)
        hint = state.frame.hint_at(event.x, event.y)
        if hint is not None:
            return press(state, hint, now)
        row = event.y - (box.y + 2)
        if isinstance(modal, ActionMenu) and 0 <= row < len(modal.actions):
            return pick(state, dataclasses.replace(modal, cursor=row), now)
        return state, []
    if isinstance(modal, (Help, News)):
        view = overlay_box(state, modal)
        inside = view.x <= event.x < view.x + view.width and view.y <= event.y < view.y + view.height
        if not inside:
            return close(state, "", now) if event.pressed and code == LEFT else (state, [])
        if event.pressed and code == LEFT:
            hint = state.frame.hint_at(event.x, event.y)
            if hint is not None:
                return press(state, hint, now)
            tab = state.frame.filter_at(event.x, event.y) if isinstance(modal, News) else None
            if tab is not None:  # a tool filter in the top border: as ←→ do
                return with_modal(state, News(tab)), []
        if event.pressed and code in (WHEEL_UP, WHEEL_DOWN):
            offset = view.offset + (WHEEL if code == WHEEL_DOWN else -WHEEL)
            scroll = _layout.clamp_scroll(offset, view.total, view.visible)
            return with_modal(state, dataclasses.replace(modal, scroll=scroll)), []
        return state, []
    if event.pressed and code == LEFT:
        hint = state.frame.hint_at(event.x, event.y)
        if hint is not None:
            return press(state, hint, now)
    if event.pressed and code == RIGHT:  # a right-click on a row: select it, then its actions
        if state.frame.hint_at(event.x, event.y) is not None:
            return state, []
        selected, effects = click(state, Mouse(LEFT, event.x, event.y, True), now)
        if selected.ui.modal is None and mode_of(selected) in (Mode.MAIN, Mode.PAGE):
            return open_actions(selected, now), effects
        return selected, effects
    if not event.pressed:
        return state, []
    at = state.frame.version_at
    if code == LEFT and at and event.y == at[0] and at[1] <= event.x < at[2]:
        return open_news(state, "", now)  # the footer's what's new opens it
    if not _frame_matches_page(state):
        return state, []
    target = state.frame.target_at(event.x, event.y) if code == LEFT else None
    if target is not None:  # a link: a summary, DETAILS' lane or process row, a page's link back
        return follow_link(state, target), []
    title = state.frame.switch
    if code == LEFT and title and event.y == title[0] and title[1] <= event.x < title[2]:
        state, effects = switch_log(1)(state, "", now)  # EVENTS' title switches its log
        return focus_panel(state, "events"), effects
    if code == LEFT:
        hit = panel_at(state, event.x, event.y)
        if hit is not None:
            state = focus_panel(state, hit)
    for name, view in reversed(list(state.frame.tables.items())):
        if not (view["x"] < event.x < view["x"] + view["width"] - 1
                and view["y"] < event.y < view["y"] + view["height"] - 1):
            continue
        if code in (WHEEL_UP, WHEEL_DOWN):
            offset = state.scroll.get(name, int(view["offset"])) + (WHEEL if code == WHEEL_DOWN else -WHEEL)
            scroll = _layout.clamp_scroll(offset, int(view["count"]), int(view["visible"]))
            return dataclasses.replace(state, scroll={**state.scroll, name: scroll}), []
        row = event.y - int(view["top"])
        if code == LEFT and 0 <= row < min(int(view["visible"]), int(view["count"]) - int(view["offset"])):
            position = int(view["offset"]) + row
            row_items = cast(Union[list[Any], None], view.get("row_items"))
            if name in ("lanes", "procs") and row_items is not None and row_items[position] is not None:
                return page_select(state, state.ui.page, int(row_items[position])), []
            if row_items is not None and row_items[position] is not None:
                return move(state, int(row_items[position])), []
            if row_items is None and view.get("base") is not None:
                return move(state, int(view["base"]) + position), []
            if name == "events" and state.ui.page == "overview":  # the cursor on it, DETAILS shows it (fathers#152)
                return event_move(state, position), []
        return state, []
    return state, []


def panel_at(state: AppState, x: int, y: int) -> Union[str, None]:
    """The focusable panel under a click."""
    for name in _layout.FOCUSABLE:
        view = state.frame.tables.get(name)
        if view and view["x"] <= x < view["x"] + view["width"] and view["y"] <= y < view["y"] + view["height"]:
            return name
    return None


def dispatch(state: AppState, event: Union[Key, Mouse], now: float) -> Result:
    """A key or a mouse event at `now` (Settings times its changes): a click on a footer hint is the hint's key."""
    if isinstance(event, Mouse):
        return click(state, event, now)
    return press(state, event.key, now)


def reduce(state: AppState, event: Event, now: float) -> Result:
    """The one reducer: the state after `event` at `now`, and what the app must do."""
    effects: list[Effect] = []
    if isinstance(event, (Key, Mouse)):  # only these scroll: a refresh that moves EVENTS down is no flash
        old, before, panels = state, offsets(state), offsets(state, False)  # Settings' offset before it moves
        state, effects = dispatch(state, event, now)
        state, marked = closed_news(old, state, QUIT in effects)
        effects = marked + effects  # saved before the app quits
        same_overlay = overlay_identity(state) == overlay_identity(old)  # an overlay opened or switched is not scrolled
        moved = scrolled(before if same_overlay else panels, offsets(state, same_overlay))
        moved |= {effect.key for effect in effects if effect.kind == "flash"}  # a log switch's arrow (fathers#165)
        effects = [effect for effect in effects if effect.kind != "flash"]
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
        context = settings_context(state)
        requests = [_settings.Request("open")] if event.loading else []
        state, effects = settings_result(state, _settings.opened(event.sections, context, now, event.loading), context,
                                         requests)
    elif isinstance(event, (SettingsSections, SettingsRead, SettingsChecked, SettingsWritten)):
        state, effects = settings_event(state, event, now)
    elif isinstance(event, ActionDone):  # its line in the footer, the source polled at once (SPEC 24)
        line = dashboard_text(event.line) or ("done" if event.ok else "failed")
        state = dataclasses.replace(state, notice=Notice(line, now, "ok" if event.ok else "bad"),
                                    acting=state.acting - {event.key})
        effects = [Effect("poll", key=event.tool)] if event.tool else []
    elif isinstance(event, HandOff):  # the pager runs on the screen's thread, which owns the terminal
        effects = [Effect("hand-off", item=(event.action, event.key))]
    elif isinstance(event, ToolLoaded):
        state = dataclasses.replace(state, tools={**state.tools, event.tool.name: event.tool})
    elif isinstance(event, NewsLoaded):
        state = news_loaded(state, event.news)
    elif isinstance(event, SettingsFailed):
        state = dataclasses.replace(state, notice=Notice(dashboard_text(event.error), now))
    elif isinstance(event, Drawn):
        screen = state.ui.screen
        home = PAGE_TABLES[screen.page]  # QUEUE, or the page's table: where the focus goes back to
        focus = screen.focus if screen.focus == home or screen.focus in event.frame.tables else home
        modal = state.ui.modal
        if "events" not in event.frame.tables and isinstance(modal, FilterEdit):
            modal = None
        if isinstance(screen, OverviewScreen):
            shows = focus if focus in ("requests", "events") else screen.shows
            if shows == "events" and event.frame.width and "events" not in event.frame.tables:
                shows = "requests"
            next_screen: ScreenState = OverviewScreen(focus, shows)
        else:
            next_screen = dataclasses.replace(screen, focus=focus)
        state = dataclasses.replace(
            state,
            frame=event.frame,
            ui=dataclasses.replace(state.ui, screen=next_screen, modal=modal),
        )
        if isinstance(state.ui.modal, SettingsPanel):  # another size: the focused row kept in view
            state = with_modal(state, _settings.place(state.ui.modal, settings_context(state)))
    else:
        second = event.moment.replace(microsecond=0) if event.moment is not None else state.moment
        if second != state.moment:
            state = dataclasses.replace(state, moment=second)
        if isinstance(state.ui.modal, SettingsPanel):  # a change that settled is saved, reads come due
            state, effects = settings_event(state, event, now)
    state, more = settle(leave_absent(state), now)
    return state, effects + more
