#!/usr/bin/env python3
"""Test hygiene (fathers#235): no fixed sleeps and no raw timing assertions in the repository's Python tests, and no
Git path list that Git may quote in any of its Python.

  python3 tools/hygiene.py     scan every Python file Git knows in the repository it runs in (tracked or untracked,
                               not ignored); exit 1 on a finding

  fixed-sleep    sleep(<n>), time.sleep(<n>) or asyncio.sleep(<n>) with a literal n of at least 0.2 s, and Playwright's
                 wait_for_timeout(...). A line, or the comment lines right above it, carrying `sleep-ok: <reason>`
                 is allowed. Wait for the condition instead: an event, or a poll with a deadline.
  timing-assert  an assertion bounding a measured duration from above (`time.monotonic() - started < 2`,
                 `self.assertLess(elapsed, 0.2)`). A budget goes through the perf helper (perf_budget), which
                 enforces only in the perf suite (perf/ and test_perf_*.py); a deadline that only bounds a wait passes
                 with a literal bound of at least 30 s, or with a `load-tolerant: <why, in three words or more>`
                 comment. A file outside the perf suite that calls perf_budget fails: its budgets never hold.
  git-quoted-paths
                 a call whose arguments (or a list or tuple among them) hold `--name-only` or `--name-status` but
                 neither `core.quotePath=false` nor `-z`, in any Python file. Without either, Git prints a path with
                 a non-ASCII or unusual byte quoted and escaped ("caf\\303\\251.txt"), and the caller reads a path
                 that does not exist. Pass `-c core.quotePath=false` before the subcommand, or `-z` and split on NUL.

The two test rules read lines with comments and string contents blanked, as Repo-Kit's `test-hygiene` did (this is
its port, for Python tests); its baseline allowed no finding, so none is allowed here. git-quoted-paths reads the
syntax tree, so a path list inside a string (a fixture script a test writes) is not a call.
"""
from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys

RULES = ("fixed-sleep", "timing-assert", "git-quoted-paths")
TESTS = ("test/**/*.py", "tests/**/*.py", "**/test_*.py", "**/*_test.py", "test-*.py", "{scripts,tools}/**/test-*.py")
EXCLUDE = ("**/node_modules/**", "**/dist/**", "**/fixtures/**")
PERF_SUITE = ("**/perf/**", "**/test_perf_*.py", "**/*.perf.test.*")
FLOOR_SECONDS = 30
NUMBER = r"(\d[\d_]*(?:\.\d+)?|\.\d+)"
SLEEP_OK = re.compile(r"sleep-ok:\s*\S+")
LOAD_TOLERANT = re.compile(r"load-tolerant:\s*\S+(?:\s+\S+){2,}")
COMMENT_LINE = re.compile(r"^\s*(?://|#|/?\*)")
SLEEPS = (
    (re.compile(rf"(?:\b(?:time|asyncio)\.|(?<![\w.]))sleep\s*\(\s*{NUMBER}\s*\)"), lambda s: s >= 0.2),
    (re.compile(r"\bwait_for_timeout\s*\("), lambda _: True),  # Playwright: a fixed wait whatever its argument
)
CLOCK = re.compile(r"\btime\.(?:monotonic|perf_counter|time|process_time|thread_time)(?:_ns)?\(\)|\bmonotonic\(\)"
                   r"|\bperf_counter\(\)")
ASSERTION = re.compile(r"\bassert\b|\bself\.assert\w+\s*\(")
HELPER = re.compile(r"\bperf_budget\s*\(")
DEFINITION = re.compile(r"\b(?:func|function|def|fn)\s+(?:perfBudget|perf_budget)\b"
                        r"|\bexport\s+(?:const|let)\s+perfBudget\b")
ASSIGNED = re.compile(r"\b(\w+)\s*(?::\s*[\w.<>\[\]]+\s*)?=(?![=>])\s*((?:[^,;=]|==)+)")
COLLECTED = re.compile(r"\b(\w+)\s*\.\s*(?:append|push|insert)\((.*)\)")
DIFFERENCE = re.compile(r"-|\.elapsed\(\)|\.timeIntervalSince\(|\.timeIntervalSinceNow\b")
DEADLINE = re.compile(rf"(?:{CLOCK.pattern})\s*\+\s*(.+)")
NAME = re.compile(r"^\s*(\w+)\b(?!\s*[(.\[])")
LITERAL = re.compile(rf"^\s*{NUMBER}(?![\w.(])")
BEFORE = re.compile(rf"(\w+|{NUMBER})\s*$")
PATH_LISTS = ("--name-only", "--name-status")
UNQUOTED = re.compile(r"^core\.quotepath=(?:false|no|off|0)$", re.IGNORECASE)
PYTHON_SHEBANG = re.compile(r"^#!.*\bpython")


@dataclass(frozen=True)
class Finding:
    rule: str
    file: str
    line: int
    text: str


def glob_pattern(glob: str) -> re.Pattern[str]:
    """A glob (`**`, `*`, `?`, `{a,b}`) as a regular expression over a repository-relative path."""
    out = ""
    index = 0
    while index < len(glob):
        char = glob[index]
        if char == "*" and glob[index + 1:index + 2] == "*":
            if glob[index + 2:index + 3] == "/":  # `**/` matches no directory or any number of them
                out += "(?:.*/)?"
                index += 2
            else:
                out += ".*"
                index += 1
        elif char == "*":
            out += "[^/]*"
        elif char == "?":
            out += "[^/]"
        elif char == "{":
            end = glob.index("}", index)
            out += "(?:" + "|".join(re.escape(part) for part in glob[index + 1:end].split(",")) + ")"
            index = end
        else:
            out += re.escape(char)
        index += 1
    return re.compile(f"^{out}$")


def matcher(globs: Iterable[str]):
    patterns = [glob_pattern(glob) for glob in globs]
    return lambda path: any(pattern.match(path) for pattern in patterns)


def code(line: str) -> str:
    """The line without its comment and with string literal contents blanked: neither asserts nor waits."""
    kept = ""
    quote = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
                kept += char
            index += 1
            continue
        if char == "#":
            break
        if char in "\"'":
            quote = char
        kept += char
        index += 1
    return kept


def marked(raw: Sequence[str], index: int, marker: re.Pattern[str]) -> bool:
    """Whether the marker is on the line or in the comment lines right above it."""
    if marker.search(raw[index]):
        return True
    above = index - 1
    while above >= 0 and COMMENT_LINE.match(raw[above]):
        if marker.search(raw[above]):
            return True
        above -= 1
    return False


def number(text: str) -> float:
    return float(text.replace("_", ""))


def fixed_sleeps(text: str) -> list[tuple[int, str]]:
    raw = text.split("\n")
    found: list[tuple[int, str]] = []
    for index, line in enumerate(raw):
        stripped = code(line)
        for pattern, counts in SLEEPS:
            match = pattern.search(stripped)
            if not match or not counts(number(match[1]) if pattern.groups else None):
                continue
            if not marked(raw, index, SLEEP_OK):
                found.append((index + 1, line.strip()))
            break
    return found


def seconds(text: str, deadlines: dict[str, float | None] | None = None) -> float | None:
    """A literal bound in seconds, or a deadline variable's bound; None for any other bound."""
    name = NAME.match(text)
    if deadlines and name and name[1] in deadlines:
        return deadlines[name[1]]
    match = LITERAL.match(text)
    return number(match[1]) if match else None


def window(lines: Sequence[str], start: int) -> tuple[int, str]:
    """The assertion starting on `start` up to its closing parenthesis, at most six lines."""
    depth = 0
    end = start
    limit = min(start + 5, len(lines) - 1)
    while end < limit:
        # An escaped parenthesis (in a regular expression literal) opens nothing.
        depth += len(re.findall(r"(?<!\\)\(", lines[end])) - len(re.findall(r"(?<!\\)\)", lines[end]))
        if depth <= 0:
            break
        end += 1
    return end, " ".join(lines[start:end + 1])


def bounds(text: str, measured: str, deadlines: dict[str, float | None]) -> list[float | None]:
    """The upper bounds the assertion puts on a measurement (None for a bound that is not a literal)."""
    found: list[float | None] = []
    for match in re.finditer(rf"(?:{measured})(?:[^,;<>&|?]|->)*?(?<![<-])<(?![<-])=?", text):
        found.append(seconds(text[match.end():], deadlines))
    for match in re.finditer(rf"(?<![=\-<>])>=?\s*(?:{measured})", text):
        before = BEFORE.search(text[:match.start()])
        found.append(seconds(before[1], deadlines) if before else None)
    for match in re.finditer(rf"assertLess(?:Equal)?\(\s*[^,]*?(?:{measured})[^,]*,", text):
        found.append(seconds(text[match.end():], deadlines))
    for match in re.finditer(rf"assertGreater(?:Equal)?\(([^,]*),\s*(?:{measured})", text):
        found.append(seconds(match[1], deadlines))
    return found


def timing_asserts(text: str) -> list[tuple[int, str]]:
    if not CLOCK.search(text):
        return []
    raw = text.split("\n")
    lines = [code(line) for line in raw]
    names: set[str] = set()
    assigned: list[tuple[int, str, float | None]] = []
    derived: list[tuple[str, str]] = []
    for line_number, line in enumerate(lines):
        # `elapsed = time.monotonic() - started`; `deadline = time.monotonic() + 5` is a deadline with its bound.
        for match in ASSIGNED.finditer(line):
            name, value = match[1], match[2]
            derived.append((name, value))
            if not CLOCK.search(value):
                continue
            plus = DEADLINE.search(value)
            if plus:
                assigned.append((line_number, name, seconds(plus[1])))
            elif DIFFERENCE.search(value):
                names.add(name)
        # Durations collected for a statistic: `samples.append(time.monotonic() - start)`.
        for match in COLLECTED.finditer(line):
            if CLOCK.search(match[2]) and DIFFERENCE.search(match[2]):
                names.add(match[1])
    # What is computed from a measurement measures too: a median of samples, not their count.
    for _ in range(3):
        for name, value in derived:
            if name not in names and any(
                    re.search(rf"(?<!len\()\b{re.escape(known)}\b(?!\s*\.\s*(?:count|length|isEmpty)\b)", value)
                    for known in list(names)):
                names.add(name)
    measured = CLOCK.pattern + "".join(rf"|\b{re.escape(name)}\b" for name in sorted(names))
    found: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if not ASSERTION.search(line):
            continue
        end, statement = window(lines, index)
        if HELPER.search(statement):
            continue
        deadlines = {name: bound for line_number, name, bound in assigned if line_number <= index}
        tight = [bound for bound in bounds(statement, measured, deadlines) if bound is None or bound < FLOOR_SECONDS]
        if not tight:
            continue
        above = index
        while above > 0 and not lines[above - 1].strip() and raw[above - 1].strip():
            above -= 1
        if LOAD_TOLERANT.search("\n".join(raw[above:end + 1])):
            continue
        limits = [bound for bound in tight if bound is not None]
        bound = "a computed bound" if len(limits) < len(tight) else f"{min(limits):g} s"
        found.append((index + 1, f"timing assertion with {bound}: {raw[index].strip()}"))
    return found


def arguments(call: ast.Call) -> list[str]:
    """The string literals a call passes: its positional arguments and the items of lists and tuples among them."""
    found: list[str] = []
    pending: list[ast.expr] = list(call.args)
    while pending:
        node = pending.pop(0)
        if isinstance(node, ast.Starred):
            pending.insert(0, node.value)
        elif isinstance(node, (ast.List, ast.Tuple)):
            pending[:0] = node.elts
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
    return found


def quoted_paths(text: str, filename: str = "<unknown>") -> list[tuple[int, str]]:
    """The calls that ask Git for a path list (--name-only, --name-status) it may print quoted."""
    found: list[tuple[int, str]] = []
    raw = text.split("\n")
    for node in ast.walk(ast.parse(text, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        passed = arguments(node)
        listing = [flag for flag in PATH_LISTS if flag in passed]
        if listing and "-z" not in passed and not any(UNQUOTED.match(value) for value in passed):
            found.append((node.lineno, f"{listing[0]} without -c core.quotePath=false or -z: "
                                       f"{raw[node.lineno - 1].strip()}"))
    return sorted(found)


def calls_perf_helper(text: str) -> bool:
    """Whether a file calls the perf helper (and not only defines it)."""
    return any(HELPER.search(code(line)) and not DEFINITION.search(line) for line in text.split("\n"))


def python_files(root: Path) -> list[str]:
    """The Python files Git knows under `root` (a .py file or a script with a python shebang): tracked or untracked
    and not ignored, outside fixtures."""
    listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root,
                            capture_output=True, text=True, check=True).stdout
    excluded = matcher(EXCLUDE)
    return [path for path in sorted({path for path in listed.split("\0") if path})
            if not excluded(path) and (root / path).is_file() and is_python(root / path)]


def is_python(path: Path) -> bool:
    if path.suffix == ".py":
        return True
    if path.suffix:
        return False
    with path.open("rb") as handle:
        return bool(PYTHON_SHEBANG.match(handle.readline(200).decode("utf-8", "replace")))


def test_files(root: Path) -> list[str]:
    """The Python test files Git knows under `root`: tracked or untracked and not ignored, outside fixtures."""
    is_test = matcher(TESTS)
    return [path for path in python_files(root) if path.endswith(".py") and is_test(path)]


def scan(root: Path, files: Iterable[str] | None = None) -> tuple[list[Finding], list[str], int]:
    """Every finding, the files outside the perf suite that call perf_budget, and how many test files were scanned.
    `files` names the test files to scan instead of every one; git-quoted-paths reads every Python file either way."""
    in_perf_suite = matcher(PERF_SUITE)
    all_python = python_files(root)
    findings: list[Finding] = []
    for file in all_python:
        found = quoted_paths((root / file).read_text(encoding="utf-8"), filename=file)
        findings += [Finding("git-quoted-paths", file, line, text) for line, text in found]
    unrun: list[str] = []
    scanned = 0
    is_test = matcher(TESTS)
    targets = [p for p in all_python if p.endswith(".py") and is_test(p)] if files is None else files
    for file in targets:
        text = (root / file).read_text(encoding="utf-8")
        scanned += 1
        findings += [Finding("fixed-sleep", file, line, found) for line, found in fixed_sleeps(text)]
        findings += [Finding("timing-assert", file, line, found) for line, found in timing_asserts(text)]
        if not in_perf_suite(file) and calls_perf_helper(text):
            unrun.append(file)
    return findings, unrun, scanned


def main(root: Path | None = None) -> int:
    if root is None:
        root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                                   check=True).stdout.strip())
    findings, unrun, scanned = scan(root)
    for file in unrun:
        print(f"test-hygiene: {file} states a perf budget (perf_budget), but it is not in the perf suite (perf/, "
              "test_perf_*.py), so its budgets never hold", file=sys.stderr)
    for finding in findings:
        print(f"test-hygiene: {finding.rule}: {finding.file}:{finding.line}: {finding.text}", file=sys.stderr)
    rules = {finding.rule for finding in findings}
    if "fixed-sleep" in rules:
        print("Wait for the condition instead (an event, a poll with a deadline), or mark a wait that must be fixed "
              "with `sleep-ok: <reason>`.", file=sys.stderr)
    if "timing-assert" in rules:
        print(f"Move a budget into the perf helper (perf_budget), give a functional deadline a bound of at least "
              f"{FLOOR_SECONDS} s, or a tighter one a `load-tolerant: <why>` comment.", file=sys.stderr)
    if "git-quoted-paths" in rules:
        print("Git quotes a path with an unusual byte in --name-only and --name-status output: pass "
              "`-c core.quotePath=false` before the subcommand, or `-z` and split on NUL.", file=sys.stderr)
    if findings or unrun:
        return 1
    print(f"test-hygiene: ok ({', '.join(RULES)}; {scanned} test files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
