"""The thin CLI (fathers#80): a handler for every command, `--json` optional where a command prints state, every
tunable defined once in defaults.py, and the duration and time formats in text.py."""
from __future__ import annotations

import argparse
import json
import re
import unittest

from support import ROOT, Case

from junkfather import changelog, cli, defaults, text


class DispatchTests(unittest.TestCase):
    def test_every_command_has_a_handler(self):
        sub = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(sorted(sub.choices), sorted(cli.COMMANDS))


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
