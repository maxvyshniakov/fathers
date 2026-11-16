"""The scrollable overlays' text, help and what's new, laid out at a width, and where an overlay goes on a screen
and how much of it shows (overlay_view()): the one geometry drawing (views/overlays.py), the keys and the wheel share,
so the keys right after one opens scroll it before it was ever drawn (fathers#17). Layout, not drawing: the reducer
reads it without importing a view (fathers#141)."""
from __future__ import annotations

import re
import textwrap
from typing import Any, NamedTuple, Optional

from . import layout as _layout
from . import news as _news
from .text import Part, text_width

Line = list[Part]
HELP_INDENT, HELP_GAP = 2, 2  # before a term, and between the term column and its description
HEADING_BEFORE, HEADING_AFTER = 2, 1  # blank lines around an overlay's section or release title (owner)
OVERLAY_WIDTH, OVERLAY_CHROME = 76, 5  # the most columns; the frame, a blank row above and below, the footer
TEXT_INSET = 7  # the text starts under the title's first letter (x + 3); on the right two padding cells, the
# scrollbar in the outer one, a blank and the frame: Panel(left=3, right=4)


def wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, max(1, width), break_long_words=True, break_on_hyphens=False) or [""]


def add_heading(lines: list[Line], title: Line) -> None:
    """Adds a section title: HEADING_BEFORE blank lines before it but the first, HEADING_AFTER after it."""
    blank: Line = []
    lines.extend([blank[:] for _ in range(HEADING_BEFORE if lines else 0)] + [title]
                 + [blank[:] for _ in range(HEADING_AFTER)])


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
        ("← → · f", "events: the next log (or click its title)"),
        ("r", "events: every event as logged"),
        ("/", "events: filter by text (esc clears)"),
        ("1 2 3", "the overview, lanes, processes (tools present)"),
        ("enter · a", "the selected row's actions (or right-click it)"),
        ("s", "settings"),
        ("?", "help"),
        ("v", "what's new (or click Godfather what's new?)"),
        ("esc", "close an overlay · leave a page"),
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
        ("🐌", "batch: unattended work, after the rest"),
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
        ("", "One log at a time; ↑↓ pick a run or event"),
        ("▪ · ▫", "slots held · handed back, or never held"),
        ("wait · no wait", "how long it waited for them"),
        ("ok · exit N", "how a lease ended, and how long it ran"),
        ("gave up", "a waiter stopped waiting, after so long"),
        ("owner died", "its process exited; Slotfather reaped the entry"),
        ("▲ ↗ ↘", "an outlier run · its run type got slower · faster"),
    ]),
    ("PAGES", [
        ("2 lanes", "by project: stage, receipt, review, claims, age"),
        ("▶️ 🗓️ ✅ 💤", "active · planned · landed · parked"),
        ("2 lanes N", "SLOTS' border: lanes active, planned, reviews, 🔥 a red gate"),
        ("3 procs", "owned processes and resources, then the last sweep's unowned"),
        ("▶️ 👻 ⌛", "running · its owner is gone · past its TTL"),
        ("❔ 📦", "an unowned process · an unowned resource"),
        ("3 procs N", "SLOTS' border: owned, orphan, expired, unowned"),
        ("", "A lane, lease or process in DETAILS is a link; esc goes back."),
    ]),
    ("MACHINE", [
        ("load", "the 1-minute load average / cores (not CPU %)"),
        ("mem", "the kernel's memory pressure: normal, warning, critical (Linux: used / total)"),
        ("swap", "used / total, in GB"),
        ("", "Admission follows memory pressure, not used RAM, which the file cache keeps near full."),
        ("🆗 · 🔥", "fine · critical"),
        ("unleased", "demand outside leased work"),
        ("admission", "slots a new request could get now"),
        ("", "Bars run indigo→red, and turn all red when the resource is critical."),
        ("", "The chart shows held slots over time, now on the right."),
    ]),
    ("SETTINGS", [
        ("↑ ↓", "move through every section"),
        ("← →", "change the value"),
        ("enter", "edit · save"),
        ("d · D D", "reset one · reset its section"),
        ("esc", "close"),
        ("", "A section per tool; status rows are read-only."),
        ("", "A lower capacity drains: running work keeps its slots."),
        ("", "Colours follow the terminal theme; Colour theme picks Auto, Terminal or Built-in."),
    ]),
]


HELP_COLUMN_MIN, HELP_COLUMN_MOST, HELP_COLUMNS, COLUMN_GAP = 40, 64, 3, 3  # a help column's text width; the gap


def help_sections(width: int, after: int = HEADING_AFTER) -> list[list[list[Line]]]:
    """Each help section laid out at `width` as blocks no column splits: its title, `after` blank lines and its
    first row, then each further row (a wrapped one whole) or note."""
    return [section_blocks(title, entries, width, after) for title, entries in HELP]


def help_lines(width: int) -> list[Line]:
    """The help laid out at `width`: a section title, then its rows with the terms in one column as wide as the
    section's longest term and each description wrapped under itself (a hanging indent), then its notes. Two blank
    lines before a section title but the first, one after it (owner). A row too narrow for both columns puts its
    description under its term."""
    lines: list[Line] = []
    for blocks in help_sections(width):
        lines.extend(blank_lines(HEADING_BEFORE if lines else 0))
        for block in blocks:
            lines.extend(block)
    return lines


def help_columns(screen_width: int, visible: int) -> tuple[int, list[Line]]:
    """The help in as few columns as fit the screen without scrolling (fathers#145), read down then across like a
    newspaper: a section goes on in the next column between two rows, never inside one, and never leaves its title
    alone at a column's foot; one blank line between sections and none under a title, so 156x43 shows it whole. One
    column, scrolled, where no layout fits. (text width, lines)."""
    room = max(0, screen_width - 2) - TEXT_INSET
    for count in range(2, HELP_COLUMNS + 1):
        width = min(HELP_COLUMN_MOST, (room - (count - 1) * COLUMN_GAP) // count)
        if width < HELP_COLUMN_MIN:
            break
        columns: list[list[Line]] = [[]]
        for blocks in help_sections(width, after=0):
            for index, block in enumerate(blocks):
                gap = 1 if index == 0 and columns[-1] else 0
                if columns[-1] and len(columns[-1]) + gap + len(block) > visible:
                    columns.append([])
                    gap = 0
                columns[-1].extend(blank_lines(gap) + block)
        if len(columns) <= count and max(len(column) for column in columns) <= visible:
            return width * len(columns) + COLUMN_GAP * (len(columns) - 1), side_by_side(columns, width)
    width = min(OVERLAY_WIDTH - TEXT_INSET, room)
    return width, help_lines(width)


class OverlayView(NamedTuple):
    """Where a scrollable overlay (help, the changelog) was drawn, and what it shows: the hit-test geometry."""
    x: int
    y: int
    width: int
    height: int
    offset: int
    visible: int
    total: int
    name: str = "help"


def blank_lines(count: int) -> list[Line]:
    return [[] for _ in range(count)]


def side_by_side(columns: list[list[Line]], width: int) -> list[Line]:
    """Columns of lines as one: each row's parts, every column padded to `width` and COLUMN_GAP after it."""
    rows: list[Line] = []
    for index in range(max(len(column) for column in columns)):
        row: Line = []
        for number, column in enumerate(columns):
            parts = column[index] if index < len(column) else []
            used = sum(text_width(text) for text, _ in parts)
            row += parts
            if number < len(columns) - 1:
                row.append((" " * (width - used + COLUMN_GAP), "text"))
        rows.append(row)
    return rows


def section_blocks(title: str, entries: list[tuple[str, ...]], width: int, after: int = HEADING_AFTER
                   ) -> list[list[Line]]:
    """One help section as blocks: the title, `after` blank lines and the first row; then each row or note."""
    terms = max([text_width(entry[0]) for entry in entries if entry[0]] or [0])
    column = HELP_INDENT + terms + HELP_GAP
    pad = " " * HELP_INDENT
    blocks: list[list[Line]] = []
    for entry in entries:
        term, text = entry[0], entry[1]
        if not term:
            style = entry[2] if len(entry) > 2 else "dim"
            block: list[Line] = [[(pad + line, style)] for line in wrap(text, width - HELP_INDENT)]
        elif width - column >= 12:
            wrapped = wrap(text, width - column)
            block = [[(pad, "text"), (term, "bold"), (" " * (column - HELP_INDENT - text_width(term)), "text"),
                      (wrapped[0], "text")]] + [[(" " * column + line, "text")] for line in wrapped[1:]]
        else:
            block = [[(pad, "text"), (term, "bold")]] + \
                [[(" " * (HELP_INDENT * 2) + line, "text")] for line in wrap(text, width - 2 * HELP_INDENT)]
        blocks.append(block)
    heading: list[Line] = [[(title, "title")]] + [[] for _ in range(after)]
    return [heading + (blocks[0] if blocks else [])] + blocks[1:]


# What's new (fathers#102): one timeline of every tool's entries, Unreleased ones included, newest first, or one
# tool's (its filter). Owner's layout of 2026-11-12: one left column for the release title, the section heading and
# the bullet; wrapped lines hang two cells in; a ticket reference dimmed at the bullet's end.

TEXT_MOST = 72  # the text's most cells, in any overlay width
REFERENCE = re.compile(r"\s*\(((?:[\w.-]+)?#\d+(?:,\s*(?:[\w.-]+)?#\d+)*)\)(\.?)\s*$")


def reference(text: str) -> tuple[str, str]:
    """An entry and the ticket reference at its end, `(fathers#17).` → (`….`, `fathers#17`); no reference: ''."""
    found = REFERENCE.search(text)
    if found is None:
        return text, ""
    return text[:found.start()] + found[2], found[1]


def bullet(lines: list[Line], text: str, width: int) -> None:
    """One entry: `• ` and its text, wrapped lines two cells in, its reference dimmed two cells after it."""
    body, ref = reference(text)
    wrapped = wrap(body, width - 2)
    rows: list[Line] = [[("• ", "dim"), (wrapped[0], "text")]] + [[("  " + more, "text")] for more in wrapped[1:]]
    if ref:
        last = rows[-1]
        if sum(text_width(part[0]) for part in last) + 2 + text_width(ref) <= width:
            last.append(("  " + ref, "dim"))
        else:
            rows.append([("  " + ref, "dim")])
    lines.extend(rows)


def sections(lines: list[Line], items: list[_news.Item], width: int) -> None:
    """A release's entries under their section headings (ADDED, FIXED: caps, the dim label colour), one blank line
    after each heading and between entries."""
    section: Optional[str] = None
    for item in items:
        if item.section != section:
            section = item.section
            lines.extend([[], [(section.upper(), "dim")], []])
        else:
            lines.append([])
        bullet(lines, item.text, width)


def filters(news: _news.News) -> list[str]:
    """The overlay's filters: all, then each tool with entries, in TOOLS order."""
    tools: set[str] = {item.tool for item in news.items}
    return ["all", *(tool for tool in _news.TOOLS if tool in tools)]


def news_lines(width: int, news: _news.News, read: Optional[_news.Read] = None, tool: str = "all") -> list[Line]:
    """What's new at `width` (at most TEXT_MOST): one timeline (fathers#102: no versions block; a stale install is
    the footer's ↻), the releases of every tool or of `tool`, newest first, each titled `Tool version · date`, an
    install from main's Unreleased entries `Tool · unreleased · installed <commit>`; `installed` marks what is
    installed (the Unreleased entries where a tool has them) and `new` a release with unread entries."""
    width = min(width, TEXT_MOST)
    read = {} if read is None else read
    lines: list[Line] = []
    versions = {row.tool: row.version for row in news.installed}
    unread = set(_news.unread(news, read))
    items = [item for item in news.items if tool in ("all", item.tool)]
    from_main = {item.tool for item in items if item.unreleased}
    releases: dict[tuple[str, str], list[_news.Item]] = {}
    for item in items:
        releases.setdefault((item.tool, item.version), []).append(item)
    for (name, version), entries in releases.items():
        first = entries[0]
        if first.unreleased:
            title: Line = [(_news.title(name), "text"), (" · ", "dim"), ("unreleased", "bold")]
            if first.date:
                title += [(" · ", "dim"), (first.date, "dim")]
            title += [(" · ", "dim"), ("installed" + (f" {first.commit}" if first.commit else ""), "accent")]
        else:
            title = [(_news.title(name) + " ", "text"), (version, "bold")]
            if first.date:
                title += [(" · ", "dim"), (first.date, "dim")]
            if versions.get(name) == version and name not in from_main:
                title += [(" · ", "dim"), ("installed", "accent")]
        if any(entry in unread for entry in entries):
            title += [(" · ", "dim"), ("new", "ok")]
        add_heading(lines, title)
        del lines[-1]  # sections() puts the one blank line after the title
        sections(lines, entries, width)
    if not items:
        add_heading(lines, [("Nothing in the changelogs yet.", "dim")])
        del lines[-1]
    return lines


_waiting: list[_news.News] = []  # Godfather's own news, the tools being read: what shows before they answer


def shown_news(news: Optional[_news.News]) -> _news.News:
    if news is not None:
        return news
    if not _waiting:
        _waiting.append(_news.placeholder())
    return _waiting[0]


LAID_OUT: dict[tuple[Any, ...], list[Line]] = {}  # an overlay's text by what it was laid out from: keys reuse it


def overlay_lines(kind: str, width: int, tab: int = 0, news: Optional[_news.News] = None,
                  read: Optional[_news.Read] = None, height: int = 0) -> tuple[str, list[Line], list[str]]:
    """An overlay's title, its text at `width`, and what's new's filters: help, or what's new filtered by `tab`
    (0: all). Laid out once per text and width, so scrolling never wraps it again."""
    if kind == "help":
        key: tuple[Any, ...] = (kind, width, height)
        shown: list[str] = []
    else:
        found = shown_news(news)
        shown = filters(found)
        key = (kind, width, shown[tab % len(shown)], found, tuple(sorted((read or {}).items())))
    if key not in LAID_OUT:
        if len(LAID_OUT) > 16:
            LAID_OUT.clear()
        if kind == "help":
            LAID_OUT[key] = help_columns(width, height)[1] if height else help_lines(width)
        else:
            LAID_OUT[key] = news_lines(width, shown_news(news), read, shown[tab % len(shown)])
    return ("HELP" if kind == "help" else "WHAT'S NEW"), LAID_OUT[key], shown


def overlay_view(kind: str, width: int, height: int, offset: int = 0, tab: int = 0,
                 news: Optional[_news.News] = None, read: Optional[_news.Read] = None
                 ) -> tuple[OverlayView, list[Line], str, list[str]]:
    """Where an overlay goes on a screen this size and what of it shows from `offset` (kept in range): the one
    geometry painting, the keys and the wheel share; its lines, title and what's new's filters too."""
    if kind == "help":  # as many columns as the screen takes (fathers#145)
        visible = max(0, min(height - 2, height) - OVERLAY_CHROME)
        text, _ = help_columns(width, visible)
        most = text + TEXT_INSET
        title, lines, shown = overlay_lines(kind, width, tab, news, read, visible)
    else:
        most = OVERLAY_WIDTH
        title, lines, shown = overlay_lines(kind, _layout.overlay_text(width, OVERLAY_WIDTH, TEXT_INSET), tab, news,
                                            read)
    box = _layout.centred(width, height, most, len(lines), OVERLAY_CHROME)
    visible = max(0, box.height - OVERLAY_CHROME)
    offset = _layout.clamp_scroll(offset, len(lines), visible)
    return OverlayView(box.x, box.y, box.width, box.height, offset, visible, len(lines), kind), lines, title, shown




# A row's actions (fathers#88): the menu, a park's reason and the confirmation, their lines and where they go

ACTION_WIDTH, ACTION_CHROME = 76, 5  # the most columns; the frame, a blank row above and below, the footer


def action_lines(overlay: Any) -> list[Line]:
    """What the action menu, the reason field or the confirmation says, line by line (the menu: one per action, in
    the order the cursor moves)."""
    kind = getattr(overlay, "kind", "")
    if kind == "actions":
        return [[(action.name, "text")] for action in overlay.actions]
    action = overlay.action
    target: list[Line] = [[(f"{name:<9}", "dim"), (value, "text")] for name, value in action.target]
    if kind == "reason":
        return [[("Why park it? An empty reason cancels.", "dim")], [], *target, [],
                [("reason   ", "dim"), (overlay.text, "text"), ("_", "key")]]
    command = wrap(action.shown(overlay.reason), ACTION_WIDTH - TEXT_INSET - 9)  # the whole command, never cut
    return [*target, *([[("reason   ", "dim"), (overlay.reason, "text")]] if overlay.reason else []), [],
            *[[("runs     " if index == 0 else " " * 9, "dim"), (line, "accent")] for index, line in enumerate(command)]]


def action_box(overlay: Any, width: int, height: int) -> _layout.Rect:
    return _layout.centred(width, height, ACTION_WIDTH, len(action_lines(overlay)), ACTION_CHROME)


def action_title(overlay: Any) -> str:
    kind = getattr(overlay, "kind", "")
    if kind == "actions":
        return "ACTIONS"
    return str(overlay.action.name).upper()


def action_hints(overlay: Any) -> list[Part]:
    kind = getattr(overlay, "kind", "")
    if kind == "actions":
        return [("↑↓", "choose"), ("enter", "run the chosen"), ("esc", "close")]
    if kind == "reason":
        return [("enter", "next"), ("esc", "cancel")]
    return [("y", "run it"), ("n", "cancel"), ("esc", "cancel")]
