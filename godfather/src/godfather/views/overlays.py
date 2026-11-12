"""The scrollable overlays, help and what's new: their text laid out at a width, and their frame, scrollbar and
footer. Where one goes and how much of it shows (overlay_view()) is what both drawing and the keys use, so the keys
right after one opens scroll it before it was ever drawn (fathers#17)."""
from __future__ import annotations

import re
import textwrap
from typing import Any, Optional

from .. import layout as _layout
from .. import news as _news
from ..tui import Canvas, text_width
from ..widgets import OverlayView, Panel, Part

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
        ("s", "settings"),
        ("?", "help"),
        ("v", "what's new (or click Godfather  what's new?)"),
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
        ("▪", "slots taken: granted"),
        ("▫", "slots handed back: released, reaped, or a waiter that left"),
        ("wait · no wait", "how long it waited for them"),
        ("ok · exit N", "how a lease ended, and how long it ran"),
        ("gave up", "a waiter stopped waiting, after so long"),
        ("owner died", "its process exited; Slotfather reaped the entry"),
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
        ("← →", "change the value"),
        ("enter", "edit · save"),
        ("d · D D", "reset one · reset all"),
        ("esc", "close"),
        ("", "A lower capacity drains: running work keeps its slots."),
        ("", "Colours follow the terminal theme; Colour theme picks Auto, Terminal or Built-in."),
    ]),
]


def help_lines(width: int) -> list[Line]:
    """The help laid out at `width` cells: a section title, then its rows with the terms in one column as wide as the
    section's longest term and each description wrapped under itself (a hanging indent), then its notes. Two blank
    lines before a section title but the first, one after it (owner). A row too narrow for both columns puts its
    description under its term."""
    lines: list[Line] = []
    for title, entries in HELP:
        add_heading(lines, [(title, "title")])
        terms = max([text_width(entry[0]) for entry in entries if entry[0]] or [0])
        column = HELP_INDENT + terms + HELP_GAP
        for entry in entries:
            term, text = entry[0], entry[1]
            pad = " " * HELP_INDENT
            if not term:
                style = entry[2] if len(entry) > 2 else "dim"
                lines.extend([[(pad + line, style)] for line in wrap(text, width - HELP_INDENT)])
            elif width - column >= 12:
                wrapped = wrap(text, width - column)
                lines.append([(pad, "text"), (term, "bold"), (" " * (column - HELP_INDENT - text_width(term)), "text"),
                              (wrapped[0], "text")])
                lines.extend([[(" " * column + line, "text")] for line in wrapped[1:]])
            else:
                lines.append([(pad, "text"), (term, "bold")])
                lines.extend([[(" " * (HELP_INDENT * 2) + line, "text")] for line in wrap(text, width - 2 * HELP_INDENT)])
    return lines


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
                  read: Optional[_news.Read] = None) -> tuple[str, list[Line], list[str]]:
    """An overlay's title, its text at `width`, and what's new's filters: help, or what's new filtered by `tab`
    (0: all). Laid out once per text and width, so scrolling never wraps it again."""
    if kind == "help":
        key: tuple[Any, ...] = (kind, width)
        shown: list[str] = []
    else:
        found = shown_news(news)
        shown = filters(found)
        key = (kind, width, shown[tab % len(shown)], found, tuple(sorted((read or {}).items())))
    if key not in LAID_OUT:
        if len(LAID_OUT) > 16:
            LAID_OUT.clear()
        if kind == "help":
            LAID_OUT[key] = help_lines(width)
        else:
            LAID_OUT[key] = news_lines(width, shown_news(news), read, shown[tab % len(shown)])
    return ("HELP" if kind == "help" else "WHAT'S NEW"), LAID_OUT[key], shown


def overlay_view(kind: str, width: int, height: int, offset: int = 0, tab: int = 0,
                 news: Optional[_news.News] = None, read: Optional[_news.Read] = None
                 ) -> tuple[OverlayView, list[Line], str, list[str]]:
    """Where an overlay goes on a screen this size and what of it shows from `offset` (kept in range): the one
    geometry painting, the keys and the wheel share; its lines, title and what's new's filters too."""
    title, lines, shown = overlay_lines(kind, _layout.overlay_text(width, OVERLAY_WIDTH, TEXT_INSET), tab, news, read)
    box = _layout.centred(width, height, OVERLAY_WIDTH, len(lines), OVERLAY_CHROME)
    visible = max(0, box.height - OVERLAY_CHROME)
    offset = max(0, min(offset, max(0, len(lines) - visible)))
    return OverlayView(box.x, box.y, box.width, box.height, offset, visible, len(lines), kind), lines, title, shown


def draw_overlay(canvas: Canvas, kind: str, offset: int = 0, tab: int = 0, news: Optional[_news.News] = None,
                 read: Optional[_news.Read] = None) -> None:
    """A centred, scrollable overlay (help, what's new): the frame, scrollbar, counts and centred footer are the
    same for every one."""
    view, lines, title, shown = overlay_view(kind, canvas.width, canvas.height, offset, tab, news, read)
    canvas.help_view = view
    panel = Panel(canvas, _layout.Rect(view.y, view.x, view.height, view.width), kind, right=4, footer=True)
    panel.overlay(title)
    if view.height < 6:
        panel.note("Enlarge terminal")
        return
    panel.scrollbar(view.offset, view.visible, view.total)
    for row, parts in enumerate(lines[view.offset:view.offset + view.visible], panel.inner.y):
        canvas.segments(row, panel.inner.x, parts, panel.inner.width)
    percent = f"{round(100 * (view.offset + view.visible) / view.total)}%" if view.total > view.visible else ""
    hints = ([("←→", "tool")] if len(shown) > 1 else []) + [("↑↓", "scroll"), ("esc", "close")] \
        + ([("q", "quit")] if kind == "help" else [])
    panel.footer(hints, percent)
    if kind == "changelog":
        filters_border(panel, shown, tab)


def filters_border(panel: Panel, shown: list[str], tab: int) -> None:
    """With more than one filter, their names in the top border, the shown one bright; ←/→ or a click switch them.
    Each name's cells, as drawn, go to Canvas.filter_hits: the one registry clicks are tested against."""
    if len(shown) < 2:
        return
    current = tab % len(shown)
    parts: list[Part] = []
    for index, name in enumerate(shown):
        parts += ([(" · ", "dim")] if index else []) + [(_news.title(name), "title" if index == current else "dim")]
    at = panel.border(parts, "WHAT'S NEW", margin=3)
    if at is None:
        return
    x = at + 1  # border() pads the parts with a space
    for index, name in enumerate(shown):
        x += text_width(" · ") if index else 0
        end = x + text_width(_news.title(name))
        panel.canvas.filter_hits.append((panel.rect[0], x, end, index))
        x = end


def draw_help(canvas: Canvas, offset: int = 0) -> None:
    draw_overlay(canvas, "help", offset)


def draw_changelog(canvas: Canvas, tab: int = 0, offset: int = 0, news: Optional[_news.News] = None,
                   read: Optional[_news.Read] = None) -> None:
    draw_overlay(canvas, "changelog", offset, tab, news, read)
