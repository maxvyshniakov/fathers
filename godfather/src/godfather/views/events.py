"""EVENTS: one feed of every present source (fathers#84), newest first, read from the tools' event logs.

Columns: time, source, event, slots, label, detail and pid. With Slotfather alone the source and event columns are
not drawn: its slot cells say what happened (owner, 2026-11-12), so its rows look as they did when they came from its
snapshot. The detail, right-aligned, comes from each event's structured fields (events.py's formatters). A narrow
panel shortens the label first, then drops pid, then slots, then shows the source as a one-cell coloured mark.

`f` cycles the source filter and `/` edits a text filter in the bottom border (Filter); the top border names the
active filter where there is more than one source or a filter is set."""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Optional, cast

from ..layout import Rect
from ..tui import Canvas, clock, text_width
from ..widgets import FLEXIBLE_MIN, FOCUS_FRAME, REST_FRAME, Panel, Part, Spec, Table
from . import common as _common
from .common import Model, cell_run, cells_width

DETAIL_MIN, DETAIL_MAX = 6, 28  # the detail column's least and greatest width; it grows to its longest value
LABEL_LEAST = 6  # the label's least width with several sources, once the event has shrunk
EVENT_MAX, EVENT_MIN = 10, 5  # the event column's greatest and least width
TIME_WIDTH, TIME_MIXED = 10, 9  # the time column (`11:59:35`, `Wed 11:59`); with several sources it gives a cell
SOURCE_STYLES = {"slot": "source-slot", "lane": "source-lane", "junk": "source-junk"}  # theme tokens by tag
TOO_OLD = "slotfather too old for events"  # its capabilities name no event log
TOO_NEW = "slotfather's events need a newer godfather"  # they name one of no schema version this one reads


@dataclass(frozen=True)
class Filter:
    """EVENTS' filters (fathers#84): the source shown (None: all), the text matched against kind, subject id,
    label and detail, and whether the text field is open. They last until Godfather restarts."""
    source: Optional[str] = None
    text: str = ""
    editing: bool = False

    def active(self) -> bool:
        return self.source is not None or bool(self.text)


def shown_rows(model: Optional[Model], found: Filter) -> list[dict[str, Any]]:
    """The rows the filter lets through, oldest first."""
    from ..events import text_of

    rows = cast(list[dict[str, Any]], (model or {}).get("events") or [])
    needle = found.text.lower()
    return [row for row in rows if (found.source is None or row.get("source") == found.source)
            and (not needle or needle in text_of(row))]


def present(model: Optional[Model]) -> list[tuple[str, str]]:
    """The sources EVENTS reads, (name, tag) in the order slotfather, lanefather, junkfather."""
    pairs = cast(list[Any], (model or {}).get("sources") or [])
    return [(str(pair[0]), str(pair[1])) for pair in pairs]


def source_style(tag: str) -> str:
    return SOURCE_STYLES.get(tag, "dim")


def filter_note(model: Model, found: Filter) -> list[Part]:
    """The top border's note: why there are no events, then the active filter (`all sources`, `lanefather`,
    `"710"`) where there is more than one source or a filter is set; both where both apply (fathers#84 review: a
    filter that hides rows is always named)."""
    parts: list[Part] = []
    if model.get("events_note"):
        parts.append((str(model["events_note"]), "warn"))
    if len(present(model)) < 2 and not found.active():
        return parts
    words = [found.source or "all sources"] if found.source or len(present(model)) > 1 else []
    if found.text:
        words.append(f'"{found.text}"')
    if words:
        parts += [(" · ", "dim")] if parts else []
        parts.append((" · ".join(words), "text" if found.active() else "dim"))
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
    """The detail column: the formatter's parts, read from the event's fields (no prose is parsed)."""
    return [(str(text), str(style)) for text, style in row.get("detail") or [("", "dim")]]


def event_outcome(row: dict[str, Any], marks: _common.Marks) -> list[Part]:
    """The detail with the run's mark (fathers#108): `ok 34m ▲` for an outlier, `ok 34m ↗` for the run that turned
    its run type's trend slower; nothing for a trend row, whose label says it all."""
    if row.get("kind") == "run.trend":
        return [("", "dim")]
    found = event_result(row)
    mark = marks.run.get(str(row.get("run")))
    return found + [(" " + mark, "warn")] if mark else found


def trend_label(row: dict[str, Any]) -> list[Part]:
    """A trend row's label (fathers#108): the run type, its parts in words with the first capitalised, then how its
    recent median compares and why, `Straylight gate · slower +18% · same plan`; yellow when slower."""
    trend: dict[str, Any] = row.get("trend") or {}
    words = " ".join(part for part in str(row.get("label") or "?").split(" · ") if part)
    name = words[:1].upper() + words[1:]
    verdict = str(trend.get("verdict") or "changed")
    ratio = trend.get("ratio")
    change = f" {round((float(ratio) - 1) * 100):+d}%" if isinstance(ratio, (int, float)) and ratio > 0 else ""
    why = str(trend.get("why") or "").replace(": ", " ")
    reason = f" · {verdict}{change}" + (f" · {why}" if why else "")
    return [(name, "text"), (reason, "warn" if verdict == "slower" else "dim")]


def event_cells(row: dict[str, Any], size: int, style: str = "text") -> list[Part]:
    """A row's slots, in the text colour (owner, 2026-11-12): filled for slots taken (granted), hollow for slots
    handed back (released, a held lease reaped, a yield) or never held (a waiter that left or was reaped)."""
    return cell_run(int(row.get("count") or 0), size, style, hollow=not row.get("filled"))


def columns(width: int, rows: list[dict[str, Any]], results: list[list[Part]], mixed: bool,
            slots: bool) -> list[Spec]:
    """The columns for an inner width, in the order a narrow panel gives way: the label shortens to FLEXIBLE_MIN,
    then pid goes, then slots, then the source becomes a one-cell mark."""
    cells = cells_width([int(row.get("count") or 0) for row in rows])
    # One source keeps today's widths; with several the detail takes at most a quarter of the panel
    most = DETAIL_MAX if not mixed else max(DETAIL_MIN, min(DETAIL_MAX, width // 4))
    detail = min(most, max([DETAIL_MIN] + [sum(text_width(text) for text, _ in parts) for parts in results]))
    event = min(EVENT_MAX, max([5] + [text_width(str(row.get("event") or "")) for row in rows]))
    pids = [len(str(row["pid"])) for row in rows if row.get("pid")]
    stages: list[tuple[bool, bool, int]] = [(bool(pids), slots, 4), (False, slots, 4), (False, False, 4),
                                            (False, False, 1)]  # (pid, slots, source cells)
    spec: list[Spec] = []
    for pid, cells_shown, source in stages:
        spec = [("time", "time", TIME_WIDTH if not mixed else TIME_MIXED, False, 0)]
        if mixed:
            spec += [("source", "source" if source > 1 else "", source, False, 0),
                     ("event", "event", event, False, 0)]
        if cells_shown:
            spec.append(("slots", "slots", cells, False, 0))
        spec += [("label", "label", 0, False, 0), ("detail", "detail", detail, True, 0)]
        if pid:
            spec.append(("pid", "pid", max([3] + pids), True, 0))
        if room(spec, width) >= FLEXIBLE_MIN:
            break
    # Narrower still, these shrink and never go: the event to eight cells (`released`); with several sources the
    # label then gives way down to LABEL_LEAST before the detail, which says how it went (`2 findings · 9m`); then
    # the detail to eight (`ok 5m41s`), then both to their least
    label = LABEL_LEAST if mixed else FLEXIBLE_MIN
    for key, least, keep in (("event", 8, FLEXIBLE_MIN), ("detail", 8, label), ("event", EVENT_MIN, label),
                             ("detail", DETAIL_MIN, label)):
        if room(spec, width) < keep:
            spec = [(name, title, max(least, size + room(spec, width) - keep) if name == key else size,
                     right, rank) for name, title, size, right, rank in spec]
    return spec


def room(spec: list[Spec], width: int) -> int:
    """The cells the label is left with."""
    return width - sum(column[2] for column in spec) - 2 * (len(spec) - 1)


def draw_events(canvas: Canvas, rect: Rect, model: Model, moment: Optional[datetime.datetime] = None,
                found: Optional[Filter] = None) -> None:
    """EVENTS: time · [source · event] · slots · label · detail · pid, newest first. The slot cells say what
    happened to slots (filled: taken, hollow: handed back), the label takes every cell the other columns leave, and
    the detail, right-aligned, says how it went. Scrolled down, what arrived since is counted in its bottom border
    (Home goes back to it); the text field, while it is open, takes that place."""
    found = found or Filter()
    rows = shown_rows(model, found)[::-1]
    panel = Panel(canvas, rect, "events")
    sources = present(model)
    mixed = len(sources) > 1
    marks = _common.marks_of(canvas)
    results = [event_outcome(row, marks) for row in rows]
    slots = not sources or any(name == "slotfather" for name, _ in sources)
    table = clipped(Table.fit(panel.inner.width, columns(panel.inner.width, rows, results, mixed, slots)),
                    panel.inner.width)
    size = table.width("slots", 0)
    note = filter_note(model, found)
    table.header(panel, "EVENTS")
    if note:
        border_note(panel, table, note)
    skipped = skipped_note(model)
    offset, visible = panel.viewport(len(rows))
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
        row, outcome = rows[position], results[position]
        tag = str(row.get("tag") or "")
        source: Part = (tag if table.width("source", 4) > 1 else tag[:1], source_style(tag))
        when = (clock(cast(Optional[datetime.datetime], row.get("when")), moment), "dim")
        if row.get("kind") == "run.trend":  # its own row: the run type and why across slots, label and detail
            table.row(canvas, line, panel.inner.x, {"time": when, "source": source, "event": ("trend", "text")})
            start = next((x for key, _, x, _, _ in table.placed if key in ("slots", "label")), 0)
            end = panel.inner.width
            canvas.segments(line, panel.inner.x + start, trend_label(row), end - start)
            continue
        table.row(canvas, line, panel.inner.x, {
            "time": when, "source": source, "event": (str(row.get("event") or ""), "text"),
            "label": str(row.get("label") or "?"), "slots": event_cells(row, size) if size else "",
            "detail": outcome, "pid": (str(row.get("pid") or ""), "dim")})


def clipped(table: Table, width: int) -> Table:
    """The columns that start within `width`, the last cut to it: on a panel too narrow for every column's least,
    nothing is drawn past its frame."""
    return Table([(key, title, x, min(size, width - x), right) for key, title, x, size, right in table.placed
                  if x < width])


def border_note(panel: Panel, table: Table, note: list[Part]) -> None:
    """The note set into the top border after the title (`EVENTS ── all sources ──`), over the column labels it
    reaches, which give way: the labels after it stay over their columns. A note longer than the border is cut with
    an ellipsis, never left out."""
    canvas, (y, x, _, width) = panel.canvas, panel.rect
    start = x + 3 + text_width("EVENTS") + 1
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
