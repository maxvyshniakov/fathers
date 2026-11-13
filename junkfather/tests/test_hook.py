"""What each lifecycle event asks for, decided without running anything."""
from __future__ import annotations

import time
from typing import List, Optional

from support import Case, as_entry

from junkfather import hook


class PlanTests(Case):
    def entry(self, entry_id: str, agent: Optional[str] = None, keep: str = "owner", session: str = "s1",
              expires: Optional[float] = None, state: str = "running") -> None:
        self.store.save(as_entry({"id": entry_id, "state": state, "keep": keep, "created": time.time(),
                         "expires": expires or time.time() + 3600,
                         "owner": {"kind": "claude", "session": session, "agent": agent}}))

    def test_subagent_stop_reaps_only_when_that_agent_owns_something_not_kept(self):
        self.entry("j1", agent="a1", keep="session")
        self.assertIsNone(hook.plan({"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a1"},
                                    self.store))
        self.entry("j2", agent="a1")
        self.assertEqual(["reap", "--owner", "s1", "--agent", "a1", "--reason", "subagent-stop", "--quiet", "--via",
                          "hook"],
                         hook.plan({"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a1"},
                                   self.store))

    def test_session_end_reaps_the_sessions_entries(self):
        self.entry("j1", session="s2")
        self.assertIsNone(hook.plan({"hook_event_name": "SessionEnd", "session_id": "s1"}, self.store))
        self.entry("j2")
        plan = hook.plan({"hook_event_name": "SessionEnd", "session_id": "s1", "reason": "logout"}, self.store)
        self.assertEqual(["reap", "--owner", "s1"], (plan or [])[:3])

    def test_stop_is_a_turn_end_and_reaps_only_expired_entries(self):
        self.entry("j1")
        self.assertIsNone(hook.plan({"hook_event_name": "Stop", "session_id": "s1"}, self.store))
        self.entry("j2", expires=time.time() - 1)
        self.assertIn("--expired", hook.plan({"hook_event_name": "Stop", "session_id": "s1"}, self.store) or [])

    def test_pre_tool_use_claims_only_commands_that_name_junkfather(self):
        spawned: List[List[str]] = []
        for command in ("ls -la", "junkfather own -- vite"):
            hook.handle({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_use_id": f"t{len(command)}",
                         "agent_id": "a1", "tool_name": "Bash", "tool_input": {"command": command}}, self.store,
                        spawn=spawned.append)
        claims = sorted(p.name for p in (self.state / "claims" / "s1").iterdir())
        self.assertEqual(["t22.json"], claims)
        self.assertEqual([], spawned)

    def test_a_reap_runs_detached(self):
        self.entry("j1", agent="a1")
        spawned: List[List[str]] = []
        hook.handle({"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a1"}, self.store,
                    spawn=spawned.append)
        self.assertEqual(1, len(spawned))
        self.assertEqual(["reap", "--owner", "s1", "--agent", "a1"], spawned[0][3:8])
