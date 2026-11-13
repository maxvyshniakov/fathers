"""events.jsonl in Godfather's schema v1, from real commands; status --json and capabilities --json."""
from __future__ import annotations

import json
import os
import stat
import unittest

from support import Case

import eventcheck
from junkfather import events


class EventLogTests(Case):
    def lines(self):
        path = self.state / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_every_command_writes_valid_events_of_its_documented_kinds(self):
        claude = self.fake_claude("s1")
        env = self.claude_env("s1", claude)
        self.assertEqual(0, self.cli("own", "--ttl", "1m", "--", "true", env=env).returncode)
        entry = self.own_detached(env, "--ttl", "1h", "--label", "vite dev")
        self.assertEqual(0, self.cli("extend", entry["id"], "--ttl", "2h", env=env).returncode)
        self.fake_claude("s2")
        self.assertEqual(0, self.cli("hand", entry["id"], "--to", "s2", env=env).returncode)
        self.assertEqual(0, self.cli("reap", "--id", entry["id"], "--reason", "godfather: orphan",
                                     env={**env, "GODFATHER_ACTOR": "godfather/0.1.0"}).returncode)
        child = self.spawn("sleep", "600", start_new_session=True)
        self.assertEqual(0, self.cli("adopt", str(child.pid), "--ttl", "1h").returncode)
        self.assertEqual(0, self.cli("sweep", "--quiet").returncode)
        self.assertEqual(0, self.cli("hook", input=json.dumps({"hook_event_name": "SessionEnd", "session_id": 5})
                                     ).returncode)
        raw = (self.state / "events.jsonl").read_text().splitlines()
        for line in raw:
            self.assertEqual([], eventcheck.validate_line(line, strict=True), line)
        found = self.lines()
        kinds = [e["kind"] for e in found]
        for kind in ("entry.owned", "entry.exited", "entry.extended", "entry.handed", "entry.reaped",
                     "entry.adopted", "sweep.finished"):
            self.assertIn(kind, kinds)
        self.assertTrue(set(kinds) <= set(events.KINDS), kinds)
        owned = next(e for e in found if e["kind"] == "entry.owned" and e["subject"].get("label") == "vite dev")
        self.assertEqual({"type": "entry", "id": entry["id"], "label": "vite dev", "pid": entry["pid"]},
                         owned["subject"])
        self.assertEqual(("agent", "s1", claude.pid), (owned["actor"]["type"], owned["actor"]["session"],
                                                       owned["actor"]["pid"]))
        self.assertEqual(("junkfather", 1), (owned["source"], owned["v"]))
        reaped = next(e for e in found if e["kind"] == "entry.reaped")
        self.assertEqual(("godfather", "godfather: orphan", ["TERM"]),
                         (reaped["actor"].get("via"), reaped["details"]["reason"], reaped["details"]["signals"]))
        sweep = next(e for e in found if e["kind"] == "sweep.finished")
        self.assertEqual({"type": "tool", "name": "junkfather", "via": "sweep"}, sweep["actor"])
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.state / "events.jsonl").st_mode))

    def test_a_hook_that_fails_writes_hook_failed(self):
        payload = {"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a1"}
        os.environ["JUNKFATHER_STATE"] = str(self.state)
        from junkfather import hook
        from junkfather.state import Store
        original = Store.entries
        Store.entries = lambda self: (_ for _ in ()).throw(RuntimeError("broken store"))  # type: ignore
        try:
            import io
            import sys
            stdin = sys.stdin
            sys.stdin = io.StringIO(json.dumps(payload))
            try:
                self.assertEqual(0, hook.main())
            finally:
                sys.stdin = stdin
        finally:
            Store.entries = original  # type: ignore
        (event,) = self.lines()
        self.assertEqual(("hook.failed", "error"), (event["kind"], event["level"]))
        self.assertEqual([], eventcheck.validate(event, strict=True))

    def test_control_characters_and_long_commands_are_cleaned(self):
        events.write(self.state, "entry.owned", {"type": "entry", "id": "j1", "label": "a\x1b[31mred"},
                     events.tool("sweep"), {"argv": ["x" * 1000], "deep": {"a": {"b": {"c": {"d": 1}}}}})
        (event,) = self.lines()
        self.assertEqual([], eventcheck.validate(event, strict=True))
        self.assertEqual("a [31mred", event["subject"]["label"])
        self.assertEqual(400, len(event["details"]["argv"][0]))

    def test_the_log_rotates_past_four_mebibytes(self):
        self.state.mkdir(parents=True)
        (self.state / "events.jsonl").write_bytes(b"{}\n" * ((4 << 20) // 3 + 1))
        events.write(self.state, "sweep.finished", {"type": "sweep", "id": "x"}, events.tool("sweep"), {})
        self.assertTrue((self.state / "events.jsonl.1").exists())
        self.assertEqual(1, len(self.lines()))


class DashboardContractTests(Case):
    def test_status_holds_running_entries_unowned_processes_and_the_last_sweep(self):
        entry = self.own_detached(self.env, "--ttl", "1h")
        self.assertEqual(0, self.cli("sweep", "--quiet").returncode)
        status = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual(1, status["v"])
        self.assertTrue(status["host"] and status["time"].endswith("Z"))
        self.assertEqual([entry["id"]], [r["id"] for r in status["entries"]])
        self.assertIsNotNone(status["sweep"]["time"])
        self.assertEqual(1, status["sweep"]["result"]["running"])
        for item in status["unowned"]:
            self.assertTrue({"pid", "start", "cmd", "cwd", "root", "age"} <= set(item))

    def test_capabilities_name_the_dashboard_contract(self):
        caps = json.loads(self.cli("capabilities", "--json").stdout)
        board = caps["dashboard"]
        self.assertEqual(("junkfather", "junk", ["status", "--json"], 5, ["reap", "extend"]),
                         (caps["tool"], board["tag"], board["snapshot"]["argv"], board["snapshot"]["poll"],
                          board["actions"]))
        self.assertEqual(str(self.state / "events.jsonl"), board["events"]["path"])
        self.assertEqual([1], board["events"]["versions"])
        self.assertEqual({"versions": [1]}, board["settings"], "the settings contract (fathers#75)")
        self.assertTrue(os.path.isfile(board["changelog"]))


if __name__ == "__main__":
    unittest.main()
