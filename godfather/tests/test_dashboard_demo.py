"""The navigation scene (the README media) advances recorded observations without inventing queue events."""
import importlib.util
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import view as _view
from helpers import ROOT


def load_demo() -> Any:
    path = ROOT / "tools/dashboard-demo.py"
    spec = importlib.util.spec_from_file_location("dashboard_demo_fixture", path)
    assert spec and spec.loader
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    return demo


class DemoCase(unittest.TestCase):
    def tearDown(self) -> None:
        _view.fresh()
        importlib.reload(importlib.import_module("godfather.sources"))


class NavigationDemoTests(DemoCase):
    def test_clock_and_recorded_samples_advance_while_queue_members_stay_fixed(self) -> None:
        demo = load_demo()
        with patch.object(demo.time, "monotonic", return_value=100) as clock:
            app, _ = demo.load_scene(advance=True)
            scene = app.Scene()
            first = scene.load()[0]
            clock.return_value = 105
            later = scene.load()[0]
        self.assertEqual(5, (later["updated"] - first["updated"]).total_seconds())
        for key in ("holders", "waiters"):
            self.assertEqual([(x["key"], x["count"]) for x in first[key]], [(x["key"], x["count"]) for x in later[key]])
        self.assertEqual(first["events"], later["events"])
        self.assertNotEqual([h["cpu"] for h in first["holders"]], [h["cpu"] for h in later["holders"]])
        tool = _view.current()
        window = first["history_window"]
        self.assertNotEqual(tool.history_buckets(first["history"], 56, window),
                            tool.history_buckets(later["history"], 56, window))

    def test_recorded_chart_has_peaks_and_fractional_vertical_cells(self) -> None:
        demo = load_demo()
        with patch.object(demo.time, "monotonic", return_value=100):
            _, model = demo.load_scene(advance=True)
        tool = _view.current()
        canvas = tool.Canvas(156, 43)
        tool.dashboard_draw(canvas, model, cwd="~/straylight")
        text = canvas.render(False)
        self.assertTrue(any(glyph in text for glyph in "▁▂▃▄▅▆▇"))
        values = tool.history_buckets(model["history"], 56, model["history_window"])
        self.assertGreaterEqual(sum(a != b for a, b in zip(values, values[1:])), 5)
        self.assertGreaterEqual(max(values) - min(values), 3)


class LaneContextDemoTests(DemoCase):
    def test_frozen_scene_shows_structured_request_and_historical_lane_context(self) -> None:
        _, model = load_demo().load_scene()
        tool = _view.current()
        canvas = tool.Canvas(156, 70)
        tool.dashboard_draw(canvas, model, cwd="~/straylight")
        text = canvas.render(False)
        self.assertRegex(text, r"ticket\s+#644")
        self.assertRegex(text, r"phase\s+check")
        self.assertRegex(text, r"stage\s+active")
        self.assertRegex(text, r"receipt\s+in progress")
        self.assertNotIn("class=normal", text)
        self.assertIn("https://github.com/maxvyshniakov/straylight/issues/644", canvas.render(True))
        with patch("godfather.adapters.lanefather.fetch", side_effect=AssertionError("demo must stay offline")):
            tool.enrich_details(model["holders"][0])
        self.assertTrue(Path(ROOT / "tools/fixtures/navigation.json").is_file())


if __name__ == "__main__":
    unittest.main()
