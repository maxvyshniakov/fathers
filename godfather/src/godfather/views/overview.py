"""The overview, Slotfather's dashboard screen moved unchanged: SLOTS, QUEUE, DETAILS, EVENTS and MACHINE where
layout.overview() places them, the footer, and the overlay open over them. It draws the model the Slotfather adapter
decodes from `slotfather status --json --full` (docs/contracts/slotfather.md) on a tui.Canvas; the live view and
--once write that canvas as the same escape sequences, so they are the same picture. render() draws an app state
(state.py); dashboard_draw() is the same picture from its parts."""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, Optional, cast

from .. import layout as _layout
from .. import news as _news
from .. import version as _version
from ..tui import Canvas, clock
from ..widgets import Footer, Panel, Part
from . import common as _common
from . import details as _details
from . import events as _events
from . import machine as _machine
from . import overlays as _overlays
from . import queue as _queue
from .common import Model

if TYPE_CHECKING:
    from ..state import AppState

# The footer's hints in their order: navigation, panels, configuration, help, quit last; a narrow footer drops them
# (widgets.FOOTER_STEPS), help and quit last to go. On a scrollable panel the arrows scroll.
FOOTER_HINTS: tuple[Part, ...] = (("↑↓", "select"), ("tab", "panel"), ("s", "settings"), ("?", "help"), ("q", "quit"))


def footer_hints(focus: str) -> list[Part]:
    return [("↑↓", "select" if focus == "requests" else "scroll"), *FOOTER_HINTS[1:]]


WHATS_NEW = "what's new?"


def draw_footer(canvas: Canvas, model: Optional[Model], error: Optional[str], message: Optional[str],
                unread: bool = False) -> None:
    """`Godfather  what's new?` (fathers#102: the name in the title style; what's new? in the text colour while any
    tool has unread entries, else muted like the hints, never the accent; ↻ after it
    while a newer install waits for a restart; a click opens what's new), the hints, then the refresh failure or a
    notice (fathers#24: a notice never says the refresh failed, nor makes the status stale), then the last update and
    the status."""
    installed = _common.stale_install(model)
    Footer(canvas).draw([(_version.NAME, "title"), ("  " + WHATS_NEW + (" ↻" if installed else ""),
                                                      "text" if unread else "dim")],
                        footer_hints(canvas.focus), ("● stale", "bad") if error else ("● live", "ok"),
                        clock(model["updated"]) if model else "",
                        (message, "bad") if message else (f"refresh failed: {error}", "bad") if error else ("", ""))


def dashboard_draw(canvas: Canvas, model: Optional[Model], selected: int = 0, overlay: Any = None,
                   error: Optional[str] = None, cwd: Optional[str] = None, scroll: Optional[dict[str, int]] = None,
                   notice: Optional[list[str]] = None, message: Optional[str] = None,
                   failure: Optional[str] = None, news: Any = None, read: Optional[dict[str, str]] = None) -> None:
    """The whole screen. `overlay` is the one open (state.Help, Changelog or SettingsOverlay); `error` the refresh's
    failure, `message` a passing notice; `notice` stands in for the panels when Slotfather is absent or unsupported;
    `failure` is why the selected request's details did not load. A stale model (`stale_since`) is drawn dimmed, with
    the time it went stale in SLOTS' border. `news` and `read` are what's new and its read versions."""
    canvas.scroll = scroll if scroll is not None else {}
    draw_footer(canvas, model, error, message, bool(_news.unread(news, read or {})))
    if notice:
        draw_notice(canvas, notice)
        model = None
    elif model is None:
        canvas.put(2, 3, f"Queue unavailable: {error}" if error else "Reading the queue…",
                   "bad" if error else "dim", canvas.width - 6)
    if model is not None:
        draw_panels(canvas, model, selected, cwd, failure)
    if overlay is not None:
        draw_overlay(canvas, overlay, model, news, read)


def draw_panels(canvas: Canvas, model: Model, selected: int, cwd: Optional[str], failure: Optional[str]) -> None:
    moment = datetime.datetime.now(datetime.timezone.utc)
    screen = _layout.overview(canvas.width, canvas.height, _queue.queue_rows(model), _layout.machine_height)
    panels = screen.panels
    canvas.order = screen.order
    if "slots" in panels:
        Panel(canvas, panels["slots"]).frame("SLOTS", _queue.slots_note(model))
    _queue.draw_slots(canvas, screen.slots, model)
    if screen.note_row is not None:
        canvas.put(screen.note_row, screen.slots.x, "Enlarge the terminal for leases and the queue", "dim",
                   screen.slots.width)
    for name, rect in panels.items():
        if name == "requests":
            _queue.draw_requests(canvas, rect, model, selected, moment)
        elif name == "events":
            _events.draw_events(canvas, rect, model)
        elif name == "details":
            _details.draw_detail(canvas, rect, model, selected, cwd, moment, failure)
        elif name == "machine":
            _machine.draw_machine(canvas, rect, model)
    if model.get("stale_since"):  # the last good picture, dimmed, until a snapshot arrives again
        dim_panels(canvas)


def draw_overlay(canvas: Canvas, overlay: Any, model: Optional[Model], news: Any = None,
                 read: Optional[dict[str, str]] = None) -> None:
    kind = str(overlay.kind)
    if kind == "settings":
        overlay.panel.draw(canvas, model)
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
    """An app state drawn: the focus, the details' spinner while they load, the thumbs that flash, then the screen."""
    from .. import state as _state

    found = _state.items(state)
    index, cwd, failure = state.selection.index, None, None
    canvas.focus, canvas.fresh_events, canvas.glow = state.focus, state.fresh_events, _state.glowing(state, now)
    if found:
        item = found[index]
        known = state.details.get(_state.details_key(item, cast(_state.EntryId, state.selection.key)))
        if known is None:
            canvas.loading = int(now * 10)  # a spinner at ten frames a second
        else:
            if known.lane is not None:
                item["lane"] = known.lane
            cwd, failure = known.cwd, known.error
    dashboard_draw(canvas, state.model, index, state.overlay, state.refresh.error, cwd, state.scroll, notice,
                   state.notice.text if state.notice else None, failure, state.news, state.read)
