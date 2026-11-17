"""The thin CLI (fathers#80): a handler for every command, `--json` optional where a command prints state, every
tunable defined once in defaults.py, and the duration and time formats in text.py."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
from typing import Any, Dict, List, cast
import unittest
import unittest.mock

from support import ROOT, Case

from junkfather import changelog, cli, defaults, text
from junkfather.state import Store


class DispatchTests(unittest.TestCase):
    def test_every_command_has_a_handler(self):
        actions: List[argparse.Action] = vars(cli.parser())["_actions"]  # argparse has no public list of them
        choices = next(cast(Dict[str, Any], a.choices) for a in actions if a.nargs == argparse.PARSER)
        self.assertEqual(sorted(choices), sorted(cli.COMMANDS))


class WithoutJsonTests(Case):
    def test_status_capabilities_and_changelog_print_for_people_without_json(self):
        for command, expect in (("status", "0 running, 0 unowned, 0 unowned resources; last sweep never"),
                                ("capabilities", "snapshot `junkfather status --json` every 5 s"),
                                ("changelog", "Unreleased\n  ")):
            result = self.cli(command)
            self.assertEqual((0, ""), (result.returncode, result.stderr), command)
            self.assertIn(expect, result.stdout, command)
            with self.assertRaises(ValueError, msg=command):
                json.loads(result.stdout)
            self.assertIsInstance(json.loads(self.cli(command, "--json").stdout), dict, command)

    def test_status_text_lists_entries_unowned_items_and_the_last_sweep(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        self.assertEqual(0, self.cli("adopt", str(child.pid), "--ttl", "1h", "--label", "web").returncode)
        (entry,) = self.store.entries()
        (self.state / "unowned.json").write_text(json.dumps({"v": 1, "items": [
            {"pid": 4242, "start": "Thu Nov 12 10:00:00 2026", "cmd": "node server.js", "why": "working directory in "
             "/tmp/claude-1/"}], "resources": [{"kind": "docker", "id": "a" * 64, "name": "db",
                                                "why": "a running docker no entry owns"}]}))
        (self.state / "sweep.json").write_text(json.dumps({"v": 1, "at": 1794528000.25, "result": {}}))
        lines = self.cli("status").stdout.splitlines()
        self.assertTrue(lines[0].startswith("ID       STATE"), lines)
        self.assertIn(entry["id"], lines[1])
        self.assertIn("[web] sleep 600", lines[1])
        self.assertEqual(["unowned pid 4242 (working directory in /tmp/claude-1/): node server.js",
                          "unowned docker aaaaaaaaaaaa (db): a running docker no entry owns",
                          "1 running, 1 unowned, 1 unowned resources; last sweep 2026-11-13T00:00:00.250Z"],
                         lines[2:])

    def test_capabilities_text_is_one_line(self):
        lines = self.cli("capabilities").stdout.splitlines()
        self.assertEqual(1, len(lines))
        self.assertRegex(lines[0], r"^junkfather \d+\.\d+\.\d+ \(checkout\): snapshot `junkfather status "
                                   r"--json` every 5 s; events .*/events\.jsonl; actions reap, extend; changelog "
                                   r".*CHANGELOG\.md$")

    def test_changelog_text_groups_entries_by_release(self):
        found = [{"version": None, "unreleased": True, "date": None, "section": "Added", "text": "a"},
                 {"version": None, "unreleased": True, "date": None, "section": "Fixed", "text": "b"},
                 {"version": "0.1.0", "date": "2026-11-12", "section": "Added", "text": "c"}]
        self.assertEqual("Unreleased\n  Added: a\n  Fixed: b\n\n0.1.0 - 2026-11-12\n  Added: c",
                         changelog.text(found))
        self.assertEqual("", changelog.text([]))

    def test_a_bad_since_is_a_usage_error_either_way(self):
        for extra in ((), ("--json",)):
            result = self.cli("changelog", "--since", "nope", *extra)
            self.assertEqual((2, "junkfather: --since takes a version like 0.1.0, not 'nope'\n"
                                 "Try 'junkfather changelog --help'.\n"),
                             (result.returncode, result.stderr))


class ConventionTests(Case):
    """docs/cli.md beyond what common/cliconvention.py can check without knowing junkfather (fathers#153)."""

    def test_no_command_is_a_usage_error_with_the_help_pointer(self):
        result = self.cli()
        self.assertEqual((2, "", "junkfather: no command given\nTry 'junkfather --help'.\n"),
                         (result.returncode, result.stdout, result.stderr))

    def test_an_unknown_command_is_named(self):
        result = self.cli("statsu")
        self.assertEqual((2, "junkfather: unknown command 'statsu'\nTry 'junkfather --help'.\n"),
                         (result.returncode, result.stderr))

    def test_a_bad_value_names_the_option_and_points_to_the_command_help(self):
        result = self.cli("extend", "j1", "--ttl", "2w")
        self.assertEqual(2, result.returncode)
        self.assertRegex(result.stderr, r"^junkfather: --ttl: .*'2w'.*\nTry 'junkfather extend --help'\.\n$")
        result = self.cli("extend", "j1", "--ttl", "--unknown")
        self.assertEqual((2, "junkfather: unknown option --unknown\nTry 'junkfather extend --help'.\n"),
                         (result.returncode, result.stderr))
        result = self.cli("reap")
        self.assertEqual("junkfather: reap takes one of --id, --owner, --lane or --pid\n"
                         "Try 'junkfather reap --help'.\n", result.stderr)

    def test_the_help_names_every_command_but_the_internal_ones(self):
        shown = self.cli("--help").stdout
        self.assertNotIn("SUPPRESS", shown)
        self.assertNotIn("supervise", shown)
        self.assertNotRegex(shown, r"\n    background\b", "the line the hook writes before a background command")
        for command in [c for c in cli.COMMANDS if c not in ("supervise", "background")]:
            self.assertRegex(shown, rf"\n    {re.escape(command)}\b", command)

    def test_version_flag_and_command_agree(self):
        flag, command = self.cli("--version"), self.cli("version")
        self.assertEqual((0, ""), (flag.returncode, flag.stderr))
        self.assertRegex(flag.stdout, r"^junkfather \d+\.\d+\.\d+ \(checkout\)\n$")
        self.assertEqual(flag.stdout, command.stdout)

    def test_hand_and_extend_tell_a_missing_entry_from_a_finished_one(self):
        for argv in (("hand", "jnosuch", "--to", "s2"), ("extend", "jnosuch", "--ttl", "1h")):
            result = self.cli(*argv)
            self.assertEqual((6, "", "junkfather: no entry jnosuch\n"),
                             (result.returncode, result.stdout, result.stderr), argv)
        self.assertEqual(0, self.cli("own", "--ttl", "1m", "--quiet", "--", "true").returncode)
        (entry,) = self.store.entries()
        for argv in (("hand", entry["id"], "--to", "s2"), ("extend", entry["id"], "--ttl", "1h")):
            result = self.cli(*argv)
            self.assertEqual((3, f"junkfather: {entry['id']} is not running (exited)\n"),
                             (result.returncode, result.stderr), argv)

    def test_a_blank_stop_or_alive_command_is_refused_at_registration(self):
        for alive, stop in (("true", " "), (" ", "true")):
            result = self.cli("own-resource", "--kind", "command", "--id", "x", "--alive", alive, "--stop", stop)
            self.assertEqual((2, "junkfather: --kind command needs --alive CMD and --stop CMD, each naming a command\n"
                                 "Try 'junkfather own-resource --help'.\n"), (result.returncode, result.stderr))
        self.assertEqual([], self.store.entries())

    def test_reap_of_a_missing_entry_is_not_found(self):
        result = self.cli("reap", "--id", "jnosuch")
        self.assertEqual((6, "", "junkfather: jnosuch: unknown (no such entry; pids -)\n"),
                         (result.returncode, result.stdout, result.stderr))
        both = self.cli("reap", "--id", "jnosuch", "--id", "jother", "--json")
        self.assertEqual((6, "junkfather: jnosuch: unknown (no such entry; pids -) (and 1 more)\n"),
                         (both.returncode, both.stderr))
        self.assertEqual(["jnosuch", "jother"], [r["id"] for r in json.loads(both.stdout)["results"]])

    def test_an_unexpected_exception_is_one_line_and_its_traceback_goes_to_the_log(self):
        def broken(store: Store, args: argparse.Namespace) -> int:
            raise RuntimeError("boom")

        err = io.StringIO()
        with unittest.mock.patch.dict(cli.COMMANDS, {"status": broken}), contextlib.redirect_stderr(err):
            code = cli.main(["status"])
        self.assertEqual((1, "junkfather: internal error: RuntimeError: boom\n"), (code, err.getvalue()))
        logged = (self.state / "logs" / "junkfather.log").read_text()
        self.assertIn("Traceback (most recent call last)", logged)
        self.assertIn("RuntimeError: boom", logged)


class DefaultsTests(unittest.TestCase):
    def test_each_tunable_is_defined_in_defaults_only(self):
        names = [n for n in vars(defaults) if n.isupper()]
        assigned = re.compile(rf"^({'|'.join(names)})\s*=\s*[\d(]", re.MULTILINE)
        found = [f"{path.name}: {m[0]}" for path in sorted((ROOT / "junkfather").glob("*.py"))
                 if path.name != "defaults.py" for m in assigned.finditer(path.read_text())]
        self.assertEqual([], found)

    def test_no_second_literal_ttl_or_grace(self):
        literal = re.compile(r"(?<![\w.])(7200(\.0)?|10\.0)(?![\w.])")
        found = [f"{path.name}:{number}" for path in sorted((ROOT / "junkfather").glob("*.py"))
                 if path.name != "defaults.py"
                 for number, line in enumerate(path.read_text().splitlines(), 1) if literal.search(line)]
        self.assertEqual([], found)


class TextTests(unittest.TestCase):
    def test_durations_in_and_out(self):
        self.assertEqual([90.0, 1800.0, 7200.0, 86400.0, 5.0],
                         [text.seconds(t) for t in ("90s", "30m", "2h", "1d", "5")])
        for bad in ("0", "2w", "-1s", ""):
            with self.assertRaises(argparse.ArgumentTypeError):
                text.seconds(bad)
        self.assertEqual(["0s", "119s", "2m", "119m", "2h", "2h05m", "2d0h"],
                         [text.duration(s) for s in (-5, 119, 120, 7140, 7200, 7500, 172800)])

    def test_iso_is_rfc3339_utc_with_milliseconds(self):
        self.assertEqual("2026-11-13T00:00:00.250Z", text.iso(1794528000.25))
        self.assertIsNone(text.iso("1794528000"))
        self.assertIsNone(text.iso(None))


if __name__ == "__main__":
    unittest.main()
