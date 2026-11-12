#!/usr/bin/env python3
"""lanefather's checks: ruff, pyright strict against a baseline that only shrinks, and the test suite.

  python3 tools/check.py                    every check
  python3 tools/check.py --lint             ruff and pyright only
  python3 tools/check.py --update-baseline  rewrite pyright-baseline.json after errors went away; refuses to grow it

ruff and pyright run through uvx (no install step; lanefather itself stays standard library only). The baseline counts
pyright's errors per file and rule: a count that grows fails the check, one that shrinks asks for --update-baseline.
Heavy runs go through slotfather where it is installed (`slotfather run -n 1`).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "pyright-baseline.json"
RUFF = ["uvx", "ruff@0.17.3"]
PYRIGHT = ["uvx", "--from", "pyright==1.1.414", "pyright"]

Counts = dict[str, dict[str, int]]


def pyright_counts() -> Counts:
    result = subprocess.run([*PYRIGHT, "--outputjson"], cwd=ROOT, capture_output=True, text=True)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        sys.exit(f"pyright printed no report (exit {result.returncode}): {result.stderr.strip()[:500]}")
    counts: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for diagnostic in report["generalDiagnostics"]:
        if diagnostic["severity"] == "error":
            path = os.path.relpath(diagnostic["file"], ROOT)
            counts[path][diagnostic.get("rule") or "error"] += 1
    return {path: dict(sorted(rules.items())) for path, rules in sorted(counts.items())}


def compare(current: Counts, baseline: Counts) -> tuple[list[str], list[str]]:
    """The (file, rule) counts that grew, and those that shrank."""
    grew: list[str] = []
    shrank: list[str] = []
    for path in sorted(set(current) | set(baseline)):
        for rule in sorted(set(current.get(path, {})) | set(baseline.get(path, {}))):
            now, then = current.get(path, {}).get(rule, 0), baseline.get(path, {}).get(rule, 0)
            if now > then:
                grew.append(f"{path} {rule}: {then} -> {now}")
            elif now < then:
                shrank.append(f"{path} {rule}: {then} -> {now}")
    return grew, shrank


def lint(update: bool) -> int:
    status = subprocess.run([*RUFF, "check", "."], cwd=ROOT).returncode
    current = pyright_counts()
    baseline: Counts = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    grew, shrank = compare(current, baseline)
    if grew:
        print("pyright: errors above the baseline (fix them; the baseline only shrinks):\n  " + "\n  ".join(grew))
        status = status or 1
    if update:
        # The first baseline is whatever the code has; from then on it only shrinks.
        if grew and BASELINE.exists():
            print("pyright: --update-baseline refuses a baseline that grows")
            return 1
        BASELINE.write_text(json.dumps(current, indent=2) + "\n")
        print(f"pyright: baseline {sum(sum(r.values()) for r in current.values())} errors ({BASELINE.name})")
    elif shrank:
        print("pyright: fewer errors than the baseline; run tools/check.py --update-baseline:\n  "
              + "\n  ".join(shrank))
        status = status or 1
    else:
        print(f"pyright: {sum(sum(r.values()) for r in current.values())} errors, as the baseline")
    return status


def tests() -> int:
    argv = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
    if shutil.which("slotfather") and not os.environ.get("SLOTFATHER_LEASE"):
        argv = ["slotfather", "run", "-n", "2", "--label", "lanefather-tests", "--", *argv]
    return subprocess.run(argv, cwd=ROOT).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lint", action="store_true", help="ruff and pyright only")
    parser.add_argument("--update-baseline", action="store_true", help="rewrite the pyright baseline (shrink only)")
    args = parser.parse_args()
    status = lint(args.update_baseline)
    if not args.lint and not args.update_baseline:
        status = tests() or status
    return status


if __name__ == "__main__":
    sys.exit(main())
