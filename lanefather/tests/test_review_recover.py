"""Tests for recovering historical review evidence without rerunning completed reviews (fathers#179)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
import unittest

from support import NO_FINDINGS, LaneCase

from lanefather import config, done, history, review, review_class, review_gate, review_record, state, status


def _make_fake_gh(
    tmp: Path,
    *,
    comments_by_id: dict[str, dict[str, Any]] | None = None,
    issue_comments: dict[str, list[dict[str, Any]]] | None = None,
    fail_ids: set[str] | None = None,
) -> Path:
    """Create a stand-in `gh` executable for `LANEFATHER_GH` that serves mocked issue comments."""
    script = tmp / "fake-gh"
    payload = {
        "comments_by_id": comments_by_id or {},
        "issue_comments": issue_comments or {},
        "fail_ids": sorted(fail_ids or set()),
    }
    data_path = tmp / "fake-gh-data.json"
    data_path.write_text(json.dumps(payload), encoding="utf-8")
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"data = json.loads(open({str(data_path)!r}, encoding='utf-8').read())\n"
        "args = sys.argv[1:]\n"
        "if len(args) >= 2 and args[0] == 'api' and '/issues/comments/' in args[1]:\n"
        "    cid = args[1].rsplit('/issues/comments/', 1)[1]\n"
        "    if cid in data['fail_ids'] or cid not in data['comments_by_id']:\n"
        "        sys.stderr.write(f'HTTP 404: comment {cid} not found\\n')\n"
        "        sys.exit(1)\n"
        "    print(json.dumps(data['comments_by_id'][cid]))\n"
        "    sys.exit(0)\n"
        "if len(args) >= 3 and args[0] == 'issue' and args[1] == 'view':\n"
        "    num = args[2]\n"
        "    if num in data['fail_ids']:\n"
        "        sys.stderr.write(f'HTTP 404: issue {num} not found\\n')\n"
        "        sys.exit(1)\n"
        "    comments = data['issue_comments'].get(num, [])\n"
        "    print(json.dumps({'url': f'https://github.com/example/repo/issues/{num}', 'comments': comments}))\n"
        "    sys.exit(0)\n"
        "print(json.dumps({'comments': [{'createdAt': '2099-01-01T00:00:00Z', 'body': 'evidence'}]}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


class ReviewRecoverTests(LaneCase):
    def _set_gh(
        self,
        *,
        comments_by_id: dict[str, dict[str, Any]] | None = None,
        issue_comments: dict[str, list[dict[str, Any]]] | None = None,
        fail_ids: set[str] | None = None,
    ) -> None:
        self.env["LANEFATHER_GH"] = str(
            _make_fake_gh(
                self.tmp,
                comments_by_id=comments_by_id,
                issue_comments=issue_comments,
                fail_ids=fail_ids,
            )
        )

    def _land_lane(
        self,
        lane_id: str,
        ticket: str,
        file_path: str,
        content: str,
        *,
        extra_start: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], str]:
        self.lane(
            "start", lane_id, "--ticket", ticket, "--base", "integrate/q", "--claims", file_path,
            "--create", *extra_start,
        )
        wt = Path(self.record(lane_id)["worktree"])
        self.commit(wt, {file_path: content})
        self.lane("check", lane_id)
        self.lane("land", lane_id, "--wait", "0", "--keep")
        rec = self.record(lane_id)
        merge_sha = rec["landed"]["merge"]
        return rec, merge_sha

    def _add_integration_commit(self, rel_path: str, content: str, message: str = "fix review finding") -> str:
        return self.commit(self.integration, {rel_path: content}, message=message)

    def _store(self) -> state.Store:
        cfg = config.load(self.integration / ".lanefather.toml")
        return state.Store(cfg, directory=self.state)

    def test_complete_historical_evidence_satisfies_gate_distinguished_and_preserved_by_done(self) -> None:
        rec, merge_sha = self._land_lane("hist", "42", "lib/a.txt", "v1\n")
        fix_sha = self._add_integration_commit("lib/a.txt", "v2\n", "fix finding 1")
        source_url = "https://github.com/example/repo/issues/42#issuecomment-1001"
        body = (
            f"Historical review of lane hist (merge: {merge_sha[:10]}):\n"
            "This lane is fine overall; Review of PR #12 notes:\n"
            f"1. lib/a.txt:1 Null check missing [fixed: {fix_sha[:10]}]\n"
            "2. lib/a.txt:2 Rename helper [rejected: matches public API]\n"
        )
        self._set_gh(
            comments_by_id={
                "1001": {
                    "id": 1001,
                    "body": body,
                    "created_at": "2099-01-01T10:00:00Z",
                    "user": {"login": "reviewer-bot"},
                }
            },
            issue_comments={"42": [{"createdAt": "2099-01-01T10:00:00Z", "body": body, "url": source_url}]},
        )
        out = self.lane("review", "hist", "--recover", "--source", source_url)
        self.assertIn("Recovered historical review evidence of hist (complete)", out)
        self.assertIn("missing facts (not fabricated):", out)

        # Verify strict truthfulness on disk: never invented model, reviewer, severity, or tree.
        raw = self.review("hist")
        self.assertTrue(raw["recovered"])
        self.assertEqual(raw["evidence_source"], "historical")
        self.assertTrue(raw["complete"])
        self.assertEqual(raw["sources"], [source_url])
        self.assertIsNone(raw["model"])
        self.assertIsNone(raw["reviewer"])
        self.assertNotIn("tree", raw)
        self.assertIn("model", raw["missing_facts"])
        self.assertIn("severity", raw["missing_facts"])
        self.assertIn("tree", raw["missing_facts"])
        self.assertTrue(raw["recovered_by"])
        self.assertTrue(raw["recovered_at"])
        self.assertEqual(len(raw["dispositions"]), 2)
        self.assertTrue(raw["dispositions"][0]["in_target_history"])
        self.assertTrue(raw["dispositions"][0]["verified"])
        self.assertEqual(raw["dispositions"][0]["commit"], fix_sha)
        self.assertIsNone(raw["dispositions"][0]["severity"])
        self.assertTrue(raw["dispositions"][1]["verified"])
        self.assertEqual(raw["dispositions"][1]["kind"], "rejected")

        # Review gate is satisfied by complete historical evidence.
        store = self._store()
        head_sha = self.tip()
        report = review_gate.gate_report(store, str(self.integration), rec["common_dir"], head_sha)
        self.assertEqual(report["unfinished"], [])
        self.assertEqual(report["blocking"], [])

        # history.stats skips recovered receipts in review/reviewer metrics.
        stats_report = history.stats(store, days=7, lane="hist")
        self.assertEqual(stats_report["review"]["reviewed_lanes"], 0)
        self.assertEqual(stats_report["reviewers"], [])

        # list, show (text + JSON), and status (text + JSON) distinguish recovered evidence from executed review.
        listed = json.loads(self.lane("list", "--json"))
        lane_row = next(r for r in listed["lanes"] if r["id"] == "hist")
        self.assertIn("recovered historical evidence", lane_row["receipt"])
        self.assertIn("in target history", lane_row["receipt"])

        shown = json.loads(self.lane("show", "hist", "--json"))
        self.assertEqual(shown["review"]["status"], "recovered")
        self.assertTrue(shown["review"]["recovered"])
        self.assertEqual(shown["review"]["evidence_source"], "historical")
        self.assertTrue(shown["review"]["complete"])
        self.assertEqual(shown["review"]["sources"], [source_url])
        self.assertIn("model", shown["review"]["missing_facts"])

        shown_text = self.lane("show", "hist")
        self.assertIn("recovered", shown_text)

        status_entry = status.lane_entry(
            self.record("hist"), self.state, "example/repo", {}, {}, store=store
        )
        self.assertEqual(status_entry["review"]["status"], "recovered")
        self.assertTrue(status_entry["review"]["recovered"])
        self.assertTrue(status_entry["review"]["complete"])

        # Running `review hist` without --force-reason prints recovered evidence and does NOT rerun.
        rerun_check = self.lane("review", "hist")
        self.assertIn("Recovered historical review evidence of hist (complete)", rerun_check)

        # `done` succeeds and preserves the recovered provenance record intact.
        self.lane("done", "hist")
        self.assertTrue((self.state / "hist.review.json").is_file())
        after_done = self.review("hist")
        self.assertEqual(after_done["sources"], [source_url])
        self.assertTrue(after_done["recovered"])
        self.assertEqual(after_done["dispositions"][0]["commit"], fix_sha)

    def test_auto_discovery_from_linked_ticket_when_source_omitted(self) -> None:
        _rec, merge_sha = self._land_lane("auto-disc", "77", "lib/b.txt", "v1\n")
        fix_sha = self._add_integration_commit("lib/b.txt", "v2\n", "fix auto-disc")
        comment_url = "https://github.com/example/repo/issues/77#issuecomment-2002"
        self._set_gh(
            issue_comments={
                "77": [
                    {
                        "url": comment_url,
                        "body": f"Review of auto-disc (merge: {merge_sha[:10]}):\n1. Edge case [fixed: {fix_sha[:10]}]\n",
                        "createdAt": "2099-01-01T11:00:00Z",
                    }
                ]
            },
        )
        self.lane("review", "auto-disc", "--recover")
        raw = self.review("auto-disc")
        self.assertTrue(raw["complete"])
        self.assertEqual(raw["sources"], [comment_url])

    def test_prose_clean_phrase_unverified_scope_unrelated_issue_and_pre_creation_stay_partial(self) -> None:
        rec, merge_sha = self._land_lane("strict", "55", "lib/strict.txt", "v1\n")
        fix_sha = self._add_integration_commit("lib/strict.txt", "v2\n", "fix strict")
        store = self._store()

        # 1. Prose sentence containing "no issues found" stays partial even with matching merge.
        url_prose = "https://github.com/example/repo/issues/55#issuecomment-5501"
        self._set_gh(
            comments_by_id={
                "5501": {
                    "id": 5501,
                    "body": f"merge: {merge_sha[:10]}\nTried to reproduce on my laptop, no issues found.\n",
                    "created_at": "2099-01-01T10:00:00Z",
                }
            },
        )
        out1 = self.lane("review", "strict", "--recover", "--source", url_prose, ok=False)
        self.assertIn("partial", out1.lower())
        self.assertIsInstance(
            review_record.ReviewRecord.load(store, "strict", record=self.record("strict")).verdict,
            review_record.PartialEvidence,
        )

        # 2. Standalone clean line paired with unparsed finding-like lines stays partial (`- P1`, `### P1:`, `- [P1]`).
        for cid, shape in (
            ("5502", "- P1 lib/strict.txt:1 Broken check"),
            ("5506", "### P1: auth bypass"),
            ("5507", "- [P1] lib/strict.txt:3 missing guard"),
        ):
            url_unparsed = f"https://github.com/example/repo/issues/55#issuecomment-{cid}"
            self._set_gh(
                comments_by_id={
                    cid: {
                        "id": int(cid),
                        "body": f"merge: {merge_sha[:10]}\n{shape}\n0 findings\n",
                        "created_at": "2099-01-01T10:00:00Z",
                    }
                },
            )
            out2 = self.lane("review", "strict", "--recover", "--source", url_unparsed, ok=False)
            self.assertIn("unparsed finding-like", out2.lower())

        # 3. Dispositions with no reviewed scope or matching merge stay partial ("reviewed scope unverified").
        url_noscope = "https://github.com/example/repo/issues/55#issuecomment-5503"
        self._set_gh(
            comments_by_id={
                "5503": {
                    "id": 5503,
                    "body": f"1. Bug [fixed: {fix_sha[:10]}]\n",
                    "created_at": "2099-01-01T10:00:00Z",
                }
            },
        )
        out3 = self.lane("review", "strict", "--recover", "--source", url_noscope, ok=False)
        self.assertIn("reviewed scope unverified", out3.lower())

        # 4. Unrelated issue comment stays partial (scope mismatch on ticket).
        url_other_issue = "https://github.com/example/repo/issues/999#issuecomment-5504"
        self._set_gh(
            comments_by_id={
                "5504": {
                    "id": 5504,
                    "body": f"merge: {merge_sha[:10]}\nClean review.\n",
                    "created_at": "2099-01-01T10:00:00Z",
                }
            },
        )
        out4 = self.lane("review", "strict", "--recover", "--source", url_other_issue, ok=False)
        self.assertIn("not the lane's linked ticket", out4.lower())

        # 5. Pre-lane-creation comment stays partial even with standalone clean statement and matching merge.
        url_old_ts = "https://github.com/example/repo/issues/55#issuecomment-5505"
        self._set_gh(
            comments_by_id={
                "5505": {
                    "id": 5505,
                    "body": f"merge: {merge_sha[:10]}\nClean review.\n",
                    "created_at": "2020-01-01T00:00:00Z",
                }
            },
        )
        out5 = self.lane("review", "strict", "--recover", "--source", url_old_ts, ok=False)
        self.assertIn("predates lane creation", out5.lower())

        # Gate still holds `strict` after all partial attempts.
        report = review_gate.gate_report(store, str(self.integration), rec["common_dir"], self.tip())
        self.assertTrue(any(e["lane"] == "strict" for e in report["unfinished"]))

    def test_refuses_active_lane_and_invalid_reviewed_range_upfront(self) -> None:
        self.lane("start", "act", "--ticket", "60", "--base", "integrate/q", "--claims", "lib/act.txt", "--create")
        wt = Path(self.record("act")["worktree"])
        self.commit(wt, {"lib/act.txt": "v1\n"})
        url = "https://github.com/example/repo/issues/60#issuecomment-6001"
        self._set_gh(comments_by_id={"6001": {"id": 6001, "body": "Clean review.\n"}})

        # Active (unlanded) lane is refused upfront.
        refused_active = self.lane("review", "act", "--recover", "--source", url, ok=False)
        self.assertIn("not landed", refused_active)
        self.assertFalse((self.state / "act.review.json").exists())

        # Land it, then test invalid --reviewed-range is refused upfront without writing a receipt.
        self.lane("check", "act")
        self.lane("land", "act", "--wait", "0", "--keep")
        refused_range = self.lane(
            "review", "act", "--recover", "--source", url, "--reviewed-range", "not-a-sha..HEAD", ok=False
        )
        self.assertIn("is not a commit", refused_range)
        self.assertFalse((self.state / "act.review.json").exists())

    def test_partial_evidence_visibly_incomplete_and_never_satisfies_gate(self) -> None:
        rec, merge_sha = self._land_lane("part", "43", "lib/c.txt", "v1\n")
        fix_sha = self._add_integration_commit("lib/c.txt", "v2\n", "fix 1")
        source_url = "https://github.com/example/repo/issues/43#issuecomment-1002"
        self._set_gh(
            comments_by_id={
                "1002": {
                    "id": 1002,
                    "body": (
                        f"merge: {merge_sha[:10]}\n"
                        f"1. P2 lib/c.txt:1 Fixed issue [fixed: {fix_sha[:10]}]\n"
                        "2. P1 lib/c.txt:2 Unresolved issue [open]\n"
                    ),
                    "created_at": "2099-01-01T10:00:00Z",
                }
            },
        )
        out = self.lane("review", "part", "--recover", "--source", source_url, ok=False)
        self.assertIn("partial", out.lower())

        store = self._store()
        rev = review_record.ReviewRecord.load(store, "part", record=self.record("part"))
        self.assertIsInstance(rev.verdict, review_record.PartialEvidence)
        self.assertFalse(rev.allows_promote)

        report = review_gate.gate_report(store, str(self.integration), rec["common_dir"], self.tip())
        self.assertTrue(any(e["lane"] == "part" for e in report["unfinished"]))

        shown = json.loads(self.lane("show", "part", "--json"))
        self.assertEqual(shown["review"]["status"], "recovered (partial)")
        self.assertFalse(shown["review"]["complete"])

        readiness = done.done_readiness(self.record("part"), store)
        self.assertFalse(readiness.ready)
        self.assertIn("--recover", readiness.refusal or "")

        check_out = self.lane("review", "part", ok=False)
        self.assertIn("partial", check_out.lower())

    def test_unavailable_source_records_clearly_without_fabricating(self) -> None:
        rec, _merge_sha = self._land_lane("unavail", "44", "lib/d.txt", "v1\n")
        source_url = "https://github.com/example/repo/issues/44#issuecomment-4040"
        self._set_gh(fail_ids={"4040"})
        out = self.lane("review", "unavail", "--recover", "--source", source_url, ok=False)
        self.assertIn("unavailable source", out.lower())

        raw = self.review("unavail")
        self.assertFalse(raw["complete"])
        self.assertEqual(raw["dispositions"], [])
        self.assertEqual(raw["findings"], [])
        self.assertEqual(len(raw["unavailable_sources"]), 1)
        self.assertEqual(raw["unavailable_sources"][0]["url"], source_url)
        self.assertIsNone(raw["model"])

        store = self._store()
        report = review_gate.gate_report(store, str(self.integration), rec["common_dir"], self.tip())
        self.assertTrue(any(e["lane"] == "unavail" for e in report["unfinished"]))

    def test_fix_commit_not_in_target_history_is_partial_and_holds_gate(self) -> None:
        rec, merge_sha = self._land_lane("badfix", "45", "lib/e.txt", "v1\n")
        orig_branch = self.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        self.git(self.repo, "checkout", "-q", "-b", "unmerged-side")
        unmerged_sha = self.commit(self.repo, {"lib/side.txt": "side\n"}, message="unmerged fix")
        self.git(self.repo, "checkout", "-q", orig_branch)

        source_url = "https://github.com/example/repo/issues/45#issuecomment-1005"
        self._set_gh(
            comments_by_id={
                "1005": {
                    "id": 1005,
                    "body": f"merge: {merge_sha[:10]}\n1. Bug [fixed: {unmerged_sha[:10]}]\n",
                    "created_at": "2099-01-01T10:00:00Z",
                }
            },
        )
        out = self.lane("review", "badfix", "--recover", "--source", source_url, ok=False)
        self.assertIn("NOT in target history", out)

        raw = self.review("badfix")
        self.assertFalse(raw["complete"])
        self.assertFalse(raw["dispositions"][0]["in_target_history"])
        self.assertFalse(raw["dispositions"][0]["verified"])

        store = self._store()
        report = review_gate.gate_report(store, str(self.integration), rec["common_dir"], self.tip())
        self.assertTrue(any(e["lane"] == "badfix" for e in report["unfinished"]))

    def test_scope_mismatch_and_lane_id_reuse_rejected_as_partial(self) -> None:
        _rec1, _merge1 = self._land_lane("reuse", "46", "lib/r1.txt", "incarnation 1\n")
        fix1_sha = self._add_integration_commit("lib/r1.txt", "incarnation 1 fixed\n", "fix incarnation 1")
        self._set_gh()
        self.lane("done", "reuse", "--force-reason", "test reuse")

        rec2, merge2 = self._land_lane("reuse", "47", "lib/r2.txt", "incarnation 2\n")

        old_url = "https://github.com/example/repo/issues/46#issuecomment-9001"
        self._set_gh(
            comments_by_id={
                "9001": {
                    "id": 9001,
                    "body": f"1. Old bug [fixed: {fix1_sha[:10]}]\n",
                    "created_at": "2099-01-01T10:00:00Z",
                }
            },
        )
        out = self.lane("review", "reuse", "--recover", "--source", old_url, ok=False)
        self.assertIn("scope mismatch", out.lower())

        raw = self.review("reuse")
        self.assertFalse(raw["complete"])
        self.assertFalse(raw["dispositions"][0]["verified"])

        store = self._store()
        report = review_gate.gate_report(store, str(self.integration), rec2["common_dir"], self.tip())
        self.assertTrue(any(e["lane"] == "reuse" for e in report["unfinished"]))

        other_url = "https://github.com/example/repo/issues/47#issuecomment-9002"
        fix2_sha = self._add_integration_commit("lib/r2.txt", "incarnation 2 fixed\n", "fix incarnation 2")
        self._set_gh(
            comments_by_id={
                "9002": {
                    "id": 9002,
                    "body": f"merge: {merge2[:10]}\n1. New bug [fixed: {fix2_sha[:10]}]\n",
                    "created_at": "2099-01-01T10:00:00Z",
                }
            },
        )
        out_range = self.lane(
            "review", "reuse", "--recover", "--source", other_url,
            "--reviewed-range", f"{fix1_sha}^..{fix1_sha}",
            ok=False,
        )
        self.assertIn("scope mismatch", out_range.lower())

    def test_interrupted_duplicate_review_preserved_and_executed_or_stale_review_refused(self) -> None:
        rec, merge_sha = self._land_lane("dup", "48", "lib/f.txt", "v1\n")
        fix_sha = self._add_integration_commit("lib/f.txt", "v2\n", "fix dup")
        reviews_dir = self.state / "reviews"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        dup_log = reviews_dir / "dup.log"
        dup_log.write_text("partial interrupted review log\n", encoding="utf-8")
        store = self._store()
        review_record.ReviewRecord.begin(
            store,
            "dup",
            {"kind": "lane", "lane": "dup", "lane_created": rec["created"]},
            pid=999_999_999,
            pid_start=None,
            log=str(dup_log),
            effort="medium",
        )

        source_url = "https://github.com/example/repo/issues/48#issuecomment-1008"
        self._set_gh(
            comments_by_id={
                "1008": {
                    "id": 1008,
                    "body": f"merge: {merge_sha[:10]}\n1. Fixed issue [fixed: {fix_sha[:10]}]\n",
                    "created_at": "2099-01-01T10:00:00Z",
                }
            },
        )
        out = self.lane("review", "dup", "--recover", "--source", source_url)
        self.assertIn("preserved artifacts: 1", out)

        self.assertEqual(dup_log.read_text(encoding="utf-8"), "partial interrupted review log\n")
        raw = self.review("dup")
        self.assertEqual(len(raw["preserved_artifacts"]), 1)
        self.assertEqual(raw["preserved_artifacts"][0]["kind"], "died_review")
        self.assertEqual(raw["preserved_artifacts"][0]["phase"], "died")
        self.assertEqual(raw["preserved_artifacts"][0]["receipt"]["pid"], 999_999_999)
        self.assertIn(str(dup_log), raw["preserved_artifacts"][0]["files"])

        # Complete executed review refuses --recover without overwriting.
        rec_exec, _ = self._land_lane("exec", "49", "lib/g.txt", "v1\n")
        live_pid = os.getpid()
        review_record.ReviewRecord.begin(
            store,
            "exec",
            {"kind": "lane", "lane": "exec", "lane_created": rec_exec["created"], "tip": rec_exec["landed"]["tip"]},
            pid=live_pid,
            log=None,
            effort="medium",
        )
        review_record.ReviewRecord.finish(
            store,
            "exec",
            {"reviewer": "sol", "model": "gpt-6.1-sol", "findings": []},
            owner_pid=live_pid,
        )
        before_bytes = (self.state / "exec.review.json").read_bytes()
        refused = self.lane("review", "exec", "--recover", "--source", source_url, ok=False)
        self.assertIn("already has an executed review receipt", refused)
        self.assertEqual((self.state / "exec.review.json").read_bytes(), before_bytes)

        # Stale executed review also gives clear refusal (not "cannot apply 'recover' in phase 'finished'").
        rec_stale, _ = self._land_lane("stale-ex", "52", "lib/st.txt", "v1\n")
        review_record.ReviewRecord.begin(
            store,
            "stale-ex",
            {
                "kind": "lane",
                "lane": "stale-ex",
                "lane_created": rec_stale["created"],
                "tip": rec_stale["landed"]["previous"],
            },
            pid=live_pid,
            log=None,
            effort="medium",
        )
        review_record.ReviewRecord.finish(
            store,
            "stale-ex",
            {"reviewer": "sol", "model": "gpt-6.1-sol", "findings": []},
            owner_pid=live_pid,
        )
        refused_stale = self.lane("review", "stale-ex", "--recover", "--source", source_url, ok=False)
        self.assertIn("already has an executed review receipt", refused_stale)

    def test_complete_recovered_not_wiped_by_failed_rerun_without_force(self) -> None:
        _rec, merge_sha = self._land_lane("keep-rec", "53", "lib/k.txt", "v1\n")
        fix_sha = self._add_integration_commit("lib/k.txt", "v2\n", "fix k")
        good_url = "https://github.com/example/repo/issues/53#issuecomment-5301"
        bad_url = "https://github.com/example/repo/issues/53#issuecomment-5302"
        self._set_gh(
            comments_by_id={
                "5301": {
                    "id": 5301,
                    "body": f"merge: {merge_sha[:10]}\n1. Bug [fixed: {fix_sha[:10]}]\n",
                    "created_at": "2099-01-01T10:00:00Z",
                }
            },
            fail_ids={"5302"},
        )
        self.lane("review", "keep-rec", "--recover", "--source", good_url)
        before_bytes = (self.state / "keep-rec.review.json").read_bytes()

        # Re-running with an unavailable source refuses without wiping the complete recovered receipt.
        refused = self.lane("review", "keep-rec", "--recover", "--source", bad_url, ok=False)
        self.assertIn("already has complete recovered evidence", refused)
        self.assertEqual((self.state / "keep-rec.review.json").read_bytes(), before_bytes)

        # With --force-reason, preserves the previous complete recovered payload in preserved_artifacts.
        forced = self.lane(
            "review", "keep-rec", "--recover", "--source", bad_url, "--force-reason", "re-verify from archive",
            ok=False,
        )
        self.assertIn("preserved artifacts: 1", forced)
        after = self.review("keep-rec")
        self.assertFalse(after["complete"])
        self.assertEqual(len(after["preserved_artifacts"]), 1)
        self.assertEqual(after["preserved_artifacts"][0]["kind"], "recovered_review")
        self.assertTrue(after["preserved_artifacts"][0]["receipt"]["complete"])

    def test_record_does_not_merge_into_recovered_receipt_on_double_lane(self) -> None:
        _rec, merge_sha = self._land_lane("dbl", "54", "lib/dbl.txt", "v1\n", extra_start=("--review", "double"))
        fix_sha = self._add_integration_commit("lib/dbl.txt", "v2\n", "fix dbl")
        source_url = "https://github.com/example/repo/issues/54#issuecomment-5401"
        self._set_gh(
            comments_by_id={
                "5401": {
                    "id": 5401,
                    "body": f"reviewer: opus\nmerge: {merge_sha[:10]}\n1. Issue [fixed: {fix_sha[:10]}]\n",
                    "created_at": "2099-01-01T10:00:00Z",
                }
            },
        )
        self.lane("review", "dbl", "--recover", "--source", source_url)
        raw_rec = self.review("dbl")
        self.assertTrue(raw_rec["recovered"])
        # Recovered receipts NEVER count toward distinct_finished_reviewers for double-review class.
        self.assertEqual(review_class.distinct_finished_reviewers(raw_rec), [])

        report_file = self.tmp / "sol-report.md"
        report_file.write_text(NO_FINDINGS, encoding="utf-8")

        # Without --force-reason, --record refuses to merge into a recovered receipt.
        out = self.lane("review", "dbl", "--record", str(report_file), "--reviewer", "sol")
        self.assertIn("recovered historical review evidence", out.lower())
        still_rec = self.review("dbl")
        self.assertTrue(still_rec["recovered"])
        self.assertNotIn("panel", still_rec)

        # With --force-reason, --record replaces cleanly without recovered=True or phantom panel.
        self.lane(
            "review", "dbl", "--record", str(report_file), "--reviewer", "sol",
            "--force-reason", "replace historical recovery with fresh recorded review",
        )
        replaced = self.review("dbl")
        self.assertNotIn("recovered", replaced)
        self.assertNotIn("evidence_source", replaced)
        self.assertEqual(review_class.distinct_finished_reviewers(replaced), ["sol"])
        self.assertIsNotNone(review_class.double_review_problem(replaced, label="dbl"))

    def test_idempotent_recovery(self) -> None:
        _rec, merge_sha = self._land_lane("idem", "50", "lib/h.txt", "v1\n")
        fix_sha = self._add_integration_commit("lib/h.txt", "v2\n", "fix idem")
        source_url = "https://github.com/example/repo/issues/50#issuecomment-1050"
        self._set_gh(
            comments_by_id={
                "1050": {
                    "id": 1050,
                    "body": f"merge: {merge_sha[:10]}\n1. Issue [fixed: {fix_sha[:10]}]\n",
                    "created_at": "2099-01-01T10:00:00Z",
                }
            },
        )
        self.lane("review", "idem", "--recover", "--source", source_url)
        first_raw = self.review("idem")

        self.lane("review", "idem", "--recover", "--source", source_url)
        second_raw = self.review("idem")

        self.assertEqual(first_raw, second_raw)
        self.assertEqual(len(second_raw["sources"]), 1)
        self.assertEqual(len(second_raw["dispositions"]), 1)
        self.assertEqual(second_raw["preserved_artifacts"], [])

    def test_surfaces_recovery_option_before_recommending_duplicate_review(self) -> None:
        rec, _merge_sha = self._land_lane("surf", "51", "lib/s.txt", "v1\n")
        store = self._store()

        shown_text = self.lane("show", "surf")
        self.assertIn("review surf --recover", shown_text)
        shown_json = json.loads(self.lane("show", "surf", "--json"))
        self.assertIn("review surf --recover", shown_json["review"]["recovery_hint"])

        status_entry = status.lane_entry(
            self.record("surf"), self.state, "example/repo", {}, {}, store=store
        )
        self.assertIn("review surf --recover", status_entry["review"]["recovery_hint"])

        report = review_gate.gate_report(store, str(self.integration), rec["common_dir"], self.tip())
        self.assertTrue(any("--recover" in e["text"] for e in report["unfinished"]))

        readiness = done.done_readiness(self.record("surf"), store)
        self.assertFalse(readiness.ready)
        self.assertIn("--recover", readiness.refusal or "")

        review_record.ReviewRecord.begin(
            store,
            "surf",
            {"kind": "lane", "lane": "surf", "lane_created": rec["created"]},
            pid=999_999_999,
            pid_start=None,
            log="/tmp/surf.log",
            effort="medium",
        )
        dead_rev = review_record.ReviewRecord.load(store, "surf", record=self.record("surf"))
        lines = review.review_lines(dead_rev, record=self.record("surf"), prog="lanefather")
        self.assertTrue(any("--recover" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
