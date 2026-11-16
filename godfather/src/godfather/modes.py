"""Keys and the mouse by mode (fathers#22): which key does what in which mode is this table, not the order of a chain
of branches. A mode is the open overlay, or the main screen with QUEUE or another panel focused. Every handler takes
the state and returns the new state and its effects; a key a mode does not list does nothing there.

    main     QUEUE focused: ↑↓ and the page keys move the selection; Tab moves the focus; ? v s open an overlay
    panel    DETAILS or EVENTS focused: the same, but ↑↓ and the page keys scroll the panel, also with nothing to
             scroll, so they never fall through to QUEUE (the audit's focus-while-loading case)
    help     ↑↓ and the page keys scroll; ? and Esc close, v switches to what's new; q quits
    changelog  what's new: the same, v and Esc close, ? switches to help; ←→ and Tab switch its tool filter;
             opening it asks every tool again, closing it marks everything read (state.closed_news())
    settings ↑↓ move the cursor through every section's rows, ←→ change a value (saved once the keys settle),
             Enter saves now or edits a number, a digit starts typing one, d resets a row, D twice its section,
             Esc cancels a pending change, then closes; the page keys and the wheel scroll (settings_model.py)
    settings-edit  a value typed: digits and a dot, Backspace, Ctrl-U clears, Enter or Tab saves, Esc cancels;
             typed values are not taken as hotkeys
    settings-small Settings too small to show: only Esc, s and q
    page     a page's table focused (fathers#85: Lanes on `2`, Processes on `3`): ↑↓ and the page keys move its
             selection over the rows that can be selected; Esc goes back where a link came from, else to the
             overview; `1` `2` `3` switch pages on the main screen and every page; Tab moves the focus
    page-panel  a page's DETAILS or EVENTS strip focused: the same, but ↑↓ and the page keys scroll it
    actions  a row's action menu (fathers#88), opened by Enter, `a` or a right-click on QUEUE's or a page's row:
             ↑↓ move, Enter picks, Esc closes, q quits; a click picks, a click outside closes
    reason   the reason a park asks for: what is typed, Backspace, Enter goes on (empty cancels), Esc cancels
    confirm  an irreversible action: `y` runs it, `n` or Esc cancels; nothing is preselected
    filter   EVENTS' text field, opened by `/` (fathers#84): what is typed goes into it, Backspace deletes, Enter
             keeps the filter, Esc clears it; on the main screen and a panel `f` cycles EVENTS' log
             (SLOTS LOG, LANES LOG, PROCS LOG, ALL LOG), as ← → do while EVENTS has the focus and a click on its
             title; `r` shows every event as logged instead of one row per run (fathers#152)

A key is a hotkey by its US-QWERTY position (#13) everywhere but while a Settings value is typed. A click on a footer
hint is that hint's key, in every mode."""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Union, cast

from . import keys as _keys
from . import layout as _layout
from . import settings_model as _settings
from .pages import follow_link, go_back, open_actions, open_page, page_move, page_select
from .state import (LOAD_NEWS, OPEN_SETTINGS, PAGE_TABLES, QUIT, ActionMenu, AppState, Changelog, Confirm, Effect, Help,
                    Key, Mouse, Notice, Reason, Result, SettingsOverlay, event_index, event_move, filtered, items, move,
                    overlay_box, settings_context, settings_result)
from . import events as _events_view
from . import overlay_text as _overlays

Handler = Callable[[AppState, str], Result]
NAVIGATION = ("up", "k", "down", "j", "page_up", "page_down", "home", "g", "end", "G")
WHEEL = 3  # rows a wheel step scrolls
LEFT, RIGHT, WHEEL_UP, WHEEL_DOWN = 0, 2, 64, 65


def same(state: AppState, _: str) -> Result:
    return state, []


def quit_(state: AppState, _: str) -> Result:
    return state, [QUIT]


# The main screen

def focus_step(step: int) -> Handler:
    """Tab and Shift-Tab: the next panel drawn, by columns, in the order the layout placed them (layout.tab_order());
    a focus whose panel is no longer drawn starts again from QUEUE (on a page, from its table)."""
    def handler(state: AppState, _: str) -> Result:
        order = state.frame.order
        if state.focus not in order:
            return dataclasses.replace(state, focus=PAGE_TABLES[state.page]), []
        return dataclasses.replace(state, focus=order[(order.index(state.focus) + step) % len(order)]), []
    return handler


def select(target: Callable[[int, int], int]) -> Handler:
    """↑↓ and the page keys on QUEUE: the selection moves (state.move()), from the row it is on."""
    def handler(state: AppState, _: str) -> Result:
        return move(state, target(state.selection.index, len(items(state))), by_key=True), []
    return handler


def open_help(state: AppState, _: str) -> Result:
    return dataclasses.replace(state, overlay=Help()), []


def open_changelog(state: AppState, _: str) -> Result:
    """What's new, all tools, at its top; the tools are asked again for their news."""
    return dataclasses.replace(state, overlay=Changelog()), [LOAD_NEWS]


def open_settings(state: AppState, _: str) -> Result:
    return state, [OPEN_SETTINGS]


def scroll_panel(state: AppState, key: str) -> Result:
    """A navigation key on the focused DETAILS: a line, a page, the top or the bottom (#15), from the viewport the
    last frame drew; nothing to scroll (still loading, empty) takes the key all the same. On EVENTS it moves the
    row cursor (fathers#152), which the scroll follows; Home goes back to the newest."""
    view = state.frame.tables.get(state.focus)
    if not view:
        return state, []
    if state.focus == "events" and state.page == "overview":  # a page's strip only scrolls
        index, page = event_index(state), max(1, int(view["visible"]) - 1)
        moves = {"up": -1, "k": -1, "down": 1, "j": 1, "page_up": -page, "page_down": page}
        target = 0 if key in ("home", "g") else int(view["count"]) - 1 if key in ("end", "G") else index + moves[key]
        return event_move(state, target), []
    visible, count = int(view["visible"]), int(view["count"])
    last = _layout.last_offset(count, visible)
    offset = state.scroll.get(state.focus, int(view["offset"]))
    moves = {"up": -1, "k": -1, "down": 1, "j": 1, "page_up": -max(1, visible - 1), "page_down": max(1, visible - 1)}
    offset = 0 if key in ("home", "g") else last if key in ("end", "G") else offset + moves[key]
    return dataclasses.replace(state, scroll={**state.scroll, state.focus: _layout.clamp_scroll(offset, count,
                                                                                                visible)}), []


def events_shown(state: AppState) -> bool:
    return "events" in state.frame.tables


def switch_log(step: int) -> Handler:
    """EVENTS' log (fathers#152): the next one the title names, SLOTS LOG, LANES LOG, PROCS LOG, then ALL LOG, of
    the tools present. `f` anywhere EVENTS is drawn, ← → while it has the focus."""
    def handler(state: AppState, _: str) -> Result:
        if not events_shown(state):
            return state, []
        after = _events_view.cycled(state.model, state.events_filter, step)
        if after is None:
            return state, []
        return filtered(state, dataclasses.replace(state.events_filter, source=after)), []
    return handler


def cycle_source(state: AppState, key: str) -> Result:
    return switch_log(1)(state, key)


def arrows(step: int) -> Handler:
    """← → on a focused panel: EVENTS switches its log; DETAILS has nothing to switch."""
    def handler(state: AppState, key: str) -> Result:
        return switch_log(step)(state, key) if state.focus == "events" else (state, [])
    return handler


def toggle_raw(state: AppState, _: str) -> Result:
    """`r`: every event as logged, a run's grant and end apart; again, one row per run."""
    if not events_shown(state):
        return state, []
    current = state.events_filter
    return filtered(state, dataclasses.replace(current, raw=not current.raw)), []


def open_filter(state: AppState, _: str) -> Result:
    """`/`: EVENTS' text field, in its bottom border, with the filter as it is."""
    if not events_shown(state):
        return state, []
    return dataclasses.replace(state, events_filter=dataclasses.replace(state.events_filter, editing=True)), []


def type_filter(state: AppState, key: str) -> Result:
    """A key in EVENTS' text field: a character is typed, Backspace deletes one, Enter keeps the filter and closes
    the field, Esc clears it and closes the field; other keys do nothing."""
    current = state.events_filter
    if key == "esc":
        return filtered(state, _events_view.Filter(current.source, raw=current.raw)), []
    if key in ("\r", "\n"):
        return dataclasses.replace(state, events_filter=dataclasses.replace(current, editing=False)), []
    if key in ("\x7f", "\x08"):
        return filtered(state, dataclasses.replace(current, text=current.text[:-1])), []
    if len(key) == 1 and key.isprintable() and len(current.text) < FILTER_MAX:
        return filtered(state, dataclasses.replace(current, text=current.text + key)), []
    return state, []


def clear_filter(state: AppState, _: str) -> Result:
    """Esc on the main screen: a text filter Enter kept is cleared (the source filter stays, as Esc in the field
    leaves it); nothing else happens."""
    if not events_shown(state) or not state.events_filter.text:
        return state, []
    return filtered(state, _events_view.Filter(state.events_filter.source, raw=state.events_filter.raw)), []


FILTER_MAX = 40  # characters EVENTS' text filter takes


def go_page(page: str) -> Handler:
    """`1` `2` `3`: the overview, Lanes, Processes; a page whose tool is absent does nothing (fathers#85)."""
    def handler(state: AppState, _: str) -> Result:
        return open_page(state, page), []
    return handler


def back(state: AppState, _: str) -> Result:
    return go_back(state), []


def page_select_key(target: Callable[[int, int], int]) -> Handler:
    """↑↓ and the page keys on a page's table: its selection moves over the rows that can be selected."""
    def handler(state: AppState, _: str) -> Result:
        return page_move(state, state.page, target), []
    return handler


PAGE_KEYS: dict[str, Handler] = {"1": go_page("overview"), "2": go_page("lanes"), "3": go_page("processes")}

MAIN: dict[str, Handler] = {
    "q": quit_, "Q": quit_, "f": cycle_source, "r": toggle_raw, "/": open_filter, "esc": clear_filter,
    "\t": focus_step(1), "back_tab": focus_step(-1),
    "?": open_help, "v": open_changelog, "s": open_settings,
    "j": select(lambda index, _: index + 1), "down": select(lambda index, _: index + 1),
    "k": select(lambda index, _: index - 1), "up": select(lambda index, _: index - 1),
    "g": select(lambda _, __: 0), "home": select(lambda _, __: 0), "page_up": select(lambda _, __: 0),
    "G": select(lambda _, count: count - 1), "end": select(lambda _, count: count - 1),
    "page_down": select(lambda _, count: count - 1), **PAGE_KEYS,
}
PANEL: dict[str, Handler] = {**MAIN, **{key: scroll_panel for key in NAVIGATION},
                              "left": arrows(-1), "h": arrows(-1), "right": arrows(1), "l": arrows(1)}
PAGE: dict[str, Handler] = {
    "q": quit_, "Q": quit_, "esc": back, "\t": focus_step(1), "back_tab": focus_step(-1),
    "?": open_help, "v": open_changelog, "s": open_settings, **PAGE_KEYS,
    "j": page_select_key(lambda index, _: index + 1), "down": page_select_key(lambda index, _: index + 1),
    "k": page_select_key(lambda index, _: index - 1), "up": page_select_key(lambda index, _: index - 1),
    "g": page_select_key(lambda _, __: 0), "home": page_select_key(lambda _, __: 0),
    "page_up": page_select_key(lambda _, __: 0), "G": page_select_key(lambda _, count: count - 1),
    "end": page_select_key(lambda _, count: count - 1), "page_down": page_select_key(lambda _, count: count - 1),
}
PAGE_PANEL: dict[str, Handler] = {**PAGE, **{key: scroll_panel for key in NAVIGATION}}


# Help and the changelog: each owns its scroll; ? and v switch between them

def scroll_overlay(state: AppState, key: str) -> Result:
    overlay = cast(Union[Help, Changelog], state.overlay)
    view = overlay_box(state, overlay)
    page, last = max(1, view.visible - 1), _layout.last_offset(view.total, view.visible)
    moves = {"j": 1, "down": 1, "k": -1, "up": -1, "page_down": page, " ": page, "page_up": -page}
    offset = 0 if key in ("g", "home") else last if key in ("G", "end") else view.offset + moves[key]
    return dataclasses.replace(state, overlay=dataclasses.replace(
        overlay, scroll=_layout.clamp_scroll(offset, view.total, view.visible))), []


def close(state: AppState, _: str) -> Result:
    return dataclasses.replace(state, overlay=None), []


def changelog_tab(step: int) -> Handler:
    """←→, h l, Tab and Shift-Tab in what's new: the next tool filter, from its top."""
    def handler(state: AppState, _: str) -> Result:
        overlay = cast(Changelog, state.overlay)
        tab = (overlay.tab + step) % len(_overlays.filters(_overlays.shown_news(state.news)))
        return dataclasses.replace(state, overlay=Changelog(tab)), []
    return handler


OVERLAY_SCROLL = {key: scroll_overlay for key in (*NAVIGATION, " ")}
HELP: dict[str, Handler] = {**OVERLAY_SCROLL, "?": close, "esc": close, "v": open_changelog, "q": quit_, "Q": quit_}
CHANGELOG: dict[str, Handler] = {
    **OVERLAY_SCROLL, "v": close, "esc": close, "?": open_help, "q": quit_, "Q": quit_,
    "left": changelog_tab(-1), "h": changelog_tab(-1), "back_tab": changelog_tab(-1),
    "right": changelog_tab(1), "l": changelog_tab(1), "\t": changelog_tab(1),
}


# Settings (fathers#87): its keys act on the panel value through settings_model

Transition = Callable[[_settings.Panel, _settings.Context, str, float], _settings.Result]
SettingsHandler = Callable[[AppState, str, float], Result]


def on_panel(transition: Transition) -> SettingsHandler:
    def handler(state: AppState, key: str, now: float) -> Result:
        panel, context, requests = transition(cast(SettingsOverlay, state.overlay), settings_context(state), key, now)
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


DIGITS = tuple("0123456789.")
SETTINGS: dict[str, SettingsHandler] = {
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
SETTINGS_EDIT: dict[str, SettingsHandler] = {
    "esc": on_panel(edit_cancel),
    **{key: on_panel(edit_commit) for key in ("\r", "\n", "\t")},
    **{key: on_panel(keep(edit_type)) for key in ("\x7f", "\b", "\x15", *DIGITS)},
}
SETTINGS_SMALL: dict[str, SettingsHandler] = {
    "esc": on_panel(settings_escape), "s": on_panel(settings_escape),
    "q": on_panel(settings_quit), "Q": on_panel(settings_quit),
}
SETTINGS_TABLE: dict[str, dict[str, SettingsHandler]] = {
    "settings": SETTINGS, "settings-edit": SETTINGS_EDIT, "settings-small": SETTINGS_SMALL}


def settings_press(state: AppState, key: str, now: float) -> Result:
    """A key in Settings, from its mode's table; any key but D withdraws the reset question."""
    panel = cast(SettingsOverlay, state.overlay)
    which = mode(state)
    if which == "settings" and panel.armed is not None and key != "D":
        state = dataclasses.replace(state, overlay=_settings.replace(panel, armed=None, armed_tool="", flash=None))
    handler = SETTINGS_TABLE[which].get(key)
    return handler(state, key, now) if handler else (state, [])


def settings_click(state: AppState, event: Mouse, now: float) -> Result:
    """A click selects a row, a click on the selected one changes or edits it, a click outside closes (as Esc
    does) unless a value is typed or pending; the wheel scrolls."""
    panel = cast(SettingsOverlay, state.overlay)
    context = settings_context(state)
    view = _settings.geometry(panel, context)
    y, x, height, width = view.rect
    code = event.button & ~28
    if not (x <= event.x < x + width and y <= event.y < y + height):
        if event.pressed and code == LEFT and not panel.editing and panel.pending is None:
            return settings_result(state, panel, context, [_settings.Request("close")])
        return state, []
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
    return settings_press(state, " " if row is not None and row.type in ("choice", "theme", "boolean") else "\r",
                          now)


TABLE: dict[str, dict[str, Handler]] = {"main": MAIN, "panel": PANEL, "help": HELP, "changelog": CHANGELOG,
                                        "page": PAGE, "page-panel": PAGE_PANEL}


def mode(state: AppState) -> str:
    """The mode keys are read in: the open overlay (Settings by whether a value is typed or it is too small to
    show), else the main screen by its focus."""
    overlay = state.overlay
    if isinstance(overlay, SettingsOverlay):
        if _settings.small(overlay, settings_context(state)):
            return "settings-small"
        return "settings-edit" if overlay.editing else "settings"
    if overlay is not None:
        return str(overlay.kind)
    if state.page != "overview":
        return "page" if state.focus == PAGE_TABLES[state.page] else "page-panel"
    if state.events_filter.editing:
        return "filter"
    return "main" if state.focus == "requests" else "panel"


REASON_MAX = 120  # characters a park reason takes


def pick(state: AppState, menu: ActionMenu, now: float) -> Result:
    """The menu's action picked: a park asks its reason first, a confirmed one asks, any other runs now."""
    action = menu.actions[menu.cursor]
    if action.reason:
        return dataclasses.replace(state, overlay=Reason(action)), []
    if action.confirm:
        return dataclasses.replace(state, overlay=Confirm(action)), []
    return run_action(state, action, "", now)


def run_action(state: AppState, action: Any, reason: str, now: float) -> Result:
    """The action handed to the app, which checks the row in a fresh snapshot and runs it in the background."""
    return dataclasses.replace(state, overlay=None, notice=Notice(f"{action.name}…", now, "dim")), [
        Effect("action", item=(action, reason))]


def cancelled(state: AppState, now: float, why: str = "nothing run") -> Result:
    return dataclasses.replace(state, overlay=None, notice=Notice(why, now, "dim")), []


def action_press(state: AppState, key: str, now: float) -> Result:
    """A key in the action menu, the reason field or the confirmation (fathers#88)."""
    overlay = state.overlay
    if isinstance(overlay, ActionMenu):
        hot = _keys.hotkey(key)
        if hot in ("q", "Q"):
            return state, [QUIT]
        if hot == "esc":
            return cancelled(state, now)
        if hot in ("up", "k", "down", "j", "home", "g", "end", "G"):
            last = len(overlay.actions) - 1
            cursor = {"up": overlay.cursor - 1, "k": overlay.cursor - 1, "down": overlay.cursor + 1,
                      "j": overlay.cursor + 1, "home": 0, "g": 0, "end": last, "G": last}[hot]
            return dataclasses.replace(state, overlay=dataclasses.replace(overlay, cursor=min(max(cursor, 0), last))), []
        if hot in ("\r", "\n", " "):
            return pick(state, overlay, now)
        return state, []
    if isinstance(overlay, Reason):  # typed text stays as typed
        if key == "esc":
            return cancelled(state, now)
        if key in ("\r", "\n"):
            if not overlay.text.strip():
                return cancelled(state, now, "no reason: nothing run")
            return dataclasses.replace(state, overlay=Confirm(overlay.action, overlay.text.strip())), []
        if key in ("\x7f", "\x08"):
            return dataclasses.replace(state, overlay=dataclasses.replace(overlay, text=overlay.text[:-1])), []
        if len(key) == 1 and key.isprintable() and len(overlay.text) < REASON_MAX:
            return dataclasses.replace(state, overlay=dataclasses.replace(overlay, text=overlay.text + key)), []
        return state, []
    assert isinstance(overlay, Confirm)
    hot = _keys.hotkey(key)
    if hot in ("y", "Y"):
        return run_action(state, overlay.action, overlay.reason, now)
    if hot in ("n", "N", "esc"):
        return cancelled(state, now)
    return state, []


def press(state: AppState, key: str, now: float) -> Result:
    """A key in the current mode: a hotkey by its US-QWERTY position (#13) unless a Settings value is typed."""
    overlay = state.overlay
    if isinstance(overlay, SettingsOverlay):
        return settings_press(state, key if overlay.editing else _keys.hotkey(key), now)
    if isinstance(overlay, (ActionMenu, Reason, Confirm)):
        return action_press(state, key, now)
    if overlay is None and _keys.hotkey(key) in ("\r", "\n", "a") and mode(state) in ("main", "page"):
        return open_actions(state, now), []  # a row's actions (fathers#88)
    if overlay is None and state.page == "overview" and state.events_filter.editing:  # typed text stays as typed
        return type_filter(state, key)
    key = _keys.hotkey(key)
    return TABLE[mode(state)].get(key, same)(state, key)


# The mouse

def click(state: AppState, event: Mouse, now: float) -> Result:
    """The mouse in the current mode. In an overlay the wheel scrolls it, a click on one of what's new's tool filters
    shows that tool and a click outside closes it; on the main
    screen the footer's `Godfather what's new?` opens what's new, a click focuses the panel it lands in and selects a QUEUE row,
    and the wheel scrolls the panel under the pointer. Shift+drag stays the terminal's own selection."""
    overlay, code = state.overlay, event.button & ~28  # Shift / Alt / Ctrl do not change the button
    if isinstance(overlay, SettingsOverlay):
        return settings_click(state, event, now)
    if isinstance(overlay, (ActionMenu, Reason, Confirm)):
        if not event.pressed or code != LEFT:
            return state, []
        box = _overlays.action_box(overlay, state.frame.width, state.frame.height)
        if not (box.x <= event.x < box.x + box.width and box.y <= event.y < box.y + box.height):
            return cancelled(state, now)
        row = event.y - (box.y + 2)
        if isinstance(overlay, ActionMenu) and 0 <= row < len(overlay.actions):
            return pick(state, dataclasses.replace(overlay, cursor=row), now)
        return state, []
    if overlay is None and event.pressed and code == RIGHT:  # a right-click on a row: select it, then its actions
        selected, effects = click(state, Mouse(LEFT, event.x, event.y, True), now)
        if selected.overlay is None and mode(selected) in ("main", "page"):
            return open_actions(selected, now), effects
        return selected, effects
    if overlay is not None:
        view = overlay_box(state, overlay)
        inside = view.x <= event.x < view.x + view.width and view.y <= event.y < view.y + view.height
        if not inside:
            return (close(state, "") if event.pressed and code == LEFT else (state, []))
        tab = state.frame.filter_at(event.x, event.y) if isinstance(overlay, Changelog) else None
        if event.pressed and code == LEFT and tab is not None:  # a tool filter in the top border: as ←→ do
            return dataclasses.replace(state, overlay=Changelog(tab)), []
        if event.pressed and code in (WHEEL_UP, WHEEL_DOWN):
            offset = view.offset + (WHEEL if code == WHEEL_DOWN else -WHEEL)
            scroll = _layout.clamp_scroll(offset, view.total, view.visible)
            return dataclasses.replace(state, overlay=dataclasses.replace(overlay, scroll=scroll)), []
        return state, []
    if not event.pressed:
        return state, []
    at = state.frame.version_at
    if code == LEFT and at and event.y == at[0] and at[1] <= event.x < at[2]:
        return open_changelog(state, "")  # the footer's what's new opens it
    target = state.frame.target_at(event.x, event.y) if code == LEFT else None
    if target is not None:  # a link: a summary, DETAILS' lane or process row, a page's link back
        return follow_link(state, target), []
    title = state.frame.switch
    if code == LEFT and title and event.y == title[0] and title[1] <= event.x < title[2]:
        state, effects = switch_log(1)(state, "")  # EVENTS' title switches its log
        return dataclasses.replace(state, focus="events"), effects
    if code == LEFT:
        state = dataclasses.replace(state, focus=panel_at(state, event.x, event.y) or state.focus)
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
                return page_select(state, state.page, int(row_items[position])), []
            if row_items is not None and row_items[position] is not None:
                return move(state, int(row_items[position])), []
            if row_items is None and view.get("base") is not None:
                return move(state, int(view["base"]) + position), []
            if name == "events" and state.page == "overview":  # the cursor on it, DETAILS shows it (fathers#152)
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
        hint = state.frame.hint_at(event.x, event.y) if event.pressed and event.button & ~28 == LEFT else None
        if hint is None:
            return click(state, event, now)
        return press(state, hint, now)
    return press(state, event.key, now)

