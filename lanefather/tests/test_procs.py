"""Process-source behavior through slotfather's real versioned command seam."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
import unittest.mock

from support import LaneCase

from lanefather import config, procs, slots
from lanefather.procs import SlotfatherSource

SCRIPT = '''#!/usr/bin/env python3
import json,os,pathlib,sys
root = pathlib.Path(os.environ["PROCESS_FIXTURE"])
data = json.loads((root / "answer.json").read_text())
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as f: f.write(json.dumps(args)+"\\n")
if args[0] == "procs":
    rows = data["initial"]
    answer = {k: data.get(k, []) for k in ("cwd_unknown", "lease_unknown")}
    if "unknown" in data: answer["unknown"] = data["unknown"]
    if "--cwd" in args:
        cwd = args[args.index("--cwd") + 1]
        rows = [p for p in rows if p.get("cwd") == cwd or str(p.get("cwd", "")).startswith(cwd + "/")]
        # "rounds": what successive --cwd queries report unknown, the last one repeating (fathers#158).
        counter = root / "cwd-queries"
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        if data.get("rounds"):
            answer = data["rounds"][min(count, len(data["rounds"]) - 1)]
            answer = dict(answer, **{k: answer.get(k, []) for k in ("cwd_unknown", "lease_unknown")})
    elif "--pid" in args:
        pid = int(args[args.index("--pid") + 1])
        rows = [p for p in rows if p.get("pid") == pid]
        answer = {"cwd_unknown": [], "lease_unknown": []}
    print(json.dumps({"v": data.get("v",1), **answer, "processes": rows}))
else:
    if "--live" in args: raise SystemExit("fixture forbids PID-only live stopping")
    print(json.dumps({"v": data.get("sweep_v", data.get("v", 1)), "items": [dict(p, action="would-stop")
                                       for p in data.get("offers", [])]}))
'''


class ProcessSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        program = self.root / "slotfather"
        program.write_text(SCRIPT)
        program.chmod(0o755)
        env = unittest.mock.patch.dict(os.environ, {"PROCESS_FIXTURE": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.source = SlotfatherSource(str(program))
        self.source.pause = 0
        self.process: dict[str, Any] = {"pid": 1234, "start": "one", "cmd": "runner", "cwd": "/work/lane"}

    def answer(self, **extra: Any) -> None:
        data = {"initial": [self.process], "offers": [{"pid": 1234, "cmd": "runner", "tree": [1234]}]}
        (self.root / "answer.json").write_text(json.dumps({**data, **extra}))

    def test_pid_only_sweep_is_never_used_to_stop_an_earlier_observation(self) -> None:
        self.answer()
        stopping, held = self.source.sort_for_removal("/work/lane")
        self.assertEqual(held, [])
        self.assertEqual([p.pid for p in stopping], [1234])
        left = self.source.stop(stopping, "/work/lane")
        self.assertEqual([p.pid for p in left], [1234])
        self.assertIn("identity-bound", left[0].describe())
        calls = [json.loads(s) for s in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertFalse(any("--live" in call for call in calls))

    def test_unknown_schema_unknown_membership_and_spared_processes_hold_the_worktree(self) -> None:
        values_list: list[dict[str, Any]] = [{"v": 99}, {"v": True}, {"v": "2"}, {"cwd_unknown": [333]},
                                            {"lease_unknown": [1234]}, {"offers": []}]
        for values in values_list:
            with self.subTest(values=values):
                self.answer(**values)
                stopping, held = self.source.sort_for_removal("/work/lane")
                self.assertEqual(stopping, [])
                self.assertTrue(held)

    def test_committed_slotfather_v2_queries_are_recognized_without_enabling_pid_only_stops(self) -> None:
        # Slotfather 08ede50: procs.query + sweep report are v2; process identity fields are unchanged.
        self.answer(v=2, initial=[{**self.process, "leases": [{"pid": 1234, "start": "one",
            "priority": "interactive", "ticket": "648", "meta": {"lane": "candidate"}}]}])
        stopping, held = self.source.sort_for_removal("/work/lane")
        self.assertEqual(held, [])
        self.assertEqual([item.pid for item in stopping], [1234])
        retained = self.source.stop(stopping, "/work/lane")
        self.assertEqual([item.pid for item in retained], [1234])
        self.assertIn("identity-bound", retained[0].describe())
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertFalse(any("--live" in call for call in calls))

    def test_an_older_slotfather_without_procs_keeps_legacy_done_behavior(self) -> None:
        program = self.root / "old-slotfather"
        program.write_text("#!/bin/sh\nprintf 'unknown procs\\n' >&2\nexit 2\n")
        program.chmod(0o755)
        with unittest.mock.patch.object(slots, "command", return_value=str(program)):
            source = procs.source(config.Config())
        self.assertIsInstance(source, procs.NoSource)
        self.assertEqual(source.sort_for_removal("/work/lane"), ([], []))

    def test_inspection_failure_is_not_mistaken_for_an_older_command_surface(self) -> None:
        program = self.root / "broken-slotfather"
        program.write_text("#!/bin/sh\nprintf 'process table unavailable\\n' >&2\nexit 2\n")
        program.chmod(0o755)
        with unittest.mock.patch.object(slots, "command", return_value=str(program)):
            source = procs.source(config.Config())
        self.assertIsInstance(source, SlotfatherSource)
        _, held = source.sort_for_removal("/work/lane")
        self.assertTrue(held)

    def test_a_command_line_reference_retains_a_worktree_even_with_a_different_cwd(self) -> None:
        self.answer(initial=[dict(self.process, cmd="cargo --manifest-path=/work/lane/Cargo.toml", cwd="/elsewhere")],
                    offers=[])
        stopping, held = self.source.sort_for_removal("/work/lane")
        self.assertEqual(stopping, [])
        self.assertTrue(held)

    def cwd_queries(self) -> int:
        return int((self.root / "cwd-queries").read_text())

    def test_unknowns_that_come_and_go_are_asked_again_and_an_idle_worktree_is_free(self) -> None:
        # fathers#158: three queries for one idle worktree reported [16067], then other helpers, then nothing.
        self.answer(initial=[dict(self.process, cwd="/elsewhere")], offers=[],
                    rounds=[{"cwd_unknown": [16067]}, {"cwd_unknown": [15891, 16016], "lease_unknown": [16110]}, {}])
        self.assertEqual(self.source.sort_for_removal("/work/lane"), ([], []))
        self.assertEqual(self.cwd_queries(), 3)

    def test_a_process_still_unknown_after_the_retries_holds_and_is_named(self) -> None:
        # The newer Slotfather names each unknown pid with its command (`unknown`).
        self.answer(rounds=[{"cwd_unknown": [4242], "unknown": [
            {"pid": 4242, "start": "s", "cmd": "node server.js", "cwd_unknown": True, "lease_unknown": False}]}])
        stopping, held = self.source.sort_for_removal("/work/lane")
        self.assertEqual(stopping, [])
        self.assertEqual(len(held), 1)
        self.assertIn("unknown after 5 queries", held[0])
        self.assertIn("pid 4242 (node server.js) cwd unknown", held[0])
        self.assertEqual(self.cwd_queries(), 5)

    def test_an_older_slotfather_s_unknown_pid_is_named_through_procs_pid(self) -> None:
        self.answer(initial=[self.process, {"pid": 777, "start": "t", "cmd": "cargo build", "cwd": "/x"}],
                    rounds=[{"lease_unknown": [1234], "cwd_unknown": [777, 999]}])
        _, held = self.source.sort_for_removal("/work/lane")
        self.assertIn("pid 777 (cargo build) cwd unknown", held[0])
        self.assertIn("pid 999 (exited) cwd unknown", held[0])
        self.assertIn("pid 1234 (runner) lease unknown", held[0])


class DoneWhileHelpersComeAndGoTests(LaneCase):
    """fathers#158: done on an idle landed lane goes through while other short-lived processes come and go."""

    def test_done_on_an_idle_landed_lane_succeeds_while_unknowns_come_and_go(self) -> None:
        worktree = self.created("a", "lib/a.txt")
        self.commit(worktree, {"lib/a.txt": "a\n"})
        self.lane("check", "a")
        self.lane("land", "a", "--keep")
        program = self.tmp / "procs-slotfather"
        program.write_text(SCRIPT)
        program.chmod(0o755)
        (self.tmp / "answer.json").write_text(json.dumps({
            "initial": [], "offers": [], "rounds": [{"cwd_unknown": [16067]}, {"lease_unknown": [15891]}, {}]}))
        self.env["PROCESS_FIXTURE"] = str(self.tmp)
        source = SlotfatherSource(str(program))
        source.pause = 0
        def given(project: Any = None) -> SlotfatherSource:
            del project
            return source

        with unittest.mock.patch.object(procs, "source", given):
            output = self.lane("done", "a", process=False)
        self.assertIn("Lane a done", output)
        self.assertFalse(Path(worktree).exists())

