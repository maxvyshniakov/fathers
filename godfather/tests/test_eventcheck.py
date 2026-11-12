"""The event schema v1 validator: SPEC.md's examples pass strict, every schema rule has a failing fixture."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from godfather import eventcheck  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "events"
VALID = {
    "v": 1, "time": "2026-11-12T02:24:16.204Z", "host": "mbp", "source": "slotfather", "kind": "lease.granted",
    "subject": {"type": "request", "id": "0000009701-51587"}, "actor": {"type": "process"}, "details": {},
}


def spec_examples() -> list:
    """The example events in SPEC.md, one JSON object per blank-line-separated block of its json fence."""
    text = (ROOT / "SPEC.md").read_text(encoding="utf-8")
    start = text.index("Example events, one per line")
    block = re.search(r"```json\n(.*?)```", text[start:], re.S)
    assert block is not None
    return [json.loads(chunk) for chunk in block.group(1).split("\n\n") if chunk.strip()]


class Examples(unittest.TestCase):
    def test_spec_examples_pass_strict(self) -> None:
        examples = spec_examples()
        self.assertEqual(len(examples), 7)
        for event in examples:
            with self.subTest(kind=event["kind"]):
                self.assertEqual(eventcheck.validate(event, strict=True), [])

    def test_minimal_event_is_valid(self) -> None:
        self.assertEqual(eventcheck.validate(VALID, strict=True), [])

    def test_optional_fields(self) -> None:
        event = dict(VALID, level="warn", id="0000009701")
        self.assertEqual(eventcheck.validate(event, strict=True), [])

    def test_single_word_kind_is_allowed(self) -> None:
        # Lanefather's documented "override" kind has no dot.
        self.assertEqual(eventcheck.validate(dict(VALID, kind="override"), strict=True), [])

    def test_details_four_levels_deep_is_allowed(self) -> None:
        event = dict(VALID, details={"a": {"b": {"c": {}}}})
        self.assertEqual(eventcheck.validate(event, strict=True), [])


class Invalid(unittest.TestCase):
    def test_each_fixture_fails_naming_its_field(self) -> None:
        cases = json.loads((FIXTURES / "invalid.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 30)
        for case in cases:
            with self.subTest(case["name"]):
                problems = eventcheck.validate(case["event"])
                self.assertTrue(problems, "expected a problem")
                self.assertTrue(
                    any(problem.startswith(case["field"] + ":") or problem.startswith(case["field"] + " ")
                        or problem.startswith(case["field"] + ".") for problem in problems),
                    f"no problem names {case['field']}: {problems}",
                )

    def test_schema_table_rules_are_all_covered(self) -> None:
        cases = json.loads((FIXTURES / "invalid.json").read_text(encoding="utf-8"))
        fields = {case["field"].split(".")[0] for case in cases}
        for field in eventcheck.REQUIRED + eventcheck.OPTIONAL:
            self.assertIn(field, fields)

    def test_not_an_object(self) -> None:
        self.assertEqual(eventcheck.validate([1]), ["event: must be a JSON object"])

    def test_strict_refuses_unknown_top_level_field(self) -> None:
        event = dict(VALID, extra=1)
        self.assertEqual(eventcheck.validate(event), [])
        self.assertEqual(eventcheck.validate(event, strict=True), ["extra: unknown top-level field (strict)"])

    def test_lenient_ignores_unknown_nested_fields(self) -> None:
        event = dict(VALID, subject=dict(VALID["subject"], ticket="710"), actor={"type": "user", "tty": "s001"})
        self.assertEqual(eventcheck.validate(event, strict=True), [])

    def test_non_finite_number(self) -> None:
        problems = eventcheck.validate(dict(VALID, details={"cpu": float("nan")}))
        self.assertEqual(problems, ["event.details.cpu: must be a finite number"])

    def test_control_character_in_a_key(self) -> None:
        problems = eventcheck.validate(dict(VALID, details={"a\x07": 1}))
        self.assertTrue(problems and problems[0].startswith("event.details: key"))


class Lines(unittest.TestCase):
    def test_valid_line(self) -> None:
        self.assertEqual(eventcheck.validate_line(json.dumps(VALID) + "\n", strict=True), [])
        self.assertEqual(eventcheck.validate_line(json.dumps(VALID).encode()), [])

    def test_line_too_long(self) -> None:
        line = json.dumps(dict(VALID, details={"x": "a" * eventcheck.MAX_LINE_BYTES})) + "\n"
        problems = eventcheck.validate_line(line)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("line: "))

    def test_line_exactly_at_limit(self) -> None:
        compact = json.dumps(dict(VALID, details={"x": ""}), separators=(",", ":"))
        pad = eventcheck.MAX_LINE_BYTES - 1 - len(compact.encode())
        line = json.dumps(dict(VALID, details={"x": "a" * pad}), separators=(",", ":")) + "\n"
        self.assertEqual(len(line.encode()), eventcheck.MAX_LINE_BYTES)
        self.assertEqual(eventcheck.validate_line(line), [])

    def test_not_json(self) -> None:
        self.assertTrue(eventcheck.validate_line("{not json")[0].startswith("line: not one JSON object"))

    def test_nan_literal_is_not_json(self) -> None:
        line = json.dumps(dict(VALID, details={"cpu": 1.0})).replace("1.0", "NaN")
        self.assertTrue(eventcheck.validate_line(line)[0].startswith("line: not one JSON object"))

    def test_duplicate_key(self) -> None:
        line = json.dumps(VALID)[:-1] + ', "v": 1}'
        self.assertIn("duplicate key", eventcheck.validate_line(line)[0])

    def test_not_utf8(self) -> None:
        self.assertTrue(eventcheck.validate_line(b'{"v": "\xff"}')[0].startswith("line: not UTF-8"))


class Cli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def run_main(self, *args: str) -> tuple:
        out = io.StringIO()
        with redirect_stdout(out):
            code = eventcheck.main(list(args))
        return code, out.getvalue()

    def test_good_file(self) -> None:
        path = self.write("events.jsonl", "".join(json.dumps(e) + "\n" for e in spec_examples()))
        self.assertEqual(self.run_main("--strict", path), (0, ""))

    def test_empty_file_is_fine(self) -> None:
        self.assertEqual(self.run_main(self.write("events.jsonl", "")), (0, ""))

    def test_bad_line_names_file_and_line(self) -> None:
        path = self.write("events.jsonl", json.dumps(VALID) + "\n" + json.dumps(dict(VALID, v=2)) + "\n")
        code, out = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertEqual(out, f"{path}:2: v: must be 1, got 2\n")

    def test_strict_flag(self) -> None:
        path = self.write("events.jsonl", json.dumps(dict(VALID, extra=True)) + "\n")
        self.assertEqual(self.run_main(path)[0], 0)
        self.assertEqual(self.run_main("--strict", path)[0], 1)

    def test_partial_last_line(self) -> None:
        path = self.write("events.jsonl", json.dumps(VALID))
        code, out = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertIn(":1: line: no newline at the end", out)

    def test_blank_line_is_a_problem(self) -> None:
        path = self.write("events.jsonl", json.dumps(VALID) + "\n\n")
        code, out = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertIn(":2: line: not one JSON object", out)

    def test_missing_file(self) -> None:
        code, out = self.run_main(os.path.join(self.tmp.name, "nope.jsonl"))
        self.assertEqual(code, 1)
        self.assertIn(":0: cannot read", out)

    def test_usage(self) -> None:
        self.assertEqual(eventcheck.main([]), 2)
        self.assertEqual(eventcheck.main(["--lenient", "x"]), 2)

    def test_runs_as_a_standalone_script(self) -> None:
        path = self.write("events.jsonl", json.dumps(dict(VALID, v=3)) + "\n")
        script = ROOT / "src" / "godfather" / "eventcheck.py"
        result = subprocess.run([sys.executable, "-I", str(script), "--strict", path],
                                capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(result.returncode, 1)
        self.assertIn(":1: v: must be 1", result.stdout)

    def test_standalone_imports_only_the_standard_library(self) -> None:
        source = (ROOT / "src" / "godfather" / "eventcheck.py").read_text(encoding="utf-8")
        imports = set(re.findall(r"^(?:from|import) (\w+)", source, re.M))
        self.assertLessEqual(imports, {"__future__", "datetime", "json", "re", "sys", "typing"})
        self.assertTrue(source.startswith("# MIT License"))


if __name__ == "__main__":
    unittest.main()
