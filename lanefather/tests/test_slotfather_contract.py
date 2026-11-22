"""Contract gate for `tests/fixtures/slotfather`, `tests/fixtures/procs-slotfather`, and Lanefather's Slotfather
adapters against the real `../slotfather/slotfather` CLI in an isolated `SLOTFATHER_DIR` (fathers#222).

Runs the sibling checkout's `../slotfather/slotfather` in the `fathers` repository (`SLOTFATHER_BIN` names another
executable); skips gracefully outside the monorepo when no compatible `slotfather` is available. When the sibling
checkout is present, any CLI break or fixture drift fails here instead of skipping.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, cast
import unittest
from unittest import mock

from support import FIXTURES, ROOT, close_process

from lanefather import admission, config, host, procs, slots
from lanefather.project import Project

SIBLING = ROOT.parent / "slotfather" / "slotfather"
EXECUTABLE = os.environ.get("SLOTFATHER_BIN") or (str(SIBLING) if SIBLING.is_file() else shutil.which("slotfather"))
FIXTURE_SLOTFATHER = FIXTURES / "slotfather"
FIXTURE_PROCS = FIXTURES / "procs-slotfather"


def probe_capabilities(executable: str | None, environ: dict[str, str]) -> dict[str, Any] | None:
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "capabilities", "--json"], capture_output=True, text=True, timeout=10,
                                env=environ)
        if result.returncode != 0:
            return None
        parsed: object = json.loads(result.stdout)
        return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


class SlotfatherFixtureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="lanefather-slotfather-contract-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        base_env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("SLOTFATHER_", "LANEFATHER_", "FAKE_SLOTFATHER_", "FIXTURE_"))}
        self.real_env = {
            **base_env,
            "SLOTFATHER_DIR": str(self.tmp / "real-state"),
            "SLOTFATHER_LEGACY_DIRS": "",
            "SLOTFATHER_CORES": "12",
            "SLOTFATHER_MEMORY_GIB": "64",
            "SLOTFATHER_LOADAVG": "0.0 0.0 0.0",
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
        }
        self.fake_env = {
            **base_env,
            "FAKE_SLOTFATHER_TOTAL": "4",
            "FAKE_SLOTFATHER_HELD": "0",
            "FAKE_SLOTFATHER_RANGE": "1",
            "FAKE_SLOTFATHER_LOG": str(self.tmp / "fake-slotfather.log"),
            "SLOTFATHER_LOADAVG": "0.0 0.0 0.0",
        }
        found = probe_capabilities(EXECUTABLE, self.real_env)
        features = found.get("features") if isinstance(found, dict) else None
        if found is None or not isinstance(features, list) or "slot-range" not in features:
            self.assertNotEqual(EXECUTABLE, str(SIBLING), f"../slotfather/slotfather capabilities --json: {found!r}")
            self.skipTest("no ../slotfather checkout or installed slotfather with slot-range capability")
        assert EXECUTABLE is not None
        self.real_bin: str = EXECUTABLE
        self.fake_bin: str = str(FIXTURE_SLOTFATHER)

    def run_cli(self, binary: str, env: dict[str, str], *args: str, code: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([binary, *args], cwd=self.tmp, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, code, f"{binary} {' '.join(args)} exit {result.returncode}:\n"
                                                  f"{result.stdout}{result.stderr}")
        return result

    def json_cli(self, binary: str, env: dict[str, str], *args: str) -> dict[str, Any]:
        out = self.run_cli(binary, env, *args).stdout
        parsed: object = json.loads(out)
        self.assertIsInstance(parsed, dict, out)
        return cast(dict[str, Any], parsed)

    def with_slotfather_on_path(self, binary: str, env: dict[str, str], subdir: str) -> dict[str, str]:
        bin_dir = self.tmp / subdir
        bin_dir.mkdir(exist_ok=True)
        link = bin_dir / "slotfather"
        link.unlink(missing_ok=True)
        link.symlink_to(binary)
        return {**env, "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"}

    def test_capabilities_and_slot_range_negotiation_match(self) -> None:
        real_caps = self.json_cli(self.real_bin, self.real_env, "capabilities", "--json")
        fake_caps = self.json_cli(self.fake_bin, self.fake_env, "capabilities", "--json")
        for caps in (real_caps, fake_caps):
            self.assertEqual((caps["v"], caps["tool"]), (1, "slotfather"))
            self.assertIsInstance(caps["version"], str)
            self.assertIn("slot-range", caps["features"])
            self.assertEqual(caps["run"].get("slot_range"), True)

        for label, binary, env in (("real", self.real_bin, self.real_env), ("fake", self.fake_bin, self.fake_env)):
            with self.subTest(target=label), \
                    mock.patch.dict(os.environ, self.with_slotfather_on_path(binary, env, f"bin-{label}"), clear=True):
                self.assertTrue(slots.supports_range())

        legacy_env = {k: v for k, v in self.fake_env.items() if k != "FAKE_SLOTFATHER_RANGE"}
        with mock.patch.dict(os.environ, self.with_slotfather_on_path(self.fake_bin, legacy_env, "bin-legacy"),
                             clear=True):
            self.assertFalse(slots.supports_range())

    def test_slots_available_and_advise_budget_match(self) -> None:
        for label, binary, env in (("real", self.real_bin, self.real_env), ("fake", self.fake_bin, self.fake_env)):
            with self.subTest(target=label), \
                    mock.patch.dict(os.environ, self.with_slotfather_on_path(binary, env, f"slots-{label}"),
                                    clear=True):
                self.assertEqual(slots.total(), 4)
                self.assertEqual(self.run_cli(binary, env, "available").stdout.strip(), "4")

        shared_fields = ("v", "jobs", "maximum", "exclusive", "cores", "total", "cores_per_slot", "held", "free",
                         "load", "unleased_load", "capacity", "waiting")
        for extra in ((), ("--max", "2"), ("--max", "3", "--exclusive")):
            with self.subTest(args=extra):
                real_adv = self.json_cli(self.real_bin, self.real_env, "advise", *extra)
                fake_adv = self.json_cli(self.fake_bin, self.fake_env, "advise", *extra)
                for key in shared_fields:
                    self.assertIn(key, real_adv)
                    self.assertIn(key, fake_adv)
                    self.assertEqual(fake_adv[key], real_adv[key], f"advise {' '.join(extra)} field {key}")

        host_info = {"disk_free_gb": 500.0, "load15": 0.0, "cores": 12}
        with mock.patch.dict(os.environ, self.with_slotfather_on_path(self.real_bin, self.real_env, "jobs-real"),
                             clear=True):
            real_decision = host.check_jobs(host_info, minimum=1)
        with mock.patch.dict(os.environ, self.with_slotfather_on_path(self.fake_bin, self.fake_env, "jobs-fake"),
                             clear=True):
            fake_decision = host.check_jobs(host_info, minimum=1)
        for key in ("jobs", "min_jobs", "slot_range", "total", "free", "held"):
            self.assertEqual(fake_decision[key], real_decision[key], key)

    def test_status_and_pid_identity_query_match(self) -> None:
        self.run_cli(self.real_bin, self.real_env, "status")
        self.run_cli(self.fake_bin, self.fake_env, "status")

        real_status = self.json_cli(self.real_bin, self.real_env, "status", "--json")
        fake_status = self.json_cli(self.fake_bin, self.fake_env, "status", "--json")
        for key in ("v", "slots", "held", "free", "draining", "entries", "legacy"):
            self.assertEqual(fake_status[key], real_status[key], key)

        my_pid = os.getpid()
        with mock.patch.dict(os.environ, self.with_slotfather_on_path(self.real_bin, self.real_env, "st-real"),
                             clear=True):
            real_pid_status = slots.request_status(my_pid)
        with mock.patch.dict(os.environ, self.with_slotfather_on_path(self.fake_bin, self.fake_env, "st-fake"),
                             clear=True):
            fake_pid_status = slots.request_status(my_pid)
        assert real_pid_status is not None and fake_pid_status is not None
        self.assertEqual(real_pid_status["query"]["pid"], my_pid)
        self.assertTrue(real_pid_status["query"]["start"])
        self.assertEqual(fake_pid_status["query"], real_pid_status["query"])

    def test_run_lease_env_yield_nested_reuse_and_wait_timeout_match(self) -> None:
        probe_script = (
            "import json, os, subprocess, sys\n"
            "bin_path = sys.argv[1]\n"
            "yield_rc = subprocess.run([bin_path, 'yield'], capture_output=True).returncode\n"
            "nested_rc = subprocess.run([bin_path, 'run', '-n', '1', '--label', 'inner', '--', "
            "sys.executable, '-c', 'import os, sys; sys.exit(0 if os.environ.get(\"SLOTFATHER_LEASE\") else 3)'], "
            "capture_output=True).returncode\n"
            "print(json.dumps({'lease': bool(os.environ.get('SLOTFATHER_LEASE')), "
            "'count': os.environ.get('SLOTFATHER_COUNT'), 'slots': os.environ.get('SLOTFATHER_SLOTS'), "
            "'yield_env': bool(os.environ.get('SLOTFATHER_YIELD')), 'yield_rc': yield_rc, 'nested_rc': nested_rc}))\n"
        )
        for label, binary, env in (("real", self.real_bin, self.real_env), ("fake", self.fake_bin, self.fake_env)):
            with self.subTest(target=label):
                path_env = self.with_slotfather_on_path(binary, env, f"run-{label}")
                with mock.patch.dict(os.environ, path_env, clear=True):
                    argv = slots.wrap(
                        2, "lane-contract", [sys.executable, "-c", probe_script, binary],
                        wait=5, meta={"lane": "a", "phase": "check"}, priority="interactive",
                        yieldable=True, ticket="222", min_count=1,
                    )
                self.assertIn("1-2", argv)
                completed = subprocess.run(argv, cwd=self.tmp, env=path_env, capture_output=True, text=True, timeout=15)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                observed = json.loads(completed.stdout)
                self.assertEqual(observed, {
                    "lease": True,
                    "count": "2",
                    "slots": "2",
                    "yield_env": True,
                    "yield_rc": 0,
                    "nested_rc": 0,
                })
                # Outside a lease, `yield` fails with exit status 2 (or 1 for SlotError).
                outside = subprocess.run([binary, "yield"], cwd=self.tmp, env=path_env, capture_output=True, text=True,
                                         timeout=10)
                self.assertNotEqual(outside.returncode, 0)

        # Saturated `--wait 0` exits 75 (`slots.TIMED_OUT`) without running the child command on both.
        marker = self.tmp / "must-not-run"
        busy_fake_env = {**self.fake_env, "FAKE_SLOTFATHER_BUSY": "1"}
        self.run_cli(self.fake_bin, busy_fake_env, "run", "-n", "1", "--wait", "0", "--",
                     sys.executable, "-c", f"import pathlib; pathlib.Path({str(marker)!r}).touch()",
                     code=slots.TIMED_OUT)
        self.assertFalse(marker.exists())

        one_slot_env = {**self.real_env, "SLOTFATHER_DIR": str(self.tmp / "one-slot-state"),
                        "SLOTFATHER_CORES": "3", "SLOTFATHER_MEMORY_GIB": "16"}
        holder = subprocess.Popen(
            [self.real_bin, "run", "-n", "1", "--label", "holder", "--",
             sys.executable, "-c", "import sys; print('held', flush=True); sys.stdin.read()"],
            cwd=self.tmp, env=one_slot_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(close_process, holder)
        assert holder.stdout is not None and holder.stdin is not None
        self.assertEqual(holder.stdout.readline().strip(), "held")
        self.run_cli(self.real_bin, one_slot_env, "run", "-n", "1", "--wait", "0", "--",
                     sys.executable, "-c", f"import pathlib; pathlib.Path({str(marker)!r}).touch()",
                     code=slots.TIMED_OUT)
        self.assertFalse(marker.exists())
        holder.stdin.close()
        holder.wait(timeout=10)

    def test_sweep_and_procs_stand_ins_match_real_slotfather(self) -> None:
        my_pid = str(os.getpid())
        real_sweep = self.json_cli(self.real_bin, self.real_env, "sweep", "--json", "--no-log", "--pid", my_pid)
        fake_sweep = self.json_cli(self.fake_bin, self.fake_env, "sweep", "--json", "--no-log", "--pid", my_pid)
        for key in ("v", "live", "only", "items", "live_refused", "failed"):
            self.assertEqual(fake_sweep[key], real_sweep[key], key)

        if not FIXTURE_PROCS.is_file():
            return
        procs_bin = str(FIXTURE_PROCS)
        procs_env = {**self.real_env, "FIXTURE_PROCS_ROOT": my_pid}
        procs_sweep = self.json_cli(procs_bin, procs_env, "sweep", "--json", "--no-log", "--pid", my_pid)
        self.assertEqual((procs_sweep["v"], procs_sweep["items"]), (real_sweep["v"], real_sweep["items"]))

        worktree = self.tmp / "worktree"
        worktree.mkdir()
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
            cwd=worktree, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(close_process, child)
        assert child.stdout is not None and child.stdin is not None
        self.assertEqual(child.stdout.readline().strip(), "ready")

        real_pid = self.json_cli(self.real_bin, procs_env, "procs", "--json", "--pid", str(child.pid))
        fake_pid = self.json_cli(procs_bin, procs_env, "procs", "--json", "--pid", str(child.pid))
        self.assertEqual((fake_pid["v"], fake_pid["cwd_unknown"], fake_pid["lease_unknown"]),
                         (real_pid["v"], real_pid["cwd_unknown"], real_pid["lease_unknown"]))
        self.assertEqual(len(real_pid["processes"]), 1)
        self.assertEqual(len(fake_pid["processes"]), 1)
        for field in ("pid", "ppid", "uid", "start"):
            self.assertEqual(fake_pid["processes"][0][field], real_pid["processes"][0][field], field)

        real_cwd = self.json_cli(self.real_bin, procs_env, "procs", "--json", "--cwd", str(worktree))
        fake_cwd = self.json_cli(procs_bin, procs_env, "procs", "--json", "--cwd", str(worktree))
        self.assertEqual(fake_cwd["v"], real_cwd["v"])
        real_by_pid = {row["pid"]: row for row in real_cwd["processes"]}
        fake_by_pid = {row["pid"]: row for row in fake_cwd["processes"]}
        self.assertIn(child.pid, real_by_pid)
        self.assertIn(child.pid, fake_by_pid)
        for field in ("pid", "ppid", "uid", "start", "cwd"):
            self.assertEqual(fake_by_pid[child.pid][field], real_by_pid[child.pid][field], field)

        with mock.patch.dict(os.environ, procs_env, clear=True):
            real_source = procs.SlotfatherSource(self.real_bin)
            fake_source = procs.SlotfatherSource(procs_bin)
            real_source.pause = 0
            fake_source.pause = 0
            self.assertTrue(real_source.supported())
            self.assertTrue(fake_source.supported())
            real_stop, real_held = real_source.sort_for_removal(str(worktree))
            fake_stop, fake_held = fake_source.sort_for_removal(str(worktree))
            self.assertEqual((real_stop, fake_stop), ([], []))
            self.assertTrue(real_held and fake_held)

            child.stdin.close()
            child.wait(timeout=10)
            self.assertEqual(real_source.sort_for_removal(str(worktree)), ([], []))
            self.assertEqual(fake_source.sort_for_removal(str(worktree)), ([], []))

    def test_lane_admission_v1_contract_against_real_slotfather(self) -> None:
        cfg_path = self.tmp / ".lanefather.toml"
        cfg_path.write_text(f'project = "Contract"\nstate_dir = "{self.tmp / "lanes"}"\n'
                            '[admission]\nprovider = "slotfather"\n')
        queue_path = self.tmp / "queue" / "queue.toml"
        queue_path.parent.mkdir(parents=True)
        queue_path.write_text('[[lane]]\nid = "next"\nticket = 1\nclaims = ["src/**"]\n')
        adm_env = self.with_slotfather_on_path(
            self.real_bin,
            {**self.real_env, "LANEFATHER_CORES": "12", "LANEFATHER_MEMORY_GB": "32", "LANEFATHER_LOAD15": "0.5",
             "LANEFATHER_DISK_FREE_GB": "500"},
            "adm-real",
        )
        project = config.load(cfg_path)
        with mock.patch.dict(os.environ, adm_env, clear=True):
            answer = admission.ask(project, 0)
            self.assertEqual((answer["v"], answer["kind"], answer["cap"], answer["derived_cap"], answer["cap_source"]),
                             (1, "lane-lifetime", 6, 6, "derived"))
            readiness = Project(project).readiness()
            self.assertTrue(readiness.can_start, readiness.blocked)
            self.assertEqual(readiness.cap, 6)

        with mock.patch.dict(os.environ, {**adm_env, "LANEFATHER_CAP": "2"}, clear=True):
            capped = Project(project).readiness()
            self.assertEqual(capped.cap, 2)
            self.assertIn("LANEFATHER_CAP", capped.cap_source)

        with mock.patch.dict(os.environ, {**adm_env, "LANEFATHER_CAP": "typo"}, clear=True):
            fallback = admission.ask(project, 0)
            self.assertEqual((fallback["cap"], fallback["cap_source"], fallback["warnings"]),
                             (6, "derived", ["invalid_cap_override"]))


if __name__ == "__main__":
    unittest.main()
