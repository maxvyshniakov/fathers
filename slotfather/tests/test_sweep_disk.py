"""Tests for machine-wide disk hygiene in `slotfather sweep` (fathers#6).

Covers Part A (`docker-anonymous`, `docker-dangling-images`, `docker-superseded`, named volumes and unlisted
images) and Part B (`claude-scratch` including process and Junkfather sparing, `package-caches`, `low-disk`) using
fake CLIs (`docker`, `uv`, `brew`, `du`, `junkfather`, notifier), report/JSON output, `--live` acting only on
candidates, and `sweep.json` byte preservation when no disk setting is changed.
"""
from __future__ import annotations

import sys as _path_sys
from pathlib import Path as _Path

_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import textwrap
from typing import Any
import unittest
from unittest import mock

from slotfather import settings as sf_settings
from slotfather import sweep as sf_sweep
from slotfather import sweep_disk as sf_disk
from slotfather import sweep_plan as sf_plan

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
UID = 501


def write_cli(bin_dir: Path, name: str, python_body: str) -> Path:
    script = bin_dir / f"_{name}_impl.py"
    script.write_text(textwrap.dedent(python_body), encoding="utf-8")
    wrapper = bin_dir / name
    wrapper.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


class FakeCLICase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="slotfather-sweep-disk-")
        self.addCleanup(self.tmp_obj.cleanup)
        self.tmp = Path(os.path.realpath(self.tmp_obj.name))
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.state = self.tmp / "state"
        self.calls_file = self.tmp / "cli-calls.jsonl"
        self.du_sizes: dict[str, int] = {}
        self.du_sizes_file = self.tmp / "du-sizes.json"
        self._sync_du_sizes()
        self._install_fake_du()

        base_env = {k: v for k, v in os.environ.items() if not k.startswith("SLOTFATHER_")}
        base_env.update({
            "SLOTFATHER_DIR": str(self.state),
            "SLOTFATHER_LEGACY_DIRS": "",
            "SLOTFATHER_SWEEP_SCRATCH_ROOT": str(self.tmp / "claude-scratch"),
            "SLOTFATHER_SWEEP_CACHE_HOME": str(self.tmp / "cache-home"),
            "SLOTFATHER_SWEEP_DISK_FREE_GB": "250",
            "SLOTFATHER_SWEEP_CONSUMER_PATHS": "",
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "FAKE_CALLS": str(self.calls_file),
            "FAKE_DU_SIZES": str(self.du_sizes_file),
        })
        for var in ("UV_CACHE_DIR", "HOMEBREW_CACHE", "NPM_CONFIG_CACHE", "BUN_INSTALL_CACHE_DIR"):
            base_env.pop(var, None)
        self.env_patch = mock.patch.dict(os.environ, base_env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _sync_du_sizes(self) -> None:
        self.du_sizes_file.write_text(json.dumps(self.du_sizes), encoding="utf-8")

    def set_du_kb(self, path: Path, kb: int) -> None:
        self.du_sizes[str(path)] = kb
        self.du_sizes[os.path.realpath(str(path))] = kb
        self._sync_du_sizes()

    def _install_fake_du(self) -> None:
        write_cli(self.bin_dir, "du", """
            import json, os, sys
            with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as log:
                log.write(json.dumps(["du", *sys.argv[1:]]) + "\\n")
            target = sys.argv[-1]
            sizes = json.loads(open(os.environ["FAKE_DU_SIZES"], encoding="utf-8").read())
            kb = sizes.get(target, sizes.get(os.path.realpath(target), 1024))
            print(f"{kb}\\t{target}")
        """)

    def calls(self, tool: str | None = None) -> list[list[str]]:
        if not self.calls_file.exists():
            return []
        rows = [cast_row(json.loads(line)) for line in self.calls_file.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if tool is None or r[0] == tool]

    def empty_snap(self, procs: dict[int, sf_plan.Proc] | None = None,
                   cwds: dict[int, sf_plan.Cwd] | None = None) -> sf_plan.Snapshot:
        return sf_plan.Snapshot(procs or {}, cwds or {}, moment=NOW, uid=UID, me=(99999,))


def cast_row(raw: Any) -> list[str]:
    return [str(x) for x in raw]


class DockerHygieneTests(FakeCLICase):
    def install_fake_docker(self, state_data: dict[str, Any]) -> None:
        fixture = self.tmp / "docker-fixture.json"
        fixture.write_text(json.dumps(state_data), encoding="utf-8")
        os.environ["FAKE_DOCKER_FIXTURE"] = str(fixture)
        write_cli(self.bin_dir, "docker", """
            import json, os, sys
            args = sys.argv[1:]
            with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as log:
                log.write(json.dumps(["docker", *args]) + "\\n")
            data = json.loads(open(os.environ["FAKE_DOCKER_FIXTURE"], encoding="utf-8").read())
            if args[:2] == ["ps", "-aq"]:
                for c in data.get("containers", []):
                    print(c["Id"])
            elif args[:1] == ["inspect"]:
                wanted = set(args[1:])
                print(json.dumps([c for c in data.get("containers", []) if c["Id"] in wanted]))
            elif args[:2] == ["volume", "ls"]:
                for v in data.get("volumes", []):
                    print(v["Name"])
            elif args[:2] == ["volume", "inspect"]:
                wanted = set(args[2:])
                print(json.dumps([v for v in data.get("volumes", []) if v["Name"] in wanted]))
            elif args[:2] == ["volume", "rm"]:
                sys.exit(0)
            elif args[:2] == ["image", "ls"] and "dangling=true" in " ".join(args):
                for img in data.get("dangling_images", []):
                    print(img["Id"])
            elif args[:2] == ["image", "ls"]:
                for img in data.get("all_images", []):
                    print(img["Id"])
            elif args[:2] == ["image", "inspect"]:
                wanted = set(args[2:])
                pool = {img["Id"]: img for img in [*data.get("dangling_images", []), *data.get("all_images", [])]}
                print(json.dumps([pool[i] for i in args[2:] if i in pool]))
            elif args[:2] == ["image", "rm"]:
                sys.exit(0)
            else:
                sys.exit(1)
        """)

    def test_docker_anonymous_dangling_and_superseded_report_and_live_removes_only_candidates(self) -> None:
        self.install_fake_docker({
            "containers": [{
                "Id": "cont-live-1",
                "Name": "/journey-runner",
                "Image": "sha256:danglingused0001",
                "Config": {"Image": "straylight-host-journeys:t1"},
                "Mounts": [{"Name": "anon-mounted"}],
            }],
            "volumes": [
                # Kept: anonymous but young (30 min < 1 h)
                {"Name": "anon-young", "CreatedAt": "2027-01-15 07:30:00 +0000 UTC",
                 "Labels": {"com.docker.volume.anonymous": ""}, "UsageData": {"Size": 100_000_000}},
                # Kept: mounted by container
                {"Name": "anon-mounted", "CreatedAt": "2027-01-14 00:00:00 +0000 UTC",
                 "Labels": {"com.docker.volume.anonymous": ""}, "UsageData": {"Size": 100_000_000}},
                # Reported only, never removed: named volume (no anonymous label)
                {"Name": "postgres-data", "CreatedAt": "2027-01-10 00:00:00 +0000 UTC",
                 "Labels": {"com.docker.compose.volume": "pg"}, "UsageData": {"Size": 500_000_000}},
                # Removed: anonymous dangling volume > 1 h old
                {"Name": "anon-old-deadbeef", "CreatedAt": "2027-01-15 05:00:00 +0000 UTC",
                 "Labels": {"com.docker.volume.anonymous": ""}, "UsageData": {"Size": 400_000_000}},
            ],
            "dangling_images": [
                # Kept: <none> image < 24 h old (2 h)
                {"Id": "sha256:danglingyoung001", "RepoTags": ["<none>:<none>"],
                 "Created": "2027-01-15T06:00:00Z", "Size": 200_000_000},
                # Kept: <none> image > 24 h old but used by container
                {"Id": "sha256:danglingused0001", "RepoTags": [],
                 "Created": "2027-01-10T00:00:00Z", "Size": 250_000_000},
                # Removed: <none> image > 24 h old, unused
                {"Id": "sha256:danglingold00001", "RepoTags": ["<none>:<none>"],
                 "Created": "2027-01-13T00:00:00Z", "Size": 600_000_000},
            ],
            "all_images": [
                # Listed family `straylight-host-journeys` (keep newest 2: t4, t3; t2 removed; t1 spared by container)
                {"Id": "sha256:shj4", "RepoTags": ["straylight-host-journeys:t4"],
                 "Created": "2027-01-15T07:00:00Z", "Size": 300_000_000},
                {"Id": "sha256:shj3", "RepoTags": ["straylight-host-journeys:t3"],
                 "Created": "2027-01-14T07:00:00Z", "Size": 300_000_000},
                {"Id": "sha256:shj2", "RepoTags": ["straylight-host-journeys:t2"],
                 "Created": "2027-01-13T07:00:00Z", "Size": 350_000_000},
                {"Id": "sha256:shj1", "RepoTags": ["straylight-host-journeys:t1"],
                 "Created": "2027-01-12T07:00:00Z", "Size": 350_000_000},
                # Listed wildcard family `ghcr.io/home-assistant/*` (keep newest 2: 2026.11, 2026.10; 2026.9 removed)
                {"Id": "sha256:ha11", "RepoTags": ["ghcr.io/home-assistant/core:2026.11"],
                 "Created": "2027-01-15T01:00:00Z", "Size": 100_000_000},
                {"Id": "sha256:ha10", "RepoTags": ["ghcr.io/home-assistant/core:2026.10"],
                 "Created": "2027-01-10T01:00:00Z", "Size": 100_000_000},
                {"Id": "sha256:ha09", "RepoTags": ["ghcr.io/home-assistant/core:2026.9"],
                 "Created": "2027-01-01T01:00:00Z", "Size": 120_000_000},
                # Unlisted family: reported only, never removed even with >2 tags
                {"Id": "sha256:pg16", "RepoTags": ["postgres:16", "postgres:15", "postgres:14"],
                 "Created": "2026-12-01T00:00:00Z", "Size": 200_000_000},
            ],
        })

        settings = sf_settings.sweep_defaults()
        settings["disk_classes"] = list(sf_disk.DOCKER_CLASSES)
        settings["docker_superseded"] = ["straylight-host-journeys", "ghcr.io/home-assistant/*"]
        settings["docker_superseded_keep"] = 2.0

        dry = sf_disk.sweep_disk(self.empty_snap(), settings, live=False)
        self.assertTrue(dry["docker"]["available"])
        by_name = {item["name"]: item for item in dry["items"]}
        self.assertNotIn("anon-young", by_name)
        self.assertNotIn("anon-mounted", by_name)
        self.assertNotIn("danglingyoun", by_name)
        self.assertNotIn("danglingused", by_name)
        self.assertEqual("report", by_name["postgres-data"]["action"])
        self.assertEqual("report", by_name["postgres (3 tag(s))"]["action"])
        self.assertEqual("spared", by_name["straylight-host-journeys:t1"]["action"])
        self.assertEqual("would-remove", by_name["anon-old-deadbeef"]["action"])
        self.assertEqual("would-remove", by_name["danglingold0"]["action"])
        self.assertEqual("would-remove", by_name["straylight-host-journeys:t2"]["action"])
        self.assertEqual("would-remove", by_name["ghcr.io/home-assistant/core:2026.9"]["action"])
        self.assertEqual([], [c for c in self.calls("docker") if "rm" in c], "dry run removes nothing")

        live = sf_disk.sweep_disk(self.empty_snap(), settings, live=True)
        rm_calls = [c[1:] for c in self.calls("docker") if "rm" in c]
        self.assertEqual([
            ["volume", "rm", "anon-old-deadbeef"],
            ["image", "rm", "sha256:danglingold00001"],
            ["image", "rm", "ghcr.io/home-assistant/core:2026.9"],
            ["image", "rm", "straylight-host-journeys:t2"],
        ], rm_calls)
        live_by_name = {item["name"]: item for item in live["items"]}
        self.assertEqual("report", live_by_name["postgres-data"]["action"])
        self.assertEqual("report", live_by_name["postgres (3 tag(s))"]["action"])
        self.assertEqual("spared", live_by_name["straylight-host-journeys:t1"]["action"])
        self.assertEqual(400_000_000 + 600_000_000 + 350_000_000 + 120_000_000, live["freed_bytes"])


class ScratchCachesAndLowDiskTests(FakeCLICase):
    def make_scratch_session(self, project: str, session_id: str, age_seconds: float, size_kb: int) -> Path:
        root = Path(os.environ["SLOTFATHER_SWEEP_SCRATCH_ROOT"])
        session = root / project / session_id
        session.mkdir(parents=True, exist_ok=True)
        marker = session / "artifact.bin"
        marker.write_text("x", encoding="utf-8")
        mtime = NOW - age_seconds
        os.utime(marker, (mtime, mtime))
        os.utime(session, (mtime, mtime))
        self.set_du_kb(session, size_kb)
        return session

    def test_claude_scratch_removes_idle_over_2gb_and_spares_young_small_process_and_spawnfather_entries(self) -> None:
        s_remove = self.make_scratch_session("proj-a", "sess-remove", age_seconds=30 * 3600, size_kb=2_500_000)
        s_young = self.make_scratch_session("proj-a", "sess-young", age_seconds=4 * 3600, size_kb=3_000_000)
        s_small = self.make_scratch_session("proj-a", "sess-small", age_seconds=48 * 3600, size_kb=500_000)
        s_proc = self.make_scratch_session("proj-b", "sess-proc", age_seconds=48 * 3600, size_kb=3_000_000)
        s_sf = self.make_scratch_session("proj-b", "sess-sf", age_seconds=48 * 3600, size_kb=3_000_000)

        sf_payload = {"v": 1, "entries": [
            {"id": "sf-firefox-42", "state": "running", "cwd": str(s_sf / "sub")},
            {"id": "sf-reaped-99", "state": "reaped", "cwd": str(s_remove)},
        ]}
        write_cli(self.bin_dir, "spawnfather", f"""
            import json, os, sys
            with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as log:
                log.write(json.dumps(["spawnfather", *sys.argv[1:]]) + "\\n")
            print({json.dumps(json.dumps(sf_payload))})
        """)

        active_proc = sf_plan.Proc(4242, 1, UID, 1024, "", "Wed Nov 4 01:00:00 2026", NOW - 3600, "node worker.js")
        snap = self.empty_snap(
            procs={4242: active_proc},
            cwds={4242: sf_plan.Cwd(str(s_proc), False)},
        )
        settings = sf_settings.sweep_defaults()
        settings["disk_classes"] = ["claude-scratch"]

        dry = sf_disk.sweep_disk(snap, settings, live=False)
        by_path = {item["path"]: item for item in dry["items"]}
        self.assertNotIn(str(s_young), by_path)
        self.assertNotIn(str(s_small), by_path)
        self.assertEqual("spared", by_path[str(s_proc)]["action"])
        self.assertIn("pid 4242", by_path[str(s_proc)]["reason"])
        self.assertEqual("spared", by_path[str(s_sf)]["action"])
        self.assertEqual("sf-firefox-42", by_path[str(s_sf)]["spawnfather_entry"])
        self.assertEqual("would-remove", by_path[str(s_remove)]["action"])
        self.assertTrue(s_remove.exists())

        live = sf_disk.sweep_disk(snap, settings, live=True)
        live_by_path = {item["path"]: item for item in live["items"]}
        self.assertEqual("removed", live_by_path[str(s_remove)]["action"])
        self.assertFalse(s_remove.exists(), "eligible scratchpad session removed")
        for kept in (s_young, s_small, s_proc, s_sf):
            self.assertTrue(kept.exists(), f"{kept} must remain untouched")

    def test_package_caches_report_above_threshold_and_prune_uv_and_brew_below_threshold(self) -> None:
        cache_home = Path(os.environ["SLOTFATHER_SWEEP_CACHE_HOME"])
        uv_dir = cache_home / "uv-custom"
        brew_dir = cache_home / "brew-custom"
        npm_dir = cache_home / ".npm"
        bun_dir = cache_home / ".bun/install/cache"
        for directory, kb in ((uv_dir, 4_000_000), (brew_dir, 3_000_000), (npm_dir, 1_500_000), (bun_dir, 800_000)):
            directory.mkdir(parents=True, exist_ok=True)
            self.set_du_kb(directory, kb)

        write_cli(self.bin_dir, "uv", f"""
            import json, os, sys
            args = sys.argv[1:]
            with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as log:
                log.write(json.dumps(["uv", *args]) + "\\n")
            if args == ["cache", "dir"]:
                print({str(uv_dir)!r})
            elif args == ["cache", "prune"]:
                sizes = json.loads(open(os.environ["FAKE_DU_SIZES"], encoding="utf-8").read())
                sizes[{str(uv_dir)!r}] = 1_000_000
                sizes[os.path.realpath({str(uv_dir)!r})] = 1_000_000
                open(os.environ["FAKE_DU_SIZES"], "w", encoding="utf-8").write(json.dumps(sizes))
            else:
                sys.exit(1)
        """)
        write_cli(self.bin_dir, "brew", f"""
            import json, os, sys
            args = sys.argv[1:]
            with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as log:
                log.write(json.dumps(["brew", *args]) + "\\n")
            if args == ["--cache"]:
                print({str(brew_dir)!r})
            elif args == ["cleanup"]:
                sizes = json.loads(open(os.environ["FAKE_DU_SIZES"], encoding="utf-8").read())
                sizes[{str(brew_dir)!r}] = 500_000
                sizes[os.path.realpath({str(brew_dir)!r})] = 500_000
                open(os.environ["FAKE_DU_SIZES"], "w", encoding="utf-8").write(json.dumps(sizes))
            else:
                sys.exit(1)
        """)

        settings = sf_settings.sweep_defaults()
        settings["disk_classes"] = ["package-caches"]
        settings["cache_free_gb"] = 100.0

        # Kept case: 150 GB free >= 100 GB threshold -> even --live only reports, never prunes.
        os.environ["SLOTFATHER_SWEEP_DISK_FREE_GB"] = "150"
        kept = sf_disk.sweep_disk(self.empty_snap(), settings, live=True)
        self.assertEqual({"uv": "report", "homebrew": "report", "npm": "report", "bun": "report"},
                         {item["kind"]: item["action"] for item in kept["items"]})
        self.assertNotIn(["uv", "cache", "prune"], self.calls("uv"))
        self.assertNotIn(["brew", "cleanup"], self.calls("brew"))

        # Low free space dry run: reports would-prune for uv and homebrew, report for npm and bun.
        os.environ["SLOTFATHER_SWEEP_DISK_FREE_GB"] = "60"
        dry = sf_disk.sweep_disk(self.empty_snap(), settings, live=False)
        self.assertEqual({"uv": "would-prune", "homebrew": "would-prune", "npm": "report", "bun": "report"},
                         {item["kind"]: item["action"] for item in dry["items"]})
        self.assertNotIn(["uv", "cache", "prune"], self.calls("uv"))

        # Low free space live run: prunes uv and brew, leaves npm and bun reported only.
        live = sf_disk.sweep_disk(self.empty_snap(), settings, live=True)
        self.assertEqual({"uv": "pruned", "homebrew": "pruned", "npm": "report", "bun": "report"},
                         {item["kind"]: item["action"] for item in live["items"]})
        self.assertIn(["uv", "cache", "prune"], self.calls("uv"))
        self.assertIn(["brew", "cleanup"], self.calls("brew"))
        self.assertEqual((3_000_000 + 2_500_000) * 1024, live["freed_bytes"])

    def test_low_disk_ranks_top_consumers_and_notifies_only_when_below_threshold_and_live(self) -> None:
        c1, c2 = self.tmp / "wt-big", self.tmp / "wt-small"
        c1.mkdir()
        c2.mkdir()
        self.set_du_kb(c1, 12_000_000)
        self.set_du_kb(c2, 3_000_000)
        notifier = write_cli(self.bin_dir, "fake-notify", """
            import json, os, sys
            with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as log:
                log.write(json.dumps(["notify", *sys.argv[1:]]) + "\\n")
        """)
        os.environ["SLOTFATHER_SWEEP_CONSUMER_PATHS"] = f"{c2}:{c1}"
        os.environ["SLOTFATHER_SWEEP_NOTIFY"] = str(notifier)

        settings = sf_settings.sweep_defaults()
        settings["disk_classes"] = ["low-disk"]
        settings["disk_free_gb"] = 100.0

        # Kept case: 140 GB free >= 100 GB -> no item, no notification.
        os.environ["SLOTFATHER_SWEEP_DISK_FREE_GB"] = "140"
        calm = sf_disk.sweep_disk(self.empty_snap(), settings, live=True)
        self.assertEqual([], calm["items"])
        self.assertFalse(calm["low_disk"]["low"])
        self.assertEqual([], self.calls("notify"))

        # Below threshold dry run -> would-notify with sorted top consumers, no notifier call.
        os.environ["SLOTFATHER_SWEEP_DISK_FREE_GB"] = "72"
        dry = sf_disk.sweep_disk(self.empty_snap(), settings, live=False)
        self.assertEqual(1, len(dry["items"]))
        self.assertEqual("would-notify", dry["items"][0]["action"])
        self.assertEqual([str(c1), str(c2)], [entry["path"] for entry in dry["items"][0]["top"]])
        self.assertEqual([], self.calls("notify"))

        # Below threshold live -> notified, text report renders consumers, quiet prints notice.
        live = sf_disk.sweep_disk(self.empty_snap(), settings, live=True)
        self.assertEqual("notified", live["items"][0]["action"])
        self.assertTrue(live["low_disk"]["notified"])
        self.assertEqual(1, len(self.calls("notify")))
        self.assertIn("72 GB free (budget 100 GB). Top: wt-big 12.3 GB, wt-small 3.1 GB", self.calls("notify")[0][1])

        report_doc: dict[str, Any] = {
            "at": NOW, "live": True, "dev_servers": False, "only": None, "notes": [],
            "found": {"removed": 0, "orphan": 0, "dev-server": 0, "container": 0},
            "items": [], "containers": {"available": None, "items": []}, "memory": {},
            "disk": live, "would_stop": 0, "spared": 0,
            "stopped": {"removed": 0, "orphan": 0, "dev-server": 0}, "failed": 0, "freed_mb": 0.0,
        }
        rendered = sf_sweep.text(report_doc)
        self.assertIn("low-disk", rendered)
        self.assertIn("notified", rendered)
        self.assertIn(str(c1), rendered)
        quiet = sf_sweep.text(report_doc, quiet=True)
        self.assertIn("slotfather sweep: notified low-disk 72 GB free", quiet)


class SweepSettingsBytesTests(FakeCLICase):
    def test_sweep_json_keeps_exact_bytes_when_no_disk_setting_is_changed(self) -> None:
        expected_default_classes = (
            '{"classes": ["removed", "orphan", "dev-server", "container", "memory"], '
            '"removed_age": 600.0, "orphan_age": 10800.0, "dev_server_age": 28800.0, '
            '"session_removed_age": 21600.0, "grace": 10.0, "container_minutes": 30.0, '
            '"container_restarts": 3.0, "swap_free_mb": 1024.0, "swap_free_pct": 20.0, '
            '"memory_top": 8.0}\n'
        )
        self.assertEqual(0, sf_sweep.config_command(["orphan_age", "3h"], self.state))
        self.assertEqual(expected_default_classes, (self.state / "sweep.json").read_text(encoding="utf-8"))

        # Configuring a disk setting writes only the changed disk setting alongside the base keys.
        self.assertEqual(
            0,
            sf_sweep.config_command(
                ["docker_superseded", "straylight-host-journeys,ghcr.io/home-assistant/*", "disk_free_gb", "80"],
                self.state,
            ),
        )
        saved = json.loads((self.state / "sweep.json").read_text(encoding="utf-8"))
        self.assertEqual(["straylight-host-journeys", "ghcr.io/home-assistant/*"], saved["docker_superseded"])
        self.assertEqual(80.0, saved["disk_free_gb"])
        self.assertNotIn("docker_anonymous_age", saved, "unchanged disk defaults stay omitted")


if __name__ == "__main__":
    unittest.main()
