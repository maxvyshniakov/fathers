"""Row actions run by the owner tools' CLIs (fathers#88): which actions each row offers in which state, the menu,
the reason and the confirmation through the reducer, the run with the row checked again (recorded under
GODFATHER_FIXTURES with GODFATHER_ACTOR, never run), a failing command's stderr in red, the live flow in a PTY, the
real Spawnfather's extend and reap in a temporary state directory, and the real Slotfather's reprioritize and
ask-yield on live leases in an isolated SLOTFATHER_DIR."""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest import mock

from helpers import ROOT
from test_expected import raw_item as expected_raw_item, snapshot as expected_snapshot
from test_lanes import STRAYLIGHT, decoded as lanes_decoded, request
from test_procs import PROCS, PageTests, decoded as procs_decoded
from test_state import entry

from godfather import actions, app, eventcheck, reducer, sources, state as state_module
from godfather.adapters import lanefather, slotfather as slotfather_adapter, spawnfather
from godfather.layout import Rect
from godfather.model import Dashboard, Lane, Processes, ProcessTableRow
from godfather.tui import Canvas
from godfather.views.queue import draw_requests

START = "Tue Nov  4 09:00:00 2026"
SLOTFATHER = ROOT.parent / "slotfather" / "slotfather"  # the sibling checkout: both packages at one commit
HOLD = 'echo "start $1" >> "$LOG"; while [ ! -e "$RELEASE/$1" ]; do sleep 0.05; done'  # runs until released


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

    def test_ask_to_yield_hidden_once_yields_reaches_yields_max(self) -> None:
        """fathers#229: Slotfather refuses ask-yield once a lease has yielded YIELDS_MAX (2) times."""
        waiter = entry(1, "waiting", priority="interactive")
        for count in (0, 1):
            with self.subTest(yields=count):
                lease = entry(0, pid=42, start=START, priority="batch", yieldable=True, yields=count)
                self.assertIn("Ask to yield", names(actions.for_request(lease, [waiter], self.advertised)))
        for count in (actions.YIELDS_MAX, actions.YIELDS_MAX + 1):
            with self.subTest(yields=count):
                lease = entry(0, pid=42, start=START, priority="batch", yieldable=True, yields=count)
                found = names(actions.for_request(lease, [waiter], self.advertised))
                self.assertNotIn("Ask to yield", found)
                self.assertEqual(["Reprioritize to interactive", "Reprioritize to normal"], found)

    def test_ask_to_yield_compares_a_held_leases_own_class_against_a_waiters_served_class(self) -> None:
        """fathers#212: Slotfather's ask_yield() and yield_asks() compare a held lease by its own `priority` (even if
        it aged before grant into `served`) against each waiter's effective `served` class."""
        aged_normal = entry(0, pid=42, start=START, priority="normal", served="interactive", yieldable=True)
        interactive_waiter = entry(1, "waiting", priority="interactive")
        self.assertIn("Ask to yield",
                      names(actions.for_request(aged_normal, [interactive_waiter], self.advertised)),
                      "an aged normal lease still yields to an interactive waiter")

        aged_batch = entry(0, pid=43, start=START, priority="batch", served="normal", yieldable=True)
        normal_waiter = entry(1, "waiting", priority="normal")
        self.assertIn("Ask to yield",
                      names(actions.for_request(aged_batch, [normal_waiter], self.advertised)),
                      "an aged batch lease still yields to a normal waiter")

        normal_lease = entry(0, pid=44, start=START, priority="normal", yieldable=True)
        aged_waiter = entry(1, "waiting", priority="normal", served="interactive")
        self.assertIn("Ask to yield",
                      names(actions.for_request(normal_lease, [aged_waiter], self.advertised)),
                      "a waiter aged to interactive can ask a normal lease to yield")
        self.assertNotIn("Ask to yield",
                         names(actions.for_request(normal_lease, [normal_waiter], self.advertised)))

    def test_nothing_without_a_start_or_unadvertised(self) -> None:
        self.assertEqual([], actions.for_request(entry(0, pid=42, start=None), [], self.advertised))
        self.assertEqual([], actions.for_request(entry(0, pid=42, start=START), [], ["land", "promote"]))


class ProcessTests(unittest.TestCase):
    def row(self, ident: str) -> ProcessTableRow:
        found = spawnfather.find(procs_decoded(), ("entry", ident))
        assert found is not None
        return found

    def test_reap_an_orphan_or_expired_entry_confirmed(self) -> None:
        orphan = actions.for_process(self.row("j7a8b9c"), ["reap", "extend"])
        self.assertEqual(["Reap"], names(orphan))
        self.assertTrue(orphan[0].confirm)
        self.assertEqual("spawnfather reap --id j7a8b9c '--reason=godfather: orphan'", orphan[0].shown())
        expired = actions.for_process(self.row("j2b3c4d"), ["reap", "extend"])
        self.assertEqual("spawnfather reap --id j2b3c4d --expired '--reason=godfather: expired'", expired[0].shown(),
                         "Spawnfather refuses it too once the entry was extended")

    def test_a_running_entry_is_extended_never_reaped(self) -> None:
        found = actions.for_process(self.row("j3f9a1c"), ["reap", "extend"])
        self.assertEqual(["Extend TTL by 1 h", "Extend TTL by 4 h", "Extend TTL by 1 d"], names(found))
        self.assertEqual("spawnfather extend j3f9a1c --ttl 4h", found[1].shown())
        self.assertFalse(any(action.confirm for action in found))

    def test_an_unowned_process_is_reaped_by_pid_with_force_and_a_resource_has_none(self) -> None:
        rows = spawnfather.page_rows(procs_decoded())
        process = next(row for row in rows if row["kind"] == "unowned")
        assert process["kind"] == "unowned"
        found = actions.for_process(process, ["reap"])
        self.assertEqual("spawnfather reap --pid 88123 --force '--reason=godfather: unowned'", found[0].shown())
        self.assertEqual([], actions.for_process({**process, "alive": False}, ["reap"]), "gone since the sweep")
        self.assertEqual([], actions.for_process(next(row for row in rows if row["kind"] == "resource"), ["reap"]))
        self.assertEqual([], actions.for_process(process, ["extend", "sweep"]), "not advertised")


class LaneTests(unittest.TestCase):
    def lane(self, ident: str = "710-speech-paste-phase") -> Lane:
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
        self.assertEqual("lanefather park 710-speech-paste-phase --state-dir /fixture/straylight/lanes "
                         "'--reason=no review today'", park[-1].shown("no review today"))
        self.assertEqual("--reason=-f", park[-1].command("-f")[-1], "a reason that looks like an option stays one")
        self.assertNotIn("Park", names(actions.for_lane(self.lane("555-host-rename-names"), ["park"])), "planned")


class FlowTests(PageTests):
    def key(self, state: Any, key: str) -> tuple[Any, list[Any]]:
        state, effects = reducer.reduce(state, state_module.Key(key), 0.0)
        return self.draw(state), effects

    def to_orphan(self) -> Any:
        state = self.keys(self.screen([entry(1)]), "3")
        return dataclasses.replace(state, picked={"spawns": ("entry", "j7a8b9c")})

    def test_enter_opens_the_menu_and_a_reap_asks_first(self) -> None:
        state, _ = self.key(self.to_orphan(), "\r")
        self.assertIsInstance(state.ui.overlay, state_module.ActionMenu)
        self.assertIn("ACTIONS", self.text())
        again, effects = self.key(state, "\r")
        self.assertEqual((state.ui.overlay, []), (again.ui.overlay, effects), "a second Enter chooses nothing")
        state, _ = self.key(state, "j")
        state, effects = self.key(state, "\r")
        self.assertIsInstance(state.ui.overlay, state_module.Confirm)
        self.assertIn("spawnfather reap --id j7a8b9c '--reason=godfather: orphan'", self.text())
        self.assertIn("claude 4bceaaaf/a71c", self.text(), "the target's owner")
        self.assertEqual([], effects, "nothing runs before y")
        for cancel in ("esc", "n", "\r", "x"):
            with self.subTest(cancel=cancel):
                after, effects = self.key(state, cancel)
                self.assertEqual([], [effect for effect in effects if effect.kind == "action"])
        after, effects = self.key(state, "y")
        self.assertEqual(["action"], [effect.kind for effect in effects])
        self.assertIsNone(after.ui.overlay)

    def test_a_running_entry_extends_without_asking(self) -> None:
        state = self.keys(self.screen([entry(1)]), "3")
        state = dataclasses.replace(state, picked={"spawns": ("entry", "j3f9a1c")})
        state, _ = self.key(state, "a")
        state, _ = self.key(state, "j")
        state, _ = self.key(state, "j")
        state, effects = self.key(state, "\r")
        self.assertEqual([("extend", "j3f9a1c", "--ttl", "4h")], [effect.item[0].argv for effect in effects])

    def test_a_row_with_none_opens_no_menu_and_says_so(self) -> None:
        state = self.keys(self.screen([entry(1)]), "3")
        state = dataclasses.replace(state, picked={"spawns": ("resource", "simulator",
                                                              "9F8E7D6C-0000-4000-8000-0123456789AB")})
        state, _ = self.key(state, "\r")
        self.assertIsNone(state.ui.overlay)
        self.assertIn("no actions", self.text())

    def test_park_asks_a_reason_and_an_empty_one_cancels(self) -> None:
        self.tools["lanefather"] = dataclasses.replace(self.tools["lanefather"], actions=("park",))
        state = self.keys(self.screen([entry(1)]), "2", "j")
        state, _ = self.key(state, "\r")
        state, _ = self.key(state, "G")
        state, _ = self.key(state, "\r")
        self.assertIsInstance(state.ui.overlay, state_module.Reason)
        empty, effects = self.key(state, "\r")
        self.assertEqual((None, []), (empty.ui.overlay, effects))
        for char in "q later":  # typed, never a hotkey
            state, _ = self.key(state, char)
        state, _ = self.key(state, "\r")
        self.assertIn("lanefather park 710-speech-paste-phase --state-dir", self.text())
        self.assertIn("/fixture/straylight/lanes '--reason=q later'", self.text(), "the whole command, wrapped")
        state, effects = self.key(state, "y")
        self.assertEqual("q later", effects[0].item[1])

    def test_a_right_click_selects_the_row_and_opens_its_menu(self) -> None:
        state = self.keys(self.screen([entry(1)]), "3")
        x, y = self.at("playwright s")
        state, _ = reducer.reduce(state, state_module.Mouse(2, x, y, True), 0.0)
        self.assertEqual(("entry", "j7a8b9c"), state.picked["spawns"])
        self.assertIsInstance(state.ui.overlay, state_module.ActionMenu)

    def test_a_done_action_says_so_and_polls_its_source(self) -> None:
        state, effects = reducer.reduce(self.screen(), state_module.ActionDone("spawnfather", False, "no such entry"),
                                        0.0)
        assert state.notice is not None
        self.assertEqual(("no such entry", "bad"), (state.notice.text, state.notice.style))
        self.assertEqual([("poll", "spawnfather")], [(effect.kind, effect.key) for effect in effects
                                                     if effect.kind == "poll"])


class FixtureCase(unittest.TestCase):
    """The procs fixture copied, its writes recorded."""

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
        found = spawnfather.find(procs_decoded(), ("entry", "j7a8b9c"))
        assert found is not None
        return actions.for_process(found, ["reap"])[0]


class RunTests(FixtureCase):
    """Screen.act under fixtures: the row checked again, the argv and GODFATHER_ACTOR recorded, never run."""

    def test_recorded_with_the_actor_never_run(self) -> None:
        done = cast(Any, app.Screen().act(self.reap(), ""))
        self.assertTrue(done.ok, done.line)
        line = json.loads(self.record.read_text().splitlines()[-1])
        self.assertEqual(["spawnfather", "reap", "--id", "j7a8b9c", "--reason=godfather: orphan"], line["argv"])
        self.assertTrue(line["env"]["GODFATHER_ACTOR"].startswith("godfather/"))

    def test_a_row_that_changed_is_dropped(self) -> None:
        replies = json.loads((self.tmp / "procs" / "spawnfather" / "replies.json").read_text())
        status = replies["status --json"]["json"]
        status["entries"] = [row for row in status["entries"] if row["id"] != "j7a8b9c"]
        (self.tmp / "procs" / "spawnfather" / "replies.json").write_text(json.dumps(replies))
        screen = app.Screen()
        screen.clients["spawnfather"].discover()
        done = cast(Any, screen.act(self.reap(), ""))
        self.assertFalse(done.ok)
        self.assertIn("changed or is gone", done.line)
        self.assertFalse(self.record.exists(), "nothing run")

    def test_a_failing_command_is_its_first_stderr_line(self) -> None:
        replies = json.loads((self.tmp / "procs" / "spawnfather" / "replies.json").read_text())
        replies["reap --id j7a8b9c --reason=godfather: orphan"] = {"code": 1, "stderr": "spawnfather reap: busy\nmore"}
        (self.tmp / "procs" / "spawnfather" / "replies.json").write_text(json.dumps(replies))
        screen = app.Screen()
        screen.clients["spawnfather"].discover()
        done = cast(Any, screen.act(self.reap(), ""))
        self.assertEqual((False, "spawnfather reap: busy"), (done.ok, done.line))

    def test_a_request_is_checked_by_its_pid_and_start(self) -> None:
        item = request(pid=7, start=START)
        action = actions.for_request(item, [], ["reprioritize"])[0]
        self.assertEqual(action, actions.revalidate(action, {"holders": [item], "waiters": []}))
        self.assertIsNone(actions.revalidate(action, {"holders": [dict(item, start="later")], "waiters": []}))


class RecheckTests(unittest.TestCase):
    """fathers#88 review: an action runs only on a row still in the state it needs, rebuilt from a fresh snapshot."""

    def snapshot(self, **changes: Any) -> Processes:
        raw = json.loads((PROCS / "spawnfather" / "replies.json").read_text())["status --json"]["json"]
        for ident, fields in changes.items():
            for row in raw["entries"]:
                if row["id"] == ident:
                    row.update(fields)
        return spawnfather.decode(raw)

    def reap(self, ident: str) -> actions.Action:
        found = spawnfather.find(procs_decoded(), ("entry", ident))
        assert found is not None
        return actions.for_process(found, ["reap"])[0]

    def test_an_expired_entry_extended_meanwhile_is_not_reaped(self) -> None:
        reap = self.reap("j2b3c4d")
        self.assertEqual(reap, actions.revalidate(reap, self.snapshot()))
        self.assertIsNone(actions.revalidate(reap, self.snapshot(j2b3c4d={"ttl_left": 3600})), "running now")

    def test_an_orphan_whose_owner_came_back_is_not_reaped(self) -> None:
        self.assertIsNone(actions.revalidate(self.reap("j7a8b9c"), self.snapshot(j7a8b9c={"owner_alive": True})))

    def test_an_unowned_pid_an_owned_entry_holds_now_is_not_reaped(self) -> None:
        rows = spawnfather.page_rows(procs_decoded())
        process = next(row for row in rows if row["kind"] == "unowned")
        reap = actions.for_process(process, ["reap"])[0]
        self.assertEqual(reap, actions.revalidate(reap, self.snapshot()))
        self.assertIsNone(actions.revalidate(reap, self.snapshot(j3f9a1c={"pid": 88123})))
        raw = json.loads((PROCS / "spawnfather" / "replies.json").read_text())["status --json"]["json"]
        raw["unowned"][0]["alive"] = False
        self.assertIsNone(actions.revalidate(reap, spawnfather.decode(raw)), "gone since the sweep")

    def test_ask_to_yield_on_a_lease_no_longer_yieldable_and_park_on_a_lane_no_longer_active(self) -> None:
        lease = entry(0, pid=42, start=START, priority="batch", yieldable=True)
        waiter = entry(1, "waiting", priority="interactive")
        ask = next(action for action in actions.for_request(lease, [waiter], ["ask-yield"]))
        self.assertEqual(ask, actions.revalidate(ask, {"holders": [lease], "waiters": [waiter]}))
        self.assertIsNone(actions.revalidate(ask, {"holders": [dict(lease, yieldable=False)], "waiters": [waiter]}))
        self.assertIsNone(actions.revalidate(ask, {"holders": [dict(lease, yields=actions.YIELDS_MAX)],
                                                   "waiters": [waiter]}), "yielded YIELDS_MAX times meanwhile")
        self.assertIsNone(actions.revalidate(ask, {"holders": [lease], "waiters": []}), "no waiter above it now")
        lane = lanefather.find(lanes_decoded(), (STRAYLIGHT, "710-speech-paste-phase"))
        assert lane is not None
        park = actions.for_lane(lane, ["park"])[-1]
        raw = json.loads((ROOT / "tests" / "fixtures" / "lanes" / "lanefather" / "replies.json").read_text())
        raw = raw["status --json"]["json"]
        self.assertEqual(park, actions.revalidate(park, lanefather.decode(raw)))
        raw["projects"][0]["lanes"][1]["status"] = "landed"
        self.assertIsNone(actions.revalidate(park, lanefather.decode(raw)))

    def test_park_names_the_lanes_own_state_directory(self) -> None:
        grid = lanefather.find(lanes_decoded(), ("/fixture/data-grid/lanes", "644-build-once"))
        assert grid is not None
        park = actions.for_lane(grid, ["park"])[-1]
        self.assertEqual(("park", "644-build-once", "--state-dir", "/fixture/data-grid/lanes"), park.argv)
        self.assertEqual(("lanes", ("/fixture/data-grid/lanes", "644-build-once")), park.identity)

    def test_another_users_request_offers_nothing(self) -> None:
        item = entry(0, pid=42, start=START, uid=501)
        self.assertTrue(actions.for_request(item, [], ["reprioritize"], uid=501))
        self.assertEqual([], actions.for_request(item, [], ["reprioritize"], uid=502))

    def test_the_uid_comes_from_slotfathers_entry_and_another_users_offers_no_reprioritize(self) -> None:
        """fathers#88: Slotfather gives `uid` on its `status --json` entries; the adapter pairs it with the row, and
        only the requests of Godfather's own uid offer reprioritize. A row without one (an older Slotfather) still
        offers it: Slotfather's refusal is then the check."""
        since = "2026-11-13T09:48:00Z"
        me = os.geteuid()
        rows = [expected_raw_item(pid, since, "waiting") for pid in (41, 42, 43)]
        found = slotfather_adapter.decode(expected_snapshot([], rows, entries=[
            {"pid": 41, "since": since, "lease_id": "mine", "start": START, "uid": me},
            {"pid": 42, "since": since, "lease_id": "theirs", "start": START, "uid": me + 1},
            {"pid": 43, "since": since, "lease_id": "older", "start": START}]))
        waiters = found["waiters"]
        self.assertEqual([me, me + 1, None], [item.get("uid") for item in waiters])
        offered = [names(actions.for_request(cast(dict[str, Any], item), [], ["reprioritize"], me))
                   for item in waiters]
        self.assertEqual([["Reprioritize to interactive", "Reprioritize to batch"], [],
                          ["Reprioritize to interactive", "Reprioritize to batch"]], offered)
        theirs = actions.for_request(cast(dict[str, Any], dict(waiters[1], uid=me)), [], ["reprioritize"], me)[0]
        self.assertIsNone(actions.revalidate(theirs, cast(dict[str, Any], found), me),
                          "a fresh snapshot naming another user drops the action")


class ExecutorTests(FixtureCase):
    """fathers#88 review: the pager goes through the one executor; the worker has Sources of its own; an opener is
    not waited for; the same action in flight is not started twice."""

    def lane_action(self, name: str) -> actions.Action:
        lane = lanefather.find(lanes_decoded(), (STRAYLIGHT, "710-speech-paste-phase"))
        assert lane is not None
        return next(action for action in actions.for_lane(lane, []) if action.name == name)

    def test_the_log_is_checked_then_handed_to_the_screen(self) -> None:
        import types

        log = "/fixture/straylight/lanes/logs/lane-710-speech-paste-phase-20261104T095020.log"
        action = self.lane_action("Open the latest log")
        done = cast(Any, app.Screen().act(action, ""))
        self.assertIsInstance(done, state_module.HandOff)
        for pager_env, expected in (({}, ["less", "-R", "+G", log]), ({"PAGER": "cat"}, ["cat", log])):
            with self.subTest(pager=pager_env):
                env = {key: value for key, value in os.environ.items() if key != "PAGER"}
                env.update(pager_env)
                with mock.patch.dict(os.environ, env, clear=True):
                    paged = app.Live.page_file(cast(Any, types.SimpleNamespace()), done.action, done.key)
                self.assertTrue(paged.ok, paged.line)
                line = json.loads(self.record.read_text().splitlines()[-1])
                self.assertEqual(expected, line["argv"])

    def test_a_malformed_pager_is_a_footer_line(self) -> None:
        import types

        with mock.patch.dict(os.environ, {"PAGER": "less 'unclosed"}):
            done = app.Live.page_file(cast(Any, types.SimpleNamespace()), self.lane_action("Open the latest log"),
                                      ())
        self.assertFalse(done.ok)
        self.assertIn("PAGER", done.line)

    def test_the_worker_reads_its_own_sources(self) -> None:
        screen = app.Screen()
        client = screen.clients["spawnfather"]
        self.assertIsNone(client.state().snapshot)
        with mock.patch.object(client, "refresh", side_effect=AssertionError("loader's")):
            done = cast(Any, screen.act(self.reap(), ""))
        self.assertTrue(done.ok)
        self.assertIsNone(client.state().snapshot, "poll_fresh never mutates the cached snapshot")

    def test_an_opener_is_started_not_waited_for(self) -> None:
        started: list[dict[str, Any]] = []

        def popen(argv: list[str], **kwargs: Any) -> None:
            started.append(dict(kwargs, argv=argv))

        with mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": ""}):
            os.environ.pop("GODFATHER_FIXTURES")
            with mock.patch("subprocess.run", side_effect=AssertionError("waited for")), \
                    mock.patch("subprocess.Popen", side_effect=popen):
                reply = sources.act("", ["xdg-open", "https://example.org"], "godfather/test", detach=True)
        self.assertEqual(0, reply.code)
        self.assertEqual(["xdg-open", "https://example.org"], started[0]["argv"])
        self.assertTrue(started[0]["start_new_session"], "in a session of its own")
        self.assertEqual("godfather/test", started[0]["env"]["GODFATHER_ACTOR"])

    def test_the_same_action_in_flight_is_not_started_twice(self) -> None:
        reap = self.reap()
        confirm = state_module.AppState(
            ui=state_module.UiState(state_module.PageScreen("spawns"), state_module.Confirm(reap)),
            tools={"spawnfather": state_module.Tool("spawnfather", "ok")},
        )
        state, effects = reducer.reduce(confirm, state_module.Key("y"), 0.0)
        self.assertEqual(["action"], [effect.kind for effect in effects])
        self.assertIsNone(state.ui.modal)
        again_confirm = dataclasses.replace(
            state,
            ui=dataclasses.replace(state.ui, modal=state_module.Confirm(reap)),
        )
        again, effects = reducer.reduce(again_confirm, state_module.Key("y"), 0.0)
        self.assertEqual([], effects)
        self.assertIn("already running", cast(Any, again.notice).text)
        done, _ = reducer.reduce(state, state_module.ActionDone("spawnfather", True, "ok",
                                                                state_module.action_key(reap)), 0.0)
        self.assertEqual(frozenset(), done.acting)


class LiveTests(unittest.TestCase):
    def test_reap_an_orphan_in_a_pty(self) -> None:
        import terminal

        tmp = Path(tempfile.mkdtemp(prefix="godfather-actions-live-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        record = tmp / "record.jsonl"
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GODFATHER_", "SLOTFATHER_"))}
        env.update(GODFATHER_FIXTURES=str(PROCS), GODFATHER_FIXTURES_RECORD=str(record), TZ="UTC")
        screen = terminal.Terminal(self, env, 43, 156, [sys.executable, str(terminal.ENTRY), "--interval", "0.5"])
        screen.receive("3 spawns 5")
        at = screen.mark()
        screen.write("3")
        screen.receive("SPAWN DETAILS", since=at)
        screen.write("gjjj")  # vite dev, storybook, the iPhone, then the orphaned Playwright server
        at = screen.mark()
        screen.write("\r")
        screen.receive("ACTIONS", "Reap", since=at)
        at = screen.mark()
        screen.write("j\r")
        screen.receive("spawnfather reap --id j7a8b9c", "y run it", since=at)
        at = screen.mark()
        screen.write("y")
        screen.until(lambda _: record.exists() and record.read_text().endswith("\n"), what="the reap recorded")
        line = json.loads(record.read_text().splitlines()[-1])
        self.assertEqual(["spawnfather", "reap", "--id", "j7a8b9c", "--reason=godfather: orphan"], line["argv"])
        screen.receive("recorded: spaw", since=at)  # the footer's line, as long as it fits
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


class EveryRowTests(unittest.TestCase):
    """Each row of the actions table in a PTY under fixtures (fathers#88 review 10): the keys to the row and through
    the menu (and the confirmation or the reason), and the argv recorded, never run."""

    LOG = "/fixture/straylight/lanes/logs/lane-644-build-once-20261104T0950110.log"
    CASES = [  # name, keys to the row, keys in the menu and after, the exact recorded argv
        ("reprioritize", "", "j\r",
         ["slotfather", "reprioritize", "10000", "interactive", "--start", "Tue Nov  4 08:40:00 2026"]),
        ("ask to yield", "j", "jjj\r",
         ["slotfather", "ask-yield", "10001", "--start", "Tue Nov  4 08:41:00 2026"]),
        ("extend", "3g", "j\r", ["spawnfather", "extend", "j3f9a1c", "--ttl", "1h"]),
        ("reap an orphan", "3gjjj", "j\ry", ["spawnfather", "reap", "--id", "j7a8b9c", "--reason=godfather: orphan"]),
        ("reap an expired entry", "3gjjjj", "j\ry",
         ["spawnfather", "reap", "--id", "j2b3c4d", "--expired", "--reason=godfather: expired"]),
        ("reap an unowned process", "3Gkk", "j\ry",
         ["spawnfather", "reap", "--pid", "88123", "--force", "--reason=godfather: unowned"]),
        ("open the ticket", "2", "j\r", [actions.opener(), "https://github.com/maxvyshniakov/straylight/issues/644"]),
        ("open the latest log", "2", "jj\r", ["less", "-R", "+G", LOG]),
        ("open the worktree", "2", "jjj\r", [actions.opener(), "/fixture/straylight/worktrees/644-build-once"]),
        ("park", "2", "G\rlater\ry",
         ["lanefather", "park", "644-build-once", "--state-dir", "/fixture/straylight/lanes", "--reason=later"]),
    ]

    def test_each_row_and_its_action(self) -> None:
        import terminal

        for name, to_row, menu, argv in self.CASES:
            with self.subTest(name):
                tmp = Path(tempfile.mkdtemp(prefix="godfather-every-row-"))
                self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
                record = tmp / "record.jsonl"
                env = {key: value for key, value in os.environ.items()
                       if not key.startswith(("GODFATHER_", "SLOTFATHER_", "PAGER"))}
                env.update(GODFATHER_FIXTURES=str(PROCS), GODFATHER_FIXTURES_RECORD=str(record), TZ="UTC")
                screen = terminal.Terminal(self, env, 43, 156,
                                           [sys.executable, str(terminal.ENTRY), "--interval", "0.5"])
                screen.receive("3 spawns 5", "2 lanes")
                for key in to_row:
                    at = screen.mark()
                    screen.write(key)
                    if key in "23":
                        screen.receive("LANE DETAILS" if key == "2" else "SPAWN DETAILS", since=at)
                at = screen.mark()
                screen.write("\r")
                screen.receive("ACTIONS", since=at)
                for key in menu:
                    screen.write(key)
                screen.until(lambda _, written=record: written.exists() and written.read_text().endswith("\n"),
                             what=f"{name} recorded")
                line = json.loads(record.read_text().splitlines()[-1])
                self.assertEqual(argv, line["argv"])
                self.assertTrue(line["env"]["GODFATHER_ACTOR"].startswith("godfather/"))
                self.assertEqual(0, screen.quit())


class StandInTests(unittest.TestCase):
    """The live dashboard against a stand-in Slotfather (tests/fake_slotfather.py) that answers reprioritize and
    ask-yield as Slotfather does: Godfather's argv and GODFATHER_ACTOR, the ❓ on the next poll and the
    `lease.yield_asked` event with `actor.via` godfather in EVENTS (fathers#88 review 10)."""

    def test_ask_to_yield_and_reprioritize(self) -> None:
        import terminal

        case = terminal.LiveCase("run")
        case.setUp()
        self.addCleanup(case.fake.close)
        case.fake.update(actions=["reprioritize", "ask-yield"])
        case.queue(holders=[terminal.item("batchy", 300, priority="batch", yieldable=True, start=START)],
                   waiters=[terminal.item("hurry", 301, kind="waiting", priority="interactive", start=START)])
        case.fake.update(snapshot=dict(case.fake.read()["snapshot"]))
        screen = terminal.Terminal(self, terminal.environ(case.fake), 40, 140,
                                   [sys.executable, str(terminal.ENTRY), "--interval", "0.3"])
        screen.receive("batchy", "hurry")
        at = screen.mark()
        screen.write("\r")
        screen.receive("ACTIONS", "Ask to yield", since=at)
        at = screen.mark()
        screen.write("jjj\r")  # Reprioritize to interactive, to normal, then Ask to yield
        screen.receive("❓", since=at)
        written = case.fake.state.with_name(case.fake.state.name + ".env")
        line = json.loads(written.read_text().splitlines()[-1])
        self.assertEqual(["ask-yield", "300", "--start", START], line["argv"])
        self.assertTrue(line["GODFATHER_ACTOR"].startswith("godfather/"))
        events = [json.loads(text) for text in (case.fake.root / "events.jsonl").read_text().splitlines()]
        from godfather import eventcheck

        for emitted in events:
            self.assertEqual([], eventcheck.validate(emitted, strict=True), emitted)
        asked = [event for event in events if event["kind"] == "lease.yield_asked"]
        self.assertEqual("godfather", asked[-1]["actor"]["via"])
        self.assertEqual({"count": 1, "for": {"id": "r-301", "label": "hurry", "priority": "interactive"},
                          "by_hand": True}, asked[-1]["details"])
        screen.write("\t")  # EVENTS: the event is there, with the run's rows
        screen.receive("asked", "for hurry", since=at)
        at = screen.mark()
        screen.write("\t\t\r")  # back to QUEUE, its menu again: reprioritize to interactive
        screen.receive("ACTIONS", since=at)
        screen.write("j\r")
        screen.until(lambda _: "reprioritize" in written.read_text(), what="the reprioritize")
        line = json.loads(written.read_text().splitlines()[-1])
        self.assertEqual(["reprioritize", "300", "interactive", "--start", START], line["argv"])
        reprioritized = [json.loads(text) for text in (case.fake.root / "events.jsonl").read_text().splitlines()][-1]
        self.assertEqual([], eventcheck.validate(reprioritized, strict=True))
        self.assertEqual("request.reprioritized", reprioritized["kind"])
        self.assertEqual(0, screen.quit())

    def test_fake_slotfather_rejects_invalid_events_strictly(self) -> None:
        import fake_slotfather

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "events.jsonl"
            with self.assertRaisesRegex(ValueError, "invalid event"):
                fake_slotfather.append_event(log, {"v": 1, "kind": "lease.yield_asked", "extra": True})
            self.assertFalse(log.exists())


class ContractTests(unittest.TestCase):
    """The real Spawnfather (the sibling checkout) in a temporary state directory: extend and reap a resource entry
    through sources.act, as the app runs them."""

    def test_extend_and_reap(self) -> None:
        package = ROOT.parent / "spawnfather"
        if not (package / "spawnfather" / "dashboard.py").is_file():
            self.skipTest("no spawnfather checkout beside godfather")
        tmp = Path(tempfile.mkdtemp(prefix="godfather-spawn-actions-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        python = shutil.which("python3") or sys.executable
        (bin_dir / "spawnfather").write_text(f"#!/bin/sh\nPYTHONPATH={package} exec {python} -m spawnfather \"$@\"\n")
        (bin_dir / "spawnfather").chmod(0o755)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp), "SPAWNFATHER_STATE": str(tmp / "state")}
        flag = tmp / "running"  # the resource: alive while this file is there, stopped by removing it
        flag.write_text("")
        owned = subprocess.run([str(bin_dir / "spawnfather"), "own-resource", "--kind", "command", "--id", "fixture-2",
                                "--alive", f"test -e {flag}", "--stop", f"rm -f {flag}", "--label", "fixture",
                                "--ttl", "3600"],
                               capture_output=True, text=True, env=env, cwd=tmp, timeout=60)
        if owned.returncode != 0:
            self.skipTest(f"spawnfather cannot run here: {owned.stderr.strip().splitlines()[-1:]}")
        with mock.patch.dict(os.environ, env, clear=True):
            found = spawnfather.decode(json.loads(sources.run("spawnfather", ["status", "--json"], 30).stdout))
            entry_row = next(row for row in found["entries"] if row["resource"])
            extend = actions.for_process(entry_row, ["extend"])[0]
            reply = sources.act("spawnfather", list(extend.argv), "godfather/test")
            self.assertEqual(0, reply.code, f"extend: {reply.stderr}")
            reap = actions.Action("spawnfather", "Reap", ("reap", "--id", entry_row["id"], "--reason",
                                                          "godfather: test"), confirm=True)
            reply = sources.act("spawnfather", list(reap.argv), "godfather/test")
            self.assertEqual(0, reply.code, f"reap: {reply.stdout} {reply.stderr}")
            after = spawnfather.decode(json.loads(sources.run("spawnfather", ["status", "--json"], 30).stdout))
            self.assertFalse([row for row in after["entries"] if row["id"] == entry_row["id"]])


class SlotfatherContractTests(unittest.TestCase):
    """The real Slotfather (the sibling checkout) in an isolated SLOTFATHER_DIR, with live leases and waiters:
    reprioritize and ask-yield run through Screen.act, as the app runs them (the row checked again in a fresh
    `status --json --full`, then the command with GODFATHER_ACTOR), and a fresh snapshot shows what changed."""

    def setUp(self) -> None:
        if not SLOTFATHER.is_file():
            self.skipTest("no slotfather checkout beside godfather")
        self.tmp = Path(tempfile.mkdtemp(prefix="godfather-slot-actions-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        self.started_log, self.release = self.tmp / "log", self.tmp / "release"
        self.release.mkdir()
        self.started_log.touch()
        (self.tmp / "memory").write_text("8000 1\n")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.tool = bin_dir / "slotfather"  # this interpreter, whatever python3 /usr/bin has
        self.tool.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{SLOTFATHER}" "$@"\n')
        self.tool.chmod(0o755)
        # 12 cores and 64 GiB: 4 slots; the host's own load and Spawnfather stay out of it
        self.env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(self.tmp), "TMPDIR": str(self.tmp),
                    "XDG_CONFIG_HOME": str(self.tmp / "config"), "SLOTFATHER_DIR": str(self.state),
                    "SLOTFATHER_LEGACY_DIRS": "", "SLOTFATHER_CORES": "12", "SLOTFATHER_MEMORY_GIB": "64",
                    "SLOTFATHER_MEMORY_FILE": str(self.tmp / "memory"), "SLOTFATHER_LOADAVG": "0",
                    "SLOTFATHER_POLL": "0.05", "SLOTFATHER_POLL_MAX": "0.5", "SLOTFATHER_OVERTAKE_WINDOW": "0",
                    "SLOTFATHER_AUTO_TAGS": "0", "SLOTFATHER_SPAWNFATHER": str(self.tmp / "no-spawnfather"),
                    "LOG": str(self.started_log), "RELEASE": str(self.release)}
        self.processes: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self.stop_all)
        # nobody asks a lease to yield on their own here: only Godfather's ask-yield does
        self.slotfather("order", "--yield-after", "3600")
        found = json.loads(self.slotfather("capabilities", "--json").stdout)
        self.advertised: list[str] = found["dashboard"]["actions"]
        self.assertTrue({"reprioritize", "ask-yield"} <= set(self.advertised), found)
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stop_all(self) -> None:
        """Each request runs in a session of its own: stopping its group stops its work too."""
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):  # macOS: a group whose leader exited unreaped
                pass
            process.wait()

    def slotfather(self, *argv: str) -> subprocess.CompletedProcess[str]:
        found = subprocess.run([str(self.tool), *argv], capture_output=True, text=True, env=self.env,
                               stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(0, found.returncode, f"slotfather {' '.join(argv)}: {found.stderr}")
        return found

    def request(self, label: str, count: int, *options: str) -> None:
        """`slotfather run -n COUNT --label LABEL OPTIONS -- work` in the background, running until released."""
        self.processes.append(subprocess.Popen(
            [str(self.tool), "run", "-n", str(count), "--label", label, *options, "--", "sh", "-c", HOLD, "work",
             label], env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True))

    def entries(self) -> dict[str, dict[str, Any]]:
        return {item["label"]: item for item in json.loads(self.slotfather("status", "--json").stdout)["entries"]}

    def wait_for(self, predicate: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}: {self.slotfather('status').stdout}")

    def queued(self, holder: tuple[str, str], waiter: tuple[str, str]) -> None:
        """A yieldable 4-slot lease `holder` (label, class) running, and `waiter` (1 slot) queued behind it."""
        self.request(holder[0], 4, "--priority", holder[1], "--yieldable")
        self.wait_for(lambda: f"start {holder[0]}" in self.started_log.read_text(), f"{holder[0]} to start")
        self.request(waiter[0], 1, "--priority", waiter[1])
        self.wait_for(lambda: self.entries().get(waiter[0], {}).get("state") == "waiting", f"{waiter[0]} to queue")

    def fresh(self) -> Dashboard:
        return slotfather_adapter.decode(json.loads(self.slotfather("status", "--json", "--full").stdout))

    def last_event(self, kind: str) -> dict[str, Any]:
        found = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]
        matching = [event for event in found if event["kind"] == kind]
        self.assertTrue(matching, f"no {kind} in {[event['kind'] for event in found]}")
        self.assertEqual([], eventcheck.validate(matching[-1], strict=True), matching[-1])
        return matching[-1]

    def run_action(self, label: str, name: str) -> None:
        """The action `name` QUEUE offers on the request `label`, run as the app runs it."""
        with app.Screen() as screen:
            model, error = screen.load()
            self.assertIsNone(error, screen.clients["slotfather"].state().problem)
            assert model is not None
            waiters = cast(list[dict[str, Any]], model["waiters"])
            item = next(row for row in [*cast(list[dict[str, Any]], model["holders"]), *waiters]
                        if row["label"] == label)
            self.assertEqual(os.geteuid(), item.get("uid"), "Slotfather names the holder's uid")
            offered = actions.for_request(item, waiters, self.advertised, os.geteuid())
            action = next((found for found in offered if found.name == name), None)
            self.assertIsNotNone(action, names(offered))
            assert action is not None
            done = cast(Any, screen.act(action, ""))
            self.assertTrue(done.ok, done.line)

    def test_reprioritize_a_queued_request(self) -> None:
        self.queued(("gate", "normal"), ("quick", "batch"))
        self.run_action("quick", "Reprioritize to interactive")
        waiter = next(item for item in self.fresh()["waiters"] if item["label"] == "quick")
        self.assertEqual(("interactive", "batch"), (waiter["priority"], waiter.get("reprioritized")))
        moved = self.last_event("request.reprioritized")
        self.assertEqual(("godfather", "batch", "interactive"),
                         (moved["actor"]["via"], moved["details"]["from"], moved["details"]["to"]))

    def test_ask_a_lower_class_lease_to_yield_for_a_waiter(self) -> None:
        # The holder does not look between its steps: the request stands, ❓ on QUEUE, and nothing stops it
        self.queued(("gate", "batch"), ("quick", "interactive"))
        self.run_action("gate", "Ask to yield")
        model = self.fresh()
        held = next(item for item in model["holders"] if item["label"] == "gate")
        self.assertEqual("quick", held["asked"])
        canvas = Canvas(120, 8)
        draw_requests(canvas, Rect(0, 0, 8, 120), model, 0, datetime.datetime.now(datetime.UTC))
        rows = ["".join(str(cell[0]) for cell in line) for line in canvas.cells]
        self.assertIn("❓", next(row for row in rows if "gate" in row), "\n".join(rows))
        asked = self.last_event("lease.yield_asked")
        self.assertEqual(("godfather", "quick", True),
                         (asked["actor"]["via"], asked["details"]["for"]["label"], asked["details"]["by_hand"]))
        self.assertEqual("held", self.entries()["gate"]["state"], "the holder runs on")


if __name__ == "__main__":
    unittest.main()
