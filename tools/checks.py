#!/usr/bin/env python3
"""The repository's checks and which of them a change selects (fathers#116).

Repo-Kit runs the checks: `bun run verify`, the pre-push gate and Lanefather's lane checks (repo-kit.config.mjs reads
the steps from here). This file adds what Repo-Kit does not do for this repository: choosing each changed package's
checks from the changed paths.

  python3 tools/checks.py steps            the selected checks as Repo-Kit steps (JSON); FATHERS_CHECKS=all selects
                                           every check, FATHERS_CHECK_BASE names the base (default origin/main)
  python3 tools/checks.py run NAME         run one check (the steps' command, through `bun run check NAME`)
  python3 tools/checks.py plan BASE        Lanefather's plan for the changes since BASE (tools/lane-check answers it)

A change under a package selects its lint gate and tests, except package documentation (GLOSSARY.md, docs/**); one
under common/ selects every package; Slotfather's command and sources also select Godfather's tests, which run them.
Documentation selects only the cheap root checks, which every selection includes. A path no rule knows selects
everything. A tree without changes since its base verifies everything.
"""
from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("slotfather", "lanefather", "junkfather", "godfather", "tokenfather")
# Packages whose test files use isolated tempdirs and can safely run in parallel across workers (fathers#209).
PARALLEL_TEST_PACKAGES = frozenset({"lanefather"})
MAX_DEFAULT_TEST_WORKERS = 4
RAN_TESTS_RE = re.compile(r"^Ran (\d+) tests? in ", re.M)
SKIPPED_TESTS_RE = re.compile(r"\bskipped=(\d+)\b")
DEFAULT_BASE = "origin/main"
# Paths that no check reads: they select only the root checks every selection runs.
DOCUMENTATION = ("LICENSE", ".gitignore", ".github/dependabot.yml")
# Package documentation that no package check or installer reads (fathers#193).
PACKAGE_DOCUMENTATION = ("GLOSSARY.md",)
# Repo-Kit's own files: its configuration check and the root tool tests cover them.
KIT_FILES = ("package.json", "bun.lock", "bunfig.toml", "repo-kit.config.mjs", ".lanefather.toml")


@dataclass(frozen=True)
class Check:
    name: str
    command: list[str]  # bun arguments, as a Repo-Kit step takes them
    memory: float  # GiB at its peak, Repo-Kit's step memory (measured 2026-11-13: lint 0.4-0.6, tests 0.1-0.2)
    timeout: int | None = None  # minutes; Repo-Kit's default (15) without one


def python_check(name: str) -> list[str]:
    return ["run", "check", name]


TESTS = {
    "slotfather": ["-s", "tests"],
    "lanefather": ["-s", "tests", "-p", "test_*.py"],
    "junkfather": ["-s", "tests"],
    "godfather": ["-s", "tests", "-t", "tests"],
    "tokenfather": ["-s", "tests"],
}
CHECKS = [
    *(check for name in PACKAGES for check in (
        Check(f"{name} lint", python_check(f"{name} lint"), 0.7),
        Check(f"{name} tests", python_check(f"{name} tests"), 0.3, 30 if name == "lanefather" else None),
    )),
    Check("common lint", python_check("common lint"), 0.4),
    Check("common tests", python_check("common tests"), 0.2),
    Check("tools", python_check("tools"), 0.2),
    Check("kit", ["run", "check:kit"], 0.2),
    Check("test hygiene", ["run", "check:hygiene"], 0.2),
]
ALWAYS = ("tools", "kit", "test hygiene")
NAMES = [check.name for check in CHECKS]


def owners(path: str) -> set[str] | None:
    """The checks a changed path selects besides the root checks; None for a path no rule knows."""
    first, _, rest = path.partition("/")
    if first in PACKAGES:
        if rest in PACKAGE_DOCUMENTATION or rest.startswith("docs/"):
            return set()
        chosen = {f"{first} lint", f"{first} tests"}
        if first == "slotfather" and (rest == "slotfather" or rest.startswith("src/")):
            chosen.add("godfather tests")  # its contract tests run ../slotfather
        return chosen
    if first == "common":
        return {name for name in NAMES if name.split()[0] in PACKAGES} | {"common lint", "common tests"}
    if path == "install.py":
        return {"common lint", "common tests"}
    if path == "tools/ruff.toml":
        return {f"{name} lint" for name in PACKAGES}
    if first == "tools":
        return set()
    if path.startswith(".github/workflows/"):
        workflow = Path(path).stem
        if workflow in PACKAGES:
            return {f"{workflow} lint", f"{workflow} tests"}
        if workflow == "package":  # the shape the five call: tools/test_checks.py reads it
            return set()
        return {"common lint", "common tests"} if workflow == "common" else None
    if path in KIT_FILES or first == ".githooks":
        return set()
    if path in DOCUMENTATION or first in ("docs", "notes") or ("/" not in path and path.endswith(".md")):
        return set()
    return None


@dataclass
class Selection:
    full: bool = False
    unowned: list[str] = field(default_factory=list)
    checks: dict[str, list[str]] = field(default_factory=dict)  # name -> the paths that chose it


def select(paths: list[str]) -> Selection:
    """The checks `paths` select, in CHECKS order."""
    chosen: dict[str, list[str]] = {}
    unowned: list[str] = []
    for path in sorted(set(paths)):
        found = owners(path)
        if found is None:
            unowned.append(path)
            continue
        for name in found | set(ALWAYS):
            chosen.setdefault(name, []).append(path)
    for name in ALWAYS:
        chosen.setdefault(name, [])
    if unowned:
        return Selection(True, unowned, {name: chosen.get(name, []) for name in NAMES})
    return Selection(False, [], {name: chosen[name] for name in NAMES if name in chosen})


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, check=True).stdout


def changed_paths(root: Path, base: str) -> list[str]:
    """The paths the checkout changes since `base`: committed, uncommitted and untracked, as Repo-Kit's plan."""
    revision = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()
    listed = git(root, "diff", "--name-only", "-z", revision, "--").split("\0")
    listed += git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted({path for path in listed if path})


def encode(name: str) -> str:
    """A step name as Repo-Kit's check ids carry it (JavaScript's encodeURIComponent)."""
    return quote(name, safe="-_.!~*'()")


def plan(root: Path, base: str) -> dict[str, object]:
    """Lanefather's check contract: {v, full, paths, unowned, suites, reasons, checks: [{id, suite, paths,
    producer}]}, with the ids Repo-Kit's lane adapter gives the same steps."""
    paths = changed_paths(root, base)
    selection = select(paths)
    why = (["A path no rule knows changed: every check runs."] if selection.full
           else ["Each changed package's checks, and the root checks."])
    return {"v": 1, "full": selection.full, "paths": paths, "unowned": selection.unowned, "suites": ["verify"],
            "reasons": {"verify": why},
            "checks": [{"id": f"verify:{encode(name)}", "suite": "verify", "paths": chosen, "producer": False}
                       for name, chosen in selection.checks.items()]}


def steps(environment: dict[str, str]) -> list[dict[str, object]]:
    """The Repo-Kit steps to run: every check, or those the changes since the base select."""
    wanted = environment.get("FATHERS_CHECKS", "")
    if wanted not in ("", "all"):
        raise ValueError(f"FATHERS_CHECKS must be 'all' or unset, not {wanted!r}")
    names = NAMES
    if wanted != "all":
        base = environment.get("FATHERS_CHECK_BASE") or DEFAULT_BASE
        try:
            merge_base = git(ROOT, "merge-base", "HEAD", base).strip()
            paths = changed_paths(ROOT, merge_base)
        except subprocess.CalledProcessError:
            paths = []  # no such base here: verify everything
        if paths:
            names = list(select(paths).checks)
    rows: list[dict[str, object]] = []
    for check in CHECKS:
        if check.name in names:
            row: dict[str, object] = {"name": check.name, "command": check.command, "memory": check.memory}
            if check.timeout is not None:
                row["timeout"] = check.timeout
            rows.append(row)
    return rows


def lint_tools(*arguments: str) -> list[str]:
    """Ruff or Pyright for the root's own files: on PATH, else common/requirements-dev.txt's pins (through uv)."""
    tool = arguments[0]
    if shutil.which(tool):
        return list(arguments)
    return ["uv", "run", "--quiet", "--no-project", "--with-requirements", "common/requirements-dev.txt",
            *arguments]


def commands(name: str) -> tuple[Path, list[list[str]]]:
    """Where a check runs and its commands, in order."""
    owner, _, kind = name.partition(" ")
    if owner in PACKAGES and kind == "lint":
        return ROOT / owner, [[sys.executable, "tools/check.py"]]
    if owner in PACKAGES and kind == "tests":
        return ROOT / owner, [[sys.executable, "-m", "unittest", "discover", *TESTS[owner]]]
    if name == "common lint":
        return ROOT, [lint_tools("ruff", "check", "common", "install.py"), lint_tools("pyright", "-p", "common")]
    if name == "common tests":  # the CLI convention's helper (docs/cli.md)
        return ROOT, [[sys.executable, "-m", "unittest", "discover", "-s", "common/tests"]]
    if name == "tools":
        return ROOT, [[sys.executable, "-m", "unittest", "discover", "-s", "tools", "-p", "test_*.py"],
                      lint_tools("ruff", "check", "--config", "tools/ruff.toml", "tools", "tools/lane-check")]
    raise ValueError(f"unknown check {name!r}; one of: {', '.join(NAMES)}")


@dataclass(frozen=True)
class FileTestResult:
    index: int
    relative: str
    returncode: int
    stdout: str
    stderr: str
    elapsed: float
    ran: int
    skipped: int


def test_workers(environment: Mapping[str, str] | None = None) -> int:
    """Bounded worker count for parallel test-file execution; FATHERS_TEST_WORKERS overrides the default."""
    env = os.environ if environment is None else environment
    raw = env.get("FATHERS_TEST_WORKERS", "").strip()
    if not raw:
        return min(MAX_DEFAULT_TEST_WORKERS, os.cpu_count() or 1)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"FATHERS_TEST_WORKERS must be a positive integer, not {raw!r}") from None
    if value < 1:
        raise ValueError(f"FATHERS_TEST_WORKERS must be a positive integer, not {raw!r}")
    return value


def discover_test_files(directory: Path, discover_args: list[str]) -> tuple[list[Path], str | None]:
    """Test files under `directory` matching `unittest discover` arguments, plus any `-t` top-level directory."""
    start = "."
    pattern = "test*.py"
    top_level: str | None = None
    index = 0
    while index < len(discover_args):
        arg = discover_args[index]
        if arg in ("-s", "--start-directory") and index + 1 < len(discover_args):
            start = discover_args[index + 1]
            index += 2
        elif arg in ("-p", "--pattern") and index + 1 < len(discover_args):
            pattern = discover_args[index + 1]
            index += 2
        elif arg in ("-t", "--top-level-directory") and index + 1 < len(discover_args):
            top_level = discover_args[index + 1]
            index += 2
        else:
            index += 1
    base = directory / start
    if not base.is_dir():
        return [], top_level
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        if current != base and not (current / "__init__.py").is_file():
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__" and (current / d / "__init__.py").is_file())
        for filename in sorted(filenames):
            if fnmatch.fnmatchcase(filename, pattern):
                found.append(current / filename)
    return sorted(found), top_level


def _file_unittest_argv(directory: Path, file: Path, top_level: str | None) -> list[str]:
    start_dir = file.parent.relative_to(directory).as_posix()
    argv = [sys.executable, "-m", "unittest", "discover", "-s", start_dir]
    if top_level is not None:
        argv.extend(["-t", top_level])
    argv.extend(["-p", file.name])
    return argv


def _parse_unittest_counts(stderr: str) -> tuple[int, int]:
    ran_match = RAN_TESTS_RE.search(stderr)
    ran = int(ran_match.group(1)) if ran_match else 0
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    skipped = 0
    if lines and lines[-1].startswith(("OK", "FAILED")):
        skip_match = SKIPPED_TESTS_RE.search(lines[-1])
        if skip_match:
            skipped = int(skip_match.group(1))
    return ran, skipped


def run_unittest_parallel(directory: Path, discover_args: list[str], environment: dict[str, str],
                          workers: int) -> int:
    """Run independent unittest files across a bounded subprocess pool with deterministic output and exit code."""
    files, top_level = discover_test_files(directory, discover_args)
    if workers <= 1 or len(files) <= 1:
        argv = [sys.executable, "-m", "unittest", "discover", *discover_args]
        print("+", " ".join(argv), flush=True)
        return subprocess.run(argv, cwd=directory, env=environment).returncode

    pool_size = min(workers, len(files))
    print(f"+ {sys.executable} -m unittest discover {' '.join(discover_args)} "
          f"({len(files)} files, {pool_size} workers)", flush=True)
    # Schedule largest test files first (LPT) to avoid a long tail while reporting in deterministic sorted order.
    schedule = sorted(range(len(files)), key=lambda i: (-files[i].stat().st_size, i))
    started = time.monotonic()

    def run_one(index: int) -> FileTestResult:
        file = files[index]
        argv = _file_unittest_argv(directory, file, top_level)
        file_started = time.monotonic()
        completed = subprocess.run(argv, cwd=directory, env=environment, capture_output=True, text=True)
        file_elapsed = time.monotonic() - file_started
        ran, skipped = _parse_unittest_counts(completed.stderr)
        return FileTestResult(index=index, relative=file.relative_to(directory).as_posix(),
                              returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr,
                              elapsed=file_elapsed, ran=ran, skipped=skipped)

    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        results = sorted(pool.map(run_one, schedule), key=lambda item: item.index)

    elapsed = time.monotonic() - started
    total_ran = sum(item.ran for item in results)
    total_skipped = sum(item.skipped for item in results)
    failed = [item for item in results if item.returncode != 0]

    for item in failed:
        print(f"=== FAIL: {item.relative} (exit {item.returncode}, {item.elapsed:.2f}s) ===", file=sys.stderr)
        if item.stdout:
            sys.stderr.write(item.stdout)
            if not item.stdout.endswith("\n"):
                sys.stderr.write("\n")
        if item.stderr:
            sys.stderr.write(item.stderr)
            if not item.stderr.endswith("\n"):
                sys.stderr.write("\n")

    print("-" * 70, file=sys.stderr)
    test_word = "test" if total_ran == 1 else "tests"
    print(f"Ran {total_ran} {test_word} in {elapsed:.3f}s ({len(files)} files, {pool_size} workers)", file=sys.stderr)
    print(file=sys.stderr)
    if failed:
        names = ", ".join(item.relative for item in failed)
        print(f"FAILED (files={len(failed)}: {names})", file=sys.stderr)
        return failed[0].returncode
    if total_skipped:
        print(f"OK (skipped={total_skipped})", file=sys.stderr)
    else:
        print("OK", file=sys.stderr)
    return 0


def run(name: str) -> int:
    owner, _, kind = name.partition(" ")
    environment = {**os.environ, "PYRIGHT_PYTHON_IGNORE_WARNINGS": "1"}
    if owner in PARALLEL_TEST_PACKAGES and kind == "tests":
        workers = test_workers(environment)
        if workers > 1:
            return run_unittest_parallel(ROOT / owner, TESTS[owner], environment, workers)
    directory, argvs = commands(name)
    for argv in argvs:
        print("+", " ".join(argv), flush=True)
        status = subprocess.run(argv, cwd=directory, env=environment).returncode
        if status:
            return status
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["steps"] and len(argv) == 1:
            print(json.dumps(steps(dict(os.environ))))
            return 0
        if argv[:1] == ["run"] and len(argv) == 2:
            return run(argv[1])
        if argv[:1] == ["plan"] and len(argv) == 2:
            print(json.dumps(plan(Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip()), argv[1])))
            return 0
    except (ValueError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", None) or error
        print(f"checks: {detail}", file=sys.stderr)
        return 2
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
