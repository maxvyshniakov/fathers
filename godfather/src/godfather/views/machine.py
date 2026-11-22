"""MACHINE: load, memory and swap as ramp bars with their health, and the history chart of held slots with its time
axis, or, while the load outside leases is high, the processes behind it (fathers#203); where its parts go is
layout.machine()."""
from __future__ import annotations

import datetime
import math
from typing import Any, Mapping, NamedTuple, Optional, Sequence, cast

from .. import layout as _layout
from .. import theme as _theme
from ..layout import Rect
from ..model import PsRow, TreeRow
from ..tui import Canvas
from ..text import clip, text_width
from ..widgets import Panel, Part
from . import common as _common
from .common import Model, ramp_bar

SPARK = "▁▂▃▄▅▆▇█"
CHART_TITLE = "slots held"  # what the history chart shows (fathers#140)
HISTORY_WINDOW = 3600  # seconds of queue.log the utilisation strip rebuilds; a model's `history_window` overrides it
PRESSURE_LEVELS = {1: ("normal", 1), 2: ("warning", 2), 4: ("critical", 3)}  # kern.memorystatus_vm_pressure_level
Step = tuple[datetime.datetime, datetime.datetime, int]


def held_history(events: list[dict[str, Any]], held_now: int, now_moment: datetime.datetime,
                 window: float = HISTORY_WINDOW * 2) -> list[Step]:
    """Held slots over the last `window` seconds as (start, end, slots) steps, walked back from held_now."""
    begin = now_moment - datetime.timedelta(seconds=window)
    steps: list[Step] = []
    level, end = held_now, now_moment
    for event in reversed(events):
        when = cast(datetime.datetime, event["when"])
        if when <= begin:
            break
        if event["delta"] and when < end:
            steps.append((when, end, max(0, level)))
            end = when
        level -= int(event["delta"])
    steps.append((begin, end, max(0, level)))
    return steps[::-1]


def history_window(model: Model) -> float:
    """The seconds the history chart spans: HISTORY_WINDOW, or the model's `history_window`. That key is no part of
    Slotfather's contract (docs/contracts/slotfather.md) and its adapter never sets it: only tools/dashboard-demo.py
    (its 60 s zoom) and the goldens and parity test drawing that scene put it into the model they draw."""
    return float(model.get("history_window") or HISTORY_WINDOW)


def history_buckets(steps: list[Step], columns: int, window: float = HISTORY_WINDOW) -> list[int]:
    """Peak slots per fixed wall-clock bucket over `window` seconds; only the newest bucket is incomplete.

    Anchoring buckets to the clock keeps old peaks stable between scroll steps.
    """
    if not steps or columns <= 0:
        return []
    end = steps[-1][1]
    seconds = max(1, math.ceil(window / columns))
    edge = math.ceil(end.timestamp() / seconds) * seconds
    end = datetime.datetime.fromtimestamp(edge, end.tzinfo)
    width = datetime.timedelta(seconds=seconds)
    begin = end - width * columns
    buckets: list[int] = []
    for column in range(columns):
        low, high = begin + width * column, begin + width * (column + 1)
        buckets.append(max((level for start, stop, level in steps if start < high and stop > low), default=0))
    return buckets


def history_summary(steps: list[Step], slots: int, window: float = HISTORY_WINDOW) -> dict[str, Any]:
    # Retain extra history for whole clock buckets, but summarise only the trailing hour.
    if steps:
        begin = steps[-1][1] - datetime.timedelta(seconds=window)
        steps = [(max(start, begin), stop, level) for start, stop, level in steps if stop > begin]
    total_seconds = sum((stop - start).total_seconds() for start, stop, _ in steps) or 1
    held = sum((stop - start).total_seconds() * min(level, slots) for start, stop, level in steps)
    full = sum((stop - start).total_seconds() for start, stop, level in steps if level >= slots)
    return {"average": held / total_seconds / slots, "full": full / total_seconds,
            "peak": max((level for _, _, level in steps), default=0)}


def machine_memory_status(memory: dict[str, Any]) -> Part:
    """RAM/pressure health only; swap has a separate status and never colours RAM by itself. Slotfather judges it
    (`memory.low` in its snapshot: the pressure level on macOS, MemAvailable and PSI on Linux); null is unknown."""
    low = memory.get("low")
    if low is None:
        return "memory unknown", "dim"
    return ("memory low", "bad") if low else ("memory ok", "status-normal")


def machine_note(model: Model) -> list[Part]:
    """MACHINE's top border, on the right (owner, 2026-11-12): the load outside leases and whether the queue admits
    work, and, where it does not, why (fathers#156)."""
    return [(f"unleased {model['advice']['unleased_load']:.1f}", "status-normal"), (" · ", "dim"),
            *admission_note(model)]


# The machine's own reasons admission is closed, red and first (fathers#156, fathers#206): the model's field that says
# why, and the border's words for it
MACHINE_BLOCKS = (("memory_block", "memory short"), ("memory_budget_block", "memory budget full"),
                  ("load_block", "CPU load high"))


def admission_note(model: Model) -> list[Part]:
    """What a new request could get now, or why nothing (fathers#156), from the fields Slotfather's model carries:
    the machine's reasons (red, first where there is another reason too): memory short, the expected-peak memory
    budget full and sustained CPU load (fathers#206; an older Slotfather sends neither); then draining after a
    lowered capacity, one request holding the whole machine (named), the slots all held, or requests waiting ahead.
    A full or queued machine is the queue working: the normal colour, never a fault. The load limit `lanefather`
    refuses lanes by is Lanefather's, not this."""
    admission = model.get("admission")
    if admission is None:
        return [("admission: ?", "dim")]
    if admission:
        return [(f"admission: {admission} slots", "status-normal")]
    slots, used = int(model.get("slots") or 0), int(model.get("used") or 0)
    found: list[Part] = []
    for key, words in MACHINE_BLOCKS:
        if cast(Mapping[str, Any], model).get(key):
            found += ([(" · ", "dim")] if found else []) + [(words, "bad")]
    holders = cast(list[dict[str, Any]], model.get("holders") or [])
    whole = next((holder for holder in holders if holder.get("kind") == "held" and slots
                  and (holder.get("all") or int(holder.get("count") or 0) >= slots)), None)
    waiting = len(cast(list[Any], model.get("waiters") or []))
    if int(model.get("draining") or 0):
        why: Optional[Part] = (f"draining · {used} of {slots} held", "warn")
    elif whole is not None:
        why = (f"whole machine: {whole.get('label') or whole.get('pid') or '?'}", "status-normal")
    elif used >= slots:
        why = (f"full · {used} of {slots} held", "status-normal")
    elif waiting and not found:
        why = (f"queue ahead · {waiting} waiting", "status-normal")
    else:
        why = None if found else ("no admission", "status-normal")
    if why is not None:
        found += ([(" · ", "dim")] if found else []) + [why]
    return found


class Resource(NamedTuple):
    """A MACHINE row: its short label, how full (None: unknown), its used/total value in full and short, and whether
    it is critical by the rules that turn its bar red."""
    label: str
    fraction: Optional[float]
    value: str
    short: str
    critical: bool


def machine_resources(model: Model) -> list[Resource]:
    """load, mem and swap as used/total (owner, 2026-11-12): `10.1/12`, `23.0/32G`, `10.0/11G`; short `23/32G`."""
    memory = model["memory"]
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
    level = memory.get("level")
    psi = [memory.get(f"psi_{kind}") for kind in ("some", "full")]
    if level is not None:
        # macOS (owner, 2026-11-12): what admission reads, the kernel's pressure level, not used RAM, which the
        # file cache keeps near full: a three-step gauge, the level's word as the value
        word, step = PRESSURE_LEVELS.get(int(level), ("critical", 3))
        found.append(Resource("mem", step / 3, word, word, low))
    elif low and memory.get("psi_low") and any(value is not None for value in psi):
        # Linux, where PSI holds admission back (Slotfather's `psi_low`, by its limits): the pressure it reads,
        # beside MemAvailable's bar
        value = f"psi {max(value for value in psi if value is not None):.0f}%"
        found.append(Resource("mem", (used / total) if total and used is not None else None, value, value, low))
    elif total and used is not None:
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


def draw_resource_rows(canvas: Canvas, area: Rect, resources: list[Resource]) -> None:
    """The three rows in the area: health mark, label, a bar, and the value right-aligned in one column as wide as
    the longest, so every bar has the same length; short values (`23/32G`) where full ones leave no bar."""
    bar_width, shortened = _layout.resource_bars(area.width, max(text_width(item.value) for item in resources),
                                                 max(text_width(item.short) for item in resources))
    values = [resource.short if shortened else resource.value for resource in resources]
    health = _common.marks_of(canvas).health
    for line, (resource, value) in enumerate(zip(resources, values), area.y):
        canvas.put(line, area.x, health["hot" if resource.critical else "ok"], "text", 2)
        canvas.put(line, area.x + 3, resource.label, "dim")
        if bar_width and resource.fraction is not None:
            canvas.segments(line, area.x + _layout.MACHINE_LABEL,
                            ramp_bar(resource.fraction, bar_width, resource.critical, canvas.theme.track), bar_width)
        canvas.put(line, area.x + area.width - text_width(value), value, "text")


def draw_machine(canvas: Canvas, rect: Rect, model: Model) -> None:
    panel = Panel(canvas, rect).frame("MACHINE")
    note = machine_note(model)
    for parts in (note, note[2:]):  # all of it, else the admission alone, else nothing
        if panel.border(parts, "MACHINE") is not None:
            break
    slots = int(model["slots"])
    resources = machine_resources(model)
    inside = _layout.machine(rect, max(text_width(resource.value) for resource in resources))
    if inside.rows is not None:
        draw_resource_rows(canvas, inside.rows, resources)
    elif inside.compact is not None:
        canvas.segments(inside.compact.y, inside.compact.x, compact_resources(
            [(resource.label, resource.fraction, resource.value, resource.critical) for resource in resources],
            inside.compact.width, [resource.short for resource in resources], _common.marks_of(canvas),
            canvas.theme.track), inside.compact.width)
    if inside.chart is not None:
        top = unleased_rows(model)
        if top:
            draw_unleased(canvas, inside.chart, top, titled=inside.side)
        else:
            draw_chart(canvas, inside.chart, model, slots, titled=inside.side)


# fathers#203: while the load outside leases is high, the chart's place names the processes behind it
UNLEASED_SHARE = 0.5  # unleased load at or over this share of the cores is high
UNLEASED_TOP = 5  # the most processes Godfather keeps for the list
UNLEASED_TITLE = "top unleased"
UNLEASED_NAME_MIN = 6  # a process name's least cells before the list drops its pid column


def unleased_high(model: Model) -> bool:
    """Whether the load outside leases is high enough for MACHINE to name the processes behind it (fathers#203):
    Slotfather's `advice.unleased_load` at or over half the cores."""
    advice: Any = model.get("advice")
    value: Any = cast(dict[str, Any], advice).get("unleased_load") if isinstance(advice, dict) else None
    cores: Any = model.get("cores")
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(cores, bool)
            or not isinstance(cores, int) or cores <= 0):
        return False
    return value >= cores * UNLEASED_SHARE


def unleased_top(table: Sequence[PsRow], holders: Sequence[Mapping[str, Any]],
                 limit: int = UNLEASED_TOP) -> list[TreeRow]:
    """The busiest processes outside leases, as (pid, cpu %, rss KB, name), the highest cpu first, then the most
    memory: every process of the machine's table but the running leases' (each holder's pid, the process rows
    Slotfather gives for it, and their descendants in the table). A waiting request holds no lease: its processes
    count as unleased, as Slotfather's unleased load counts them."""
    leased: set[int] = set()
    for holder in holders:
        if holder.get("kind") == "waiting":
            continue
        tree: Any = holder.get("tree") or []
        for pid in [holder.get("pid"), *(row[0] for row in cast(list[Any], tree) if row)]:
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:  # never the whole machine
                leased.add(pid)
    children: dict[int, list[int]] = {}
    for pid, ppid, *_ in table:
        if pid != ppid:
            children.setdefault(ppid, []).append(pid)
    pending = list(leased)
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in leased:
                leased.add(child)
                pending.append(child)
    rows = [(pid, cpu, rss, name) for pid, _, cpu, rss, name in table if pid not in leased]
    rows.sort(key=lambda row: (-row[1], -row[2], row[0]))
    return rows[:limit]


def unleased_rows(model: Model) -> list[TreeRow]:
    """The top unleased processes MACHINE lists now: the model's, only while unleased load is high; none without
    them (an older Godfather's model, a failed `ps`, a fake that gives none), so the chart stays."""
    return list(model.get("unleased_top") or []) if unleased_high(model) else []


def memory_text(rss_kb: float) -> str:
    """A process's resident memory in four or five cells: 640M, 1.2G."""
    megabytes = rss_kb / 1024
    return f"{megabytes / 1024:.1f}G" if megabytes >= 1024 else f"{megabytes:.0f}M"


def unleased_lines(rows: Sequence[TreeRow], width: int) -> list[list[Part]]:
    """The list in `width` cells: a header (`top unleased`, then the column heads where they fit after it), then a
    line per process: its name, cut to fit, and pid, cpu and memory right-aligned in columns as wide as their widest.
    A narrow list closes its gaps first, then drops the pid; a cpu over one core is yellow."""
    pids = [str(pid) for pid, *_ in rows]
    cpus = [f"{cpu:.0f}%" for _, cpu, *_ in rows]
    memories = [memory_text(rss) for _, _, rss, _ in rows]
    columns: list[tuple[str, list[str]]] = [("pid", pids), ("cpu", cpus), ("mem", memories)]
    widths = [max([text_width(head), *(text_width(value) for value in values)]) for head, values in columns]
    gap, first, name_width = 1, 1, 0
    for gap, first in ((2, 0), (1, 0), (1, 1)):
        name_width = width - sum(column + gap for column in widths[first:])
        if name_width >= UNLEASED_NAME_MIN:
            break
    name_width = max(0, name_width)
    shown = list(zip(columns[first:], widths[first:]))
    if text_width(UNLEASED_TITLE) <= name_width:
        header: list[Part] = [(UNLEASED_TITLE + " " * (name_width - text_width(UNLEASED_TITLE)), "dim")]
        header += [(" " * gap + head.rjust(column), "dim") for (head, _), column in shown]
    else:
        header = [(clip(UNLEASED_TITLE, width), "dim")]
    lines = [header]
    for index, (_, cpu, _, name) in enumerate(rows):
        parts: list[Part] = []
        if name_width > 0:
            label = clip(name, name_width)
            parts.append((label + " " * (name_width - text_width(label)), "text"))
        for col_index, ((head, values), column) in enumerate(shown):
            style = "dim" if head == "pid" else "warn" if head == "cpu" and cpu >= 100 else "text"
            prefix = "" if name_width == 0 and col_index == 0 else " " * gap
            parts.append((prefix + values[index].rjust(column), style))
        lines.append(parts)
    return lines


def draw_unleased(canvas: Canvas, area: Rect, rows: Sequence[TreeRow], titled: bool = False) -> None:
    """The top unleased processes where the chart would be, in its rows and its axis line: beside the gauges
    (`titled`) the header in the blank row above, as the chart's title; stacked, the header in its first row."""
    top = area.y - 1 if titled else area.y
    room = area.height + (2 if titled else 1)
    for offset, parts in enumerate(unleased_lines(rows, area.width)[:room]):
        canvas.segments(top + offset, area.x, parts, area.width)


def draw_chart(canvas: Canvas, area: Rect, model: Model, slots: int, titled: bool = False) -> None:
    """The history chart in the area's rows, with half a cell of air at each end (owner, 2026-11-12), and its axis
    line right under it. It says it shows slots held (fathers#140). `titled` (beside the gauges, whose rows its own
    would otherwise read as sparklines of): the title and its figures in the blank row above it, the axis line only
    the window and now, so no figure sits under a gauge's value. Stacked: the axis line keeps the figures and the
    bottom border, right under it, names the chart. Neither costs a row."""
    chart_width = max(1, area.width)
    history: list[Step] = model["history"]
    window = history_window(model)
    buckets = history_buckets(history, chart_width, window)
    rows = area.height
    for line, column_glyphs in enumerate(chart_cells(buckets, slots, rows)):
        colour = round((rows - 1 - line) / (rows - 1) * (_theme.CHART_STEPS - 1))
        for column, glyph in enumerate(column_glyphs):
            canvas.put(area.y + line, area.x + column, glyph, f"chart{colour}" if glyph != " " else "dim")
    summary = history_summary(history, slots, window)
    if titled:
        canvas.segments(area.y - 1, area.x, chart_title(summary, slots, area.width), area.width)
    else:
        canvas.put(area.y + rows + 1, area.x - 1, f" {CHART_TITLE} ", "dim", area.width)
    canvas.segments(area.y + rows, area.x, machine_legend(summary, slots, chart_width, window, stats=not titled),
                    area.width)


def chart_stats(summary: dict[str, Any], slots: int) -> list[list[Part]]:
    """The chart's figures, the least needed last: the mean and most held slots, the share of the time all were."""
    return [[("avg", "dim"), (f" {summary['average'] * slots:.1f}/{slots}", "text")],
            [("peak", "dim"), (f" {summary['peak']}", "text")],
            [("full", "dim"), (f" {summary['full']:.0%}", "text")]]


def joined(stats: list[list[Part]]) -> list[Part]:
    return [part for index, stat in enumerate(stats) for part in ([(" · ", "dim")] if index else []) + stat]


def chart_title(summary: dict[str, Any], slots: int, width: int) -> list[Part]:
    """`slots held  avg 4.4/6 · peak 6 · full 10%` above a chart beside the gauges, its figures dropped from the
    last where the width is short; the title always."""
    stats = chart_stats(summary, slots)
    while stats and text_width(CHART_TITLE) + sum(text_width(text) for text, _ in joined(stats)) + 2 > width:
        stats.pop()
    return [(CHART_TITLE, "dim")] + ([("  ", "text")] + joined(stats) if stats else [])


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


def gigabytes(value_mb: Optional[float], total: bool = False) -> str:
    """Megabytes as a short GB number for the compact resource line: 24.1, or a total as 32 or 0.5."""
    if value_mb is None:
        return "?"
    value = value_mb / 1024
    return (f"{value:.1f}".rstrip("0").rstrip(".") or "0") if total else f"{value:.1f}"


def compact_resources(resources: list[tuple[str, Optional[float], str, bool]], width: int,
                      shorts: Optional[list[str]], marks: _common.Marks, track: str) -> list[Part]:
    """Load, memory and swap on one line (owner, 2026-11-12) when MACHINE is too short for their rows: short ramp
    bars (6 to 4 cells, red when critical), else the numbers alone, a critical one in red; at the narrowest the
    short values (`23/32G`), then no marks. Never drops one."""
    parts: list[Part] = []
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
                parts.append((marks.health["hot" if critical else "ok"] + " ", "text"))
            parts.append((label + " ", "dim"))
            if bar_width and fraction is not None:
                parts += ramp_bar(fraction, bar_width, critical, track) + [(" ", "text")]
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


def machine_legend(summary: dict[str, Any], slots: int, chart_width: int, window: float = HISTORY_WINDOW,
                   stats: bool = True) -> list[Part]:
    """The history chart's legend, a time axis under it: the window it shows on the left, under its first column,
    `now` on the right under its last, and between them, centred, the mean and most held slots and the share of
    the time every slot was held (none without `stats`: they are in the chart's title). A narrow chart drops full,
    then peak, then avg; the axis stays."""
    left, right = window_text(chart_width * max(1, math.ceil(window / chart_width))), "now"
    figures = chart_stats(summary, slots) if stats else []
    room = chart_width - text_width(left) - text_width(right) - 4  # two cells each side of the stats
    parts: list[Part] = []
    while figures:
        parts = joined(figures)
        if sum(text_width(text) for text, _ in parts) <= room:
            break
        figures.pop()
        parts = []
    middle = sum(text_width(text) for text, _ in parts)
    gap = max(0, chart_width - text_width(left) - text_width(right) - middle)
    before = gap // 2 if parts else gap
    return ([(left, "dim"), (" " * before, "text")] + parts
            + [(" " * (gap - before), "text"), (right, "dim")])
