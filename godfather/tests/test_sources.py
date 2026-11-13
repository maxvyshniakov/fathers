"""Discovery and snapshots: absent, unsupported and failing Slotfather behave as SPEC's rules say, from fixtures and
from the fake Slotfather; fixture mode records writes and never runs a tool."""
from __future__ import annotations

import copy
import datetime
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import view as _view
from helpers import ROOT, FakeSlotfather

from godfather import app, sources, tui
from godfather.adapters import slotfather as adapter
from godfather.views import common, overlays, overview, settings_draw

SCENE = ROOT / "tests" / "fixtures" / "scene"


def replies(root: Path) -> dict[str, Any]:
    return json.loads((root / "slotfather" / "replies.json").read_text())


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="godfather-fixtures-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copytree(SCENE, self.tmp / "scene")
        self.root = self.tmp / "scene"
        patcher = mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(self.root), "TZ": "UTC",
                                               "PATH": "/usr/bin:/bin", "HOME": str(self.tmp)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_view.fresh)
        tui.set_theme(app._theme.from_environment(environ={}))

    def reply(self, argv: str, value: dict[str, Any]) -> None:
        found = replies(self.root)
        found[argv] = value
        (self.root / "slotfather" / "replies.json").write_text(json.dumps(found))

    def frame(self, screen: app.Screen, width: int = 156, height: int = 43) -> str:
        model, error = screen.load()
        canvas = tui.Canvas(width, height)
        if model:
            common.dashboard_colours(model, {}, tui.current_theme.holders())
        overview.dashboard_draw(canvas, model, error=error, notice=screen.notice())
        self.canvas = canvas
        return canvas.render(False)


class AbsentTests(FixtureCase):
    def test_without_slotfather_one_panel_names_the_tools_looked_for(self) -> None:
        shutil.rmtree(self.root / "slotfather")
        text = self.frame(app.Screen())
        self.assertIn("No tool found. Godfather looked for these on GODFATHER_FIXTURES:", text)
        for tool in ("slotfather", "lanefather", "junkfather"):
            self.assertRegex(text, rf"{tool} +not found")
        self.assertNotIn("SLOTS", text)
        self.assertEqual("absent", app.Screen().source.state)

    def test_on_path_absent_says_path(self) -> None:
        os.environ.pop("GODFATHER_FIXTURES")
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp / "empty")}):
            text = self.frame(app.Screen(), 100, 20)
        self.assertIn("looked for these on PATH", text)

    def test_the_settings_of_an_absent_slotfather_are_this_dashboards_alone(self) -> None:
        shutil.rmtree(self.root / "slotfather")
        screen = app.Screen()
        screen.load()
        dialog = screen.settings(1.0, "auto")
        self.assertEqual(["interval", "theme"], dialog.fields())


class UnsupportedTests(FixtureCase):
    def test_capabilities_without_a_dashboard_block_say_update_slotfather(self) -> None:
        self.reply("capabilities --json", {"code": 0, "json": {"v": 1, "tool": "slotfather", "version": "0.1.0"}})
        before = len(sources.recorded)
        screen = app.Screen()
        text = self.frame(screen)
        self.assertIn("slotfather 0.1.0: no dashboard contract; update slotfather", text)
        self.assertEqual("unsupported", screen.source.state)
        self.assertEqual(before, len(sources.recorded), "nothing written")

    def test_a_failing_capabilities_command_is_unsupported_too(self) -> None:
        self.reply("capabilities --json", {"code": 2, "stderr": "slotfather: unknown command capabilities"})
        screen = app.Screen()
        text = self.frame(screen)
        self.assertIn("slotfather: no dashboard contract (capabilities --json exited 2); update slotfather", text)

    def test_only_unknown_snapshot_versions_need_a_newer_godfather(self) -> None:
        found = replies(self.root)["capabilities --json"]
        found["json"]["dashboard"]["snapshot"]["versions"] = [3]
        self.reply("capabilities --json", found)
        self.assertIn("slotfather 0.2.0: needs a newer godfather", self.frame(app.Screen()))


class FailingTests(FixtureCase):
    def test_a_failed_snapshot_keeps_the_last_good_one_dimmed_and_stale(self) -> None:
        screen = app.Screen()
        good = self.frame(screen)
        self.assertIn("lane-644-build-once", good)
        self.reply("status --json --full", {"code": 1, "stderr": "slotfather: queue unreadable"})
        before = datetime.datetime.now(datetime.timezone.utc).astimezone().strftime("%H:%M")
        text = self.frame(screen)
        self.assertIn("lane-644-build-once", text, "the last good snapshot stays on screen")
        self.assertRegex(text, r"stale since \d\d:\d\d")
        self.assertIn(f"stale since {before}", text.split("\n")[0])
        footer = text.split("\n")[-1]
        self.assertIn("● stale", footer)
        self.assertRegex(footer, r"refresh failed: slot\w*…", "the reason, cut to the footer's room")
        self.assertEqual("slotfather: queue unreadable", screen.source.problem)
        self.assertEqual({"dim"}, {cell[1] for row in self.canvas.cells[:-1] for cell in row if cell[0].strip()},
                         "every panel dimmed")
        self.assertEqual("stale", screen.source.state)
        dialog = screen.settings(1.0, "auto")
        self.assertIn("slotfather: queue unreadable", "\n".join(
            "".join(part[1] for part in row.parts) for row in settings_draw.status_rows(dialog, None)))
        self.reply("status --json --full", replies(SCENE)["status --json --full"])
        text = self.frame(screen)
        self.assertNotIn("stale", text, "a good snapshot ends it")
        self.assertEqual("ok", screen.source.state)

    def test_an_unknown_snapshot_version_is_stale_not_guessed(self) -> None:
        screen = app.Screen()
        self.frame(screen)
        found = copy.deepcopy(replies(SCENE)["status --json --full"])
        found["json"]["dashboard"]["v"] = 2
        self.reply("status --json --full", found)
        text = self.frame(screen)
        self.assertEqual("dashboard block version 2; needs a newer godfather", screen.source.problem)
        self.assertIn("stale since", text)

    def test_a_first_snapshot_that_fails_says_why_without_a_screen(self) -> None:
        self.reply("status --json --full", {"code": 1, "stderr": "slotfather: no queue"})
        text = self.frame(app.Screen(), 100, 20)
        self.assertIn("Queue unavailable: slotfather: no queue", text)


class RediscoveryTests(FixtureCase):
    def test_a_failed_capabilities_call_after_a_good_one_keeps_the_screen_stale(self) -> None:
        screen = app.Screen()
        self.assertIn("lane-644-build-once", self.frame(screen))
        self.reply("capabilities --json", {"code": 124, "stderr": "slotfather capabilities --json took longer than 2 s"})
        screen.source.checked = -1e9  # discovery is due
        text = self.frame(screen)
        self.assertIn("lane-644-build-once", text, "the screen stays")
        self.assertNotIn("update slotfather", text)
        self.assertIsNotNone(screen.source.capabilities, "the contract it answered with before is kept")
        self.assertEqual("ok", screen.source.state, "its snapshot still answers")
        self.reply("status --json --full", {"code": 124, "stderr": "took longer than 5 s"})
        screen.source.checked = -1e9
        text = self.frame(screen)
        self.assertIn("stale since", text, "both slow: the last good screen, stale")
        self.assertIn("lane-644-build-once", text)

    def test_a_small_terminal_is_dimmed_when_stale_too(self) -> None:
        screen = app.Screen()
        self.frame(screen, 40, 12)
        self.reply("status --json --full", {"code": 1, "stderr": "slotfather: queue unreadable"})
        self.frame(screen, 40, 12)
        self.assertEqual({"dim"}, {cell[1] for row in self.canvas.cells[:-1] for cell in row if str(cell[0]).strip()})

    def test_a_snapshot_that_prints_nothing_says_so(self) -> None:
        screen = app.Screen()
        self.reply("status --json --full", {"code": 0, "stdout": ""})
        _, error = screen.load()
        self.assertEqual("slotfather status --json --full printed nothing", error)


class SlowSnapshotTests(unittest.TestCase):
    def test_a_snapshot_over_five_seconds_is_a_failure(self) -> None:
        fake = FakeSlotfather({"sleep": {"status": 2}})
        self.addCleanup(fake.close)
        with fake.active(), mock.patch.object(sources, "SNAPSHOT_TIMEOUT", 0.5):
            screen = app.Screen()
            model, error = screen.load()
        self.assertIsNone(model)
        self.assertIn("took longer than 0.5 s", str(error))


class FakeContractTests(unittest.TestCase):
    def test_godfather_reads_the_fake_slotfather_through_its_cli(self) -> None:
        fake = FakeSlotfather()
        self.addCleanup(fake.close)
        with fake.active():
            screen = app.Screen()
            model, error = screen.load()
        self.assertIsNone(error)
        assert model is not None
        self.assertEqual(4, model["slots"])
        self.assertEqual([["capabilities", "--json"], ["status", "--json", "--full"]], fake.argv())


class RecordTests(FixtureCase):
    def test_writes_are_recorded_and_never_run(self) -> None:
        record = self.tmp / "writes.jsonl"
        os.environ["GODFATHER_FIXTURES_RECORD"] = str(record)
        self.reply("settings set order.safety 4 --json", {"code": 0, "json": replies(self.root)["settings --json"]["json"]})
        before = len(sources.recorded)
        code, value, _ = sources.run_json("slotfather", ["settings", "set", "order.safety", "4", "--json"], 1)
        self.assertEqual(0, code)
        self.assertEqual(["slotfather", "settings", "set", "order.safety", "4", "--json"], sources.recorded[before])
        self.assertEqual([["slotfather", "settings", "set", "order.safety", "4", "--json"]],
                         [json.loads(line) for line in record.read_text().splitlines()])
        code, _, problem = sources.run_json("slotfather", ["status", "--json"], 1)
        self.assertEqual((127, "slotfather: no fixture for status --json"), (code, problem))


class DecodeTests(unittest.TestCase):
    def test_times_with_and_without_milliseconds_and_offsets(self) -> None:
        utc = datetime.timezone.utc
        for text in ("2026-11-12T04:25:56Z", "2026-11-12T04:25:56.000Z", "2026-11-12T06:25:56+02:00",
                     "2026-11-12T04:25:56.0Z"):
            self.assertEqual(datetime.datetime(2026, 11, 12, 4, 25, 56, tzinfo=utc), adapter.parse_time(text), text)
        for text in (None, "", "yesterday", "2026-11-12T04:25:56"):
            self.assertIsNone(adapter.parse_time(text), text)

    def test_a_malformed_block_is_unsupported(self) -> None:
        snapshot = copy.deepcopy(replies(SCENE)["status --json --full"]["json"])
        snapshot["dashboard"]["holders"][0]["tree"] = [[1, 2]]
        with self.assertRaises(adapter.Unsupported):
            adapter.decode(snapshot)
        snapshot = copy.deepcopy(replies(SCENE)["status --json --full"]["json"])
        snapshot["dashboard"]["slots"] = "six"
        with self.assertRaisesRegex(adapter.Unsupported, "dashboard.slots"):
            adapter.decode(snapshot)
        with self.assertRaisesRegex(adapter.Unsupported, "version 1"):
            adapter.decode(dict(snapshot, v=1))

    def test_decoding_gives_the_renderers_their_types(self) -> None:
        model = adapter.decode(replies(SCENE)["status --json --full"]["json"])
        holder = model["holders"][0]
        self.assertIsInstance(holder["key"], tuple)
        self.assertIsInstance(holder["granted"], datetime.datetime)
        self.assertTrue(all(isinstance(step, tuple) for step in model["history"]))
        self.assertTrue(all(isinstance(event["when"], datetime.datetime) for event in model["events"]))


class ChoicesTests(unittest.TestCase):
    def test_refresh_every_and_colour_theme_are_kept_between_runs(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home}):
            for name in ("GODFATHER_THEME", "SLOTFATHER_THEME"):
                os.environ.pop(name, None)
            self.assertEqual((1.0, "auto"), app.starting_choices(None))
            app.save_choices(5.0, "builtin")
            self.assertEqual((5.0, "builtin"), app.starting_choices(None))
            self.assertEqual((2.0, "builtin"), app.starting_choices(2.0), "--interval wins for this run")
            os.environ["SLOTFATHER_THEME"] = "terminal"
            self.assertEqual("terminal", app.starting_choices(None)[1], "the old name still sets it")
            os.environ["GODFATHER_THEME"] = "auto"
            self.assertEqual("auto", app.starting_choices(None)[1], "the new name first")
            saved = json.loads((Path(home) / "godfather" / "settings.json").read_text())
            self.assertEqual({"v": 1, "interval": 5.0, "theme": "builtin"}, saved)


class WhatsNewTests(FixtureCase):
    def test_a_tools_changelog_json_is_in_the_overlay_and_one_without_shows_its_version(self) -> None:
        from godfather import news
        self.reply("changelog --json", {"code": 0, "json": {"v": 1, "tool": "slotfather", "version": "0.2.0",
                                                           "source": None, "entries": [
            {"version": "0.2.0", "date": "2026-11-12", "section": "Added", "text": "`capabilities --json`."}]}})
        (self.root / "junkfather").mkdir()
        (self.root / "junkfather" / "replies.json").write_text(json.dumps(
            {"capabilities --json": {"code": 0, "json": {"v": 1, "tool": "junkfather", "version": "0.1.0"}}}))
        canvas = tui.Canvas(120, 40)
        overlays.draw_changelog(canvas, news=news.load("", stale=False))
        text = canvas.render(False)
        self.assertIn("All · Slotfather", text.split("\n")[canvas.help_view.y])  # type: ignore[union-attr]
        self.assertRegex(text, r"Slotfather 0\.2\.0 · 2026-11-12 · installed · new")
        self.assertIn("• capabilities --json.", text)
        for gone in ("not installed", "versions only", "Junkfather 0.1.0"):
            self.assertNotIn(gone, text, "fathers#102: only the timeline")


if __name__ == "__main__":
    unittest.main()
