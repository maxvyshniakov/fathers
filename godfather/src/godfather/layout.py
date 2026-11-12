"""Where everything goes, in one place (fathers#90): each panel declares its least, comfortable and natural size and
the order it gives way in, and the functions here allocate the screen for the wide, one-column and small
arrangements, MACHINE's inside, the centred overlays and the SLOTS bar. Views draw into the rectangles they are
given and do no arithmetic of their own about where things go.

Today's rules, which these tables reproduce exactly: SLOTS on top at its five rows; from WIDE columns the panels sit
in two columns (QUEUE over EVENTS on the left, DETAILS over MACHINE on the right, the right column 2/5 of the width
and at least 52); below that one column (QUEUE, DETAILS, then EVENTS beside MACHINE, or MACHINE alone where EVENTS
would get under 38 columns, which is under 83); under SMALL_WIDTH or SMALL_HEIGHT only the slots and a note. MACHINE
sits at its natural height (7 with its chart beside the rows, from 62 columns; 11 stacked); DETAILS takes the rest.
Tab goes by columns, left to right and each top to bottom."""
from __future__ import annotations

from typing import Callable, NamedTuple, Optional


class Rect(NamedTuple):
    y: int
    x: int
    height: int
    width: int

    def inset(self, top: int, left: int, bottom: int, right: int) -> Rect:
        return Rect(self.y + top, self.x + left, max(0, self.height - top - bottom), max(0, self.width - left - right))


WIDE = 110  # columns from which the panels sit in two columns
SMALL_WIDTH, SMALL_HEIGHT = 50, 14  # below either, only the slots and a note
SLOTS_HEIGHT = 5
SMALL_NOTE_ROW = 6
RIGHT_MIN = 52  # the right column's least width; it takes 2/5 of the screen where that is more
NARROW_MACHINE, NARROW_EVENTS = 44, 38  # the least width each takes side by side in one column; below both, EVENTS goes
QUEUE_MIN, QUEUE_FLOOR, DETAILS_FLOOR = 10, 6 + 4, 9 + 4  # rows: QUEUE's least, one column's comfortable floors
EVENTS_MIN, DETAILS_MIN, DETAILS_WIDE_MIN, MACHINE_MIN = 5, 5, 10, 6

# MACHINE inside: a frame, a blank row, three resource rows and the history chart with its axis line under it
CHART_ROWS, MIN_CHART_ROWS = 4, 2  # the chart's rows when there is room; fewer than two, it goes (owner)
MACHINE_HEIGHT = 7 + CHART_ROWS  # stacked: frame, a blank row, 3 resource rows, the chart and its axis line
MACHINE_SIDE_HEIGHT = 7  # the chart beside the rows: frame, a blank row, 3 rows level with 3 chart rows, the axis
MACHINE_LABEL = 8  # a MACHINE row: its health mark, a gap, the label (load, mem, swap), a gap, then the bar
MACHINE_BAR_MIN, MACHINE_BAR_SIDE = 4, 12  # the shortest bar before values go short; beside the chart, its bars
MACHINE_GAP, MACHINE_CHART_MIN = 2, 24  # between the rows and a chart beside them, which needs this many columns
# The width from which the chart sits beside the rows: frame and padding (6), the rows with a 9-cell value
# column, the gap and the chart's least width (owner, 2026-11-12: picked by rendering)
MACHINE_SIDE_WIDTH = 6 + MACHINE_LABEL + MACHINE_BAR_SIDE + 1 + 9 + MACHINE_GAP + MACHINE_CHART_MIN
MACHINE_RESOURCES = 3


class Tier(NamedTuple):
    """One way to share a column's rows: each panel's least rows, in the order they are kept (`whole`: all of them or
    none, so the next tier is tried); then growth towards each one's natural height, in `grow` order; what is left
    goes to the first of `rest` that was placed."""
    minimums: tuple[tuple[str, int], ...]
    grow: tuple[str, ...]
    rest: tuple[str, ...]
    whole: bool = False


def stack(available: int, tiers: tuple[Tier, ...], naturals: dict[str, int]) -> dict[str, int]:
    """Rows for the panels of one column by the first tier that fits; a panel with no natural height stops at its
    least and only the rest makes it grow."""
    for tier in tiers:
        if tier.whole and sum(minimum for _, minimum in tier.minimums) > available:
            continue
        heights: dict[str, int] = {}
        left = available
        for name, minimum in tier.minimums:
            if minimum <= left:
                heights[name], left = minimum, left - minimum
        for name in tier.grow:
            if name in heights:
                extra = min(left, max(0, naturals.get(name, 0) - heights[name]))
                heights[name], left = heights[name] + extra, left - extra
        taker = next((name for name in tier.rest if name in heights), None)
        if taker is not None:
            heights[taker] += left
        return heights
    return {}


def split(width: int, natural: int, least: int, other_least: int, gap: int = 1) -> tuple[int, int]:
    """Two panels side by side in `width`: the first at its natural width (at least `least`), the second the rest
    after a gap; (first, second) widths, with the second 0, and the first the whole width, where the second would
    get under `other_least`."""
    first = max(least, natural)
    return (first, width - first - gap) if width - first - gap >= other_least else (width, 0)


class Overview(NamedTuple):
    arrangement: str  # "wide", "column" or "small"
    panels: dict[str, Rect]  # slots, requests, events, details, machine: those that get rows
    order: tuple[str, ...]  # Tab's order
    slots: Rect  # where the slot blocks and their labels go
    note_row: Optional[int] = None  # the small screen's note


def queue_height(rows: int) -> int:
    """QUEUE's natural height for its table rows: a frame and a blank padding row above and below them."""
    return rows + 4


def overview(width: int, height: int, rows: int, machine_natural: Callable[[int], int]) -> Overview:
    """The overview at this size; `rows` is QUEUE's table rows (its entries, at least one, and the gap between
    running and waiting), `machine_natural` MACHINE's natural height at a width."""
    if width < SMALL_WIDTH or height < SMALL_HEIGHT:
        return Overview("small", {}, ("requests",), Rect(min(2, height - 4), 3, 2, width - 6),
                        SMALL_NOTE_ROW if SMALL_NOTE_ROW < height - 1 else None)
    queue = queue_height(rows)
    available = height - 1 - SLOTS_HEIGHT  # the footer row and SLOTS
    panels: dict[str, Rect] = {}
    if width >= WIDE:
        right = max(RIGHT_MIN, width * 2 // 5)
        left = width - right - 1
        heights = stack(available, (Tier((("requests", min(queue, QUEUE_MIN)), ("events", EVENTS_MIN)),
                                         grow=("requests",), rest=("events", "requests")),), {"requests": queue})
        column(panels, ("requests", "events"), heights, SLOTS_HEIGHT, 0, left)
        sides = stack(height - 1, (Tier((("details", DETAILS_WIDE_MIN), ("machine", MACHINE_MIN)), grow=("machine",),
                                        rest=("details",)),), {"machine": machine_natural(right)})
        column(panels, ("details", "machine"), sides, 0, left + 1, right)
        arrangement = "wide"
        slots_width = left
    else:
        machine_width, events_width = split(width, (width - 1) // 2, NARROW_MACHINE, NARROW_EVENTS)
        bottom = machine_natural(machine_width)
        heights = stack(available, (
            Tier((("requests", min(queue, QUEUE_FLOOR)), ("details", DETAILS_FLOOR), ("bottom", MACHINE_MIN)),
                 grow=("bottom", "requests"), rest=("details", "requests"), whole=True),
            Tier((("requests", min(queue, QUEUE_MIN)), ("details", DETAILS_MIN), ("bottom", MACHINE_MIN)),
                 grow=("requests", "details", "bottom"), rest=("details", "requests"))),
            {"requests": queue, "details": DETAILS_MIN, "bottom": bottom})
        column(panels, ("requests", "details", "bottom"), heights, SLOTS_HEIGHT, 0, width)
        row = panels.pop("bottom", None)
        if row is not None and events_width:  # EVENTS on the left, MACHINE on the right (owner, 2026-11-12)
            panels["events"] = Rect(row.y, 0, row.height, events_width)
            panels["machine"] = Rect(row.y, events_width + 1, row.height, machine_width)
        elif row is not None:
            panels["machine"] = row
        arrangement = "column"
        slots_width = width
    panels = {"slots": Rect(0, 0, SLOTS_HEIGHT, slots_width), **panels}
    return Overview(arrangement, panels, tab_order(panels), Rect(2, 3, 2, slots_width - 6))


def column(panels: dict[str, Rect], names: tuple[str, ...], heights: dict[str, int], y: int, x: int,
           width: int) -> None:
    """The panels of one column top to bottom from row y, those with rows only."""
    for name in names:
        if heights.get(name):
            panels[name] = Rect(y, x, heights[name], width)
            y += heights[name]


FOCUSABLE = ("requests", "details", "events")  # the panels Tab and a click focus


def tab_order(panels: dict[str, Rect]) -> tuple[str, ...]:
    """Tab's order: by columns, left to right and each top to bottom, as the layout placed them; QUEUE first even
    where it got no rows, so the focus always has somewhere to go back to."""
    found = sorted((name for name in FOCUSABLE if name in panels), key=lambda name: (panels[name].x, panels[name].y))
    return tuple(found if "requests" in found else ["requests", *found])


class Machine(NamedTuple):
    side: bool  # the chart beside the rows (else under them)
    rows: Optional[Rect]  # the three resource rows
    compact: Optional[Rect]  # or, short of rows, one line with all three
    chart: Optional[Rect]  # the chart's rows; its axis line is the row under them


def machine_side_by_side(width: int) -> bool:
    """Whether MACHINE is wide enough for its chart beside the three rows rather than under them (owner)."""
    return width >= MACHINE_SIDE_WIDTH


def machine_height(width: int) -> int:
    """MACHINE's natural height at this width: shorter with the chart beside the rows."""
    return MACHINE_SIDE_HEIGHT if machine_side_by_side(width) else MACHINE_HEIGHT


def machine(panel: Rect, values: int) -> Machine:
    """MACHINE's inside; `values` is the widest resource value. Wide (owner, 2026-11-12): the chart beside the rows,
    its rows level with theirs, its axis under it alone. Stacked: a blank row, the rows, the chart and its axis line,
    which the bottom frame follows directly. Short of rows the chart gives up height first, 4 → 3 → 2 rows and then
    entirely (never one row); then the blank rows; then the rows become one compact line. Without the chart a blank
    row stays under the rows as well as over them (owner); with room for one only, it is the lower one."""
    y, x, height, width = panel
    inner, interior, rows = width - 6, height - 2, MACHINE_RESOURCES
    if machine_side_by_side(width) and interior >= 1 + rows + 1:
        metrics = MACHINE_LABEL + MACHINE_BAR_SIDE + 1 + values
        return Machine(True, Rect(y + 2, x + 3, rows, metrics), None,
                       Rect(y + 2, x + 3 + metrics + MACHINE_GAP, min(CHART_ROWS, interior - 2),
                            inner - metrics - MACHINE_GAP))
    chart_rows = min(CHART_ROWS, interior - 1 - rows - 1)
    chart_rows = chart_rows if chart_rows >= MIN_CHART_ROWS else 0
    row = y + 2 if interior >= rows + (1 if chart_rows else 2) else y + 1
    lines: Optional[Rect] = None
    compact: Optional[Rect] = None
    if interior >= rows:
        lines, row = Rect(row, x + 3, rows, inner), row + rows
    elif interior >= 1:
        compact, row = Rect(row, x + 3, 1, inner), row + 1
    return Machine(False, lines, compact, Rect(row, x + 3, chart_rows, inner) if chart_rows else None)


def centred(width: int, height: int, most: int, rows: int, chrome: int) -> Rect:
    """An overlay over the screen: at most `most` columns and two less than the screen, as tall as its `rows` and
    its `chrome` (frame, padding, footer) allow, centred."""
    box_width = min(max(0, width - 2), most)
    box_height = min(max(0, height - 2), rows + chrome)
    return Rect((height - box_height) // 2, (width - box_width) // 2, box_height, box_width)


def slot_cells(x: int, width: int, slots: int) -> list[tuple[int, int]]:
    """The slot blocks spread across `width` cells from x: (start, width) each, two-cell gaps where they fit, else
    one, else none, the first blocks a cell wider where the width does not divide."""
    area = max(0, width)
    gap = 2 if area >= slots + 2 * (slots - 1) else 1 if area >= 2 * slots - 1 else 0
    cell, remainder = divmod(max(0, area - gap * (slots - 1)), slots)
    found: list[tuple[int, int]] = []
    for index in range(slots):
        size = cell + (index < remainder)
        found.append((x, size))
        x += size + gap
    return found


SETTINGS_WIDTH, SETTINGS_CHROME = 80, 9  # Settings' most columns; frame, padding, the rule, notes and footer rows
SETTINGS_MIN_WIDTH, SETTINGS_MIN_HEIGHT = 30, 12  # below this the panel only says to enlarge the terminal


class SettingsBox(NamedTuple):
    rows: Rect  # the scrolling list, two cells in, two padding cells on the right (the scrollbar in one)
    notes: Rect  # the rule over the description and what a change did, as wide as the frame allows
    small: bool  # too small to show: only a note to enlarge the terminal


def settings(box: Rect) -> SettingsBox:
    """Settings' inside: its list, then a rule and up to three lines of notes, then the footer."""
    y, x, height, width = box
    inner = max(0, width - 4)
    return SettingsBox(Rect(y + 2, x + 2, max(0, height - SETTINGS_CHROME), max(0, inner - 2)),
                       Rect(y + height - 6, x + 2, 4, inner),
                       height < SETTINGS_MIN_HEIGHT or width < SETTINGS_MIN_WIDTH)


NOTICE_WIDTH, NOTICE_HEIGHT = 12, 5  # below either the notice is one line, without its frame


def notice(width: int, height: int, lines: int) -> Rect:
    """The one panel that stands in for the overview: as tall as its lines, above the footer."""
    return Rect(0, 0, min(height - 1, lines + 4), width)
