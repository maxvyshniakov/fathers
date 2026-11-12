#!/usr/bin/env python3
"""One-off: port Slotfather's dashboard tests to Godfather (slice 1). Kept for the record of what moved and how.

    python3 tools/port_tests.py ~/slotfather/tests NAME...

Each tests/NAME.py is rewritten against tests/view.py (one namespace over Godfather's modules) and written to
Godfather's tests/; classes built on SlotCase (a real Slotfather queue) are left out and listed, since Godfather
reads Slotfather only through its CLI: their flows are covered again against the fake Slotfather.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPLACEMENTS = [
    (re.compile(r"^import sys as _path_sys\nfrom pathlib import Path as _Path\n_path_sys\.path\.insert\(0, str\(_Path\(__file__\)\.resolve\(\)\.parents\[1\] / \"src\"\)\)\n", re.M),
     "import helpers  # noqa: F401 - src/ on sys.path\nimport view as _view\n"),
    (re.compile(r"^sys\.path\.insert\(0, str\((?:Path\(__file__\)\.resolve\(\)\.parents\[1\]|ROOT) / ['\"]src['\"]\)\)\n", re.M),
     "import helpers  # noqa: F401 - src/ on sys.path\nimport view as _view\n"),
    (re.compile(r"importlib\.reload\(importlib\.import_module\([\"']slotfather\.dashboard[\"']\)\)"), "_view.fresh()"),
    (re.compile(r"importlib\.import_module\([\"']slotfather\.dashboard[\"']\)"), "_view.current()"),
    (re.compile(r"^from slotfather import theme$", re.M), "from godfather import theme"),
    (re.compile(r"^from slotfather import keys as sf_keys$", re.M), "from godfather import keys as sf_keys"),
    (re.compile(r"^from slotfather import version$", re.M), "from godfather import version"),
    (re.compile(r"^from slotfather import dashboard_settings as sf_settings$", re.M),
     "from godfather.views import settings as sf_settings"),
    (re.compile(r"slotfather\.lane_context\.fetch"), "godfather.adapters.lanefather.fetch"),
]


def port(source: Path) -> tuple[str, list[str]]:
    text = source.read_text()
    tree = ast.parse(text)
    lines = text.split("\n")
    dropped: list[str] = []
    for node in sorted(tree.body, key=lambda node: -node.lineno):
        if isinstance(node, ast.ClassDef) and any(isinstance(base, ast.Name) and base.id == "SlotCase"
                                                  for base in node.bases):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
            del lines[start:node.end_lineno]
            dropped.append(node.name)
    text = "\n".join(lines)
    for pattern, replacement in REPLACEMENTS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"^from test_slotfather import .*\n", "", text, flags=re.M)
    return text, dropped


def main(argv: list[str]) -> int:
    folder = Path(argv[0]).expanduser()
    for name in argv[1:]:
        text, dropped = port(folder / f"{name}.py")
        (ROOT / "tests" / f"{name}.py").write_text(text)
        print(f"{name}: left out {', '.join(dropped) or 'nothing'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
