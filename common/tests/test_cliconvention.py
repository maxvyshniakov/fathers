"""common/cliconvention.py against two stand-in tools: one that follows docs/cli.md and one that breaks each rule.

Run from the repository root: python3 -m unittest discover -s common/tests
"""
from __future__ import annotations

import atexit
from pathlib import Path
import shutil
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cliconvention  # common/cliconvention.py

# A tool that follows the convention: `good status [--json] [--since V]`, `good run [--] CMD...`.
GOOD = '''
import json, sys

def fail(reason, command=None):
    print("good: " + reason, file=sys.stderr)
    print("Try 'good%s --help'." % (" " + command if command else ""), file=sys.stderr)
    return 2

def main(argv):
    if argv in (["--help"], ["-h"]):
        print("usage: good status [--json] [--since V]\\n       good run [--] COMMAND...")
        return 0
    if argv == ["--version"]:
        print("good 1.2.3 (from a checkout)")
        return 0
    if not argv:
        return fail("a command is required")
    command, rest = argv[0], argv[1:]
    if command not in ("status", "run"):
        return fail("unknown command %r" % command)
    if rest == ["--help"]:
        print("usage: good %s ..." % command)
        return 0
    since, as_json, index = None, False, 0
    while index < len(rest):
        word = rest[index]
        if word == "--json" and command == "status":
            as_json = True
        elif word == "--since" and index + 1 < len(rest):
            since, index = rest[index + 1], index + 1
        elif word.startswith("--since="):
            since = word.split("=", 1)[1]
        elif word == "--":
            break
        elif word.startswith("-"):
            return fail("unknown option %s for %s" % (word, command), command)
        index += 1
    if as_json:
        print(json.dumps({"v": 1, "since": since}))
    elif command == "status":
        print("since: %s" % since)
    return 0

sys.exit(main(sys.argv[1:]))
'''

# GOOD with two faults a quick look misses: `--since=V` is accepted but its value dropped, and an unknown option's
# pointer names the tool's help instead of the command's.
SLOPPY = GOOD.replace('since = word.split("=", 1)[1]', 'pass').replace(
    'return fail("unknown option %s for %s" % (word, command), command)',
    'return fail("unknown option %s for %s" % (word, command))')

# A tool in the style the convention replaces: the whole usage on every error, no --version, no `<cmd> --help`,
# `--json` required and its reply without `v`, and `--since=V` refused.
BAD = '''
import json, sys

USAGE = "usage: bad status [--json] [--since V]\\n       bad run COMMAND...\\n\\nState: somewhere"

def main(argv):
    if argv == ["--help"]:
        print("usage: bad status")
        return 0
    if not argv or argv[0] not in ("status", "run") or "--help" in argv or any(w.startswith("--since=") for w in argv):
        print(USAGE, file=sys.stderr)
        return 2
    if any(w.startswith("-") and w not in ("--json", "--since") for w in argv[1:]):
        print(USAGE, file=sys.stderr)
        return 2
    if argv[0] == "status" and "--json" not in argv:
        print("bad: status takes --json", file=sys.stderr)
        return 2
    if argv[0] == "status" and "--json" in argv:
        print(json.dumps({"version": 1}))
        print("trailing text")
        return 0
    return 0

sys.exit(main(sys.argv[1:]))
'''

COMMANDS = (cliconvention.Command("status", json=(), option=("--since", "1.0.0"), shows="since: 1.0.0"),
            cliconvention.Command("run"))


# Not a module cleanup: the suites these tests run inside a test would end the module and run it early.
SCRATCH = tempfile.mkdtemp()
atexit.register(shutil.rmtree, SCRATCH, True)


def tool(source: str, name: str) -> cliconvention.Tool:
    directory = Path(tempfile.mkdtemp(dir=SCRATCH))
    script = directory / name
    script.write_text(textwrap.dedent(source))
    return cliconvention.Tool(name, (sys.executable, str(script)), COMMANDS, cwd=str(directory))


class Cases(unittest.TestCase):
    def test_every_case_has_a_stable_id(self) -> None:
        ids = [case.id for case in cliconvention.cases(tool(GOOD, "good"))]
        self.assertEqual(["version", "help", "help -h", "help lists commands", "unknown command", "unknown option",
                          "status --help", "status unknown option", "status --json", "status without --json",
                          "status --opt=value",
                          "run --help", "run unknown option"], ids)
        self.assertEqual(len(ids), len(set(ids)))


class Conforming(unittest.TestCase):
    def test_a_tool_that_follows_the_convention_passes_every_case(self) -> None:
        good = tool(GOOD, "good")
        found = {case.id: cliconvention.problems(good, case) for case in cliconvention.cases(good)}
        self.assertEqual({key: [] for key in found}, found)


class Breaking(unittest.TestCase):
    bad: cliconvention.Tool
    found: dict[str, list[str]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.bad = tool(BAD, "bad")
        cls.found = {case.id: cliconvention.problems(cls.bad, case) for case in cliconvention.cases(cls.bad)}

    def assertProblem(self, case: str, fragment: str) -> None:
        self.assertTrue(any(fragment in problem for problem in self.found[case]),
                        f"{case}: no problem mentions {fragment!r}: {self.found[case]}")

    def test_a_usage_error_must_be_one_reason_line_and_a_pointer_to_help(self) -> None:
        self.assertProblem("unknown command", "first stderr line is not 'bad: <reason>'")
        self.assertProblem("unknown command", "one line pointing to 'bad --help'")
        self.assertProblem("status unknown option", "first stderr line is not 'bad: <reason>'")

    def test_version_and_command_help_are_required(self) -> None:
        self.assertProblem("version", "exit 2, not 0")
        self.assertProblem("help -h", "exit 2, not 0")
        self.assertProblem("status --help", "exit 2, not 0")
        self.assertProblem("run --help", "exit 2, not 0")

    def test_the_help_names_every_command(self) -> None:
        self.assertEqual(["the help does not name: run"], self.found["help lists commands"])

    def test_a_json_reply_is_one_object(self) -> None:
        self.assertProblem("status --json", "not one JSON object")

    def test_json_is_optional(self) -> None:
        self.assertProblem("status without --json", "exit 2, not 0")

    def test_opt_equals_value_is_accepted_like_opt_space_value(self) -> None:
        self.assertProblem("status --opt=value", "--since=1.0.0")

    def test_what_the_tool_does_right_passes(self) -> None:
        self.assertEqual([], self.found["help"])


class Sloppy(unittest.TestCase):
    """Faults that only show in what the tool prints, not in its exit status."""

    found: dict[str, list[str]]

    @classmethod
    def setUpClass(cls) -> None:
        sloppy = tool(SLOPPY, "good")
        cls.found = {case.id: cliconvention.problems(sloppy, case) for case in cliconvention.cases(sloppy)}

    def test_an_opt_equals_value_whose_value_is_dropped_is_found(self) -> None:
        self.assertEqual(["'--since=1.0.0' does not print 'since: 1.0.0'"], self.found["status --opt=value"])

    def test_the_pointer_must_name_the_command(self) -> None:
        for command in ("status", "run"):
            self.assertEqual([f"the pointer is not to 'good {command} --help': \"Try 'good --help'.\""],
                             self.found[f"{command} unknown option"])

    def test_the_rest_passes(self) -> None:
        self.assertEqual([], [case for case, found in self.found.items()
                              if found and not case.endswith(("--opt=value", "unknown option"))])


class OptionEquals(unittest.TestCase):
    def test_without_shows_both_spellings_must_print_the_same(self) -> None:
        source = GOOD.replace('since = word.split("=", 1)[1]', 'since = "other"')
        sloppy = cliconvention.Tool("good", tool(source, "good").argv,
                                    (cliconvention.Command("status", option=("--since", "1.0.0")),))
        case = next(case for case in cliconvention.cases(sloppy) if case.id == "status --opt=value")
        self.assertEqual(["'--since=1.0.0' prints other than '--since 1.0.0'"], cliconvention.problems(sloppy, case))


class JsonReply(unittest.TestCase):
    def problems(self, stdout: str) -> list[str]:
        return cliconvention.json_check(tool(GOOD, "good"), cliconvention.Run(0, stdout, ""))

    def test_an_object_with_an_integer_v_passes(self) -> None:
        self.assertEqual([], self.problems('{"v": 2, "rows": []}\n'))

    def test_v_must_be_present_and_an_integer(self) -> None:
        for reply in ('{"version": 1}', '{"v": "1"}', '{"v": true}', '{"v": 1.0}'):
            with self.subTest(reply=reply):
                self.assertEqual(1, len(self.problems(reply)))
                self.assertIn("no integer 'v'", self.problems(reply)[0])

    def test_a_reply_that_is_not_an_object_fails(self) -> None:
        self.assertEqual(["stdout is not one JSON object"], self.problems("[1]"))


class JsonlReply(unittest.TestCase):
    def problems(self, stdout: str) -> list[str]:
        return cliconvention.jsonl_check(tool(GOOD, "good"), cliconvention.Run(0, stdout, ""))

    def test_empty_and_multi_line_objects_with_integer_v_pass(self) -> None:
        self.assertEqual([], self.problems(""))
        self.assertEqual([], self.problems('{"v": 1, "kind": "a"}\n{"v": 2, "kind": "b"}\n'))
        self.assertEqual([], self.problems('{"v": 1, "text": "a\u2028b\u2029c"}\n'))

    def test_invalid_line_or_missing_v_fails(self) -> None:
        self.assertIn("line 2 is not a JSON object", self.problems('{"v": 1}\nnot-json\n')[0])
        self.assertIn("line 1 is not a JSON object", self.problems("[1]\n")[0])
        self.assertIn("line 2 has no integer 'v'", self.problems('{"v": 1}\n{"version": 1}\n')[0])
        self.assertIn("line 1 has no integer 'v'", self.problems('{"v": true}\n')[0])

    def test_command_with_jsonl_uses_jsonl_check(self) -> None:
        stream = cliconvention.Tool("good", tool(GOOD, "good").argv,
                                    (cliconvention.Command("status", json=(), jsonl=True),))
        case = next(case for case in cliconvention.cases(stream) if case.id == "status --json")
        self.assertIs(case.check, cliconvention.jsonl_check)


class ErrorLine(unittest.TestCase):
    def test_the_reason_must_name_the_refused_word(self) -> None:
        problems = cliconvention.error_problems("t", "t: bad input\nTry 't --help'.\n", word="--x")
        self.assertEqual(["the reason does not name '--x': 't: bad input'"], problems)

    def test_a_traceback_is_never_the_error(self) -> None:
        stderr = "Traceback (most recent call last):\n  File \"x\", line 1\nKeyError: 'a'\n"
        self.assertIn("a traceback on stderr", cliconvention.error_problems("t", stderr))

    def test_the_reason_is_followed_by_exactly_one_pointer(self) -> None:
        self.assertEqual(["after the reason, one line pointing to 't --help', not 0 lines"],
                         cliconvention.error_problems("t", "t: unknown option --x\n"))
        self.assertEqual([], cliconvention.error_problems("t", "t: unknown option --x\nTry 't --help'.\n"))

    def test_a_known_command_gets_its_own_help_in_the_pointer(self) -> None:
        stderr = "t: unknown option --x for run\nTry 't --help'.\n"
        self.assertEqual(["the pointer is not to 't run --help': \"Try 't --help'.\""],
                         cliconvention.error_problems("t", stderr, command="run"))
        self.assertEqual([], cliconvention.error_problems("t", "t: --x\nTry 't run --help'.\n", command="run"))


class Expected(unittest.TestCase):
    def run_suite(self, case: type) -> unittest.TestResult:
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
        return result

    def test_listed_failures_are_expected_and_the_rest_must_pass(self) -> None:
        bad = tool(BAD, "bad")
        failing = [case.id for case in cliconvention.cases(bad) if cliconvention.problems(bad, case)]
        listed = {case: "fathers#999" for case in failing}
        result = self.run_suite(cliconvention.conformance(bad, expected=listed))
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual(len(failing), len(result.expectedFailures))

    def test_a_listed_failure_that_passes_fails_the_run(self) -> None:
        good = tool(GOOD, "good")
        result = self.run_suite(cliconvention.conformance(good, expected={"version": "fathers#999"}))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(1, len(result.unexpectedSuccesses))

    def test_an_unlisted_failure_fails_with_its_problems(self) -> None:
        result = self.run_suite(cliconvention.conformance(tool(BAD, "bad")))
        self.assertFalse(result.wasSuccessful())
        messages = "\n".join(text for _, text in result.failures)
        self.assertIn("docs/cli.md", messages)

    def test_an_expected_failure_must_name_a_case_and_a_ticket(self) -> None:
        good = tool(GOOD, "good")
        with self.assertRaisesRegex(ValueError, "no case 'versoin'"):
            cliconvention.conformance(good, expected={"versoin": "fathers#1"})
        with self.assertRaisesRegex(ValueError, "names no ticket"):
            cliconvention.conformance(good, expected={"version": ""})

    def test_prepare_gives_each_test_its_environment(self) -> None:
        good = tool(GOOD, "good")
        seen: list[str] = []

        class Prepared(cliconvention.conformance(good)):
            def prepare(self, tool: cliconvention.Tool) -> cliconvention.Tool:
                seen.append(self.id())
                return tool

        result = self.run_suite(Prepared)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(cliconvention.cases(good)), len(seen))

    def test_the_test_names_say_what_they_check(self) -> None:
        names = unittest.defaultTestLoader.getTestCaseNames(cliconvention.conformance(tool(GOOD, "good")))
        self.assertIn("test_status_opt_value", names)
        self.assertIn("test_unknown_command", names)


if __name__ == "__main__":
    unittest.main()
