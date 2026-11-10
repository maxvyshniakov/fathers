"""Tests of start, next, adopt, list and the queue (lanefather.lanes, lanefather.queue, lanefather.claims): replays of orchestration incidents in scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap
import unittest

from support import LaneCase, load_lane


class StartTests(LaneCase):
    """B4 start: caps, disk, load and path claims."""

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

    def test_a_bad_cap_falls_back_to_the_derived_cap_with_a_warning(self):
        # #454: int() at import crashed every tools/lane command on a bad LANEFATHER_CAP.
        for value in ("many", "0", "-2", "2.5"):
            self.env["LANEFATHER_CAP"] = value
            output = self.lane("list")
            self.assertIn("0 of 3 lanes planned or active", output, value)
            self.assertIn(f"LANEFATHER_CAP={value!r} is not a positive whole number; the cap is 3", output)
        self.env["LANEFATHER_CAP"] = ""
        self.assertNotIn("LANEFATHER_CAP", self.lane("list"), "an empty value is the default")

    def test_low_disk_or_high_load_is_refused(self):
        self.env["LANEFATHER_DISK_FREE_GB"] = "120"
        self.assertIn("150 GB", self.start("disk", "a/**", ok=False))
        self.env["LANEFATHER_DISK_FREE_GB"] = "500"
        self.env["LANEFATHER_LOAD15"] = "9.1"
        self.assertIn("load15 is 9.1, above 1.5x6 cores", self.start("load", "a/**", ok=False))

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

    def agent_worktree(self, name, branch):
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
        self.start("474-review-fixes", "native/straylight-core/src/keyboard/indication.rs")
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

    def test_a_malformed_entry_is_named(self):
        self.queue('[[lane]]\nticket = 1\nid = "bad id"\nclaims = ["a/**"]\n')
        self.assertIn("entry 1", self.lane("next", ok=False))
        self.queue('[[lane]]\nticket = 1\nid = "ok"\n')
        self.assertIn("claims", self.lane("next", ok=False))


if __name__ == "__main__":
    unittest.main()
