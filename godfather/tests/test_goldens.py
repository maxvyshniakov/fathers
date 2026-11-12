"""Godfather's screens of Slotfather's recorded scene, through fixture mode and the Slotfather adapter, match the
goldens in tests/goldens/ exactly: text and the escape sequences in Seti and Pro Light (tools/goldens.py writes
them; run it after a deliberate visible change and review the diff)."""
from __future__ import annotations

import importlib.util
import unittest

import view as _view
from helpers import ROOT


def goldens_tool():
    spec = importlib.util.spec_from_file_location("godfather_goldens", ROOT / "tools" / "goldens.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GoldenTests(unittest.TestCase):
    def tearDown(self) -> None:
        _view.fresh()

    def test_every_screen_matches_its_golden(self) -> None:
        tool = goldens_tool()
        found = tool.goldens()
        self.assertEqual(21, len(found))
        for name, text in found.items():
            with self.subTest(name):
                self.assertEqual((ROOT / "tests" / "goldens" / name).read_text(), text,
                                 f"{name} differs; python3 tools/goldens.py rewrites it")


if __name__ == "__main__":
    unittest.main()
