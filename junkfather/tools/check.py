#!/usr/bin/env python3
"""The quality gate: Ruff and Pyright at the versions pinned in requirements-dev.txt (through uv), with no budget:
every diagnostic fails. Run it through a machine slot: slotfather run -n 1 -- python3 tools/check.py"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    pins = dict(line.strip().split("==") for line in (ROOT / "requirements-dev.txt").read_text().splitlines()
                if "==" in line)
    failed = 0
    for tool, args in (("ruff", ["check", "."]), ("pyright", [])):
        command = ["uvx", f"{tool}@{pins[tool]}", *args]
        print("+", " ".join(command), flush=True)
        failed |= subprocess.run(command, cwd=ROOT).returncode
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
