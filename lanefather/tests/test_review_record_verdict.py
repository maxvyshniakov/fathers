"""`review <id> --record --verdict` (fathers#256): the verdict of a review delegated outside lanefather (a T3 Code
`delegate_task` reviewer) recorded against the lane's tip and tree, so land, promote, done, show and list read it
like a review lanefather ran, without `--force-reason`."""
from __future__ import annotations

import json
from typing import Any

from support import LaneCase

REF = "https://github.com/example/project/pull/7#issuecomment-1"
ONE_P1_REPORT = """\
Found one bug.

```json
{"findings": [{"severity": "P1", "category": "bug", "file": "lib/api.txt", "line": 2,
               "title": "missing null guard", "detail": "fails on empty input"}]}
```
"""


class RecordVerdictTests(LaneCase):
    def landed(self, lane_id: str, text: str) -> str:
        self.git(self.repo, "branch", "dev")
        worktree = self.created(lane_id, "lib/**")
        self.commit(worktree, {"lib/api.txt": text})
        self.lane("check", lane_id)
        self.lane("land", lane_id)
        return worktree

    def show(self, lane_id: str) -> dict[str, Any]:
        return json.loads(self.lane("show", lane_id, "--json"))

    def test_an_approve_verdict_finishes_the_lane_without_force_reason(self) -> None:
        self.landed("a", "foo\napproved\n")
        # LANEFATHER_REVIEW_START=manual: no review, so promote refuses and the lane stays landed.
        sha = self.tip()
        self.assertIn("lane a", self.lane("promote", sha, "--no-push", "--wait", "0", ok=False))
        self.assertEqual(self.record("a")["status"], "landed")

        out = self.lane("review", "a", "--record", "--reviewer", "claude-fable-5-2", "--verdict", "approve",
                        "--ref", REF, "--note", "clean; checked the parser edge cases")
        self.assertIn("by claude-fable-5-2", out)
        self.assertIn("0 findings", out)
        rev = self.review("a")
        record = self.record("a")
        self.assertEqual((rev["status"], rev["recorded"], rev["verdict"], rev["ref"], rev["findings"]),
                         ("finished", True, "approve", REF, []))
        self.assertEqual(rev["note"], "clean; checked the parser edge cases")
        self.assertIsNone(rev["source_file"])
        # Bound to the landed tip and its tree, as a review lanefather runs is.
        landed_tip = record["landed"]["tip"]
        self.assertEqual(rev["tip"], landed_tip)
        self.assertEqual(rev["tree"], self.git(self.repo, "rev-parse", landed_tip + "^{tree}"))
        report = (self.state / "reviews" / "a.md").read_text()
        self.assertIn(REF, report)
        self.assertIn('"verdict": "approve"', report)
        ledger = [e for e in self.events_of("review.finished") if e.get("details", {}).get("recorded")]
        self.assertEqual(ledger[-1]["details"]["verdict"], "approve")
        self.assertEqual(ledger[-1]["details"]["ref"], REF)

        # The recorded review settles the lane on its own, and promote takes it without an override.
        self.assertEqual(record["status"], "done")
        shown = self.show("a")["review"]
        self.assertEqual(shown["status"], "finished")
        self.assertEqual(shown["recorded"]["verdict"], "approve")
        self.assertEqual(shown["recorded"]["ref"], REF)
        self.assertEqual(shown["recorded"]["reviewer"], "claude-fable-5-2")
        self.assertIn(f"review recorded: approve by claude-fable-5-2 at {landed_tip[:10]} ({REF})",
                      self.lane("show", "a"))
        self.lane("promote", sha, "--no-push", "--wait", "0")
        self.assertFalse([e for e in self.events_of("override") if e.get("lane") == "a"])

    def test_changes_requested_holds_promote_until_the_finding_is_marked(self) -> None:
        self.landed("c", "foo\nchanges\n")
        sha = self.tip()
        refused = self.lane("review", "c", "--record", "--reviewer", "t3-delegate",
                            "--verdict", "changes_requested", ok=False)
        self.assertIn("requires --note", refused)
        self.assertFalse((self.state / "c.review.json").exists())

        self.lane("review", "c", "--record", "--reviewer", "t3-delegate", "--verdict", "changes_requested",
                  "--note", "rename the flag\nthe old name collides with --jobs", "--ref", REF)
        rev = self.review("c")
        # A name the models file does not know is kept as given, as an external reviewer.
        self.assertEqual((rev["reviewer"], rev["model"], rev["cli"]), ("t3-delegate", "t3-delegate", "external"))
        self.assertEqual(rev["verdict"], "changes_requested")
        self.assertEqual([(f["n"], f["severity"], f["title"]) for f in rev["findings"]],
                         [(1, "P1", "rename the flag")])
        self.assertIn(REF, rev["findings"][0]["detail"])
        self.assertEqual(self.record("c")["status"], "landed")
        self.assertEqual(self.show("c")["review"]["unresolved"], 1)
        self.assertIn("P1/P2 review findings unmarked",
                      self.lane("promote", sha, "--no-push", "--wait", "0", ok=False))
        self.lane("review", "c", "--mark", "1", "rejected:the owner kept the name")
        self.lane("promote", sha, "--no-push", "--wait", "0")

    def test_a_pre_land_verdict_goes_stale_when_the_lane_moves(self) -> None:
        worktree = self.created("p", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\npre\n"})
        self.lane("check", "p")
        self.lane("review", "p", "--record", "--reviewer", "claude-fable-5-2", "--verdict", "approve")
        first = self.review("p")
        self.assertTrue(first["pre_land"])
        self.assertEqual(first["tip"], self.git(worktree, "rev-parse", "HEAD"))
        self.assertIn("reviewed before land", self.lane("list"))

        self.commit(worktree, {"lib/api.txt": "foo\npre\nmore\n"})
        shown = self.show("p")["review"]
        self.assertEqual(shown["status"], "stale")
        self.lane("check", "p")
        self.assertIn("unreviewed commits since pre-land review", self.lane("land", "p", ok=False))
        # Replacing the stale review is logged, as for a review lanefather ran; land then takes it.
        refused = self.lane("review", "p", "--record", "--reviewer", "claude-fable-5-2", "--verdict", "approve",
                            ok=False)
        self.assertIn("review stale", refused)
        self.assertIn("--force-reason", refused)
        self.lane("review", "p", "--record", "--reviewer", "claude-fable-5-2", "--verdict", "approve",
                  "--force-reason", "the lane moved; the reviewer read the new tip")
        self.assertEqual(self.review("p")["tip"], self.git(worktree, "rev-parse", "HEAD"))
        self.assertEqual(self.show("p")["review"]["status"], "finished")
        overrides = [e for e in self.events_of("override") if e.get("details", {}).get("command") == "review"]
        self.assertTrue(overrides, "replacing a stale recorded review with --force-reason must log an override event")
        self.git(self.repo, "branch", "dev")
        self.lane("land", "p")
        self.assertEqual(self.record("p")["status"], "done")

    def test_a_recorded_review_of_a_landed_lane_is_bound_to_its_landed_tip(self) -> None:
        """A recorded review names the tip it reviewed; after land that is the landed tip, so a second record of
        the same lane is refused as already reviewed rather than silently replacing it."""
        self.landed("s", "foo\nlanded\n")
        self.lane("review", "s", "--record", "--reviewer", "opus", "--verdict", "approve", "--ref", REF)
        self.assertEqual(self.review("s")["tip"], self.record("s")["landed"]["tip"])
        again = self.lane("review", "s", "--record", "--reviewer", "neon", "--verdict", "approve")
        self.assertIn("already reviewed", again)
        self.assertEqual(self.review("s")["reviewer"], "opus")

    def test_a_report_file_and_a_verdict_must_agree(self) -> None:
        self.landed("f", "foo\nfile\n")
        report = self.tmp / "review.md"
        report.write_text(ONE_P1_REPORT)
        refused = self.lane("review", "f", "--record", str(report), "--reviewer", "opus", "--verdict", "approve",
                            ok=False)
        self.assertIn("--verdict approve", refused)
        self.assertIn("blocking finding", refused)
        self.assertFalse((self.state / "f.review.json").exists())
        self.lane("review", "f", "--record", str(report), "--reviewer", "opus", "--verdict", "changes_requested",
                  "--ref", REF)
        rev = self.review("f")
        self.assertEqual((rev["verdict"], rev["ref"], len(rev["findings"])), ("changes_requested", REF, 1))
        self.assertEqual(rev["source_file"], str(report.resolve()))

    def test_verdict_flags_are_validated(self) -> None:
        self.landed("v", "foo\nvalid\n")
        cases = [
            (("--verdict", "approve"), "go with --record"),
            (("--ref", REF), "go with --record"),
            (("--record", "--reviewer", "opus"), "requires --verdict"),
            (("--record", "--verdict", "approve"), "requires --reviewer"),
            (("--record", "--reviewer", "../evil", "--verdict", "approve"), "external reviewer's name"),
            (("--record", "--reviewer", "opus", "--verdict", "approve", "--ref", "a\nb"), "--ref: give one printable line"),
            (("--record", "--reviewer", "opus", "--verdict", "approve", "--ref", "x\x1b[31m\u2028y"), "--ref: give one printable line"),
            (("--record", "--reviewer", "opus", "--verdict", "approve", "--note", "  "), "--note: give a non-blank"),
            (("--record", "--reviewer", "opus", "--verdict", "approve", "--note", "bad\x1btitle"), "--note: first line must contain only printable"),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertIn(expected, self.lane("review", "v", *argv, ok=False))
        self.assertFalse((self.state / "v.review.json").exists())
        self.assertIn("invalid choice", self.lane("review", "v", "--record", "--reviewer", "opus",
                                                  "--verdict", "lgtm", ok=False))

    def test_double_panel_merge_preserves_second_reviewers_verdict_and_ref(self) -> None:
        """Merging a second recorded reviewer into a double-class panel must keep per-member verdict/ref and let
        changes_requested win at the top level instead of keeping the first reviewer's approve."""
        self.git(self.repo, "branch", "dev")
        worktree = self.created("d", "lib/**", "--review", "double")
        self.commit(worktree, {"lib/api.txt": "foo\ndouble\n"})
        self.lane("check", "d")
        self.lane("land", "d")
        self.lane("review", "d", "--record", "--reviewer", "ext-a", "--verdict", "approve", "--ref", "https://x/1")
        self.lane("review", "d", "--record", "--reviewer", "ext-b", "--verdict", "changes_requested",
                  "--note", "fix it", "--ref", "https://x/2")
        rev = self.review("d")
        self.assertEqual(rev["verdict"], "changes_requested")
        self.assertEqual(rev["ref"], "https://x/1, https://x/2")
        self.assertEqual([(m["reviewer"], m["verdict"], m["ref"]) for m in rev["panel"]], [
            ("ext-a", "approve", "https://x/1"),
            ("ext-b", "changes_requested", "https://x/2"),
        ])
