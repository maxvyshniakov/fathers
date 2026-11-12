"""SLOTS and QUEUE: the slot bar with each lease's blocks in its colour, and the table of running and waiting
requests, one row each, the selected one marked on both frames."""
from __future__ import annotations

import datetime
from typing import NamedTuple, Optional, cast

from .. import layout as _layout
from .. import tui as _tui
from ..layout import Rect
from ..tui import Canvas, short, span, text_width
from ..widgets import Panel, Part, Table, Value
from . import common as _common
from .common import Item, Model, cell_run, fill, share, share_style

QUEUE_LABEL_MAX = 40  # the most cells a label asks for when the table is wide enough to share out its width
CPU_BAR = 5  # the CPU bar's base length; a wide table lets it grow to twice that
MARK_COLUMNS = 6  # the state and the priority marks before the table: two cells and a gap each


def draw_slots(canvas: Canvas, area: Rect, model: Model) -> None:
    """Solid slots spread across the area, with two-cell gaps, each lease's label under its first block and a
    right-aligned free label."""
    slots = int(model["slots"])
    cells = _layout.slot_cells(area.x, area.width, slots)
    labels: list[tuple[int, str, str]] = []
    filled = 0
    for holder in cast(list[Item], model["holders"]):
        count = min(int(holder["count"]), slots - filled)
        if count <= 0:
            break
        labels.append((cells[filled][0], str(holder["label"]), str(holder["style"])))
        for start, size in cells[filled:filled + count]:
            canvas.put(area.y, start, "🮅" * size, fill(str(holder["style"])))
        filled += count
    for start, size in cells[filled:]:
        canvas.put(area.y, start, free_slot(size), "free")
    label_end = area.x + area.width
    if filled < slots and area.width >= 4:
        free_start = area.x + area.width - 4
        canvas.put(area.y + 1, free_start, "free", "dim")
        label_end = free_start - 2
    for index, (label_x, label, style) in enumerate(labels):
        limit = labels[index + 1][0] - 2 if index + 1 < len(labels) else label_end
        canvas.put(area.y + 1, label_x, label, style, max(0, limit - label_x))


def free_slot(width: int) -> str:
    """A free slot `width` cells wide: a block in the theme's free colour, or outlined where the theme has no surface
    colours (#14)."""
    if _tui.current_theme.free == "block" or width < 1:
        return "🮅" * width
    return "▭" if width == 1 else "╭" + "─" * (width - 2) + "╮"


def slots_note(model: Model) -> str:
    """SLOTS' top border: cores and slots, how capacity is set, what drains and since when the picture is stale."""
    stale = cast(Optional[datetime.datetime], model.get("stale_since"))
    drain = model.get("draining")
    return (f"{model['cores']} cores · {model['slots']} slots " + ("(manual)" if model["saved"] else "(auto)")
            + (f" · draining {drain}" if drain else "")
            + (f" · stale since {stale.astimezone().strftime('%H:%M')}" if stale else ""))


def queue_bar_style(item: Item, model: Model) -> str:
    """A QUEUE row's CPU bar fills in the holder's fill (its SLOTS colour), except when it warns: red over 1.5 times
    its share, yellow while the lease is idle (#12, share_style())."""
    style = share_style(item, model)
    return fill(str(item["style"])) if style == "accent" else style


class QueueLayout(NamedTuple):
    table: Table  # (key, title, x, width, right-aligned) columns
    natural: dict[str, int]  # each column's own width: its widest value (a label at most QUEUE_LABEL_MAX)
    use_width: int  # the CPU numbers: `use` right-aligned to this, then `/`, then `share` left-aligned to share_width
    share_width: int
    bar: int  # the CPU bar's length


QUEUE_COLUMNS = [("label", "label", False), ("slots", "slots", False), ("cpu", "cpu use/share", False),
                 ("time", "time", True), ("pid", "pid", True)]


def queue_layout(inner: int, model: Model) -> QueueLayout:
    """The QUEUE table's columns in `inner` cells, shared out by their natural widths (Table.share): a wide table
    lets the CPU bar grow with its column up to twice CPU_BAR; a narrow one shrinks the label first, then drops pid,
    time and cpu use/share in that order."""
    holders = cast(list[Item], model["holders"])
    waiters = cast(list[Item], model["waiters"])
    items = holders + waiters
    shares = [share(item, model) for item in holders]
    use_width = max([3] + [len(f"{used:.1f}") for used, _ in shares])
    share_width = max([1] + [len(short(allotted)) for _, allotted in shares])
    natural = {
        "label": max(10, min(QUEUE_LABEL_MAX, max([0] + [text_width(str(item["label"])) for item in items]))),
        "slots": _common.cells_width([int(item["count"]) for item in items]),
        "cpu": max(len("cpu use/share"), CPU_BAR + 1 + use_width + 1 + share_width,
                   max([0] + [len(f"waiting #{n}") for n in range(1, len(waiters) + 1)])),
        "time": 8, "pid": max([6] + [len(str(item["pid"])) for item in items])}
    table = Table.share(inner, QUEUE_COLUMNS, natural, "label")
    grown = table.width("cpu", natural["cpu"]) - natural["cpu"]
    surplus = inner - sum(natural.values()) - 2 * (len(QUEUE_COLUMNS) - 1)
    return QueueLayout(table, natural, use_width, share_width, min(2 * CPU_BAR, CPU_BAR + grown) if surplus > 0
                       else CPU_BAR)


def row_state(item: Item, model: Model) -> str:
    """The MARKS key of a QUEUE row: `asked` (a waiter asked this lease for its slots, the actionable state) over
    `idle` (#12) over `hot` (above 1.5 times its share, as its red bar) over `legacy` (an older client's lease)."""
    if item["kind"] == "waiting":
        return "yielded" if item.get("yields") else "waiting"
    if item.get("asked"):
        return "asked"
    if item.get("idle_since") is not None:
        return "idle"
    used, allotted = share(item, model)
    if used > 1.5 * allotted:
        return "hot"
    return "legacy" if item["kind"] == "legacy" else "running"


def queue_rows(model: Model) -> int:
    """QUEUE's table rows: every request (at least one row, for the empty note) and a gap between running and
    waiting."""
    holders, waiters = cast(list[Item], model["holders"]), cast(list[Item], model["waiters"])
    return max(1, len(holders) + len(waiters)) + bool(holders and waiters)


def draw_requests(canvas: Canvas, rect: Rect, model: Model, selected: int, moment: datetime.datetime) -> None:
    holders = cast(list[Item], model["holders"])
    waiters = cast(list[Item], model["waiters"])
    items = holders + waiters
    row_items: list[Optional[int]] = list(range(len(holders)))
    if holders and waiters:
        row_items.append(None)  # visual separator, never a selectable request
    row_items.extend(range(len(holders), len(items)))
    panel = Panel(canvas, rect, "requests")
    # cpu use/share: the bar starts at the column's left edge on every row, then `use` right-aligned and `/share`
    # left-aligned around the slash, padded to the widest on screen; a waiting row's state starts where the bars do.
    layout = queue_layout(panel.inner.width - MARK_COLUMNS, model)
    size = layout.table.width("slots", layout.natural["slots"])
    layout.table.header(panel, "QUEUE", gutter=MARK_COLUMNS)
    if not items:
        panel.put(0, f"No requests · all {model['slots']} slots free", "dim")
    visual_selected = row_items.index(selected) if selected in row_items else None
    offset, rows = panel.viewport(len(row_items), visual_selected, row_items=row_items)
    for row, visual in panel.rows(offset, rows, len(row_items)):
        index = row_items[visual]
        if index is None:
            continue
        item = items[index]
        waiting = index >= len(holders)
        # One colour per row: a running lease's SLOTS colour (its CPU bar too, but for a warning), the text colour for a
        # request that holds no slots.
        style = "text" if waiting else str(item["style"])
        if waiting:
            place = index - len(holders) + 1
            state = f"yielded #{place}" if item.get("yields") else f"waiting #{place}"  # back in the queue to resume
            age = (moment - item["since"]).total_seconds() if item["since"] else None
            cpu: list[Part] = [(state, style)]
        else:
            age = (moment - item["granted"]).total_seconds() if item["granted"] else None
            used, allotted = share(item, model)
            cpu = _common.bar(used / allotted if allotted else 0, layout.bar, queue_bar_style(item, model)) + [
                (f" {used:.1f}".rjust(layout.use_width + 1) + "/" + short(allotted).ljust(layout.share_width), style)]
        # The state is the row's first content, where every panel's starts; the priority, then the label after it
        canvas.put(row, panel.inner.x, _common.marks[row_state(item, model)], style, 2)
        canvas.put(row, panel.inner.x + 3, _common.priority_mark(item), style, 2)  # blank for normal: labels align
        values: dict[str, Value] = {
            "label": (str(item["label"]), style), "slots": slot_cells(item, size),
            "time": (span(age) if age is not None else "—", style), "cpu": cpu, "pid": (str(item["pid"]), style)}
        layout.table.row(canvas, row, panel.inner.x + MARK_COLUMNS, values)
        if index == selected:  # a selected running row keeps its colour and gets its own tint behind it (#15)
            panel.mark(row, fill(style), style if style in _tui.current_theme.tints else True)


def slot_cells(item: Item, size: int) -> list[Part]:
    """A QUEUE row's slots as countable cells in the row's colour (the fill of its SLOTS blocks for a lease; the
    plain text colour for a waiting request, whose state the cpu column gives); see cell_run()."""
    style = "text" if item["kind"] == "waiting" else str(item["style"])
    return cell_run(int(item["count"]), size, fill(style), whole=bool(item.get("all")), text=style)

