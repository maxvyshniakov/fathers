"""The overview, Slotfather's dashboard screen moved unchanged: SLOTS, QUEUE, DETAILS, EVENTS and MACHINE where
layout.overview() places them, the footer, and the overlay open over them. It draws the model the Slotfather adapter
decodes from `slotfather status --json --full` (docs/contracts/slotfather.md) on a tui.Canvas; the live view and
--once write that canvas as the same escape sequences, so they are the same picture. render() draws an app state
(state.py); dashboard_draw() is the same picture from its parts. Both are functions of what they are given
(fathers#138): the moment comes from the state, the theme and marks with the canvas, and nothing is written back."""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, Mapping, Optional, cast

from .. import actions as _actions
from .. import layout as _layout
from .. import version as _version
from ..tui import Canvas
from ..text import clock
from ..events import Filter, newest_first
from ..widgets import Footer, Panel, Part
from . import common as _common
from . import details as _details
from . import events as _events
from . import machine as _machine
from . import overlays as _overlays
from . import pages as _pages
from ..adapters import junkfather as _junkfather
from . import queue as _queue
from . import settings as _settings_view
from .common import Model

if TYPE_CHECKING:
    from ..state import AppState

# The footer's hints in their order: navigation, panels, configuration, help, quit last; a narrow footer drops them
# (widgets.FOOTER_STEPS), help and quit last to go. On a scrollable panel the arrows scroll.
FOOTER_HINTS: tuple[Part, ...] = (("↑↓", "select"), ("tab", "panel"), ("s", "settings"), ("?", "help"), ("q", "quit"))


def footer_hints(focus: str, pages: str = "", actions: bool = False) -> list[Part]:
    """The footer's hints; with EVENTS focused, ← → switch its log (fathers#152); on PROCESSES with an actionable
    row selected, `a actions` opens its action menu; `pages` the page keys of the tools present (`1 2 3`,
    fathers#85), none where only the overview is."""
    found: list[Part] = [("↑↓", "select" if focus in ("requests", "events", "lanes", "procs") else "scroll")]
    if focus == "events":
        found.append(("←→", "log"))
    if focus == "procs" and actions:
        found.append(("a", "actions"))
    found.append(FOOTER_HINTS[1])
    if pages:
        found.append((pages, "pages"))
    return [*found, *FOOTER_HINTS[2:]]


def proc_actions(page: str, page_row: Optional[int], tools: Optional[dict[str, Any]]) -> Optional[bool]:
    """Whether the selected PROCESSES row has actions: True/False on a selectable row, None elsewhere."""
    if page != "processes" or page_row is None or not tools or "junkfather" not in tools:
        return None
    tool = tools["junkfather"]
    if not tool.readable:
        return None
    rows = _junkfather.page_rows(tool.snapshot)
    if not 0 <= page_row < len(rows) or rows[page_row]["kind"] == "separator":
        return None
    return bool(_actions.for_process(rows[page_row], tool.actions))


def page_keys(tools: Optional[dict[str, Any]]) -> str:
    """`1 2 3`: the overview's key and those of the pages whose tool is present; empty with none."""
    keys = [key for page, key in _pages.PAGE_KEYS.items()
            if (tools or {}).get("lanefather" if page == "lanes" else "junkfather") is not None
            and (tools or {})["lanefather" if page == "lanes" else "junkfather"].present]
    return " ".join(["1", *keys]) if keys else ""


WHATS_NEW = "what's new?"


def draw_footer(canvas: Canvas, model: Optional[Model], error: Optional[str], message: Optional[str],
                moment: Optional[datetime.datetime] = None, pages: str = "", message_style: str = "bad",
                actions: Optional[bool] = None) -> None:
    """`Godfather what's new?` (fathers#102: the name in the title style; what's new? always muted like the key
    hints, unread or not, which only the overlay marks (owner, 2026-11-13); ↻ after it
    while a newer install waits for a restart; a click opens what's new), the hints, then the refresh failure or a
    notice (fathers#24: a notice never says the refresh failed, nor makes the status stale), then the last update and
    the status."""
    installed = _common.stale_install(model)
    if message:
        note: Part = (message, message_style)
    elif error:
        note = (f"refresh failed: {error}", "bad")
    elif canvas.focus == "procs" and actions is False:
        note = ("no actions", "dim")
    else:
        note = ("", "")
    Footer(canvas).draw([(_version.NAME, "title"), (" " + WHATS_NEW + (" ↻" if installed else ""), "dim")],
                        footer_hints(canvas.focus, pages, bool(actions) and not message and not error),
                        ("● stale", "bad") if error else ("● live", "ok"),
                        clock(model["updated"], moment) if model else "", note)


def dashboard_draw(canvas: Canvas, model: Optional[Model], selected: int = 0, overlay: Any = None,
                   error: Optional[str] = None, cwd: Optional[str] = None, scroll: Optional[dict[str, int]] = None,
                   notice: Optional[list[str]] = None, message: Optional[str] = None,
                   failure: Optional[str] = None, news: Any = None, read: Optional[dict[str, str]] = None,
                   moment: Optional[datetime.datetime] = None, events_filter: Optional[_events.Filter] = None,
                   settings: Any = None, event: Optional[int] = None, tools: Optional[dict[str, Any]] = None,
                   page: str = "overview", page_row: Optional[int] = None, message_style: str = "bad") -> None:
    """The whole screen at `moment` (where None, the model's own update time: the screen never reads the clock).
    `overlay` is the one open (state.Help, Changelog or SettingsOverlay); `error` the refresh's
    failure, `message` a passing notice; `notice` stands in for the panels when Slotfather is absent or unsupported;
    `failure` is why the selected request's details did not load. A stale model (`stale_since`) is drawn dimmed, with
    the time it went stale in SLOTS' border. `news` and `read` are what's new and its read versions; `events_filter`
    EVENTS' source and text filters (fathers#84); `settings` what Settings reads beside its panel
    (settings_model.Context: this dashboard's own settings, the refresh error); `event` EVENTS' selected row (newest
    first) while DETAILS shows it instead of the selected request (fathers#152). `tools` are the other tools as
    sources (state.Tool by name), `page` the page shown and `page_row` its selected row (fathers#85)."""
    canvas.scroll = scroll if scroll is not None else {}
    if moment is None and model is not None:
        moment = cast(Optional[datetime.datetime], model.get("updated"))
    draw_footer(canvas, model, error, message, moment, page_keys(tools), message_style,
                proc_actions(page, page_row, tools))
    if notice:
        draw_notice(canvas, notice)
        model = None
    elif model is None:
        canvas.put(2, 3, f"Queue unavailable: {error}" if error else "Reading the queue…",
                   "bad" if error else "dim", canvas.width - 6)
    if model is not None and page != "overview":
        draw_page(canvas, page, tools or {}, page_row, model, moment)
    elif model is not None:
        draw_panels(canvas, model, selected, cwd, failure, cast(datetime.datetime, moment), events_filter, event,
                    tools)
    if overlay is not None:
        draw_overlay(canvas, overlay, model, news, read, settings)


def draw_panels(canvas: Canvas, model: Model, selected: int, cwd: Optional[str], failure: Optional[str],
                moment: datetime.datetime, events_filter: Optional[_events.Filter] = None,
                event: Optional[int] = None, tools: Optional[dict[str, Any]] = None) -> None:
    screen = _layout.overview(canvas.width, canvas.height, _queue.queue_rows(model), _layout.machine_height,
                              details_wanted(canvas, model, moment, events_filter, event))
    panels = screen.panels
    canvas.order = screen.order
    if "slots" in panels:
        Panel(canvas, panels["slots"]).frame("SLOTS", _queue.slots_note(model))
        _pages.draw_summaries(canvas, panels["slots"], tools or {}, SUMMARIES)
    _queue.draw_slots(canvas, screen.slots, model)
    if screen.note_row is not None:
        canvas.put(screen.note_row, screen.slots.x, "Enlarge the terminal for leases and the queue", "dim",
                   screen.slots.width)
    if "events" not in panels:  # one column without EVENTS: DETAILS has only QUEUE's entry to show
        event = None
    for name, rect in panels.items():
        if name == "requests":
            _queue.draw_requests(canvas, rect, model, selected, moment)
        elif name == "events":
            _events.draw_events(canvas, rect, model, moment, events_filter, event)
        elif name == "details" and event is not None:
            rows = newest_first(model, events_filter or Filter())
            _details.draw_event_detail(canvas, rect, rows[event] if 0 <= event < len(rows) else None, moment)
        elif name == "details":
            _details.draw_detail(canvas, rect, model, selected, cwd, moment, failure, readable(tools, "lanefather"),
                                 readable(tools, "junkfather"))
        elif name == "machine":
            _machine.draw_machine(canvas, rect, model)
    if model.get("stale_since"):  # the last good picture, dimmed, until a snapshot arrives again
        dim_panels(canvas)


def details_wanted(canvas: Canvas, model: Model, moment: datetime.datetime, events_filter: Optional[_events.Filter],
                   event: Optional[int]) -> int:
    """The rows DETAILS asks the layout for: its key fields in its frame, the most any QUEUE entry has; with the
    queue empty, the shown event's facts, else its least."""
    rows = _details.key_rows(model, moment, _common.marks_of(canvas), canvas.theme.track)
    if not rows and event is not None:
        found = newest_first(model, events_filter or Filter())
        if 0 <= event < len(found):
            rows = len(_details.event_facts(found[event], moment, _common.marks_of(canvas)))
    return _layout.framed(rows) if rows else _layout.DETAILS_MIN


SUMMARIES = {"lanes": _pages.lanes_summary, "processes": _pages.procs_summary}  # SLOTS' bottom border, by page


def readable(tools: Optional[dict[str, Any]], name: str) -> Optional[dict[str, Any]]:
    """A tool's snapshot where it has one to show (dimmed while stale), else None."""
    tool = (tools or {}).get(name)
    return cast(dict[str, Any], tool.snapshot) if tool is not None and tool.readable else None


def draw_page(canvas: Canvas, page: str, tools: dict[str, Any], row: Optional[int], model: Model,
              moment: Optional[datetime.datetime]) -> None:
    """A page instead of the overview's panels (fathers#85)."""
    procs = readable(tools, "junkfather")
    if page == "lanes" and "lanefather" in tools:
        elapsed = _junkfather.elapsed(procs, moment)

        def extra(lane: Mapping[str, Any]) -> list[Any]:  # its processes (fathers#86)
            return _pages.process_links(_junkfather.of_lane(lane, procs), elapsed) if procs is not None else []

        _pages.draw_lanes(canvas, tools["lanefather"], row, model, moment, extra)
    elif page == "processes" and "junkfather" in tools:
        _pages.draw_processes(canvas, tools["junkfather"], row, model, readable(tools, "lanefather"), moment)


def draw_overlay(canvas: Canvas, overlay: Any, model: Optional[Model], news: Any = None,
                 read: Optional[dict[str, str]] = None, settings: Any = None) -> None:
    kind = str(overlay.kind)
    if kind in ("actions", "reason", "confirm"):  # fathers#88
        _overlays.draw_actions(canvas, overlay)
    elif kind == "settings":
        from .. import settings_model as _model

        context = settings if settings is not None else _model.Context()
        _settings_view.draw(canvas, overlay, context._replace(model=model))
    elif kind == "changelog":
        _overlays.draw_changelog(canvas, int(overlay.tab), int(overlay.scroll), news, read)
    else:
        _overlays.draw_help(canvas, int(overlay.scroll))


def dim_panels(canvas: Canvas) -> None:
    """Every cell above the footer in the muted token: a stale snapshot's picture."""
    for row in canvas.cells[:canvas.height - 1]:
        for cell in row:
            cell[1] = "dim"


def draw_notice(canvas: Canvas, lines: list[str]) -> None:
    """One panel instead of the overview: the tools Godfather looked for, or why Slotfather cannot be shown."""
    width, height = canvas.width, canvas.height
    if width < _layout.NOTICE_WIDTH or height < _layout.NOTICE_HEIGHT:
        canvas.put(0, 1, lines[0], "bad", max(0, width - 2))
        return
    panel = Panel(canvas, _layout.notice(width, height, len(lines))).frame("GODFATHER")
    for row, line in enumerate(lines[:panel.inner.height]):
        panel.put(row, line, "text" if row == 0 else "dim")


def render(canvas: Canvas, state: AppState, now: float, notice: Optional[list[str]] = None) -> None:
    """An app state drawn at its moment: the focus, the details' spinner while they load, the thumbs that flash, then
    the screen. The selected entry's lane is in the model already (the reducer joins it, state.settle())."""
    from .. import pages as _pages_state
    from .. import state as _state

    found = _state.items(state)
    index, cwd, failure = state.selection.index, None, None
    canvas.focus, canvas.fresh_events, canvas.glow = state.focus, state.fresh_events, _state.glowing(state, now)
    event = _state.event_index(state) if state.shows == "events" and state.page == "overview" else None
    if found and event is None:
        item = found[index]
        known = state.details.get(_state.details_key(item, cast(_state.EntryId, state.selection.key)))
        if known is None:
            canvas.loading = int(now * 10)  # a spinner at ten frames a second
        else:
            cwd, failure = known.cwd, known.error
    dashboard_draw(canvas, state.model, index, state.overlay, state.refresh.error, cwd, state.scroll, notice,
                   state.notice.text if state.notice else None, failure, state.news, state.read, state.moment,
                   state.events_filter, _settings_view.context_of(state, canvas.width, canvas.height), event,
                   state.tools, state.page,
                   _pages_state.page_index(state, state.page) if state.page != "overview" else None,
                   state.notice.style if state.notice else "bad")
