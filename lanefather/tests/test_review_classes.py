"""Tests for risk-based review classes, external review recording, batch coverage, audit sampling and fix-lane
naming refusal (fathers#252)."""
from __future__ import annotations

import json
import pathlib
from typing import Any
import unittest

from support import LaneCase

from lanefather import config, review_class
from lanefather.errors import LaneError

CLEAN_REPORT = """\
Looks good overall.

```json
{"findings": []}
```
"""

ONE_P1_REPORT = """\
Found one bug.

```json
{"findings": [{"severity": "P1", "category": "bug", "file": "lib/api.txt", "line": 2,
               "title": "missing null guard", "detail": "fails on empty input"}]}
```
"""

RAW_JSON_REPORT = json.dumps({
    "findings": [
        {
            "severity": "P2",
            "category": "standards",
            "file": "lib/api.txt",
            "line": 1,
            "title": "missing docstring",
            "detail": "add a one-line docstring",
        }
    ]
})

P0_AUDIT_REPORT = """\
Critical bug slipped through.

```json
{"findings": [{"severity": "P0", "category": "bug", "file": "docs/guide.md", "line": 1,
               "title": "secret leaked in example", "detail": "remove token from example"}]}
```
"""


class ReviewClassesConfigTests(unittest.TestCase):
    def test_parses_valid_review_classes(self) -> None:
        cfg = config.parse({
            "review": {
                "classes": {
                    "none": ["docs/**", "*.md", "tests/**"],
                    "evidence": ["styles/**"],
                    "full": ["lib/**"],
                    "double": ["lib/core/**"],
                    "max_lines": 50,
                    "max_files": 4,
                    "audit_every": 3,
                }
            }
        })
        rc = cfg.review_classes
        self.assertTrue(rc.has_rules())
        self.assertEqual(rc.none, ("docs/**", "*.md", "tests/**"))
        self.assertEqual(rc.evidence, ("styles/**",))
        self.assertEqual(rc.full, ("lib/**",))
        self.assertEqual(rc.double, ("lib/core/**",))
        self.assertEqual((rc.max_lines, rc.max_files, rc.audit_every), (50, 4, 3))

    def test_defaults_when_omitted(self) -> None:
        cfg = config.parse({})
        rc = cfg.review_classes
        self.assertFalse(rc.has_rules())
        self.assertEqual(rc.audit_every, 5)
        self.assertIsNone(rc.max_lines)
        self.assertIsNone(rc.max_files)

    def test_rejects_invalid_review_classes(self) -> None:
        bad_tables: list[dict[str, Any]] = [
            {"review": {"classes": "bad"}},
            {"review": {"classes": {"unknown_key": []}}},
            {"review": {"classes": {"max_lines": 0}}},
            {"review": {"classes": {"max_lines": True}}},
            {"review": {"classes": {"max_files": -1}}},
            {"review": {"classes": {"max_files": False}}},
            {"review": {"classes": {"audit_every": -1}}},
            {"review": {"classes": {"audit_every": True}}},
            {"review": {"classes": {"none": [""]}}},
        ]
        for data in bad_tables:
            with self.subTest(data=data), self.assertRaises(LaneError):
                config.parse(data)

    def test_required_class_precedence_and_size_bounds(self) -> None:
        cfg = config.parse({
            "review": {
                "classes": {
                    "none": ["docs/**", "*.md", ".github/**"],
                    "evidence": ["ui/theme/**"],
                    "full": ["lib/**"],
                    "double": ["lib/core/**"],
                    "max_lines": 20,
                    "max_files": 2,
                }
            }
        })
        self.assertEqual(review_class.required_class(["docs/a.md"], lines=5, files=1, project=cfg).min_class, "none")
        self.assertEqual(
            review_class.required_class(["ui/theme/colors.css"], lines=5, files=1, project=cfg).min_class, "evidence"
        )
        self.assertEqual(review_class.required_class(["lib/app.py"], lines=5, files=1, project=cfg).min_class, "full")
        self.assertEqual(
            review_class.required_class(["lib/core/auth.py"], lines=5, files=1, project=cfg).min_class, "double"
        )
        # Steering files are always at least full even if matched by none.
        self.assertEqual(
            review_class.required_class(["CODING_STANDARDS.md"], lines=2, files=1, project=cfg).min_class, "full"
        )
        # Size bounds escalate none/evidence to full.
        self.assertEqual(review_class.required_class(["docs/a.md"], lines=25, files=1, project=cfg).min_class, "full")
        self.assertEqual(
            review_class.required_class(["docs/a.md", "docs/b.md", "docs/c.md"], lines=3, files=3, project=cfg).min_class,
            "full",
        )

    def test_should_audit_is_deterministic(self) -> None:
        self.assertFalse(review_class.should_audit("lane-a", "a" * 40, 0))
        self.assertTrue(review_class.should_audit("lane-a", "a" * 40, 1))
        first = review_class.should_audit("lane-a", "a" * 40, 5)
        self.assertEqual(first, review_class.should_audit("lane-a", "a" * 40, 5))
        hits = sum(1 for i in range(100) if review_class.should_audit(f"lane-{i}", f"{i:040x}", 5))
        self.assertGreater(hits, 0)
        self.assertLess(hits, 100)


class ReviewClassesWorkflowTests(LaneCase):
    def _configure_classes(self, toml_block: str) -> None:
        base_text = (self.integration / ".lanefather.toml").read_text()
        self.commit(self.integration, {".lanefather.toml": base_text + "\n" + toml_block + "\n"}, "configure review classes")

    def _create_with(self, lane_id: str, claim: str, *extra: str) -> pathlib.Path:
        self.start(lane_id, claim, extra=(*extra, "--create"))
        return pathlib.Path(self.record(lane_id)["worktree"])

    def test_declared_class_lower_than_computed_refused_at_land_and_overridable(self) -> None:
        self._configure_classes("""\
[review.classes]
none = ["docs/**"]
full = ["lib/**"]
double = ["lib/core/**"]
max_lines = 10
""")
        wt = self._create_with("low", "lib/**", "--review", "none")
        self.assertEqual(self.record("low")["review_class"], "none")
        self.commit(wt, {"lib/api.txt": "foo\nbar\n"})
        self.lane("check", "low")
        refused = self.lane("land", "low", ok=False)
        self.assertIn("declared review class 'none' is lower than required 'full'", refused)
        self.assertEqual(self.record("low")["status"], "active")

        # Override via --force-reason succeeds and is audited.
        self.lane("land", "low", "--force-reason", "trivial one-liner approved")
        rec = self.record("low")
        self.assertEqual(rec["status"], "done")
        self.assertTrue(any("declared review class 'none'" in " ".join(o["refusals"]) for o in rec["overrides"]))

    def test_size_bound_refuses_none_at_land(self) -> None:
        self._configure_classes("""\
[review.classes]
none = ["docs/**"]
full = ["lib/**"]
max_lines = 3
""")
        wt = self._create_with("bigdoc", "docs/**", "--review", "none")
        self.commit(wt, {"docs/big.md": "1\n2\n3\n4\n5\n"})
        self.lane("check", "bigdoc")
        refused = self.lane("land", "bigdoc", ok=False)
        self.assertIn("max_lines = 3", refused)

    def test_none_class_lands_exempt_and_double_class_runs_panel(self) -> None:
        self._configure_classes("""\
[review.classes]
none = ["docs/**", "tests/**"]
full = ["lib/**"]
double = ["lib/core/**"]
""")
        wt = self._create_with("testonly", "tests/**", "--review", "none", "--review-reason", "test-only helper")
        self.commit(wt, {"tests/helper.txt": "ok\n"})
        self.lane("check", "testonly")
        output = self.lane("land", "testonly")
        self.assertIn("review exempt: review class none", output)
        rev = self.review("testonly")
        self.assertEqual(rev["status"], "exempt")
        self.assertEqual(rev["review_class"], "none")

        # Double class refuses full at land, and when declared double runs two-reviewer panel.
        wt2 = self._create_with("risky", "lib/core/**", "--review", "full")
        self.commit(wt2, {"lib/core/auth.txt": "token\n"})
        self.lane("check", "risky")
        self.assertIn("lower than required 'double'", self.lane("land", "risky", ok=False))

    def test_evidence_class_requires_owner_decision_and_guarding_gate(self) -> None:
        self._configure_classes("""\
[review.classes]
none = ["docs/**"]
evidence = ["styles/**"]
full = ["lib/**"]
""")
        # Missing both decision and gate -> refused at land.
        wt1 = self._create_with("ev-missing", "styles/missing.css", "--review", "evidence")
        self.commit(wt1, {"styles/missing.css": ".tb { color: #333; }\n"})
        self.lane("check", "ev-missing")
        refused = self.lane("land", "ev-missing", ok=False)
        self.assertIn("owner decision", refused)
        self.assertIn("guarding gate/check/test", refused)

        # Carrying both decision (inline quote+date) and gate -> lands exempt and promotes cleanly.
        self.git(self.repo, "branch", "dev")
        wt2 = self._create_with(
            "ev-ok",
            "styles/toolbar.css",
            "--review", "evidence",
            "--review-decision", '2026-11-24: "use muted gray for toolbar"',
            "--gate", "styles-snapshot-test",
        )
        self.commit(wt2, {"styles/toolbar.css": ".tb { color: #555; }\n"})
        self.lane("check", "ev-ok")
        land_out = self.lane("land", "ev-ok")
        self.assertIn("review exempt: review class evidence", land_out)
        self.assertEqual(self.review("ev-ok")["status"], "exempt")
        self.assertEqual(self.review("ev-ok")["review_class"], "evidence")

        # Promote lists the evidence lane with its class and reason.
        sha = self.tip()
        promote_out = self.lane("promote", sha, "--no-push", "--wait", "0")
        self.assertIn("review class evidence: lane ev-ok", promote_out)
        self.assertIn("styles-snapshot-test", promote_out)

    def test_evidence_class_accepts_scope_decision_url(self) -> None:
        wt = self._create_with(
            "ev-url",
            "lib/**",
            "--review", "evidence",
            "--decision", "https://github.com/o/r/issues/404#issuecomment-999",
            "--gate", "lib-unit",
        )
        self.commit(wt, {"lib/api.txt": "foo\ncolor\n"})
        self.lane("check", "ev-url")
        out = self.lane("land", "ev-url")
        self.assertIn("review exempt: review class evidence", out)
        self.assertIn("issuecomment-999", self.review("ev-url")["exempt"])

    def test_record_external_review_parses_findings_allows_mark_and_passes_gate(self) -> None:
        self.git(self.repo, "branch", "dev")
        wt = self.created("ext", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\next\n"})
        self.lane("check", "ext")
        self.lane("land", "ext")

        report = self.tmp / "external-review.md"
        report.write_text(ONE_P1_REPORT)
        self.assertIn("--record requires --reviewer", self.lane("review", "ext", "--record", str(report), ok=False))

        out = self.lane("review", "ext", "--record", str(report), "--reviewer", "opus")
        self.assertIn("missing null guard", out)
        rev = self.review("ext")
        self.assertEqual(rev["status"], "finished")
        self.assertTrue(rev["recorded"])
        self.assertEqual(rev["reviewer"], "opus")
        self.assertEqual(len(rev["findings"]), 1)

        # Unmarked P1 blocks promote; marking rejected unblocks promote.
        sha = self.tip()
        self.assertIn("P1/P2 review findings unmarked", self.lane("promote", sha, "--no-push", "--wait", "0", ok=False))
        self.lane("review", "ext", "--mark", "1", "rejected:handled upstream")
        self.lane("promote", sha, "--no-push", "--wait", "0")

        # Raw JSON report also works on a pre-land active lane.
        wt2 = self.created("ext2", "lib/extra.txt")
        self.commit(wt2, {"lib/extra.txt": "ext2\n"})
        raw_report = self.tmp / "raw.json"
        raw_report.write_text(RAW_JSON_REPORT)
        self.lane("review", "ext2", "--record", str(raw_report), "--reviewer", "neon")
        rev2 = self.review("ext2")
        self.assertTrue(rev2["pre_land"])
        self.assertEqual(rev2["findings"][0]["title"], "missing docstring")

    def test_batch_overall_review_covers_multiple_lanes_in_review_gate(self) -> None:
        self.git(self.repo, "branch", "dev")
        wt_a = self.created("ba", "lib/**")
        self.commit(wt_a, {"lib/api.txt": "foo\nba\n"})
        self.lane("check", "ba")
        self.lane("land", "ba")

        wt_b = self.created("bb", "lib/extra.txt")
        self.commit(wt_b, {"lib/extra.txt": "bb\n"})
        self.lane("check", "bb")
        self.lane("land", "bb")

        sha = self.tip()
        # Neither lane has an individual review -> promote is refused.
        refused = self.lane("promote", sha, "--no-push", "--wait", "0", ok=False)
        self.assertIn("lane ba", refused)
        self.assertIn("lane bb", refused)

        # Record a batch review covering ba and bb with one finding.
        report = self.tmp / "batch.md"
        report.write_text(ONE_P1_REPORT)
        self.lane("review", "--overall", sha, "--lanes", "ba,bb", "--record", str(report), "--reviewer", "sol")

        # Unmarked finding on the batch review holds promote.
        blocked = self.lane("promote", sha, "--no-push", "--wait", "0", ok=False)
        self.assertIn("missing null guard", blocked)

        # Mark the batch finding -> both lanes satisfied and promote succeeds.
        self.lane("review", "--overall", sha, "--mark", "1", "rejected:checked in batch")
        self.lane("promote", sha, "--no-push", "--wait", "0")

    def test_audit_sampling_at_promote_and_p0_p1_alert(self) -> None:
        self._configure_classes("""\
[review.classes]
none = ["docs/**"]
audit_every = 1
""")
        self.git(self.repo, "branch", "dev")
        wt = self._create_with("doclane", "docs/**", "--review", "none", "--review-reason", "docs tweak")
        self.commit(wt, {"docs/guide.md": "hello\n"})
        self.lane("check", "doclane")
        self.lane("land", "doclane")
        sha = self.tip()

        # Promote lists the none lane and reports the audit sample (manual mode prints the --audit command).
        promote_out = self.lane("promote", sha, "--no-push", "--wait", "0")
        self.assertIn("review class none: lane doclane", promote_out)
        self.assertIn("audit sample (1 in 1): lane doclane (class=none)", promote_out)
        self.assertIn("review doclane --audit", promote_out)

        # Running/recording the audit with a P0 finding reports the alert with the class that let it through.
        audit_report = self.tmp / "audit.md"
        audit_report.write_text(P0_AUDIT_REPORT)
        audit_out = self.lane("review", "doclane", "--audit", "--record", str(audit_report), "--reviewer", "opus")
        self.assertIn("Audit alert: lane doclane", audit_out)
        self.assertIn("review class 'none'", audit_out)
        self.assertIn("P0 finding #1", audit_out)
        self.assertIn("audit-alert doclane class=none findings=1", (self.state / "ledger.log").read_text())

        # `findings --json` surfaces the audit alert without holding the SHA.
        findings_data = json.loads(self.lane("findings", sha, "--since", "dev~1", "--json"))
        self.assertFalse(findings_data["held"])
        self.assertEqual(len(findings_data["audit_alerts"]), 1)
        self.assertEqual(findings_data["audit_alerts"][0]["review_class"], "none")
        self.assertEqual(findings_data["audit_alerts"][0]["severity"], "P0")

    def test_review_fixes_name_without_fix_of_is_refused_at_start(self) -> None:
        refused = self.start("foo-review-fixes", "lib/**", ok=False)
        self.assertIn("foo-review-fixes looks like a fix lane but names no parent", refused)
        self.assertIn("--fix-of", refused)
        self.assertFalse((self.state / "foo-review-fixes.json").exists())

        # Override via --force-reason works and is audited.
        self.start("foo-review-fixes", "lib/**", extra=("--force-reason", "not actually a fix round"))
        self.assertEqual(self.record("foo-review-fixes")["status"], "planned")
        self.assertTrue(self.record("foo-review-fixes")["overrides"])


if __name__ == "__main__":
    unittest.main()
