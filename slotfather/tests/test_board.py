#!/usr/bin/env python3
"""The dashboard contracts (slotfather#26): `capabilities --json` and `status --json --full`, whose `dashboard` block
is the in-tree dashboard's model (board.dashboard_model()) from the same look at the queue as the status beside it."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import datetime
import importlib
import json
import os
import time
from typing import Any
from unittest import mock

from test_slotfather import ROOT, SlotCase

# Facts that move between two looks a moment apart: process samples, the clock and what is computed from it.
MOVING_TOP = ("updated", "history", "advice", "averages", "memory", "start", "reserved")
MOVING_ITEM = ("cpu", "rss", "procs", "tree", "est_start")


def encoded(value: Any) -> Any:
    """The block's encoding, written out here on its own: datetimes as RFC 3339 UTC with milliseconds, tuples as
    arrays."""
    if isinstance(value, datetime.datetime):
        utc = value.astimezone(datetime.timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
    if isinstance(value, dict):
        return {key: encoded(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encoded(item) for item in value]
    return value


def steady(block: dict[str, Any]) -> dict[str, Any]:
    found = {key: value for key, value in block.items() if key not in MOVING_TOP}
    for name in ("holders", "waiters"):
        found[name] = [{key: value for key, value in row.items() if key not in MOVING_ITEM} for row in block[name]]
    return found


class BoardTests(SlotCase):
    def setUp(self) -> None:
        super().setUp()
        self.env["SLOTFATHER_LOADAVG"] = "1"

    def full(self) -> dict[str, Any]:
        result = self.tool("status", "--json", "--full")
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def model(self) -> dict[str, Any]:
        with mock.patch.dict(os.environ, self.env, clear=True):
            return importlib.import_module("slotfather.board").dashboard_model()

    def fixture(self) -> tuple[Any, str]:
        """A running lease asked to yield (its marker written as a waiter writes it), a yieldable one, and a waiting
        entry with a marker of its own (#20: one pid's markers are read by entry, not by pid)."""
        holder = self.request("holder", 1, options=("--yieldable", "--priority", "batch", "--meta", "lane=l-1"))
        self.started("holder")
        other = self.request("other", 2)
        self.started("other")
        pid, start = self.holder()
        stem = f"{99:010d}-{pid}"
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))
        (self.state / "queue" / f"{stem}.json").write_text(json.dumps(
            {"pid": pid, "start": start, "count": 4, "label": "waiter", "cmd": "fake", "since": since,
             "state": "waiting", "priority": "interactive"}))
        held = next(path for path in (self.state / "queue").glob("*.json")
                    if json.loads(path.read_text())["label"] == "holder")
        asked = {"by": stem, "label": "waiter", "priority": "interactive", "count": 4,
                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        held.with_suffix(".yield").write_text(json.dumps(asked))
        (self.state / "queue" / f"{stem}.yield").write_text(json.dumps(dict(asked, label="stray")))
        self.addCleanup(lambda: [self.release(label) for label in ("holder", "other")])
        return (holder, other), stem

    def test_the_dashboard_block_is_the_in_tree_model(self) -> None:
        processes, _ = self.fixture()
        block = self.full()["dashboard"]
        model = self.model()
        self.assertEqual(1, block.pop("v"))
        self.assertEqual(0.0, block.pop("overtake_window"), "SLOTFATHER_OVERTAKE_WINDOW=0 in the fixture")
        memory = block["memory"]
        self.assertEqual((False, False), (memory.pop("low"), memory.pop("swap_low")))
        for row in model["holders"] + model["waiters"]:
            self.assertIn(row.pop("style"), ("text", "legacy"), "presentation stays out of the block")
        self.assertEqual(steady(encoded(model)), steady(block))
        self.assertEqual(["holder", "other"], [row["label"] for row in block["holders"]])
        self.assertEqual(["waiter"], [row["label"] for row in block["waiters"]])
        holder = block["holders"][0]
        self.assertEqual(("waiter", True, "batch", {"lane": "l-1"}),
                         (holder["asked"], holder["yieldable"], holder["priority"], holder["context"]))
        self.assertRegex(holder["asked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertIsInstance(holder["key"], list)
        self.assertTrue(all(isinstance(row, list) and len(row) == 4 for row in holder["tree"]))
        self.assertTrue(all(isinstance(step, list) and len(step) == 3 for step in block["history"]))
        for label in ("holder", "other"):
            self.release(label)
        for process in processes:
            self.finish(process)

    def test_status_and_its_block_read_one_look(self) -> None:
        """#20: each entry's yield request and idle mark are the same in the status and in the block."""
        processes, stem = self.fixture()
        found = self.full()
        rows = {(row["pid"], row["key"][1]): row for row in found["dashboard"]["holders"] + found["dashboard"]["waiters"]}
        self.assertEqual(3, len(found["entries"]))
        for entry in found["entries"]:
            row = rows[(entry["pid"], entry["since"])]
            self.assertEqual((entry["yield_asked"] or {}).get("label") or "", row["asked"], entry["label"])
            self.assertEqual(entry["idle_since"] is None, row["idle_since"] is None)
            self.assertEqual(entry["state"] == "held", row["kind"] == "held")
        waiter = next(entry for entry in found["entries"] if entry["id"] == stem)
        self.assertEqual("stray", waiter["yield_asked"]["label"])
        for label in ("holder", "other"):
            self.release(label)
        for process in processes:
            self.finish(process)

    def test_full_status_is_the_status_older_readers_read_and_more(self) -> None:
        holder = self.request("holder", 1)
        self.started("holder")
        full = self.full()
        plain = json.loads(self.tool("status", "--json").stdout)
        self.assertEqual(sorted(plain) + ["dashboard", "host", "time"], sorted(full))
        self.assertEqual(2, full["v"])
        self.assertRegex(full["host"], r"^[a-z0-9-]+$")
        self.assertRegex(full["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        moving = ("idle_since", "waited", "est_start", "tree")
        self.assertEqual([{k: v for k, v in e.items() if k not in moving} for e in plain["entries"]],
                         [{k: v for k, v in e.items() if k not in moving} for e in full["entries"]])
        self.assertEqual(sorted(plain["entries"][0]), sorted(full["entries"][0]), "no entry field added")
        self.release("holder")
        self.finish(holder)

    def test_capabilities_name_the_snapshot_and_the_changelog(self) -> None:
        result = self.tool("capabilities", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        found = json.loads(result.stdout)
        self.assertEqual((1, "slotfather"), (found["v"], found["tool"]))
        version = importlib.import_module("slotfather.version").VERSION
        self.assertEqual(version, found["version"])
        dashboard = found["dashboard"]
        self.assertEqual((1, "slot"), (dashboard["v"], dashboard["tag"]))
        self.assertEqual({"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1}, dashboard["snapshot"])
        self.assertEqual(str(ROOT / "CHANGELOG.md"), dashboard["changelog"])
        self.assertIn("reprioritize", dashboard["actions"])
        self.assertEqual(2, self.tool("capabilities").returncode, "only --json")
