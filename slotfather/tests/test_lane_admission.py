"""Lifetime policy through the same JSON command used by workflow callers."""
from __future__ import annotations

import contextlib
import io
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
cli = importlib.import_module("slotfather.cli")


def physical_pages(name: str) -> int:
    return {"SC_PHYS_PAGES": 8388608, "SC_PAGE_SIZE": 4096}[name]


class LaneAdmissionTests(unittest.TestCase):
    def request(self, **changes: object) -> dict[str, Any]:
        return {"v": 1, "kind": "lane-lifetime", "scope": "/example/registry", "occupied": 5,
                "cap_override": None, "sample_overrides": {
                    "cores": {"value": 12, "source": "fixture"},
                    "memory_gib": {"value": 32, "source": "fixture"},
                    "load15": {"value": 18, "source": "fixture"}}, **changes}

    def command(self, request: object, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ROOT / "slotfather"), "lane-admission", "--json"],
                              input=json.dumps(request), text=True, capture_output=True,
                              env={**os.environ, **env}, timeout=10)

    def test_exact_load_boundary_allows_registration_without_leasing(self) -> None:
        result = self.command(self.request())
        self.assertEqual(0, result.returncode, result.stderr)
        response = json.loads(result.stdout)
        self.assertTrue(response["allowed"])
        self.assertEqual(6, response["cap"])
        self.assertEqual(18, response["load_limit"])
        self.assertEqual([], response["refusals"])
        self.assertEqual("/example/registry", response["scope"])
        self.assertNotIn("jobs", response)
        self.assertNotIn("lease", response)

    def test_cap_and_load_refusals_include_override_provenance(self) -> None:
        request = self.request(occupied=7, cap_override={"value": "7", "source": "LANEFATHER_CAP"})
        request["sample_overrides"]["load15"]["value"] = 18.01
        result = self.command(request)
        self.assertEqual(0, result.returncode, result.stderr)
        response = json.loads(result.stdout)
        self.assertFalse(response["allowed"])
        self.assertEqual(["cap", "load15"], response["refusals"])
        self.assertEqual(7, response["cap"])
        self.assertEqual(6, response["derived_cap"])
        self.assertEqual("LANEFATHER_CAP", response["cap_source"])
        self.assertEqual(18.01, response["host"]["load15"]["value"])
        self.assertEqual("fixture", response["host"]["load15"]["source"])
        for raw in ("typo", "0", "-1", "2.5"):
            with self.subTest(raw=raw):
                response = json.loads(self.command(self.request(cap_override={"value": raw, "source": "CAP"})).stdout)
                self.assertEqual(6, response["cap"])
                self.assertEqual("derived", response["cap_source"])
                self.assertEqual(["invalid_cap_override"], response["warnings"])

    def test_unavailable_is_distinct_from_a_policy_refusal(self) -> None:
        cases: list[object] = [self.request(v=2), self.request(v=True), self.request(kind="jobs"),
                               self.request(occupied=-1), self.request(occupied=True), [],
                               self.request(typo=1), self.request(scope=""),
                               self.request(cap_override={"value": 3, "source": "CAP"})]
        for field, bad in (("cores", 0), ("cores", True), ("memory_gib", -1), ("load15", float("nan")),
                           ("load15", float("inf"))):
            request = self.request()
            request["sample_overrides"][field]["value"] = bad
            cases.append(request)
        for request in cases:
            with self.subTest(request=request):
                result = self.command(request)
                self.assertEqual(75, result.returncode, result.stderr)
                response = json.loads(result.stdout)
                self.assertFalse(response["available"])
                self.assertNotIn("allowed", response)
                self.assertIn(response["error"]["code"], ("invalid_request", "unsupported_version"))
                self.assertNotIn("Traceback", result.stderr)


    def test_physical_fallbacks_are_explicit_and_ignore_execution_settings(self) -> None:
        output = io.StringIO()
        request = self.request(occupied=0, sample_overrides={})
        with patch("os.cpu_count", return_value=None), patch("os.sysconf", side_effect=OSError("unknown")), \
                patch("os.getloadavg", return_value=(0.0, 0.0, 0.0)), \
                patch.dict(os.environ, {"SLOTFATHER_CORES": "100", "SLOTFATHER_MEMORY_GIB": "1000",
                                        "SLOTFATHER_LOADAVG": "999", "SLOTFATHER_CGROUP": "/limited"}), \
                patch("sys.stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(output):
            self.assertEqual(0, cli.main(["lane-admission", "--json"]))
        response = json.loads(output.getvalue())
        self.assertEqual(1, response["cap"])
        self.assertTrue(response["allowed"])
        self.assertEqual({"value": 1, "source": "unknown_fallback"}, response["host"]["cores"])
        self.assertEqual({"value": 0.0, "source": "unknown_fallback"}, response["host"]["memory_gib"])

    def test_physical_memory_is_not_cgroup_or_execution_memory(self) -> None:
        output = io.StringIO()
        request = self.request(sample_overrides={})
        with patch("os.cpu_count", return_value=12), \
                patch("os.sysconf", side_effect=physical_pages), \
                patch("os.getloadavg", return_value=(100.0, 100.0, 18.0)), \
                patch.dict(os.environ, {"SLOTFATHER_MEMORY_GIB": "4", "SLOTFATHER_CORES": "1",
                                        "SLOTFATHER_LOADAVG": "999", "SLOTFATHER_CGROUP": "/limited"}), \
                patch("sys.stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(output):
            self.assertEqual(0, cli.main(["lane-admission", "--json"]))
        response = json.loads(output.getvalue())
        self.assertEqual(6, response["cap"])
        self.assertTrue(response["allowed"])
        self.assertEqual({"value": 32.0, "source": "physical"}, response["host"]["memory_gib"])

    def test_query_leaves_all_state_unchanged_and_does_not_create_missing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "queue-state"
            directory.mkdir()
            (directory / "queue").mkdir()
            for name in ("queue/stale.json", "schema.json", "capacity.json", "order.json", "queue.log",
                         "history.jsonl", "history.jsonl.1", "machine.jsonl", "estimates.json", "queue/stale.yield"):
                (directory / name).write_text("invalid future data; must remain opaque")
            def snapshot() -> dict[str, tuple[bytes, int, int]]:
                return {str(p.relative_to(directory)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
                        for p in directory.rglob("*") if p.is_file()}
            before = snapshot()
            result = self.command(self.request(), SLOTFATHER_DIR=str(directory))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(before, snapshot())
            missing = Path(temp) / "missing"
            result = self.command(self.request(), SLOTFATHER_DIR=str(missing))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(missing.exists())

    def test_load_inspection_failure_is_technical_unavailability(self) -> None:
        for load in (OSError("cannot inspect"), float("nan")):
            with self.subTest(load=load):
                output = io.StringIO()
                request = self.request(sample_overrides={"cores": {"value": 12, "source": "fixture"},
                                                        "memory_gib": {"value": 32, "source": "fixture"}})
                with patch("os.getloadavg", side_effect=load if isinstance(load, OSError) else None,
                           return_value=(0.0, 0.0, load)), \
                        patch("sys.stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(output):
                    self.assertEqual(75, cli.main(["lane-admission", "--json"]))
                response = json.loads(output.getvalue())
                self.assertEqual("host_unavailable", response["error"]["code"])
                self.assertNotIn("allowed", response)

    def test_malformed_duplicate_oversized_and_unknown_sample_input(self) -> None:
        for raw in ("{", '{"v":1,"v":1}', " " * 65537, "[" * 2000,
                    json.dumps(self.request(sample_overrides={"disk_free_gb": {"value": 1000, "source": "x"}}))):
            with self.subTest(raw=raw[:40]):
                result = subprocess.run([sys.executable, str(ROOT / "slotfather"), "lane-admission", "--json"],
                                        input=raw, capture_output=True, text=True, timeout=10)
                self.assertEqual(75, result.returncode, result.stderr)
                self.assertEqual("invalid_request", json.loads(result.stdout)["error"]["code"])
                self.assertNotIn("Traceback", result.stderr)

    def test_floor_minimum_and_one_or_both_refusals(self) -> None:
        for cores, memory, occupied, load, cap, refusals in (
                (7, 18, 1, 10.5, 2, []), (12, 0, 0, 0, 1, []),
                (1, 4, 1, 0, 1, ["cap"]), (12, 32, 0, 18.01, 6, ["load15"]),
                (12, 32, 6, 18, 6, ["cap"])):
            with self.subTest(cores=cores, memory=memory, occupied=occupied, load=load):
                request = self.request(occupied=occupied)
                for key, value in (("cores", cores), ("memory_gib", memory), ("load15", load)):
                    request["sample_overrides"][key]["value"] = value
                result = self.command(request)
                self.assertEqual(0, result.returncode, result.stderr)
                response = json.loads(result.stdout)
                self.assertEqual(cap, response["cap"])
                self.assertEqual(refusals, response["refusals"])
                self.assertEqual(not refusals, response["allowed"])

    def test_scope_is_echoed_not_an_execution_registry_or_reservation(self) -> None:
        for scope in ("/first/registry", "/second/registry"):
            response = json.loads(self.command(self.request(scope=scope)).stdout)
            self.assertEqual(scope, response["scope"])
            self.assertTrue(response["allowed"])
            self.assertEqual(5, response["occupied"])
        response = json.loads(self.command(self.request(occupied=6)).stdout)
        self.assertFalse(response["allowed"])
        self.assertEqual(["cap"], response["refusals"])
