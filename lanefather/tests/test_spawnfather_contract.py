"""Lanefather and the real Spawnfather of this repository together (fathers#171): a lane owns its worktree, scratch
and build directories through Spawnfather (owner `lane:<main checkout>/<id>`), and done, drop and gc have it remove
them, with real git, a temporary SPAWNFATHER_STATE and the machine's own process table.

The stand-in (fixtures/spawnfather) answers the same contract for the rest of the suite without reading the process
table; these tests are where the two CLIs meet."""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Any
import unittest

from support import SPAWNFATHER, TWO_FINDINGS, LaneCase, run_in_process


@unittest.skipUnless((SPAWNFATHER / "spawnfather" / "cli.py").is_file(), "needs this repository's spawnfather")
class SpawnfatherContractTests(LaneCase):
    def setUp(self) -> None:
        super().setUp()
        self.spawn_state = self.use_real_spawnfather()

    # Helpers

    def spawnfather(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.tmp / "bin" / "spawnfather"), *args], env=self.env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=120)

    def entries(self) -> list[dict[str, Any]]:
        directory = self.spawn_state / "entries"
        return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))] if directory.exists() else []

    def lane_entries(self, lane_id: str, repo: pathlib.Path | None = None) -> list[tuple[str, str, str]]:
        """(kind, path, state) of the resources the lane owner holds, from the entry files."""
        project = str((repo or self.repo).resolve())
        found: list[tuple[str, str, str]] = []
        for entry in self.entries():
            owner: dict[str, Any] = entry.get("owner") or {}
            if owner.get("kind") == "lane" and owner.get("lane") == lane_id and owner.get("lane_project") == project:
                found.append((str(entry["resource"]["kind"]), str(entry["resource"]["id"]), str(entry["state"])))
        return sorted(found)

    def spawn_events(self, kind: str) -> list[dict[str, Any]]:
        path = self.spawn_state / "events.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [event for event in map(json.loads, lines) if event["kind"] == kind]

    def owner(self, lane_id: str, repo: pathlib.Path | None = None) -> str:
        return f"lane:{(repo or self.repo).resolve()}/{lane_id}"

    def landed(self, lane_id: str, *, create: bool = True) -> tuple[pathlib.Path, dict[str, Any]]:
        """A lane started with its own worktree, a commit, checked and landed with --keep (done runs by hand)."""
        if create:
            worktree = pathlib.Path(self.created(lane_id, f"lib/{lane_id}.txt"))
        else:
            self.start(lane_id, f"lib/{lane_id}.txt")
            worktree = self.tmp / f"adopted-{lane_id}"
            self.git(self.repo, "worktree", "add", "-q", "-b", f"lane/{lane_id}", str(worktree), "integrate/q")
            self.lane("adopt", lane_id, str(worktree), f"lane/{lane_id}")
        self.commit(worktree, {f"lib/{lane_id}.txt": f"{lane_id}\n"})
        self.lane("check", lane_id)
        self.lane("land", lane_id, "--keep")
        return worktree, self.record(lane_id)

    def daemon(self, cwd: pathlib.Path, name: str) -> int:
        """A leftover that outlived its lane: double-forked in `cwd`, so pid 1 adopted it (below no session)."""
        pid_file = self.tmp / f"{name}.pid"
        script = ("import os, time\n"
                  "if os.fork(): os._exit(0)\n"
                  "os.setsid()\n"
                  "if os.fork(): os._exit(0)\n"
                  f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                  "time.sleep(300)\n")
        subprocess.run([sys.executable, "-c", script, name], cwd=cwd, check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while not (pid_file.exists() and pid_file.read_text()):
            self.assertLess(time.monotonic(), deadline, "the leftover did not start")
            time.sleep(0.05)
        pid = int(pid_file.read_text())
        self.addCleanup(self.kill, pid)
        return pid

    def kill(self, pid: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)

    def runs(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    # The flows

    def test_start_registers_and_done_reaps_through_spawnfather_with_its_events(self):
        worktree = pathlib.Path(self.created("a", "lib/a.txt"))
        record = self.record("a")
        scratch = pathlib.Path(record["scratch"][0]).resolve()
        self.assertEqual([("dir", str(scratch), "running"), ("worktree", str(worktree.resolve()), "running")],
                         self.lane_entries("a"))
        self.commit(worktree, {"lib/a.txt": "a\n"})
        self.lane("check", "a")
        self.lane("land", "a", "--keep")
        target = self.cache / "lane-a-target"  # a build made it after start: done registers it on demand
        self.write(target, "debug/libcore.a", "x")
        self.write(worktree, "desktop/node_modules/pkg/index.js", "x")  # disposable build output
        output = self.lane("done", "a")
        self.assertIn(f"removed {worktree.resolve()}", output)
        self.assertFalse(worktree.exists() or scratch.exists() or target.exists())
        self.assertNotIn("lane/a", self.git(self.repo, "branch", "--list"))
        self.assertEqual("done", self.record("a")["status"])
        stopped = self.spawn_events("resource.stopped")
        self.assertEqual({self.owner("a")}, {e["details"]["owner"] for e in stopped})
        self.assertEqual({str(worktree.resolve()), str(scratch), str(target.resolve())},
                         {e["details"]["resource"]["id"] for e in stopped})
        self.assertEqual({"reaped"}, {state for _, _, state in self.lane_entries("a")})

    def test_a_leftover_process_refuses_done_and_stop_inside_stops_it(self):
        worktree, _ = self.landed("a")
        pid = self.daemon(worktree, "verify-leftover-661")
        output = self.lane("done", "a", ok=False)
        self.assertIn(f"in use by pid {pid}", output)
        self.assertTrue(worktree.exists())
        self.assertEqual("landed", self.record("a")["status"])
        config = self.integration / ".lanefather.toml"
        config.write_text(config.read_text() + "\n[done]\nstop_inside = true\n")
        output = self.lane("done", "a")
        self.assertIn(f"stopped pid {pid} ", output)
        deadline = time.monotonic() + 30
        while self.runs(pid):
            self.assertLess(time.monotonic(), deadline, "the leftover still runs")
            time.sleep(0.05)
        self.assertFalse(worktree.exists())
        self.assertEqual([pid], [e["subject"]["pid"] for e in self.spawn_events("unowned.reaped")])
        self.assertIn(f"Spawnfather stopped pid {pid}", (self.state / "ledger.log").read_text())

    def test_an_unpromoted_landed_lane_goes_because_done_names_the_integration_tip(self):
        worktree, _ = self.landed("a")
        alone = self.spawnfather("reap", "--owner", self.owner("a"), "--dry-run", "--json")
        rows = {r["resource"]["kind"]: r for r in json.loads(alone.stdout)["results"]}
        self.assertIn("commits not in main", rows["worktree"]["reason"])
        self.lane("done", "a")
        self.assertFalse(worktree.exists())
        self.assertNotIn("lane/a", self.git(self.repo, "branch", "--list"))

    def test_an_ignored_env_file_keeps_the_lane_until_disposable_or_an_override(self):
        common = pathlib.Path(self.git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        with open(common / "info" / "exclude", "a") as exclude:
            exclude.write(".env\n")
        worktree, _ = self.landed("a")
        self.write(worktree, ".env", "SECRET=1\n")
        output = self.lane("done", "a", ok=False)
        self.assertIn("ignored files: .env", output)
        self.lane("done", "a", "--force-reason", "the .env is a copy of the main checkout's")
        self.assertFalse(worktree.exists())
        stopped = [e for e in self.spawn_events("resource.stopped") if e["details"]["resource"]["kind"] == "worktree"]
        self.assertEqual("the .env is a copy of the main checkout's", stopped[-1]["details"]["override"]["reason"])
        other, _ = self.landed("b")
        self.write(other, ".env", "SECRET=2\n")
        config = self.integration / ".lanefather.toml"
        config.write_text(config.read_text() + '\n[gc]\ndisposable = ["node_modules", ".env"]\n')
        self.lane("done", "b")
        self.assertFalse(other.exists())

    def test_drop_removes_a_lane_that_adds_nothing(self):
        worktree = pathlib.Path(self.created("a", "lib/a.txt"))
        self.lane("drop", "a")
        self.assertFalse(worktree.exists())
        self.assertEqual(("done", True), (self.record("a")["status"], "dropped" in self.record("a")))
        self.assertEqual({"reaped"}, {state for _, _, state in self.lane_entries("a")})

    def test_the_fix_lane_takes_its_parents_worktree_over_and_done_removes_it(self):
        self.review_text.write_text(TWO_FINDINGS)
        worktree, _ = self.landed("a")
        self.lane("review", "a")
        self.lane("review", "a", "--fix-lane")
        fix = "a-review-fixes"
        held = [path for kind, path, state in self.lane_entries(fix) if kind == "worktree" and state == "running"]
        self.assertEqual([str(worktree.resolve())], held)
        self.assertEqual([], [p for k, p, s in self.lane_entries("a") if k == "worktree" and s == "running"])
        self.git(worktree, "checkout", "-q", "-B", f"lane/{fix}", "integrate/q")
        self.commit(worktree, {"lib/a.txt": "a\nfixed\n"}, "fix(review): the findings")
        self.lane("check", fix, cwd=worktree)
        self.lane("land", fix, "--keep")
        self.lane("done", fix)
        self.assertFalse(worktree.exists())

    def test_gc_removes_a_merged_idle_agent_worktree(self):
        old = self.repo / ".claude/worktrees/agent-old"
        self.git(self.repo, "worktree", "add", "-q", "-b", "worktree-agent-old", str(old), "integrate/q")
        past = time.time() - 3 * 3600
        gitdir = pathlib.Path(self.git(old, "rev-parse", "--absolute-git-dir"))
        for path in (old, gitdir / "HEAD", gitdir / "index", gitdir / "logs" / "HEAD"):
            os.utime(path, (past, past))
        report = json.loads(self.lane("gc", "--json"))
        self.assertFalse(old.exists(), report)
        self.assertEqual("lanefather gc", self.spawn_events("resource.stopped")[-1]["details"]["reason"])

    def test_a_lane_from_before_the_upgrade_registers_on_demand(self):
        stand_in = self.tmp / "bin" / "spawnfather"
        real = stand_in.read_text()
        stand_in.unlink()
        self.env["LANEFATHER_SPAWNFATHER"] = str(self.tmp / "bin" / "none")
        output = self.lane("start", "a", "--ticket", "404", "--base", "integrate/q", "--claims", "lib/a.txt",
                           "--create")
        self.assertIn("Spawnfather is not installed", output)
        del self.env["LANEFATHER_SPAWNFATHER"]
        stand_in.write_text(real)
        stand_in.chmod(0o755)
        self.assertEqual([], self.lane_entries("a"))
        worktree = pathlib.Path(self.record("a")["worktree"])
        self.commit(worktree, {"lib/a.txt": "a\n"})
        self.lane("check", "a")
        self.lane("land", "a", "--keep")
        self.lane("done", "a")
        self.assertFalse(worktree.exists())
        self.assertEqual({"reaped"}, {state for _, _, state in self.lane_entries("a")})

    def test_an_adopted_worktree_outside_claude_worktrees_and_a_stale_base_checkout_in_the_scratch(self):
        worktree, record = self.landed("a", create=False)
        scratch = pathlib.Path(record["scratch"][0])
        stale = scratch / "base-0123456789ab"  # a killed red check's base checkout (#568)
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(stale), "integrate/q")
        self.lane("done", "a")
        self.assertFalse(worktree.exists() or scratch.exists())
        self.assertNotIn(str(stale), self.git(self.repo, "worktree", "list"))

    def test_two_projects_with_the_same_lane_id_never_cross(self):
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(other)], check=True, capture_output=True)
        self.git(other, "config", "user.name", "Lane Test")
        self.git(other, "config", "user.email", "lane@example.com")
        other_integration = other / ".claude" / "worktrees" / "integrate-q"
        self.git(other, "worktree", "add", "-q", "-b", "integrate/q", str(other_integration), "origin/integrate/q")
        other_state = self.tmp / "other-state"
        env = {**self.env, "LANEFATHER_STATE": str(other_state)}
        code, output = run_in_process(["start", "a", "--ticket", "404", "--base", "integrate/q", "--claims",
                                       "lib/a.txt", "--create"], other_integration, env)
        self.assertEqual(0, code, output)
        worktree, _ = self.landed("a")
        theirs = self.lane_entries("a", other)
        self.assertEqual(2, len(theirs))
        self.lane("done", "a")
        self.assertFalse(worktree.exists())
        self.assertEqual(theirs, self.lane_entries("a", other))
        self.assertTrue(all(pathlib.Path(path).exists() for _, path, _ in theirs))

    def test_real_spawnfather_gc_dry_run_gone_lane_with_base_checkout_and_symlink_scratch(self):
        """fathers#259 review (F1/F3/F6): under real Spawnfather, gc --dry-run previews dropping a gone lane with
        base-* without error, live gc drops it, orphaned scratch with a nested repo is kept, and symlinked scratch
        refuses done with 'a symbolic link'."""
        # 1. Gone lane with stale linked base-* checkout inside its scratch.
        self.start("g", "lib/g.txt", "--create")
        wt = pathlib.Path(self.record("g")["worktree"])
        scratch = self.state / "g.scratch"
        stale_base = scratch / "base-0123456789ab"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(stale_base), "integrate/q")
        self.git(self.repo, "worktree", "remove", "--force", str(wt))
        self.git(self.repo, "branch", "-D", "lane/g")
        dry = json.loads(self.lane("gc", "--dry-run", "--json"))
        self.assertEqual(dry["errors"], [], dry)
        self.assertIn("g", [r["path"] for r in dry["removed"] if r["kind"] == "lane"])
        self.assertTrue(stale_base.is_dir(), "gc --dry-run must not delete base-*")
        live = json.loads(self.lane("gc", "--json"))
        self.assertEqual(live["errors"], [], live)
        self.assertFalse(scratch.exists())

        # 2. Orphaned scratch holding a standalone git repo is kept by real Spawnfather.
        orphan = self.state / "orphan-git.scratch"
        sub = orphan / "sub"
        sub.mkdir(parents=True)
        self.git(sub, "init", "-q")
        after = json.loads(self.lane("gc", "--json"))
        self.assertTrue((sub / ".git").exists())
        self.assertIn(str(orphan.resolve()), [k["path"] for k in after["kept"]])

        # 3. Symlinked scratch refuses done with 'a symbolic link' under real Spawnfather.
        wt_s, _ = self.landed("s")
        s_scratch = self.state / "s.scratch"
        if s_scratch.exists():
            shutil.rmtree(s_scratch)
        outside = self.tmp / "outside-real"
        self.write(outside, "keep.txt", "safe\n")
        s_scratch.symlink_to(outside)
        refused = self.lane("done", "s", ok=False)
        self.assertIn("a symbolic link", refused)
        self.assertTrue((outside / "keep.txt").exists())
        self.assertTrue(wt_s.exists())


if __name__ == "__main__":
    unittest.main()
