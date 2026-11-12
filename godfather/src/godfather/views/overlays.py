"""The scrollable overlays, help and the changelog: their text laid out at a width, and their frame, scrollbar and
footer. Where one goes and how much of it shows (overlay_view()) is what both drawing and the keys use, so the keys
right after one opens scroll it before it was ever drawn (fathers#17)."""
from __future__ import annotations

import re
import textwrap
from typing import Callable, NamedTuple, Optional

from .. import layout as _layout
from .. import version as _version
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


# The changelog: CHANGELOG.md shipped with the installed package, as titles and bullet rows

RELEASE = re.compile(r"^## \[?([^\]\s]+)\]?(?: - (\d{4}-\d\d-\d\d))?(?: \((\w+)\))?")
MARKUP = re.compile(r"\*\*|`|\[([^\]]*)\]\([^)]*\)")


class Release(NamedTuple):
    version: str
    date: str
    commit: str
    sections: list[tuple[str, list[str]]]  # (Added, [entries])


def plain(text: str) -> str:
    """Markdown taken out of a changelog entry: bold, code and links."""
    return MARKUP.sub(lambda found: found[1] or "", text)


def parse_changelog(text: str) -> list[Release]:
    """The releases of a Keep a Changelog file, newest first, with markdown taken out of the entries; an entry's
    continuation lines join it."""
    found: list[Release] = []
    for line in text.splitlines():
        heading = RELEASE.match(line)
        if heading:
            found.append(Release(heading[1], heading[2] or "", heading[3] or "", []))
        elif not found:
            continue
        elif line.startswith("### "):
            found[-1].sections.append((line[4:].strip(), []))
        elif line.startswith("- ") and found[-1].sections:
            found[-1].sections[-1][1].append(plain(line[2:].strip()))
        elif line.startswith("  ") and line.strip() and found[-1].sections and found[-1].sections[-1][1]:
            entries = found[-1].sections[-1][1]
            entries[-1] += " " + plain(line.strip())
    return found


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)) or (-1,)


def changelog_lines(width: int, releases: Optional[list[Release]] = None, running: Optional[str] = None,
                    stale: bool = False) -> list[Line]:
    """The changelog laid out at `width`: a release title (version · date · commit), the running one marked
    installed and, when a newer install waits for a restart, newer ones marked new; then each section's entries as
    bullets with a hanging indent. An empty release (Unreleased) goes."""
    releases = parse_changelog(_version.changelog_text()) if releases is None else releases
    running = _version.VERSION if running is None else running
    lines: list[Line] = []
    for release in releases:
        if not any(entries for _, entries in release.sections):
            continue
        title: Line = [(release.version, "title")]
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


def overlay_lines(kind: str, tab: int = 0, stale: bool = False) -> tuple[str, Callable[[int], list[Line]]]:
    """An overlay's title and its text at a width: help, or the changelog's `tab` (the Godfather one marks newer
    releases while a newer install waits for a restart)."""
    if kind == "help":
        return "HELP", help_lines
    shown = tabs()
    current = shown[tab % len(shown)]

    def layout(width: int) -> list[Line]:
        return changelog_lines(width, parse_changelog(current.text), current.running,
                               stale=stale and current.name == "Godfather")

    return "CHANGELOG", layout


def overlay_view(kind: str, width: int, height: int, offset: int = 0, tab: int = 0,
                 stale: bool = False) -> tuple[OverlayView, list[Line]]:
    """Where an overlay goes on a screen this size and what of it shows from `offset` (kept in range): the one
    geometry painting, the keys and the wheel share."""
    box = _layout.centred(width, height, OVERLAY_WIDTH, 0, 0)
    lines = overlay_lines(kind, tab, stale)[1](box.width - TEXT_INSET)
    box = _layout.centred(width, height, OVERLAY_WIDTH, len(lines), OVERLAY_CHROME)
    visible = max(0, box.height - OVERLAY_CHROME)
    offset = max(0, min(offset, max(0, len(lines) - visible)))
    return OverlayView(box.x, box.y, box.width, box.height, offset, visible, len(lines), kind), lines


def draw_overlay(canvas: Canvas, kind: str, offset: int = 0, tab: int = 0, stale: bool = False) -> None:
    """A centred, scrollable overlay (help, the changelog): the frame, scrollbar, counts and centred footer are the
    same for every one."""
    view, lines = overlay_view(kind, canvas.width, canvas.height, offset, tab, stale)
    canvas.help_view = view
    panel = Panel(canvas, _layout.Rect(view.y, view.x, view.height, view.width), kind, right=4, footer=True)
    panel.overlay(overlay_lines(kind, tab, stale)[0])
    if view.height < 6:
        canvas.put(view.y + 1, view.x + 2, "Enlarge terminal", "dim", max(0, view.width - 4))
        return
    panel.scrollbar(view.offset, view.visible, view.total)
    for row, parts in enumerate(lines[view.offset:view.offset + view.visible], panel.inner.y):
        canvas.segments(row, panel.inner.x, parts, panel.inner.width)
    shown = f"{round(100 * (view.offset + view.visible) / view.total)}%" if view.total > view.visible else ""
    hints = [("↑↓", "scroll"), ("esc", "close")] + ([("q", "quit")] if kind == "help" else [])
    panel.footer(hints, shown)
    if kind == "changelog":
        changelog_tabs_border(panel, tab)


def changelog_tabs_border(panel: Panel, tab: int) -> None:
    """With more than one tab, their names in the changelog's top border, the shown one bright; ←/→ switch them."""
    shown = tabs()
    if len(shown) < 2:
        return
    current = shown[tab % len(shown)]
    parts: list[Part] = []
    for index, item in enumerate(shown):
        parts += ([(" · ", "dim")] if index else []) + [(item.name, "title" if item is current else "dim")]
    panel.border(parts, "CHANGELOG", margin=3)


def draw_help(canvas: Canvas, offset: int = 0) -> None:
    draw_overlay(canvas, "help", offset)


def draw_changelog(canvas: Canvas, stale: bool = False, tab: int = 0, offset: int = 0) -> None:
    draw_overlay(canvas, "changelog", offset, tab, stale)
