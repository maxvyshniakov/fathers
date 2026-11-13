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


class GcCase(LaneCase):
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


class GcTests(GcCase):

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

    def test_gc_keeps_the_worktree_it_is_invoked_from(self):
        old = self.candidate()
        self.lane("gc", "--json", cwd=old)
        self.assertTrue(old.exists(), "a collector must not remove its own working directory")

    def test_gc_keeps_an_ancestor_using_the_candidate_even_when_the_child_runs_elsewhere(self):
        old = self.candidate()
        script = ("import os,subprocess,sys; "
                  "raise SystemExit(subprocess.call([sys.executable,'-m','lanefather','gc','--json'], "
                  "cwd=os.environ['GC_RUN_FROM']))")
        env = dict(self.env, GC_RUN_FROM=str(self.integration))
        run = subprocess.run([sys.executable, "-c", script], cwd=old, env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(old.exists(), "the querying tool's ancestor still uses this worktree")

    def mutation_hook(self, old: Path, mutation: str) -> None:
        marker = self.tmp / "stopped"
        text = ("import os,pathlib,subprocess,time\n"
                "class Item:\n    pid=1\n    def describe(self): return 'controlled stop adapter'\n"
                f"ROOT={str(old.resolve())!r}\nMARK=pathlib.Path({str(marker)!r})\n"
                "def sort_for_removal(root):\n    return ([Item()],[]) if root==ROOT and not MARK.exists() else ([],[])\n"
                "def stop(processes,root):\n" + mutation + "\n    MARK.touch()\n    return []\n")
        hook = self.write(self.repo, "gc-mutation.py", text)
        cfg = self.integration / ".lanefather.toml"
        cfg.write_text(cfg.read_text().replace('process_scan = "tools/process_scan.py"',
                                               f'process_scan = "{hook}"'))

    def test_gc_keeps_a_new_manual_lock_created_during_process_cleanup(self):
        old = self.candidate()
        self.git(self.repo, "worktree", "lock", "--reason", "claude agent test (pid 99999)", str(old))
        self.mutation_hook(old, "    subprocess.run(['git','worktree','unlock',root],check=True)\n"
                               "    subprocess.run(['git','worktree','lock','--reason','owner retention',root],check=True)")
        self.collect()
        self.assertTrue(old.exists())
        self.assertIn("owner retention", self.git(self.repo, "worktree", "list", "--porcelain"))

    def test_gc_keeps_a_clean_unmerged_detached_head_created_during_process_cleanup(self):
        old, unmerged = self.candidate(), self.candidate("unmerged-stop")
        sha = self.commit(unmerged, {"lib/new.txt": "unmerged"})
        mutation = (f"    subprocess.run(['git','-C',root,'checkout','--detach','-q',{sha!r}],check=True)\n"
                    "    gitdir=subprocess.check_output(['git','-C',root,'rev-parse','--absolute-git-dir'],text=True).strip()\n"
                    "    past=time.time()-3*3600\n"
                    "    for path in [root,*[os.path.join(gitdir,n) for n in ['HEAD','index','logs/HEAD']]]:\n"
                    "        os.utime(path,(past,past))")
        self.mutation_hook(old, mutation)
        self.collect()
        self.assertTrue(old.exists())
        self.assertEqual(self.git(old, "rev-parse", "HEAD"), sha)

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

    def test_gc_includes_only_successfully_reported_project_cleanup_bytes(self):
        old = self.candidate()
        hook = self.write(self.repo, "cleanup-bytes.py", "import json\n"
                          "print(json.dumps({'v': 1, 'freed': 12345}))\n")
        cfg = self.integration / ".lanefather.toml"
        cfg.write_text(cfg.read_text() + f'\n[gc]\nafter_remove_report = true\nafter_remove = ["python3", "{hook}", "{{worktree}}"]\n')
        preview = self.collect("--dry-run")
        worktree_bytes = next(a["bytes"] for a in preview["removed"] if a["kind"] == "worktree")
        self.assertEqual(preview["freed"], worktree_bytes)
        report = self.collect()
        self.assertFalse(old.exists())
        self.assertEqual(report["freed"], worktree_bytes + 12345)
        self.assertEqual(report["errors"], [])

    def test_gc_never_counts_failed_or_invalid_project_cleanup_reports(self):
        cases = [("failed", {"v": 1, "freed": 12345}, 3),
                 ("negative", {"v": 1, "freed": -1}, 0),
                 ("boolean", {"v": 1, "freed": True}, 0),
                 ("version", {"v": 2, "freed": 12345}, 0),
                 ("malformed", "{ truncated", 0),
                 ("empty", "", 0)]
        for name, payload, status in cases:
            with self.subTest(name=name):
                old = self.candidate(name)
                output = payload if isinstance(payload, str) else json.dumps(payload)
                hook = self.write(self.repo, "cleanup-invalid.py",
                                  f"print({output!r})\nraise SystemExit({status})\n")
                cfg = self.integration / ".lanefather.toml"
                text = cfg.read_text().split("\n[gc]", 1)[0]
                cfg.write_text(text + f'\n[gc]\nafter_remove_report = true\nafter_remove = ["python3", "{hook}", "{{worktree}}"]\n')
                preview = self.collect("--dry-run")
                report = self.collect()
                self.assertFalse(old.exists())
                self.assertEqual(report["freed"], preview["freed"])
                self.assertEqual(len(report["errors"]), 1)
                self.assertIn("exit 3" if status else "invalid cleanup report", report["errors"][0])

    def test_gc_ignores_successful_legacy_hook_stdout_without_report_opt_in(self):
        outputs = ["2026-11-05T23:00:00Z delete Cargo target: cache (0.1 GB)",
                   '{"v":1,"freed":12345}']
        for number, output in enumerate(outputs):
            with self.subTest(output=output):
                old = self.candidate(f"legacy-output-{number}")
                hook = self.write(self.repo, "cleanup-legacy.py", f"print({output!r})\n")
                cfg = self.integration / ".lanefather.toml"
                text = cfg.read_text().split("\n[gc]", 1)[0]
                cfg.write_text(text + f'\n[gc]\nafter_remove = ["python3", "{hook}", "{{worktree}}"]\n')
                preview = self.collect("--dry-run")
                report = self.collect()
                self.assertFalse(old.exists())
                self.assertEqual(report["errors"], [])
                self.assertEqual(report["freed"], preview["freed"],
                                 "legacy stdout is diagnostic, even when it resembles a report")


class SessionWorktreeTests(GcCase):
    """Claude Code and Codex session worktrees are collected under the same rules (fathers#44)."""

    def session(self, name: str, branch: str | None, *, age: bool = True) -> Path:
        path = self.repo / ".claude/worktrees" / name
        if branch:
            self.git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), "integrate/q")
        else:
            self.git(self.repo, "worktree", "add", "-q", "--detach", str(path), "integrate/q")
        if age:
            self.age(path)
        return path

    def branches(self) -> str:
        return self.git(self.repo, "branch", "--list")

    def test_merged_clean_idle_claude_and_codex_worktrees_are_removed_with_their_branches(self):
        claude = self.session("affectionate-hypatia-145d8b", "claude/affectionate-hypatia-145d8b")
        codex = self.session("codex-item-compact-spacing", "codex/item-compact-spacing")
        report = self.collect()
        self.assertFalse(claude.exists())
        self.assertFalse(codex.exists())
        removed = {(a["kind"], a["path"]) for a in report["removed"]}
        self.assertIn(("branch", "claude/affectionate-hypatia-145d8b"), removed)
        self.assertIn(("branch", "codex/item-compact-spacing"), removed)
        self.assertNotIn("claude/", self.branches())
        self.assertNotIn("codex/", self.branches())
        self.assertTrue(self.integration.exists(), "the integration worktree stays")
        self.assertTrue((self.repo / ".git").is_dir(), "the main checkout stays")

    def test_unmerged_dirty_recent_and_used_session_worktrees_are_kept(self):
        unmerged = self.session("brave-turing-1a2b3c", "claude/brave-turing-1a2b3c")
        self.commit(unmerged, {"lib/api.txt": "unmerged"})
        self.age(unmerged)
        dirty = self.session("codex-dirty", "codex/dirty")
        self.write(dirty, "lib/api.txt", "unsaved")
        self.age(dirty)
        recent = self.session("eager-lovelace-4d5e6f", "claude/eager-lovelace-4d5e6f", age=False)
        used = self.session("codex-used", "codex/used")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=used)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        report = self.collect()
        for path in (unmerged, dirty, recent, used):
            self.assertTrue(path.exists(), str(path))
        reasons = {Path(a["path"]).name: a["reason"] for a in report["kept"]}
        self.assertIn("not merged", reasons["brave-turing-1a2b3c"])
        self.assertIn("uncommitted", reasons["codex-dirty"])
        self.assertIn("active", reasons["eager-lovelace-4d5e6f"])
        self.assertIn(str(child.pid), reasons["codex-used"])
        for branch in ("claude/brave-turing-1a2b3c", "codex/dirty", "claude/eager-lovelace-4d5e6f", "codex/used"):
            self.assertIn(branch, self.branches())

    def test_a_branch_outside_the_patterns_outlives_its_removed_worktree(self):
        feature = self.session("feature-work", "feature/work")
        detached = self.session("smoke-0", None)
        self.collect()
        self.assertFalse(feature.exists())
        self.assertFalse(detached.exists())
        self.assertIn("feature/work", self.branches(), "only a branch the patterns name is deleted")

    def test_the_patterns_are_project_config(self):
        claude = self.session("affectionate-hypatia-145d8b", "claude/affectionate-hypatia-145d8b")
        old = self.candidate()
        cfg = self.integration / ".lanefather.toml"
        cfg.write_text(cfg.read_text() + '\n[gc]\ndirs = ["agent-*"]\nbranches = ["worktree-agent-*"]\n')
        report = self.collect()
        self.assertFalse(old.exists())
        self.assertTrue(claude.exists())
        self.assertIn("claude/affectionate-hypatia-145d8b", self.branches())
        reason = next(a["reason"] for a in report["kept"] if a["path"] == str(claude.resolve()))
        self.assertIn("[gc] dirs", reason)

    def test_a_worktree_holding_another_worktree_is_kept(self):
        outer = self.session("codex-outer", "codex/outer")
        inner = outer / ".claude/worktrees/codex-inner"
        self.git(self.repo, "worktree", "add", "-q", "-b", "codex/inner", str(inner), "integrate/q")
        self.write(inner, "lib/api.txt", "unsaved inside")
        self.age(inner)
        # The fixture ignores .claude/, as projects do, so the outer worktree reads clean.
        self.age(outer)
        report = self.collect()
        self.assertTrue(inner.exists())
        self.assertEqual((inner / "lib/api.txt").read_text(), "unsaved inside")
        self.assertTrue(outer.exists())
        reason = next(a["reason"] for a in report["kept"] if a["path"] == str(outer.resolve()))
        self.assertIn("holds the worktree", reason)
