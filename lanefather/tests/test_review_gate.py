"""Unmarked P1/P2 review findings hold the batch gate, promote and a publish (fathers#109): table tests over finding
states and severities, the override, and the `findings` command a publish (Repo-Kit's release:publish) calls."""
from __future__ import annotations

import json
from typing import Any
import unittest

from support import LaneCase


class ReviewGateTableTests(LaneCase):
    def setUp(self):
        super().setUp()
        self.git(self.repo, "branch", "dev")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/extra.txt": "a\n"})
        self.lane("check", "a")
        self.lane("land", "a")
        self.lane("review", "a")
        self.sha = self.tip()

    def findings(self, *found: dict[str, Any], status: str = "finished") -> None:
        """Lane a's review, finished (or not) with these findings."""
        path = self.state / "a.review.json"
        receipt = json.loads(path.read_text())
        receipt.update(status=status, findings=[{"n": n, "title": f"finding {n}", "file": "lib/extra.txt",
                                                 "line": 1, "mark": None, **f} for n, f in enumerate(found, 1)])
        if status == "running":
            receipt["pid"] = 999999999  # a process that is gone: the review died
        path.write_text(json.dumps(receipt))

    def answer(self, *extra: str) -> dict[str, Any]:
        return json.loads(self.lane("findings", self.sha, "--json", *extra, ok=None))

    def test_states_and_severities(self):
        outside = self.git(self.repo, "commit-tree", "-m", "a fix elsewhere", f"{self.sha}^{{tree}}")
        rows: list[tuple[list[dict[str, Any]], str, bool, int, int]] = [
            # (findings, review status, held, blocking, reported)
            ([{"severity": "P1"}], "finished", True, 1, 0),
            ([{"severity": "P2"}], "finished", True, 1, 0),
            ([{"severity": "P3"}], "finished", False, 0, 1),
            ([{"severity": "P1", "mark": {"kind": "rejected", "reason": "out of scope"}}], "finished", False, 0, 0),
            ([{"severity": "P2", "mark": {"kind": "fixed", "merge": self.sha}}], "finished", False, 0, 0),
            ([{"severity": "P2", "mark": {"kind": "fixed", "merge": outside}}], "finished", True, 1, 0),
            ([{"severity": "P3"}, {"severity": "P2"}], "finished", True, 1, 1),
            ([{"severity": "P1"}], "running", True, 0, 0),
            ([], "exempt", False, 0, 0),
        ]
        for found, status, held, blocking, reported in rows:
            with self.subTest(found=found, status=status):
                self.findings(*found, status=status)
                answer = self.answer()
                self.assertEqual((answer["held"], len(answer["blocking"]), len(answer["reported"])),
                                 (held, blocking, reported))
                self.assertEqual(bool(answer["unfinished"]), status == "running")
        (self.state / "a.review.json").unlink()
        self.assertEqual(self.answer()["unfinished"][0]["lane"], "a")

    def test_a_blocking_finding_holds_the_batch_gate_with_its_mark_commands_and_the_override_is_logged(self):
        self.findings({"severity": "P1"}, {"severity": "P3"})
        output = self.lane("promote", self.sha, "--no-push", "--wait", "0", ok=False)
        self.assertIn("unmarked P1/P2 review findings hold the batch gate and dev", output)
        self.assertIn("lanefather review a --mark 1 fixed:<merge> | lanefather review a --mark 1 rejected:<reason>",
                      output)
        self.assertIn("reported, does not block: lane a (#404): finding #2 P3", output)
        self.assertNotIn("FULL structure,lib,native,host-journeys", self.verify_runs())
        self.lane("promote", self.sha, "--no-push", "--wait", "0", "--force-reason", "owner accepted the risk")
        self.assertEqual(self.git(self.repo, "rev-parse", "dev"), self.sha)
        self.assertRegex((self.state / "ledger.log").read_text(),
                         rf"override promote {self.sha}: owner accepted the risk \(lane a \(#404\): finding #1 P1")

    def test_p3_alone_does_not_block_unless_the_project_says_so(self):
        self.findings({"severity": "P3"})
        self.assertFalse(self.answer()["held"])
        config = (self.integration / ".lanefather.toml").read_text() + '\n[review]\nblocks = ["P1", "P2", "P3"]\n'
        (self.integration / ".lanefather.toml").write_text(config)
        answer = self.answer()
        self.assertEqual((answer["held"], answer["blocks"]), (True, ["P1", "P2", "P3"]))

    def test_the_publish_path_holds_with_exit_1_and_an_override_answers_0_logged(self):
        self.findings({"severity": "P2"})
        output = self.lane("findings", self.sha, ok=False)
        self.assertIn("held: lane a (#404): finding #1 P2 lib/extra.txt:1 finding 1", output)
        # A publish compares against its last release, not dev: lanes landed after it count.
        base = self.git(self.repo, "rev-list", "--max-parents=0", "HEAD")
        self.assertTrue(self.answer("--since", base)["held"])
        self.assertFalse(self.answer("--since", self.sha)["held"])
        self.lane("findings", self.sha, "--force-reason", "release of a hotfix")
        self.assertRegex((self.state / "ledger.log").read_text(), rf"override findings {self.sha}: release of a hotfix")


if __name__ == "__main__":
    unittest.main()
