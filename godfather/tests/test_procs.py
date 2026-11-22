"""Junkfather as a source (fathers#86): its status snapshot decoded (versions 1 and 2), the procs summary, the joins
of an entry to a request (pid in its tree, its lane) and to a lane (lane id and worktree), DETAILS' processes rows and
the Processes page's links through the reducer, the event formatter over every kind, `--once --page processes`, the
page in a PTY, and the real Junkfather's `status --json` in a temporary state directory."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from helpers import ROOT
from test_lanes import STRAYLIGHT, Case, decoded as lanes_decoded, request
from test_state import entry

from godfather import events, sources, state as state_module
from godfather.adapters import junkfather, lanefather
from godfather.model import Processes

PROCS = ROOT / "tests" / "fixtures" / "procs"


def snapshot() -> dict[str, Any]:
    return json.loads((PROCS / "junkfather" / "replies.json").read_text())["status --json"]["json"]


def decoded() -> Processes:
    return junkfather.decode(snapshot())


class DecodeTests(unittest.TestCase):
    def test_states_order_and_the_summary(self) -> None:
        rows = junkfather.page_rows(decoded())
        self.assertEqual(["running", "running", "running", "orphan", "expired"],
                         [row["state"] for row in rows if row["kind"] == "entry"])
        self.assertEqual(["entry"] * 5 + ["separator", "unowned", "separator", "resource", "resource"],
                         [row["kind"] for row in rows])
        self.assertEqual(["unowned processes · 1 · last sweep", "unowned resources · 2 · last sweep"],
                         [row["label"] for row in rows if row["kind"] == "separator"])
        self.assertEqual({"count": 5, "orphan": 1, "expired": 1, "unowned": 1, "resources": 2},
                         junkfather.summary(decoded()))

    def test_dead_sweep_entries_and_resources_are_separated_from_live_unowned(self) -> None:
        raw: dict[str, Any] = {
            "v": 2, "host": "mbp", "time": "2026-11-04T09:59:39Z", "entries": [],
            "unowned": [
                {"pid": 101, "start": "t1", "cmd": "node live", "why": "orphan", "age": 120, "alive": True},
                {"pid": 102, "start": "t2", "cmd": "node gone", "why": "orphan", "age": 600, "alive": False},
                {"pid": 103, "start": "t3", "cmd": "node unknown", "why": "orphan", "age": 300, "alive": None},
            ],
            "unowned_resources": [
                {"kind": "worktree", "id": "/repo/wt1", "name": "wt1", "why": "merged, idle 4d"},
                {"kind": "tailscale", "id": "https://box/app", "name": "app", "why": "no owner"},
            ],
            "sweep": {"time": "2026-11-04T09:30:00Z", "result": {"reaped": [], "failed": []}},
            "errors": [],
        }
        found = junkfather.decode(raw)
        self.assertEqual({"count": 0, "orphan": 0, "expired": 0, "unowned": 2, "resources": 2},
                         junkfather.summary(found))
        rows = junkfather.page_rows(found)
        self.assertEqual(["separator", "unowned", "unowned", "separator", "unowned", "separator", "resource",
                          "resource"], [row["kind"] for row in rows])
        self.assertEqual(["unowned processes · 2 · last sweep", "gone since the sweep · 1",
                          "unowned resources · 2 · last sweep"],
                         [row["label"] for row in rows if row["kind"] == "separator"])
        self.assertEqual([101, 103, 102], [row["pid"] for row in rows if row["kind"] == "unowned"])

    def test_version_1_reads_too_and_another_is_an_error(self) -> None:
        older = dict(snapshot(), v=1)
        older.pop("cpu")
        self.assertEqual(5, len(junkfather.decode(older)["entries"]))
        cases: list[Any] = [dict(snapshot(), v=3), {"v": 2}, None]
        for bad in cases:
            with self.subTest(bad=str(bad)[:40]), self.assertRaises(sources.SnapshotError):
                junkfather.decode(bad)

    def test_a_resource_entry_keeps_its_kind_and_id(self) -> None:
        simulator = junkfather.find(decoded(), ("entry", "j8d9e0f"))
        assert simulator is not None and simulator["kind"] == "entry"
        self.assertEqual("simulator", (simulator["resource"] or {})["kind"])
        self.assertIsNone(simulator["pid"])

    def test_no_ttl_flag_is_preserved_and_defaults_to_false(self) -> None:
        sentinel = 100 * 365 * 86400.0
        raw = snapshot()
        # Older snapshots without `no_ttl` keep working unchanged and default to False
        self.assertTrue(all(found["no_ttl"] is False for found in junkfather.entries(decoded())))
        raw["entries"] = [
            {
                "id": "jbg0001", "state": "running", "owner": "claude 4bceaaaf", "session": "4bceaaaf",
                "agent": None, "lane": None, "owner_alive": True, "age": 120, "ttl": sentinel,
                "ttl_left": sentinel - 120, "no_ttl": True, "keep": "owner", "pid": 44100, "pgid": 44100,
                "processes": 1, "label": "bg dev", "mode": "background", "cwd": "/fixture/straylight",
                "argv": ["bun", "run", "dev"], "log": None, "end": None, "exit": None, "resource": None,
            },
            {
                "id": "jbg0002", "state": "running", "owner": "claude 4bceaaaf", "owner_alive": True,
                "age": 120, "ttl": sentinel, "ttl_left": -10, "no_ttl": True,
            },
            {
                "id": "jlegacy", "state": "running", "owner": "claude 4bceaaaf", "owner_alive": True,
                "age": 120, "ttl": 7200, "ttl_left": 3600, "no_ttl": "true",
            },
        ]
        found = junkfather.decode(raw)
        by_id = {row["id"]: row for row in junkfather.entries(found)}
        self.assertTrue(by_id["jbg0001"]["no_ttl"])
        self.assertEqual("running", by_id["jbg0001"]["state"])
        self.assertEqual("—", junkfather.ttl_text(by_id["jbg0001"], 30.0))
        self.assertTrue(by_id["jbg0002"]["no_ttl"])
        self.assertEqual("running", by_id["jbg0002"]["state"])
        self.assertEqual("—", junkfather.ttl_text(by_id["jbg0002"]))
        self.assertFalse(by_id["jlegacy"]["no_ttl"])
        self.assertEqual("1h00m", junkfather.ttl_text(by_id["jlegacy"]))


class JoinTests(unittest.TestCase):
    def test_pid_in_the_tree_and_the_group_leader(self) -> None:
        item = entry(0, pid=10000, tree=[(10000, 1.0, 1, "sh"), (90412, 1.0, 1, "bun")])
        self.assertEqual({"j5e6f7a", "j3f9a1c"}, {found["id"] for found in junkfather.of_request(item, decoded(),
                                                                                                    None)})

    def test_by_the_requests_lane_and_its_worktree(self) -> None:
        lane = lanefather.find(lanes_decoded(), (STRAYLIGHT, "710-speech-paste-phase"))
        assert lane is not None
        self.assertEqual(["j3f9a1c"], [found["id"] for found in junkfather.of_request(entry(0, pid=1), decoded(), lane)])
        self.assertEqual(["j3f9a1c"], [found["id"] for found in junkfather.of_lane(lane, decoded())])

    def test_the_same_lane_id_in_another_project_takes_none(self) -> None:
        other = dict(lanefather.lanes(lanes_decoded())[1], worktree="/fixture/data-grid/worktrees/710", key=("x", "y"))
        self.assertEqual([], junkfather.of_lane(other, decoded()), "lane id alone is not enough")

    def test_an_entrys_lease_and_lane(self) -> None:
        vite = junkfather.find(decoded(), ("entry", "j3f9a1c"))
        assert vite is not None
        self.assertEqual("710-speech-paste-phase", (junkfather.lane_of(vite, lanes_decoded()) or {}).get("id"))
        items = [entry(0, pid=5), entry(1, pid=6, tree=[(90412, 1.0, 1, "bun")])]
        self.assertEqual(1, junkfather.lease_of(vite, items))


class FormatterTests(unittest.TestCase):
    def test_every_kind_junkfather_logs_has_its_word(self) -> None:
        """The kinds come from Junkfather's own events.KINDS (the sibling checkout), not a list kept here
        (fathers#88 review 10); a kind it adds without a word here fails."""
        import ast

        package = ROOT.parent / "junkfather" / "junkfather" / "events.py"
        if not package.is_file():
            self.skipTest("no junkfather checkout beside godfather")
        tree = ast.parse(package.read_text())
        kinds = next(ast.literal_eval(node.value) for node in ast.walk(tree) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "KINDS" for target in node.targets))
        self.assertGreater(len(kinds), 10)
        for kind in kinds:
            with self.subTest(kind):
                self.assertIsNotNone(events.junkfather_shown(kind, {}), f"{kind} has no word in EVENTS")
        swept = events.junkfather_shown("sweep.finished", {"reaped": ["a", "b"], "failed": []})
        assert swept is not None
        self.assertEqual("2 reaped", swept.detail[0][0])


class PageTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.tools = {**self.tools, "junkfather": state_module.Tool("junkfather", "ok", None, decoded(),
                                                                    actions=("reap", "extend"))}

    def lease(self) -> dict[str, Any]:
        return request("644-build-once", STRAYLIGHT, pid=10000, label="the-lease")

    def picked_proc(self, state: Any) -> Any:
        rows = state_module.page_rows(state, "processes")
        index = state_module.page_index(state, "processes")
        return rows[index]["key"] if index is not None else None

    def test_details_list_the_requests_processes_as_links(self) -> None:
        state = self.screen([self.lease(), entry(1)])
        self.assertRegex(self.text(), r"processes\s+storybook · running · ")  # its TTL left, ticking
        state = self.click(state, *self.at("storybook · running"))
        self.assertEqual(("processes", ("entry", "j5e6f7a")), (state.ui.page, self.picked_proc(state)))
        state = self.keys(state, "esc")
        self.assertEqual(("overview", 0), (state.ui.page, state.selection.index))

    def test_none_owned(self) -> None:
        self.screen([entry(1)])
        self.assertIn("none owned", self.text())

    def test_3_opens_on_the_linked_entry_and_the_summary(self) -> None:
        state = self.screen([self.lease()])
        self.assertIn("3 procs 5 · 1 orphan · 1 expired · 1 unowned", self.text())
        self.assertNotIn("3 unowned", self.text())
        state = self.keys(state, "3")
        self.assertEqual(("entry", "j5e6f7a"), self.picked_proc(state))
        self.assertIn("PROCESS DETAILS", self.text())
        state = self.keys(state, "G")
        self.assertEqual(
            "resource",
            state_module.page_rows(state, "processes")[state_module.page_index(state, "processes") or 0]["kind"],
        )
        state = self.keys(state, "1")
        self.assertEqual("overview", state.ui.page)
        state = self.click(state, *self.at("3 procs"))
        self.assertEqual("processes", state.ui.page)

    def test_summary_separates_resources_excludes_dead_and_drops_resources_first(self) -> None:
        raw = snapshot()
        raw["entries"] = []
        raw["unowned"] = [
            {"pid": 77001, "start": "t1", "cmd": "node dead", "why": "orphan", "age": 3600, "alive": False},
        ]
        raw["unowned_resources"] = [
            {"kind": "worktree", "id": f"/wt/{n}", "name": f"wt-{n}", "why": "idle"} for n in range(75)
        ]
        self.tools["junkfather"] = state_module.Tool("junkfather", "ok", None, junkfather.decode(raw),
                                                     actions=("reap", "extend"))
        self.screen([self.lease()])
        self.assertIn("3 procs 0 · 75 resources", self.text())
        self.assertNotIn("unowned", self.text().splitlines()[4])
        raw["unowned_resources"] = raw["unowned_resources"][:1]
        self.tools["junkfather"] = state_module.Tool("junkfather", "ok", None, junkfather.decode(raw),
                                                     actions=("reap", "extend"))
        self.screen([self.lease()])
        self.assertIn("3 procs 0 · 1 resource", self.text())
        self.assertNotIn("1 resources", self.text())
        # With lanefather absent, all procs parts including resources fit on a wide border
        self.tools = {"junkfather": state_module.Tool("junkfather", "ok", None, decoded(), actions=("reap", "extend"))}
        self.screen([self.lease()])
        self.assertIn("3 procs 5 · 1 orphan · 1 expired · 1 unowned · 2 resources", self.text())
        # Without lanes everything fits at 76; with lanes, resources drops first
        self.screen([self.lease()], width=76, height=40)
        border = self.text().splitlines()[4]
        self.assertIn("3 procs 5 · 1 orphan · 1 expired · 1 unowned · 2 resources", border)
        self.tools["lanefather"] = state_module.Tool("lanefather", "ok", None, lanes_decoded())
        self.screen([self.lease()], width=76, height=40)
        narrow = self.text().splitlines()[4]
        self.assertIn("3 procs 5", narrow)
        self.assertNotIn("resources", narrow)

    def test_dead_entries_are_dimmed_and_actions_availability_is_clear(self) -> None:
        raw = snapshot()
        raw["unowned"].append({"pid": 77001, "start": "Tue Nov  4 07:00:00 2026", "cmd": "node dead-worker",
                               "cwd": "/fixture/straylight", "root": "/fixture/straylight", "why": "orphan",
                               "age": 7200, "alive": False})
        self.tools["junkfather"] = state_module.Tool("junkfather", "ok", None, junkfather.decode(raw),
                                                     actions=("reap", "extend"))
        state = self.keys(self.screen([entry(1)]), "3")
        screen = self.text()
        self.assertIn("unowned processes · 1 · last sweep", screen)
        self.assertIn("gone since the sweep · 1", screen)
        self.assertIn("unowned resources · 2 · last sweep", screen)
        # Every non-space cell of the dead process row inside the table is dimmed, with ◌ and `gone`
        _, dead_y = self.at("node dead-w")
        table = self.canvas.tables["procs"]
        dead_cells = self.canvas.cells[dead_y][table["x"] + 1:table["x"] + table["width"] - 1]
        dead_line = "".join(str(cell[0]) for cell in dead_cells)
        self.assertIn("◌", dead_line)
        self.assertIn("gone", dead_line)
        self.assertEqual({"dim"}, {cell[1] for cell in dead_cells if str(cell[0]).strip()})
        # Owned running entry: shows extend ttl and clickable `a actions` in footer
        self.assertIn("extend ttl · a / enter", screen)
        self.assertIn("a actions", screen.splitlines()[-1])
        opened = self.click(state, *self.at("a actions"))
        self.assertEqual("actions", getattr(opened.ui.overlay, "kind", None))
        self.assertIn("ACTIONS", self.text())
        # Live unowned process: shows reap and `a actions`
        state = self.draw(dataclasses.replace(state, picked={"processes": ("pid", 88123, "Tue Nov  4 08:29:00 2026")}))
        self.assertIn("reap · a / enter", self.text())
        self.assertIn("a actions", self.text().splitlines()[-1])
        # Dead unowned process: shows `none · process gone` and footer says `no actions` without `a actions`
        state = self.draw(dataclasses.replace(state, picked={"processes": ("pid", 77001, "Tue Nov  4 07:00:00 2026")}))
        self.assertIn("none · process gone", self.text())
        self.assertNotIn("a actions", self.text().splitlines()[-1])
        self.assertIn("no actions", self.text().splitlines()[-1])
        # Unowned resource: shows `none · read-only resource` and footer says `no actions`
        state = self.draw(dataclasses.replace(state, picked={"processes": ("resource", "worktree",
                                                                           "/fixture/straylight/worktrees/old-spike")}))
        self.assertIn("none · read-only resource", self.text())
        self.assertNotIn("a actions", self.text().splitlines()[-1])
        self.assertIn("no actions", self.text().splitlines()[-1])
        # Entry when tool advertises no actions: shows `none` and `no actions`
        self.tools["junkfather"] = state_module.Tool("junkfather", "ok", None, junkfather.decode(raw), actions=())
        state = self.keys(self.screen([entry(1)]), "3")
        self.assertRegex(self.text(), r"actions\s+none\s")
        self.assertNotIn("a actions", self.text().splitlines()[-1])
        self.assertIn("no actions", self.text().splitlines()[-1])

    def test_empty_owned_entries_shows_sweep_headings_and_selects_first_selectable(self) -> None:
        raw = snapshot()
        raw["entries"] = []
        raw["unowned"] = [{"pid": 77001, "start": "t1", "cmd": "node dead", "why": "orphan", "age": 60, "alive": False}]
        self.tools["junkfather"] = state_module.Tool("junkfather", "ok", None, junkfather.decode(raw),
                                                     actions=("reap", "extend"))
        state = self.keys(self.screen([entry(1)]), "3")
        self.assertEqual(("pid", 77001, "t1"), self.picked_proc(state))
        self.assertIn("gone since the sweep · 1", self.text())
        self.assertIn("unowned resources · 2 · last sweep", self.text())
        state = self.keys(state, "j")
        self.assertEqual(("resource", "worktree", "/fixture/straylight/worktrees/old-spike"), self.picked_proc(state))

    def test_the_separator_is_skipped_and_unowned_rows_show_why(self) -> None:
        state = self.keys(self.screen([entry(1)]), "3")
        rows = state_module.page_rows(state, "processes")
        keys = [row.get("key") for row in rows if row.get("key")]
        state = dataclasses.replace(state, picked={"processes": keys[4]})
        state = self.keys(state, "j")
        self.assertEqual(("pid", 88123, "Tue Nov  4 08:29:00 2026"), self.picked_proc(state))
        self.assertIn("orphan", self.text())
        self.assertIn("merged, idle 9d", self.text())

    def test_lane_details_list_its_processes_and_an_entry_links_to_its_lane(self) -> None:
        state = self.keys(self.screen([entry(1)]), "2", "j")
        self.assertEqual((STRAYLIGHT, "710-speech-paste-phase"), self.picked(state))
        self.assertIn("vite dev · running · ", self.text())
        state = self.click(state, *self.at("vite dev · running"))
        self.assertEqual(("processes", ("entry", "j3f9a1c")), (state.ui.page, self.picked_proc(state)))
        state = self.click(state, *self.at("710-speech-paste-phase · Straylight"))
        self.assertEqual("lanes", state.ui.page)
        state = self.keys(state, "esc")
        self.assertEqual("processes", state.ui.page, "Esc goes back where the link came from")
        state = self.keys(state, "esc")
        self.assertEqual("lanes", state.ui.page)

    def test_no_ttl_entry_renders_dash_in_processes_table_details_and_links(self) -> None:
        sentinel = 100 * 365 * 86400.0
        raw = snapshot()
        raw["entries"] = [
            {
                "id": "jbg0001", "state": "running", "owner": "claude 4bceaaaf", "session": "4bceaaaf",
                "agent": None, "lane": None, "owner_alive": True, "age": 1800, "ttl": sentinel,
                "ttl_left": sentinel - 1800, "no_ttl": True, "keep": "owner", "pid": 55001, "pgid": 10000,
                "processes": 2, "label": "bg dev server", "mode": "background", "cwd": "/fixture/straylight",
                "argv": ["bun", "run", "dev"], "log": None, "end": None, "exit": None, "resource": None,
            },
        ]
        self.tools["junkfather"] = state_module.Tool("junkfather", "ok", None, junkfather.decode(raw),
                                                     actions=("reap", "extend"))
        state = self.screen([self.lease()])
        overview = self.text()
        self.assertIn("bg dev server · running · —", overview)
        self.assertNotIn("876000h", overview)
        self.assertNotIn("875999h", overview)
        self.keys(state, "3")
        screen = self.text()
        _, row_y = self.at("bg dev server")
        table = self.canvas.tables["procs"]
        row_text = "".join(str(cell[0]) for cell in self.canvas.cells[row_y][table["x"] + 1:table["x"] + table["width"] - 1])
        self.assertIn("—", row_text)
        self.assertNotIn("876000h", row_text)
        self.assertNotIn("875999h", row_text)
        self.assertRegex(screen, r"ttl left\s+—\s")
        self.assertNotIn("extend ttl", screen)
        self.assertRegex(screen, r"actions\s+none\b")

    def test_absent_and_unsupported(self) -> None:
        self.tools = {name: tool for name, tool in self.tools.items() if name != "junkfather"}
        state = self.keys(self.screen([self.lease()]), "3")
        self.assertEqual("overview", state.ui.page)
        self.assertNotRegex(self.text(), r"processes\s+(none owned|storybook)")
        self.assertNotIn("3 procs", self.text())
        self.tools["junkfather"] = state_module.Tool("junkfather", "unsupported", "junkfather 0.1.0: no dashboard "
                                                                                  "contract; update junkfather")
        self.keys(self.screen([self.lease()]), "3")
        self.assertIn("update junkfather", self.text())


class OnceTests(unittest.TestCase):
    def test_once_draws_the_processes_page(self) -> None:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GODFATHER_", "SLOTFATHER_"))}
        env.update(GODFATHER_FIXTURES=str(PROCS), PYTHONPATH=str(ROOT / "src"), TZ="UTC", NO_COLOR="1")
        found = subprocess.run([sys.executable, "-m", "godfather", "--once", "--page", "processes", "--width", "156",
                                "--height", "43"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(0, found.returncode, found.stderr)
        self.assertIn("PROCESS DETAILS", found.stdout)
        self.assertIn("iPhone 17 Pro", found.stdout)


class LiveTests(unittest.TestCase):
    def test_3_a_link_and_esc_in_a_pty(self) -> None:
        import terminal

        env = {key: value for key, value in os.environ.items() if not key.startswith(("GODFATHER_", "SLOTFATHER_"))}
        env.update(GODFATHER_FIXTURES=str(PROCS), TZ="UTC")
        screen = terminal.Terminal(self, env, 43, 156, [sys.executable, str(terminal.ENTRY), "--interval", "0.5"])
        screen.receive("3 procs 5", "storybook · running")
        at = screen.mark()
        screen.write("3")
        screen.receive("PROCESS DETAILS", "booted, no owner", since=at)
        at = screen.mark()
        screen.write("\x1b")
        screen.receive("LEASE DETAILS", since=at)
        screen.until(lambda _: any("storybook · running" in line for line in screen.rows().values()),
                     what="the processes row again")
        row, line = next((row, line) for row, line in screen.rows().items() if "storybook · running" in line)
        at = screen.mark()
        screen.click(line.index("storybook · running") + 2, row)
        screen.receive("PROCESS DETAILS", since=at)
        self.assertEqual(0, screen.quit())


class ContractTests(unittest.TestCase):
    """The real Junkfather (the sibling checkout) with JUNKFATHER_STATE in a temporary directory: its capabilities
    name a snapshot this Godfather reads, and an owned resource entry decodes with its kind and id."""

    def test_status_with_an_owned_resource(self) -> None:
        package = ROOT.parent / "junkfather"
        if not (package / "junkfather" / "dashboard.py").is_file():
            self.skipTest("no junkfather checkout beside godfather")
        tmp = Path(tempfile.mkdtemp(prefix="godfather-junkfather-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "PYTHONPATH": str(package),
               "JUNKFATHER_STATE": str(tmp / "state")}
        python = shutil.which("python3") or sys.executable

        def run(*argv: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([python, "-m", "junkfather", *argv], capture_output=True, text=True, env=env,
                                  cwd=tmp, timeout=60)

        capabilities = run("capabilities", "--json")
        if capabilities.returncode != 0:
            self.skipTest(f"junkfather cannot run here: {capabilities.stderr.strip().splitlines()[-1:]}")
        self.assertIsNone(junkfather.unsupported(json.loads(capabilities.stdout)))
        owned = run("own-resource", "--kind", "command", "--id", "fixture-1", "--alive", "true", "--stop", "true",
                    "--label", "fixture", "--ttl", "3600")
        if owned.returncode != 0:
            self.skipTest(f"junkfather own-resource refused here: {owned.stderr.strip().splitlines()[-1:]}")
        found = junkfather.decode(json.loads(run("status", "--json").stdout))
        held = next(row["resource"] for row in found["entries"] if row["resource"])
        self.assertEqual(("command", "fixture-1"), (held["kind"], held["id"]))


if __name__ == "__main__":
    unittest.main()
