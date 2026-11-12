#!/usr/bin/env python3
"""Default labels (fathers#100) over command shapes taken from the Mac's history.jsonl: the project, the part of the
command that names the work and the lane metadata; generic labels get the derived parts; good labels stay."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import labels  # noqa: E402

import json
import os
import subprocess
import unittest
from typing import Any

from test_slotfather import TOOL, SlotCase

HOME = "/Users/someone"
REPOS = {f"{HOME}/straylight": "straylight", f"{HOME}/data-grid": "data-grid", f"{HOME}/fathers": "fathers",
         f"{HOME}/sid-kit": "sid-kit", f"{HOME}/mastack": "mastack"}
SCRATCH = "/private/tmp/claude-501/-Users-someone-straylight/b42a/scratchpad"


def look(path: str) -> str | None:
    """A fake repository lookup as labels.repository() answers: the repository the path lies in (a worktree under
    .claude/worktrees names its main one) and the top-level directory below the worktree's top."""
    for root, name in REPOS.items():
        if path == root or path.startswith(root + "/"):
            below = path[len(root) + 1:].split("/") if path != root else []
            if below[:2] == [".claude", "worktrees"]:
                below = below[3:]
            return f"{name}/{below[0]}" if below else name
    return None


# (cwd, argv, meta, ticket, expected label), each shape seen in history.jsonl (paths shortened).
TABLE: list[tuple[str, list[str], dict[str, str], str | None, str]] = [
    (f"{HOME}/straylight", ["node", f"{HOME}/.local/state/grid-header-inner-v2/inspect.mjs"], {}, None,
     "straylight · inspect"),
    (f"{HOME}/straylight/desktop", ["node", "--test", "--test-name-pattern=Failed output keeps",
                                    "tests/connection-routing.test.mjs"], {}, None,
     "straylight/desktop · connection-routing.test"),
    (f"{HOME}/data-grid", ["./node_modules/.bin/vitest", "--config", "vitest.config.ts", "run", "src/quick-find"],
     {}, None, "data-grid · vitest run"),
    (f"{HOME}/data-grid", ["bun", "run", "typecheck"], {}, None, "data-grid · bun run typecheck"),
    (f"{HOME}/fathers/slotfather", ["python3", "-m", "unittest", "discover", "-s", "tests"], {}, None,
     "fathers/slotfather · unittest discover"),
    (f"{HOME}/data-grid/.claude/worktrees/lane-doc-xy-default", ["node", f"{SCRATCH}/probe/probe.mjs", "/tmp/out"],
     {"lane": "doc-xy-default", "phase": "probe"}, None, "data-grid · lane doc-xy-default · probe"),
    (f"{HOME}/data-grid", [f"{HOME}/.nvm/versions/node/v24/bin/node",
                           f"{HOME}/data-grid/node_modules/@someone/repo-kit/src/cli.mjs", "pre-push", "origin"],
     {}, None, "data-grid · repo-kit pre-push"),
    (f"{HOME}/straylight", ["python3", "-"], {}, None, "straylight · python3 -"),
    (f"{HOME}/fathers/slotfather", ["sh", "-c", "set -e && uv run --no-project --with-requirements "
                                    "requirements-dev.txt python tools/check.py && cd tests && python3 -m unittest"],
     {}, None, "fathers/slotfather · check"),
    (f"{HOME}/straylight/.claude/worktrees/agent-a0f9", [f"{HOME}/.local/bin/python3",
     f"{HOME}/straylight/.claude/worktrees/agent-a0f9/tools/heavy.py", "repeat", "1", "--", "env",
     f"STRAYLIGHT_DOMAIN_DIR={HOME}/x", "python3", "tools/test-schema-8-conversion.py"],
     {"lane": "624-flows-are-graphs-review-fixes", "kind": "repro"}, None,
     "straylight · lane 624-flows-are-graphs-review-fixes · test-schema-8-conversion"),
    (f"{HOME}/straylight/.claude/worktrees/lane-voice-04", [f"{HOME}/.local/bin/python3", "tools/heavy.py", "repeat",
     "1", "--", "tools/swift-test.sh", "voice-speech"], {"lane": "voice-04-speech-runtime", "kind": "check"}, None,
     "straylight · lane voice-04-speech-runtime · swift-test.sh voice-speech"),
    (f"{HOME}/straylight", ["env", "TAG=reference-red", "node", f"{HOME}/.local/state/grid-fade/pixels.mjs"], {},
     None, "straylight · pixels"),
    (f"{HOME}/data-grid", ["bash", "-c", f"cd {HOME}/.codex/lab && env -u SID_KIT_SOURCE bun install --frozen-lockfile"],
     {}, None, "data-grid · bun install"),
    (f"{HOME}/straylight/core", ["cargo", "test", "--no-run", "--target-dir", f"{SCRATCH}/target"], {}, None,
     "straylight/core · cargo test"),
    (f"{HOME}/sid-kit", ["bunx", "vitest", "--config", "vitest.config.ts", "run", "src/__tests__/button.test.tsx"],
     {}, None, "sid-kit · vitest run"),
    (f"{HOME}/sid-kit/.claude/worktrees/lane-sk-token-scales", [f"{HOME}/.nvm/versions/node/v24/bin/node",
     f"{HOME}/sid-kit/.claude/worktrees/lane-sk-token-scales/node_modules/@someone/repo-kit/src/lanefather.mjs",
     "--checks", "verify:lint,verify:build", "--keep-going"], {"lane": "sk-token-scales", "phase": "check"}, None,
     "sid-kit · lane sk-token-scales · lanefather · check"),
    (f"{HOME}/fathers", ["slotfather", "run", "-n", "1", "--", "python3", "-m", "unittest", "discover"], {}, None,
     "fathers · unittest discover"),
    (SCRATCH, ["python3", f"{SCRATCH}/after/godfather/tools/measure_cpu.py", "--seconds", "300"], {}, None,
     "godfather · measure_cpu"),
    (f"{HOME}/mastack", ["bin/check"], {}, None, "mastack · bin/check"),
    (f"{HOME}/straylight", ["./verify.sh", "--checks", "a,b"], {}, "743", "straylight · #743 · verify.sh"),
    (f"{HOME}/straylight/desktop", ["node", "--test"], {"lane": "743", "phase": "check"}, None,
     "straylight/desktop · lane 743 · node --test · check"),
    (SCRATCH, ["node", "marquee-frames.mjs"], {}, None, "marquee-frames"),
    (f"{HOME}/straylight", ["bash", "-lc", "cd desktop && npm test"], {}, None, "straylight · npm test"),
    (f"{HOME}/straylight", ["sh", "-euc", "if [ -d x ]; then\nmake -C core test\nfi"], {}, None,
     "straylight · make test"),
    (f"{HOME}/straylight", ["node", "-e", "require('x')\nconsole.log(1)"], {}, None, "straylight · node -e"),
    (f"{HOME}/straylight", ["gtimeout", "300", "make", "test"], {}, None, "straylight · make test"),
    (f"{HOME}/straylight", ["caffeinate", "-t", "600", "python3", "m.py"], {}, None, "straylight · m"),
    (SCRATCH, ["/opt/homebrew/bin/cargo", "test"], {}, None, "cargo test"),
    (f"{HOME}/straylight", [".venv/bin/pytest", "-q"], {}, None, "straylight · pytest"),
    (f"{HOME}/straylight", ["xcodebuild", "-scheme", "App", "test"], {}, None, "straylight · xcodebuild test"),
]


class DeriveTests(unittest.TestCase):
    def test_real_command_shapes(self):
        for cwd, argv, meta, ticket, expected in TABLE:
            with self.subTest(argv=argv):
                self.assertEqual(expected, labels.derive(cwd, argv, meta, ticket, look=look))

    def test_a_good_label_stays_and_a_generic_one_gets_the_derived_parts(self):
        argv = ["python3", f"{SCRATCH}/after/godfather/tools/measure_cpu.py"]
        good = labels.choose("lane-743-desktop-flakes", f"{HOME}/straylight", ["make"], look=look)
        self.assertEqual(("lane-743-desktop-flakes", None), (good["label"], good["hint"]))
        same_as_worktree = labels.choose("lane-doc-xy", f"{HOME}/data-grid/.claude/worktrees/lane-doc-xy",
                                         ["bun", "run", "verify"], look=look)
        self.assertEqual("lane-doc-xy", same_as_worktree["label"], "a lane worktree's own name is a good label")
        scratch = labels.choose("scratchpad", SCRATCH, argv, look=look)
        self.assertEqual("godfather · measure_cpu · scratchpad", scratch["label"])
        from slotfather import schedule
        self.assertEqual("godfather", schedule.family(scratch["label"]), "the family is the project, not the word")
        self.assertIn("says little", scratch["hint"] or "")
        project = labels.choose("mastack", f"{HOME}/mastack", ["bin/check"], look=look)
        self.assertEqual(("mastack · bin/check", "mastack", "mastack · bin/check"),
                         (project["label"], project["given"], project["derived"]))
        self.assertNotIn("\n", labels.derive(f"{HOME}/x", ["sh", "-c", "a\nb"], look=look) or "")
        nothing = labels.choose(None, "/", [], look=look)
        self.assertEqual("request", nothing["label"])

    def test_the_family_is_the_project(self):
        from slotfather import schedule
        self.assertEqual("straylight", schedule.family("straylight · lane 743 · check"))
        self.assertEqual("lane", schedule.family("lane-636-measure"))


class DefaultLabelTests(SlotCase):
    def history(self) -> list[Any]:
        return [json.loads(x) for x in (self.state / "history.jsonl").read_text().splitlines()]

    def test_run_without_a_label_names_the_project_and_the_script(self):
        repo = self.tmp / "work" / "mastack"
        (repo / "bin").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        script = repo / "bin" / "check"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        self.env["SLOTFATHER_TEMP_ROOTS"] = str(self.tmp / "nothing-here")
        result = subprocess.run([TOOL, "run", "--", "bin/check"], cwd=repo, env=self.env, capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        [line] = self.history()
        self.assertEqual(("mastack · bin/check", "mastack · bin/check"), (line["label"], line["label_derived"]))
        self.assertNotIn("label_given", line)

        generic = subprocess.run([TOOL, "run", "--label", "tmp", "--", "bin/check"], cwd=repo, env=self.env,
                                 capture_output=True, text=True, timeout=20)
        self.assertIn("hint: the label 'tmp' says little", generic.stderr)
        last = self.history()[-1]
        self.assertEqual(("mastack · bin/check · tmp", "tmp"), (last["label"], last["label_given"]))

        self.assertFalse(labels.temporary(f"{HOME}/app/tmp"), "a repository's own tmp/ is no scratch directory")
        good = subprocess.run([TOOL, "run", "--label", "lane-100-labels", "--", "bin/check"], cwd=repo, env=self.env,
                              capture_output=True, text=True, timeout=20)
        self.assertNotIn("hint", good.stderr)
        self.assertEqual("lane-100-labels", self.history()[-1]["label"])
        by = json.loads(self.tool("stats", "--json", "--by", "derived").stdout)
        self.assertEqual({"mastack · bin/check": 3}, {g["derived"]: g["requests"] for g in by["groups"]})

    def test_a_temporary_directory_is_no_project(self):
        self.assertTrue(labels.temporary(SCRATCH))
        self.assertTrue(labels.temporary(os.path.realpath(self.tmp)))
        self.assertFalse(labels.temporary(f"{HOME}/straylight"))


if __name__ == "__main__":
    unittest.main()
