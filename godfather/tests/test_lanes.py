"""Lanefather as a source (fathers#85): its status snapshot decoded, the lanes summary, the joins of a request to a
lane (state directory, worktree fallback, never by id alone across projects), `show` asked only where advertised,
the Lanes page's keys and links through the reducer, `--once --page lanes`, the live page in a PTY, and the real
Lanefather's `status --json` in an isolated registry."""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

import helpers
from helpers import ROOT
from test_state import entry, model

from godfather import pages, app, reducer, sources, state as state_module, tui
from godfather.adapters import lanefather
from godfather.model import Dashboard, Lanes
from godfather.views import overview

LANES = ROOT / "tests" / "fixtures" / "lanes"
STRAYLIGHT, GRID = "/fixture/straylight/lanes", "/fixture/data-grid/lanes"


def snapshot() -> dict[str, Any]:
    replies = json.loads((LANES / "lanefather" / "replies.json").read_text())
    return replies["status --json"]["json"]


def decoded() -> Lanes:
    return lanefather.decode(snapshot())


def request(lane: str = "", state_dir: str = "", workdir: str = "", **fields: Any) -> dict[str, Any]:
    context = {"lane": lane} if lane else {}
    if state_dir:
        context["lanefather.state"] = state_dir
    return entry(0, context=context, workdir=workdir, **fields)


class DecodeTests(unittest.TestCase):
    def test_projects_lanes_and_an_error_project(self) -> None:
        found = decoded()
        self.assertEqual(["Straylight", "data-grid", "sid-kit"], [project["project"] for project in found["projects"]])
        self.assertIn("config not found", found["projects"][2].get("error", ""))
        lane = found["projects"][0]["lanes"][0]
        self.assertEqual(((STRAYLIGHT, "644-build-once"), "Straylight"), (lane["key"], lane["project"]))
        self.assertEqual("in progress", lane["receipt"]["status"])

    def test_another_version_or_shape_is_a_snapshot_error(self) -> None:
        cases: list[Any] = [{"v": 2, "projects": []}, {"v": 1}, [], None]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(sources.SnapshotError):
                lanefather.decode(bad)

    def test_the_summary_counts_and_the_red_gate(self) -> None:
        self.assertEqual({"active": 4, "planned": 1, "reviews": 1, "red": True}, lanefather.summary(decoded()))
        green = snapshot()
        green["projects"][0]["gate"]["status"] = "green"
        self.assertFalse(lanefather.summary(lanefather.decode(green))["red"])

    def test_capabilities_without_a_status_snapshot_are_unsupported(self) -> None:
        good = {"v": 1, "dashboard": {"v": 1, "snapshot": {"argv": ["status", "--json"], "versions": [1]}}}
        self.assertIsNone(lanefather.unsupported(good))
        self.assertIn("update lanefather", str(lanefather.unsupported({"v": 1, "dashboard": {"v": 1}})))
        newer = {"v": 1, "dashboard": {"v": 1, "snapshot": {"argv": ["status", "--json"], "versions": [2]}}}
        self.assertIn("newer godfather", str(lanefather.unsupported(newer)))
        self.assertFalse(lanefather.show_supported(good))
        self.assertTrue(lanefather.show_supported({"dashboard": {"show": {"versions": [1]}}}))


class JoinTests(unittest.TestCase):
    def test_by_the_state_directory(self) -> None:
        lane = lanefather.lane_of(request("644-build-once", STRAYLIGHT), decoded())
        self.assertEqual((STRAYLIGHT, "644-build-once"), lane and lane["key"])
        lane = lanefather.lane_of(request("644-build-once", GRID + "/"), decoded())
        self.assertEqual((GRID, "644-build-once"), lane and lane["key"], "the other project's lane of that id")

    def test_with_only_the_lane_by_the_worktree(self) -> None:
        inside = request("644-build-once", workdir="/fixture/data-grid/worktrees/644-build-once/src")
        self.assertEqual(GRID, (lanefather.lane_of(inside, decoded()) or {}).get("state_dir"))
        held = request("644-build-once")
        self.assertEqual(STRAYLIGHT, (lanefather.lane_of(held, decoded(),
                                                         "/fixture/straylight/worktrees/644-build-once")
                                      or {}).get("state_dir"), "the holder's directory when it names none")

    def test_never_by_the_id_alone(self) -> None:
        for item in (request("644-build-once"), request("644-build-once", workdir="/elsewhere"),
                     request("644-build-once", "/fixture/unknown/lanes"),
                     request("644-build-once", workdir="/fixture/straylight/worktrees/644-build-once-other")):
            with self.subTest(item=item["context"], workdir=item["workdir"]):
                self.assertIsNone(lanefather.lane_of(item, decoded()))

    def test_a_lanes_leases(self) -> None:
        items = [request("644-build-once", STRAYLIGHT), request("644-build-once", GRID), request()]
        lane = lanefather.find(decoded(), (STRAYLIGHT, "644-build-once"))
        assert lane is not None
        self.assertEqual([0], lanefather.leases_of(lane, items))


def fake_lanefather(folder: str, lane: str, calls: Path) -> Path:
    """A `lanefather` in `folder` that records its argv in `calls` and shows `lane` of project `p`."""
    command = Path(folder) / "lanefather"
    command.write_text("#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n"
                       + f"Path({str(calls)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                       + 'print(json.dumps({"version":1,"id":' + json.dumps(lane) + ',"project":"p","status":"active"}))\n')
    command.chmod(0o755)
    return command


class ShowTests(unittest.TestCase):
    """Hermetic: PATH is only the fake's folder, so a lanefather installed on the machine never answers, and the
    response cache starts empty."""

    def setUp(self) -> None:
        lanefather._DEFAULT_CACHE.clear()  # pyright: ignore[reportPrivateUsage]
        self.addCleanup(lanefather._DEFAULT_CACHE.clear)  # pyright: ignore[reportPrivateUsage]
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("GODFATHER_FIXTURES", None)

    def test_show_is_asked_only_of_a_lanefather_that_advertises_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls"
            fake_lanefather(tmp, "shown", calls)
            with mock.patch.dict(os.environ, {"PATH": tmp}):
                context = {"lane": "shown", "lanefather.state": "/project/state"}
                found = lanefather.fetch(context, "", {"v": 1, "dashboard": {"v": 1}})
                self.assertEqual({"error": "lanefather too old"}, found)
                self.assertFalse(calls.exists(), "never queried")
                found = lanefather.fetch(context, "", {"v": 1, "dashboard": {"v": 1, "show": {"versions": [1]}}})
                self.assertEqual("p", found.get("project"))

    def test_a_cached_summary_survives_lanefather_leaving_path(self) -> None:
        # CI run 43841660797: the `not installed` check ran before the cache, so a transient PATH change dropped a
        # fresh summary; a lanefather in ~/.local/bin hid it on developer machines.
        shows = {"v": 1, "dashboard": {"v": 1, "show": {"versions": [1]}}}
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls"
            command = fake_lanefather(tmp, "kept", calls)
            context = {"lane": "kept", "lanefather.state": "/project/state"}
            with mock.patch.dict(os.environ, {"PATH": tmp}):
                self.assertEqual("p", lanefather.fetch(context, "", shows).get("project"))
                command.unlink()
                calls.unlink()
                self.assertEqual("p", lanefather.fetch(context, "", shows).get("project"), "lanefather left PATH")
                self.assertEqual("p", lanefather.fetch(context, "", None).get("project"), "its capabilities went too")
                self.assertFalse(calls.exists(), "the cached summary asks nothing")
                self.assertEqual({"error": "lanefather not installed"},
                                 lanefather.fetch({"lane": "kept", "lanefather.state": "/other/state"}, "", shows),
                                 "another project has no cached summary to reuse")


class Case(unittest.TestCase):
    """The reducer over the lanes fixture's snapshot and a drawn screen."""

    def setUp(self) -> None:
        self.tools = {"lanefather": state_module.Tool("lanefather", "ok", None, decoded())}

    def screen(self, items: list[dict[str, Any]] | None = None, width: int = 156, height: int = 43,
               **fields: Any) -> Any:
        found = model(items if items is not None else [request("644-build-once", STRAYLIGHT), entry(1)])
        state = dataclasses.replace(state_module.AppState(), tools=self.tools, **fields)
        state, _ = reducer.reduce(state, state_module.ModelLoaded(cast(Dashboard, found), 8), 0.0)
        return self.draw(self.loaded(state), width, height)

    def loaded(self, state: Any) -> Any:
        """The selected request's slow facts arrived (no lane from `show`, no holder directory)."""
        item = pages.selected_item(state)
        if item is None:
            return state
        key = state_module.details_key(item, state.selection.key)
        return reducer.reduce(state, state_module.DetailsLoaded(key), 0.0)[0]

    def draw(self, state: Any, width: int = 156, height: int = 43) -> Any:
        canvas = tui.Canvas(width, height)
        overview.render(canvas, state, 0.0)
        self.canvas = canvas
        state, _ = reducer.reduce(state, state_module.Drawn(helpers.frame_of(canvas)), 0.0)
        return state

    def keys(self, state: Any, *keys: str) -> Any:
        for key in keys:
            state, _ = reducer.reduce(state, state_module.Key(key), 0.0)
            state = self.draw(self.loaded(state))
        return state

    def click(self, state: Any, x: int, y: int) -> Any:
        state, _ = reducer.reduce(state, state_module.Mouse(0, x, y, True), 0.0)
        return self.draw(state)

    def text(self) -> str:
        return self.canvas.render(False)

    def at(self, text: str) -> tuple[int, int]:
        for y, row in enumerate(self.canvas.cells):
            line = "".join(str(cell[0]) for cell in row)
            if text in line:
                return line.index(text), y
        raise AssertionError(f"{text!r} is not on screen")

    def picked(self, state: Any) -> Any:
        rows = pages.page_rows(state, "lanes")
        index = pages.page_index(state, "lanes")
        return rows[index]["key"] if index is not None else None


class PageTests(Case):
    def test_2_opens_the_page_on_the_selected_requests_lane_and_esc_returns(self) -> None:
        state = self.keys(self.screen(), "2")
        self.assertEqual(("lanes", "lanes"), (state.page, state.focus))
        self.assertEqual((STRAYLIGHT, "644-build-once"), self.picked(state))
        self.assertIn("LANE DETAILS", self.text())
        state = self.keys(state, "esc")
        self.assertEqual(("overview", 0, "requests"), (state.page, state.selection.index, state.focus))

    def test_without_a_link_the_first_lane_and_arrows_skip_project_rows(self) -> None:
        state = self.keys(self.screen([entry(1)]), "2")
        self.assertEqual((STRAYLIGHT, "644-build-once"), self.picked(state))
        state = self.keys(state, "G")
        self.assertEqual((GRID, "644-build-once"), self.picked(state), "the last lane; sid-kit's error row is none")
        state = self.keys(state, "k")
        self.assertEqual((STRAYLIGHT, "555-host-rename-names"), self.picked(state), "data-grid's project row skipped")

    def test_2_does_nothing_without_lanefather_and_the_hint_is_gone(self) -> None:
        self.tools = {}
        state = self.keys(self.screen(), "2")
        self.assertEqual("overview", state.page)
        self.assertNotIn("pages", self.text())
        self.tools = {"lanefather": state_module.Tool("lanefather", "ok", None, decoded())}
        self.screen()
        self.assertIn("1 2 pages", self.text())

    def test_unsupported_says_why_on_the_page(self) -> None:
        self.tools = {"lanefather": state_module.Tool("lanefather", "unsupported", "lanefather 0.3.0: no dashboard "
                                                                                  "contract; update lanefather")}
        state = self.keys(self.screen(), "2")
        self.assertEqual("lanes", state.page)
        self.assertIn("update lanefather", self.text())
        self.assertNotIn("2 lanes", self.text().split("\n")[4], "no summary")

    def test_stale_is_dimmed_with_its_time(self) -> None:
        since = datetime.datetime(2026, 11, 4, 9, 0, tzinfo=datetime.UTC)
        self.tools = {"lanefather": state_module.Tool("lanefather", "stale", "timed out", decoded(), since)}
        self.keys(self.screen(), "2")
        self.assertRegex(self.text(), r"stale since \d\d:\d\d")
        self.assertEqual({"dim"}, {cell[1] for row in self.canvas.cells[:self.canvas.height - 1] for cell in row
                                   if str(cell[0]).strip()})

    def test_the_error_project_shows_and_the_rest_works(self) -> None:
        self.keys(self.screen(), "2")
        self.assertIn("sid-kit · config not found", self.text())
        self.assertIn("data-grid · 1 of 2 lanes", self.text())


class LinkTests(Case):
    def test_the_summary_counts_and_opens_the_page(self) -> None:
        state = self.screen()
        self.assertIn("2 lanes 4 active · 1 planned · 1 review · 🔥 gate red", self.text())
        state = self.click(state, *self.at("2 lanes"))
        self.assertEqual("lanes", state.page)

    def test_the_lane_row_in_details_opens_the_page_on_that_lane(self) -> None:
        state = self.screen([entry(1), request("644-build-once", GRID)])
        state = self.keys(state, "j")
        self.assertIn("644-build-once · data-grid", self.text())
        state = self.click(state, *self.at("644-build-once · data-grid"))
        self.assertEqual(("lanes", (GRID, "644-build-once")), (state.page, self.picked(state)))
        state = self.keys(state, "esc")
        self.assertEqual(("overview", 1), (state.page, state.selection.index), "back, the request still selected")

    def test_a_lease_in_lane_details_links_back_to_the_request(self) -> None:
        state = self.keys(self.screen([entry(1), request("644-build-once", STRAYLIGHT, label="the-lease")]), "2")
        self.assertEqual((STRAYLIGHT, "644-build-once"), self.picked(state))
        state = self.click(state, *self.at("the-lease"))
        self.assertEqual(("overview", 1), (state.page, state.selection.index))

    def test_a_request_of_no_known_lane_is_no_link(self) -> None:
        state = self.screen([request("644-build-once")])
        self.assertNotIn("· Straylight", self.text())
        self.assertFalse([target for target in state.frame.targets if target[3][0] == "lanes" and len(target[3]) > 1])


class OnceTests(unittest.TestCase):
    def run_once(self, *argv: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GODFATHER_", "SLOTFATHER_"))}
        env.update(GODFATHER_FIXTURES=str(LANES), PYTHONPATH=str(ROOT / "src"), TZ="UTC", NO_COLOR="1")
        return subprocess.run([sys.executable, "-m", "godfather", "--once", *argv], capture_output=True, text=True,
                              env=env, timeout=60)

    def test_once_draws_the_lanes_page(self) -> None:
        found = self.run_once("--page", "lanes", "--width", "156", "--height", "43")
        self.assertEqual(0, found.returncode, found.stderr)
        self.assertIn("LANES", found.stdout)
        self.assertRegex(found.stdout, r"┃\s+▶️\s+644-build-once")

    def test_an_unknown_page_is_a_usage_error(self) -> None:
        found = self.run_once("--page", "nowhere")
        self.assertEqual(2, found.returncode)
        self.assertIn("--page takes overview, lanes", found.stderr)


class LiveTests(unittest.TestCase):
    def test_2_esc_and_a_click_on_the_lane_row_in_a_pty(self) -> None:
        import terminal

        env = {key: value for key, value in os.environ.items() if not key.startswith(("GODFATHER_", "SLOTFATHER_"))}
        env.update(GODFATHER_FIXTURES=str(LANES), TZ="UTC")
        screen = terminal.Terminal(self, env, 43, 156, [sys.executable, str(terminal.ENTRY), "--interval", "0.5"])
        screen.receive("2 lanes 4 active", "644-build-once · Straylight")
        at = screen.mark()
        screen.write("2")
        screen.receive("LANE DETAILS", "sid-kit · config not found", since=at)
        at = screen.mark()
        screen.write("\x1b")
        screen.receive("LEASE DETAILS", since=at)
        screen.until(lambda _: any("644-build-once · Straylight" in line for line in screen.rows().values()),
                     what="the lane row again")
        row, line = next((row, line) for row, line in screen.rows().items() if "644-build-once · Straylight" in line)
        at = screen.mark()
        screen.click(line.index("644-build-once · Straylight") + 2, row)
        screen.receive("LANE DETAILS", since=at)
        self.assertEqual(0, screen.quit())


class ContractTests(unittest.TestCase):
    """The real Lanefather (the sibling checkout) in an isolated registry: `status --json` decodes, an unreadable
    project is an error entry, and its capabilities name a snapshot this Godfather reads."""

    def test_status_and_capabilities(self) -> None:
        package = ROOT.parent / "lanefather"
        python = shutil.which("python3") or sys.executable
        if not (package / "lanefather" / "status.py").is_file():
            self.skipTest("no lanefather checkout beside godfather")
        tmp = Path(tempfile.mkdtemp(prefix="godfather-lanefather-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "empty").mkdir()
        seen = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        registry = tmp / "projects.json"
        registry.write_text(json.dumps({"v": 1, "projects": [
            {"state_dir": str(tmp / "empty"), "project": "empty", "seen": seen},
            {"state_dir": str(tmp / "gone"), "config": str(tmp / "gone" / ".lanefather.toml"), "project": "gone",
             "seen": seen}]}))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "PYTHONPATH": str(package),
               "LANEFATHER_REGISTRY": str(registry)}

        def run(*argv: str) -> Any:
            found = subprocess.run([python, "-m", "lanefather", *argv], capture_output=True, text=True, env=env,
                                   cwd=tmp, timeout=60)
            if found.returncode != 0:
                self.skipTest(f"lanefather cannot run here: {found.stderr.strip().splitlines()[-1:]}")
            return json.loads(found.stdout)

        capabilities = run("capabilities", "--json")
        self.assertIsNone(lanefather.unsupported(capabilities))
        found = lanefather.decode(run("status", "--json"))
        by_name = {project["project"]: project for project in found["projects"]}
        self.assertEqual([], by_name["empty"]["lanes"])
        self.assertIn("error", by_name["gone"])
        self.assertEqual({"active": 0, "planned": 0, "reviews": 0, "red": False}, lanefather.summary(found))


class SourceTests(unittest.TestCase):
    def test_load_tool_from_the_fixture_and_stale_after_a_failure(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="godfather-lanes-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        shutil.copytree(LANES, tmp / "lanes")
        with mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(tmp / "lanes")}):
            screen = app.Screen()
            tool = screen.load_tool("lanefather")
            self.assertEqual("ok", tool.state)
            self.assertEqual(5.0, screen.poll_every("lanefather"))
            replies = json.loads((tmp / "lanes" / "lanefather" / "replies.json").read_text())
            replies["status --json"] = {"code": 1, "stderr": "lanefather: broken"}
            (tmp / "lanes" / "lanefather" / "replies.json").write_text(json.dumps(replies))
            tool = screen.load_tool("lanefather")
            self.assertEqual(("stale", "lanefather: broken"), (tool.state, tool.problem))
            self.assertIsNotNone(tool.snapshot, "the last good snapshot stays")
            self.assertTrue(re.match(r"\d{4}-", str(tool.stale_since)))


if __name__ == "__main__":
    unittest.main()
