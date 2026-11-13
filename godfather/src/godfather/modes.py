"""Keys and the mouse by mode (fathers#22): which key does what in which mode is this table, not the order of a chain
of branches. A mode is the open overlay, or the main screen with QUEUE or another panel focused. Every handler takes
the state and returns the new state and its effects; a key a mode does not list does nothing there.

    main     QUEUE focused: ↑↓ and the page keys move the selection; Tab moves the focus; ? v s open an overlay
    panel    DETAILS or EVENTS focused: the same, but ↑↓ and the page keys scroll the panel, also with nothing to
             scroll, so they never fall through to QUEUE (the audit's focus-while-loading case)
    help     ↑↓ and the page keys scroll; ? and Esc close, v switches to what's new; q quits
    changelog  what's new: the same, v and Esc close, ? switches to help; ←→ and Tab switch its tool filter;
             opening it asks every tool again, closing it marks everything read (state.closed_news())
    settings the Settings panel's own keys (views/settings.py); typed values are not taken as hotkeys
    filter   EVENTS' text field, opened by `/` (fathers#84): what is typed goes into it, Backspace deletes, Enter
             keeps the filter, Esc clears it; on the main screen and a panel `f` cycles EVENTS' source filter

A key is a hotkey by its US-QWERTY position (#13) everywhere but while a Settings value is typed. A click on a footer
hint is that hint's key, in every mode."""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Union, cast

from . import keys as _keys
from . import layout as _layout
from .state import (LOAD_NEWS, OPEN_SETTINGS, QUIT, AppState, Changelog, Help, Key, Mouse, Result, SettingsOverlay, filtered,
                    items, move, overlay_box, settings_effects)
from .views import events as _events_view
from .views import overlays as _overlays

Handler = Callable[[AppState, str], Result]
NAVIGATION = ("up", "k", "down", "j", "page_up", "page_down", "home", "g", "end", "G")
WHEEL = 3  # rows a wheel step scrolls
LEFT, WHEEL_UP, WHEEL_DOWN = 0, 64, 65


def same(state: AppState, _: str) -> Result:
    return state, []


def quit_(state: AppState, _: str) -> Result:
    return state, [QUIT]


# The main screen

def focus_step(step: int) -> Handler:
    """Tab and Shift-Tab: the next panel drawn, by columns, in the order the layout placed them (layout.tab_order());
    a focus whose panel is no longer drawn starts again from QUEUE."""
    def handler(state: AppState, _: str) -> Result:
        order = state.frame.order
        if state.focus not in order:
            return dataclasses.replace(state, focus="requests"), []
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
    """A navigation key on the focused DETAILS or EVENTS: a line, a page, the top or the bottom (#15), from the
    viewport the last frame drew; nothing to scroll (still loading, empty) takes the key all the same."""
    view = state.frame.tables.get(state.focus)
    if not view:
        return state, []
    visible, count = int(view["visible"]), int(view["count"])
    last = max(0, count - visible)
    offset = state.scroll.get(state.focus, int(view["offset"]))
    moves = {"up": -1, "k": -1, "down": 1, "j": 1, "page_up": -max(1, visible - 1), "page_down": max(1, visible - 1)}
    offset = 0 if key in ("home", "g") else last if key in ("end", "G") else offset + moves[key]
    return dataclasses.replace(state, scroll={**state.scroll, state.focus: max(0, min(last, offset))}), []


def events_shown(state: AppState) -> bool:
    return "events" in state.frame.tables


def cycle_source(state: AppState, _: str) -> Result:
    """`f`: EVENTS' source filter, all, then each present source in the order slotfather, lanefather, junkfather."""
    if not events_shown(state):
        return state, []
    names = [None, *(name for name, _ in _events_view.present(state.model))]
    now = state.events_filter.source
    after = names[(names.index(now) + 1) % len(names)] if now in names else None
    return filtered(state, dataclasses.replace(state.events_filter, source=after)), []


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
        return filtered(state, _events_view.Filter(current.source)), []
    if key in ("\r", "\n"):
        return dataclasses.replace(state, events_filter=dataclasses.replace(current, editing=False)), []
    if key in ("\x7f", "\x08"):
        return filtered(state, dataclasses.replace(current, text=current.text[:-1])), []
    if len(key) == 1 and key.isprintable() and len(current.text) < FILTER_MAX:
        return filtered(state, dataclasses.replace(current, text=current.text + key)), []
    return state, []


FILTER_MAX = 40  # characters EVENTS' text filter takes

MAIN: dict[str, Handler] = {
    "q": quit_, "Q": quit_, "f": cycle_source, "/": open_filter,
    "\t": focus_step(1), "back_tab": focus_step(-1),
    "?": open_help, "v": open_changelog, "s": open_settings,
    "j": select(lambda index, _: index + 1), "down": select(lambda index, _: index + 1),
    "k": select(lambda index, _: index - 1), "up": select(lambda index, _: index - 1),
    "g": select(lambda _, __: 0), "home": select(lambda _, __: 0), "page_up": select(lambda _, __: 0),
    "G": select(lambda _, count: count - 1), "end": select(lambda _, count: count - 1),
    "page_down": select(lambda _, count: count - 1),
}
PANEL: dict[str, Handler] = {**MAIN, **{key: scroll_panel for key in NAVIGATION}}


# Help and the changelog: each owns its scroll; ? and v switch between them

def scroll_overlay(state: AppState, key: str) -> Result:
    overlay = cast(Union[Help, Changelog], state.overlay)
    view = overlay_box(state, overlay)
    page, last = max(1, view.visible - 1), max(0, view.total - view.visible)
    moves = {"j": 1, "down": 1, "k": -1, "up": -1, "page_down": page, " ": page, "page_up": -page}
    offset = 0 if key in ("g", "home") else last if key in ("G", "end") else view.offset + moves[key]
    return dataclasses.replace(state, overlay=dataclasses.replace(overlay, scroll=max(0, min(offset, last)))), []


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


def settings_key(state: AppState, key: str) -> Result:
    """Every key goes to the Settings panel, which answers close, quit, changed or handled."""
    panel = cast(SettingsOverlay, state.overlay).panel
    return settings_effects(state, str(panel.key(key)))


TABLE: dict[str, dict[str, Handler]] = {"main": MAIN, "panel": PANEL, "help": HELP, "changelog": CHANGELOG}


def mode(state: AppState) -> str:
    """The mode keys are read in: the open overlay, else the main screen by its focus."""
    if state.overlay is not None:
        return state.overlay.kind
    if state.events_filter.editing:
        return "filter"
    return "main" if state.focus == "requests" else "panel"


def press(state: AppState, key: str) -> Result:
    """A key in the current mode: a hotkey by its US-QWERTY position (#13) unless a Settings value is typed."""
    overlay = state.overlay
    if isinstance(overlay, SettingsOverlay):
        return settings_key(state, key if overlay.panel.editing else _keys.hotkey(key))
    if overlay is None and state.events_filter.editing:  # typed text stays as typed
        return type_filter(state, key)
    key = _keys.hotkey(key)
    return TABLE[mode(state)].get(key, same)(state, key)


# The mouse

def click(state: AppState, event: Mouse) -> Result:
    """The mouse in the current mode. In an overlay the wheel scrolls it, a click on one of what's new's tool filters
    shows that tool and a click outside closes it; on the main
    screen the footer's `Godfather what's new?` opens what's new, a click focuses the panel it lands in and selects a QUEUE row,
    and the wheel scrolls the panel under the pointer. Shift+drag stays the terminal's own selection."""
    overlay, code = state.overlay, event.button & ~28  # Shift / Alt / Ctrl do not change the button
    if isinstance(overlay, SettingsOverlay):
        return settings_effects(state, str(overlay.panel.mouse(("mouse", event.button, event.x, event.y,
                                                                event.pressed))))
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
            scroll = max(0, min(offset, max(0, view.total - view.visible)))
            return dataclasses.replace(state, overlay=dataclasses.replace(overlay, scroll=scroll)), []
        return state, []
    if not event.pressed:
        return state, []
    at = state.frame.version_at
    if code == LEFT and at and event.y == at[0] and at[1] <= event.x < at[2]:
        return open_changelog(state, "")  # the footer's what's new opens it
    if code == LEFT:
        state = dataclasses.replace(state, focus=panel_at(state, event.x, event.y) or state.focus)
    for name, view in reversed(list(state.frame.tables.items())):
        if not (view["x"] < event.x < view["x"] + view["width"] - 1
                and view["y"] < event.y < view["y"] + view["height"] - 1):
            continue
        if code in (WHEEL_UP, WHEEL_DOWN):
            offset = state.scroll.get(name, int(view["offset"])) + (WHEEL if code == WHEEL_DOWN else -WHEEL)
            scroll = max(0, min(offset, max(0, int(view["count"]) - int(view["visible"]))))
            return dataclasses.replace(state, scroll={**state.scroll, name: scroll}), []
        row = event.y - int(view["top"])
        if code == LEFT and 0 <= row < min(int(view["visible"]), int(view["count"]) - int(view["offset"])):
            position = int(view["offset"]) + row
            row_items = cast(Union[list[Any], None], view.get("row_items"))
            if row_items is not None and row_items[position] is not None:
                return move(state, int(row_items[position])), []
            if row_items is None and view.get("base") is not None:
                return move(state, int(view["base"]) + position), []
        return state, []
    return state, []


def panel_at(state: AppState, x: int, y: int) -> Union[str, None]:
    """The focusable panel under a click."""
    for name in _layout.FOCUSABLE:
        view = state.frame.tables.get(name)
        if view and view["x"] <= x < view["x"] + view["width"] and view["y"] <= y < view["y"] + view["height"]:
            return name
    return None


def dispatch(state: AppState, event: Union[Key, Mouse]) -> Result:
    """A key or a mouse event: a click on a footer hint is the hint's key."""
    if isinstance(event, Mouse):
        hint = state.frame.hint_at(event.x, event.y) if event.pressed and event.button & ~28 == LEFT else None
        if hint is None:
            return click(state, event)
        return press(state, hint)
    return press(state, event.key)

