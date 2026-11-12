"""The machine-wide status snapshot (lanefather.status) over the project registry (lanefather.registry)."""
from __future__ import annotations

import datetime
import json
import pathlib
import shutil
import subprocess
import sys
from typing import Any

from support import LaneCase


class StatusTests(LaneCase):
    # End to end: every command as its own process (fathers#95).
    process_cli = True

    def status(self) -> dict[str, Any]:
        return json.loads(self.lane("status", "--json", cwd=self.tmp))

    def registry(self) -> dict[str, Any]:
        return json.loads(pathlib.Path(self.env["LANEFATHER_REGISTRY"]).read_text())

    def second_project(self) -> tuple[pathlib.Path, pathlib.Path]:
        """Another repository with its own state directory and a lane of the same id as the first project's."""
        other = self.tmp / "other"
        other.mkdir()
        self.git(other, "init", "-q", "-b", "main")
        self.git(other, "config", "user.name", "Lane Test")
        self.git(other, "config", "user.email", "lane@example.com")
        self.write(other, ".lanefather.toml", f'project = "Other"\nstate_dir = "{self.tmp / "other-state"}"\n')
        self.write(other, "lib/api.txt", "foo\n")
        self.git(other, "add", ".")
        self.git(other, "commit", "-qm", "base")
        self.git(other, "remote", "add", "origin", "git@github.com:fixture/other.git")
        environment = dict(self.env)
        del environment["LANEFATHER_STATE"]
        subprocess.run([sys.executable, "-m", "lanefather", "start", "a", "--ticket", "7", "--claims", "lib/**"], cwd=other,
                       env=environment, check=True, capture_output=True)
        return other, self.tmp / "other-state"

    def test_two_registered_projects_are_reported_and_a_lane_id_in_both_stays_distinct(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\na\n"})
        self.lane("check", "a")
        _, other_state = self.second_project()
        found = self.status()
        self.assertEqual((found["v"], found["registry"]), (1, self.env["LANEFATHER_REGISTRY"]))
        projects = {project["project"]: project for project in found["projects"]}
        self.assertEqual(set(projects), {"Fixture", "Other"})
        fixture, other = projects["Fixture"], projects["Other"]
        self.assertEqual((fixture["state_dir"], other["state_dir"]), (str(self.state), str(other_state)))
        [mine], [theirs] = fixture["lanes"], other["lanes"]
        self.assertEqual((mine["id"], theirs["id"]), ("a", "a"))
        self.assertEqual((mine["status"], theirs["status"]), ("active", "planned"))
        self.assertEqual((fixture["active"], other["planned"]), (1, 1))
        self.assertEqual(mine["receipt"]["status"], "green")
        self.assertIs(mine["receipt"]["tree_match"], True)
        self.assertEqual(mine["review"], {"status": "not started", "open": 0, "findings": 0})
        self.assertIn("check", mine["logs"])
        self.assertTrue(mine["logs"]["check"].endswith(".log"))
        self.assertEqual(theirs["ticket"], {"number": "7", "url": "https://github.com/fixture/other/issues/7"})
        self.assertEqual(other["repository"], "fixture/other")
        self.assertIsNone(mine["outcome"])
        self.assertIsNone(fixture["gate"])
        self.assertIsInstance(fixture["cap"], int)
        # A new commit on the lane after its receipt: the tree no longer matches.
        self.commit(worktree, {"lib/api.txt": "foo\na\nb\n"})
        again = {project["project"]: project for project in self.status()["projects"]}
        self.assertIs(again["Fixture"]["lanes"][0]["receipt"]["tree_match"], False)

    def test_a_broken_project_gives_an_error_entry_and_an_old_missing_one_is_dropped(self):
        self.start("a", "lib/**")
        other, _ = self.second_project()
        (other / ".lanefather.toml").write_text("this is = not toml [")
        found = {project["project"]: project for project in self.status()["projects"]}
        self.assertEqual(found["Fixture"]["lanes"][0]["id"], "a")
        self.assertIn("error", found["Other"])
        self.assertIn(".lanefather.toml", found["Other"]["error"])
        # Gone for more than 7 days: no longer reported, and the next registration forgets it.
        shutil.rmtree(other)
        registry = self.registry()
        for entry in registry["projects"]:
            if entry["project"] == "Other":
                entry["seen"] = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=8)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
        pathlib.Path(self.env["LANEFATHER_REGISTRY"]).write_text(json.dumps(registry))
        self.assertEqual([project["project"] for project in self.status()["projects"]], ["Fixture"])
        self.start("b", "docs/**")
        self.assertEqual([entry["project"] for entry in self.registry()["projects"]], ["Fixture"])

    def test_status_reads_without_registering_or_writing(self):
        self.lane("status", "--json", cwd=self.repo)
        self.assertFalse(pathlib.Path(self.env["LANEFATHER_REGISTRY"]).exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.status()["projects"], [])

    def test_a_command_outside_a_repository_registers_nothing(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        self.lane("list", cwd=outside)
        self.assertFalse(pathlib.Path(self.env["LANEFATHER_REGISTRY"]).exists())

    def test_the_capabilities_advertise_the_snapshot(self):
        block = json.loads(self.lane("capabilities", "--json"))["dashboard"]
        self.assertEqual(block["snapshot"], {"argv": ["status", "--json"], "versions": [1], "poll": 5})

    def test_the_newest_batch_gate_is_reported(self):
        self.start("a", "lib/**")
        for sha, status, finished in (("a" * 40, "green", "2026-11-12T01:00:00Z"),
                                      ("b" * 40, "red", "2026-11-12T02:00:00Z")):
            (self.state / f"promote-{sha[:10]}.gate.json").write_text(
                json.dumps({"sha": sha, "status": status, "finished": finished}))
        self.assertEqual(self.status()["projects"][0]["gate"],
                         {"sha": "b" * 40, "status": "red", "at": "2026-11-12T02:00:00Z"})

