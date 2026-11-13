"""`junkfather settings` (Godfather's settings contract, godfather/SPEC.md "Settings contract"; fathers#75): the
schema, check/set/reset, a stale revision, read-only environment rows, and own, sweep and reap reading the saved
values at the point of use. Every command runs on the test's scratch state directory."""
from __future__ import annotations

import json
import plistlib
from typing import Any, Dict, List
import unittest
from unittest import mock

from support import Case, as_entry

from junkfather import procs, reap, resources, settings, sweep

A = "Thu Nov 12 10:00:00 2026"


class SettingsCase(Case):
    def settings(self, *args: str, code: int = 0) -> Dict[str, Any]:
        result = self.cli("settings", *args, "--json")
        self.assertEqual(code, result.returncode, result.stdout + result.stderr)
        found: Dict[str, Any] = json.loads(result.stdout)
        return found

    def rows(self, found: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {row["key"]: row for section in found["sections"] for row in section["settings"]}


class SchemaTests(SettingsCase):
    def test_the_schema_lists_the_three_settings_at_their_defaults(self):
        found = self.settings()
        self.assertEqual((1, "junkfather", True, None), (found["v"], found["tool"], found["writable"], found["reason"]))
        self.assertEqual(["own", "sweep", "reap", "environment"], [s["id"] for s in found["sections"]])
        rows = self.rows(found)
        expected = {"own.default_ttl": (7200, 60), "sweep.interval": (3600, 60), "reap.grace": (10, 1)}
        for key, (default, least) in expected.items():
            row = rows[key]
            self.assertEqual(("duration", default, default, "default", True, "s", least),
                             (row["type"], row["value"], row["default"], row["source"], row["writable"], row["unit"],
                              row["min"]), key)
            self.assertEqual(found["revision"], row["revision"])
            for field in ("label", "max", "step", "description"):
                self.assertIn(field, row)
        self.assertNotIn("sweep.session_age", rows, "no such tunable exists (fathers#75, Owner questions)")
        self.assertFalse((self.state / "settings.json").exists(), "reading saves nothing")

    def test_environment_overrides_are_read_only_rows_with_their_source(self):
        rows = self.rows(self.settings())
        self.assertEqual((str(self.state), "env JUNKFATHER_STATE", False),
                         (rows["env.JUNKFATHER_STATE"]["value"], rows["env.JUNKFATHER_STATE"]["source"],
                          rows["env.JUNKFATHER_STATE"]["writable"]))
        self.assertEqual("env JUNKFATHER_CLAUDE_SESSIONS", rows["env.JUNKFATHER_CLAUDE_SESSIONS"]["source"])
        self.assertEqual("env JUNKFATHER_LANE_RECORDS", rows["env.JUNKFATHER_LANE_RECORDS"]["source"])
        unset = rows["env.JUNKFATHER_SCRATCH_ROOTS"]
        self.assertEqual(("default", False), (unset["source"], unset["writable"]))
        self.assertEqual({"env.JUNKFATHER_STATE", "env.JUNKFATHER_SCRATCH_ROOTS", "env.JUNKFATHER_CLAUDE_SESSIONS",
                          "env.JUNKFATHER_LANE_RECORDS"}, {k for k in rows if k.startswith("env.")})

    def test_without_json_it_prints_the_settings_for_people(self):
        result = self.cli("settings")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("own.default_ttl = 2h  [default]", result.stdout)
        self.assertIn("env.JUNKFATHER_STATE", result.stdout)
        self.assertIn("(read-only)", result.stdout)

    def test_capabilities_offer_settings_version_1(self):
        result = self.cli("capabilities", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"versions": [1]}, json.loads(result.stdout)["dashboard"]["settings"])


class ChangeTests(SettingsCase):
    def test_check_set_and_reset_round_trip(self):
        checked = self.settings("check", "own.default_ttl", "1h")
        self.assertEqual((True, None), (checked["ok"], checked["error"]))
        self.assertIn("60m", checked["preview"])
        self.assertFalse((self.state / "settings.json").exists(), "check saves nothing")

        saved = self.settings("set", "own.default_ttl", "1h")
        self.assertEqual((True, None), (saved["ok"], saved["error"]))
        row = self.rows(saved)["own.default_ttl"]
        self.assertEqual((3600, "saved"), (row["value"], row["source"]))
        self.assertEqual({"v": 1, "settings": {"own.default_ttl": 3600}},
                         json.loads((self.state / "settings.json").read_text()))
        self.assertEqual(3600, self.rows(self.settings())["own.default_ttl"]["value"])

        self.settings("set", "reap.grace", "1")
        self.settings("set", "sweep.interval", "2h")
        reset = self.settings("reset", "own.default_ttl")
        rows = self.rows(reset)
        self.assertEqual((7200, "default"), (rows["own.default_ttl"]["value"], rows["own.default_ttl"]["source"]))
        self.assertEqual((1, 7200), (rows["reap.grace"]["value"], rows["sweep.interval"]["value"]))
        everything = self.rows(self.settings("reset", "--all"))
        self.assertEqual(["default"] * 3, [everything[k]["source"] for k in ("own.default_ttl", "sweep.interval",
                                                                            "reap.grace")])

    def test_invalid_values_and_keys_exit_2_and_save_nothing(self):
        for args in (("check", "sweep.interval", "30"), ("check", "own.default_ttl", "soon"),
                     ("set", "reap.grace", "-1"), ("set", "sweep.interval", "90.5"), ("set", "own.default_ttl", "0"),
                     ("set", "no.such", "1"), ("set", "own.default_ttl"), ("reset",), ("reset", "reap.grace", "--all")):
            found = self.settings(*args, code=2)
            self.assertEqual((False, None), (found["ok"], found["preview"]), args)
            self.assertTrue(found["error"], args)
        self.assertFalse((self.state / "settings.json").exists())

    def test_a_grace_under_a_second_and_a_ttl_under_a_minute_are_refused(self):
        for key, text in (("reap.grace", "0"), ("reap.grace", "0.5"), ("own.default_ttl", "59"),
                          ("own.default_ttl", "1")):
            for action in ("check", "set"):
                found = self.settings(action, key, text, code=2)
                self.assertIn("must be from", found["error"], (key, text))
        self.settings("set", "reap.grace", "1s")
        rows = self.rows(self.settings("set", "own.default_ttl", "1m"))
        self.assertEqual((1, 60), (rows["reap.grace"]["value"], rows["own.default_ttl"]["value"]))

    def test_a_saved_value_below_the_minimum_is_not_used_and_says_so_once(self):
        self.state.mkdir(parents=True)
        (self.state / "settings.json").write_text(json.dumps({"v": 1, "settings": {"reap.grace": 0}}))
        settings.forget_warnings()
        with mock.patch("sys.stderr") as stderr:
            self.assertEqual(10, settings.value("reap.grace", self.store.root))
            self.assertEqual(10, settings.value("reap.grace", self.store.root))
        written = "".join(str(call.args[0]) for call in stderr.write.call_args_list)
        self.assertEqual(1, written.count("using the defaults"), written)
        self.assertIn("reap.grace", written)

    def test_an_unreadable_file_is_reported_by_a_command_that_uses_the_setting(self):
        self.state.mkdir(parents=True)
        (self.state / "settings.json").write_text("{not json")
        result = self.cli("sweep", "--plist")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, result.stderr.count("settings.json: is not JSON"), result.stderr)
        self.assertEqual(3600, plistlib.loads(result.stdout.encode())["StartInterval"])

    def test_a_failed_write_that_is_not_a_permission_problem_is_not_read_only(self):
        store = self.store
        full = OSError(28, "No space left on device")
        with mock.patch.object(settings, "write", side_effect=full), mock.patch("sys.stdout"):
            code = settings.command(store, "set", "reap.grace", "5", False, None, True)
        self.assertEqual(settings.FAILED, code)
        denied = PermissionError(13, "Permission denied")
        with mock.patch.object(settings, "write", side_effect=denied), mock.patch("sys.stdout"):
            code = settings.command(store, "set", "reap.grace", "5", False, None, True)
        self.assertEqual(settings.READ_ONLY, code)

    def test_a_stale_revision_is_a_conflict_that_returns_the_current_settings(self):
        before = self.settings()["revision"]
        self.settings("set", "reap.grace", "5", "--revision", before)
        found = self.settings("set", "reap.grace", "3", "--revision", before, code=3)
        self.assertEqual((False, True), (found["ok"], found["conflict"]))
        self.assertIn("changed", found["error"])
        self.assertEqual(5, self.rows(found)["reap.grace"]["value"], "the reply is the current settings")
        self.settings("reset", "reap.grace", "--revision", before, code=3)
        current = found["revision"]
        self.assertNotEqual(before, current)
        self.assertEqual(3, self.rows(self.settings("set", "reap.grace", "3", "--revision", current))["reap.grace"]
                         ["value"])

    def test_an_environment_row_is_read_only(self):
        found = self.settings("set", "env.JUNKFATHER_STATE", "/tmp/elsewhere", code=settings.READ_ONLY)
        self.assertEqual(False, found["ok"])
        self.assertIn("environment", found["error"])
        self.settings("check", "env.JUNKFATHER_STATE", "/tmp/elsewhere", code=settings.READ_ONLY)
        self.settings("reset", "env.JUNKFATHER_STATE", code=settings.READ_ONLY)
        self.assertNotEqual(4, settings.READ_ONLY, "4 is Slotfather's 'another user's queue' (fathers#118)")

    def test_an_unreadable_settings_file_makes_the_settings_read_only_and_is_never_overwritten(self):
        self.state.mkdir(parents=True)
        (self.state / "settings.json").write_text("{not json")
        found = self.settings()
        self.assertEqual(False, found["writable"])
        self.assertIn("settings.json", found["reason"])
        self.assertEqual(7200, self.rows(found)["own.default_ttl"]["value"], "an unreadable file counts as unset")
        self.settings("set", "reap.grace", "1", code=settings.READ_ONLY)
        self.assertEqual("{not json", (self.state / "settings.json").read_text())

    def test_a_sweep_interval_change_says_the_agent_needs_a_reload(self):
        checked = self.settings("check", "sweep.interval", "30m")
        self.assertIn("install.py --launchd", checked["preview"])


class SavedValuesTests(SettingsCase):
    """own, sweep and reap read the saved values when they run, not when their module was imported."""

    def test_own_and_adopt_take_the_saved_default_ttl(self):
        self.settings("set", "own.default_ttl", "30m")
        child = self.spawn("sleep", "600", start_new_session=True)
        self.wait_for(lambda: procs.Table.read().get(child.pid) is not None, what="the child")
        result = self.cli("adopt", str(child.pid))
        self.assertEqual(0, result.returncode, result.stderr)
        (entry,) = self.store.entries()
        self.assertEqual(1800, entry["ttl"])
        self.assertEqual(0, self.cli("reap", "--id", entry["id"], "--grace", "1").returncode)
        explicit = self.cli("own", "--ttl", "1h", "--", "true")
        self.assertEqual(0, explicit.returncode, explicit.stderr)
        self.assertEqual([1800, 3600], [e["ttl"] for e in self.store.entries()], "--ttl still wins")

    def test_sweep_plist_takes_the_saved_interval(self):
        self.settings("set", "sweep.interval", "15m")
        result = self.cli("sweep", "--plist")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(900, plistlib.loads(result.stdout.encode())["StartInterval"])
        given = self.cli("sweep", "--plist", "--interval", "120")
        self.assertEqual(120, plistlib.loads(given.stdout.encode())["StartInterval"], "--interval still wins")

    def owned_child(self, entry_id: str) -> Dict[str, Any]:
        child = self.spawn("sleep", "600", start_new_session=True)
        mine = procs.Table.read().get(child.pid)
        assert mine is not None
        record: Dict[str, Any] = {
            "id": entry_id, "state": "running", "pid": child.pid, "pgid": child.pid, "start": mine.start,
            "seen": mine.cmd, "group": True, "known": [[child.pid, mine.start]], "argv": ["sleep", "600"],
            "expires": 0, "keep": "owner", "owner": {}, "created": 1.0}
        self.store.save(as_entry(record))
        return record

    def test_reap_takes_the_saved_grace(self):
        self.settings("set", "reap.grace", "2.5")
        self.owned_child("jgrace")
        graces: List[float] = []

        def stopper(targets: List[procs.Proc], grace: float) -> Dict[str, Any]:
            graces.append(grace)
            return reap.stop(targets, 0.5)

        self.assertEqual("stopped", reap.reap(self.store, "jgrace", "test", stopper=stopper)["result"])
        self.assertEqual(2.5, graces[0])

    def test_the_sweep_reaps_with_the_saved_grace(self):
        self.settings("set", "reap.grace", "3")
        self.owned_child("jswept")
        seen: List[Any] = []
        real = reap.reap

        def recording(*args: Any, **options: Any) -> Dict[str, Any]:
            seen.append(options.get("grace"))
            return real(*args, **{**options, "dry_run": True})
        with mock.patch.object(reap, "reap", recording), \
                mock.patch.object(resources, "unowned", return_value=([], [])):
            sweep.sweep(self.store, cwds={}, sessions=[], dry_run=True)
        self.assertEqual([3], seen)

    def test_the_values_are_read_at_the_point_of_use_not_at_import(self):
        self.assertEqual(10, settings.value("reap.grace", self.store.root))
        self.settings("set", "reap.grace", "4")
        self.assertEqual(4, settings.value("reap.grace", self.store.root), "the same process sees the new value")
        self.assertEqual(4, settings.value("reap.grace"), "the default root is $JUNKFATHER_STATE")


if __name__ == "__main__":
    unittest.main()
