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

    def session(self, name: str, branch: str | None, *, work: bool = True, publish: bool = True,
                age: bool = True) -> Path:
        """A Claude Code or Codex session worktree under .claude/worktrees: with `work`, a commit of its own; with
        `publish`, its HEAD reaches origin/main, the default branch's upstream."""
        path = self.repo / ".claude/worktrees" / name
        if branch:
            self.git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), "integrate/q")
        else:
            self.git(self.repo, "worktree", "add", "-q", "--detach", str(path), "integrate/q")
        if work:
            self.commit(path, {f"docs/{name}.txt": "session work\n"}, "session work")
        if publish:
            self.publish(path)
        if age:
            self.age(path)
        return path

    def publish(self, path: Path) -> None:
        """Make origin/main contain the worktree's HEAD: a commit whose parents are the old origin/main and it."""
        head = self.git(path, "rev-parse", "HEAD")
        parents = ["-p", head]
        old = subprocess.run(["git", "rev-parse", "--verify", "-q", "refs/remotes/origin/main"], cwd=self.repo,
                             capture_output=True, text=True).stdout.strip()
        if old:
            parents = ["-p", old, *parents]
        merged = self.git(self.repo, "commit-tree", f"{head}^{{tree}}", *parents, "-m", "published")
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", merged)

    def branches(self) -> str:
        return self.git(self.repo, "branch", "--list")

    def family(self, kind: str, name: str, *, age: bool = True) -> Path:
        """A collectable worktree of one family: an agent worktree, or a published session worktree."""
        if kind == "agent":
            return self.candidate(name, age=age)
        return self.session(f"{name}-session", f"claude/{name}", age=age)


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

    def test_gc_keeps_dirty_young_unmerged_retained_owned_and_used_worktrees_of_every_family(self):
        for kind in ("agent", "session"):
            with self.subTest(kind):
                dirty, young, unmerged, retained, owned, used = (
                    self.family(kind, f"{kind}-{n}", age=n != "young")
                    for n in ("dirty", "young", "unmerged", "retained", "owned", "used"))
                self.write(dirty, "lib/api.txt", "unsaved")
                self.age(dirty)
                self.commit(unmerged, {"lib/api.txt": "unmerged"})
                self.age(unmerged)
                self.write(retained, ".keep", "baseline")
                self.age(retained)
                self.state.mkdir(parents=True, exist_ok=True)
                branch = self.git(owned, "branch", "--show-current")
                (self.state / f"held-{kind}.json").write_text(json.dumps({
                    "id": f"held-{kind}", "status": "landed", "worktree": str(owned.resolve()), "branch": branch}))
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=used)
                self.addCleanup(child.wait)
                self.addCleanup(child.kill)
                report = self.collect()
                reasons = {a["path"]: a["reason"] for a in report["kept"]}
                for path, text in ((dirty, "uncommitted"), (young, "active"), (unmerged, "not merged"),
                                   (retained, ".keep"), (owned, "record"), (used, str(child.pid))):
                    self.assertTrue(path.exists(), str(path))
                    self.assertIn(text, reasons[str(path.resolve())])
                    self.assertIn(self.git(path, "branch", "--show-current"), self.branches())

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
    """Claude Code and Codex session worktrees are collected, more conservatively than agent worktrees: a session
    may still be open, and its ignored files may be the owner's (fathers#44 review)."""

    def kept(self, report: dict[str, Any], path: Path) -> str:
        return next(a["reason"] for a in report["kept"] if a["path"] == str(path.resolve()))

    def configure(self, text: str) -> None:
        cfg = self.integration / ".lanefather.toml"
        cfg.write_text(cfg.read_text() + "\n[gc]\n" + text)

    def test_published_clean_idle_claude_and_codex_worktrees_are_removed_with_their_branches(self):
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

    def test_a_session_without_a_commit_of_its_own_is_kept_unless_the_project_opts_in(self):
        fresh = self.session("eager-lovelace-4d5e6f", "claude/eager-lovelace-4d5e6f", work=False)
        detached = self.session("smoke-0", None, work=False)
        agent = self.candidate()
        report = self.collect()
        self.assertFalse(agent.exists(), "an agent worktree without commits is collected as before")
        for path in (fresh, detached):
            self.assertTrue(path.exists())
            self.assertIn("no commit of its own", self.kept(report, path))
        self.assertIn("claude/eager-lovelace-4d5e6f", self.branches())
        self.configure("collect_uncommitted_sessions = true\n")
        self.collect()
        self.assertFalse(fresh.exists())
        self.assertFalse(detached.exists())

    def test_a_session_holding_ignored_files_is_kept_unless_they_are_disposable(self):
        notes = self.session("brave-turing-1a2b3c", "claude/brave-turing-1a2b3c", age=False)
        self.write(notes, ".claude/settings.local.json", "{}\n")
        self.age(notes)
        builds = self.session("codex-builds", "codex/builds", age=False)
        self.write(builds, "node_modules/pkg/index.js", "x\n")
        self.write(builds, "lib/__pycache__/api.pyc", "x\n")
        self.age(builds)
        agent = self.candidate("ignored", age=False)
        self.write(agent, ".claude/settings.local.json", "{}\n")
        self.age(agent)
        report = self.collect()
        self.assertTrue(notes.exists())
        self.assertEqual((notes / ".claude/settings.local.json").read_text(), "{}\n")
        self.assertIn("ignored files: .claude/", self.kept(report, notes))
        self.assertFalse(builds.exists(), "build output under [gc] disposable does not hold a session")
        self.assertFalse(agent.exists(), "an agent worktree's ignored files are collected as before")
        self.configure('disposable = ["node_modules"]\n')
        again = self.session("codex-pycache", "codex/pycache", age=False)
        self.write(again, "lib/__pycache__/api.pyc", "x\n")
        self.age(again)
        self.assertIn("ignored files: lib/__pycache__/", self.kept(self.collect(), again))

    def test_a_session_merged_only_into_a_local_integration_branch_is_kept(self):
        local = self.session("codex-local-only", "codex/local-only", publish=False)
        agent = self.candidate("local", age=False)
        self.commit(agent, {"docs/agent.txt": "agent work\n"})
        for worktree in (local, agent):
            branch = self.git(worktree, "branch", "--show-current")
            self.git(self.integration, "merge", "-q", "--no-edit", branch)
        self.age(agent)
        report = self.collect()
        self.assertTrue(local.exists())
        self.assertIn("not merged into origin", self.kept(report, local))
        self.assertIn("codex/local-only", self.branches())
        self.assertFalse(agent.exists(), "an agent worktree merged into integrate/* is collected as before")
        self.git(self.repo, "worktree", "remove", str(local))
        self.collect()
        self.assertIn("codex/local-only", self.branches(), "an unpublished session branch is not deleted")
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", "codex/local-only")
        self.collect()
        self.assertNotIn("codex/local-only", self.branches())

    def test_a_worktree_holding_another_repository_is_kept(self):
        for path in (self.candidate("vendored"), self.session("codex-vendored", "codex/vendored")):
            nested = path / "node_modules/vendored"
            nested.mkdir(parents=True)
            self.git(nested, "init", "-q")
            self.write(nested, "local.txt", "another project's work\n")
            self.age(path)
        report = self.collect()
        for path in (self.repo / ".claude/worktrees/agent-vendored", self.repo / ".claude/worktrees/codex-vendored"):
            self.assertTrue((path / "node_modules/vendored/local.txt").exists())
            self.assertIn("holds a Git repository at node_modules/vendored", self.kept(report, path))

    def test_a_lock_naming_a_live_claude_agent_keeps_its_worktree(self):
        live, dead = self.candidate("live-lock"), self.candidate("dead-lock")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.tmp)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.git(self.repo, "worktree", "lock", "--reason", f"claude agent live (pid {child.pid})", str(live))
        self.git(self.repo, "worktree", "lock", "--reason", f"claude agent dead (pid {gone.pid})", str(dead))
        report = self.collect()
        self.assertTrue(live.exists())
        self.assertIn(f"locked by a live claude agent (pid {child.pid})", self.kept(report, live))
        self.assertFalse(dead.exists())

    def test_a_branch_outside_the_patterns_outlives_its_removed_worktree(self):
        feature = self.session("feature-work", "feature/work")
        self.collect()
        self.assertFalse(feature.exists())
        self.assertIn("feature/work", self.branches(), "only a branch the patterns name is deleted")

    def test_the_patterns_are_project_config(self):
        claude = self.session("affectionate-hypatia-145d8b", "claude/affectionate-hypatia-145d8b")
        old = self.candidate()
        self.configure('dirs = ["agent-*"]\nbranches = ["worktree-agent-*"]\n')
        report = self.collect()
        self.assertFalse(old.exists())
        self.assertTrue(claude.exists())
        self.assertIn("claude/affectionate-hypatia-145d8b", self.branches())
        self.assertIn("[gc] dirs", self.kept(report, claude))

    def test_a_worktree_holding_another_worktree_is_kept(self):
        outer = self.session("codex-outer", "codex/outer")
        inner = outer / ".claude/worktrees/codex-inner"
        self.git(self.repo, "worktree", "add", "-q", "-b", "codex/inner", str(inner), "integrate/q")
        self.write(inner, "lib/api.txt", "unsaved inside")
        self.age(inner)
        self.age(outer)
        report = self.collect()
        self.assertTrue(inner.exists())
        self.assertEqual((inner / "lib/api.txt").read_text(), "unsaved inside")
        self.assertTrue(outer.exists())
        self.assertIn("holds the worktree", self.kept(report, outer))
