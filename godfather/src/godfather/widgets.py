"""The shared widgets every panel and overlay is drawn with (fathers#90). `Panel` is the frame, the title and labels
set into its border, the padding, the focus colour, a selected row's markers on both frames, the viewport that
painting, hit testing and scrolling share, and the scrollbar's thumb with its flash. `Table` is column sharing and
dropping, header labels in the border and rows of emoji cells cut to their columns. A view says what to show; where
it goes comes from layout.py and from these."""
from __future__ import annotations

from typing import Any, NamedTuple, Optional, Union

from .layout import Rect
from .tui import HINT_GAP, Canvas, hints_width, text_width

Part = tuple[str, str]  # (text, style)
Placed = tuple[str, str, int, int, bool]  # (key, title, x, width, right-aligned)
Spec = tuple[str, str, int, bool, int]  # (key, title, width (0: the flexible one), right-aligned, drop priority)
Value = Union[str, Part, list[Part]]

SELECTED_FRAME = "┃"  # the left frame beside a selected row, heavy in the row's colour (#15); # on ASCII
# A scrollbar's thumb (#15): a whole cell, a cell's lower half (where it starts), its upper half (where it ends);
# # on an ASCII terminal
SCROLL_THUMB, THUMB_LOWER, THUMB_UPPER = "┃", "╻", "╹"
FLEXIBLE_MIN = 10  # the flexible column's cells before other columns drop
OVERLAY_DROP = ("↑↓", "q", "←→", "d", "enter")  # an overlay footer's hints, the least needed first


class Panel:
    """A framed panel in `rect`. Its content sits inside a blank padding row under the top border and one over the
    bottom border, three cells from each side frame; the scrollbar runs in the outer of the right padding cells, so
    a blank cell stays between it and the content and nothing moves (#15). `name` makes it a focusable panel: its
    frame is the accent while it holds the focus, and its viewport is what clicks and the wheel reach."""

    def __init__(self, canvas: Canvas, rect: Rect, name: str = "", left: int = 3, right: int = 3,
                 footer: bool = False) -> None:
        self.canvas, self.rect, self.name = canvas, rect, name
        y, x, height, width = rect
        # The content: a panel's three cells in from each frame; an overlay's under its title's first letter (left
        # 3) or two in (Settings), a cell more on the right, above its footer row
        self.inner = Rect(y + 2, x + left, max(0, height - (5 if footer else 4)), max(0, width - left - right))
        self.footer_row = y + height - 2
        self.left = left  # the content's column within the panel

    @property
    def focused(self) -> bool:
        return bool(self.name) and self.name == self.canvas.focus

    def frame(self, title: str = "", note: str = "") -> Panel:
        """A rounded light frame in the frame token, the focused panel's in the accent blue, the same glyphs: focus
        shows by colour alone (owner). The title is set into the top border at x + 2, a note on its right."""
        canvas, (y, x, height, width) = self.canvas, self.rect
        if height < 2 or width < 6:
            return self
        style = "accent" if self.focused else "frame"
        canvas.put(y, x, "╭" + "─" * (width - 2) + "╮", style)
        for row in range(y + 1, y + height - 1):
            canvas.put(row, x, "│", style)
            canvas.put(row, x + width - 1, "│", style)
        canvas.put(y + height - 1, x, "╰" + "─" * (width - 2) + "╯", style)
        after = canvas.put(y, x + 2, f" {title} ", "title", width - 4) if title else x + 2
        if note and x + width - 2 - text_width(note) - 2 > after:
            canvas.right(y, x + width - 2, f" {note} ", "dim")
        return self

    def overlay(self, title: str = "", note: str = "") -> Panel:
        """An overlay's frame: the cells under it cleared first."""
        self.canvas.fill(*self.rect)
        return self.frame(title, note)

    def border(self, parts: list[Part], title: str, margin: int = 2) -> bool:
        """`parts` set into the top border at the right, padded by a space each side, where they fit `margin` cells
        after the title; False where they do not."""
        y, x, _, width = self.rect
        size = sum(text_width(text) for text, _ in parts) + 2
        if x + 2 + text_width(f" {title} ") + margin + size > x + width - 2:
            return False
        self.canvas.segments(y, x + width - 2 - size, [(" ", "text"), *parts, (" ", "text")], size)
        return True

    def bottom(self, text: str, style: str) -> None:
        """A note set into the bottom border on the left."""
        y, x, height, width = self.rect
        self.canvas.put(y + height - 1, x + 2, text, style, width - 4)

    def viewport(self, count: int, selected: Optional[int] = None, offset: Optional[int] = None,
                 row_items: Optional[list[Optional[int]]] = None, base: Optional[int] = None) -> tuple[int, int]:
        """The rows from the top that painting, hit testing and wheel scrolling share: (offset, visible). The offset
        is the one asked for, else the panel's scroll (Canvas.scroll), else the one that keeps `selected` in view, kept
        in range; the scrollbar's thumb runs in the same rows and shows what overflows (no row counts)."""
        visible = self.inner.height
        automatic = max(0, (selected or 0) - visible + 1) if selected is not None else 0
        offset = self.canvas.scroll.get(self.name, automatic) if offset is None else offset
        offset = max(0, min(offset, max(0, count - visible)))
        y, x, height, width = self.rect
        self.canvas.tables[self.name] = dict(x=x, y=y, width=width, height=height, top=self.inner.y,
                                             visible=visible, count=count, offset=offset, base=base,
                                             row_items=row_items)
        if visible:
            self.scrollbar(offset, visible, count)
        return offset, visible

    def scrollbar(self, offset: int, visible: int, total: int, height: Optional[int] = None) -> None:
        """The thumb alone, no track (owner), in the content's rows (`height`, the visible rows unless given), as
        long as the visible share, at the bottom on the last page, in half cells so it moves in half-cell steps
        (owner); in the muted token at rest and in the foreground for a flash while the panel scrolls (owner).
        Nothing while everything fits."""
        top, column = self.inner.y, self.rect.x + self.rect.width - 2
        height = visible if height is None else height
        if total <= visible or height <= 0 or visible <= 0:
            return
        halves = 2 * height
        length = min(halves, max(1, round(halves * visible / total)))
        start = round((halves - length) * min(offset, total - visible) / (total - visible))
        for row in range(height):
            upper, lower = start <= 2 * row < start + length, start <= 2 * row + 1 < start + length
            if upper or lower:
                glyph = SCROLL_THUMB if upper and lower else THUMB_LOWER if lower else THUMB_UPPER
                self.canvas.put(top + row, column, glyph, "text" if self.name in self.canvas.glow else "dim")

    def mark(self, row: int, style: str, tint: Union[bool, str] = True, marker: str = SELECTED_FRAME) -> None:
        """A selected row: its background between the frames (a holder's own tint, or the neutral selection) and
        both frames at that row heavy in its colour: no cursor glyph."""
        x, width = self.rect.x, self.rect.width
        self.canvas.select(row, x + 1, x + width - 1, tint)
        for frame in (x, x + width - 1):
            self.canvas.put(row, frame, marker, style)

    def centre(self, lines: list[Part]) -> None:
        """Lines in the middle of the panel, each centred: a spinner and its word."""
        y, x, height, width = self.rect
        middle = y + max(1, (height - 2) // 2)
        for offset, (text, style) in enumerate(lines):
            if offset and middle + offset >= y + height - 1:
                break
            column = x + (width - 1) // 2 if not offset else x + (width - text_width(text)) // 2
            self.canvas.put(middle + offset, column, text, style)

    def rows(self, offset: int, visible: int, count: int) -> list[tuple[int, int]]:
        """The content rows the viewport shows: (screen row, position in the content) each."""
        return [(self.inner.y + row, offset + row) for row in range(max(0, min(visible, count - offset)))]

    def put(self, row: int, text: Any, style: str = "text") -> None:
        """A line of content `row` rows into the panel, cut to its width."""
        self.canvas.put(self.inner.y + row, self.inner.x, text, style, self.inner.width)

    def note(self, text: str) -> None:
        """One muted line just inside the frame, where a panel too small to show its content says so."""
        y, x, _, width = self.rect
        self.canvas.put(y + 1, x + 2, text, "dim", max(0, width - 4))

    def document(self, document: Canvas, height: int, offset: Optional[int] = None) -> None:
        """A document as wide as the panel, its content at the panel's own columns, drawn offscreen and scrolled
        within the panel: the rows the viewport shows, links and all."""
        start, visible = self.viewport(height, offset=offset)
        canvas, x = self.canvas, self.rect.x
        for row in range(min(visible, height - start)):
            for column in range(self.left, self.left + self.inner.width):
                target_y, target_x = self.inner.y + row, x + column
                if 0 <= target_y < canvas.height and 0 <= target_x < canvas.width:
                    canvas.cells[target_y][target_x] = document.cells[start + row][column][:]
                    url = document.links.get((start + row, column))
                    canvas.links.pop((target_y, target_x), None)
                    if url:
                        canvas.links[target_y, target_x] = url

    def footer(self, hints: list[Part], right: str = "") -> None:
        """An overlay's key line, as the main footer (#15): the hints centred in key and muted tokens, the least
        needed dropped when narrow (closing last), and a muted position on the right."""
        canvas, (_, x, _, width), y = self.canvas, self.rect, self.footer_row
        inner = max(0, width - 4)
        room = inner - (text_width(right) + 2 if right else 0)
        for drop in OVERLAY_DROP:
            if hints_width(hints) <= room:
                break
            hints = [hint for hint in hints if hint[0] != drop]
        start = x + 2 + max(0, (inner - hints_width(hints)) // 2)
        if right:
            canvas.right(y, x + width - 2, right, "dim")
        for index, (key, action) in enumerate(hints):
            start = canvas.hint(y, start, key, action, x + width - 2 - start)
            if index < len(hints) - 1:
                start += HINT_GAP


def label_column(labels: list[str], width: int) -> tuple[int, int]:
    """A document of label and value rows in `width` cells: (the labels' column with two cells after the longest,
    the values' width, at least one)."""
    labels_width = max(text_width(label) for label in labels) + 2
    return labels_width, max(1, width - labels_width)


class Table:
    """Columns placed in a width: (key, title, x, width, right-aligned) each, x from the table's left edge with two
    cells between columns."""

    def __init__(self, placed: list[Placed]) -> None:
        self.placed = placed

    @classmethod
    def fit(cls, width: int, spec: list[Spec]) -> Table:
        """spec: (key, title, width, right-aligned, priority) with width 0 for the one flexible column. Drops the
        columns of the highest priority number until the rest leave the flexible one FLEXIBLE_MIN cells."""
        keep = list(spec)
        while sum(c[2] for c in keep) + 2 * (len(keep) - 1) + FLEXIBLE_MIN > width and any(c[2] and c[4] for c in keep):
            keep.remove(max((c for c in keep if c[2] and c[4]), key=lambda c: c[4]))
        flexible = max(4, width - sum(c[2] for c in keep) - 2 * (len(keep) - 1))
        placed: list[Placed] = []
        x = 0
        for key, title, size, right, _ in keep:
            placed.append((key, title, x, size or flexible, right))
            x += (size or flexible) + 2
        return cls(placed)

    @classmethod
    def share(cls, width: int, columns: list[tuple[str, str, bool]], natural: dict[str, int],
              flexible: str) -> Table:
        """Each column has a natural width (its widest value). A table wider than the natural widths and their gaps
        shares the surplus out in proportion to them, so it fills the panel evenly (the largest remainders get the
        cells rounding leaves); a narrower one keeps `flexible` flexible: it shrinks first, then the other columns
        drop from the last."""
        gaps = 2 * (len(columns) - 1)
        surplus = width - sum(natural.values()) - gaps
        if surplus <= 0:
            return cls.fit(width, [(key, title, 0 if key == flexible else natural[key], right, rank)
                                   for rank, (key, title, right) in enumerate(columns)])
        total = sum(natural.values())
        sizes = {key: natural[key] + surplus * natural[key] // total for key in natural}
        left = width - gaps - sum(sizes.values())
        for key in sorted(natural, key=lambda key: -(surplus * natural[key] % total))[:left]:
            sizes[key] += 1
        placed: list[Placed] = []
        column = 0
        for key, title, right in columns:
            placed.append((key, title, column, sizes[key], right))
            column += sizes[key] + 2
        return cls(placed)

    def shifted(self, offset: int) -> Table:
        """The same columns `offset` cells further right: after columns another table draws before them."""
        return Table([(key, title, x + offset, size, right) for key, title, x, size, right in self.placed])

    def width(self, key: str, default: int) -> int:
        return next((size for name, _, _, size, _ in self.placed if name == key), default)

    def header(self, panel: Panel, title: str, gutter: int = 0) -> None:
        """The panel's frame, its top border doubling as the table's header: the title where every panel's sits
        (x + 3), the other labels over their columns, `gutter` in, in the foreground on a focused panel."""
        panel.frame()
        canvas, (y, x, _, width) = panel.canvas, panel.rect
        for index, (_, label, column, size, right) in enumerate(self.placed):
            value = title if index == 0 else label.lower()
            start = x + 3 if index == 0 else x + 3 + gutter + column + (max(0, size - text_width(value)) if right else 0)
            canvas.put(y, start - 1, " ", "text")
            canvas.put(y, start, value, "title" if index == 0 else "text" if panel.focused else "dim", size)
            canvas.put(y, min(x + width - 2, start + min(size, text_width(value))), " ", "text")

    def row(self, canvas: Canvas, y: int, x: int, values: dict[str, Value]) -> None:
        """One row from column x: each value (text, a (text, style) part or a list of parts) cut to its column,
        right-aligned where the column is."""
        for key, _, column, size, right in self.placed:
            value = values.get(key, "")
            parts: list[Part] = value if isinstance(value, list) else [(value, "text")] if isinstance(value, str) \
                else [value]
            start = x + column
            if right:
                start += max(0, size - sum(text_width(text) for text, _ in parts))
            canvas.segments(y, start, parts, x + column + size - start)


# How a narrow main footer gives way (fathers#102): (the hints dropped, the right side's stamp) in order, so `? help`
# stays while lower hints go, and quit is the last hint left.
FOOTER_STEPS: tuple[tuple[tuple[str, ...], str], ...] = (
    ((), "updated"), (("tab",), "updated"), (("tab", "s"), "updated"), (("tab", "s"), "time"),
    (("tab", "s", "↑↓"), "time"), (("tab", "s", "↑↓"), ""), (("tab", "s", "↑↓", "?"), ""),
    (("tab", "s", "↑↓", "?", "q"), ""))
FOOTER_PAD = 2  # the least blank cells between the hints and what is left and right of them


class Footer:
    """The screen's last row: `Godfather · what's new?` on the left (a click opens what's new), the status on the
    right (the last update, live or stale), and the key hints centred between the two at every width, the least
    needed dropped first when narrow; a failure or notice after them. Clicks on the hints press their keys."""

    def __init__(self, canvas: Canvas) -> None:
        self.canvas, self.row = canvas, canvas.height - 1

    def draw(self, label: Part, hints: list[Part], status: Part, stamp: str = "", message: Part = ("", "")) -> None:
        canvas, y = self.canvas, self.row
        left = canvas.put(y, 1, label[0], label[1], max(0, canvas.width - 2))
        if left > 1:
            canvas.version_at = (y, 1, left)
        end = canvas.width - 1
        shown, right, tail = [], end, ""
        for dropped, form in FOOTER_STEPS:
            shown = [hint for hint in hints if hint[0] not in dropped]
            tail = ("updated " + stamp if form == "updated" else stamp) if stamp and form else ""
            right = end - text_width(status[0]) - (text_width(tail) + 2 if tail else 0)
            if right - left >= hints_width(shown) + 2 * FOOTER_PAD and right >= left + 2:
                break
        if right >= left + 2:
            at = canvas.right(y, end, *status)
            if tail:
                canvas.right(y, at - 2, tail, "dim")
        else:
            right, shown = end, []
        after = self.hints(shown, left, right)
        if message[0]:
            canvas.put(y, after + 2, message[0], message[1], max(0, right - after - 4))

    def hints(self, hints: list[Part], start: int, end: int) -> int:
        """The hints centred between columns start and end; the column after them."""
        if not hints:
            return start
        x = start + (end - start - hints_width(hints)) // 2
        for index, (key, action) in enumerate(hints):
            x = self.canvas.hint(self.row, x, key, action)
            if index < len(hints) - 1:
                x += HINT_GAP
        return x


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
