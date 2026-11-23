"""A lane owner and the lifetimes it owns (fathers#171): `lane:<project>/<id>` entries with no TTL, worktree and dir
resources registered by Lanefather, `reap --owner lane:...` with the requester's evidence (--base, --disposable,
--override, --stop-inside), `reap --worktree`, the hand-over to a fix lane, and the guards no override lifts."""
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

from support import as_entry

import eventcheck
from spawnfather import dirs, entry, owner, reap, sweep
from test_worktrees import WorktreeCase


class LaneOwnerTests(unittest.TestCase):
    def test_the_project_is_everything_before_the_last_slash(self):
        found = owner.lane_owner("lane:/Users/me/work/straylight/fix-login")
        self.assertEqual(("lane", "/Users/me/work/straylight", "fix-login"),
                         (found["kind"], found["lane_project"], found["lane"]))
        self.assertEqual("lane:/Users/me/work/straylight/fix-login", owner.lane_key(found))
        self.assertEqual(("org/repo", "a.b_c-1"), (owner.lane_owner("lane:org/repo/a.b_c-1")["lane_project"],
                                                    owner.lane_owner("lane:org/repo/a.b_c-1")["lane"]))
        for bad in ("lane:", "lane:x", "lane:/id", "lane:p/", "lane:p/a b", "lane:p/..", "session:p/a", "lane:p\n/a"):
            with self.subTest(bad), self.assertRaises(ValueError):
                owner.lane_owner(bad)

    def test_a_lane_owner_is_never_gone_and_never_another_project_s(self):
        lane = owner.lane_owner("lane:/a/repo/x")
        self.assertIsNone(owner.alive(lane, None))
        from spawnfather import procs
        self.assertIsNone(owner.alive(lane, procs.Table({})))
        self.assertTrue(owner.same_lane(lane, owner.lane_owner("lane:/a/repo/x")))
        self.assertFalse(owner.same_lane(lane, owner.lane_owner("lane:/b/repo/x")))
        self.assertEqual("lane /a/repo/x", owner.describe(lane))


class LaneResourceTests(WorktreeCase):
    project = "/projects/straylight"

    def lane(self, lane_id: str = "a", project: str | None = None) -> str:
        return f"lane:{project or self.project}/{lane_id}"

    def own_lane(self, kind: str, path: Path, lane_id: str = "a", project: str | None = None, ok: int = 0
                 ) -> Dict[str, Any]:
        done = self.cli("own-resource", "--owner", self.lane(lane_id, project), "--kind", kind, "--id", str(path),
                        "--json")
        self.assertEqual(ok, done.returncode, done.stderr)
        return json.loads(done.stdout) if done.returncode == 0 else {}

    def reap_lane(self, *options: str, lane_id: str = "a", project: str | None = None, ok: int = 0
                  ) -> List[Dict[str, Any]]:
        done = self.cli("reap", "--owner", self.lane(lane_id, project), "--json", *options)
        self.assertEqual(ok, done.returncode, done.stdout + done.stderr)
        return json.loads(done.stdout)["results"]

    def folder(self, name: str) -> Path:
        path = self.tmp / name
        path.mkdir(parents=True)
        (path / "probe.log").write_text("evidence")
        return path.resolve()

    def landed(self) -> tuple[Path, str]:
        """A lane worktree with a commit on an integration branch that is not promoted: not in origin/main."""
        self.git(self.repo, "branch", "integrate/q", "main")
        path = self.tmp / "lane-a"
        self.git(self.repo, "worktree", "add", "-q", "-b", "lane/a", str(path), "integrate/q")
        tip = self.commit(path.resolve(), "work.txt")
        integration = self.tmp / "integration"
        self.git(self.repo, "worktree", "add", "-q", str(integration), "integrate/q")
        self.git(integration, "merge", "-q", "--ff-only", "lane/a")
        return path.resolve(), tip

    def test_owning_is_idempotent_for_the_lane_and_refused_for_anyone_else(self):
        path = self.worktree("lane-a", "lane/a")
        first = self.own_lane("worktree", path)
        again = self.own_lane("worktree", path)
        self.assertEqual((False, True, first["id"]), (first["already"], again["already"], again["id"]))
        self.own_lane("worktree", path, project="/projects/other", ok=3)
        self.own_lane("dir", self.tmp / "missing", ok=6)
        loaded = self.loaded(first["id"])
        self.assertTrue(loaded["no_ttl"])
        self.assertEqual("lane", loaded["owner"]["kind"])
        owned = [e for e in self.events() if e["kind"] == "resource.owned"]
        self.assertEqual([self.lane()], [e["details"]["owner"] for e in owned])
        for line in (self.state / "events.jsonl").read_text().splitlines():
            self.assertEqual([], eventcheck.validate_line(line, strict=True), line)

    def test_a_lane_entry_has_no_ttl_and_outlives_any_owner_check_of_the_sweep(self):
        path = self.worktree("lane-a", "lane/a")
        found = self.own_lane("worktree", path)
        stored = self.loaded(found["id"])
        later = time.time() + 10 * 365 * 86400
        self.assertFalse(entry.due(as_entry(stored), later))
        report = sweep.sweep(self.store, now=later, sessions=[])
        self.assertEqual([], report["reaped"])
        self.assertEqual("running", self.state_of(found["id"]))
        refused = self.cli("extend", found["id"], "--ttl", "1h")
        self.assertEqual(3, refused.returncode)
        self.assertIn("no TTL", refused.stderr)
        self.assertEqual([], json.loads(self.cli("reap", "--lane", "a", "--json").stdout)["results"])

    def test_two_projects_with_the_same_lane_id_never_cross(self):
        mine, theirs = self.folder("mine/a.scratch"), self.folder("theirs/a.scratch")
        self.own_lane("dir", mine)
        self.own_lane("dir", theirs, project="/projects/other")
        results = self.reap_lane()
        self.assertEqual([str(mine)], [r["resource"]["id"] for r in results])
        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists())

    def test_the_lane_reap_ends_processes_then_worktrees_then_folders_deepest_first(self):
        path = self.worktree("lane-a", "lane/a")
        outer = self.folder("scratch")
        inner = self.folder("scratch/target")
        for kind, where in (("dir", outer), ("dir", inner), ("worktree", path)):
            self.own_lane(kind, where)
        entries = self.store.entries()
        ordered = sorted(entries, key=reap.removal_order)
        self.assertEqual([str(path), str(inner), str(outer)], [e.get("resource", {})["id"] for e in ordered])
        self.assertEqual(["stopped"] * 3, [r["result"] for r in self.reap_lane()])
        self.assertFalse(path.exists() or outer.exists())

    def test_an_unpromoted_landed_lane_goes_with_its_integration_tip_as_base(self):
        path, tip = self.landed()
        self.own_lane("worktree", path)
        preview = self.reap_lane("--dry-run")
        self.assertEqual("would-fail", preview[0]["result"])
        self.assertIn("commits not in origin/main", preview[0]["reason"])
        integration = self.git(self.tmp / "integration", "rev-parse", "HEAD").strip()
        self.assertEqual("would-stop", self.reap_lane("--dry-run", "--base", integration)[0]["result"])
        self.assertEqual("stopped", self.reap_lane("--base", integration)[0]["result"])
        self.assertFalse(path.exists())
        self.assertNotIn("lane/a", self.branches())  # its tip is in the base it named
        del tip

    def test_ignored_files_go_with_disposable_or_an_override_that_is_evented(self):
        path, _ = self.landed()
        integration = self.git(self.tmp / "integration", "rev-parse", "HEAD").strip()
        (path / ".env").write_text("SECRET=1\n")
        self.own_lane("worktree", path)
        refused = self.reap_lane("--dry-run", "--base", integration)
        self.assertIn("ignored files: .env", refused[0]["reason"])
        self.assertEqual("would-stop", self.reap_lane("--dry-run", "--base", integration, "--disposable", ".env")[0]
                         ["result"])
        results = self.reap_lane("--base", integration, "--override", "the lane's .env is a copy")
        self.assertEqual("stopped", results[0]["result"])
        self.assertIn("ignored files: .env", " ".join(results[0]["lifted"]))
        stopped = [e for e in self.events() if e["kind"] == "resource.stopped"][-1]
        self.assertEqual("the lane's .env is a copy", stopped["details"]["override"]["reason"])
        self.assertEqual(self.lane(), stopped["details"]["owner"])
        self.assertEqual("warn", stopped.get("level"))
        for line in (self.state / "events.jsonl").read_text().splitlines():
            self.assertEqual([], eventcheck.validate_line(line, strict=True), line)

    def test_an_override_lifts_a_dirty_tree_but_never_an_integrity_guard(self):
        dirty = self.worktree("lane-a", "lane/a")
        (dirty / "README").write_text("unsaved\n")
        self.own_lane("worktree", dirty)
        self.assertEqual("stopped", self.reap_lane("--override", "the owner said so")[0]["result"])
        self.assertFalse(dirty.exists())
        nested = self.worktree("lane-b", "lane/b")
        self.git(self.tmp, "init", "-q", str(nested / "vendor" / "inner"))
        self.own_lane("worktree", nested, lane_id="b")
        kept = self.reap_lane("--override", "the owner said so", lane_id="b", ok=1)
        self.assertIn("it holds a Git repository at vendor/inner", kept[0]["reason"])
        self.assertTrue(nested.exists())
        protected = self.tmp / "on-dev"
        self.git(self.repo, "worktree", "add", "-q", "-b", "dev", str(protected), "main")
        self.own_lane("worktree", protected.resolve(), lane_id="c")
        kept = self.reap_lane("--override", "the owner said so", lane_id="c", ok=1)
        self.assertIn("it has dev checked out", kept[0]["reason"])

    def test_a_worktree_with_a_submodule_is_refused_clearly(self):
        sub = self.tmp / "sub"
        self.git(self.tmp, "init", "-q", "-b", "main", str(sub))
        (sub / "f").write_text("f")
        self.git(sub, "add", "f")
        self.git(sub, "commit", "-q", "-m", "f")
        path = self.worktree("lane-a", "lane/a")
        self.git(path, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendored")
        self.git(path, "commit", "-q", "-m", "submodule")
        self.git(path, "push", "-q", "origin", "lane/a:main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.own_lane("worktree", path)
        kept = self.reap_lane("--override", "no override lifts it", ok=1)
        self.assertIn("it has submodules (vendored)", kept[0]["reason"])
        self.assertTrue(path.exists())

    def test_a_dir_refuses_home_state_links_and_anything_holding_a_worktree(self):
        link = self.tmp / "link"
        link.symlink_to(self.folder("real"))
        for given, code, why in ((str(Path.home()), 3, "home folder"), (str(self.state), 3, "state folder"),
                                 ("/", 3, "the root"), (str(link), 3, "symbolic link")):
            self.state.mkdir(exist_ok=True)
            with self.subTest(given):
                done = self.cli("own-resource", "--owner", self.lane(), "--kind", "dir", "--id", given)
                self.assertEqual(code, done.returncode, done.stderr)
                self.assertIn(why, done.stderr)
        scratch = self.folder("a.scratch")
        self.own_lane("dir", scratch)
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(scratch / "base-0123456789ab"), "main")
        kept = self.reap_lane("--override", "no override lifts it", ok=1)
        self.assertIn("it holds a Git repository or worktree at base-0123456789ab", kept[0]["reason"])
        self.assertTrue(scratch.exists())
        self.assertEqual(2, self.cli("own-resource", "--kind", "dir", "--id", str(scratch)).returncode)

    def test_a_process_inside_a_folder_keeps_it_unless_an_override_lifts_it(self):
        scratch = self.folder("a.scratch")
        self.fake_proc(cwd=str(scratch), cmd="python3 leftover.py")
        self.own_lane("dir", scratch)
        kept = self.reap_lane(ok=1)
        self.assertIn("in use by pid", kept[0]["reason"])
        self.assertEqual("stopped", self.reap_lane("--override", "it is a test's")[0]["result"])
        self.assertFalse(scratch.exists())
        self.assertIsNone(dirs.integrity_refusal(str(self.folder("plain"))))

    def test_the_fix_lane_takes_its_parents_worktree_by_hand_over(self):
        path = self.worktree("lane-a", "lane/a")
        scratch = self.folder("a.scratch")
        self.own_lane("worktree", path)
        self.own_lane("dir", scratch)
        done = self.cli("hand", "--owner", self.lane(), "--kind", "worktree", "--to", self.lane("a-fix"), "--json")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(1, len(json.loads(done.stdout)["handed"]))
        self.assertEqual([str(scratch)], [r["resource"]["id"] for r in self.reap_lane("--dry-run")])
        moved = [r["resource"]["id"] for r in self.reap_lane("--dry-run", lane_id="a-fix")]
        self.assertEqual([str(path)], moved)
        handed = [e for e in self.events() if e["kind"] == "entry.handed"][-1]
        self.assertEqual("a-fix", handed["details"]["to"]["lane"])

    def test_reap_worktree_removes_an_unowned_one_and_refuses_an_owned_one(self):
        loose = self.worktree("agent-old", "worktree-agent-old")
        self.assertEqual("would-stop", json.loads(self.cli("reap", "--worktree", str(loose), "--dry-run", "--json")
                                                  .stdout)["results"][0]["result"])
        self.assertEqual(0, self.cli("reap", "--worktree", str(loose), "--base", "main", "--json").returncode)
        self.assertFalse(loose.exists())
        owned = self.worktree("lane-a", "lane/a")
        self.own_lane("worktree", owned)
        refused = self.cli("reap", "--worktree", str(owned), "--json")
        self.assertEqual(3, refused.returncode)
        self.assertIn("reap it through that owner", refused.stderr)

    def test_a_dead_agents_lock_is_lifted_and_a_live_one_is_a_work_guard(self):
        dead = self.worktree("agent-dead", "worktree-agent-dead")
        self.git(self.repo, "worktree", "lock", "--reason", "claude agent agent-dead (pid 99999999)", str(dead))
        self.assertEqual(0, self.cli("reap", "--worktree", str(dead), "--json").returncode)
        self.assertFalse(dead.exists())
        live = self.worktree("agent-live", "worktree-agent-live")
        self.git(self.repo, "worktree", "lock", "--reason", f"claude agent agent-live (pid {os.getpid()})", str(live))
        refused = self.cli("reap", "--worktree", str(live), "--json")
        self.assertEqual(1, refused.returncode)
        self.assertIn("locked: claude agent agent-live", refused.stdout)
        self.assertEqual(0, self.cli("reap", "--worktree", str(live), "--override", "its agent hung",
                                     "--json").returncode)
        self.assertFalse(live.exists())

    def test_usage_of_the_new_options(self):
        for words, why in ((("reap", "--owner", "s1", "--base", "main"), "go with --id"),
                           (("reap", "--owner", "lane:/p/a", "--override", " "), "needs a reason"),
                           (("own-resource", "--owner", "s1", "--kind", "dir", "--id", "/x"), "lane:PROJECT/ID"),
                           (("own-resource", "--owner", "lane:/p/a", "--kind", "docker", "--id", "x"),
                            "worktree or dir"),
                           (("hand", "--to", "self"), "an entry ID or --owner")):
            with self.subTest(words):
                done = self.cli(*words)
                self.assertEqual(2, done.returncode, done.stderr)
                self.assertIn(why, done.stderr)


@unittest.skipUnless(shutil.which("git") and shutil.which("lsof"), "needs git and lsof")
class StopInsideTests(WorktreeCase):
    """--stop-inside with the machine's own process table: a daemon left in a lane's worktree (launchd adopted it, as
    a verify that outlived its lane) is stopped; anything below this test's own tree, a live session's when an
    agent runs the suite, is not stopped and keeps the worktree."""

    def daemon(self, cwd: Path) -> int:
        """A detached sleeper working in `cwd`: double-forked, so pid 1 adopted it."""
        pid_file = self.tmp / f"daemon-{time.monotonic_ns()}.pid"
        script = ("import os, sys, time\n"
                  "if os.fork(): os._exit(0)\n"
                  "os.setsid()\n"
                  "if os.fork(): os._exit(0)\n"
                  f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                  "time.sleep(300)\n")
        subprocess.run([sys.executable, "-c", script, "lane-leftover-171"], cwd=cwd, check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_for(lambda: pid_file.exists() and pid_file.read_text(), what="the daemon's pid")
        pid = int(pid_file.read_text())
        self.addCleanup(self.stop, pid)
        return pid

    def stop(self, pid: int) -> None:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass

    def test_stop_inside_stops_a_leftover_daemon_then_removes_the_worktree(self):
        self.use_real_procs(lsof=True)
        path = self.worktree("lane-a", "lane/a")
        pid = self.daemon(path)
        lane = "lane:/projects/straylight/a"
        self.assertEqual(0, self.cli("own-resource", "--owner", lane, "--kind", "worktree", "--id", str(path))
                         .returncode)
        refused = json.loads(self.cli("reap", "--owner", lane, "--dry-run", "--json").stdout)["results"][0]
        self.assertIn(f"in use by pid {pid}", refused["reason"])
        preview = json.loads(self.cli("reap", "--owner", lane, "--stop-inside", "--dry-run", "--json").stdout)
        self.assertEqual("would-stop", preview["results"][0]["result"], preview)
        self.assertIn(f"pid {pid} ", preview["results"][0]["stops"][0])
        done = self.cli("reap", "--owner", lane, "--stop-inside", "--grace", "2", "--json")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.wait_for(lambda: not self.runs(pid), what="the daemon's end")
        self.assertFalse(path.exists())
        reaped = [e for e in self.events() if e["kind"] == "unowned.reaped"]
        self.assertEqual([pid], [e["subject"]["pid"] for e in reaped])
        self.assertIn(f"working inside {path}", reaped[0]["details"]["reason"])


if __name__ == "__main__":
    unittest.main()
