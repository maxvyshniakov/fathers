"""Tests for `lanefather.receipts` (fathers#71, Tidy 3):
- Receipt I/O for every kind (`check`/`receipt`, `first`, `post-merge`, `recheck`, `review`, `gate`, `perf`) with an
  explicit `state.Store` and read-only guarantees.
- Typed `check_verdict` (`NoReceipt`, `Green`, `Red`, `StaleTree`, `StaleToolchain`) and `review_verdict`
  (`Absent`, `Running`, `Died`, `Failed`, `Exempt`, `Unmarked`, `Complete`).
- Table test feeding receipt fixtures through `land`, `check`, `list`, `show` and `status`, pinning today's outputs
  and verifying byte-identical `--json` outputs across `list --json`, `show --json` and `status --json`.
- Static AST guards enforcing that `receipts.py` owns receipt reads/writes and that no module outside `receipts.py`
  reads a lane receipt's `status`, `tree` or `toolchain`.
"""
from __future__ import annotations

import ast
import io
import json
import os
import pathlib
import shutil
import tempfile
from typing import Any
import unittest
from unittest import mock

from support import RUST_STUB, LaneCase

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "lanefather"

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


class ReceiptGuardTests(unittest.TestCase):
    """Static architecture guards for fathers#71."""

    def test_receipts_module_never_uses_ambient_config_or_state_dir(self) -> None:
        violations: list[str] = []
        for mod_name in ("receipts.py", "review_record.py"):
            tree = ast.parse((PKG / mod_name).read_text(encoding="utf-8"), filename=mod_name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in ("current", "state_dir"):
                    violations.append(f"{mod_name}:{node.lineno}: ambient .{node.attr}() call")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_no_receipt_file_access_outside_receipts_and_state(self) -> None:
        violations: list[str] = []
        for path in sorted(PKG.glob("*.py")):
            if path.name in ("receipts.py", "review_record.py", "state.py"):
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if ((isinstance(func, ast.Attribute) and func.attr == "receipt_path")
                            or (isinstance(func, ast.Name) and func.id == "receipt_path")):
                        violations.append(f"{path.name}:{node.lineno}: calls receipt_path() outside receipts.py")
            # Walk statements excluding docstrings for receipt filename suffixes.
            self._check_non_docstring_constants(tree, path.name, violations)
        self.assertEqual(violations, [], "\n".join(violations))

    def _check_non_docstring_constants(self, tree: ast.Module, filename: str, violations: list[str]) -> None:
        docstring_ids: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstring_ids.add(id(first.value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstring_ids
                    and any(node.value.endswith(suffix) for suffix in RECEIPT_SUFFIXES)):
                violations.append(f"{filename}:{node.lineno}: hardcodes receipt filename {node.value!r}")

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

        for path in sorted(PKG.glob("*.py")):
            if path.name in ("receipts.py", "review_record.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            # Track Assign targets so `x["status"] = ...` is distinguished from reads.
            write_subscripts: set[int] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Subscript):
                            write_subscripts.add(id(target))

            for node in ast.walk(tree):
                # Subscript read: `<receipt>["status" | "tree" | "toolchain"]`
                if (isinstance(node, ast.Subscript) and id(node) not in write_subscripts
                        and isinstance(node.slice, ast.Constant) and node.slice.value in forbidden_keys):
                    name = receiver_name(node.value)
                    if name in RECEIPT_NAMES or node.slice.value == "toolchain":
                        violations.append(
                            f"{path.name}:{node.lineno}: reads {name or '<expr>'}[{node.slice.value!r}]"
                        )
                # `.get("status" | "tree" | "toolchain", ...)` read
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "get" and node.args
                        and isinstance(node.args[0], ast.Constant) and node.args[0].value in forbidden_keys):
                    name = receiver_name(node.func.value)
                    if name in RECEIPT_NAMES or node.args[0].value == "toolchain":
                        violations.append(
                            f"{path.name}:{node.lineno}: reads {name or '<expr>'}.get({node.args[0].value!r})"
                        )

        self.assertEqual(violations, [], "\n".join(violations))


class ReceiptStoreAndVerdictUnitTests(unittest.TestCase):
    """Focused unit tests for `receipts` I/O, `check_verdict`, `review_verdict`, and `lane_view`."""

    def setUp(self) -> None:
        from lanefather import config, state
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-receipts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir_a = self.tmp / "state-a"
        self.dir_b = self.tmp / "state-b"
        self.store_a = state.Store(config.Config(project="Alpha"), directory=self.dir_a)
        self.store_b = state.Store(config.Config(project="Beta"), directory=self.dir_b)

    def test_reads_and_writes_every_receipt_kind_against_explicit_store(self) -> None:
        from lanefather import receipts, review_record

        # Reading from a missing store directory never creates it.
        for kind in ("check", "receipt", "first", "post-merge", "recheck", "review", "gate", "perf"):
            self.assertIsNone(receipts.read(self.store_a, "lane1", kind))  # type: ignore[arg-type]
        self.assertFalse(self.dir_a.exists())

        sha = "abcdef1234567890" * 2 + "12345678"
        cases: list[tuple[receipts.ReceiptKind, str, str]] = [
            ("check", "lane1", "lane1.receipt.json"),
            ("receipt", "lane1", "lane1.receipt.json"),
            ("first", "lane1", "lane1.first.json"),
            ("post-merge", "lane1", "lane1.post-merge.json"),
            ("recheck", "lane1", "lane1.recheck.json"),
            ("gate", sha, f"promote-{sha[:10]}.gate.json"),
            ("perf", sha, f"perf-{sha[:10]}.perf.json"),
        ]
        for kind, target, expected_name in cases:
            with self.subTest(kind=kind):
                payload = {"kind": kind, "status": "green", "tree": "a" * 40, "finished": "2026-11-21T10:00:00Z"}
                written = receipts.write(self.store_a, target, payload, kind)
                self.assertEqual(written, self.dir_a / expected_name)
                self.assertEqual(receipts.read(self.store_a, target, kind), payload)
                # Explicit Store isolation: store_b remains untouched and uncreated.
                self.assertIsNone(receipts.read(self.store_b, target, kind))
                self.assertFalse(self.dir_b.exists())

        with self.assertRaises(ValueError):
            receipts.write(self.store_a, "lane1", {"status": "exempt"}, "review")
        review_record.ReviewRecord.exempt(self.store_a, "lane1", "docs only")

        self.assertEqual(len(receipts.gates(self.store_a)), 1)
        self.assertEqual(len(receipts.reviews(self.store_a)), 1)
        self.assertEqual(
            receipts.newest_gate(self.store_a),
            {"sha": None, "status": "green", "at": "2026-11-21T10:00:00Z"},
        )

    def test_ensure_first_preserves_same_incarnation_and_replaces_mismatched_incarnation(self) -> None:
        from lanefather import receipts

        rec_v1 = {"id": "a", "created": "2026-11-20T00:00:00Z"}
        first_v1 = receipts.ensure_first(self.store_a, rec_v1, None, "h1", "t1", ["c1"])
        self.assertEqual(first_v1, {"head": "h1", "tree": "t1", "failed_checks": ["c1"],
                                    "lane_created": "2026-11-20T00:00:00Z"})
        # Second check on same incarnation keeps first_v1 even when head/tree change.
        again = receipts.ensure_first(self.store_a, rec_v1, None, "h2", "t2", [])
        self.assertEqual(again, first_v1)

        # Re-registered lane with same id and new `created` replaces the stale incarnation's first.json.
        rec_v2 = {"id": "a", "created": "2026-11-21T00:00:00Z"}
        first_v2 = receipts.ensure_first(self.store_a, rec_v2, None, "h3", "t3", ["c9"])
        self.assertEqual(first_v2, {"head": "h3", "tree": "t3", "failed_checks": ["c9"],
                                    "lane_created": "2026-11-21T00:00:00Z"})

    def test_check_verdict_table(self) -> None:
        from lanefather import receipts

        wt = self.tmp / "wt"
        wt.mkdir()
        record = {"id": "a", "status": "active", "worktree": str(wt)}
        pin_v1 = {"rustc": "1.98.1"}
        pin_v2 = {"rustc": "1.99.0"}

        self.assertEqual(receipts.check_verdict(record, None, "t1"), receipts.NoReceipt())
        self.assertEqual(
            receipts.check_verdict(record, {"status": "green", "tree": "t1", "toolchain": pin_v1,
                                            "platform": "darwin-arm64"}, "t1", pin_v1, platform="darwin-arm64"),
            receipts.Green(tree="t1", tree_match=True),
        )
        # Without `toolchain` argument, toolchain/platform differences are not checked (land/list/show/status).
        self.assertEqual(
            receipts.check_verdict(record, {"status": "green", "tree": "t1", "toolchain": pin_v1,
                                            "platform": "linux-x64"}, "t1"),
            receipts.Green(tree="t1", tree_match=True),
        )
        # Toolchain changed -> StaleToolchain(toolchain_changed=True).
        self.assertEqual(
            receipts.check_verdict(record, {"status": "green", "tree": "t1", "toolchain": pin_v1,
                                            "platform": "darwin-arm64"}, "t1", pin_v2, platform="darwin-arm64"),
            receipts.StaleToolchain(tree="t1", tree_match=True, toolchain_changed=True, platform="darwin-arm64"),
        )
        # Pin removed (toolchain=None explicitly) -> StaleToolchain(toolchain_changed=True).
        self.assertEqual(
            receipts.check_verdict(record, {"status": "green", "tree": "t1", "toolchain": pin_v1,
                                            "platform": "darwin-arm64"}, "t1", None, platform="darwin-arm64"),
            receipts.StaleToolchain(tree="t1", tree_match=True, toolchain_changed=True, platform="darwin-arm64"),
        )
        # Platform mismatch -> StaleToolchain(toolchain_changed=False).
        self.assertEqual(
            receipts.check_verdict(record, {"status": "green", "tree": "t1", "toolchain": pin_v1,
                                            "platform": "linux-x64"}, "t1", pin_v1, platform="darwin-arm64"),
            receipts.StaleToolchain(tree="t1", tree_match=True, toolchain_changed=False, platform="darwin-arm64"),
        )
        # Tree mismatch -> StaleTree (carries underlying status).
        self.assertEqual(
            receipts.check_verdict(record, {"status": "red", "tree": "t0"}, "t1", pin_v1, platform="darwin-arm64"),
            receipts.StaleTree(receipt_tree="t0", tip_tree="t1", status="red"),
        )
        # Red receipt with failed suites, not_run suites, and classified regression.
        red_receipt = {
            "status": "red",
            "tree": "t1",
            "results": [
                {"suite": "structure", "status": "failed"},
                {"suite": "lib", "status": "not_run"},
                {"suite": "docs", "status": "passed"},
            ],
            "classification": {
                "base": "deadbeef12345678",
                "checks": [
                    {"check": "structure-check", "class": "regression", "alone": {"status": "failed"}},
                ],
            },
        }
        self.assertEqual(
            receipts.check_verdict(record, red_receipt, "t1", pin_v1, platform="darwin-arm64"),
            receipts.Red(
                failed=("structure",),
                unstarted=("lib",),
                regressions=("structure-check",),
                rerun_alone=True,
                classification_base="deadbeef12345678",
                tree="t1",
                tree_match=True,
                status="red",
            ),
        )

    def test_check_status_word_covers_all_verdicts_and_defaults(self) -> None:
        from dataclasses import replace

        from lanefather import receipts

        rec: dict[str, Any] = {"id": "a", "status": "active", "ticket": "none", "claims": [], "after": []}
        base_view = receipts.lane_view(self.store_a, rec, tip_tree=None, repository=None)
        cases: list[tuple[receipts.CheckVerdict, str]] = [
            (receipts.NoReceipt(), "not run"),
            (receipts.Green(tree="t1"), "green"),
            (receipts.StaleToolchain(tree="t1"), "green"),
            (receipts.Red(status="red"), "red"),
            (receipts.Red(status=None), "fallback"),
            (receipts.StaleTree(receipt_tree="t0", tip_tree="t1", status="green"), "green"),
            (receipts.StaleTree(receipt_tree="t0", tip_tree="t1", status="red"), "red"),
            (receipts.StaleTree(receipt_tree="t0", tip_tree="t1", status=None), "fallback"),
        ]
        for verdict, expected in cases:
            with self.subTest(verdict=verdict):
                view = replace(base_view, check=verdict)
                self.assertEqual(view._check_status_word(default="fallback"), expected)  # pyright: ignore[reportPrivateUsage]


class ReceiptReadersTableTests(LaneCase):
    """Acceptance table test (fathers#71): feeds check and review receipt fixtures through `land`, `check`, `list`,
    `show` and `status`, pinning today's outputs (including current disagreements) and verifying `lane_view` parity."""

    def setUp(self) -> None:
        super().setUp()
        self.git(self.repo, "remote", "add", "origin", "git@github.com:fixture/project.git")
        self.write(self.integration, "tools/rust-toolchain.py", RUST_STUB)
        self.commit(
            self.integration,
            {
                "tools/rust-toolchain.py": RUST_STUB,
                "rust-toolchain.toml": '[toolchain]\nchannel = "1.98.1"\n',
            },
            "pin rust toolchain",
        )
        self.env["FIXTURE_RUST"] = "1.98.1"

    def test_check_receipt_fixtures_across_land_check_list_show_and_status(self) -> None:
        from lanefather import checks, config, land, plan, receipts, results, state

        worktree = pathlib.Path(self.created("fx", "lib/**"))
        self.commit(worktree, {"lib/api.txt": "foo\nfx\n"})
        # Register the project in the machine registry via a normal command (`start` already ran in `created`).
        tip = self.git(worktree, "rev-parse", "HEAD")
        tip_tree = self.git(worktree, "rev-parse", "HEAD^{tree}")
        stale_tree = "0" * 40

        with mock.patch.dict(os.environ, self.env, clear=False):
            cfg = config.load(self.integration / ".lanefather.toml")
            config.use(cfg)
            self.addCleanup(config.use, None)
            host_plat = results.host_platform()
            current_tc = plan.toolchain_state(worktree)
            old_tc = {"rustc": "rustc 1.0.0"}

        fixtures: list[tuple[str, dict[str, Any] | None, dict[str, Any]]] = [
            (
                "no_receipt",
                None,
                {
                    "land_problem": "no receipt for lane fx: run lanefather check fx in its worktree",
                    "check_short_circuit": False,
                    "check_notice": None,
                    "list_receipt": "no receipt",
                    "show_checks": {"status": "not run", "current": None, "tree_match": None},
                    "status_receipt": {"status": "not run", "tree_match": None, "has_path": False},
                },
            ),
            (
                "green_current",
                {
                    "version": 1, "lane": "fx", "head": tip, "tree": tip_tree, "status": "green",
                    "toolchain": current_tc, "platform": host_plat, "results": [], "check_results": {},
                },
                {
                    "land_problem": None,
                    "check_short_circuit": True,
                    "check_notice": f"Receipt current: green for tree {tip_tree} on {host_plat}",
                    "list_receipt": "green",
                    "show_checks": {"status": "green", "current": None, "tree_match": True},
                    "status_receipt": {"status": "green", "tree_match": True, "has_path": True},
                },
            ),
            (
                "stale_toolchain",
                {
                    "version": 1, "lane": "fx", "head": tip, "tree": tip_tree, "status": "green",
                    "toolchain": old_tc, "platform": host_plat, "results": [], "check_results": {},
                },
                {
                    # Disagreement preserved in Tidy 3 (#71): land ignores toolchain, show cannot certify it,
                    # check reruns with toolchain notice.
                    "land_problem": None,
                    "check_short_circuit": False,
                    "check_notice": "lane: the Rust toolchain changed since the receipt (#571); running the checks again",
                    "list_receipt": "green",
                    "show_checks": {"status": "green", "current": None, "tree_match": True},
                    "status_receipt": {"status": "green", "tree_match": True, "has_path": True},
                },
            ),
            (
                "stale_platform",
                {
                    "version": 1, "lane": "fx", "head": tip, "tree": tip_tree, "status": "green",
                    "toolchain": current_tc, "platform": "freebsd-riscv64", "results": [], "check_results": {},
                },
                {
                    "land_problem": None,
                    "check_short_circuit": False,
                    "check_notice": f"lane: the receipt was not made on {host_plat}; running the checks again",
                    "list_receipt": "green",
                    "show_checks": {"status": "green", "current": None, "tree_match": True},
                    "status_receipt": {"status": "green", "tree_match": True, "has_path": True},
                },
            ),
            (
                "stale_tree_green",
                {
                    "version": 1, "lane": "fx", "head": tip, "tree": stale_tree, "status": "green",
                    "toolchain": current_tc, "platform": host_plat, "results": [], "check_results": {},
                },
                {
                    "land_problem": (
                        f"the receipt is stale: it covers tree {stale_tree}, the lane tip {tip[:10]} has tree "
                        f"{tip_tree}; rerun lanefather check fx"
                    ),
                    "check_short_circuit": False,
                    "check_notice": None,
                    "list_receipt": "green (stale)",
                    "show_checks": {"status": "green", "current": False, "tree_match": False},
                    "status_receipt": {"status": "green", "tree_match": False, "has_path": True},
                },
            ),
            (
                "stale_tree_red",
                {
                    "version": 1, "lane": "fx", "head": tip, "tree": stale_tree, "status": "red",
                    "toolchain": current_tc, "platform": host_plat,
                    "results": [{"suite": "lib", "status": "failed"}], "check_results": {},
                },
                {
                    "land_problem": (
                        f"the receipt is stale: it covers tree {stale_tree}, the lane tip {tip[:10]} has tree "
                        f"{tip_tree}; rerun lanefather check fx"
                    ),
                    "check_short_circuit": False,
                    "check_notice": None,
                    "list_receipt": "red (stale)",
                    "show_checks": {"status": "red", "current": False, "tree_match": False},
                    "status_receipt": {"status": "red", "tree_match": False, "has_path": True},
                },
            ),
            (
                "red_with_regression_and_unstarted",
                {
                    "version": 1, "lane": "fx", "head": tip, "tree": tip_tree, "status": "red",
                    "toolchain": current_tc, "platform": host_plat,
                    "results": [
                        {"suite": "structure", "status": "failed"},
                        {"suite": "lib", "status": "not_run"},
                    ],
                    "classification": {
                        "base": "1234567890abcdef",
                        "checks": [
                            {"check": "structure-check", "class": "regression", "alone": {"status": "failed"}},
                        ],
                    },
                    "check_results": {},
                },
                {
                    "land_problem": (
                        f"the receipt for tree {tip_tree} is red (structure; not run: lib); "
                        "regression: structure-check fails on the lane again when rerun by itself and passes on its "
                        "base 1234567890"
                    ),
                    "check_short_circuit": False,
                    "check_notice": None,
                    "list_receipt": "red",
                    "show_checks": {"status": "red", "current": None, "tree_match": True},
                    "status_receipt": {"status": "red", "tree_match": True, "has_path": True},
                },
            ),
        ]

        receipt_file = self.state / "fx.receipt.json"
        for name, fixture, expected in fixtures:
            with self.subTest(fixture=name):
                if fixture is None:
                    receipt_file.unlink(missing_ok=True)
                else:
                    state.write_json(receipt_file, fixture)

                # 1. `list --json`
                list_raw = self.lane("list", "--json")
                list_data = json.loads(list_raw)
                [list_lane] = list_data["lanes"]
                self.assertEqual(list_lane["receipt"], expected["list_receipt"])

                # 2. `show --json`
                show_raw = self.lane("show", "fx", "--json")
                show_data = json.loads(show_raw)
                self.assertEqual(show_data["checks"], expected["show_checks"])

                # 3. `status --json`
                status_raw = self.lane("status", "--json", cwd=self.tmp)
                status_data = json.loads(status_raw)
                [proj] = status_data["projects"]
                [status_lane] = proj["lanes"]
                expected_status_rec = {
                    "status": expected["status_receipt"]["status"],
                    "tree_match": expected["status_receipt"]["tree_match"],
                    "path": str(receipt_file) if expected["status_receipt"]["has_path"] else None,
                }
                self.assertEqual(status_lane["receipt"], expected_status_rec)

                # Verify `lane_view` produces byte-identical per-lane structures for all three JSON commands.
                cfg = config.load(self.integration / ".lanefather.toml")
                store = state.Store(cfg, directory=self.state)
                rec = store.load("fx")
                view = receipts.lane_view(store, rec, logs=status_lane["logs"])
                self.assertEqual(
                    json.dumps(list_lane, sort_keys=True),
                    json.dumps(view.for_list(rec), sort_keys=True),
                )
                self.assertEqual(
                    show_raw.strip(),
                    json.dumps(view.for_show()),
                )
                self.assertEqual(
                    json.dumps(status_lane, sort_keys=True),
                    json.dumps(view.for_status(), sort_keys=True),
                )

                # 4 & 5. `land.merge_problem` and `checks.select`
                with mock.patch.dict(os.environ, self.env, clear=False):
                    config.use(cfg)
                    problem = land.merge_problem("fx", tip, tip_tree, fixture, record=rec)
                    self.assertEqual(problem, expected["land_problem"])

                    prepared = checks.prepare("fx")
                    buf = io.StringIO()
                    with mock.patch("sys.stdout", buf):
                        selected = checks.select(prepared, rerun=False, slots_override=1)
                    if expected["check_short_circuit"]:
                        self.assertIsNone(selected)
                    else:
                        self.assertIsNotNone(selected)
                    if expected["check_notice"]:
                        self.assertIn(expected["check_notice"], buf.getvalue())

    def test_review_receipt_fixtures_across_list_show_status_done_and_gate(self) -> None:
        from lanefather import state

        cfg_file = self.integration / ".lanefather.toml"
        self.commit(
            self.integration,
            {".lanefather.toml": cfg_file.read_text() + '\n[review]\npanel = ["sol", "opus"]\npanel_required = true\n'},
            "enable required review panel",
        )
        self.git(self.repo, "branch", "dev")
        wt = pathlib.Path(self.created("rv", "lib/**"))
        self.commit(wt, {"lib/api.txt": "foo\nrv\n"})
        self.lane("check", "rv")
        self.lane("land", "rv", "--keep")
        sha = self.tip()

        review_file = self.state / "rv.review.json"
        live_pid = os.getpid()
        dead_pid = 999_999_999

        cases: list[tuple[str, dict[str, Any] | None, dict[str, Any]]] = [
            (
                "absent",
                None,
                {
                    "list_tail": "no review: run lanefather review rv",
                    "show_review": {"status": "not started", "findings": 0, "unresolved": 0},
                    "status_review": {"status": "not started", "open": 0, "findings": 0},
                    "gate_held": True,
                },
            ),
            (
                "running_live",
                {"version": 1, "label": "rv", "status": "running", "pid": live_pid,
                 "started": "2026-11-21T10:00:00Z", "log": "/tmp/rv.log"},
                {
                    "list_tail": f"review running since 2026-11-21T10:00:00Z (pid {live_pid}, /tmp/rv.log)",
                    "show_review": {"status": "running", "findings": 0, "unresolved": 0},
                    "status_review": {"status": "running", "open": 0, "findings": 0},
                    "gate_held": True,
                },
            ),
            (
                "died_dead_pid",
                {"version": 1, "label": "rv", "status": "running", "pid": dead_pid,
                 "started": "2026-11-21T10:00:00Z", "log": "/tmp/rv.log"},
                {
                    # Disagreement preserved in Tidy 3 (#71): show reports raw 'running' on a dead review,
                    # while status reports 'stopped' and list/review_state report 'review died'.
                    "list_tail": f"review died (pid {dead_pid}, /tmp/rv.log): rerun lanefather review rv",
                    "show_review": {"status": "running", "findings": 0, "unresolved": 0},
                    "status_review": {"status": "stopped", "open": 0, "findings": 0},
                    "gate_held": True,
                },
            ),
            (
                "failed",
                {"version": 1, "label": "rv", "status": "failed", "error": "timeout"},
                {
                    "list_tail": "review failed: timeout; rerun lanefather review rv",
                    "show_review": {"status": "failed", "findings": 0, "unresolved": 0},
                    "status_review": {"status": "failed", "open": 0, "findings": 0},
                    "gate_held": True,
                },
            ),
            (
                "partial_panel",
                {
                    "version": 1, "label": "rv", "status": "finished", "reviewer": "sol+opus",
                    "findings": [
                        {"n": 1, "severity": "P3", "title": "nit", "file": "lib/api.txt", "line": 1, "mark": None},
                    ],
                    "panel": [
                        {"member": "sol", "status": "finished", "numbers": [1]},
                        {"member": "opus", "status": "failed", "error": "timeout"},
                    ],
                },
                {
                    "list_tail": (
                        "panel member opus gave no review ([review] panel_required): "
                        "lanefather review rv --force-reason <why> reviews again"
                    ),
                    "show_review": {"status": "finished", "findings": 1, "unresolved": 1},
                    "status_review": {"status": "finished", "open": 1, "findings": 1},
                    "gate_held": True,
                },
            ),
            (
                "exempt",
                {"version": 1, "label": "rv", "status": "exempt", "exempt": "documentation only"},
                {
                    "list_tail": "review exempt: documentation only",
                    "show_review": {"status": "exempt", "findings": 0, "unresolved": 0},
                    "status_review": {"status": "exempt", "open": 0, "findings": 0},
                    "gate_held": False,
                },
            ),
            (
                "unmarked",
                {
                    "version": 1, "label": "rv", "status": "finished", "reviewer": "sol",
                    "findings": [
                        {"n": 1, "severity": "P2", "title": "bug", "file": "lib/api.txt", "line": 1, "mark": None},
                        {"n": 2, "severity": "P3", "title": "nit", "file": "lib/api.txt", "line": 2,
                         "mark": {"kind": "rejected", "reason": "fine"}},
                    ],
                },
                {
                    "list_tail": "1 of 2 review findings unmarked (#1)",
                    "show_review": {"status": "finished", "findings": 2, "unresolved": 1},
                    "status_review": {"status": "finished", "open": 1, "findings": 2},
                    "gate_held": True,
                },
            ),
            (
                "complete",
                {
                    "version": 1, "label": "rv", "status": "finished", "reviewer": "sol",
                    "findings": [
                        {"n": 1, "severity": "P2", "title": "bug", "file": "lib/api.txt", "line": 1,
                         "mark": {"kind": "rejected", "reason": "by design"}},
                    ],
                },
                {
                    "list_tail": "reviewed by sol: 1 findings, all marked",
                    "show_review": {"status": "finished", "findings": 1, "unresolved": 0},
                    "status_review": {"status": "finished", "open": 0, "findings": 1},
                    "gate_held": False,
                },
            ),
        ]

        for name, fixture, expected in cases:
            with self.subTest(review=name):
                if fixture is None:
                    review_file.unlink(missing_ok=True)
                else:
                    state.write_json(review_file, fixture)

                listed = json.loads(self.lane("list", "--json"))["lanes"][0]
                self.assertEqual(listed["receipt"], f"green, post-merge green, {expected['list_tail']}")

                shown = json.loads(self.lane("show", "rv", "--json"))
                self.assertEqual(shown["review"], expected["show_review"])

                status_lane = json.loads(self.lane("status", "--json", cwd=self.tmp))["projects"][0]["lanes"][0]
                self.assertEqual(status_lane["review"], expected["status_review"])

                gate = json.loads(self.lane("findings", sha, "--json", ok=None))
                self.assertEqual(gate["held"], expected["gate_held"])


if __name__ == "__main__":
    unittest.main()
