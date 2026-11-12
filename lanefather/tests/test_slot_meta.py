"""The run type on every slot request (fathers#107): phase, plan (the results store's key of the plan, fathers#91)
and checks, so Slotfather keeps expected durations per run type (fathers#106)."""
from __future__ import annotations

import json
import pathlib
from typing import Any
import unittest

from support import LaneCase


def plan_key(selection: Any) -> str:
    from lanefather import results
    return results.plan_hash(selection)


class SlotMetaTests(LaneCase):
    def requests(self) -> list[dict[str, Any]]:
        """The meta each verify run that queued itself handed slotfather, in order."""
        path = pathlib.Path(str(self.verify_log) + ".requests")
        return [json.loads(line)["meta"] for line in path.read_text().splitlines()] if path.exists() else []

    def wrapped(self) -> list[dict[str, str]]:
        """The --meta of each `slotfather run` lanefather started itself (reruns, promote steps)."""
        log = self.tmp / "slotfather.log"
        found: list[dict[str, str]] = []
        for line in log.read_text().splitlines() if log.exists() else []:
            call = json.loads(line)
            if call["command"] != "run":
                continue
            args = call["args"]
            found.append(dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--meta"))
        return found

    def test_a_lane_check_and_its_smoke_name_their_plan_and_check_count(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        check = self.requests()[-1]
        self.assertEqual((check["phase"], check["plan"], check["checks"]),
                         ("check", plan_key(["structure-check", "lib-check"]), "2"))
        self.integration_moves()
        self.lane("land", "a")
        smoke = self.requests()[-1]
        self.assertEqual(smoke["phase"], "post-merge")
        self.assertEqual(smoke["plan"], plan_key(["structure-check", "lib-check"]))
        self.assertEqual(smoke["checks"], "2")

    def test_a_rerun_names_the_plan_of_the_checks_it_reruns(self):
        flag = self.tmp / "flake-once"
        flag.touch()
        self.env["FIXTURE_FLAKE_ONCE"] = f"lib-check:{flag}"
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        rerun = self.wrapped()[-1]
        self.assertEqual((rerun["phase"], rerun["plan"], rerun["checks"]), ("rerun", plan_key(["lib-check"]), "1"))

    def test_the_batch_gate_names_the_full_plan_and_the_trees_checks(self):
        self.git(self.repo, "branch", "dev")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/extra.txt": "a\n"})
        self.lane("check", "a")
        self.lane("land", "a")
        self.lane("review", "a")
        self.lane("promote", self.tip(), "--no-push", "--wait", "0")
        gate = self.requests()[-1]
        # The fixture's plan module defines five checks.
        self.assertEqual((gate["phase"], gate["plan"], gate["checks"]), ("gate", plan_key("full"), "5"))

    def test_a_tree_whose_plan_names_whole_suites_counts_their_checks(self):
        self.env["FIXTURE_SUITE_PLAN"] = "1"
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        check = self.requests()[-1]
        self.assertEqual((check["plan"], check["checks"]), (plan_key(["suite:structure", "suite:lib"]), "3"))


if __name__ == "__main__":
    unittest.main()
