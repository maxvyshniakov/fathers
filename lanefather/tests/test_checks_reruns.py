"""Tests of check toolchain invalidation, dependency-blocked skips, and unblocked reruns (lanefather.checks)."""
from __future__ import annotations

import json
import pathlib
import textwrap
from typing import Any
import unittest

from support import RUST_STUB, LaneCase


class ToolchainAndEnvironmentReceiptTests(LaneCase):
    """#571 review findings: a receipt or its results never outlive the Rust toolchain they ran with."""

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def pin_rust(self):
        self.commit(self.integration, {"rust-toolchain.toml": '[toolchain]\nchannel = "1.98.1"\n',
                                       "tools/rust-toolchain.py": RUST_STUB}, "pin Rust")

    def test_a_green_receipt_is_not_current_after_the_rust_toolchain_changes(self):
        self.pin_rust()
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.assertEqual(self.receipt("a")["toolchain"]["bin"], "/rust/1.98.1")
        self.assertIn("Receipt current", self.lane("check", "a"))
        self.assertEqual(len(self.verify_runs()), 1)
        self.env["FIXTURE_RUST"] = "1.99.0"
        output = self.lane("check", "a")
        self.assertNotIn("Receipt current", output)
        self.assertIn("the Rust toolchain changed", output)
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        self.assertEqual(self.receipt("a")["toolchain"]["bin"], "/rust/1.99.0")
        self.env["FIXTURE_RUST"] = "missing"
        self.assertNotIn("Receipt current", self.lane("check", "a"))
        self.assertEqual(len(self.verify_runs()), 3)
        self.assertIn("refused", self.receipt("a")["toolchain"])

    def test_results_from_another_toolchain_are_not_reused_for_a_later_tree(self):
        self.pin_rust()
        worktree = self.created("a", "lib/**", "README.md")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.env["FIXTURE_RUST"] = "1.99.0"
        self.commit(worktree, {"README.md": "edited\n"})
        self.lane("check", "a")
        # Without the change only structure-check would run; lib passed under the old Rust.
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")

    def test_a_run_failing_outside_any_check_voids_its_results_and_none_are_reused(self):
        # Review of #422, finding 3: verify.sh exits non-zero (setup, the slot limiter) although every check of its
        # report passed; nothing of that run counts as passed.
        worktree = self.created("a", "lib/**", "README.md")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.env["FIXTURE_EXIT"] = "lane-a:3"
        self.lane("check", "a", ok=False)
        receipt = self.receipt("a")
        self.assertEqual({c: e["status"] for c, e in receipt["check_results"].items()},
                         {"structure-check": "void", "lib-check": "void"})
        self.assertEqual({r["suite"]: r["status"] for r in receipt["results"]}, {"structure": "failed", "lib": "failed"})
        del self.env["FIXTURE_EXIT"]
        self.commit(worktree, {"README.md": "edited\n"})
        self.lane("check", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check")
        self.assertFalse(any(r.get("reused_from") for r in self.receipt("a")["results"]))

    def test_an_environment_failed_run_passes_no_suite_and_none_of_its_results_are_reused(self):
        worktree = self.created("a", "lib/**", "native/**", "README.md")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n", "native/straylight-core/notes.txt": "lane\n"})
        self.env["FIXTURE_ENVIRONMENT"] = "toolchain changed"
        self.lane("check", "a", ok=False)
        results = {r["suite"]: r for r in self.receipt("a")["results"]}
        self.assertEqual({suite: r["status"] for suite, r in results.items()},
                         {"structure": "failed", "lib": "failed", "native": "failed"})
        self.assertEqual({r["environment"] for r in results.values()}, {"toolchain changed"})
        del self.env["FIXTURE_ENVIRONMENT"]
        self.commit(worktree, {"README.md": "edited\n"})
        self.lane("check", "a")
        self.assertEqual(self.verify_runs()[-1], "checks structure-check,lib-check,native-check")
        self.assertFalse(any(r.get("reused_from") for r in self.receipt("a")["results"]))


class DependencyBlockedTests(LaneCase):
    """fathers#195: checks skipped/blocked because a dependency failed stay separate from executed failures, never
    consume rerun budget, are never labeled independent regressions, and never turn green."""

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def install_dep_verify(self) -> pathlib.Path:
        """A consumer verify script (like Repo-Kit, no plan_module) with 1 failed prerequisite, 3 dependency-blocked
        children (two `blocked`, one `skipped` with `blocked_by`), and 2 unrelated executed failures."""
        runs_log = self.tmp / "dep-verify-runs.log"
        config = (self.integration / ".lanefather.toml").read_text().replace(
            'plan_module = "tools/verification_plan.py"\n', "")
        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, os, pathlib, sys, uuid
            args = sys.argv[1:]
            root = pathlib.Path(__file__).resolve().parent
            checks = [
                ("verify:unit", "verify"),
                ("verify:typecheck", "verify"),
                ("verify:lint", "verify"),
                ("browser:build", "browser"),
                ("browser:storybook", "browser"),
                ("browser:story-baseline", "browser"),
            ]
            if args[:1] == ["--changed"]:
                paths = ["src/unit.test.ts", "src/app.ts"]
                rows = [{{"id": cid, "suite": suite, "paths": paths, "producer": False}} for cid, suite in checks]
                print(json.dumps({{"full": True, "paths": paths, "unowned": [], "suites": ["verify", "browser"],
                                   "reasons": {{"verify": paths, "browser": paths}}, "checks": rows}}))
                sys.exit(0)
            ids = args[args.index("--checks") + 1].split(",") if "--checks" in args else [c for c, _ in checks]
            label = os.environ.get("LANEFATHER_SLOT_LABEL", "")
            with open({str(runs_log)!r}, "a") as handle:
                handle.write(label + " " + ",".join(ids) + "\\n")
            run = root / ".build" / "verification-runs" / ("dep-" + uuid.uuid4().hex[:8])
            run.mkdir(parents=True)
            rows = []
            exit_code = 0
            flaky = os.environ.get("DEP_FLAKY_ALONE") == "1"
            alone_blocked = os.environ.get("DEP_ALONE_BLOCKED", "")
            for cid in ids:
                if label.endswith("-base"):
                    st, bb = "passed", []
                elif label.endswith("-alone"):
                    if cid == alone_blocked:
                        st, bb = "blocked", ["verify:unit"]
                    elif flaky:
                        st, bb = "passed", []
                    else:
                        st, bb = "failed", []
                else:
                    if cid in ("verify:unit", "verify:typecheck", "verify:lint"):
                        st, bb = "failed", []
                    elif cid == "browser:story-baseline":
                        st, bb = "skipped", ["verify:unit"]
                    else:
                        st, bb = "blocked", ["verify:unit"]
                if st in ("failed", "blocked") or (st == "skipped" and bb):
                    exit_code = 1
                rows.append({{"id": cid, "status": st, "exit": 0 if st == "passed" else 1,
                             "log": str(run / (cid.replace(":", "_") + ".log")), "seconds": 0.01,
                             **({{"blocked_by": bb}} if bb else {{}})}})
            report = {{"exit": exit_code, "checks": rows, "stopped": None}}
            (run / "report.json").write_text(json.dumps(report))
            print("Verification report: " + str(run / "report.json"))
            sys.exit(exit_code)
            """)
        self.commit(self.integration, {".lanefather.toml": config, "verify.sh": script},
                    "install dependency-blocked verify fixture")
        return runs_log

    def test_dependency_blocked_children_do_not_consume_rerun_budget_or_become_regressions(self):
        runs_log = self.install_dep_verify()
        self.env["LANEFATHER_RERUNS"] = "3"
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/unit.test.ts": "expect(1).toBe(2)\n", "src/app.ts": "export const x = 1\n"})
        output = self.lane("check", "a", ok=False)
        receipt = self.receipt("a")
        self.assertEqual(receipt["status"], "red")

        # Only the 3 actual executed failures were rerun on alone and base; none of the 3 blocked browser checks.
        logged = runs_log.read_text().splitlines()
        self.assertEqual(len(logged), 3, logged)
        self.assertEqual(logged[1], "lane-a-alone verify:unit,verify:typecheck,verify:lint")
        self.assertEqual(logged[2], "lane-a-base verify:unit,verify:typecheck,verify:lint")

        # Suite results separate executed failures from blocked suites.
        by_suite = {r["suite"]: r for r in receipt["results"]}
        self.assertEqual(by_suite["verify"]["status"], "failed")
        self.assertEqual(by_suite["verify"]["failed_checks"], ["verify:unit", "verify:typecheck", "verify:lint"])
        self.assertEqual(by_suite["browser"]["status"], "blocked")
        self.assertEqual(by_suite["browser"]["failed_checks"], [])
        self.assertEqual(by_suite["browser"]["blocked_checks"],
                         ["browser:build", "browser:storybook", "browser:story-baseline"])

        # Per-check results preserve blocked status and blocked_by (including skipped-with-blocked_by).
        for child in ("browser:build", "browser:storybook", "browser:story-baseline"):
            self.assertEqual(receipt["check_results"][child]["status"], "blocked")
            self.assertEqual(receipt["check_results"][child]["blocked_by"], ["verify:unit"])

        # Classification: executed failures are regressions; blocked children are dependency-blocked, never regressions.
        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        for real in ("verify:unit", "verify:typecheck", "verify:lint"):
            self.assertEqual(entries[real]["class"], "regression")
        for child in ("browser:build", "browser:storybook", "browser:story-baseline"):
            self.assertEqual(entries[child]["class"], "dependency-blocked")
            self.assertEqual(entries[child]["blocked_by"], ["verify:unit"])
            self.assertIn("skipped because dependency failed (verify:unit)", entries[child]["reason"])
            self.assertIn(f"{child}: dependency-blocked: skipped because dependency failed (verify:unit)", output)

        # First receipt and changed_red_tests attribute only executed failures, never blocked browser checks.
        self.assertEqual(receipt["first"]["failed_checks"], ["verify:unit", "verify:typecheck", "verify:lint"])
        self.assertEqual({t["check"] for t in receipt["changed_red_tests"]},
                         {"verify:unit", "verify:typecheck", "verify:lint"})

    def test_dependency_blocked_children_keep_receipt_red_even_when_executed_failures_pass_alone(self):
        self.install_dep_verify()
        self.env.update(LANEFATHER_RERUNS="3", DEP_FLAKY_ALONE="1")
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/app.ts": "export const x = 2\n"})
        self.lane("check", "a", ok=False)
        receipt = self.receipt("a")
        self.assertEqual(receipt["status"], "red")
        self.assertFalse(receipt["classification"]["green"])
        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        self.assertEqual(entries["verify:unit"]["class"], "flaky-under-load")
        self.assertEqual(entries["browser:build"]["class"], "dependency-blocked")

    def test_check_blocked_during_alone_rerun_is_not_labeled_regression(self):
        runs_log = self.install_dep_verify()
        self.env.update(LANEFATHER_RERUNS="3", DEP_ALONE_BLOCKED="verify:typecheck")
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/app.ts": "export const x = 3\n"})
        self.lane("check", "a", ok=False)
        receipt = self.receipt("a")
        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        self.assertEqual(entries["verify:typecheck"]["class"], "dependency-blocked")
        self.assertEqual(entries["verify:typecheck"]["blocked_by"], ["verify:unit"])
        # verify:typecheck was blocked on alone, so it did not run on base.
        logged = runs_log.read_text().splitlines()
        self.assertEqual(logged[2], "lane-a-base verify:unit,verify:lint")

    def install_interleaved_verify(self) -> pathlib.Path:
        """Sid-Kit's report order in one suite: the failed prerequisite, three children it blocks (one waits on
        another child, one is `skipped` with `blocked_by`), then two unrelated executed failures. On the base the
        prerequisite and browser:story pass and browser:control fails."""
        runs_log = self.tmp / "interleaved-verify-runs.log"
        config = (self.integration / ".lanefather.toml").read_text().replace(
            'plan_module = "tools/verification_plan.py"\n', "")
        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, os, pathlib, sys, uuid
            args = sys.argv[1:]
            root = pathlib.Path(__file__).resolve().parent
            lane = {{"browser:unit": ("failed", []), "browser:build": ("blocked", ["browser:unit"]),
                     "browser:storybook": ("blocked", ["browser:build"]),
                     "browser:baseline": ("skipped", ["browser:build"]),
                     "browser:control": ("failed", []), "browser:story": ("failed", [])}}
            if args[:1] == ["--changed"]:
                paths = ["src/unit.test.ts"]
                rows = [{{"id": cid, "suite": "browser", "paths": paths, "producer": False}} for cid in lane]
                print(json.dumps({{"full": True, "paths": paths, "unowned": [], "suites": ["browser"],
                                   "reasons": {{"browser": paths}}, "checks": rows}}))
                sys.exit(0)
            ids = args[args.index("--checks") + 1].split(",") if "--checks" in args else list(lane)
            label = os.environ.get("LANEFATHER_SLOT_LABEL", "")
            with open({str(runs_log)!r}, "a") as handle:
                handle.write(label + " " + ",".join(ids) + "\\n")
            run = root / ".build" / "verification-runs" / ("dep-" + uuid.uuid4().hex[:8])
            run.mkdir(parents=True)
            rows = []
            for cid in ids:
                if label.endswith("-base"):
                    st, bb = ("failed" if cid == "browser:control" else "passed"), []
                elif label.endswith("-alone"):
                    st, bb = "failed", []
                else:
                    st, bb = lane[cid]
                row = {{"id": cid, "suite": "browser", "status": st, "blocked_by": bb,
                        "log": str(run / (cid.replace(":", "_") + ".log")), "seconds": 0.01}}
                if st == "failed":
                    row["failure_evidence"] = {{
                        "version": 1, "comparable": True, "context": "interleaved-v1",
                        "cases": [{{"id": cid, "signature": "fail:" + cid}}],
                    }}
                rows.append(row)
            exit_code = int(any(r["status"] != "passed" for r in rows))
            (run / "report.json").write_text(json.dumps({{"exit": exit_code, "checks": rows, "stopped": None}}))
            print("Verification report: " + str(run / "report.json"))
            sys.exit(exit_code)
            """)
        self.commit(self.integration, {".lanefather.toml": config, "verify.sh": script},
                    "install interleaved dependency-blocked verify fixture")
        return runs_log

    def test_children_listed_before_unrelated_failures_take_no_rerun_slot(self):
        runs_log = self.install_interleaved_verify()
        self.env["LANEFATHER_RERUNS"] = "2"
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/unit.test.ts": "expect(1).toBe(2)\n"})
        output = self.lane("check", "a", ok=False)
        receipt = self.receipt("a")
        self.assertEqual(receipt["status"], "red")
        children = ["browser:build", "browser:storybook", "browser:baseline"]

        # The two rerun slots go to the first two checks that ran and failed, never to a child listed before them.
        logged = runs_log.read_text().splitlines()
        self.assertEqual(logged[1:], ["lane-a-alone browser:unit,browser:control",
                                      "lane-a-base browser:unit,browser:control"])
        result = receipt["results"][0]
        self.assertEqual(result["failed_checks"], ["browser:unit", "browser:control", "browser:story"])
        self.assertEqual(result["blocked_checks"], children)

        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        self.assertEqual(entries["browser:unit"]["class"], "regression")
        self.assertEqual(entries["browser:control"]["class"], "pre-existing")
        self.assertEqual(entries["browser:story"]["class"], "unclassified")
        self.assertEqual(entries["browser:story"]["reason"], "only the first 2 red checks rerun")
        for child in children:
            self.assertEqual(receipt["check_results"][child]["status"], "blocked")
            self.assertEqual(entries[child]["class"], "dependency-blocked")
            self.assertEqual(entries[child]["prerequisites"], ["browser:unit"])
            self.assertIn("failed prerequisite: browser:unit (regression); not run, so unresolved",
                          entries[child]["reason"])
        self.assertEqual(entries["browser:storybook"]["blocked_by"], ["browser:build"])
        self.assertIn("lane: 3 dependency-blocked check(s) did not run and stay unresolved behind browser:unit",
                      output)
        self.assertEqual(receipt["first"]["failed_checks"], ["browser:unit", "browser:control", "browser:story"])

        # The land refusal names the regression and the children that never ran, behind their prerequisite.
        from lanefather import land, receipts
        verdict = receipts.check_verdict(None, receipt, receipt["tree"])
        assert isinstance(verdict, receipts.Red)
        self.assertEqual(verdict.regressions, ("browser:unit",))
        self.assertEqual(verdict.blocked, tuple(children))
        self.assertEqual(verdict.prerequisites, ("browser:unit",))
        problem = land.merge_problem("a", receipt["head"], receipt["tree"], receipt)
        self.assertIn("; dependency-blocked, not run: browser:build, browser:storybook, browser:baseline behind "
                      "browser:unit", str(problem))


class UnblockedAfterRerunTests(LaneCase):
    """fathers#240: after a failed prerequisite passes its own rerun (`flaky-under-load`), `lanefather check` runs
    the checks it blocked in the same invocation, transitively and within the rerun budget."""

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def install_unblock_verify(self, checks: list[tuple[str, str]], deps: dict[str, list[str]]) -> pathlib.Path:
        """Configurable verify script with explicit dependencies and per-check flake/fail rules via env vars:
        - `UNBLOCK_FAIL_FIRST`: comma-separated check ids that fail on their first non-base run (and pass on alone).
        - `UNBLOCK_FAIL_ALWAYS`: comma-separated check ids that fail on both lane and alone runs (pass on base)."""
        runs_log = self.tmp / "unblock-verify-runs.log"
        config = (self.integration / ".lanefather.toml").read_text().replace(
            'plan_module = "tools/verification_plan.py"\n', "")
        suites = list(dict.fromkeys(suite for _, suite in checks))
        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, os, pathlib, sys, uuid
            args = sys.argv[1:]
            root = pathlib.Path(__file__).resolve().parent
            checks = {checks!r}
            suites = {suites!r}
            deps = {deps!r}
            if args[:1] == ["--changed"]:
                paths = ["src/app.ts"]
                rows = [{{"id": cid, "suite": suite, "paths": paths, "producer": False}} for cid, suite in checks]
                print(json.dumps({{"full": True, "paths": paths, "unowned": [], "suites": suites,
                                   "reasons": {{s: paths for s in suites}}, "checks": rows}}))
                sys.exit(0)
            ids = args[args.index("--checks") + 1].split(",") if "--checks" in args else [c for c, _ in checks]
            label = os.environ.get("LANEFATHER_SLOT_LABEL", "")
            with open({str(runs_log)!r}, "a") as handle:
                handle.write(label + " " + ",".join(ids) + "\\n")
            fail_first = set(filter(None, os.environ.get("UNBLOCK_FAIL_FIRST", "").split(",")))
            fail_always = set(filter(None, os.environ.get("UNBLOCK_FAIL_ALWAYS", "").split(",")))
            run = root / ".build" / "verification-runs" / ("unblock-" + uuid.uuid4().hex[:8])
            run.mkdir(parents=True)
            rows = []
            statuses = {{}}
            for cid in ids:
                if label.endswith("-base"):
                    st, bb = "passed", []
                elif label.endswith("-alone"):
                    st, bb = ("failed" if cid in fail_always else "passed"), []
                else:
                    blockers = [d for d in deps.get(cid, []) if statuses.get(d) in ("failed", "blocked")]
                    if blockers:
                        st, bb = "blocked", blockers
                    elif cid in fail_always or cid in fail_first:
                        st, bb = "failed", []
                    else:
                        st, bb = "passed", []
                statuses[cid] = st
                rows.append({{"id": cid, "status": st, "exit": 0 if st == "passed" else 1,
                             "log": str(run / (cid.replace(":", "_") + ".log")), "seconds": 0.01,
                             **({{"blocked_by": bb}} if bb else {{}})}})
            exit_code = int(any(r["status"] != "passed" for r in rows))
            (run / "report.json").write_text(json.dumps({{"exit": exit_code, "checks": rows, "stopped": None}}))
            print("Verification report: " + str(run / "report.json"))
            sys.exit(exit_code)
            """)
        self.commit(self.integration, {".lanefather.toml": config, "verify.sh": script},
                    "install unblock-after-rerun verify fixture")
        return runs_log

    def test_unblocked_children_run_and_turn_receipt_green_when_prerequisite_passes_alone(self):
        runs_log = self.install_unblock_verify(
            [("core:a", "core"), ("app:b", "app"), ("app:c", "app")],
            {"app:b": ["core:a"], "app:c": ["core:a"]},
        )
        self.env["UNBLOCK_FAIL_FIRST"] = "core:a"
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/app.ts": "export const x = 1\n"})
        output = self.lane("check", "a")
        receipt = self.receipt("a")

        self.assertEqual(runs_log.read_text().splitlines(), [
            "lane-a core:a,app:b,app:c",
            "lane-a-alone core:a",
            "lane-a app:b,app:c",
        ])
        self.assertEqual(receipt["status"], "green")
        self.assertTrue(receipt["classification"]["green"])
        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        self.assertEqual(list(entries), ["core:a"])
        self.assertEqual(entries["core:a"]["class"], "flaky-under-load")
        self.assertEqual(entries["core:a"]["alone"]["status"], "passed")
        self.assertNotEqual(receipt["check_results"]["core:a"]["log"], entries["core:a"]["alone"]["log"])
        self.assertEqual(receipt["check_results"]["core:a"]["status"], "failed")
        self.assertEqual(receipt["check_results"]["app:b"]["status"], "passed")
        self.assertEqual(receipt["check_results"]["app:c"]["status"], "passed")
        by_suite = {r["suite"]: r for r in receipt["results"]}
        self.assertEqual(by_suite["app"]["status"], "passed")
        self.assertNotIn("blocked_checks", by_suite["app"])
        self.assertIn("core:a passed rerun by itself; running 2 unblocked check(s): app:b, app:c", output)
        self.assertIn("core:a: flaky-under-load", output)
        self.assertIn("raw verification RED; every red check is flaky-under-load or pre-existing: "
                      "the classified receipt is green", output)
        self.env.pop("UNBLOCK_FAIL_FIRST", None)
        self.lane("land", "a")

    def test_children_stay_unresolved_when_prerequisite_fails_rerun_too(self):
        runs_log = self.install_unblock_verify(
            [("core:a", "core"), ("app:b", "app"), ("app:c", "app")],
            {"app:b": ["core:a"], "app:c": ["core:a"]},
        )
        self.env["UNBLOCK_FAIL_ALWAYS"] = "core:a"
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/app.ts": "export const x = 2\n"})
        self.lane("check", "a", ok=False)
        receipt = self.receipt("a")

        self.assertEqual(runs_log.read_text().splitlines(), [
            "lane-a core:a,app:b,app:c",
            "lane-a-alone core:a",
            "lane-a-base core:a",
        ])
        self.assertEqual(receipt["status"], "red")
        self.assertFalse(receipt["classification"]["green"])
        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        self.assertEqual(entries["core:a"]["class"], "regression")
        for child in ("app:b", "app:c"):
            self.assertEqual(entries[child]["class"], "dependency-blocked")
            self.assertEqual(entries[child]["prerequisites"], ["core:a"])
            self.assertEqual(receipt["check_results"][child]["status"], "blocked")

    def test_check_blocked_by_permanent_failure_and_flaky_prerequisite_stays_blocked(self):
        runs_log = self.install_unblock_verify(
            [("core:a", "core"), ("core:d", "core"), ("app:b", "app"), ("app:c", "app")],
            {"app:b": ["core:a", "core:d"], "app:c": ["core:a"]},
        )
        self.env.update(UNBLOCK_FAIL_FIRST="core:a", UNBLOCK_FAIL_ALWAYS="core:d")
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/app.ts": "export const x = 3\n"})
        self.lane("check", "a", ok=False)
        receipt = self.receipt("a")

        # Only app:c (blocked solely by core:a) runs; app:b stays blocked behind core:d.
        self.assertEqual(runs_log.read_text().splitlines(), [
            "lane-a core:a,core:d,app:b,app:c",
            "lane-a-alone core:a,core:d",
            "lane-a-base core:d",
            "lane-a app:c",
        ])
        self.assertEqual(receipt["status"], "red")
        self.assertFalse(receipt["classification"]["green"])
        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        self.assertEqual(entries["core:a"]["class"], "flaky-under-load")
        self.assertEqual(entries["core:d"]["class"], "regression")
        self.assertEqual(entries["app:b"]["class"], "dependency-blocked")
        self.assertNotIn("app:c", entries)
        self.assertEqual(receipt["check_results"]["app:b"]["status"], "blocked")
        self.assertEqual(receipt["check_results"]["app:c"]["status"], "passed")
        by_suite = {r["suite"]: r for r in receipt["results"]}
        self.assertEqual(by_suite["app"]["status"], "blocked")
        self.assertEqual(by_suite["app"]["blocked_checks"], ["app:b"])

    def test_chained_dependencies_unblock_transitively_and_turn_green(self):
        runs_log = self.install_unblock_verify(
            [("chain:a", "chain"), ("chain:b", "chain"), ("chain:c", "chain")],
            {"chain:b": ["chain:a"], "chain:c": ["chain:b"]},
        )
        self.env["UNBLOCK_FAIL_FIRST"] = "chain:a,chain:b"
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/app.ts": "export const x = 4\n"})
        self.lane("check", "a")
        receipt = self.receipt("a")

        self.assertEqual(runs_log.read_text().splitlines(), [
            "lane-a chain:a,chain:b,chain:c",
            "lane-a-alone chain:a",
            "lane-a chain:b,chain:c",
            "lane-a-alone chain:b",
            "lane-a chain:c",
        ])
        self.assertEqual(receipt["status"], "green")
        self.assertTrue(receipt["classification"]["green"])
        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        self.assertEqual({k: v["class"] for k, v in entries.items()},
                         {"chain:a": "flaky-under-load", "chain:b": "flaky-under-load"})
        self.assertEqual(len(receipt["classification"]["runs"]), 2)
        self.assertEqual(receipt["check_results"]["chain:c"]["status"], "passed")
        self.assertNotIn("blocked_checks", receipt["results"][0])

    def test_chained_dependencies_respect_rerun_budget_across_waves(self):
        runs_log = self.install_unblock_verify(
            [("chain:a", "chain"), ("chain:b", "chain"), ("chain:c", "chain")],
            {"chain:b": ["chain:a"], "chain:c": ["chain:b"]},
        )
        self.env.update(LANEFATHER_RERUNS="1", UNBLOCK_FAIL_FIRST="chain:a,chain:b")
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/app.ts": "export const x = 5\n"})
        self.lane("check", "a", ok=False)
        receipt = self.receipt("a")

        # chain:a uses the 1 rerun slot; chain:b fails in wave 2 and must not rerun.
        self.assertEqual(runs_log.read_text().splitlines(), [
            "lane-a chain:a,chain:b,chain:c",
            "lane-a-alone chain:a",
            "lane-a chain:b,chain:c",
        ])
        self.assertEqual(receipt["status"], "red")
        self.assertFalse(receipt["classification"]["green"])
        entries = {e["check"]: e for e in receipt["classification"]["checks"]}
        self.assertEqual(entries["chain:a"]["class"], "flaky-under-load")
        self.assertEqual(entries["chain:b"]["class"], "unclassified")
        self.assertEqual(entries["chain:b"]["reason"], "the rerun budget (1) was used by earlier checks")
        self.assertEqual(entries["chain:c"]["class"], "dependency-blocked")
        self.assertEqual(entries["chain:c"]["prerequisites"], ["chain:b"])

    def test_scoped_plan_with_producer_unblocks_and_turns_receipt_green(self):
        runs_log = self.tmp / "scoped-producer-runs.log"
        config = (self.integration / ".lanefather.toml").read_text().replace(
            'plan_module = "tools/verification_plan.py"\n', "")
        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, os, pathlib, sys, uuid
            args = sys.argv[1:]
            root = pathlib.Path(__file__).resolve().parent
            if args[:1] == ["--changed"]:
                paths = ["src/app.ts"]
                rows = [
                    {{"id": "verify:unit", "suite": "verify", "paths": paths, "producer": False}},
                    {{"id": "browser:build", "suite": "browser", "paths": [], "producer": True}},
                    {{"id": "browser:story", "suite": "browser", "paths": paths, "producer": False}},
                ]
                print(json.dumps({{"full": False, "paths": paths, "unowned": [], "suites": ["verify", "browser"],
                                   "reasons": {{"verify": paths, "browser": paths}}, "checks": rows}}))
                sys.exit(0)
            ids = args[args.index("--checks") + 1].split(",") if "--checks" in args else ["verify:unit", "browser:story"]
            label = os.environ.get("LANEFATHER_SLOT_LABEL", "")
            with open({str(runs_log)!r}, "a") as handle:
                handle.write(label + " " + ",".join(ids) + "\\n")
            run = root / ".build" / "verification-runs" / ("prod-" + uuid.uuid4().hex[:8])
            run.mkdir(parents=True)
            fail_first = os.environ.get("PROD_FAIL_FIRST") == "1"
            rows = []
            if label.endswith("-alone") or label.startswith("post-merge-"):
                for cid in ids:
                    rows.append({{"id": cid, "status": "passed", "exit": 0,
                                 "log": str(run / (cid.replace(":", "_") + ".log")), "seconds": 0.01}})
            elif "verify:unit" in ids and fail_first:
                rows = [
                    {{"id": "verify:unit", "status": "failed", "exit": 1,
                      "log": str(run / "verify_unit.log"), "seconds": 0.01}},
                    {{"id": "browser:build", "status": "blocked", "exit": 1, "blocked_by": ["verify:unit"],
                      "log": str(run / "browser_build.log"), "seconds": 0.01}},
                    {{"id": "browser:story", "status": "blocked", "exit": 1, "blocked_by": ["browser:build"],
                      "log": str(run / "browser_story.log"), "seconds": 0.01}},
                ]
            else:
                # Running --checks browser:story executes its producer browser:build and then browser:story.
                rows = [
                    {{"id": "browser:build", "status": "passed", "exit": 0,
                      "log": str(run / "browser_build.log"), "seconds": 0.01}},
                    {{"id": "browser:story", "status": "passed", "exit": 0,
                      "log": str(run / "browser_story.log"), "seconds": 0.01}},
                ]
            exit_code = int(any(r["status"] != "passed" for r in rows))
            (run / "report.json").write_text(json.dumps({{"exit": exit_code, "checks": rows, "stopped": None}}))
            print("Verification report: " + str(run / "report.json"))
            sys.exit(exit_code)
            """)
        self.commit(self.integration, {".lanefather.toml": config, "verify.sh": script},
                    "install scoped producer verify fixture")
        self.env["PROD_FAIL_FIRST"] = "1"
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/app.ts": "export const p = 1\n"})
        self.lane("check", "a")
        receipt = self.receipt("a")
        self.assertEqual(runs_log.read_text().splitlines(), [
            "lane-a verify:unit,browser:story",
            "lane-a-alone verify:unit",
            "lane-a browser:story",
        ])
        self.assertEqual(receipt["status"], "green")
        self.assertTrue(receipt["classification"]["green"])
        self.assertEqual([e["check"] for e in receipt["classification"]["checks"]], ["verify:unit"])
        self.assertEqual(receipt["check_results"]["browser:story"]["status"], "passed")
        self.env.pop("PROD_FAIL_FIRST", None)
        self.lane("land", "a")

    def test_whole_suite_plan_unblocks_without_narrowing_suite_to_single_check(self):
        runs_log = self.tmp / "whole-suite-runs.log"
        config = (self.integration / ".lanefather.toml").read_text().replace(
            'plan_module = "tools/verification_plan.py"\n', "")
        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, os, pathlib, sys, uuid
            args = sys.argv[1:]
            root = pathlib.Path(__file__).resolve().parent
            if args[:1] == ["--changed"]:
                paths = ["src/app.ts"]
                print(json.dumps({{"full": True, "paths": paths, "unowned": [], "suites": ["core", "app"],
                                   "reasons": {{"core": paths, "app": paths}}}}))
                sys.exit(0)
            label = os.environ.get("LANEFATHER_SLOT_LABEL", "")
            sel = (args[args.index("--checks") + 1] if "--checks" in args
                   else "suites:" + args[args.index("--suites") + 1])
            with open({str(runs_log)!r}, "a") as handle:
                handle.write(label + " " + sel + "\\n")
            run = root / ".build" / "verification-runs" / ("whole-" + uuid.uuid4().hex[:8])
            run.mkdir(parents=True)
            if label.endswith("-alone") or "--checks" in args:
                ids = args[args.index("--checks") + 1].split(",")
                rows = [{{"id": cid, "status": "passed", "exit": 0,
                          "log": str(run / (cid.replace(":", "_") + ".log")), "seconds": 0.01}} for cid in ids]
            else:
                rows = [
                    {{"id": "core:a", "status": "failed", "exit": 1, "log": str(run / "core_a.log"), "seconds": 0.01}},
                    {{"id": "app:b", "status": "blocked", "exit": 1, "blocked_by": ["core:a"],
                      "log": str(run / "app_b.log"), "seconds": 0.01}},
                ]
            exit_code = int(any(r["status"] != "passed" for r in rows))
            (run / "report.json").write_text(json.dumps({{"exit": exit_code, "checks": rows, "stopped": None}}))
            print("Verification report: " + str(run / "report.json"))
            sys.exit(exit_code)
            """)
        self.commit(self.integration, {".lanefather.toml": config, "verify.sh": script},
                    "install whole-suite unblock fixture")
        worktree = self.created("a", "src/**")
        self.commit(worktree, {"src/app.ts": "export const w = 1\n"})
        self.lane("check", "a")
        receipt = self.receipt("a")
        self.assertEqual(runs_log.read_text().splitlines(), [
            "lane-a suites:core,app",
            "lane-a-alone core:a",
            "lane-a app:b",
        ])
        self.assertEqual(receipt["status"], "green")
        by_suite = {r["suite"]: r for r in receipt["results"]}
        self.assertEqual(by_suite["app"]["status"], "passed")
        self.assertNotIn("checks", by_suite["app"])
        # All 3 runs (wave 1, solo rerun, wave 2) appear in leases.
        self.assertEqual(len(receipt["leases"]), 3)

    def test_stopped_first_tier_does_not_spend_heavier_stage_rerun_budget(self):
        # Break structure-check on both base and lane (#639 pre-existing), and flake lib-check once with RERUNS=1.
        self.commit(
            self.integration,
            {"checks/structure.sh": "test ! -e lib/structure-broken\n", "lib/structure-broken": "pre-existing\n"},
            "break structure on base",
        )
        flag = self.tmp / "heavier-flake-once"
        flag.touch()
        self.env.update(LANEFATHER_RERUNS="1", FIXTURE_FLAKE_ONCE=f"lib-check:{flag}", FIXTURE_FAILURE_EVIDENCE="1")
        worktree = self.created("cheap", "lib/**")
        self.commit(worktree, {"lib/thing.txt": "lane\n"})
        self.lane("check", "cheap")
        receipt = self.receipt("cheap")
        self.assertEqual(receipt["status"], "green")
        entries = {e["check"]: e["class"] for e in receipt["classification"]["checks"]}
        self.assertEqual(entries, {"structure-check": "pre-existing", "lib-check": "flaky-under-load"})


if __name__ == "__main__":
    unittest.main()
