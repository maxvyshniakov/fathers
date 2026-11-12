"""Owners: who launched an entry, which subagent (claims), and whether that owner still lives (fake registry)."""
from __future__ import annotations

import calendar
import json
import time
import unittest

from support import Case, proc, table

from junkfather import owner, procs

START = "Thu Nov 12 10:00:00 2026"
UTC = time.strftime(procs.LSTART, time.gmtime(procs.started_at(START) or 0))


class AliveTests(unittest.TestCase):
    claude = {"kind": "claude", "session": "s1", "pid": 500, "pid_start": START}

    def test_the_recorded_session_process_lives(self):
        self.assertTrue(owner.alive(self.claude, table(proc(500, start=START)), sessions=[]))

    def test_a_reused_session_pid_is_not_the_session(self):
        self.assertFalse(owner.alive(self.claude, table(proc(500, start="Thu Nov 12 12:00:00 2026")), sessions=[]))

    def test_a_resumed_session_in_another_process_lives(self):
        registry = [{"pid": 600, "sessionId": "s1", "procStart": UTC}]
        self.assertTrue(owner.alive(self.claude, table(proc(600, start=START)), sessions=registry))

    def test_a_registry_record_whose_pid_was_reused_does_not_count(self):
        registry = [{"pid": 600, "sessionId": "s1", "procStart": UTC}]
        self.assertFalse(owner.alive(self.claude, table(proc(600, start="Thu Nov 12 12:00:00 2026")),
                                     sessions=registry))

    def test_another_sessions_record_does_not_count(self):
        registry = [{"pid": 600, "sessionId": "s2", "procStart": UTC}]
        self.assertFalse(owner.alive(self.claude, table(proc(600, start=START)), sessions=registry))

    def test_a_claude_owner_without_a_process_is_unknown_unless_the_registry_names_it(self):
        bare = {"kind": "claude", "session": "s1", "pid": None}
        self.assertIsNone(owner.alive(bare, table(proc(600, start=START)), sessions=[]))
        registry = [{"pid": 600, "sessionId": "s1", "procStart": UTC}]
        self.assertTrue(owner.alive(bare, table(proc(600, start=START)), sessions=registry))

    def test_unknown_is_never_gone(self):
        self.assertIsNone(owner.alive(self.claude, None))
        self.assertIsNone(owner.alive({"kind": "codex", "session": "t1", "pid": None}, table()))

    def test_a_session_whose_process_ended_and_that_the_registry_does_not_name_is_gone(self):
        self.assertFalse(owner.alive(self.claude, table(proc(600, start=START)), sessions=[]))

    def test_utc_procstart_matches_local_lstart(self):
        local = procs.started_at(START)
        self.assertEqual(calendar.timegm(time.strptime(UTC, procs.LSTART)), local)


class DetectTests(unittest.TestCase):
    def test_claude_session_and_process_from_the_environment(self):
        t = table(proc(500, cmd="/Users/x/.local/share/claude/versions/2.1.322 --resume"), proc(510, ppid=500,
                  cmd="/bin/zsh -c x"), proc(520, ppid=510, cmd="python3 -m junkfather own"))
        found = owner.detect({"CLAUDE_CODE_SESSION_ID": "s1", "CLAUDE_PID": "500",
                              "JUNKFATHER_LANE": "lane-9"}, t, "/", me=520)
        self.assertEqual(("claude", "s1", 500, "lane-9"), (found["kind"], found["session"], found["pid"],
                                                           found["lane"]))

    def test_codex_session_is_its_nearest_codex_ancestor(self):
        t = table(proc(400, cmd="/opt/bin/codex exec"), proc(410, ppid=400, cmd="zsh"), proc(420, ppid=410))
        found = owner.detect({"CODEX_THREAD_ID": "t1", "JUNKFATHER_LANE_RECORDS": "/nonexistent"}, t, "/", me=420)
        self.assertEqual(("codex", "t1", 400), (found["kind"], found["session"], found["pid"]))

    def test_outside_an_agent_the_owner_is_the_parent(self):
        t = table(proc(300, cmd="zsh", tty="ttys001"), proc(310, ppid=300))
        found = owner.detect({"JUNKFATHER_LANE_RECORDS": "/nonexistent"}, t, "/", me=310)
        self.assertEqual(("shell", None, 300), (found["kind"], found["session"], found["pid"]))


class ClaimTests(Case):
    def claim(self, agent, command, at, tool):
        owner.write_claim(self.store, {"session_id": "s1", "agent_id": agent, "tool_use_id": tool,
                                       "tool_input": {"command": command}}, now=at)

    def test_the_newest_matching_claim_names_the_agent(self):
        now = time.time()
        self.claim("a1", "cd x && junkfather own --ttl 1h -- npm run dev", now - 300, "t1")
        self.claim(None, "junkfather own --ttl 1h -- npm run dev", now - 5, "t2")
        self.claim("a2", "junkfather own -- vite", now - 1, "t3")
        found, note = owner.find_claim(self.store, "s1", ["own", "npm", "run", "dev"], now)
        self.assertEqual((None, ""), (found and found["agent"], note))
        self.assertEqual("t2", found and found["tool_use_id"])

    def test_two_agents_running_the_same_command_together_is_ambiguous(self):
        now = time.time()
        self.claim("a1", "junkfather own -- npm run dev", now - 10, "t1")
        self.claim("a2", "junkfather own -- npm run dev", now - 5, "t2")
        found, note = owner.find_claim(self.store, "s1", ["own", "npm", "run", "dev"], now)
        self.assertIsNone(found)
        self.assertIn("ambiguous", note)

    def test_a_claim_that_does_not_name_junkfather_does_not_count(self):
        now = time.time()
        self.claim("a1", "npm run dev", now - 1, "t1")
        self.assertIsNone(owner.find_claim(self.store, "s1", ["own", "npm"], now)[0])

    def test_a_claim_that_does_not_hold_every_word_is_no_claim(self):
        now = time.time()
        self.claim("agent-b", "junkfather list", now - 60, "t1")
        self.claim("agent-c", "junkfather own -- npm run build", now - 30, "t2")
        found, note = owner.find_claim(self.store, "s1", ["own", "npm", "run", "dev"], now)
        self.assertIsNone(found)
        self.assertIn("no claim", note)

    def test_a_claim_is_used_once_per_command(self):
        now = time.time()
        self.claim("a1", "junkfather own -- vite", now - 5, "t1")
        self.assertEqual("a1", (owner.find_claim(self.store, "s1", ["own", "vite"], now)[0] or {}).get("agent"))
        self.assertIsNone(owner.find_claim(self.store, "s1", ["own", "vite"], now)[0])

    def test_unsafe_names_write_no_claim(self):
        self.assertIsNone(owner.write_claim(self.store, {"session_id": "../x", "tool_use_id": "t",
                                                         "tool_input": {"command": "junkfather"}}))
        self.assertFalse((self.state / "x").exists())

    def test_old_claims_are_pruned(self):
        self.claim("a1", "junkfather own -- x", time.time() - 2 * 86400, "old")
        path = self.state / "claims" / "s1" / "old.json"
        old = time.time() - 2 * 86400
        import os
        os.utime(path, (old, old))
        self.claim("a1", "junkfather own -- y", time.time(), "new")
        self.assertFalse(path.exists())
        self.assertEqual("junkfather own -- y",
                         json.loads((self.state / "claims" / "s1" / "new.json").read_text())["command"])


if __name__ == "__main__":
    unittest.main()
