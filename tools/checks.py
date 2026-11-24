#!/usr/bin/env python3
"""The repository's checks and which of them a change selects (fathers#116, fathers#235).

The checks run through tools/verify.py: this command's `verify`, the pre-push gate (.githooks/pre-push,
tools/prepush.py) and Lanefather's lane checks (tools/lane-check). This file names them and chooses each changed
package's checks from the changed paths.

  python3 tools/checks.py verify [--keep-going] [--fresh]
                                           the selected checks under one machine lease, side by side; a check that
                                           passed on this clean tree before is skipped (--fresh runs it again).
                                           FATHERS_CHECKS=all selects every check, FATHERS_CHECK_BASE names the base
                                           (default origin/main)
  python3 tools/checks.py run NAME         run one check (each verify step runs this)
  python3 tools/checks.py plan BASE        Lanefather's plan for the changes since BASE (tools/lane-check answers it)

A change under a package selects its lint gate and tests, except package documentation (GLOSSARY.md, docs/**); one
under common/ selects every package; cross-package contract edges (Slotfather and Spawnfather sources -> Lanefather
tests; Slotfather, Spawnfather and Lanefather sources -> Godfather tests; Spawnfather sources -> Slotfather tests;
package installers, versions and changelogs -> common tests) come from each check's declared `inputs`. Changes to
`tools/checks.py` or `tools/verify.py` select every check. Documentation selects only the cheap root checks, which
every selection includes. A path no rule knows selects everything. A tree without changes since its base verifies
everything.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import fnmatch
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify as runner  # tools/verify.py: the step runner, its lease and cache

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("slotfather", "lanefather", "spawnfather", "godfather", "tokenfather")
# Packages whose test files use isolated tempdirs and can safely run in parallel across workers (fathers#209, #219).
PARALLEL_TEST_PACKAGES = frozenset({"slotfather", "lanefather", "spawnfather", "godfather"})
MAX_DEFAULT_TEST_WORKERS = 4
TEST_TIMINGS_RECORD = "fathers-test-timings.json"
RAN_TESTS_RE = re.compile(r"^Ran (\d+) tests? in ", re.M)
SKIPPED_TESTS_RE = re.compile(r"\bskipped=(\d+)\b")
DEFAULT_BASE = "origin/main"
# Paths that no check reads: they select only the root checks every selection runs.
DOCUMENTATION = ("LICENSE", ".gitignore", ".github/dependabot.yml")
# Package documentation that no package check or installer reads (fathers#193).
PACKAGE_DOCUMENTATION = ("GLOSSARY.md",)
# Root configuration the root tool tests cover. A file not named here (a package.json, say) selects everything.
ROOT_CONFIG = (".lanefather.toml",)
# Gate and runner files: a change to either selects every check and invalidates every check's cache key.
RUNNER_INPUTS = ("tools/checks.py", "tools/verify.py")


@dataclass(frozen=True)
class Check:
    name: str
    memory: float  # GiB at its peak, what the runner fits beside the others (measured 2026-11-13: lint 0.4-0.6,
    # tests 0.1-0.2)
    timeout: int = 15  # minutes
    inputs: tuple[str, ...] = ()  # repo-relative paths/prefixes (ending in '/'); empty => full HEAD^{tree}


TESTS = {
    "slotfather": ["-s", "tests"],
    "lanefather": ["-s", "tests", "-p", "test_*.py"],
    "spawnfather": ["-s", "tests"],
    "godfather": ["-s", "tests", "-t", "tests"],
    "tokenfather": ["-s", "tests"],
}
COMMON_PACKAGE_INPUTS = tuple(
    path for pkg in PACKAGES for path in (
        f"{pkg}/CHANGELOG.md",
        f"{pkg}/pyproject.toml",
        f"{pkg}/install.py",
        f"{pkg}/src/{pkg}/version.py",
        f"{pkg}/{pkg}/__init__.py",
    )
)


def _package_inputs(name: str, kind: str) -> tuple[str, ...]:
    base = (f"{name}/", "common/", *RUNNER_INPUTS, f".github/workflows/{name}.yml")
    if kind == "lint":
        return (*base, "tools/ruff.toml")
    if name == "slotfather":
        return (*base, "spawnfather/spawnfather/")
    if name == "lanefather":
        return (*base, "slotfather/slotfather", "slotfather/src/", "spawnfather/spawnfather/")
    if name == "godfather":
        return (*base, "slotfather/slotfather", "slotfather/src/", "spawnfather/spawnfather/", "lanefather/lanefather/")
    if name == "tokenfather":
        return (*base, "godfather/tests/fixtures/changelog/")
    return base


CHECKS = [
    *(check for name in PACKAGES for check in (
        Check(f"{name} lint", 0.7, inputs=_package_inputs(name, "lint")),
        Check(f"{name} tests", 0.3, 30 if name == "lanefather" else 15, inputs=_package_inputs(name, "tests")),
    )),
    Check("common lint", 0.4, inputs=("common/", "install.py", ".github/workflows/common.yml", *RUNNER_INPUTS)),
    Check("common tests", 0.2, inputs=(
        "common/", "install.py", ".github/workflows/common.yml", *RUNNER_INPUTS,
        "slotfather/src/", "spawnfather/spawnfather/", *COMMON_PACKAGE_INPUTS,
    )),
    Check("tools", 0.2),
    Check("test hygiene", 0.2),
]
CHECKS_BY_NAME = {check.name: check for check in CHECKS}
ALWAYS = ("tools", "test hygiene")
NAMES = [check.name for check in CHECKS]


def _matches_input(path: str, spec: str) -> bool:
    return path.startswith(spec) if spec.endswith("/") else path == spec


def owners(path: str) -> set[str] | None:
    """The checks a changed path selects besides the root checks; None for a path no rule knows."""
    first, _, rest = path.partition("/")
    if first in PACKAGES and (rest in PACKAGE_DOCUMENTATION or rest.startswith("docs/")):
        return set()
    matched = {check.name for check in CHECKS if any(_matches_input(path, spec) for spec in check.inputs)}
    if matched:
        return matched
    if first == "tools" or path == ".github/workflows/package.yml":
        return set()
    if path in ROOT_CONFIG or first == ".githooks":
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
    """The paths the checkout changes since `base`: committed, uncommitted and untracked."""
    revision = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()
    listed = git(root, "diff", "--name-only", "-z", revision, "--").split("\0")
    listed += git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted({path for path in listed if path})


def encode(name: str) -> str:
    """A check's name as its Lanefather id carries it (JavaScript's encodeURIComponent, as Repo-Kit gave them)."""
    return quote(name, safe="-_.!~*'()")


def plan(root: Path, base: str) -> dict[str, object]:
    """Lanefather's check contract: {v, full, paths, unowned, suites, reasons, checks: [{id, suite, paths,
    producer}]}, with the ids tools/lane-check runs them by."""
    paths = changed_paths(root, base)
    selection = select(paths)
    why = (["A path no rule knows changed: every check runs."] if selection.full
           else ["Each changed package's checks, and the root checks."])
    return {"v": 1, "full": selection.full, "paths": paths, "unowned": selection.unowned, "suites": ["verify"],
            "reasons": {"verify": why}, "capabilities": ["base"],
            "checks": [{"id": f"verify:{encode(name)}", "suite": "verify", "paths": chosen, "producer": False}
                       for name, chosen in selection.checks.items()]}


def _selected_with_full_tree(environment: Mapping[str, str]) -> tuple[list[Check], bool]:
    wanted = environment.get("FATHERS_CHECKS", "")
    if wanted not in ("", "all"):
        raise ValueError(f"FATHERS_CHECKS must be 'all' or unset, not {wanted!r}")
    names = NAMES
    full_tree = False
    if wanted != "all":
        base = environment.get("FATHERS_CHECK_BASE") or DEFAULT_BASE
        try:
            merge_base = git(ROOT, "merge-base", "HEAD", base).strip()
            paths = changed_paths(ROOT, merge_base)
        except subprocess.CalledProcessError:
            paths = []  # no such base here: verify everything
        if paths:
            selection = select(paths)
            names = list(selection.checks)
            full_tree = bool(selection.unowned)
    return [check for check in CHECKS if check.name in names], full_tree


def selected(environment: Mapping[str, str]) -> list[Check]:
    """The checks to run, in CHECKS order: every one (FATHERS_CHECKS=all), or those the changes since the merge-base
    with FATHERS_CHECK_BASE (default origin/main) select; everything when nothing changed or there is no such base."""
    return _selected_with_full_tree(environment)[0]


def step(check: Check) -> runner.Step:
    """A check as the runner's step: `checks.py run NAME` in the root, with its memory, timeout and cache inputs."""
    return runner.Step(check.name, [sys.executable, str(ROOT / "tools/checks.py"), "run", check.name], check.memory,
                       check.timeout, ROOT, check.inputs)


def cache_keys(root: Path, tree: str, names: Iterable[str], *, full_tree: bool = False,
               env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Per-check cache keys for `tree`, from each check's declared `inputs` (or full `tree` when `full_tree`)."""
    if full_tree:
        return {name: tree for name in names}
    steps = [step(CHECKS_BY_NAME[name]) if name in CHECKS_BY_NAME else runner.Step(name, []) for name in names]
    return runner.tree_keys(root, tree, steps, env=env)


def verify(arguments: list[str]) -> int:
    """`checks.py verify [--keep-going] [--fresh]`: the selected checks under one lease, skipping those whose inputs
    passed already (--fresh runs them again)."""
    unknown = [argument for argument in arguments if argument not in ("--keep-going", "--fresh")]
    if unknown:
        raise ValueError(f"verify takes --keep-going and --fresh, not {' '.join(unknown)}")
    environment = dict(os.environ)
    checks, full_tree = _selected_with_full_tree(environment)
    slots = environment.get("FATHERS_VERIFY_SLOTS") or "2"
    leased = runner.lease_self(slots, "verify", [sys.executable, str(ROOT / "tools/checks.py"), "verify", *arguments],
                               root=ROOT, environment=environment)
    if leased is not None:
        return leased
    return runner.gate([step(check) for check in checks], root=ROOT, keep_going="--keep-going" in arguments,
                       use_cache="--fresh" not in arguments, full_tree=full_tree, env=environment)


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
    if name == "test hygiene":
        return ROOT, [[sys.executable, "tools/hygiene.py"]]
    if name == "tools":
        return ROOT, [[sys.executable, "-m", "unittest", "discover", "-s", "tools", "-p", "test_*.py"],
                      lint_tools("ruff", "check", "--config", "tools/ruff.toml", "tools", "tools/lane-check",
                                 ".githooks/pre-push")]
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


def _positive_int(raw: str) -> int | None:
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value > 0 else None


def test_workers(environment: Mapping[str, str] | None = None) -> int:
    """Bounded worker count for parallel test-file execution; FATHERS_TEST_WORKERS overrides the default.
    Under a Slotfather lease or FATHERS_VERIFY_SLOTS, scales to max(2, min(MAX_DEFAULT_TEST_WORKERS, slots))
    (bounded by CPU count) so concurrent package suites do not oversubscribe a small lease."""
    env = os.environ if environment is None else environment
    raw = env.get("FATHERS_TEST_WORKERS", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"FATHERS_TEST_WORKERS must be a positive integer, not {raw!r}") from None
        if value < 1:
            raise ValueError(f"FATHERS_TEST_WORKERS must be a positive integer, not {raw!r}")
        return value
    cpus = os.cpu_count() or 1
    slots = _positive_int(env.get("SLOTFATHER_SLOTS", ""))
    if slots is None and env.get("SLOTFATHER_LEASE"):
        slots = _positive_int(env.get("SLOTFATHER_COUNT", ""))
    if slots is None:
        slots = _positive_int(env.get("FATHERS_VERIFY_SLOTS", ""))
    if slots is not None:
        return min(cpus, max(2, min(MAX_DEFAULT_TEST_WORKERS, slots)))
    return min(MAX_DEFAULT_TEST_WORKERS, cpus)


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


def _test_timings_path(directory: Path, environment: Mapping[str, str]) -> Path | None:
    """`<git common dir>/fathers-test-timings.json`, or None when `directory` is outside a Git repository."""
    try:
        common = runner.common_dir(directory, runner.clean_environment(environment))
    except (OSError, subprocess.CalledProcessError):
        return None
    return common / TEST_TIMINGS_RECORD


def _read_timings_document(path: Path) -> dict[str, dict[str, float]]:
    """Parsed `{package: {relative_path: elapsed_seconds}}` from `path`, or `{}` when missing or corrupt."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    packages_raw = raw.get("packages")
    if not isinstance(packages_raw, dict):
        return {}
    packages: dict[str, dict[str, float]] = {}
    for pkg, entries in packages_raw.items():
        if not isinstance(pkg, str) or not isinstance(entries, dict):
            continue
        cleaned: dict[str, float] = {}
        for rel, elapsed in entries.items():
            if (isinstance(rel, str) and isinstance(elapsed, (int, float))
                    and not isinstance(elapsed, bool) and math.isfinite(elapsed) and elapsed >= 0):
                cleaned[rel] = float(elapsed)
        if cleaned:
            packages[pkg] = cleaned
    return packages


def load_test_timings(path: Path | None, package: str) -> dict[str, float]:
    """Recorded per-file elapsed seconds for `package`, or `{}` when unavailable or corrupt."""
    if path is None:
        return {}
    return _read_timings_document(path).get(package, {})


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        pending.write_text(text, encoding="utf-8")
        os.replace(pending, path)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise


def save_test_timings(path: Path | None, package: str, results: Iterable[FileTestResult]) -> None:
    """Atomically persist per-file elapsed timings for `package`, failing open silently on I/O errors."""
    if path is None:
        return
    items = list(results)
    if not items:
        return
    try:
        packages = _read_timings_document(path)
        previous = packages.get(package, {})
        updated: dict[str, float] = {}
        for item in items:
            if item.returncode == 0 and math.isfinite(item.elapsed) and item.elapsed >= 0:
                updated[item.relative] = round(item.elapsed, 4)
            elif item.relative in previous:
                updated[item.relative] = previous[item.relative]
        if updated:
            packages[package] = updated
        else:
            packages.pop(package, None)
        payload = json.dumps({"v": 1, "packages": packages}, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, payload)
    except OSError:
        return


def schedule_test_files(directory: Path, files: list[Path], timings: Mapping[str, float]) -> list[int]:
    """Indices of `files` ordered longest-first by recorded elapsed time, falling back to file size."""
    sizes = [file.stat().st_size for file in files]
    relatives = [file.relative_to(directory).as_posix() for file in files]
    recorded_seconds = 0.0
    recorded_bytes = 0
    for rel, size in zip(relatives, sizes, strict=True):
        if rel in timings:
            recorded_seconds += timings[rel]
            recorded_bytes += size
    rate = recorded_seconds / recorded_bytes if recorded_bytes > 0 else None

    def sort_key(index: int) -> tuple[float, int, int]:
        rel = relatives[index]
        size = sizes[index]
        if rel in timings:
            estimate = timings[rel]
        elif rate is not None:
            estimate = size * rate
        else:
            estimate = float(size)
        return (-estimate, -size, index)

    return sorted(range(len(files)), key=sort_key)


def run_unittest_parallel(directory: Path, discover_args: list[str], environment: dict[str, str],
                          workers: int, *, timings_path: Path | None = None,
                          package: str | None = None) -> int:
    """Run independent unittest files across a bounded subprocess pool with deterministic output and exit code."""
    files, top_level = discover_test_files(directory, discover_args)
    if workers <= 1 or len(files) <= 1:
        argv = [sys.executable, "-m", "unittest", "discover", *discover_args]
        print("+", " ".join(argv), flush=True)
        return subprocess.run(argv, cwd=directory, env=environment).returncode

    pool_size = min(workers, len(files))
    print(f"+ {sys.executable} -m unittest discover {' '.join(discover_args)} "
          f"({len(files)} files, {pool_size} workers)", flush=True)
    # Schedule longest test files first (measured LPT, falling back to file size) while reporting in sorted order.
    resolved_timings_path = _test_timings_path(directory, environment) if timings_path is None else timings_path
    package_key = package or directory.name
    schedule = schedule_test_files(directory, files, load_test_timings(resolved_timings_path, package_key))
    started = time.monotonic()

    def run_one(index: int) -> FileTestResult:
        file = files[index]
        argv = _file_unittest_argv(directory, file, top_level)
        file_started = time.monotonic()
        completed = subprocess.run(argv, cwd=directory, env=environment, capture_output=True, text=True)
        file_elapsed = time.monotonic() - file_started
        ran, skipped = _parse_unittest_counts(completed.stderr)
        code = 0 if completed.returncode == 5 and ran == 0 else completed.returncode
        return FileTestResult(index=index, relative=file.relative_to(directory).as_posix(),
                              returncode=code, stdout=completed.stdout, stderr=completed.stderr,
                              elapsed=file_elapsed, ran=ran, skipped=skipped)

    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        results = sorted(pool.map(run_one, schedule), key=lambda item: item.index)

    save_test_timings(resolved_timings_path, package_key, results)

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
        return 1 if failed[0].returncode < 0 else failed[0].returncode
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
        if argv[:1] == ["run"] and len(argv) == 2:
            return run(argv[1])
        if argv[:1] == ["verify"]:
            return verify(argv[1:])
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
