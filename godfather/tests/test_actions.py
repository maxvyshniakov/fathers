"""Row actions run by the owner tools' CLIs (fathers#88): which actions each row offers in which state, the menu,
the reason and the confirmation through the reducer, the run with the row checked again (recorded under
GODFATHER_FIXTURES with GODFATHER_ACTOR, never run), a failing command's stderr in red, the live flow in a PTY, and
the real Junkfather's extend and reap in a temporary state directory."""
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
from unittest import mock

from helpers import ROOT
from test_lanes import STRAYLIGHT, decoded as lanes_decoded, request
from test_procs import PROCS, PageTests, decoded as procs_decoded
from test_state import entry

from godfather import actions, app, reducer, sources, state as state_module
from godfather.adapters import junkfather, lanefather

START = "Tue Nov  4 09:00:00 2026"


def names(found: list[actions.Action]) -> list[str]:
    return [action.name for action in found]


class RequestTests(unittest.TestCase):
    advertised = ["reprioritize", "ask-yield", "land"]

    def test_reprioritize_to_each_other_class(self) -> None:
        item = entry(0, pid=42, start=START, priority="batch")
        found = actions.for_request(item, [], self.advertised)
        self.assertEqual(["Reprioritize to interactive", "Reprioritize to normal"], names(found))
        self.assertEqual("slotfather reprioritize 42 interactive --start 'Tue Nov  4 09:00:00 2026'", found[0].shown())
        self.assertFalse(found[0].confirm)

    def test_ask_to_yield_only_a_yieldable_lease_of_a_lower_class_than_a_waiter(self) -> None:
        lease = entry(0, pid=42, start=START, priority="batch", yieldable=True)
        waiter = entry(1, "waiting", priority="interactive")
        self.assertIn("Ask to yield", names(actions.for_request(lease, [waiter], self.advertised)))
        self.assertNotIn("Ask to yield", names(actions.for_request(lease, [], self.advertised)), "no waiter")
        same = entry(2, "waiting", priority="batch")
        self.assertNotIn("Ask to yield", names(actions.for_request(lease, [same], self.advertised)), "not higher")
        for changed in ({"yieldable": False}, {"asked": "gate"}, {"kind": "waiting"}):
            with self.subTest(changed=changed):
                self.assertNotIn("Ask to yield", names(actions.for_request(dict(lease, **changed), [waiter],
                                                                           self.advertised)))

    def test_nothing_without_a_start_or_unadvertised(self) -> None:
        self.assertEqual([], actions.for_request(entry(0, pid=42, start=None), [], self.advertised))
        self.assertEqual([], actions.for_request(entry(0, pid=42, start=START), [], ["land", "promote"]))


class ProcessTests(unittest.TestCase):
    def row(self, ident: str) -> dict[str, Any]:
        found = junkfather.find(procs_decoded(), ("entry", ident))
        assert found is not None
        return found

    def test_reap_an_orphan_or_expired_entry_confirmed(self) -> None:
        orphan = actions.for_process(self.row("j7a8b9c"), ["reap", "extend"])
        self.assertEqual(["Reap"], names(orphan))
        self.assertTrue(orphan[0].confirm)
        self.assertEqual("junkfather reap --id j7a8b9c --reason 'godfather: orphan'", orphan[0].shown())
        expired = actions.for_process(self.row("j2b3c4d"), ["reap", "extend"])
        self.assertEqual("junkfather reap --id j2b3c4d --reason 'godfather: expired'", expired[0].shown())

    def test_a_running_entry_is_extended_never_reaped(self) -> None:
        found = actions.for_process(self.row("j3f9a1c"), ["reap", "extend"])
        self.assertEqual(["Extend TTL by 1 h", "Extend TTL by 4 h", "Extend TTL by 1 d"], names(found))
        self.assertEqual("junkfather extend j3f9a1c --ttl 4h", found[1].shown())
        self.assertFalse(any(action.confirm for action in found))

    def test_an_unowned_process_is_reaped_by_pid_with_force_and_a_resource_has_none(self) -> None:
        rows = junkfather.page_rows(procs_decoded())
        process = next(row for row in rows if row["kind"] == "unowned")
        found = actions.for_process(process, ["reap"])
        self.assertEqual("junkfather reap --pid 88123 --force --reason 'godfather: unowned'", found[0].shown())
        self.assertEqual([], actions.for_process(next(row for row in rows if row["kind"] == "resource"), ["reap"]))
        self.assertEqual([], actions.for_process(process, ["extend", "sweep"]), "not advertised")


class LaneTests(unittest.TestCase):
    def lane(self, ident: str = "710-speech-paste-phase") -> dict[str, Any]:
        found = lanefather.find(lanes_decoded(), (STRAYLIGHT, ident))
        assert found is not None
        return found

    def test_open_the_ticket_the_log_and_the_worktree_and_park_only_where_advertised(self) -> None:
        found = actions.for_lane(self.lane(), [])
        self.assertEqual(["Open the ticket", "Open the latest log", "Open the worktree"], names(found))
        self.assertEqual((actions.opener(), "https://github.com/maxvyshniakov/straylight/issues/710"),
                         found[0].command())
        with mock.patch.dict(os.environ, {"PAGER": "most -s"}):
            self.assertEqual(("most", "-s", self.lane()["logs"]["check"]), found[1].command())
        park = actions.for_lane(self.lane(), ["park", "land", "promote", "done", "sync"])
        self.assertEqual("Park", park[-1].name)
        self.assertTrue(park[-1].confirm and park[-1].reason)
        self.assertEqual("lanefather park 710-speech-paste-phase --reason 'no review today'",
                         park[-1].shown("no review today"))
        self.assertNotIn("Park", names(actions.for_lane(self.lane("555-host-rename-names"), ["park"])), "planned")


class FlowTests(PageTests):
    def key(self, state: Any, key: str) -> tuple[Any, list[Any]]:
        state, effects = reducer.reduce(state, state_module.Key(key), 0.0)
        return self.draw(state), effects

    def to_orphan(self) -> Any:
        state = self.keys(self.screen([entry(1)]), "3")
        return dataclasses.replace(state, picked={"processes": ("entry", "j7a8b9c")})

    def test_enter_opens_the_menu_and_a_reap_asks_first(self) -> None:
        state, _ = self.key(self.to_orphan(), "\r")
        self.assertIsInstance(state.overlay, state_module.ActionMenu)
        self.assertIn("ACTIONS", self.text())
        state, effects = self.key(state, "\r")
        self.assertIsInstance(state.overlay, state_module.Confirm)
        self.assertIn("junkfather reap --id j7a8b9c --reason 'godfather: orphan'", self.text())
        self.assertIn("claude 4bceaaaf/a71c", self.text(), "the target's owner")
        self.assertEqual([], effects, "nothing runs before y")
        for cancel in ("esc", "n", "\r", "x"):
            with self.subTest(cancel=cancel):
                after, effects = self.key(state, cancel)
                self.assertEqual([], [effect for effect in effects if effect.kind == "action"])
        after, effects = self.key(state, "y")
        self.assertEqual(["action"], [effect.kind for effect in effects])
        self.assertIsNone(after.overlay)

    def test_a_running_entry_extends_without_asking(self) -> None:
        state = self.keys(self.screen([entry(1)]), "3")
        state = dataclasses.replace(state, picked={"processes": ("entry", "j3f9a1c")})
        state, _ = self.key(state, "a")
        state, _ = self.key(state, "j")
        state, effects = self.key(state, "\r")
        self.assertEqual([("extend", "j3f9a1c", "--ttl", "4h")], [effect.item[0].argv for effect in effects])

    def test_a_row_with_none_opens_no_menu_and_says_so(self) -> None:
        state = self.keys(self.screen([entry(1)]), "3")
        state = dataclasses.replace(state, picked={"processes": ("resource", "simulator",
                                                                 "9F8E7D6C-0000-4000-8000-0123456789AB")})
        state, _ = self.key(state, "\r")
        self.assertIsNone(state.overlay)
        self.assertIn("no actions", self.text())

    def test_park_asks_a_reason_and_an_empty_one_cancels(self) -> None:
        self.tools["lanefather"] = dataclasses.replace(self.tools["lanefather"], actions=("park",))
        state = self.keys(self.screen([entry(1)]), "2", "j")
        state, _ = self.key(state, "\r")
        state, _ = self.key(state, "G")
        state, _ = self.key(state, "\r")
        self.assertIsInstance(state.overlay, state_module.Reason)
        empty, effects = self.key(state, "\r")
        self.assertEqual((None, []), (empty.overlay, effects))
        for char in "q later":  # typed, never a hotkey
            state, _ = self.key(state, char)
        state, _ = self.key(state, "\r")
        self.assertIn("lanefather park 710-speech-paste-phase --reason 'q later'", self.text())
        state, effects = self.key(state, "y")
        self.assertEqual("q later", effects[0].item[1])

    def test_a_right_click_selects_the_row_and_opens_its_menu(self) -> None:
        state = self.keys(self.screen([entry(1)]), "3")
        x, y = self.at("playwright s")
        state, _ = reducer.reduce(state, state_module.Mouse(2, x, y, True), 0.0)
        self.assertEqual(("entry", "j7a8b9c"), state.picked["processes"])
        self.assertIsInstance(state.overlay, state_module.ActionMenu)

    def test_a_done_action_says_so_and_polls_its_source(self) -> None:
        state, effects = reducer.reduce(self.screen(), state_module.ActionDone("junkfather", False, "no such entry"),
                                        0.0)
        assert state.notice is not None
        self.assertEqual(("no such entry", "bad"), (state.notice.text, state.notice.style))
        self.assertEqual([("poll", "junkfather")], [(effect.kind, effect.key) for effect in effects
                                                    if effect.kind == "poll"])


class RunTests(unittest.TestCase):
    """Screen.act under fixtures: the row checked again, the argv and GODFATHER_ACTOR recorded, never run."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="godfather-actions-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copytree(PROCS, self.tmp / "procs")
        self.record = self.tmp / "record.jsonl"
        patcher = mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(self.tmp / "procs"),
                                               "GODFATHER_FIXTURES_RECORD": str(self.record)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def reap(self) -> actions.Action:
        found = junkfather.find(procs_decoded(), ("entry", "j7a8b9c"))
        assert found is not None
        return actions.for_process(found, ["reap"])[0]

    def test_recorded_with_the_actor_never_run(self) -> None:
        done = app.Screen().act(self.reap(), "")
        self.assertTrue(done.ok, done.line)
        line = json.loads(self.record.read_text().splitlines()[-1])
        self.assertEqual(["junkfather", "reap", "--id", "j7a8b9c", "--reason", "godfather: orphan"], line["argv"])
        self.assertTrue(line["env"]["GODFATHER_ACTOR"].startswith("godfather/"))

    def test_a_row_that_changed_is_dropped(self) -> None:
        replies = json.loads((self.tmp / "procs" / "junkfather" / "replies.json").read_text())
        status = replies["status --json"]["json"]
        status["entries"] = [row for row in status["entries"] if row["id"] != "j7a8b9c"]
        (self.tmp / "procs" / "junkfather" / "replies.json").write_text(json.dumps(replies))
        screen = app.Screen()
        screen.tool_sources["junkfather"].discover()
        done = screen.act(self.reap(), "")
        self.assertFalse(done.ok)
        self.assertIn("changed or is gone", done.line)
        self.assertFalse(self.record.exists(), "nothing run")

    def test_a_failing_command_is_its_first_stderr_line(self) -> None:
        replies = json.loads((self.tmp / "procs" / "junkfather" / "replies.json").read_text())
        replies["reap --id j7a8b9c --reason godfather: orphan"] = {"code": 1, "stderr": "junkfather reap: busy\nmore"}
        (self.tmp / "procs" / "junkfather" / "replies.json").write_text(json.dumps(replies))
        screen = app.Screen()
        screen.tool_sources["junkfather"].discover()
        done = screen.act(self.reap(), "")
        self.assertEqual((False, "junkfather reap: busy"), (done.ok, done.line))

    def test_a_request_is_checked_by_its_pid_and_start(self) -> None:
        action = actions.for_request(request(pid=7, start=START), [], ["reprioritize"])[0]
        good = {"holders": [{"key": action.identity[1], "pid": 7, "start": START}], "waiters": []}
        self.assertTrue(actions.still(action, good))
        self.assertFalse(actions.still(action, {"holders": [dict(good["holders"][0], start="later")], "waiters": []}))


class LiveTests(unittest.TestCase):
    def test_reap_an_orphan_in_a_pty(self) -> None:
        import terminal

        tmp = Path(tempfile.mkdtemp(prefix="godfather-actions-live-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        record = tmp / "record.jsonl"
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GODFATHER_", "SLOTFATHER_"))}
        env.update(GODFATHER_FIXTURES=str(PROCS), GODFATHER_FIXTURES_RECORD=str(record), TZ="UTC")
        screen = terminal.Terminal(self, env, 43, 156, [sys.executable, str(terminal.ENTRY), "--interval", "0.5"])
        screen.receive("3 procs 5")
        at = screen.mark()
        screen.write("3")
        screen.receive("PROCESS DETAILS", since=at)
        screen.write("gjjj")  # vite dev, storybook, the iPhone, then the orphaned Playwright server
        at = screen.mark()
        screen.write("\r")
        screen.receive("ACTIONS", "Reap", since=at)
        at = screen.mark()
        screen.write("\r")
        screen.receive("junkfather reap --id j7a8b9c", "y run it", since=at)
        at = screen.mark()
        screen.write("y")
        screen.until(lambda _: record.exists(), what="the reap recorded")
        line = json.loads(record.read_text().splitlines()[-1])
        self.assertEqual(["junkfather", "reap", "--id", "j7a8b9c", "--reason", "godfather: orphan"], line["argv"])
        screen.receive("recorded: junk", since=at)  # the footer's line, as long as it fits
        self.assertEqual(0, screen.quit())


class SuspendTests(unittest.TestCase):
    def test_the_pager_gets_the_terminal_and_kitty_mode_comes_back(self) -> None:
        import pty

        from godfather import keys, tui

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        out_r, out_w = os.pipe()
        self.addCleanup(os.close, out_r)
        with tui.suspended(slave, out_w):
            os.write(out_w, b"<pager>")
        os.close(out_w)
        written = os.read(out_r, 65536)
        left, _, back = written.partition(b"<pager>")
        self.assertIn(keys.KITTY_OFF, left)
        self.assertIn(b"\x1b[?1049l", left, "the shell's screen for the pager")
        self.assertIn(keys.KITTY_ON, back)
        self.assertIn(b"\x1b[?1049h", back, "the dashboard's screen again")
        out_w = -1


class ContractTests(unittest.TestCase):
    """The real Junkfather (the sibling checkout) in a temporary state directory: extend and reap a resource entry
    through sources.act, as the app runs them."""

    def test_extend_and_reap(self) -> None:
        package = ROOT.parent / "junkfather"
        if not (package / "junkfather" / "dashboard.py").is_file():
            self.skipTest("no junkfather checkout beside godfather")
        tmp = Path(tempfile.mkdtemp(prefix="godfather-junk-actions-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        python = shutil.which("python3") or sys.executable
        (bin_dir / "junkfather").write_text(f"#!/bin/sh\nPYTHONPATH={package} exec {python} -m junkfather \"$@\"\n")
        (bin_dir / "junkfather").chmod(0o755)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp), "JUNKFATHER_STATE": str(tmp / "state")}
        flag = tmp / "running"  # the resource: alive while this file is there, stopped by removing it
        flag.write_text("")
        owned = subprocess.run([str(bin_dir / "junkfather"), "own-resource", "--kind", "command", "--id", "fixture-2",
                                "--alive", f"test -e {flag}", "--stop", f"rm -f {flag}", "--label", "fixture",
                                "--ttl", "3600"],
                               capture_output=True, text=True, env=env, cwd=tmp, timeout=60)
        if owned.returncode != 0:
            self.skipTest(f"junkfather cannot run here: {owned.stderr.strip().splitlines()[-1:]}")
        with mock.patch.dict(os.environ, env, clear=True):
            found = junkfather.decode(json.loads(sources.run("junkfather", ["status", "--json"], 30).stdout))
            entry_row = next(row for row in found["entries"] if row["resource"])
            extend = actions.for_process(entry_row, ["extend"])[0]
            reply = sources.act("junkfather", list(extend.argv), "godfather/test")
            self.assertEqual(0, reply.code, f"extend: {reply.stderr}")
            reap = actions.Action("junkfather", "Reap", ("reap", "--id", entry_row["id"], "--reason",
                                                        "godfather: test"), confirm=True)
            reply = sources.act("junkfather", list(reap.argv), "godfather/test")
            self.assertEqual(0, reply.code, f"reap: {reply.stdout} {reply.stderr}")
            after = junkfather.decode(json.loads(sources.run("junkfather", ["status", "--json"], 30).stdout))
            self.assertFalse([row for row in after["entries"] if row["id"] == entry_row["id"]])


if __name__ == "__main__":
    unittest.main()
