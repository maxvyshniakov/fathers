"""Detached macOS browser and XPC helper attribution to leases (fathers#202).

Tests pure attribution rules, exclusion of user/system browsers, multi-lease and unleased ambiguity,
UID/start-time validation, and integration across board, usage, query, procs, and sweep with mocked
process tables (no live XPC services required).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slotfather import board as sf_board
from slotfather import process as sf_process
from slotfather import procs as sf_procs
from slotfather import query as sf_query
from slotfather import sweep as sf_sweep
from slotfather import sweep_plan
from slotfather import usage as sf_usage

UID = os.geteuid()
SINCE = 1_800_000_000.0
BORN_UTC = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(SINCE))

PW_WEBKIT_2370 = "/Users/dev/Library/Caches/ms-playwright/webkit-2370"
PW_WEBKIT_2359 = "/Users/dev/Library/Caches/ms-playwright/webkit-2359"
PW_CHROME_1248 = "/Users/dev/Library/Caches/ms-playwright/chromium-1248"
PW_FIREFOX_1555 = "/Users/dev/Library/Caches/ms-playwright/firefox-1555"

WEBKIT_DRIVER_2370 = f"{PW_WEBKIT_2370}/Playwright.app/Contents/MacOS/Playwright --inspector-pipe --headless --no-startup-window"
WEBKIT_DRIVER_2359 = f"{PW_WEBKIT_2359}/Playwright.app/Contents/MacOS/Playwright --inspector-pipe --headless --no-startup-window"
WEBKIT_WEBCONTENT_2370 = (
    f"{PW_WEBKIT_2370}/com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent.Development"
)
WEBKIT_GPU_2370 = (
    f"{PW_WEBKIT_2370}/com.apple.WebKit.GPU.xpc/Contents/MacOS/com.apple.WebKit.GPU.Development"
)
WEBKIT_NET_2370 = (
    f"{PW_WEBKIT_2370}/com.apple.WebKit.Networking.xpc/Contents/MacOS/com.apple.WebKit.Networking.Development"
)
WEBKIT_FRAMEWORK_2370 = (
    f"{PW_WEBKIT_2370}/WebKit.framework/Versions/A/XPCServices/com.apple.WebKit.WebContent.xpc"
    "/Contents/MacOS/com.apple.WebKit.WebContent.Development"
)
WEBKIT_WEBCONTENT_2359 = (
    f"{PW_WEBKIT_2359}/com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent.Development"
)

SYSTEM_WEBKIT_CONTENT = (
    "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
    "com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent"
)
SAFARI_TP_CONTENT = (
    "/Applications/Safari Technology Preview.app/Contents/Frameworks/WebKit.framework/Versions/A/"
    "XPCServices/com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent"
)
USER_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
USER_CHROME_HELPER = (
    "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/"
    "Versions/157.0/Helpers/Google Chrome Helper (Renderer).app/Contents/MacOS/"
    "Google Chrome Helper (Renderer) --type=renderer"
)
USER_FIREFOX_HELPER = (
    "/Applications/Firefox.app/Contents/MacOS/plugin-container.app/Contents/MacOS/plugin-container -isForBrowser tab"
)


def lstart(offset: float = 0.0) -> str:
    return time.strftime("%a %b %e %H:%M:%S %Y", time.localtime(SINCE + offset))


class DetachedBrowserHelperRuleTests(unittest.TestCase):
    def test_single_lease_playwright_webkit_xpc_helpers_attributed_and_user_browsers_excluded(self) -> None:
        parents = {
            100: 1, 101: 100, 102: 101, 103: 101,
            # Detached Playwright WebKit XPC helpers (PPID=1) + child of one helper
            501: 1, 502: 1, 503: 1, 504: 1, 505: 501,
            # Unrelated user/system browser processes (PPID=1)
            601: 1, 602: 1, 603: 1, 604: 1, 605: 1,
        }
        commands = {
            100: "python3 slotfather run --label e2e -- npx playwright test",
            101: "node /w/node_modules/.bin/playwright test --project=webkit",
            102: WEBKIT_DRIVER_2370,
            103: WEBKIT_DRIVER_2370,  # parallel worker inside the SAME lease tree
            501: WEBKIT_WEBCONTENT_2370,
            502: WEBKIT_GPU_2370,
            503: WEBKIT_NET_2370,
            504: WEBKIT_FRAMEWORK_2370,
            505: "/usr/libexec/some-sub-helper",
            601: SYSTEM_WEBKIT_CONTENT,
            602: SAFARI_TP_CONTENT,
            603: USER_CHROME,
            604: USER_CHROME_HELPER,
            605: USER_FIREFOX_HELPER,
        }
        uids = {pid: UID for pid in parents}
        starts: dict[int, float | None] = {pid: SINCE + 5.0 for pid in parents}
        found = sf_process.detached_browser_helpers(
            parents, commands, [100], uids=uids, starts=starts, lease_since=[SINCE],
        )
        self.assertEqual({0: {501, 502, 503, 504, 505}}, found)

    def test_shell_pw_run_and_custom_browsers_path_match(self) -> None:
        custom = "/tmp/custom-pw-browsers/webkit-2370"
        parents = {100: 1, 101: 100, 501: 1}
        commands = {
            100: "python3 slotfather run -- bash run.sh",
            101: f"/bin/bash {custom}/pw_run.sh --inspector-pipe --headless",
            501: f"{custom}/com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent.Development",
        }
        found = sf_process.detached_browser_helpers(parents, commands, [100])
        self.assertEqual({0: {501}}, found)

    def test_unrelated_lease_does_not_claim_unleased_playwright_helpers_or_grep_commands(self) -> None:
        parents = {100: 1, 101: 100, 501: 1, 700: 1, 701: 700}
        commands = {
            100: "python3 slotfather run -- pytest",
            101: f"grep {PW_WEBKIT_2370} /tmp/log.txt",  # mentions path as argument, NOT a driver
            501: WEBKIT_WEBCONTENT_2370,
            700: "node playwright test",
            701: WEBKIT_DRIVER_2370,
        }
        found = sf_process.detached_browser_helpers(parents, commands, [100])
        self.assertEqual({}, found)

    def test_multi_lease_same_webkit_driver_is_ambiguous_and_not_double_counted(self) -> None:
        parents = {
            100: 1, 101: 100,  # lease 0
            200: 1, 201: 200,  # lease 1
            501: 1, 502: 1,    # detached XPC helpers for webkit-2370
        }
        commands = {
            100: "python3 slotfather run --label a -- npx playwright test",
            101: WEBKIT_DRIVER_2370,
            200: "python3 slotfather run --label b -- npx playwright test",
            201: WEBKIT_DRIVER_2370,
            501: WEBKIT_WEBCONTENT_2370,
            502: WEBKIT_GPU_2370,
        }
        found = sf_process.detached_browser_helpers(parents, commands, [100, 200])
        self.assertEqual({}, found, "ambiguous helpers must never be double-counted or guessed across leases")

    def test_lease_plus_unleased_same_webkit_driver_is_ambiguous(self) -> None:
        parents = {
            100: 1, 101: 100,  # lease 0
            900: 1, 901: 900,  # unleased Playwright run using the same webkit-2370
            501: 1,
        }
        commands = {
            100: "python3 slotfather run --label a -- npx playwright test",
            101: WEBKIT_DRIVER_2370,
            900: "node /other/playwright test",
            901: WEBKIT_DRIVER_2370,
            501: WEBKIT_WEBCONTENT_2370,
        }
        found = sf_process.detached_browser_helpers(parents, commands, [100])
        self.assertEqual({}, found)

    def test_multi_lease_distinct_webkit_versions_attribute_independently(self) -> None:
        parents = {
            100: 1, 101: 100,
            200: 1, 201: 200,
            501: 1, 502: 1,
        }
        commands = {
            100: "python3 slotfather run --label a -- npx playwright test",
            101: WEBKIT_DRIVER_2370,
            200: "python3 slotfather run --label b -- npx playwright test",
            201: WEBKIT_DRIVER_2359,
            501: WEBKIT_WEBCONTENT_2370,
            502: WEBKIT_WEBCONTENT_2359,
        }
        found = sf_process.detached_browser_helpers(parents, commands, [100, 200])
        self.assertEqual({0: {501}, 1: {502}}, found)

    def test_multi_lease_same_chromium_disambiguated_by_unique_temp_profile(self) -> None:
        prof_a = "/var/folders/ab/cdefgh/T/playwright_chromiumdev_profile-AAA"
        prof_a_priv = "/private/var/folders/ab/cdefgh/T/playwright_chromiumdev_profile-AAA"
        prof_b = "/var/folders/ab/cdefgh/T/playwright_chromiumdev_profile-BBB"
        prof_stale = "/var/folders/ab/cdefgh/T/playwright_chromiumdev_profile-OLD"
        chrome_bin = f"{PW_CHROME_1248}/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        helper_bin = (
            f"{PW_CHROME_1248}/chrome-mac-arm64/Google Chrome for Testing.app/Contents/Frameworks/"
            "Google Chrome for Testing Framework.framework/Versions/130.0/Helpers/"
            "Google Chrome for Testing Helper (Renderer).app/Contents/MacOS/"
            "Google Chrome for Testing Helper (Renderer)"
        )
        parents = {
            100: 1, 101: 100,
            200: 1, 201: 200,
            501: 1, 502: 1, 503: 1, 504: 1,
        }
        commands = {
            100: "python3 slotfather run --label a -- test",
            101: f"{chrome_bin} --headless --user-data-dir={prof_a}",
            200: "python3 slotfather run --label b -- test",
            201: f"{chrome_bin} --headless --user-data-dir={prof_b}",
            501: f"{helper_bin} --type=renderer --user-data-dir={prof_a_priv}",
            502: f"{helper_bin} --type=renderer --user-data-dir={prof_b}",
            503: f"{helper_bin} --type=gpu-process",  # no profile -> falls back to ambiguous chromium-1248
            504: f"{helper_bin} --type=renderer --user-data-dir={prof_stale}",  # unmatched temp profile
        }
        found = sf_process.detached_browser_helpers(parents, commands, [100, 200])
        self.assertEqual({0: {501}, 1: {502}}, found)

    def test_channel_chrome_only_matches_isolated_profile_and_spares_user_chrome(self) -> None:
        prof = "/var/folders/ab/cdefgh/T/playwright_chromiumdev_profile-CHN"
        user_prof = "/Users/dev/Library/Application Support/Google/Chrome"
        parents = {100: 1, 101: 100, 501: 1, 502: 1, 503: 1}
        commands = {
            100: "python3 slotfather run --label a -- test",
            101: f"{USER_CHROME} --headless --user-data-dir={prof}",
            501: f"{USER_CHROME_HELPER} --user-data-dir={prof}",
            502: USER_CHROME_HELPER,
            503: f"{USER_CHROME_HELPER} --user-data-dir={user_prof}",
        }
        found = sf_process.detached_browser_helpers(parents, commands, [100])
        self.assertEqual({0: {501}}, found)

    def test_firefox_profile_flag_and_uid_and_start_time_filtering(self) -> None:
        prof = "/var/folders/ab/cdefgh/T/playwright_firefoxdev_profile-FF1"
        ff_driver = f"{PW_FIREFOX_1555}/firefox/Nightly.app/Contents/MacOS/firefox -no-remote -headless -profile {prof}"
        ff_helper = f"{PW_FIREFOX_1555}/firefox/Nightly.app/Contents/MacOS/plugin-container.app/Contents/MacOS/plugin-container -profile {prof}"
        parents = {100: 1, 101: 100, 501: 1, 502: 1, 503: 1}
        commands = {
            100: "python3 slotfather run --label ff -- test",
            101: ff_driver,
            501: ff_helper,  # same uid, started after lease -> match
            502: ff_helper,  # different uid -> excluded
            503: ff_helper,  # started well before lease -> excluded
        }
        uids = {100: UID, 101: UID, 501: UID, 502: UID + 1, 503: UID}
        starts: dict[int, float | None] = {100: SINCE, 101: SINCE + 1, 501: SINCE + 2, 502: SINCE + 2, 503: SINCE - 120}
        found = sf_process.detached_browser_helpers(
            parents, commands, [100], uids=uids, starts=starts, lease_since=[SINCE],
        )
        self.assertEqual({0: {501}}, found)


class DashboardAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="slotfather-board-xpc-")
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        (self.state / "queue").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {
            "SLOTFATHER_DIR": str(self.state),
            "SLOTFATHER_LEGACY_DIRS": "",
            "SLOTFATHER_CORES": "12",
            "SLOTFATHER_MEMORY_GIB": "64",
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_lease(self, seq: int, pid: int, label: str, start: str, count: int = 2) -> None:
        path = self.state / "queue" / f"{seq:010d}-{pid}.json"
        path.write_text(json.dumps({
            "pid": pid, "start": start, "count": count, "state": "held",
            "label": label, "since": BORN_UTC, "granted": BORN_UTC, "cmd": "npx playwright test",
        }))

    def _ps_handler(self, rows: list[tuple[int, int, int, float, int, str, str]]) -> Any:
        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv and argv[0] == "ps":
                if sf_process.IDENTITY in argv:
                    text = "".join(f"{pid} {ppid} S {start}\n" for pid, ppid, _, _, _, start, _ in rows)
                    return subprocess.CompletedProcess(argv, 0, text, "")
                if sf_process.CPU_FULL in argv:
                    text = "".join(
                        f"{pid} {ppid} {uid} {cpu:.1f} {rss} {start} {cmd}\n"
                        for pid, ppid, uid, cpu, rss, start, cmd in rows
                    )
                    return subprocess.CompletedProcess(argv, 0, text, "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return fake_run

    def test_dashboard_model_includes_detached_webkit_xpc_cpu_rss_and_processes(self) -> None:
        born = lstart(0.0)
        later = lstart(5.0)
        me = os.getpid()
        self._write_lease(1, 100, "e2e-webkit", born, count=2)
        rows = [
            (me, 1, UID, 0.0, 1000, born, "python3 -m unittest"),
            (100, 1, UID, 10.0, 2048, born, "python3 slotfather run --label e2e-webkit -- npx playwright test"),
            (101, 100, UID, 160.0, 8192, later, WEBKIT_DRIVER_2370),
            # Detached XPC helpers driving 10 cores (1000% CPU) + 2 GB RSS
            (501, 1, UID, 750.0, 1_500_000, later, WEBKIT_WEBCONTENT_2370),
            (502, 1, UID, 200.0, 400_000, later, WEBKIT_GPU_2370),
            (503, 1, UID, 50.0, 100_000, later, WEBKIT_NET_2370),
            # Unrelated system WebKit XPC helper (must NOT be attributed)
            (601, 1, UID, 80.0, 50_000, later, SYSTEM_WEBKIT_CONTENT),
        ]
        with mock.patch("sys.platform", "darwin"), \
                mock.patch("subprocess.run", side_effect=self._ps_handler(rows)), \
                mock.patch("slotfather.platform.swap_usage", return_value=(0.0, 0.0)), \
                mock.patch("slotfather.platform.pressure_level", return_value=1.0), \
                mock.patch("slotfather.platform.loads", return_value=(12.0, 11.5, 10.0)), \
                mock.patch("slotfather.platform.sysctl", return_value=None):
            model = sf_board.dashboard_model()
        self.assertEqual(1, len(model["holders"]))
        holder = model["holders"][0]
        self.assertAlmostEqual(1170.0, holder["cpu"], places=1)
        self.assertEqual(2048 + 8192 + 1_500_000 + 400_000 + 100_000, holder["rss"])
        self.assertEqual(5, holder["procs"])
        pids_in_tree = [entry[0] for entry in holder["tree"]]
        self.assertEqual([501, 502, 101, 503, 100], pids_in_tree)
        self.assertNotIn(601, pids_in_tree)
        names_by_pid = {entry[0]: entry[3] for entry in holder["tree"]}
        self.assertEqual("com.apple.WebKit.WebContent.Development", names_by_pid[501])
        self.assertEqual("com.apple.WebKit.GPU.Development", names_by_pid[502])
        self.assertEqual("Playwright", names_by_pid[101])

    def test_dashboard_model_does_not_double_count_when_two_leases_share_webkit_driver(self) -> None:
        born = lstart(0.0)
        later = lstart(5.0)
        me = os.getpid()
        self._write_lease(1, 100, "e2e-a", born, count=1)
        self._write_lease(2, 200, "e2e-b", born, count=1)
        rows = [
            (me, 1, UID, 0.0, 1000, born, "python3 -m unittest"),
            (100, 1, UID, 10.0, 2048, born, "python3 slotfather run --label e2e-a -- test"),
            (101, 100, UID, 40.0, 4096, later, WEBKIT_DRIVER_2370),
            (200, 1, UID, 10.0, 2048, born, "python3 slotfather run --label e2e-b -- test"),
            (201, 200, UID, 30.0, 4096, later, WEBKIT_DRIVER_2370),
            (501, 1, UID, 600.0, 900_000, later, WEBKIT_WEBCONTENT_2370),
        ]
        with mock.patch("sys.platform", "darwin"), \
                mock.patch("subprocess.run", side_effect=self._ps_handler(rows)), \
                mock.patch("slotfather.platform.swap_usage", return_value=(0.0, 0.0)), \
                mock.patch("slotfather.platform.pressure_level", return_value=1.0), \
                mock.patch("slotfather.platform.loads", return_value=(8.0, 8.0, 8.0)), \
                mock.patch("slotfather.platform.sysctl", return_value=None):
            model = sf_board.dashboard_model()
        by_label = {row["label"]: row for row in model["holders"]}
        self.assertAlmostEqual(50.0, by_label["e2e-a"]["cpu"], places=1)
        self.assertAlmostEqual(40.0, by_label["e2e-b"]["cpu"], places=1)
        self.assertNotIn(501, [p[0] for p in by_label["e2e-a"]["tree"]])
        self.assertNotIn(501, [p[0] for p in by_label["e2e-b"]["tree"]])


class UsageAndWatchAttributionTests(unittest.TestCase):
    def test_tree_usage_and_watch_attribute_detached_webkit_xpc_helpers_outside_lock(self) -> None:
        born = lstart(0.0)
        later = lstart(5.0)
        table: dict[int, tuple[int, str, int, int, float]] = {
            100: (50, born, UID, 2048, 1.0),
            101: (100, later, UID, 4096, 2.0),
            501: (1, later, UID, 512_000, 18.5),  # Playwright WebKit WebContent XPC helper
            601: (1, later, UID, 50_000, 9.0),    # System WebKit XPC helper (excluded)
        }
        ctx = sf_process.ProcessContext(
            parents={100: 50, 101: 100, 501: 1, 601: 1},
            uids={100: UID, 101: UID, 501: UID, 601: UID},
            starts={100: SINCE, 101: SINCE + 5, 501: SINCE + 5, 601: SINCE + 5},
            raw_starts={100: born, 101: later, 501: later, 601: later},
            commands={100: "node playwright test", 101: WEBKIT_DRIVER_2370,
                      501: WEBKIT_WEBCONTENT_2370, 601: SYSTEM_WEBKIT_CONTENT},
        )
        lock_states: list[bool] = []
        watch: sf_usage.Watch

        def attributed(pids: list[int], rows: dict[int, tuple[Any, ...]]) -> set[int]:
            lock_states.append(watch.lock.locked())
            return sf_process.attributed_browser_helpers(pids, rows, 100, SINCE)

        usage = sf_usage.TreeUsage(100, SINCE, lambda _pids: set(), uid=UID, attributed=attributed)
        watch = sf_usage.Watch(usage, Path("/nonexistent"))
        with mock.patch.object(sf_process, "usage_table", return_value=table), \
                mock.patch.object(sf_process, "commands", return_value={501: WEBKIT_WEBCONTENT_2370, 601: SYSTEM_WEBKIT_CONTENT}), \
                mock.patch.object(sf_process, "helper_snapshot", return_value=ctx), \
                mock.patch.object(sf_usage, "sample_machine", return_value=False):
            watch.look()
        self.assertEqual([False], lock_states, "helper attribution must run outside Watch.lock")
        # Next sample: helper burns more CPU, then everything exits
        usage.observe(SINCE + 10, {**table, 501: (1, later, UID, 512_000, 25.0)}, adopted=set())
        usage.observe(SINCE + 12, {}, adopted=set())
        result = usage.result(reaped_cpu=3.0)
        self.assertEqual(25.0, result["cpu_detached"])
        self.assertEqual(28.0, result["cpu"])
        self.assertEqual(3, result["processes"])
        self.assertGreaterEqual(result["rss_peak_mb"], 500.0)

    def test_attributed_browser_helpers_rejects_reused_pid_and_skips_full_scan_for_plain_procs(self) -> None:
        born = lstart(0.0)
        later = lstart(5.0)
        reused = lstart(60.0)
        table: dict[int, tuple[int, str, int, int, float]] = {
            100: (50, born, UID, 2048, 1.0),
            501: (1, later, UID, 10_000, 1.0),
        }
        # Plain process: must never invoke helper_snapshot
        with mock.patch.object(sf_process, "commands", return_value={501: "/usr/bin/python3 worker.py"}), \
                mock.patch.object(sf_process, "helper_snapshot") as full_scan:
            self.assertEqual(set(), sf_process.attributed_browser_helpers([501], table, 100, SINCE))
            full_scan.assert_not_called()

        # Reused PID (start time changed between usage_table and helper_snapshot): must NOT attribute
        ctx = sf_process.ProcessContext(
            parents={100: 50, 101: 100, 501: 1},
            uids={100: UID, 101: UID, 501: UID},
            starts={100: SINCE, 101: SINCE + 5, 501: SINCE + 60},
            raw_starts={100: born, 101: later, 501: reused},
            commands={100: "node playwright", 101: WEBKIT_DRIVER_2370, 501: WEBKIT_WEBCONTENT_2370},
        )
        with mock.patch.object(sf_process, "commands", return_value={501: WEBKIT_WEBCONTENT_2370}), \
                mock.patch.object(sf_process, "helper_snapshot", return_value=ctx):
            self.assertEqual(set(), sf_process.attributed_browser_helpers([501], table, 100, SINCE))


class MembershipStatusProcsSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="slotfather-member-xpc-")
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        (self.state / "queue").mkdir(parents=True)
        self.born = lstart(0.0)
        self.later = lstart(5.0)
        self.lease_path = self.state / "queue" / f"{1:010d}-100.json"
        self.lease_path.write_text(json.dumps({
            "pid": 100, "start": self.born, "count": 2, "state": "held", "label": "e2e",
            "since": BORN_UTC, "granted": BORN_UTC, "cmd": "npx playwright test",
        }))

    def test_lease_membership_attributes_hidden_env_xpc_helper_and_clears_unknown(self) -> None:
        parents = {100: 1, 101: 100, 501: 1, 601: 1}
        commands = {
            100: "python3 slotfather run --label e2e -- test",
            101: WEBKIT_DRIVER_2370,
            501: WEBKIT_WEBCONTENT_2370,
            601: SYSTEM_WEBKIT_CONTENT,
        }
        leases = [sf_process.Lease(str(self.lease_path), 100)]

        def hidden_markers(pids: Any, _paths: Any) -> tuple[dict[int, set[str]], list[int]]:
            return {}, list(pids)  # simulates unreadable Apple/XPC environments

        found = sf_process.lease_membership(
            parents, leases, [501, 601], markers=hidden_markers, detached_only=True,
            commands=commands, uids={pid: UID for pid in parents},
            starts={pid: SINCE + 5 for pid in parents}, lease_since=[SINCE],
        )
        self.assertEqual([(0, "helper")], found.leases[501])
        self.assertEqual([], found.leases[601])
        self.assertEqual([601], found.unknown, "resolved XPC helper leaves unknown; unresolved system helper stays unknown")

    def test_status_procs_and_sweep_agree_on_detached_webkit_xpc_helper(self) -> None:
        me = os.getpid()
        rows = [
            (me, 1, UID, self.born, "python3 -m unittest"),
            (100, 1, UID, self.born, "python3 slotfather run --label e2e -- npx playwright test"),
            (101, 100, UID, self.later, WEBKIT_DRIVER_2370),
            (501, 1, UID, self.later, WEBKIT_WEBCONTENT_2370),
            (601, 1, UID, self.later, SYSTEM_WEBKIT_CONTENT),
        ]

        def fake_ps(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if sf_process.IDENTITY in argv:
                text = "".join(f"{pid} {ppid} S {start}\n" for pid, ppid, _, start, _ in rows)
                return subprocess.CompletedProcess(argv, 0, text, "")
            if sf_process.MEMBERS in argv:
                text = "".join(f"{pid} {ppid} {uid} ?? S {start} {cmd}\n" for pid, ppid, uid, start, cmd in rows)
                return subprocess.CompletedProcess(argv, 0, text, "")
            if sf_process.HELPER_SNAPSHOT in argv:
                text = "".join(f"{pid} {ppid} {uid} {start} {cmd}\n" for pid, ppid, uid, start, cmd in rows)
                return subprocess.CompletedProcess(argv, 0, text, "")
            if sf_process.ARGUMENTS in argv and "-wwE" not in argv:
                wanted = {int(p) for p in argv[argv.index("-p") + 1].split(",")}
                text = "".join(f"{pid} {cmd}\n" for pid, _, _, _, cmd in rows if pid in wanted)
                return subprocess.CompletedProcess(argv, 0, text, "")
            if "-wwE" in argv:
                wanted = {int(p) for p in argv[argv.index("-p") + 1].split(",")}
                text = "".join(f"{pid} {cmd}\n" for pid, _, _, _, cmd in rows if pid in wanted)
                return subprocess.CompletedProcess(argv, 0, text, "")
            if sf_process.STARTS in argv:
                wanted = {int(p) for p in argv[argv.index("-p") + 1].split(",")}
                text = "".join(f"{pid} S {start}\n" for pid, _, _, start, _ in rows if pid in wanted)
                return subprocess.CompletedProcess(argv, 0, text, "")
            raise AssertionError(f"unexpected ps argv: {argv}")

        with mock.patch.dict(os.environ, {"SLOTFATHER_DIR": str(self.state), "SLOTFATHER_LEGACY_DIRS": "",
                                          "SLOTFATHER_CORES": "12", "SLOTFATHER_MEMORY_GIB": "64"}), \
                mock.patch("sys.platform", "darwin"), \
                mock.patch("slotfather.macos_process.basic_snapshot", side_effect=OSError("no libproc")), \
                mock.patch("slotfather.macos_process.snapshot", side_effect=OSError("no libproc")), \
                mock.patch("slotfather.macos_process.environment", return_value=None), \
                mock.patch("subprocess.run", side_effect=fake_ps):
            status_hit = sf_query.status_json(self.state, pid=501)
            status_miss = sf_query.status_json(self.state, pid=601)
            procs_all = sf_procs.query(directory=self.state)
            procs_hit = sf_procs.query(pid=501, directory=self.state)
            parents_map, found_leases = sf_sweep.leases(self.state)
            sweep_procs = {
                pid: sweep_plan.Proc(pid, ppid, uid, 1024, "", start, SINCE + 5, cmd)
                for pid, ppid, uid, start, cmd in rows
            }
            _, carriers = sf_sweep.protection(parents_map, found_leases, procs=sweep_procs)
            assert carriers is not None
            carried = carriers([501, 601])

        self.assertEqual([("0000000001-100", "helper")], [(e["id"], e["via"]) for e in status_hit["entries"]])
        self.assertNotIn("lease_unknown", status_hit)
        self.assertEqual([], status_miss["entries"])
        self.assertEqual([601], status_miss["lease_unknown"])

        by_pid_all = {p["pid"]: p for p in procs_all["processes"]}
        self.assertEqual([("0000000001-100", "helper")], [(item["id"], item["via"]) for item in by_pid_all[501]["leases"]])
        self.assertNotIn(501, procs_all["lease_unknown"])
        self.assertEqual([], by_pid_all[601]["leases"])
        self.assertIn(601, procs_all["lease_unknown"])

        self.assertEqual([("0000000001-100", "helper")],
                         [(item["id"], item["via"]) for item in procs_hit["processes"][0]["leases"]])
        self.assertEqual([], procs_hit["lease_unknown"])

        self.assertEqual({501: "e2e", 601: None}, dict(carried))


if __name__ == "__main__":
    unittest.main()
