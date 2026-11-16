"""The pages one key away from the overview (fathers#85, fathers#86) as the reducer moves through them: each page's
rows from its tool's snapshot, the row picked by its key (AppState.picked, the selection EVENTS' cursor uses too),
the row the selected request links to, opening a page, following a link and going back, and the actions the row the
keys act on offers (fathers#88; their rules are actions.py's). Layout layer: the reducer and modes read it; it reads
the state and the adapters, never a view."""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Mapping, Optional, cast

from . import actions as _actions
from .adapters import junkfather as _junkfather
from .adapters import lanefather as _lanefather
from .state import (PAGE_TABLES, PAGE_TOOLS, ActionMenu, AppState, Notice, Target, Tool, details_key, entries, items,
                    move)


def applicable(state: AppState) -> list[_actions.Action]:
    """The actions of the row the keys act on (fathers#88): QUEUE's selected request on the overview, a page's
    selected row on a page; those its tool advertises and that apply to it now."""
    if state.page == "overview":
        item = selected_item(state)
        if item is None or state.model is None:
            return []
        uid = state.model.get("uid")
        return _actions.for_request(item, cast(list[dict[str, Any]], state.model["waiters"]),
                                    state.model.get("actions") or (), uid if isinstance(uid, int) else None)
    rows = page_rows(state, state.page)
    index = page_index(state, state.page, rows)
    if index is None:
        return []
    if state.page == "processes":
        return _actions.for_process(rows[index], tool(state, "junkfather").actions)
    return _actions.for_lane(rows[index], tool(state, "lanefather").actions)


def open_actions(state: AppState, now: float) -> AppState:
    """Enter, `a` or a right-click on a row: its action menu, or `no actions` in the footer."""
    found = applicable(state)
    if not found:
        return dataclasses.replace(state, notice=Notice("no actions", now, "dim"))
    return dataclasses.replace(state, overlay=ActionMenu(tuple(found)))


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


def page_rows(state: AppState, page: str) -> list[Mapping[str, Any]]:
    """A page's table rows, from its tool's last good snapshot; a row without a `key` is not selectable."""
    if page == "lanes":
        return _lanefather.page_rows(tool(state, "lanefather").snapshot)
    if page == "processes":
        return _junkfather.page_rows(tool(state, "junkfather").snapshot)
    return []


def page_index(state: AppState, page: str, rows: Optional[list[Mapping[str, Any]]] = None) -> Optional[int]:
    """The selected row of a page: the one picked while it is there, else the first selectable one."""
    rows = page_rows(state, page) if rows is None else rows
    keys = [row.get("key") for row in rows]
    picked = state.picked.get(page)
    if picked is not None and picked in keys:
        return keys.index(picked)
    return next((index for index, key in enumerate(keys) if key is not None), None)


def linked(state: AppState, page: str) -> Any:
    """The row of `page` the selected request links to: its lane (fathers#85), its first owned process
    (fathers#86); None where it has none."""
    item = selected_item(state)
    if item is None:
        return None
    lane = _lanefather.lane_of(item, tool(state, "lanefather").snapshot, selected_cwd(state))
    if page == "lanes":
        return lane["key"] if lane is not None else None
    if page == "processes":  # its first owned entry (fathers#86)
        owned = _junkfather.of_request(item, tool(state, "junkfather").snapshot, lane)
        return owned[0]["key"] if owned else None
    return None


PAGE_SCROLLS = ("details", "events", "lanes", "procs")  # what a page shows: scrolled again from the top on a switch


def open_page(state: AppState, page: str, key: Any = None, link: bool = False) -> AppState:
    """`page` shown (fathers#85): from the overview with the row the selected request links to selected, else `key`,
    else the row picked before; a link (`link`) remembers where it came from for Esc. A page whose tool is absent
    does not open."""
    if page != "overview" and not tool(state, PAGE_TOOLS[page]).present:
        return state
    if page == state.page and key is None:
        return state
    picked = dict(state.picked)
    if page != "overview":
        found = key if key is not None else linked(state, page) if state.page == "overview" else None
        if found is not None:
            picked[page] = tuple(cast(list[Any], found)) if isinstance(found, list) else found
    if not link:
        came: tuple[str, ...] = ()
    else:
        came = (*state.came_from, state.page) if page != state.page else state.came_from
    scroll = {name: offset for name, offset in state.scroll.items() if name not in PAGE_SCROLLS}
    return dataclasses.replace(state, page=page, picked=picked, came_from=came, focus=PAGE_TABLES[page],
                               scroll=scroll, events_filter=dataclasses.replace(state.events_filter, editing=False))


def follow_link(state: AppState, target: Target) -> AppState:
    """A click on a link: to a page's row, or back to the overview with a request selected."""
    page, key = target[0], target[1] if len(target) > 1 else None
    if page == "overview":
        state = open_page(state, "overview")
        known = entries(state)
        return move(state, known[1].index(key), known=known) if key in known[1] else state
    return open_page(state, page, key, link=True)


def go_back(state: AppState) -> AppState:
    """Esc on a page: the page a link came from, else the overview."""
    if state.page == "overview":
        return state
    back = state.came_from[-1] if state.came_from else "overview"
    opened = open_page(dataclasses.replace(state, came_from=()), back, state.picked.get(back) if back != "overview"
                       else None)
    return dataclasses.replace(opened, came_from=state.came_from[:-1])


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
    """A page whose tool went away: the overview."""
    if state.page != "overview" and not tool(state, PAGE_TOOLS[state.page]).present:
        return open_page(state, "overview")
    return state
