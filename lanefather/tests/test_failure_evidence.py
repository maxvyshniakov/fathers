"""fathers#113: comparable failing-case evidence is required before classifying a red check pre-existing."""
from __future__ import annotations

import contextlib
import copy
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

from lanefather import config, plan, state, verify


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


if __name__ == "__main__":
    unittest.main()
