"""fathers#113: comparable failing-case evidence is required before classifying a red check pre-existing."""
from __future__ import annotations

import contextlib
import copy
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from lanefather import config, history, plan, state, verify


def _evidence(
    cases: list[dict[str, Any]],
    *,
    context: str = "valid-runtime-049/report-v1",
    comparable: Any = True,
    version: Any = 1,
) -> dict[str, Any]:
    return {
        "version": version,
        "comparable": comparable,
        "context": context,
        "cases": cases,
    }


def _report(
    check_id: str = "journeys",
    *,
    cases: list[dict[str, Any]] | None = None,
    context: str = "valid-runtime-049/report-v1",
    exit_code: Any = 1,
    process_exit: Any = None,
    status: str = "failed",
    extra_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"id": check_id, "status": status}
    if status == "failed":
        row["failure_evidence"] = _evidence(
            cases if cases is not None else [{"id": "picker opens", "signature": "assert: reveal requested"}],
            context=context,
        )
    rep: dict[str, Any] = {"exit": exit_code, "checks": [row, *(extra_rows or [])]}
    if process_exit is not None:
        rep["_lanefather_process_exit"] = process_exit
    return rep


class FailureEvidenceTests(unittest.TestCase):
    def test_equivalent_complete_cases_are_order_independent(self) -> None:
        candidate = _report(cases=[
            {"id": "picker opens", "signature": "assert: reveal requested"},
            {"id": "save", "signature": "assert: saved"},
        ])
        base = copy.deepcopy(candidate)
        base["checks"][0]["failure_evidence"]["cases"].reverse()
        self.assertIsNone(verify.comparison_problem([candidate, base], "journeys", ["journeys"]))

    def test_candidate_subset_of_base_cases_is_pre_existing_when_context_matches(self) -> None:
        candidate = _report(cases=[{"id": "case-a", "signature": "assert: a"}])
        alone = copy.deepcopy(candidate)
        base = _report(cases=[
            {"id": "case-a", "signature": "assert: a"},
            {"id": "case-b", "signature": "assert: b"},
        ])
        self.assertIsNone(verify.comparison_problem([candidate, alone, base], "journeys", ["journeys"]))

    def test_candidate_introducing_new_case_on_top_of_base_failure_stays_red(self) -> None:
        candidate = _report(cases=[
            {"id": "case-a", "signature": "assert: a"},
            {"id": "case-new", "signature": "assert: new"},
        ])
        alone = copy.deepcopy(candidate)
        base = _report(cases=[{"id": "case-a", "signature": "assert: a"}])
        problem = verify.comparison_problem([candidate, alone, base], "journeys", ["journeys"])
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("case-new", problem)

    def test_live_sid_kit_case_different_failing_checks_in_same_step_must_stay_red(self) -> None:
        """2026-11-23 sid-kit sk78-land-polish: lane failed check-has-selectors, base failed
        check-token-preview-scroll inside browser:browser checks -> must stay unclassified/RED."""
        check_id = "browser:browser checks"
        candidate = _report(
            check_id,
            context="sid-kit-browser-v1",
            cases=[{"id": "check-has-selectors", "signature": "assert: selector missing"}],
        )
        alone = copy.deepcopy(candidate)
        base = _report(
            check_id,
            context="sid-kit-browser-v1",
            cases=[{"id": "check-token-preview-scroll", "signature": "assert: scroll offset"}],
        )
        fresh = [{"suite": "browser", "status": "failed", "failed_checks": [check_id]}]
        store = state.Store(config.DEFAULT)

        with (
            patch.object(verify, "run_rerun", side_effect=[
                ("run-alone", alone, Path("/tmp/alone.log"), None, None),
                ("run-base", base, Path("/tmp/base.log"), None, None),
            ]),
            patch.object(verify.worktrees, "base_checkout", return_value=contextlib.nullcontext("/tmp/base")),
            patch.object(plan, "known_checks", return_value={check_id}),
            patch.object(plan, "supports_only", return_value=False),
            patch.object(verify, "rerun_cap", return_value=5),
        ):
            result = verify.classify_failures(
                store, {"id": "sk78-land-polish", "ticket": "78"}, "/tmp/lane", "deadbeef" * 5, fresh, candidate,
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result["green"])
        self.assertEqual(result["raw_status"], "red")
        self.assertEqual(result["checks"][0]["class"], "unclassified")
        self.assertIn("check-has-selectors", result["checks"][0]["reason"])

    def test_unusable_or_inequivalent_baselines_never_prove_existing_failure(self) -> None:
        candidate = _report()
        variants: dict[str, Any] = {
            "missing": None,
            "malformed-empty": {},
            "malformed-non-dict": ["not", "a", "dict"],
            "bool-exit": {"exit": True, "checks": []},
            "bad-rows": {"exit": 1, "checks": [None]},
            "duplicate-check-id": {
                "exit": 1,
                "checks": [
                    candidate["checks"][0],
                    copy.deepcopy(candidate["checks"][0]),
                ],
            },
            "empty-check-id": {"exit": 1, "checks": [{"id": "", "status": "failed"}]},
            "outside-any-check": {"exit": 1, "checks": [{"id": "journeys", "status": "passed"}]},
        }
        for key, value in [
            ("version", True),
            ("version", 2),
            ("comparable", False),
            ("comparable", 1),
            ("context", ""),
            ("context", "   "),
            ("context", 123),
            ("context", "invalid-runtime-pin"),
            ("cases", []),
            ("cases", [{"id": "setup", "signature": "pin mismatch"}]),
            ("cases", [{"id": "picker opens", "signature": "different assertion"}]),
            ("cases", [{"id": "picker opens", "signature": ""}]),
            ("cases", [{"id": "picker opens", "signature": 42}]),
            ("cases", [{"id": "picker opens", "signature": "assert: reveal requested", "extra": "x"}]),
            ("cases", [{"id": "picker opens", "signature": "assert: reveal requested"}] * 2),
        ]:
            base = _report()
            base["checks"][0]["failure_evidence"][key] = value
            variants[f"{key}={value!r}"] = base
        base_extra_key = _report()
        base_extra_key["checks"][0]["failure_evidence"]["extra"] = True
        variants["extra-evidence-key"] = base_extra_key
        for key, value in [
            ("environment", {"class": "runtime"}),
            ("interrupted", True),
            ("cleanup_error", "orphan"),
            ("exit", 0),
            ("_lanefather_process_exit", 9),
            ("_lanefather_process_exit", True),
        ]:
            base = _report()
            base[key] = value
            variants[key] = base
        for status in ("blocked", "not_run", "passed"):
            base = _report(status=status)
            variants[f"status={status}"] = base
        base_unrelated = _report(extra_rows=[{"id": "setup", "status": "failed"}])
        variants["unrelated-failure"] = base_unrelated
        base_legacy = _report()
        del base_legacy["checks"][0]["failure_evidence"]
        variants["legacy-no-evidence"] = base_legacy

        for name, base in variants.items():
            with self.subTest(name=name):
                self.assertIsNotNone(verify.comparison_problem([candidate, base], "journeys", ["journeys"]))

    def test_alone_failure_must_match_the_original_candidate_too(self) -> None:
        first = _report()
        alone = _report(cases=[{"id": "another test", "signature": "assert: reveal requested"}])
        base = copy.deepcopy(alone)
        self.assertIsNotNone(verify.comparison_problem([first, alone, base], "journeys", ["journeys"]))

    def test_dependency_blocked_rows_on_candidate_do_not_block_pre_existing_comparison(self) -> None:
        candidate = _report(
            "control",
            cases=[{"id": "control-case", "signature": "assert: control"}],
            extra_rows=[{"id": "build", "status": "blocked", "blocked_by": ["unit"]}],
        )
        alone = _report("control", cases=[{"id": "control-case", "signature": "assert: control"}])
        base = _report("control", cases=[{"id": "control-case", "signature": "assert: control"}])
        self.assertIsNone(verify.comparison_problem([candidate, alone, base], "control", ["unit", "control"]))

    def test_legacy_reports_without_failure_evidence_stay_unclassified_and_red(self) -> None:
        legacy_candidate: dict[str, Any] = {
            "exit": 1,
            "environment": None,
            "interrupted": None,
            "cleanup_error": None,
            "checks": [{"id": "tools/test-janitor.py", "status": "failed", "blocked_by": [], "exit": 1}],
        }
        legacy_base = copy.deepcopy(legacy_candidate)
        fresh = [{"suite": "tooling", "status": "failed", "failed_checks": ["tools/test-janitor.py"]}]
        store = state.Store(config.DEFAULT)

        with (
            patch.object(verify, "run_rerun", side_effect=[
                ("run-alone", legacy_candidate, Path("/tmp/candidate.log"), None, None),
                ("run-base", legacy_base, Path("/tmp/base.log"), None, None),
            ]),
            patch.object(verify.worktrees, "base_checkout", return_value=contextlib.nullcontext("/tmp/base")),
            patch.object(plan, "known_checks", return_value=None),
            patch.object(plan, "supports_only", return_value=False),
            patch.object(verify, "rerun_cap", return_value=5),
        ):
            result = verify.classify_failures(
                store, {"id": "113-replay", "ticket": "113"}, "/tmp/candidate", "base-sha", fresh, legacy_candidate,
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result["green"])
        self.assertEqual(result["raw_status"], "red")
        self.assertEqual(result["checks"][0]["class"], "unclassified")
        self.assertIn("failure_evidence", result["checks"][0]["reason"])

    def test_rerun_problem_rejects_unrun_or_void_outside_any_check_and_allows_dependency_skipped(self) -> None:
        # P1 #1a: exit=1 with c1 passed and c2 unrun/void must NOT pass rerun_problem (else c1 becomes flaky-under-load).
        for status in ("unrun", "void"):
            rep: dict[str, Any] = {
                "exit": 1,
                "checks": [
                    {"id": "c1", "status": "passed"},
                    {"id": "c2", "status": status},
                ],
            }
            problem = verify.rerun_problem(rep, Path("/tmp/alone.log"))
            self.assertIsNotNone(problem, f"expected outside-any-check problem for status={status}")
            assert problem is not None
            self.assertIn("outside any check", problem)

        # P1 #1b: #195 skipped+blocked_by with exit=1 is dependency-blocked, NOT "failed outside any check".
        dep_skipped: dict[str, Any] = {
            "exit": 1,
            "checks": [{"id": "c1", "status": "skipped", "blocked_by": ["unit"]}],
        }
        self.assertIsNone(verify.rerun_problem(dep_skipped, Path("/tmp/alone.log")))

        # P1 #1c: interrupted verify.py report with cancelled rows reports interrupted, not malformed.
        cancelled_rep: dict[str, Any] = {
            "exit": 130,
            "interrupted": "SIGINT",
            "checks": [{"id": "c1", "status": "cancelled"}],
        }
        cancelled_problem = verify.rerun_problem(cancelled_rep, Path("/tmp/alone.log"))
        self.assertIsNotNone(cancelled_problem)
        assert cancelled_problem is not None
        self.assertIn("interrupted", cancelled_problem)

    def test_initial_interrupted_cleanup_or_exit_mismatch_cannot_become_flaky_green(self) -> None:
        # P1 #2: interrupted initial run, cleanup_error, or process-vs-report exit mismatch must refuse green.
        fresh = [{"suite": "lib", "status": "failed", "failed_checks": ["lib-check"], "exit": 1}]
        for name, extra in [
            ("interrupted", {"interrupted": "SIGINT"}),
            ("cleanup_error", {"cleanup_error": "orphan process"}),
            ("exit-mismatch", {"_lanefather_process_exit": 9}),
        ]:
            with self.subTest(name=name):
                initial: dict[str, Any] = {
                    "exit": 1,
                    "checks": [
                        {"id": "lib-check", "status": "failed"},
                        {"id": "journeys-check", "status": "not_run"},
                    ],
                    **extra,
                }
                planned = verify.plan_classification(fresh, initial, cap=5, only_supported=False)
                self.assertIsInstance(planned, dict)
                assert isinstance(planned, dict)
                self.assertFalse(planned["green"])
                self.assertEqual(planned["raw_status"], "red")
                self.assertTrue(planned.get("skipped"))

    def test_legacy_verifier_without_top_level_exit_keeps_flaky_under_load_but_refuses_pre_existing(self) -> None:
        # P2 #3: documented check contract without top-level `exit` still gets flaky-under-load on alone pass.
        legacy_alone_pass: dict[str, Any] = {
            "checks": [{"id": "lib-check", "status": "passed"}],
            "_lanefather_process_exit": 0,
        }
        self.assertIsNone(verify.rerun_problem(legacy_alone_pass, Path("/tmp/alone.log")))

        # Without top-level `exit`, pre-existing comparison still refuses (strict exit required for comparison).
        ev = _evidence([{"id": "t1", "signature": "assert: 1"}])
        legacy_fail: dict[str, Any] = {
            "checks": [{"id": "lib-check", "status": "failed", "failure_evidence": ev}],
            "_lanefather_process_exit": 1,
        }
        problem = verify.comparison_problem([legacy_fail, copy.deepcopy(legacy_fail)], "lib-check", ["lib-check"])
        self.assertIsNotNone(problem)

    def test_malformed_reports_do_not_crash_build_outcome_history_or_plan_classification(self) -> None:
        # P2 #4: malformed report.json payloads must not raise inside Run.execute / record_run / plan_classification.
        payloads: list[dict[str, Any]] = [
            {"exit": 1, "checks": [None]},
            {"exit": 1, "checks": None},
            {"exit": 1, "checks": [{"status": "failed"}]},
            {"exit": 1, "environment": "runtime-broken", "checks": []},
            {"exit": 1, "checks": [{"id": "c1", "status": "blocked", "blocked_by": 123}]},
            {"exit": 1, "checks": [{"id": "c1", "status": "skipped", "blocked_by": "unit"}]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = state.Store(config.DEFAULT, root / "state")
            for idx, payload in enumerate(payloads):
                with self.subTest(idx=idx, payload=payload):
                    report_file = root / f"report-{idx}.json"
                    report_file.write_text(json.dumps(payload))
                    stub = root / f"verify-{idx}.sh"
                    stub.write_text(f"#!/bin/sh\necho 'Verification report: {report_file}'\nexit 1\n")
                    stub.chmod(0o755)
                    with patch.object(plan, "verify_path", return_value=str(stub)):
                        outcome = verify.Run(
                            root, verify.Selection.checks(["c1"]), f"malformed-{idx}", "check", 1,
                            store=store, wait_for_load=False,
                        ).execute()
                    self.assertEqual(outcome.exit, 1)
                    fresh, _ = verify.suite_outcomes(root, ["s"], {"s": ["c1"]}, outcome, project=store.project)
                    verify.plan_classification(fresh, outcome.report, cap=5, only_supported=False)
                    history.record_run(
                        store, outcome.report, lane="a", phase="check", tree="abc", run="r1",
                        started="2026-11-24T00:00:00Z", finished="2026-11-24T00:00:01Z", seconds=1.0,
                    )

    def test_per_report_selected_rejects_unselected_failure_on_alone_or_base(self) -> None:
        # P2 #5: initial had c1+c2 failed, alone only selected [c1] but c2 also failed unexpectedly -> reject!
        ev1 = _evidence([{"id": "case-1", "signature": "sig-1"}])
        ev2 = _evidence([{"id": "case-2", "signature": "sig-2"}])
        initial: dict[str, Any] = {
            "exit": 1,
            "checks": [
                {"id": "c1", "status": "failed", "failure_evidence": ev1},
                {"id": "c2", "status": "failed", "failure_evidence": ev2},
            ],
        }
        alone: dict[str, Any] = {
            "exit": 1,
            "checks": [
                {"id": "c1", "status": "failed", "failure_evidence": ev1},
                {"id": "c2", "status": "failed", "failure_evidence": ev2},
            ],
        }
        base: dict[str, Any] = {
            "exit": 1,
            "checks": [{"id": "c1", "status": "failed", "failure_evidence": ev1}],
        }
        problem = verify.comparison_problem(
            [
                verify.ComparisonRun("the initial run", initial, log="/tmp/init.log", selected=["c1", "c2"]),
                verify.ComparisonRun("the alone rerun", alone, log="/tmp/alone.log", selected=["c1"]),
                verify.ComparisonRun("the base rerun", base, log="/tmp/base.log", selected=["c1"]),
            ],
            "c1",
        )
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("the alone rerun", problem)
        self.assertIn("/tmp/alone.log", problem)
        self.assertNotIn("(None)", problem)

    def test_polish_phase_labels_distinct_reasons_v1_list_shape_and_joined_skipped_raw_status(self) -> None:
        # P3: distinct messages for context vs alone-vs-initial; v1 list shape in failure_comparison; joined raw_status.
        first = _report(cases=[{"id": "a", "signature": "sig-a"}])
        diff_ctx = _report(context="other-ctx", cases=[{"id": "a", "signature": "sig-a"}])
        ctx_problem = verify.comparison_problem([first, diff_ctx], "journeys", ["journeys"])
        self.assertIsNotNone(ctx_problem)
        assert ctx_problem is not None
        self.assertIn("context", ctx_problem)
        self.assertNotIn("alone rerun", ctx_problem)

        diff_alone = _report(cases=[{"id": "b", "signature": "sig-b"}])
        alone_problem = verify.comparison_problem([first, diff_alone, diff_alone], "journeys", ["journeys"])
        self.assertIsNotNone(alone_problem)
        assert alone_problem is not None
        self.assertIn("alone rerun", alone_problem)

        # v1 list form in failure_comparison
        entry = verify.evaluate_base_row(
            "journeys",
            {"check": "journeys", "alone": {"status": "failed", "log": "/tmp/a.log"}},
            base="deadbeef",
            present=True,
            row=first["checks"][0],
            base_log="/tmp/b.log",
            comparison_reports=[first, first, first],
            selected=["journeys"],
        )
        self.assertEqual(entry["class"], "pre-existing")
        self.assertEqual(
            entry["failure_comparison"],
            {
                "version": 1,
                "context": "valid-runtime-049/report-v1",
                "cases": [{"id": "a", "signature": "sig-a"}],
            },
        )

        # joined_classification preserves raw_status="red" on second_cls skipped branch.
        joined = verify.joined_classification(
            {"base": "deadbeef", "checks": [], "runs": [], "unaccounted": [], "green": False},
            {"skipped": "environment: rust-drift", "checks": [], "runs": [], "green": False},
        )
        self.assertEqual(joined["raw_status"], "red")
        self.assertFalse(joined["green"])


if __name__ == "__main__":
    unittest.main()
