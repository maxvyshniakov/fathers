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
        from lanefather import postmerge, receipts, review_record

        # Reading from a missing store directory never creates it.
        for kind in ("check", "receipt", "first", "post-merge", "recheck", "review", "gate", "perf"):
            self.assertIsNone(receipts.read(self.store_a, "lane1", kind))  # type: ignore[arg-type]
        self.assertFalse(self.dir_a.exists())

        sha = "abcdef1234567890" * 2 + "12345678"
        cases: list[tuple[receipts.ReceiptKind, str, str]] = [
            ("check", "lane1", "lane1.receipt.json"),
            ("receipt", "lane1", "lane1.receipt.json"),
            ("first", "lane1", "lane1.first.json"),
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

        for guarded_kind in ("review", "post-merge", "recheck"):
            with self.subTest(refused=guarded_kind), self.assertRaises(ValueError):
                receipts.write(self.store_a, "lane1", {"status": "green"}, guarded_kind)  # type: ignore[arg-type]

        self.store_a.save({
            "id": "lane1",
            "status": "landed",
            "ticket": "none",
            "claims": [],
            "after": [],
            "landed": {"merge": sha},
        })
        _, pm_written = postmerge.PostMergeRecord.record_smoke(
            self.store_a, "lane1", merge_sha=sha, status="red", payload={"tree": "a" * 40}
        )
        self.assertEqual(pm_written, self.dir_a / "lane1.post-merge.json")
        self.assertEqual((receipts.read(self.store_a, "lane1", "post-merge") or {}).get("status"), "red")
        _, rc_written = postmerge.PostMergeRecord.record_recheck(
            self.store_a, "lane1", merge_sha=sha, tip=sha, status="green", payload={"tree": "a" * 40}
        )
        self.assertEqual(rc_written, self.dir_a / "lane1.recheck.json")
        self.assertEqual((receipts.read(self.store_a, "lane1", "recheck") or {}).get("status"), "green")
        self.assertFalse(self.dir_b.exists())

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
        # Environment comparison uses results.env_hash (canonical JSON), so bool vs int is distinct.
        self.assertEqual(
            receipts.check_verdict(
                record,
                {"status": "green", "tree": "t1", "toolchain": {"flag": True}, "platform": "darwin-arm64"},
                "t1",
                {"flag": 1},
                platform="darwin-arm64",
            ),
            receipts.StaleToolchain(tree="t1", tree_match=True, toolchain_changed=True, platform="darwin-arm64"),
        )
        self.assertFalse(
            receipts.same_environment(
                {"status": "green", "tree": "t1", "toolchain": {"flag": True}, "platform": "darwin-arm64"},
                {"flag": 1},
                worktree=wt,
                platform="darwin-arm64",
            )
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
            host_plat = results.host_platform(cfg)
            current_tc = plan.toolchain_state(worktree, cfg)
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
                    "list_receipt_with_tc": "no receipt",
                    "show_checks": {"status": "not run", "current": None, "tree_match": None},
                    "show_checks_with_tc": {"status": "not run", "current": None, "tree_match": None},
                    "status_receipt": {"status": "not run", "tree_match": None, "has_path": False},
                    "results_verified": False,
                },
            ),
            (
                "generation_mismatch",
                {
                    "version": 1, "lane": "fx", "lane_created": "2020-01-01T00:00:00Z",
                    "head": tip, "tree": tip_tree, "status": "green",
                    "toolchain": current_tc, "platform": host_plat, "results": [], "check_results": {},
                },
                {
                    # fathers#54: older lane incarnation receipt is treated as absent by every reader.
                    "land_problem": "no receipt for lane fx: run lanefather check fx in its worktree",
                    "check_short_circuit": False,
                    "check_notice": None,
                    "list_receipt": "no receipt",
                    "list_receipt_with_tc": "no receipt",
                    "show_checks": {"status": "not run", "current": None, "tree_match": None},
                    "show_checks_with_tc": {"status": "not run", "current": None, "tree_match": None},
                    "status_receipt": {"status": "not run", "tree_match": None, "has_path": False},
                    "results_verified": None,
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
                    "list_receipt_with_tc": "green",
                    "show_checks": {"status": "green", "current": None, "tree_match": True},
                    "show_checks_with_tc": {"status": "green", "current": None, "tree_match": True},
                    "status_receipt": {"status": "green", "tree_match": True, "has_path": True},
                    "results_verified": True,
                },
            ),
            (
                "stale_toolchain",
                {
                    "version": 1, "lane": "fx", "head": tip, "tree": tip_tree, "status": "green",
                    "toolchain": old_tc, "platform": host_plat, "results": [], "check_results": {},
                },
                {
                    # fathers#59: land refuses StaleToolchain consistently with check and results-store reuse;
                    # read-only list/show/status never run the toolchain resolver, and agree when given toolchain.
                    "land_problem": (
                        "the receipt is stale: the Rust toolchain changed since the lane check; "
                        "rerun lanefather check fx"
                    ),
                    "check_short_circuit": False,
                    "check_notice": "lane: the Rust toolchain changed since the receipt (#571); running the checks again",
                    "list_receipt": "green",
                    "list_receipt_with_tc": "green (stale)",
                    "show_checks": {"status": "green", "current": None, "tree_match": True},
                    "show_checks_with_tc": {"status": "green", "current": False, "tree_match": True},
                    "status_receipt": {"status": "green", "tree_match": True, "has_path": True},
                    "results_verified": False,
                },
            ),
            (
                "stale_platform",
                {
                    "version": 1, "lane": "fx", "head": tip, "tree": tip_tree, "status": "green",
                    "toolchain": current_tc, "platform": "freebsd-riscv64", "results": [], "check_results": {},
                },
                {
                    "land_problem": f"the receipt is stale: it was not made on {host_plat}; rerun lanefather check fx",
                    "check_short_circuit": False,
                    "check_notice": f"lane: the receipt was not made on {host_plat}; running the checks again",
                    "list_receipt": "green",
                    "list_receipt_with_tc": "green (stale)",
                    "show_checks": {"status": "green", "current": None, "tree_match": True},
                    "show_checks_with_tc": {"status": "green", "current": False, "tree_match": True},
                    "status_receipt": {"status": "green", "tree_match": True, "has_path": True},
                    "results_verified": False,
                },
            ),
            (
                "corrupt_platform",
                {
                    "version": 1, "lane": "fx", "head": tip, "tree": tip_tree, "status": "green",
                    "toolchain": current_tc, "platform": 123, "results": [], "check_results": {},
                },
                {
                    "land_problem": f"the receipt is stale: it was not made on {host_plat}; rerun lanefather check fx",
                    "check_short_circuit": False,
                    "check_notice": f"lane: the receipt was not made on {host_plat}; running the checks again",
                    "list_receipt": "green",
                    "list_receipt_with_tc": "green (stale)",
                    "show_checks": {"status": "green", "current": None, "tree_match": True},
                    "show_checks_with_tc": {"status": "green", "current": False, "tree_match": True},
                    "status_receipt": {"status": "green", "tree_match": True, "has_path": True},
                    "results_verified": False,
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
                    "list_receipt_with_tc": "green (stale)",
                    "show_checks": {"status": "green", "current": False, "tree_match": False},
                    "show_checks_with_tc": {"status": "green", "current": False, "tree_match": False},
                    "status_receipt": {"status": "green", "tree_match": False, "has_path": True},
                    "results_verified": False,
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
                    "list_receipt_with_tc": "red (stale)",
                    "show_checks": {"status": "red", "current": False, "tree_match": False},
                    "show_checks_with_tc": {"status": "red", "current": False, "tree_match": False},
                    "status_receipt": {"status": "red", "tree_match": False, "has_path": True},
                    "results_verified": False,
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
                    "list_receipt_with_tc": "red",
                    "show_checks": {"status": "red", "current": None, "tree_match": True},
                    "show_checks_with_tc": {"status": "red", "current": None, "tree_match": True},
                    "status_receipt": {"status": "red", "tree_match": True, "has_path": True},
                    "results_verified": False,
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
                shutil.rmtree(self.state / "results", ignore_errors=True)

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

                # Verify `lane_view` produces byte-identical per-lane structures for all three JSON commands,
                # and agrees on StaleToolchain when toolchain/platform are supplied.
                cfg = config.load(self.integration / ".lanefather.toml")
                store = state.Store(cfg, directory=self.state)
                rec = store.load("fx")
                view = receipts.lane_view(store, rec, logs=status_lane["logs"])
                self.assertEqual(
                    json.dumps(list_lane, sort_keys=True),
                    json.dumps(dict(view.for_list(rec), project="straylight", gone=False), sort_keys=True),
                )
                self.assertEqual(
                    show_raw.strip(),
                    json.dumps(dict(view.for_show(), gone=False)),
                )
                self.assertEqual(
                    json.dumps(status_lane, sort_keys=True),
                    json.dumps(dict(view.for_status(), gone=False), sort_keys=True),
                )
                with mock.patch.dict(os.environ, self.env, clear=False):
                    view_tc = receipts.lane_view(
                        store, rec, toolchain=current_tc, platform=host_plat, logs=status_lane["logs"],
                    )
                    self.assertEqual(view_tc.list_receipt_text(), expected["list_receipt_with_tc"])
                    self.assertEqual(view_tc.for_show()["checks"], expected["show_checks_with_tc"])

                    # 4 & 5. `land.merge_problem`, `checks.select`, and results-store parity
                    problem = land.merge_problem(
                        "fx", tip, tip_tree, fixture,
                        record=rec, prog=store.prog, toolchain=current_tc, platform=host_plat, project=cfg,
                    )
                    self.assertEqual(problem, expected["land_problem"])

                    if expected["results_verified"] is not None:
                        if fixture is not None:
                            check_status = "passed" if fixture.get("status") == "green" else "failed"
                            results.record(
                                store,
                                fixture["tree"],
                                {"lib-check": check_status},
                                source="check",
                                toolchain=fixture.get("toolchain"),
                                platform=fixture.get("platform"),
                                lane="fx",
                            )
                        lookup = results.lookup(
                            store, tip_tree, ["lib-check"], toolchain=current_tc, platform=host_plat,
                        )
                        self.assertEqual(lookup["verified"], expected["results_verified"])

                    prepared = checks.prepare(store, "fx")
                    buf = io.StringIO()
                    with mock.patch("sys.stdout", buf):
                        selected = checks.select(store, prepared, rerun=False, slots_override=1)
                    if expected["check_short_circuit"]:
                        self.assertIsNone(selected)
                    else:
                        self.assertIsNotNone(selected)
                    if expected["check_notice"]:
                        self.assertIn(expected["check_notice"], buf.getvalue())

    def test_toolchain_change_invalidates_both_land_and_results_store_reuse(self) -> None:
        from lanefather import config, plan, results, state

        worktree = pathlib.Path(self.created("tc", "lib/**"))
        self.commit(worktree, {"lib/api.txt": "foo\ntc\n"})
        self.lane("check", "tc")
        tip_tree = self.git(worktree, "rev-parse", "HEAD^{tree}")

        with mock.patch.dict(os.environ, self.env, clear=False):
            cfg = config.load(self.integration / ".lanefather.toml")
            store = state.Store(cfg, directory=self.state)
            host_plat = results.host_platform(cfg)
            tc_v1 = plan.toolchain_state(worktree, cfg)
            self.assertTrue(
                results.lookup(store, tip_tree, ["lib-check"], toolchain=tc_v1, platform=host_plat)["verified"]
            )

        # Toolchain changes: both `land` and results-store reuse treat the prior receipt/entries as stale.
        self.env["FIXTURE_RUST"] = "1.99.0"
        with mock.patch.dict(os.environ, self.env, clear=False):
            cfg = config.load(self.integration / ".lanefather.toml")
            store = state.Store(cfg, directory=self.state)
            host_plat = results.host_platform(cfg)
            tc_v2 = plan.toolchain_state(worktree, cfg)
            self.assertNotEqual(
                results.env_hash(tc_v1, host_plat, project=cfg),
                results.env_hash(tc_v2, host_plat, project=cfg),
            )
            self.assertFalse(
                results.lookup(store, tip_tree, ["lib-check"], toolchain=tc_v2, platform=host_plat)["verified"]
            )
            self.assertEqual(results.passed(store, tip_tree, toolchain=tc_v2, platform=host_plat), {})

        refused = self.lane("land", "tc", ok=False)
        self.assertIn("the receipt is stale: the Rust toolchain changed since the lane check", refused)
        self.assertEqual(self.record("tc")["status"], "active")
        # Overridable via --force-reason (audited in the lane record).
        self.lane("land", "tc", "--force-reason", "toolchain upgrade verified separately", "--keep")
        self.assertEqual(self.record("tc")["status"], "landed")

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
                    # Resolved in fathers#60: show and status both report 'stopped' on a dead review,
                    # while list/review_state report 'review died'.
                    "list_tail": f"review died (pid {dead_pid}, /tmp/rv.log): rerun lanefather review rv",
                    "show_review": {"status": "stopped", "findings": 0, "unresolved": 0},
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

        rv_created = self.record("rv")["created"]
        for name, fixture, expected in cases:
            with self.subTest(review=name):
                if fixture is None:
                    review_file.unlink(missing_ok=True)
                else:
                    state.write_json(review_file, {**fixture, "lane_created": rv_created})

                listed = json.loads(self.lane("list", "--json"))["lanes"][0]
                self.assertEqual(listed["receipt"], f"green, post-merge green, {expected['list_tail']}")

                shown = json.loads(self.lane("show", "rv", "--json"))
                self.assertEqual(shown["review"], expected["show_review"])

                status_lane = json.loads(self.lane("status", "--json", cwd=self.tmp))["projects"][0]["lanes"][0]
                self.assertEqual(status_lane["review"], expected["status_review"])

                gate = json.loads(self.lane("findings", sha, "--json", ok=None))
                self.assertEqual(gate["held"], expected["gate_held"])


class ReusedLaneIdIsolationTests(unittest.TestCase):
    """fathers#54: lane receipts are stamped with `lane_created` and mismatched receipts are treated as absent."""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lanefather-reused-id-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        from lanefather import config, state

        self.store = state.Store(config.Config(), directory=self.tmp / "state")

    def test_receipt_matches_lane_strict_types_and_legacy_compat(self) -> None:
        from lanefather import state

        t1 = "2026-11-24T01:00:00Z"
        t2 = "2026-11-24T02:00:00Z"
        self.assertTrue(state.receipt_matches_lane({"lane_created": t1}, t1))
        self.assertFalse(state.receipt_matches_lane({"lane_created": t1}, t2))
        self.assertTrue(state.receipt_matches_lane({"status": "green"}, t2))
        self.assertTrue(state.receipt_matches_lane({"lane_created": None}, t2))
        self.assertTrue(state.receipt_matches_lane({"started": t2, "finished": t2}, t2))
        self.assertTrue(state.receipt_matches_lane({"finished": t2}, t1))
        self.assertFalse(state.receipt_matches_lane({"started": t1, "finished": t1}, t2))
        self.assertFalse(state.receipt_matches_lane({"finished": t1}, t2))
        self.assertFalse(state.receipt_matches_lane({"started": 123}, t2))
        self.assertFalse(state.receipt_matches_lane({"finished": True}, t2))
        self.assertTrue(state.receipt_matches_lane({"lane_created": t1}, None))
        self.assertFalse(state.receipt_matches_lane(None, t1))
        for bad in (123, True, False, ["t"], {"t": 1}, ""):
            self.assertFalse(state.receipt_matches_lane({"lane_created": bad}, t1), f"bad={bad!r}")
        self.assertIsNone(state.lane_created_of(None))
        self.assertIsNone(state.lane_created_of({}))
        self.assertIsNone(state.lane_created_of({"created": ""}))
        self.assertIsNone(state.lane_created_of({"created": 123}))
        self.assertEqual(state.lane_created_of({"created": t1}), t1)

    def test_readers_ignore_older_generation_receipts_and_keep_legacy_compat(self) -> None:
        from lanefather import postmerge, receipts, review_record, state

        t1 = "2026-11-24T01:00:00Z"
        t2 = "2026-11-24T02:00:00Z"
        rec_gen1 = {"id": "a", "status": "done", "created": t1, "common_dir": "/repo/.git",
                    "landed": {"merge": "m1"}}
        rec_gen2 = {"id": "a", "status": "landed", "created": t2, "common_dir": "/repo/.git",
                    "landed": {"merge": "m2"}}
        self.store.save(rec_gen2)

        # Write gen-1 receipts of every lane kind.
        gen1_check = {
            "version": 1, "lane": "a", "lane_created": t1, "status": "green", "tree": "tree1",
            "results": [{"suite": "unit", "status": "passed", "checks": ["c1"]}],
        }
        receipts.write(self.store, "a", gen1_check, "receipt")
        receipts.write(self.store, "a", {"head": "h1", "tree": "tree1", "failed_checks": ["c1"],
                                         "lane_created": t1}, "first")
        state.write_json(
            self.store.receipt_path("a", "post-merge"),
            {"version": 1, "lane": "a", "lane_created": t1, "merge": "m2", "status": "red",
             "results": [{"suite": "smoke", "status": "failed", "checks": ["s1"]}]},
        )
        state.write_json(
            self.store.receipt_path("a", "recheck"),
            {"version": 1, "lane": "a", "lane_created": t1, "merge": "m2", "status": "green"},
        )
        state.write_json(
            self.store.receipt_path("a", "review"),
            {
                "version": 1, "label": "a", "kind": "lane", "lane": "a", "lane_created": t1,
                "status": "finished", "reviewer": "sol",
                "findings": [{"n": 1, "severity": "P1", "title": "old", "file": "a.py", "line": 1, "mark": None}],
            },
        )

        # Gen 1 sees its own receipts; gen 2 treats every gen-1 receipt as absent.
        for kind in ("receipt", "first", "post-merge", "recheck", "review"):
            self.assertIsNotNone(receipts.read(self.store, "a", kind, record=rec_gen1), kind)
            self.assertIsNone(receipts.read(self.store, "a", kind, record=rec_gen2), kind)

        self.assertIsInstance(receipts.check_verdict(rec_gen2, gen1_check, "tree1"), receipts.NoReceipt)
        self.assertIsInstance(receipts.check_verdict(rec_gen1, gen1_check, "tree1"), receipts.Green)

        # ensure_first ignores gen-1 `previous` and writes a fresh first receipt for gen 2.
        first = receipts.ensure_first(self.store, rec_gen2, gen1_check, "h2", "tree2", ["c2"])
        self.assertEqual(first, {"head": "h2", "tree": "tree2", "failed_checks": ["c2"], "lane_created": t2})

        # lane_ran and postmerge.red_receipt ignore gen-1 receipts.
        self.assertEqual(receipts.lane_ran(self.store, "a", record=rec_gen2), (set(), set(), set()))
        self.assertEqual(receipts.lane_ran(self.store, "a", record=rec_gen1)[0], {"c1", "s1"})
        self.assertIsNone(postmerge.red_receipt(rec_gen2, self.store))

        # ReviewRecord readers ignore gen-1 review for gen 2.
        rev_gen2 = review_record.ReviewRecord.load(self.store, "a", record=rec_gen2)
        self.assertIsInstance(rev_gen2.verdict, review_record.Absent)
        self.assertEqual(list(review_record.ReviewRecord.unmarked(self.store, [rec_gen2])), [])
        with self.assertRaises(Exception) as ctx:
            review_record.ReviewRecord.fix_parent(self.store, "a", "/repo/.git", "--fix-of a")
        self.assertIn("no finished review with unmarked findings", str(ctx.exception))

        # Legacy unstamped receipts (pre-upgrade):
        # (a) A live lane across the upgrade (created <= receipt started/finished) keeps reading them.
        live_legacy_check = {
            "version": 1, "lane": "a", "status": "green", "tree": "tree2",
            "started": t2, "finished": t2,
        }
        receipts.write(self.store, "a", live_legacy_check, "receipt")
        self.assertEqual(receipts.read(self.store, "a", "receipt", record=rec_gen2), live_legacy_check)
        self.assertIsInstance(receipts.check_verdict(rec_gen2, live_legacy_check, "tree2"), receipts.Green)
        state.write_json(
            self.store.receipt_path("a", "review"),
            {"version": 1, "label": "a", "kind": "lane", "lane": "a", "status": "finished",
             "reviewer": "sol", "started": t2, "finished": t2, "findings": []},
        )
        legacy_rev = review_record.ReviewRecord.load(self.store, "a", record=rec_gen2)
        self.assertIsInstance(legacy_rev.verdict, review_record.Complete)

        # (b) A reused lane id (gen2.created > gen1 unstamped receipt timestamp) ignores pre-upgrade receipts.
        old_unstamped_check = {
            "version": 1, "lane": "a", "status": "green", "tree": "tree1",
            "started": t1, "finished": t1,
        }
        receipts.write(self.store, "a", old_unstamped_check, "receipt")
        state.write_json(
            self.store.receipt_path("a", "post-merge"),
            {"version": 1, "lane": "a", "merge": "m2", "status": "red", "finished": t1},
        )
        state.write_json(
            self.store.receipt_path("a", "review"),
            {
                "version": 1, "label": "a", "kind": "lane", "lane": "a", "status": "finished",
                "reviewer": "sol", "started": t1, "finished": t1,
                "findings": [{"n": 1, "severity": "P1", "title": "old", "file": "a.py", "line": 1, "mark": None}],
            },
        )
        self.assertIsNone(receipts.read(self.store, "a", "receipt", record=rec_gen2))
        self.assertIsInstance(receipts.check_verdict(rec_gen2, old_unstamped_check, "tree1"), receipts.NoReceipt)
        self.assertIsNone(postmerge.red_receipt(rec_gen2, self.store))
        self.assertIsInstance(
            review_record.ReviewRecord.load(self.store, "a", record=rec_gen2).verdict,
            review_record.Absent,
        )
        with self.assertRaises(Exception) as ctx2:
            review_record.ReviewRecord.fix_parent(self.store, "a", "/repo/.git", "--fix-of a")
        self.assertIn("no finished review with unmarked findings", str(ctx2.exception))


if __name__ == "__main__":
    unittest.main()
