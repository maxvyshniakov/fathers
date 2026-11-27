"""Git worktrees as resources (fathers#112), against temporary repositories with an origin: owning one, the guards
that keep one (dirty, unique commits, ignored files, a nested repository, a process inside, a lock), removal at
the owner's end, and the sweep's pass over unowned worktrees with its dry run and `git worktree prune`."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List
import unittest

from support import ROOT, Case

import eventcheck
from spawnfather import resources, sweep, worktrees

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
        from spawnfather import procs
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
        tip = self.git(self.repo, "rev-parse", "picked").strip()
        self.assertEqual(("picked", tip), worktrees.branch_tip(str(self.repo), "picked", items, str(path)))
        self.assertIsNone(worktrees.branch_tip(str(self.repo), "main", items, str(path)))

    def test_a_process_inside_keeps_it_and_so_does_the_sweep_being_inside(self):
        self.use_real_procs(lsof=True)
        path = self.worktree("busy", "busy")
        self.spawn("sleep", "600", cwd=str(path))
        self.wait_for(lambda: (self.refusal(path) or "").startswith("in use by pid"), what="the process inside")
        quiet = self.worktree("quiet", "quiet")
        here = os.getcwd()
        os.chdir(quiet)
        self.addCleanup(os.chdir, here)
        self.assertEqual("spawnfather runs inside it", self.refusal(quiet))

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


class MoreGuardTests(WorktreeCase):
    def test_a_merge_with_its_own_resolution_is_not_merged_though_git_cherry_finds_every_patch(self):
        side = self.worktree("side", "side")
        self.commit(side, "a.txt")
        path = self.worktree("merging", "merging")
        self.git(path, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        (path / "resolution.txt").write_text("only in the merge")
        self.git(path, "add", "resolution.txt")
        self.git(path, "commit", "-q", "--amend", "--no-edit")
        self.git(self.repo, "cherry-pick", self.git(self.repo, "rev-parse", "side").strip())
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.assertNotIn("+", self.git(path, "cherry", "origin/main", "HEAD"))  # what cherry alone would pass
        self.assertEqual("commits not in origin/main", self.refusal(path))
        items = worktrees.listing(str(self.repo)) or []
        self.assertIsNone(worktrees.branch_tip(str(self.repo), "merging", items, str(path)))

    def test_only_the_checked_tip_of_the_branch_is_deleted(self):
        path = self.worktree("moving", "moving")
        resource = {"kind": "worktree", "id": str(path), "repo": str(self.repo), "branch": "moving"}
        steps = worktrees.plan(resource)
        self.assertEqual("update-ref", steps[1][3])
        moved = self.commit(path, "late.txt")  # another agent's work lands on the branch after the check
        outcome = resources.stop(resource, 1, lambda: resources.Snapshot(None).alive(resource), commands=steps)
        self.assertEqual("stopped", outcome["result"], outcome)
        self.assertIn("warning", outcome)
        self.assertEqual(moved, self.git(self.repo, "rev-parse", "moving").strip())

    def test_a_keep_of_any_kind_a_protected_branch_and_a_symbolic_link_keep_it(self):
        keep_dir = self.worktree("keep-dir", "keep-dir")
        (keep_dir / ".keep").mkdir()
        keep_file = self.worktree("keep-file", "keep-file")
        (keep_file / ".keep").write_text("mine")
        integrate = self.worktree("integration", "integrate/batch")
        self.git(self.repo, "branch", "dev", "main")
        dev = self.tmp / "on-dev"
        self.git(self.repo, "worktree", "add", "-q", str(dev), "dev")
        real = self.worktree("real", "real")
        link = self.tmp / "link"
        os.symlink(real, link)
        self.assertIn(self.refusal(keep_dir), (".keep (unreadable)", ".keep: "))
        self.assertEqual(".keep: mine", self.refusal(keep_file))
        self.assertEqual("it has integrate/batch checked out", self.refusal(integrate))
        self.assertEqual("it has dev checked out", self.refusal(dev.resolve()))
        item = {"path": str(real), "listed": str(link), "branch": "real"}
        self.assertEqual("missing or a symbolic link", worktrees.refusal(item, str(self.repo), None, None, []))

    def test_lanes_come_from_lanefathers_registry_and_an_unreadable_one_protects_claude_worktrees(self):
        root = self.tmp / "lanefather"
        states = self.tmp / "elsewhere" / "lanes"
        states.mkdir(parents=True)
        (states / "a.json").write_text(json.dumps({"worktree": str(self.tmp / "wt-a"), "status": "active"}))
        (states / "b.json").write_text(json.dumps({"worktree": str(self.tmp / "wt-b"), "status": "done"}))
        root.mkdir()
        self.assertEqual(([], False), tuple(worktrees.lane_worktrees(str(root))))
        (root / "projects.json").write_text(json.dumps({"v": 1, "projects": [{"state_dir": str(states)}]}))
        lanes = worktrees.lane_worktrees(str(root))
        self.assertEqual(([os.path.realpath(self.tmp / "wt-a")], True), tuple(lanes))
        blind = worktrees.Lanes([], False)
        self.assertIsNotNone(blind.holds("/r/.claude/worktrees/agent-1"))
        self.assertIsNone(blind.holds("/r-wt-feature"))
        self.assertEqual(([], True), tuple(worktrees.lane_worktrees(str(self.tmp / "absent"))))

    def test_released_lane_is_treated_as_live_until_done(self):
        root = self.tmp / "lanefather-live"
        states = root / "lanes"
        states.mkdir(parents=True)
        for status in ("planned", "active", "landed", "released", "done"):
            (states / f"{status}.json").write_text(json.dumps(
                {"id": f"lane-{status}", "worktree": str(self.tmp / f"wt-{status}"), "status": status}))
        (root / "projects.json").write_text(json.dumps({"v": 1, "projects": [{"state_dir": str(states)}]}))
        lanes = worktrees.lane_worktrees(str(root))
        self.assertTrue(lanes.known)
        expected = {os.path.realpath(self.tmp / f"wt-{s}") for s in ("planned", "active", "landed", "released")}
        self.assertEqual(expected, set(lanes.paths))
        self.assertNotIn(os.path.realpath(self.tmp / "wt-done"), lanes.paths)

    def test_a_process_whose_working_directory_cannot_be_read_counts_as_inside(self):
        from spawnfather import procs
        path = self.worktree("unknown", "unknown")
        child = self.spawn("sleep", "600")
        self.wait_for(lambda: child.pid in procs.Table.read().procs, what="the child")
        here = procs.Table.read()
        cwds = {pid: "/" for pid in here.procs}
        cwds.pop(child.pid)
        items = worktrees.listing(str(self.repo)) or []
        item = next(i for i in items if i["path"] == str(path))
        found = worktrees.refusal(item, str(self.repo), here, cwds, items)
        self.assertIn(f"pid {child.pid} (sleep 600; its working directory is unknown)", found or "")

    def test_a_setuid_binary_with_unknown_cwd_does_not_refuse_removal(self):
        import stat
        from unittest import mock
        from spawnfather import procs
        path = self.worktree("setuid", "setuid")
        me = os.getuid()
        fake = procs.Proc(
            pid=999999, ppid=1, pgid=999999, uid=me, cpu=0.0, tty="?",
            start="Mon Jan  1 00:00:00 2026", cmd="/usr/bin/top -l 1",
        )
        table = procs.Table({fake.pid: fake})
        items = worktrees.listing(str(self.repo)) or []
        item = next(i for i in items if i["path"] == str(path))
        fake_stat = mock.Mock(st_mode=stat.S_IFREG | stat.S_ISUID | 0o555)
        real_stat = os.stat

        def stat_side_effect(target: Any, *args: Any, **kwargs: Any) -> Any:
            return fake_stat if target == "/usr/bin/top" else real_stat(target, *args, **kwargs)

        with mock.patch.object(worktrees.os, "stat", side_effect=stat_side_effect), \
                mock.patch.object(procs, "identities", return_value={fake.pid: fake.start}):
            self.assertEqual(([], []), worktrees.users(str(path), table, {}))
            self.assertIsNone(worktrees.refusal(item, str(self.repo), table, {}, items))

        # Relative argv[0] ("top"), missing system paths, and non-system setuid paths must NOT be exempt.
        for cmd in ("top -l 1", "/usr/bin/no-such-setuid-binary", "/tmp/setuid-helper"):
            with self.subTest(cmd=cmd):
                other = procs.Proc(
                    pid=999998, ppid=1, pgid=999998, uid=me, cpu=0.0, tty="?",
                    start="Mon Jan  1 00:00:00 2026", cmd=cmd,
                )
                other_table = procs.Table({other.pid: other})
                with mock.patch.object(worktrees.os, "stat", side_effect=stat_side_effect), \
                        mock.patch.object(procs, "identities", return_value={other.pid: other.start}):
                    _, unseen = worktrees.users(str(path), other_table, {}) or ([], [])
                    self.assertEqual(1, len(unseen), f"{cmd!r} must still count as unknown cwd")

    def test_origin_dev_and_local_dev_are_recognized_as_merge_bases(self):
        # Push a dev branch to origin; work merged into origin/dev (not origin/main) may go.
        self.git(self.repo, "checkout", "-q", "-b", "dev", "main")
        self.git(self.repo, "push", "-q", "origin", "dev")
        self.git(self.repo, "fetch", "-q", "origin")
        self.git(self.repo, "checkout", "-q", "main")

        merged_dev = self.worktree("merged-dev", "merged-dev")
        sha = self.commit(merged_dev, "on-dev.txt")
        self.git(self.repo, "checkout", "-q", "dev")
        self.git(self.repo, "merge", "-q", "--ff-only", sha)
        # Only on local dev so far: origin exists, so local dev must NOT count as merged yet.
        self.assertEqual("commits not in origin/main or origin/dev", self.refusal(merged_dev))
        self.git(self.repo, "push", "-q", "origin", "dev")
        self.git(self.repo, "fetch", "-q", "origin")
        self.git(self.repo, "checkout", "-q", "main")
        self.assertIsNone(self.refusal(merged_dev))
        items = worktrees.listing(str(self.repo)) or []
        self.assertEqual(("merged-dev", sha),
                         worktrees.branch_tip(str(self.repo), "merged-dev", items, str(merged_dev)))

        # Without an origin remote, local dev in LOCAL_BASES is recognized as a base.
        local_repo = self.tmp / "local-only"
        self.git(self.tmp, "init", "-q", "-b", "dev", str(local_repo))
        (local_repo / "README").write_text("hi\n")
        self.git(local_repo, "add", ".")
        self.git(local_repo, "commit", "-q", "-m", "init")
        local_repo = local_repo.resolve()
        wt = (self.tmp / "local-wt").resolve()
        self.git(local_repo, "worktree", "add", "-q", "-b", "feat", str(wt), "dev")
        self.commit(wt, "feat.txt")
        self.assertEqual(["refs/heads/dev"], worktrees.default_bases(str(local_repo)))
        local_items = worktrees.listing(str(local_repo)) or []
        wt_item = next(i for i in local_items if i["path"] == str(wt))
        from spawnfather import procs
        self.assertEqual("commits not in dev", worktrees.refusal(
            wt_item, str(local_repo), procs.Table.read(), procs.working_dirs_or_none(os.getuid()), local_items))
        self.git(local_repo, "merge", "-q", "--ff-only", "feat")
        self.assertIsNone(worktrees.refusal(
            wt_item, str(local_repo), procs.Table.read(), procs.working_dirs_or_none(os.getuid()), local_items))

    def test_multi_commit_squash_merge_is_detected_and_does_not_mutate_refs(self):
        path = self.worktree("squashed", "squashed")
        self.commit(path, "one.txt")
        self.commit(path, "two.txt")
        (path / "one.txt").write_text("one updated\n")
        self.git(path, "commit", "-q", "-am", "three")
        tip = self.git(path, "rev-parse", "HEAD").strip()

        # Advance main with an unrelated commit before and after the squash.
        self.commit(self.repo, "unrelated-before.txt")
        self.git(self.repo, "merge", "-q", "--squash", "squashed")
        self.git(self.repo, "commit", "-q", "-m", "squash feat")
        self.commit(self.repo, "unrelated-after.txt")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")

        # git cherry alone misses multi-commit squashes.
        self.assertIn("+", self.git(path, "cherry", "origin/main", "HEAD"))
        refs_before = self.git(self.repo, "for-each-ref")
        objects_before = sorted(str(p.relative_to(self.repo / ".git/objects"))
                                for p in (self.repo / ".git/objects").rglob("*") if p.is_file())
        self.assertIsNone(self.refusal(path))
        items = worktrees.listing(str(self.repo)) or []
        self.assertEqual(("squashed", tip), worktrees.branch_tip(str(self.repo), "squashed", items, str(path)))
        self.assertEqual(refs_before, self.git(self.repo, "for-each-ref"))
        objects_after = sorted(str(p.relative_to(self.repo / ".git/objects"))
                               for p in (self.repo / ".git/objects").rglob("*") if p.is_file())
        self.assertEqual(objects_before, objects_after, "read-only squash check must not write to .git/objects")

        # Works even when GIT_AUTHOR_* / GIT_COMMITTER_* are absent from the environment.
        saved = {k: os.environ.pop(k, None) for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                                                      "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
        try:
            self.assertTrue(worktrees.merged(str(path), "HEAD", ["refs/remotes/origin/main"]))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_multi_commit_squash_merge_into_origin_dev_is_detected(self):
        self.git(self.repo, "checkout", "-q", "-b", "dev", "main")
        self.git(self.repo, "push", "-q", "origin", "dev")
        self.git(self.repo, "fetch", "-q", "origin")
        path = (self.tmp / "dev-squash").resolve()
        self.git(self.repo, "worktree", "add", "-q", "-b", "dev-squash", str(path), "dev")
        self.commit(path, "a.txt")
        self.commit(path, "b.txt")
        self.git(self.repo, "merge", "-q", "--squash", "dev-squash")
        self.git(self.repo, "commit", "-q", "-m", "squash into dev")
        self.git(self.repo, "push", "-q", "origin", "dev")
        self.git(self.repo, "fetch", "-q", "origin")
        self.git(self.repo, "checkout", "-q", "main")
        self.assertIsNone(self.refusal(path))

    def test_squash_merge_safety_cases_stay_refused(self):
        # 1. Partial squash: only 2 of 3 commits were squashed into origin/main.
        partial = self.worktree("partial", "partial")
        self.commit(partial, "p1.txt")
        p2 = self.commit(partial, "p2.txt")
        self.commit(partial, "p3.txt")
        self.git(self.repo, "merge", "-q", "--squash", p2)
        self.git(self.repo, "commit", "-q", "-m", "partial squash")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.assertEqual("commits not in origin/main", self.refusal(partial))

        # 2. Extra unmerged commit added after a full multi-commit squash.
        extra = self.worktree("extra", "extra")
        self.commit(extra, "e1.txt")
        self.commit(extra, "e2.txt")
        self.git(self.repo, "merge", "-q", "--squash", "extra")
        self.git(self.repo, "commit", "-q", "-m", "squash extra")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.assertIsNone(self.refusal(extra))
        self.commit(extra, "e3-after-squash.txt")
        self.assertEqual("commits not in origin/main", self.refusal(extra))

        # 3. Merge commit in target..HEAD: refused even when squashed cleanly into origin/main.
        side = self.worktree("side-sq", "side-sq")
        self.commit(side, "s1.txt")
        has_merge = self.worktree("has-merge", "has-merge")
        self.commit(has_merge, "h1.txt")
        self.git(has_merge, "merge", "-q", "--no-ff", "-m", "merge side", "side-sq")
        self.git(self.repo, "merge", "-q", "--squash", "has-merge")
        self.git(self.repo, "commit", "-q", "-m", "squash branch with merge")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.assertEqual("commits not in origin/main", self.refusal(has_merge))

        # 4. Conflict-adjusted squash: diff on base differs from branch's net diff.
        (self.repo / "shared.txt").write_text("base\n")
        self.git(self.repo, "add", "shared.txt")
        self.git(self.repo, "commit", "-q", "-m", "add shared")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        conflict = self.worktree("conflict", "conflict")
        (conflict / "shared.txt").write_text("branch-1\n")
        self.git(conflict, "commit", "-q", "-am", "branch 1")
        (conflict / "shared.txt").write_text("branch-2\n")
        self.git(conflict, "commit", "-q", "-am", "branch 2")
        (self.repo / "shared.txt").write_text("main-change\n")
        self.git(self.repo, "commit", "-q", "-am", "main change")
        squash_proc = subprocess.run(["git", "-C", str(self.repo), "merge", "--squash", "conflict"],
                                     capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        self.assertNotEqual(0, squash_proc.returncode)
        (self.repo / "shared.txt").write_text("main-change + branch-2\n")
        self.git(self.repo, "add", "shared.txt")
        self.git(self.repo, "commit", "-q", "-m", "conflict-adjusted squash")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.assertEqual("commits not in origin/main", self.refusal(conflict))

        # 5. Net-empty branch (add + revert) must NOT falsely match an empty commit on origin/main.
        self.git(self.repo, "commit", "-q", "--allow-empty", "-m", "empty on main")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        net_empty = self.worktree("net-empty", "net-empty")
        self.commit(net_empty, "temp.txt")
        self.git(net_empty, "rm", "-q", "temp.txt")
        self.git(net_empty, "commit", "-q", "-m", "remove temp")
        self.assertEqual("commits not in origin/main", self.refusal(net_empty))

        # 6. Post-squash trailing --allow-empty commit or reverted commit pair must stay refused (F4).
        trailing_empty = self.worktree("trailing-empty", "trailing-empty")
        self.commit(trailing_empty, "te1.txt")
        self.commit(trailing_empty, "te2.txt")
        self.git(self.repo, "merge", "-q", "--squash", "trailing-empty")
        self.git(self.repo, "commit", "-q", "-m", "squash trailing-empty")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.assertIsNone(self.refusal(trailing_empty))
        self.git(trailing_empty, "commit", "-q", "--allow-empty", "-m", "unmerged note")
        self.assertEqual("commits not in origin/main", self.refusal(trailing_empty))

        trailing_revert = self.worktree("trailing-revert", "trailing-revert")
        self.commit(trailing_revert, "tr1.txt")
        self.commit(trailing_revert, "tr2.txt")
        self.git(self.repo, "merge", "-q", "--squash", "trailing-revert")
        self.git(self.repo, "commit", "-q", "-m", "squash trailing-revert")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.assertIsNone(self.refusal(trailing_revert))
        self.commit(trailing_revert, "tr3.txt")
        self.git(trailing_revert, "revert", "--no-edit", "HEAD")
        self.assertEqual("commits not in origin/main", self.refusal(trailing_revert))

    def test_squash_merge_fails_safe_on_git_errors(self):
        from unittest import mock
        path = self.worktree("err-sq", "err-sq")
        self.commit(path, "a.txt")
        self.commit(path, "b.txt")
        self.git(self.repo, "merge", "-q", "--squash", "err-sq")
        self.git(self.repo, "commit", "-q", "-m", "squash err-sq")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        real_git = worktrees.git
        for failing_cmd in ("merge-base", "diff-tree", "patch-id"):
            with self.subTest(failing_cmd=failing_cmd):
                def fail_one(where: str, *args: str, _cmd: str = failing_cmd, **kwargs: Any) -> Any:
                    if _cmd in args and "--is-ancestor" not in args:
                        return 128, "simulated git error"
                    return real_git(where, *args, **kwargs)

                with mock.patch.object(worktrees, "git", side_effect=fail_one):
                    self.assertFalse(worktrees.merged(str(path), "HEAD", ["refs/remotes/origin/main"]))

    def test_ignored_artifacts_directory_is_disposable_unless_untracked_or_holding_a_repo(self):
        unignored = self.worktree("unignored-artifacts", "unignored-artifacts")
        (unignored / "artifacts").mkdir()
        (unignored / "artifacts" / "out.json").write_text("{}\n")
        self.assertEqual("uncommitted or untracked changes", self.refusal(unignored))

        (self.repo / ".gitignore").write_text("node_modules/\n.env\nartifacts/\n")
        self.git(self.repo, "commit", "-q", "-am", "ignore artifacts")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")

        path = self.worktree("with-artifacts", "with-artifacts")
        (path / "artifacts" / "reports").mkdir(parents=True)
        (path / "artifacts" / "reports" / "out.json").write_text("{}\n")
        (path / "pkg" / "artifacts").mkdir(parents=True)
        (path / "pkg" / "artifacts" / "trace.log").write_text("ok\n")
        self.assertIsNone(self.refusal(path))

        # A nested Git repo inside ignored artifacts/ still blocks removal via the integrity guard.
        (path / "artifacts" / "nested" / ".git").mkdir(parents=True)
        self.assertEqual("it holds a Git repository at artifacts/nested", self.refusal(path))


class OwnedTests(WorktreeCase):
    def test_own_resource_then_reap_removes_it_and_its_merged_branch(self):
        path = self.worktree("wt", "feat")
        entry = self.own(path)
        self.assertEqual({"kind": "worktree", "id": str(path), "repo": str(self.repo), "branch": "feat",
                          "common": str((self.repo / ".git").resolve())}, entry.get("resource"))
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

    def test_a_repository_replaced_at_the_same_path_has_nothing_removed(self):
        path = self.worktree("wt", "feat")
        entry = self.own(path)
        saved = json.loads(self.store.path(entry["id"]).read_text())
        saved["resource"]["common"] = "/elsewhere/.git"
        self.store.path(entry["id"]).write_text(json.dumps(saved))
        result = self.cli("reap", "--id", entry["id"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("gone", result.stdout)
        self.assertTrue(path.is_dir())
        self.assertIn(str(path), self.listed())
        with self.assertRaises(resources.Refused):
            worktrees.plan(saved["resource"])

    def test_the_dry_run_runs_the_guards(self):
        path = self.worktree("wt", "feat")
        entry = self.own(path)
        (path / "notes.txt").write_text("unsaved")
        result = self.cli("reap", "--id", entry["id"], "--dry-run", "--json")
        (row,) = json.loads(result.stdout)["results"]
        self.assertEqual("would-fail", row["result"])
        self.assertIn("uncommitted or untracked changes", row["reason"])

    def test_own_kind_worktree_registers_what_git_worktree_add_made(self):
        path = self.tmp / "made"
        result = self.cli("own", "--kind", "worktree", "--ttl", "1h", "--", "git", "-C", str(self.repo), "worktree",
                          "add", "-q", "-b", "made", str(path), "main")
        self.assertEqual(0, result.returncode, result.stderr)
        (entry,) = self.store.entries()
        self.assertEqual(str(path.resolve()), (entry.get("resource") or {}).get("id"))
        self.assertTrue([e for e in self.events() if e["kind"] == "resource.owned"])

    def test_own_resource_refuses_the_main_worktree_and_a_plain_folder(self):
        for given, why, code in ((self.repo, "is a repository's main worktree", 3),
                                 (self.tmp, "is not a git worktree", 6)):
            result = self.cli("own-resource", "--kind", "worktree", "--id", str(given))
            self.assertEqual(code, result.returncode, result.stderr)
            self.assertIn(why, result.stderr)

    def test_the_session_end_removes_a_clean_owned_worktree_and_keeps_a_dirty_one(self):
        clean = self.worktree("clean", "clean")
        dirty = self.worktree("dirty", "dirty")
        claude = self.spawn("sleep", "600", cwd=str(clean))  # the ending session itself works in the worktree
        self.register_session("s1", claude.pid)
        env = self.claude_env("s1", claude)
        self.own(clean, env=env)
        entries = {(e.get("resource") or {}).get("id"): e["id"] for e in self.store.entries()}
        manual = self.cli("reap", "--id", entries[str(clean)], "--reason", "session-end (operator note)",
                          "--dry-run", "--json", env=env)
        self.assertEqual(0, manual.returncode, manual.stderr)
        (row,) = json.loads(manual.stdout)["results"]
        self.assertEqual("would-fail", row["result"], "manual reaps still honor the process using the worktree")
        result = self.cli("own-resource", "--kind", "worktree", "--id", str(dirty), "--ttl", "1h", env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        (dirty / "notes.txt").write_text("unsaved")
        hook = subprocess.run([sys.executable, "-m", "spawnfather", "hook"], cwd=str(clean), capture_output=True,
                              text=True, timeout=60, input=json.dumps({"hook_event_name": "SessionEnd",
                                                                       "session_id": "s1"}),
                              env={**env, "PYTHONPATH": str(ROOT)})
        self.assertEqual(0, hook.returncode, hook.stderr)
        self.wait_for(lambda: not clean.exists(), what="the clean worktree to go",
                      seen=lambda: (self.state / "hook.out").read_text() if (self.state / "hook.out").exists()
                      else json.dumps(self.events())[-2000:])
        dirty_id = next(e["id"] for e in self.store.entries() if (e.get("resource") or {}).get("id") == str(dirty))
        self.wait_for(lambda: any(e["kind"] == "resource.stop_failed" for e in self.events()), what="the refusal")
        self.assertTrue(dirty.is_dir())
        self.assertEqual("running", self.state_of(dirty_id))
        self.assertEqual("reaped", self.state_of(entries[str(clean)]))

    def register_session(self, session: str, pid: int) -> None:
        from spawnfather import procs
        here = procs.Table.read().get(pid)
        assert here is not None
        utc = time.strftime(procs.LSTART, time.gmtime(procs.started_at(here.start)))
        (self.sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": session, "procStart": utc}))


class SweepTests(WorktreeCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["SPAWNFATHER_REPOS"] = str(self.repo)

    def run_sweep(self, dry_run: bool = False) -> Dict[str, Any]:
        from spawnfather import procs
        table, cwds = procs.Table.read(), procs.working_dirs_or_none(os.getuid())
        return sweep.sweep(self.store, dry_run=dry_run, table=table, cwds=cwds, sessions=[])

    def test_the_removal_reads_processes_again_instead_of_the_sweeps_first_look(self):
        from spawnfather import procs
        path = self.worktree("late", "late")
        self.make_idle(path)
        stale = procs.Table.read()
        cwds = procs.working_dirs_or_none(os.getuid()) or {}
        child = self.spawn("sleep", "600", cwd=str(path))  # starts inside after the sweep's first look
        self.wait_for(lambda: child.pid in procs.Table.read().procs, what="the child")
        self.make_idle(path)
        report = sweep.sweep(self.store, table=stale, cwds=cwds, sessions=[])
        self.assertEqual([], report["worktrees"]["removed"])
        (item,) = [i for i in report["unowned_resources"] if i["id"] == str(path)]
        self.assertIn(f"in use by pid {child.pid}", item["why"])
        self.assertTrue(path.is_dir())

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
                    if e["kind"] == "unowned.reported" and e["subject"].get("kind") == "worktree"]
        self.assertEqual({f"worktree:{fresh}", f"worktree:{work}"}, {e["subject"]["id"] for e in reported})
        for line in (self.state / "events.jsonl").read_text().splitlines():
            self.assertEqual([], eventcheck.validate_line(line, strict=True), line)
        status = json.loads(self.cli("status", "--json", env={**self.env, "SPAWNFATHER_REPOS": ""}).stdout)
        self.assertEqual({str(fresh), str(work)},
                         {i["id"] for i in status["unowned_resources"] if i["kind"] == "worktree"})
        shown = next(i for i in status["unowned_resources"] if i["id"] == str(work))
        self.assertEqual(("work", str(self.repo), True, True),
                         (shown["branch"], shown["repo"], isinstance(shown["size"], int), shown["age"] > DAY))

    def test_sweep_keeps_a_released_lanes_idle_worktree_until_done(self):
        released_wt = self.worktree("released-lane", "lane/released")
        done_wt = self.worktree("done-lane", "lane/done")
        self.make_idle(released_wt)
        self.make_idle(done_wt)
        records = self.tmp / "no-lanefather" / "fathers" / "lanes"
        records.mkdir(parents=True)
        (records / "released.json").write_text(json.dumps(
            {"id": "r", "worktree": str(released_wt), "status": "released"}))
        (records / "done.json").write_text(json.dumps(
            {"id": "d", "worktree": str(done_wt), "status": "done"}))
        report = self.run_sweep()
        self.assertEqual([str(done_wt)], [i["id"] for i in report["worktrees"]["removed"]])
        self.assertTrue(released_wt.is_dir())
        self.assertFalse(done_wt.exists())
        self.assertNotIn(str(released_wt), [i["id"] for i in report["unowned_resources"]])

    def test_sweep_reaps_idle_worktree_squash_merged_into_origin_dev_with_ignored_artifacts(self):
        (self.repo / ".gitignore").write_text("node_modules/\n.env\nartifacts/\n")
        self.git(self.repo, "commit", "-q", "-am", "ignore artifacts")
        self.git(self.repo, "checkout", "-q", "-b", "dev", "main")
        self.git(self.repo, "push", "-q", "origin", "main", "dev")
        self.git(self.repo, "fetch", "-q", "origin")

        wt = (self.tmp / "dev-squashed").resolve()
        self.git(self.repo, "worktree", "add", "-q", "-b", "dev-squashed", str(wt), "dev")
        self.commit(wt, "f1.txt")
        self.commit(wt, "f2.txt")
        (wt / "artifacts" / "cov").mkdir(parents=True)
        (wt / "artifacts" / "cov" / "index.html").write_text("<html/>\n")

        self.git(self.repo, "merge", "-q", "--squash", "dev-squashed")
        self.git(self.repo, "commit", "-q", "-m", "squash dev-squashed")
        self.git(self.repo, "push", "-q", "origin", "dev")
        self.git(self.repo, "fetch", "-q", "origin")
        self.git(self.repo, "checkout", "-q", "main")
        self.make_idle(wt)

        report = self.run_sweep()
        self.assertEqual([str(wt)], [i["id"] for i in report["worktrees"]["removed"]])
        self.assertFalse(wt.exists())
        self.assertNotIn("dev-squashed", self.branches())


if __name__ == "__main__":
    unittest.main()
