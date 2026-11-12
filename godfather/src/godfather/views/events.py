"""EVENTS: what the queue did lately, newest first, as time · slots · label · result."""
from __future__ import annotations

import re
from typing import Any, Optional, cast

from ..layout import Rect
from ..tui import Canvas, clock, minutes, text_width
from ..widgets import Panel, Part, Table
from . import common as _common
from .common import Model, cell_run, cells_width

SPAN = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")
RESULT_MIN = 6  # the result column's least width ("result"); it grows to its longest value


def span_seconds(text: str) -> Optional[float]:
    """A span as span() writes it (45s, 3m25s, 1h05m, 2d03h) back in seconds; None for anything else."""
    found = SPAN.fullmatch(text.strip())
    if not text.strip() or found is None or not any(found.groups()):
        return None
    days, hours, mins, secs = (int(part or 0) for part in found.groups())
    return float(((days * 24 + hours) * 60 + mins) * 60 + secs)


def event_result(event: dict[str, Any]) -> Part:
    """EVENTS' result column (owner, 2026-11-12), short, from what Slotfather says of the event (its `detail`):
    `wait 32s` or `no wait` for a grant; `ok 3m25s` (muted) for a lease that ended well, `exit 1 · 26s` (red) for
    one that did not; `gave up 5m` for a waiter that left; `owner died · 12m` for a reap, red where it held slots,
    yellow where it only waited. (text, style)."""
    verb, detail = str(event.get("verb")), str(event.get("detail") or "")
    if verb == "granted":
        waited = re.fullmatch(r"waited (\S+)", detail)
        if waited:
            return f"wait {waited[1]}", "text"
        return (detail, "dim") if detail else ("", "dim")
    if verb == "released":
        ended = re.fullmatch(r"exit (-?\d+)(?:, ran (\S+))?", detail)
        if ended is None:
            ran = re.fullmatch(r"ran (\S+)", detail)
            return (f"ended {ran[1]}" if ran else detail, "dim")
        status, ran = int(ended[1]), ended[2]
        if status == 0:
            return ("ok " + ran if ran else "ok"), "dim"
        return f"exit {status}" + (f" · {ran}" if ran else ""), "bad"
    if verb == "left":
        after = re.fullmatch(r"gave up after (\S+)", detail)
        seconds = span_seconds(after[1]) if after else None
        return ("gave up" + (f" {minutes(seconds)}" if seconds is not None else "")), "dim"
    if verb == "reaped":
        ran = re.search(r"ran (\S+)", detail)
        seconds = span_seconds(ran[1]) if ran else None
        return ("owner died" + (f" · {minutes(seconds)}" if seconds is not None else ""),
                "bad" if event.get("state") == "held" or int(event.get("delta") or 0) < 0 else "warn")
    if verb == "waiting":  # a long wait, said by the waiter: `12m, est. 10:30`
        waited = re.match(r"(\S+?),", detail)
        seconds = span_seconds(waited[1]) if waited else None
        return (f"waiting {minutes(seconds)}" if seconds is not None else detail), "warn"
    return detail, "dim"  # reprioritized: `batch → interactive`


def event_outcome(event: dict[str, Any]) -> list[Part]:
    """The result column with the run's mark (fathers#108): `ok 34m ▲` for an outlier, `ok 34m ↗` for the run that
    turned its run type's trend slower; nothing for a trend row, whose label says it all."""
    if event.get("verb") == "trend":
        return [("", "dim")]
    found = [event_result(event)]
    mark = _common.run_marks.get(str(event.get("run")))
    return found + [(" " + mark, "warn")] if mark else found


def trend_label(event: dict[str, Any]) -> list[Part]:
    """A trend row's label (fathers#108): the run type, its parts in words with the first capitalised, then how its
    recent median compares and why, `Straylight gate · slower +18% · same plan`; yellow when slower."""
    trend: dict[str, Any] = event.get("trend") or {}
    words = " ".join(part for part in str(event.get("label") or "?").split(" · ") if part)
    name = words[:1].upper() + words[1:]
    verdict = str(trend.get("verdict") or "changed")
    ratio = trend.get("ratio")
    change = f" {round((float(ratio) - 1) * 100):+d}%" if isinstance(ratio, (int, float)) and ratio > 0 else ""
    why = str(trend.get("why") or "").replace(": ", " ")
    reason = f" · {verdict}{change}" + (f" · {why}" if why else "")
    return [(name, "text"), (reason, "warn" if verdict == "slower" else "dim")]


def event_cells(event: dict[str, Any], size: int, style: str = "text") -> list[Part]:
    """An EVENTS row's slots, in the text colour (owner, 2026-11-12): filled for slots taken (granted), hollow for
    slots handed back (released, a held lease reaped) or never held (a waiter that left or was reaped). The cells
    carry what happened; the event's name is not a column."""
    return cell_run(int(event["count"]), size, style, hollow=event["verb"] != "granted")


def draw_events(canvas: Canvas, rect: Rect, model: Model) -> None:
    """EVENTS (owner, 2026-11-12): time · slots · label · result. The slot cells say what happened (filled: taken,
    hollow: handed back), the label takes every cell the other columns leave, and the result, right-aligned, says
    how it went. Scrolled down, what arrived since is counted in its bottom border (Home goes back to it)."""
    events = cast(list[dict[str, Any]], model["events"])[::-1]
    panel = Panel(canvas, rect, "events")
    cells = cells_width([int(event["count"]) for event in events])
    results = [event_outcome(event) for event in events]
    result = max([RESULT_MIN] + [sum(text_width(text) for text, _ in parts) for parts in results])
    table = Table.fit(panel.inner.width, [
        ("time", "time", 10, False, 0), ("slots", "slots", cells, False, 1), ("label", "label", 0, False, 0),
        ("result", "result", result, True, 2)])
    size = table.width("slots", cells)
    table.header(panel, "EVENTS")
    offset, visible = panel.viewport(len(events))
    if visible and offset and canvas.fresh_events:
        panel.bottom(f" ↑ {canvas.fresh_events} new ", "text" if panel.focused else "dim")
    if not events:
        panel.put(0, "No grants, releases or reaps logged yet", "dim")
    for row, position in panel.rows(offset, visible, len(events)):
        event, outcome = events[position], results[position]
        if event.get("verb") == "trend":  # its own row: the run type and why across slots, label and result
            table.row(canvas, row, panel.inner.x, {"time": (clock(event["when"]), "dim")})
            start = next((x for key, _, x, _, _ in table.placed if key in ("slots", "label")), 0)
            end = max(x + width for _, _, x, width, _ in table.placed)
            canvas.segments(row, panel.inner.x + start, trend_label(event), end - start)
            continue
        table.row(canvas, row, panel.inner.x, {
            "time": (clock(event["when"]), "dim"), "label": str(event["label"] or "?"),
            "slots": event_cells(event, size), "result": outcome})
