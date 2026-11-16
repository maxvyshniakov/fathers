"""The scrollable overlays drawn, help and what's new: their frame, text, scrollbar, footer and what's new's tool
filters, from the layout overlay_text.py gives (fathers#141: the reducer reads that geometry without a view)."""
from __future__ import annotations

from typing import Optional

from .. import layout as _layout
from .. import news as _news
from ..overlay_text import overlay_view
from ..tui import Canvas
from ..text import text_width
from ..widgets import Panel, Part


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
