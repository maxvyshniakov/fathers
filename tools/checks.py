#!/usr/bin/env python3
"""The repository's checks and which of them a change selects (fathers#116).

Repo-Kit runs the checks: `bun run verify`, the pre-push gate and Lanefather's lane checks (repo-kit.config.mjs reads
the steps from here). This file adds what Repo-Kit does not do for this repository: choosing each changed package's
checks from the changed paths.

  python3 tools/checks.py steps            the selected checks as Repo-Kit steps (JSON); FATHERS_CHECKS=all selects
                                           every check, FATHERS_CHECK_BASE names the base (default origin/main)
  python3 tools/checks.py run NAME         run one check (the steps' command, through `bun run check NAME`)
  python3 tools/checks.py plan BASE        Lanefather's plan for the changes since BASE (tools/lane-check answers it)

A change under a package selects its lint gate and tests; one under common/ selects every package; Slotfather's
command and sources also select Godfather's tests, which run them. Documentation selects only the cheap root checks,
which every selection includes. A path no rule knows selects everything. A tree without changes since its base
verifies everything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("slotfather", "lanefather", "junkfather", "godfather")
DEFAULT_BASE = "origin/main"
# Paths that no check reads: they select only the root checks every selection runs.
DOCUMENTATION = ("LICENSE", ".gitignore", ".github/dependabot.yml")
# Repo-Kit's own files: its configuration check and the root tool tests cover them.
KIT_FILES = ("package.json", "bun.lock", "bunfig.toml", "repo-kit.config.mjs", ".lanefather.toml")


@dataclass(frozen=True)
class Check:
    name: str
    command: list[str]  # bun arguments, as a Repo-Kit step takes them
    memory: float  # GiB at its peak, Repo-Kit's step memory
    timeout: int | None = None  # minutes; Repo-Kit's default (15) without one


def python_check(name: str) -> list[str]:
    return ["run", "check", name]


TESTS = {
    "slotfather": ["-s", "tests"],
    "lanefather": ["-s", "tests", "-p", "test_*.py"],
    "junkfather": ["-s", "tests"],
    "godfather": ["-s", "tests", "-t", "tests"],
}
CHECKS = [
    *(check for name in PACKAGES for check in (
        Check(f"{name} lint", python_check(f"{name} lint"), 0.8),
        Check(f"{name} tests", python_check(f"{name} tests"), 0.6, 30 if name == "lanefather" else None),
    )),
    Check("common", python_check("common"), 0.8),
    Check("tools", python_check("tools"), 0.3),
    Check("kit", ["run", "check:kit"], 0.2),
    Check("test hygiene", ["run", "check:hygiene"], 0.2),
]
ALWAYS = ("tools", "kit", "test hygiene")
NAMES = [check.name for check in CHECKS]


def owners(path: str) -> set[str] | None:
    """The checks a changed path selects besides the root checks; None for a path no rule knows."""
    first, _, rest = path.partition("/")
    if first in PACKAGES:
        chosen = {f"{first} lint", f"{first} tests"}
        if first == "slotfather" and (rest == "slotfather" or rest.startswith("src/")):
            chosen.add("godfather tests")  # its contract tests run ../slotfather
        return chosen
    if first == "common":
        return {name for name in NAMES if name.split()[0] in PACKAGES} | {"common"}
    if path == "install.py":
        return {"common"}
    if path == "tools/ruff.toml":
        return {f"{name} lint" for name in PACKAGES}
    if first == "tools":
        return set()
    if path.startswith(".github/workflows/"):
        workflow = Path(path).stem
        if workflow in PACKAGES:
            return {f"{workflow} lint", f"{workflow} tests"}
        return {"common"} if workflow == "common" else None
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
    """Ruff or Pyright for the root's own files: on PATH, else the pins every package shares (through uv)."""
    tool = arguments[0]
    if shutil.which(tool):
        return list(arguments)
    return ["uv", "run", "--quiet", "--no-project", "--with-requirements", "slotfather/requirements-dev.txt",
            *arguments]


def commands(name: str) -> tuple[Path, list[list[str]]]:
    """Where a check runs and its commands, in order."""
    owner, _, kind = name.partition(" ")
    if owner in PACKAGES and kind == "lint":
        return ROOT / owner, [[sys.executable, "tools/check.py"]]
    if owner in PACKAGES and kind == "tests":
        return ROOT / owner, [[sys.executable, "-m", "unittest", "discover", *TESTS[owner]]]
    if name == "common":  # the lint of .github/workflows/common.yml
        return ROOT, [lint_tools("ruff", "check", "common", "install.py"), lint_tools("pyright", "-p", "common")]
    if name == "tools":
        return ROOT, [[sys.executable, "-m", "unittest", "discover", "-s", "tools", "-p", "test_*.py"],
                      lint_tools("ruff", "check", "--config", "tools/ruff.toml", "tools", "tools/lane-check")]
    raise ValueError(f"unknown check {name!r}; one of: {', '.join(NAMES)}")


def run(name: str) -> int:
    directory, argvs = commands(name)
    for argv in argvs:
        print("+", " ".join(argv), flush=True)
        environment = {**os.environ, "PYRIGHT_PYTHON_IGNORE_WARNINGS": "1"}
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
