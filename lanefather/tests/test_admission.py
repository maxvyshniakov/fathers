"""Lifetime admission at the workflow CLI and explicit Project interface."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from support import LaneCase

from lanefather import config
from lanefather.errors import LaneError
from lanefather.project import Project


class AdmissionTests(LaneCase):
    def test_legacy_readiness_and_registration_allow_exact_load_boundary(self):
        self.env.update(LANEFATHER_LOAD15="9", LANEFATHER_CORES="6")
        self.write(self.state.parent, "queue/queue.toml", '[[lane]]\nid="next"\nticket=2\nclaims=["other/**"]\n')
        project = config.load(self.integration / ".lanefather.toml")
        with patch.dict("os.environ", self.env, clear=True):
            readiness = Project(project).readiness()
        self.assertTrue(readiness.can_start, readiness.blocked)
        self.start("first", "src/**")

    def provider(self, script: str | None = None):
        path = self.integration / ".lanefather.toml"
        path.write_text(path.read_text() + '\n[admission]\nprovider="slotfather"\n')
        binary = self.tmp / "bin" / "slotfather"
        binary.write_text(script or '''#!/usr/bin/env python3
import json, os, sys
request=json.load(sys.stdin)
host={"cores":{"value":6,"source":"physical"},"memory_gib":{"value":20,"source":"physical"},"load15":{"value":0.1,"source":"physical"}}
host.update(request["sample_overrides"])
answer={"v":1,"kind":"lane-lifetime","policy":"lane-lifetime-v1","available":True,
"scope":request["scope"],"occupied":request["occupied"],"cap_override":request["cap_override"],
"allowed":True,"refusals":[],"cap":int(os.environ.get("ADMISSION_CAP","6")),"derived_cap":int(os.environ.get("ADMISSION_CAP","6")),"cap_source":"derived","warnings":[],
"load_limit":9.0,"sampled_at":1793923200.0,"host":host}
answer["refusals"] = (["cap"] if request["occupied"] >= answer["cap"] else []) + (["load15"] if host["load15"]["value"] > answer["load_limit"] else [])
answer["allowed"] = not answer["refusals"]
answer.update(json.loads(os.environ.get("ADMISSION_PATCH","{}")))
if os.environ.get("ADMISSION_REQUEST_LOG"):
    with open(os.environ["ADMISSION_REQUEST_LOG"],"w") as log: json.dump(request, log)
print(json.dumps(answer))
''')
        binary.chmod(0o755)

    def test_opt_in_uses_provider_cap_not_duplicate_local_formula(self):
        self.provider()
        self.write(self.state.parent, "queue/queue.toml", '[[lane]]\nid="next"\nticket=2\nclaims=["other/**"]\n')
        with patch.dict("os.environ", self.env, clear=True):
            readiness = Project(config.load(self.integration / ".lanefather.toml")).readiness()
        self.assertEqual(6, readiness.cap, "provider owns policy; local fixture formula would give3")
        self.assertTrue(readiness.can_start, readiness.blocked)
        self.assertFalse(self.state.exists(), "readiness remains read-only")
        self.start("first", "src/**")
        self.assertEqual(6, json.loads(self.lane("list", "--json"))["cap"])
        self.assertIn("cap=6", (self.state / "ledger.log").read_text())


    def test_corrupt_registry_cannot_be_bypassed_with_force(self):
        self.state.mkdir(parents=True)
        (self.state / "unknown.json").write_text("not JSON")
        output = self.start("first", "src/**", extra=("--force-reason", "override"), ok=False)
        self.assertIn("unreadable", output)
        self.assertFalse((self.state / "first.json").exists())

    def test_known_refusals_are_audited_but_claims_remain_non_bypassable(self):
        self.provider()
        self.env["ADMISSION_CAP"] = "1"
        self.start("first", "src/**")
        self.env.update(LANEFATHER_DISK_FREE_GB="120", LANEFATHER_LOAD15="9.01")
        output = self.start("blocked", "other/**", ok=False)
        self.assertIn("the cap is 1", output)
        self.assertIn("free disk is 120", output)
        self.assertIn("load15 is 9.0", output)
        self.start("forced", "other/**", extra=("--force-reason", "owner decision"))
        audit = self.record("forced")["overrides"][0]
        self.assertEqual("owner decision", audit["reason"])
        self.assertEqual(3, len(audit["refusals"]))
        output = self.start("collision", "src/**", extra=("--force-reason", "owner decision"), ok=False)
        self.assertIn("claims overlap lane first", output)
        self.assertFalse((self.state / "collision.json").exists())

    def test_wrong_or_mismatched_response_is_unavailable_even_with_force(self):
        self.provider()
        for change in ({"v":2}, {"available":False}, {"scope":"wrong"}, {"occupied":True},
                       {"allowed":True,"refusals":["cap"]}, {"cap":True},
                       {"cap_override":{"value":"3","source":"other"}}, {"cap_source":"untrusted"},
                       {"host":{"cores":{"value":1,"source":"different"}}}):
            with self.subTest(change=change):
                self.env["ADMISSION_PATCH"] = json.dumps(change)
                output = self.start("bad", "src/**", extra=("--force-reason", "override"), ok=False)
                self.assertIn("lifetime admission unavailable", output)
                self.assertFalse((self.state / "bad.json").exists())

    def test_concurrent_last_place_is_serialized_by_the_store_lock(self):
        self.provider()
        self.env["ADMISSION_CAP"] = "1"
        commands = [[sys.executable,"-m","lanefather","start",name,"--ticket","2","--base","integrate/q",
                     "--claims",name+"/**"] for name in ("first","second")]
        children = [subprocess.Popen(argv,cwd=self.integration,env=self.env,stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,text=True) for argv in commands]
        results = [child.communicate(timeout=15) for child in children]
        self.assertEqual([0,1],sorted(child.returncode for child in children),results)
        records = list(self.state.glob("*.json"))
        self.assertEqual(1,len(records))


    def test_transport_failure_and_resource_bounds_never_register(self):
        scripts = {
            "bad-json": "print('{')",
            "duplicate": "print('{\"v\":1,\"v\":1}')",
            "deep": "print('['*2000)",
            "stdout-limit": "import sys; sys.stdout.write('x'*100000)",
            "stderr-limit": "import sys; sys.stderr.write('x'*100000)",
            "nonzero": "import sys; sys.exit(75)",
            "timeout": "import time; time.sleep(30)",
            "descendant-pipe": "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])",
        }
        self.provider()
        for name, script in scripts.items():
            with self.subTest(name=name):
                binary = self.tmp / "bin" / "slotfather"
                binary.write_text("#!/usr/bin/env python3\n"+script+"\n")
                started = time.monotonic()
                output = self.start(name, "src/**", extra=("--force-reason","override"), ok=False)
                self.assertLess(time.monotonic()-started,8)
                self.assertIn("lifetime admission unavailable", output)
                self.assertNotIn("Traceback", output)
                self.assertFalse((self.state / (name+".json")).exists())

    def test_alias_provenance_and_conflicts_use_the_explicit_project(self):
        self.provider()
        path = self.integration / ".lanefather.toml"
        text = path.read_text()
        path.write_text('legacy_env_prefix="OLD_"\n'+text)
        del self.env["LANEFATHER_CORES"]
        self.env["OLD_CORES"] = "6"
        self.env["ADMISSION_REQUEST_LOG"] = str(self.tmp / "request.json")
        self.start("first", "src/**")
        request = json.loads((self.tmp / "request.json").read_text())
        self.assertEqual({"value":6,"source":"OLD_CORES"},request["sample_overrides"]["cores"])
        self.env["LANEFATHER_CORES"] = "12"
        output = self.start("conflict", "other/**", ok=False)
        self.assertIn("differ",output)
        self.assertFalse((self.state / "conflict.json").exists())

    @unittest.skipUnless(os.environ.get("LANEFATHER_TEST_SLOTFATHER_ARGV"), "optional exact-source cross-repo acceptance")
    def test_exact_slotfather_source_readiness_start_next_lifecycle_and_force(self):
        argv = json.loads(os.environ["LANEFATHER_TEST_SLOTFATHER_ARGV"])
        self.provider("#!/usr/bin/env python3\nimport os,sys\nos.execv("+repr(argv[0])+", "+repr(argv)+"+sys.argv[1:])\n")
        self.env.update(LANEFATHER_CORES="12", LANEFATHER_MEMORY_GB="32", LANEFATHER_LOAD15="18",
                        SLOTFATHER_CORES="1",SLOTFATHER_MEMORY_GIB="4",SLOTFATHER_LOADAVG="999")
        self.write(self.state.parent,"queue/queue.toml",'[[lane]]\nid="next"\nticket=2\nclaims=["next/**"]\n')
        project = config.load(self.integration / ".lanefather.toml")
        with patch.dict("os.environ",self.env,clear=True):
            snapshot = Project(project).readiness()
        self.assertTrue(snapshot.can_start,snapshot.blocked)
        self.assertEqual(6,snapshot.cap)
        self.start("planned","planned/**")
        self.start("active","active/**",extra=("--create",))
        for status in ("landed","released","done"):
            (self.state / (status+".json")).write_text(json.dumps({"id":status,"status":status,"claims":[]}))
        self.env["LANEFATHER_CAP"]="2"
        with patch.dict("os.environ",self.env,clear=True):
            snapshot = Project(project).readiness()
        self.assertEqual({"planned","active"},{r["id"] for r in snapshot.planned_or_active})
        self.assertFalse(snapshot.can_start)
        self.assertIn("LANEFATHER_CAP",snapshot.cap_source)
        self.assertIn("refused",self.lane("next","--start",ok=False))
        self.lane("next","--start","--force-reason","owner decision")
        self.assertEqual(1,len(self.record("next")["overrides"][0]["refusals"]))
        self.env["LANEFATHER_CAP"]="typo"
        with patch.dict("os.environ",self.env,clear=True):
            self.assertEqual(6,Project(project).readiness().cap)


    def test_missing_provider_and_unknown_configuration_fail_without_a_fallback(self):
        self.assertEqual("legacy",config.parse({}).admission_provider)
        for raw in ({"provider":"typo"},{"provider":True},{"provider":"slotfather","command":["checkout.py"]}):
            with self.subTest(raw=raw), self.assertRaises(LaneError):
                config.parse({"admission":raw})
        self.provider()
        self.write(self.state.parent,"queue/queue.toml",'[[lane]]\nid="next"\nticket=2\nclaims=["src/**"]\n')
        project = config.load(self.integration / ".lanefather.toml")
        with patch.dict("os.environ",{**self.env,"PATH":str(self.tmp / "empty")},clear=True), \
                self.assertRaisesRegex(LaneError,"slotfather is not on PATH"):
            Project(project).readiness()
        self.assertFalse(self.state.exists())

    def test_invalid_status_cannot_silently_undercount(self):
        self.state.mkdir(parents=True)
        (self.state / "unknown.json").write_text(json.dumps({"id":"unknown","status":"typo"}))
        output = self.start("first","src/**",extra=("--force-reason","override"),ok=False)
        self.assertIn("invalid identity or status",output)
        self.assertFalse((self.state / "first.json").exists())

    def test_provider_cannot_ignore_an_explicit_cap_even_with_force(self):
        self.provider()
        self.start("first", "first/**")
        self.env["LANEFATHER_CAP"] = "1"
        output = self.start("ignored", "other/**", extra=("--force-reason", "owner decision"), ok=False)
        self.assertIn("lifetime admission unavailable", output)
        self.assertFalse((self.state / "ignored.json").exists())

    def test_valid_invalid_and_whitespace_cap_controls_match_the_wire_contract(self):
        self.provider()
        self.write(self.state.parent, "queue/queue.toml", '[[lane]]\nid="next"\nticket=2\nclaims=["src/**"]\n')
        project = config.load(self.integration / ".lanefather.toml")
        for raw, cap, source, warnings in (
                (None, 6, "derived", []), ("", 6, "derived", []),
                ("1", 1, "LANEFATHER_CAP", []), (" 1 ", 1, "LANEFATHER_CAP", []),
                ("01", 1, "LANEFATHER_CAP", []), ("١", 1, "LANEFATHER_CAP", []),
                (" ", 6, "derived", ["invalid_cap_override"]),
                ("0", 6, "derived", ["invalid_cap_override"]),
                ("-1", 6, "derived", ["invalid_cap_override"]),
                ("+1", 6, "derived", ["invalid_cap_override"]),
                ("1.0", 6, "derived", ["invalid_cap_override"]),
                ("typo", 6, "derived", ["invalid_cap_override"]),
                ("²", 6, "derived", ["invalid_cap_override"])):
            with self.subTest(raw=raw):
                self.env.pop("LANEFATHER_CAP", None)
                if raw is not None:
                    self.env["LANEFATHER_CAP"] = raw
                self.env["ADMISSION_PATCH"] = json.dumps({"cap": cap, "cap_source": source, "warnings": warnings})
                with patch.dict("os.environ", self.env, clear=True):
                    snapshot = Project(project).readiness()
                self.assertTrue(snapshot.can_start, snapshot.blocked)
                self.assertEqual(cap, snapshot.cap)
                self.assertIn(source, snapshot.cap_source)
        self.assertFalse(self.state.exists(), "wire controls stay read-only")

    def test_wrong_override_source_cap_or_warnings_are_technical_not_forceable(self):
        self.provider()
        for index, (raw, cap, source, warnings) in enumerate((
                ("1", 1, "LANEFATHER_CAP", ["invalid_cap_override"]),
                ("1", 1, "derived", []), ("1", 1, "OTHER_CAP", []),
                ("1", 6, "LANEFATHER_CAP", []),
                ("typo", 6, "LANEFATHER_CAP", ["invalid_cap_override"]),
                ("typo", 6, "derived", []), (" ", 6, "derived", []),
                ("", 6, "derived", ["invalid_cap_override"]),
                (None, 6, "derived", ["invalid_cap_override"]))):
            with self.subTest(raw=raw, cap=cap, source=source, warnings=warnings):
                self.env.pop("LANEFATHER_CAP", None)
                if raw is not None:
                    self.env["LANEFATHER_CAP"] = raw
                self.env["ADMISSION_PATCH"] = json.dumps({"cap": cap, "cap_source": source, "warnings": warnings})
                lane_id = f"invalid-override-{index}"
                output = self.start(lane_id, "src/**", extra=("--force-reason", "owner decision"), ok=False)
                self.assertIn("lifetime admission unavailable", output)
                self.assertFalse((self.state / f"{lane_id}.json").exists())
