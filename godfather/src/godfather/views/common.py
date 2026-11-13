"""What several overview panels draw alike: the state, priority and health marks, holder colours and fills, bars,
countable slot cells, and a request's share of the CPU and its class and ticket as Slotfather reads them."""
from __future__ import annotations

import json
import math
import re
from typing import Any, NamedTuple, Optional, Union, cast

from .. import theme as _theme
from .. import tui as _tui
from .. import version as _version
from ..tui import text_width

Part = tuple[str, str]
Model = dict[str, Any]
Item = dict[str, Any]

# A QUEUE row's state (#15), the first column of every row: (emoji, ASCII). A running row shows the first of asked,
# idle, hot and legacy that applies, else running; a waiting row yielded or waiting.
MARKS = {"asked": ("❓", "?"), "idle": ("💤", "z"), "hot": ("🔥", "!"), "legacy": ("👨‍🦳", "o"), "running": ("▶️", ">"),
         "yielded": ("↩️", "<"), "waiting": ("⏸️", ".")}
OLD_FALLBACK = "👴"  # for a terminal that draws 👨‍🦳 as two glyphs (theme.ask() measures it)
# A request's priority (owner, 2026-11-11): its own column after the state; normal has none, so the column is blank
PRIORITY_MARKS = {"interactive": ("⚡", "!"), "batch": ("🐌", "~")}
# A MACHINE resource's health (owner, 2026-11-12): ok, or critical by the rules that turn its bar red
HEALTH_MARKS = {"ok": ("🆗", "+"), "hot": ("🔥", "!")}
# A finished run in EVENTS (fathers#108): an outlier above its run type's p90, or one of a run type turning slower
RUN_MARKS = {"outlier": ("▲", "^"), "slower": ("↗", "/")}


class Marks(NamedTuple):
    """The marks a terminal is drawn with (fathers#138: a value the canvas carries, never a module's state): the
    QUEUE state, the priority, MACHINE's health and EVENTS' run marks, by key."""
    state: dict[str, str]
    priority: dict[str, str]
    health: dict[str, str]
    run: dict[str, str]


def marks_for(ascii_only: bool = False, joined: bool = True) -> Marks:
    """The marks for this terminal: ASCII where it has no UTF-8, 👴 for 👨‍🦳 where it does not join."""
    def pick(table: dict[str, tuple[str, str]]) -> dict[str, str]:
        return {key: plain if ascii_only else emoji for key, (emoji, plain) in table.items()}

    state = pick(MARKS)
    if not ascii_only and not joined:
        state["legacy"] = OLD_FALLBACK
    return Marks(state, pick(PRIORITY_MARKS), pick(HEALTH_MARKS), pick(RUN_MARKS))


EMOJI = marks_for()


def marks_of(canvas: _tui.Canvas) -> Marks:
    """The marks a canvas is drawn with: its own, else the emoji ones."""
    return canvas.marks if isinstance(canvas.marks, Marks) else EMOJI


def entry_ids(items: list[Item]) -> list[tuple[Any, int]]:
    """Each entry's identity on screen (fathers#21): its key, the lease id (fathers#101), and which of the entries
    sharing that key it is, so two entries of an older Slotfather's (keyed `[pid, since]`) of one pid created in the
    same second never share a colour, a selection or their details."""
    seen: dict[Any, int] = {}
    found: list[tuple[Any, int]] = []
    for item in items:
        key = item["key"]
        found.append((key, seen.get(key, 0)))
        seen[key] = seen.get(key, 0) + 1
    return found


def dashboard_colours(model: Model, assigned: dict[Any, int], count: int) -> None:
    """Gives each running lease one of `count` colours (the theme's holders(): 8 less any it reserves, or the
    builtin 6, #14) it keeps until it ends, preferring colours no other lease shows, so the slot bar, the table
    swatch and the detail heading stay linked as the queue moves. A lease that yields keeps its colour while it waits
    to resume (fathers#21)."""
    holders = cast(list[Item], model["holders"])
    ids = entry_ids(holders + cast(list[Item], model.get("waiters") or []))
    for key in [key for key in assigned if key not in ids]:
        del assigned[key]
    for holder, identity in zip(holders, ids):
        if holder["kind"] != "held":
            continue
        if identity not in assigned:
            taken = [index % count for index in assigned.values()]
            assigned[identity] = min(range(count), key=lambda n: (taken.count(n), n))
        holder["style"] = f"h{assigned[identity] % count}"


def stale_install(model: Optional[Model]) -> bool:
    """A newer install waits for a restart: the footer shows ↻ and the changelog marks the newer releases new."""
    return bool(model and model.get("installed") not in (None, _version.running_source()))


def fill(style: str, theme: _theme.Theme) -> str:
    """A holder's fill token (`f3`) for its ink token (`h3`) in `theme`: what paints blocks, cells, bars and the
    selection marker; any other style is its own fill."""
    token = "f" + style[1:]
    return token if style[:1] == "h" and style[1:].isdigit() and token in theme.styles else style


def bar(fraction: float, width: int, style: str, track: str) -> list[Part]:
    """Compact squares leave vertical breathing room between adjacent bar rows; `track` is the theme's empty cell."""
    width = max(0, width)
    full = max(0, min(width, math.floor(fraction * width + 0.5)))
    return [("■" * full, style), (track * (width - full), "track")]


def ramp_bar(fraction: float, width: int, critical: bool, track: str) -> list[Part]:
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
    return parts + [(track * (width - full), "track")]


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


def expected_run(item: Item) -> Optional[dict[str, Any]]:
    """A request's expected run as Slotfather gives it (fathers#106): {"key", "p50", "p90", "runs", "fallback"}."""
    found: Any = item.get("expected")
    if not isinstance(found, dict):
        return None
    expected = cast(dict[str, Any], found)
    return expected if expected.get("p50") is not None else None


def priority_mark(item: Item, marks: Marks) -> str:
    """⚡ for an interactive request, 🐌 for a batch one (! and ~ in ASCII), nothing for normal: the class it is
    served in, aged (`served`), else the one it asked."""
    return marks.priority.get(priority_of({"priority": item.get("served") or item.get("priority")}), "")
