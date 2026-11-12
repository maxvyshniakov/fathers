"""tools/raster.py (fathers#114): frames to pixels with box drawing and blocks as geometry. Corners, joins and bars are
checked pixel by pixel against tests/goldens/raster/ and by where their strokes fall; the same cells give the same
pixels; a fractional cell width leaves no seam; emoji take two cells in colour. Needs Pillow (requirements-dev.txt)."""
from __future__ import annotations

import importlib.util
import unittest
from typing import Any

import helpers  # noqa: F401  # pyright: ignore[reportUnusedImport] - src/ on sys.path
from helpers import ROOT

try:
    import PIL  # noqa: F401  # pyright: ignore[reportUnusedImport]
except ImportError:  # the 3.9 jobs run without the development tools
    raise unittest.SkipTest("Pillow is not installed (requirements-dev.txt)") from None


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"godfather_{name}", ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


raster = load("raster")
FG = "\x1b[0;38;2;200;200;200m"


def lit(image: Any, x: int, y: int) -> bool:
    return sum(image.getpixel((x, y))) > 3 * 100


class GeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.draw = raster.Raster(6, 4, raster.THEMES["seti"])
        self.g = self.draw.geometry

    def column_of(self, image: Any, column: int, row: int, near: str = "middle") -> list[int]:
        """The lit pixel columns of a vertical stroke in a cell: halfway down, or near its top or bottom edge."""
        top, bottom = self.g.y(row), self.g.y(row + 1)
        y = {"middle": (top + bottom) // 2, "top": top + 1, "bottom": bottom - 2}[near]
        return [x for x in range(self.g.x(column), self.g.x(column + 1)) if lit(image, x, y)]

    def row_of(self, image: Any, column: int, row: int) -> list[int]:
        x = (self.g.x(column) + self.g.x(column + 1)) // 2
        return [y - self.g.y(row) for y in range(self.g.y(row), self.g.y(row + 1)) if lit(image, x, y)]

    def test_the_cell_is_a_fractional_width_drawn_at_whole_pixel_edges(self) -> None:
        self.assertNotEqual(int(self.g.cell_width), self.g.cell_width, "JetBrains Mono at 18 px: 10.8 px")
        widths = {self.g.x(n + 1) - self.g.x(n) for n in range(6)}
        self.assertEqual(2, len(widths), "cells of 10 and 11 pixels that share their edges")

    def test_corners_and_joins_meet_their_lines(self) -> None:
        image = self.draw.frame(FG + "╭─┬─╮\n│ │ │\n├─┼─┤\n╰─┴─╯")
        for column in (0, 2, 4):
            side = self.column_of(image, column, 1)  # │ below the top row
            self.assertEqual(1, len(side), "a light line at 18 px is one pixel")
            for row, near in ((0, "bottom"), (2, "top"), (2, "bottom"), (3, "top")):
                with self.subTest(column=column, row=row, near=near):
                    self.assertEqual(side, self.column_of(image, column, row, near), "the stroke runs on unbroken")
        line = self.row_of(image, 1, 0)
        for column, row in ((1, 2), (3, 0), (3, 3), (2, 2)):
            with self.subTest(column=column, row=row):
                self.assertEqual(line, self.row_of(image, column, row), "every horizontal stroke on the same row")
        # the rounded corners reach their neighbours' strokes: lit where the cells meet
        for column, row, x_side in ((0, 0, False), (4, 0, True), (0, 3, False), (4, 3, True)):
            with self.subTest(corner=(column, row)):
                y = self.g.y(row) + line[0]
                edge = self.g.x(column + 1) - 1 if not x_side else self.g.x(column)
                self.assertTrue(lit(image, edge, y), "the arc ends on the horizontal line")
                x = self.column_of(image, column, 1)[0]  # this column's own side stroke
                edge_y = self.g.y(row + 1) - 1 if row == 0 else self.g.y(row)
                self.assertTrue(lit(image, x, edge_y), "the arc ends on the vertical line")

    def test_blocks_fill_their_share_without_seams(self) -> None:
        image = self.draw.frame(FG + "██████\n▀▀▀▀▀▀\n▂▂▂▂▂▂\n🮅🮅🮅🮅🮅🮅")
        g = self.g
        for row, share, at in ((0, 1.0, "top"), (1, 0.5, "top"), (2, 0.25, "bottom"), (3, 0.75, "top")):
            with self.subTest(row=row):
                top, bottom = g.y(row), g.y(row + 1)
                full = [y for y in range(top, bottom) if all(lit(image, x, y) for x in range(g.x(0), g.x(6)))]
                some = [y for y in range(top, bottom) if any(lit(image, x, y) for x in range(g.x(0), g.x(6)))]
                self.assertEqual(full, some, "a row is lit edge to edge or not at all: no seam between cells")
                self.assertEqual(round((bottom - top) * share), len(full))
                self.assertEqual(top if at == "top" else bottom - len(full), full[0])
                self.assertEqual(list(range(full[0], full[0] + len(full))), full)

    def test_the_geometry_matches_its_golden_in_both_themes(self) -> None:
        goldens = load("goldens")
        for name, image in goldens.raster_goldens().items():
            with self.subTest(name):
                from PIL import Image

                golden = Image.open(ROOT / "tests" / "goldens" / "raster" / name).convert("RGB")
                self.assertEqual(golden.size, image.size)
                self.assertEqual(golden.tobytes(), image.tobytes(), f"{name}; python3 tools/goldens.py rewrites it")


class FrameTests(unittest.TestCase):
    def test_reverse_video_swaps_the_themes_colours_where_none_is_set(self) -> None:
        theme = raster.THEMES["seti"]
        grid = raster.parse("\x1b[0;7;38;2;9;8;7mOK\x1b[0;7mx\x1b[0;38:2::1:2:3my", 4, 1, theme.palette, theme.foreground,
                            theme.background)
        self.assertEqual((theme.background, (9, 8, 7)), (grid[0][0].fg, grid[0][0].bg), "a badge")
        self.assertEqual((theme.background, theme.foreground), (grid[0][2].fg, grid[0][2].bg))
        self.assertEqual(("y", (1, 2, 3)), (grid[0][3].text, grid[0][3].fg), "colon sub-parameters")

    def test_writing_over_half_a_wide_cluster_blanks_its_other_half(self) -> None:
        palette = raster.THEMES["seti"].palette
        row = raster.parse("🆗\x1b[2Gx", 4, 1, palette)[0]
        self.assertEqual([(" ", 1), ("x", 1)], [(cell.text, cell.width) for cell in row[:2]])
        row = raster.parse("a🆗\x1b[1G🆗", 4, 1, palette)[0]
        self.assertEqual([("🆗", 2), ("", 0), (" ", 1)], [(cell.text, cell.width) for cell in row[:3]])

    def test_colours_wide_clusters_links_and_column_moves_are_read(self) -> None:
        palette = raster.THEMES["seti"].palette
        grid = raster.parse("\x1b[0;1;38;2;1;2;3;48;2;4;5;6mab\x1b[0m\x1b]8;;https://x\x1b\\c\x1b]8;;\x1b\\🆗d"
                            "\x1b[10Ge\x1b[0;31mf", 12, 1, palette)
        row = grid[0]
        self.assertEqual(raster.Cell("a", (1, 2, 3), (4, 5, 6), True, False, 1), row[0])
        self.assertEqual(("c", None), (row[2].text, row[2].fg), "a link's text, its escape dropped")
        self.assertEqual(("🆗", 2, "", 0), (row[3].text, row[3].width, row[4].text, row[4].width))
        self.assertEqual("d", row[5].text)
        self.assertEqual("e", row[9].text, "CHA moves to column 10")
        self.assertEqual(("f", palette[1]), (row[10].text, row[10].fg))

    def test_the_same_cells_give_the_same_pixels(self) -> None:
        text = (ROOT / "tests" / "goldens" / "expected-100x40.seti.ansi").read_text()
        columns, rows = raster.size_of(text)
        self.assertEqual((100, 40), (columns, rows))
        first = raster.Raster(columns, rows, raster.THEMES["seti"]).frame(text)
        again = raster.Raster(columns, rows, raster.THEMES["seti"]).frame(text)
        self.assertEqual(first.tobytes(), again.tobytes())

    def test_an_emoji_is_drawn_in_colour_across_its_two_cells(self) -> None:
        draw = raster.Raster(4, 1, raster.THEMES["prolight"])
        if draw.fonts.emoji is None:
            self.skipTest("no colour emoji font on this machine")
        image = draw.frame("🆗  ")
        g = draw.geometry
        colours = {image.getpixel((x, y)) for x in range(g.x(0), g.x(2)) for y in range(g.y(0), g.y(1))}
        self.assertTrue(any(max(c) - min(c) > 60 for c in colours), "coloured, not a monochrome glyph")
        middle = (g.y(0) + g.y(1)) // 2
        inked = [x for x in range(g.x(0), g.x(4)) if image.getpixel((x, middle)) != (255, 255, 255)]
        self.assertLess(max(inked), g.x(2), "within its two cells")
        self.assertGreater(max(inked) - min(inked), g.cell_width, "wider than one cell")
        joined = draw.frame("👨\u200d🦳  ")  # one glyph, shaped or as the fallback Godfather itself uses
        self.assertTrue(any(max(joined.getpixel((x, middle))) - min(joined.getpixel((x, middle))) > 60
                            for x in range(g.x(0), g.x(2))), "the joined emoji in colour, not two glyphs")


if __name__ == "__main__":
    unittest.main()
