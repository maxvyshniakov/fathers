"""Tests of a project's seams (lanefather.config) and the seams to other tools: slotfather (lanefather.slots), the
process source (lanefather.procs) and the review role of models.toml (lanefather.review)."""
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import textwrap
import unittest
import unittest.mock

from support import FIXTURE_CONFIG, ROOT  # noqa: F401  (ROOT puts the package on sys.path)

from lanefather import config, procs, reviewer, slots
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
