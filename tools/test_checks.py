"""The root check entry: which checks a change selects, the plan Lanefather reads, and the repository rules that
hold the four packages to one lint gate (fathers#116). Run: python3 -m unittest discover -s tools -p 'test_*.py'."""
from __future__ import annotations

import ast
from collections import Counter
import difflib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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
        self.assertEqual(package("slotfather") | ALWAYS, self.selected("slotfather/tests/test_usage.py"))

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

    def test_package_documentation_selects_only_the_cheap_root_checks(self) -> None:
        plan = checks.select(["lanefather/GLOSSARY.md", "slotfather/docs/api.md",
                              "lanefather/docs/adr/0001-real-adapters-before-general-ports.md"])
        self.assertFalse(plan.full)
        self.assertEqual(ALWAYS, set(plan.checks))
        for path in ("lanefather/install.py", "lanefather/hooks/pre-commit",
                     "lanefather/lanefather/cli.py", "lanefather/lanefather/README.md",
                     "lanefather/tests/fixtures/notes.md"):
            with self.subTest(path=path):
                self.assertEqual(package("lanefather") | ALWAYS, self.selected(path))
        for path, extra in (
            ("lanefather/CHANGELOG.md", package("lanefather")),
            ("slotfather/CHANGELOG.md", package("slotfather")),
            ("slotfather/pyproject.toml", package("slotfather")),
            ("slotfather/src/slotfather/version.py", package("slotfather") | {"godfather tests"}),
            ("junkfather/junkfather/__init__.py", package("junkfather")),
        ):
            with self.subTest(changelog_path=path):
                self.assertEqual(extra | {"common tests"} | ALWAYS, self.selected(path))

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

    def test_the_plan_for_package_documentation_only_skips_package_checks(self) -> None:
        root = self.repository()
        git = ["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
               "-c", "core.hooksPath=/dev/null"]
        (root / "lanefather/new.py").unlink()
        (root / "lanefather/GLOSSARY.md").write_text("# Glossary\n")
        subprocess.run([*git, "add", "lanefather/GLOSSARY.md"], check=True)
        subprocess.run([*git, "commit", "-qm", "docs"], check=True)
        plan = checks.plan(root, "HEAD~1")
        self.assertFalse(plan["full"])
        self.assertEqual(["lanefather/GLOSSARY.md"], plan["paths"])
        self.assertEqual(sorted(f"verify:{checks.encode(name)}" for name in ALWAYS),
                         sorted(row["id"] for row in plan["checks"]))

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
        self.assertEqual("3.11", floor((WORKFLOWS / "common.yml").read_text()))  # common/ and install.py: 3.11

    # One case per package, so a regression in one is not hidden behind another's expected failure.
    def declares_requires_python(self, name: str) -> None:
        text = (ROOT / name / "pyproject.toml").read_text()
        self.assertRegex(text, r'(?m)^requires-python = ">=3\.\d+"(?:\s|$)', name)

    def test_slotfather_declares_requires_python(self) -> None:
        self.declares_requires_python("slotfather")

    def test_lanefather_declares_requires_python(self) -> None:
        self.declares_requires_python("lanefather")

    def test_junkfather_declares_requires_python(self) -> None:
        self.declares_requires_python("junkfather")

    def test_godfather_declares_requires_python(self) -> None:
        self.declares_requires_python("godfather")

    def test_tokenfather_declares_requires_python(self) -> None:
        self.declares_requires_python("tokenfather")

    def test_the_reusable_workflow_runs_only_the_two_ubuntu_jobs(self) -> None:
        text = (WORKFLOWS / "package.yml").read_text()
        legs = re.findall(r"- os: (\S+)\n\s+python: \$\{\{ inputs\.(floor|newest) \}\}(\n\s+lint: true)?", text)
        self.assertEqual(["ubuntu-24.04", "ubuntu-latest"], re.findall(r"^\s*- os: (\S+)$", text, re.M))
        self.assertEqual([("ubuntu-24.04", "floor", ""), ("ubuntu-latest", "newest", "\n            lint: true")], legs)
        steps = re.findall(r"^      - name: .+\n(?:        if: (.+)\n)?        run: (.+)$", text, re.M)
        self.assertEqual([("matrix.lint", 'python -m pip install -r "$PACKAGE/requirements-dev.txt"'),
                          ("matrix.lint", 'python tools/checks.py run "$PACKAGE lint"'),
                          ("", 'python tools/checks.py run "$PACKAGE tests"')], steps)
        for name in CALLERS:
            self.assertIn(f"{name} lint", checks.NAMES)
            self.assertIn(f"{name} tests", checks.NAMES)


RECURSIVE_CHECK_OK_RE = re.compile(r"#[ \t]*recursive-check-ok:[ \t]*\S+")
COPY_OK_RE = re.compile(r"#[ \t]*copy-ok:[ \t]*\S+")
SLEEP_OK_RE = re.compile(r"#[ \t]*sleep-ok:[ \t]*\S+")
SHELL_SLEEP_RE = re.compile(r"\bsleep\s+(\d+(?:\.\d+)?|\.\d+)\b")
TOKEN_RE = re.compile(r"\w+|\S")
NEAR_COPY_THRESHOLD = 0.85
PACKAGE_NAME_RE = re.compile(r"(?i)\b(?:" + "|".join(checks.PACKAGES) + r")\b")
# Shrink-only ratchet for embedded shell sleeps (0.2s <= s < 30s) in test string literals (fathers#219).
EMBEDDED_SHELL_SLEEP_BASELINE: dict[str, int] = {
    "junkfather/tests/test_background.py": 6,
    "junkfather/tests/test_browser.py": 1,
    "lanefather/tests/test_checks.py": 1,
    "slotfather/tests/test_contract.py": 1,
    "slotfather/tests/test_membership.py": 1,
    "slotfather/tests/test_process_table.py": 1,
    "slotfather/tests/test_session.py": 1,
    "slotfather/tests/test_sweep.py": 15,
    "slotfather/tests/test_usage.py": 1,
    "tokenfather/tests/test_adapters.py": 1,
}


def find_recursive_checks_run_violations(source: str) -> list[str]:
    """Lines in `source` that spawn `checks.py run ...` without `# recursive-check-ok: <reason>`."""
    lines = source.splitlines()
    tree = ast.parse(source)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_text = ast.unparse(node.func)
        subprocess_calls = ("run", "Popen", "call", "check_output", "check_call")
        if not (func_text.startswith("subprocess.") or func_text in subprocess_calls):
            continue
        arg_node = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "args"), None)
        if arg_node is None:
            continue
        first_arg = ast.unparse(arg_node)
        if "checks.py" not in first_arg:
            continue
        if not re.search(r"""['"]run['"]|\brun\b""", first_arg):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start) or start
        snippet = "\n".join(lines[max(0, start - 2) : end])
        if not RECURSIVE_CHECK_OK_RE.search(snippet):
            violations.append(f"line {start}: {lines[start - 1].strip()}")
    return violations


def find_embedded_shell_sleeps(source: str) -> list[tuple[int, str]]:
    """String literals in `source` with embedded shell `sleep <0.2..29.9>` without `# sleep-ok: <reason>`."""
    lines = source.splitlines()
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        secs = [float(match.group(1)) for match in SHELL_SLEEP_RE.finditer(node.value)]
        if not any(0.2 <= value < 30.0 for value in secs):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start) or start
        snippet = "\n".join(lines[max(0, start - 2) : end])
        if not SLEEP_OK_RE.search(snippet):
            found.append((start, node.value))
    return found


def normalize_test_source(text: str) -> str:
    """Normalize package names and strip comments/blank lines for cross-package near-copy detection."""
    replaced = PACKAGE_NAME_RE.sub("PACKAGE", text)
    kept = [
        line.strip()
        for line in replaced.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return "\n".join(kept)


def find_near_copy_test_files(
    files_by_package: dict[str, dict[str, str]],
    threshold: float = NEAR_COPY_THRESHOLD,
) -> list[tuple[str, str, float]]:
    """Cross-package test file pairs whose normalized token similarity exceeds `threshold` without `# copy-ok:`."""
    entries: list[tuple[str, str, list[str], int, Counter[str]]] = []
    for pkg, pkg_files in sorted(files_by_package.items()):
        for rel_path, raw_text in sorted(pkg_files.items()):
            if COPY_OK_RE.search(raw_text):
                continue
            tokens = TOKEN_RE.findall(normalize_test_source(raw_text))
            entries.append((pkg, rel_path, tokens, len(tokens), Counter(tokens)))

    violations: list[tuple[str, str, float]] = []
    for index, (pkg_a, path_a, tok_a, len_a, cnt_a) in enumerate(entries):
        for pkg_b, path_b, tok_b, len_b, cnt_b in entries[index + 1 :]:
            if pkg_a == pkg_b:
                continue
            total = len_a + len_b
            if total == 0 or 2 * min(len_a, len_b) <= threshold * total:
                continue
            common = sum(count if count < cnt_b[tok] else cnt_b[tok] for tok, count in cnt_a.items() if tok in cnt_b)
            if 2 * common <= threshold * total:
                continue
            ratio = difflib.SequenceMatcher(None, tok_a, tok_b).ratio()
            if ratio > threshold:
                violations.append((path_a, path_b, ratio))
    return violations


class CommandTests(unittest.TestCase):
    def test_commands_maps_checks_to_their_working_directory_and_argv(self) -> None:
        self.assertEqual(
            (ROOT, [[sys.executable, "-m", "unittest", "discover", "-s", "common/tests"]]),
            checks.commands("common tests"),
        )
        self.assertEqual(
            (ROOT / "slotfather", [[sys.executable, "tools/check.py"]]),
            checks.commands("slotfather lint"),
        )
        self.assertEqual(
            (ROOT / "godfather", [[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "tests"]]),
            checks.commands("godfather tests"),
        )
        with self.assertRaisesRegex(ValueError, "unknown check"):
            checks.commands("nosuch check")

    def test_run_dispatches_in_process_without_spawning_real_suites(self) -> None:
        calls: list[tuple[list[str], Path, dict[str, str]]] = []

        def fake_run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            calls.append((argv, cwd, env))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(checks.subprocess, "run", side_effect=fake_run), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, checks.run("common tests"))

        self.assertEqual(1, len(calls))
        argv, cwd, env = calls[0]
        self.assertEqual([sys.executable, "-m", "unittest", "discover", "-s", "common/tests"], argv)
        self.assertEqual(ROOT, cwd)
        self.assertEqual("1", env.get("PYRIGHT_PYTHON_IGNORE_WARNINGS"))

        with mock.patch.object(checks, "run_unittest_parallel", return_value=0) as parallel_mock, \
                mock.patch.object(checks.subprocess, "run", side_effect=AssertionError("unexpected subprocess")), \
                mock.patch.dict(os.environ, {"FATHERS_TEST_WORKERS": "3"}):
            for pkg in ("slotfather", "lanefather", "junkfather", "godfather"):
                self.assertIn(pkg, checks.PARALLEL_TEST_PACKAGES)
                self.assertEqual(0, checks.run(f"{pkg} tests"))
            self.assertEqual(4, parallel_mock.call_count)

    def test_no_recursive_checks_run_subprocesses_in_test_checks(self) -> None:
        own_source = Path(__file__).read_text(encoding="utf-8")
        self.assertEqual([], find_recursive_checks_run_violations(own_source))

        bad_source = (
            "import subprocess, sys\n"
            'subprocess.run([sys.executable, "tools/checks.py", "run", "common tests"])\n'
        )
        self.assertEqual(1, len(find_recursive_checks_run_violations(bad_source)))

        empty_reason = bad_source.strip() + "  # recursive-check-ok:   \n"
        self.assertEqual(1, len(find_recursive_checks_run_violations(empty_reason)))

        exempt_source = bad_source.strip() + "  # recursive-check-ok: intentional recursion test\n"
        self.assertEqual([], find_recursive_checks_run_violations(exempt_source))


class ParallelRunnerTests(unittest.TestCase):
    """Safe parallel test-file execution in tools/checks.py (fathers#209)."""

    def test_workers_default_is_bounded_and_env_overridable(self) -> None:
        default = checks.test_workers({})
        self.assertGreaterEqual(default, 1)
        self.assertLessEqual(default, checks.MAX_DEFAULT_TEST_WORKERS)
        self.assertEqual(1, checks.test_workers({"FATHERS_TEST_WORKERS": "1"}))
        self.assertEqual(3, checks.test_workers({"FATHERS_TEST_WORKERS": "3"}))
        for bad in ("0", "-2", "lots"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                checks.test_workers({"FATHERS_TEST_WORKERS": bad})

    def test_discover_test_files_sorts_and_skips_non_package_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            (tests / "fixtures").mkdir(parents=True)
            (tests / "pkg").mkdir()
            (tests / "pkg" / "__init__.py").write_text("")
            (tests / "test_b.py").write_text("")
            (tests / "test_a.py").write_text("")
            (tests / "support.py").write_text("")
            (tests / "fixtures" / "test_ignored.py").write_text("")
            (tests / "pkg" / "test_nested.py").write_text("")
            found, top = checks.discover_test_files(root, ["-s", "tests", "-p", "test_*.py"])
            self.assertIsNone(top)
            self.assertEqual(["tests/pkg/test_nested.py", "tests/test_a.py", "tests/test_b.py"],
                             [path.relative_to(root).as_posix() for path in found])

    def _run_parallel_helper(self, root: Path, workers: int) -> subprocess.CompletedProcess[str]:
        script = (
            f"import os, pathlib, sys; sys.path.insert(0, {str(ROOT / 'tools')!r}); "
            "import checks; "
            f"sys.exit(checks.run_unittest_parallel(pathlib.Path({str(root)!r}), "
            f"['-s', 'tests', '-p', 'test_*.py'], dict(os.environ), {workers}))"
        )
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)

    def test_parallel_runner_aggregates_passing_files_and_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_a.py").write_text(
                "import unittest\n"
                "class A(unittest.TestCase):\n"
                "    def test_one(self): self.assertEqual(1, 1)\n"
                "    @unittest.skip('demo')\n"
                "    def test_skip(self): pass\n"
            )
            (tests / "test_b.py").write_text(
                "import unittest\n"
                "class B(unittest.TestCase):\n"
                "    def test_two(self): self.assertEqual(2, 2)\n"
            )
            result = self._run_parallel_helper(root, workers=2)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertRegex(result.stderr, r"(?m)^Ran 3 tests in ")
            self.assertIn("OK (skipped=1)", result.stderr)

    def test_parallel_runner_reports_failures_in_deterministic_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_a.py").write_text(
                "import unittest\n"
                "class A(unittest.TestCase):\n"
                "    def test_fail_a(self): self.fail('boom-a')\n"
            )
            (tests / "test_b.py").write_text(
                "import unittest\n"
                "class B(unittest.TestCase):\n"
                "    def test_ok(self): pass\n"
            )
            (tests / "test_c.py").write_text(
                "import unittest\n"
                "class C(unittest.TestCase):\n"
                "    def test_fail_c(self): self.fail('boom-c')\n"
            )
            result = self._run_parallel_helper(root, workers=3)
            self.assertNotEqual(0, result.returncode)
            pos_a = result.stderr.find("=== FAIL: tests/test_a.py")
            pos_c = result.stderr.find("=== FAIL: tests/test_c.py")
            self.assertGreaterEqual(pos_a, 0, result.stderr)
            self.assertGreater(pos_c, pos_a, result.stderr)
            self.assertIn("boom-a", result.stderr)
            self.assertIn("boom-c", result.stderr)
            self.assertIn("FAILED (files=2: tests/test_a.py, tests/test_c.py)", result.stderr)


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

    def test_no_cross_package_near_copy_test_files(self) -> None:
        files_by_package: dict[str, dict[str, str]] = {}
        for pkg in checks.PACKAGES:
            pkg_root = ROOT / pkg
            files_by_package[pkg] = {
                path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
                for path in checks.discover_test_files(pkg_root, checks.TESTS[pkg])[0]
            }
        files_by_package["common"] = {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in checks.discover_test_files(ROOT, ["-s", "common/tests"])[0]
        }

        violations = find_near_copy_test_files(files_by_package)
        self.assertEqual(
            [],
            [f"{a} <-> {b} ({ratio:.3f} > {NEAR_COPY_THRESHOLD})" for a, b, ratio in violations],
        )

        sample_a = "\n".join(f"    assert slot_helper({i}) == expected({i})" for i in range(10))
        sample_b = "\n".join(f"    assert junk_helper({i}) == expected({i})" for i in range(10))
        synthetic = {
            "slotfather": {"slotfather/tests/test_dup.py": sample_a},
            "junkfather": {"junkfather/tests/test_dup.py": sample_b},
        }
        self.assertEqual(1, len(find_near_copy_test_files(synthetic)))
        self.assertEqual(
            1,
            len(
                find_near_copy_test_files(
                    {
                        "slotfather": {"slotfather/tests/test_dup.py": "# copy-ok:   \n" + sample_a},
                        "junkfather": {"junkfather/tests/test_dup.py": sample_b},
                    }
                )
            ),
        )
        self.assertEqual(
            [],
            find_near_copy_test_files(
                {
                    "slotfather": {"slotfather/tests/test_dup.py": "# copy-ok: shared contract wrapper\n" + sample_a},
                    "junkfather": {"junkfather/tests/test_dup.py": sample_b},
                }
            ),
        )

    def test_embedded_shell_sleep_ratchet(self) -> None:
        counts: dict[str, int] = {}
        for path in sorted(ROOT.glob("*/tests/**/test*.py")):
            hits = find_embedded_shell_sleeps(path.read_text(encoding="utf-8"))
            if hits:
                counts[path.relative_to(ROOT).as_posix()] = len(hits)
        over_budget = [
            f"{rel}: {count} > baseline {EMBEDDED_SHELL_SLEEP_BASELINE.get(rel, 0)}"
            for rel, count in sorted(counts.items())
            if count > EMBEDDED_SHELL_SLEEP_BASELINE.get(rel, 0)
        ]
        self.assertEqual(
            [],
            over_budget,
            "new embedded shell sleep in test string; use `# sleep-ok: <reason>` if needed",
        )
        self.assertEqual(1, len(find_embedded_shell_sleeps('cmd = "sleep 1.5"\n')))
        self.assertEqual([], find_embedded_shell_sleeps('cmd = "sleep 1.5"  # sleep-ok: intentional\n'))
        self.assertEqual([], find_embedded_shell_sleeps('cmd = "sleep 600"\n'))


if __name__ == "__main__":
    unittest.main()
