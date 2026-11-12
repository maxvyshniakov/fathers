"""Machine-local settings (lanefather.settings) and the commands that read them, in isolated state."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any

from support import LaneCase


class SettingsTests(LaneCase):
    def settings(self, *args: str, code: int = 0, cwd: Any = None) -> dict[str, Any]:
        proc = subprocess.run([sys.executable, "-m", "lanefather", "settings", *args, "--json"],
                              cwd=cwd or self.tmp, env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def rows(self, found: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {row["key"]: row for section in found["sections"] for row in section["settings"]}

    def file(self) -> pathlib.Path:
        return pathlib.Path(self.env["LANEFATHER_SETTINGS"])

    def test_the_schema_lists_the_machine_knobs_at_their_defaults(self):
        del self.env["LANEFATHER_REVIEW_START"]
        found = self.settings()
        self.assertEqual((found["v"], found["tool"], found["writable"]), (1, "lanefather", True))
        rows = self.rows(found)
        self.assertEqual(set(rows), {"review.start", "rerun.wait", *(f"slots.priority.{p}" for p in
                                                                     ("check", "post-merge", "gate", "rerun", "perf"))})
        self.assertEqual((rows["review.start"]["value"], rows["review.start"]["source"]), ("auto", "default"))
        self.assertEqual(rows["rerun.wait"]["value"], 3300)
        self.assertEqual([c["value"] for c in rows["slots.priority.gate"]["choices"]],
                         ["project", "interactive", "normal", "batch"])
        self.assertTrue(all(row["writable"] for row in rows.values()))
        self.assertFalse(self.file().exists())

    def test_set_saves_and_the_commands_read_it_below_the_environment(self):
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
        self.assertIn("LANEFATHER_PRIORITY", self.settings("set", "slots.priority.check", "normal", code=4)["error"])

    def test_review_start_manual_from_settings_holds_lands_review(self):
        del self.env["LANEFATHER_REVIEW_START"]
        self.settings("set", "review.start", "manual")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\na\n"})
        self.lane("check", "a")
        self.assertIn("review not started (lanefather settings review.start=manual)", self.lane("land", "a"))

    def test_validation_follows_the_flags_and_environment_rules(self):
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

    def test_a_stale_revision_is_a_conflict_that_prints_the_current_settings(self):
        first = self.settings()["revision"]
        self.settings("set", "slots.priority.perf", "normal", "--revision", first)
        conflict = self.settings("set", "slots.priority.perf", "interactive", "--revision", first, code=3)
        self.assertFalse(conflict["ok"])
        self.assertEqual(self.rows(conflict)["slots.priority.perf"]["value"], "normal")
        # Without --revision it merges.
        merged = self.settings("set", "slots.priority.gate", "batch")
        self.assertEqual({key: row["value"] for key, row in self.rows(merged).items() if row["source"] == "saved"},
                         {"slots.priority.perf": "normal", "slots.priority.gate": "batch"})

    def test_reset_one_or_all(self):
        self.settings("set", "slots.priority.perf", "normal")
        self.settings("set", "rerun.wait", "600")
        self.assertEqual(self.rows(self.settings("reset", "rerun.wait"))["rerun.wait"]["source"], "default")
        self.assertEqual(self.rows(self.settings())["slots.priority.perf"]["source"], "saved")
        self.settings("reset", "--all")
        self.assertTrue(all(row["source"] != "saved" for row in self.rows(self.settings()).values()))
        self.settings("reset", code=2)

    def test_the_projects_own_priorities_are_read_only_rows(self):
        config = self.repo / ".lanefather.toml"
        config.write_text(config.read_text() + '\n[slots.priority]\ngate = "normal"\n')
        rows = self.rows(self.settings(cwd=self.repo))
        row = rows["project.slots.priority.gate"]
        self.assertEqual((row["value"], row["writable"]), ("normal", False))
        self.assertEqual(row["source"], ".lanefather.toml [slots.priority] gate")

    def test_a_write_keeps_settings_a_newer_lanefather_saved_and_never_rewrites_a_newer_file(self):
        self.file().parent.mkdir(parents=True, exist_ok=True)
        self.file().write_text(json.dumps({"v": 1, "settings": {"review.timeout": 600}}))
        self.settings("set", "rerun.wait", "600")
        self.assertEqual(json.loads(self.file().read_text())["settings"], {"review.timeout": 600, "rerun.wait": 600})
        self.file().write_text(json.dumps({"v": 2, "settings": {}}))
        self.assertFalse(self.settings()["writable"])
        self.assertIn("version 2", self.settings("set", "rerun.wait", "60", code=4)["error"])
        self.assertEqual(json.loads(self.file().read_text())["v"], 2)

    def test_an_environment_value_is_typed_and_an_unreadable_project_leaves_the_settings_readable(self):
        self.env["LANEFATHER_RERUN_WAIT"] = "600"
        self.assertEqual(self.rows(self.settings())["rerun.wait"]["value"], 600)
        broken = self.tmp / "broken"
        broken.mkdir()
        self.git(broken, "init", "-q")
        (broken / ".lanefather.toml").write_text("not = toml [")
        rows = self.rows(self.settings(cwd=broken))
        self.assertEqual((rows["rerun.wait"]["value"], rows["rerun.wait"]["writable"]), (600, False))

    def test_the_capabilities_advertise_settings(self):
        self.assertEqual(json.loads(self.lane("capabilities", "--json"))["dashboard"]["settings"], {"versions": [1]})
