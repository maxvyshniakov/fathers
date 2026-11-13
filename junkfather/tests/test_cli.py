"""The thin CLI (fathers#80): a handler for every command, `--json` optional where a command prints state, every
tunable defined once in defaults.py, and the duration and time formats in text.py."""
from __future__ import annotations

import argparse
import json
import re
import unittest

from support import ROOT, Case

from junkfather import cli, defaults, text


class DispatchTests(unittest.TestCase):
    def test_every_command_has_a_handler(self):
        sub = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(sorted(sub.choices), sorted(cli.COMMANDS))

    def test_cli_stays_thin(self):
        self.assertLess(len((ROOT / "junkfather" / "cli.py").read_text().splitlines()), 350)


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

    def test_a_bad_since_is_a_usage_error_either_way(self):
        for extra in ((), ("--json",)):
            result = self.cli("changelog", "--since", "nope", *extra)
            self.assertEqual((2, "junkfather: --since takes a version like 0.1.0, not 'nope'\n"),
                             (result.returncode, result.stderr))


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
