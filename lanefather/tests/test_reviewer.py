"""The reviewer crosses the same explicit project interface from direct and CLI callers."""
from __future__ import annotations

import os
import unittest.mock

from support import LaneCase

from lanefather import config, reviewer


class ReviewerProjectTests(LaneCase):
    def test_the_review_request_uses_its_project_when_another_project_is_current(self) -> None:
        tip = self.tip()
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            configured = config.Config(project="Selected project", coding_standards="SELECTED.md")
            runner = reviewer.Reviewer(configured)
            config.use(config.Config(project="Other project", coding_standards="OTHER.md"))
            self.addCleanup(config.use, None)
            answer = runner.run("direct", str(self.integration),
                                reviewer.Request("selected standards", "selected ticket", tip, tip),
                                self.tmp / "answer.md", self.tmp / "reviewer.log")
        self.assertIsNone(answer.error)
        self.assertEqual(answer.evidence["findings"], [])
        prompt = (self.tmp / "codex.log.prompt").read_text()
        for expected in ("Selected project", "SELECTED.md", "selected standards", "selected ticket"):
            self.assertIn(expected, prompt)
        self.assertNotIn("Other project", prompt)
        self.assertFalse(self.state.exists(), "the reviewer does not read or write lane state")
