"""Check a tool's command line against the CLI convention (docs/cli.md, fathers#118).

A test describes the tool (its command name, how to run it, the commands it has) and gets one unittest case per rule:

    TOOL = Tool("junkfather", (sys.executable, "-m", "junkfather"),
                (Command("list", json=()), Command("changelog", option=("--since", "0.1.0"), shows="0.2.0"),
                 Command("reap")))

    class CLIConvention(conformance(TOOL, expected={"version": "fathers#NNN"})):
        def prepare(self, tool: Tool) -> Tool:
            state = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, state, True)
            return dataclasses.replace(tool, env={**os.environ, "JUNKFATHER_STATE": state}, cwd=state)

`expected` lists the cases the tool does not pass yet, each with the ticket that fixes it; a listed case that passes
fails the run (an unexpected success), so the list only shrinks. Assign the class `conformance` returns only as a
base, as above: a module-level name for it would be collected and run a second time.

Only the standard library; Python 3.9 and later, like the tools it checks.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
import re
import subprocess
from typing import Callable
import unittest

# The two codes of docs/cli.md's table these cases can provoke without knowing the tool.
OK, USAGE = 0, 2

UNKNOWN_COMMAND = "no-such-command"
UNKNOWN_OPTION = "--no-such-option"
VERSION_LINE = r"\S+ v?\d+\.\d+(\.\d+)?\S*( .*)?"


@dataclass(frozen=True)
class Command:
    """One command of a tool. `json`: the arguments after the command that make it print state, `--json` appended
    (`()` for the command alone; None for a command that prints no state); the same arguments without `--json` must
    work too. `option`: a long option that takes a value and a value that is valid in the test's environment, for
    `--opt=value`; `shows`: text its stdout holds only when the value took effect. Without `shows`, `--opt=value` and
    `--opt value` must print the same."""

    name: str
    json: Sequence[str] | None = None
    option: tuple[str, str] | None = None
    shows: str | None = None


@dataclass(frozen=True)
class Tool:
    """`name` is the command name the error line starts with; `argv` runs the tool; `env` is the whole environment
    (None: this process's, with NO_COLOR=1)."""

    name: str
    argv: Sequence[str]
    commands: Sequence[Command] = ()
    env: Mapping[str, str] | None = None
    cwd: str | None = None
    timeout: float = 60.0


@dataclass(frozen=True)
class Run:
    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Case:
    id: str
    argv: tuple[str, ...]
    check: Callable[[Tool, Run], list[str]]
    again: tuple[str, ...] | None = None  # a second command line that must print what the first does
    shows: str | None = None  # text both command lines must print instead


def run(tool: Tool, argv: Sequence[str]) -> Run:
    environment = dict(os.environ if tool.env is None else tool.env)
    environment.setdefault("NO_COLOR", "1")
    done = subprocess.run([*tool.argv, *argv], cwd=tool.cwd, env=environment, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=tool.timeout, check=False)
    return Run(done.returncode, done.stdout, done.stderr)


def error_problems(name: str, stderr: str, word: str | None = None, command: str | None = None) -> list[str]:
    """What is wrong with a usage error's stderr: `<name>: <reason>` on the first line (naming `word` when given) and
    exactly one more line that points to `<name> [command] --help`."""
    lines = stderr.splitlines()
    if not lines:
        return ["nothing on stderr: a refusal says why in one line"]
    problems: list[str] = []
    if any(line.startswith("Traceback (most recent call last)") for line in lines):
        problems.append("a traceback on stderr")
    first = lines[0]
    if not first.startswith(f"{name}: ") or not first[len(name) + 2:].strip():
        problems.append(f"the first stderr line is not '{name}: <reason>': {first!r}")
    elif word is not None and word not in first:
        problems.append(f"the reason does not name {word!r}: {first!r}")
    pointer = f"{name} {command} --help" if command else f"{name} --help"
    rest = lines[1:]
    if len(rest) != 1:
        problems.append(f"after the reason, one line pointing to '{pointer}', not {len(rest)} lines")
    elif pointer not in rest[0]:
        problems.append(f"the pointer is not to '{pointer}': {rest[0]!r}")
    return problems


def exits(done: Run, wanted: int) -> list[str]:
    return [] if done.code == wanted else [f"exit {done.code}, not {wanted}"]


def help_check(word: str | None) -> Callable[[Tool, Run], list[str]]:
    def check(tool: Tool, done: Run) -> list[str]:
        problems = exits(done, OK)
        if not done.stdout.strip():
            problems.append("the help is not on stdout")
        elif word is not None and word not in done.stdout:
            problems.append(f"the help does not name {word!r}")
        if done.stderr.strip():
            problems.append(f"stderr is not empty: {done.stderr.splitlines()[0]!r}")
        return problems
    return check


def version_check(tool: Tool, done: Run) -> list[str]:
    problems = exits(done, OK)
    first = done.stdout.splitlines()[0] if done.stdout.strip() else ""
    if not first.startswith(f"{tool.name} ") or not re.fullmatch(VERSION_LINE, first):
        problems.append(f"stdout does not start with '{tool.name} <version>': {first!r}")
    return problems


def lists_commands(tool: Tool, done: Run) -> list[str]:
    if done.code != OK:
        return [f"exit {done.code}, not 0"]
    missing = [command.name for command in tool.commands
               if not re.search(rf"(?<![\w-]){re.escape(command.name)}(?![\w-])", done.stdout)]
    return [f"the help does not name: {', '.join(missing)}"] if missing else []


def usage_check(word: str, command: str | None = None) -> Callable[[Tool, Run], list[str]]:
    def check(tool: Tool, done: Run) -> list[str]:
        problems = exits(done, USAGE)
        if done.stdout.strip():
            problems.append("a usage error printed on stdout")
        return problems + error_problems(tool.name, done.stderr, word=word, command=command)
    return check


def plain_check(tool: Tool, done: Run) -> list[str]:
    problems = exits(done, OK)
    if done.code == USAGE and done.stderr.strip():
        problems.append(f"refused without --json: {done.stderr.splitlines()[0]!r}")
    return problems


def json_check(tool: Tool, done: Run) -> list[str]:
    problems = exits(done, OK)
    try:
        reply: object = json.loads(done.stdout)
    except ValueError:
        return [*problems, f"stdout is not one JSON object: {done.stdout[:80]!r}"]
    if not isinstance(reply, dict):
        return [*problems, "stdout is not one JSON object"]
    version = reply.get("v")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if isinstance(version, bool) or not isinstance(version, int):
        problems.append(f"the reply has no integer 'v' (it has {sorted(reply)[:8]})")  # pyright: ignore[reportUnknownArgumentType]
    return problems


def equals_check(option: str, value: str) -> Callable[[Tool, Run], list[str]]:
    def check(tool: Tool, done: Run) -> list[str]:
        if done.code == USAGE:
            line = done.stderr.splitlines()[0] if done.stderr.strip() else ""
            return [f"{option}={value} is refused (exit 2): {line!r}"]
        return []
    return check


def cases(tool: Tool) -> list[Case]:
    """The cases docs/cli.md asks of `tool`, in a fixed order with stable ids."""
    found = [
        Case("version", ("--version",), version_check),
        Case("help", ("--help",), help_check(None)),
        Case("help -h", ("-h",), help_check(None)),
        Case("help lists commands", ("--help",), lists_commands),
        Case("unknown command", (UNKNOWN_COMMAND,), usage_check(UNKNOWN_COMMAND)),
        Case("unknown option", (UNKNOWN_OPTION,), usage_check(UNKNOWN_OPTION)),
    ]
    for command in tool.commands:
        words = tuple(command.name.split())
        found.append(Case(f"{command.name} --help", (*words, "--help"), help_check(command.name)))
        found.append(Case(f"{command.name} unknown option", (*words, UNKNOWN_OPTION),
                          usage_check(UNKNOWN_OPTION, command.name)))
        if command.json is not None:
            found.append(Case(f"{command.name} --json", (*words, *command.json, "--json"), json_check))
            found.append(Case(f"{command.name} without --json", (*words, *command.json), plain_check))
        if command.option is not None:
            option, value = command.option
            found.append(Case(f"{command.name} --opt=value", (*words, f"{option}={value}"), equals_check(option, value),
                              again=(*words, option, value), shows=command.shows))
    return found


def problems(tool: Tool, case: Case) -> list[str]:
    """What `tool` does against `case`'s rule; empty when it follows it."""
    done = run(tool, case.argv)
    found = case.check(tool, done)
    if case.again is None or found:
        return found
    equals, spaced = case.argv[-1], " ".join(case.again[-2:])
    other = run(tool, case.again)
    if case.shows is None:
        return [] if (other.code, other.stdout) == (done.code, done.stdout) else [
            f"'{equals}' prints other than '{spaced}'"]
    return [f"'{argv}' does not print {case.shows!r}"
            for argv, printed in ((equals, done.stdout), (spaced, other.stdout)) if case.shows not in printed]


def method_name(case_id: str) -> str:
    return "test_" + re.sub(r"[^0-9a-z]+", "_", case_id.lower()).strip("_")


class Conformance(unittest.TestCase):
    """The base of the class `conformance` builds; override `prepare` to give each case its environment."""

    TOOL: Tool

    def prepare(self, tool: Tool) -> Tool:
        return tool

    def conform(self, case: Case) -> None:
        found = problems(self.prepare(self.TOOL), case)
        if found:
            self.fail(f"{self.TOOL.name} {' '.join(case.argv)}: " + "; ".join(found) + " (docs/cli.md)")


def conformance(tool: Tool, expected: Mapping[str, str] | None = None) -> type[Conformance]:
    """A TestCase class with one test per case of `tool`; `expected` maps each case it fails today to its ticket."""
    listed: dict[str, str] = dict(expected or {})
    found = cases(tool)
    ids = {case.id for case in found}
    for case_id, ticket in listed.items():
        if case_id not in ids:
            raise ValueError(f"expected failure: no case {case_id!r}; the cases are {sorted(ids)}")
        if not ticket.strip():
            raise ValueError(f"expected failure {case_id!r} names no ticket")
    namespace: dict[str, object] = {"TOOL": tool, "__doc__": f"{tool.name} against docs/cli.md."}
    for case in found:
        def test(self: Conformance, case: Case = case) -> None:
            self.conform(case)
        test.__doc__ = case.id + (f" (expected to fail until {listed[case.id]})" if case.id in listed else "")
        namespace[method_name(case.id)] = unittest.expectedFailure(test) if case.id in listed else test
    return type(f"{tool.name.capitalize()}Conformance", (Conformance,), namespace)
