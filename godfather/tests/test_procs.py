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

from godfather import pages, events, sources, state as state_module
from godfather.adapters import junkfather, lanefather

PROCS = ROOT / "tests" / "fixtures" / "procs"


def snapshot() -> dict[str, Any]:
    return json.loads((PROCS / "junkfather" / "replies.json").read_text())["status --json"]["json"]


def decoded() -> dict[str, Any]:
    return junkfather.decode(snapshot())


class DecodeTests(unittest.TestCase):
    def test_states_order_and_the_summary(self) -> None:
        rows = junkfather.page_rows(decoded())
        self.assertEqual(["running", "running", "running", "orphan", "expired"],
                         [row["state"] for row in rows if row["kind"] == "entry"])
        self.assertEqual(["entry"] * 5 + ["separator", "unowned", "resource", "resource"], [row["kind"] for row in rows])
        self.assertEqual({"count": 5, "orphan": 1, "expired": 1, "unowned": 3}, junkfather.summary(decoded()))

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
        assert simulator is not None
        self.assertEqual("simulator", (simulator["resource"] or {})["kind"])
        self.assertIsNone(simulator["pid"])


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
        kinds = ("entry.owned", "entry.adopted", "entry.exited", "entry.extended", "entry.handed", "entry.reaped",
                 "entry.reap_failed", "resource.owned", "resource.stopped", "resource.stop_failed",
                 "unowned.reported", "unowned.reaped", "sweep.finished", "hook.failed")
        package = ROOT.parent / "junkfather" / "junkfather" / "events.py"
        if package.is_file():
            text = package.read_text()
            self.assertEqual(set(kinds), {kind for kind in kinds if f'"{kind}"' in text}, "the kinds it names")
        for kind in kinds:
            with self.subTest(kind):
                self.assertIsNotNone(events.junkfather_shown(kind, {}), f"{kind} falls back to the kind itself")
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
        rows = pages.page_rows(state, "processes")
        index = pages.page_index(state, "processes")
        return rows[index]["key"] if index is not None else None

    def test_details_list_the_requests_processes_as_links(self) -> None:
        state = self.screen([self.lease(), entry(1)])
        self.assertRegex(self.text(), r"processes\s+storybook · running · ")  # its TTL left, ticking
        state = self.click(state, *self.at("storybook · running"))
        self.assertEqual(("processes", ("entry", "j5e6f7a")), (state.page, self.picked_proc(state)))
        state = self.keys(state, "esc")
        self.assertEqual(("overview", 0), (state.page, state.selection.index))

    def test_none_owned(self) -> None:
        self.screen([entry(1)])
        self.assertIn("none owned", self.text())

    def test_3_opens_on_the_linked_entry_and_the_summary(self) -> None:
        state = self.screen([self.lease()])
        self.assertIn("3 procs 5 · 1 orphan · 1 expired · 3 unowned", self.text())
        state = self.keys(state, "3")
        self.assertEqual(("entry", "j5e6f7a"), self.picked_proc(state))
        self.assertIn("PROCESS DETAILS", self.text())
        state = self.keys(state, "G")
        self.assertEqual("resource", pages.page_rows(state, "processes")[pages.page_index(state,
                                                                                                       "processes") or 0]["kind"])
        state = self.keys(state, "1")
        self.assertEqual("overview", state.page)
        state = self.click(state, *self.at("3 procs"))
        self.assertEqual("processes", state.page)

    def test_the_separator_is_skipped_and_unowned_rows_show_why(self) -> None:
        state = self.keys(self.screen([entry(1)]), "3")
        rows = pages.page_rows(state, "processes")
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
        self.assertEqual(("processes", ("entry", "j3f9a1c")), (state.page, self.picked_proc(state)))
        state = self.click(state, *self.at("710-speech-paste-phase · Straylight"))
        self.assertEqual("lanes", state.page)
        state = self.keys(state, "esc")
        self.assertEqual("processes", state.page, "Esc goes back where the link came from")
        state = self.keys(state, "esc")
        self.assertEqual("lanes", state.page)

    def test_absent_and_unsupported(self) -> None:
        self.tools = {name: tool for name, tool in self.tools.items() if name != "junkfather"}
        state = self.keys(self.screen([self.lease()]), "3")
        self.assertEqual("overview", state.page)
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
        resource = next(row for row in found["entries"] if row["resource"])
        self.assertEqual(("command", "fixture-1"), (resource["resource"]["kind"], resource["resource"]["id"]))


if __name__ == "__main__":
    unittest.main()
