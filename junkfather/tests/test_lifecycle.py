"""End to end through the CLI with real processes: own, the hooks (SubagentStop, SessionEnd, Stop), keep and
hand-over, TTL expiry, orphans by a fake session registry, adopt, and the report-only path for unowned processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from support import ROOT, Case

from junkfather import procs


def killpg(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


class OwnTests(Case):
    def test_attached_passes_output_and_exit_status_through(self):
        result = self.cli("own", "--ttl", "1m", "--", "sh", "-c", "echo out; echo err >&2; exit 7")
        self.assertEqual(7, result.returncode)
        self.assertEqual("out\n", result.stdout)
        self.assertIn("err", result.stderr)
        (entry,) = self.store.entries()
        self.assertEqual(("exited", 7, "attached"), (entry["state"], entry["exit"], entry["mode"]))
        self.assertEqual(entry["pid"], entry["pgid"], "its own process group")

    def test_a_signal_to_an_attached_junkfather_stops_the_command_group(self):
        runner = self.spawn(sys.executable, "-m", "junkfather", "own", "--ttl", "1h", "--", "sh", "-c",
                            "sleep 600 & sleep 601", cwd=ROOT, env=self.env)
        self.wait_for(lambda: any(e.get("state") == "running" for e in self.store.entries()), what="running")
        (entry,) = self.store.entries()
        self.wait_for(lambda: len(procs.Table.read().group(entry["pgid"])) >= 3, what="the group")
        group = [p.pid for p in procs.Table.read().group(entry["pgid"])]
        runner.send_signal(signal.SIGTERM)
        self.assertEqual(128 + signal.SIGTERM, runner.wait(30))
        self.assertEqual("reaped", self.state_of(entry["id"]))
        self.assertFalse([pid for pid in group if self.runs(pid)])

    def test_detach_returns_at_once_and_logs_the_output(self):
        started = time.monotonic()
        entry = self.own_detached(self.env, "--ttl", "1h", argv=("sh", "-c", "echo hello; sleep 600"))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual("running", entry["state"])
        self.assertTrue(self.runs(entry["pid"]))
        self.wait_for(lambda: "hello" in Path(entry["log"]).read_text(), what="the log")
        table = procs.Table.read()
        supervisor = table.get(entry["supervisor"]["pid"])
        assert supervisor is not None
        self.assertNotIn("sleep", supervisor.cmd, "the supervisor's command line names only the entry")
        self.assertEqual(0, self.cli("reap", "--id", entry["id"]).returncode)
        self.assertFalse(self.runs(entry["pid"]))
        self.wait_for(lambda: not self.runs(entry["supervisor"]["pid"]), what="the supervisor to end")

    def test_a_missing_command_fails_without_a_running_entry(self):
        result = self.cli("own", "--", "/nonexistent/command")
        self.assertEqual(127, result.returncode)
        self.assertEqual(["failed"], [e["state"] for e in self.store.entries()])


class HookTests(Case):
    def setUp(self):
        super().setUp()
        self.claude = self.fake_claude("s1")
        self.session_env = self.claude_env("s1", self.claude)

    def own_as(self, agent, *options, argv=("sleep", "600")):
        """What a subagent's Bash call does: the PreToolUse hook writes its claim, then the command runs."""
        command = "junkfather own --detach " + " ".join(options) + " -- " + " ".join(argv)
        self.hook("PreToolUse", "s1", tool_name="Bash", tool_input={"command": command},
                  tool_use_id=f"toolu_{time.time_ns()}", **({"agent_id": agent} if agent else {}))
        return self.own_detached(self.session_env, *options, argv=argv)

    def test_the_claim_names_the_subagent(self):
        entry = self.own_as("agent-a", "--ttl", "1h")
        self.assertEqual(("claude", "s1", "agent-a", self.claude.pid),
                         (entry["owner"]["kind"], entry["owner"]["session"], entry["owner"]["agent"],
                          entry["owner"]["pid"]))

    def test_subagent_stop_reaps_that_subagents_entries_only(self):
        mine = self.own_as("agent-a", "--ttl", "1h")
        other = self.own_as("agent-b", "--ttl", "1h", argv=("sleep", "601"))
        main = self.own_as(None, "--ttl", "1h", argv=("sleep", "602"))
        result = self.hook("SubagentStop", "s1", agent_id="agent-a")
        self.assertEqual((0, ""), (result.returncode, result.stdout), "the hook never prints or blocks")
        self.wait_for(lambda: self.state_of(mine["id"]) == "reaped", what="agent-a's entry reaped")
        self.assertFalse(self.runs(mine["pid"]))
        self.assertEqual(["running", "running"], [self.state_of(other["id"]), self.state_of(main["id"])])

    def test_keep_for_session_survives_a_subagent_stop_but_not_the_session_end(self):
        kept = self.own_as("agent-a", "--ttl", "1h", "--keep-for", "session")
        self.hook("SubagentStop", "s1", agent_id="agent-a")
        time.sleep(1.5)
        self.assertEqual("running", self.state_of(kept["id"]))
        self.assertTrue(self.runs(kept["pid"]))
        self.hook("SessionEnd", "s1", reason="other")
        self.wait_for(lambda: self.state_of(kept["id"]) == "reaped", what="session end reap")
        self.assertFalse(self.runs(kept["pid"]))

    def test_a_handed_entry_belongs_to_its_new_session(self):
        entry = self.own_as("agent-a", "--ttl", "1h")
        other = self.fake_claude("s2")
        result = self.cli("hand", entry["id"], "--to", "s2")
        self.assertEqual(0, result.returncode, result.stderr)
        handed = self.store.load(entry["id"]) or {}
        self.assertEqual(("s2", None, other.pid), (handed["owner"]["session"], handed["owner"]["agent"],
                                                    handed["owner"]["pid"]))
        self.assertEqual("s1", handed["handed"][0]["from"]["session"])
        self.hook("SubagentStop", "s1", agent_id="agent-a")
        self.hook("SessionEnd", "s1")
        time.sleep(1.5)
        self.assertEqual("running", self.state_of(entry["id"]))
        self.hook("SessionEnd", "s2")
        self.wait_for(lambda: self.state_of(entry["id"]) == "reaped", what="the new owner's end")

    def test_stop_reaps_only_expired_entries(self):
        fresh = self.own_as(None, "--ttl", "1h")
        stale = self.own_as(None, "--ttl", "1h", argv=("sleep", "603"))
        self.store.update(stale["id"], lambda e: e.update(expires=time.time() - 1))
        self.hook("Stop", "s1")
        self.wait_for(lambda: self.state_of(stale["id"]) == "reaped", what="the expired entry")
        self.assertEqual("running", self.state_of(fresh["id"]))

    def test_the_hook_fails_open_on_garbage(self):
        result = self.cli("hook", input="not json")
        self.assertEqual((0, ""), (result.returncode, result.stdout))


class TtlAndOrphanTests(Case):
    def test_the_supervisor_ends_an_entry_at_its_ttl(self):
        entry = self.own_detached(self.env, "--ttl", "2s")
        self.wait_for(lambda: self.state_of(entry["id"]) == "reaped", what="the TTL")
        self.assertEqual("ttl expired", (self.store.load(entry["id"]) or {})["end"]["reason"])
        self.assertFalse(self.runs(entry["pid"]))

    def test_sweep_reaps_an_adopted_entry_past_its_ttl(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        result = self.cli("adopt", str(child.pid), "--ttl", "1s")
        self.assertEqual(0, result.returncode, result.stderr)
        time.sleep(1.2)
        swept = json.loads(self.cli("sweep", "--json").stdout)
        self.assertEqual(["ttl expired"], [r["reason"] for r in swept["reaped"]])
        self.assertIsNotNone(child.wait(10))

    def test_sweep_reaps_the_entries_of_a_session_that_ended(self):
        claude = self.fake_claude("s1")
        entry = self.own_detached(self.claude_env("s1", claude), "--ttl", "1h")
        dry = json.loads(self.cli("sweep", "--json").stdout)
        self.assertEqual([], dry["reaped"], "a live session keeps its entries")
        claude.kill()
        claude.wait()
        swept = json.loads(self.cli("sweep", "--json").stdout)
        self.assertEqual([entry["id"]], [r["id"] for r in swept["reaped"]])
        self.assertIn("owner gone", swept["reaped"][0]["reason"])
        self.assertFalse(self.runs(entry["pid"]))

    def test_a_session_resumed_in_a_new_process_keeps_its_entries(self):
        claude = self.fake_claude("s1", register=False)
        entry = self.own_detached(self.claude_env("s1", claude), "--ttl", "1h")
        claude.kill()
        claude.wait()
        self.fake_claude("s1")  # the registry names s1 with a new, live process
        swept = json.loads(self.cli("sweep", "--json").stdout)
        self.assertEqual([], swept["reaped"])
        self.assertEqual("running", self.state_of(entry["id"]))

    def test_a_reused_pid_is_not_reaped_by_a_sweep(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        self.assertEqual(0, self.cli("adopt", str(child.pid), "--ttl", "1s").returncode)
        (entry,) = self.store.entries()
        self.store.update(entry["id"], lambda e: e.update(start="Mon Jan  1 00:00:00 2024", known=[]))
        time.sleep(1.2)
        swept = json.loads(self.cli("sweep", "--json").stdout)
        self.assertEqual([], swept["reaped"])
        self.assertEqual([entry["id"]], [g["id"] for g in swept["gone"]])
        self.assertTrue(self.runs(child.pid))


class AdoptTests(Case):
    def test_adopt_refuses_what_is_not_ours_to_own(self):
        self.assertNotEqual(0, self.cli("adopt", "1").returncode)
        self.assertNotEqual(0, self.cli("adopt", str(os.getpid())).returncode, "the caller's ancestor")
        self.assertNotEqual(0, self.cli("adopt", "999999").returncode)

    def test_adopting_a_non_leader_takes_its_tree_not_its_group(self):
        shell = self.spawn("sh", "-c", "sleep 600 & sleep 601 & wait", start_new_session=True)
        self.addCleanup(killpg, shell.pid)
        self.wait_for(lambda: len(procs.Table.read().descendants(shell.pid)) >= 2, what="children")
        first, second = sorted(p.pid for p in procs.Table.read().descendants(shell.pid))
        self.assertEqual(0, self.cli("adopt", str(first), "--ttl", "1h").returncode)
        (entry,) = self.store.entries()
        self.assertFalse(entry["group"])
        self.assertEqual(0, self.cli("reap", "--id", entry["id"]).returncode)
        self.assertFalse(self.runs(first))
        self.assertTrue(self.runs(second), "the rest of the group is not the entry's")
        self.assertTrue(self.runs(shell.pid))


class UnownedTests(Case):
    def test_unowned_scratchpad_processes_are_reported_never_stopped(self):
        scratch = self.tmp / "scratch"
        (scratch / "session").mkdir(parents=True)
        env = {**self.env, "JUNKFATHER_SCRATCH_ROOTS": str(scratch)}
        # A browser left behind: its launcher exited, so launchd adopted it (ppid 1).
        launcher = subprocess.run(["sh", "-c", "sleep 600 >/dev/null 2>&1 & echo $!"], cwd=scratch / "session",
                                  capture_output=True, text=True)
        orphan = int(launcher.stdout.strip())
        self.addCleanup(lambda: os.kill(orphan, 9) if self.runs(orphan) else None)
        self.wait_for(lambda: (procs.Table.read().get(orphan) or procs.Proc(0, 0, 0, 0, "", "", "")).ppid == 1,
                      what="the orphan's adoption")
        swept = json.loads(self.cli("sweep", "--json", env=env).stdout)
        item = next((i for i in swept["unowned"] if i["pid"] == orphan), None)
        self.assertIsNotNone(item, swept["unowned"])
        assert item is not None
        self.assertIn("working directory", item["why"])
        self.assertTrue(self.runs(orphan), "report only")
        refused = self.cli("reap", "--pid", str(orphan), env=env)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("--force", refused.stderr)
        self.assertTrue(self.runs(orphan))
        forced = self.cli("reap", "--pid", str(orphan), "--force", "--grace", "2", env=env)
        self.assertEqual(0, forced.returncode, forced.stderr)
        self.wait_for(lambda: not self.runs(orphan), what="the forced stop")

    def test_force_refuses_a_pid_the_sweep_did_not_list(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        (self.state).mkdir(parents=True, exist_ok=True)
        (self.state / "unowned.json").write_text(json.dumps({"v": 1, "items": []}))
        result = self.cli("reap", "--pid", str(child.pid), "--force")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("did not list", result.stderr)
        self.assertTrue(self.runs(child.pid))

    def test_force_refuses_a_listed_pid_that_now_belongs_to_another_process(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "unowned.json").write_text(json.dumps(
            {"v": 1, "items": [{"pid": child.pid, "start": "Mon Jan  1 00:00:00 2024"}]}))
        result = self.cli("reap", "--pid", str(child.pid), "--force")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("start time changed", result.stderr)
        self.assertTrue(self.runs(child.pid))


class ListTests(Case):
    def test_list_shows_owner_age_and_ttl(self):
        claude = self.fake_claude("s1")
        entry = self.own_detached(self.claude_env("s1", claude), "--ttl", "2h", "--label", "storybook")
        listed = json.loads(self.cli("list", "--json").stdout)["entries"]
        self.assertEqual([entry["id"]], [r["id"] for r in listed])
        row = listed[0]
        self.assertEqual(("s1", True, "storybook", 1), (row["session"], row["owner_alive"], row["label"],
                                                        row["processes"]))
        self.assertGreater(row["ttl_left"], 7000)
        text = self.cli("list").stdout
        self.assertIn(entry["id"], text)
        self.assertIn("[storybook] sleep 600", text)
