"""Git worktrees as resources (fathers#112), against temporary repositories with an origin: owning one, the guards
that keep one (dirty, unique commits, ignored files, a nested repository, a process inside, a lock), removal at
the owner's end, and the sweep's pass over unowned worktrees with its dry run and `git worktree prune`."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Dict, List
import unittest

from support import Case

import eventcheck
from junkfather import resources, sweep, worktrees

DAY = 86400.0
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


@unittest.skipUnless(shutil.which("git"), "no git here")
class WorktreeCase(Case):
    def setUp(self) -> None:
        super().setUp()
        for name, value in GIT_ENV.items():
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)
        self.env.update(GIT_ENV)
        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "repo"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.tmp, "init", "-q", "-b", "main", str(self.repo))
        (self.repo / "README").write_text("hello\n")
        (self.repo / ".gitignore").write_text("node_modules/\n.env\n")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-q", "-m", "first")
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.repo = self.repo.resolve()

    def git(self, where: Path, *args: str) -> str:
        done = subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, timeout=60,
                              env={**os.environ, **GIT_ENV})
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout

    def worktree(self, name: str, branch: str) -> Path:
        path = self.tmp / name
        self.git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
        return path.resolve()

    def commit(self, where: Path, name: str) -> str:
        (where / name).write_text(name)
        self.git(where, "add", name)
        self.git(where, "commit", "-q", "-m", name)
        return self.git(where, "rev-parse", "HEAD").strip()

    def listed(self) -> List[str]:
        return [i["path"] for i in worktrees.listing(str(self.repo)) or []]

    def branches(self) -> List[str]:
        return self.git(self.repo, "branch", "--format=%(refname:short)").split()

    def refusal(self, path: Path, **options: Any) -> Any:
        items = worktrees.listing(str(self.repo)) or []
        item = next(i for i in items if i["path"] == str(path))
        from junkfather import procs
        return worktrees.refusal(item, str(self.repo), procs.Table.read(), procs.working_dirs_or_none(os.getuid()),
                                 items, **options)

    def events(self) -> List[Dict[str, Any]]:
        path = self.state / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def own(self, path: Path, env: "Dict[str, str] | None" = None) -> Dict[str, Any]:
        result = self.cli("own-resource", "--kind", "worktree", "--id", str(path), "--ttl", "1h", env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        (entry,) = [e for e in self.store.entries() if e.get("state") == "running"]
        return dict(entry)

    def make_idle(self, path: Path) -> None:
        old = time.time() - 2 * DAY
        gitdir = self.git(path, "rev-parse", "--absolute-git-dir").strip()
        for candidate in [str(path), *(os.path.join(gitdir, n) for n in ("HEAD", "index", "logs/HEAD"))]:
            if os.path.exists(candidate):
                os.utime(candidate, (old, old))


class GuardTests(WorktreeCase):
    def test_a_clean_merged_worktree_may_go(self):
        self.assertIsNone(self.refusal(self.worktree("wt", "feat")))

    def test_what_keeps_a_worktree(self):
        dirty = self.worktree("dirty", "dirty")
        (dirty / "notes.txt").write_text("mine")
        unique = self.worktree("unique", "unique")
        self.commit(unique, "work.txt")
        env_file = self.worktree("ignored", "ignored")
        (env_file / ".env").write_text("SECRET=1")
        locked = self.worktree("locked", "locked")
        self.git(self.repo, "worktree", "lock", "--reason", "mine", str(locked))
        on_main = self.tmp / "detached-main"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(on_main), "main")
        self.assertEqual("uncommitted or untracked changes", self.refusal(dirty))
        self.assertEqual("commits not in origin/main", self.refusal(unique))
        self.assertEqual("ignored files: .env (not disposable build output)", self.refusal(env_file))
        self.assertEqual("locked: mine", self.refusal(locked))
        self.assertIsNone(self.refusal(on_main.resolve()))
        self.assertEqual("the main worktree", self.refusal(self.repo))

    def test_disposable_build_output_does_not_keep_it_but_a_nested_ignored_repository_does(self):
        path = self.worktree("built", "built")
        (path / "node_modules" / "left-pad").mkdir(parents=True)
        (path / "node_modules" / "left-pad" / "index.js").write_text("x")
        self.assertIsNone(self.refusal(path))
        (path / "node_modules" / "dep" / ".git").mkdir(parents=True)
        self.assertEqual("it holds a Git repository at node_modules/dep", self.refusal(path))

    def test_work_merged_by_content_lets_it_go_and_takes_its_branch(self):
        path = self.worktree("picked", "picked")
        sha = self.commit(path, "fix.txt")
        self.git(self.repo, "cherry-pick", sha)
        self.assertEqual("commits not in origin/main", self.refusal(path))
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.assertIsNone(self.refusal(path))
        items = worktrees.listing(str(self.repo)) or []
        self.assertEqual("picked", worktrees.branch_to_delete(str(self.repo), "picked", items, str(path)))
        self.assertIsNone(worktrees.branch_to_delete(str(self.repo), "main", items, str(path)))

    def test_a_process_inside_keeps_it_and_so_does_the_sweep_being_inside(self):
        path = self.worktree("busy", "busy")
        self.spawn("sleep", "600", cwd=str(path))
        self.wait_for(lambda: (self.refusal(path) or "").startswith("in use by pid"), what="the process inside")
        quiet = self.worktree("quiet", "quiet")
        here = os.getcwd()
        os.chdir(quiet)
        self.addCleanup(os.chdir, here)
        self.assertEqual("junkfather runs inside it", self.refusal(quiet))

    def test_the_sweep_requires_a_day_of_quiet(self):
        path = self.worktree("fresh", "fresh")
        self.assertRegex(self.refusal(path, idle=DAY), r"^active 0\.0 h ago \(under 24 h\)$")
        self.make_idle(path)
        self.assertIsNone(self.refusal(path, idle=DAY))

    def test_the_command_that_adds_a_worktree_names_its_path(self):
        kind = resources.KINDS["worktree"]
        self.assertEqual([str(self.tmp / "a")], kind.named(["git", "-C", str(self.tmp), "worktree", "add", "-b", "x",
                                                            "--lock", "a", "main"], ""))
        self.assertEqual([], kind.named(["git", "worktree", "list"], ""))
        self.assertEqual([], kind.named(["git", "status"], ""))


class OwnedTests(WorktreeCase):
    def test_own_resource_then_reap_removes_it_and_its_merged_branch(self):
        path = self.worktree("wt", "feat")
        entry = self.own(path)
        self.assertEqual({"kind": "worktree", "id": str(path), "repo": str(self.repo), "branch": "feat"},
                         entry.get("resource"))
        result = self.cli("reap", "--id", entry["id"])
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertNotIn(str(path), self.listed())
        self.assertFalse(path.exists())
        self.assertNotIn("feat", self.branches())
        stopped = [e for e in self.events() if e["kind"] == "resource.stopped"]
        self.assertEqual(1, len(stopped))
        for line in (self.state / "events.jsonl").read_text().splitlines():
            self.assertEqual([], eventcheck.validate_line(line, strict=True), line)

    def test_a_refused_stop_keeps_the_entry_and_says_why(self):
        path = self.worktree("wt", "feat")
        entry = self.own(path)
        self.commit(path, "mine.txt")
        result = self.cli("reap", "--id", entry["id"])
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(path.is_dir())
        self.assertEqual("running", self.state_of(entry["id"]))
        (failed,) = [e for e in self.events() if e["kind"] == "resource.stop_failed"]
        self.assertEqual("kept: commits not in origin/main", failed["details"]["error"])
        self.assertEqual([], eventcheck.validate(failed, strict=True))

    def test_own_kind_worktree_registers_what_git_worktree_add_made(self):
        path = self.tmp / "made"
        result = self.cli("own", "--kind", "worktree", "--ttl", "1h", "--", "git", "-C", str(self.repo), "worktree",
                          "add", "-q", "-b", "made", str(path), "main")
        self.assertEqual(0, result.returncode, result.stderr)
        (entry,) = self.store.entries()
        self.assertEqual(str(path.resolve()), (entry.get("resource") or {}).get("id"))
        self.assertTrue([e for e in self.events() if e["kind"] == "resource.owned"])

    def test_own_resource_refuses_the_main_worktree_and_a_plain_folder(self):
        for given, why in ((self.repo, "is a repository's main worktree"), (self.tmp, "is not a git worktree")):
            result = self.cli("own-resource", "--kind", "worktree", "--id", str(given))
            self.assertEqual(1, result.returncode)
            self.assertIn(why, result.stderr)

    def test_the_session_end_removes_a_clean_owned_worktree_and_keeps_a_dirty_one(self):
        claude = self.fake_claude("s1")
        env = self.claude_env("s1", claude)
        clean = self.worktree("clean", "clean")
        dirty = self.worktree("dirty", "dirty")
        self.own(clean, env=env)
        entries = {(e.get("resource") or {}).get("id"): e["id"] for e in self.store.entries()}
        result = self.cli("own-resource", "--kind", "worktree", "--id", str(dirty), "--ttl", "1h", env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        (dirty / "notes.txt").write_text("unsaved")
        self.assertEqual(0, self.hook("SessionEnd", "s1", env=env).returncode)
        self.wait_for(lambda: not clean.exists(), what="the clean worktree to go")
        dirty_id = next(e["id"] for e in self.store.entries() if (e.get("resource") or {}).get("id") == str(dirty))
        self.wait_for(lambda: any(e["kind"] == "resource.stop_failed" for e in self.events()), what="the refusal")
        self.assertTrue(dirty.is_dir())
        self.assertEqual("running", self.state_of(dirty_id))
        self.assertEqual("reaped", self.state_of(entries[str(clean)]))


class SweepTests(WorktreeCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["JUNKFATHER_REPOS"] = str(self.repo)

    def run_sweep(self, dry_run: bool = False) -> Dict[str, Any]:
        from junkfather import procs
        table, cwds = procs.Table.read(), procs.working_dirs_or_none(os.getuid())
        return sweep.sweep(self.store, dry_run=dry_run, table=table, cwds=cwds, sessions=[])

    def test_the_dry_run_prints_the_plan_and_changes_nothing(self):
        gone = self.worktree("gone", "gone")
        self.make_idle(gone)
        missing = self.worktree("missing", "missing")
        shutil.rmtree(missing)
        report = self.run_sweep(dry_run=True)
        self.assertEqual([str(gone)], [i["id"] for i in report["worktrees"]["removed"]])
        self.assertEqual(1, len(report["worktrees"]["pruned"]))
        text = sweep.text(report)
        self.assertIn(f"would remove worktree {gone} and its branch gone", text)
        self.assertIn("would prune", text)
        self.assertIn(str(gone), self.listed())
        self.assertIn("missing", self.git(self.repo, "worktree", "list"))
        self.assertIn("gone", self.branches())

    def test_the_sweep_removes_merged_idle_ones_reports_the_rest_and_prunes(self):
        gone = self.worktree("gone", "gone")
        self.make_idle(gone)
        fresh = self.worktree("fresh", "fresh")
        work = self.worktree("work", "work")
        self.commit(work, "mine.txt")
        self.make_idle(work)
        missing = self.worktree("missing", "missing")
        shutil.rmtree(missing)
        owned = self.worktree("owned", "owned")
        self.make_idle(owned)
        self.own(owned)
        lane = self.worktree("lane", "lane/x")
        self.make_idle(lane)
        records = self.tmp / "no-lanefather" / "fathers" / "lanes"
        records.mkdir(parents=True)
        (records / "x.json").write_text(json.dumps({"id": "x", "worktree": str(lane), "status": "active"}))
        report = self.run_sweep()
        self.assertEqual([str(gone)], [i["id"] for i in report["worktrees"]["removed"]])
        self.assertFalse(gone.exists())
        self.assertNotIn("gone", self.branches())
        self.assertNotIn("missing", self.git(self.repo, "worktree", "list"))
        kept = {i["id"]: i["why"] for i in report["unowned_resources"] if i["kind"] == "worktree"}
        self.assertEqual({str(fresh), str(work)}, set(kept))
        self.assertIn("commits not in origin/main", kept[str(work)])
        self.assertRegex(kept[str(fresh)], "active")
        item = next(i for i in report["unowned_resources"] if i["id"] == str(work))
        self.assertEqual(("work", True), (item["branch"], isinstance(item["size"], int)))
        stopped = [e for e in self.events() if e["kind"] == "resource.stopped"]
        self.assertEqual([f"worktree:{gone}"], [e["subject"]["id"] for e in stopped])
        reported = [e for e in self.events()
                    if e["kind"] == "unowned.reported" and e["subject"]["type"] == "resource"]
        self.assertEqual({f"worktree:{fresh}", f"worktree:{work}"}, {e["subject"]["id"] for e in reported})
        for line in (self.state / "events.jsonl").read_text().splitlines():
            self.assertEqual([], eventcheck.validate_line(line, strict=True), line)
        status = json.loads(self.cli("status", "--json", env={**self.env, "JUNKFATHER_REPOS": ""}).stdout)
        self.assertEqual({str(fresh), str(work)},
                         {i["id"] for i in status["unowned_resources"] if i["kind"] == "worktree"})


if __name__ == "__main__":
    unittest.main()
