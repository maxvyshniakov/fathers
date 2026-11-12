"""The event log (lanefather.events, lanefather.journal): every ledger entry once more as an event in Godfather's
schema, checked in strict mode by its vendored validator (tests/vendor/eventcheck.py)."""
from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
import pathlib
import sys
import tempfile
from typing import Any, cast
import unittest
from unittest import mock

from support import ROOT, LaneCase

sys.path.insert(0, str(ROOT / "tests" / "vendor"))
import eventcheck  # pyright: ignore[reportMissingImports]  vendored, unchanged

from lanefather import config, events, journal

VENDOR = ROOT / "tests" / "vendor"


def validate_file(path: pathlib.Path) -> list[str]:
    return eventcheck.validate_file(str(path), strict=True)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


class VendoredValidatorTests(unittest.TestCase):
    def test_the_vendored_validator_has_the_recorded_sha256(self):
        recorded = (VENDOR / "eventcheck.py.sha256").read_text().split()[0]
        self.assertEqual(hashlib.sha256((VENDOR / "eventcheck.py").read_bytes()).hexdigest(), recorded)

    @unittest.skipUnless(os.environ.get("GODFATHER_DIR"), "GODFATHER_DIR names no Godfather checkout")
    def test_the_vendored_validator_is_the_canonical_one(self):
        canonical = pathlib.Path(os.environ["GODFATHER_DIR"]) / "src" / "godfather" / "eventcheck.py"
        self.assertEqual((VENDOR / "eventcheck.py").read_bytes(), canonical.read_bytes(),
                         "copy the canonical eventcheck.py again and record its SHA-256")


class EventLogTests(LaneCase):
    def ledger_lines(self) -> list[str]:
        return (self.state / "ledger.log").read_text().splitlines()

    def logged(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_a_lane_from_start_to_done_writes_one_valid_event_per_ledger_line(self):
        worktree = self.created("e", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\ne\n"})
        self.lane("check", "e")
        self.lane("land", "e")
        if self.record("e")["status"] != "done":  # land finishes the lane itself unless something holds it
            self.lane("done", "e")
        self.assertEqual(self.record("e")["status"], "done")
        self.assertEqual(validate_file(self.events), [])
        found, ledger = self.logged(), self.ledger_lines()
        self.assertEqual(len(found), len(ledger))
        for event, line in zip(found, ledger, strict=True):
            # The same moment and the same text: the ledger line is the event's time to the second and its message.
            self.assertEqual(line, f"{event['time'][:19]}Z {event['details']['message']}")
            self.assertIn(event["kind"], events.KINDS)
            self.assertEqual((event["source"], event["subject"]["project"]), ("lanefather", "Fixture"))
            self.assertEqual(event["subject"]["state_dir"], str(self.state))
        kinds = [event["kind"] for event in found]
        for kind in ("lane.started", "lane.checked", "lane.landed", "lane.post_merge", "lane.done"):
            self.assertIn(kind, kinds)
        started = found[kinds.index("lane.started")]
        self.assertEqual(started["subject"], {"type": "lane", "id": "e", "ticket": "404", "project": "Fixture",
                                              "state_dir": str(self.state)})
        self.assertEqual(started["details"]["claims"], ["lib/**"])
        self.assertEqual(found[kinds.index("lane.post_merge")]["details"]["status"], "green")
        self.assertEqual(self.events.stat().st_mode & 0o777, 0o600)

    def test_an_override_is_a_warning_and_the_actor_is_the_agent_session(self):
        self.env.update(CLAUDE_CODE_SESSION_ID="4bceaaaf-session", GODFATHER_ACTOR="godfather/0.1.0")
        self.start("o", "lib/**")
        self.lane("land", "o", ok=False)
        self.lane("done", "o", "--force-reason", "parked by hand")
        self.assertEqual(validate_file(self.events), [])
        override = next(event for event in self.logged() if event["kind"] == "override")
        self.assertEqual(override["level"], "warn")
        self.assertEqual(override["details"]["command"], "done")
        self.assertEqual(override["details"]["reason"], "parked by hand")
        self.assertEqual({key: override["actor"][key] for key in ("type", "session", "name", "via")},
                         {"type": "agent", "session": "4bceaaaf-session", "name": "claude-code", "via": "godfather"})

    def test_an_event_that_cannot_be_written_leaves_the_action_and_its_ledger_line(self):
        blocker = self.tmp / "not-a-directory"
        blocker.write_text("")
        self.env["LANEFATHER_EVENTS"] = str(blocker / "events.jsonl")
        self.start("x", "lib/**")
        self.assertEqual(self.record("x")["status"], "planned")
        self.assertIn("start x ticket=404", (self.state / "ledger.log").read_text())


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lanefather-events-"))
        self.log = self.tmp / "events.jsonl"
        patched = mock.patch.dict(os.environ, {"LANEFATHER_EVENTS": str(self.log)})
        patched.start()
        self.addCleanup(patched.stop)
        self.addCleanup(config.use, None)
        config.use(config.Config(project="Writer", state_dir=str(self.tmp / "lanes")))
        (self.tmp / "lanes").mkdir()

    def test_control_characters_and_long_values_are_cleaned_and_the_line_stays_under_16_kib(self):
        journal.ledger("probe \x1b[31mred\x1b[0m", kind="lane.checked", lane="p",
                       details={"output": "x" * 40_000, "deep": {"a": {"b": {"c": {"d": 1}}}}, "nan": float("nan"),
                                "lines": ["y" * 900] * 30})
        self.assertEqual(validate_file(self.log), [])
        line = self.log.read_bytes()
        self.assertLessEqual(len(line), events.MAX_LINE)
        event = json.loads(line)
        self.assertEqual(event["details"]["message"], "probe  [31mred [0m")
        self.assertIsNone(event["details"]["nan"])
        self.assertIsInstance(event["details"]["deep"]["a"]["b"], str)

    def test_line_separators_never_split_an_event(self):
        journal.ledger("one\u2028two\u2029three", kind="queue.updated", subject={"type": "queue", "id": "q"})
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertEqual(json.loads(self.log.read_text())["details"]["message"], "one two three")

    def test_a_held_lock_drops_the_event_and_the_ledger_line_stays(self):
        with open(self.tmp / "events.lock", "a") as lock, mock.patch.object(events, "LOCK_WAIT", 0.1):
            fcntl.flock(lock, fcntl.LOCK_EX)
            journal.ledger("start held", kind="lane.started", lane="h")
        self.assertFalse(self.log.exists())
        self.assertIn("start held", (self.tmp / "lanes" / "ledger.log").read_text())

    def test_past_4_mib_the_log_becomes_events_jsonl_1(self):
        with mock.patch.object(events, "ROTATE_AT", 600):
            for number in range(5):
                journal.ledger(f"queued {number}", kind="queue.updated", subject={"type": "queue", "id": "q"})
        rotated = self.tmp / "events.jsonl.1"
        self.assertTrue(rotated.exists())
        self.assertEqual(validate_file(rotated) + validate_file(self.log), [])
        # The previous .1 is replaced: the newest line is always in the log itself.
        self.assertIn("queued 4", self.log.read_text())

    def test_a_project_in_a_temporary_directory_writes_no_machine_event_unless_the_log_is_named(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home")}):
            del os.environ["LANEFATHER_EVENTS"]
            journal.ledger("start t", kind="lane.started", lane="t")
        self.assertFalse((self.tmp / "home" / ".local" / "state" / "lanefather" / "events.jsonl").exists())
        self.assertTrue((self.tmp / "lanes" / "ledger.log").exists())

    def test_the_capabilities_name_the_event_log(self):
        from lanefather import runtime
        block = runtime.dashboard()
        self.assertEqual((block["v"], block["tag"]), (1, "lane"))
        self.assertEqual(block["events"], {"path": str(self.log), "versions": [1]})


class CallSiteTests(unittest.TestCase):
    """Two logs per call site drift (Godfather's open risk 7): every ledger line goes through journal.ledger, and
    each call names one documented kind."""

    def test_every_ledger_call_names_a_documented_kind_and_nothing_else_writes_the_ledger(self):
        calls = 0
        for path in sorted((ROOT / "lanefather").glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "ledger"
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "journal"):
                    calls += 1
                    kinds = [k.value for k in node.keywords if k.arg == "kind"]
                    kind = kinds[0] if kinds else None
                    self.assertIsInstance(kind, ast.Constant, f"{path.name}:{node.lineno}")
                    self.assertIn(str(cast(ast.Constant, kind).value), events.KINDS, f"{path.name}:{node.lineno}")
            if path.name != "journal.py":
                self.assertNotIn('"ledger.log"', path.read_text().replace("'ledger.log'", '"ledger.log"'), path.name)
        self.assertGreaterEqual(calls, 35)


if __name__ == "__main__":
    unittest.main()
