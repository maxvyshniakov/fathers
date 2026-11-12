#!/usr/bin/env python3
"""`slotfather settings` (slotfather#27): every flow of the Settings panel through the CLI, in an isolated queue:
reading, a pending value's preview, an invalid value, a conflict, a read-only queue, a reset of one and of all."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import cli as sf_cli

import contextlib
import io
import json
import os
from typing import Any
from unittest import mock

from test_slotfather import SlotCase


def rows(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["key"]: row for section in document["sections"] for row in section["settings"]}


class SettingsCliTests(SlotCase):
    def settings(self, *argv: str, code: int = 0, **env: str) -> dict[str, Any]:
        result = self.tool("settings", *argv, "--json", **env)
        self.assertEqual(code, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_it_lists_every_panel_row_with_its_status(self) -> None:
        document = self.settings()
        self.assertEqual((1, "slotfather", True, None),
                         (document["v"], document["tool"], document["writable"], document["reason"]))
        self.assertEqual(["capacity.mode", "capacity.slots", "capacity.gib_per_slot", "order.order", "order.safety",
                          "order.fallback", "order.overrun", "order.limit", "order.interactive", "order.batch",
                          "order.aging_batch", "order.aging_normal", "order.warn_wait", "order.idle_cpu",
                          "order.idle_after", "order.yield_after"], list(rows(document)))
        status = {row["label"]: row["value"] for row in document["status"]}
        self.assertEqual("4 total · 4 free · 0 held · 0 waiting", status["slots"])
        self.assertEqual("4 slots · automatic, lower of the two budgets", status["capacity"])
        self.assertEqual("SLOTFATHER_CORES, SLOTFATHER_MEMORY_GIB", status["environment"])
        found = rows(document)
        self.assertEqual(("automatic", "default"), (found["capacity.mode"]["value"], found["capacity.mode"]["source"]))
        self.assertEqual({"capacity.mode": ["fixed"]}, found["capacity.slots"]["when"])
        self.assertEqual(48, found["capacity.slots"]["max"], "4 slots per core of 12")
        warn = found["order.warn_wait"]
        self.assertEqual(("duration", 1800.0, 1800.0, "never", "s", "default"),
                         (warn["type"], warn["value"], warn["default"], warn["zero"], warn["unit"], warn["source"]))
        self.assertEqual(4, document["basis"]["slots"])

    def test_the_environment_tightening_a_value_is_its_effective_value_and_source(self) -> None:
        found = rows(self.settings(SLOTFATHER_BACKFILL_SAFETY="5"))["order.safety"]
        self.assertEqual((3.0, 5.0, "env SLOTFATHER_BACKFILL_SAFETY"),
                         (found["value"], found["effective"], found["source"]))
        self.assertNotIn("effective", rows(self.settings(SLOTFATHER_BACKFILL_SAFETY="2"))["order.safety"],
                         "a looser value does not apply")

    def test_check_previews_what_a_capacity_would_do_and_saves_nothing(self) -> None:
        holder = self.request("holder", 3)
        self.started("holder")
        self.assertEqual({"ok": True, "error": None, "preview": "→ 2 slots · will drain 1: running work keeps its "
                          "slots", "slots": 2}, self.settings("check", "capacity.slots", "2"))
        waiter = self.request("waiter", 2)
        self.wait_for(lambda: "waiting: 2 slot(s) waiter" in self.status(), "the waiter to queue")
        self.assertEqual("→ 6 slots · will start 1 waiting", self.settings("check", "capacity.slots", "6")["preview"])
        self.assertEqual("→ 1 slot · will drain 2: running work keeps its slots",
                         self.settings("check", "capacity.mode", "memory", SLOTFATHER_MEMORY_GIB="4")["preview"])
        self.assertFalse((self.state / "capacity.json").exists())
        self.release("holder")
        self.release("waiter")
        for process in (holder, waiter):
            self.finish(process)

    def test_invalid_values_say_why_in_the_panels_words(self) -> None:
        for key, value, error in (("capacity.slots", "0", "Enter a whole number of slots, at least 1."),
                                  ("capacity.slots", "49", "At most 48 slots (4 per core)."),
                                  ("order.safety", "x", "Enter a number."),
                                  ("order.safety", "0.5", "Enter a number of at least 1."),
                                  ("capacity.gib_per_slot", "2", "GiB per slot applies in the Memory and CPU + "
                                   "memory modes; set capacity.mode first."),
                                  ("order.nothing", "1", "no setting 'order.nothing'; `slotfather settings --json` "
                                   "lists them")):
            with self.subTest(key=key, value=value):
                self.assertEqual({"ok": False, "error": error, "preview": None},
                                 self.settings("check", key, value, code=2))
                self.assertEqual({"ok": False, "error": error}, self.settings("set", key, value, code=2))
        self.assertEqual([], sorted(p.name for p in self.state.glob("*.json")) if self.state.exists() else [])

    def test_set_saves_through_the_queue_and_answers_with_the_settings(self) -> None:
        document = self.settings("set", "capacity.slots", "6")
        self.assertEqual((True, "Saved · capacity 6 slots"), (document["ok"], document["message"]))
        self.assertEqual(("fixed", 6, "saved"), (rows(document)["capacity.mode"]["value"],
                                                 rows(document)["capacity.slots"]["value"],
                                                 rows(document)["capacity.mode"]["source"]))
        self.assertEqual("6 slots (saved)", self.tool("slots").stdout.strip())
        document = self.settings("set", "capacity.mode", "memory")
        self.assertEqual(("memory", 4.0), (document["basis"]["mode"], document["basis"]["gib_per_slot"]))
        document = self.settings("set", "capacity.gib_per_slot", "8")
        self.assertEqual((8, 8), (document["basis"]["gib_per_slot"], document["basis"]["slots"]))
        document = self.settings("set", "order.warn_wait", "30m")
        self.assertEqual("Saved · Warn after waiting 1800 s", document["message"])
        self.assertEqual("saved", rows(document)["order.warn_wait"]["source"])
        self.assertEqual("Saved · Queue order Arrival", self.settings("set", "order.order", "window")["message"])
        self.assertEqual("Saved · Overtaking stops after never", self.settings("set", "order.limit", "0")["message"])
        self.assertIn("window: safety 3", self.tool("order").stdout)

    def test_a_stale_revision_is_a_conflict_and_answers_with_the_current_settings(self) -> None:
        token = rows(self.settings())["capacity.slots"]["revision"]
        self.assertEqual("absent", token)
        self.assertEqual(0, self.tool("slots", "5").returncode)  # another client meanwhile
        document = self.settings("set", "capacity.slots", "3", "--revision", token, code=3)
        self.assertEqual((False, True, 5), (document["ok"], document["conflict"], document["basis"]["slots"]))
        self.assertIn("capacity.json changed in another process; nothing was saved", document["error"])
        fresh = rows(document)["capacity.slots"]["revision"]
        self.assertTrue(fresh.startswith("sha256:"))
        self.assertEqual(3, self.settings("set", "capacity.slots", "3", "--revision", fresh)["basis"]["slots"])
        order = rows(self.settings())["order.safety"]["revision"]
        self.assertEqual(0, self.tool("order", "--safety", "4").returncode)
        self.settings("set", "order.safety", "2", "--revision", order, code=3)
        self.assertEqual(2.0, rows(self.settings("set", "order.safety", "2"))["order.safety"]["value"],
                         "without a revision order settings merge")

    def test_reset_one_setting_or_all(self) -> None:
        self.settings("set", "capacity.slots", "6")
        self.settings("set", "order.batch", "60")
        self.settings("set", "order.safety", "4")
        document = self.settings("reset", "order.batch")
        self.assertEqual((1800.0, 4.0), (rows(document)["order.batch"]["value"], rows(document)["order.safety"]["value"]))
        self.assertEqual("fixed", document["basis"]["mode"])
        self.assertEqual("automatic", self.settings("reset", "capacity.slots")["basis"]["mode"])
        document = self.settings("reset", "--all")
        self.assertEqual("Saved · every setting at its default, capacity 4 slots", document["message"])
        self.assertFalse((self.state / "order.json").exists())

    def test_another_users_queue_is_read_only(self) -> None:
        self.settings("set", "capacity.slots", "6")
        before = sorted(p.name for p in self.state.rglob("*"))
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(os, "geteuid", return_value=os.geteuid() + 4242):
            for argv, code in ((["settings", "--json"], 0), (["settings", "set", "capacity.slots", "3", "--json"], 4),
                               (["settings", "reset", "--all", "--json"], 4)):
                with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(code, sf_cli.main(argv))
                reply = json.loads(out.getvalue())
                if code:
                    self.assertEqual(False, reply["ok"])
                    self.assertIn("belongs to uid", reply["error"])
                else:
                    self.assertFalse(reply["writable"])
                    self.assertIn("belongs to uid", reply["reason"])
                    self.assertFalse(any(row["writable"] for row in rows(reply).values()))
        self.assertEqual(before, sorted(p.name for p in self.state.rglob("*")))
        self.assertEqual("6 slots (saved)", self.tool("slots").stdout.strip())

    def test_capabilities_name_the_settings(self) -> None:
        found = json.loads(self.tool("capabilities", "--json").stdout)
        self.assertEqual({"versions": [1]}, found["dashboard"]["settings"])

    def test_usage_needs_json(self) -> None:
        self.assertEqual(2, self.tool("settings").returncode)
        self.assertEqual(2, self.tool("settings", "set", "capacity.slots", "--json").returncode)
