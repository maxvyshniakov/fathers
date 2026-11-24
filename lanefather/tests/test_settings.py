"""Machine-local settings (lanefather.settings) and the commands that read them, in isolated state."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from support import FIXTURE_CONFIG, LaneCase, ScratchCase, run_in_process_streams

if TYPE_CHECKING:
    _SettingsBase = ScratchCase
else:
    _SettingsBase = object


class _SettingsHelpers(_SettingsBase):
    def settings(self, *args: str, code: int = 0, cwd: Any = None) -> dict[str, Any]:
        target_cwd = cwd or self.tmp
        if os.environ.get("LANEFATHER_TEST_PROCESS"):
            proc = subprocess.run([sys.executable, "-m", "lanefather", "settings", *args, "--json"],
                                  cwd=target_cwd, env=self.env, capture_output=True, text=True)
            actual_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        else:
            actual_code, stdout, stderr = run_in_process_streams(["settings", *args, "--json"], target_cwd, self.env)
        self.assertEqual(actual_code, code, stdout + stderr)
        return json.loads(stdout)

    def rows(self, found: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {row["key"]: row for section in found["sections"] for row in section["settings"]}

    def file(self) -> pathlib.Path:
        return pathlib.Path(self.env["LANEFATHER_SETTINGS"])

    def floor_gb(self, warning: str = "") -> float:
        output = self.lane("capabilities", "--json")
        self.assertIn(warning, output)
        return json.loads(next(line for line in output.splitlines() if line.startswith("{")))["host"]["min_disk_gb"]


class SettingsScratchTests(_SettingsHelpers, ScratchCase):
    def test_the_schema_lists_the_machine_knobs_at_their_defaults(self) -> None:
        del self.env["LANEFATHER_REVIEW_START"]
        found = self.settings()
        self.assertEqual((found["v"], found["tool"], found["writable"]), (1, "lanefather", True))
        rows = self.rows(found)
        self.assertEqual(set(rows), {"review.start", "rerun.wait", *(f"slots.priority.{p}" for p in
                                                                     ("check", "post-merge", "gate", "rerun", "perf")),
                                     "host.min_disk_gb"})
        self.assertEqual((rows["review.start"]["value"], rows["review.start"]["source"]), ("auto", "default"))
        self.assertEqual(rows["rerun.wait"]["value"], 3300)
        self.assertEqual([c["value"] for c in rows["slots.priority.gate"]["choices"]],
                         ["project", "interactive", "normal", "batch"])
        self.assertTrue(all(row["writable"] for row in rows.values()))
        self.assertFalse(self.file().exists())

    def test_set_saves_and_the_commands_read_it_below_the_environment(self) -> None:
        found = self.settings("set", "slots.priority.check", "batch")
        self.assertTrue(found["ok"])
        self.assertEqual(self.rows(found)["slots.priority.check"]["source"], "saved")
        self.assertEqual(self.file().stat().st_mode & 0o777, 0o600)
        priorities = json.loads(self.lane("list", "--json"))["priorities"]
        self.assertEqual(priorities["check"], {"priority": "batch",
                                               "source": "lanefather settings slots.priority.check"})
        self.assertEqual(priorities["gate"]["source"], "built-in")
        # The environment wins and makes the row read-only.
        self.env["LANEFATHER_PRIORITY"] = "interactive"
        self.assertEqual(json.loads(self.lane("list", "--json"))["priorities"]["check"]["priority"], "interactive")
        row = self.rows(self.settings())["slots.priority.check"]
        self.assertEqual((row["value"], row["source"], row["writable"]), ("interactive", "env LANEFATHER_PRIORITY",
                                                                          False))
        self.assertIn("LANEFATHER_PRIORITY", self.settings("set", "slots.priority.check", "normal", code=5)["error"])

    def test_validation_follows_the_flags_and_environment_rules(self) -> None:
        del self.env["LANEFATHER_REVIEW_START"]
        self.assertEqual(self.settings("check", "slots.priority.gate", "batch")["ok"], True)
        self.assertIn("gate runs ask Slotfather at batch",
                      self.settings("check", "slots.priority.gate", "batch")["preview"])
        for key, bad in (("slots.priority.gate", "urgent"), ("review.start", "sometimes"), ("rerun.wait", "0"),
                         ("rerun.wait", "-5"), ("rerun.wait", "2 days"), ("no.such", "1")):
            reply = self.settings("check", key, bad, code=2)
            self.assertFalse(reply["ok"])
            self.assertTrue(reply["error"], (key, bad))
        self.assertEqual(self.settings("check", "rerun.wait", "15m")["preview"], "a rerun waits up to 900 s for its lease")
        self.settings("set", "rerun.wait", "90s")
        self.assertEqual(self.rows(self.settings())["rerun.wait"]["value"], 90)
        self.assertEqual(self.settings("set", "review.start", "never", code=2)["ok"], False)

    def test_a_stale_revision_is_a_conflict_that_prints_the_current_settings(self) -> None:
        first = self.settings()["revision"]
        self.settings("set", "slots.priority.perf", "normal", "--revision", first)
        conflict = self.settings("set", "slots.priority.perf", "interactive", "--revision", first, code=3)
        self.assertFalse(conflict["ok"])
        self.assertEqual(self.rows(conflict)["slots.priority.perf"]["value"], "normal")
        # Without --revision it merges.
        merged = self.settings("set", "slots.priority.gate", "batch")
        self.assertEqual({key: row["value"] for key, row in self.rows(merged).items() if row["source"] == "saved"},
                         {"slots.priority.perf": "normal", "slots.priority.gate": "batch"})

    def test_reset_one_or_all(self) -> None:
        self.settings("set", "slots.priority.perf", "normal")
        self.settings("set", "rerun.wait", "600")
        self.assertEqual(self.rows(self.settings("reset", "rerun.wait"))["rerun.wait"]["source"], "default")
        self.assertEqual(self.rows(self.settings())["slots.priority.perf"]["source"], "saved")
        self.settings("reset", "--all")
        self.assertTrue(all(row["source"] != "saved" for row in self.rows(self.settings()).values()))
        self.settings("reset", code=2)

    def test_the_projects_own_priorities_are_read_only_rows(self) -> None:
        cfg = self.write(self.tmp / "project", ".lanefather.toml",
                         FIXTURE_CONFIG + '\n[slots.priority]\ngate = "normal"\n')
        self.env["LANEFATHER_CONFIG"] = str(cfg)
        rows = self.rows(self.settings(cwd=cfg.parent))
        row = rows["project.slots.priority.gate"]
        self.assertEqual((row["value"], row["writable"]), ("normal", False))
        self.assertEqual(row["source"], ".lanefather.toml [slots.priority] gate")

    def test_a_write_keeps_settings_a_newer_lanefather_saved_and_never_rewrites_a_newer_file(self) -> None:
        self.file().parent.mkdir(parents=True, exist_ok=True)
        self.file().write_text(json.dumps({"v": 1, "settings": {"review.timeout": 600}}))
        self.settings("set", "rerun.wait", "600")
        self.assertEqual(json.loads(self.file().read_text())["settings"], {"review.timeout": 600, "rerun.wait": 600})
        self.file().write_text(json.dumps({"v": 2, "settings": {}}))
        self.assertFalse(self.settings()["writable"])
        self.assertIn("version 2", self.settings("set", "rerun.wait", "60", code=5)["error"])
        self.assertEqual(json.loads(self.file().read_text())["v"], 2)

    def test_an_environment_value_is_typed_and_an_unreadable_project_leaves_the_settings_readable(self) -> None:
        self.env["LANEFATHER_RERUN_WAIT"] = "600"
        self.assertEqual(self.rows(self.settings())["rerun.wait"]["value"], 600)
        broken = self.write(self.tmp / "broken", ".lanefather.toml", "not = toml [")
        self.env["LANEFATHER_CONFIG"] = str(broken)
        rows = self.rows(self.settings(cwd=broken.parent))
        self.assertEqual((rows["rerun.wait"]["value"], rows["rerun.wait"]["writable"]), (600, False))

    def test_the_capabilities_advertise_settings(self) -> None:
        self.assertEqual(json.loads(self.lane("capabilities", "--json"))["dashboard"]["settings"], {"versions": [1]})

    # The free-disk floor (fathers#163): 6 cores and 20 GB derive a cap of 3, so the derived floor is
    # 40 reserve + 3 x 3.0 GB lane peak = 49 GB.

    def test_the_disk_floor_row_is_auto_and_describes_the_derived_floor(self) -> None:
        row = self.rows(self.settings())["host.min_disk_gb"]
        self.assertEqual((row["type"], row["value"], row["default"], row["zero"], row["unit"]),
                         ("number", 0, 0, "auto", "GB"))
        self.assertEqual((row["source"], row["writable"], row["effective"]), ("default", True, 49))
        self.assertEqual((row["min"], row["step"]), (0, 5))
        self.assertGreater(row["max"], 1000)
        self.assertIn("auto: 49 GB (10^9) = 40 reserve + 3 x 3.0 GB lane peak", row["description"])
        self.assertEqual(self.floor_gb(), 49)

    def test_every_accepted_floor_reads_back_as_saved(self) -> None:
        """Review of fathers#163 (P3): a value set saves as JSON and reads back as the same value, small, large and
        in Python's float syntax alike."""
        for given, expected in (("0.00001", 1e-05), ("1e-05", 1e-05), ("1.5e2", 150), ("150", 150), ("60.5", 60.5),
                                ("100000", 100000), ("99999.99999", 99999.99999), (" 75 ", 75)):
            row = self.rows(self.settings("set", "host.min_disk_gb", given))["host.min_disk_gb"]
            self.assertEqual((row["value"], row["source"]), (expected, "saved"), given)
            self.assertEqual(self.rows(self.settings())["host.min_disk_gb"]["value"], expected, given)
            self.assertEqual(self.floor_gb(), round(expected, 1), given)

    def test_the_environment_keeps_its_numeric_syntax(self) -> None:
        """Review of fathers#163 (P1): LANEFATHER_MIN_DISK_GB takes any finite positive number, as before the
        setting, beyond the setting's own range too."""
        self.settings("set", "host.min_disk_gb", "60")
        for given, expected in (("1.5e2", 150), ("250000", 250000), ("75.5", 75.5)):
            self.env["LANEFATHER_MIN_DISK_GB"] = given
            self.assertEqual(self.floor_gb(), expected, given)
            row = self.rows(self.settings())["host.min_disk_gb"]
            self.assertEqual((row["value"], row["writable"]), (expected, False), given)
            self.assertNotIn("error", row, given)
        for given in ("-5", "inf", "nan", "lots"):
            self.env["LANEFATHER_MIN_DISK_GB"] = given
            self.assertEqual(self.floor_gb("is not auto or a positive number of GB"), 60, given)

    def test_an_invalid_floor_is_refused_with_the_settings_error(self) -> None:
        for bad in ("lots", "-5", "inf", "nan", "1e9", "100000.1", "", "50 GB"):
            reply = self.settings("set", "host.min_disk_gb", bad, code=2)
            self.assertEqual((reply["v"], reply["ok"], reply["preview"]), (1, False, None))
            self.assertIn("host.min_disk_gb", reply["error"], bad)
        self.assertFalse(self.file().exists())
        self.assertIn("150 GB", self.settings("check", "host.min_disk_gb", "150")["preview"])
        self.assertIn("derived", self.settings("check", "host.min_disk_gb", "auto")["preview"])


class SettingsTests(_SettingsHelpers, LaneCase):
    def test_review_start_manual_from_settings_holds_lands_review(self) -> None:
        del self.env["LANEFATHER_REVIEW_START"]
        self.settings("set", "review.start", "manual")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\na\n"})
        self.lane("check", "a")
        self.assertIn("review not started (lanefather settings review.start=manual)", self.lane("land", "a"))

    def test_the_saved_floor_sets_admission_capabilities_and_the_refusal_until_reset(self) -> None:
        self.env["LANEFATHER_DISK_FREE_GB"] = "100"
        found = self.settings("set", "host.min_disk_gb", "150")
        row = self.rows(found)["host.min_disk_gb"]
        self.assertEqual((row["value"], row["source"], row["effective"]), (150, "saved", 150))
        self.assertIn("auto: 49 GB", row["description"])
        self.assertEqual(self.floor_gb(), 150)
        refused = self.start("a", "lib/**", ok=False)
        self.assertIn("free disk is 100 GB (10^9), under the floor of 150 GB (10^9; lanefather settings "
                      "host.min_disk_gb)", refused)
        self.assertEqual(self.rows(self.settings("reset", "host.min_disk_gb"))["host.min_disk_gb"]["source"],
                         "default")
        self.assertEqual(self.floor_gb(), 49)
        self.start("a", "lib/**")
        # auto, and 0 (what an arrow down to the end sends), save the default: the file keeps no value.
        self.settings("set", "host.min_disk_gb", "60.5")
        self.assertEqual(self.floor_gb(), 60.5)
        self.settings("set", "host.min_disk_gb", "auto")
        self.assertNotIn("host.min_disk_gb", json.loads(self.file().read_text())["settings"])
        self.settings("set", "host.min_disk_gb", "70")
        self.settings("set", "host.min_disk_gb", "0")
        self.assertEqual(self.floor_gb(), 49)

    def test_the_environment_wins_over_the_saved_floor_and_makes_the_row_read_only(self) -> None:
        self.settings("set", "host.min_disk_gb", "150")
        self.env["LANEFATHER_MIN_DISK_GB"] = "70"
        self.assertEqual(self.floor_gb(), 70)
        row = self.rows(self.settings(cwd=self.integration))["host.min_disk_gb"]
        self.assertEqual((row["value"], row["source"], row["writable"], row["effective"]),
                         (70, "env LANEFATHER_MIN_DISK_GB", False, 70))
        reply = self.settings("set", "host.min_disk_gb", "80", code=5)
        self.assertIn("LANEFATHER_MIN_DISK_GB", reply["error"])
        self.env["LANEFATHER_DISK_FREE_GB"] = "60"
        self.assertIn("under the floor of 70 GB (10^9; LANEFATHER_MIN_DISK_GB)", self.start("a", "lib/**", ok=False))
        # An invalid environment value warns and the saved floor applies.
        self.env["LANEFATHER_MIN_DISK_GB"] = "lots"
        self.assertEqual(self.floor_gb("LANEFATHER_MIN_DISK_GB='lots' is not auto or a positive number of GB"), 150)
        # auto in the environment is the derived floor, over the saved one.
        self.env["LANEFATHER_MIN_DISK_GB"] = "auto"
        self.assertEqual(self.floor_gb(), 49)
