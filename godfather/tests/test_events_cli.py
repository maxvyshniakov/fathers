"""`godfather events` (fathers#83) under GODFATHER_FIXTURES: every present source's events merged, newest last, with
its tag; --source, --json and --follow; an unsupported source named on stderr; never an escape or a colour."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from helpers import ROOT

HOST = "mbp"


def event(source: str, kind: str, second: int, ident: str, **details: Any) -> str:
    return json.dumps({"v": 1, "time": f"2026-11-12T02:24:{second:02d}.000Z", "host": HOST, "source": source,
                       "kind": kind, "subject": {"type": "thing", "id": ident}, "actor": {"type": "tool"},
                       "details": details}) + "\n"


def capabilities(tool: str, tag: str, events: bool = True) -> dict[str, Any]:
    block: dict[str, Any] = {"v": 1, "tag": tag, "snapshot": {"argv": ["status", "--json"], "versions": [1]}}
    if events:
        block["events"] = {"path": f"/fixture/{tool}/events.jsonl", "versions": [1]}
    return {"v": 1, "tool": tool, "version": "0.9.0", "dashboard": block}


class EventsCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "host.json").write_text(json.dumps({"host": HOST}))
        for tool, tag in (("slotfather", "slot"), ("lanefather", "lane"), ("junkfather", "junk")):
            self.tool(tool, capabilities(tool, tag))
        self.log("slotfather", event("slotfather", "lease.granted", 10, "r1", waited=0)
                 + event("slotfather", "lease.released", 30, "r1", exit=0, ran=20))
        self.log("lanefather", event("lanefather", "lane.checked", 20, "710-speech", status="green",
                                     message="check 710-speech tree=abc green")
                 + event("lanefather", "lane.checked", 21, "711", status="red").replace(HOST, "vm110"))
        self.log("junkfather", event("junkfather", "entry.owned", 10, "j3f9a1c", ttl=7200)
                 + json.dumps({"v": 1, "kind": "\x1b[31mred"}) + "\n")

    def tool(self, name: str, answer: dict[str, Any] | None) -> None:
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        replies = {"capabilities --json": {"code": 0, "json": answer} if answer else {"code": 2, "stderr": "usage"}}
        (folder / "replies.json").write_text(json.dumps(replies))

    def log(self, name: str, text: str) -> None:
        with open(self.root / name / "events.jsonl", "a") as handle:
            handle.write(text)

    def environ(self) -> dict[str, str]:
        keep = {key: value for key, value in os.environ.items() if not key.startswith(("GODFATHER_", "NO_COLOR"))}
        return dict(keep, GODFATHER_FIXTURES=str(self.root), PYTHONPATH=str(ROOT / "src"), TZ="UTC")

    def run_events(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "godfather", "events", *argv], capture_output=True, text=True,
                              env=self.environ(), timeout=30)

    def test_every_source_merged_newest_last_with_its_tag(self) -> None:
        result = self.run_events()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(["junk", "slot", "lane", "slot"], [line.split()[2] for line in lines])
        self.assertEqual("2026-11-12 02:24:10  junk  entry.owned             j3f9a1c  ttl 2h00m", lines[0])
        self.assertIn("lane  lane.checked            710-speech  green", lines[2])
        self.assertTrue(lines[3].endswith("lease.released          r1  ok 20s"), lines[3])
        self.assertNotIn("\x1b", result.stdout, "no escape from a line, and no colour without a terminal")

    def test_source_filters_and_json_prints_the_lines_as_read(self) -> None:
        result = self.run_events("--source", "lanefather", "--source", "slotfather", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        found = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(["lease.granted", "lane.checked", "lease.released"], [item["kind"] for item in found])
        unknown = self.run_events("--source", "nothing")
        self.assertEqual(2, unknown.returncode)
        self.assertIn("--source takes slotfather, lanefather, junkfather", unknown.stderr)

    def test_an_absent_source_is_left_out_and_an_unsupported_one_named(self) -> None:
        for path in (self.root / "junkfather").iterdir():
            path.unlink()
        (self.root / "junkfather").rmdir()
        self.tool("lanefather", capabilities("lanefather", "lane", events=False))
        result = self.run_events()
        self.assertEqual(0, result.returncode)
        self.assertEqual(["slot", "slot"], [line.split()[2] for line in result.stdout.splitlines()])
        self.assertEqual("godfather: lanefather 0.9.0: no event log; update lanefather\n", result.stderr)

    def test_follow_prints_what_arrives_through_a_rotation(self) -> None:
        process = subprocess.Popen([sys.executable, "-m", "godfather", "events", "--follow", "--source", "slotfather"],
                                   stdout=subprocess.PIPE, text=True, env=self.environ())
        assert process.stdout is not None
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.assertIn("lease.granted", process.stdout.readline())
        self.assertIn("lease.released", process.stdout.readline())
        self.log("slotfather", event("slotfather", "request.left", 40, "r2", gave_up_after=300))
        log = self.root / "slotfather" / "events.jsonl"
        log.replace(log.with_name("events.jsonl.1"))
        self.log("slotfather", event("slotfather", "lease.granted", 50, "r3", waited=4))
        started = time.monotonic()
        later = [process.stdout.readline(), process.stdout.readline()]
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("request.left", later[0])
        self.assertIn("r3", later[1])


if __name__ == "__main__":
    unittest.main()
