"""EVENTS: one tool's log at a time (fathers#152), newest first, read from the tools' event logs (fathers#84).

The panel's title is the switch: `SLOTS LOG`, `LANES LOG`, `PROCS LOG` for each present tool, then `ALL LOG` where
there are several; ← → while EVENTS has focus, `f` anywhere, or a click on the title, cycle it; SLOTS LOG is the
default where Slotfather is present. `◂ SLOTS LOG ▸` says it cycles where there is room, the bare title where not.

Each log has its own columns:

    SLOTS LOG  time · slots · label · waited · outcome · pid      one row per run (a grant and what ended it)
    LANES LOG  time · event · lane · result · ticket
    PROCS LOG  time · event · label · detail · pid
    ALL LOG    time · type · description                            `slot run`, `lane checked`, `proc reaped`

A Slotfather run's grant and its release, reap or yield are one row, `running 3m` while held and how it ended once
done; `r` shows every event as logged instead. A run's trend and outlier are marks on its row (▲, ↗, ↘), never rows
of their own. `/` edits a text filter in the bottom border; it filters within the log shown."""
from __future__ import annotations

import datetime
from typing import Any, Optional, cast

from ..layout import Rect
from ..tui import Canvas
from ..text import clock, span, text_width
from ..widgets import FLEXIBLE_MIN, FOCUS_FRAME, REST_FRAME, Panel, Part, Spec, Table
from ..events import ALL, Filter, shown_rows, view_of, views
from . import common as _common
from .common import Model, cell_run, cells_width

DETAIL_MIN, DETAIL_MAX = 6, 28  # a detail column's least and greatest width; it grows to its longest value
EVENT_MAX = 10  # the event column's greatest width
TIME_WIDTH = 8  # `11:59:35`
OUTCOME_MAX, WAIT_MAX = 18, 10
TYPE_MAX = 15  # ALL LOG's type: `lane post-merge`
SOURCE_STYLES = {"slot": "source-slot", "lane": "source-lane", "junk": "source-junk"}  # theme tokens by tag
TOO_OLD = "slotfather too old for events"  # its capabilities name no event log
TOO_NEW = "slotfather's events need a newer godfather"  # they name one of no schema version this one reads
TITLES = {"slotfather": "SLOTS LOG", "lanefather": "LANES LOG", "junkfather": "PROCS LOG", ALL: "ALL LOG"}
TYPES = {"slotfather": "slot", "lanefather": "lane", "junkfather": "proc"}  # ALL LOG's type: the tool in a word
ARROWS = ("◂ ", " ▸")
SWITCH_ROOM = 40  # the least panel width the arrows are drawn at


def source_style(tag: str) -> str:
    return SOURCE_STYLES.get(tag, "dim")


def filter_note(model: Model, found: Filter) -> list[Part]:
    """The top border's note after the title: why there are no events, `raw`, the text filter (`"710"`)."""
    parts: list[Part] = []
    if model.get("events_note"):
        parts.append((str(model["events_note"]), "warn"))
    words = (["every event"] if found.raw else []) + ([f'"{found.text}"'] if found.text else [])
    if words:
        parts += [(" · ", "dim")] if parts else []
        parts.append((" · ".join(words), "text"))
    return parts


def skipped_note(model: Model) -> Optional[Part]:
    """Lines the reader skipped since Godfather started, and why (`3 lines skipped: other host`), so a log whose
    lines all go unread says so (fathers#84 review)."""
    from ..events import SKIP_WORDS

    counts: dict[str, int] = {}
    for found in cast(dict[str, dict[str, int]], model.get("events_skipped") or {}).values():
        for reason, count in found.items():
            counts[reason] = counts.get(reason, 0) + int(count)
    total = sum(counts.values())
    if not total:
        return None
    why = ", ".join(SKIP_WORDS.get(reason, reason) for reason, count in counts.items() if count)
    return f" {total} line{'s' if total != 1 else ''} skipped: {why} ", "warn"


def event_result(row: dict[str, Any]) -> list[Part]:
    """A row's detail: the formatter's parts, read from the event's fields (no prose is parsed)."""
    return [(str(text), str(style)) for text, style in row.get("detail") or [("", "dim")]]


def mark_of(row: dict[str, Any], marks: _common.Marks) -> list[Part]:
    """A run's mark (fathers#108, fathers#152): ▲ an outlier, ↗ its run type turned slower, ↘ faster."""
    found = row.get("run")
    mark = marks.run.get(str(found)) if found else None
    return [(" " + mark, "warn" if found != "faster" else "dim")] if mark else []


def outcome(row: dict[str, Any], marks: _common.Marks, moment: Optional[datetime.datetime]) -> list[Part]:
    """How a run went (`ok 1m13s ▲`, `exit 1 · 7s`, `gave up 5m`), `running 3m` while it holds its slots."""
    if row.get("running"):
        start = cast(Optional[datetime.datetime], row.get("start"))
        held = span((moment - start).total_seconds()) if moment and start else ""
        return [(f"running {held}".strip(), "text")]
    return event_result(row) + mark_of(row, marks)


def trend_words(row: dict[str, Any]) -> tuple[str, list[Part]]:
    """A trend as logged (`r`): its run type in words, then how its recent median compares and why."""
    trend: dict[str, Any] = row.get("trend") or row.get("details") or {}
    words = " ".join(part for part in str(row.get("label") or "?").split(" · ") if part)
    verdict = str(trend.get("verdict") or "changed")
    ratio = trend.get("ratio")
    change = f" {round((float(ratio) - 1) * 100):+d}%" if isinstance(ratio, (int, float)) and ratio > 0 else ""
    why = str(trend.get("why") or "").replace(": ", " ")
    return words[:1].upper() + words[1:], [(f"{verdict}{change}" + (f" · {why}" if why else ""),
                                            "warn" if verdict == "slower" else "dim")]


def event_cells(row: dict[str, Any], size: int, style: str = "text") -> list[Part]:
    """A row's slots, in the text colour (owner, 2026-11-12): filled while taken, hollow once handed back or never
    held (a waiter that left or was reaped)."""
    return cell_run(int(row.get("count") or 0), size, style, hollow=not row.get("filled"))


def slot_values(row: dict[str, Any], marks: _common.Marks, moment: Optional[datetime.datetime],
                size: int) -> dict[str, Any]:
    """SLOTS LOG's cells: a run's wait and outcome; a grant or an end as logged in their columns; another kind's
    word where the wait goes, its detail where the outcome goes."""
    from ..events import ENDS, WAITER_ENDS

    kind = row.get("kind")
    label: Any = str(row.get("label") or "?")
    waited: list[Part] = []
    result: list[Part] = []
    if kind == "run":
        waited, result = list(row.get("wait") or []), outcome(row, marks, moment)
    elif kind == "lease.granted":
        waited = event_result(row)
    elif kind in (*ENDS, *WAITER_ENDS):
        result = event_result(row) + mark_of(row, marks)
    elif kind == "run.trend":
        label, result = trend_words(row)
    else:
        waited, result = [(str(row.get("event") or ""), "dim")], event_result(row)
    return {"slots": event_cells(row, size) if size else "", "label": label, "waited": waited, "outcome": result,
            "pid": (str(row.get("pid") or ""), "dim")}


def description(row: dict[str, Any], marks: _common.Marks, moment: Optional[datetime.datetime]) -> list[Part]:
    """ALL LOG's one sentence from the event's fields: `sid-kit build · ok 1s · 1 slot`, `review-fixes · red · 2
    suites`, `vite dev · ttl 4h00m`."""
    if row.get("kind") == "run.trend":
        name, why = trend_words(row)
        return [(name, "text"), (" · ", "dim"), *why]
    parts: list[Part] = [(str(row.get("label") or "?"), "text")]
    said = outcome(row, marks, moment) if row.get("kind") == "run" else event_result(row) + mark_of(row, marks)
    if any(text for text, _ in said):
        parts += [(" · ", "dim"), *said]
    count = int(row.get("count") or 0)
    if count and row.get("source") == "slotfather":
        parts += [(" · ", "dim"), (f"{count} slot{'s' if count != 1 else ''}", "dim")]
    return parts


def type_of(row: dict[str, Any]) -> Part:
    """ALL LOG's type: the tool and the event in a word each, in the tool's colour."""
    tool = TYPES.get(str(row.get("source")), str(row.get("tag") or "?"))
    event = "trend" if row.get("kind") == "run.trend" else str(row.get("event") or row.get("kind") or "")
    return f"{tool} {event}", source_style(str(row.get("tag") or ""))


def widest(values: list[list[Part]], least: int, most: int) -> int:
    return min(most, max([least] + [sum(text_width(text) for text, _ in parts) for parts in values]))


def columns(view: str, width: int, rows: list[dict[str, Any]], values: list[dict[str, Any]]) -> list[Spec]:
    """A log's columns for an inner width: (key, title, width, right-aligned, drop rank). The label (or lane, or
    description) is the flexible one; a narrow panel drops pid, then slots or ticket (Table.fit)."""
    def parts(key: str) -> list[list[Part]]:
        found: list[list[Part]] = []
        for value in values:
            item: Any = value.get(key, "")
            found.append(cast(list[Part], item) if isinstance(item, list) else [cast(Part, item)]
                         if isinstance(item, tuple) else [(str(item), "")])
        return found

    pids = [len(str(row["pid"])) for row in rows if row.get("pid")]
    pid: list[Spec] = [("pid", "pid", max([3] + pids), True, 3)] if pids else []
    time: Spec = ("time", "time", TIME_WIDTH, False, 0)
    if view == "slotfather":
        cells = cells_width([int(row.get("count") or 0) for row in rows])
        return [time, ("slots", "slots", cells, False, 2), ("label", "label", 0, False, 0),
                ("waited", "waited", widest(parts("waited"), 6, WAIT_MAX), True, 1),
                ("outcome", "outcome", widest(parts("outcome"), 7, OUTCOME_MAX), True, 0), *pid]
    if view == "lanefather":
        tickets = [len(f"#{row['ticket']}") for row in rows if row.get("ticket")]
        ticket: list[Spec] = [("ticket", "ticket", max([6] + tickets), True, 2)] if any(tickets) else []
        return [time, ("event", "event", widest(parts("event"), 5, EVENT_MAX), False, 0),
                ("label", "lane", 0, False, 0), ("detail", "result", widest(parts("detail"), DETAIL_MIN, DETAIL_MAX),
                                                   True, 0), *ticket]
    if view == "junkfather":
        return [time, ("event", "event", widest(parts("event"), 5, EVENT_MAX), False, 0),
                ("label", "label", 0, False, 0), ("detail", "detail", widest(parts("detail"), DETAIL_MIN, DETAIL_MAX),
                                                  True, 0), *pid]
    return [time, ("type", "type", widest(parts("type"), 4, TYPE_MAX), False, 0),
            ("description", "description", 0, False, 0)]


SHRINK = {"detail": 8, "outcome": 8, "event": 5, "type": 8}  # how far a column gives way before the label does


def room(spec: list[Spec], width: int) -> int:
    """The cells the flexible column is left with."""
    return width - sum(column[2] for column in spec) - 2 * (len(spec) - 1)


def narrowed(spec: list[Spec], width: int) -> list[Spec]:
    """A narrow panel's columns: pid goes, then slots or ticket, then the wait; then the detail (or outcome, the
    event, the type) shrinks to its least, so the label keeps FLEXIBLE_MIN cells as long as it can."""
    found = list(spec)
    while room(found, width) < FLEXIBLE_MIN and any(column[4] for column in found):
        found.remove(max((column for column in found if column[4]), key=lambda column: column[4]))
    for key, least in SHRINK.items():
        short = FLEXIBLE_MIN - room(found, width)
        if short > 0:
            found = [(name, title, max(least, size - short) if name == key and size > least else size, right, rank)
                     for name, title, size, right, rank in found]
    return found


def row_values(view: str, row: dict[str, Any], marks: _common.Marks, moment: Optional[datetime.datetime],
               size: int) -> dict[str, Any]:
    when = (clock(cast(Optional[datetime.datetime], row.get("when")), moment), "dim")
    if view == "slotfather":
        return {"time": when, **slot_values(row, marks, moment, size)}
    if view == ALL:
        return {"time": when, "type": type_of(row), "description": description(row, marks, moment)}
    ticket = row.get("ticket")
    return {"time": when, "event": (str(row.get("event") or ""), "text"), "label": str(row.get("label") or "?"),
            "detail": event_result(row), "ticket": (f"#{ticket}" if ticket else "", "dim"),
            "pid": (str(row.get("pid") or ""), "dim")}


def draw_events(canvas: Canvas, rect: Rect, model: Model, moment: Optional[datetime.datetime] = None,
                found: Optional[Filter] = None, selected: Optional[int] = None) -> None:
    """EVENTS: the log the title names, newest first, in its columns. Scrolled down, what arrived since is counted
    in its bottom border (Home goes back to it); the text field, while it is open, takes that place. `selected` is
    the row its cursor is on while DETAILS shows it (fathers#152), marked as QUEUE's selected row is."""
    found = found or Filter()
    view = view_of(model, found)
    rows = shown_rows(model, found)[::-1]
    panel = Panel(canvas, rect, "events")
    marks = _common.marks_of(canvas)
    sizing = [row_values(view, row, marks, moment, 1) for row in rows]
    spec = narrowed(columns(view, panel.inner.width, rows, sizing), panel.inner.width)
    table = clipped(Table.fit(panel.inner.width, spec), panel.inner.width)
    size = table.width("slots", 0)
    title = switch_title(model, view, rect.width)
    table.header(panel, "")
    after = draw_title(panel, table, title, len(views(model)) > 1)
    note = filter_note(model, found)
    if note:
        border_note(panel, table, note, after)
    skipped = skipped_note(model)
    offset, visible = panel.viewport(len(rows), selected)
    if found.editing:
        canvas.segments(rect.y + rect.height - 1, rect.x + 2, [(" / ", "key"), (found.text, "text"), ("_ ", "key")],
                        max(0, rect.width - 4))
    elif visible and offset and canvas.fresh_events:
        panel.bottom(f" ↑ {canvas.fresh_events} new ", "text" if panel.focused else "dim")
    if skipped is not None and not found.editing and text_width(skipped[0]) + 16 <= rect.width:
        canvas.right(rect.y + rect.height - 1, rect.x + rect.width - 2, *skipped)
    if not rows and not model.get("events_note"):
        panel.put(0, "No events match the filter" if found.active() else "No events logged yet", "dim")
    for line, position in panel.rows(offset, visible, len(rows)):
        table.row(canvas, line, panel.inner.x, row_values(view, rows[position], marks, moment, size))
        if position == selected:
            panel.mark(line, "text")


def switch_title(model: Model, view: str, width: int) -> list[Part]:
    """The title: `◂ SLOTS LOG ▸` where it cycles and there is room, else the bare title."""
    name = TITLES.get(view, "EVENTS")
    if len(views(model)) < 2:
        return [(name, "title")]
    if width < SWITCH_ROOM:
        return [(name, "key")]
    return [(ARROWS[0], "dim"), (name, "key"), (ARROWS[1], "dim")]


def draw_title(panel: Panel, table: Table, title: list[Part], switch: bool) -> int:
    """The title set into the top border where every panel's sits, highlighted where it switches the log, over the
    column labels it reaches; the cells it takes are a click target (Canvas.switch_at). The column after it."""
    canvas, (y, x, _, width) = panel.canvas, panel.rect
    size = min(sum(text_width(text) for text, _ in title), max(0, width - 6))
    start = x + 3
    labels = [x + 3 + column + (max(0, label_size - text_width(label)) if right else 0)
              for _, label, column, label_size, right in table.placed[1:] if label]
    end = start + size
    resume = next((label - 1 for label in labels if label - 1 > end), x + width - 1)
    rule = (FOCUS_FRAME if panel.focused else REST_FRAME)[1]
    canvas.put(y, start - 1, rule * (resume - start + 1), "accent" if panel.focused else "frame")
    canvas.put(y, start - 1, " ", "text")
    canvas.segments(y, start, title, size)
    canvas.put(y, start + size, " ", "text")
    if switch:
        canvas.switch_at = (y, start, start + size)
    return start + size - x - 3


def clipped(table: Table, width: int) -> Table:
    """The columns that start within `width`, the last cut to it: on a panel too narrow for every column's least,
    nothing is drawn past its frame."""
    return Table([(key, title, x, min(size, width - x), right) for key, title, x, size, right in table.placed
                  if x < width])


def border_note(panel: Panel, table: Table, note: list[Part], after: int = 0) -> None:
    """The note set into the top border after the title (`SLOTS LOG ── "710" ──`), over the column labels it
    reaches, which give way: the labels after it stay over their columns. A note longer than the border is cut with
    an ellipsis, never left out."""
    canvas, (y, x, _, width) = panel.canvas, panel.rect
    start = x + 3 + after + 1
    room = x + width - 2 - (start + 3) - 1  # the cells between its leading space and the corner, less a space
    if room < 2:
        return
    size = min(room, sum(text_width(text) for text, _ in note))
    end = start + 3 + size + 1
    labels = [x + 3 + column + (max(0, label_size - text_width(title)) if right else 0)
              for _, title, column, label_size, right in table.placed[1:] if title]
    resume = next((label - 1 for label in labels if label - 1 > end), x + width - 1)
    rule = (FOCUS_FRAME if panel.focused else REST_FRAME)[1]
    canvas.put(y, start, rule * (resume - start), "accent" if panel.focused else "frame")
    canvas.put(y, start + 2, " ", "text")
    if size < sum(text_width(text) for text, _ in note):  # cut: the whole note as one run, ending in …
        canvas.put(y, start + 3, "".join(text for text, _ in note), note[-1][1], size)
    else:
        canvas.segments(y, start + 3, note, size)
    canvas.put(y, start + 3 + size, " ", "text")
