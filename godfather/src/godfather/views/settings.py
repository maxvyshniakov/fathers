"""The Settings overlay drawn from its value (settings_model.Panel): each section's title, its read-only STATUS
rows and its settings, a rule between sections; under the list a rule, the selected row's description and what
the last change did; the key line. Every rule is one length, the list two cells in from the frame. Geometry comes
from settings_model.geometry(), which the reducer's keys and clicks use too; nothing here reads the clock."""
from __future__ import annotations

import textwrap
from typing import Any, Optional

from .. import settings_model as _settings
from ..tui import Canvas
from ..widgets import Panel


def draw(canvas: Canvas, panel: _settings.Panel, context: _settings.Context) -> None:
    context = context._replace(width=canvas.width, height=canvas.height)
    view = _settings.geometry(panel, context)
    rect, inside = view.rect, view.inside
    frame = Panel(canvas, rect, "settings", left=3, right=3, footer=True)
    frame.overlay("SETTINGS", "a change saves after a second, or on Enter" if inside.notes.width >= 60 else "")
    if inside.small:
        frame.note("Enlarge terminal · Esc closes")
        return
    rows, rule = inside.rows, "─" * inside.notes.width
    frame.scrollbar(view.offset, view.visible, len(view.lines))
    for y, line in enumerate(view.lines[view.offset:view.offset + view.visible], rows.y):
        if line.kind == "rule":
            canvas.put(y, rows.x, rule, "frame", rows.width)
        for column, text, style in line.parts:
            if column < rows.width and text:
                canvas.put(y, rows.x + column, text, style, rows.width - column)
        if line.focus is not None and line.focus == panel.focus:  # the selected row on both frames (#15)
            frame.mark(y, "text")
    canvas.put(inside.notes.y, inside.notes.x, rule, "frame", inside.notes.width)
    for y, (text, style) in enumerate(notes(panel, context, inside.notes.width), inside.notes.y + 1):
        canvas.put(y, inside.notes.x, text, style, inside.notes.width)
    frame.footer(_settings.footer(panel))


def notes(panel: _settings.Panel, context: _settings.Context, width: int) -> list[tuple[str, str]]:
    """The description in at most two lines, then the flash in what is left of three."""
    found: list[tuple[str, str]] = []
    for text, style in _settings.notes(panel, context):
        wrapped = textwrap.wrap(text, max(1, width))[:2]
        if found:  # the flash keeps the description's first line
            found = found[:1]
        found += [(line, style) for line in wrapped]
    return found[:3]


def context_of(state: Any, width: int, height: int, model: Optional[dict[str, Any]] = None) -> _settings.Context:
    return _settings.Context(state.interval, state.theme, model if model is not None else state.model,
                             state.refresh.error, width, height)
