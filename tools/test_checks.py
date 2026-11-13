"""The root check entry: which checks a change selects, the plan Lanefather reads, and the repository rules that
hold the four packages to one lint gate (fathers#116). Run: python3 -m unittest discover -s tools -p 'test_*.py'."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checks  # tools/checks.py, beside this file

ROOT = Path(__file__).resolve().parents[1]
ALWAYS = {"tools", "kit", "test hygiene"}


def package(name: str) -> set[str]:
    return {f"{name} lint", f"{name} tests"}


class SelectionTests(unittest.TestCase):
    def selected(self, *paths: str) -> set[str]:
        return set(checks.select(list(paths)).checks)

    def test_a_package_change_selects_that_package_and_the_cheap_root_checks(self) -> None:
        self.assertEqual(package("junkfather") | ALWAYS, self.selected("junkfather/junkfather/reap.py"))
        self.assertEqual(package("lanefather") | package("godfather") | ALWAYS,
                         self.selected("lanefather/README.md", "godfather/src/godfather/views/lanes.py"))

    def test_slotfather_sources_also_select_godfathers_contract_tests(self) -> None:
        self.assertEqual(package("slotfather") | {"godfather tests"} | ALWAYS,
                         self.selected("slotfather/src/slotfather/state.py"))
        self.assertEqual(package("slotfather") | ALWAYS, self.selected("slotfather/docs/api.md"))

    def test_common_selects_every_package(self) -> None:
        every = set().union(*(package(name) for name in checks.PACKAGES))
        self.assertEqual(every | {"common"} | ALWAYS, self.selected("common/eventcheck.py"))

    def test_shared_rules_select_every_lint_gate(self) -> None:
        lint = {f"{name} lint" for name in checks.PACKAGES}
        self.assertEqual(lint | ALWAYS, self.selected("tools/ruff.toml"))

    def test_a_workflow_selects_its_package(self) -> None:
        self.assertEqual(package("slotfather") | ALWAYS, self.selected(".github/workflows/slotfather.yml"))
        self.assertEqual({"common"} | ALWAYS, self.selected(".github/workflows/common.yml", "install.py"))

    def test_documentation_selects_only_the_cheap_root_checks(self) -> None:
        plan = checks.select(["AGENTS.md", "docs/agents/checks.md", "notes/sha-map.md", "LICENSE"])
        self.assertFalse(plan.full)
        self.assertEqual(ALWAYS, set(plan.checks))

    def test_a_path_no_rule_knows_selects_everything(self) -> None:
        plan = checks.select(["slotfather/x.py", "Makefile"])
        self.assertTrue(plan.full)
        self.assertEqual(["Makefile"], plan.unowned)
        self.assertEqual([check.name for check in checks.CHECKS], list(plan.checks))

    def test_each_check_remembers_the_paths_that_chose_it(self) -> None:
        plan = checks.select(["slotfather/src/slotfather/cli.py", "README.md"])
        self.assertEqual(["slotfather/src/slotfather/cli.py"], plan.checks["godfather tests"])
        self.assertEqual(["README.md", "slotfather/src/slotfather/cli.py"], plan.checks["kit"])


class PlanTests(unittest.TestCase):
    """The answer to `tools/lane-check --changed --base B --plan --json` (lanefather/docs/reference.md)."""

    def repository(self) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(root)], check=False)
        git = ["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
               "-c", "core.hooksPath=/dev/null"]
        subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
        (root / "README.md").write_text("fathers\n")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "base"], check=True)
        (root / "junkfather").mkdir()
        (root / "junkfather/reap.py").write_text("")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "change"], check=True)
        (root / "lanefather").mkdir()
        (root / "lanefather/new.py").write_text("")  # untracked: the plan counts it too
        return root

    def test_the_plan_names_repo_kit_check_ids_with_the_paths_that_chose_them(self) -> None:
        root = self.repository()
        plan = checks.plan(root, "HEAD~1")
        self.assertEqual({"v": 1, "full": False, "unowned": [], "suites": ["verify"],
                          "paths": ["junkfather/reap.py", "lanefather/new.py"]},
                         {key: plan[key] for key in ("v", "full", "unowned", "suites", "paths")})
        rows = {row["id"]: row for row in plan["checks"]}
        self.assertEqual({"id": "verify:junkfather%20lint", "suite": "verify", "paths": ["junkfather/reap.py"],
                          "producer": False}, rows["verify:junkfather%20lint"])
        self.assertIn("verify:lanefather%20tests", rows)
        self.assertNotIn("verify:slotfather%20tests", rows)
        self.assertEqual(sorted(rows), sorted(f"verify:{checks.encode(name)}" for name in
                                              package("junkfather") | package("lanefather") | ALWAYS))

    def test_the_command_line_prints_the_plan_as_json(self) -> None:
        root = self.repository()
        result = subprocess.run([sys.executable, str(ROOT / "tools/lane-check"), "--changed", "--base", "HEAD~1",
                                 "--plan", "--json"], cwd=root, capture_output=True, text=True, check=True)
        self.assertEqual(checks.plan(root, "HEAD~1"), json.loads(result.stdout))

    def test_a_plan_without_its_arguments_is_refused(self) -> None:
        result = subprocess.run([sys.executable, str(ROOT / "tools/lane-check"), "--plan", "--json"],
                                capture_output=True, text=True)
        self.assertEqual(1, result.returncode)
        self.assertIn("--changed --base REF --plan --json", result.stderr)

    def test_ids_are_encoded_as_repo_kit_encodes_step_names(self) -> None:
        self.assertEqual("godfather%20tests", checks.encode("godfather tests"))
        self.assertEqual("a%3Ab-c_d.e!~*'()", checks.encode("a:b-c_d.e!~*'()"))


class StepTests(unittest.TestCase):
    """What repo-kit.config.mjs reads: `python3 tools/checks.py steps`."""

    def steps(self, **environment: str) -> list[dict[str, object]]:
        result = subprocess.run([sys.executable, str(ROOT / "tools/checks.py"), "steps"], cwd=ROOT,
                                env={**os.environ, **environment}, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_every_check_is_a_repo_kit_step_with_its_memory(self) -> None:
        steps = self.steps(FATHERS_CHECKS="all")
        self.assertEqual([check.name for check in checks.CHECKS], [step["name"] for step in steps])
        for step in steps:
            self.assertEqual("run", step["command"][0], step)
            self.assertGreater(step["memory"], 0, step)

    def test_a_base_narrows_the_steps_to_the_changed_packages(self) -> None:
        names = {step["name"] for step in self.steps(FATHERS_CHECK_BASE="HEAD")}
        everything = {check.name for check in checks.CHECKS}
        self.assertTrue(ALWAYS <= names <= everything)

    def test_an_unknown_selection_is_refused(self) -> None:
        result = subprocess.run([sys.executable, str(ROOT / "tools/checks.py"), "steps"], cwd=ROOT,
                                env={**os.environ, "FATHERS_CHECKS": "some"}, capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn("FATHERS_CHECKS", result.stderr)


class RepositoryRuleTests(unittest.TestCase):
    """One lint gate for the four packages: the same file, the same pinned tools, the shared Ruff rules."""

    def test_the_packages_carry_one_check_py(self) -> None:
        reference = (ROOT / "slotfather/tools/check.py").read_bytes()
        for name in checks.PACKAGES:
            with self.subTest(package=name):
                self.assertEqual(reference, (ROOT / name / "tools/check.py").read_bytes())

    def test_the_packages_pin_the_same_ruff_and_pyright(self) -> None:
        pins: dict[str, set[str]] = {"ruff": set(), "pyright": set()}
        for name in checks.PACKAGES:
            text = (ROOT / name / "requirements-dev.txt").read_text()
            for tool, versions in pins.items():
                found = re.search(rf"^{tool}==(\S+)", text, re.M)
                self.assertIsNotNone(found, f"{name}/requirements-dev.txt pins no {tool}")
                assert found
                versions.add(found[1])
        self.assertEqual({"ruff": 1, "pyright": 1}, {tool: len(versions) for tool, versions in pins.items()}, pins)

    def test_every_package_extends_the_shared_ruff_rules_without_its_own_selection(self) -> None:
        for name in checks.PACKAGES:
            with self.subTest(package=name):
                text = (ROOT / name / "pyproject.toml").read_text()
                self.assertIn('extend = "../tools/ruff.toml"', text)
                self.assertNotRegex(text, r"(?m)^(extend-)?select\s*=")


if __name__ == "__main__":
    unittest.main()
