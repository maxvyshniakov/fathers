"""Owners: who launched an entry, which subagent (claims), and whether that owner still lives (fake registry)."""
from __future__ import annotations

import calendar
import json
import re
import time
import unittest

from support import ROOT, Case, proc, table

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



class ResolveTests(Case):
    """owner.resolve: the overrides and the claim on top of detect, for own, own-resource and adopt."""

    CLAUDE = table(proc(500, cmd="/Users/x/.local/share/claude/versions/2.1.322"))
    ENV = {"CLAUDE_CODE_SESSION_ID": "s1", "CLAUDE_PID": "500", "JUNKFATHER_LANE_RECORDS": "/nonexistent"}

    def claim(self, agent: str, command: str) -> None:
        owner.write_claim(self.store, {"session_id": "s1", "agent_id": agent, "agent_type": "Explore",
                                       "tool_use_id": f"t{agent}", "tool_input": {"command": command}})

    def test_the_claim_names_the_subagent(self):
        self.claim("a1", "junkfather own -- vite")
        found = owner.resolve(self.ENV, self.CLAUDE, "/", self.store, ["own", "vite"])
        self.assertEqual(("s1", 500, "a1", "Explore", ""), (found["session"], found["pid"], found["agent"],
                                                            found["agent_type"], found["claim_note"]))

    def test_without_a_claim_the_entry_is_the_sessions_and_says_why(self):
        found = owner.resolve(self.ENV, self.CLAUDE, "/", self.store, ["own", "vite"])
        self.assertEqual(None, found["agent"])
        self.assertIn("no claim", found["claim_note"])

    def test_the_overrides_win(self):
        self.claim("a1", "junkfather own -- vite")
        env = {**self.ENV, "JUNKFATHER_AGENT": "from-env"}
        self.assertEqual("from-env", owner.resolve(env, self.CLAUDE, "/", self.store, ["own", "vite"])["agent"])
        found = owner.resolve(env, self.CLAUDE, "/", self.store, ["own", "vite"], lane="l1", agent="given")
        self.assertEqual(("given", "l1"), (found["agent"], found["lane"]))
        self.assertNotIn("claim_note", found)

    def test_adopt_takes_no_claim_and_no_environment_agent(self):
        self.claim("a1", "junkfather adopt 42")
        env = {**self.ENV, "JUNKFATHER_AGENT": "from-env"}
        found = owner.resolve(env, self.CLAUDE, "/", self.store, ["adopt", "42"], claims=False)
        self.assertEqual((None, False), (found["agent"], "claim_note" in found))
        self.assertEqual("x", owner.resolve(env, self.CLAUDE, "/", self.store, ["adopt", "42"], agent="x",
                                            claims=False)["agent"])


class HandTargetTests(Case):
    START = "Thu Nov 12 10:00:00 2026"

    def register(self, session: str, pid: int) -> None:
        utc = time.strftime(procs.LSTART, time.gmtime(procs.started_at(self.START) or 0))
        (self.sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": session, "procStart": utc}))

    def test_a_live_registered_session_with_its_process(self):
        self.register("s2", 700)
        target, warning = owner.hand_target("s2", {}, table(proc(700, start=self.START)), "/")
        assert target is not None
        self.assertEqual(("", "claude", "s2", 700, self.START, None),
                         (warning, target["kind"], target["session"], target["pid"], target["pid_start"],
                          target["agent"]))

    def test_a_registered_session_whose_process_ended_stays_claude_with_a_warning(self):
        self.register("s2", 700)
        target, warning = owner.hand_target("s2", {}, table(), "/")
        self.assertEqual(("claude", None), ((target or {}).get("kind"), (target or {}).get("pid")))
        self.assertIn("no live Claude Code session s2", warning)

    def test_an_unknown_session_is_taken_for_codex_with_a_warning(self):
        target, warning = owner.hand_target("t9", {}, table(), "/")
        self.assertEqual(("codex", "t9"), ((target or {}).get("kind"), (target or {}).get("session")))
        self.assertIn("when swept as orphaned", warning)

    def test_self_is_this_session_without_its_subagent(self):
        env = {"CLAUDE_CODE_SESSION_ID": "s1", "JUNKFATHER_LANE_RECORDS": "/nonexistent"}
        target, warning = owner.hand_target("self", env, table(), "/")
        self.assertEqual(("claude", "s1", None, ""), ((target or {}).get("kind"), (target or {}).get("session"),
                                                     (target or {}).get("agent"), warning))
        self.assertEqual((None, "--to self needs an agent session (CLAUDE_CODE_SESSION_ID or CODEX_THREAD_ID)"),
                         owner.hand_target("self", {"JUNKFATHER_LANE_RECORDS": "/nonexistent"}, table(), "/"))


class SeamTests(unittest.TestCase):
    def test_cli_leaves_owners_to_owner_py(self):
        text = (ROOT / "junkfather" / "cli.py").read_text()
        self.assertEqual([], re.findall(r"_owner\.(detect|registry|registered_alive|find_claim)\b", text))


class FromEnvTests(unittest.TestCase):
    def test_an_agent_or_the_parent_process(self):
        self.assertEqual({"kind": "claude", "session": "s1", "pid": 500, "lane": "l1"},
                         owner.from_env({"CLAUDE_CODE_SESSION_ID": "s1", "CLAUDE_PID": "500", "LANEFATHER_LANE": "l1"}))
        self.assertEqual({"kind": "shell", "session": None, "pid": 42, "lane": None}, owner.from_env({}, parent=42))

    def test_the_divergences_from_detect_kept_for_an_owner_decision(self):
        """fathers#79: CODEX_SESSION_ID is not read, and a Codex caller is kind "claude"."""
        self.assertEqual("shell", owner.from_env({"CODEX_SESSION_ID": "t1"}, parent=1)["kind"])
        self.assertEqual(("claude", "t1"), tuple(owner.from_env({"CODEX_THREAD_ID": "t1"}, parent=1)[k]
                                                 for k in ("kind", "session")))


if __name__ == "__main__":
    unittest.main()
