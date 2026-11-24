"""Tests of the ticket lifecycle (lanefather.tickets, #25): what land, promote --ship and shipped tell a lane's issue
through a stand-in gh first on PATH, in scratch Git repositories (support.py)."""
from __future__ import annotations

import json
import pathlib
from typing import Any
import unittest

from support import FIXTURE_CONFIG, TWO_FINDINGS, LaneCase

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
with open(path) as handle:
    state = json.load(handle)
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
with open(path, "w") as handle:
    json.dump(state, handle)
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


class ScopeClosureTests(TicketCase):
    """A lane that completes #404 and #405 and does part of #406 (fathers#192, #64): its merge says Fixes for the
    first two and Refs for the third; land labels the two and comments on all three; a release closes the two."""

    def scoped_lane(self, lane_id: str = "a", message: str = "lane change") -> pathlib.Path:
        self.scoped(lane_id, "--ticket", "404", "--ticket", "405", "--partial", "406")
        worktree = pathlib.Path(self.record(lane_id)["worktree"])
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"}, message)
        self.lane("check", lane_id)
        return worktree

    def test_the_merge_says_fixes_for_each_completed_ticket_and_refs_for_the_partial_one(self):
        self.scoped_lane()
        self.lane("land", "a")
        self.assertEqual(self.git(self.integration, "log", "-1", "--format=%B").splitlines(),
                         ["Merge lane/a (#404, #405, #406)", "", "Fixes #404", "Fixes #405", "Refs #406"])

    def test_a_given_message_keeps_its_own_lines_and_gains_only_the_missing_ones(self):
        self.scoped_lane()
        self.lane("land", "a", "-m", "Merge the lib change\n\nFixes #405\nRefs #406\n")
        message = self.git(self.integration, "log", "-1", "--format=%B")
        self.assertEqual(message.splitlines(),
                         ["Merge the lib change", "", "Fixes #405", "Refs #406", "", "Fixes #404"])

    def test_land_labels_the_completed_tickets_and_comments_on_every_one(self):
        self.scoped_lane()
        output = self.lane("land", "a")
        self.assertEqual(self.issue("404")["labels"], ["integrated"])
        self.assertEqual(self.issue("405")["labels"], ["integrated"])
        self.assertEqual(self.issue("406")["labels"], [], "a partial ticket stays unlabelled")
        self.assertIn("#406 commented in fixture/straylight (partial: not labelled)", output)
        [done] = self.land_comments("404")
        [also] = self.land_comments("405")
        [partial] = self.land_comments("406")
        for comment in (done, also, partial):
            self.assertIn("- Lane scope: #404, #405, #406 (partial)", comment)
            self.assertIn("?ticket=404", comment, "the preview's {ticket} stays the primary ticket")
        self.assertIn("`Fixes #404`", done)
        self.assertIn("`Fixes #405`", also)
        self.assertIn("Labelled `integrated`", also)
        self.assertIn("Partial: this merge does part of #406 (`Refs #406`); the issue stays open and is not "
                      "labelled `integrated`.", partial)
        self.assertNotIn("Labelled", partial)
        self.assertNotIn("Fixes #", partial)
        marked = self.events_of("ticket.marked")
        self.assertEqual([event["details"]["issue"] for event in marked], ["404", "405", "406"])
        self.assertEqual([event["details"].get("partial", False) for event in marked], [False, False, True])

    def test_a_red_smoke_leaves_every_ticket_unmarked(self):
        self.scoped_lane()
        self.commit(self.integration, {"calls/extra.txt": "qux\n", "tooling/moved.txt": "x\n"},
                    "the integration calls qux")
        output = self.lane("land", "a", ok=False)
        self.assertIn("#404, #405, #406 are not marked integrated: the post-merge smoke is red", output)
        self.assertEqual([event["details"]["issue"] for event in self.events_of("ticket.skipped")],
                         ["404", "405", "406"])
        self.assertEqual(self.gh_calls(), [])

    def test_a_lane_commit_that_says_fixes_for_a_partial_ticket_warns_and_lands(self):
        self.scoped_lane(message="lane change\n\nFixes #406")
        lane_commit = self.git(pathlib.Path(self.record("a")["worktree"]), "rev-parse", "HEAD")
        output = self.lane("land", "a")
        warning = (f"lane commit {lane_commit[:10]} says Fixes #406, which is partial: GitHub closes #406 when it "
                   "reaches the default branch")
        self.assertIn(f"lane: warning: {warning}", output)
        [conflict] = self.events_of("ticket.trailer_conflict")
        self.assertEqual((conflict["level"], conflict["details"]["conflicts"]), ("warn", [warning]))
        self.assertEqual(self.record("a")["status"], "landed")

    def test_a_lane_without_partial_tickets_never_warns_about_its_own_fixes_lines(self):
        worktree = self.checked()
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\nqux\n"}, "more\n\nFixes #404")
        self.lane("check", "a")
        self.assertNotIn("which is partial", self.lane("land", "a"))
        self.assertEqual(self.events_of("ticket.trailer_conflict"), [])

    def test_shipped_comments_and_unlabels_the_completed_tickets_only(self):
        base = self.tip()
        self.scoped_lane()
        self.lane("land", "a")
        output = self.lane("shipped", "v1.4.0", "--since", base)
        self.assertIn("release v1.4.0: commented on 2 of 2 issues it fixes (#404, #405)", output)
        self.assertEqual((self.issue("404")["labels"], self.issue("405")["labels"]), ([], []))
        self.assertTrue(self.land_comments("405")[-1].startswith("Released in `v1.4.0`"))
        [partial] = self.land_comments("406")
        self.assertTrue(partial.startswith("<!-- lanefather:land -->"), "the partial ticket has only the land comment")
        removals = [call[2] for call in self.gh_calls() if "--remove-label" in call]
        self.assertEqual(removals, ["404", "405"])

    def test_promote_ship_closes_the_completed_tickets_only(self):
        self.git(self.repo, "branch", "dev")
        remote = self.tmp / "remote.git"
        self.git(self.tmp, "init", "-q", "--bare", str(remote))
        (self.tmp / "no-hooks").mkdir()
        self.git(self.repo, "config", "core.hooksPath", str(self.tmp / "no-hooks"))
        self.git(self.repo, "remote", "set-url", "origin", str(remote))
        self.git(self.repo, "push", "-q", "origin", "main", "dev")
        self.commit(self.integration, {".lanefather.toml": FIXTURE_CONFIG + ALL_ON + 'repo = "fixture/straylight"\n'},
                    "name the repository")
        self.scoped_lane()
        self.lane("land", "a")
        self.lane("review", "a")
        tip = self.tip()
        output = self.lane("promote", tip, "--ship", "--wait", "0")
        self.assertIn("release " + tip[:10] + ": commented on 2 of 2 issues it fixes (#404, #405)", output)
        shipped = f"Shipped to the default branch as [`{tip[:10]}`](https://github.com/fixture/straylight/commit/{tip})."
        self.assertEqual((self.land_comments("404")[-1], self.land_comments("405")[-1]), (shipped, shipped))
        self.assertNotIn(shipped, self.land_comments("406"))
        self.assertEqual(self.issue("406")["labels"], [])

    def test_the_fix_lane_inherits_the_scope_as_partial_and_its_merge_only_refers(self):
        self.review_text.write_text(TWO_FINDINGS)
        worktree = self.scoped_lane()
        self.lane("land", "a")
        self.lane("review", "a")
        self.assertIn("scope refused: lane a is landed", self.lane("scope", "a", "--add-partial", "9", ok=False))
        self.lane("review", "a", "--fix-lane")
        fix = self.record("a-review-fixes")
        self.assertEqual(fix["ticket"], "404")
        self.assertEqual(fix["scope"], [{"issue": "404", "partial": True}, {"issue": "405", "partial": True},
                                        {"issue": "406", "partial": True}])
        self.git(worktree, "checkout", "-q", "-B", "lane/a-review-fixes", "integrate/q")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\nqux\n"}, "fix(review): keep baz")
        self.lane("check", "a-review-fixes", cwd=worktree)
        self.lane("land", "a-review-fixes")
        message = self.git(self.integration, "log", "-1", "--format=%B")
        self.assertEqual(message.splitlines()[-3:], ["Refs #404", "Refs #405", "Refs #406"])
        self.assertNotIn("Fixes", message)
        self.assertEqual(self.issue("404")["labels"], ["integrated"], "the parent's land labelled it once")


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


SID_KIT = "maxvyshniakov/sid-kit"


class ForeignTicketFormTests(unittest.TestCase):
    """A ticket of another repository (fathers#238): `owner/repo#N` or its issue URL; a bare number stays the lane's
    own repository's, stored as it always was."""

    def test_a_bare_number_reads_as_today(self):
        from lanefather import scope
        self.assertEqual((scope.ticket_number("12"), scope.ticket_number("#12")), ("12", "12"))
        issue = scope.Issue("12")
        self.assertEqual((issue.repo, issue.local, issue.ref), (None, "12", "#12"))
        self.assertEqual(scope.to_json(issue), {"issue": "12"})

    def test_owner_repo_number_and_the_issue_url_name_the_same_foreign_ticket(self):
        from lanefather import scope
        for given in (f"{SID_KIT}#97", f"https://github.com/{SID_KIT}/issues/97", f" {SID_KIT}#97 "):
            with self.subTest(given=given):
                self.assertEqual(scope.ticket_number(given), f"{SID_KIT}#97")
        issue = scope.Issue(f"{SID_KIT}#97", partial=True)
        self.assertEqual((issue.repo, issue.local, issue.ref), (SID_KIT, "97", f"{SID_KIT}#97"))
        self.assertEqual(scope.to_json(issue), {"issue": f"{SID_KIT}#97", "partial": True})

    def test_a_malformed_foreign_ticket_is_refused(self):
        from lanefather import scope
        from lanefather.errors import LaneError
        for given in (f"{SID_KIT}#", f"{SID_KIT}#x", "sid-kit#97", f"{SID_KIT}/x#97", "/sid-kit#97",
                      "o/..#1", f"https://github.com/{SID_KIT}/pull/97",
                      f"https://github.com/{SID_KIT}/issues/97#issuecomment-1", f"http://github.com/{SID_KIT}/issues/9"):
            with self.subTest(given=given), self.assertRaises(LaneError):
                scope.ticket_number(given)

    def test_a_foreign_scope_round_trips_through_the_record_and_names_the_primary(self):
        from lanefather import scope
        items = scope.from_options([(f"https://github.com/{SID_KIT}/issues/97", False), ("12", True)])
        record: dict[str, Any] = {"id": "a"}
        scope.write(record, items)
        self.assertEqual(record["ticket"], f"{SID_KIT}#97")
        self.assertEqual(record["scope"], [{"issue": f"{SID_KIT}#97"}, {"issue": "12", "partial": True}])
        self.assertEqual(scope.of(record), items)
        self.assertEqual(scope.describe(items), f"{SID_KIT}#97, #12 (partial)")
        alone: dict[str, Any] = {"id": "b"}
        scope.write(alone, scope.from_options([(f"{SID_KIT}#97", False)]))
        self.assertEqual(alone, {"id": "b", "ticket": f"{SID_KIT}#97"}, "one completed ticket writes no scope")
        self.assertEqual(scope.of(alone), (scope.Issue(f"{SID_KIT}#97"),))

    def test_the_same_number_in_two_repositories_is_two_tickets(self):
        from lanefather import scope
        items = scope.from_options([("97", False), (f"{SID_KIT}#97", True)])
        self.assertEqual([issue.ref for issue in items if isinstance(issue, scope.Issue)], ["#97", f"{SID_KIT}#97"])

    def test_the_merge_says_fixes_or_refs_in_the_cross_repository_form(self):
        from lanefather import config, scope, tickets
        rule = config.Tickets(fixes_trailer=True)
        issues = [scope.Issue("12"), scope.Issue(f"{SID_KIT}#97"), scope.Issue(f"{SID_KIT}#98", partial=True)]
        self.assertEqual(tickets.merge_message("Merge lane/a", issues, rule).splitlines(),
                         ["Merge lane/a", "", "Fixes #12", f"Fixes {SID_KIT}#97", f"Refs {SID_KIT}#98"])
        given = f"Merge it\n\nFixes {SID_KIT}#97\nFixes #98\n"
        self.assertEqual(tickets.merge_message(given, issues, rule).splitlines(),
                         ["Merge it", "", f"Fixes {SID_KIT}#97", "Fixes #98", "", "Fixes #12", f"Refs {SID_KIT}#98"],
                         "a given line keeps its ticket; #98 of this repository is not sid-kit's #98")

    def test_loose_fixes_refs_are_skipped_without_raising(self):
        from lanefather import config, scope, tickets
        long_owner = "a" * 40
        long_digits = "1" * 65
        bogus = f"Fixes foo/.#1\nFixes foo/..#1\nFixes {long_owner}/repo#1\nFixes #{long_digits}\n"
        rule = config.Tickets(fixes_trailer=True)
        merged = tickets.merge_message(f"Merge it\n\n{bogus}", [scope.Issue("42")], rule)
        self.assertIn("Fixes #42", merged)
        conflicts = tickets.trailer_conflicts(".", "HEAD", "HEAD", [scope.Issue("42", partial=True)],
                                              message=f"{bogus}Fixes #42\n")
        self.assertEqual(len(conflicts), 1)
        self.assertIn("Fixes #42", conflicts[0])

    def test_display_ticket_formats_local_foreign_and_none(self):
        from lanefather import scope
        self.assertEqual(scope.display_ticket("42"), "#42")
        self.assertEqual(scope.display_ticket("#42"), "#42")
        self.assertEqual(scope.display_ticket(f"{SID_KIT}#97"), f"{SID_KIT}#97")
        self.assertEqual(scope.display_ticket("none"), "none")

    def test_case_insensitive_owner_repo_and_own_repo_deduplication(self):
        from lanefather import config, scope, tickets
        from lanefather.errors import LaneError
        with self.assertRaises(LaneError):
            scope.from_options([("owner/repo#5", False), ("Owner/Repo#5", True)])
        with self.assertRaises(LaneError):
            scope.from_options([("https://github.com/owner/repo/issues/5", False), ("Owner/Repo#5", True)])
        rule = config.Tickets(fixes_trailer=True)
        merged = tickets.merge_message("Merge\n\nFixes Owner/Repo#5\n", [scope.Issue("owner/repo#5")], rule)
        self.assertEqual(merged.splitlines(), ["Merge", "", "Fixes Owner/Repo#5"])
        own_rule = config.Tickets(fixes_trailer=True, repo="fixture/straylight")
        merged_own = tickets.merge_message("Merge\n\nFixes Fixture/Straylight#404\n", [scope.Issue("404")], own_rule)
        self.assertEqual(merged_own.splitlines(), ["Merge", "", "Fixes Fixture/Straylight#404"])


class ForeignTicketLandTests(TicketCase):
    """A lane of this repository (fixture/straylight) that completes or does part of a sid-kit ticket (fathers#238):
    the merge uses the cross-repository form, and the label and comment go to sid-kit."""

    def foreign_lane(self, *scope_args: str) -> None:
        self.scoped("a", *scope_args)
        worktree = pathlib.Path(self.record("a")["worktree"])
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")

    def calls_on(self, repo: str, verb: str) -> list[list[str]]:
        return [call for call in self.gh_calls()
                if call[:2] == ["issue", verb] and call[call.index("--repo") + 1] == repo]

    def test_a_completed_foreign_ticket_lands_as_fixes_and_is_marked_in_its_repository(self):
        self.foreign_lane("--ticket", f"{SID_KIT}#97")
        output = self.lane("land", "a")
        merge = self.git(self.integration, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.integration, "log", "-1", "--format=%B").splitlines(),
                         [f"Merge lane/a ({SID_KIT}#97)", "", f"Fixes {SID_KIT}#97"])
        self.assertEqual({call[2] for call in self.calls_on(SID_KIT, "edit")}, {"97"})
        created = [call for call in self.gh_calls() if call[:2] == ["label", "create"]]
        self.assertEqual([call[call.index("--repo") + 1] for call in created], [SID_KIT],
                         "the label is created in sid-kit, which lacks it")
        self.assertEqual([call[2] for call in self.calls_on(SID_KIT, "comment")], ["97"])
        self.assertTrue(all(call[call.index("--repo") + 1] == SID_KIT for call in self.gh_calls()))
        self.assertEqual(self.issue("97")["labels"], ["integrated"])
        [comment] = self.land_comments("97")
        self.assertIn(f"The merge says `Fixes {SID_KIT}#97`, so GitHub closes this issue when the merge reaches "
                      "the default branch of fixture/straylight.", comment)
        self.assertIn(f"Landed on `integrate/q` as `{merge[:10]}`", comment)
        self.assertIn(f"{SID_KIT}#97 labelled integrated and commented in {SID_KIT}", output)
        [marked] = self.events_of("ticket.marked")
        self.assertEqual((marked["details"]["issue"], marked["details"]["repository"]), (f"{SID_KIT}#97", SID_KIT))

    def test_a_partial_foreign_ticket_refers_and_gets_only_the_comment(self):
        self.foreign_lane("--ticket", "404", "--partial", f"https://github.com/{SID_KIT}/issues/97")
        self.lane("land", "a")
        self.assertEqual(self.git(self.integration, "log", "-1", "--format=%B").splitlines(),
                         [f"Merge lane/a (#404, {SID_KIT}#97)", "", "Fixes #404", f"Refs {SID_KIT}#97"])
        self.assertEqual(self.calls_on(SID_KIT, "edit"), [], "a partial ticket is not labelled")
        self.assertEqual([call[2] for call in self.calls_on(SID_KIT, "comment")], ["97"])
        self.assertEqual([call[2] for call in self.calls_on("fixture/straylight", "comment")], ["404"])
        self.assertEqual(self.issue("404")["labels"], ["integrated"])
        [partial] = self.land_comments("97")
        self.assertIn(f"- Lane scope: #404, {SID_KIT}#97 (partial)", partial)
        self.assertIn(f"Partial: this merge does part of {SID_KIT}#97 (`Refs {SID_KIT}#97`); the issue stays open "
                      "and is not labelled `integrated`.", partial)

    def test_a_lane_commit_that_fixes_a_partial_foreign_ticket_warns(self):
        self.scoped("a", "--ticket", "404", "--partial", f"{SID_KIT}#97")
        worktree = pathlib.Path(self.record("a")["worktree"])
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"}, f"lane change\n\nFixes {SID_KIT}#97\nFixes #97")
        self.lane("check", "a")
        output = self.lane("land", "a")
        self.assertIn(f"says Fixes {SID_KIT}#97, which is partial", output)
        self.assertNotIn("says Fixes #97,", output, "#97 of this repository is not the partial ticket")

    def test_shipped_comments_and_unlabels_a_foreign_ticket_in_its_repository(self):
        base = self.tip()
        self.foreign_lane("--ticket", f"{SID_KIT}#97")
        self.lane("land", "a")
        output = self.lane("shipped", "v1.4.0", "--since", base)
        self.assertIn(f"release v1.4.0: commented on 1 of 1 issues it fixes ({SID_KIT}#97)", output)
        self.assertTrue(self.land_comments("97")[-1].startswith("Released in `v1.4.0`"))
        self.assertIn("https://github.com/fixture/straylight/commit/", self.land_comments("97")[-1])
        removals = [call for call in self.calls_on(SID_KIT, "edit") if "--remove-label" in call]
        self.assertEqual([call[2] for call in removals], ["97"])
        self.assertEqual(self.issue("97")["labels"], [])

    def test_done_reads_a_foreign_primary_ticket_in_its_repository(self):
        self.foreign_lane("--ticket", f"{SID_KIT}#97")
        listed = self.lane("list")
        self.assertIn(f" {SID_KIT}#97 ", listed)
        self.assertNotIn(f"#{SID_KIT}#97", listed)
        scoped = self.lane("scope", "a")
        self.assertIn(f"(primary ticket {SID_KIT}#97)", scoped)
        self.assertNotIn(f"#{SID_KIT}#97", scoped)
        self.lane("land", "a")
        output = self.lane("review", "a")
        self.assertIn(f"{SID_KIT}#97 has no comment since the lane started; post the lane's RED->GREEN evidence with "
                      f"gh issue comment 97 -R {SID_KIT}", output)
        self.assertNotIn(f"#{SID_KIT}#97", output)
        self.assertTrue(any(call[2] == "97" and "comments" in call for call in self.calls_on(SID_KIT, "view")))
        self.assertEqual(self.record("a")["status"], "landed")
        state = json.loads(self.gh_state.read_text())
        state["issues"].setdefault("97", {"labels": [], "comments": []})["comments"].append(EVIDENCE)
        self.gh_state.write_text(json.dumps(state))
        self.lane("done", "a")
        self.assertEqual(self.record("a")["status"], "done")

    def test_shipped_deduplicates_case_variants_own_repo_refs_and_skips_loose_refs(self):
        base = self.tip()
        long_owner = "a" * 40
        long_digits = "1" * 65
        self.commit(self.integration, {"docs/notes.md": "x\n"},
                    f"Fix issues\n\nFixes {SID_KIT}#97\nFixes MaxVyshniakov/Sid-Kit#97\n"
                    f"Fixes #404\nFixes Fixture/Straylight#404\n"
                    f"Fixes foo/.#1\nFixes foo/..#1\nFixes {long_owner}/repo#1\nFixes #{long_digits}\n")
        output = self.lane("shipped", "v1.5.0", "--since", base)
        self.assertIn(f"release v1.5.0: commented on 2 of 2 issues it fixes ({SID_KIT}#97, #404)", output)
        self.assertEqual([call[2] for call in self.calls_on(SID_KIT, "comment")], ["97"])
        self.assertEqual([call[2] for call in self.calls_on("fixture/straylight", "comment")], ["404"])


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
