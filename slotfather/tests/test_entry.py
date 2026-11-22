#!/usr/bin/env python3
"""The queue entry module (fathers#33): its phase, its accessors and its transitions, and a property test that random
grant, yield, resume, reprioritize and release sequences leave the same entries through the live functions as
through the transitions the replay applies (tools/replay.py), and leave an entry this client does not know as it
was (fathers#19)."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import contextlib
import io
import json
import random
import shutil
import tempfile
import unittest
from typing import Any, Callable, cast
from unittest import mock

from slotfather import config as sf_config
from slotfather import entry as sf_entry
from slotfather import leases as sf_leases
from slotfather import client as sf_client
from slotfather import queue as sf_queue
from slotfather import schedule as sf_schedule
from slotfather import settings as sf_settings
from slotfather import state as sf_state
from test_entry_pins import T0, Queue, stamp, unknown
from test_slotfather import ROOT
from test_usage import load

replay = cast(Any, load(str(ROOT / "tools" / "replay.py"), "slotfather_replay_for_entry"))
install = cast(Any, load(str(ROOT / "install.py"), "slotfather_install_for_entry"))


def waiter(**extra: Any) -> dict[str, Any]:
    return {"pid": 7, "start": "Mon Jan  1 00:00:00 2026", "count": 1, "since": "2026-01-01T00:00:00Z",
            "state": "waiting", **extra}


class PhaseTests(unittest.TestCase):
    def test_a_phase_from_the_state_and_what_lies_beside_it(self) -> None:
        held = waiter(state="held")
        self.assertEqual(("waiting", False), tuple(sf_entry.phase(waiter())))
        self.assertEqual(("yielded", False), tuple(sf_entry.phase(waiter(yielded_at=T0))))
        self.assertEqual(("held", False), tuple(sf_entry.phase(held)))
        self.assertEqual(("asked", True), tuple(sf_entry.phase(held, asked=True, idle_since=T0)))
        self.assertEqual(("held", False), tuple(sf_entry.phase(dict(held, yielded_at=T0))),
                         "an older client granted a yielded lease: it holds")

    def test_split_keeps_each_side_in_queue_order(self) -> None:
        entries = [waiter(label="a"), waiter(label="b", state="held"), waiter(label="c")]
        held, waiting = sf_entry.split(entries)
        self.assertEqual((["b"], ["a", "c"]), ([e["label"] for e in held], [e["label"] for e in waiting]))

    def test_the_outcome_of_a_request_that_leaves(self) -> None:
        self.assertEqual("released", sf_entry.outcome(waiter(state="held")))
        self.assertEqual("released", sf_entry.outcome(waiter(yields=1)), "a yielded lease ran")
        self.assertEqual("left", sf_entry.outcome(waiter()))
        self.assertEqual("reaped", sf_entry.outcome(waiter(state="held"), reaped=True))

    def test_decide_outcome_uses_client_knowledge_and_produces_history_fields(self) -> None:
        held = waiter(state="held")
        waiting = waiter()
        # Client never saw it as held -> Left even if the file says held (grant raced with timeout/signal)
        raced = sf_entry.decide_outcome(held, held_seen=False, left_reason="timeout")
        self.assertEqual(sf_entry.Left("timeout"), raced)
        self.assertEqual({"outcome": "left", "reason": "timeout"}, sf_entry.outcome_fields(raced))
        # Client saw it as held -> Ran
        ran = sf_entry.decide_outcome(held, held_seen=True, status=2)
        self.assertEqual(sf_entry.Ran(exit=2), ran)
        self.assertEqual({"outcome": "released"}, sf_entry.outcome_fields(ran))
        # Reaped distinguishes held vs waiting
        reaped_held = sf_entry.decide_outcome(held, reaped=True)
        self.assertEqual(sf_entry.Reaped(was_held=True), reaped_held)
        self.assertEqual({"outcome": "reaped", "was_held": True}, sf_entry.outcome_fields(reaped_held))
        reaped_waiting = sf_entry.decide_outcome(waiting, reaped=True)
        self.assertEqual(sf_entry.Reaped(was_held=False), reaped_waiting)
        self.assertEqual({"outcome": "reaped", "was_held": False}, sf_entry.outcome_fields(reaped_waiting))
        # Refused carries its reason
        refused = sf_entry.decide_outcome(waiting, refused="memory")
        self.assertEqual(sf_entry.Refused("memory"), refused)
        self.assertEqual({"outcome": "refused", "reason": "memory"}, sf_entry.outcome_fields(refused))

    def test_grant_injected_between_last_read_and_deadline_is_recorded_left_not_ran(self) -> None:
        """Issue #10: a concurrent grant after the client's last read at timeout records Left, not Ran,
        and does not feed runtime estimates."""
        from slotfather import estimates as sf_estimates
        root = _Path(tempfile.mkdtemp(prefix="slotfather-race-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        queue = Queue(root)
        real_release = sf_leases.release

        def inject_grant_then_release(path: _Path, count: int, status: int | None, usage: dict[str, Any] | None,
                                      **kwargs: Any) -> None:
            # Simulate another client granting this entry between wait_held's last read and lease.release()
            data = json.loads(path.read_text())
            sf_entry.to_held(data, stamp(T0 + 5), T0 + 5)
            sf_state.write(path, data)
            real_release(path, count, status, usage, **kwargs)

        with queue.running(), \
                mock.patch.object(sf_client, "wait_held", side_effect=sf_config.SlotTimeout("timed out")), \
                mock.patch.object(sf_leases, "release", side_effect=inject_grant_then_release):
            with self.assertRaises(sf_config.SlotTimeout):
                sf_client.acquire(1, "race-job", "echo hi", wait=0.1, out=io.StringIO())

        lines = [json.loads(line) for line in (queue.directory / "history.jsonl").read_text().splitlines() if line]
        self.assertEqual(1, len(lines))
        item = lines[0]
        self.assertEqual("left", item["outcome"])
        self.assertEqual("timeout", item["reason"])
        self.assertIsNone(item["granted"])
        self.assertIsNone(item["ran"])
        # Must not feed runtime estimates
        est = sf_estimates.history_estimates(queue.directory)
        self.assertIsNone(est.of("race-job", "echo hi"))


class TransitionTests(unittest.TestCase):
    def test_a_yield_and_its_resume_keep_the_first_grant(self) -> None:
        entry = waiter(state="held", granted=stamp(T0), reserved=T0 + 5, pressed=True)
        sf_entry.to_yielded(entry, T0 + 100.0004, T0 - 1)
        self.assertEqual({"state": "waiting", "yields": 1, "yielded_at": T0 + 100.0, "first_granted": stamp(T0),
                          "rank": T0 - 1}, {k: entry[k] for k in ("state", "yields", "yielded_at", "first_granted",
                                                                  "rank")})
        self.assertNotIn("reserved", entry)
        self.assertNotIn("pressed", entry)
        sf_entry.to_held(entry, stamp(T0 + 160), T0 + 160)
        self.assertEqual({"state": "held", "granted": stamp(T0), "resumed": stamp(T0 + 160), "yielded": 60.0,
                          "yields": 1}, {k: entry[k] for k in ("state", "granted", "resumed", "yielded", "yields")})
        for gone in ("yielded_at", "first_granted", "rank"):
            self.assertNotIn(gone, entry)

    def test_a_grant_marks_memory_pressure_and_ageing(self) -> None:
        entry = waiter(priority="batch")
        sf_entry.to_held(entry, stamp(T0), T0, pressed=True, served="normal")
        self.assertEqual((True, "normal"), (entry["pressed"], entry["aged"]))
        same = waiter(priority="batch", pressed=True)
        sf_entry.to_held(same, stamp(T0), T0, pressed=False, served="batch")
        self.assertNotIn("pressed", same)
        self.assertNotIn("aged", same)

    def test_trials_are_copies(self) -> None:
        entry = waiter(state="held", granted=stamp(T0), yielded=3.0)
        trial = sf_entry.trial_yielded(entry, T0)
        self.assertEqual(("waiting", T0, "held"), (trial["state"], trial["rank"], entry["state"]))
        started = sf_entry.trial_held(waiter(yielded=3.0), stamp(T0))
        self.assertEqual(("held", stamp(T0)), (started["state"], started["granted"]))
        self.assertNotIn("yielded", started)

    def test_a_priority_change_keeps_the_class_asked_first(self) -> None:
        entry = waiter(priority="batch", aged="normal")
        sf_entry.to_reprioritized(entry, "interactive", T0 + 0.0004)
        sf_entry.to_reprioritized(entry, "normal", T0 + 10)
        self.assertEqual(("normal", "batch", T0 + 10), (entry["priority"], entry["reprioritized"],
                                                        entry["reprioritized_at"]))
        self.assertNotIn("aged", entry)

    def test_a_new_request_writes_the_fields_in_the_order_clients_always_have(self) -> None:
        entry = sf_entry.new_request(1, "s", 2, "l", "c", "/w", "t", "normal", "id", started=5, ticket="3",
                                     whole=True, meta={"a": "b"}, naming={"given": "g", "project": "p"},
                                     session={"name": "n"}, git={"head": "h"}, yieldable=True, until="u",
                                     yield_after=0.0)
        self.assertEqual(["pid", "start", "count", "label", "cmd", "cwd", "since", "state", "priority", "lease_id",
                          "started", "ticket", "all", "meta", "label_given", "project", "session", "git",
                          "yieldable", "until", "yield_after"], list(entry))
        bare = sf_entry.new_request(1, "s", 2, "l", "c", "/w", "t", "normal", "id")
        self.assertEqual(["pid", "start", "count", "label", "cmd", "cwd", "since", "state", "priority", "lease_id"],
                         list(bare))


class StageTransitionTableTests(unittest.TestCase):
    """Table-driven verification of `Stage`, `LEGAL_TRANSITIONS`, `SidecarEffect`, older-client `settle()`,
    and lock-only yield accounting (fathers#224)."""

    def _entry_in_stage(self, stage: sf_entry.Stage) -> dict[str, Any]:
        if stage == sf_entry.Stage.UNKNOWN:
            return waiter(state="paused")
        if stage == sf_entry.Stage.WAITING:
            return waiter()
        if stage == sf_entry.Stage.HELD:
            entry = waiter()
            sf_entry.to_held(entry, stamp(T0), T0)
            return entry
        if stage == sf_entry.Stage.UNSETTLED:
            entry = waiter()
            sf_entry.to_held(entry, stamp(T0), T0)
            sf_entry.to_yielded(entry, T0 + 10, T0 - 1)
            entry.update(state="held", granted=stamp(T0 + 40))
            return entry
        if stage == sf_entry.Stage.YIELDED:
            entry = waiter()
            sf_entry.to_held(entry, stamp(T0), T0)
            sf_entry.to_yielded(entry, T0 + 10, T0 - 1)
            return entry
        if stage == sf_entry.Stage.LOCK_NOTED:
            entry = waiter()
            sf_entry.to_held(entry, stamp(T0), T0)
            sf_entry.to_lock_noted(entry, "/tmp/x.lock", (7, "Mon Jan  1 00:00:00 2026"))
            return entry
        if stage == sf_entry.Stage.LOCK_YIELDED:
            entry = waiter()
            sf_entry.to_held(entry, stamp(T0), T0)
            sf_entry.to_lock_waiting(entry, T0 + 10, "/tmp/x.lock", (7, "Mon Jan  1 00:00:00 2026"))
            return entry
        if stage == sf_entry.Stage.SESSION_ACTIVE:
            entry = waiter(session={"name": "perf", "idle": 60.0})
            sf_entry.to_held(entry, stamp(T0), T0)
            return entry
        if stage == sf_entry.Stage.SESSION_STOPPING:
            entry = waiter(session={"name": "perf", "idle": 60.0})
            sf_entry.to_held(entry, stamp(T0), T0)
            sf_entry.session_step(
                entry,
                {"active": {"99": {"start": "s"}}, "runs": 0, "busy": 0.0, "last_use": T0},
                moment=T0 + 5,
                began=T0,
                idle=60.0,
                stop_asked=True,
            )
            return entry
        if stage == sf_entry.Stage.SESSION_ENDED:
            entry = waiter(session={"name": "perf", "idle": 60.0})
            sf_entry.to_held(entry, stamp(T0), T0)
            sf_entry.session_step(
                entry,
                {"active": {}, "runs": 1, "busy": 2.5, "last_use": T0},
                moment=T0 + 5,
                began=T0,
                idle=60.0,
                stop_asked=True,
            )
            return entry
        raise AssertionError(f"not an entry-backed stage: {stage}")

    def test_legal_transition_table_covers_every_stage_and_leaves_terminals_closed(self) -> None:
        self.assertEqual(set(sf_entry.Stage), set(sf_entry.LEGAL_TRANSITIONS))
        for terminal in (sf_entry.Stage.UNKNOWN, *sf_entry.TERMINAL_STAGES):
            self.assertEqual(frozenset(), sf_entry.LEGAL_TRANSITIONS[terminal], terminal.value)
        live_stages = (
            sf_entry.Stage.UNKNOWN,
            sf_entry.Stage.WAITING,
            sf_entry.Stage.HELD,
            sf_entry.Stage.UNSETTLED,
            sf_entry.Stage.YIELDED,
            sf_entry.Stage.LOCK_NOTED,
            sf_entry.Stage.LOCK_YIELDED,
            sf_entry.Stage.SESSION_ACTIVE,
            sf_entry.Stage.SESSION_STOPPING,
            sf_entry.Stage.SESSION_ENDED,
        )
        for expected in live_stages:
            with self.subTest(stage=expected.value):
                self.assertEqual(expected, sf_entry.stage_of(self._entry_in_stage(expected)))

    def test_legal_transitions_and_declarative_sidecar_effects(self) -> None:
        owner = (4242, "Wed Nov 11 00:00:00 2026")
        cases: list[tuple[str, Callable[[], tuple[sf_entry.Stage, sf_entry.Stage, sf_entry.SidecarEffect]],
                          sf_entry.Stage, sf_entry.Stage, sf_entry.SidecarEffect]] = [
            (
                "waiting -> held",
                lambda: (
                    (e := waiter()),
                    before := sf_entry.stage_of(e),
                    eff := sf_entry.to_held(e, stamp(T0), T0),
                    (before, sf_entry.stage_of(e), eff),
                )[-1],
                sf_entry.Stage.WAITING,
                sf_entry.Stage.HELD,
                sf_entry.SidecarEffect(unlink_yield=True, unlink_idle=True),
            ),
            (
                "waiting -> session-active initializes .session",
                lambda: (
                    (e := waiter(session={"name": "perf", "idle": 60.0})),
                    before := sf_entry.stage_of(e),
                    eff := sf_entry.to_held(e, stamp(T0), T0),
                    (before, sf_entry.stage_of(e), eff),
                )[-1],
                sf_entry.Stage.WAITING,
                sf_entry.Stage.SESSION_ACTIVE,
                sf_entry.SidecarEffect(
                    unlink_yield=True,
                    unlink_idle=True,
                    write_session=sf_entry.initial_session_uses(T0),
                ),
            ),
            (
                "held -> yielded unlinks .yield and .idle",
                lambda: (
                    (e := self._entry_in_stage(sf_entry.Stage.HELD)),
                    before := sf_entry.stage_of(e),
                    eff := sf_entry.to_yielded(e, T0 + 10, T0 - 1),
                    (before, sf_entry.stage_of(e), eff),
                )[-1],
                sf_entry.Stage.HELD,
                sf_entry.Stage.YIELDED,
                sf_entry.SidecarEffect(unlink_yield=True, unlink_idle=True),
            ),
            (
                "held -> lock-yielded unlinks .yield and .idle",
                lambda: (
                    (e := self._entry_in_stage(sf_entry.Stage.HELD)),
                    before := sf_entry.stage_of(e),
                    eff := sf_entry.to_lock_waiting(e, T0 + 10, "/tmp/x.lock", owner),
                    (before, sf_entry.stage_of(e), eff),
                )[-1],
                sf_entry.Stage.HELD,
                sf_entry.Stage.LOCK_YIELDED,
                sf_entry.SidecarEffect(unlink_yield=True, unlink_idle=True),
            ),
            (
                "lock-yielded -> yielded via lock_taken",
                lambda: (
                    (e := self._entry_in_stage(sf_entry.Stage.LOCK_YIELDED)),
                    before := sf_entry.stage_of(e),
                    eff := sf_entry.lock_taken(e),
                    (before, sf_entry.stage_of(e), eff),
                )[-1],
                sf_entry.Stage.LOCK_YIELDED,
                sf_entry.Stage.YIELDED,
                sf_entry.NO_EFFECT,
            ),
            (
                "session yielded -> session-active preserves existing .session via init_session",
                lambda: (
                    (e := self._entry_in_stage(sf_entry.Stage.SESSION_ACTIVE)),
                    sf_entry.to_lock_waiting(e, T0 + 10, "/tmp/x.lock", owner),
                    sf_entry.lock_taken(e),
                    before := sf_entry.stage_of(e),
                    eff := sf_entry.to_held(e, stamp(T0 + 25), T0 + 25),
                    (before, sf_entry.stage_of(e), eff),
                )[-1],
                sf_entry.Stage.YIELDED,
                sf_entry.Stage.SESSION_ACTIVE,
                sf_entry.SidecarEffect(
                    unlink_yield=True,
                    unlink_idle=True,
                    init_session=sf_entry.initial_session_uses(T0 + 25),
                ),
            ),
            (
                "lock-noted -> session-stopping via lock_taken when stopping",
                lambda: (
                    (e := self._entry_in_stage(sf_entry.Stage.SESSION_STOPPING)),
                    sf_entry.to_lock_noted(e, "/tmp/x.lock", owner),
                    before := sf_entry.stage_of(e),
                    eff := sf_entry.lock_taken(e),
                    (before, sf_entry.stage_of(e), eff),
                )[-1],
                sf_entry.Stage.LOCK_NOTED,
                sf_entry.Stage.SESSION_STOPPING,
                sf_entry.NO_EFFECT,
            ),
            (
                "session-stopping -> session-ended unlinks .session",
                lambda: (
                    (e := self._entry_in_stage(sf_entry.Stage.SESSION_STOPPING)),
                    before := sf_entry.stage_of(e),
                    step := sf_entry.session_step(
                        e,
                        {"active": {}, "runs": 2, "busy": 3.4, "last_use": T0 + 5},
                        moment=T0 + 10,
                        began=T0,
                        idle=60.0,
                        stop_asked=True,
                    ),
                    (before, sf_entry.stage_of(e), step.effect),
                )[-1],
                sf_entry.Stage.SESSION_STOPPING,
                sf_entry.Stage.SESSION_ENDED,
                sf_entry.SidecarEffect(unlink_session=True),
            ),
            (
                "held -> released terminal cleanup",
                lambda: (
                    (e := self._entry_in_stage(sf_entry.Stage.HELD)),
                    before := sf_entry.stage_of(e),
                    eff := sf_entry.to_terminal(e, sf_entry.Stage.RELEASED),
                    (before, sf_entry.Stage.RELEASED, eff),
                )[-1],
                sf_entry.Stage.HELD,
                sf_entry.Stage.RELEASED,
                sf_entry.TERMINAL_EFFECT,
            ),
        ]
        for label, action, want_before, want_after, want_effect in cases:
            with self.subTest(case=label):
                before, after, effect = action()
                self.assertEqual(want_before, before)
                self.assertEqual(want_after, after)
                self.assertTrue(sf_entry.can_transition(before, after))
                self.assertEqual(want_effect, effect)

    def test_illegal_transitions_are_rejected_without_mutating_the_entry(self) -> None:
        owner = (4242, "Wed Nov 11 00:00:00 2026")
        illegal_cases: list[tuple[str, sf_entry.Stage, Callable[[dict[str, Any]], object]]] = [
            ("unknown -> to_held", sf_entry.Stage.UNKNOWN, lambda e: sf_entry.to_held(e, stamp(T0), T0)),
            ("unknown -> to_yielded", sf_entry.Stage.UNKNOWN, lambda e: sf_entry.to_yielded(e, T0, None)),
            ("unknown -> lock_taken", sf_entry.Stage.UNKNOWN, lambda e: sf_entry.lock_taken(e)),
            ("unknown -> reprioritize", sf_entry.Stage.UNKNOWN, lambda e: sf_entry.to_reprioritized(e, "batch", T0)),
            ("unknown -> terminal", sf_entry.Stage.UNKNOWN, lambda e: sf_entry.to_terminal(e, sf_entry.Stage.REAPED)),
            ("held -> to_held", sf_entry.Stage.HELD, lambda e: sf_entry.to_held(e, stamp(T0 + 5), T0 + 5)),
            ("lock-yielded -> to_held without lock_taken", sf_entry.Stage.LOCK_YIELDED,
             lambda e: sf_entry.to_held(e, stamp(T0 + 5), T0 + 5)),
            ("unsettled -> to_held", sf_entry.Stage.UNSETTLED,
             lambda e: sf_entry.to_held(e, stamp(T0 + 50), T0 + 50)),
            ("waiting -> to_yielded", sf_entry.Stage.WAITING, lambda e: sf_entry.to_yielded(e, T0, None)),
            ("yielded -> to_yielded", sf_entry.Stage.YIELDED, lambda e: sf_entry.to_yielded(e, T0 + 20, None)),
            ("session-active -> polite to_yielded", sf_entry.Stage.SESSION_ACTIVE,
             lambda e: sf_entry.to_yielded(e, T0 + 10, None)),
            ("waiting -> to_lock_waiting", sf_entry.Stage.WAITING,
             lambda e: sf_entry.to_lock_waiting(e, T0, "/tmp/x.lock", owner)),
            ("yielded -> to_lock_waiting", sf_entry.Stage.YIELDED,
             lambda e: sf_entry.to_lock_waiting(e, T0 + 20, "/tmp/x.lock", owner)),
            ("waiting -> to_lock_noted", sf_entry.Stage.WAITING,
             lambda e: sf_entry.to_lock_noted(e, "/tmp/x.lock", owner)),
            ("waiting -> lock_taken", sf_entry.Stage.WAITING, lambda e: sf_entry.lock_taken(e)),
            ("held -> lock_taken", sf_entry.Stage.HELD, lambda e: sf_entry.lock_taken(e)),
            ("yielded -> lock_taken", sf_entry.Stage.YIELDED, lambda e: sf_entry.lock_taken(e)),
            ("waiting -> settle", sf_entry.Stage.WAITING, lambda e: sf_entry.settle(e)),
            ("yielded -> settle", sf_entry.Stage.YIELDED, lambda e: sf_entry.settle(e)),
            ("lock-yielded -> settle", sf_entry.Stage.LOCK_YIELDED, lambda e: sf_entry.settle(e)),
            ("waiting -> session_step", sf_entry.Stage.WAITING,
             lambda e: sf_entry.session_step(e, None, moment=T0, began=T0, idle=60.0)),
            ("held non-session -> session_step", sf_entry.Stage.HELD,
             lambda e: sf_entry.session_step(e, None, moment=T0, began=T0, idle=60.0)),
            ("session-ended -> session_step", sf_entry.Stage.SESSION_ENDED,
             lambda e: sf_entry.session_step(e, None, moment=T0 + 20, began=T0, idle=60.0)),
            ("session-stopping -> session_start_run", sf_entry.Stage.SESSION_STOPPING,
             lambda e: sf_entry.session_start_run(
                 e, {"active": {}}, pid=88, start="s", started=1, label="r", cmd="true", since=stamp(T0)
             )),
            ("session-ended -> session_finish_run", sf_entry.Stage.SESSION_ENDED,
             lambda e: sf_entry.session_finish_run(e, {"active": {}}, pid=88, ran_seconds=1.0, moment=T0 + 20)),
            ("session-ended -> to_reprioritized", sf_entry.Stage.SESSION_ENDED,
             lambda e: sf_entry.to_reprioritized(e, "interactive", T0 + 20)),
            ("waiting -> released", sf_entry.Stage.WAITING,
             lambda e: sf_entry.decide_outcome(e, held_seen=True)),
            ("held -> refused", sf_entry.Stage.HELD,
             lambda e: sf_entry.decide_outcome(e, refused="memory")),
            ("session-ended -> left", sf_entry.Stage.SESSION_ENDED,
             lambda e: sf_entry.to_terminal(e, sf_entry.Stage.LEFT)),
        ]
        for label, stage, call in illegal_cases:
            with self.subTest(case=label):
                entry = self._entry_in_stage(stage)
                snapshot = json.dumps(entry, sort_keys=True)
                with self.assertRaises(sf_entry.IllegalTransitionError):
                    call(entry)
                self.assertEqual(snapshot, json.dumps(entry, sort_keys=True))

    def test_lock_wait_keeps_place_and_ageing_and_settle_reconciles_older_client_grants(self) -> None:
        owner = (4242, "Wed Nov 11 00:00:00 2026")
        entry = waiter(
            label="b",
            since="2026-11-11T00:00:00Z",
            state="held",
            priority="batch",
            granted="2026-11-11T02:00:00Z",
            rank=1762819000.0,
        )
        sf_entry.to_lock_waiting(entry, 1762830000.0, "/tmp/x.lock", owner)
        self.assertEqual(sf_entry.Stage.LOCK_YIELDED, sf_entry.stage_of(entry))
        self.assertEqual((1762819000.0, sf_entry.epoch("2026-11-11T00:00:00Z")),
                         (entry["rank"], sf_entry.ageing_since(entry)))
        self.assertEqual(owner, sf_entry.lock_owner(entry))

        # Older client grants directly on disk without clearing lock/yield fields -> UNSETTLED
        older = dict(entry, state="held", granted="2026-11-11T02:01:40Z")
        self.assertEqual(sf_entry.Stage.UNSETTLED, sf_entry.stage_of(older))
        # Re-yielding an unsettled older-client grant settles the prior yield first without losing first_granted
        sf_entry.to_yielded(older, 1762830200.0, 1762819500.0)
        self.assertEqual("2026-11-11T02:00:00Z", older["first_granted"])
        self.assertGreater(sf_entry.yielded_of(older), 0.0)
        self.assertEqual((None, None, None),
                         (sf_entry.lock_of(older), sf_entry.lock_owner(older), older.get("lock_yield")))

        # Normal current-client path: lock_taken -> yielded -> to_held clears lock_yield and records yielded time
        sf_entry.lock_taken(entry)
        self.assertEqual(sf_entry.Stage.YIELDED, sf_entry.stage_of(entry))
        sf_entry.to_held(entry, "2026-11-11T02:01:40Z", 1762830100.0)
        self.assertEqual(sf_entry.Stage.HELD, sf_entry.stage_of(entry))
        self.assertEqual("2026-11-11T02:00:00Z", entry["granted"])
        self.assertEqual(100.0, entry["yielded"])
        self.assertEqual((None, None, None),
                         (sf_entry.lock_of(entry), sf_entry.lock_owner(entry), entry.get("lock_yield")))

    def test_lock_only_yield_records_was_held_and_yielded_duration_without_polite_yields(self) -> None:
        root = _Path(tempfile.mkdtemp(prefix="slotfather-lock-acct-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        queue = Queue(root)
        owner = (4242, "Wed Nov 11 00:00:00 2026")
        with queue.running():
            # 1) Reaped while lock-yielded (yields unset)
            entry = waiter(since=stamp(T0 - 120), state="held", granted=stamp(T0 - 60))
            sf_entry.to_lock_waiting(entry, T0 - 25, "/tmp/x.lock", owner)
            self.assertNotIn("yields", entry)
            self.assertTrue(sf_entry.ran(entry))
            outcome = sf_entry.decide_outcome(entry, reaped=True)
            self.assertEqual(sf_entry.Reaped(was_held=True), outcome)
            reaped_item = sf_state.record(
                queue.directory, entry, outcome, capacity=lambda: 4, above=lambda _r, _p: False
            )
            self.assertIs(True, reaped_item["was_held"])
            self.assertEqual(25.0, reaped_item["yielded"])
            self.assertNotIn("yields", reaped_item)

            # 2) Released after resuming from a lock-only yield
            resumed = waiter(since=stamp(T0 - 120), state="held", granted=stamp(T0 - 60))
            sf_entry.to_lock_waiting(resumed, T0 - 40, "/tmp/x.lock", owner)
            sf_entry.lock_taken(resumed)
            sf_entry.to_held(resumed, stamp(T0 - 10), T0 - 10)
            released_item = sf_state.record(
                queue.directory,
                resumed,
                sf_entry.decide_outcome(resumed, held_seen=True, status=0),
                capacity=lambda: 4,
                above=lambda _r, _p: False,
            )
            self.assertEqual("released", released_item["outcome"])
            self.assertEqual(30.0, released_item["yielded"])
            self.assertNotIn("yields", released_item)

    def test_state_commit_and_remove_entry_manage_all_sidecars(self) -> None:
        root = _Path(tempfile.mkdtemp(prefix="slotfather-sidecar-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = root / "queue" / "0000000001-7.json"
        path.parent.mkdir(parents=True)
        entry = waiter(session={"name": "perf", "idle": 60.0})
        # Pre-seed stale .yield and .idle sidecars before granting
        sf_state.write(sf_state.marker(path), {"by": "old"})
        sf_state.write(sf_state.idle_path(path), {"since": T0})
        effect = sf_entry.to_held(entry, stamp(T0), T0)
        sf_state.commit(path, entry, effect)
        self.assertFalse(sf_state.marker(path).exists())
        self.assertFalse(sf_state.idle_path(path).exists())
        self.assertTrue(sf_state.session_path(path).exists())

        # Re-create .yield and .idle alongside .session and verify remove_entry cleans all four files
        sf_state.write(sf_state.marker(path), {"by": "later"})
        sf_state.write(sf_state.idle_path(path), {"since": T0 + 1})
        sf_state.remove_entry(path)
        self.assertEqual([], list(path.parent.iterdir()))


class InstallerCopyTests(unittest.TestCase):
    """install.py migrates queues other versions wrote, so it stays self-contained: its copies of the ticket rule
    and of the states it accepts must agree with the package's."""

    def test_the_installer_normalises_tickets_as_the_package_does(self) -> None:
        for value in (None, 42, "42", "#42", " #7 ", "#x", "None", "", "  ", "a" * 200, "a" * 201, "t\x1b",
                      True, 1.5, ["1"]):
            with self.subTest(value=value):
                try:
                    expected: object = sf_entry.checked_ticket(value)
                except sf_config.SlotError:
                    expected = ValueError
                try:
                    found: object = install.history_ticket(value)
                except ValueError:
                    found = ValueError
                self.assertEqual(expected, found)

    def test_the_installer_reads_identities_as_the_package_does(self) -> None:
        # install.py's identity rule (require_idle, queue_updates) against entry.valid_identity() and unknown(): an
        # entry the package can identify the installer accepts; one it cannot (no pid, no start, a bad pid) both
        # refuse; one of an unknown state whose holder is gone the installer reads as absent, as procs does.
        directory = _Path(tempfile.mkdtemp(prefix="slotfather-entry-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        (directory / "queue").mkdir()
        path = directory / "queue" / "0000000001-7.json"
        cases: list[tuple[dict[str, Any], str]] = [
            (waiter(), "accepted"), (waiter(state="held", priority="normal"), "accepted"),
            (waiter(state="paused"), "absent"), ({k: v for k, v in waiter().items() if k != "state"}, "absent"),
            ({k: v for k, v in waiter().items() if k != "pid"}, "refused"),
            ({k: v for k, v in waiter().items() if k != "start"}, "refused"),
            (waiter(pid="7"), "refused"), (waiter(pid=0), "refused")]
        # require_idle() checks an old queue's idleness only: a pid 0 names no process there, so it is no holder
        idle_only = {id(cases[-1][0])}
        for entry, expected in cases:
            with self.subTest(entry=entry):
                path.write_text(json.dumps(entry))
                if expected == "accepted":
                    self.assertTrue(sf_entry.valid_identity(entry))
                elif expected == "absent":
                    self.assertIsNotNone(sf_entry.unknown(entry))
                    self.assertTrue(sf_entry.checkable(entry), "its holder can be checked: here it is gone")
                else:
                    self.assertFalse(sf_entry.valid_identity(entry) or sf_entry.checkable(entry))
                for check in (install.require_idle, install.queue_updates):
                    err = io.StringIO()
                    try:
                        with contextlib.redirect_stderr(err):
                            check(directory, {})
                        found = "absent" if str(path) in err.getvalue() else "accepted"
                    except install.InstallError:
                        found = "refused"
                    want = "accepted" if check is install.require_idle and id(entry) in idle_only else expected
                    self.assertEqual(want, found, check.__name__)

AGES_OFF = {"aging_batch": 0.0, "aging_normal": 0.0}  # a grant's class is its own: no `aged` to work out


class SequenceTests(unittest.TestCase):
    """Random sequences of grants, yields (and the resumes that follow), priority changes and releases on a live
    queue, mirrored with the transitions the replay applies: replay.grant() and entry.to_yielded() for the same
    decisions. The entries must be equal, field for field and in order, but for `reserved`, which only the live
    queue's plan keeps."""

    def live(self, queue: Queue) -> dict[str, dict[str, Any]]:
        """The entries this client knows; an unknown one (fathers#19) is checked on its own."""
        found = {path.name: json.loads(path.read_text()) for path in sorted((queue.directory / "queue").glob("*.json"))}
        return {name: entry for name, entry in found.items() if sf_entry.unknown(entry) is None}

    def granted(self, queue: Queue, mirror: dict[str, dict[str, Any]], done: list[str]) -> None:
        for name, entry in self.live(queue).items():
            if sf_entry.is_held(entry) and not sf_entry.is_held(mirror[name]):
                if "yielded_at" in mirror[name]:
                    done.append("resume")
                replay.grant(mirror[name], queue.clock)

    def same(self, queue: Queue, mirror: dict[str, dict[str, Any]], step: str) -> None:
        live = self.live(queue)
        self.assertEqual(sorted(mirror), sorted(live), step)
        for name, entry in live.items():
            self.assertEqual([(k, v) for k, v in entry.items() if k != "reserved"],
                             [(k, v) for k, v in mirror[name].items() if k != "reserved"], f"{step}: {name}")

    def play(self, seed: int) -> list[str]:
        rng = random.Random(seed)
        root = _Path(tempfile.mkdtemp(prefix="slotfather-entry-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        queue = Queue(root)
        mirror: dict[str, dict[str, Any]] = {}
        done: list[str] = []
        pids = iter(range(91000, 92000))
        with queue.running(), mock.patch.object(sf_client, "held_again", unknown):
            sf_settings.configure({"order.json": dict(sf_config.ORDER_DEFAULTS, **AGES_OFF)}, directory=queue.directory)
            # Half the sequences carry a newer client's entry (a state this one does not know), alive or not: it
            # must stay as it is through every step (fathers#19).
            strange = queue.entry(next(pids), "strange", rng.randint(1, 4), "paused", since=-60,
                                  alive=rng.random() < 0.5) if seed % 2 else None
            kept = strange.read_bytes() if strange else b""
            for step in range(30):
                queue.clock += rng.choice([1.0, 5.25, 30.5, 120.0, 600.125])
                live = self.live(queue)
                held = [n for n, e in live.items() if sf_entry.is_held(e)]
                waiting = [n for n in live if n not in held]
                op = rng.choice(["arrive", "arrive", "grant", "yield", "yield", "reprioritize", "release"])
                if op == "arrive" or not live:
                    pid = next(pids)
                    path = queue.entry(pid, f"r{pid}", rng.randint(1, 4), since=queue.clock - T0,
                                       priority=rng.choice(sf_config.PRIORITIES), yieldable=rng.random() < 0.6)
                    mirror[path.name] = json.loads(path.read_text())
                    done.append("arrive")
                elif op == "grant":
                    sf_leases.grant(queue.directory, sf_queue.process_table(queue.directory))
                    self.granted(queue, mirror, done)
                    done.append("grant")
                elif op == "yield" and held and waiting:
                    name, asker = rng.choice(held), rng.choice(waiting)
                    path = queue.directory / "queue" / name
                    settings = sf_settings.order_settings(queue.directory, environment=False)
                    rank = sf_schedule.rank_time(live[asker], settings, queue.clock) + sf_config.YIELD_BEHIND
                    sf_state.write(sf_state.marker(path), {"by": asker[:-5], "label": live[asker]["label"],
                                                          "priority": "interactive", "count": 1, "at": "now"})
                    if sf_client.yield_now(path, out=io.StringIO()):
                        sf_entry.to_yielded(mirror[name], queue.clock, rank)
                        self.granted(queue, mirror, done)
                        done.append("yield")
                elif op == "reprioritize":
                    name = rng.choice(list(live))
                    priority = rng.choice(sf_config.PRIORITIES)
                    sf_leases.reprioritize(str(live[name]["pid"]), priority, queue.directory)
                    if sf_entry.priority_of(mirror[name]) != priority:
                        sf_entry.to_reprioritized(mirror[name], priority, queue.clock)
                    self.granted(queue, mirror, done)
                    done.append("reprioritize")
                elif op == "release":
                    name = rng.choice(list(live))
                    lease = sf_client.Lease(int(live[name]["count"]), queue.directory / "queue" / name)
                    lease.status = 0
                    lease.release()
                    line = json.loads((queue.directory / "history.jsonl").read_text().splitlines()[-1])
                    self.assertEqual(sf_entry.outcome(mirror.pop(name)), line["outcome"], f"step {step}")
                    self.granted(queue, mirror, done)
                    done.append("release")
                self.same(queue, mirror, f"seed {seed} step {step} {op}")
                if strange:
                    self.assertEqual(kept, strange.read_bytes(), f"seed {seed} step {step} {op}: left alone")
        return done

    def test_live_transitions_leave_what_the_replay_applies(self) -> None:
        done: list[str] = []
        for seed in range(40):
            done += self.play(seed)
        for op in ("arrive", "grant", "yield", "resume", "reprioritize", "release"):
            self.assertIn(op, done, "every transition is reached")
        self.assertGreaterEqual(done.count("yield"), 5)


if __name__ == "__main__":
    unittest.main()
