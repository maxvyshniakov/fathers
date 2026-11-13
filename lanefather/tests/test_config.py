"""Tests of a project's seams (lanefather.config) and the seams to other tools: slotfather (lanefather.slots), the
process source (lanefather.procs) and the review role of models.toml (lanefather.review)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import tempfile
import textwrap
import unittest
import unittest.mock

from support import FIXTURE_CONFIG, ROOT  # noqa: F401  (ROOT puts the package on sys.path)

from lanefather import checks, config, procs, promote, reviewer, slots
from lanefather.errors import LaneError


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lanefather-config-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(config.use, None)
        patcher = unittest.mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                                        if not k.startswith(("LANEFATHER_", "STRAYLIGHT_", "SLOTFATHER_"))
                                                        and k != "CLAUDE_CODE_SESSION_ID"},
                                           clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, text: str) -> config.Config:
        path = self.tmp / ".lanefather.toml"
        path.write_text(textwrap.dedent(text))
        config.use(config.load(path))
        return config.current()


class ConfigTests(ConfigCase):
    def test_step_leases_require_explicit_valid_cost_and_request_fields(self):
        head = '[[promote.install]]\nname="build"\nargv=["build.sh"]\n'
        for lease, message in (("true", "lease must be a table"),
                               ("{}", "slots must be"),
                               ("{slots=true}", "slots must be"),
                               ("{slots=0}", "slots must be"),
                               ("{slots=-1}", "slots must be"),
                               ('{slots="many"}', "slots must be"),
                               ('{slots=1,priority="urgent"}', "priority must be"),
                               ("{slots=1,ticket=42}", "ticket must be a string"),
                               ('{slots=1,ticket=""}', "ticket must be a short"),
                               ('{slots=1,clas="batch"}', "unknown keys clas")):
            with self.subTest(lease=lease), self.assertRaisesRegex(LaneError, message):
                self.use(head + f"lease={lease}\n")
        current = self.use(head + 'lease={slots="all",priority="batch",ticket="682"}\n')
        self.assertEqual(current.install[0].lease, config.Lease("all", "batch", "682"))

    def test_a_project_without_the_file_gets_the_defaults(self):
        config.use(config.load(None))
        current = config.current()
        self.assertEqual((current.command, current.verify, current.plan_module, current.install),
                         ("lanefather", "verify.sh", "", ()))
        self.assertEqual(config.prog(), "lanefather")

    def test_the_fixture_config_names_every_seam(self):
        current = self.use(FIXTURE_CONFIG)
        self.assertEqual(current.plan_module, "tools/verification_plan.py")
        self.assertEqual([step.name for step in current.install], ["install", "check-installed"])
        self.assertEqual(current.deploy[1].env, (("STRAYLIGHT_LINK_PACKAGE", "{package}"),))
        self.assertEqual(current.after_promote, ("python3", "tools/after-promote.py", "{sha}", "--source", "promote {short}"))
        self.assertEqual(current.queue_name, "the #347 queue")

    def test_a_setting_reads_lanefather_first_then_the_alias_then_the_legacy_prefix(self):
        current = self.use("""\
            legacy_env_prefix = "STRAYLIGHT_LANE_"
            [env]
            LANEFATHER_LOAD_THROTTLE = "STRAYLIGHT_LOAD_WAIT"
            """)
        os.environ.update(STRAYLIGHT_LANE_CAP="5", STRAYLIGHT_LOAD_WAIT="1")
        self.assertEqual(current.lookup("CAP"), ("STRAYLIGHT_LANE_CAP", "5"))
        self.assertEqual(current.env("LOAD_THROTTLE"), "1")
        os.environ["LANEFATHER_CAP"] = "5"
        self.assertEqual(current.lookup("CAP"), ("LANEFATHER_CAP", "5"))
        self.assertEqual(current.env_names("LOAD_THROTTLE"), "LANEFATHER_LOAD_THROTTLE or STRAYLIGHT_LOAD_WAIT "
                                                             "or STRAYLIGHT_LANE_LOAD_THROTTLE")
        self.assertIsNone(current.env("RERUNS"))

    def test_two_names_of_one_setting_never_split(self):
        # Review of #648, finding 3: lanefather and the tools that read only the legacy names must not read two state
        # directories or queues.
        current = self.use("""\
            legacy_env_prefix = "STRAYLIGHT_LANE_"
            shared_env = ["STATE"]
            """)
        os.environ.update(LANEFATHER_STATE="/a", STRAYLIGHT_LANE_STATE="/b")
        with self.assertRaises(LaneError) as raised:
            current.env("STATE")
        self.assertIn("LANEFATHER_STATE='/a' and STRAYLIGHT_LANE_STATE='/b' differ", str(raised.exception))
        os.environ["STRAYLIGHT_LANE_STATE"] = "/a"
        self.assertEqual(current.env("STATE"), "/a")
        del os.environ["STRAYLIGHT_LANE_STATE"]
        with self.assertRaises(LaneError) as raised:
            current.env("STATE")
        self.assertIn("LANEFATHER_STATE is set, but STRAYLIGHT_LANE_STATE is not", str(raised.exception))
        # A setting no other tool reads may go by the new name alone.
        os.environ["LANEFATHER_MODELS"] = "/m.toml"
        self.assertEqual(current.env("MODELS"), "/m.toml")

    def test_a_malformed_config_is_refused_with_its_key(self):
        for text, words in (('[checks]\nverify = 3\n', "verify must be a string"),
                            ('unclaimed = [1]\n', "unclaimed must be a string or a list of strings"),
                            ('[gc]\nafter_remove_report = 1\n', "after_remove_report must be a boolean"),
                            ('[gc]\ndirs = [1]\n', "dirs must be a string or a list of strings"),
                            ('[gc]\nbranches = [""]\n', "[gc] branches takes non-empty patterns"),
                            ('[[promote.install]]\nargv = ["x"]\n', "needs a name and an argv"),
                            ('checks = "x"\n', "[checks] must be a table"),
                            ('[env]\nLANEFATHER_X = 1\n', "[env] maps"),
                            ('verify = [\n', "Invalid value"),
                            # Review of #648, finding 4: a typo drops a seam without a word unless refused.
                            ('[promote]\nafer = ["x"]\n', "[promote] has unknown keys afer"),
                            ('projcet = "x"\n', "has unknown keys projcet"),
                            ('[[promote.install]]\nname = "i"\nargv = ["x"]\nenvv = {}\n',
                             "[promote.install] has unknown keys envv")):
            with self.subTest(text), self.assertRaises(LaneError) as raised:
                self.use(text)
            self.assertIn(words, str(raised.exception))

    def test_gc_patterns_default_to_agent_lane_claude_and_codex_worktrees(self):
        default = config.Config()
        self.assertEqual(default.gc_dirs, ("agent-*", "lane-*", ".claude/worktrees/*", "codex-*"))
        self.assertEqual(default.gc_branches, ("worktree-agent-*", "lane/*", "claude/*", "codex/*"))
        found = self.use('[gc]\ndirs = "agent-*"\nbranches = []\n')
        self.assertEqual((found.gc_dirs, found.gc_branches), (("agent-*",), ()))

    def test_find_reads_the_repository_top_or_lanefather_config(self):
        repo = self.tmp / "repo"
        (repo / "sub").mkdir(parents=True)
        os.system(f"git init -q {repo}")
        self.assertIsNone(config.find(repo / "sub"))
        (repo / ".lanefather.toml").write_text('command = "tools/lane"\n')
        found = config.find(repo / "sub")
        self.assertEqual(found and found.resolve(), (repo / ".lanefather.toml").resolve())
        os.environ["LANEFATHER_CONFIG"] = str(self.tmp / "elsewhere.toml")
        self.assertEqual(config.find(repo), self.tmp / "elsewhere.toml")


class SlotPriorityTests(ConfigCase):
    """lanefather#24: a project or an orchestrator lowers its runs' Slotfather priority without code changes. First
    wins: LANEFATHER_PRIORITY_<PHASE>, LANEFATHER_PRIORITY, [slots.priority] <phase>, [slots.priority] default,
    the built-in."""

    PROJECT = '[slots.priority]\ndefault = "batch"\ngate = "normal"\n'

    def test_the_built_in_priorities_apply_when_nothing_sets_one(self):
        self.use("")
        self.assertEqual({phase: found["priority"] for phase, found in slots.priorities().items()},
                         {"check": "normal", "post-merge": "interactive", "gate": "interactive",
                          "rerun": "interactive", "perf": "batch"})
        self.assertEqual({found["source"] for found in slots.priorities().values()}, {"built-in"})

    def test_the_projects_default_applies_to_every_phase_it_does_not_name(self):
        self.use('[slots.priority]\ndefault = "batch"\n')
        self.assertEqual(slots.phase_priority("post-merge"), ("batch", ".lanefather.toml [slots.priority] default"))

    def test_the_projects_phase_key_applies_to_that_phase(self):
        self.use('[slots.priority]\nrerun = "normal"\n')
        self.assertEqual(slots.phase_priority("rerun"), ("normal", ".lanefather.toml [slots.priority] rerun"))
        self.assertEqual(slots.phase_priority("gate"), ("interactive", "built-in"))

    def test_lanefather_priority_applies_to_every_phase(self):
        self.use("")
        os.environ["LANEFATHER_PRIORITY"] = "batch"
        self.assertEqual({phase: (found["priority"], found["source"]) for phase, found in slots.priorities().items()},
                         {phase: ("batch", "LANEFATHER_PRIORITY") for phase in slots.PHASES})

    def test_a_phase_variable_applies_to_that_phase_only(self):
        self.use("")
        os.environ.update(LANEFATHER_PRIORITY_GATE="batch", LANEFATHER_PRIORITY_POST_MERGE="normal")
        self.assertEqual(slots.phase_priority("gate"), ("batch", "LANEFATHER_PRIORITY_GATE"))
        self.assertEqual(slots.phase_priority("post-merge"), ("normal", "LANEFATHER_PRIORITY_POST_MERGE"))
        self.assertEqual(slots.phase_priority("check"), ("normal", "built-in"))

    def test_the_legacy_prefix_names_the_same_setting(self):
        self.use('legacy_env_prefix = "STRAYLIGHT_LANE_"\n')
        os.environ["STRAYLIGHT_LANE_PRIORITY"] = "batch"
        self.assertEqual(slots.phase_priority("check"), ("batch", "STRAYLIGHT_LANE_PRIORITY"))

    def test_precedence_is_phase_variable_variable_phase_key_default_built_in(self):
        self.use(self.PROJECT)
        os.environ.update(LANEFATHER_PRIORITY_GATE="interactive", LANEFATHER_PRIORITY="batch")
        steps: list[tuple[str, str]] = []
        for unset in ("LANEFATHER_PRIORITY_GATE", "LANEFATHER_PRIORITY", None):
            steps.append(slots.phase_priority("gate"))
            if unset:
                del os.environ[unset]
        steps.append(slots.phase_priority("check"))
        self.use("")
        steps.append(slots.phase_priority("check"))
        self.assertEqual(steps, [("interactive", "LANEFATHER_PRIORITY_GATE"), ("batch", "LANEFATHER_PRIORITY"),
                                 ("normal", ".lanefather.toml [slots.priority] gate"),
                                 ("batch", ".lanefather.toml [slots.priority] default"), ("normal", "built-in")])

    def test_an_empty_variable_is_unset(self):
        self.use(self.PROJECT)
        os.environ.update(LANEFATHER_PRIORITY="", LANEFATHER_PRIORITY_GATE="")
        self.assertEqual(slots.phase_priority("gate"), ("normal", ".lanefather.toml [slots.priority] gate"))

    def test_an_invalid_value_or_phase_is_a_clear_error(self):
        for text, message in (('[slots.priority]\ngate = "urgent"\n', r"gate = 'urgent' is not a Slotfather priority"),
                              ("[slots.priority]\ngate = 1\n", "is not a Slotfather priority"),
                              ('[slots.priority]\ngates = "batch"\n', r"\[slots.priority\] has unknown keys gates"),
                              ('[slots]\npriority = "batch"\n', "must be a table of phase = priority"),
                              ('[slots]\nprio = {}\n', r"\[slots\] has unknown keys prio")):
            with self.subTest(text=text), self.assertRaisesRegex(LaneError, message):
                self.use(text)
        self.use("")
        os.environ["LANEFATHER_PRIORITY"] = "low"
        with self.assertRaisesRegex(LaneError, r"LANEFATHER_PRIORITY='low' is not a Slotfather priority; "
                                               "use interactive, normal, batch"):
            slots.phase_priority("check")
        os.environ["LANEFATHER_PRIORITY"] = "batch"
        os.environ["LANEFATHER_PRIORITY_SMOKE"] = "batch"
        with self.assertRaisesRegex(LaneError, "LANEFATHER_PRIORITY_SMOKE names no phase"):
            slots.phase_priority("check")

    def test_the_request_carries_the_priority_and_its_source(self):
        self.use('[checks]\nslot_env = "STRAYLIGHT_SLOT"\n[slots.priority]\ndefault = "batch"\n')
        environment = checks.verify_env("post-merge-a", "post-merge", lane="a")
        self.assertEqual(environment["STRAYLIGHT_SLOT_PRIORITY"], "batch")
        self.assertEqual(json.loads(environment["STRAYLIGHT_SLOT_META"]),
                         {"lane": "a", "phase": "post-merge",
                          "lanefather.priority": "batch (.lanefather.toml [slots.priority] default)"})
        os.environ["LANEFATHER_PRIORITY_RERUN"] = "normal"
        with unittest.mock.patch.object(slots, "command", return_value="/bin/slotfather"):
            rerun = checks.Rerun(self.tmp, ["native-check"], "lane-a-alone", 2)
        self.assertEqual(rerun.argv[rerun.argv.index("--priority") + 1], "normal")
        self.assertIn("lanefather.priority=normal (LANEFATHER_PRIORITY_RERUN)", rerun.argv)

    def test_a_promote_step_lease_takes_the_gates_priority_unless_it_names_its_own(self):
        current = self.use('[[promote.install]]\nname="build"\nargv=["build.sh"]\nlease={slots=1}\n'
                           '[[promote.install]]\nname="pinned"\nargv=["pin.sh"]\n'
                           'lease={slots=1,priority="interactive"}\n')
        build, pinned = current.install
        self.assertIsNone(build.lease and build.lease.priority)
        self.assertEqual(promote.step_priority(build), ("normal", "built-in"))
        os.environ["LANEFATHER_PRIORITY_GATE"] = "batch"
        self.assertEqual(promote.step_priority(build), ("batch", "LANEFATHER_PRIORITY_GATE"))
        self.assertEqual(promote.step_priority(pinned), ("interactive", "step pinned lease"))


class SeamTests(ConfigCase):
    def test_verify_queues_itself_with_the_projects_slot_variables(self):
        self.use('[checks]\nslot_env = "STRAYLIGHT_SLOT"\n')
        environment = slots.queue_environment("lane-a", "interactive", ticket="42", lane="a", phase="check", empty="")
        self.assertEqual(environment, {"STRAYLIGHT_SLOT_LABEL": "lane-a", "STRAYLIGHT_SLOT_PRIORITY": "interactive", "STRAYLIGHT_SLOT_TICKET": "42",
                                       "STRAYLIGHT_SLOT_META": '{"lane": "a", "phase": "check"}',
                                       "STRAYLIGHT_SLOT_YIELDABLE": ""})
        with self.assertRaises(ValueError):
            slots.queue_environment("lane-a", "urgent")

    def test_a_lease_wraps_the_command_once(self):
        self.use("")
        with unittest.mock.patch.object(slots, "command", return_value="/bin/slotfather"):
            argv = slots.wrap("all", "rerun", ["verify.sh"], wait=30, meta={"lane": "a"}, priority="interactive", ticket="42")
            self.assertEqual(argv, ["/bin/slotfather", "run", "-n", "all", "--label", "rerun", "--meta", "lane=a",
                                    "--priority", "interactive", "--ticket", "42", "--wait", "30", "--", "verify.sh"])
            os.environ.update(SLOTFATHER_LEASE="/lease", SLOTFATHER_COUNT="2")
            self.assertEqual(slots.wrap("all", "rerun", ["verify.sh"]), ["verify.sh"])
            self.assertEqual(slots.lease_count(), 2)

    def test_priority_and_ticket_cannot_be_serialized_as_metadata(self):
        self.use("")
        for key in ("class", "priority", "ticket"):
            with self.assertRaisesRegex(ValueError, "request field"):
                slots.clean_meta({key: "normal"})

    def test_without_a_process_source_nothing_is_scanned(self):
        # With neither a project hook nor the machine-wide CLI, legacy done keeps its original behavior.
        self.use("")
        with unittest.mock.patch.object(slots, "command", return_value=None):
            source = procs.source()
        self.assertIsInstance(source, procs.NoSource)
        self.assertEqual(source.sort_for_removal(str(self.tmp)), ([], []))

    def test_the_process_source_is_the_projects_hook(self):
        (self.tmp / "tools").mkdir()
        (self.tmp / "tools" / "scan.py").write_text(
            "def sort_for_removal(root):\n    return [], ['held by the hook']\n\n"
            "def stop(processes, root):\n    return []\n")
        self.use('[hooks]\nprocess_scan = "tools/scan.py"\n')
        self.assertEqual(procs.source().sort_for_removal("/x"), ([], ["held by the hook"]))

    def test_the_review_models_come_from_the_review_role_of_models_toml(self):
        self.use("")
        models = self.tmp / "models.toml"
        models.write_text(textwrap.dedent("""\
            schema = 1
            [models.opus]
            id = "claude-opus-9"
            cli = "claude"
            [models.sol]
            id = "gpt-9-sol"
            cli = "codex"
            [roles.review]
            choices = [
              { model = "sol", effort = "high", when = { not_family = true, limit = { codex_weekly = 30 } } },
              { model = "opus", effort = "low", when = { not_family = true } },
              { model = "opus", effort = "medium" },
            ]
            """))
        os.environ["LANEFATHER_MODELS"] = str(models)
        chosen = reviewer.Reviewer(config.current()).models
        self.assertEqual((chosen["sol"], chosen["sol_effort"], chosen["sol_min_left"], chosen["opus"],
                          chosen["opus_effort"]), ("gpt-9-sol", "high", 30, "claude-opus-9", "low"))
        models.write_text("schema = 1\n")
        self.assertEqual(reviewer.Reviewer(config.current()).models, reviewer.DEFAULT_MODELS)
        os.environ["LANEFATHER_MODELS"] = str(self.tmp / "missing.toml")
        self.assertEqual(reviewer.Reviewer(config.current()).models, reviewer.DEFAULT_MODELS)


if __name__ == "__main__":
    unittest.main()
