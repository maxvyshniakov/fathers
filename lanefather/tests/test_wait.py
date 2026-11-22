"""What an agent needs without relearning the CLI or polling `list` (fathers#175): a top-level help of one screen,
a misused command's usage and example, `wait` for a lane's state, and the next commands start and adopt print."""
from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from support import FIXTURE_CONFIG, ROOT, LaneCase, ScratchCase, project

from lanefather import wait


def lanefather(*args: str, columns: int = 80) -> subprocess.CompletedProcess[str]:
    """The command outside any repository, as an agent's first call runs it."""
    with tempfile.TemporaryDirectory(prefix="lanefather-help-") as home:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LANEFATHER_")}
        environment.update(PYTHONPATH=str(ROOT), COLUMNS=str(columns), HOME=home,
                           GIT_CEILING_DIRECTORIES=str(Path(home).parent), LANEFATHER_EVENTS=f"{home}/events.jsonl",
                           LANEFATHER_REGISTRY=f"{home}/projects.json", LANEFATHER_SETTINGS=f"{home}/settings.json")
        return subprocess.run([sys.executable, "-m", "lanefather", *args], cwd=home, env=environment,
                              capture_output=True, text=True, timeout=60, check=False)


class HelpTests(unittest.TestCase):
    def test_the_top_level_help_fits_one_screen_with_one_line_per_command(self):
        done = lanefather("--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        self.assertLessEqual(len(lines), 45, done.stdout)
        self.assertLessEqual(max(len(line) for line in lines), 80, done.stdout)
        from lanefather import cli
        names = list(cli.COMMANDS)
        listed = [line.split()[0] for line in lines if re.match(r"^    \S", line)]
        self.assertEqual(listed, names, "each command on one line of its own, in the table's order")
        self.assertIn("wait", names)

    def test_a_commands_help_holds_its_details_and_an_example(self):
        done = lanefather("start", "--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage: lanefather start <id> --ticket N --claims GLOB...", done.stdout)
        self.assertIn("--fix-of", done.stdout)
        self.assertRegex(done.stdout, r"(?m)^example: lanefather start \S+ --ticket \d+ --claims ")

    def test_a_misused_command_prints_its_usage_and_one_example_in_two_lines(self):
        done = lanefather("start", "fix-login")
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, "")
        reason, pointer = done.stderr.splitlines()
        self.assertEqual(reason, "lanefather: the following arguments are required: --ticket, --claims")
        self.assertIn("Usage: lanefather start <id> --ticket N --claims GLOB...", pointer)
        self.assertIn("Example: lanefather start fix-login --ticket 12 --claims 'app/**'", pointer)
        self.assertIn("Try 'lanefather start --help'.", pointer)
        done = lanefather("wait", "x", "--for", "merged")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--for", done.stderr.splitlines()[0])
        self.assertIn("Usage: lanefather wait <id> --for registered|landed|done", done.stderr.splitlines()[1])

    def test_land_accepts_dashes_at_the_start_of_quoted_option_values(self):
        from lanefather import cli

        args = cli.parse(["land", "x", "-m", "- fix things", "--force-reason", "-1 flaky"])
        self.assertEqual((args.message, args.force_reason), ("- fix things", "-1 flaky"))

    def test_unknown_options_keep_the_help_pointer_of_the_parser_that_owns_them(self):
        from lanefather import cli

        for argv, help_for in ((["--unknown", "list"], "lanefather"), (["start", "--unknown"], "lanefather start")):
            with self.subTest(argv=argv):
                with redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as raised:
                    cli.parse(argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("lanefather: unknown option '--unknown'", error.getvalue())
                self.assertIn(f"Try '{help_for} --help'.", error.getvalue())


class WaitTests(LaneCase):
    def test_wait_answers_at_once_for_a_state_the_lane_has_reached(self):
        self.created("a", "lib/**")
        self.assertIn("Lane a is registered (active, worktree ", self.lane("wait", "a", "--for", "registered"))
        worktree = self.record("a")["worktree"]
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        self.lane("land", "a", "--keep")
        merge = self.record("a")["landed"]["merge"]
        self.assertIn(f"Lane a is landed (merge {merge[:10]})", self.lane("wait", "a", "--for", "landed"))
        reply = json.loads(self.lane("wait", "a", "--for", "registered", "--json", process=True))
        self.assertEqual({k: reply[k] for k in ("v", "lane", "for", "status", "reached")},
                         {"v": 1, "lane": "a", "for": "registered", "status": "landed", "reached": True})

    def test_a_timeout_exits_75_with_the_current_state(self):
        self.start("a", "lib/**")
        done = subprocess.run([sys.executable, "-m", "lanefather", "wait", "a", "--for", "landed", "--timeout", "0",
                               "--json"], cwd=self.integration, env=self.env, capture_output=True, text=True,
                              timeout=60, check=False)
        self.assertEqual(done.returncode, 75, done.stderr)
        self.assertEqual(done.stderr.strip(), "lanefather: lane a is planned, not landed, after 0 s")
        reply = json.loads(done.stdout)
        self.assertEqual((reply["status"], reply["reached"]), ("planned", False))
        output = self.lane("wait", "nobody", "--for", "registered", "--timeout", "0", ok=False)
        self.assertIn("lane nobody is not registered, not registered, after 0 s", output)

    def test_a_dropped_lane_never_lands_so_waiting_for_its_land_stops_at_once(self):
        self.created("a", "lib/**")
        self.lane("drop", "a")
        done = subprocess.run([sys.executable, "-m", "lanefather", "wait", "a", "--for", "landed"],
                              cwd=self.integration, env=self.env, capture_output=True, text=True, timeout=60,
                              check=False)
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("lane a is done without landing", done.stderr)
        self.assertIn("Lane a is done", self.lane("wait", "a", "--for", "done"))


class WaitUnitTests(ScratchCase):
    def test_wait_sleeps_with_a_growing_pause_until_the_state_arrives(self) -> None:
        self.write(self.tmp, ".lanefather.toml", FIXTURE_CONFIG)
        self.state.mkdir(parents=True)
        record_path = self.state / "b.json"
        naps: list[float] = []
        now = [0.0]

        def nap(seconds: float) -> None:
            naps.append(seconds)
            now[0] += seconds
            if len(naps) == 4:  # the coordinator registers the lane meanwhile
                saved: dict[str, Any] = {"id": "b", "status": "planned", "created": "later", "claims": ["native/**"]}
                record_path.write_text(json.dumps(saved))

        with patch.dict("os.environ", self.env, clear=True), project(self.tmp):
            outcome, record = wait.wait_for("b", "registered", 600, sleep=nap, clock=lambda: now[0])
        self.assertEqual((outcome, record and record["status"]), (True, "planned"))
        self.assertEqual(len(naps), 4)
        self.assertEqual(naps, sorted(naps), "the pause grows")
        self.assertLessEqual(max(naps), wait.LONGEST)
        with patch.dict("os.environ", self.env, clear=True), project(self.tmp):
            outcome, _ = wait.wait_for("c", "registered", 3, sleep=nap, clock=lambda: now[0])
        self.assertFalse(outcome)
        self.assertLessEqual(now[0] - sum(naps[:4]), 3, "never sleeps past the timeout")

    def test_registered_waits_for_a_new_lane_after_a_done_one_of_the_same_id(self) -> None:
        done_before = {"id": "r", "status": "done", "created": "2026-11-01T00:00:00Z"}
        self.assertFalse(wait.reached(done_before, "registered", done_before))
        self.assertTrue(wait.reached(dict(done_before, created="2026-11-16T00:00:00Z"), "registered", done_before))
        self.assertTrue(wait.reached(done_before, "registered", None), "registered, then done while waiting")
        self.assertTrue(wait.reached(done_before, "done", done_before))
        self.assertIsNone(wait.reached(done_before, "landed", done_before))


class NextCommandsTests(LaneCase):
    def test_start_and_adopt_print_the_lanes_next_commands(self):
        output = self.start("a", "lib/**")
        receipt = self.state / "a.receipt.json"
        self.assertIn("lanefather check a", output)
        self.assertIn("Hand back: the block in docs/agents/orchestration.md#implementer-brief, with "
                      f"Receipt: {receipt}", output)
        self.assertIn("lanefather wait a --for landed", output)
        worktree = self.tmp / "agent-a"
        self.git(self.repo, "worktree", "add", "-q", "-b", "lane/a", str(worktree), "integrate/q")
        output = self.lane("adopt", "a", str(worktree), "lane/a")
        self.assertIn("Next, in the worktree:", output)
        self.assertIn("lanefather check a", output)
        self.assertIn(f"Receipt: {receipt}", output)


if __name__ == "__main__":
    unittest.main()
