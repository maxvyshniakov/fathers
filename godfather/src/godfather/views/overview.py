"""The overview, Slotfather's dashboard screen moved unchanged: SLOTS, QUEUE, DETAILS, EVENTS and MACHINE, the
wide and one-column layouts, the footer, help and the changelog. It draws the model the Slotfather adapter decodes
from `slotfather status --json --full` (docs/contracts/slotfather.md) on a tui.Canvas; dashboard_draw() lays it
out, and the live view and --once write that canvas as the same escape sequences, so they are the same picture.
"""
from __future__ import annotations

import datetime
import json
import math
import re
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from .. import theme as _theme
from .. import tui as _tui
from .. import version as _version
from ..adapters import lanefather as _lanefather
from ..tui import (HELP_GAP, HELP_INDENT, HINT_GAP, SELECTED_FRAME, SPINNER, Canvas, Release, add_heading, allocate,
                   clip, clock, columns, dashboard_text, draw_overlay, draw_row, hints_width, megabytes, minutes,
                   parse_changelog, safe_link, short, span, table_header, table_view, text_width, tilde, version_key,
                   wrap)

if TYPE_CHECKING:
    from .settings import Settings


# dashboard_model() gathers plain data and dashboard_draw() lays it out on a Canvas of styled cells; the live view
# and --once write that canvas as the same escape sequences, so they are the same picture.

SPARK = "▁▂▃▄▅▆▇█"
CHART_ROWS, MIN_CHART_ROWS = 4, 2  # the chart's rows when there is room; fewer than two, it goes (owner)
MACHINE_HEIGHT = 7 + CHART_ROWS  # stacked: frame, a blank row, 3 resource rows, the chart and its axis line
MACHINE_SIDE_HEIGHT = 7  # the chart beside the rows: frame, a blank row, 3 rows level with 3 chart rows, the axis
MACHINE_LABEL = 8  # a MACHINE row: its health mark, a gap, the label (load, mem, swap), a gap, then the bar
MACHINE_BAR_MIN, MACHINE_BAR_SIDE = 4, 12  # the shortest bar before values go short; beside the chart, its bars
MACHINE_GAP, MACHINE_CHART_MIN = 2, 24  # between the rows and a chart beside them, which needs this many columns
# The width from which the chart sits beside the rows: frame and padding (6), the rows with a 9-cell value
# column, the gap and the chart's least width (owner, 2026-11-12: picked by rendering)
MACHINE_SIDE_WIDTH = 6 + MACHINE_LABEL + MACHINE_BAR_SIDE + 1 + 9 + MACHINE_GAP + MACHINE_CHART_MIN
HISTORY_WINDOW = 3600  # seconds of queue.log the utilisation strip rebuilds
LOG_TAIL = 262144  # bytes of queue.log read per refresh
WIDE = 110  # columns from which the panels sit side by side


def held_history(events: list[dict], held_now: int, now_moment: datetime.datetime,
                 window: float = HISTORY_WINDOW * 2) -> list[tuple[datetime.datetime, datetime.datetime, int]]:
    """Held slots over the last `window` seconds as (start, end, slots) steps, walked back from held_now."""
    begin = now_moment - datetime.timedelta(seconds=window)
    steps, level, end = [], held_now, now_moment
    for event in reversed(events):
        if event["when"] <= begin:
            break
        if event["delta"] and event["when"] < end:
            steps.append((event["when"], end, max(0, level)))
            end = event["when"]
        level -= event["delta"]
    steps.append((begin, end, max(0, level)))
    return steps[::-1]


def history_buckets(steps: list, columns: int) -> list[int]:
    """Peak slots per fixed wall-clock bucket; only the newest bucket is incomplete.

    Anchoring buckets to the clock keeps old peaks stable between scroll steps.
    """
    if not steps or columns <= 0:
        return []
    begin, end = steps[0][0], steps[-1][1]
    seconds = max(1, math.ceil(HISTORY_WINDOW / columns))
    edge = math.ceil(end.timestamp() / seconds) * seconds
    end = datetime.datetime.fromtimestamp(edge, end.tzinfo)
    width = datetime.timedelta(seconds=seconds)
    begin = end - width * columns
    buckets = []
    for column in range(columns):
        low, high = begin + width * column, begin + width * (column + 1)
        buckets.append(max((level for start, stop, level in steps if start < high and stop > low), default=0))
    return buckets


def history_summary(steps: list, slots: int) -> dict:
    # Retain extra history for whole clock buckets, but summarise only the trailing hour.
    if steps:
        begin = steps[-1][1] - datetime.timedelta(seconds=HISTORY_WINDOW)
        steps = [(max(start, begin), stop, level) for start, stop, level in steps if stop > begin]
    total_seconds = sum((stop - start).total_seconds() for start, stop, _ in steps) or 1
    held = sum((stop - start).total_seconds() * min(level, slots) for start, stop, level in steps)
    full = sum((stop - start).total_seconds() for start, stop, level in steps if level >= slots)
    return {"average": held / total_seconds / slots, "full": full / total_seconds,
            "peak": max((level for _, _, level in steps), default=0)}


def dashboard_colours(model: dict, assigned: dict) -> None:
    """Gives each running lease a colour it keeps until it ends, preferring colours no other lease shows, so the
    slot bar, the table swatch and the detail heading stay linked as the queue moves."""
    keys = {h["key"] for h in model["holders"]}
    for key in [key for key in assigned if key not in keys]:
        del assigned[key]
    count = _tui.current_theme.holders()  # 8 holder colours less any the theme reserves, or the builtin 6 (#14)
    for holder in model["holders"]:
        if holder["kind"] != "held":
            continue
        if holder["key"] not in assigned:
            taken = [index % count for index in cast("dict[Any, int]", assigned).values()]
            assigned[holder["key"]] = min(range(count), key=lambda n: (taken.count(n), n))
        holder["style"] = f"h{assigned[holder['key']] % count}"


def fill(style: str) -> str:
    """A holder's fill token (`f3`) for its ink token (`h3`): what paints blocks, cells, bars and the selection marker;
    any other style is its own fill."""
    token = "f" + style[1:]
    return token if style[:1] == "h" and style[1:].isdigit() and token in _tui.current_theme.styles else style


# Dashboard layout

def bar(fraction: float, width: int, style: str) -> list[tuple[str, str]]:
    """Compact squares leave vertical breathing room between adjacent bar rows."""
    width = max(0, width)
    full = max(0, min(width, math.floor(fraction * width + 0.5)))
    return [("■" * full, style), (_tui.current_theme.track * (width - full), "track")]


def ramp_bar(fraction: float, width: int, critical: bool = False) -> list[tuple[str, str]]:
    """A MACHINE resource bar: each filled cell in the load chart's indigo-to-red ramp at its place along the bar, so
    a nearly full bar reaches red at its tip; a critical one (🔥) entirely in that red, so full and critical match."""
    width = max(0, width)
    full = max(0, min(width, math.floor(fraction * width + 0.5)))
    parts: list[tuple[str, str]] = []
    for cell in range(full):
        step = round(cell / (width - 1) * (_theme.CHART_STEPS - 1)) if width > 1 else _theme.CHART_STEPS - 1
        style = f"chart{_theme.CHART_STEPS - 1}" if critical else f"chart{step}"  # all the ramp's red: full = critical
        if parts and parts[-1][1] == style:
            parts[-1] = (parts[-1][0] + "■", style)
        else:
            parts.append(("■", style))
    return parts + [(_tui.current_theme.track * (width - full), "track")]


def share(item: dict, model: dict) -> tuple[float, float]:
    """(cores the lease's commands use, cores its slots stand for)."""
    return item["cpu"] / 100, item["count"] * model["cores"] / model["slots"]


def share_style(item: dict, model: dict, moment: datetime.datetime) -> str:
    used, allotted = share(item, model)
    if item.get("idle_since") is not None:
        return "warn"  # an idle lease (#12; the holder's record, idle.idle_since()): holds slots doing nothing
    return "bad" if used > 1.5 * allotted else "accent"


def queue_bar_style(item: dict[str, Any], model: dict[str, Any], moment: datetime.datetime) -> str:
    """A QUEUE row's CPU bar fills in the holder's fill (its SLOTS colour), except when it warns: red over 1.5 times
    its share, yellow while the lease is idle (#12, share_style())."""
    style = share_style(item, model, moment)
    return fill(str(item["style"])) if style == "accent" else style




def draw_header(canvas: Canvas, model: dict | None, error: str | None) -> tuple[int, int]:
    """The name on the left, the last update and live or stale on the right; (first free column, last free one)."""
    y = canvas.height - 1
    x = canvas.put(y, 1, _version.NAME, "title", max(0, canvas.width - 2))
    status, style = ("● stale", "bad") if error else ("● live", "ok")
    room = canvas.width - 1 - x - 2
    right = canvas.width - 1
    if text_width(status) <= room:
        right = canvas.right(y, right, status, style)
        if model:
            stamp = clock(model["updated"])
            if text_width("updated " + stamp) + text_width(status) + 2 <= room:
                stamp = "updated " + stamp
            if text_width(stamp) + text_width(status) + 2 <= room:
                right = canvas.right(y, right - 2, stamp, "dim")
    return x + 3, right - 2


def draw_slots(canvas: Canvas, y: int, x: int, width: int, model: dict) -> None:
    """Solid slots spread across the widget, with two-cell gaps and right-aligned free label."""
    slots = model["slots"]
    area = max(0, width)
    gap = 2 if area >= slots + 2 * (slots - 1) else 1 if area >= 2 * slots - 1 else 0
    cell, remainder = divmod(max(0, area - gap * (slots - 1)), slots)
    widths = [cell + (index < remainder) for index in range(slots)]
    starts, cursor = [], x
    for size in widths:
        starts.append(cursor)
        cursor += size + gap
    labels, filled = [], 0
    for holder in model["holders"]:
        count = min(holder["count"], slots - filled)
        if count <= 0:
            break
        labels.append((starts[filled], holder["label"], holder["style"]))
        for index in range(filled, filled + count):
            canvas.put(y, starts[index], "🮅" * widths[index], fill(str(holder["style"])))
        filled += count
    for index in range(filled, slots):
        canvas.put(y, starts[index], free_slot(widths[index]), "free")
    label_end = x + area
    if filled < slots and area >= 4:
        free_start = x + area - 4
        canvas.put(y + 1, free_start, "free", "dim")
        label_end = free_start - 2
    for index, (label_x, label, style) in enumerate(labels):
        limit = labels[index + 1][0] - 2 if index + 1 < len(labels) else label_end
        canvas.put(y + 1, label_x, label, style, max(0, limit - label_x))


def free_slot(width: int) -> str:
    """A free slot `width` cells wide: a block in the theme's free colour, or outlined where the theme has no surface
    colours (#14)."""
    if _tui.current_theme.free == "block" or width < 1:
        return "🮅" * width
    return "▭" if width == 1 else "╭" + "─" * (width - 2) + "╮"


SLOT_CELLS = 8  # most cells the QUEUE table's slots column shows before it counts the rest
# How a slot cell is drawn: (glyph, gap between cells). Each cell must stay countable at a glance, never run into a
# bar: "spaced" puts a space between full squares, "small" uses a small square whose side bearing separates it.
SLOT_CELL_FORMS = {"spaced": ("■", " "), "small": ("▪", "")}
SLOT_CELL = "small"  # the owner's pick (2026-11-11)
HOLLOW = {"■": "□", "▪": "▫"}  # slots handed back or never held: the same size, empty (U+25AB is narrow like U+25AA)


def cells_text(count: int, hollow: bool = False) -> str:
    glyph, gap = SLOT_CELL_FORMS[SLOT_CELL]
    return gap.join((HOLLOW[glyph] if hollow else glyph) * count)


def cell_run(count: int, size: int, style: str, hollow: bool = False, whole: bool = False,
             text: str = "") -> list[tuple[str, str]]:
    """`count` countable cells in `style` within `size` columns and at most SLOT_CELLS cells. A count wider than
    that keeps the cells that fit
    and counts the rest (`▪▪▪▪ +16` for 20 in 8 columns); a `whole` (-n all) one that does not fit says `all`. The
    words are in `text` (a holder's ink), else in `style`."""
    text = text or style
    count = max(0, count)
    size = min(size, text_width(cells_text(SLOT_CELLS)))  # more than SLOT_CELLS cells cannot be counted at a glance
    if text_width(cells_text(count, hollow)) <= size:
        return [(cells_text(count, hollow), style)]
    if whole:
        return [("all", text)]
    shown = min(count, size)  # no more cells than the column has cells: at most `size` tries
    while shown and text_width(cells_text(shown, hollow)) + len(f" +{count - shown}") > size:
        shown -= 1
    return [(cells_text(shown, hollow), style), (f" +{count - shown}", text)]


def slot_cells(item: dict[str, Any], size: int) -> list[tuple[str, str]]:
    """A QUEUE row's slots as countable cells in the row's colour (the fill of its SLOTS blocks for a lease; the
    plain text colour for a waiting request, whose state the cpu column gives); see cell_run()."""
    style = "text" if item["kind"] == "waiting" else str(item["style"])
    return cell_run(int(item["count"]), size, fill(style), whole=bool(item.get("all")), text=style)


def event_cells(event: dict[str, Any], size: int, style: str = "text") -> list[tuple[str, str]]:
    """An EVENTS row's slots, in the text colour (owner, 2026-11-12): filled for slots taken (granted), hollow for
    slots handed back (released, a held lease reaped) or never held (a waiter that left or was reaped). The cells
    carry what happened; the event's name is not a column."""
    return cell_run(int(event["count"]), size, style, hollow=event["verb"] != "granted")


SPAN = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def span_seconds(text: str) -> float | None:
    """A span as span() writes it (45s, 3m25s, 1h05m, 2d03h) back in seconds; None for anything else."""
    found = SPAN.fullmatch(text.strip())
    if not text.strip() or found is None or not any(found.groups()):
        return None
    days, hours, mins, secs = (int(part or 0) for part in found.groups())
    return float(((days * 24 + hours) * 60 + mins) * 60 + secs)


def event_result(event: dict[str, Any]) -> tuple[str, str]:
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


QUEUE_LABEL_MAX = 40  # the most cells a label asks for when the table is wide enough to share out its width
CPU_BAR = 5  # the CPU bar's base length; a wide table lets it grow to twice that


class QueueLayout(NamedTuple):
    placed: list[tuple[str, str, int, int, bool]]  # (key, title, x, width, right-aligned), as columns() gives
    natural: dict[str, int]  # each column's own width: its widest value (a label at most QUEUE_LABEL_MAX)
    use_width: int  # the CPU numbers: `use` right-aligned to this, then `/`, then `share` left-aligned to share_width
    share_width: int
    bar: int  # the CPU bar's length


def queue_layout(inner: int, model: dict[str, Any]) -> QueueLayout:
    """The QUEUE table's columns in `inner` cells. Each column has a natural width (its widest value). A table wider
    than the natural widths and their gaps shares the surplus out in proportion to them, so it fills the panel
    evenly, and the CPU bar grows with its column up to twice CPU_BAR; a narrower one keeps the label flexible: it
    shrinks first, then pid, time and cpu use/share drop in that order."""
    holders = cast(list[dict[str, Any]], model["holders"])
    waiters = cast(list[dict[str, Any]], model["waiters"])
    items = holders + waiters
    shares = [share(item, model) for item in holders]
    use_width = max([3] + [len(f"{used:.1f}") for used, _ in shares])
    share_width = max([1] + [len(short(allotted)) for _, allotted in shares])
    natural = {
        "label": max(10, min(QUEUE_LABEL_MAX, max([0] + [text_width(str(item["label"])) for item in items]))),
        "slots": max([5] + [text_width(cells_text(min(SLOT_CELLS, int(item["count"])))) for item in items]),
        "cpu": max(len("cpu use/share"), CPU_BAR + 1 + use_width + 1 + share_width,
                   max([0] + [len(f"waiting #{n}") for n in range(1, len(waiters) + 1)])),
        "time": 8, "pid": max([6] + [len(str(item["pid"])) for item in items])}
    spec = [("label", "label", False), ("slots", "slots", False), ("cpu", "cpu use/share", False),
            ("time", "time", True), ("pid", "pid", True)]
    gaps = 2 * (len(spec) - 1)
    surplus = inner - sum(natural.values()) - gaps
    if surplus <= 0:  # the label shrinks first, then columns drop: pid, time, cpu use/share
        placed = cast(list[tuple[str, str, int, int, bool]], columns(inner, [
            (key, title, 0 if key == "label" else natural[key], right, rank)
            for rank, (key, title, right) in enumerate(spec)]))
        return QueueLayout(placed, natural, use_width, share_width, CPU_BAR)
    total = sum(natural.values())
    sizes = {key: natural[key] + surplus * natural[key] // total for key in natural}
    left = inner - gaps - sum(sizes.values())  # what rounding down left over: to the largest remainders
    for key in sorted(natural, key=lambda key: -(surplus * natural[key] % total))[:left]:
        sizes[key] += 1
    placed: list[tuple[str, str, int, int, bool]] = []
    column = 0
    for key, title, right in spec:
        placed.append((key, title, column, sizes[key], right))
        column += sizes[key] + 2
    bar_length = min(2 * CPU_BAR, CPU_BAR + sizes["cpu"] - natural["cpu"])
    return QueueLayout(placed, natural, use_width, share_width, bar_length)


# A QUEUE row's state (#15), the first column of every row: (emoji, ASCII). A running row shows the first of asked,
# idle, hot and legacy that applies, else running; a waiting row yielded or waiting.
MARKS = {"asked": ("❓", "?"), "idle": ("💤", "z"), "hot": ("🔥", "!"), "legacy": ("👨‍🦳", "o"), "running": ("▶️", ">"),
         "yielded": ("↩️", "<"), "waiting": ("⏸️", ".")}
OLD_FALLBACK = "👴"  # for a terminal that draws 👨‍🦳 as two glyphs (theme.ask() measures it)
marks = {key: emoji for key, (emoji, _) in MARKS.items()}  # what this terminal is drawn with
# A request's priority (owner, 2026-11-11): its own column after the state; normal has none, so the column is blank
PRIORITY_MARKS = {"interactive": ("⚡", "!"), "batch": ("🐢", "~")}
priority_marks = {key: emoji for key, (emoji, _) in PRIORITY_MARKS.items()}
# A MACHINE resource's health (owner, 2026-11-12): ok, or critical by the rules that turn its bar red
HEALTH_MARKS = {"ok": ("🆗", "+"), "hot": ("🔥", "!")}
health_marks = {key: emoji for key, (emoji, _) in HEALTH_MARKS.items()}


def use_marks(ascii_only: bool, joined: bool = True) -> None:
    """The QUEUE state column for this terminal: ASCII where it has no UTF-8, 👴 for 👨‍🦳 where it does not join."""
    marks.update({key: plain if ascii_only else emoji for key, (emoji, plain) in MARKS.items()})
    priority_marks.update({key: plain if ascii_only else emoji for key, (emoji, plain) in PRIORITY_MARKS.items()})
    health_marks.update({key: plain if ascii_only else emoji for key, (emoji, plain) in HEALTH_MARKS.items()})
    if not ascii_only and not joined:
        marks["legacy"] = OLD_FALLBACK


PRIORITIES = ("interactive", "normal", "batch")  # Slotfather's request classes
META_VALUE_MAX = 200  # the longest ticket Slotfather accepts


def priority_of(entry: dict[str, Any]) -> str:
    """A request's class as Slotfather reads it: normal for anything it does not know."""
    value = entry.get("priority")
    return value if isinstance(value, str) and value in PRIORITIES else "normal"


def ticket_of(entry: dict[str, Any]) -> str | None:
    """A ticket as Slotfather reads one: short printable text, `#12` as `12`, empty or "none" as no ticket."""
    value = entry.get("ticket")
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or len(value) > META_VALUE_MAX or not value.isprintable():
        return None
    value = value.strip()
    if not value or value.lower() == "none":
        return None
    return value[1:] if value.startswith("#") and value[1:].isdigit() else value


def meta_text(meta: dict[str, str]) -> str:
    """Metadata for one line, as Slotfather writes it: key=value pairs joined by commas, a value quoted where it
    holds a space, comma, quote or equals sign."""
    plain = re.compile(r'[^\s,="]+')
    return ",".join(f"{k}={v if plain.fullmatch(v) else json.dumps(v)}" for k, v in meta.items())


def priority_mark(item: dict[str, Any]) -> str:
    """⚡ for an interactive request, 🐢 for a batch one (! and ~ in ASCII), nothing for normal: the class it is
    served in, aged (`served`), else the one it asked."""
    return priority_marks.get(priority_of({"priority": item.get("served") or item.get("priority")}), "")


def row_state(item: dict[str, Any], model: dict[str, Any]) -> str:
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




def draw_requests(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict, selected: int,
               moment: datetime.datetime) -> None:
    holders = cast(list[dict[str, Any]], model["holders"])
    waiters = cast(list[dict[str, Any]], model["waiters"])
    items = holders + waiters
    row_items = list(range(len(holders)))
    if holders and waiters:
        row_items.append(None)  # visual separator, never a selectable request
    row_items.extend(range(len(holders), len(items)))
    # cpu use/share: the bar starts at the column's left edge on every row, then `use` right-aligned and `/share`
    # left-aligned around the slash, padded to the widest on screen; a waiting row's state starts where the bars do.
    layout = queue_layout(width - 12, cast(dict[str, Any], model))  # the state and priority: two cells and a gap each
    placed, cells = layout.placed, layout.natural["slots"]
    shares = {index: share(item, model) for index, item in enumerate(holders)}
    use_width, share_width = layout.use_width, layout.share_width
    size = next((column[3] for column in placed if column[0] == "slots"), cells)
    table_header(canvas, y, x, height, width, "QUEUE", placed, gutter=6, name="requests")
    if not items:
        canvas.put(y + 2, x + 3, f"No requests · all {model['slots']} slots free", "dim", width - 6)
    visual_selected = row_items.index(selected) if selected in row_items else None
    offset, rows = table_view(canvas, "requests", y, x, height, width, y + 2, height - 4,
                              len(row_items), visual_selected, row_items=row_items)
    for visual, index in enumerate(row_items[offset:offset + rows], offset):
        if index is None:
            continue
        item = items[index]
        row = y + 2 + visual - offset
        waiting = index >= len(holders)
        # One colour per row: a running lease's SLOTS colour (its CPU bar too, but for a warning), the text colour for a
        # request that holds no slots.
        style = "text" if waiting else str(item["style"])
        # A selected running row keeps its colour and gets its own tint behind it (#15)
        tinted = index == selected and style in _tui.current_theme.tints
        ink = style
        if waiting:
            place = index - len(holders) + 1
            state = f"yielded #{place}" if item.get("yields") else f"waiting #{place}"  # back in the queue to resume
            age = (moment - item["since"]).total_seconds() if item["since"] else None
            cpu: list[tuple[str, str]] = [(state, style)]
        else:
            age = (moment - item["granted"]).total_seconds() if item["granted"] else None
            used, allotted = shares[index]
            cpu = bar(used / allotted if allotted else 0, layout.bar, queue_bar_style(item, cast(dict[str, Any], model), moment)) + [
                (f" {used:.1f}".rjust(use_width + 1) + "/" + short(allotted).ljust(share_width), ink)]
        # The state is the row's first content, where every panel's starts (x + 3); the label after it (#15)
        canvas.put(row, x + 3, marks[row_state(item, cast(dict[str, Any], model))], style, 2)
        canvas.put(row, x + 6, priority_mark(item), style, 2)  # blank for normal: the labels stay in one column
        draw_row(canvas, row, x + 9, placed, {
            "label": (str(item["label"]), ink), "slots": slot_cells(item, size),
            "time": (span(age) if age is not None else "—", ink), "cpu": cpu, "pid": (str(item["pid"]), ink)})
        if index == selected:  # its background, and the left frame heavy in the row's colour: no cursor glyph
            canvas.select(row, x + 1, x + width - 1, style if tinted else True)
            for frame in (x, x + width - 1):  # both frames, at that row; the thumb is in the padding cell (#15)
                canvas.put(row, frame, SELECTED_FRAME, fill(style))


def queue_policy(model: dict) -> dict[str, str]:
    """Admission estimates and deadlines for the head, exposed as table fields."""
    if not model["waiters"]:
        return {}
    head = model["waiters"][0]
    if head["count"] <= model["free"]:
        return {}
    fields = {}
    if model["free"] and head.get("passable") and head.get("since"):
        fields["pass until"] = clock(head["since"] + datetime.timedelta(seconds=float(model.get("overtake_window") or 0)))
    if model.get("order") == "backfill" and model.get("start") is not None and head.get("since"):
        fields["start est"] = clock(model["start"])
        promised: Any = cast(Any, model).get("reserved")  # the start it is promised: nothing that passes it ends later
        if promised is not None:
            fields["reserved"] = clock(promised)
        if model.get("limit"):
            fields["pass until"] = clock(head["since"] + datetime.timedelta(seconds=model["limit"]))
    return fields



def detail_ticket(item: dict[str, Any], lane: dict[str, Any], context: dict[str, str]) -> tuple[str, str]:
    source: dict[str, Any] = {}
    if item.get("ticket") is not None:
        source["ticket"] = item["ticket"]
    ticket: dict[str, Any] = lane.get("ticket") or {}
    value = ticket_of(source) or ticket_of({"ticket": ticket.get("number")})
    if not value:
        return "", ""
    url = value if value.startswith("https://") and safe_link(value) else ""
    repository = str(lane.get("repository") or "")
    if value.isdigit() and repository.startswith("https://") and safe_link(repository):
        url = repository.rstrip("/") + "/issues/" + value
    elif value == str(ticket.get("number")):
        candidate = str(ticket.get("url") or "")
        if candidate.startswith("https://") and safe_link(candidate):
            url = candidate
    return ("#" + value if value.isdigit() else value), url


def detail_title(item: dict[str, Any] | None) -> str:
    return "LEASE DETAILS" if item is not None and item["kind"] != "waiting" else "REQUEST DETAILS"


def draw_detail(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict, selected: int,
                cwd: str | None, moment: datetime.datetime) -> None:
    items = cast(list[dict[str, Any]], model["holders"] + model["waiters"])
    # The title follows the selection: a request whose lease holds slots has LEASE DETAILS; a waiting or yielded
    # one, holding none, REQUEST DETAILS.
    canvas.box(y, x, height, width, detail_title(items[selected] if items else None), name="details")
    inner = width - 6
    if not items or canvas.loading is not None:  # drawn, so it can hold the focus, with nothing to scroll yet
        table_view(canvas, "details", y, x, height, width, y + 2, max(0, height - 4), 0)
    if not items:
        canvas.put(y + 2, x + 3, "Nothing to select · the queue is idle", "dim", inner)
        return
    if canvas.loading is not None:  # loading in the background (#15): a spinner, centred, until they arrive
        middle = y + max(1, (height - 2) // 2)
        glyph = SPINNER[canvas.loading % len(SPINNER)]
        canvas.put(middle, x + (width - 1) // 2, glyph, "dim")
        if middle + 1 < y + height - 1:
            canvas.put(middle + 1, x + (width - len("loading")) // 2, "loading", "dim")
        return
    item = items[selected]
    lane: dict[str, Any] = item.get("lane") or {}
    context: dict[str, str] = item.get("context") or {}
    ticket_text, ticket_url = detail_ticket(item, lane, context)
    if item["kind"] == "waiting":
        position = selected - len(model["holders"])
        heading = f"waiting · #{position + 1} · wants {item['count']}"
    else:
        heading = ("earlier queue" if item["kind"] == "legacy" else "running") + f" · {item['count']} slot{'s' if item['count'] != 1 else ''}"
    facts: list[tuple[str, list[tuple[str, str]]]] = [("label", [(item["label"] or "(no label)", "bold")]), ("status", [(heading, "text")])]
    priority = priority_of({"priority": item.get("served") or item.get("priority")})
    mark = priority_mark(item)
    asked = priority_of({"priority": item.get("priority")})
    how = f" · aged from {asked}" if priority != asked else ""
    how += f" · reprioritized from {item['reprioritized']}" if item.get("reprioritized") else ""
    facts.append(("priority", ([(mark, "text"), (" ", "text")] if mark else []) + [(priority, "text"),
                                                                                 (how, "dim")]))
    if ticket_text:
        facts.append(("ticket", [(ticket_text, "accent" if ticket_url else "text")]))
    for key, label in (("phase", "phase"), ("kind", "kind"), ("check", "check"), ("sha", "revision"),
                       ("on", "run on"), ("session", "session")):
        if context.get(key):
            facts.append((label, [(dashboard_text(context[key]), "dim" if key in ("sha", "session") else "text")]))
    if item["kind"] == "waiting":
        facts.append(("queued", [(f"{clock(item['since'])} · waiting "
                                  f"{span((moment - item['since']).total_seconds()) if item['since'] else '?'}", "text")]))
        if position == 0:
            facts.append(("free", [(f"{model['free']} of {model['slots']} slots", "text")]))
            facts.extend((name, [(value, "text")]) for name, value in queue_policy(model).items())
            if model["used"] and model["memory_block"]:
                facts.append(("blocked", [(model["memory_block"], "warn")]))
        else:
            facts.append(("ahead", [(f"{position} requests · {sum(w['count'] for w in model['waiters'][:position])} slots", "text")]))
            if item.get("est_start") is not None:  # schedule.forecast(): if nothing more comes
                facts.append(("start est", [(clock(item["est_start"]), "text")]))
    else:
        used, allotted = share(item, model)
        style = share_style(item, model, moment)
        note = " · above its share" if style == "bad" else ""
        facts.append(("cpu", bar(used / allotted if allotted else 0, 10, style) +
                      [(f" {used:.1f} of {short(allotted)} cores{note}", "text")]))
        if item["granted"]:
            waited = f" · waited {span((item['granted'] - item['since']).total_seconds())}" if item["since"] else ""
            facts.append(("granted", [(f"{clock(item['granted'])}{waited} · running "
                                       f"{span((moment - item['granted']).total_seconds())}", "text")]))
        facts.append(("memory", [(f"{megabytes(item['rss'] / 1024)} in {item['procs']} processes", "text")]))
    if item["kind"] != "waiting" and item.get("idle_since") is not None:  # #12: one definition (idle.idle_since())
        since = item["idle_since"]
        facts.append(("idle", [(f"{minutes((moment - since).total_seconds())} since "
                                f"{since.astimezone().strftime('%H:%M')}", "warn")]))
    if item.get("asked"):
        at = item.get("asked_at")
        facts.append(("yield", [(f"asked at {at.astimezone().strftime('%H:%M')} for {item['asked']}"
                                 if at else f"asked to yield for {item['asked']}", "warn")]))
    elif item.get("yieldable") or item.get("yields"):
        facts.append(("yield", [("yields between steps when asked" + (f" · yielded {item['yields']}×"
                                                                       if item.get("yields") else ""), "dim")]))
    facts.append(("owner pid", [(str(item["pid"]), "text")]))
    where = tilde(item["queue"] or cwd or "")
    label_width = max(text_width(name) for name in [*(name for name, _ in facts), "command", "meta", "cwd"]) + 2
    value_width = max(1, inner - label_width)
    if text_width(where) > value_width:
        where = "…" + where[-max(0, value_width - 1):]
    worktree_rows: set[int] = set()
    worktree_url = ""
    if lane.get("worktree"):
        folder = Path(str(lane["worktree"]))
        if folder.is_absolute():
            worktree_url = folder.as_uri()
        for index, line in enumerate(textwrap.wrap(tilde(dashboard_text(lane["worktree"])), value_width,
                                                   break_on_hyphens=False)):
            worktree_rows.add(len(facts))
            facts.append(("worktree" if index == 0 else "", [(line, "dim")]))
    blocks = [("command", item["cmd"] or "—")]
    metadata: str = item.get("meta", "")
    if context:
        builtin = {"class", "priority", "ticket", "lane", "phase", "kind", "check", "sha", "on", "session", "lanefather.state"}
        metadata = dashboard_text(meta_text({key: value for key, value in context.items() if key not in builtin}))
    if metadata:
        blocks.append(("meta", metadata))
    wrapped = [textwrap.wrap(text, value_width, break_on_hyphens=False) or ["—"] for _, text in blocks]
    for (name, _), lines in zip(blocks, wrapped):
        for index, line in enumerate(lines[:3]):
            if index == 2 and len(lines) > 3:
                line = clip(line + " …", value_width)
            facts.append((name if index == 0 else "", [(line, "dim")]))
    if where:
        facts.append(("cwd", [(where, "dim")]))
    if context.get("lane"):
        facts.extend([("", []), ("lane", [(dashboard_text(context["lane"]), "bold")])])
        if lane.get("error") or not lane:
            facts.append(("details", [("unavailable" + (f" · {lane['error']}" if lane.get("error") else ""), "dim")]))
        else:
            for name in ("project", "status", "branch", "base"):
                text = dashboard_text(lane.get(name) or "—")
                for index, line in enumerate(textwrap.wrap(text, value_width, break_on_hyphens=False)):
                    label = "stage" if name == "status" else name
                    facts.append((label if index == 0 else "", [(line, "text")]))
            checks: dict[str, Any] = lane.get("checks") or {}
            check_text = str(checks.get("status") or "unknown")
            if checks.get("current") is not None:
                check_text += " · current" if checks["current"] else " · stale"
            facts.append(("receipt", [(dashboard_text(check_text), "text")]))
            review: dict[str, Any] = lane.get("review") or {}
            review_text = str(review.get("status") or "unknown")
            if review.get("findings"):
                review_text += f" · {review.get('unresolved', '?')} unresolved of {review['findings']}"
            facts.append(("review", [(dashboard_text(review_text), "text")]))
            for name in ("claims", "after"):
                text = ", ".join(dashboard_text(value) for value in lane.get(name, [])) or "—"
                for index, line in enumerate(textwrap.wrap(text, value_width)):
                    facts.append((name if index == 0 else "", [(line, "dim")]))
    content_height = len(facts) + (3 + len(item["tree"]) if item["tree"] else 0)
    document = Canvas(width, content_height)
    for row, (name, parts) in enumerate(facts):
        document.put(row, 3, name, "dim")
        if row in worktree_rows and worktree_url:
            document.hyperlink(row, 3 + label_width, str(parts[0][0]), worktree_url, value_width, "dim")
        elif name == "ticket" and ticket_url:
            document.hyperlink(row, 3 + label_width, str(parts[0][0]), ticket_url, value_width)
        else:
            document.segments(row, 3 + label_width, parts, value_width)
    if item["tree"]:
        row = len(facts)
        placed = columns(inner, [("name", "process", 0, False, 0), ("cpu", "cpu", 6, True, 1),
                                 ("rss", "memory", 8, True, 2), ("pid", "pid", 6, True, 0)])
        draw_row(document, row + 1, 3, placed, {key: (title, "dim") for key, title, *_ in placed})
        for process_row, (pid, cpu, rss, name) in enumerate(item["tree"], row + 3):
            draw_row(document, process_row, 3, placed, {"pid": str(pid), "cpu": f"{cpu:.0f}%",
                     "rss": megabytes(rss / 1024), "name": name})
    offset, visible = table_view(canvas, "details", y, x, height, width, y + 2,
                                 max(0, height - 4), content_height)
    for row in range(min(visible, content_height - offset)):
        for column in range(3, max(3, width - 3)):
            target_y, target_x = y + 2 + row, x + column
            if 0 <= target_y < canvas.height and 0 <= target_x < canvas.width:
                canvas.cells[target_y][target_x] = document.cells[offset + row][column][:]
                url = document.links.get((offset + row, column))
                canvas.links.pop((target_y, target_x), None)
                if url:
                    canvas.links[target_y, target_x] = url


def enrich_details(item: dict[str, Any]) -> None:
    context: dict[str, str] = item.get("context") or {}
    if context.get("lane"):
        item["lane"] = _lanefather.fetch(context, item.get("workdir") or "")


def machine_memory_status(memory: dict) -> tuple[str, str]:
    """RAM/pressure health only; swap has a separate status and never colours RAM by itself. Slotfather judges it
    (`memory.low` in its snapshot: the pressure level on macOS, MemAvailable and PSI on Linux); null is unknown."""
    low = memory.get("low")
    if low is None:
        return "memory unknown", "dim"
    return ("memory low", "bad") if low else ("memory ok", "status-normal")


def machine_note(model: dict[str, Any]) -> list[tuple[str, str]]:
    """MACHINE's top border, on the right (owner, 2026-11-12): the load outside leases and whether the queue admits
    work, the admission red when it blocks."""
    admission = model.get("admission")
    permit = (("admission: ?", "dim") if admission is None else ("no admission", "bad") if not admission
              else (f"admission: {admission} slots", "status-normal"))
    return [(f"unleased {model['advice']['unleased_load']:.1f}", "status-normal"), (" · ", "dim"), permit]


class Resource(NamedTuple):
    """A MACHINE row: its short label, how full (None: unknown), its used/total value in full and short, and whether
    it is critical by the rules that turn its bar red."""
    label: str
    fraction: float | None
    value: str
    short: str
    critical: bool


def machine_resources(model: dict[str, Any]) -> list[Resource]:
    """load, mem and swap as used/total (owner, 2026-11-12): `10.1/12`, `23.0/32G`, `10.0/11G`; short `23/32G`."""
    memory = cast(dict[str, Any], model["memory"])
    cores, slots = int(model["cores"]), int(model["slots"])
    load1 = float(model["averages"][0])
    # Critical where the one-minute load leaves less than one slot's share of the cores free (#15 review): by load
    # alone, so a fully leased but quiet machine is not red
    load_value = f"{load1:.1f}/{cores}"
    found = [Resource("load", load1 / cores, load_value, load_value, cores - load1 < cores / slots)]
    used, total = memory.get("used"), memory.get("total")
    if used is None and total and memory.get("available") is not None:
        used = max(0, total - memory["available"])
    low = machine_memory_status(memory)[1] == "bad"
    if total and used is not None:
        found.append(Resource("mem", used / total, f"{gigabytes(used)}/{gigabytes(total, True)}G",
                              f"{round(used / 1024)}/{round(total / 1024)}G", low))
    else:
        found.append(Resource("mem", None, "?", "?", low))
    swap_total = float(memory.get("swap_total") or 0)
    if swap_total and memory.get("swap_free") is not None:
        swap_used = max(0.0, swap_total - float(memory["swap_free"]))
        swap_low = bool(memory.get("swap_low"))  # free swap under Slotfather's floor, as it judges it
        found.append(Resource("swap", swap_used / swap_total, f"{gigabytes(swap_used)}/{gigabytes(swap_total, True)}G",
                              f"{round(swap_used / 1024)}/{round(swap_total / 1024)}G", swap_low))
    else:
        found.append(Resource("swap", None, "off" if memory.get("swap_total") == 0 else "?",
                              "off" if memory.get("swap_total") == 0 else "?", False))
    return found


def machine_side_by_side(width: int) -> bool:
    """Whether MACHINE is wide enough for its chart beside the three rows rather than under them (owner)."""
    return width >= MACHINE_SIDE_WIDTH


def machine_height(width: int) -> int:
    """MACHINE's natural height at this width: shorter with the chart beside the rows."""
    return MACHINE_SIDE_HEIGHT if machine_side_by_side(width) else MACHINE_HEIGHT


def draw_resource_rows(canvas: Canvas, row: int, x: int, width: int, resources: list[Resource]) -> None:
    """The three rows in `width` cells from x: health mark, label, a bar, and the value right-aligned in one column
    as wide as the longest, so every bar has the same length; short values (`23/32G`) where full ones leave no bar."""
    values = [resource.value for resource in resources]
    column = max(text_width(value) for value in values)
    bar_width = width - MACHINE_LABEL - 1 - column
    if bar_width < MACHINE_BAR_MIN:
        values = [resource.short for resource in resources]
        column = max(text_width(value) for value in values)
        bar_width = width - MACHINE_LABEL - 1 - column
    bar_width = max(0, bar_width)
    for line, (resource, value) in enumerate(zip(resources, values)):
        canvas.put(row + line, x, health_marks["hot" if resource.critical else "ok"], "text", 2)
        canvas.put(row + line, x + 3, resource.label, "dim")
        if bar_width and resource.fraction is not None:
            canvas.segments(row + line, x + MACHINE_LABEL, ramp_bar(resource.fraction, bar_width, resource.critical),
                            bar_width)
        canvas.put(row + line, x + width - text_width(value), value, "text")


def draw_machine(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict) -> None:
    canvas.box(y, x, height, width, "MACHINE")
    note = machine_note(cast(dict[str, Any], model))
    for parts in (note, note[2:]):  # all of it, else the admission alone, else nothing
        size = sum(text_width(text) for text, _ in parts) + 2
        if x + 2 + text_width(" MACHINE ") + 2 + size <= x + width - 2:
            canvas.segments(y, x + width - 2 - size, [(" ", "text"), *parts, (" ", "text")], size)
            break
    inner, slots = width - 6, int(cast(int, model["slots"]))
    resources = machine_resources(cast(dict[str, Any], model))
    rows = len(resources)
    interior = height - 2
    # Wide (owner, 2026-11-12): the chart beside the rows, its rows level with theirs, its axis under it alone
    if machine_side_by_side(width) and interior >= 1 + rows + 1:
        metrics = MACHINE_LABEL + MACHINE_BAR_SIDE + 1 + max(text_width(resource.value) for resource in resources)
        chart_x, chart_width = x + 3 + metrics + MACHINE_GAP, inner - metrics - MACHINE_GAP
        chart_rows = min(CHART_ROWS, interior - 2)
        draw_resource_rows(canvas, y + 2, x + 3, metrics, resources)
        draw_chart(canvas, y + 2, chart_x, chart_width, chart_rows, cast(dict[str, Any], model), slots)
        return
    # Stacked: a blank row, the three rows, the chart and its axis line, which the bottom frame follows directly.
    # Short of rows (owner, 2026-11-12) the chart gives up height first, 4 → 3 → 2 rows and then entirely (never one
    # row); then the blank row; then the rows become one compact line; memory and swap never go.
    full = interior >= rows
    padded = interior >= 1 + rows
    chart_rows = min(CHART_ROWS, interior - 1 - rows - 1) if padded else 0
    chart_rows = chart_rows if chart_rows >= MIN_CHART_ROWS else 0
    row = y + 2 if padded else y + 1
    if full:
        draw_resource_rows(canvas, row, x + 3, inner, resources)
        row += rows
    elif interior >= 1:
        canvas.segments(row, x + 3, compact_resources(
            [(resource.label, resource.fraction, resource.value, resource.critical) for resource in resources],
            inner, [resource.short for resource in resources]), inner)
        row += 1
    if chart_rows:
        draw_chart(canvas, row, x + 3, inner, chart_rows, cast(dict[str, Any], model), slots)


def draw_chart(canvas: Canvas, row: int, x: int, width: int, rows: int, model: dict[str, Any], slots: int) -> None:
    """The history chart, `rows` high from `row`, with half a cell of air at each end (owner, 2026-11-12), and its
    axis line right under it."""
    chart_width = max(1, width)
    history = cast(list[tuple[datetime.datetime, datetime.datetime, int]], model["history"])
    buckets = history_buckets(history, chart_width)
    for line, column_glyphs in enumerate(chart_cells(buckets, slots, rows)):
        colour = round((rows - 1 - line) / (rows - 1) * (_theme.CHART_STEPS - 1))
        for column, glyph in enumerate(column_glyphs):
            canvas.put(row + line, x + column, glyph, f"chart{colour}" if glyph != " " else "dim")
    summary = history_summary(history, slots)
    canvas.segments(row + rows, x, machine_legend(cast(dict[str, Any], summary), slots, chart_width), width)


def chart_cells(buckets: list[int], slots: int, rows: int, half_gaps: bool = True) -> list[list[str]]:
    """The history chart's glyphs, top row first, one per bucket. With `half_gaps` (owner, 2026-11-12) the span
    runs from the bottom cell's mid-line to the top cell's: a column with any height shows `▀` in the bottom row,
    the rows above fill from their own bottoms in exact eighths, and a full column tops out at `▄` in the top row,
    so half a cell of air sits above the chart and half over its axis. Without them, the full rows."""
    span = 8 * (rows - 1) if half_gaps else 8 * rows
    base = 4 if half_gaps else 0  # eighths below the span's zero: the bottom cell's lower half
    grid: list[list[str]] = [[] for _ in range(rows)]
    for level in buckets:
        height = base + math.ceil(min(1.0, level / slots) * span) if level > 0 and slots else 0
        for line in range(rows):
            above = rows - 1 - line  # rows below this one
            if half_gaps and above == 0:
                glyph = "▀" if height else " "
            else:
                fill = max(0, min(8, height - 8 * above))
                glyph = SPARK[fill - 1] if fill else " "
            grid[line].append(glyph)
    return grid


def gigabytes(value_mb: float | None, total: bool = False) -> str:
    """Megabytes as a short GB number for the compact resource line: 24.1, or a total as 32 or 0.5."""
    if value_mb is None:
        return "?"
    value = value_mb / 1024
    return (f"{value:.1f}".rstrip("0").rstrip(".") or "0") if total else f"{value:.1f}"


def compact_resources(resources: list[tuple[str, float | None, str, bool]], width: int,
                      shorts: list[str] | None = None) -> list[tuple[str, str]]:
    """Load, memory and swap on one line (owner, 2026-11-12) when MACHINE is too short for their rows: short ramp
    bars (6 to 4 cells, red when critical), else the numbers alone, a critical one in red; at the narrowest the
    short values (`23/32G`), then no marks. Never drops one."""
    parts: list[tuple[str, str]] = []
    forms = [(6, " · ", True, False), (5, " · ", True, False), (4, " · ", True, False), (0, " · ", True, False),
             (0, "  ", True, False), (0, " ", True, False), (0, " ", True, True), (0, " ", False, False),
             (0, " ", False, True)]
    for bar_width, separator, marked, shortened in forms:
        parts = []
        for index, (label, fraction, value, critical) in enumerate(resources):
            if shortened and shorts:
                value = shorts[index]
            if index:
                parts.append((separator, "dim"))
            if marked:
                parts.append((health_marks["hot" if critical else "ok"] + " ", "text"))
            parts.append((label + " ", "dim"))
            if bar_width and fraction is not None:
                parts += ramp_bar(fraction, bar_width, critical) + [(" ", "text")]
            parts.append((value, "bad" if critical and not bar_width else "text"))
        if sum(text_width(text) for text, _ in parts) <= width:
            break
    return parts


def window_text(seconds: float) -> str:
    """How far back a chart reaches, in a friendly unit: −45s, −90s, −15m, −60m, −2h."""
    seconds = max(1, round(seconds))
    if seconds < 120:
        return f"−{seconds if seconds < 30 else 5 * round(seconds / 5)}s"
    minutes = seconds / 60
    if minutes < 120:
        return f"−{round(minutes) if minutes < 10 else 5 * round(minutes / 5)}m"
    return f"−{round(minutes / 60)}h"


def machine_legend(summary: dict[str, Any], slots: int, chart_width: int) -> list[tuple[str, str]]:
    """The history chart's legend, a time axis under it: the window it shows on the left, under its first column,
    `now` on the right under its last, and between them, centred, the mean and most held slots and the share of
    the time every slot was held. A narrow chart drops full, then peak, then avg; the axis stays."""
    left, right = window_text(chart_width * max(1, math.ceil(HISTORY_WINDOW / chart_width))), "now"
    stats = [[("avg", "dim"), (f" {summary['average'] * slots:.1f}/{slots}", "text")],
             [("peak", "dim"), (f" {summary['peak']}", "text")],
             [("full", "dim"), (f" {summary['full']:.0%}", "text")]]
    room = chart_width - text_width(left) - text_width(right) - 4  # two cells each side of the stats
    parts: list[tuple[str, str]] = []
    while stats:
        parts = [part for index, stat in enumerate(stats) for part in ([(" · ", "dim")] if index else []) + stat]
        if sum(text_width(text) for text, _ in parts) <= room:
            break
        stats.pop()
        parts = []
    middle = sum(text_width(text) for text, _ in parts)
    gap = max(0, chart_width - text_width(left) - text_width(right) - middle)
    before = gap // 2 if parts else gap
    return ([(left, "dim"), (" " * before, "text")] + parts
            + [(" " * (gap - before), "text"), (right, "dim")])


RESULT_MIN = 6  # the result column's least width ("result"); it grows to its longest value


def draw_events(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict) -> None:
    """EVENTS (owner, 2026-11-12): time · slots · label · result. The slot cells say what happened (filled: taken,
    hollow: handed back), the label takes every cell the other columns leave, and the result, right-aligned, says
    how it went. The pid is in an event's details from slice 3, where its rows become selectable."""
    events = cast(list[dict[str, Any]], model["events"])[::-1]
    cells = max([5] + [text_width(cells_text(min(SLOT_CELLS, int(event["count"])))) for event in events])
    results = [event_result(event) for event in events]
    result = max([RESULT_MIN] + [text_width(text) for text, _ in results])
    placed = cast(list[tuple[str, str, int, int, bool]], columns(width - 6, [
        ("time", "time", 10, False, 0), ("slots", "slots", cells, False, 1), ("label", "label", 0, False, 0),
        ("result", "result", result, True, 2)]))
    size = next((column[3] for column in placed if column[0] == "slots"), cells)
    table_header(canvas, y, x, height, width, "EVENTS", placed, name="events")
    offset, visible = table_view(canvas, "events", y, x, height, width, y + 2, height - 4, len(events))
    if not events:
        canvas.put(y + 2, x + 3, "No grants, releases or reaps logged yet", "dim", width - 6)
    for row, (event, outcome) in enumerate(list(zip(events, results))[offset:offset + visible], y + 2):
        draw_row(canvas, row, x + 3, placed, {
            "time": (clock(event["when"]), "dim"), "label": str(event["label"] or "?"),
            "slots": event_cells(event, size), "result": outcome})


# The help (#15): sections of term → description rows and short notes. A row is (term, description); a note is
# ("", text) in the muted token, or ("", text, "text") in the text colour.
HELP: list[tuple[str, list[tuple[str, ...]]]] = [
    ("KEYS", [
        ("↑ ↓", "select a row · scroll a panel"),
        ("pgup pgdn", "a page"),
        ("home end", "first · last row, top · bottom"),
        ("tab", "next panel, by columns (shift-tab: back)"),
        ("click", "a row, a panel, a footer hint (as its key)"),
        ("wheel", "scroll the panel under the pointer"),
        ("s", "settings"),
        ("?", "help"),
        ("v", "changelog (or click the version)"),
        ("esc", "close an overlay"),
        ("q", "quit"),
        ("shift+drag", "select text"),
        ("", "Hotkeys work on any keyboard layout."),
    ]),
    ("STATES", [
        ("▶️", "running"),
        ("⏸️", "waiting"),
        ("↩️", "yielded, back in the queue"),
        ("❓", "asked to yield its slots"),
        ("💤", "idle: under 0.1 core for 5 min"),
        ("🔥", "above 1.5× its CPU share"),
        ("👨‍🦳", "lease of an older client"),
    ]),
    ("PRIORITY", [
        ("⚡", "interactive: someone is waiting on it"),
        ("🐢", "batch: unattended work, after the rest"),
        ("", "Normal has no mark."),
    ]),
    ("SLOTS", [
        ("", "One block per slot; the colour is its owner.", "text"),
        ("", "Slots budget scheduling, not CPU or memory."),
        ("manual · auto", "saved or computed capacity"),
        ("draining N", "held above a lowered capacity"),
    ]),
    ("QUEUE", [
        ("slots", "cells held or asked for"),
        ("cpu", "cores used / cores its slots stand for (a soft limit)"),
        ("time", "running or waiting for"),
        ("pid", "lease owner"),
        ("", "The bar turns red above 1.5× its share and yellow when idle."),
        ("", "An idle lease that can yield is asked to when a request waits for its slots. Nothing is ever killed."),
    ]),
    ("DETAILS", [
        ("start est", "when the queue head should start"),
        ("pass until", "later work may overtake until then"),
        ("receipt", "the lane's check: green or red, current or stale"),
        ("review", "review findings still open"),
        ("", "Estimates, not promises."),
    ]),
    ("EVENTS", [
        ("▪", "slots taken: granted"),
        ("▫", "slots handed back: released, reaped, or a waiter that left"),
        ("wait · no wait", "how long it waited for them"),
        ("ok · exit N", "how a lease ended, and how long it ran"),
        ("gave up", "a waiter stopped waiting, after so long"),
        ("owner died", "its process exited; Slotfather reaped the entry"),
    ]),
    ("MACHINE", [
        ("load", "the 1-minute load average / cores (not CPU %)"),
        ("mem · swap", "used / total, in GB"),
        ("🆗 · 🔥", "fine · critical"),
        ("unleased", "demand outside leased work"),
        ("admission", "slots a new request could get now"),
        ("", "Bars run indigo→red, and turn all red when the resource is critical."),
        ("", "The chart shows held slots over time, now on the right."),
    ]),
    ("SETTINGS", [
        ("← →", "change the value"),
        ("enter", "edit · save"),
        ("d · D D", "reset one · reset all"),
        ("esc", "close"),
        ("", "A lower capacity drains: running work keeps its slots."),
        ("", "Colours follow the terminal theme; Colour theme picks Auto, Terminal or Built-in."),
    ]),
]


def help_lines(width: int) -> list[list[tuple[str, str]]]:
    """The help laid out at `width` cells: a section title, then its rows with the terms in one column as wide as the
    section's longest term and each description wrapped under itself (a hanging indent), then its notes. Two blank
    lines before a section title but the first, one after it (owner). A row too narrow for both columns puts its
    description under its term."""
    lines: list[list[tuple[str, str]]] = []
    for title, entries in HELP:
        add_heading(lines, [(title, "title")])
        terms = max([text_width(entry[0]) for entry in entries if entry[0]] or [0])
        column = HELP_INDENT + terms + HELP_GAP
        for entry in entries:
            term, text = entry[0], entry[1]
            pad = " " * HELP_INDENT
            if not term:
                lines.extend([[(pad + line, entry[2] if len(entry) > 2 else "dim")] for line in wrap(text, width - HELP_INDENT)])
            elif width - column >= 12:
                wrapped = wrap(text, width - column)
                lines.append([(pad, "text"), (term, "bold"), (" " * (column - HELP_INDENT - text_width(term)), "text"),
                              (wrapped[0], "text")])
                lines.extend([[(" " * column + line, "text")] for line in wrapped[1:]])
            else:
                lines.append([(pad, "text"), (term, "bold")])
                lines.extend([[(" " * (HELP_INDENT * 2) + line, "text")] for line in wrap(text, width - 2 * HELP_INDENT)])
    return lines



def draw_help(canvas: Canvas) -> None:
    draw_overlay(canvas, "HELP", help_lines, "help")



def changelog_lines(width: int, releases: list[Release] | None = None, running: str | None = None,
                    stale: bool = False) -> list[list[tuple[str, str]]]:
    """The changelog laid out at `width`: a release title (version · date · commit), the running one marked
    installed and, when a newer install waits for a restart, newer ones marked new; then each section's entries as
    bullets with a hanging indent. An empty release (Unreleased) goes."""
    releases = parse_changelog(_version.changelog_text()) if releases is None else releases
    running = _version.VERSION if running is None else running
    lines: list[list[tuple[str, str]]] = []
    for release in releases:
        if not any(entries for _, entries in release.sections):
            continue
        title: list[tuple[str, str]] = [(release.version, "title")]
        title += [(f" · {part}", "dim") for part in (release.date, release.commit) if part]
        if release.version == running:
            title.append(("  installed", "ok"))
        elif stale and release.version[:1].isdigit() and version_key(release.version) > version_key(running):
            title.append(("  new — restart to get these", "warn"))
        add_heading(lines, title)
        for heading, entries in release.sections:
            if not entries:
                continue
            lines.append([("  " + heading, "bold")])
            for entry in entries:
                wrapped = wrap(entry, width - 6)
                lines.append([("    • ", "dim"), (wrapped[0], "text")])
                lines.extend([[(" " * 6 + more, "text")] for more in wrapped[1:]])
    return lines or [[("No changelog in this installation.", "dim")]]


class ChangelogTab(NamedTuple):
    """One tab of the changelog overlay: Godfather's own first, then each tool whose capabilities name a changelog
    (SPEC: the footer says Godfather, and a tool's release notes stay one key away)."""
    name: str
    text: str
    running: str  # the version running or installed, marked "installed"


changelog_tabs: list[ChangelogTab] = []  # the tools' tabs after Godfather's, set by the app from their capabilities


def tabs() -> list[ChangelogTab]:
    return [ChangelogTab("Godfather", _version.changelog_text(), _version.VERSION), *changelog_tabs]


def draw_changelog(canvas: Canvas, stale: bool = False, tab: int = 0) -> None:
    """The changelog overlay; with more than one tab, their names in its top border, the shown one bright, and
    ←/→ switch them (app)."""
    shown = tabs()
    current = shown[tab % len(shown)]

    def layout(width: int) -> list[list[tuple[str, str]]]:
        return changelog_lines(width, parse_changelog(current.text), current.running,
                               stale=stale and current.name == "Godfather")

    draw_overlay(canvas, "CHANGELOG", layout, "changelog")
    view = canvas.help_view
    if len(shown) < 2 or view is None or view.height < 6:
        return
    parts: list[tuple[str, str]] = [(" ", "text")]
    for index, item in enumerate(shown):
        parts += ([(" · ", "dim")] if index else []) + [(item.name, "title" if item is current else "dim")]
    parts.append((" ", "text"))
    size = sum(text_width(text) for text, _ in parts)
    if view.x + 3 + text_width(" CHANGELOG ") + 2 + size <= view.x + view.width - 2:
        canvas.segments(view.y, view.x + view.width - 2 - size, parts, size)


NARROW_MACHINE, NARROW_EVENTS = 44, 38  # the least width each takes side by side; below both, EVENTS goes


def narrow_machine_width(width: int) -> int:
    """MACHINE's width in the one-column layout: half, at least NARROW_MACHINE, with EVENTS beside it in the rest
    and a one-cell gap; the whole width where EVENTS would get under NARROW_EVENTS."""
    machine = max(NARROW_MACHINE, (width - 1) // 2)
    return machine if width - machine - 1 >= NARROW_EVENTS else width



# The footer's hints in their order: navigation, panels, configuration, help, quit last; a narrow footer drops them
# in DROP_ORDER, quit last to go. On a scrollable panel the arrows scroll.
FOOTER_HINTS: tuple[tuple[str, str], ...] = (("↑↓", "select"), ("tab", "panel"), ("s", "settings"), ("?", "help"),
                                             ("q", "quit"))
DROP_ORDER = ("?", "s", "tab", "↑↓", "q")


def footer_hints(focus: str) -> list[tuple[str, str]]:
    return [("↑↓", "select" if focus == "requests" else "scroll"), *FOOTER_HINTS[1:]]



def draw_hints(canvas: Canvas, start: int, end: int) -> int:
    """The hints centred on the footer between columns start and end (the name and the status), never over them;
    the column after the last hint."""
    hints: list[tuple[str, str]] = footer_hints(canvas.focus)

    def width() -> int:
        return hints_width(hints)

    for key in DROP_ORDER:
        if not hints or width() <= end - start - 2:
            break
        hints = [hint for hint in hints if hint[0] != key]
    if not hints:
        return start
    x = max(start + 1, min((canvas.width - width()) // 2, end - 1 - width()))
    for index, (key, action) in enumerate(hints):
        x = canvas.hint(canvas.height - 1, x, key, action)
        if index < len(hints) - 1:
            x += HINT_GAP
    return x


def dashboard_draw(canvas: Canvas, model: dict[str, Any] | None, selected: int = 0, help_open: bool | str = False,
                   error: str | None = None, cwd: str | None = None, scroll: dict | None = None,
                   settings: Settings | None = None, changelog_tab: int = 0,
                   notice: list[str] | None = None) -> None:
    """The whole screen. `notice` stands in for the panels when Slotfather is absent or unsupported; a stale model
    (`stale_since`) is drawn dimmed, with the time it went stale in SLOTS' border."""
    canvas.scroll = scroll if scroll is not None else {}
    width, height = canvas.width, canvas.height
    x, status_end = draw_header(canvas, model, error)
    version = _version.running_version() + (" ↻" if model and model.get("installed") not in (None, _version.running_source()) else "")
    if version and status_end - x - text_width(version) - 1 >= hints_width(footer_hints(canvas.focus)) + 2:
        end = canvas.put(height - 1, x - 2, version, "dim")  # the version goes before any hint on a narrow footer
        canvas.version_at = (height - 1, x - 2, end)  # a click here opens the changelog
        x = end + 3
    x = draw_hints(canvas, x, status_end)
    if error:
        canvas.put(height - 1, x + 2, f"refresh failed: {error}", "bad", max(0, status_end - x - 2))
    if notice:
        draw_notice(canvas, notice)
        model = None
    elif model is None:
        canvas.put(2, 3, f"Queue unavailable: {error}" if error else "Reading the queue…",
                   "bad" if error else "dim", width - 6)
    if model is None:
        if help_open:
            draw_open_overlay(canvas, help_open, model, changelog_tab)
        if settings is not None:
            settings.draw(canvas, model)
        return
    moment = datetime.datetime.now(datetime.timezone.utc)
    if width < 50 or height < 14:
        draw_slots(canvas, min(2, height - 4), 3, width - 6, model)
        if 6 < height - 1:
            canvas.put(6, 3, "Enlarge the terminal for leases and the queue", "dim", width - 6)
        if help_open:
            draw_open_overlay(canvas, help_open, model, changelog_tab)
        if settings is not None:
            settings.draw(canvas, model)
        return
    left = width - max(52, width * 2 // 5) - 1 if width >= WIDE else width
    slot_height = 5
    stale = model.get("stale_since")
    canvas.box(0, 0, slot_height, left, "SLOTS", f"{model['cores']} cores · {model['slots']} slots "
               + ("(manual)" if model["saved"] else "(auto)")
               + (f" · draining {drain}" if (drain := model.get("draining")) else "")
               + (f" · stale since {stale.astimezone().strftime('%H:%M')}" if stale else ""))
    draw_slots(canvas, 2, 3, left - 6, model)
    top, available = slot_height, height - 1 - slot_height
    holders, waiters = model["holders"], model["waiters"]
    requests = max(1, len(holders) + len(waiters)) + bool(holders and waiters) + 4  # the frame and a blank row each
    if width >= WIDE:
        heights = allocate(available, [("requests", min(requests, 10), requests), ("events", 5, available)])
        y = top
        for name in ("requests", "events"):
            if name in heights:
                draw_panel(canvas, name, y, 0, heights[name], left, model, selected, cwd, moment)
                y += heights[name]
        right_available = height - 1
        machine = min(machine_height(width - left - 1), right_available - 10) if right_available >= 16 else 0
        right = {"detail": right_available - machine, "machine": machine}
        y = 0
        for name in ("detail", "machine"):
            if right[name]:
                draw_panel(canvas, name, y, left + 1, right[name], width - left - 1, model, selected, cwd, moment)
                y += right[name]
    else:  # one column: QUEUE, the details, then MACHINE and EVENTS side by side, or MACHINE alone (owner)
        machine_width = narrow_machine_width(width)
        # The bottom row is MACHINE's own height (EVENTS beside it scrolls in the same); QUEUE gets its rows and the
        # details what is left (owner, 2026-11-12). Short of room, each gives way down to its minimum.
        heights = allocate(available, [("requests", min(requests, 10), requests), ("detail", 5, 5),
                                       ("bottom", 6, machine_height(machine_width))])
        extra = heights.get("bottom", 0) - machine_height(machine_width)
        if extra > 0:  # MACHINE never grows past its own height: the rest goes to the details
            heights["bottom"] = machine_height(machine_width)
            heights["detail" if "detail" in heights else "requests"] += extra
        y = top
        for name in ("requests", "detail", "bottom"):
            if name not in heights:
                continue
            if name != "bottom":
                draw_panel(canvas, name, y, 0, heights[name], width, model, selected, cwd, moment)
            elif machine_width < width:  # EVENTS on the left, MACHINE on the right (owner, 2026-11-12)
                events_width = width - machine_width - 1
                draw_panel(canvas, "events", y, 0, heights[name], events_width, model, selected, cwd, moment)
                draw_panel(canvas, "machine", y, events_width + 1, heights[name], machine_width, model, selected,
                           cwd, moment)
            else:
                draw_panel(canvas, "machine", y, 0, heights[name], width, model, selected, cwd, moment)
            y += heights[name]
    if stale:  # the last good picture, dimmed, until a snapshot arrives again
        for row in canvas.cells[:height - 1]:
            for cell in row:
                cell[1] = "dim"
    if help_open:
        draw_open_overlay(canvas, help_open, model, changelog_tab)
    if settings is not None:
        settings.draw(canvas, model)


def draw_notice(canvas: Canvas, lines: list[str]) -> None:
    """One panel instead of the overview: the tools Godfather looked for, or why Slotfather cannot be shown."""
    width, height = canvas.width, canvas.height
    if width < 12 or height < 5:
        canvas.put(0, 1, lines[0], "bad", max(0, width - 2))
        return
    canvas.box(0, 0, min(height - 1, len(lines) + 4), width, "GODFATHER")
    for row, line in enumerate(lines[:max(0, height - 5)], 2):
        canvas.put(row, 3, line, "text" if row == 2 else "dim", width - 6)


def draw_open_overlay(canvas: Canvas, which: bool | str, model: dict[str, Any] | None, tab: int = 0) -> None:
    if which == "changelog":
        draw_changelog(canvas, bool(model and model.get("installed") not in (None, _version.running_source())), tab)
    else:
        draw_help(canvas)


def draw_panel(canvas: Canvas, name: str, y: int, x: int, height: int, width: int, model: dict, selected: int,
               cwd: str | None, moment: datetime.datetime) -> None:
    if name == "requests":
        draw_requests(canvas, y, x, height, width, model, selected, moment)
    elif name == "detail":
        draw_detail(canvas, y, x, height, width, model, selected, cwd, moment)
    elif name == "machine":
        draw_machine(canvas, y, x, height, width, model)
    else:
        draw_events(canvas, y, x, height, width, model)

