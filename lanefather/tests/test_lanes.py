"""Tests of start, next, adopt, list and the queue (lanefather.lanes, lanefather.queue, lanefather.claims): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap
import unittest

from support import LaneCase, ScratchCase, load_lane


class ListAndCapScratchTests(ScratchCase):
    def test_a_bad_cap_falls_back_to_the_derived_cap_with_a_warning(self) -> None:
        # #454: int() at import crashed every tools/lane command on a bad LANEFATHER_CAP.
        for value in ("many", "0", "-2", "2.5"):
            self.env["LANEFATHER_CAP"] = value
            output = self.lane("list")
            self.assertIn("0 of 3 lanes planned or active", output, value)
            self.assertIn(f"LANEFATHER_CAP={value!r} is not a positive whole number; the cap is 3", output)
        self.env["LANEFATHER_CAP"] = ""
        self.assertNotIn("LANEFATHER_CAP", self.lane("list"), "an empty value is the default")

    def test_list_shows_each_runs_effective_slot_priority_and_its_source(self) -> None:
        # lanefather#24: an orchestrator demoted to batch sees it before its runs queue.
        self.assertIn("slot priorities: check normal, post-merge interactive, gate interactive, rerun interactive, "
                      "perf batch\n", self.lane("list"))
        self.env.update(LANEFATHER_PRIORITY="batch", LANEFATHER_PRIORITY_PERF="normal")
        self.assertIn("slot priorities: check batch (LANEFATHER_PRIORITY), post-merge batch (LANEFATHER_PRIORITY), "
                      "gate batch (LANEFATHER_PRIORITY), rerun batch (LANEFATHER_PRIORITY), "
                      "perf normal (LANEFATHER_PRIORITY_PERF)", self.lane("list"))
        listed = json.loads(self.lane("list", "--json"))
        self.assertEqual(listed["priorities"]["gate"], {"priority": "batch", "source": "LANEFATHER_PRIORITY"})
        self.env["LANEFATHER_PRIORITY"] = "urgent"
        self.assertIn("LANEFATHER_PRIORITY='urgent' is not a Slotfather priority", self.lane("list", ok=False))


class StartTests(LaneCase):
    """B4 start: caps, disk, path claims and load advice."""

    def test_header_names_ticket_branch_claims_and_report_contract(self):
        header = self.start("tool", "tools/lane", "tools/test-lane.py")
        for text in ("#404", "Branch: lane/tool", "integrate/q", "tools/lane", "tools/test-lane.py",
                     "lanefather check tool", "Receipt: " + str(self.state / "tool.receipt.json"), "docs/agents/orchestration.md#implementer-brief"):
            self.assertIn(text, header)
        record = self.record("tool")
        self.assertEqual((record["status"], record["ticket"], record["base"]), ("planned", "404", "integrate/q"))
        self.assertEqual(record["base_sha"], self.tip())

    def test_a_fourth_lane_is_refused_and_a_logged_override_passes(self):
        for index, claim in enumerate(("a/**", "b/**", "c/**")):
            self.start(f"l{index}", claim)
        output = self.start("l3", "d/**", ok=False)
        self.assertIn("3 lanes are planned or active", output)
        self.assertFalse((self.state / "l3.json").exists())
        self.start("l3", "d/**", extra=("--force-reason", "owner allowed a fourth lane for a docs fix"))
        self.assertIn("owner allowed a fourth lane", (self.state / "ledger.log").read_text())

    def test_the_cap_follows_lanefather_cap(self):
        self.env["LANEFATHER_CAP"] = "5"
        for index, claim in enumerate(("a/**", "b/**", "c/**", "d/**", "e/**")):
            self.start(f"l{index}", claim)
        output = self.start("l5", "f/**", ok=False)
        self.assertIn("5 lanes are planned or active", output)
        self.assertIn("the cap is 5 (LANEFATHER_CAP override; derived: 3)", output)
        # #572: the override stays, and every start logs it.
        self.assertIn("cap=5 (LANEFATHER_CAP override; derived: 3)", (self.state / "ledger.log").read_text())
        self.assertIn("5 of 5 lanes planned or active (cap LANEFATHER_CAP override; derived: 3)", self.lane("list"))

    def test_the_default_cap_is_derived_from_the_cores_and_the_memory(self):
        # #572: the orchestrator raised LANEFATHER_CAP by hand up to 16 on 12 cores while load15 reached 66.
        lane = load_lane()
        for cores, memory, cap in ((12, 32, 6), (12, 64, 6), (16, 16, 2), (24, 128, 12), (2, 8, 1), (1, 4, 1)):
            with self.subTest(cores=cores, memory=memory):
                self.assertEqual(lane.host.derived_cap(cores, memory), cap)
        self.env.update(LANEFATHER_CORES="12", LANEFATHER_MEMORY_GB="32")
        output = self.lane("list")
        self.assertIn("0 of 6 lanes planned or active (cap derived from 12 cores and 32 GB of memory)", output)
        for index, claim in enumerate(("a/**", "b/**", "c/**", "d/**", "e/**", "f/**")):
            self.start(f"l{index}", claim)
        self.assertIn("6 lanes are planned or active", self.start("l6", "g/**", ok=False))
        self.assertIn("cap=6 (derived from 12 cores and 32 GB of memory)", (self.state / "ledger.log").read_text())

    def test_low_disk_refuses_registration_and_high_load_is_advisory(self):
        self.env["LANEFATHER_DISK_FREE_GB"] = "20"
        # The derived floor (fathers#157): 40 GB reserve + a cap of 3 x the 3 GB lane peak of no done lane yet.
        self.assertIn("under the floor of 49 GB (10^9) = 40 reserve + 3 x 3.0 GB lane peak",
                      self.start("disk", "a/**", ok=False))
        self.env["LANEFATHER_DISK_FREE_GB"] = "500"
        self.env["LANEFATHER_LOAD15"] = "9.1"
        output = self.start("load", "a/**", extra=("--create",))
        self.assertIn("load15 is 9.1, above the advisory limit of 9.0", output)
        self.assertIn("Checks may wait for a Slotfather slot.", output)
        self.assertEqual(self.record("load")["status"], "active")
        self.assertEqual(self.record("load")["overrides"], [])

    def test_overlapping_claims_are_refused_naming_the_holder(self):
        # Replay: #401 held desktop/src/states/** while another lane edited StatesPage's directory.
        self.start("401", "desktop/src/states/**")
        for claim in ("desktop/src/states/StatesPage.tsx", "desktop/src/**/*.tsx", "desktop/src/states"):
            with self.subTest(claim):
                output = self.start("other", claim, ok=False)
                self.assertIn("401", output)
                self.assertIn("desktop/src/states/**", output)
        self.assertIn("401", self.start("css", "desktop/src/**/*.css", ok=False))
        self.start("disjoint", "native/**", "desktop/src/layout/**")
        self.start("top-level-css", "desktop/src/*.css")

    def test_a_declared_sequence_allows_overlap_and_lands_in_order(self):
        first = self.created("first", "lib/**")
        self.assertIn("first", self.start("second", "lib/api.txt", ok=False))
        self.start("second", "lib/api.txt", extra=("--after", "first", "--create"))
        second = self.record("second")["worktree"]
        self.assertEqual(self.record("second")["after"], ["first"])
        self.commit(second, {"lib/api.txt": "foo\nsecond\n"})
        self.lane("check", "second")
        self.assertIn("first", self.lane("land", "second", ok=False))
        self.commit(first, {"lib/extra.txt": "first\n"})
        self.lane("check", "first")
        self.lane("land", "first")
        self.lane("land", "second")

    def test_planned_lane_activates_with_start_create_after_dependency_lands(self):
        # fathers#241: a planned lane with --after stays planned after its dependency lands and can become active via
        # `start <id> --create` without repeating --ticket or --claims.
        first = self.created("first", "lib/**")
        self.lane(
            "start", "second", "--ticket", "405", "--partial", "406",
            "--base", "integrate/q", "--claims", "lib/api.txt", "--after", "first",
        )
        self.assertEqual(self.record("second")["status"], "planned")
        self.assertIsNone(self.record("second")["worktree"])

        # Duplicate start without --create refuses and does not point at `done`.
        dup = self.lane(
            "start", "second", "--ticket", "405", "--base", "integrate/q", "--claims", "lib/api.txt", ok=False,
        )
        self.assertIn("lane second exists (planned)", dup)
        self.assertNotIn("lanefather done", dup)
        self.assertIn("lanefather start second --create", dup)
        self.assertIn("lanefather drop second", dup)

        # Activating before `first` lands refuses without pointing at `done`.
        too_early = self.lane("start", "second", "--create", ok=False)
        self.assertIn("first", too_early)
        self.assertNotIn("lanefather done", too_early)
        self.assertEqual(self.record("second")["status"], "planned")

        # Land `first`, then activate `second` without repeating --ticket or --claims.
        self.commit(first, {"lib/from-first.txt": "from first\n"})
        self.lane("check", "first")
        self.lane("land", "first")
        integration_tip = self.tip()

        output = self.lane("start", "second", "--create")
        rec = self.record("second")
        self.assertEqual(rec["status"], "active")
        self.assertIsNotNone(rec["worktree"])
        self.assertEqual(rec["ticket"], "405")
        self.assertEqual(rec["scope"], [{"issue": "405"}, {"issue": "406", "partial": True}])
        self.assertEqual(rec["claims"], ["lib/api.txt"])
        self.assertEqual(rec["after"], ["first"])
        self.assertEqual(rec["base_sha"], integration_tip)
        self.assertTrue((pathlib.Path(rec["worktree"]) / "lib/from-first.txt").is_file())
        self.assertIn(f"Worktree: {rec['worktree']}", output)

        # Calling start --create again on an already-active lane refuses without pointing at `done`.
        already_active = self.lane("start", "second", "--create", ok=False)
        self.assertIn("lane second exists (active)", already_active)
        self.assertNotIn("lanefather done", already_active)

        # Calling start --create on a brand-new lane without --ticket / --claims is a CLI usage error (exit 2).
        proc = subprocess.run(
            [sys.executable, "-m", "lanefather", "start", "brand-new", "--create"],
            cwd=self.integration, env=self.env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("--ticket", proc.stderr)
        self.assertIn("--claims", proc.stderr)
        self.assertIn("Try 'lanefather start --help'.", proc.stderr)
        self.assertFalse((self.state / "brand-new.json").exists())

    def test_activating_planned_lane_refuses_mismatched_options_and_enforces_disk_floor(self):
        # Review of fathers#241: mismatched registration-only options on `start <id> --create` refuse instead of
        # being silently ignored, exact repeats are accepted, and the free-disk floor is enforced.
        self.lane(
            "start", "plan1", "--ticket", "410", "--base", "integrate/q", "--claims", "lib/api.txt",
        )
        for extra, word in (
            (("--ticket", "999"), "scope"),
            (("--partial", "410"), "scope"),
            (("--claims", "calls/**"), "--claims"),
            (("--after", "other"), "--after"),
            (("--fix-of", "other"), "--fix-of"),
            (("--spec", "docs/spec.md"), "--spec"),
        ):
            with self.subTest(extra=extra):
                err = self.lane("start", "plan1", *extra, "--create", ok=False)
                self.assertIn(word, err)
                self.assertEqual(self.record("plan1")["status"], "planned")

        # Low disk refuses activation until freed or overridden with --force-reason.
        self.env["LANEFATHER_DISK_FREE_GB"] = "20"
        disk_err = self.lane("start", "plan1", "--create", ok=False)
        self.assertIn("under the floor of", disk_err)
        self.assertEqual(self.record("plan1")["status"], "planned")

        # Repeating the exact stored options with --force-reason activates cleanly and logs the override.
        self.lane(
            "start", "plan1", "--ticket", "410", "--claims", "lib/api.txt", "--create",
            "--force-reason", "small scratch fix on low disk",
        )
        rec = self.record("plan1")
        self.assertEqual(rec["status"], "active")
        self.assertEqual(len(rec["overrides"]), 1)
        self.assertIn("small scratch fix on low disk", (self.state / "ledger.log").read_text())

    def test_repeated_after_flags_allow_all_overlaps_and_wait_for_every_lane(self):
        # fathers#241: `--after A --after B` must keep both A and B, not only the last flag.
        wt_a = self.created("sk113", "lib/**")
        wt_b = self.created("sk114", "calls/**")

        self.lane(
            "start", "sk115", "--ticket", "115", "--base", "integrate/q",
            "--claims", "lib/api.txt", "calls/x.txt",
            "--after", "sk113", "--after", "sk114",
        )
        rec = self.record("sk115")
        self.assertEqual(rec["status"], "planned")
        self.assertEqual(rec["after"], ["sk113", "sk114"])

        # Neither has landed yet.
        wait_both = self.lane("start", "sk115", "--create", ok=False)
        self.assertIn("sk113", wait_both)
        self.assertIn("sk114", wait_both)

        # Land sk113 only; sk115 must still wait for sk114.
        self.commit(wt_a, {"lib/from-113.txt": "113\n"})
        self.lane("check", "sk113")
        self.lane("land", "sk113")
        wait_b = self.lane("start", "sk115", "--create", ok=False)
        self.assertNotIn("sk113", wait_b)
        self.assertIn("sk114", wait_b)
        self.assertEqual(self.record("sk115")["status"], "planned")

        # Land sk114; now sk115 activates with both landed changes in its worktree.
        self.commit(wt_b, {"calls/from-114.txt": "foo\n"})
        self.lane("check", "sk114")
        self.lane("land", "sk114")
        self.lane("start", "sk115", "--create")
        rec = self.record("sk115")
        self.assertEqual(rec["status"], "active")
        self.assertEqual(rec["claims"], ["lib/api.txt", "calls/x.txt"])
        self.assertEqual(rec["ticket"], "115")
        self.assertTrue((pathlib.Path(rec["worktree"]) / "lib/from-113.txt").is_file())
        self.assertTrue((pathlib.Path(rec["worktree"]) / "calls/from-114.txt").is_file())

        # Space-separated and repeated --after both work and deduplicate in order.
        self.created("sk116", "docs/**")
        self.lane(
            "start", "sk117", "--ticket", "117", "--base", "integrate/q",
            "--claims", "docs/a.md", "lib/api.txt",
            "--after", "sk115", "sk116", "--after", "sk115",
        )
        self.assertEqual(self.record("sk117")["after"], ["sk115", "sk116"])


class ProjectCapTests(LaneCase):
    """fathers#149: the lane cap counts the lanes of one repository; another project's lanes in the same state
    directory count only toward an explicit global ceiling."""

    def foreign(self, *ids: str, claims: tuple[str, ...] = ("a/**",)) -> None:
        """Active lanes of another repository in the shared state directory, claiming the paths the tests claim."""
        self.state.mkdir(parents=True, exist_ok=True)
        for lane_id in ids:
            (self.state / f"{lane_id}.json").write_text(json.dumps({
                "id": lane_id, "ticket": "7", "branch": f"lane/{lane_id}", "base": "main", "base_sha": "0" * 40,
                "claims": list(claims), "after": [], "common_dir": str(self.tmp / "data-grid" / ".git"),
                "integration": None, "worktree": str(self.tmp / "data-grid" / lane_id), "status": "active",
                "created": "2026-11-13T09:00:00Z", "scratch": [], "overrides": [], "rules": []}))

    def test_another_projects_lanes_neither_fill_the_cap_nor_hold_claims(self):
        self.foreign("dg-1", "dg-2", "dg-3")
        self.assertIn("0 of 3 lanes planned or active", self.lane("list"))
        for index, claim in enumerate(("a/**", "b/**", "c/**")):
            self.start(f"l{index}", claim)
        listed = json.loads(self.lane("list", "--json"))
        self.assertEqual((listed["active"], listed["cap"]), (3, 3))
        self.assertEqual(listed["global"], {"active": 6, "cap": None, "cap_source": None})
        output = self.start("l3", "d/**", ok=False)
        self.assertIn("3 lanes are planned or active in straylight (l0, l1, l2); the cap is 3 (", output)
        self.assertNotIn("dg-1", output)

    def test_an_explicit_global_ceiling_counts_every_project_and_lists_them_grouped(self):
        self.foreign("dg-1", "dg-2", "dg-3")
        self.env["LANEFATHER_GLOBAL_CAP"] = "4"
        self.start("l0", "b/**")
        output = self.start("l1", "c/**", ok=False)
        self.assertIn("4 lanes are planned or active across projects (data-grid: dg-1, dg-2, dg-3; straylight: l0); "
                      "the global ceiling is 4 (LANEFATHER_GLOBAL_CAP)", output)
        self.assertNotIn("the cap is 3", output)
        self.assertIn("1 of 3 lanes planned or active (cap derived from 6 cores and 20 GB of memory); "
                      "4 of 4 across projects (LANEFATHER_GLOBAL_CAP)", self.lane("list"))
        self.start("l1", "c/**", extra=("--force-reason", "owner allowed a fifth lane"))
        for value in ("many", "0"):
            self.env["LANEFATHER_GLOBAL_CAP"] = value
            output = self.lane("list")
            self.assertIn(f"LANEFATHER_GLOBAL_CAP={value!r} is not a positive whole number; no global ceiling",
                          output)
            self.assertNotIn("across projects", output)

    def test_the_project_sets_its_cap_below_the_environment(self):
        path = self.integration / ".lanefather.toml"
        path.write_text(path.read_text() + "\n[admission]\ncap = 1\n")
        self.start("l0", "a/**")
        output = self.start("l1", "b/**", ok=False)
        self.assertIn("the cap is 1 (.lanefather.toml [admission] cap; derived: 3)", output)
        self.env["LANEFATHER_CAP"] = "2"
        self.start("l1", "b/**")
        self.assertIn("the cap is 2 (LANEFATHER_CAP override; derived: 3)", self.start("l2", "c/**", ok=False))
        self.env["LANEFATHER_CAP"] = "typo"
        self.assertIn("LANEFATHER_CAP='typo' is not a positive whole number; the cap is 1", self.lane("list"))

    def test_the_queue_ignores_another_projects_claims(self):
        self.foreign("dg-1", claims=("src/**",))
        self.write(self.state.parent, "queue/queue.toml", '[[lane]]\nid="next"\nticket=2\nclaims=["src/**"]\n')
        self.lane("next", "--start")
        self.assertEqual(self.record("next")["status"], "planned")

    def test_readiness_and_list_count_the_repository_they_run_in_not_the_configs(self):
        """Review of fathers#149: a configuration outside the repository named no repository, so every project's
        lanes counted toward this one's cap."""
        import contextlib
        from unittest.mock import patch

        from lanefather import config
        from lanefather.project import Project
        self.foreign("dg-1", "dg-2", "dg-3", claims=("other/**",))
        self.write(self.state.parent, "queue/queue.toml", '[[lane]]\nid="next"\nticket=2\nclaims=["src/**"]\n')
        elsewhere = self.write(self.tmp / "elsewhere", ".lanefather.toml",
                               (self.integration / ".lanefather.toml").read_text())
        with patch.dict("os.environ", self.env, clear=True), contextlib.chdir(self.integration):
            readiness = Project(config.load(elsewhere)).readiness()
        self.assertTrue(readiness.can_start, readiness.blocked)
        self.assertEqual(readiness.planned_or_active, ())
        # list outside any repository counts the repository its configuration is in.
        self.env["LANEFATHER_CONFIG"] = str(self.integration / ".lanefather.toml")
        self.assertIn("0 of 3 lanes planned or active", self.lane("list", cwd=self.tmp))

    def test_a_cap_in_other_digits_warns_instead_of_crashing(self):
        for name in ("LANEFATHER_CAP", "LANEFATHER_GLOBAL_CAP"):
            with self.subTest(name):
                self.env[name] = "\u00b2"
                output = self.lane("list")
                self.assertIn(f"{name}='\u00b2' is not a positive whole number", output)
                self.start(f"lane-{name[11:].lower()}", f"{name.lower()}/**")
                del self.env[name]

    def test_the_global_ceiling_tells_two_repositories_of_one_name_apart(self):
        self.foreign("dg-1")
        (self.state / "svc.json").write_text(json.dumps(dict(
            json.loads((self.state / "dg-1.json").read_text()), id="svc",
            common_dir=str(self.tmp / "other" / "data-grid" / ".git"))))
        self.env["LANEFATHER_GLOBAL_CAP"] = "2"
        output = self.start("l0", "b/**", ok=False)
        self.assertIn(f"data-grid ({self.tmp / 'data-grid'}): dg-1; "
                      f"data-grid ({self.tmp / 'other' / 'data-grid'}): svc", output)

    def test_check_widens_claims_without_warning_of_another_repositorys_lane(self):
        self.foreign("dg-1", claims=("lib/**",))
        worktree = self.created("mine", "native/**")
        self.commit(worktree, {"lib/api.txt": "foo\nmine\n"})
        output = self.lane("check", "mine")
        self.assertIn("lib/api.txt", self.record("mine")["claims"])
        self.assertNotIn("overlaps the claims of lane dg-1", output)


class BriefRulesTests(LaneCase):
    """B4 start composes the brief from the rule snippets whose paths the claims touch."""

    RULES = {
        "tools/agent-hooks/rules/ui.md": "---\ntitle: Desktop UI\npaths:\n  - desktop/src/**\n---\n"
                                         "- Screenshots in light, dark and 600 px.\n- No intent on SegmentedControl.\n",
        "tools/agent-hooks/rules/link.md": "---\ntitle: Linux Host\npaths:\n  - native/straylight-link-linux/**\n"
                                           "  - tools/straylight-link/**\n---\n- Real binary as root from empty state.\n",
        "tools/agent-hooks/rules/README.md": "# About the rules\n",
    }

    def setUp(self):
        super().setUp()
        for name, text in self.RULES.items():
            self.write(self.integration, name, text)
        self.git(self.integration, "add", "-A")
        self.git(self.integration, "commit", "-qm", "rules")

    def test_header_lists_the_rules_that_apply_to_the_claims(self):
        header = self.start("ui", "desktop/src/states/**")
        self.assertIn("Rules for these claims", header)
        self.assertIn("Desktop UI (desktop/src/**)", header)
        self.assertIn("- Screenshots in light, dark and 600 px.", header)
        self.assertIn("- No intent on SegmentedControl.", header)
        self.assertNotIn("Linux Host", header)
        self.assertEqual(self.record("ui")["rules"], ["ui"])

    def test_a_broader_claim_and_several_rules(self):
        header = self.start("wide", "desktop/**", "tools/straylight-link/deploy.sh")
        self.assertIn("Desktop UI", header)
        self.assertIn("Linux Host (tools/straylight-link/**)", header)
        self.assertIn("- Real binary as root from empty state.", header)
        self.assertEqual(self.record("wide")["rules"], ["link", "ui"])

    def test_claims_no_rule_covers_say_so(self):
        header = self.start("tool", "tools/lane")
        self.assertIn("Rules for these claims: none", header)
        self.assertEqual(self.record("tool")["rules"], [])


class AdoptAndListTests(LaneCase):
    """The Agent tool creates its own worktree; tools/lane adopts it."""

    def agent_worktree(self, name: str, branch: str) -> pathlib.Path:
        worktree = self.repo / ".claude" / "worktrees" / f"agent-{name}"
        self.git(self.repo, "worktree", "add", "-q", "-b", f"worktree-agent-{name}", str(worktree), "integrate/q")
        self.git(worktree, "checkout", "-q", "-B", branch, "integrate/q")
        return worktree

    def test_adopt_registers_an_agent_worktree(self):
        self.start("x", "lib/**")
        worktree = self.agent_worktree("x1", "lane/x")
        self.lane("adopt", "x", str(worktree), "lane/x")
        record = self.record("x")
        self.assertEqual((record["status"], record["worktree"]), ("active", str(worktree.resolve())))
        self.assertIn("refused", self.lane("adopt", "x", str(worktree), "lane/other", ok=False))

    def test_check_from_the_planned_branch_adopts_its_worktree(self):
        self.start("y", "lib/**")
        worktree = self.agent_worktree("y1", "lane/y")
        self.commit(worktree, {"lib/api.txt": "foo\ny\n"})
        self.lane("check", "y", cwd=worktree)
        self.assertEqual(self.record("y")["worktree"], str(worktree.resolve()))

    def test_every_record_has_a_worktree_and_list_json_prints_it(self):
        # #577: a planned lane's JSON had no worktree a script could read, and it built None/tools/... (10-24).
        header = self.start("x", "lib/**")
        self.assertIn('Worktree: yours; first run git checkout -B lane/x integrate/q, then lanefather adopt x "$PWD" '
                      'lane/x', header)
        self.assertIn("worktree", self.record("x"))
        created = self.created("c", "calls/**")
        worktree = self.agent_worktree("x1", "lane/x")
        # The agent adopts its worktree as its first step, as the header says, before any check.
        self.lane("adopt", "x", str(worktree), "lane/x", cwd=worktree)
        self.start("p", "docs/**")
        # A record written before the key existed.
        old = self.state / "old.json"
        old.write_text(json.dumps({k: v for k, v in self.record("p").items() if k != "worktree"} | {"id": "old",
                                                                                                    "claims": ["z/**"]}))
        listed = json.loads(self.lane("list", "--json"))
        by_id = {r["id"]: r for r in listed["lanes"]}
        self.assertEqual({lane_id: (r["status"], r["worktree"]) for lane_id, r in by_id.items()},
                         {"x": ("active", str(worktree.resolve())), "c": ("active", created),
                          "p": ("planned", None), "old": ("planned", None)})
        self.assertEqual(by_id["x"]["receipt"], "no receipt")
        self.assertEqual((listed["cap"], listed["active"]), (3, 4))
        self.assertIn("load15", listed["host"])

    def test_list_shows_lanes_claims_receipts_disk_and_load(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nz\n"})
        self.lane("check", "a")
        self.start("b", "docs/**")
        output = self.lane("list")
        for text in ("a", "lib/**", "green", "b", "planned", "docs/**", "disk", "load15"):
            self.assertIn(text, output)


class QueueTests(LaneCase):
    """#446: a ranked queue in queue/queue.toml beside the lane state, and lanefather next."""

    def queue(self, text: str) -> pathlib.Path:
        path = self.state.parent / "queue" / "queue.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    QUEUE = """\
        base = "integrate/q"

        [[lane]]
        ticket = 501
        id = "501-owner"
        claims = ["docs/**"]
        blocked = "owner question: which default"

        [[lane]]
        ticket = 502
        id = "502-later"
        claims = ["native/**"]
        after = ["500-first"]

        [[lane]]
        ticket = 503
        id = "503-overlap"
        claims = ["lib/api.txt"]

        [[lane]]
        ticket = 504
        id = "504-ready"
        claims = ["calls/**", "tools/x.py"]

        [[lane]]
        ticket = 505
        id = "505-also-ready"
        claims = ["other/**"]
        """

    def test_next_prints_the_brief_header_of_the_first_ready_entry(self):
        self.queue(self.QUEUE)
        self.start("500-first", "lib/**")
        proc = subprocess.run([sys.executable, "-m", "lanefather", "next"], cwd=self.integration, env=self.env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        header = proc.stdout
        for text in ("Lane 504-ready: ticket #504", "Branch: lane/504-ready from integrate/q", "Claims: calls/** tools/x.py",
                     "lanefather check 504-ready", "Receipt: " + str(self.state / "504-ready.receipt.json")):
            self.assertIn(text, header)
        for text in ("501-owner (#501): blocked: owner question: which default",
                     "502-later (#502): waits for 500-first (planned)",
                     "503-overlap (#503): claims overlap lane 500-first"):
            self.assertIn(text, proc.stderr)
        self.assertFalse((self.state / "504-ready.json").exists())

    def test_next_start_registers_the_lane_and_the_queue_moves_on(self):
        self.queue(self.QUEUE)
        self.start("500-first", "lib/**")
        self.assertIn("Lane 504-ready", self.lane("next", "--start"))
        self.assertEqual(self.record("504-ready")["status"], "planned")
        self.assertIn("start 504-ready ticket=504 branch=lane/504-ready base=integrate/q",
                      (self.state / "ledger.log").read_text())
        self.assertIn("504-ready (#504): registered (planned)", self.lane("next"))
        self.assertIn("Lane 505-also-ready", self.lane("next"))

    def test_an_entry_waits_until_its_after_lanes_landed(self):
        self.queue(self.QUEUE.replace('blocked = "owner question: which default"', 'blocked = "hardware"')
                   .replace('claims = ["calls/**", "tools/x.py"]', 'claims = ["lib/**"]')
                   .replace('claims = ["other/**"]', 'claims = ["lib/**"]'))
        worktree = self.created("500-first", "lib/**")
        output = self.lane("next", ok=False)
        self.assertIn("No ready entry", output)
        self.commit(worktree, {"lib/extra.txt": "x\n"})
        self.lane("check", "500-first")
        self.lane("land", "500-first")
        self.assertIn("Lane 502-later", self.lane("next"))

    def test_next_skips_an_entry_on_a_claim_check_added_until_that_lane_lands(self):
        # #574 review: the widened claims hold the queue, not only lanefather start.
        self.queue("""\
            [[lane]]
            ticket = 506
            id = "506-calls"
            claims = ["calls/**"]
            """)
        worktree = self.created("a", "lib/**")
        self.assertIn("Lane 506-calls", self.lane("next"))
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n", "calls/extra.txt": "baz\n"})
        self.lane("check", "a")
        self.assertEqual(self.record("a")["claims"], ["lib/**", "calls/extra.txt"])
        output = self.lane("next", ok=False)
        self.assertIn("506-calls (#506): claims overlap lane a: calls/** with calls/extra.txt", output)
        self.assertIn("No ready entry", output)
        self.lane("land", "a")
        self.assertIn("Lane 506-calls", self.lane("next"))

    def test_a_file_claim_is_one_path_not_a_directory_globs_reach_into(self):
        # Replay (lanefather#2): 480-busy-errors waited for hours behind false overlaps: its glob reached under the
        # file claims of 428-slice4 and 474 through imaginary paths like desktop_methods.json/ledgerX.rs.
        self.queue("""\
            [[lane]]
            ticket = 480
            id = "480-busy-errors"
            claims = ["native/straylight-core/src/runtime/engine/capacity*.rs",
                      "native/straylight-core/src/**/ledger*.rs"]
            """)
        self.start("428-slice4", "native/straylight-core/src/access/desktop_methods.json")
        self.start("474-indication", "native/straylight-core/src/keyboard/indication.rs")
        self.assertIn("Lane 480-busy-errors", self.lane("next"))

    def test_a_directory_claim_still_covers_the_files_under_it(self):
        # lanefather#2: a literal names a directory when HEAD has a tree there (whatever its name), or, for a path
        # HEAD does not have, when its last segment has no extension; a file at HEAD is one path.
        self.env["LANEFATHER_CAP"] = "6"
        self.commit(self.integration, {"desktop/App.xcodeproj/project.pbxproj": "x\n"}, "a directory with a dot")
        self.start("project", "desktop/App.xcodeproj")
        self.assertIn("claims overlap lane project", self.start("pbx", "desktop/**/*.pbxproj", ok=False))
        self.start("new-dir", "desktop/src/future")
        self.assertIn("claims overlap lane new-dir", self.start("tsx", "desktop/src/**/*.tsx", ok=False))
        self.start("ignore", ".gitignore")
        self.assertIn("Lane md", self.start("md", "*/*.md"))

    def test_a_submodule_claim_is_a_directory_whatever_its_name(self):
        # Review of lanefather#2: HEAD answers a submodule as `<sha> submodule`, not as a type.
        sub = self.tmp / "sub"
        sub.mkdir()
        self.git(sub, "init", "-q", "-b", "main")
        self.git(sub, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "s")
        self.git(self.integration, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "mods/sub.d")
        self.git(self.integration, "commit", "-qm", "a submodule with a dot")
        self.start("sub", "mods/sub.d")
        self.assertIn("claims overlap lane sub", self.start("txt", "mods/**/*.txt", ok=False))

    def test_claim_kinds_come_from_the_project_not_the_working_directory(self):
        # Review of lanefather#2: next run from another checkout (main, which lacks the directory) still asks the
        # HEAD of the project its configuration names.
        self.commit(self.integration, {"desktop/App.xcodeproj/project.pbxproj": "x\n"}, "a directory with a dot")
        self.start("project", "desktop/App.xcodeproj")
        self.queue("""\
            [[lane]]
            ticket = 507
            id = "507-pbx"
            claims = ["desktop/**/*.pbxproj"]
            """)
        output = self.lane("--config", str(self.integration / ".lanefather.toml"), "next", cwd=self.repo, ok=False)
        self.assertIn("507-pbx (#507): claims overlap lane project", output)

    def test_a_malformed_entry_is_named(self):
        self.queue('[[lane]]\nticket = 1\nid = "bad id"\nclaims = ["a/**"]\n')
        self.assertIn("entry 1", self.lane("next", ok=False))
        self.queue('[[lane]]\nticket = 1\nid = "ok"\n')
        self.assertIn("claims", self.lane("next", ok=False))


DECISION = "https://github.com/fixture/straylight/issues/404#issuecomment-2541"


class ScopeRecordTests(unittest.TestCase):
    """The scope record (lanefather.scope, fathers#192): the primary ticket, the legacy reading and its fallback."""

    def test_a_legacy_record_reads_as_its_one_ticket_and_writes_no_scope_key(self):
        from lanefather import scope
        record = {"id": "a", "ticket": "404"}
        self.assertEqual(scope.of(record), (scope.Issue("404"),))
        self.assertEqual(scope.of({"id": "b", "ticket": "none"}), ())
        scope.write(record, scope.of(record))
        self.assertEqual(record, {"id": "a", "ticket": "404"})
        scope.write(record, (scope.Issue("404"), scope.Issue("9", partial=True)))
        self.assertEqual(record["scope"], [{"issue": "404"}, {"issue": "9", "partial": True}])
        # Back to one completed ticket: the key goes, so the record is today's again.
        scope.write(record, (scope.Issue("404"),))
        self.assertEqual(record, {"id": "a", "ticket": "404"})

    def test_the_primary_is_the_first_completed_ticket_else_the_first_ticket_else_none(self):
        from lanefather import scope
        partial, done, other = scope.Issue("9", True), scope.Issue("404"), scope.Issue("12")
        spec = scope.parse_spec("./docs/spec.md")
        self.assertEqual(spec, scope.Spec("docs/spec.md"))
        self.assertEqual(scope.primary((partial, done, other)), "404")
        self.assertEqual(scope.primary((partial, scope.Issue("8", True))), "9")
        self.assertEqual(scope.primary((spec,)), "none")
        self.assertEqual(scope.primary(()), "none")

    def test_a_malformed_scope_warns_and_reads_the_ticket_alone(self):
        import contextlib
        import io

        from lanefather import scope
        for raw in ("garbage", [{"issue": "404"}, {"issue": "404"}], [{"decision": "https://example.com/x"}],
                    [{"spec": "../outside.md"}], [{"issue": "9", "partial": "yes"}], [{"unknown": 1}]):
            with self.subTest(raw=raw):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    items = scope.of({"id": f"m{len(repr(raw))}", "ticket": "404", "scope": raw})
                self.assertEqual(items, (scope.Issue("404"),))
                self.assertIn("has a malformed scope", stderr.getvalue())
                self.assertIn("reading its ticket #404 alone", stderr.getvalue())

    def test_a_decision_is_one_issue_comment_url(self):
        from lanefather import scope
        from lanefather.errors import LaneError
        self.assertEqual(scope.parse_decision(DECISION),
                         scope.Decision(DECISION, "fixture/straylight", "404", "2541"))
        for bad in ("https://github.com/fixture/straylight/issues/404", "http://github.com/o/r/issues/1#issuecomment-2",
                    "https://example.com/o/r/issues/1#issuecomment-2"):
            with self.subTest(url=bad), self.assertRaises(LaneError):
                scope.parse_decision(bad)


class ScopeTests(LaneCase):
    """start's --ticket, --partial, --decision and --spec, and the scope command (fathers#192)."""

    def test_interleaved_tickets_and_partials_keep_their_order(self):
        output = self.scoped("m", "--partial", "9", "--ticket", "404", "--ticket", "#12", "--partial", "7",
                             "--decision", DECISION, "--spec", "docs/lanes.md", create=False)
        record = self.record("m")
        self.assertEqual(record["ticket"], "404", "the first ticket the lane completes is the primary one")
        self.assertEqual(record["scope"], [{"issue": "9", "partial": True}, {"issue": "404"}, {"issue": "12"},
                                           {"issue": "7", "partial": True}, {"decision": DECISION},
                                           {"spec": "docs/lanes.md"}])
        self.assertIn("Lane m: ticket #404\nScope: #9 (partial), #404, #12, #7 (partial), 1 decision, 1 spec\n",
                      output)
        [started] = self.events_of("lane.started")
        self.assertEqual(started["details"]["scope"], record["scope"])

    def test_a_lane_of_partial_tickets_takes_the_first_as_primary(self):
        self.scoped("p", "--partial", "9", "--partial", "8", create=False)
        record = self.record("p")
        self.assertEqual((record["ticket"], record["scope"]),
                         ("9", [{"issue": "9", "partial": True}, {"issue": "8", "partial": True}]))

    def test_one_completed_ticket_or_none_writes_no_scope_key(self):
        header = self.scoped("one", "--ticket", "404", create=False)
        self.scoped("zero", "--ticket", "none", claims=("calls/**",), create=False)
        self.assertNotIn("Scope:", header, "a one-ticket lane's brief header is today's")
        self.assertNotIn("scope", self.record("one"))
        self.assertNotIn("scope", self.record("zero"))
        self.assertEqual((self.record("one")["ticket"], self.record("zero")["ticket"]), ("404", "none"))
        self.assertEqual(self.lane("scope", "one").splitlines(),
                         ["Lane one: scope #404 (primary ticket #404)", "  1. ticket #404 (completes: Fixes)"])

    def test_start_refuses_a_scope_that_contradicts_itself(self):
        for options, words in (
                (("--ticket", "none", "--partial", "9"), "--ticket none names no ticket"),
                (("--ticket", "404", "--partial", "404"), "scope names issue #404 twice"),
                (("--ticket", "404", "--decision", "https://github.com/o/r/issues/1"),
                 "give the URL of one issue comment"),
                (("--ticket", "404", "--spec", "../secrets.md"), "without '..'"),
                (("--ticket", "404", "--spec", "/etc/passwd"), "relative to the repository"),
                (("--ticket", "404", "--spec", "docs/a.md", "--spec", "docs/a.md"), "scope names spec docs/a.md twice"),
                (("--ticket", "four oh four",), "give an issue number")):
            with self.subTest(options=options):
                output = self.scoped("bad", *options, create=False, ok=False)
                self.assertIn(words, output)
                self.assertFalse((self.state / "bad.json").exists(), "a refused start writes nothing")

    def test_a_partial_alone_names_the_scope_but_nothing_at_all_is_a_usage_error(self):
        self.scoped("p", "--partial", "9", create=False)
        output = self.lane("start", "q", "--base", "integrate/q", "--claims", "lib/**", ok=False)
        self.assertIn("the following arguments are required: --ticket", output)

    def test_the_scope_command_adds_and_marks_and_logs_the_change(self):
        self.scoped("a", "--ticket", "404", create=False)
        output = self.lane("scope", "a", "--add-partial", "9", "--add-ticket", "12", "--add-decision", DECISION,
                           "--add-spec", "docs/lanes.md")
        self.assertIn("Lane a: scope #404, #9 (partial), #12, 1 decision, 1 spec (primary ticket #404)", output)
        self.assertIn("  2. ticket #9 (partial: its merge says Refs)", output)
        self.assertIn(f"  4. decision {DECISION}", output)
        self.assertEqual(self.record("a")["scope"], [{"issue": "404"}, {"issue": "9", "partial": True},
                                                     {"issue": "12"}, {"decision": DECISION},
                                                     {"spec": "docs/lanes.md"}])
        [changed] = self.events_of("lane.scope_changed")
        self.assertEqual(changed["details"]["before"], [{"issue": "404"}])
        self.assertEqual(changed["details"]["after"], self.record("a")["scope"])
        self.assertIn("scope a: #404 -> #404, #9 (partial), #12, 1 decision, 1 spec ticket=404",
                      (self.state / "ledger.log").read_text())
        # The primary follows the first completed ticket.
        self.lane("scope", "a", "--mark-partial", "404")
        self.assertEqual(self.record("a")["ticket"], "12")
        reply = json.loads(self.lane("scope", "a", "--mark-complete", "9", "--json"))
        self.assertEqual((reply["v"], reply["lane"], reply["ticket"]), (1, "a", "9"))
        self.assertEqual(reply["scope"][:3], [{"issue": "404", "partial": True}, {"issue": "9"}, {"issue": "12"}])
        self.assertEqual(len(self.events_of("lane.scope_changed")), 3)
        # Printing changes nothing and logs nothing.
        self.lane("scope", "a")
        self.assertEqual(len(self.events_of("lane.scope_changed")), 3)

    def test_the_scope_command_refuses_what_the_lane_cannot_take(self):
        self.scoped("a", "--ticket", "404", create=False)
        for options, words in ((("--add-ticket", "404"), "scope names issue #404 twice"),
                               (("--mark-partial", "77"), "lane a has no ticket #77"),
                               (("--add-ticket", "none"), "give an issue number"),
                               (("--add-spec", "a/../../b"), "without '..'")):
            with self.subTest(options=options):
                self.assertIn(words, self.lane("scope", "a", *options, ok=False))
        self.assertNotIn("scope", self.record("a"))
        self.assertEqual(self.events_of("lane.scope_changed"), [])

    def test_a_legacy_none_lane_gains_its_first_ticket_without_a_scope_key(self):
        self.scoped("z", "--ticket", "none", create=False)
        self.lane("scope", "z", "--add-ticket", "404")
        record = self.record("z")
        self.assertEqual(record["ticket"], "404")
        self.assertNotIn("scope", record)


class ParkAndAbandonTests(LaneCase):
    """Parking and abandoning lanes as explicit outcomes, never as landed (fathers#57)."""

    def write_queue(self, text: str) -> pathlib.Path:
        path = self.tmp / "queue.toml"
        path.write_text(text)
        self.env["LANEFATHER_QUEUE"] = str(path)
        return path

    def test_park_blocks_queue_after_and_unpark_then_land_unblocks_it(self):
        from lanefather import lifecycle

        wt = pathlib.Path(self.created("p", "lib/**"))
        tip = self.commit(wt, {"lib/api.txt": "foo\nparked work\n"})
        self.lane("check", "p")
        receipt_path = self.state / "p.receipt.json"
        self.assertTrue(receipt_path.exists())

        self.write_queue(
            'base = "integrate/q"\n\n'
            '[[lane]]\n'
            'id = "dep"\n'
            'ticket = 505\n'
            'claims = ["lib/**"]\n'
            'after = ["p"]\n'
        )

        # Also start a planned lane depending on p to verify activate_planned refuses parked dependency.
        self.lane("start", "planned-dep", "--ticket", "506", "--base", "integrate/q",
                  "--claims", "lib/**", "--after", "p")

        park_out = self.lane("park", "p", "--reason", "waiting on upstream API")
        self.assertIn("Lane p parked at park/p", park_out)
        self.assertFalse(wt.exists())
        self.assertNotIn("lane/p", self.git(self.repo, "branch", "--list"))
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/heads/park/p"), tip)
        self.assertTrue(receipt_path.exists(), "park keeps receipts")

        rec = self.record("p")
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["outcome"], "parked")
        self.assertEqual(rec["parked"]["ref"], "park/p")
        self.assertEqual(rec["parked"]["tip"], tip)
        self.assertEqual(rec["parked"]["reason"], "waiting on upstream API")
        self.assertFalse(lifecycle.landed(rec))

        # list --all shows the parked outcome.
        self.assertIn("done (parked)", self.lane("list", "--all"))

        # Queue next skips dependants with "waits for p (parked)".
        next_out = self.lane("next", ok=False)
        self.assertIn("waits for p (parked)", next_out)

        # Planned dependant cannot activate while p is parked.
        act_out = self.lane("start", "planned-dep", "--create", ok=False)
        self.assertIn("p (parked)", act_out)

        # wait --for landed exits 3 (cannot land while parked).
        wait_res = subprocess.run(
            [sys.executable, "-m", "lanefather", "wait", "p", "--for", "landed", "--timeout", "0"],
            cwd=self.integration, env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(wait_res.returncode, 3, wait_res.stdout + wait_res.stderr)

        # Drop planned-dep so claims are free for unpark.
        self.lane("drop", "planned-dep")

        # Unpark p: recreates worktree from park/p, preserves scope/claims/base, does not inherit old receipt.
        unpark_out = self.lane("unpark", "p")
        self.assertIn("Lane p: ticket #404", unpark_out)
        self.assertNotIn("park/p", self.git(self.repo, "branch", "--list"))
        resumed = self.record("p")
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["claims"], ["lib/**"])
        self.assertEqual(resumed["base"], "integrate/q")
        new_wt = pathlib.Path(resumed["worktree"])
        self.assertTrue(new_wt.exists())
        self.assertEqual(self.git(new_wt, "rev-parse", "HEAD"), tip)

        # Old receipt is not inherited thanks to fathers#54 lane_created.
        self.assertIn("no receipt", self.lane("land", "p", ok=False))

        # Re-check and land p -> dependant in queue becomes ready.
        self.lane("check", "p")
        self.lane("land", "p")
        self.assertTrue(lifecycle.landed(self.record("p")))
        ready_out = self.lane("next")
        self.assertIn("Lane dep: ticket #505", ready_out)

    def test_abandon_empty_and_unmerged_lanes_with_keep_branch_and_force_reason(self):
        from lanefather import lifecycle

        # 1. Abandon an empty lane: removes worktree, branch, scratch, claims.
        wt_empty = pathlib.Path(self.created("empty-ab", "lib/**"))
        self.write(self.record("empty-ab")["scratch"][0], "note.log", "scratch")
        out = self.lane("abandon", "empty-ab", "--reason", "no longer needed")
        self.assertIn("Lane empty-ab abandoned", out)
        self.assertFalse(wt_empty.exists())
        self.assertNotIn("lane/empty-ab", self.git(self.repo, "branch", "--list"))
        rec_empty = self.record("empty-ab")
        self.assertFalse(pathlib.Path(rec_empty["scratch"][0]).exists())
        self.assertEqual(rec_empty["status"], "done")
        self.assertEqual(rec_empty["outcome"], "abandoned")
        self.assertFalse(lifecycle.landed(rec_empty))
        self.assertIn("done (abandoned)", self.lane("list", "--all"))

        # Queue entry after an abandoned lane is never ready.
        self.write_queue(
            'base = "integrate/q"\n\n'
            '[[lane]]\n'
            'id = "after-ab"\n'
            'ticket = 510\n'
            'claims = ["lib/**"]\n'
            'after = ["empty-ab"]\n'
        )
        self.assertIn("waits for empty-ab (abandoned)", self.lane("next", ok=False))

        # 2. Abandon a lane with unmerged commits: refused -> succeeds with --keep-branch park/<id>.
        wt1 = pathlib.Path(self.created("u1", "lib/**"))
        tip1 = self.commit(wt1, {"lib/api.txt": "foo\nunmerged 1\n"})
        refused = self.lane("abandon", "u1", "--reason", "superseded", ok=False)
        self.assertIn("abandon refused", refused)
        self.assertIn("--keep-branch park/u1", refused)
        self.assertIn("--force-reason", refused)
        self.assertTrue(wt1.exists())
        self.assertEqual(self.record("u1")["status"], "active")

        kept_out = self.lane("abandon", "u1", "--reason", "superseded", "--keep-branch", "park/u1")
        self.assertIn("branch kept at park/u1", kept_out)
        self.assertFalse(wt1.exists())
        self.assertNotIn("lane/u1", self.git(self.repo, "branch", "--list"))
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/heads/park/u1"), tip1)
        rec1 = self.record("u1")
        self.assertEqual(rec1["status"], "done")
        self.assertEqual(rec1["outcome"], "abandoned")
        self.assertEqual(rec1["abandoned"]["kept_branch"], "park/u1")
        self.assertFalse(lifecycle.landed(rec1))

        # 3. Abandon a lane with unmerged commits using --force-reason (audited via journal.override).
        wt2 = pathlib.Path(self.created("u2", "lib/**"))
        self.commit(wt2, {"lib/api.txt": "foo\nunmerged 2\n"})
        self.lane("abandon", "u2", "--reason", "spike failed", "--force-reason", "discard spike")
        self.assertFalse(wt2.exists())
        self.assertNotIn("lane/u2", self.git(self.repo, "branch", "--list"))
        rec2 = self.record("u2")
        self.assertEqual(rec2["status"], "done")
        self.assertEqual(rec2["outcome"], "abandoned")
        self.assertFalse(lifecycle.landed(rec2))
        self.assertTrue(any(o.get("reason") == "discard spike" for o in rec2.get("overrides", [])))
        self.assertIn("override abandon u2: discard spike", (self.state / "ledger.log").read_text())

    def test_done_on_unlanded_lane_names_park_and_abandon(self):
        self.created("unlanded", "lib/**")
        out = self.lane("done", "unlanded", ok=False)
        self.assertIn("not landed", out)
        self.assertIn("park unlanded", out)
        self.assertIn("abandon unlanded", out)

    def test_abandon_keep_branch_refuses_existing_protected_and_reflog_refs(self):
        wt = pathlib.Path(self.created("kb", "lib/**"))
        tip = self.commit(wt, {"lib/api.txt": "foo\nkeep-branch work\n"})
        base_head = self.git(self.repo, "rev-parse", "integrate/q")
        self.git(self.repo, "branch", "precious", base_head)

        # Reflog syntax, existing branch pointing elsewhere, own branch, and base branch are all refused.
        for bad_ref, expected in (
            ("@{-1}", "invalid branch name"),
            ("precious", "already exists"),
            ("lane/kb", "lane's own branch"),
            ("integrate/q", "base branch"),
            ("main", "promoted branch"),
        ):
            with self.subTest(bad_ref=bad_ref):
                out = self.lane("abandon", "kb", "--reason", "done", "--keep-branch", bad_ref, ok=False)
                self.assertIn(expected, out)
        # Existing branch was not moved and lane is still active.
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/heads/precious"), base_head)
        self.assertEqual(self.record("kb")["status"], "active")
        self.assertTrue(wt.exists())
        # A fresh ref succeeds.
        self.lane("abandon", "kb", "--reason", "done", "--keep-branch", "park/kb")
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/heads/park/kb"), tip)

    def test_park_refuses_diverged_existing_park_branch_and_start_refuses_parked_id(self):
        wt = pathlib.Path(self.created("pk", "lib/**"))
        tip = self.commit(wt, {"lib/api.txt": "foo\nnew work\n"})
        # Seed a diverged park/pk branch on an unrelated commit.
        other_wt = pathlib.Path(self.created("other", "calls/**"))
        other_tip = self.commit(other_wt, {"calls/other.txt": "diverged\n"})
        self.git(self.repo, "branch", "park/pk", other_tip)

        out = self.lane("park", "pk", "--reason", "pause", ok=False)
        self.assertIn("already exists", out)
        self.assertIn("not an ancestor", out)
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/heads/park/pk"), other_tip)
        self.assertEqual(self.record("pk")["status"], "active")

        # After removing the conflicting park/pk, park succeeds.
        self.git(self.repo, "branch", "-D", "park/pk")
        self.lane("park", "pk", "--reason", "pause")
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/heads/park/pk"), tip)

        # start on a parked id refuses and points to unpark.
        start_out = self.lane("start", "pk", "--ticket", "404", "--base", "integrate/q",
                              "--claims", "lib/**", "--create", ok=False)
        self.assertIn("exists (parked)", start_out)
        self.assertIn("unpark pk", start_out)
        self.assertEqual(self.record("pk")["outcome"], "parked")

    def test_abandon_keep_branch_refuses_unmerged_detached_head(self):
        wt = pathlib.Path(self.created("dh", "lib/**"))
        self.commit(wt, {"lib/api.txt": "foo\nbranch commit\n"})
        self.git(wt, "checkout", "-q", "--detach", "HEAD")
        self.commit(wt, {"lib/api.txt": "foo\ndetached commit\n"})

        refused = self.lane("abandon", "dh", "--reason", "drop it", "--keep-branch", "park/dh", ok=False)
        self.assertIn("abandon refused", refused)
        self.assertIn("not merged anywhere", refused)
        self.assertTrue(wt.exists())

        # With --force-reason it proceeds and audits the override.
        self.lane("abandon", "dh", "--reason", "drop it", "--keep-branch", "park/dh",
                  "--force-reason", "discard detached commit")
        self.assertFalse(wt.exists())
        self.assertEqual(self.record("dh")["outcome"], "abandoned")

    def test_park_and_abandon_refuse_unmarked_review_findings(self):
        wt = pathlib.Path(self.created("rf", "lib/**"))
        self.commit(wt, {"lib/api.txt": "foo\nreviewed\n"})
        rec = self.record("rf")
        (self.state / "rf.review.json").write_text(json.dumps({
            "version": 1, "lane": "rf", "status": "finished", "lane_created": rec["created"],
            "findings": [{"n": 1, "severity": "P1", "title": "bug", "location": "lib/api.txt:1"}],
        }))
        park_out = self.lane("park", "rf", "--reason", "pause", ok=False)
        self.assertIn("park refused", park_out)
        self.assertIn("unmarked finding", park_out)
        ab_out = self.lane("abandon", "rf", "--reason", "drop", "--keep-branch", "park/rf", ok=False)
        self.assertIn("abandon refused", ab_out)
        self.assertIn("unmarked finding", ab_out)
        self.assertEqual(self.record("rf")["status"], "active")

    def test_after_satisfied_by_dropped_and_land_or_unpark_override_for_abandoned(self):
        # 1. Dropped dependency satisfies --after so dependant can land without --force-reason.
        self.created("d0", "lib/a.txt")
        self.start("dep-on-drop", "lib/a.txt", extra=("--after", "d0", "--create"))
        wt_a = pathlib.Path(self.record("dep-on-drop")["worktree"])
        self.commit(wt_a, {"lib/a.txt": "after dropped\n"})
        self.lane("check", "dep-on-drop")
        self.lane("drop", "d0")
        self.lane("land", "dep-on-drop")
        self.assertIn(self.record("dep-on-drop")["status"], ("landed", "done"))

        # 2. Abandoned dependency blocks land unless --force-reason overrides (audited).
        self.created("ab0", "calls/x.txt")
        self.start("dep-on-ab", "calls/x.txt", extra=("--after", "ab0", "--create"))
        wt_b = pathlib.Path(self.record("dep-on-ab")["worktree"])
        self.commit(wt_b, {"calls/x.txt": "foo\n"})
        self.lane("check", "dep-on-ab")
        self.lane("abandon", "ab0", "--reason", "superseded")
        self.assertIn("ab0 (abandoned)", self.lane("land", "dep-on-ab", ok=False))
        self.lane("land", "dep-on-ab", "--force-reason", "ab0 no longer needed")
        self.assertIn("override land dep-on-ab: ab0 no longer needed", (self.state / "ledger.log").read_text())

        # 3. unpark refuses abandoned --after without --force-reason, succeeds with --force-reason,
        # and sets base_sha to merge-base(park_tip, base) even after integration moves on.
        self.created("ab1", "tooling/y.txt")
        self.start("pk-ab", "tooling/y.txt", extra=("--after", "ab1", "--create"))
        wt_c = pathlib.Path(self.record("pk-ab")["worktree"])
        original_base = self.record("pk-ab")["base_sha"]
        self.commit(wt_c, {"tooling/y.txt": "parked work\n"})
        self.lane("park", "pk-ab", "--reason", "pause")
        self.lane("abandon", "ab1", "--reason", "dropped plan")
        self.commit(self.integration, {"notes.txt": "integration moved forward\n"}, "move integration")

        self.assertIn("abandoned", self.lane("unpark", "pk-ab", ok=False))
        self.lane("unpark", "pk-ab", "--force-reason", "ab1 not needed")
        resumed = self.record("pk-ab")
        self.assertEqual(resumed["after"], [])
        self.assertEqual(resumed["base_sha"], original_base)


class MergeUnionAndGlobUnclaimedTests(LaneCase):
    """Retro #7: gitattributes merge=union files and glob patterns in unclaimed."""

    def test_merge_union_files_do_not_conflict_across_lanes_or_broad_globs(self) -> None:
        self.env["LANEFATHER_CAP"] = "6"
        self.commit(
            self.integration,
            {
                ".gitattributes": "packages/*/CHANGELOG.md merge=union\n",
                "packages/a/CHANGELOG.md": "## Unreleased\n",
                "packages/a/src/foo.ts": "export const x = 1;\n",
            },
            "add gitattributes merge=union",
        )
        # Two parallel lanes both claiming a merge=union file start cleanly.
        self.start("u1", "packages/a/CHANGELOG.md")
        self.start("u2", "packages/a/CHANGELOG.md")

        # queue_states marks both ready when claiming a merge=union file.
        from lanefather import config, gitops, queue, state
        project = config.load(self.integration / ".lanefather.toml")
        store = state.Store(project, directory=self.state)
        entries = [
            {"id": "q1", "ticket": "1", "claims": ["packages/a/CHANGELOG.md"], "after": []},
            {"id": "q2", "ticket": "2", "claims": ["packages/a/CHANGELOG.md"], "after": []},
        ]
        states = queue.queue_states(
            entries,
            store.lanes(),
            gitops.common_dir(self.integration),
            root=str(self.integration),
            project=project,
        )
        self.assertEqual([(e["id"], reason) for e, reason in states], [("q1", None), ("q2", None)])

        # Lane claiming merge=union file and lane claiming packages/a/** do NOT overlap.
        self.start("u3", "packages/a/**")

        # Negative guard: two lanes claiming packages/a/** STILL fail with overlap refusal.
        overlap_broad = self.start("u4", "packages/a/**", ok=False)
        self.assertIn("u3", overlap_broad)
        self.assertIn("packages/a/**", overlap_broad)

        # Negative guard: two lanes claiming a normal file without merge=union STILL fail with overlap refusal.
        self.start("n1", "packages/b/src/foo.ts")
        overlap_normal = self.start("n2", "packages/b/src/foo.ts", ok=False)
        self.assertIn("n1", overlap_normal)
        self.assertIn("packages/b/src/foo.ts", overlap_normal)

    def test_check_widening_skips_merge_union_files_and_does_not_warn(self) -> None:
        self.commit(
            self.integration,
            {
                ".gitattributes": "packages/*/CHANGELOG.md merge=union\n",
                "packages/a/CHANGELOG.md": "## Unreleased\n",
                "packages/a/src/foo.ts": "export const x = 1;\n",
            },
            "add gitattributes merge=union",
        )
        # Another lane holds packages/a/**; our lane edits lib/api.txt + packages/a/CHANGELOG.md.
        self.start("holder", "packages/a/**")
        wt = pathlib.Path(self.created("editor", "lib/**"))
        self.commit(wt, {"lib/api.txt": "foo\nupdated\n", "packages/a/CHANGELOG.md": "## Unreleased\n- entry\n"})
        out = self.lane("check", "editor")
        self.assertNotIn("packages/a/CHANGELOG.md", self.record("editor")["claims"])
        self.assertNotIn("overlaps the claims of lane holder", out)

    def test_config_unclaimed_supports_globs_alongside_prefixes(self) -> None:
        cfg_path = self.integration / ".lanefather.toml"
        lines = [line for line in cfg_path.read_text().splitlines() if not line.startswith("unclaimed =")]
        cfg_text = 'unclaimed = ["release-notes/", "packages/*/CHANGELOG.md"]\n' + "\n".join(lines) + "\n"
        self.commit(
            self.integration,
            {
                ".lanefather.toml": cfg_text,
                "packages/a/CHANGELOG.md": "## Unreleased\n",
                "release-notes/v1.md": "notes\n",
            },
            "configure unclaimed prefix and glob",
        )
        lane = load_lane()
        project = lane.config.load(cfg_path)
        self.assertTrue(lane.claims.unclaimed("release-notes/v1.md", project, root=str(self.integration)))
        self.assertTrue(lane.claims.unclaimed("packages/a/CHANGELOG.md", project, root=str(self.integration)))
        self.assertFalse(lane.claims.unclaimed("packages/a/src/foo.ts", project, root=str(self.integration)))
        # Two parallel lanes claiming glob-unclaimed file start cleanly.
        self.start("g1", "packages/a/CHANGELOG.md")
        self.start("g2", "packages/a/CHANGELOG.md")

    def test_non_git_directory_and_repo_without_gitattributes_fail_closed(self) -> None:
        import shutil
        import tempfile
        lane = load_lane()
        # Repo without .gitattributes fails closed to False.
        project = lane.config.load(self.integration / ".lanefather.toml")
        self.assertFalse(lane.claims.unclaimed("lib/api.txt", project, root=str(self.integration)))
        # Non-git directory fails closed cleanly without raising.
        non_git = pathlib.Path(tempfile.mkdtemp(prefix="non-git-claims-"))
        self.addCleanup(shutil.rmtree, non_git, True)
        (non_git / "CHANGELOG.md").write_text("# Changelog\n")
        self.assertEqual(lane.claims.merge_attr("CHANGELOG.md", root=str(non_git)), "")
        self.assertFalse(lane.claims.unclaimed("CHANGELOG.md", project, root=str(non_git)))


if __name__ == "__main__":
    unittest.main()
