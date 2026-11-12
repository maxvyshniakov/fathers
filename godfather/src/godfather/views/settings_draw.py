"""The Settings overlay drawn: a read-only STATUS of the queue above the editable fields, each field's description
and what a change did under them, and its key line; in a shared Panel, where layout.settings() places the parts."""
from __future__ import annotations

import os
import textwrap
from typing import TYPE_CHECKING, Any, NamedTuple, Optional, cast

from .. import layout as _layout
from ..tui import Canvas
from ..widgets import Panel, Part

if TYPE_CHECKING:
    from .settings import Settings

STATUS_LABEL = 13  # columns of a status label
FIELD_LABEL = 28  # columns of a setting's label, its gutter included
FLASH_SECONDS = 4.0  # how long a confirmation shows, and how long a first D stays armed
FLASH_MARKS = {"ok": "✓ ", "warn": "! ", "bad": "✗ ", "accent": "… "}


class Row(NamedTuple):
    """One body line: `key` is the field it selects ("" for none); parts are (column, text, style)."""
    key: str
    parts: tuple[tuple[int, str, str], ...]


def number(value: float) -> str:
    return f"{value:g}"


def location(settings: Settings) -> str:
    """The queue's directory, tilde-shortened: Slotfather's STATUS `queue` row."""
    found = next((item.get("value") for item in status_items(settings) if item.get("label") == "queue"), None)
    directory, home = str(found or settings.basis.get("directory") or "?"), os.path.expanduser("~")
    return "~" + directory[len(home):] if directory.startswith(home + os.sep) else directory


def status_items(settings: Settings) -> list[dict[str, Any]]:
    items = cast(list[Any], settings.reply.get("status") or [])
    return [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]


def status_rows(settings: Settings, model: Optional[dict[str, Any]]) -> list[Row]:
    def row(label: str, text: str, style: str = "text") -> Row:
        return Row("", ((2, label, "dim"), (2 + STATUS_LABEL, text, style)))

    rows = [Row("", ((0, "STATUS", "title"), (7, "· read-only, live", "dim")))]
    if settings.local_only:
        rows.append(row("slotfather", settings.problem() or "no settings contract; update slotfather", "dim"))
        return rows
    rows.append(row("queue", settings.location()))
    if model:
        rows.append(row("slots", f"{model['slots']} total · {model.get('free', 0)} free · "
                                 f"{model.get('used', 0)} held · {len(model.get('waiters', []))} waiting"))
    else:
        rows.append(row("slots", "queue unavailable", "dim"))
    # The capacity the queue's last look read under its lock; this panel's own reading only without a model.
    basis = cast(dict[str, Any], (model or {}).get("basis") or settings.basis)
    per = number(basis["gib_per_slot"] or 0)
    how = {"fixed": "saved, fixed count", "memory": f"saved, one per {per} GiB of memory",
           "lower": f"saved, lower of CPU and one per {per} GiB",
           "automatic": "automatic, lower of the two budgets"}[basis["mode"]]
    rows.append(row("capacity", f"{basis['slots']} slots · {how}"))
    budget_slots = basis["memory_slots"] if basis["memory_slots"] is not None else settings.memory_budget
    memory = basis["memory_gib"] if basis["memory_gib"] is not None else settings.memory_gib
    budget = (f"CPU {basis['cpu_slots']} ({basis['cores']} cores) · memory "
              + (f"{budget_slots}" if budget_slots is not None else "unknown")
              + (f" ({number(memory)} GiB)" if memory is not None else ""))
    rows.append(row("budgets", budget))
    drain = max(0, settings.held - int(model["slots"])) if model else 0
    rows.append(row("draining", f"{drain} slots · nothing starts until at most {model['slots']} are held"
                    if drain and model else "none", "warn" if drain else "dim"))
    environment = next((item for item in status_items(settings) if item.get("label") == "environment"), None)
    if environment and environment.get("value"):
        rows.append(row("environment", str(environment["value"]), "dim"))
    problem = settings.problem()
    if problem:  # the Slotfather snapshot is stale or unsupported: why, from the source
        rows.append(row("slotfather", problem, "bad"))
    return rows


def field_rows(settings: Settings, key: str, value_column: int) -> list[Row]:
    """The field's row; while it is typed or pending, a second row says what the value would do, or why not."""
    selected = key == settings.focus
    label, room = settings.label(key), value_column - 3
    label = label if len(label) <= room else label[:max(0, room - 1)] + "…"
    parts: list[tuple[int, str, str]] = [(0, "▶" if selected else " ", "cursor"), (2, label, "text")]
    if selected and settings.editing:
        parts.append((value_column, "[" + settings.buffer + "▏]", "cursor"))
        note, style = settings.preview()
        return [Row(key, tuple(parts)), Row(key, ((4, note, style),))]
    pending = settings.pending is not None and settings.pending.key == key
    value = settings.text(key, settings.shown_value(key))
    if selected and key in ("mode", "order", "theme"):
        value = f"‹ {value} ›"
    parts.append((value_column, value, "cursor" if pending else "accent"))
    after = value_column + len(value) + 2
    if pending:
        parts.append((after, "pending", "dim"))
        if key in ("mode", "amount"):
            note, style = settings.effect(key, settings.shown_value(key))
            return [Row(key, tuple(parts)), Row(key, ((4, note, style),))]
        return [Row(key, tuple(parts))]
    if key == "amount" and settings.mode() == "fixed":
        note = settings.drain_note(settings.basis["slots"])
        if note:
            parts.append((after, note.replace("will drain", "draining"), "warn"))
    elif key in settings.effective and key not in ("interval", "theme") and \
            settings.effective[key] != settings.saved[key]:
        shown = settings.text(key, settings.effective[key])
        parts.append((after, f"effective here: {shown}", "dim"))  # this dashboard's environment tightens it
    return [Row(key, tuple(parts))]


def body(settings: Settings, model: Optional[dict[str, Any]], inner: int) -> list[Row]:
    rows = status_rows(settings, model)
    rows.append(Row("", ((0, "─" * inner, "frame"),)))
    value_column = min(FIELD_LABEL, max(12, inner // 2))
    for key in settings.fields():  # one flat list (owner, 2026-11-11)
        rows.extend(field_rows(settings, key, value_column))
    return rows


def footer(settings: Settings) -> list[Part]:
    """The panel's short key line, centred like the dashboard's footer (Panel.footer())."""
    if settings.editing:
        return [("enter", "save"), ("esc", "cancel")]
    if settings.pending:
        return [("enter", "save now"), ("esc", "cancel")]
    return [("↑↓", "select"), ("←→", "change"), ("enter", "edit"), ("d", "reset"), ("esc", "close")]


def notes(settings: Settings, width: int) -> list[Part]:
    """Under the rule: the focused field's description, and what the last change did while it is news."""
    found = [(text, "dim") for text in textwrap.wrap(settings.description(settings.focus), max(1, width))[:2]]
    flash = settings.flash
    if flash and (settings.clock() - flash[2] < FLASH_SECONDS or flash[1] == "bad"
                  or (flash[1] == "accent" and settings.pending)):
        text, style, _ = flash
        found = found[:1] + [(line, style) for line in
                             textwrap.wrap(FLASH_MARKS.get(style, "") + text, max(1, width))[:2]]
    return found[:3]


def draw(settings: Settings, canvas: Canvas, model: Optional[dict[str, Any]]) -> None:
    settings.refresh()
    if model:
        settings.held = int(model.get("used", 0))
        settings.waiting = [int(waiter.get("count", 1)) for waiter in model.get("waiters", [])]
    settings.settle_focus()
    width = _layout.centred(canvas.width, canvas.height, _layout.SETTINGS_WIDTH, 0, 0).width
    lines = body(settings, model, max(0, width - 6))
    rect = _layout.centred(canvas.width, canvas.height, _layout.SETTINGS_WIDTH, len(lines), _layout.SETTINGS_CHROME)
    inside = _layout.settings(rect)
    settings.bounds = rect.x, rect.y, rect.width, rect.height
    settings.hits = []
    panel = Panel(canvas, rect, "settings", left=2, right=4, footer=True)
    panel.overlay("SETTINGS", "saved at once · applies at the next grant" if inside.notes.width >= 60 else "")
    settings.small = inside.small
    if inside.small:
        canvas.put(rect.y + 1, rect.x + 2, "Enlarge terminal · Esc closes", "dim", inside.notes.width)
        return
    settings.visible, settings.total = inside.rows.height, len(lines)
    if settings.reveal:
        found = [n for n, row in enumerate(lines) if row.key == settings.focus]
        if found:
            settings.offset = min(settings.offset, found[0])
            settings.offset = max(settings.offset, found[-1] - settings.visible + 1)
        settings.reveal = False
    settings.offset = max(0, min(settings.offset, max(0, len(lines) - settings.visible)))
    panel.scrollbar(settings.offset, settings.visible, settings.total)
    for line, row in enumerate(lines[settings.offset:settings.offset + settings.visible], inside.rows.y):
        for column, text, style in row.parts:
            if column < inside.rows.width:
                canvas.put(line, inside.rows.x + column, text, style, inside.rows.width - column)
        if row.key:
            settings.hits.append((row.key, rect.x + 1, line, rect.width - 2, 1))
            if row.key == settings.focus:  # the selected row on both frames, as in QUEUE (#15)
                panel.mark(line, "text")
    canvas.put(inside.notes.y, inside.notes.x, "─" * inside.notes.width, "frame", inside.notes.width)
    for line, (text, style) in enumerate(notes(settings, inside.notes.width), inside.notes.y + 1):
        canvas.put(line, inside.notes.x, text, style, inside.notes.width)
    panel.footer(footer(settings))
