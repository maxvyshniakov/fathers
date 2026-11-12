"""What several overview panels draw alike: the state, priority and health marks, holder colours and fills, bars,
countable slot cells, and a request's share of the CPU and its class and ticket as Slotfather reads them."""
from __future__ import annotations

import json
import math
import re
from typing import Any, Union, cast

from .. import theme as _theme
from .. import tui as _tui
from ..tui import text_width

Part = tuple[str, str]
Model = dict[str, Any]
Item = dict[str, Any]

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


def entry_ids(items: list[Item]) -> list[tuple[Any, int]]:
    """Each entry's identity on screen (fathers#21): its key, and which of the entries sharing that key it is, so two
    entries of one pid created in the same second (Slotfather's `[pid, since]`) never share a colour, a selection or
    their details."""
    seen: dict[Any, int] = {}
    found: list[tuple[Any, int]] = []
    for item in items:
        key = item["key"]
        found.append((key, seen.get(key, 0)))
        seen[key] = seen.get(key, 0) + 1
    return found


def dashboard_colours(model: Model, assigned: dict[Any, int]) -> None:
    """Gives each running lease a colour it keeps until it ends, preferring colours no other lease shows, so the
    slot bar, the table swatch and the detail heading stay linked as the queue moves. A lease that yields keeps its
    colour while it waits to resume (fathers#21)."""
    holders = cast(list[Item], model["holders"])
    ids = entry_ids(holders + cast(list[Item], model.get("waiters") or []))
    for key in [key for key in assigned if key not in ids]:
        del assigned[key]
    count = _tui.current_theme.holders()  # 8 holder colours less any the theme reserves, or the builtin 6 (#14)
    for holder, identity in zip(holders, ids):
        if holder["kind"] != "held":
            continue
        if identity not in assigned:
            taken = [index % count for index in assigned.values()]
            assigned[identity] = min(range(count), key=lambda n: (taken.count(n), n))
        holder["style"] = f"h{assigned[identity] % count}"


def fill(style: str) -> str:
    """A holder's fill token (`f3`) for its ink token (`h3`): what paints blocks, cells, bars and the selection marker;
    any other style is its own fill."""
    token = "f" + style[1:]
    return token if style[:1] == "h" and style[1:].isdigit() and token in _tui.current_theme.styles else style


def bar(fraction: float, width: int, style: str) -> list[Part]:
    """Compact squares leave vertical breathing room between adjacent bar rows."""
    width = max(0, width)
    full = max(0, min(width, math.floor(fraction * width + 0.5)))
    return [("■" * full, style), (_tui.current_theme.track * (width - full), "track")]


def ramp_bar(fraction: float, width: int, critical: bool = False) -> list[Part]:
    """A MACHINE resource bar: each filled cell in the load chart's indigo-to-red ramp at its place along the bar, so
    a nearly full bar reaches red at its tip; a critical one (🔥) entirely in that red, so full and critical match."""
    width = max(0, width)
    full = max(0, min(width, math.floor(fraction * width + 0.5)))
    parts: list[Part] = []
    for cell in range(full):
        step = round(cell / (width - 1) * (_theme.CHART_STEPS - 1)) if width > 1 else _theme.CHART_STEPS - 1
        style = f"chart{_theme.CHART_STEPS - 1}" if critical else f"chart{step}"  # all the ramp's red: full = critical
        if parts and parts[-1][1] == style:
            parts[-1] = (parts[-1][0] + "■", style)
        else:
            parts.append(("■", style))
    return parts + [(_tui.current_theme.track * (width - full), "track")]


def share(item: Item, model: Model) -> tuple[float, float]:
    """(cores the lease's commands use, cores its slots stand for)."""
    return item["cpu"] / 100, item["count"] * model["cores"] / model["slots"]


def share_style(item: Item, model: Model) -> str:
    used, allotted = share(item, model)
    if item.get("idle_since") is not None:
        return "warn"  # an idle lease (#12; the holder's record, idle.idle_since()): holds slots doing nothing
    return "bad" if used > 1.5 * allotted else "accent"


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
             text: str = "") -> list[Part]:
    """`count` countable cells in `style` within `size` columns and at most SLOT_CELLS cells. A count wider than
    that keeps the cells that fit and counts the rest (`▪▪▪▪ +16` for 20 in 8 columns); a `whole` (-n all) one that
    does not fit says `all`. The words are in `text` (a holder's ink), else in `style`."""
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


def cells_width(counts: list[int]) -> int:
    """The slots column's natural width: the widest run of countable cells, at least the title's five."""
    return max([5] + [text_width(cells_text(min(SLOT_CELLS, count))) for count in counts])


PRIORITIES = ("interactive", "normal", "batch")  # Slotfather's request classes
META_VALUE_MAX = 200  # the longest ticket Slotfather accepts


def priority_of(entry: Item) -> str:
    """A request's class as Slotfather reads it: normal for anything it does not know."""
    value = entry.get("priority")
    return value if isinstance(value, str) and value in PRIORITIES else "normal"


def ticket_of(entry: Item) -> Union[str, None]:
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


def priority_mark(item: Item) -> str:
    """⚡ for an interactive request, 🐢 for a batch one (! and ~ in ASCII), nothing for normal: the class it is
    served in, aged (`served`), else the one it asked."""
    return priority_marks.get(priority_of({"priority": item.get("served") or item.get("priority")}), "")
