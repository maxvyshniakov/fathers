"""Tests of lane check planning, claims, merge-base, load throttling, slot allocation, and verifier resolution (lanefather.checks, lanefather.host)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import time
import types
from typing import Any
import unittest
import unittest.mock

from support import FIXTURE_CONFIG, LaneCase, ScratchCase, load_lane, project


class ClaimsFollowTheDiffTests(LaneCase):
    """#574: about 100 of 144 lane reports had files outside their claims, and the orchestrator widened claims by
    script before every land. check adds the lane's changed paths to its claims and warns only on an overlap with
    another planned or active lane."""

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def test_a_changed_path_no_other_lane_claims_joins_the_claims_without_a_warning(self):
        worktree = self.created("a", "lib/**")
        self.start("other", "docs/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n", "calls/extra.txt": "foo\n",
                               "release-notes/unreleased/a.md": "- a\n"})
        output = self.lane("check", "a")
        self.assertNotIn("warning", output)
        self.assertIn("claims widened: calls/extra.txt", output)
        # Release notes merge by union, so nobody claims them.
        self.assertEqual(self.record("a")["claims"], ["lib/**", "calls/extra.txt"])
        receipt = self.receipt("a")
        self.assertEqual((receipt["claims_added"], receipt["claim_overlaps"]), (["calls/extra.txt"], []))
        self.assertIn("claims a + calls/extra.txt", (self.state / "ledger.log").read_text())
        # The widened claim now holds: a new lane on that path waits for this one.
        self.assertIn("claims overlap lane a", self.start("late", "calls/**", ok=False))
        # A second check adds nothing new.
        self.commit(worktree, {"lib/api.txt": "foo\nqux\n"})
        self.assertNotIn("claims widened", self.lane("check", "a"))
        self.assertEqual(self.record("a")["claims"], ["lib/**", "calls/extra.txt"])

    def test_a_changed_path_another_active_lane_claims_warns_naming_that_lane(self):
        worktree = self.created("a", "lib/**")
        self.created("calls-lane", "calls/**")
        self.start("planned-docs", "docs/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n", "calls/extra.txt": "foo\n", "docs/install.md": "x\n"})
        output = self.lane("check", "a")
        self.assertIn("lane: warning: calls/extra.txt overlaps the claims of lane calls-lane (#404): calls/**", output)
        self.assertIn("lane: warning: docs/install.md overlaps the claims of lane planned-docs (#404): docs/**", output)
        self.assertEqual(self.record("a")["claims"], ["lib/**", "calls/extra.txt", "docs/install.md"])
        self.assertEqual(self.receipt("a")["claim_overlaps"], [
            {"path": "calls/extra.txt", "lane": "calls-lane", "ticket": "404", "claim": "calls/**"},
            {"path": "docs/install.md", "lane": "planned-docs", "ticket": "404", "claim": "docs/**"}])

    def test_a_declared_sequence_or_a_landed_lane_does_not_warn(self):
        first = self.created("first", "lib/**")
        self.commit(first, {"lib/api.txt": "foo\nfirst\n"})
        self.lane("check", "first")
        self.start("second", "calls/**", extra=("--after", "first", "--create"))
        second = self.record("second")["worktree"]
        self.commit(second, {"calls/main.txt": "foo\n\n", "lib/second.txt": "x\n"})
        self.assertNotIn("warning", self.lane("check", "second"))
        self.lane("land", "first")
        third = self.created("third", "native/**")
        self.commit(third, {"native/straylight-core/notes.txt": "x\n", "lib/third.txt": "x\n"})
        output = self.lane("check", "third")
        # first landed: it holds no claims; second is active and claims lib/second.txt only.
        self.assertNotIn("warning", output)


class NewestMergeBaseTests(LaneCase):
    """lanefather#3: a lane registered at a literal commit keeps it after it merges the integration branch, and
    check then planned and widened claims from everything the integration gained since (Straylight 515: 148 files
    instead of 10, the full gate, and false overlap warnings)."""

    def queue(self, base: str) -> None:
        path = self.state.parent / "queue" / "queue.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'base = "{base}"\n')

    def literal_lane(self, lane_id: str, claim: str) -> str:
        self.lane("start", lane_id, "--ticket", "404", "--base", self.tip(), "--claims", claim, "--create")
        return self.record(lane_id)["worktree"]

    def test_a_literal_base_lane_that_merged_the_integration_branch_plans_against_its_tip(self):
        self.queue("integrate/q")
        worktree = self.literal_lane("a", "lib/**")
        self.created("other", "unowned/**")
        # The integration gains a path no check reads (the full gate) and one another lane claims.
        moved = self.commit(self.integration, {"unowned/arrived.txt": "x\n"}, "the integration moves on")
        self.git(worktree, "merge", "-q", "--no-edit", "integrate/q")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        output = self.lane("check", "a")
        receipt = json.loads((self.state / "a.receipt.json").read_text())
        self.assertEqual(receipt["base"], moved)
        self.assertFalse(receipt["full"], output)
        self.assertEqual((receipt["outside_claims"], receipt["claim_overlaps"]), ([], []))
        self.assertNotIn("warning", output)
        self.assertEqual(self.record("a")["claims"], ["lib/**"])
        self.assertEqual(self.record("a")["base"], self.record("a")["base_sha"], "the registration stays as it was")

    def test_a_malformed_queue_entry_does_not_drop_the_queue_base(self):
        # Review of lanefather#3: the queue's base is a top-level setting; an entry next for its own reasons
        # refuses must not silently change what check plans against.
        self.queue("integrate/q")
        path = self.state.parent / "queue" / "queue.toml"
        path.write_text(path.read_text() + '[[lane]]\nticket = 1\nid = "bad id"\nclaims = ["a/**"]\n')
        worktree = self.literal_lane("a", "lib/**")
        moved = self.commit(self.integration, {"unowned/arrived.txt": "x\n"}, "the integration moves on")
        self.git(worktree, "merge", "-q", "--no-edit", "integrate/q")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.assertEqual(json.loads((self.state / "a.receipt.json").read_text())["base"], moved)

    def test_an_unreadable_queue_warns_that_check_plans_without_its_base(self):
        self.queue("integrate/q")
        (self.state.parent / "queue" / "queue.toml").write_text("base = \n")
        worktree = self.literal_lane("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.assertIn("lane: warning: ", self.lane("check", "a"))

    def test_a_lane_that_did_not_merge_the_integration_branch_keeps_its_own_base(self):
        self.queue("integrate/q")
        worktree = self.literal_lane("a", "lib/**")
        base = self.record("a")["base_sha"]
        self.commit(self.integration, {"unowned/arrived.txt": "x\n"}, "the integration moves on")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.assertEqual(json.loads((self.state / "a.receipt.json").read_text())["base"], base)


    def test_a_lane_based_on_another_branch_that_merges_the_queue_branch_keeps_its_own_base(self):
        # Review of lanefather#3: the queue's base is not the branch every lane lands into. A dev-based lane that
        # merges integrate/q would plan against that merge and land integrate/q's commits into dev un-gated.
        self.queue("integrate/q")
        self.git(self.repo, "branch", "dev")
        self.lane("start", "a", "--ticket", "404", "--base", "dev", "--claims", "lib/**", "--create")
        worktree = self.record("a")["worktree"]
        dev = self.git(self.repo, "rev-parse", "dev")
        self.commit(self.integration, {"unowned/arrived.txt": "x\n"}, "the integration moves on")
        self.git(worktree, "merge", "-q", "--no-edit", "integrate/q")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        receipt = json.loads((self.state / "a.receipt.json").read_text())
        self.assertEqual(receipt["base"], dev)
        self.assertTrue(receipt["full"], "what the integration brought in is checked against dev")


class LoadThrottleTests(LaneCase):
    """#572: every result records the load at its verify run's start and end. Owner decision on the #572 review: the
    load wait is off by default; with LANEFATHER_LOAD_THROTTLE=1 a verify run of check, land or promote starts only while
    load15 is at or below the local advisory limit (1.5 x cores), and the result records that wait."""

    def receipt(self, name: str) -> dict[str, Any]:
        return json.loads((self.state / f"{name}.json").read_text())

    def test_by_default_a_loaded_machine_runs_at_once_and_the_receipt_records_the_load(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        # A run that waited for the load would wait the whole ten minutes: the bound only bounds machine load.
        self.env.update(LANEFATHER_LOAD15="12.5", LANEFATHER_LOAD_WAIT="600", LANEFATHER_LOAD_POLL="0.1")
        started = time.monotonic()
        output = self.lane("check", "a")
        self.assertLess(time.monotonic() - started, 300)
        self.assertNotIn("waits", output)
        for result in self.receipt("a.receipt")["results"]:
            self.assertEqual(result["load_wait"], {"enabled": False, "limit": 9.0, "load15_before": 12.5,
                                                   "load15": 12.5, "seconds": 0.0})
            self.assertEqual((result["load"]["start"][2], result["load"]["end"][2]), (12.5, 12.5))
        self.assertNotIn("load-wait", (self.state / "ledger.log").read_text())

    def test_check_and_land_wait_while_load15_is_above_the_limit_and_the_receipts_show_it(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        # 6 cores: the limit is 9. The load never drops here, so each run waits its whole patience and starts.
        self.env.update(LANEFATHER_LOAD_THROTTLE="1", LANEFATHER_LOAD15="12.5", LANEFATHER_LOAD_WAIT="0.4",
                        LANEFATHER_LOAD_POLL="0.1")
        output = self.lane("check", "a")
        self.assertIn("load15 12.5 is above the limit 9.0", output)
        for result in self.receipt("a.receipt")["results"]:
            wait = result["load_wait"]
            self.assertGreaterEqual(wait["seconds"], 0.4)
            self.assertEqual((wait["limit"], wait["load15_before"], wait["load15"], wait["gave_up"]),
                             (9.0, 12.5, 12.5, True))
            self.assertEqual([len(result["load"][when]) for when in ("start", "end")], [3, 3])
            self.assertEqual(result["load"]["start"][2], 12.5)
        self.integration_moves()
        self.lane("land", "a")
        [result] = [r for r in self.receipt("a.post-merge")["results"] if r["suite"] == "lib"]
        self.assertTrue(result["load_wait"]["gave_up"])
        ledger = (self.state / "ledger.log").read_text()
        self.assertRegex(ledger, r"load-wait lane-a seconds=0\.\d load15=12\.5->12\.5 limit=9\.0 gave up")
        self.assertRegex(ledger, r"load-wait post-merge-a seconds=0\.\d")

    def test_under_the_limit_nothing_waits_and_the_load_is_still_recorded(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        for result in self.receipt("a.receipt")["results"]:
            self.assertEqual(result["load_wait"], {"enabled": False, "limit": 9.0, "load15_before": 0.1,
                                                   "load15": 0.1, "seconds": 0.0})
            self.assertEqual(result["load"]["end"][2], 0.1)
        self.assertNotIn("load-wait", (self.state / "ledger.log").read_text())

    def in_process(self, **env: str) -> types.SimpleNamespace:
        lane = load_lane()
        patcher = unittest.mock.patch.dict(os.environ, {**self.env, "LANEFATHER_LOAD_THROTTLE": "1",
                                                        "LANEFATHER_LOAD_POLL": "0.01", **env}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        cfg = self.enterContext(project(self.integration))
        lane.store = lane.state.Store(cfg, directory=self.state)
        return lane

    def test_the_batch_gate_sizes_its_jobs_after_the_load_wait(self):
        """Review of #599: the gate's job count comes from the load once its wait is over, not before it."""
        tip = self.tip()
        common = self.git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        lane = self.in_process(SLOTFATHER_LOADAVG="12")

        def wait(_store: Any, _label: str) -> dict[str, Any]:
            os.environ["SLOTFATHER_LOADAVG"] = "0.1"  # the load dropped while the gate waited
            return {"enabled": True, "limit": 9.0, "load15_before": 12.0, "load15": 0.1, "seconds": 1.0}
        with unittest.mock.patch.object(lane.host, "wait_for_load", side_effect=wait), \
                contextlib.redirect_stdout(io.StringIO()):
            lane.promote.run_batch_gate(lane.store, str(self.repo), common, tip, None)
        decision = self.receipt(f"promote-{tip[:10]}.gate")["jobs_decision"]
        self.assertEqual((decision["load"], decision["jobs"]), (0.1, 2))

    def test_the_batch_gate_waits_for_load_too(self):
        self.git(self.repo, "branch", "dev")
        self.env.update(LANEFATHER_LOAD_THROTTLE="1", LANEFATHER_LOAD15="12.5", LANEFATHER_LOAD_WAIT="0.2",
                        LANEFATHER_LOAD_POLL="0.1")
        tip = self.tip()
        self.lane("promote", tip, "--no-push")
        gate = self.receipt(f"promote-{tip[:10]}.gate")
        self.assertTrue(gate["load_wait"]["gave_up"])
        self.assertEqual(gate["results"][0]["load_wait"], gate["load_wait"])


class LoadThrottleUnitTests(ScratchCase):
    def in_process_scratch(self, **env: str) -> Any:
        self.write(self.tmp, ".lanefather.toml", FIXTURE_CONFIG)
        lane = load_lane()
        patcher = unittest.mock.patch.dict(os.environ, {**self.env, "LANEFATHER_LOAD_THROTTLE": "1",
                                                        "LANEFATHER_LOAD_POLL": "0.01", **env}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        cfg = self.enterContext(project(self.tmp))
        lane.store = lane.state.Store(cfg, directory=self.state)
        return lane

    def test_a_run_starts_once_load15_drops_under_the_limit(self) -> None:
        lane = self.in_process_scratch(LANEFATHER_LOAD_POLL="0.06")
        loads = iter([20.0, 12.0, 8.9])
        def next_load(*_args: Any) -> float:
            return next(loads)
        with unittest.mock.patch.object(lane.host, "load15", side_effect=next_load), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            wait = lane.host.wait_for_load(lane.store, "lane-a")
        self.assertIn("lane-a starts after 0 s at load15 8.9", printed.getvalue())
        self.assertEqual((wait["load15_before"], wait["load15"], wait["limit"]), (20.0, 8.9, 9.0))
        self.assertNotIn("gave_up", wait)
        self.assertGreater(wait["seconds"], 0)

    def test_a_run_under_a_lease_it_already_holds_does_not_wait(self) -> None:
        # #568's rerun alone holds every slot: a wait there would hold them idle while the load can only drop.
        lane = self.in_process_scratch(SLOTFATHER_LEASE=str(self.tmp / "lease"), LANEFATHER_LOAD15="30")
        wait = lane.host.wait_for_load(lane.store, "lane-a-alone")
        self.assertEqual(wait["seconds"], 0.0)
        self.assertIn("slot lease", wait["skipped"])


class MinSlotsTests(LaneCase):
    """fathers#173: a lane check honours a step's declared minimum slots (from --plan --json or [checks.min_slots])
    instead of falling to 1 slot on a busy machine."""

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def test_plan_declared_slots_floor_prevents_falling_to_one_slot_on_busy_machine(self):
        # Mark lib-check as declaring slots=2 in --plan --json.
        verify = (self.integration / "verify.sh").read_text().replace(
            'rows = [dict(row, producer=not row["paths"]) for row in rows if full or row["paths"]]',
            'rows = [dict(row, producer=not row["paths"], **({"slots": 2} if row["id"] == "lib-check" else {})) '
            'for row in rows if full or row["paths"]]')
        self.commit(self.integration, {"verify.sh": verify}, "declare slots=2 for lib-check")
        # 3 of 4 slots held -> slotfather advise returns jobs=1.
        self.env.update(FAKE_SLOTFATHER_HELD="3")
        worktree = self.created("a", "lib/**", "README.md")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        receipt = self.receipt("a")
        self.assertEqual(receipt["jobs"], 2)
        self.assertEqual(receipt["jobs_decision"]["advised_jobs"], 1)
        self.assertEqual(receipt["jobs_decision"]["min_slots"], 2)
        self.assertEqual(receipt["jobs_decision"]["min_slots_for"], ["lib-check"])
        leases = pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()
        self.assertEqual(leases[-1], "lane-a --jobs 2")

        # When lib-check is reused (not remaining), only structure-check runs -> falls back to advise (1 slot).
        self.commit(worktree, {"README.md": "docs edit\n"})
        self.lane("check", "a")
        receipt2 = self.receipt("a")
        self.assertEqual(receipt2["jobs"], 1)
        self.assertNotIn("min_slots", receipt2["jobs_decision"])

        # Explicit --slots 1 override still wins over declared min_slots.
        self.lane("check", "a", "--rerun", "--slots", "1")
        receipt3 = self.receipt("a")
        self.assertEqual(receipt3["jobs"], 1)
        self.assertEqual(receipt3["jobs_decision"], {"jobs": 1, "override": "--slots"})

    def test_config_min_slots_pattern_honours_floor_on_busy_machine(self):
        config = (self.integration / ".lanefather.toml").read_text() + '\n[checks.min_slots]\n"lib-*" = 2\n'
        self.commit(self.integration, {".lanefather.toml": config}, "configure [checks.min_slots]")
        self.env.update(FAKE_SLOTFATHER_HELD="3")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        receipt = self.receipt("a")
        self.assertEqual(receipt["jobs"], 2)
        self.assertEqual(receipt["jobs_decision"]["advised_jobs"], 1)
        self.assertEqual(receipt["jobs_decision"]["min_slots"], 2)
        self.assertEqual(receipt["jobs_decision"]["min_slots_for"], ["lib-check"])


class MinSlotsUnitTests(unittest.TestCase):
    def test_min_slots_is_capped_at_machine_total_slot_capacity(self) -> None:
        from lanefather import host, slots
        with unittest.mock.patch.object(slots, "advise", return_value={"jobs": 1, "slots": 1, "held": 0, "free": 1}):
            decision = host.check_jobs({"disk_free_gb": 500, "load15": 0.1, "cores": 2}, minimum=2,
                                       min_for=["browser:story-checks"])
        self.assertEqual(decision["jobs"], 1)
        self.assertEqual(decision["min_slots"], 1)

    def test_classify_reruns_honour_declared_min_slots(self):
        from lanefather import config, plan, state, verify
        lane_plan: dict[str, Any] = {
            "selection": [{"id": "browser:e2e", "suite": "browser", "paths": ["a.ts"], "min_slots": 4}],
        }
        fresh = [{"suite": "browser", "status": "failed", "failed_checks": ["browser:e2e"]}]
        report = {"checks": [{"id": "browser:e2e", "status": "failed"}]}
        seen_jobs: list[int] = []

        def fake_run_rerun(
            _store: state.Store,
            _root: Any,
            _ids: list[str],
            label: str,
            jobs: int,
            **_kwargs: Any,
        ) -> tuple[str, dict[str, Any], pathlib.Path, None, None]:
            seen_jobs.append(jobs)
            return f"run-{label}", report, pathlib.Path("/tmp/l"), None, None

        @contextlib.contextmanager
        def fake_base(*_args: Any, **_kwargs: Any) -> Any:
            yield "/tmp"

        store = state.Store(config.DEFAULT)
        with (unittest.mock.patch.object(verify, "run_rerun", side_effect=fake_run_rerun),
              unittest.mock.patch.object(verify.worktrees, "base_checkout", side_effect=fake_base),
              unittest.mock.patch.object(plan, "known_checks", return_value={"browser:e2e"})):
            verify.classify_failures(
                store, {"id": "a", "ticket": "1"}, "/tmp", "deadbeef", fresh, report, jobs=4, lane_plan=lane_plan
            )
        self.assertEqual(seen_jobs, [4, 4])

    def test_fix_plan_preserves_min_slots_on_added_parent_checks(self):
        from lanefather import plan
        lane_plan: dict[str, Any] = {
            "paths": ["src/new.ts"],
            "selection": [{"id": "unit", "suite": "verify", "paths": ["src/new.ts"]}],
            "selected": ["verify"],
            "by_suite": {"verify": ["unit"]},
        }
        parent: dict[str, Any] = {
            "lane": "p",
            "tree": "abc",
            "paths": ["src/old.ts"],
            "selection": [{"id": "browser:e2e", "suite": "browser", "paths": ["src/old.ts"], "min_slots": 4}],
        }
        about = plan.fix_plan(lane_plan, parent, known={"unit", "browser:e2e"})
        self.assertTrue(about["union"])
        selection: list[dict[str, Any]] = lane_plan["selection"]
        added = next(r for r in selection if r["id"] == "browser:e2e")
        self.assertEqual(added.get("min_slots"), 4)

    def test_default_min_slots_fallback_and_specific_override(self) -> None:
        from lanefather import config, plan, verify
        cfg = config.parse({"checks": {"min_slots": {"default": 2, "browser": 3, "structure": 1}}})
        lane_plan: dict[str, Any] = {
            "selection": [
                {"id": "structure-check", "suite": "structure", "paths": ["README.md"]},
                {"id": "lib-check", "suite": "lib", "paths": ["lib/a.txt"]},
                {"id": "browser:e2e", "suite": "browser", "paths": ["app/a.ts"]},
            ],
        }
        # Explicit structure=1 overrides default=2.
        self.assertEqual(
            plan.required_min_slots(lane_plan, {"structure": ["structure-check"]}, ["structure"], project=cfg),
            (1, ["structure-check"]),
        )
        # Unmatched lib falls back to default=2.
        self.assertEqual(
            plan.required_min_slots(lane_plan, {"lib": ["lib-check"]}, ["lib"], project=cfg),
            (2, ["lib-check"]),
        )
        # Specific browser=3 wins over default=2.
        self.assertEqual(
            plan.required_min_slots(
                lane_plan, {"lib": ["lib-check"], "browser": ["browser:e2e"]}, ["lib", "browser"], project=cfg
            ),
            (3, ["browser:e2e"]),
        )
        # Suite-level fallback (no per-check selection) also honours default and specific patterns.
        self.assertEqual(plan.required_min_slots({}, None, ["lib"], project=cfg), (2, ["lib"]))
        self.assertEqual(plan.required_min_slots({}, None, ["structure"], project=cfg), (1, ["structure"]))
        # Plan-wide declared slots/min_slots suppresses default, while a higher explicit pattern still wins.
        self.assertEqual(plan.required_min_slots({"min_slots": 1}, None, ["lib"], project=cfg), (1, ["plan"]))
        self.assertEqual(plan.required_min_slots({"slots": 1}, None, ["browser"], project=cfg), (3, ["browser"]))
        # Rerun floor honours default fallback when no selection is present, and matches suite patterns for checks
        # whose id differs from their suite name.
        self.assertEqual(verify.rerun_min_slots(["lib-check"], None, [], project=cfg), 2)
        self.assertEqual(verify.rerun_min_slots(["structure"], None, [], project=cfg), 1)
        suite_plan: dict[str, Any] = {"selection": None}
        fresh_structure = [{"suite": "structure", "status": "failed", "failed_checks": ["structure-check"]}]
        self.assertEqual(verify.rerun_min_slots(["structure-check"], suite_plan, fresh_structure, project=cfg), 1)
        fresh_lib = [{"suite": "lib", "status": "failed", "failed_checks": ["lib-check"]}]
        self.assertEqual(verify.rerun_min_slots(["lib-check"], suite_plan, fresh_lib, project=cfg), 2)
        self.assertEqual(
            verify.rerun_min_slots(["lib-check"], {"selection": None, "min_slots": 1}, fresh_lib, project=cfg),
            1,
        )

    def test_slots_wrap_formats_raised_min_count_range(self) -> None:
        from lanefather import slots
        with (unittest.mock.patch.object(slots, "command", return_value="/usr/bin/slotfather"),
              unittest.mock.patch.object(slots, "leased", return_value=False),
              unittest.mock.patch.object(slots, "supports_range", return_value=True)):
            wrapped_range = slots.wrap(4, "lane-a", ["echo", "hi"], min_count=2)
            wrapped_exact = slots.wrap(2, "lane-a", ["echo", "hi"], min_count=2)
            wrapped_default = slots.wrap(4, "lane-a", ["echo", "hi"])
        self.assertEqual(wrapped_range[wrapped_range.index("-n") + 1], "2-4")
        self.assertEqual(wrapped_exact[wrapped_exact.index("-n") + 1], "2")
        self.assertEqual(wrapped_default[wrapped_default.index("-n") + 1], "4")


class SlotRangeWorkerTests(LaneCase):
    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def leases(self) -> list[str]:
        return pathlib.Path(str(self.verify_log) + ".leases").read_text().splitlines()

    def history_entries(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted((self.state / "history").glob("*.jsonl")):
            for line in path.read_text().splitlines():
                if line:
                    rows.append(json.loads(line))
        return rows

    def test_slot_range_requests_range_and_records_granted_count(self):
        from lanefather import slots

        sf_log_path = self.state / "slotfather.log"
        self.env.update(
            FAKE_SLOTFATHER_RANGE="1",
            FAKE_SLOTFATHER_TOTAL="4",
            FAKE_SLOTFATHER_HELD="3",
            FAKE_SLOTFATHER_LOG=str(sf_log_path),
        )
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/a.txt": "range\n"})
        self.lane("check", "a")

        receipt = self.receipt("a")
        self.assertEqual(receipt["jobs"], 1)
        self.assertTrue(receipt["jobs_decision"]["slot_range"])
        self.assertEqual(receipt["jobs_decision"]["min_jobs"], 1)
        self.assertEqual(receipt["jobs_decision"]["max_jobs"], 2)
        self.assertEqual(receipt["jobs_decision"]["granted"], 1)
        self.assertEqual(self.leases(), ["lane-a --jobs 2 held 1"])

        sf_log: list[dict[str, Any]] = [json.loads(line) for line in sf_log_path.read_text().splitlines() if line]
        runs = [entry for entry in sf_log if entry["command"] == "run"]
        self.assertTrue(any("-n" in entry["args"] and "1-2" in entry["args"] for entry in runs))

        entries = [row for row in self.history_entries() if row["kind"] == "check" and row.get("lane") == "a"]
        self.assertTrue(entries)
        self.assertTrue(all(row.get("jobs") == 1 for row in entries))

        # With free capacity and FAKE_SLOTFATHER_GRANTED=2, the same range yields 2 workers.
        self.env.update(FAKE_SLOTFATHER_HELD="0", FAKE_SLOTFATHER_GRANTED="2")
        self.commit(worktree, {"lib/a.txt": "range-two\n"})
        self.lane("check", "a")
        receipt2 = self.receipt("a")
        self.assertEqual(receipt2["jobs"], 2)
        self.assertEqual(receipt2["jobs_decision"]["granted"], 2)
        latest = [row for row in self.history_entries()
                  if row["kind"] == "check" and row.get("lane") == "a" and row.get("tree") == receipt2["tree"]]
        self.assertTrue(latest)
        self.assertTrue(all(row.get("jobs") == 2 for row in latest))

        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            wrapped = slots.wrap(2, "lane-a", ["echo", "hi"], min_count=1)
        self.assertIn("1-2", wrapped)

    def test_without_slot_range_preserves_exact_argv_receipt_and_history(self):
        from lanefather import slots

        self.env.pop("FAKE_SLOTFATHER_RANGE", None)
        self.env.update(FAKE_SLOTFATHER_TOTAL="4", FAKE_SLOTFATHER_HELD="3")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/a.txt": "legacy\n"})
        self.lane("check", "a")

        receipt = self.receipt("a")
        self.assertEqual(receipt["jobs"], 1)
        self.assertNotIn("slot_range", receipt["jobs_decision"])
        self.assertNotIn("min_jobs", receipt["jobs_decision"])
        self.assertNotIn("max_jobs", receipt["jobs_decision"])
        self.assertNotIn("granted", receipt["jobs_decision"])
        self.assertEqual(self.leases(), ["lane-a --jobs 1"])

        entries = [row for row in self.history_entries() if row["kind"] == "check" and row.get("lane") == "a"]
        self.assertTrue(entries)
        self.assertTrue(all("jobs" not in row for row in entries))

        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            wrapped = slots.wrap(2, "lane-a", ["echo", "hi"], min_count=1)
        self.assertIn("-n", wrapped)
        self.assertEqual(wrapped[wrapped.index("-n") + 1], "2")

    def test_slot_range_honours_config_min_slots_floor_only_for_matching_selection(self):
        config = (self.integration / ".lanefather.toml").read_text() + '\n[checks.min_slots]\nlib = 2\n'
        self.commit(self.integration, {".lanefather.toml": config}, "configure [checks.min_slots] for lib")
        sf_log_path = self.state / "slotfather.log"
        self.env.update(
            FAKE_SLOTFATHER_RANGE="1",
            FAKE_SLOTFATHER_TOTAL="4",
            FAKE_SLOTFATHER_HELD="3",
            FAKE_SLOTFATHER_LOG=str(sf_log_path),
        )
        worktree = self.created("a", "lib/**", "README.md")
        self.commit(worktree, {"lib/api.txt": "foo\nmin-slots\n"})
        self.lane("check", "a")

        receipt = self.receipt("a")
        self.assertEqual(receipt["jobs"], 2)
        self.assertTrue(receipt["jobs_decision"]["slot_range"])
        self.assertEqual(receipt["jobs_decision"]["min_jobs"], 2)
        self.assertEqual(receipt["jobs_decision"]["min_slots"], 2)
        self.assertEqual(receipt["jobs_decision"]["min_slots_for"], ["lib-check"])
        self.assertEqual(receipt["jobs_decision"]["granted"], 2)
        sf_log: list[dict[str, Any]] = [json.loads(line) for line in sf_log_path.read_text().splitlines() if line]
        runs = [entry for entry in sf_log if entry["command"] == "run"]
        self.assertTrue(any("-n" in entry["args"] and "2-2" in entry["args"] for entry in runs))

        # Structure-only run (lib-check reused) does not match lib = 2 -> falls back to 1-2 and 1 granted slot.
        sf_log_path.write_text("")
        self.commit(worktree, {"README.md": "docs only\n"})
        self.lane("check", "a")
        receipt2 = self.receipt("a")
        self.assertEqual(receipt2["jobs"], 1)
        self.assertEqual(receipt2["jobs_decision"]["min_jobs"], 1)
        self.assertNotIn("min_slots", receipt2["jobs_decision"])
        self.assertEqual(receipt2["jobs_decision"]["granted"], 1)
        sf_log2: list[dict[str, Any]] = [json.loads(line) for line in sf_log_path.read_text().splitlines() if line]
        runs2 = [entry for entry in sf_log2 if entry["command"] == "run"]
        self.assertTrue(any("-n" in entry["args"] and "1-2" in entry["args"] for entry in runs2))


class MissingVerifierTests(LaneCase):
    """fathers#244: a missing verifier refuses cleanly instead of crashing with a FileNotFoundError traceback."""

    def test_check_refuses_cleanly_when_verifier_is_missing(self):
        worktree = self.created("a", "lib/**")
        (pathlib.Path(worktree) / "verify.sh").unlink()
        self.commit(worktree, {"lib/a.txt": "change\n"}, "drop verify.sh")
        output = self.lane("check", "a", ok=False)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("FileNotFoundError", output)
        self.assertIn("lanefather: verifier not found:", output)
        self.assertIn("verify.sh", output)
        self.assertIn("[checks].verify", output)
        self.assertIn(".lanefather.toml", output)

    def test_check_refuses_cleanly_when_configured_verifier_is_missing(self):
        self.commit(
            self.integration,
            {".lanefather.toml": '[checks]\nverify = "./scripts/missing-verify.sh"\n'},
            "point checks.verify to missing script",
        )
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/a.txt": "change\n"})
        output = self.lane("check", "a", ok=False)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("FileNotFoundError", output)
        self.assertIn("lanefather: verifier not found:", output)
        self.assertIn("scripts/missing-verify.sh", output)
        self.assertNotIn("/./", output)
        self.assertIn("[checks].verify", output)
        self.assertIn(".lanefather.toml", output)

    def test_check_refuses_cleanly_when_verifier_is_not_executable(self):
        worktree = self.created("a", "lib/**")
        (pathlib.Path(worktree) / "verify.sh").chmod(0o644)
        self.commit(worktree, {"lib/a.txt": "change\n"}, "chmod verify.sh non-executable")
        output = self.lane("check", "a", ok=False)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("PermissionError", output)
        self.assertIn("lanefather: cannot run verifier", output)
        self.assertIn("verify.sh", output)
        self.assertIn("[checks].verify", output)


class ScopedBaseAndOuterLeaseTests(LaneCase):
    """fathers#253: --base alongside --checks on scoped runs, outer 1-slot lease warning, and renamed checks on base."""

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def entries(self, lane_id: str) -> dict[str, dict[str, Any]]:
        return {e["check"]: e for e in self.receipt(lane_id)["classification"]["checks"]}

    def test_scoped_check_omits_base_without_capability_and_passes_base_with_capability(self):
        calls_file = self.tmp / "scoped-base-calls.jsonl"
        self.env["SCOPED_CALLS"] = str(calls_file)
        snippet = (
            'if args[:1] != ["--changed"] and os.environ.get("SCOPED_CALLS"):\n'
            '    with open(os.environ["SCOPED_CALLS"], "a") as out:\n'
            '        out.write(json.dumps({"label": os.environ.get("LANEFATHER_SLOT_LABEL", ""), "args": args}) + "\\n")\n'
        )
        verifier = (self.integration / "verify.sh").read_text().replace(
            "args = sys.argv[1:]\n", "args = sys.argv[1:]\n" + snippet,
        )
        self.commit(self.integration, {"verify.sh": verifier}, "record verify args without base capability")

        # 1. Without "base" capability: neither scoped check nor solo rerun receives --base.
        wt_no_cap = self.created("nocap", "calls/**")
        self.commit(wt_no_cap, {"calls/main.txt": "foo\nbar\n"})
        self.lane("check", "nocap", ok=False)
        nocap_calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
        self.assertGreaterEqual(len(nocap_calls), 2)
        for call in nocap_calls:
            self.assertNotIn("--base", call["args"], call)

        # 2. Advertise "base" in plan capabilities: scoped check and solo rerun receive --base; full & base rerun omit.
        calls_file.unlink()
        cap_verifier = verifier.replace(
            'plan = {"full": full, "paths": paths, "unowned": unowned, "checks": rows, "suites": suites, "reasons": reasons}',
            'plan = {"full": full, "paths": paths, "unowned": unowned, "checks": rows, "suites": suites, '
            '"reasons": reasons, "capabilities": ["base"]}',
        )
        self.commit(self.integration, {"verify.sh": cap_verifier}, "advertise base capability in plan")

        wt_scoped = self.created("scoped", "native/**")
        base_scoped = self.tip()
        self.commit(wt_scoped, {"native/straylight-core/corpus.txt": "v2\n"})
        self.lane("check", "scoped", ok=False)
        scoped_calls = {
            row["label"]: row["args"]
            for row in (json.loads(line) for line in calls_file.read_text().splitlines())
        }
        self.assertIn("--base", scoped_calls["lane-scoped"])
        self.assertEqual(scoped_calls["lane-scoped"][scoped_calls["lane-scoped"].index("--base") + 1], base_scoped)
        self.assertIn("--base", scoped_calls["lane-scoped-alone"])
        self.assertEqual(
            scoped_calls["lane-scoped-alone"][scoped_calls["lane-scoped-alone"].index("--base") + 1],
            base_scoped,
        )
        self.assertNotIn("--base", scoped_calls["lane-scoped-base"])

        wt_full = self.created("fullplan", "tooling/**")
        self.commit(wt_full, {"tooling/build.txt": "v2\n"})
        self.lane("check", "fullplan")
        full_args = json.loads(calls_file.read_text().splitlines()[-1])["args"]
        self.assertIn("--checks", full_args)
        self.assertNotIn("--base", full_args)

    def test_outer_one_slot_lease_warns_on_check_only_when_running_checks(self):
        self.env.update(SLOTFATHER_LEASE=str(self.tmp / "outer-one"), SLOTFATHER_SLOTS="1", SLOTFATHER_COUNT="1")
        wt = self.created("pinned", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\nbaz\n"})
        output = self.lane("check", "pinned")
        self.assertIn("lane: warning:", output)
        self.assertIn("outer Slotfather lease of 1 slot", output)
        self.assertIn("must not be wrapped in `slotfather run --`", output)
        # Re-checking the same green tree short-circuits and emits no outer-lease warning.
        again = self.lane("check", "pinned")
        self.assertNotIn("lane: warning:", again)

    def _verifier_without_plan_module(self, *, omit_unknown: bool = False) -> None:
        config = (self.integration / ".lanefather.toml").read_text().replace(
            'plan_module = "tools/verification_plan.py"\n', "",
        )
        self.commit(self.integration, {".lanefather.toml": config}, "remove plan_module like package-kit consumer")
        if omit_unknown:
            verifier = (self.integration / "verify.sh").read_text().replace(
                '    if any(id not in CHECKS for id in ids):\n'
                '        # Like verify.py, an unknown check id refuses the whole selection.\n'
                '        print("Unknown verification check or empty selection", file=sys.stderr)\n'
                '        sys.exit(64)\n'
                '    run_checks = [(id, CHECKS[id][0]) for id in ids]\n',
                '    run_checks = [(id, CHECKS[id][0]) for id in ids if id in CHECKS]\n',
            )
            self.commit(self.integration, {"verify.sh": verifier}, "verifier omits unknown checks from report")

    def test_renamed_check_on_lane_without_plan_module_is_regression_absent_on_base(self):
        self._verifier_without_plan_module()
        wt = self.created("renamed", "native/**")
        # Rename native-check -> native-check-v2 on the lane only (base still has native-check).
        lane_verify = (pathlib.Path(wt) / "verify.sh").read_text().replace('"native-check"', '"native-check-v2"')
        self.commit(
            wt,
            {
                "verify.sh": lane_verify,
                "native/straylight-core/corpus.txt": "v2\n",
            },
            "rename native check and break it",
        )
        output = self.lane("check", "renamed", ok=False)
        receipt = self.receipt("renamed")
        self.assertEqual(receipt["status"], "red")
        entry = self.entries("renamed")["native-check-v2"]
        self.assertEqual(entry["class"], "regression")
        self.assertEqual(entry["base"]["status"], "absent")
        self.assertEqual(entry["base"]["reason"], "the base defines no such check")
        self.assertIn("native-check-v2: regression: the base defines no such check", output)

    def test_multi_check_base_rerun_probes_individually_when_one_check_was_renamed(self):
        self._verifier_without_plan_module()
        wt = self.created("mixed", "calls/**", "native/**")
        lane_verify = (pathlib.Path(wt) / "verify.sh").read_text().replace('"native-check"', '"native-check-v2"')
        self.commit(
            wt,
            {
                "verify.sh": lane_verify,
                "calls/main.txt": "foo\nbar\n",
                "native/straylight-core/corpus.txt": "v2\n",
            },
            "break existing lib-check and renamed native-check-v2",
        )
        output = self.lane("check", "mixed", ok=False)
        receipt = self.receipt("mixed")
        self.assertEqual(receipt["status"], "red")
        entries = self.entries("mixed")
        self.assertEqual(entries["lib-check"]["class"], "regression")
        self.assertEqual(entries["lib-check"]["base"]["status"], "passed")
        self.assertEqual(entries["native-check-v2"]["class"], "regression")
        self.assertEqual(entries["native-check-v2"]["base"]["status"], "absent")
        self.assertIn("native-check-v2: regression: the base defines no such check", output)

    def test_renamed_check_omitted_from_valid_base_report_is_regression_absent(self):
        self._verifier_without_plan_module(omit_unknown=True)
        wt = self.created("omitted", "native/**")
        lane_verify = (pathlib.Path(wt) / "verify.sh").read_text().replace('"native-check"', '"native-check-v2"')
        self.commit(
            wt,
            {
                "verify.sh": lane_verify,
                "native/straylight-core/corpus.txt": "v2\n",
            },
            "rename native check with omitting base verifier",
        )
        self.lane("check", "omitted", ok=False)
        receipt = self.receipt("omitted")
        self.assertEqual(receipt["status"], "red")
        entry = self.entries("omitted")["native-check-v2"]
        self.assertEqual(entry["class"], "regression")
        self.assertEqual(entry["base"]["status"], "absent")
        self.assertEqual(entry["base"]["reason"], "the base defines no such check")

    def test_base_crash_with_invalid_step_size_stays_unclassified_not_absent(self):
        self._verifier_without_plan_module()
        crash_snippet = (
            'if os.environ.get("LANEFATHER_SLOT_LABEL", "").endswith("-base"):\n'
            '    print("ValueError: invalid step size", file=sys.stderr)\n'
            '    sys.exit(1)\n'
        )
        verifier = (self.integration / "verify.sh").read_text().replace(
            "args = sys.argv[1:]\n", "args = sys.argv[1:]\n" + crash_snippet,
        )
        self.commit(self.integration, {"verify.sh": verifier}, "simulate real base crash mentioning invalid step")
        wt = self.created("crash", "native/**")
        self.commit(wt, {"native/straylight-core/corpus.txt": "v2\n"})
        self.lane("check", "crash", ok=False)
        receipt = self.receipt("crash")
        self.assertEqual(receipt["status"], "red")
        entry = self.entries("crash")["native-check"]
        self.assertEqual(entry["class"], "unclassified")
        self.assertNotEqual(entry["base"]["status"], "absent")
        self.assertIn("wrote no verification report", entry["reason"])

    def test_multi_check_base_rerun_with_absent_and_pre_existing_failure(self):
        self.env["FIXTURE_FAILURE_EVIDENCE"] = "1"
        self._verifier_without_plan_module()
        # Break lib-check on base so it fails identically on both lane and base with comparable failure_evidence.
        self.commit(self.integration, {"calls/main.txt": "foo\nbar\n"}, "pre-existing lib failure on base")
        wt = self.created("premix", "calls/**", "native/**")
        lane_verify = (pathlib.Path(wt) / "verify.sh").read_text().replace('"native-check"', '"native-check-v2"')
        self.commit(
            wt,
            {
                "verify.sh": lane_verify,
                "calls/notes.txt": "foo\n",
                "native/straylight-core/corpus.txt": "v2\n",
            },
            "touch lib and break renamed native-check-v2",
        )
        self.lane("check", "premix", ok=False)
        receipt = self.receipt("premix")
        self.assertEqual(receipt["status"], "red")
        entries = self.entries("premix")
        self.assertEqual(entries["native-check-v2"]["class"], "regression")
        self.assertEqual(entries["native-check-v2"]["base"]["status"], "absent")
        self.assertEqual(entries["lib-check"]["class"], "pre-existing")
        self.assertIn("failure_comparison", entries["lib-check"])


if __name__ == "__main__":
    unittest.main()
