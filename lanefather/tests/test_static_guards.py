"""Pure, subprocess-free AST and source-text static guards for Lanefather.

Run all static architecture, call-site, lifecycle, receipt, docs, and changelog layout guards in one sub-second
command alongside `lanefather/tools/check.py` and `tools/hygiene.py`:

    cd lanefather && PYTHONPATH=tests python3 -m unittest tests/test_static_guards.py
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
import functools
import inspect
import pathlib
import re
from typing import cast
import unittest

from support import ROOT

from lanefather import changelog, config, done, events, land, lifecycle, state

PKG = ROOT / "lanefather"

CORE_MODULES = {
    "lanes",
    "review",
    "done",
    "tickets",
    "checks",
    "history",
    "review_record",
    "lifecycle",
    "postmerge",
    "land",
}

ISSUE_REF = re.compile(r"(?P<prefix>(?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+)?#(?P<num>\d+)\b")
AVOID_CLAUSE = re.compile(r"_Avoid_:\s*([^.|]+)", re.IGNORECASE)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
FENCE = re.compile(r"^\s*```")

CHANGELOG_SECTIONS = ["Breaking", "Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]

# Variable/attribute names that denote a receipt dictionary across the package.
RECEIPT_NAMES = frozenset({
    "receipt",
    "previous",
    "parent",
    "current",
    "first",
    "review",
    "review_receipt",
    "review_rec",
    "check_rec",
    "smoke",
    "gate",
    "red",
    "green",
})
RECEIPT_SUFFIXES = (
    ".receipt.json",
    ".first.json",
    ".post-merge.json",
    ".recheck.json",
    ".review.json",
    ".gate.json",
    ".perf.json",
)


@dataclass(frozen=True)
class _FileIndex:
    source: str
    tree: ast.Module
    calls: tuple[ast.Call, ...]
    assigns: tuple[ast.Assign, ...]
    attrs: tuple[ast.Attribute, ...]
    compares: tuple[ast.Compare, ...]
    subscripts: tuple[ast.Subscript, ...]
    imports: tuple[ast.Import | ast.ImportFrom, ...]
    str_constants: tuple[ast.Constant, ...]
    docstring_ids: frozenset[int]
    receipt_calls: tuple[tuple[str, ast.Call], ...]


@functools.cache
def _index_file(path: pathlib.Path) -> _FileIndex:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    calls: list[ast.Call] = []
    assigns: list[ast.Assign] = []
    attrs: list[ast.Attribute] = []
    compares: list[ast.Compare] = []
    subscripts: list[ast.Subscript] = []
    imports: list[ast.Import | ast.ImportFrom] = []
    str_constants: list[ast.Constant] = []
    docstring_ids: set[int] = set()
    receipt_calls: list[tuple[str, ast.Call]] = []

    def visit(node: ast.AST, func_stack: list[str]) -> None:
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstring_ids.add(id(first.value))
        if isinstance(node, ast.Call):
            calls.append(node)
            if (func_stack and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("write", "write_json")):
                for fname in func_stack:
                    receipt_calls.append((fname, node))
        elif isinstance(node, ast.Assign):
            assigns.append(node)
        elif isinstance(node, ast.Attribute):
            attrs.append(node)
        elif isinstance(node, ast.Compare):
            compares.append(node)
        elif isinstance(node, ast.Subscript):
            subscripts.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            str_constants.append(node)

        pushed = isinstance(node, ast.FunctionDef)
        if pushed:
            func_stack.append(node.name)
        for child in ast.iter_child_nodes(node):
            visit(child, func_stack)
        if pushed:
            func_stack.pop()

    visit(tree, [])
    return _FileIndex(
        source=source,
        tree=tree,
        calls=tuple(calls),
        assigns=tuple(assigns),
        attrs=tuple(attrs),
        compares=tuple(compares),
        subscripts=tuple(subscripts),
        imports=tuple(imports),
        str_constants=tuple(str_constants),
        docstring_ids=frozenset(docstring_ids),
        receipt_calls=tuple(receipt_calls),
    )


@functools.cache
def _pkg_files() -> tuple[pathlib.Path, ...]:
    return tuple(sorted(PKG.glob("*.py")))


def _imported_internal_modules(path: pathlib.Path) -> tuple[set[str], set[str]]:
    """Return `(top_level, local)` relative module names imported from `lanefather` in `path`."""
    top: set[str] = set()
    local: set[str] = set()
    for node in _index_file(path).imports:
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            names = [a.name for a in node.names] if node.module is None else [node.module]
            target = top if node.col_offset == 0 else local
            target.update(names)
    return top, local


def _prose_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return (1-based line number, line) outside fenced code blocks."""
    lines: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append((lineno, line))
    return lines


def _slugify(heading: str) -> str:
    slug = heading.strip().lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug


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


class Tidy4StructureTests(unittest.TestCase):
    """Static acceptance checks for #72."""

    def test_land_and_done_function_sizes_and_signatures(self) -> None:
        land_lines = len(inspect.getsourcelines(land.land)[0])
        finish_lines = len(inspect.getsourcelines(done.finish)[0])
        self.assertLessEqual(land_lines, 40, f"land.land is {land_lines} lines")
        self.assertLessEqual(finish_lines, 40, f"done.finish is {finish_lines} lines")

        allowed_namespace = {
            "land.py": {"land", "recheck"},
            "done.py": {"done", "drop", "keep"},
        }
        for filename, allowed in allowed_namespace.items():
            tree = _index_file(PKG / filename).tree
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name not in allowed:
                    arg_names = [a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)]
                    self.assertNotIn("args", arg_names, f"{filename}:{node.name} must not take namespace `args`")


class CallSiteTests(unittest.TestCase):
    """Two logs per call site drift (Godfather's open risk 7): every ledger line goes through journal.ledger, and
    each call names one documented kind."""

    def test_every_ledger_call_names_a_documented_kind_and_nothing_else_writes_the_ledger(self) -> None:
        calls = 0
        for path in _pkg_files():
            idx = _index_file(path)
            for node in idx.calls:
                if (isinstance(node.func, ast.Attribute) and node.func.attr == "ledger"
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "journal"):
                    calls += 1
                    kinds = [k.value for k in node.keywords if k.arg == "kind"]
                    kind = kinds[0] if kinds else None
                    self.assertIsInstance(kind, ast.Constant, f"{path.name}:{node.lineno}")
                    self.assertIn(str(cast(ast.Constant, kind).value), events.KINDS, f"{path.name}:{node.lineno}")
            if path.name != "journal.py":
                self.assertNotIn('"ledger.log"', idx.source.replace("'ledger.log'", '"ledger.log"'), path.name)
        self.assertGreaterEqual(calls, 35)

    def test_every_override_is_audited_by_journal_override(self) -> None:
        """Review of fathers#172, finding 4: the override audit (record entry and `override` ledger line) is one
        helper, not a copy per command."""
        sites: list[str] = []
        for path in _pkg_files():
            if path.name == "journal.py":
                continue
            text = _index_file(path).source
            self.assertFalse('kind="override"' in text, f"{path.name} audits an override by hand")
            self.assertFalse('setdefault("overrides", []).append' in text, f"{path.name} audits an override by hand")
            self.assertFalse('["overrides"].append' in text, f"{path.name} audits an override by hand")
            sites += [path.name] * text.count("journal.override(")
        self.assertEqual(sorted(sites), ["done.py", "done.py", "land.py", "land.py", "land.py", "land.py",
                                        "lanes.py", "lanes.py", "lanes.py", "postmerge.py", "promote.py",
                                        "review.py", "review_gate.py"])


class LifecycleGuardTests(unittest.TestCase):
    """Static guard (fathers#69): no module outside `state.py` / `lifecycle.py` mutates lane status or saves a lane
    record directly, nor compares lane record status against string literals."""

    def test_no_direct_status_mutation_or_record_save_outside_lifecycle_and_state(self) -> None:
        status_literals = set(lifecycle.STATUSES)
        transition_kinds = {"lane.started", "lane.kept", *lifecycle.EVENT_KINDS.values()}
        violations: list[str] = []

        for path in _pkg_files():
            idx = _index_file(path)
            if path.name != "lifecycle.py":
                for assign in idx.assigns:
                    for target in assign.targets:
                        if (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                                and target.slice.value in ("post_merge", "keep")):
                            violations.append(f"{path.name}:{assign.lineno}: direct [{target.slice.value!r}] = assignment")
                for call in idx.calls:
                    if (isinstance(call.func, ast.Attribute)
                            and call.func.attr in ("update", "setdefault", "pop", "__setitem__")):
                        for kw in call.keywords:
                            if kw.arg in ("post_merge", "keep"):
                                violations.append(f"{path.name}:{call.lineno}: .{call.func.attr}({kw.arg}=...) call")
                        if (call.func.attr in ("setdefault", "pop", "__setitem__") and call.args
                                and isinstance(call.args[0], ast.Constant)
                                and call.args[0].value in ("post_merge", "keep")):
                            violations.append(f"{path.name}:{call.lineno}: .{call.func.attr}({call.args[0].value!r}) call")
            if path.name in ("state.py", "lifecycle.py"):
                continue
            for call in idx.calls:
                # 1. No `state.save(...)` or `store.save(...)` calls outside state.py / lifecycle.py
                if isinstance(call.func, ast.Attribute) and call.func.attr == "save":
                    violations.append(f"{path.name}:{call.lineno}: direct .save() call")
                # 2. No `record.update(status=...)` calls on lane records
                if isinstance(call.func, ast.Attribute) and call.func.attr == "update":
                    for kw in call.keywords:
                        if kw.arg == "status":
                            val_const = kw.value.value if isinstance(kw.value, ast.Constant) else None
                            recv = call.func.value.id if isinstance(call.func.value, ast.Name) else ""
                            if val_const in status_literals or recv in ("record", "rec", "current", "latest"):
                                violations.append(f"{path.name}:{call.lineno}: .update(status=...) call")
                # 4. No `kind="lane.<transition>"` outside lifecycle.py (events.py only lists KINDS dict)
                if path.name != "events.py":
                    for kw in call.keywords:
                        if (kw.arg == "kind" and isinstance(kw.value, ast.Constant)
                                and kw.value.value in transition_kinds):
                            violations.append(f"{path.name}:{call.lineno}: emits {kw.value.value!r} outside lifecycle.py")
            # 3. No `...["status"] = <lane status>` or `record["status"] = ...`
            for assign in idx.assigns:
                for target in assign.targets:
                    if (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                            and target.slice.value == "status"):
                        val_const = assign.value.value if isinstance(assign.value, ast.Constant) else None
                        if val_const in status_literals or path.name != "reviewer.py":
                            violations.append(f"{path.name}:{assign.lineno}: direct ['status'] = assignment")
            # 5. No comparing a lane record's status against lane status literals
            for cmp in idx.compares:
                operands = [cmp.left, *cmp.comparators]
                reads_status = any(
                    (isinstance(op, ast.Subscript) and isinstance(op.slice, ast.Constant)
                     and op.slice.value == "status")
                    or (isinstance(op, ast.Call) and isinstance(op.func, ast.Attribute)
                        and op.func.attr == "get" and op.args
                        and isinstance(op.args[0], ast.Constant) and op.args[0].value == "status")
                    or (isinstance(op, ast.Name) and op.id == "status")
                    for op in operands
                )
                if not reads_status:
                    continue
                has_lane_literal = False
                for op in operands:
                    if ((isinstance(op, ast.Constant) and op.value in status_literals)
                            or (isinstance(op, (ast.Tuple, ast.List, ast.Set))
                                and any(isinstance(el, ast.Constant) and el.value in status_literals
                                        for el in op.elts))
                            or (isinstance(op, ast.Attribute) and op.attr in ("OCCUPYING", "LANDED"))):
                        has_lane_literal = True
                if has_lane_literal:
                    violations.append(f"{path.name}:{cmp.lineno}: compares status against literal/constant")

        self.assertEqual(violations, [], "\n".join(violations))

    def test_lifecycle_never_calls_config_current(self) -> None:
        for node in _index_file(PKG / "lifecycle.py").attrs:
            if node.attr == "current":
                self.fail(f"lifecycle.py:{node.lineno} must take an explicit state.Store, never config.current()")


class Tidy6ArchitectureTests(unittest.TestCase):
    """Static architecture guards for Tidy 6 seams (fathers#74, fathers#216)."""

    def test_no_gh_subprocess_outside_github_module(self) -> None:
        for py_file in _pkg_files():
            if py_file.name == "github.py":
                continue
            idx = _index_file(py_file)
            for node in idx.calls:
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr in ("run", "Popen")
                        and isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
                    src = ast.get_source_segment(idx.source, node) or ""
                    self.assertNotIn("gh", src.lower(), f"{py_file.name} invokes gh outside github.py: {src}")

    def test_no_import_cycle_or_local_import_among_core_modules(self) -> None:
        graph: dict[str, set[str]] = {}
        for mod in CORE_MODULES:
            top, local = _imported_internal_modules(PKG / f"{mod}.py")
            local_core = local & CORE_MODULES
            self.assertEqual(local_core, set(), f"{mod}.py has function-local imports: {local_core}")
            graph[mod] = top & CORE_MODULES

        visiting: set[str] = set()
        visited: set[str] = set()

        def dfs(node: str, trail: list[str]) -> None:
            if node in visited:
                return
            visiting.add(node)
            for nxt in graph[node]:
                if nxt in visiting:
                    self.fail(f"import cycle among core modules: {' -> '.join([*trail, nxt])}")
                dfs(nxt, [*trail, nxt])
            visiting.remove(node)
            visited.add(node)

        for mod in sorted(CORE_MODULES):
            dfs(mod, [mod])

        history_top, history_local = _imported_internal_modules(PKG / "history.py")
        self.assertEqual(history_local, set(), f"history.py has local imports: {history_local}")
        self.assertNotIn("review", history_top | history_local)
        self.assertIn("review_record", history_top)

    def test_review_receipts_written_only_by_review_record_transition(self) -> None:
        writers: list[tuple[str, str]] = []
        for py_file in _pkg_files():
            for func_name, inner in _index_file(py_file).receipt_calls:
                func = cast(ast.Attribute, inner.func)
                if (func.attr == "write"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "receipts"):
                    kinds = [
                        *(inner.args[3:4]),
                        *(kw.value for kw in inner.keywords if kw.arg == "kind"),
                    ]
                    for kind_arg in kinds:
                        if isinstance(kind_arg, ast.Constant) and kind_arg.value == "review":
                            self.fail(f"{py_file.name}:{func_name} calls receipts.write(..., 'review')")
                if func.attr == "write_json":
                    target_src = ast.unparse(inner.args[0]) if inner.args else ""
                    if py_file.name == "review_record.py" or '"review"' in target_src or "'review'" in target_src:
                        writers.append((py_file.name, func_name))
        self.assertEqual(writers, [("review_record.py", "_transition")])

    def test_post_merge_and_recheck_receipts_written_only_by_postmerge(self) -> None:
        writers: list[tuple[str, str]] = []
        for py_file in _pkg_files():
            for func_name, inner in _index_file(py_file).receipt_calls:
                func = cast(ast.Attribute, inner.func)
                if (func.attr == "write"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "receipts"):
                    kinds = [
                        *(inner.args[3:4]),
                        *(kw.value for kw in inner.keywords if kw.arg == "kind"),
                    ]
                    for kind_arg in kinds:
                        if isinstance(kind_arg, ast.Constant) and kind_arg.value in ("post-merge", "recheck"):
                            self.fail(f"{py_file.name}:{func_name} calls receipts.write(..., {kind_arg.value!r})")
                if func.attr == "write_json":
                    target_src = ast.unparse(inner.args[0]) if inner.args else ""
                    if py_file.name == "postmerge.py" or any(
                        k in target_src for k in ('"post-merge"', "'post-merge'", '"recheck"', "'recheck'")
                    ):
                        writers.append((py_file.name, func_name))
        self.assertEqual(writers, [("postmerge.py", "_write_receipt")])


class DocsHygieneTests(unittest.TestCase):
    """Documentation hygiene guards: no private former-repo issue references, glossary/module agreement, and valid
    anchors."""

    def test_no_private_issue_references_in_docs(self) -> None:
        doc_files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
        offenders: list[str] = []
        allowed_prefixes = {"fathers", "maxvyshniakov/fathers"}
        for path in doc_files:
            rel = path.relative_to(ROOT).as_posix()
            for lineno, line in _prose_lines(path):
                for match in ISSUE_REF.finditer(line):
                    prefix = match.group("prefix") or ""
                    if prefix.lower() not in allowed_prefixes:
                        offenders.append(f"{rel}:{lineno}: {match.group(0)}")
        self.assertEqual(offenders, [], "Found non-fathers #NNN references:\n" + "\n".join(offenders))

    def test_glossary_avoid_terms_do_not_match_module_names(self) -> None:
        glossary = (ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
        avoided: set[str] = set()
        for match in AVOID_CLAUSE.finditer(glossary):
            for part in re.split(r"[,;]|\bor\b", match.group(1)):
                term = part.strip().strip("`").lower()
                if term:
                    avoided.add(term)
        modules = {
            path.stem.lower()
            for path in _pkg_files()
            if path.stem not in ("__init__", "__main__")
        }
        overlap = sorted(avoided & modules)
        self.assertEqual(overlap, [], f"Glossary _Avoid_ terms match module names in lanefather/: {overlap}")

    def test_api_status_anchor_resolves_in_reference(self) -> None:
        api_text = (ROOT / "docs" / "api.md").read_text(encoding="utf-8")
        match = re.search(r"`lanefather status --json`[^\n]*\[reference\]\(reference\.md#([a-z0-9-]+)\)", api_text)
        self.assertIsNotNone(match, "docs/api.md must link `lanefather status --json` to a reference.md anchor")
        assert match is not None
        anchor = match.group(1)
        ref_headings = {
            _slugify(heading.group(1))
            for _, line in _prose_lines(ROOT / "docs" / "reference.md")
            if (heading := HEADING.match(line))
        }
        self.assertIn(anchor, ref_headings, f"Anchor #{anchor} not found in docs/reference.md")


class ChangelogLayoutGuardTests(unittest.TestCase):
    """Static layout guard for `lanefather/CHANGELOG.md` (fathers#104)."""

    def test_the_packages_changelog_parses_and_keeps_its_layout(self) -> None:
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        found = changelog.entries(text)
        releases = [e["version"] for e in found if e["version"]]
        self.assertIn("0.1.0", releases)
        self.assertTrue(all(e["text"] and e["section"] in CHANGELOG_SECTIONS for e in found))
        lines = text.splitlines()
        self.assertEqual(lines[0], "# Lanefather Changelog")
        headings = [line for line in lines if line.startswith("## ")]
        self.assertEqual(headings[0], "## Unreleased")
        dates = [re.fullmatch(r"## \d+\.\d+\.\d+\S* - (\d{4}-\d\d-\d\d)", h) for h in headings[1:]]
        self.assertTrue(all(dates), headings)
        self.assertEqual([d[1] for d in dates if d], sorted((d[1] for d in dates if d), reverse=True))
        section: list[str] = []
        for line in [*lines, "## end"]:
            if line.startswith("## "):
                self.assertEqual(sorted(section, key=CHANGELOG_SECTIONS.index), section, f"section order before {line}")
                section = []
            elif line.startswith("### "):
                section.append(line[4:])


class AmbientConfigAndStateGuardTests(unittest.TestCase):
    """Static architecture guard (fathers#234): zero ambient `config.current()` / `config.use()` / `config._current` /
    `config.prog()`, zero module-level `state.*` (`state_dir`, `record_path`, `receipt_path`, `load`, `save`, `lanes`,
    `locked`), and zero `journal.logs()` / `journal.log_path()` definitions or references across `lanefather/lanefather/`."""

    def test_no_ambient_config_state_or_journal_helpers_across_package(self) -> None:
        forbidden_config = {"current", "use", "_current", "prog"}
        forbidden_state = {"state_dir", "record_path", "receipt_path", "load", "save", "lanes", "locked"}
        forbidden_journal = {"logs", "log_path"}
        violations: list[str] = []

        for name in forbidden_config:
            if hasattr(config, name):
                violations.append(f"config.{name} still exists on lanefather.config")
        for name in forbidden_state:
            if hasattr(state, name):
                violations.append(f"state.{name} still exists at module level on lanefather.state")

        for py_file in _pkg_files():
            idx = _index_file(py_file)
            for node in idx.tree.body:
                if isinstance(node, ast.FunctionDef):
                    if py_file.name == "config.py" and node.name in forbidden_config:
                        violations.append(f"config.py:{node.lineno} defines {node.name}")
                    if py_file.name == "state.py" and node.name in forbidden_state:
                        violations.append(f"state.py:{node.lineno} defines module-level {node.name}")
                    if py_file.name == "journal.py" and node.name in forbidden_journal:
                        violations.append(f"journal.py:{node.lineno} defines module-level {node.name}")
            for attr in idx.attrs:
                if isinstance(attr.value, ast.Name):
                    if attr.value.id == "config" and attr.attr in forbidden_config:
                        violations.append(f"{py_file.name}:{attr.lineno} references config.{attr.attr}")
                    elif attr.value.id == "state" and attr.attr in forbidden_state:
                        violations.append(f"{py_file.name}:{attr.lineno} references state.{attr.attr}")
                    elif attr.value.id == "journal" and attr.attr in forbidden_journal:
                        violations.append(f"{py_file.name}:{attr.lineno} references journal.{attr.attr}")

        self.assertEqual(violations, [], "\n".join(violations))


class PromoteImportGuardTests(unittest.TestCase):
    """Static import guard (fathers#73): `lanes.py` must not import `promote`."""

    def test_lanes_does_not_import_promote(self) -> None:
        imported: set[str] = set()
        for node in _index_file(PKG / "lanes.py").imports:
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
                for alias in node.names:
                    imported.add(alias.name)
            else:
                for alias in node.names:
                    imported.add(alias.name)
        self.assertNotIn("promote", imported)
        self.assertNotIn("lanefather.promote", imported)


class ReceiptGuardTests(unittest.TestCase):
    """Static architecture guards for fathers#71."""

    def test_receipts_module_never_uses_ambient_config_or_state_dir(self) -> None:
        violations: list[str] = []
        for mod_name in ("receipts.py", "review_record.py", "postmerge.py"):
            for node in _index_file(PKG / mod_name).attrs:
                if node.attr in ("current", "state_dir"):
                    violations.append(f"{mod_name}:{node.lineno}: ambient .{node.attr}() call")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_no_receipt_file_access_outside_receipts_and_state(self) -> None:
        violations: list[str] = []
        for path in _pkg_files():
            if path.name in ("receipts.py", "review_record.py", "postmerge.py", "state.py"):
                continue
            idx = _index_file(path)
            for node in idx.calls:
                func = node.func
                if ((isinstance(func, ast.Attribute) and func.attr == "receipt_path")
                        or (isinstance(func, ast.Name) and func.id == "receipt_path")):
                    violations.append(f"{path.name}:{node.lineno}: calls receipt_path() outside receipts.py")
            for const in idx.str_constants:
                if (id(const) not in idx.docstring_ids
                        and any(str(const.value).endswith(suffix) for suffix in RECEIPT_SUFFIXES)):
                    violations.append(f"{path.name}:{const.lineno}: hardcodes receipt filename {const.value!r}")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_no_module_outside_receipts_reads_receipt_status_tree_or_toolchain(self) -> None:
        forbidden_keys = {"status", "tree", "toolchain"}
        violations: list[str] = []

        def receiver_name(expr: ast.AST) -> str | None:
            if isinstance(expr, ast.Name):
                return expr.id
            if (isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or)
                    and expr.values and isinstance(expr.values[0], ast.Name)):
                return expr.values[0].id
            return None

        for path in _pkg_files():
            if path.name in ("receipts.py", "review_record.py", "postmerge.py"):
                continue
            idx = _index_file(path)
            # Track Assign targets so `x["status"] = ...` is distinguished from reads.
            write_subscripts: set[int] = set()
            for assign in idx.assigns:
                for target in assign.targets:
                    if isinstance(target, ast.Subscript):
                        write_subscripts.add(id(target))

            # Subscript read: `<receipt>["status" | "tree" | "toolchain"]`
            for sub in idx.subscripts:
                if (id(sub) not in write_subscripts
                        and isinstance(sub.slice, ast.Constant) and sub.slice.value in forbidden_keys):
                    name = receiver_name(sub.value)
                    if name in RECEIPT_NAMES or sub.slice.value == "toolchain":
                        violations.append(
                            f"{path.name}:{sub.lineno}: reads {name or '<expr>'}[{sub.slice.value!r}]"
                        )
            # `.get("status" | "tree" | "toolchain", ...)` read
            for call in idx.calls:
                if (isinstance(call.func, ast.Attribute)
                        and call.func.attr == "get" and call.args
                        and isinstance(call.args[0], ast.Constant) and call.args[0].value in forbidden_keys):
                    name = receiver_name(call.func.value)
                    if name in RECEIPT_NAMES or call.args[0].value == "toolchain":
                        violations.append(
                            f"{path.name}:{call.lineno}: reads {name or '<expr>'}.get({call.args[0].value!r})"
                        )

        self.assertEqual(violations, [], "\n".join(violations))


class VerifyArchitectureGuardTests(unittest.TestCase):
    """Static architecture guards for `lanefather.verify` (fathers#70)."""

    def test_no_subprocess_call_of_verify_path_outside_verify_module(self) -> None:
        offenders: list[str] = []
        for path in _pkg_files():
            if path.name == "verify.py":
                continue
            for node in _index_file(path).calls:
                if not _is_subprocess_exec(node):
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

    def test_review_does_not_import_checks(self) -> None:
        for mod_name in ("review.py", "land.py"):
            imported_checks: list[str] = []
            for node in _index_file(PKG / mod_name).imports:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "checks" or alias.name.endswith(".checks"):
                            imported_checks.append(f"{mod_name}:{node.lineno}: import {alias.name}")
                else:
                    if node.module == "checks" or (node.module or "").endswith(".checks"):
                        imported_checks.append(f"{mod_name}:{node.lineno}: from {node.module} import ...")
                    for alias in node.names:
                        if alias.name == "checks":
                            imported_checks.append(f"{mod_name}:{node.lineno}: from {node.module} import checks")
            self.assertEqual(imported_checks, [])


if __name__ == "__main__":
    unittest.main()
