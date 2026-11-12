"""`slotfather version` and the changelog the installer ships beside the package (capabilities name it for Godfather)."""
from __future__ import annotations

import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import importlib.util
import unittest
from typing import Any

from test_slotfather import ROOT, SlotCase


class VersionTests(SlotCase):
    def test_version_prints_the_semver_and_the_commit(self) -> None:
        result = self.tool("version")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertRegex(result.stdout, r"^slotfather \d+\.\d+\.\d+ \([0-9a-f]{7}(\+dirty)?\) source [0-9a-f]{64}$")

    def test_install_ships_the_changelog(self) -> None:
        spec = importlib.util.spec_from_file_location("installer_for_test", ROOT / "install.py")
        assert spec and spec.loader
        installer: Any = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        self.assertIn(ROOT / "CHANGELOG.md", installer.source_files(ROOT))
        home = self.tmp / "home"
        release = installer.install_package(home, ROOT)
        self.assertEqual((ROOT / "CHANGELOG.md").read_bytes(), (release / "slotfather" / "CHANGELOG.md").read_bytes())


if __name__ == "__main__":
    unittest.main()
