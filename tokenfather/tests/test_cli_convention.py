"""Tokenfather's command line against the CLI convention (docs/cli.md, common/cliconvention.py)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))

import cliconvention  # pyright: ignore[reportMissingImports]
from cliconvention import Command  # pyright: ignore[reportMissingImports]

PACKAGE = Path(__file__).resolve().parents[1]

TOOL = cliconvention.Tool(
    "tokenfather",
    (sys.executable, "-m", "tokenfather"),
    (
        Command("capabilities", json=()),
        Command("changelog", json=(), option=("--since", "0.1.0")),
        Command("collect", json=()),
        Command("report", json=("--no-collect",)),
    ),
)
EXPECTED: dict[str, str] = {}


class CLIConvention(cliconvention.conformance(TOOL, expected=EXPECTED)):  # pyright: ignore[reportUntypedBaseClass]
    def prepare(self, tool: cliconvention.Tool) -> cliconvention.Tool:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith(("TOKENFATHER_", "LANEFATHER_"))
        }
        environment.update(
            TOKENFATHER_STATE=str(root / "state"),
            TOKENFATHER_EVENTS=str(root / "events.jsonl"),
            HOME=str(root),
            PYTHONPATH=str(PACKAGE),
            NO_COLOR="1",
        )
        return cliconvention.Tool(tool.name, tool.argv, tool.commands, environment, str(root))


if __name__ == "__main__":
    unittest.main()
