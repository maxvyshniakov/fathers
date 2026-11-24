"""The run history (fathers#92): per-check durations, the lease run id and flakes, kept after the lane is done."""
from __future__ import annotations

import json
import pathlib
from typing import Any
import unittest

from support import LaneCase, ScratchCase


class HistoryTests(LaneCase):
    def lines(self, kind: str | None = None) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for path in sorted((self.state / "history").glob("*.jsonl")):
            found += [json.loads(line) for line in path.read_text().splitlines()]
        return [line for line in found if kind is None or line["kind"] == kind]

    def receipt(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.receipt.json").read_text())

    def test_each_check_of_a_lane_is_kept_after_done_with_its_run_and_tree(self):
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        receipt = self.receipt("a")
        run = receipt["leases"][0]["run"]
        self.assertEqual({r["run"] for r in receipt["results"]}, {run})
        # The lease's meta names the run and the tree, so a slotfather entry finds its receipt.
        meta = json.loads(pathlib.Path(str(self.verify_log) + ".requests").read_text().splitlines()[-1])["meta"]
        self.assertEqual((meta["run"], meta["tree"]), (run, receipt["tree"]))
        self.lane("land", "a")
        self.lane("review", "a")
        self.lane("done", "a", ok=None)
        checks = [line for line in self.lines("check") if line["lane"] == "a" and line["phase"] == "check"]
        self.assertEqual([line["check"] for line in checks], ["structure-check", "lib-check"])
        for line in checks:
            self.assertEqual((line["status"], line["tree"], line["run"]), ("passed", receipt["tree"], run))
            self.assertTrue(line["started"] and line["finished"])
        stats = json.loads(self.lane("stats", "--json"))
        self.assertEqual(stats["v"], 1)
        row = next(r for r in stats["checks"] if r["check"] == "lib-check")
        self.assertEqual((row["runs"], row["failed"], row["flakes"]), (1, 0, 0))
        self.assertIsNotNone(row["p50"])
        self.assertIsNotNone(row["p90"])
        self.assertTrue(all(isinstance(line.get("seconds"), float) for line in checks))
        post = json.loads((self.state / "a.post-merge.json").read_text())
        self.assertIn("leases", post)

    def test_a_check_red_then_green_on_one_tree_is_one_flake(self):
        flag = self.tmp / "flake-once"
        flag.touch()
        self.env["FIXTURE_FLAKE_ONCE"] = f"lib-check:{flag}"
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a")
        flakes = self.lines("flake")
        self.assertEqual([(f["check"], f["how"], f["tree"]) for f in flakes],
                         [("lib-check", "rerun-alone", self.receipt("a")["tree"])])
        # The rerun's own run is in the history too, under its phase.
        self.assertIn("rerun", {line["phase"] for line in self.lines("check")})
        stats = json.loads(self.lane("stats", "--json", "--lane", "a"))
        self.assertEqual(next(r for r in stats["checks"] if r["check"] == "lib-check")["flakes"], 1)
        self.assertIn("lib-check:", self.lane("stats"))
        # The receipt names the rerun's run too, so its slot lease finds the receipt.
        self.assertEqual([lease.get("rerun") for lease in self.receipt("a")["leases"]], [None, "alone"])
        # The smoke passes lib-check on the same tree again: still one flake (review of fathers#92).
        self.lane("land", "a")
        self.assertEqual(len(self.lines("flake")), 1)

    def test_a_later_pass_after_a_red_check_on_the_same_tree_is_a_flake(self):
        flag = self.tmp / "flake-once"
        flag.touch()
        # Only the first run is red, and with no reruns it stays red: the next check passes on the same tree.
        self.env.update(FIXTURE_FLAKE_ONCE=f"lib-check:{flag}", LANEFATHER_RERUNS="1", FAKE_SLOTFATHER_BUSY="1")
        worktree = self.created("a", "lib/**")
        self.commit(worktree, {"lib/api.txt": "foo\nbaz\n"})
        self.lane("check", "a", ok=False)
        del self.env["FAKE_SLOTFATHER_BUSY"]
        self.lane("check", "a")
        flakes = self.lines("flake")
        self.assertEqual([(f["check"], f["how"]) for f in flakes], [("lib-check", "later-run")])


class JunitFailuresTests(ScratchCase):
    def test_a_flake_names_the_failed_junit_tests(self) -> None:
        from lanefather import history
        report = self.tmp / "junit.xml"
        report.write_text('<testsuite><testcase classname="grid" name="scrolls"><failure/></testcase>'
                          '<testcase classname="grid" name="sorts"/></testsuite>')
        self.assertEqual(history.junit_failures(report), ["grid.scrolls"])
        self.assertEqual(history.junit_failures(self.tmp / "missing.xml"), [])


class StatsMetricsTests(ScratchCase):
    """Fixture-driven verification of `lanefather stats` orchestration metrics (fathers#47)."""

    def _iso(self, base: Any, offset_seconds: int) -> str:
        import datetime
        return (base + datetime.timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _setup_fixture_store(self) -> tuple[Any, pathlib.Path, Any]:
        import datetime

        from lanefather import config, state

        base = datetime.datetime.now(datetime.UTC).replace(microsecond=0) - datetime.timedelta(hours=2)
        state_dir = self.tmp / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        store = state.Store(config.DEFAULT, directory=state_dir)
        events_file = self.tmp / "events.jsonl"

        def write_json(name: str, payload: dict[str, Any]) -> None:
            (state_dir / name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        # 1. alpha: landed + promoted, with finished review (2 fixed, 1 rejected)
        write_json("alpha.json", {
            "id": "alpha",
            "ticket": "101",
            "status": "done",
            "outcome": "landed",
            "created": self._iso(base, 0),
            "landed": {"at": self._iso(base, 300), "merge": "aaaa1111"},
        })
        write_json("alpha.receipt.json", {
            "lane_created": self._iso(base, 0),
            "status": "green",
            "started": self._iso(base, 90),
            "finished": self._iso(base, 100),
            "leases": [{"run": "run-alpha-check", "lease": "lease-alpha-check"}],
        })
        write_json("alpha.review.json", {
            "version": 1,
            "label": "alpha",
            "lane": "alpha",
            "lane_created": self._iso(base, 0),
            "status": "finished",
            "reviewer": "sol",
            "model": "gpt-6.1-sol",
            "finished": self._iso(base, 400),
            "findings": [
                {"n": 1, "severity": "P1", "title": "bug 1", "mark": {"kind": "fixed", "merge": "dddd4444"}},
                {"n": 2, "severity": "P2", "title": "bug 2", "mark": {"kind": "fixed", "merge": "dddd4444"}},
                {"n": 3, "severity": "P3", "title": "nit", "mark": {"kind": "rejected", "reason": "by design"}},
            ],
        })

        # 2. beta: landed, NOT promoted, with a lane override (present in both events and record to test dedup)
        write_json("beta.json", {
            "id": "beta",
            "ticket": "102",
            "status": "landed",
            "created": self._iso(base, 0),
            "landed": {"at": self._iso(base, 500), "merge": "bbbb2222"},
            "overrides": [
                {"at": self._iso(base, 495), "reason": "urgent hotfix", "refusals": ["no green receipt"]},
            ],
        })
        write_json("beta.receipt.json", {
            "lane_created": self._iso(base, 0),
            "status": "green",
            "started": self._iso(base, 180),
            "finished": self._iso(base, 200),
            "leases": [{"run": "run-beta-check", "lease": "lease-beta-check"}],
        })

        # 3. gamma: parked (no green receipt, no land, no promote)
        write_json("gamma.json", {
            "id": "gamma",
            "ticket": "103",
            "status": "done",
            "outcome": "parked",
            "created": self._iso(base, 0),
            "parked": {"at": self._iso(base, 400), "reason": "waiting on API", "ref": "park/gamma"},
        })

        # 4. delta: active fix lane of alpha (no green receipt yet)
        write_json("delta.json", {
            "id": "delta",
            "ticket": "101",
            "status": "active",
            "created": self._iso(base, 600),
            "review_fix_of": "alpha",
        })

        # Run history for check + post-merge phases
        history_dir = state_dir / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        month = base.strftime("%Y-%m")
        history_lines = [
            {
                "v": 1, "kind": "check", "at": self._iso(base, 100), "lane": "alpha", "phase": "check",
                "check": "lib-check", "status": "passed", "seconds": 10.0,
                "started": self._iso(base, 90), "finished": self._iso(base, 100),
                "run": "run-alpha-check", "lease": "lease-alpha-check",
            },
            {
                "v": 1, "kind": "check", "at": self._iso(base, 200), "lane": "beta", "phase": "check",
                "check": "lib-check", "status": "passed", "seconds": 20.0,
                "started": self._iso(base, 180), "finished": self._iso(base, 200),
                "run": "run-beta-check", "lease": "lease-beta-check",
            },
            {
                "v": 1, "kind": "check", "at": self._iso(base, 350), "lane": "alpha", "phase": "post-merge",
                "check": "lib-check", "status": "passed", "seconds": 30.0,
                "started": self._iso(base, 320), "finished": self._iso(base, 350),
                "run": "run-alpha-smoke", "lease": "lease-alpha-smoke",
            },
        ]
        (history_dir / f"{month}.jsonl").write_text(
            "\n".join(json.dumps(line) for line in history_lines) + "\n", encoding="utf-8"
        )

        # Machine events: promote.finished for alpha, duplicate beta land override, promote review-gate override
        event_rows = [
            {
                "v": 1, "source": "lanefather", "kind": "promote.finished", "time": self._iso(base, 900),
                "level": "info",
                "subject": {"type": "commit", "id": "aaaa1111", "state_dir": str(state_dir)},
                "details": {"branches": ["dev"], "pushed": True, "failed": []},
            },
            {
                "v": 1, "source": "lanefather", "kind": "override", "time": self._iso(base, 495),
                "level": "warn",
                "subject": {"type": "lane", "id": "beta", "state_dir": str(state_dir)},
                "details": {"command": "land", "reason": "urgent hotfix", "refusals": ["no green receipt"]},
            },
            {
                "v": 1, "source": "lanefather", "kind": "override", "time": self._iso(base, 890),
                "level": "warn",
                "subject": {"type": "commit", "id": "aaaa1111", "state_dir": str(state_dir)},
                "details": {
                    "command": "promote",
                    "reason": "release window",
                    "refusals": ["lane alpha: finding #1", "lane alpha: finding #2"],
                },
            },
        ]
        events_file.write_text("\n".join(json.dumps(ev) for ev in event_rows) + "\n", encoding="utf-8")
        return store, events_file, base

    def test_stats_metrics_exact_percentiles_omission_and_unknown_wait_without_slotfather(self) -> None:
        import contextlib
        import io
        import os
        import types
        from unittest import mock

        from lanefather import history

        store, events_file, _ = self._setup_fixture_store()
        with (
            mock.patch.dict(os.environ, {"LANEFATHER_EVENTS": str(events_file)}),
            mock.patch("lanefather.slots.command", return_value=None),
        ):
            report = history.stats(store, days=7)

            self.assertEqual(report["v"], 1)
            # Backwards-compatible blocks stay present.
            self.assertEqual(len(report["checks"]), 1)
            self.assertEqual(report["checks"][0]["check"], "lib-check")
            self.assertEqual(len(report["reviewers"]), 1)

            # lane_flow: alpha (100s) and beta (200s) green; gamma & delta omitted (never 0).
            flow = report["lane_flow"]
            self.assertEqual(flow["lanes"], 4)
            self.assertEqual(flow["to_first_green"], {"count": 2, "p50": 150.0, "p90": 190.0})
            self.assertEqual(flow["to_land"], {"count": 2, "p50": 400.0, "p90": 480.0})
            # Only alpha promoted (900s); unpromoted beta/gamma/delta omitted!
            self.assertEqual(flow["to_promote"], {"count": 1, "p50": 900.0, "p90": 900.0})
            self.assertFalse(flow["slotfather_available"])

            phases = {row["phase"]: row for row in flow["phases"]}
            self.assertEqual(set(phases), {"check", "post-merge"})
            self.assertEqual(phases["check"]["runs"], 2)
            self.assertEqual(phases["check"]["run_p50"], 15.0)
            self.assertEqual(phases["check"]["run_p90"], 19.0)
            self.assertIsNone(phases["check"]["wait_p50"])
            self.assertIsNone(phases["check"]["wait_p90"])
            self.assertEqual(phases["check"]["wait_status"], "unknown")
            self.assertEqual(phases["check"]["matched_leases"], 0)
            self.assertIsNone(phases["post-merge"]["wait_p50"])
            self.assertEqual(phases["post-merge"]["wait_status"], "unknown")

            # outcomes
            self.assertEqual(report["outcomes"], {
                "total": 4, "landed": 2, "parked": 1, "abandoned": 0,
                "dropped": 0, "active": 1, "planned": 0, "other": 0,
            })

            # overrides & fix_lanes (beta's override is deduplicated between events.jsonl and beta.json)
            overrides = report["overrides"]
            self.assertEqual(overrides["total"], 2)
            self.assertEqual(overrides["lane"], 1)
            self.assertEqual(overrides["promote"], 1)
            self.assertEqual(overrides["by_command"], {"land": 1, "promote": 1})
            self.assertEqual(overrides["promote_reasons"], [{"reason": "release window", "count": 1}])
            self.assertEqual(overrides["fix_lanes"], {
                "total": 1,
                "single": 1,
                "batch": 0,
                "lanes": [{"id": "delta", "fix_of": ["alpha"], "batch": False}],
            })

            # review
            rev = report["review"]
            self.assertEqual(rev["reviewed_lanes"], 1)
            self.assertEqual(rev["findings"], 3)
            self.assertEqual(rev["p1"], 1)
            self.assertEqual(rev["p2"], 1)
            self.assertEqual(rev["p3"], 1)
            self.assertEqual(rev["fixed"], 2)
            self.assertEqual(rev["rejected"], 1)
            self.assertEqual(rev["unmarked"], 0)
            self.assertEqual(rev["findings_per_lane"], 3.0)
            self.assertEqual(rev["fixed_share"], round(2 / 3, 3))
            self.assertEqual(rev["rejected_share"], round(1 / 3, 3))
            self.assertEqual(rev["unmarked_share"], 0.0)
            self.assertEqual(rev["gate"], {"held": 2, "overridden": 1})

            # Human-readable rendering
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = history.command(types.SimpleNamespace(days=7, lane=None, json=False), store)
            self.assertEqual(code, 0)
            text = buf.getvalue()
            self.assertIn("Lane flow:", text)
            self.assertIn("to promote: p50 900.0s, p90 900.0s (n=1)", text)
            self.assertIn("wait unknown", text)
            self.assertIn("Outcomes:", text)
            self.assertIn("2 landed, 1 parked", text)
            self.assertIn("Overrides:", text)
            self.assertIn("fix lanes: 1 (1 single, 0 batch)", text)
            self.assertIn("Review:", text)
            self.assertIn("gate: 2 held, 1 overridden", text)

            # Lane filter scopes cleanly to alpha
            alpha_only = history.stats(store, days=7, lane="alpha")
            self.assertEqual(alpha_only["outcomes"]["total"], 1)
            self.assertEqual(alpha_only["outcomes"]["landed"], 1)
            self.assertEqual(alpha_only["lane_flow"]["to_promote"], {"count": 1, "p50": 900.0, "p90": 900.0})

    def test_stats_joins_slotfather_lease_wait_by_lease_id(self) -> None:
        import os
        from unittest import mock

        from support import FIXTURES

        from lanefather import history

        store, events_file, _ = self._setup_fixture_store()
        sf_history = self.tmp / "slotfather-history.jsonl"
        sf_rows = [
            {"v": 2, "lease_id": "lease-alpha-check", "waited": 4, "ran": 10, "meta": {"run": "run-alpha-check"}},
            {"v": 2, "lease_id": "lease-beta-check", "waited": 14, "ran": 20, "meta": {"run": "run-beta-check"}},
            # post-merge lease intentionally absent -> must remain unknown, never 0!
        ]
        sf_history.write_text("\n".join(json.dumps(r) for r in sf_rows) + "\n", encoding="utf-8")
        fake_sf = FIXTURES / "slotfather"
        with (
            mock.patch.dict(os.environ, {
                "LANEFATHER_EVENTS": str(events_file),
                "FAKE_SLOTFATHER_HISTORY": str(sf_history),
            }),
            mock.patch("lanefather.slots.command", return_value=str(fake_sf)),
        ):
            report = history.stats(store, days=7)
            flow = report["lane_flow"]
            self.assertTrue(flow["slotfather_available"])
            phases = {row["phase"]: row for row in flow["phases"]}
            self.assertEqual(phases["check"]["matched_leases"], 2)
            self.assertEqual(phases["check"]["wait_status"], "known")
            self.assertEqual(phases["check"]["wait_p50"], 9.0)
            self.assertEqual(phases["check"]["wait_p90"], 13.0)
            # Unmatched phase must report unknown (None), never 0.
            self.assertEqual(phases["post-merge"]["matched_leases"], 0)
            self.assertEqual(phases["post-merge"]["wait_status"], "unknown")
            self.assertIsNone(phases["post-merge"]["wait_p50"])
            self.assertIsNone(phases["post-merge"]["wait_p90"])

    def test_stats_handles_malformed_events_missing_receipts_and_legacy_records(self) -> None:
        import datetime
        import os
        from unittest import mock

        from lanefather import config, history, state

        now_dt = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
        created = (now_dt - datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        landed_at = (now_dt - datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

        state_dir = self.tmp / "messy-state"
        state_dir.mkdir(parents=True, exist_ok=True)
        store = state.Store(config.DEFAULT, directory=state_dir)

        # Legacy landed record without `outcome`
        (state_dir / "legacy-landed.json").write_text(json.dumps({
            "id": "legacy-landed",
            "status": "done",
            "created": created,
            "landed": {"at": landed_at, "merge": "1111aaaa"},
        }), encoding="utf-8")
        # Legacy dropped record without `outcome`
        (state_dir / "legacy-dropped.json").write_text(json.dumps({
            "id": "legacy-dropped",
            "status": "done",
            "created": created,
            "dropped": {"why": "no commits"},
        }), encoding="utf-8")
        # Legacy forced-done record without `outcome`, `landed`, or `dropped`
        (state_dir / "legacy-other.json").write_text(json.dumps({
            "id": "legacy-other",
            "status": "done",
            "created": created,
        }), encoding="utf-8")
        # Corrupt JSON file must not crash stats
        (state_dir / "corrupt.json").write_text("{not valid json", encoding="utf-8")

        # Malformed events.jsonl with bad JSON and bad field types
        bad_events = self.tmp / "bad-events.jsonl"
        bad_events.write_text(
            "not-json\n"
            "[]\n"
            '{"kind": 123, "time": true}\n'
            '{"v": 1, "source": "lanefather", "kind": "override", "time": "bad-ts", "details": "oops"}\n',
            encoding="utf-8",
        )
        with (
            mock.patch.dict(os.environ, {"LANEFATHER_EVENTS": str(bad_events)}),
            mock.patch("lanefather.slots.command", return_value=None),
        ):
            report = history.stats(store, days=7)
            self.assertEqual(report["v"], 1)
            self.assertEqual(report["outcomes"], {
                "total": 3, "landed": 1, "parked": 0, "abandoned": 0,
                "dropped": 1, "active": 0, "planned": 0, "other": 1,
            })
            self.assertEqual(report["lane_flow"]["to_land"]["count"], 1)
            self.assertEqual(report["lane_flow"]["to_first_green"], {"count": 0, "p50": None, "p90": None})
            self.assertEqual(report["lane_flow"]["to_promote"], {"count": 0, "p50": None, "p90": None})
            self.assertEqual(report["review"]["reviewed_lanes"], 0)
            self.assertIsNone(report["review"]["findings_per_lane"])


if __name__ == "__main__":
    unittest.main()
