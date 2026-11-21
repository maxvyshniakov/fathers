"""Tests for lanefather.verify: one verify runner for check, post-merge, rerun, base rerun, gate and perf (fathers#70)."""
from __future__ import annotations

import ast
import json
import os
import pathlib
import signal
from typing import Any
import unittest
import unittest.mock

from support import FAKE_PLAN, FAKE_VERIFY, LaneCase

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
        path = PACKAGE_DIR / "review.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_checks: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "checks" or alias.name.endswith(".checks"):
                        imported_checks.append(f"line {node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "checks" or (node.module or "").endswith(".checks"):
                    imported_checks.append(f"line {node.lineno}: from {node.module} import ...")
                for alias in node.names:
                    if alias.name == "checks":
                        imported_checks.append(f"line {node.lineno}: from {node.module} import checks")
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
        from lanefather import config, slots, verify
        config.use(config.Config(project="Test"))
        self.addCleanup(config.use, None)
        tmp = pathlib.Path(self.id().replace(".", "_"))
        with (unittest.mock.patch.object(slots, "command", return_value="/bin/slotfather"),
              unittest.mock.patch.object(slots, "leased", return_value=False),
              unittest.mock.patch.object(verify, "tree_of", return_value=None),
              unittest.mock.patch.object(verify.journal, "log_path",
                                         return_value=pathlib.Path(f"/tmp/test-timeout-{os.getpid()}.log")),
              unittest.mock.patch.object(verify.subprocess, "Popen") as popen_mock):
            proc = unittest.mock.MagicMock()
            proc.wait.return_value = slots.TIMED_OUT
            proc.poll.return_value = slots.TIMED_OUT
            popen_mock.return_value = proc
            run = verify.Run(
                tmp, verify.Selection.checks(["lib-check"]), "lane-a-alone", "rerun", 2,
                meta={"lane": "a", "check": "lib-check"}, ticket="42",
                run_type={"plan": "abc", "checks": "1"},
                lease=verify.Lease(wait=12.0, kind="rerun"),
                wait_for_load=False, stoppable=True,
            )
            self.addCleanup(run.log.unlink, missing_ok=True)
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
        from lanefather import checks, config
        wt = self.created("stages", "lib/**")
        self.commit(wt, {"lib/api.txt": "foo\nbar\n"})
        with unittest.mock.patch.dict(os.environ, self.env, clear=False):
            config.use(config.load(config.find(wt)))
            self.addCleanup(config.use, None)
            prepared = checks.prepare("stages", agent="codex")
            self.assertEqual(prepared.record["id"], "stages")
            selection = checks.select(prepared, rerun=False, slots_override=1)
            assert selection is not None
            self.assertEqual(selection.decision, {"jobs": 1, "override": "--slots"})
            executed = checks.run(prepared, selection)
            rc = checks.record(prepared, selection, executed)
            self.assertEqual(rc, 0)
            receipt: dict[str, Any] = json.loads((self.state / "stages.receipt.json").read_text())
            self.assertEqual(receipt["status"], "green")
            self.assertEqual(receipt["agent"], "codex")
            # Re-selecting the same green tree short-circuits to None.
            again = checks.prepare("stages")
            self.assertIsNone(checks.select(again, rerun=False, slots_override=None))


if __name__ == "__main__":
    unittest.main()
