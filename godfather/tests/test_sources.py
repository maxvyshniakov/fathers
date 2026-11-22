"""Discovery and snapshots: absent, unsupported and failing Slotfather behave as SPEC's rules say, from fixtures and
from the fake Slotfather; fixture mode records writes and never runs a tool; pure state-machine transitions and
thread-safe ToolClient / ShowCache concurrency."""
from __future__ import annotations

import copy
import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any, Optional
import unittest
from unittest import mock

from helpers import ROOT, FakeSlotfather
import view as _view

from godfather import app, entries, news, reducer, settings_layout, settings_model, sources, state as state_module, theme, tui
from godfather.adapters import junkfather, lanefather, slotfather as adapter
from godfather.views import overlays, overview

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
        tui.set_theme(theme.from_environment(environ={}))

    def reply(self, argv: str, value: dict[str, Any]) -> None:
        found = replies(self.root)
        found[argv] = value
        (self.root / "slotfather" / "replies.json").write_text(json.dumps(found))

    def frame(self, screen: app.Screen, width: int = 156, height: int = 43) -> str:
        self.addCleanup(screen.close)
        model, error = screen.load()
        canvas = tui.Canvas(width, height)
        if model:
            entries.dashboard_colours(model, {}, tui.current_theme.holders())
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
        self.assertEqual("absent", app.Screen().clients["slotfather"].state().state)

    def test_on_path_absent_says_path(self) -> None:
        os.environ.pop("GODFATHER_FIXTURES")
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp / "empty")}):
            text = self.frame(app.Screen(), 100, 20)
        self.assertIn("looked for these on PATH", text)

    def test_the_settings_of_an_absent_slotfather_are_this_dashboards_alone(self) -> None:
        shutil.rmtree(self.root / "slotfather")
        with app.Screen() as screen:
            screen.load()
            context = settings_model.Context(width=100, height=40)
            panel = settings_model.opened(screen.settings_sections(), context, 0.0)
        self.assertEqual([("godfather", "interval"), ("godfather", "theme")],
                         [row.focus for row in settings_model.selectable(panel, context)])
        self.assertEqual(["godfather"], settings_model.tools(panel), "no section for an absent tool")


class UnsupportedTests(FixtureCase):
    def test_capabilities_without_a_dashboard_block_say_update_slotfather(self) -> None:
        self.reply("capabilities --json", {"code": 0, "json": {"v": 1, "tool": "slotfather", "version": "0.1.0"}})
        before = len(sources.recorded)
        screen = app.Screen()
        text = self.frame(screen)
        self.assertIn("slotfather 0.1.0: no dashboard contract; update slotfather", text)
        self.assertEqual("unsupported", screen.clients["slotfather"].state().state)
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
        before = datetime.datetime.now(datetime.UTC).astimezone().strftime("%H:%M")
        text = self.frame(screen)
        self.assertIn("lane-644-build-once", text, "the last good snapshot stays on screen")
        self.assertRegex(text, r"stale since \d\d:\d\d")
        self.assertIn(f"stale since {before}", text.split("\n")[0])
        footer = text.split("\n")[-1]
        self.assertIn("● stale", footer)
        self.assertRegex(footer, r"refresh failed: slot\w*…", "the reason, cut to the footer's room")
        slot_state = screen.clients["slotfather"].state()
        self.assertEqual("slotfather: queue unreadable", slot_state.problem)
        self.assertEqual({"dim"}, {cell[1] for row in self.canvas.cells[:-1] for cell in row if str(cell[0]).strip()},
                         "every panel dimmed")
        self.assertEqual("stale", slot_state.state)
        context = settings_model.Context(problem=slot_state.problem, width=100, height=40)
        panel = settings_model.opened(screen.settings_sections(), context, 0.0)
        self.assertIn("slotfather: queue unreadable", "\n".join(
            "".join(part[1] for part in line.parts) for line in settings_layout.lines(panel, context)))
        self.reply("status --json --full", replies(SCENE)["status --json --full"])
        text = self.frame(screen)
        self.assertNotIn("stale", text, "a good snapshot ends it")
        self.assertEqual("ok", screen.clients["slotfather"].state().state)

    def test_an_unknown_snapshot_version_is_stale_not_guessed(self) -> None:
        screen = app.Screen()
        self.frame(screen)
        found = copy.deepcopy(replies(SCENE)["status --json --full"])
        found["json"]["dashboard"]["v"] = 2
        self.reply("status --json --full", found)
        text = self.frame(screen)
        self.assertEqual("dashboard block version 2; needs a newer godfather",
                         screen.clients["slotfather"].state().problem)
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
        screen.clients["slotfather"].discover(force=True)
        text = self.frame(screen)
        self.assertIn("lane-644-build-once", text, "the screen stays")
        self.assertNotIn("update slotfather", text)
        self.assertIsNotNone(screen.clients["slotfather"].state().capabilities,
                             "the contract it answered with before is kept")
        self.assertEqual("ok", screen.clients["slotfather"].state().state, "its snapshot still answers")
        self.reply("status --json --full", {"code": 124, "stderr": "took longer than 5 s"})
        screen.clients["slotfather"].discover(force=True)
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
        with app.Screen() as screen:
            self.reply("status --json --full", {"code": 0, "stdout": ""})
            _, error = screen.load()
        self.assertEqual("slotfather status --json --full printed nothing", error)


class SlowSnapshotTests(unittest.TestCase):
    def test_a_snapshot_over_five_seconds_is_a_failure(self) -> None:
        fake = FakeSlotfather({"sleep": {"status": 2}})
        self.addCleanup(fake.close)
        with fake.active(), mock.patch.object(sources, "SNAPSHOT_TIMEOUT", 0.5), app.Screen() as screen:
            model, error = screen.load()
        self.assertIsNone(model)
        self.assertIn("took longer than 0.5 s", str(error))


class FakeContractTests(unittest.TestCase):
    def test_godfather_reads_the_fake_slotfather_through_its_cli(self) -> None:
        fake = FakeSlotfather()
        self.addCleanup(fake.close)
        # What is tested is the CLI contract, not how fast an interpreter starts on a loaded machine: no deadline
        with fake.active(), mock.patch.object(sources, "CAPABILITIES_TIMEOUT", None), \
                mock.patch.object(sources, "SNAPSHOT_TIMEOUT", None), app.Screen() as screen:
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
        code, _, _ = sources.run_json("slotfather", ["settings", "set", "order.safety", "4", "--json"], 1)
        self.assertEqual(0, code)
        self.assertEqual(["slotfather", "settings", "set", "order.safety", "4", "--json"], sources.recorded[before])
        self.assertEqual([["slotfather", "settings", "set", "order.safety", "4", "--json"]],
                         [json.loads(line) for line in record.read_text().splitlines()])
        code, _, problem = sources.run_json("slotfather", ["status", "--json"], 1)
        self.assertEqual((127, "slotfather: no fixture for status --json"), (code, problem))


class DecodeTests(unittest.TestCase):
    def test_times_with_and_without_milliseconds_and_offsets(self) -> None:
        utc = datetime.UTC
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
        self.assertEqual({tuple}, {type(step) for step in model["history"]})
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

    def test_news_reuses_discovered_tool_state_without_repeating_which_or_capabilities(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        class CountingRunner(sources.FixtureRunner):
            def which(self, tool: str) -> Optional[str]:
                calls.append((tool, ["which"]))
                return super().which(tool)

            def run_json(self, tool: str, argv: list[str], timeout: Optional[float],
                         executable: Optional[str] = None) -> tuple[int, Any, str]:
                calls.append((tool, list(argv)))
                return super().run_json(tool, argv, timeout, executable)

        runner = CountingRunner(self.root)
        disc = sources.on_discovered(
            sources.Discovery(), "slotfather", adapter, "/bin/slotfather", "/bin/slotfather", 0,
            replies(self.root)["capabilities --json"]["json"], "", 10.0,
        )
        known = sources.build_tool_state("slotfather", adapter, disc, sources.SnapshotState())
        row, items = news.tool("slotfather", discovered=known, runner=runner)
        self.assertEqual(news.Installed("slotfather", "0.2.0", "versions", "slotfather changelog --json exited 127"),
                         row)
        self.assertEqual([], items)
        self.assertEqual([("slotfather", ["changelog", "--json"])], calls,
                         "reused discovered executable and version without calling which or capabilities --json")


class PureTransitionTableTests(unittest.TestCase):
    """Table tests for pure Discovery / SnapshotState / ToolState transitions."""

    GOOD_CAPS: dict[str, Any] = {
        "v": 1,
        "tool": "lanefather",
        "version": "0.4.0",
        "dashboard": {
            "v": 1,
            "tag": "lane",
            "snapshot": {"argv": ["status", "--json"], "versions": [1], "poll": 7},
            "events": {"path": "/state/lanefather/events.jsonl", "versions": [1]},
            "settings": {"versions": [1]},
            "actions": ["park"],
            "show": {"versions": [1]},
        },
    }

    def test_discovery_due_and_on_path_checked_table(self) -> None:
        disc = sources.Discovery(state="ok", executable="/bin/lane", resolved="/rel/v1", checked_at=100.0,
                                 looked_at=100.0, generation=1)
        self.assertFalse(sources.discovery_due(disc, 105.0), "within 10s resolve window")
        self.assertTrue(sources.discovery_due(disc, 110.0), "at 10s without resolved arg triggers path check")
        cases = [
            ("at 10s with unchanged resolved path", 110.0, "/rel/v1", False),
            ("at 10s with new resolved release", 110.0, "/rel/v2", True),
            ("at 60s full discovery interval even if path unchanged", 160.0, "/rel/v1", True),
        ]
        for label, now, resolved, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(expected, sources.discovery_due(disc, now, resolved))
        checked = sources.on_path_checked(disc, "/rel/v1", 110.0)
        self.assertEqual((100.0, 110.0), (checked.checked_at, checked.looked_at))
        self.assertFalse(sources.discovery_due(checked, 115.0))

    def test_absent_and_unsupported_clear_snapshot_while_transient_failure_preserves_it(self) -> None:
        ok_disc = sources.on_discovered(sources.Discovery(), "lanefather", lanefather, "/bin/lane", "/rel/v1",
                                        0, self.GOOD_CAPS, "", 10.0)
        good_snap = sources.on_snapshot(ok_disc, sources.SnapshotState(), {"projects": []}, None, 1000.0,
                                        expected_generation=ok_disc.generation)
        self.assertEqual("ok", sources.build_tool_state("lanefather", lanefather, ok_disc, good_snap).state)

        cases: list[tuple[str, Optional[str], int, Any, str, str, bool, Optional[str]]] = [
            (
                "transient timeout while ok preserves capabilities and snapshot, marks stale",
                "/bin/lane", 124, None, "lanefather capabilities --json took longer than 2 s",
                "stale", True, "lanefather capabilities --json took longer than 2 s",
            ),
            (
                "transition to absent clears capabilities and snapshot",
                None, 127, None, "",
                "absent", False, None,
            ),
            (
                "transition to unsupported snapshot version clears snapshot",
                "/bin/lane", 0, {
                    "v": 1, "version": "0.5.0",
                    "dashboard": {"v": 1, "snapshot": {"argv": ["status", "--json"], "versions": [99]}},
                }, "",
                "unsupported", False, "lanefather 0.5.0: its status snapshot needs a newer godfather",
            ),
        ]
        for label, exe, code, payload, problem, exp_state, keep_snap, exp_problem in cases:
            with self.subTest(label=label):
                new_disc = sources.on_discovered(ok_disc, "lanefather", lanefather, exe, exe, code, payload,
                                                 problem, 70.0)
                new_snap = sources.reconcile_discovery(ok_disc, new_disc, good_snap, 1060.0)
                view = sources.build_tool_state("lanefather", lanefather, new_disc, new_snap)
                self.assertEqual(exp_state, view.state)
                self.assertEqual(exp_problem, view.problem)
                self.assertEqual(keep_snap, view.snapshot is not None)
                if not keep_snap:
                    self.assertIsNone(view.snapshot_at)
                    self.assertIsNone(view.failed_at)

    def test_partial_capabilities_retain_events_settings_actions_and_show_when_snapshot_unsupported(self) -> None:
        partial_caps: dict[str, Any] = {
            "v": 1,
            "tool": "lanefather",
            "version": "0.9.0",
            "dashboard": {
                "v": 1,
                "tag": "ln",
                "snapshot": {"argv": ["status", "--json"], "versions": [99], "poll": 8},
                "events": {"path": "/var/log/lane/events.jsonl", "versions": [1]},
                "settings": {"versions": [1]},
                "actions": ["park"],
                "show": {"versions": [1]},
            },
        }
        disc = sources.on_discovered(sources.Discovery(), "lanefather", lanefather, "/bin/lane", "/bin/lane",
                                     0, partial_caps, "", 20.0)
        view = sources.build_tool_state("lanefather", lanefather, disc, sources.SnapshotState())
        self.assertEqual("unsupported", view.state)
        self.assertIn("needs a newer godfather", str(view.problem))
        self.assertEqual("0.9.0", view.version)
        self.assertEqual("ln", view.tag)
        self.assertEqual(8.0, view.poll_interval)
        self.assertEqual("/var/log/lane/events.jsonl", view.event_log)
        self.assertIsNone(view.event_problem)
        self.assertTrue(view.settings_supported)
        self.assertIsNone(view.settings_problem)
        self.assertEqual(("park",), view.actions)
        self.assertTrue(view.show_supported)

    def test_on_snapshot_drops_stale_generation_and_preserves_first_failed_at(self) -> None:
        disc_v1 = sources.on_discovered(sources.Discovery(), "lanefather", lanefather, "/bin/lane", "/bin/lane",
                                        0, self.GOOD_CAPS, "", 10.0)
        snap_v1 = sources.on_snapshot(disc_v1, sources.SnapshotState(), {"projects": ["v1"]}, None, 500.0,
                                      expected_generation=disc_v1.generation)
        disc_v2 = sources.on_discovered(disc_v1, "lanefather", lanefather, "/bin/lane", "/bin/lane",
                                        0, self.GOOD_CAPS, "", 70.0)
        # Stale poll from generation v1 must be ignored (both success and failure).
        dropped_good = sources.on_snapshot(disc_v2, snap_v1, {"projects": ["stale"]}, None, 505.0,
                                           expected_generation=disc_v1.generation)
        self.assertEqual(snap_v1, dropped_good)
        dropped_err = sources.on_snapshot(disc_v2, snap_v1, None, "old error", 505.0,
                                          expected_generation=disc_v1.generation)
        self.assertEqual(snap_v1, dropped_err)

        # Matching generation failure marks stale and records failed_at; second failure keeps first failed_at.
        failed_1 = sources.on_snapshot(disc_v2, snap_v1, None, "timeout 1", 510.0,
                                       expected_generation=disc_v2.generation)
        self.assertEqual(({"projects": ["v1"]}, 510.0, "timeout 1"),
                         (failed_1.snapshot, failed_1.failed_at, failed_1.problem))
        failed_2 = sources.on_snapshot(disc_v2, failed_1, None, "timeout 2", 525.0,
                                       expected_generation=disc_v2.generation)
        self.assertEqual((510.0, "timeout 2"), (failed_2.failed_at, failed_2.problem))

    def test_poll_interval_clamping_table(self) -> None:
        for hint, expected in ((None, 5.0), (True, 5.0), ("fast", 5.0), (1, 5.0), (7.5, 7.5), (30, 10.0)):
            caps = {"dashboard": {"snapshot": {"poll": hint}}} if hint is not None else None
            self.assertEqual(expected, sources.poll_interval(caps), f"hint={hint!r}")


class ConcurrencyTests(unittest.TestCase):
    """Thread-safety tests for ToolClient (single-flight discover, lock-free I/O, concurrent refresh/poll_fresh)
    and ShowCache."""

    def test_single_flight_discover_runs_capabilities_once_and_state_never_blocks_on_io(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        barrier = threading.Barrier(4)
        cap_calls = 0
        count_lock = threading.Lock()

        class CoordinatedRunner(sources.SystemRunner):
            def which(self, tool: str) -> Optional[str]:
                return f"/bin/{tool}"

            def realpath(self, path: str) -> str:
                return path

            def run_json(self, tool: str, argv: list[str], timeout: Optional[float],
                         executable: Optional[str] = None) -> tuple[int, Any, str]:
                nonlocal cap_calls
                del tool, argv, timeout, executable
                with count_lock:
                    cap_calls += 1
                entered.set()
                release.wait(5.0)
                return 0, PureTransitionTableTests.GOOD_CAPS, ""

        client = sources.ToolClient("lanefather", lanefather, runner=CoordinatedRunner(), clock=lambda: 100.0)
        results: list[sources.ToolState] = []
        results_lock = threading.Lock()

        def worker() -> None:
            barrier.wait(5.0)
            found = client.discover()
            with results_lock:
                results.append(found)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(5.0), "first thread entered capabilities I/O")
        # While discovery I/O is blocked, state() must not block because _state_lock is not held across I/O.
        live_state = client.state()
        self.assertEqual("absent", live_state.state)
        release.set()
        for thread in threads:
            thread.join(5.0)
        self.assertEqual(1, cap_calls, "single-flight discovery ran capabilities --json once across 4 threads")
        self.assertEqual(4, len(results))
        self.assertTrue(all(item.state == "ok" and item.version == "0.4.0" for item in results))

    def test_concurrent_refresh_poll_fresh_and_state_keep_snapshot_isolated(self) -> None:
        barrier = threading.Barrier(3)
        polls = 0
        lock = threading.Lock()
        raw_snapshot = json.loads((ROOT / "tests" / "fixtures" / "procs" / "junkfather" / "replies.json").read_text())
        caps = raw_snapshot["capabilities --json"]["json"]
        status = raw_snapshot["status --json"]["json"]

        class FastRunner(sources.SystemRunner):
            def which(self, tool: str) -> Optional[str]:
                return f"/bin/{tool}"

            def realpath(self, path: str) -> str:
                return path

            def run_json(self, tool: str, argv: list[str], timeout: Optional[float],
                         executable: Optional[str] = None) -> tuple[int, Any, str]:
                nonlocal polls
                del tool, timeout, executable
                if argv == ["capabilities", "--json"]:
                    return 0, caps, ""
                with lock:
                    polls += 1
                return 0, status, ""

        client = sources.ToolClient("junkfather", junkfather, runner=FastRunner(), clock=lambda: 50.0)
        client.discover()
        errors: list[BaseException] = []

        def refresher() -> None:
            try:
                barrier.wait(5.0)
                for _ in range(15):
                    state = client.refresh()
                    assert state.state == "ok" and state.snapshot is not None
            except BaseException as err:  # noqa: BLE001
                errors.append(err)

        def fresh_poller() -> None:
            try:
                barrier.wait(5.0)
                for _ in range(15):
                    fresh = client.poll_fresh()
                    assert "entries" in fresh
            except BaseException as err:  # noqa: BLE001
                errors.append(err)

        def reader() -> None:
            try:
                barrier.wait(5.0)
                for _ in range(30):
                    state = client.state()
                    assert state.state == "ok"
            except BaseException as err:  # noqa: BLE001
                errors.append(err)

        threads = [threading.Thread(target=fn) for fn in (refresher, fresh_poller, reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)
        self.assertEqual([], errors)
        self.assertEqual(30, polls)

    def test_show_cache_is_thread_safe_and_enforces_ttl_and_limit(self) -> None:
        now = [100.0]
        cache = lanefather.ShowCache(ttl=5.0, limit=8, clock=lambda: now[0])
        barrier = threading.Barrier(4)
        errors: list[BaseException] = []

        def worker(offset: int) -> None:
            try:
                barrier.wait(5.0)
                for i in range(25):
                    key = (f"/dir/{(offset + i) % 12}", f"lane-{i % 6}", "state-v1")
                    cache.put(key, {"id": key[1], "n": i})
                    found = cache.get(key)
                    assert found is None or "id" in found
            except BaseException as err:  # noqa: BLE001
                errors.append(err)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)
        self.assertEqual([], errors)
        self.assertLessEqual(len(cache), 8)
        cache.put(("/p", "l1", "state-v1"), {"id": "l1"})
        self.assertEqual({"id": "l1"}, cache.get(("/p", "l1", "state-v1")))
        now[0] += 5.0
        self.assertIsNone(cache.get(("/p", "l1", "state-v1")), "expired entry evicted on read")

    def test_slotfather_notice_flows_through_model_loaded_into_overview_render(self) -> None:
        notice = ("No tool found. Godfather looked for these on PATH:", "slotfather  not found")
        state, _ = reducer.reduce(
            state_module.AppState(),
            state_module.ModelLoaded(None, 6, None, notice),
            1.0,
        )
        self.assertEqual(notice, state.slotfather_notice)
        canvas = tui.Canvas(100, 20)
        overview.render(canvas, state, 1.0)
        rendered = canvas.render(False)
        self.assertIn("No tool found. Godfather looked for these on PATH:", rendered)
        self.assertIn("slotfather  not found", rendered)


if __name__ == "__main__":
    unittest.main()
