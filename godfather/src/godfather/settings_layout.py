"""Settings laid out as lines and placed on the screen (fathers#87, fathers#233): the one geometry, scroll clamp,
notes and footer hints shared by the reducer and `views/settings.py`, with no canvas or terminal imports."""
from __future__ import annotations

import dataclasses
from typing import Any, NamedTuple, Optional, Sequence, cast

from . import layout as _layout
from . import settings_model as _model
from . import settings_schema as _schema
from .text import dashboard_text, tilde

STATUS_LABEL = 13  # columns of a status label
FIELD_LABEL = 28  # columns of a setting's label, its cursor included
_FLASH_MARKS = {"ok": "✓ ", "warn": "! ", "bad": "✗ ", "accent": "… "}


class Line(NamedTuple):
    """One line of the list: kind is title, problem, status, row, note or rule; `focus` the row it selects (rows
    and their notes), parts are (column, text, style)."""
    kind: str
    focus: Optional[_schema.Focus]
    parts: tuple[tuple[int, str, str], ...]


class Geometry(NamedTuple):
    rect: _layout.Rect
    inside: _layout.SettingsBox
    lines: list[Line]
    offset: int  # the first line shown

    @property
    def visible(self) -> int:
        return self.inside.rows.height

    def hits(self) -> list[tuple[_schema.Focus, int]]:
        """(row it selects, screen row) for each selectable line shown."""
        shown_lines = self.lines[self.offset:self.offset + self.visible]
        return [(line.focus, self.inside.rows.y + n) for n, line in enumerate(shown_lines)
                if line.focus is not None]


# Notes under a pending or typed row

def answer_note(reply: Optional[dict[str, Any]], typed: bool) -> tuple[str, str]:
    """Under a pending or typed row: why the tool refuses the value, or what it would do; "" for nothing to say."""
    if reply is None:
        return "checking…", "dim"
    if not reply.get("ok"):
        return "✗ " + str(reply.get("error") or "Not accepted."), "bad"
    if reply.get("preview"):  # what the tool says the value would do: the tools own their rules
        return str(reply["preview"]), "dim"
    return ("Enter saves", "dim") if typed else ("", "dim")


def typed_note(panel: _model.Panel, row: _schema.Row) -> tuple[str, str]:
    if not panel.buffer:
        return "", "dim"
    if row.tool == _schema.GODFATHER:
        _, problem = _schema.parse_typed(row, panel.buffer)
        return ("✗ " + problem, "bad") if problem else ("Enter saves", "dim")
    return answer_note(_model.check_reply(panel, row, panel.buffer), True)


def pending_note(panel: _model.Panel, row: _schema.Row) -> tuple[str, str]:
    change = panel.pending
    if change is None or (change.tool, change.key) != row.focus or row.tool == _schema.GODFATHER:
        return "", "dim"
    return answer_note(_model.check_reply(panel, row, _schema.argument(change.value)), False)


# The lines of the list

def status_lines(context: _model.Context, found: _schema.Section) -> list[Line]:
    def line(label: str, text: str, style: str = "text") -> Line:
        return Line("status", None, ((2, label, "dim"), (2 + STATUS_LABEL, text, style)))

    reply = found.reply or {}
    items = [cast(dict[str, Any], item) for item in cast(list[Any], reply.get("status") or [])
             if isinstance(item, dict)]
    if found.tool != "slotfather":
        return [line(str(item.get("label") or ""), tilde(str(item.get("value") or "")),
                     str(item.get("style") or "text")) for item in items]
    # Slotfather's, as the panel showed it (#7): the capacity its queue read under its status snapshot, from the model
    model = context.model
    where = next((item.get("value") for item in items if item.get("label") == "queue"), None)
    mine = cast(dict[str, Any], reply.get("basis") or {})
    lines = [line("queue", tilde(str(where or mine.get("directory") or "?")))]
    if model:
        lines.append(line("slots", f"{model['slots']} total · {model.get('free', 0)} free · "
                                   f"{model.get('used', 0)} held · {len(model.get('waiters', []))} waiting"))
    else:
        lines.append(line("slots", "queue unavailable", "dim"))
    basis = cast(dict[str, Any], (model or {}).get("basis") or mine)
    if basis.get("mode") in ("fixed", "memory", "lower", "automatic") and "slots" in basis:
        per = _schema.number(basis.get("gib_per_slot") or 0)
        how = {"fixed": "saved, fixed count", "memory": f"saved, one per {per} GiB of memory",
               "lower": f"saved, lower of CPU and one per {per} GiB",
               "automatic": "automatic, lower of the two budgets"}[str(basis["mode"])]
        lines.append(line("capacity", f"{basis['slots']} slots · {how}"))
        slots = basis.get("memory_slots") if basis.get("memory_slots") is not None else mine.get("memory_slots")
        memory = basis.get("memory_gib") if basis.get("memory_gib") is not None else mine.get("memory_gib")
        lines.append(line("budgets", f"CPU {basis.get('cpu_slots')} ({basis.get('cores')} cores) · memory "
                                     + (f"{slots}" if slots is not None else "unknown")
                                     + (f" ({_schema.number(memory)} GiB)" if memory is not None else "")))
    drain = max(0, int(model.get("used", 0)) - int(model["slots"])) if model else 0
    lines.append(line("draining", f"{drain} slots · nothing starts until at most {model['slots']} are held"
                      if drain and model else "none", "warn" if drain else "dim"))
    environment = next((item for item in items if item.get("label") == "environment"), None)
    if environment and environment.get("value"):
        lines.append(line("environment", str(environment["value"]), "dim"))
    compatibility = next((item.get("value") for item in items if item.get("label") == "compatibility"), None)
    if isinstance(compatibility, str) and compatibility:
        lines.append(line("clients", dashboard_text(compatibility), "warn"))
    if context.problem:  # the Slotfather snapshot is stale: why, from the source
        lines.append(line("slotfather", context.problem, "bad"))
    return lines


def row_lines(panel: _model.Panel, context: _model.Context, row: _schema.Row, value_column: int) -> list[Line]:
    """The row; while it is typed or pending, or refused, a second line says what the value would do, or why not."""
    selected = row.focus == panel.focus and row.editable
    label, room = row.label, value_column - 3
    label = label if len(label) <= room else label[:max(0, room - 1)] + "…"
    parts: list[tuple[int, str, str]] = [(0, "▶" if selected else " ", "cursor"), (2, label, "text")]
    row_notes: list[tuple[str, str]] = []
    if selected and panel.editing:
        parts.append((value_column, "[" + panel.buffer + "▏]", "cursor"))
        row_notes.append(typed_note(panel, row))
    else:
        value = _model.shown_value(panel, context, row)
        pending = panel.pending is not None and (panel.pending.tool, panel.pending.key) == row.focus
        saving = any((op.tool, op.key) == row.focus for op in (*panel.saving, *panel.queued)) and not pending
        text = _schema.value_text(row, value)
        if selected and row.type in ("choice", "theme", "boolean"):
            text = f"‹ {text} ›"
        parts.append((value_column, text, "cursor" if pending or saving else "accent" if row.editable else "dim"))
        after = value_column + len(text) + 2
        if pending or saving:
            parts.append((after, "pending" if pending else "saving…", "dim"))
            row_notes.append(pending_note(panel, row))
        elif not row.editable:
            why = row.source if row.source and row.source not in ("saved", "default") else "read-only"
            if row.writable and row.type not in _schema.EDITABLE:
                why = "read-only here: needs a newer godfather"
            parts.append((after, why, "dim"))
        elif "effective" in row.schema and row.schema["effective"] != row.value:
            parts.append((after, f"effective here: {_schema.value_text(row, row.schema['effective'])}", "dim"))
    warning = row.schema.get("warning")
    if selected and isinstance(warning, str) and warning:
        row_notes.append((dashboard_text(warning), "warn"))
    refusal = panel.refusal
    if refusal is not None and (refusal.tool, refusal.key) == row.focus or refusal is not None and selected \
            and refusal.tool == row.tool and _model.row_of(panel, context, (refusal.tool, refusal.key)) is None:
        row_notes.append(("✗ " + refusal.text, "bad"))  # under its row, or the one that took its place
    found = [Line("row", row.focus if row.editable else None, tuple(parts))]
    found += [Line("note", row.focus if row.editable else None, ((4, text, style),))
              for text, style in row_notes if text]
    return found


def section_title(panel: _model.Panel, tool: str) -> Line:
    title = _schema.TITLES.get(tool, tool.upper())
    found = _model.section(panel, tool)
    if tool == _schema.GODFATHER:
        note = "· this dashboard only"
    elif found is not None and found.reply is not None and found.reply.get("writable") is False:
        note = "· read-only here"
    elif found is not None and found.reply is not None and found.reply.get("status"):
        note = "· status read-only, live"
    else:
        note = ""
    return Line("title", None, ((0, title, "title"), (len(title) + 1, note, "dim")))


def lines(panel: _model.Panel, context: _model.Context, inner: int = 0) -> list[Line]:
    """Every line of the list, top to bottom: each section's title, its STATUS rows, then its settings; a rule
    between sections."""
    value_column = min(FIELD_LABEL, max(12, inner // 2)) if inner else FIELD_LABEL
    found: list[Line] = []
    for tool in _model.tools(panel):
        if found:
            found.append(Line("rule", None, ()))
        found.append(section_title(panel, tool))
        current = _model.section(panel, tool)
        if current is not None:
            if current.reply is not None:
                reason = current.reply.get("reason")
                if current.reply.get("writable") is False and reason:
                    found.append(Line("problem", None, ((2, str(reason), "dim"),)))
                found.extend(status_lines(context, current))
            if current.problem:
                found.append(Line("problem", None, ((2, current.problem, "dim" if current.reply is None
                                                     else "bad"),)))
            elif current.reply is None:
                found.append(Line("problem", None, ((2, "reading…", "dim"),)))
        for row in _model.rows(panel, context, tool):
            found.extend(row_lines(panel, context, row, value_column))
    if panel.loading:
        found += [Line("rule", None, ()), Line("problem", None, ((0, "Reading the tools' settings…", "dim"),))]
    return found


# Viewport geometry, scrolling and footer

def geometry(panel: _model.Panel, context: _model.Context) -> Geometry:
    inner = _layout.overlay_text(context.width, _layout.SETTINGS_WIDTH, _layout.SETTINGS_INSET)
    found = lines(panel, context, inner)
    rect = _layout.centred(context.width, context.height, _layout.SETTINGS_WIDTH, len(found), _layout.SETTINGS_CHROME)
    inside = _layout.settings(rect)
    return Geometry(rect, inside, found, scrolled(panel, found, inside.rows.height))


def scrolled(panel: _model.Panel, found: Sequence[Line], visible: int) -> int:
    """The first line shown: the panel's offset, moved to show the focused row when `reveal`, kept in range."""
    offset = panel.offset
    if panel.reveal:
        at = [n for n, line in enumerate(found) if line.focus == panel.focus]
        if at:
            offset = min(offset, at[0])
            offset = max(offset, at[-1] - visible + 1)
    return _layout.clamp_scroll(offset, len(found), visible)


def place(panel: _model.Panel, context: _model.Context) -> _model.Panel:
    """The offset the screen shows, kept: a reveal is done once."""
    if not context.width or not context.height:
        return panel
    offset = geometry(panel, context).offset
    if offset == panel.offset and not panel.reveal:
        return panel
    return dataclasses.replace(panel, offset=offset, reveal=False)


def small(panel: _model.Panel, context: _model.Context) -> bool:
    """Too small to show anything but a note to enlarge the terminal: only the ways out work."""
    return context.width > 0 and geometry(panel, context).inside.small


def scroll(panel: _model.Panel, context: _model.Context, rows_by: int) -> _model.Panel:
    view = geometry(panel, context)
    return dataclasses.replace(
        panel,
        offset=_layout.clamp_scroll(view.offset + rows_by, len(view.lines), view.visible),
        reveal=False,
    )


def page(panel: _model.Panel, context: _model.Context, direction: int) -> _model.Panel:
    return scroll(panel, context, direction * max(1, geometry(panel, context).visible - 1))


def notes(panel: _model.Panel, context: _model.Context) -> list[tuple[str, str]]:
    """Under the rule: the focused row's description, and what the last change did while it is news (the flash)."""
    row = _model.focused(panel, context)
    found = [(row.description, "dim")] if row is not None and row.description else []
    flash = panel.flash
    if flash is not None:
        found = found[:1] + [(_FLASH_MARKS.get(flash.style, "") + flash.text, flash.style)]
    return found


def footer(panel: _model.Panel) -> list[tuple[str, str]]:
    """The panel's short key line, centred like the dashboard's footer."""
    if panel.editing:
        return [("enter", "save"), ("esc", "cancel")]
    if panel.pending:
        return [("enter", "save now"), ("esc", "cancel")]
    return [("↑↓", "select"), ("←→", "change"), ("enter", "edit"), ("d", "reset"), ("esc", "close")]
