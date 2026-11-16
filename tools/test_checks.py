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
        self.assertEqual(every | package("common") | ALWAYS, self.selected("common/eventcheck.py"))

    def test_shared_rules_select_every_lint_gate(self) -> None:
        lint = {f"{name} lint" for name in checks.PACKAGES}
        self.assertEqual(lint | ALWAYS, self.selected("tools/ruff.toml"))

    def test_a_workflow_selects_its_package(self) -> None:
        self.assertEqual(package("slotfather") | ALWAYS, self.selected(".github/workflows/slotfather.yml"))
        self.assertEqual(package("common") | ALWAYS, self.selected(".github/workflows/common.yml", "install.py"))
        self.assertEqual(ALWAYS, self.selected(".github/workflows/package.yml"))

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


WORKFLOWS = ROOT / ".github/workflows"
CALLERS = (*checks.PACKAGES, "common")


def floor(text: str) -> str:
    found = re.search(r"^\s+floor: '(3\.\d+)'$", text, re.M)
    assert found, "no floor"
    return found[1]


class WorkflowTests(unittest.TestCase):
    """One reusable workflow (package.yml) the five call with their name, paths and floor (fathers#120)."""

    def test_the_five_workflows_differ_only_in_package_name_paths_and_floor(self) -> None:
        def shape(name: str) -> str:
            text = (WORKFLOWS / f"{name}.yml").read_text().replace(name, "PACKAGE")
            text = re.sub(r"^\s+- '[^']+'\n", "", text, flags=re.M)  # the paths that trigger it
            return re.sub(r"^(\s+floor: )'3\.\d+'$", r"\1FLOOR", text, flags=re.M)
        reference = shape("slotfather")
        self.assertIn("uses: ./.github/workflows/package.yml", reference)
        for name in CALLERS:
            with self.subTest(workflow=name):
                self.assertEqual(reference, shape(name))

    def test_each_floor_is_the_installers_and_the_pyprojects(self) -> None:
        for name in checks.PACKAGES:
            with self.subTest(package=name):
                wanted = floor((WORKFLOWS / f"{name}.yml").read_text())
                installer = re.search(r"^MIN_PYTHON = \((\d+), (\d+)\)$", (ROOT / name / "install.py").read_text(),
                                      re.M)
                assert installer, f"{name}/install.py has no MIN_PYTHON"
                self.assertEqual(wanted, f"{installer[1]}.{installer[2]}")
                declared = re.search(r'^requires-python = ">=(3\.\d+)"(?:\s|$)',
                                     (ROOT / name / "pyproject.toml").read_text(), re.M)
                if declared:
                    self.assertEqual(wanted, declared[1])
        self.assertEqual("3.9", floor((WORKFLOWS / "common.yml").read_text()))  # common/ and install.py: 3.9

    # One case per package, so a regression in one is not hidden behind another's expected failure.
    def declares_requires_python(self, name: str) -> None:
        text = (ROOT / name / "pyproject.toml").read_text()
        self.assertRegex(text, r'(?m)^requires-python = ">=3\.\d+"(?:\s|$)', name)

    @unittest.expectedFailure  # slotfather/pyproject.toml gains it in lane sf-38 (fathers#120)
    def test_slotfather_declares_requires_python(self) -> None:
        self.declares_requires_python("slotfather")

    def test_lanefather_declares_requires_python(self) -> None:
        self.declares_requires_python("lanefather")

    def test_junkfather_declares_requires_python(self) -> None:
        self.declares_requires_python("junkfather")

    def test_godfather_declares_requires_python(self) -> None:
        self.declares_requires_python("godfather")

    def test_the_reusable_workflow_runs_the_floor_and_the_newest_on_both_systems(self) -> None:
        text = (WORKFLOWS / "package.yml").read_text()
        legs = re.findall(r"- os: (\S+)\n\s+python: \$\{\{ inputs\.(floor|newest) \}\}(\n\s+lint: true)?", text)
        self.assertEqual([("ubuntu-24.04", "floor", ""), ("ubuntu-latest", "newest", "\n            lint: true"),
                          ("macos-latest", "floor", ""), ("macos-latest", "newest", "\n            lint: true")], legs)
        steps = re.findall(r"^      - name: .+\n(?:        if: (.+)\n)?        run: (.+)$", text, re.M)
        self.assertEqual([("matrix.lint", 'python -m pip install -r "$PACKAGE/requirements-dev.txt"'),
                          ("matrix.lint", 'python tools/checks.py run "$PACKAGE lint"'),
                          ("", 'python tools/checks.py run "$PACKAGE tests"')], steps)
        for name in CALLERS:
            self.assertIn(f"{name} lint", checks.NAMES)
            self.assertIn(f"{name} tests", checks.NAMES)


class CommandTests(unittest.TestCase):
    def test_common_tests_runs_commons_own_tests(self) -> None:
        result = subprocess.run([sys.executable, str(ROOT / "tools/checks.py"), "run", "common tests"], cwd=ROOT,
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(0, result.returncode, result.stderr[-2000:])
        ran = re.search(r"^Ran (\d+) tests?", result.stderr, re.M)
        assert ran, result.stderr[-2000:]
        self.assertGreater(int(ran[1]), 0)


class RepositoryRuleTests(unittest.TestCase):
    """One lint gate for the four packages: the same file, the same pinned tools, the shared Ruff rules."""

    def test_the_packages_carry_one_check_py(self) -> None:
        reference = (ROOT / "slotfather/tools/check.py").read_bytes()
        for name in checks.PACKAGES:
            with self.subTest(package=name):
                self.assertEqual(reference, (ROOT / name / "tools/check.py").read_bytes())

    def test_the_packages_pin_the_same_ruff_and_pyright(self) -> None:
        pins: dict[str, set[str]] = {"ruff": set(), "pyright": set()}
        for name in (*checks.PACKAGES, "common"):
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
