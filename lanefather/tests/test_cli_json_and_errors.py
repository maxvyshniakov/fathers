"""CLI convention conformance, versioned JSON replies, --slots/--jobs aliases, exit codes, and error handling
(fathers#130, docs/cli.md)."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from support import LaneCase, ScratchCase, run_in_process_streams

from lanefather import cli, lanes


class CLIVersionedJSONTests(LaneCase):
    def test_start_check_land_promote_done_smoke_and_list_show_json_all_carry_v1(self) -> None:
        # start --json (planned)
        code, out, err = run_in_process_streams(
            ["start", "a", "--ticket", "404", "--claims", "lib/**", "--json"], self.integration, self.env,
        )
        self.assertEqual(code, 0, out + err)
        started = json.loads(out)
        self.assertEqual(started["v"], 1)
        self.assertEqual(started["lane"], "a")
        self.assertEqual(started["status"], "planned")
        self.assertFalse(started["activated"])
        self.assertIn("Lane a", started["brief"])

        # start --create --json (activate planned lane)
        code, out, err = run_in_process_streams(
            ["start", "a", "--create", "--json"], self.integration, self.env,
        )
        self.assertEqual(code, 0, out + err)
        activated = json.loads(out)
        self.assertEqual(activated["v"], 1)
        self.assertEqual(activated["lane"], "a")
        self.assertEqual(activated["status"], "active")
        self.assertTrue(activated["activated"])
        self.assertIn("Lane a", activated["brief"])
        worktree = activated["worktree"]
        self.assertTrue(worktree)

        # list --json carries v=1
        listed = json.loads(self.lane("list", "--json"))
        self.assertEqual(listed["v"], 1)
        self.assertEqual([item["id"] for item in listed["lanes"]], ["a"])

        # show --json carries both v=1 and legacy version=1
        shown = json.loads(self.lane("show", "a", "--json"))
        self.assertEqual(shown["v"], 1)
        self.assertEqual(shown["version"], 1)
        self.assertEqual(shown["id"], "a")

        # commit a change and run check --json
        self.commit(worktree, {"lib/api.txt": "foo\na\n"})
        code, out, err = run_in_process_streams(
            ["check", "a", "--json"], self.integration, self.env,
        )
        self.assertEqual(code, 0, out + err)
        checked = json.loads(out)
        self.assertEqual(checked["v"], 1)
        self.assertEqual(checked["lane"], "a")
        self.assertEqual(checked["status"], "green")
        self.assertFalse(checked["reused"])
        self.assertEqual(checked["exit_code"], 0)

        # check --json again reuses green receipt
        code, out, err = run_in_process_streams(
            ["check", "a", "--json"], self.integration, self.env,
        )
        self.assertEqual(code, 0, out + err)
        reused = json.loads(out)
        self.assertEqual(reused["v"], 1)
        self.assertTrue(reused["reused"])

        # land --keep --json
        code, out, err = run_in_process_streams(
            ["land", "a", "--keep", "--json"], self.integration, self.env,
        )
        self.assertEqual(code, 0, out + err)
        landed = json.loads(out)
        self.assertEqual(landed["v"], 1)
        self.assertEqual(landed["lane"], "a")
        self.assertEqual(landed["post_merge"], "green")
        self.assertTrue(landed["keep"])
        self.assertEqual(landed["exit_code"], 0)
        merge_sha = landed["merge"]
        self.assertTrue(merge_sha)

        # smoke --json
        code, out, err = run_in_process_streams(
            ["smoke", "a", "--json"], self.integration, self.env,
        )
        self.assertEqual(code, 0, out + err)
        smoked = json.loads(out)
        self.assertEqual(smoked["v"], 1)
        self.assertEqual(smoked["lane"], "a")
        self.assertEqual(smoked["status"], "green")

        # done --json
        code, out, err = run_in_process_streams(
            ["done", "a", "--json"], self.integration, self.env,
        )
        self.assertEqual(code, 0, out + err)
        finished = json.loads(out)
        self.assertEqual(finished["v"], 1)
        self.assertEqual(finished["lane"], "a")
        self.assertEqual(finished["status"], "done")
        self.assertIsInstance(finished["freed_bytes"], int)

        # done --finished --dry-run --json
        code, out, err = run_in_process_streams(
            ["done", "--finished", "--dry-run", "--json"], self.integration, self.env,
        )
        self.assertEqual(code, 0, out + err)
        dry = json.loads(out)
        self.assertEqual(dry["v"], 1)
        self.assertTrue(dry["dry_run"])

        # promote --json (local promote with --no-push)
        self.git(self.repo, "branch", "dev")
        code, out, err = run_in_process_streams(
            ["promote", merge_sha, "--no-push", "--wait", "0", "--force-reason", "test promote json", "--json"],
            self.integration, self.env,
        )
        self.assertEqual(code, 0, out + err)
        promoted = json.loads(out)
        self.assertEqual(promoted["v"], 1)
        self.assertEqual(promoted["sha"], merge_sha)
        self.assertEqual(promoted["status"], "completed")
        self.assertTrue(promoted["no_push"])
        self.assertEqual(promoted["exit_code"], 0)

    def test_slots_and_jobs_aliases_parse_identically(self) -> None:
        self.assertEqual(cli.parse(["check", "a", "--slots", "2"]).slots, 2)
        self.assertEqual(cli.parse(["check", "a", "--jobs", "2"]).slots, 2)
        self.assertEqual(cli.parse(["land", "a", "--slots", "3"]).slots, 3)
        self.assertEqual(cli.parse(["land", "a", "--jobs", "3"]).slots, 3)
        self.assertEqual(cli.parse(["recheck", "a", "--slots", "2"]).slots, 2)
        self.assertEqual(cli.parse(["recheck", "a", "--jobs", "2"]).slots, 2)
        self.assertEqual(cli.parse(["smoke", "a", "--rerun", "--slots", "2"]).slots, 2)
        self.assertEqual(cli.parse(["smoke", "a", "--rerun", "--jobs", "2"]).slots, 2)
        self.assertEqual(cli.parse(["promote", "HEAD", "--slots", "4"]).jobs, 4)
        self.assertEqual(cli.parse(["promote", "HEAD", "--jobs", "4"]).jobs, 4)

    def test_error_prefix_uses_configured_command_name_and_not_found_exits_6(self) -> None:
        cfg = self.tmp / "mytool.toml"
        cfg.write_text('command = "mytool"\n' + (self.repo / ".lanefather.toml").read_text())
        code, out, err = run_in_process_streams(
            ["--config", str(cfg), "show", "no-such-lane"], self.integration, self.env,
        )
        self.assertEqual(code, 6)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("mytool: no lane no-such-lane"), err)

        code, out, err = run_in_process_streams(
            ["--config", str(cfg), "check", "no-such-lane"], self.integration, self.env,
        )
        self.assertEqual(code, 6)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("mytool: no lane no-such-lane"), err)

    def test_unexpected_exception_prints_one_line_and_logs_traceback_to_events(self) -> None:
        events_path = self.tmp / "events.jsonl"
        self.env["LANEFATHER_EVENTS"] = str(events_path)
        with mock.patch.object(lanes, "list_lanes", side_effect=KeyError("boom")):
            code, out, err = run_in_process_streams(["list"], self.integration, self.env)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, err)
        self.assertEqual(lines[0], "lanefather: internal error: KeyError: 'boom'")
        ledger = (self.state / "ledger.log").read_text()
        self.assertIn("internal-error list: KeyError: 'boom'", ledger)
        self.assertNotIn("Traceback (most recent call last):", ledger)
        event_rows = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        internal_events = [row for row in event_rows if row.get("kind") == "cli.internal_error"]
        self.assertTrue(internal_events, event_rows)
        tb = internal_events[-1]["details"]["traceback"]
        self.assertIn("Traceback (most recent call last):", tb)
        self.assertIn("KeyError: 'boom'", tb)

    def test_keyboard_interrupt_is_not_swallowed(self) -> None:
        with mock.patch.object(lanes, "list_lanes", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            run_in_process_streams(["list"], self.integration, self.env)


class CLIScratchErrorTests(ScratchCase):
    def test_missing_lane_with_show_state_dir_exits_6(self) -> None:
        code, out, err = run_in_process_streams(
            ["show", "missing", "--state-dir", str(self.tmp / "empty")], self.tmp, self.env,
        )
        self.assertEqual(code, 6)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("lanefather: no lane missing"), err)


if __name__ == "__main__":
    unittest.main()
