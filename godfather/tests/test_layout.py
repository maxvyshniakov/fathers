"""The declarative layout (fathers#90): one function places every panel of the overview, the overlays, MACHINE's
inside and the SLOTS bar. The tables are today's rules, measured on the dashboard before the layout moved: the wide
arrangement from 110 columns, the one-column one below it with EVENTS beside MACHINE from 83 columns, the small
screen under 50 columns or 14 rows, MACHINE's chart beside its rows from 62 columns."""
from __future__ import annotations

import unittest

import helpers
from godfather import layout
from godfather.layout import Rect

SRC = helpers.ROOT / "src"  # helpers puts it on sys.path
M7, M11 = 7, 11  # MACHINE's natural height with its chart beside the rows, and stacked


def natural(width: int) -> int:
    return M7 if width >= 62 else M11


class OverviewTests(unittest.TestCase):
    # (width, height, queue rows): {panel: (y, x, height, width)}, Tab order
    CASES: list[tuple[tuple[int, int, int], dict[str, tuple[int, int, int, int]], tuple[str, ...]]] = [
        # wide: QUEUE and EVENTS on the left, DETAILS and MACHINE on the right (2/5, at least 52)
        ((156, 43, 1), {"slots": (0, 0, 5, 93), "requests": (5, 0, 5, 93), "events": (10, 0, 32, 93),
                        "details": (0, 94, 35, 62), "machine": (35, 94, 7, 62)}, ("requests", "events", "details")),
        ((110, 40, 1), {"slots": (0, 0, 5, 57), "requests": (5, 0, 5, 57), "events": (10, 0, 29, 57),
                        "details": (0, 58, 28, 52), "machine": (28, 58, 11, 52)}, ("requests", "events", "details")),
        ((156, 43, 12), {"slots": (0, 0, 5, 93), "requests": (5, 0, 16, 93), "events": (21, 0, 21, 93),
                         "details": (0, 94, 35, 62), "machine": (35, 94, 7, 62)}, ("requests", "events", "details")),
        # wide and short: MACHINE needs 16 rows on the right; QUEUE gives way where EVENTS keeps its 5
        ((156, 20, 1), {"slots": (0, 0, 5, 93), "requests": (5, 0, 5, 93), "events": (10, 0, 9, 93),
                        "details": (0, 94, 12, 62), "machine": (12, 94, 7, 62)}, ("requests", "events", "details")),
        ((156, 17, 1), {"slots": (0, 0, 5, 93), "requests": (5, 0, 5, 93), "events": (10, 0, 6, 93),
                        "details": (0, 94, 10, 62), "machine": (10, 94, 6, 62)}, ("requests", "events", "details")),
        ((156, 16, 1), {"slots": (0, 0, 5, 93), "requests": (5, 0, 5, 93), "events": (10, 0, 5, 93),
                        "details": (0, 94, 15, 62)}, ("requests", "events", "details")),
        # wide and too short for EVENTS under QUEUE (fathers#140): QUEUE at its rows, MACHINE's compact line in the
        # rows left under it, DETAILS the whole right column; with under three rows left, QUEUE takes them
        ((156, 14, 1), {"slots": (0, 0, 5, 93), "requests": (5, 0, 5, 93), "machine": (10, 0, 3, 93),
                        "details": (0, 94, 13, 62)}, ("requests", "details")),
        ((156, 20, 6), {"slots": (0, 0, 5, 93), "requests": (5, 0, 10, 93), "machine": (15, 0, 4, 93),
                        "details": (0, 94, 19, 62)}, ("requests", "details")),
        ((156, 19, 6), {"slots": (0, 0, 5, 93), "requests": (5, 0, 10, 93), "machine": (15, 0, 3, 93),
                        "details": (0, 94, 18, 62)}, ("requests", "details")),
        ((156, 18, 6), {"slots": (0, 0, 5, 93), "requests": (5, 0, 12, 93), "details": (0, 94, 10, 62),
                        "machine": (10, 94, 7, 62)}, ("requests", "details")),
        ((156, 21, 6), {"slots": (0, 0, 5, 93), "requests": (5, 0, 10, 93), "events": (15, 0, 5, 93),
                        "details": (0, 94, 13, 62), "machine": (13, 94, 7, 62)}, ("requests", "events", "details")),
        ((110, 14, 8), {"slots": (0, 0, 5, 57), "events": (5, 0, 8, 57), "details": (0, 58, 13, 52)},
         ("requests", "events", "details")),
        # one column: QUEUE, DETAILS, then EVENTS beside MACHINE from 83 columns, MACHINE alone below
        ((109, 40, 1), {"slots": (0, 0, 5, 109), "requests": (5, 0, 5, 109), "details": (10, 0, 18, 109),
                        "events": (28, 0, 11, 54), "machine": (28, 55, 11, 54)}, ("requests", "details", "events")),
        ((100, 40, 1), {"slots": (0, 0, 5, 100), "requests": (5, 0, 5, 100), "details": (10, 0, 18, 100),
                        "events": (28, 0, 11, 50), "machine": (28, 51, 11, 49)}, ("requests", "details", "events")),
        ((83, 40, 1), {"slots": (0, 0, 5, 83), "requests": (5, 0, 5, 83), "details": (10, 0, 18, 83),
                       "events": (28, 0, 11, 38), "machine": (28, 39, 11, 44)}, ("requests", "details", "events")),
        ((82, 40, 1), {"slots": (0, 0, 5, 82), "requests": (5, 0, 5, 82), "details": (10, 0, 22, 82),
                       "machine": (32, 0, 7, 82)}, ("requests", "details")),
        ((76, 40, 1), {"slots": (0, 0, 5, 76), "requests": (5, 0, 5, 76), "details": (10, 0, 22, 76),
                       "machine": (32, 0, 7, 76)}, ("requests", "details")),
        ((60, 40, 1), {"slots": (0, 0, 5, 60), "requests": (5, 0, 5, 60), "details": (10, 0, 18, 60),
                       "machine": (28, 0, 11, 60)}, ("requests", "details")),
        # one column, short: the floors (QUEUE 10, DETAILS 13, the bottom row 6) give way to the minimums
        ((100, 34, 3), {"slots": (0, 0, 5, 100), "requests": (5, 0, 7, 100), "details": (12, 0, 13, 100),
                        "events": (25, 0, 8, 50), "machine": (25, 51, 8, 49)}, ("requests", "details", "events")),
        ((100, 33, 3), {"slots": (0, 0, 5, 100), "requests": (5, 0, 7, 100), "details": (12, 0, 13, 100),
                        "events": (25, 0, 7, 50), "machine": (25, 51, 7, 49)}, ("requests", "details", "events")),
        ((100, 30, 12), {"slots": (0, 0, 5, 100), "requests": (5, 0, 13, 100), "details": (18, 0, 5, 100),
                         "events": (23, 0, 6, 50), "machine": (23, 51, 6, 49)}, ("requests", "details", "events")),
        ((100, 25, 12), {"slots": (0, 0, 5, 100), "requests": (5, 0, 14, 100), "details": (19, 0, 5, 100)},
         ("requests", "details")),
        ((100, 20, 3), {"slots": (0, 0, 5, 100), "requests": (5, 0, 7, 100), "details": (12, 0, 7, 100)},
         ("requests", "details")),
        ((60, 14, 8), {"slots": (0, 0, 5, 60), "details": (5, 0, 8, 60)}, ("requests", "details")),
        ((50, 14, 1), {"slots": (0, 0, 5, 50), "requests": (5, 0, 8, 50)}, ("requests",)),
    ]

    def test_each_arrangement_and_threshold(self) -> None:
        for (width, height, rows), panels, order in self.CASES:
            with self.subTest(width=width, height=height, rows=rows):
                found = layout.overview(width, height, rows, natural)
                self.assertEqual("wide" if width >= 110 else "column", found.arrangement)
                self.assertEqual({name: Rect(*rect) for name, rect in panels.items()}, found.panels)
                self.assertEqual(order, found.order)

    def test_a_small_screen_has_only_the_slots_and_a_note(self) -> None:
        for width, height in ((49, 30), (80, 13), (30, 8), (5, 5)):
            with self.subTest(width=width, height=height):
                found = layout.overview(width, height, 1, natural)
                self.assertEqual("small", found.arrangement)
                self.assertEqual({}, found.panels)
                self.assertEqual(("requests",), found.order)
                self.assertEqual(Rect(min(2, height - 4), 3, 2, width - 6), found.slots)
                self.assertEqual(6 if 6 < height - 1 else None, found.note_row)

    def test_the_thresholds_in_one_place(self) -> None:
        self.assertEqual((110, 50, 14), (layout.WIDE, layout.SMALL_WIDTH, layout.SMALL_HEIGHT))
        beside = [width for width in range(60, 120) if "events" in layout.overview(width, 40, 1, natural).panels]
        self.assertEqual(83, beside[0], "EVENTS beside MACHINE from 83 columns")
        self.assertEqual(62, next(width for width in range(30, 120) if layout.machine_side_by_side(width)))


class TabOrderTests(unittest.TestCase):
    def test_by_columns_left_to_right_each_top_to_bottom_queue_always_first(self) -> None:
        cases: list[tuple[dict[str, Rect], tuple[str, ...]]] = [
            ({"requests": Rect(5, 0, 5, 9), "events": Rect(10, 0, 5, 9), "details": Rect(0, 10, 5, 9)},
             ("requests", "events", "details")),
            ({"events": Rect(10, 0, 5, 9), "details": Rect(0, 10, 5, 9)}, ("requests", "events", "details")),
            ({"machine": Rect(0, 0, 5, 9)}, ("requests",)),
        ]
        for panels, order in cases:
            with self.subTest(panels=list(panels)):
                self.assertEqual(order, layout.tab_order(panels))


class StackTests(unittest.TestCase):
    def test_minimums_in_order_then_growth_then_the_rest(self) -> None:
        tier = layout.Tier(minimums=(("a", 3), ("b", 4)), grow=("a",), rest=("b", "a"))
        cases: list[tuple[int, dict[str, int], dict[str, int]]] = [  # (available, naturals): heights
            (20, {"a": 5}, {"a": 5, "b": 15}),
            (8, {"a": 5}, {"a": 4, "b": 4}),
            (5, {"a": 5}, {"a": 5}),  # b does not fit: a takes the rest
            (2, {"a": 5}, {}),
        ]
        for available, naturals, heights in cases:
            with self.subTest(available=available):
                self.assertEqual(heights, layout.stack(available, (tier,), naturals))

    def test_an_all_or_nothing_tier_falls_back_to_the_next(self) -> None:
        floors = layout.Tier(minimums=(("a", 5), ("b", 5)), grow=("a",), rest=("b",), whole=True)
        least = layout.Tier(minimums=(("a", 2), ("b", 2)), grow=("b", "a"), rest=("b",))
        self.assertEqual({"a": 6, "b": 6}, layout.stack(12, (floors, least), {"a": 6, "b": 2}))
        self.assertEqual({"a": 6, "b": 3}, layout.stack(9, (floors, least), {"a": 6, "b": 3}))


class MachineTests(unittest.TestCase):
    # (width, height): side by side, the rows' rectangle, the compact line, the chart (rows)
    CASES: list[tuple[tuple[int, int], bool, tuple[int, int, int, int] | None, tuple[int, int, int, int] | None,
                      tuple[int, int, int, int] | None]] = [
        ((62, 7), True, (2, 3, 3, 30), None, (2, 35, 3, 24)),
        ((62, 11), True, (2, 3, 3, 30), None, (2, 35, 4, 24)),
        ((61, 11), False, (2, 3, 3, 55), None, (5, 3, 4, 55)),
        ((61, 10), False, (2, 3, 3, 55), None, (5, 3, 3, 55)),
        ((61, 9), False, (2, 3, 3, 55), None, (5, 3, 2, 55)),
        ((61, 8), False, (2, 3, 3, 55), None, None),  # one row of chart never shows: blank rows instead
        ((61, 6), False, (1, 3, 3, 55), None, None),
        ((61, 5), False, (1, 3, 3, 55), None, None),
        ((61, 4), False, None, (1, 3, 1, 55), None),  # the rows share one line
        ((61, 2), False, None, None, None),
        ((62, 5), False, (1, 3, 3, 56), None, None),  # too short for the chart beside: stacked
    ]

    def test_the_chart_beside_or_under_the_rows_and_what_gives_way(self) -> None:
        for (width, height), side, rows, compact, chart in self.CASES:
            with self.subTest(width=width, height=height):
                found = layout.machine(Rect(0, 0, height, width), values=9)
                self.assertEqual(side, found.side)
                self.assertEqual(Rect(*rows) if rows else None, found.rows)
                self.assertEqual(Rect(*compact) if compact else None, found.compact)
                self.assertEqual(Rect(*chart) if chart else None, found.chart)

    def test_the_natural_height(self) -> None:
        self.assertEqual([11, 7], [layout.machine_height(61), layout.machine_height(62)])


class OverlayTests(unittest.TestCase):
    def test_centred_and_capped_by_the_screen(self) -> None:
        cases = [  # (screen width, height, most width, body rows, chrome rows): rect
            ((156, 43, 76, 100, 5), Rect(1, 40, 41, 76)),
            ((156, 43, 76, 10, 5), Rect(14, 40, 15, 76)),
            ((60, 20, 76, 100, 5), Rect(1, 1, 18, 58)),
            ((156, 43, 80, 25, 9), Rect(4, 38, 34, 80)),
            ((1, 1, 76, 3, 5), Rect(0, 0, 0, 0)),
        ]
        for (width, height, most, rows, chrome), rect in cases:
            with self.subTest(width=width, height=height, rows=rows):
                self.assertEqual(rect, layout.centred(width, height, most, rows, chrome))


class SlotsTests(unittest.TestCase):
    def test_slots_spread_with_gaps_and_the_free_label_right_aligned(self) -> None:
        cases = [  # (x, width, slots): [(start, width)]: two-cell gaps, then one, then none
            ((3, 87, 8), [(3, 10), (15, 9), (26, 9), (37, 9), (48, 9), (59, 9), (70, 9), (81, 9)]),
            ((3, 16, 8), [(3, 2), (6, 1), (8, 1), (10, 1), (12, 1), (14, 1), (16, 1), (18, 1)]),
            ((3, 8, 8), [(3, 1), (4, 1), (5, 1), (6, 1), (7, 1), (8, 1), (9, 1), (10, 1)]),
            ((3, 22, 8), [(3, 1), (6, 1), (9, 1), (12, 1), (15, 1), (18, 1), (21, 1), (24, 1)]),
            ((3, 4, 8), [(3, 1), (4, 1), (5, 1), (6, 1), (7, 0), (7, 0), (7, 0), (7, 0)]),
        ]
        for (x, width, slots), cells in cases:
            with self.subTest(width=width, slots=slots):
                self.assertEqual(cells, layout.slot_cells(x, width, slots))

    def test_labels_end_two_cells_before_the_next_and_free_sits_right(self) -> None:
        cases: list[tuple[tuple[int, int], list[int], bool, list[tuple[int, int]], int | None]] = [
            ((3, 87), [3, 26], True, [(3, 21), (26, 58)], 86),
            ((3, 87), [3, 26], False, [(3, 21), (26, 64)], None),
            ((3, 3), [3], True, [(3, 3)], None),  # no room for "free"
        ]
        for (x, width), starts, free, places, free_at in cases:
            with self.subTest(width=width, free=free):
                self.assertEqual((places, free_at), layout.slot_labels(Rect(0, x, 2, width), starts, free))


if __name__ == "__main__":
    unittest.main()
