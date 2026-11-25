"""Godfather's command line against the CLI convention (docs/cli.md, common/cliconvention.py; fathers#118).

EXPECTED lists the cases that fail today, each with its ticket: none since fathers#154.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))

import cliconvention  # common/cliconvention.py
from cliconvention import Command

PACKAGE = Path(__file__).resolve().parents[1]
HOST = "test-host"

# `events` runs against a minimal fixture log (prepare()), so `--json` (JSON Lines) and `--source=slotfather` are
# checked on real events rather than empty output.
TOOL = cliconvention.Tool("godfather", (sys.executable, str(PACKAGE / "godfather")),
                          (Command("events", json=(), jsonl=True, option=("--source", "slotfather"),
                                   shows="lease.granted"),))
EXPECTED: dict[str, str] = {}


def _write_fixture(root: Path) -> None:
    (root / "host.json").write_text(json.dumps({"host": HOST}))
    for name, tag, kind, ident in (("slotfather", "slot", "lease.granted", "r1"),
                                   ("lanefather", "lane", "lane.checked", "l1")):
        folder = root / name
        folder.mkdir()
        caps = {
            "v": 1, "tool": name, "version": "0.9.0",
            "dashboard": {
                "v": 1, "tag": tag,
                "snapshot": {"argv": ["status", "--json"], "versions": [1]},
                "events": {"path": f"/fixture/{name}/events.jsonl", "versions": [1]},
            },
        }
        (folder / "replies.json").write_text(json.dumps({"capabilities --json": {"code": 0, "json": caps}}))
        event = {
            "v": 1, "time": "2026-11-12T02:24:10.000Z", "host": HOST, "source": name,
            "kind": kind, "subject": {"type": "thing", "id": ident}, "actor": {"type": "tool"}, "details": {},
        }
        (folder / "events.jsonl").write_text(json.dumps(event) + "\n")


class CLIConvention(cliconvention.conformance(TOOL, expected=EXPECTED)):
    def prepare(self, tool: cliconvention.Tool) -> cliconvention.Tool:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        fixtures = root / "fixtures"
        fixtures.mkdir()
        _write_fixture(fixtures)
        environment = dict(os.environ, XDG_CONFIG_HOME=str(root), XDG_STATE_HOME=str(root),
                           GODFATHER_FIXTURES=str(fixtures), NO_COLOR="1", PATH=str(root))
        return cliconvention.Tool(tool.name, tool.argv, tool.commands, environment, str(root))


if __name__ == "__main__":
    unittest.main()
