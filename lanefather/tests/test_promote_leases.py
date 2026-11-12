"""Promotion children observed under real Slotfather leases in an isolated queue (#682)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest

from support import FAKE_VERIFY, LaneCase

REAL_SLOTFATHER = shutil.which("slotfather")

OBSERVE = r'''
def observe(name):
    report = json.loads(subprocess.check_output([os.environ["REAL_SLOTFATHER"], "status", "--json"], text=True))
    row = {"step": name, "pid": os.getpid(), "lease": os.environ.get("SLOTFATHER_LEASE"),
           "count": os.environ.get("SLOTFATHER_COUNT"), "status": report,
           "sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    with open(os.environ["STEP_OBSERVATIONS"], "a") as out:
        out.write(json.dumps(row) + "\n")
'''

STEP = r'''import json, os, pathlib, subprocess, sys
__OBSERVE__
name = __NAME__
observe(name)
if name == "install":
    # Like an installer whose reusable gate bundle is missing: it must rebuild under its execution lease.
    bundle = pathlib.Path(os.environ["REUSABLE_BUNDLE"])
    if not bundle.exists():
        pathlib.Path(os.environ["REBUILT_BUNDLE"]).write_text("rebuilt")
        bundle.write_text("bundle")
sys.exit(7 if os.environ.get("FAIL_STEP") == name else 0)
'''


@unittest.skipUnless(REAL_SLOTFATHER, "real installed Slotfather required for child execution observations")
class PromotionLeaseTests(LaneCase):
    def setUp(self):
        super().setUp()
        self.observations = self.tmp / "observations.jsonl"
        self.bundle = self.tmp / "bundle"
        self.rebuilt = self.tmp / "rebuilt"
        self.calls = self.tmp / "real-slotfather-calls.jsonl"
        self.env.update(REAL_SLOTFATHER=str(REAL_SLOTFATHER), STEP_OBSERVATIONS=str(self.observations),
                        REUSABLE_BUNDLE=str(self.bundle), REBUILT_BUNDLE=str(self.rebuilt),
                        SLOTFATHER_DIR=str(self.tmp / "real-slotfather"), SLOTFATHER_CORES="6",
                        SLOTFATHER_MEMORY_GIB="8", SLOTFATHER_POLL="0.01", LANEFATHER_LOAD_THROTTLE="0",
                        REAL_SLOT_CALLS=str(self.calls))
        # Record invocations, but delegate all admission, queue files, ancestry and cleanup to the real CLI.
        wrapper = self.tmp / "bin" / "slotfather"
        wrapper.write_text(f"#!{sys.executable}\n" + r'''import json, os, sys
with open(os.environ["REAL_SLOT_CALLS"], "a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\n")
os.execv(os.environ["REAL_SLOTFATHER"], [os.environ["REAL_SLOTFATHER"], *sys.argv[1:]])
''')
        wrapper.chmod(0o755)
        # The controlled verifier takes a real execution lease, then releases it before promote's next phase.
        insertion = OBSERVE + r'''
if args[:1] != ["--changed"]:
    if not os.environ.get("SLOTFATHER_LEASE"):
        os.execvp("slotfather", ["slotfather", "run", "-n", args[args.index("--jobs") + 1],
                  "--label", os.environ.get("LANEFATHER_SLOT_LABEL", "gate"),
                  "--priority", os.environ.get("LANEFATHER_SLOT_PRIORITY", "normal"),
                  "--meta", "phase=gate", "--", sys.executable, __file__, *args])
    observe("gate")
'''
        verifier = FAKE_VERIFY.replace('args = sys.argv[1:]\n', 'args = sys.argv[1:]\n' + insertion)
        self.write(self.integration, "verify.sh", verifier).chmod(0o755)
        for name in ("install", "check-installed", "link-package", "deploy"):
            self.write(self.integration, f"steps/{name}.py", STEP.replace("__OBSERVE__", OBSERVE)
                       .replace("__NAME__", repr(name)))
        self.steps_config = "\n".join(
            f'[[promote.{"install" if name in ("install", "check-installed") else "deploy"}]]\n'
            f'name = "{name}"\nargv = ["{sys.executable}", "steps/{name}.py"]\n'
            + ('lease = { slots = 1, ticket = "682" }\n' if name == "install" else
               'lease = { slots = 2, ticket = "682" }\n' if name == "link-package" else "")
            for name in ("install", "check-installed", "link-package", "deploy"))
        config_path = self.integration / ".lanefather.toml"
        original = config_path.read_text()
        start, end = original.index("[[promote.install]]"), original.index("[hooks]")
        config_path.write_text(original[:start] + self.steps_config + "\n" + original[end:])
        self.commit(self.integration, {}, "promotion execution fixture")
        self.git(self.repo, "branch", "dev")
        self.remote = self.tmp / "remote.git"
        self.git(self.tmp, "init", "-q", "--bare", str(self.remote))
        self.git(self.repo, "config", "core.hooksPath", str(self.tmp / "no-hooks"))
        self.git(self.repo, "remote", "add", "origin", str(self.remote))
        self.git(self.repo, "push", "-q", "origin", "main", "dev")
        self.sha = self.tip()

    def rows(self):
        return [json.loads(line) for line in self.observations.read_text().splitlines()]

    def test_fresh_gate_then_already_stamped_missing_bundle_children_hold_real_phase_leases(self):
        self.lane("promote", self.sha, "--jobs", "2", "--install", "--deploy", "host", "--wait", "0")
        rows = self.rows()
        self.assertEqual([row["step"] for row in rows], ["gate", "install", "check-installed", "link-package", "deploy"])
        for row in rows:
            self.assertEqual(row["sha"], self.sha)
            expected = {"gate": 2, "install": 1, "link-package": 2}.get(row["step"], 0)
            self.assertEqual(row["status"]["held"], expected, row)
            self.assertEqual(bool(row["lease"]), bool(expected), row)
            if row["step"] in ("install", "link-package"):
                self.assertEqual(row["status"]["entries"][0]["meta"]["phase"], row["step"])
                self.assertEqual(row["count"], str(expected))
        self.assertNotEqual(rows[0]["lease"], rows[1]["lease"], "the gate ended before the install lease")
        self.assertNotEqual(rows[1]["lease"], rows[3]["lease"], "each heavy phase owns its own execution lease")
        gate_runs = len(self.verify_runs())
        self.bundle.unlink()
        self.rebuilt.unlink()
        self.observations.unlink()
        self.lane("promote", self.sha, "--jobs", "2", "--install", "--deploy", "host", "--wait", "0")
        self.assertEqual(len(self.verify_runs()), gate_runs, "an already-stamped tree reuses its gate")
        self.assertTrue(self.rebuilt.exists(), "missing reusable bundle caused an install rebuild")
        again = self.rows()
        self.assertEqual([row["step"] for row in again], ["install", "check-installed", "link-package", "deploy"])
        self.assertEqual([row["status"]["held"] for row in again], [1, 0, 2, 0])
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        phases = [call for call in calls if "--ticket" in call]
        self.assertEqual(len(phases), 4, "both heavy phases requested leases on both promotions")
        for call in phases:
            self.assertEqual(call[call.index("--priority") + 1], "normal")
            self.assertEqual(call[call.index("--ticket") + 1], "682")
            self.assertNotIn("--class", call)
            self.assertFalse(any(value.startswith(("ticket=", "priority=", "class=")) for value in call))

    def test_a_failed_leased_package_stops_deploy_and_releases_its_capacity(self):
        self.env["FAIL_STEP"] = "link-package"
        output = self.lane("promote", self.sha, "--jobs", "2", "--install", "--deploy", "host", "--wait", "0", ok=False)
        rows = self.rows()
        self.assertEqual([row["step"] for row in rows], ["gate", "install", "check-installed", "link-package"])
        self.assertEqual(rows[-1]["status"]["held"], 2)
        self.assertIn("link-package failed (exit 7); later steps not run: deploy", output)
        status = subprocess.check_output([str(REAL_SLOTFATHER), "status", "--json"], env=self.env, text=True)
        self.assertEqual(json.loads(status)["held"], 0)

    def test_an_actual_outer_lease_is_reused_by_gate_and_post_promotion_commands(self):
        command = [str(REAL_SLOTFATHER), "run", "-n", "2", "--label", "outer-promote", "--priority", "batch",
                   "--ticket", "outer682", "--", sys.executable, "-m", "lanefather", "promote", self.sha,
                   "--jobs", "2", "--install", "--deploy", "host", "--wait", "0"]
        result = subprocess.run(command, cwd=self.integration, env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.rows()
        self.assertEqual([row["step"] for row in rows], ["gate", "install", "check-installed", "link-package", "deploy"])
        self.assertEqual({row["lease"] for row in rows}, {rows[0]["lease"]})
        self.assertTrue(rows[0]["lease"])
        self.assertTrue(all(row["status"]["held"] == 2 and len(row["status"]["entries"]) == 1 for row in rows),
                        "nested commands never reserve a second lease or deadlock")

    def test_missing_required_slotfather_refuses_before_the_gate_or_publication(self):
        (self.tmp / "bin" / "slotfather").unlink()
        self.env["PATH"] = str(self.tmp / "bin") + ":/usr/bin:/bin"
        before = self.git(self.remote, "rev-parse", "dev")
        output = self.lane("promote", self.sha, "--install", "--deploy", "host", "--wait", "0", ok=False)
        self.assertIn("configured execution leases require slotfather on PATH; nothing published", output)
        self.assertEqual(self.git(self.remote, "rev-parse", "dev"), before)
        self.assertFalse(self.observations.exists())

    def test_unconfigured_steps_keep_their_direct_execution_behavior(self):
        path = self.integration / ".lanefather.toml"
        text = "\n".join(line for line in path.read_text().splitlines() if not line.startswith("lease ="))
        self.commit(self.integration, {".lanefather.toml": text + "\n"}, "unconfigured steps")
        self.sha = self.tip()
        self.lane("promote", self.sha, "--jobs", "2", "--install", "--deploy", "host", "--wait", "0")
        self.assertEqual([row["status"]["held"] for row in self.rows()], [2, 0, 0, 0, 0])
