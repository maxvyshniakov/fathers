"""Garbage collection through its real CLI: conservative retention in scratch Git repositories."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from support import LaneCase


class GcTests(LaneCase):
    def candidate(self, name: str = "old", *, age: bool = True) -> Path:
        path = self.repo / ".claude/worktrees" / f"agent-{name}"
        self.git(self.repo, "worktree", "add", "-q", "-b", f"worktree-agent-{name}", str(path), "integrate/q")
        if age:
            self.age(path)
        return path

    def age(self, path: Path) -> None:
        past = time.time() - 3 * 3600
        gitdir = Path(self.git(path, "rev-parse", "--absolute-git-dir"))
        for item in (path, gitdir / "HEAD", gitdir / "index", gitdir / "logs/HEAD"):
            os.utime(item, (past, past))

    def collect(self, *args: str) -> dict[str, Any]:
        return json.loads(self.lane("gc", "--json", *args))

    def test_gc_removes_only_merged_idle_worktrees_and_their_unused_branches(self):
        old = self.candidate()
        preview = self.collect("--dry-run")
        self.assertTrue(old.exists())
        self.assertTrue(any(a["kind"] == "worktree" and a["path"] == str(old.resolve()) for a in preview["removed"]))
        report = self.collect()
        self.assertFalse(old.exists())
        self.assertNotIn("worktree-agent-old", self.git(self.repo, "branch", "--list"))
        self.assertTrue(self.integration.exists())
        self.assertTrue(any(a["kind"] == "branch" for a in report["removed"]))

    def test_gc_keeps_dirty_young_unmerged_retained_and_owned_worktrees(self):
        dirty, young, unmerged, retained, owned = (self.candidate(n, age=n != "young")
                                                   for n in ("dirty", "young", "unmerged", "retained", "owned"))
        self.write(dirty, "lib/api.txt", "unsaved")
        self.age(dirty)
        self.commit(unmerged, {"lib/api.txt": "unmerged"})
        self.age(unmerged)
        self.write(retained, ".keep", "baseline")
        self.age(retained)
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "held.json").write_text(json.dumps({"id": "held", "status": "landed",
                                                         "worktree": str(owned.resolve()),
                                                         "branch": "worktree-agent-owned"}))
        report = self.collect()
        for path in (dirty, young, unmerged, retained, owned):
            self.assertTrue(path.exists(), str(path))
        reasons = "\n".join(a["reason"] for a in report["kept"])
        for text in ("uncommitted", "active", "not merged", ".keep", "record"):
            self.assertIn(text, reasons)

    def test_gc_keeps_everything_when_an_ownership_record_cannot_be_read(self):
        old = self.candidate()
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "broken.json").write_text("{ interrupted write")
        output = self.lane("gc", "--json", ok=False)
        self.assertIn("lane state", output)
        self.assertTrue(old.exists())

    def test_gc_refuses_when_process_source_is_missing(self):
        old = self.candidate()
        (self.integration / "tools/process_scan.py").unlink()
        report = self.collect()
        self.assertTrue(old.exists())
        self.assertTrue(any("process source" in a["reason"] for a in report["kept"]))

    def test_gc_keeps_a_process_naming_a_worktree_outside_its_working_directory(self):
        old = self.candidate()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(old.resolve())], cwd=self.repo)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        report = self.collect()
        self.assertTrue(old.exists())
        self.assertTrue(any(str(child.pid) in a["reason"] for a in report["kept"]))

    def test_gc_runs_project_cleanup_only_after_removal_and_reports_failures(self):
        old = self.candidate()
        marker = self.tmp / "hook.log"
        hook = self.write(self.repo, "cleanup.py", "import pathlib,sys\n"
                          f"pathlib.Path({str(marker)!r}).write_text(sys.argv[1])\n"
                          "raise SystemExit(3)\n")
        cfg = self.integration / ".lanefather.toml"
        cfg.write_text(cfg.read_text() + f'\n[gc]\nafter_remove = ["python3", "{hook}", "{{worktree}}"]\n')
        self.collect("--dry-run")
        self.assertFalse(marker.exists())
        report = self.collect()
        self.assertFalse(old.exists())
        self.assertEqual(marker.read_text(), str(old.resolve()))
        self.assertTrue(any("exit 3" in e for e in report["errors"]))
