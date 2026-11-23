"""A Store several repositories share (fathers#236) and lanes whose branch and worktree are gone (fathers#237), in
scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import types
from typing import Any
import unittest
from unittest import mock

from support import LaneCase, template_repository


class SharedStoreTests(LaneCase):
    """fathers#236: `list` shows the repository it runs in; a lane command refuses another repository's lane."""

    def setUp(self) -> None:
        super().setUp()
        self.other = self.tmp / "sid-kit"
        shutil.copytree(template_repository(), self.other, symlinks=True)

    def foreign(self, lane_id: str, status: str = "active") -> None:
        """A lane of the other repository, in the shared Store."""
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / f"{lane_id}.json").write_text(json.dumps({
            "id": lane_id, "ticket": "7", "branch": f"lane/{lane_id}", "base": "main", "base_sha": "0" * 40,
            "claims": ["other/**"], "after": [], "common_dir": os.path.realpath(self.other / ".git"),
            "integration": None, "worktree": None, "status": status,
            "created": "2026-11-23T09:00:00Z", "scratch": [], "overrides": [], "rules": []}))

    def test_list_shows_this_repositorys_lanes_and_all_projects_shows_every_one_labelled(self):
        self.start("here", "lib/**")
        self.foreign("sk-1")
        listed = self.lane("list")
        self.assertIn("here  planned", listed)
        self.assertNotIn("sk-1", listed)
        lanes = json.loads(self.lane("list", "--json"))["lanes"]
        self.assertEqual([(lane["id"], lane["project"]) for lane in lanes], [("here", "straylight")])
        there = self.lane("list", cwd=self.other)
        self.assertIn("sk-1  active", there)
        self.assertNotIn("here", there)
        everything = self.lane("list", "--all-projects")
        self.assertRegex(everything, r"here  planned .*project: straylight")
        self.assertRegex(everything, r"sk-1  active .*project: sid-kit")
        lanes = json.loads(self.lane("list", "--all-projects", "--json"))["lanes"]
        self.assertEqual(sorted((lane["id"], lane["project"]) for lane in lanes),
                         [("here", "straylight"), ("sk-1", "sid-kit")])

    def test_a_lane_command_refuses_another_repositorys_lane_and_names_where_to_run_it(self):
        self.foreign("sk-1")
        before = self.record("sk-1")
        for command in (("wait", "sk-1", "--for", "landed", "--timeout", "0"), ("drop", "sk-1"), ("done", "sk-1"),
                        ("land", "sk-1"), ("review", "sk-1"), ("check", "sk-1"), ("recheck", "sk-1"),
                        ("keep", "sk-1"), ("scope", "sk-1", "--add-partial", "8")):
            with self.subTest(command[0]):
                output = self.lane(*command, ok=False)
                self.assertIn("lane sk-1 belongs to sid-kit", output)
                self.assertIn(f"run it there ({os.path.realpath(self.other)})", output)
        self.assertEqual(self.record("sk-1"), before)
        self.assertNotIn("belongs to", self.lane("wait", "sk-1", "--for", "registered", "--timeout", "0",
                                                 cwd=self.other))

    def test_show_reads_another_repositorys_lane_but_not_with_this_project_dir(self):
        """show is read-only and names the lane's project (from its configuration), so it reads any lane."""
        self.foreign("sk-1")
        shown = json.loads(self.lane("show", "sk-1", "--json"))
        self.assertEqual((shown["id"], shown["project"]), ("sk-1", "Fixture"))
        self.assertIn("lane belongs to another repository",
                      self.lane("show", "sk-1", "--project-dir", str(self.repo), ok=False))


class GoneLaneTests(LaneCase):
    """fathers#237: a planned or active lane whose branch and worktree are gone."""

    def gone(self, lane_id: str, *claims: str) -> None:
        worktree = self.created(lane_id, *claims)
        self.git(self.repo, "worktree", "remove", "--force", worktree)
        self.git(self.repo, "branch", "-D", f"lane/{lane_id}")

    def listed(self) -> dict[str, dict[str, Any]]:
        return {lane["id"]: lane for lane in json.loads(self.lane("list", "--json"))["lanes"]}

    def test_list_show_and_status_mark_a_gone_lane(self):
        self.gone("g", "lib/**")
        self.start("fresh", "docs/**")
        self.assertRegex(self.lane("list"), r"g  active .*gone: no branch or worktree")
        self.assertIn("1 gone lane holds the cap and its claims: lanefather drop g", self.lane("list"))
        listed = self.listed()
        self.assertEqual((listed["g"]["gone"], listed["fresh"]["gone"]), (True, False))
        self.assertTrue(json.loads(self.lane("show", "g", "--json"))["gone"])
        self.assertIn("gone: no branch or worktree", self.lane("show", "g"))
        lanes = {lane["id"]: lane for project in json.loads(self.lane("status", "--json"))["projects"]
                 for lane in project.get("lanes", [])}
        self.assertEqual((lanes["g"]["gone"], lanes["fresh"]["gone"]), (True, False))
        self.assertRegex(self.lane("status"), r"g  active .*gone")

    def test_start_at_the_cap_names_the_gone_lanes_and_the_drop_that_frees_them(self):
        self.gone("g1", "a/**")
        self.gone("g2", "b/**")
        self.start("live", "c/**")
        output = self.start("next", "d/**", ok=False)
        self.assertIn("3 lanes are planned or active in straylight (g1, g2, live)", output)
        self.assertIn("g1, g2 have no branch or worktree left: lanefather drop g1 g2 frees them", output)

    def test_start_overlapping_a_gone_lane_names_the_drop_not_after(self):
        self.gone("g", "lib/**")
        output = self.start("next", "lib/**", ok=False)
        self.assertIn("claims overlap lane g (#404)", output)
        self.assertIn("g has no branch or worktree left: lanefather drop g frees its claims", output)
        self.assertNotIn("--after g", output)

    def test_drop_takes_several_ids(self):
        self.gone("g1", "a/**")
        self.gone("g2", "b/**")
        output = self.lane("drop", "g1", "g2")
        self.assertIn("Lane g1 dropped (no branch and no commits)", output)
        self.assertIn("Lane g2 dropped (no branch and no commits)", output)
        self.assertEqual((self.record("g1")["status"], self.record("g2")["status"]), ("done", "done"))

    def test_gc_drops_a_gone_lane_and_dry_run_only_reports_it(self):
        self.gone("g", "lib/**")
        self.assertIn("would drop lane g: its branch and worktree are gone", self.lane("gc", "--dry-run"))
        self.assertEqual(self.record("g")["status"], "active")
        dry = json.loads(self.lane("gc", "--dry-run", "--json"))
        self.assertIn({"kind": "lane", "path": "g"}, dry["removed"])
        self.assertIn("dropped lane g: its branch and worktree are gone", self.lane("gc"))
        self.assertEqual(self.record("g")["status"], "done")
        self.start("next", "lib/**")

    def test_drop_and_gc_keep_a_gone_lane_whose_review_has_unmarked_findings(self):
        from lanefather import review_record
        self.gone("g", "lib/**")
        reviewed = types.SimpleNamespace(open_findings=({"n": 1}, {"n": 2}))
        with mock.patch.object(review_record.ReviewRecord, "load", return_value=reviewed):
            output = self.lane("drop", "g", ok=False)
            self.assertIn("drop refused: lane g's review has 2 unmarked findings, which hold lands", output)
            self.assertIn("lane g: drop refused", self.lane("gc", "--json", ok=None))
        self.assertEqual(self.record("g")["status"], "active")

    def test_a_lane_whose_branch_or_worktree_exists_is_not_gone(self):
        branch_only = self.created("branch-only", "a/**")
        shutil.rmtree(branch_only)
        self.git(self.repo, "worktree", "prune")
        worktree_only = self.created("worktree-only", "b/**")
        self.git(worktree_only, "checkout", "-q", "--detach")
        self.git(self.repo, "branch", "-D", "lane/worktree-only")
        self.start("planned", "c/**")
        listed = self.listed()
        self.assertEqual({lane_id: lane["gone"] for lane_id, lane in listed.items()},
                         {"branch-only": False, "worktree-only": False, "planned": False})
        self.assertNotIn("gone", self.lane("list"))
        output = self.lane("gc")
        self.assertNotIn("drop", output)
        for lane_id in listed:
            self.assertNotEqual(self.record(lane_id)["status"], "done")
        self.assertTrue(pathlib.Path(worktree_only).is_dir())


if __name__ == "__main__":
    unittest.main()
