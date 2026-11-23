"""A lane's spawns (fathers#171): the worktree and folders a lane owns through Spawnfather join that lane by project
and id, wherever they are, on SPAWNS (the lane column, SPAWN DETAILS' lane link) and in LANE DETAILS. A lane id
alone never crosses projects."""
from __future__ import annotations

import copy
from typing import Any, cast
import unittest

from test_lanes import GRID, STRAYLIGHT, Case, decoded as lanes_decoded
from test_procs import snapshot as procs_snapshot

from godfather import state as state_module
from godfather.adapters import lanefather, spawnfather
from godfather.model import Lanes, Processes

CHECKOUT = "/fixture/straylight"  # Straylight's main checkout: the project its lane owners name
LANE = "644-build-once"  # a lane id both Straylight and data-grid have


def lane_owned(entry_id: str, kind: str, path: str, project: str = CHECKOUT, lane: str = LANE,
               label: str | None = None) -> dict[str, Any]:
    """A `status --json` row of a resource a lane owns (no TTL, no process, created from the main checkout)."""
    return {"id": entry_id, "state": "running", "owner": f"lane {project}/{lane}", "session": None, "agent": None,
            "lane": lane, "owner_kind": "lane", "lane_project": project, "owner_alive": None, "age": 600,
            "ttl": None, "ttl_left": 0, "no_ttl": True, "keep": "owner", "pid": None, "pgid": None,
            "processes": None, "label": label, "mode": "resource", "cwd": project, "argv": None, "log": None,
            "end": None, "exit": None, "resource": {"kind": kind, "id": path, "name": path.rsplit("/", 1)[-1]}}


def spawns(*rows: dict[str, Any]) -> Processes:
    raw = copy.deepcopy(procs_snapshot())
    raw["entries"] = [*raw["entries"], *rows]
    return spawnfather.decode(raw)


def lanes(checkouts: bool = True) -> Lanes:
    """The lanes fixture, each project naming its main checkout (a Lanefather with `checkout`) or none (older)."""
    found = lanes_decoded()
    for project in found["projects"]:
        project["checkout"] = project["state_dir"].removesuffix("/lanes") if checkouts else None
        for lane in project["lanes"]:
            lane["checkout"] = project["checkout"]
    return found


def lane(decoded: Lanes, state_dir: str, lane_id: str = LANE) -> dict[str, Any]:
    found = lanefather.find(decoded, (state_dir, lane_id))
    assert found is not None
    return cast(dict[str, Any], found)


OWNED = (lane_owned("jwt0001", "worktree", f"{CHECKOUT}/worktrees/{LANE}", label="lane worktree"),
         lane_owned("jdir001", "dir", f"/fixture/state/{LANE}.scratch", label="lane scratch"))


class JoinTests(unittest.TestCase):
    def test_a_lane_owners_entries_join_by_project_and_id_wherever_they_are(self) -> None:
        found = spawns(*OWNED)
        mine = spawnfather.of_lane(lane(lanes(), STRAYLIGHT), found)
        self.assertEqual(["jwt0001", "jdir001"], [entry["id"] for entry in mine],
                         "the scratch folder outside the worktree joins too")
        self.assertEqual([], spawnfather.of_lane(lane(lanes(), GRID), found), "the same lane id in data-grid")
        by_id = {entry["id"]: entry for entry in spawnfather.entries(found)}
        self.assertEqual(("lane", CHECKOUT), (by_id["jwt0001"]["owner_kind"], by_id["jwt0001"]["lane_project"]))
        joined = spawnfather.lane_of(by_id["jdir001"], lanes())
        self.assertEqual((STRAYLIGHT, LANE), (joined or {}).get("key"))

    def test_another_projects_lane_owner_with_the_same_id_never_joins(self) -> None:
        other = lane_owned("jgrid01", "worktree", f"/fixture/data-grid/worktrees/{LANE}", project="/fixture/data-grid")
        found = spawns(other)
        self.assertEqual([], spawnfather.of_lane(lane(lanes(), STRAYLIGHT), found))
        self.assertEqual(["jgrid01"], [entry["id"] for entry in spawnfather.of_lane(lane(lanes(), GRID), found)])

    def test_beside_an_older_lanefather_the_worktree_names_the_project(self) -> None:
        older = lanes(checkouts=False)
        moved = lane_owned("jwt0002", "worktree", f"/fixture/straylight/worktrees/{LANE}", project="/elsewhere/straylight")
        scratch = lane_owned("jdir002", "dir", "/tmp/scratch", project="/elsewhere/straylight")
        found = spawns(*OWNED, moved, scratch)
        self.assertEqual(["jwt0001", "jdir001", "jwt0002"],
                         [entry["id"] for entry in spawnfather.of_lane(lane(older, STRAYLIGHT), found)],
                         "the project holding the lane's worktree, or the worktree itself; never a stray folder")
        self.assertEqual([], spawnfather.of_lane(lane(older, GRID), found))

    def test_a_tagged_entry_keeps_the_directory_rule_and_its_project_must_match(self) -> None:
        tagged = dict(procs_snapshot()["entries"][0], id="jtag001", lane=LANE, lane_project="/fixture/data-grid",
                      cwd=f"/fixture/straylight/worktrees/{LANE}")
        untagged = dict(tagged, id="jtag002", lane_project=None)
        found = spawns(tagged, untagged)
        self.assertEqual(["jtag002"], [entry["id"] for entry in spawnfather.of_lane(lane(lanes(), STRAYLIGHT), found)])
        outside = dict(untagged, id="jtag003", cwd="/fixture/straylight")
        self.assertEqual([], spawnfather.of_lane(lane(lanes(), STRAYLIGHT), spawns(outside)),
                         "an agent's process outside the lane's worktree is not the lane's")

    def test_the_lane_owners_spawns_come_first(self) -> None:
        tagged = dict(procs_snapshot()["entries"][0], id="jtag001", lane=LANE, cwd=f"{CHECKOUT}/worktrees/{LANE}")
        found = spawns(tagged, *OWNED)
        self.assertEqual(["jwt0001", "jdir001", "jtag001"],
                         [entry["id"] for entry in spawnfather.of_lane(lane(lanes(), STRAYLIGHT), found)])


class ScreenTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.tools = {"lanefather": state_module.Tool("lanefather", "ok", None, lanes()),
                      "spawnfather": state_module.Tool("spawnfather", "ok", None, spawns(*OWNED),
                                                       actions=("reap", "extend"))}

    def test_lane_details_list_the_lanes_worktree_and_folder(self) -> None:
        state = self.keys(self.screen(), "2")
        self.assertEqual((STRAYLIGHT, LANE), self.picked(state))
        screen = self.text()
        self.assertIn("LANE DETAILS", screen)
        self.assertRegex(screen, r"spawns\s+lane worktree · running · —")
        self.assertIn("lane scratch · running · —", screen)
        state = self.keys(state, "G")  # data-grid's lane of the same id
        self.assertEqual((GRID, LANE), self.picked(state))
        self.assertNotIn("lane worktree", self.text())
        self.assertRegex(self.text(), r"spawns\s+none owned")

    def test_spawns_fills_the_lane_column_and_links_the_lane(self) -> None:
        state = self.keys(self.screen(), "3")
        rows = state_module.page_rows(state, "spawns")
        wanted = next(number for number, row in enumerate(rows) if row.get("key") == ("entry", "jwt0001"))
        state = self.keys(state, *(["j"] * (wanted - (state_module.page_index(state, "spawns") or 0))))
        self.assertEqual(("entry", "jwt0001"), rows[state_module.page_index(state, "spawns") or 0]["key"])
        table = self.canvas.tables["spawns"]
        lines = ["".join(str(cell[0]) for cell in row[table["x"] + 1:table["x"] + table["width"] - 1])
                 for row in self.canvas.cells]
        row_text = next(line for line in lines if "lane worktree" in line)
        self.assertIn(LANE[:10], row_text, "the lane column")
        screen = self.text()
        self.assertIn(f"{LANE} · Straylight", screen, "SPAWN DETAILS links the lane, not data-grid's")
        self.assertNotIn(f"{LANE} · data-grid", screen)

if __name__ == "__main__":
    unittest.main()
