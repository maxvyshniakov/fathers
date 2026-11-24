"""Tests for lanefather.verify: one verify runner for check, post-merge, rerun, base rerun, gate and perf (fathers#70)."""
from __future__ import annotations

import ast
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any
import unittest
import unittest.mock

from support import FAKE_PLAN, FAKE_VERIFY, LaneCase, ScratchCase, close_process

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent / "lanefather"

CAPTURE_ENV_SNIPPET = r'''
if args[:1] != ["--changed"] and os.environ.get("CAPTURE_VERIFY_CALLS"):
    prefix = os.environ.get("CAPTURE_SLOT_PREFIX", "LANEFATHER_SLOT")
    slot_vars = {k: os.environ[k] for k in sorted(os.environ) if k.startswith(prefix + "_")}
    with open(os.environ["CAPTURE_VERIFY_CALLS"], "a") as out:
        out.write(json.dumps({
            "argv": [os.path.basename(sys.argv[0]), *args],
            "env": slot_vars,
            "leased": bool(os.environ.get("SLOTFATHER_LEASE")),
        }) + "\n")
'''


def _contains_verify_path(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id == "verify_path":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "verify_path":
                return True
    return False


def _is_subprocess_exec(call: ast.Call) -> bool:
    func = call.func
    return (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and func.attr in ("run", "Popen", "call", "check_call", "check_output"))


class ArchitectureGuardTests(unittest.TestCase):
    def test_no_subprocess_call_of_verify_path_outside_verify_module(self):
        offenders: list[str] = []
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            if path.name == "verify.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_subprocess_exec(node):
                    continue
                if not node.args or not _contains_verify_path(node.args[0]):
                    continue
                # plan.py invokes verify --changed --base <base> --plan --json to read the plan, not to run checks.
                if path.name == "plan.py" and isinstance(node.args[0], ast.List):
                    literals = {elt.value for elt in node.args[0].elts
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}
                    if {"--plan", "--json"}.issubset(literals):
                        continue
                offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [], f"subprocess call of verify_path outside verify.py: {offenders}")

    def test_review_does_not_import_checks(self):
        for mod_name in ("review.py", "land.py"):
            path = PACKAGE_DIR / mod_name
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported_checks: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "checks" or alias.name.endswith(".checks"):
                            imported_checks.append(f"{mod_name}:{node.lineno}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "checks" or (node.module or "").endswith(".checks"):
                        imported_checks.append(f"{mod_name}:{node.lineno}: from {node.module} import ...")
                    for alias in node.names:
                        if alias.name == "checks":
                            imported_checks.append(f"{mod_name}:{node.lineno}: from {node.module} import checks")
            self.assertEqual(imported_checks, [])


class VerifyUnitTests(unittest.TestCase):
    def test_selection_argv(self):
        from lanefather import verify
        self.assertEqual(verify.Selection.full().argv(), ())
        self.assertEqual(verify.Selection.suites(["lib", "perf"]).argv(), ("--suites", "lib,perf"))
        self.assertEqual(verify.Selection.checks(["a", "b"]).argv(), ("--checks", "a,b"))

    def test_terminable_restores_handler_and_exits_on_sigterm(self):
        from lanefather import verify
        previous = signal.getsignal(signal.SIGTERM)
        with verify.terminable():
            handler = signal.getsignal(signal.SIGTERM)
            self.assertNotEqual(handler, previous)
            assert callable(handler)
            with self.assertRaises(SystemExit) as ctx:
                handler(signal.SIGTERM, None)
            self.assertEqual(ctx.exception.code, 128 + int(signal.SIGTERM))
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_lease_timeout_raises_lease_timed_out(self):
        import shutil
        import tempfile

        from lanefather import config, slots, state, verify
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="test-timeout-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        store = state.Store(config.Config(project="Test"), directory=scratch / "state")
        with (unittest.mock.patch.object(slots, "command", return_value="/bin/slotfather"),
              unittest.mock.patch.object(slots, "leased", return_value=False),
              unittest.mock.patch.object(verify, "tree_of", return_value=None),
              unittest.mock.patch.object(verify.subprocess, "Popen") as popen_mock):
            proc = unittest.mock.MagicMock()
            proc.wait.return_value = slots.TIMED_OUT
            proc.poll.return_value = slots.TIMED_OUT
            popen_mock.return_value = proc
            run = verify.Run(
                scratch, verify.Selection.checks(["lib-check"]), "lane-a-alone", "rerun", 2,
                store=store,
                meta={"lane": "a", "check": "lib-check"}, ticket="42",
                run_type={"plan": "abc", "checks": "1"},
                lease=verify.Lease(wait=12.0, kind="rerun"),
                wait_for_load=False, stoppable=True,
            )
            with self.assertRaisesRegex(verify.LeaseTimedOut, r"no lease of 2 slot\(s\) within 12 s"):
                run.execute()


class VerifyCallSitePinTests(LaneCase):
    def setUp(self):
        super().setUp()
        self.calls_file = self.tmp / "captured-verify-calls.jsonl"
        self.env["CAPTURE_VERIFY_CALLS"] = str(self.calls_file)
        verifier = FAKE_VERIFY.replace("args = sys.argv[1:]\n", "args = sys.argv[1:]\n" + CAPTURE_ENV_SNIPPET)
        self.write(self.integration, "verify.sh", verifier).chmod(0o755)
        self.commit(self.integration, {}, "capture verify argv and slot env")

    def captured(self) -> list[dict[str, Any]]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def slot_calls(self) -> list[list[str]]:
        log = self.tmp / "slotfather.log"
        if not log.exists():
            return []
        return [json.loads(line)["args"] for line in log.read_text().splitlines()
                if json.loads(line)["command"] == "run"]

    def test_pins_argv_and_slot_env_across_all_six_verify_call_sites(self):
        from lanefather import results

        # 1. check (selected checks) + 2. post-merge smoke
        wt = self.created("a", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        check_call = self.captured()[-1]
        self.assertEqual(check_call["argv"][:3], ["verify.sh", "--checks", "structure-check,lib-check"])
        self.assertEqual(check_call["argv"][3:5], ["--keep-going", "--jobs"])
        self.assertEqual(check_call["env"]["LANEFATHER_SLOT_LABEL"], "lane-a")
        self.assertEqual(check_call["env"]["LANEFATHER_SLOT_PRIORITY"], "normal")
        self.assertEqual(check_call["env"]["LANEFATHER_SLOT_TICKET"], "404")
        self.assertEqual(check_call["env"]["LANEFATHER_SLOT_YIELDABLE"], "")
        check_meta = json.loads(check_call["env"]["LANEFATHER_SLOT_META"])
        self.assertEqual(check_meta["phase"], "check")
        self.assertEqual(check_meta["lane"], "a")
        self.assertEqual(check_meta["plan"], results.plan_hash(["structure-check", "lib-check"]))
        self.assertEqual(check_meta["checks"], "2")
        self.assertTrue(check_meta["run"])
        self.assertTrue(check_meta["tree"])

        self.integration_moves()
        self.lane("land", "a")
        smoke_call = self.captured()[-1]
        self.assertEqual(smoke_call["argv"][:3], ["verify.sh", "--checks", "structure-check,lib-check"])
        self.assertEqual(smoke_call["argv"][3:5], ["--keep-going", "--jobs"])
        self.assertEqual(smoke_call["env"]["LANEFATHER_SLOT_LABEL"], "post-merge-a")
        self.assertEqual(smoke_call["env"]["LANEFATHER_SLOT_PRIORITY"], "interactive")
        self.assertEqual(smoke_call["env"]["LANEFATHER_SLOT_TICKET"], "404")
        smoke_meta = json.loads(smoke_call["env"]["LANEFATHER_SLOT_META"])
        self.assertEqual(smoke_meta["phase"], "post-merge")
        self.assertEqual(smoke_meta["lane"], "a")
        self.assertEqual(smoke_meta["plan"], results.plan_hash(["structure-check", "lib-check"]))
        self.assertEqual(smoke_meta["checks"], "2")

        # 3. rerun (alone) + 4. base rerun (regression triggers both alone and base reruns)
        wt_b = self.created("b", "native/**")
        self.commit(wt_b, {"native/straylight-core/corpus.txt": "bad\n"})
        self.write(wt_b, "checks/native.sh", "#!/usr/bin/env bash\nexit 1\n").chmod(0o755)
        self.commit(wt_b, {}, "break native check")
        self.lane("check", "b", ok=False)
        calls = self.captured()
        alone_call = next(c for c in calls if c["env"]["LANEFATHER_SLOT_LABEL"] == "lane-b-alone")
        base_call = next(c for c in calls if c["env"]["LANEFATHER_SLOT_LABEL"] == "lane-b-base")

        self.assertEqual(alone_call["argv"][:3], ["verify.sh", "--checks", "native-check"])
        self.assertEqual(alone_call["argv"][3:5], ["--keep-going", "--jobs"])
        self.assertEqual(alone_call["env"]["LANEFATHER_SLOT_PRIORITY"], "interactive")
        self.assertEqual(alone_call["env"]["LANEFATHER_SLOT_TICKET"], "404")
        alone_meta = json.loads(alone_call["env"]["LANEFATHER_SLOT_META"])
        self.assertEqual(alone_meta["phase"], "rerun")
        self.assertEqual(alone_meta["check"], "native-check")
        self.assertEqual(alone_meta["lane"], "b")
        self.assertEqual(alone_meta["plan"], results.plan_hash(["native-check"]))
        self.assertEqual(alone_meta["checks"], "1")
        # Both alone and base reruns are wrapped in `slotfather run`
        wrapped = self.slot_calls()
        wrapped_labels = [args[args.index("--label") + 1] for args in wrapped]
        self.assertIn("lane-b-alone", wrapped_labels)
        self.assertIn("lane-b-base", wrapped_labels)

        self.assertEqual(base_call["argv"][:3], ["verify.sh", "--checks", "native-check"])
        self.assertEqual(base_call["argv"][3:5], ["--keep-going", "--jobs"])
        self.assertEqual(base_call["env"]["LANEFATHER_SLOT_PRIORITY"], "interactive")
        base_meta = json.loads(base_call["env"]["LANEFATHER_SLOT_META"])
        self.assertEqual(base_meta["phase"], "rerun")
        self.assertEqual(base_meta["on"], "base")
        self.assertEqual(base_meta["check"], "native-check")
        self.assertEqual(base_meta["plan"], results.plan_hash(["native-check"]))
        self.assertEqual(base_meta["checks"], "1")

        # 5. batch gate (full gate: no --checks or --suites)
        self.git(self.repo, "branch", "dev")
        self.lane("review", "a")
        tip = self.tip()
        self.lane("promote", tip, "--no-push", "--wait", "0")
        gate_call = self.captured()[-1]
        self.assertEqual(gate_call["argv"][:3], ["verify.sh", "--keep-going", "--jobs"])
        self.assertEqual(gate_call["env"]["LANEFATHER_SLOT_LABEL"], f"promote-{tip[:10]}")
        self.assertEqual(gate_call["env"]["LANEFATHER_SLOT_PRIORITY"], "interactive")
        self.assertEqual(gate_call["env"]["LANEFATHER_SLOT_TICKET"], "")
        gate_meta = json.loads(gate_call["env"]["LANEFATHER_SLOT_META"])
        self.assertEqual(gate_meta["phase"], "gate")
        self.assertEqual(gate_meta["sha"], tip[:10])
        self.assertEqual(gate_meta["plan"], results.plan_hash("full"))
        self.assertEqual(gate_meta["checks"], "5")

        # 6. perf suite (--suites perf --keep-going --jobs 1)
        self.commit(self.integration, {"tools/verification_plan.py": FAKE_PLAN + 'PERF = "perf"\n'}, "perf suite")
        perf_sha = self.tip()
        self.lane("perf", perf_sha)
        perf_call = self.captured()[-1]
        self.assertEqual(perf_call["argv"], ["verify.sh", "--suites", "perf", "--keep-going", "--jobs", "1"])
        self.assertEqual(perf_call["env"]["LANEFATHER_SLOT_LABEL"], f"perf-{perf_sha[:10]}")
        self.assertEqual(perf_call["env"]["LANEFATHER_SLOT_PRIORITY"], "batch")
        self.assertEqual(perf_call["env"]["LANEFATHER_SLOT_TICKET"], "")
        perf_meta = json.loads(perf_call["env"]["LANEFATHER_SLOT_META"])
        self.assertEqual(perf_meta["phase"], "perf")
        self.assertEqual(perf_meta["sha"], perf_sha[:10])
        self.assertEqual(perf_meta["plan"], results.plan_hash(["suite:perf"]))
        self.assertEqual(perf_meta["checks"], "0")

    def test_check_stages_prepare_select_run_record_accept_explicit_values(self):
        from lanefather import checks, config, state
        wt = self.created("stages", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\nbar\n"})
        with unittest.mock.patch.dict(os.environ, self.env, clear=False):
            cfg = config.load(config.find(wt))
            store = state.Store(cfg, directory=self.state)
            prepared = checks.prepare(store, "stages", agent="codex")
            self.assertEqual(prepared.record["id"], "stages")
            selection = checks.select(store, prepared, rerun=False, slots_override=1)
            assert selection is not None
            self.assertEqual(selection.decision, {"jobs": 1, "override": "--slots"})
            executed = checks.run(store, prepared, selection)
            rc = checks.record(store, prepared, selection, executed)
            self.assertEqual(rc, 0)
            receipt: dict[str, Any] = json.loads((self.state / "stages.receipt.json").read_text())
            self.assertEqual(receipt["status"], "green")
            self.assertEqual(receipt["agent"], "codex")
            # Re-selecting the same green tree short-circuits to None.
            again = checks.prepare(store, "stages")
            self.assertIsNone(checks.select(store, again, rerun=False, slots_override=None))


class VerifyProgressHeartbeatTests(LaneCase):
    """fathers#190: bounded progress heartbeats during quiet verify runs, held children, and Slotfather queue waits."""

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def test_quiet_active_fixture_without_newlines_emits_heartbeats_and_preserves_receipt(self):
        self.env["LANEFATHER_VERIFY_HEARTBEAT"] = "0.1"
        # A streaming verifier that writes dots without newlines across several heartbeat intervals while working.
        verifier = (self.integration / "verify.sh").read_text().replace(
            '    with open(check_log, "w") as out:\n'
            '        code = subprocess.call(["bash", str(script)], cwd=root, stdout=out, stderr=subprocess.STDOUT) if script.exists() else 0\n'
            '    sys.stdout.write(check_log.read_text())',
            '    with open(check_log, "w") as out:\n'
            '        if script.exists():\n'
            '            with subprocess.Popen(["bash", str(script)], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:\n'
            '                assert proc.stdout is not None\n'
            '                while True:\n'
            '                    ch = proc.stdout.read(1)\n'
            '                    if not ch:\n'
            '                        break\n'
            '                    text = ch.decode(errors="replace")\n'
            '                    out.write(text); out.flush()\n'
            '                    sys.stdout.write(text); sys.stdout.flush()\n'
            '                code = proc.wait()\n'
            '            sys.stdout.write("\\n"); sys.stdout.flush()\n'
            '        else:\n'
            '            code = 0',
        )
        script = (
            "python3 - <<'EOF'\n"
            "import sys, time\n"
            "for _ in range(6):\n"
            "    _ = sum(range(150000))\n"
            "    sys.stdout.write('.')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.08)\n"
            "EOF\n"
        )
        self.commit(self.integration, {"verify.sh": verifier, "checks/lib.sh": script},
                    "quiet active dots without newlines")
        wt = self.created("quiet", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\nbaz\n"})
        output = self.lane("check", "quiet")
        heartbeats = [line for line in output.splitlines() if "[active]" in line]
        self.assertGreaterEqual(len(heartbeats), 2, output)
        for line in heartbeats:
            self.assertIn("lane-quiet [active]", line)
            self.assertIn("cpu ", line)
            self.assertNotIn("; test ", line)
        self.assertTrue(any("no newline for" in line for line in heartbeats), heartbeats)
        receipt = self.receipt("quiet")
        self.assertEqual(receipt["status"], "green")
        self.assertEqual(receipt["check_results"]["lib-check"]["status"], "passed")

    def test_held_child_reports_stale_output_age_and_activity_without_fabricating_test_name(self):
        self.env["LANEFATHER_VERIFY_HEARTBEAT"] = "0.1"
        hold = self.tmp / "child-hold"
        started = self.tmp / "child-hold.started"
        script = (
            "echo 'lib-check'\n"
            "python3 - <<EOF\n"
            "import pathlib, time\n"
            f"pathlib.Path({str(started)!r}).write_text('ok')\n"
            f"while not pathlib.Path({str(hold)!r}).exists():\n"
            "    time.sleep(0.05)\n"
            "EOF\n"
        )
        self.commit(self.integration, {"checks/lib.sh": script}, "held child check")
        wt = self.created("held", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\nbaz\n"})
        proc = subprocess.Popen(
            [sys.executable, "-m", "lanefather", "check", "held"],
            cwd=wt, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(close_process, proc)
        self.addCleanup(hold.touch)
        deadline = time.monotonic() + 30
        while not started.exists():
            self.assertIsNone(proc.poll(), "lanefather check exited before held child started")
            self.assertLess(time.monotonic(), deadline, "held child did not start")
            time.sleep(0.05)
        time.sleep(0.35)  # sleep-ok: let >=2 heartbeat intervals (0.1s) elapse while held child runs
        hold.touch()
        output, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, output)
        heartbeats = [line for line in output.splitlines() if "lane-held [active]" in line]
        self.assertGreaterEqual(len(heartbeats), 2, output)
        self.assertTrue(any("last output " in line and "child" in line for line in heartbeats), heartbeats)
        for line in heartbeats:
            self.assertNotIn("; test ", line)
        self.assertEqual(self.receipt("held")["status"], "green")

    def test_queued_check_shows_slotfather_queue_wait_as_its_own_state(self):
        self.env["LANEFATHER_VERIFY_HEARTBEAT"] = "0.1"
        queue_hold = self.tmp / "queue-hold"
        self.env["FAKE_SLOTFATHER_QUEUE_HOLD"] = str(queue_hold)
        # Make verify.sh wrap its check execution under `slotfather run` like a real queued verifier.
        verifier = (self.integration / "verify.sh").read_text().replace(
            '        code = subprocess.call(["bash", str(script)], cwd=root, stdout=out, stderr=subprocess.STDOUT) if script.exists() else 0',
            '        code = subprocess.call(["slotfather", "run", "-n", "1", "--label", os.environ.get("LANEFATHER_SLOT_LABEL", "check"), "--", "bash", str(script)], cwd=root, stdout=out, stderr=subprocess.STDOUT) if script.exists() else 0',
        )
        self.commit(self.integration, {"verify.sh": verifier}, "queue check via slotfather run")
        wt = self.created("queued", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\nbaz\n"})
        proc = subprocess.Popen(
            [sys.executable, "-m", "lanefather", "check", "queued"],
            cwd=wt, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(close_process, proc)
        self.addCleanup(queue_hold.touch)
        waiting_marker = pathlib.Path(str(queue_hold) + ".waiting")
        deadline = time.monotonic() + 30
        while not waiting_marker.exists():
            self.assertIsNone(proc.poll(), "lanefather check exited before entering slotfather queue")
            self.assertLess(time.monotonic(), deadline, "queued check did not enter slotfather")
            time.sleep(0.05)
        time.sleep(0.45)  # sleep-ok: let >=2 heartbeat intervals (0.1s) elapse while queued in slotfather
        queue_hold.touch()
        output, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, output)
        queued_lines = [line for line in output.splitlines() if "lane-queued [queued]" in line]
        self.assertGreaterEqual(len(queued_lines), 2, output)
        self.assertTrue(any("slotfather waiting" in line for line in queued_lines), queued_lines)
        self.assertEqual(self.receipt("queued")["status"], "green")


class ProgressTrackerUnitTests(ScratchCase):
    def test_tracker_identifies_setup_state_and_observed_unittest_name_without_fabricating(self) -> None:
        from lanefather import verify
        log = self.tmp / "progress.log"
        tracker = verify._ProgressTracker(log, verify.Selection.checks(["lib-check", "native-check"]))  # pyright: ignore[reportPrivateUsage]
        tracker.reset(10.0)
        log.write_bytes(b"setup: installing dependencies\n")
        tracker.poll(10.5)
        self.assertTrue(tracker.in_setup)
        self.assertIsNone(tracker.observed_test)
        with open(log, "ab") as handle:
            handle.write(b"==> lib-check\ntest_slow (test_mod.SlowTests) ... ")
        tracker.poll(11.0)
        self.assertFalse(tracker.in_setup)
        self.assertEqual(tracker.active_target, "lib-check")
        self.assertEqual(tracker.observed_test, "test_slow (test_mod.SlowTests)")
        with open(log, "ab") as handle:
            handle.write(b"ok\n....")
        tracker.poll(11.5)
        self.assertIsNone(tracker.observed_test)


class PureClassificationTableTests(unittest.TestCase):
    """Zero-I/O table tests for pure classification planning and evaluation (fathers#234)."""

    def test_pure_classification_transition_table_zero_io(self) -> None:
        from lanefather import verify

        base_sha = "1234567890abcdef" * 2 + "12345678"
        load_sample: dict[str, Any] = {"first_run": [0.1, 0.2, 0.3], "alone": [0.2, 0.3, 0.4]}
        log_path = pathlib.Path("/tmp/rerun.log")

        with (
            unittest.mock.patch.object(subprocess, "run", side_effect=AssertionError("zero I/O violated")),
            unittest.mock.patch.object(subprocess, "Popen", side_effect=AssertionError("zero I/O violated")),
            unittest.mock.patch.object(os.path, "isdir", side_effect=AssertionError("zero I/O violated")),
        ):
            # 0. Green run returns None.
            green_fresh: list[dict[str, Any]] = [{"suite": "lib", "status": "passed", "failed_checks": []}]
            self.assertIsNone(verify.plan_classification(green_fresh, {"checks": []}, cap=3, only_supported=False))

            # 1. Environment failure returns a skipped classification dict with green=False.
            env_report: dict[str, Any] = {
                "environment": {"class": "rust-drift", "detail": "rustc changed"},
                "checks": [{"id": "native-check", "status": "failed"}],
            }
            env_fresh: list[dict[str, Any]] = [
                {"suite": "native", "status": "failed", "failed_checks": ["native-check"], "environment": "rust-drift"}
            ]
            env_out = verify.plan_classification(env_fresh, env_report, cap=3, only_supported=False)
            assert isinstance(env_out, dict)
            self.assertIn("environment: rust-drift", str(env_out.get("skipped") or ""))
            self.assertFalse(env_out["green"])

            # 2. First-tier stopped (#639): skips alone rerun (`still_red` populated immediately);
            #    pre-existing base failure makes classification `green=True` and `first=True`.
            stop_report: dict[str, Any] = {
                "stopped": {"failed": ["structure-check"], "not_run": 2},
                "checks": [{"id": "structure-check", "status": "failed"}],
            }
            stop_fresh: list[dict[str, Any]] = [
                {"suite": "structure", "status": "failed", "failed_checks": ["structure-check"]},
                {"suite": "lib", "status": "not_run", "failed_checks": []},
            ]
            stop_plan = verify.plan_classification(stop_fresh, stop_report, cap=3, only_supported=False)
            assert isinstance(stop_plan, verify.ClassificationPlan)
            self.assertTrue(stop_plan.first)
            self.assertEqual(stop_plan.active_rerun, ["structure-check"])
            self.assertEqual(stop_plan.still_red, ["structure-check"])
            self.assertEqual(stop_plan.entries["structure-check"]["alone"]["status"], "not rerun")
            base_pre = verify.evaluate_base_row(
                "structure-check",
                stop_plan.entries["structure-check"],
                base=base_sha,
                present=True,
                row={"status": "failed", "exit": 1},
                base_log=log_path,
            )
            self.assertEqual(base_pre["class"], verify.CheckClass.PRE_EXISTING)
            stop_out = verify.finalize_classification(
                base_sha,
                stop_plan,
                {"structure-check": base_pre},
                load=load_sample,
                runs=[],
            )
            self.assertTrue(stop_out["first"])
            self.assertTrue(stop_out["green"])

            # 3. Reruns cap + dependency-blocked (#195) + --only (#147) outside_failed_tests on initial report.
            mixed_report: dict[str, Any] = {
                "capabilities": ["only"],
                "checks": [
                    {"id": "c1", "status": "failed", "failed_tests": ["t1.ts"]},
                    {"id": "c2", "status": "failed", "failed_tests": ["t2.ts"], "outside_failed_tests": ["leak.ts"]},
                    {"id": "c3", "status": "failed", "failed_tests": ["t3.ts"]},
                    {"id": "c4", "status": "blocked", "blocked_by": ["c1"]},
                ],
            }
            mixed_fresh: list[dict[str, Any]] = [{
                "suite": "verify",
                "status": "failed",
                "failed_checks": ["c1", "c2", "c3"],
                "blocked_checks": ["c4"],
            }]
            mixed_plan = verify.plan_classification(mixed_fresh, mixed_report, cap=2, only_supported=True)
            assert isinstance(mixed_plan, verify.ClassificationPlan)
            self.assertEqual(mixed_plan.active_rerun, ["c1"])
            self.assertEqual(mixed_plan.alone_tests, ["t1.ts"])
            self.assertEqual(mixed_plan.blocked_rows, {"c4": ["c1"]})
            self.assertEqual(mixed_plan.entries["c2"]["class"], verify.CheckClass.UNCLASSIFIED)
            self.assertIn("outside failed_tests", str(mixed_plan.entries["c2"]["reason"]))
            self.assertEqual(mixed_plan.entries["c3"]["class"], verify.CheckClass.UNCLASSIFIED)
            self.assertIn("only the first 2", str(mixed_plan.entries["c3"]["reason"]))

            # 4. Alone row transitions: passed -> flaky-under-load; blocked -> dependency-blocked;
            #    outside_failed_tests -> unclassified; problem (timeout) -> unclassified; failed -> needs_base=True;
            #    unrun -> unclassified.
            first_row: dict[str, Any] = {"id": "c1", "status": "failed"}
            entry_flaky, need_flaky = verify.evaluate_alone_row(
                "c1",
                {"status": "passed", "exit": 0},
                first_row=first_row,
                log=log_path,
                problem=None,
                load=load_sample,
                check_tests=["t1.ts"],
            )
            self.assertFalse(need_flaky)
            self.assertEqual(entry_flaky["class"], verify.CheckClass.FLAKY_UNDER_LOAD)
            self.assertEqual(entry_flaky["tests"], ["t1.ts"])

            entry_blk, need_blk = verify.evaluate_alone_row(
                "c1",
                {"status": "blocked", "blocked_by": ["build"]},
                first_row=first_row,
                log=log_path,
                problem=None,
                load=load_sample,
            )
            self.assertFalse(need_blk)
            self.assertEqual(entry_blk["class"], verify.CheckClass.DEPENDENCY_BLOCKED)

            entry_out, need_out = verify.evaluate_alone_row(
                "c1",
                {"status": "failed", "exit": 1, "failed_tests": ["other.ts"]},
                first_row=first_row,
                log=log_path,
                problem=None,
                load=load_sample,
                check_tests=["t1.ts"],
            )
            self.assertFalse(need_out)
            self.assertEqual(entry_out["class"], verify.CheckClass.UNCLASSIFIED)
            self.assertEqual(entry_out["outside_failed_tests"], ["other.ts"])

            entry_timeout, need_timeout = verify.evaluate_alone_row(
                "c1",
                {"status": "not_run"},
                first_row=first_row,
                log=None,
                problem="no lease of 2 slot(s) within 1 s",
                load=load_sample,
            )
            self.assertFalse(need_timeout)
            self.assertEqual(entry_timeout["class"], verify.CheckClass.UNCLASSIFIED)
            self.assertIn("no lease", str(entry_timeout["reason"]))

            entry_unrun, need_unrun = verify.evaluate_alone_row(
                "c1",
                {"status": "unrun", "exit": None},
                first_row=first_row,
                log=log_path,
                problem=None,
                load=load_sample,
            )
            self.assertFalse(need_unrun)
            self.assertEqual(entry_unrun["class"], verify.CheckClass.UNCLASSIFIED)

            entry_fail, need_fail = verify.evaluate_alone_row(
                "c1",
                {"status": "failed", "exit": 1, "failed_tests": ["t1.ts"]},
                first_row=first_row,
                log=log_path,
                problem=None,
                load=load_sample,
                check_tests=["t1.ts"],
            )
            self.assertTrue(need_fail)
            self.assertEqual(entry_fail["alone"]["status"], "failed")

            # 5. Base row transitions: absent -> regression; passed -> regression; failed -> pre-existing;
            #    blocked -> unclassified.
            absent_entry = verify.evaluate_base_row("c1", entry_fail, base=base_sha, present=False)
            self.assertEqual(absent_entry["class"], verify.CheckClass.REGRESSION)
            self.assertEqual(absent_entry["base"]["status"], "absent")

            reg_entry = verify.evaluate_base_row(
                "c1",
                entry_fail,
                base=base_sha,
                present=True,
                row={"status": "passed", "exit": 0},
                base_log=log_path,
            )
            self.assertEqual(reg_entry["class"], verify.CheckClass.REGRESSION)

            pre_entry = verify.evaluate_base_row(
                "c1",
                entry_fail,
                base=base_sha,
                present=True,
                row={"status": "failed", "exit": 1},
                base_log=log_path,
            )
            self.assertEqual(pre_entry["class"], verify.CheckClass.PRE_EXISTING)

            uncls_base = verify.evaluate_base_row(
                "c1",
                entry_fail,
                base=base_sha,
                present=True,
                row={"status": "blocked", "blocked_by": ["fixture-build"]},
                base_log=log_path,
            )
            self.assertEqual(uncls_base["class"], verify.CheckClass.UNCLASSIFIED)
            self.assertIn("blocked by fixture-build", str(uncls_base["reason"]))

            # 6. Unaccounted suite failure keeps classification red even when all classified checks are landable.
            unaccounted_fresh: list[dict[str, Any]] = [
                {"suite": "lib", "status": "failed", "failed_checks": ["c1"]},
                {"suite": "mystery", "status": "failed", "failed_checks": []},
            ]
            unaccounted_plan = verify.plan_classification(
                unaccounted_fresh,
                {"checks": [{"id": "c1", "status": "failed"}]},
                cap=2,
                only_supported=False,
            )
            assert isinstance(unaccounted_plan, verify.ClassificationPlan)
            self.assertEqual(unaccounted_plan.unaccounted, ["mystery"])
            unaccounted_out = verify.finalize_classification(
                base_sha,
                unaccounted_plan,
                {"c1": entry_flaky},
                load=load_sample,
                runs=[],
            )
            self.assertFalse(unaccounted_out["green"])


class DependencyBlockedClassificationTests(unittest.TestCase):
    """fathers#195 regression fixture, pure: one failed prerequisite, three dependency-skipped children (one waits on
    another child) listed before two unrelated executed failures in the same suite, as Sid-Kit's report did."""

    BASE = "abcdef0123456789" * 2 + "abcdef01"
    LOAD: dict[str, Any] = {"first_run": [0.1, 0.2, 0.3], "alone": [0.2, 0.3, 0.4]}
    REPORT: dict[str, Any] = {"checks": [
        {"id": "unit", "status": "failed", "blocked_by": []},
        {"id": "build", "status": "blocked", "blocked_by": ["unit"]},
        {"id": "storybook", "status": "blocked", "blocked_by": ["build"]},
        {"id": "baseline", "status": "skipped", "blocked_by": ["build"]},
        {"id": "control", "status": "failed", "blocked_by": []},
        {"id": "story", "status": "failed", "blocked_by": []},
    ]}
    CHILDREN = ("build", "storybook", "baseline")

    def fresh(self) -> list[dict[str, Any]]:
        from lanefather import verify
        executed = [r["id"] for r in self.REPORT["checks"] if verify.is_executed_failure(r)]
        blocked = [r["id"] for r in self.REPORT["checks"] if verify.is_dependency_blocked(r)]
        return [{"suite": "browser", "status": "failed", "failed_checks": executed, "blocked_checks": blocked}]

    def classify(self, cap: int, base_status: dict[str, str]) -> tuple[Any, dict[str, Any]]:
        from lanefather import verify
        cplan = verify.plan_classification(self.fresh(), self.REPORT, cap=cap, only_supported=False)
        assert isinstance(cplan, verify.ClassificationPlan)
        entries = dict(cplan.entries)
        for check_id in cplan.active_rerun:
            alone, needs_base = verify.evaluate_alone_row(
                check_id, {"status": "failed"}, first_row=verify.report_row(self.REPORT, check_id),
                log="/nonexistent/alone.log", problem=None, load=self.LOAD)
            self.assertTrue(needs_base)
            entries[check_id] = verify.evaluate_base_row(
                check_id, alone, base=self.BASE, present=True, row={"status": base_status[check_id]},
                base_log="/nonexistent/base.log")
        return cplan, verify.finalize_classification(self.BASE, cplan, entries, load=self.LOAD, runs=[])

    def test_only_executed_failures_take_the_rerun_slots(self) -> None:
        cplan, _ = self.classify(2, {"unit": "passed", "control": "failed"})
        self.assertEqual(cplan.candidates, ["unit", "control", "story"])
        self.assertEqual(cplan.active_rerun, ["unit", "control"])
        self.assertEqual(cplan.blocked_rows, {"build": ["unit"], "storybook": ["build"], "baseline": ["build"]})
        self.assertEqual(cplan.entries["story"]["reason"], "only the first 2 red checks rerun")
        self.assertFalse(set(self.CHILDREN) & set(cplan.entries))

    def test_children_stay_dependency_blocked_behind_the_executed_prerequisite(self) -> None:
        from lanefather import verify
        _, out = self.classify(3, {"unit": "passed", "control": "failed", "story": "passed"})
        entries = {e["check"]: e for e in out["checks"]}
        self.assertEqual([e["check"] for e in out["checks"]],
                         ["unit", "control", "story", "build", "storybook", "baseline"])
        self.assertEqual({c: entries[c]["class"] for c in ("unit", "control", "story")},
                         {"unit": "regression", "control": "pre-existing", "story": "regression"})
        for child in self.CHILDREN:
            self.assertEqual(entries[child]["class"], "dependency-blocked")
            self.assertEqual(entries[child]["prerequisites"], ["unit"])
            self.assertNotIn("alone", entries[child])
            self.assertNotIn("base", entries[child])
        self.assertEqual(entries["storybook"]["blocked_by"], ["build"])
        self.assertEqual(entries["storybook"]["reason"], "skipped because dependency failed (build); failed "
                         "prerequisite: unit (regression); not run, so unresolved")
        self.assertFalse(out["green"])
        lines = verify.classification_lines(out)
        self.assertIn("  baseline: dependency-blocked: skipped because dependency failed (build); failed "
                      "prerequisite: unit (regression); not run, so unresolved", lines)
        self.assertIn("lane: 3 dependency-blocked check(s) did not run and stay unresolved behind unit; they used "
                      "no rerun and count as neither passed nor regressed", lines)

    def test_children_keep_the_receipt_red_when_every_executed_failure_is_landable(self) -> None:
        _, out = self.classify(3, {"unit": "failed", "control": "failed", "story": "failed"})
        entries = {e["check"]: e for e in out["checks"]}
        self.assertEqual({entries[c]["class"] for c in ("unit", "control", "story")}, {"pre-existing"})
        self.assertIn("unit (pre-existing)", entries["build"]["reason"])
        self.assertFalse(out["green"])

    def test_prerequisites_follow_blocked_chains_without_looping(self) -> None:
        from lanefather import verify
        graph = {"a": ["b", "x"], "b": ["c", "a"], "c": ["y", "x"]}
        self.assertEqual(verify.failed_prerequisites("a", graph), ["y", "x"])
        self.assertEqual(verify.failed_prerequisites("z", graph), [])
        entry = verify.dependency_blocked_entry("a", [], [])
        self.assertEqual(entry["reason"], "skipped because dependency failed (the report names none); not run, so "
                                          "unresolved")


class UnblockedAfterRerunPureTests(unittest.TestCase):
    """fathers#240 pure helpers: unblocked selection, shared rerun budget, and wave joining."""

    def test_unblocked_checks_only_when_every_prerequisite_passed_alone(self) -> None:
        from lanefather import verify
        cls = {
            "base": "a" * 40,
            "checks": [
                {"check": "A", "class": "flaky-under-load", "alone": {"status": "passed", "log": "/tmp/a.log"}},
                {"check": "D", "class": "regression", "alone": {"status": "failed", "log": "/tmp/d.log"}},
                {"check": "P", "class": "pre-existing", "alone": {"status": "failed", "log": "/tmp/p.log"}},
                verify.dependency_blocked_entry("B", ["A"], ["A"], {"A": "flaky-under-load"}),
                verify.dependency_blocked_entry("C", ["B"], ["A"], {"A": "flaky-under-load"}),
                verify.dependency_blocked_entry("E", ["A", "D"], ["A", "D"], {"A": "flaky-under-load", "D": "regression"}),
                verify.dependency_blocked_entry("F", ["P"], ["P"], {"P": "pre-existing"}),
                verify.dependency_blocked_entry("G", [], []),
            ],
            "unaccounted": [],
            "green": False,
            "runs": [{"run": "r1", "rerun": "alone"}],
        }
        self.assertEqual(verify.unblocked_checks(cls), (["B", "C"], ["A"]))
        self.assertEqual(verify.unblocked_checks({"skipped": "environment", "checks": [], "green": False}), ([], []))
        self.assertEqual(verify.unblocked_checks(None), ([], []))

    def test_plan_classification_honors_remaining_budget_and_reports_total_cap(self) -> None:
        from lanefather import verify
        fresh = [{"suite": "s", "status": "failed", "failed_checks": ["b", "c"], "log": "/tmp/s.log"}]
        report = {"checks": [{"id": "b", "status": "failed"}, {"id": "c", "status": "failed"}]}
        one_left = verify.plan_classification(fresh, report, cap=3, budget=1)
        assert isinstance(one_left, verify.ClassificationPlan)
        self.assertEqual(one_left.active_rerun, ["b"])
        self.assertEqual(one_left.entries["c"]["class"], "unclassified")
        self.assertEqual(one_left.entries["c"]["reason"], "the rerun budget (3) was used by earlier checks")

        none_left = verify.plan_classification(fresh, report, cap=2, budget=0)
        assert isinstance(none_left, verify.ClassificationPlan)
        self.assertEqual(none_left.active_rerun, [])
        self.assertEqual(none_left.entries["b"]["reason"], "the rerun budget (2) was used by earlier checks")
        self.assertEqual(none_left.entries["c"]["reason"], "the rerun budget (2) was used by earlier checks")

    def test_reruns_used_counts_only_executed_reruns(self) -> None:
        from lanefather import verify
        cls = {
            "checks": [
                {"check": "a", "class": "flaky-under-load", "alone": {"status": "passed", "log": "/tmp/a.log"}},
                {"check": "b", "class": "regression", "alone": {"status": "failed"}, "base": {"status": "passed"}},
                {"check": "c", "class": "unclassified", "reason": "only the first 2 red checks rerun"},
                {"check": "c2", "class": "unclassified", "reason": "the rerun budget (2) was used by earlier checks"},
                {"check": "d", "class": "unclassified", "outside_failed_tests": ["t1"], "reason": "outside"},
                verify.dependency_blocked_entry("e", ["a"], ["a"]),
            ],
        }
        self.assertEqual(verify.reruns_used(cls), 2)
        self.assertEqual(verify.reruns_used({"skipped": "env", "checks": []}), 0)
        self.assertEqual(verify.reruns_used(None), 0)
        # #639 first-tier stopped checks skip the solo rerun and do not consume the heavier stage's budget.
        first_tier = {
            "first": True,
            "checks": [
                {"check": "structure", "class": "pre-existing",
                 "alone": {"status": "not rerun", "reason": "first check"}, "base": {"status": "failed"}},
            ],
        }
        self.assertEqual(verify.reruns_used(first_tier), 0)

    def test_joined_results_and_classification_drop_resolved_blocked_checks(self) -> None:
        from lanefather import verify
        first_fresh = [
            {"suite": "core", "status": "failed", "failed_checks": ["A"], "log": "/tmp/run0.log", "checks": ["A"]},
            {"suite": "app", "status": "blocked", "failed_checks": [], "blocked_checks": ["B", "E"],
             "log": "/tmp/run0.log", "checks": ["B", "E"]},
        ]
        second_fresh = [
            {"suite": "app", "status": "passed", "failed_checks": [], "log": "/tmp/run1.log", "checks": ["B"]},
        ]
        joined = verify.joined_results(first_fresh, second_fresh, attempted=["B"])
        by_suite = {r["suite"]: r for r in joined}
        self.assertEqual(by_suite["app"]["status"], "blocked")
        self.assertEqual(by_suite["app"]["blocked_checks"], ["E"])
        self.assertEqual(by_suite["app"]["first_log"], "/tmp/run0.log")

        # When every blocked check in a suite runs and passes, blocked_checks clears and the suite passes.
        all_passed = verify.joined_results(
            [{"suite": "app", "status": "blocked", "failed_checks": [], "blocked_checks": ["B", "C"],
              "log": "/tmp/run0.log", "checks": ["B", "C"]}],
            [{"suite": "app", "status": "passed", "failed_checks": [], "log": "/tmp/run1.log", "checks": ["B", "C"]}],
            attempted=["B", "C"],
        )
        self.assertEqual(all_passed[0]["status"], "passed")
        self.assertNotIn("blocked_checks", all_passed[0])

        # Joining classification when all unblocked checks pass turns green while keeping flaky-under-load visible.
        first_cls = {
            "base": "a" * 40,
            "checks": [
                {"check": "A", "class": "flaky-under-load", "load": {"first_run": [1, 1, 1], "alone": [1, 1, 1]},
                 "alone": {"status": "passed", "log": "/tmp/a.log"}},
                verify.dependency_blocked_entry("B", ["A"], ["A"], {"A": "flaky-under-load"}),
            ],
            "unaccounted": [],
            "green": False,
            "runs": [{"run": "r1", "rerun": "alone"}],
        }
        merged_green = verify.joined_classification(first_cls, None, resolved=["B"])
        self.assertTrue(merged_green["green"])
        self.assertEqual([e["check"] for e in merged_green["checks"]], ["A"])
        self.assertEqual(merged_green["runs"], [{"run": "r1", "rerun": "alone"}])

        # Environment failure in wave 2 keeps classification red with skipped set AND keeps Wave 1's blocked entries.
        merged_env = verify.joined_classification(
            first_cls, {"skipped": "environment: disk", "checks": [], "green": False}, resolved=["B"],
        )
        self.assertFalse(merged_env["green"])
        self.assertEqual(merged_env["skipped"], "environment: disk")
        self.assertEqual([e["check"] for e in merged_env["checks"]], ["A", "B"])
        env_lines = "\n".join(verify.classification_lines(merged_env))
        self.assertIn("environment: disk", env_lines)
        self.assertIn("A: flaky-under-load", env_lines)

        # Symmetry: skipped in first_cls stays skipped when joined with a non-skipped second_cls.
        sym = verify.joined_classification(
            {"skipped": "environment: oom", "checks": [], "green": False, "unaccounted": [], "runs": []},
            {"base": "a" * 40, "checks": [{"check": "X", "class": "flaky-under-load"}],
             "unaccounted": [], "green": True, "runs": [{"run": "r2", "rerun": "alone"}]},
        )
        self.assertFalse(sym["green"])
        self.assertEqual(sym["skipped"], "environment: oom")

    def test_joined_results_preserves_whole_suite_runs_logs_and_failure_exit(self) -> None:
        from lanefather import verify
        # Whole-suite row (no "checks") joined with a targeted unblocked wave stays whole-suite.
        whole_wave1 = [{"suite": "app", "status": "blocked", "exit": 1, "failed_checks": [], "blocked_checks": ["B"],
                        "log": "/tmp/w1.log", "run": "r1", "lease": "l1"}]
        whole_wave2 = [{"suite": "app", "status": "passed", "exit": 0, "failed_checks": [], "checks": ["B"],
                        "log": "/tmp/w2.log", "run": "r2", "lease": "l2"}]
        whole_merged = verify.joined_results(whole_wave1, whole_wave2, attempted=["B"])
        self.assertNotIn("checks", whole_merged[0])
        self.assertEqual(whole_merged[0]["status"], "passed")
        self.assertEqual(verify.leases_of(whole_merged), [
            {"run": "r1", "lease": "l1"},
            {"run": "r2", "lease": "l2"},
        ])

        # 3-wave chain in one suite preserves all runs/leases, all logs, and a failing exit/log on the failed suite.
        w1 = [{"suite": "chain", "status": "failed", "exit": 1, "failed_checks": ["A"], "blocked_checks": ["B", "C"],
               "log": "/tmp/w1.log", "run": "run1", "lease": "lease1", "checks": ["A", "B", "C"]}]
        w2 = [{"suite": "chain", "status": "failed", "exit": 2, "failed_checks": ["B"], "blocked_checks": ["C"],
               "log": "/tmp/w2.log", "run": "run2", "lease": "lease2", "checks": ["B", "C"]}]
        w3 = [{"suite": "chain", "status": "passed", "exit": 0, "failed_checks": [],
               "log": "/tmp/w3.log", "run": "run3", "lease": "lease3", "checks": ["C"]}]
        m12 = verify.joined_results(w1, w2, attempted=["B", "C"])
        m123 = verify.joined_results(m12, w3, attempted=["C"])
        row = m123[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["exit"], 2)
        self.assertEqual(row["log"], "/tmp/w2.log")
        self.assertEqual(row["first_log"], "/tmp/w1.log")
        self.assertEqual(row["logs"], ["/tmp/w1.log", "/tmp/w2.log", "/tmp/w3.log"])
        self.assertEqual(verify.leases_of(m123, {"runs": [{"run": "alone1", "rerun": "alone"}]}), [
            {"run": "run1", "lease": "lease1"},
            {"run": "run2", "lease": "lease2"},
            {"run": "run3", "lease": "lease3"},
            {"run": "alone1", "rerun": "alone"},
        ])

    def test_joined_classification_recomputes_prerequisites_on_still_blocked_checks(self) -> None:
        from lanefather import verify
        first_cls = {
            "base": "a" * 40,
            "checks": [
                {"check": "A", "class": "flaky-under-load", "alone": {"status": "passed", "log": "/tmp/a.log"}},
                {"check": "D", "class": "regression", "alone": {"status": "failed"}, "base": {"status": "passed"}},
                verify.dependency_blocked_entry("B", ["A"], ["A"], {"A": "flaky-under-load"}),
                verify.dependency_blocked_entry("E", ["B", "D"], ["A", "D"],
                                                {"A": "flaky-under-load", "D": "regression"}),
            ],
            "unaccounted": [],
            "green": False,
            "runs": [{"run": "r1", "rerun": "alone"}],
        }
        # When B passes in wave 2, E's prerequisites drop the A->B branch and name only D.
        after_b_pass = verify.joined_classification(first_cls, None, resolved=["B"])
        e_pass = next(e for e in after_b_pass["checks"] if e["check"] == "E")
        self.assertEqual(e_pass["prerequisites"], ["D"])
        self.assertIn("failed prerequisite: D (regression)", e_pass["reason"])
        self.assertNotIn("A (", e_pass["reason"])

        # When B regresses in wave 2, E's prerequisites point at B (regression) and D (regression).
        second_cls = {
            "base": "a" * 40,
            "checks": [
                {"check": "B", "class": "regression", "alone": {"status": "failed"}, "base": {"status": "passed"}},
            ],
            "unaccounted": [],
            "green": False,
            "runs": [{"run": "r2", "rerun": "alone"}],
        }
        after_b_reg = verify.joined_classification(first_cls, second_cls, resolved=["B"])
        e_reg = next(e for e in after_b_reg["checks"] if e["check"] == "E")
        self.assertEqual(e_reg["prerequisites"], ["B", "D"])
        self.assertIn("failed prerequisites: B (regression), D (regression)", e_reg["reason"])


if __name__ == "__main__":
    unittest.main()
