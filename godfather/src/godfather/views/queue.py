"""SLOTS and QUEUE: the slot bar with each lease's blocks in its colour, and the table of running and waiting
requests, one row each, the selected one marked on both frames."""
from __future__ import annotations

import datetime
from typing import NamedTuple, Optional, cast

from .. import layout as _layout
from .. import tui as _tui
from ..layout import Rect
from ..tui import Canvas, minutes, short, span, text_width
from ..widgets import Panel, Part, Table, Value
from . import common as _common
from .common import Item, Model, cell_run, fill, share, share_style

QUEUE_LABEL_MAX = 40  # the most cells a label asks for when the table is wide enough to share out its width
CPU_BAR = 5  # the CPU bar's base length; a wide table lets it grow to twice that
MARKS_TABLE = Table([("state", "", 0, 2, False), ("priority", "", 3, 2, False)])  # before the shared columns
MARK_COLUMNS = 6  # the state and the priority marks: two cells and a gap each


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
    places, free_at = _layout.slot_labels(area, [x for x, _, _ in labels], filled < slots)
    if free_at is not None:
        canvas.put(area.y + 1, free_at, "free", "dim")
    for (x, width), (_, label, style) in zip(places, labels):
        canvas.put(area.y + 1, x, label, style, width)


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


def queue_layout(inner: int, model: Model, moment: Optional[datetime.datetime] = None) -> QueueLayout:
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
                   max([0] + [text_width(waiting_text(item, n)) for n, item in enumerate(waiters, 1)])),
        "time": max([8] + [text_width(time_cell(item, moment)[0]) for item in items if moment is not None]),
        "pid": max([6] + [len(str(item["pid"])) for item in items])}
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


def time_cell(item: Item, moment: datetime.datetime) -> tuple[str, bool]:
    """A QUEUE row's time and whether it is past its expected run's p90 (fathers#108): a waiter's wait; a lease's
    run, `12m / ~18m` against its run type's p50 where Slotfather knows one. The time is since the grant, as the
    time column always was; the highlight is Slotfather's `overrun`, which leaves out time spent yielded."""
    started = item.get("since") if item["kind"] == "waiting" else item.get("granted")
    if started is None:
        return "—", False
    age = (moment - started).total_seconds()
    expected = _common.expected_run(item)
    if item["kind"] == "waiting" or expected is None:
        return span(age), False
    return f"{minutes(age)} / ~{minutes(expected['p50'])}", bool(item.get("overrun"))


def waiting_text(item: Item, place: int) -> str:
    """A waiting row's state in the cpu column, with the run it expects once granted: `waiting #1 · ~18m`."""
    state = f"yielded #{place}" if item.get("yields") else f"waiting #{place}"  # back in the queue to resume
    expected = _common.expected_run(item)
    return state + (f" · ~{minutes(expected['p50'])}" if expected else "")


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
    layout = queue_layout(panel.inner.width - MARK_COLUMNS, model, moment)
    size = layout.table.width("slots", layout.natural["slots"])
    table = layout.table.shifted(MARK_COLUMNS)
    table.header(panel, "QUEUE")
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
        when, overrun = time_cell(item, moment)
        if waiting:
            cpu: list[Part] = [(waiting_text(item, index - len(holders) + 1), style)]
        else:
            used, allotted = share(item, model)
            cpu = _common.bar(used / allotted if allotted else 0, layout.bar, queue_bar_style(item, model)) + [
                (f" {used:.1f}".rjust(layout.use_width + 1) + "/" + short(allotted).ljust(layout.share_width), style)]
        # The state is the row's first content, where every panel's starts; the priority (blank for normal, so the
        # labels align), then the label after it
        MARKS_TABLE.row(canvas, row, panel.inner.x, {"state": (_common.marks[row_state(item, model)], style),
                                                     "priority": (_common.priority_mark(item), style)})
        values: dict[str, Value] = {
            "label": (str(item["label"]), style), "slots": slot_cells(item, size),
            "time": (when, "warn" if overrun else style), "cpu": cpu, "pid": (str(item["pid"]), style)}
        table.row(canvas, row, panel.inner.x, values)
        if index == selected:  # a selected running row keeps its colour and gets its own tint behind it (#15)
            panel.mark(row, fill(style), style if style in _tui.current_theme.tints else True)


def slot_cells(item: Item, size: int) -> list[Part]:
    """A QUEUE row's slots as countable cells in the row's colour (the fill of its SLOTS blocks for a lease; the
    plain text colour for a waiting request, whose state the cpu column gives); see cell_run()."""
    style = "text" if item["kind"] == "waiting" else str(item["style"])
    return cell_run(int(item["count"]), size, fill(style), whole=bool(item.get("all")), text=style)

