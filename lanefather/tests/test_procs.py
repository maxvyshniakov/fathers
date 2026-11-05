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

from lanefather.procs import Item, SlotfatherSource

SCRIPT = '''#!/usr/bin/env python3
import json,os,pathlib,sys
root = pathlib.Path(os.environ["PROCESS_FIXTURE"])
data = json.loads((root / "answer.json").read_text())
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as f: f.write(json.dumps(args)+"\\n")
if args[0] == "procs":
    phase = "after" if (root / "stopped").exists() else "initial"
    rows = data.get("current", data[phase]) if "--pid" in args else data[phase]
    if "--cwd" in args:
        cwd = args[args.index("--cwd") + 1]
        rows = [p for p in rows if p.get("cwd") == cwd or str(p.get("cwd", "")).startswith(cwd + "/")]
    print(json.dumps({"v": data.get("v",1), "cwd_unknown": data.get("cwd_unknown",[]),
                      "lease_unknown": data.get("lease_unknown",[]), "processes": rows}))
else:
    if "--live" in args: (root / "stopped").touch()
    print(json.dumps({"v": 1, "items": [dict(p, action="stopped" if "--live" in args else "would-stop")
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
        data = {"initial": [self.process], "after": [], "offers": [{"pid": 1234, "cmd": "runner", "tree": [1234]}]}
        (self.root / "answer.json").write_text(json.dumps({**data, **extra}))

    def test_an_offered_process_stops_only_through_sweep_and_the_worktree_is_queried_again(self) -> None:
        self.answer()
        stopping, held = self.source.sort_for_removal("/work/lane")
        self.assertEqual(held, [])
        self.assertEqual([p.pid for p in stopping], [1234])
        self.assertEqual(self.source.stop(stopping, "/work/lane"), [])
        calls = [json.loads(s) for s in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertIn(["procs", "--json", "--pid", "1234"], calls)
        self.assertIn(["sweep", "--json", "--live", "--pid", "1234"], calls)
        self.assertEqual(calls[-2:], [["procs", "--json", "--cwd", "/work/lane"], ["procs", "--json"]])

    def test_unknown_schema_unknown_membership_and_spared_processes_hold_the_worktree(self) -> None:
        values_list: list[dict[str, Any]] = [{"v": 2}, {"cwd_unknown": [333]},
                                            {"lease_unknown": [1234]}, {"offers": []}]
        for values in values_list:
            with self.subTest(values=values):
                self.answer(**values)
                stopping, held = self.source.sort_for_removal("/work/lane")
                self.assertEqual(stopping, [])
                self.assertTrue(held)

    def test_reused_pid_is_not_given_to_live_sweep(self) -> None:
        self.answer(current=[dict(self.process, start="two")])
        process = Item(1234, "one", "runner")
        self.assertEqual(self.source.stop([process], "/work/lane"), [process])
        self.assertFalse((self.root / "stopped").exists())

    def test_a_command_line_reference_retains_a_worktree_even_with_a_different_cwd(self) -> None:
        self.answer(initial=[dict(self.process, cmd="cargo --manifest-path=/work/lane/Cargo.toml", cwd="/elsewhere")],
                    offers=[])
        stopping, held = self.source.sort_for_removal("/work/lane")
        self.assertEqual(stopping, [])
        self.assertTrue(held)
