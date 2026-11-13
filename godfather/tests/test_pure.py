"""Views are functions of the state (fathers#138): the reducer puts the moment and the selected entry's lane into the
state, the theme and marks come with the canvas, so one state draws the same picture every time, in any theme, and
drawing changes nothing in it."""
from __future__ import annotations

import copy
import datetime
import json
import re
import unittest
from typing import Any

import view as _view
from helpers import ROOT
from test_goldens import goldens_tool

VIEWS = ROOT / "src" / "godfather" / "views"


class PureRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = _view.fresh()
        self.addCleanup(_view.fresh)

    def scene_state(self) -> tuple[Any, Any, dict[str, Any]]:
        """The golden scene as the live view holds it: loaded, its first lease's lane loaded, at the scene's moment."""
        tool, goldens = self.tool, goldens_tool()
        scene = json.loads((goldens.SCENE / "scene.json").read_text())
        with goldens.environment(None):
            model, error = tool.Screen().load()
        self.assertIsNone(error)
        assert model is not None
        moment = tool.parse_time(scene["moment"])
        state, _ = tool.reduce(tool.AppState(), tool.ModelLoaded(model, 8), 0.0)
        state, _ = tool.reduce(state, tool.Tick(moment), 0.0)
        first = state.model["holders"][0]
        lane = scene["lanes"][goldens.lane_key(first["key"])]
        key = tool.details_key(first, state.selection.key)
        state, _ = tool.reduce(state, tool.DetailsLoaded(key, lane, "~/straylight"), 0.0)
        return state, moment, lane

    def draw(self, state: Any, theme: Any, marks: Any = None) -> Any:
        canvas = self.tool.Canvas(156, 43, theme=theme, marks=marks)
        self.tool.render(canvas, state, 0.0)
        return canvas

    def test_one_state_draws_the_same_picture_twice_in_two_themes_and_stays_as_it_was(self) -> None:
        from godfather import theme

        state, moment, lane = self.scene_state()
        self.assertEqual(moment, state.moment, "the reducer holds the moment the views draw at")
        self.assertEqual(lane, state.model["holders"][0]["lane"], "the reducer joins the loaded lane into the model")
        before = copy.deepcopy(state)
        dark = theme.resolve("auto", environ={"GODFATHER_TERMINAL_BG": "#111213", "GODFATHER_TERMINAL_FG": "#cacecd"})
        light = theme.resolve("auto", environ={"GODFATHER_TERMINAL_BG": "#ffffff", "GODFATHER_TERMINAL_FG": "#191919"})
        self.assertNotEqual(dark.styles, light.styles)
        first, other, again = self.draw(state, dark), self.draw(state, light), self.draw(state, dark)
        self.assertEqual(first.render(True), again.render(True), "the same state, the same escape sequences")
        self.assertNotEqual(first.render(True), other.render(True), "each canvas draws in its own theme")
        self.assertEqual(first.render(False), other.render(False), "the same picture in either theme")
        self.assertEqual(before, state, "drawing changes nothing in the state")
        self.assertIn("644-build-once", first.render(False), "DETAILS shows the lane the reducer joined")

    def test_marks_come_with_the_canvas(self) -> None:
        tool = self.tool
        state, _, _ = self.scene_state()
        emoji = self.draw(state, None).render(False)
        plain = self.draw(state, None, tool.marks_for(True)).render(False)
        self.assertIn("▶️", emoji)
        self.assertNotIn("▶️", plain)
        self.assertEqual(emoji, self.draw(state, None).render(False), "an ASCII canvas leaves the next one as it was")

    def test_the_moment_is_the_states_not_the_clock(self) -> None:
        state, moment, _ = self.scene_state()
        self.assertIn(" 35m10s ", self.draw(state, None).render(False), "the first lease's run at the scene's moment")
        later, _ = self.tool.reduce(state, self.tool.Tick(moment + datetime.timedelta(minutes=5)), 0.0)
        self.assertIn(" 40m10s ", self.draw(later, None).render(False), "five minutes later by the state's moment")

    def test_the_state_keeps_the_moment_to_the_second(self) -> None:
        state, moment, _ = self.scene_state()
        within, _ = self.tool.reduce(state, self.tool.Tick(moment + datetime.timedelta(milliseconds=900)), 0.0)
        self.assertIs(state, within, "a pass within the same second changes nothing, so nothing is drawn again")
        next_second, _ = self.tool.reduce(state, self.tool.Tick(moment + datetime.timedelta(seconds=1.4)), 0.0)
        self.assertEqual(moment + datetime.timedelta(seconds=1), next_second.moment, "cut to the second")

    def test_a_selected_waiters_lane_is_joined_into_a_copy_of_the_model(self) -> None:
        tool = self.tool
        state, _, lane = self.scene_state()
        state = tool.move(state, len(state.model["holders"]))  # the first waiter
        before = state.model
        waiter = before["waiters"][0]
        self.assertNotIn("lane", waiter)
        key = tool.details_key(waiter, state.selection.key)
        self.assertEqual("request", key[1])
        state, _ = tool.reduce(state, tool.DetailsLoaded(key, lane, None), 0.0)
        self.assertEqual(lane, state.model["waiters"][0]["lane"])
        self.assertNotIn("lane", before["waiters"][0], "the model drawn before is left as it was")
        self.assertIs(before["holders"], state.model["holders"], "the other group is shared, not copied")

    def test_no_view_reads_the_clock_or_a_theme_global(self) -> None:
        # The Settings panel is a state machine of its own (state.SettingsOverlay), its clock injected and
        # time.monotonic only by default: no drawing reads it
        for path in sorted(set(VIEWS.glob("*.py")) - {VIEWS / "settings.py"}):
            text = path.read_text()
            with self.subTest(path.name):
                self.assertIsNone(re.search(r"^\s*(import time|from time import)", text, re.M))
                self.assertNotIn("datetime.now(", text)
                self.assertNotIn("date.today(", text)
                self.assertNotIn("current_theme", text)
                self.assertNotIn("use_marks", text)

    def test_the_golden_tool_patches_nothing_but_the_environment(self) -> None:
        text = (ROOT / "tools" / "goldens.py").read_text()
        patches = re.findall(r"mock\.patch[\w.]*\(\w*", text)
        self.assertTrue(patches)
        self.assertEqual({"mock.patch.dict(os"}, set(patches), "only os.environ is set: no module, function or clock")
        self.assertNotIn("set_theme", text)


if __name__ == "__main__":
    unittest.main()
