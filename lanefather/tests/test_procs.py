"""Process-source behavior through slotfather's real versioned command seam."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
import unittest.mock

from support import ROOT  # noqa: F401  # pyright: ignore[reportUnusedImport]

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
    if "--cwd" in args:
        cwd = args[args.index("--cwd") + 1]
        rows = [p for p in rows if p.get("cwd") == cwd or str(p.get("cwd", "")).startswith(cwd + "/")]
    print(json.dumps({"v": data.get("v",1), "cwd_unknown": data.get("cwd_unknown",[]),
                      "lease_unknown": data.get("lease_unknown",[]), "processes": rows}))
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
