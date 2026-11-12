"""Tests of the ticket lifecycle (lanefather.tickets, #25): what land, promote --ship and shipped tell a lane's issue
through a stand-in gh first on PATH, in scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import pathlib
from typing import Any
import unittest

from support import FIXTURE_CONFIG, LaneCase

# A stand-in gh with a repository's labels and issues in FAKE_TICKETS_STATE; FAKE_TICKETS_FAIL fails every call whose
# arguments contain it, as an outage or a missing token does.
FAKE_TICKETS_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_TICKETS_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
fail = os.environ.get("FAKE_TICKETS_FAIL")
if fail and fail in " ".join(args):
    print("HTTP 502: Bad Gateway (https://api.github.com/graphql)", file=sys.stderr)
    sys.exit(1)
path = os.environ["FAKE_TICKETS_STATE"]
state = json.load(open(path))
issue = lambda number: state["issues"].setdefault(number, {"labels": [], "comments": []})
if args[:2] == ["label", "create"]:
    if args[2] in state["labels"]:
        print(f"label with name \"{args[2]}\" already exists; use `--force` to update its color and description",
              file=sys.stderr)
        sys.exit(1)
    state["labels"][args[2]] = {"color": args[args.index("--color") + 1],
                                "description": args[args.index("--description") + 1]}
elif args[:2] == ["issue", "edit"]:
    flag = "--add-label" if "--add-label" in args else "--remove-label"
    name = args[args.index(flag) + 1]
    if name not in state["labels"]:
        print(f"failed to update https://github.com/fixture/straylight/issues/{args[2]}: '{name}' not found",
              file=sys.stderr)
        sys.exit(1)
    labels = issue(args[2])["labels"]
    if flag == "--add-label":
        labels.append(name)
    elif name in labels:
        labels.remove(name)
elif args[:2] == ["issue", "comment"]:
    issue(args[2])["comments"].append({"body": args[args.index("--body") + 1], "createdAt": "2999-01-01T00:00:00Z"})
elif args[:2] == ["issue", "view"]:
    print(json.dumps({"comments": issue(args[2])["comments"]}))
else:
    sys.exit(2)
json.dump(state, open(path, "w"))
'''

ALL_ON = """
[tickets]
integrated_label = true
land_comment = true
fixes_trailer = true
release_comment = true
preview = "https://preview.example/{short}/{lane}?ticket={ticket}"
"""

EVIDENCE = {"body": "RED -> GREEN: lib-check", "createdAt": "2999-01-01T00:00:00Z"}


class TicketCase(LaneCase):
    tickets = ALL_ON

    def setUp(self):
        super().setUp()
        gh = self.tmp / "bin" / "gh"
        gh.write_text(FAKE_TICKETS_GH)
        # gh is found on PATH, as in a project's shell.
        del self.env["LANEFATHER_GH"]
        self.gh_state = self.tmp / "tickets.json"
        self.gh_state.write_text(json.dumps({"labels": {}, "issues": {"404": {"labels": [], "comments": [EVIDENCE]}}}))
        self.env.update(FAKE_TICKETS_STATE=str(self.gh_state), FAKE_TICKETS_LOG=str(self.tmp / "tickets.log"))
        self.git(self.repo, "remote", "add", "origin", "https://github.com/fixture/straylight.git")
        if self.tickets:
            self.commit(self.integration, {".lanefather.toml": FIXTURE_CONFIG + self.tickets}, "ticket rules on")

    def gh_calls(self) -> list[list[str]]:
        log = self.tmp / "tickets.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def issue(self, number: str) -> dict[str, Any]:
        return json.loads(self.gh_state.read_text())["issues"].get(number, {"labels": [], "comments": []})

    def land_comments(self, number: str = "404") -> list[str]:
        return [c["body"] for c in self.issue(number)["comments"] if c != EVIDENCE]

    def ledger(self) -> str:
        return (self.state / "ledger.log").read_text()

    def checked(self, lane_id: str = "a") -> pathlib.Path:
        worktree = pathlib.Path(self.created(lane_id, "lib/**"))
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", lane_id)
        return worktree


class LandTests(TicketCase):
    def test_land_labels_the_issue_and_comments_the_merge_its_checks_and_the_preview(self):
        self.checked()
        output = self.lane("land", "a")
        merge = self.record("a")["landed"]["merge"]
        self.assertIn("#404 labelled integrated and commented in fixture/straylight", output)
        labels = json.loads(self.gh_state.read_text())["labels"]
        self.assertEqual(labels, {"integrated": {
            "color": "0E8A16", "description": "Done and landed in the integration/dev branch; not yet on main or released"}})
        self.assertEqual(self.issue("404")["labels"], ["integrated"])
        [comment] = self.land_comments()
        self.assertIn(f"Landed on `integrate/q` as `{merge[:10]}` (local only: origin does not have this merge yet)",
                      comment)
        self.assertNotIn("/commit/", comment, "a local-only SHA does not resolve on GitHub")
        self.assertIn("- Integration branch: `integrate/q`, local only", comment)
        self.assertIn("lane receipt green on tree", comment)
        self.assertIn("suites structure, lib (2 checks)", comment)
        self.assertIn("post-merge smoke green (", comment)
        self.assertIn(f"- Try it: https://preview.example/{merge[:10]}/a?ticket=404", comment)
        self.assertIn("Labelled `integrated`", comment)
        self.assertIn("`Fixes #404`", comment)
        # Every call names the project's repository and gh never prompts: stdin is closed.
        self.assertTrue(all(call[-2:] == ["--repo", "fixture/straylight"] for call in self.gh_calls()), self.gh_calls())
        self.assertIn("tickets land a #404: labelled integrated, commented (fixture/straylight)", self.ledger())

    def test_the_landing_merge_says_fixes_the_ticket(self):
        self.checked()
        self.lane("land", "a")
        message = self.git(self.integration, "log", "-1", "--format=%B")
        self.assertEqual(message.splitlines(), ["Merge lane/a (#404)", "", "Fixes #404"])

    def test_a_given_message_keeps_its_own_fixes_line(self):
        self.checked()
        self.lane("land", "a", "-m", "Merge the lib change\n\nFixes #404\n")
        self.assertEqual(self.git(self.integration, "log", "-1", "--format=%B").count("Fixes #404"), 1)

    def test_a_merge_origin_has_is_linked_and_its_branch_named_pushed(self):
        self.checked()
        # The smoke runs the full gate, so the test can move origin's ref while it holds.
        self.integration_moves()
        hold = self.tmp / "hold-a"
        land = self.background_land("a", hold)  # pyright: ignore[reportUnknownMemberType]
        self.wait_started(hold, land)  # pyright: ignore[reportUnknownMemberType]
        merge = self.record("a")["landed"]["merge"]
        self.git(self.repo, "update-ref", "refs/remotes/origin/integrate/q", merge)
        hold.touch()
        output = land.communicate(timeout=60)[0]
        self.assertEqual(land.returncode, 0, output)
        [comment] = self.land_comments()
        self.assertIn(f"as [`{merge[:10]}`](https://github.com/fixture/straylight/commit/{merge}) "
                      "(pushed: origin/integrate/q)", comment)
        self.assertIn("- Integration branch: `integrate/q`, on origin", comment)
        self.assertNotIn("local only", comment)

    def test_a_gh_failure_warns_in_the_ledger_and_never_fails_the_land(self):
        self.checked()
        self.env["FAKE_TICKETS_FAIL"] = "issue"
        output = self.lane("land", "a")
        merge = self.record("a")["landed"]["merge"]
        self.assertEqual(self.tip(), merge)
        self.assertIn("lane: warning: land a #404: label integrated: gh issue edit exited 1: HTTP 502", output)
        self.assertIn("lane: warning: land a #404: comment: gh issue comment exited 1: HTTP 502", output)
        self.assertIn("tickets land a #404 warning: label integrated: gh issue edit exited 1", self.ledger())
        self.assertEqual(self.land_comments(), [])

    def test_a_red_smoke_leaves_the_issue_unmarked(self):
        self.checked()
        # The integration calls what no lib defines after the lane checked; the merge's smoke goes red.
        self.commit(self.integration, {"calls/extra.txt": "qux\n", "tooling/moved.txt": "x\n"},
                    "the integration calls qux")
        output = self.lane("land", "a", ok=False)
        self.assertIn("#404 is not marked integrated: the post-merge smoke is red", output)
        self.assertEqual(self.gh_calls(), [])

    def test_done_still_wants_the_implementers_evidence_besides_the_land_comment(self):
        self.gh_state.write_text(json.dumps({"labels": {}, "issues": {}}))
        self.checked()
        self.lane("land", "a")
        self.assertEqual(len(self.land_comments()), 1)
        # The clean review finishes the lane as done does; done counts only the implementer's comments.
        output = self.lane("review", "a")
        self.assertIn("#404 has no comment since the lane started", output)
        self.assertEqual(self.record("a")["status"], "landed")
        self.gh_state.write_text(json.dumps({**json.loads(self.gh_state.read_text()),
                                             "issues": {"404": {"labels": [], "comments": [EVIDENCE]}}}))
        self.lane("done", "a")
        self.assertEqual(self.record("a")["status"], "done")

    def test_start_warns_about_a_lane_without_a_ticket(self):
        output = self.lane("start", "b", "--ticket", "none", "--base", "integrate/q", "--claims", "calls/**")
        self.assertIn("lane: warning: lane b has no ticket, but [tickets] in .lanefather.toml is on", output)
        self.assertIn("--ticket N", output)
        queue = self.tmp / "queue.toml"
        queue.write_text('[[lane]]\nticket = "none"\nid = "c"\nclaims = ["native/**"]\n')
        self.env["LANEFATHER_QUEUE"] = str(queue)
        output = self.lane("next", "--start", "--base", "integrate/q")
        self.assertIn("lane: warning: lane c has no ticket", output)


class ReleaseTests(TicketCase):
    def test_shipped_comments_the_version_on_each_issue_a_fixes_line_names(self):
        base = self.tip()
        self.checked()
        self.lane("land", "a")
        self.commit(self.integration, {"docs/notes.md": "x\n"}, "Fix the guide\n\nfixes #77")
        self.commit(self.integration, {"docs/notes.md": "y\n"}, "Fix the guide again\n\nFixes #77\nRefs #12")
        head = self.tip()
        output = self.lane("shipped", "v1.4.0", "--since", base)
        self.assertIn("release v1.4.0: commented on 2 of 2 issues it fixes (#404, #77)", output)
        released = f"Released in `v1.4.0` ([`{head[:10]}`](https://github.com/fixture/straylight/commit/{head}))."
        self.assertEqual(self.land_comments("77"), [released])
        self.assertEqual(self.land_comments("404")[-1], released)
        self.assertEqual(self.issue("12")["comments"], [])
        # Released, #404 is no longer "not yet on main".
        self.assertEqual(self.issue("404")["labels"], [])
        removals = [call for call in self.gh_calls() if "--remove-label" in call]
        self.assertEqual([call[2] for call in removals], ["404", "77"])
        self.assertIn("label integrated removed from 2", self.ledger())
        self.assertNotIn("warning", output)

    def test_ship_comments_the_main_sha_on_the_issues_it_closes(self):
        self.git(self.repo, "branch", "dev")
        remote = self.tmp / "remote.git"
        self.git(self.tmp, "init", "-q", "--bare", str(remote))
        (self.tmp / "no-hooks").mkdir()
        self.git(self.repo, "config", "core.hooksPath", str(self.tmp / "no-hooks"))
        self.git(self.repo, "remote", "set-url", "origin", str(remote))
        self.git(self.repo, "push", "-q", "origin", "main", "dev")
        # origin is a local bare repository here, so the config names the GitHub repository.
        self.commit(self.integration, {".lanefather.toml": FIXTURE_CONFIG + ALL_ON + 'repo = "fixture/straylight"\n'},
                    "name the repository")
        self.checked()
        self.lane("land", "a")
        self.lane("review", "a")
        tip = self.tip()
        output = self.lane("promote", tip, "--ship", "--wait", "0")
        self.assertIn("release " + tip[:10] + ": commented on 1 of 1 issues it fixes (#404)", output)
        self.assertEqual(self.land_comments("404")[-1],
                         f"Shipped to the default branch as [`{tip[:10]}`](https://github.com/fixture/straylight/"
                         f"commit/{tip}).")
        self.assertEqual(self.issue("404")["labels"], [])

    def test_a_failed_label_removal_warns_in_the_ledger_and_keeps_the_comment(self):
        base = self.tip()
        self.checked()
        self.lane("land", "a")
        self.env["FAKE_TICKETS_FAIL"] = "--remove-label"
        output = self.lane("shipped", "v1.4.0", "--since", base)
        self.assertIn("lane: warning: release v1.4.0: #404: remove label integrated: gh issue edit exited 1: HTTP 502",
                      output)
        self.assertIn("tickets release v1.4.0 warning: #404: remove label integrated", self.ledger())
        self.assertTrue(self.land_comments("404")[-1].startswith("Released in `v1.4.0`"))
        self.assertEqual(self.issue("404")["labels"], ["integrated"])

    def test_a_release_comment_failure_warns_and_never_fails_the_command(self):
        base = self.tip()
        self.commit(self.integration, {"docs/notes.md": "x\n"}, "Fix the guide\n\nFixes #77")
        self.env["FAKE_TICKETS_FAIL"] = "comment"
        output = self.lane("shipped", "v1.4.0", "--since", base)
        self.assertIn("lane: warning: release v1.4.0: #77: gh issue comment exited 1", output)
        self.assertIn("tickets release v1.4.0 warning: #77", self.ledger())


class ConfigOffTests(TicketCase):
    tickets = ""

    def test_without_the_table_land_and_shipped_leave_the_issue_alone(self):
        base = self.tip()
        output = self.lane("start", "b", "--ticket", "none", "--base", "integrate/q", "--claims", "calls/**")
        self.assertNotIn("warning", output)
        self.checked()
        output = self.lane("land", "a")
        self.assertEqual(self.git(self.integration, "log", "-1", "--format=%B"), "Merge lane/a (#404)")
        self.assertNotIn("integrated", output)
        self.assertEqual(self.gh_calls(), [])
        self.commit(self.integration, {"docs/notes.md": "x\n"}, "Fix the guide\n\nFixes #77")
        output = self.lane("shipped", "v1.4.0", "--since", base)
        self.assertIn("release_comment is off", output)
        self.assertEqual(self.gh_calls(), [])


class ConfigTests(unittest.TestCase):
    def test_the_table_is_checked(self):
        from lanefather import config
        from lanefather.errors import LaneError
        self.assertEqual(config.parse({}).tickets, config.Tickets())
        self.assertFalse(config.parse({}).tickets.enabled())
        parsed = config.parse({"tickets": {"land_comment": True, "preview": "https://x/{sha}"}}).tickets
        self.assertEqual((parsed.land_comment, parsed.integrated_label, parsed.preview),
                         (True, False, "https://x/{sha}"))
        for table, words in (({"land_coment": True}, "[tickets] has unknown keys land_coment"),
                             ({"fixes_trailer": "yes"}, "fixes_trailer must be a boolean"),
                             ({"repo": "straylight"}, "repo must be owner/name")):
            with self.subTest(table=table), self.assertRaises(LaneError) as caught:
                config.parse({"tickets": table})
            self.assertIn(words, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
