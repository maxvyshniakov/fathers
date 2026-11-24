"""The declarative layout (fathers#90): one function places every panel of the overview, the overlays, MACHINE's
inside and the SLOTS bar. The tables are today's rules, measured on the dashboard before the layout moved: the wide
arrangement from 110 columns, the one-column one below it with EVENTS full width and MACHINE beside DETAILS under it
from 83 columns (fathers#169), the small
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
        # wide: QUEUE and EVENTS on the left, DETAILS and MACHINE on the right (2/5, at least 52); row 0 is the header
        ((156, 43, 1), {"slots": (1, 0, 5, 93), "requests": (6, 0, 5, 93), "events": (11, 0, 31, 93),
                        "details": (1, 94, 34, 62), "machine": (35, 94, 7, 62)}, ("requests", "events", "details")),
        ((110, 40, 1), {"slots": (1, 0, 5, 57), "requests": (6, 0, 5, 57), "events": (11, 0, 28, 57),
                        "details": (1, 58, 27, 52), "machine": (28, 58, 11, 52)}, ("requests", "events", "details")),
        ((156, 43, 12), {"slots": (1, 0, 5, 93), "requests": (6, 0, 16, 93), "events": (22, 0, 20, 93),
                         "details": (1, 94, 34, 62), "machine": (35, 94, 7, 62)}, ("requests", "events", "details")),
        # wide and short: MACHINE needs 16 rows on the right; QUEUE gives way where EVENTS keeps its 5
        ((156, 20, 1), {"slots": (1, 0, 5, 93), "requests": (6, 0, 5, 93), "events": (11, 0, 8, 93),
                        "details": (1, 94, 11, 62), "machine": (12, 94, 7, 62)}, ("requests", "events", "details")),
        ((156, 18, 1), {"slots": (1, 0, 5, 93), "requests": (6, 0, 5, 93), "events": (11, 0, 6, 93),
                        "details": (1, 94, 10, 62), "machine": (11, 94, 6, 62)}, ("requests", "events", "details")),
        ((156, 17, 1), {"slots": (1, 0, 5, 93), "requests": (6, 0, 5, 93), "events": (11, 0, 5, 93),
                        "details": (1, 94, 15, 62)}, ("requests", "events", "details")),
        # wide and too short for EVENTS under QUEUE (fathers#140): QUEUE at its rows less what MACHINE's three-row
        # compact form needs, MACHINE under it, DETAILS the whole right column; never a padded QUEUE
        ((156, 14, 1), {"slots": (1, 0, 5, 93), "requests": (6, 0, 4, 93), "machine": (10, 0, 3, 93),
                        "details": (1, 94, 12, 62)}, ("requests", "details")),
        ((156, 20, 6), {"slots": (1, 0, 5, 93), "requests": (6, 0, 10, 93), "machine": (16, 0, 3, 93),
                        "details": (1, 94, 18, 62)}, ("requests", "details")),
        ((156, 19, 6), {"slots": (1, 0, 5, 93), "requests": (6, 0, 9, 93), "machine": (15, 0, 3, 93),
                        "details": (1, 94, 17, 62)}, ("requests", "details")),
        ((156, 18, 6), {"slots": (1, 0, 5, 93), "requests": (6, 0, 8, 93), "machine": (14, 0, 3, 93),
                        "details": (1, 94, 16, 62)}, ("requests", "details")),
        ((156, 22, 6), {"slots": (1, 0, 5, 93), "requests": (6, 0, 10, 93), "events": (16, 0, 5, 93),
                        "details": (1, 94, 13, 62), "machine": (14, 94, 7, 62)}, ("requests", "events", "details")),
        ((110, 14, 8), {"slots": (1, 0, 5, 57), "requests": (6, 0, 4, 57), "machine": (10, 0, 3, 57),
                        "details": (1, 58, 12, 52)}, ("requests", "details")),
        # one column from 83 columns (fathers#169): QUEUE, EVENTS the whole width, then MACHINE on the left and
        # DETAILS on the right, the row as tall as DETAILS' key fields (13); below 83 QUEUE, DETAILS and MACHINE alone
        ((109, 40, 1), {"slots": (1, 0, 5, 109), "requests": (6, 0, 5, 109), "events": (11, 0, 15, 109),
                        "machine": (26, 0, 13, 54), "details": (26, 55, 13, 54)}, ("requests", "events", "details")),
        ((100, 40, 1), {"slots": (1, 0, 5, 100), "requests": (6, 0, 5, 100), "events": (11, 0, 15, 100),
                        "machine": (26, 0, 13, 49), "details": (26, 50, 13, 50)}, ("requests", "events", "details")),
        ((83, 40, 1), {"slots": (1, 0, 5, 83), "requests": (6, 0, 5, 83), "events": (11, 0, 15, 83),
                       "machine": (26, 0, 13, 44), "details": (26, 45, 13, 38)}, ("requests", "events", "details")),
        ((82, 40, 1), {"slots": (1, 0, 5, 82), "requests": (6, 0, 5, 82), "details": (11, 0, 21, 82),
                       "machine": (32, 0, 7, 82)}, ("requests", "details")),
        ((76, 40, 1), {"slots": (1, 0, 5, 76), "requests": (6, 0, 5, 76), "details": (11, 0, 21, 76),
                       "machine": (32, 0, 7, 76)}, ("requests", "details")),
        ((60, 40, 1), {"slots": (1, 0, 5, 60), "requests": (6, 0, 5, 60), "details": (11, 0, 17, 60),
                       "machine": (28, 0, 11, 60)}, ("requests", "details")),
        # one column, short (fathers#169): the bottom row gives way first (DETAILS' 13, MACHINE's 11, its 7 without
        # the chart, its 6), then EVENTS (7, then 5); QUEUE keeps its least; without a bottom row the band is DETAILS'
        ((100, 34, 3), {"slots": (1, 0, 5, 100), "requests": (6, 0, 7, 100), "events": (13, 0, 7, 100),
                        "machine": (20, 0, 13, 49), "details": (20, 50, 13, 50)}, ("requests", "events", "details")),
        ((100, 33, 3), {"slots": (1, 0, 5, 100), "requests": (6, 0, 7, 100), "events": (13, 0, 7, 100),
                        "machine": (20, 0, 12, 49), "details": (20, 50, 12, 50)}, ("requests", "events", "details")),
        ((100, 30, 12), {"slots": (1, 0, 5, 100), "requests": (6, 0, 10, 100), "events": (16, 0, 7, 100),
                         "machine": (23, 0, 6, 49), "details": (23, 50, 6, 50)}, ("requests", "events", "details")),
        ((100, 25, 12), {"slots": (1, 0, 5, 100), "requests": (6, 0, 13, 100), "details": (19, 0, 5, 100)},
         ("requests", "details")),
        ((100, 20, 3), {"slots": (1, 0, 5, 100), "requests": (6, 0, 7, 100), "details": (13, 0, 6, 100)},
         ("requests", "details")),
        ((60, 14, 8), {"slots": (1, 0, 5, 60), "details": (6, 0, 7, 60)}, ("requests", "details")),
        ((50, 14, 1), {"slots": (1, 0, 5, 50), "requests": (6, 0, 7, 50)}, ("requests",)),
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
                self.assertEqual(Rect(min(3, max(1, height - 4)), 3, 2, width - 6), found.slots)
                self.assertEqual(7 if 7 < height - 1 else None, found.note_row)

    def test_the_thresholds_in_one_place(self) -> None:
        self.assertEqual((110, 50, 14), (layout.WIDE, layout.SMALL_WIDTH, layout.SMALL_HEIGHT))
        beside = [width for width in range(60, 120) if "events" in layout.overview(width, 40, 1, natural).panels]
        self.assertEqual(83, beside[0], "EVENTS, and MACHINE beside DETAILS, from 83 columns")
        self.assertEqual(62, next(width for width in range(30, 120) if layout.machine_side_by_side(width)))


class ShortWideTests(unittest.TestCase):
    """fathers#140 review: as a wide terminal grows taller for a fixed queue, EVENTS once shown stays, MACHINE
    moves from under QUEUE to the right column at most once, and QUEUE never has more rows than its own."""

    def test_panels_come_in_order_as_the_height_grows(self) -> None:
        for rows in (1, 3, 6, 12, 20, 40):
            shown, right = False, False
            for height in range(layout.SMALL_HEIGHT, 70):
                with self.subTest(rows=rows, height=height):
                    panels = layout.overview(156, height, rows, natural).panels
                    self.assertFalse(shown and "events" not in panels, "EVENTS went again")
                    shown = shown or "events" in panels
                    machine = panels.get("machine")  # the usual tier drops it on the shortest right columns
                    left = machine is not None and machine.x == 0
                    self.assertFalse(right and left, "MACHINE came back to the left")
                    right = right or (machine is not None and machine.x > 0)
                    self.assertEqual(not shown, left, "MACHINE is under QUEUE exactly while EVENTS is not shown")
                    self.assertLessEqual(panels["requests"].height, layout.queue_height(rows), "no padded QUEUE")
                    if not shown:
                        self.assertEqual(height - 2, panels["details"].height, "DETAILS the whole column")


class OneColumnTests(unittest.TestCase):
    """fathers#169: from 83 columns EVENTS takes the full-width band under QUEUE and DETAILS the bottom row's right,
    beside MACHINE, at the heights the band and the row had before; where the bottom row does not fit, the band is
    DETAILS'."""

    def test_events_the_band_and_details_beside_machine_under_it(self) -> None:
        for width in range(83, 110):
            for rows in (1, 3, 12):
                for height in range(layout.SMALL_HEIGHT, 70):
                    with self.subTest(width=width, rows=rows, height=height):
                        panels = layout.overview(width, height, rows, natural).panels
                        if "events" not in panels:
                            self.assertNotIn("machine", panels, "no bottom row: the band is DETAILS'")
                            continue
                        events, machine, details = panels["events"], panels["machine"], panels["details"]
                        self.assertEqual((0, width), (events.x, events.width), "EVENTS the whole width")
                        self.assertEqual(events.y + events.height, machine.y, "the bottom row under it")
                        self.assertEqual((machine.y, machine.height), (details.y, details.height), "one row")
                        self.assertEqual((0, machine.width + 1), (machine.x, details.x), "MACHINE left, DETAILS right")
                        self.assertEqual(width, machine.width + 1 + details.width)
                        self.assertEqual(height - 1, details.y + details.height, "down to the footer")


class AllocateTests(unittest.TestCase):
    """fathers#169: one column's rows by floors and priorities. Each case: (width, height, queue rows, DETAILS'
    wanted rows) and the heights of QUEUE, the band and the bottom row (MACHINE's, and DETAILS' beside it)."""

    CASES: list[tuple[tuple[int, int, int, int], dict[str, int]]] = [
        # tall, few queue rows: QUEUE its rows, the bottom row DETAILS' key fields, EVENTS the rest
        ((100, 50, 6, 13), {"requests": 10, "events": 20, "machine": 13}),
        ((100, 40, 6, 13), {"requests": 10, "events": 10, "machine": 13}),
        # nothing to show in DETAILS: the bottom row is MACHINE's natural height, EVENTS gets the rows
        ((100, 40, 6, 5), {"requests": 10, "events": 12, "machine": 11}),
        # many queue rows: the floors first (QUEUE 10, EVENTS 7, the row 13), then QUEUE takes what is left
        ((100, 40, 17, 13), {"requests": 13, "events": 7, "machine": 13}),
        ((100, 60, 30, 13), {"requests": 33, "events": 7, "machine": 13}),
        # short: the row gives way before EVENTS' three rows, EVENTS before QUEUE's least
        ((100, 31, 6, 13), {"requests": 10, "events": 7, "machine": 7}),
        ((100, 30, 6, 13), {"requests": 10, "events": 7, "machine": 6}),
        ((100, 29, 6, 13), {"requests": 10, "events": 5, "machine": 7}),
        ((100, 28, 6, 13), {"requests": 10, "events": 5, "machine": 6}),
        ((100, 27, 6, 13), {"requests": 10, "details": 10}),  # no bottom row: DETAILS has the band
        # 83 columns, the narrowest with the bottom row; 82, DETAILS' band over MACHINE alone, as before
        ((83, 40, 6, 13), {"requests": 10, "events": 10, "machine": 13}),
        ((82, 40, 6, 13), {"requests": 10, "details": 16, "machine": 7}),
        ((60, 40, 6, 13), {"requests": 10, "details": 13, "machine": 10}),
        # wide: unchanged, QUEUE over EVENTS beside DETAILS over MACHINE at its natural height
        ((156, 43, 6, 13), {"requests": 10, "events": 26, "details": 34, "machine": 7}),
        ((156, 43, 6, 5), {"requests": 10, "events": 26, "details": 34, "machine": 7}),
    ]

    def test_floors_then_priorities(self) -> None:
        for (width, height, rows, details), heights in self.CASES:
            with self.subTest(width=width, height=height, rows=rows, details=details):
                panels = layout.overview(width, height, rows, natural, details).panels
                self.assertEqual(heights, {name: panels[name].height for name in heights})
                if "events" in panels and width < layout.WIDE:
                    self.assertEqual(panels["machine"].height, panels["details"].height, "one bottom row")
                self.assertEqual(height - 1, max(rect.y + rect.height for rect in panels.values()), "every row")

    def test_allocate_steps_down_floors_in_order_then_grows_by_priority(self) -> None:
        bands = (layout.Band("a", (4,), 9), layout.Band("b", (6, 3)), layout.Band("c", (8, 5, 2), 10))
        def heights(available: int) -> dict[str, int]:
            return layout.allocate(available, bands, give_way=("c", "b"), grow=("a", "c"), rest=("b", "a"))
        self.assertEqual({"a": 9, "b": 8, "c": 10}, heights(27), "a, then c to their naturals; b the rest")
        self.assertEqual({"a": 6, "b": 6, "c": 8}, heights(20), "every first floor; a grows first")
        self.assertEqual({"a": 4, "b": 6, "c": 5}, heights(15), "c gives way a floor")
        self.assertEqual({"a": 4, "b": 6, "c": 2}, heights(12))
        self.assertEqual({"a": 4, "b": 3, "c": 2}, heights(9), "then b")
        self.assertEqual({"a": 5, "b": 3}, heights(8), "at the last floors those that fit, from the top")
        self.assertEqual({"a": 4}, heights(4))

    def test_a_taller_stacked_machine_gives_its_chart_the_rows(self) -> None:
        self.assertEqual(4, layout.machine(Rect(0, 0, 11, 49), 9).chart.height)  # type: ignore[union-attr]
        self.assertEqual(6, layout.machine(Rect(0, 0, 13, 49), 9).chart.height)  # type: ignore[union-attr]
        self.assertEqual(4, layout.machine(Rect(0, 0, 11, 62), 9).chart.height, "beside the rows: CHART_ROWS")  # type: ignore[union-attr]


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


class PageLayoutTests(unittest.TestCase):
    """fathers#85: a page's table, details and EVENTS strip; as the terminal grows taller panels only arrive, and the
    table never has more rows than its own."""

    def test_panels_only_arrive_as_the_height_grows(self) -> None:
        for width in (60, 100, 156):
            for rows in (1, 4, 12, 40):
                seen: set[str] = set()
                for height in range(layout.SMALL_HEIGHT, 70):
                    with self.subTest(width=width, rows=rows, height=height):
                        page = layout.page(width, height, rows, "lanes")
                        self.assertLessEqual(seen, set(page.panels), "a panel went again")
                        seen = set(page.panels)
                        self.assertIn("lanes", page.panels)
                        self.assertEqual("lanes", page.order[0], "Tab starts at the table")
                        if "events" in page.panels:
                            self.assertLessEqual(page.panels["lanes"].height, layout.queue_height(rows))

    def test_small_is_a_note(self) -> None:
        self.assertEqual("small", layout.page(40, 30, 3, "lanes").arrangement)
