#!/usr/bin/env python3
"""events.jsonl in Godfather's event schema (slotfather#29): real commands in a temporary queue, every line valid in
the vendored validator's strict mode, one event for each queue.log line, and queue.log as it always was."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
_path_sys.path.insert(0, str(_Path(__file__).resolve().parent / "vendor"))
from slotfather import events as sf_events

import hashlib
import json
import os
import re
import stat
import time
import unittest
from typing import Any
from unittest import mock

import eventcheck  # tests/vendor/eventcheck.py, Godfather's validator, vendored unchanged
from test_classes import YIELDER
from test_slotfather import TOOL, SlotCase

VENDOR = _Path(__file__).resolve().parent / "vendor"
# queue.log's lines, by their first words, and the event kinds that mirror them
MIRROR = (("granted ", ("lease.granted",)), ("released ", ("lease.released", "request.left")),
          ("reaped ", ("lease.reaped", "request.reaped")), ("reprioritized ", ("request.reprioritized",)),
          ("waiting long: ", ("request.waiting_long",)), ("asked ", ("lease.yield_asked",)),
          ("withdrew ", ("lease.yield_withdrawn",)), ("yielded ", ("lease.yielded",)),
          ("sweep ", ("process.swept",)))
QUEUE_LOG = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ (granted \d+ to .* \(pid \d+\)|released \d+ \(\d+-\d+\.json\)|"
                       r"reaped (held|waiting) lease of \d+ for .* \(pid \d+ exited\)|reprioritized .* \(pid \d+\) "
                       r"from \w+ to \w+|waiting long: .*|asked .* \(pid \d+\) to yield \d+ for .*|withdrew the yield "
                       r"request to .* \(pid \d+\)|yielded \d+ \(\d+-\d+\.json\) to .*)$")


class VendoredValidatorTests(unittest.TestCase):
    def test_the_vendored_validator_is_the_recorded_file(self) -> None:
        data = (VENDOR / "eventcheck.py").read_bytes()
        self.assertEqual((VENDOR / "eventcheck.py.sha256").read_text().strip(), hashlib.sha256(data).hexdigest())

    @unittest.skipUnless(os.environ.get("GODFATHER_DIR"), "GODFATHER_DIR names no Godfather checkout")
    def test_the_vendored_validator_is_godfathers(self) -> None:
        canonical = _Path(os.environ["GODFATHER_DIR"]) / "src" / "godfather" / "eventcheck.py"
        self.assertEqual(canonical.read_bytes(), (VENDOR / "eventcheck.py").read_bytes(),
                         "copy it again and record its new SHA-256 in tests/vendor/eventcheck.py.sha256")


class EventLogTests(SlotCase):
    def setUp(self) -> None:
        super().setUp()
        self.env["TOOL"] = TOOL

    def lines(self) -> list[dict[str, Any]]:
        path = self.state / "events.jsonl"
        self.assertEqual([], eventcheck.validate_file(str(path), strict=True))
        return [json.loads(line) for line in path.read_text().splitlines()]

    def kinds(self) -> list[str]:
        path = self.state / "events.jsonl"
        return [json.loads(line)["kind"] for line in path.read_text().splitlines()] if path.exists() else []

    def assert_mirrors_queue_log(self, events: list[dict[str, Any]]) -> None:
        """Every queue.log line, in order, has its event, and queue.log's lines keep their exact form."""
        logged = (self.state / "queue.log").read_text().splitlines()
        for line in logged:
            self.assertRegex(line, QUEUE_LOG)
        wanted = [next(kinds for prefix, kinds in MIRROR if line.split(" ", 1)[1].startswith(prefix)) for line in logged]
        mirrored = [event["kind"] for event in events if event["kind"] not in ("capacity.changed", "order.changed")]
        self.assertEqual(len(wanted), len(mirrored), (logged, mirrored))
        for kinds, kind in zip(wanted, mirrored):
            self.assertIn(kind, kinds)

    def test_run_release_reap_reprioritize_yield_and_a_long_wait_validate_strictly(self) -> None:
        self.assertEqual(0, self.tool("order", "--yield-after", "1").returncode)
        gate = self.request("gate", 4, "sh", "-c", YIELDER, "work", "gate",
                            options=("--priority", "batch", "--yieldable", "--meta", "lane=l-9"))
        self.started("gate")
        time.sleep(1.1)
        quick = self.request("quick", 1, options=("--priority", "interactive"))
        self.started("quick")
        late = self.request("late", 4)
        self.wait_for(lambda: "waiting: 4 slot(s) late" in self.status(), "late to queue")
        self.assertEqual(0, self.tool("reprioritize", "late", "interactive", GODFATHER_ACTOR="godfather/0.1.0").returncode)
        self.release("quick")
        self.wait_for(lambda: "resume gate" in self.events(), "the gate to resume")
        self.release("gate")
        self.started("late")
        self.release("late")
        for process in (gate, quick, late):
            self.finish(process)
        # A lease whose holder died, reaped by the next look
        pid, start = self.holder()
        (self.state / "queue" / f"{90:010d}-{pid}.json").write_text(json.dumps(
            {"pid": pid, "start": start + "x", "count": 1, "label": "ghost", "cmd": "fake", "since": "2026-11-12T00:00:00Z",
             "state": "held", "granted": "2026-11-12T00:00:10Z"}))
        self.tool("status")
        # A long wait: a waiter that says it has waited
        holder = self.request("hog", 4)
        self.started("hog")
        waiter = self.request("patient", 1, options=("--priority", "batch"))
        self.env["SLOTFATHER_WARN_WAIT"] = "1"
        waiter_long = self.request("impatient", 1)
        self.wait_for(lambda: "request.waiting_long" in self.kinds(), "a long wait")
        self.release("hog")
        for label in ("patient", "impatient"):
            self.release(label)
        for process in (holder, waiter, waiter_long):
            self.finish(process)
        self.assertEqual(0, self.tool("slots", "5").returncode)
        self.assertEqual(0, self.tool("order", "--safety", "4").returncode)

        events = self.lines()
        kinds = [event["kind"] for event in events]
        for kind in ("lease.granted", "lease.released", "lease.reaped", "request.reprioritized", "request.waiting_long",
                     "lease.yield_asked", "lease.yielded", "capacity.changed", "order.changed"):
            self.assertIn(kind, kinds)
        self.assertTrue(set(kinds) <= set(sf_events.KINDS), set(kinds) - set(sf_events.KINDS))
        self.assert_mirrors_queue_log(events)
        mode = stat.S_IMODE((self.state / "events.jsonl").stat().st_mode)
        self.assertEqual(0o600, mode)

        by_kind: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            by_kind.setdefault(event["kind"], []).append(event)
        granted = next(e for e in by_kind["lease.granted"] if e["subject"]["label"] == "gate")
        self.assertEqual(("slotfather", "request", "batch", {"lane": "l-9"}, "l-9"),
                         (granted["source"], granted["subject"]["type"], granted["details"]["priority"],
                          granted["details"]["meta"], granted["actor"]["lane"]))
        self.assertRegex(granted["subject"]["id"], r"^\d{10}-\d+$")
        released = next(e for e in by_kind["lease.released"] if e["subject"]["label"] == "quick")
        self.assertEqual(0, released["details"]["exit"])
        self.assertIsInstance(released["details"]["ran"], int)
        self.assertIn("rss_peak_mb", released["details"])
        moved = by_kind["request.reprioritized"][0]
        self.assertEqual(({"from": "normal", "to": "interactive", "state": "waiting"}, "godfather"),
                         (moved["details"], moved["actor"]["via"]))
        reaped = by_kind["lease.reaped"][0]
        self.assertEqual(("warn", "ghost", "owner exited"),
                         (reaped["level"], reaped["subject"]["label"], reaped["details"]["reason"]))
        long = by_kind["request.waiting_long"][0]
        self.assertEqual("warn", long["level"])
        self.assertIsInstance(long["details"]["waited"], int)
        asked = by_kind["lease.yield_asked"][0]
        self.assertEqual(("gate", "quick"), (asked["subject"]["label"], asked["details"]["for"]["label"]))
        self.assertEqual("quick", by_kind["lease.yielded"][0]["details"]["to"]["label"])
        self.assertEqual(5, by_kind["capacity.changed"][0]["details"]["slots"])
        self.assertEqual({"saved": {"safety": 4.0}}, by_kind["order.changed"][-1]["details"])

    def test_a_waiter_that_gives_up_has_left(self) -> None:
        holder = self.request("hog", 4)
        self.started("hog")
        result = self.tool("run", "--wait", "0.3", "--label", "brief", "--", "true")
        self.assertEqual(75, result.returncode)
        self.release("hog")
        self.finish(holder)
        left = [e for e in self.lines() if e["kind"] == "request.left"]
        self.assertEqual(["brief"], [e["subject"]["label"] for e in left])
        self.assertIsInstance(left[0]["details"]["gave_up_after"], int)

    def test_a_write_that_fails_never_fails_the_operation(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / "events.jsonl").mkdir()  # nothing can be appended there
        result = self.tool("run", "--label", "fine", "--", "true")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("granted 1 to fine", (self.state / "queue.log").read_text())

    def test_strings_lose_control_characters_and_long_lines_their_details(self) -> None:
        line = sf_events.line("lease.granted", {"type": "request", "id": "1-2", "label": "a\x1b[31mb"},
                              {"meta": {"cmd": "x" * 20000}}, sf_events.actor())
        event = json.loads(line)
        self.assertEqual([], eventcheck.validate(event, strict=True))
        self.assertEqual("a [31mb", event["subject"]["label"])
        self.assertEqual({"truncated": True}, event["details"])
        self.assertLessEqual(len(line), sf_events.LINE_MAX)

    def test_the_log_rotates_past_its_size_under_its_lock(self) -> None:
        self.state.mkdir(parents=True)
        with mock.patch.object(sf_events, "EVENTS_MAX", 600), mock.patch.dict(os.environ, self.env, clear=True):
            for n in range(8):
                sf_events.emit(self.state, "order.changed", {"type": "order", "id": "order"}, {"n": n})
        rotated = self.state / "events.jsonl.1"
        self.assertTrue(rotated.exists())
        self.assertTrue((self.state / "events.lock").exists())
        for path in (rotated, self.state / "events.jsonl"):
            if path.stat().st_size:
                self.assertEqual([], eventcheck.validate_file(str(path), strict=True))

    def test_capabilities_name_the_log(self) -> None:
        found = json.loads(self.tool("capabilities", "--json").stdout)["dashboard"]["events"]
        self.assertEqual({"path": str(self.state / "events.jsonl"), "versions": [1]}, found)
