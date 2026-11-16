"""The package's layers (fathers#141): data → layout → views → app. A module imports only from its own layer or one
below it, so the reducer (state, modes) reads no view, and the views read the state they draw; one scroll clamp
(layout.clamp_scroll) and one hint lookup (text.hint_at)."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "godfather"
LAYERS = {
    # what the tools say and how it is read: no geometry, no drawing
    "data": ("keys", "theme", "version", "eventcheck", "sources", "events", "news", "entries", "adapters", "text",
             "actions", "model"),
    # where things go and the one state and its reducer: no canvas, no terminal
    "layout": ("layout", "overlay_text", "settings_model", "state", "pages", "modes", "reducer"),
    # drawing: the canvas and terminal (tui), the shared widgets, the screens
    "views": ("tui", "widgets", "views"),
    "app": ("app", "__main__", "__init__"),
}
ORDER = list(LAYERS)


def module_of(path: Path) -> str:
    return ".".join(path.relative_to(PACKAGE).with_suffix("").parts)


def layer_of(module: str) -> str:
    head = module.split(".")[0]
    return next(layer for layer, names in LAYERS.items() if head in names)


def imports(path: Path) -> set[str]:
    """Every godfather module `path` imports, at its top or inside a function."""
    module = module_of(path)
    package = module.split(".")[:-1] if path.name != "__init__.py" else module.split(".")
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = package[:len(package) - (node.level - 1)] if node.level > 1 else package
            target = ".".join([*base, *(node.module.split(".") if node.module else [])])
            if node.module:
                found.add(target)
            else:
                found.update(".".join([*base, alias.name]) for alias in node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names]
            found.update(name[len("godfather."):] for name in names if name.startswith("godfather."))
    return {name for name in found if name}


class LayerTests(unittest.TestCase):
    def test_every_module_imports_its_own_layer_or_below(self) -> None:
        for path in sorted(PACKAGE.rglob("*.py")):
            module = module_of(path)
            own = ORDER.index(layer_of(module))
            for target in imports(path):
                with self.subTest(module=module, imports=target):
                    self.assertLessEqual(ORDER.index(layer_of(target)), own,
                                         f"{module} ({ORDER[own]}) imports {target} ({layer_of(target)})")

    def test_no_function_imports_a_module_of_its_own_package_to_break_a_cycle(self) -> None:
        """state.reduce imported modes inside a function (fathers#141): reducer.py imports both at its top."""
        for name in ("state", "modes", "reducer"):
            tree = ast.parse((PACKAGE / f"{name}.py").read_text())
            inner = [node for function in ast.walk(tree) if isinstance(function, ast.FunctionDef)
                     for node in ast.walk(function) if isinstance(node, ast.ImportFrom) and node.level]
            self.assertEqual([], [ast.unparse(node) for node in inner], name)

    def test_the_reducer_imports_no_view(self) -> None:
        for name in ("state", "modes", "reducer", "settings_model"):
            with self.subTest(name):
                self.assertFalse({target for target in imports(PACKAGE / f"{name}.py") if target.startswith("views")})

    def test_one_scroll_clamp_and_one_hint_lookup(self) -> None:
        """No scroll kept in range by hand: no `max(0, min(` nor a bottom offset worked out (`total - visible`)
        outside layout.py; the few `max(0, min(` that clamp no scroll are named here."""
        clamps = re.compile(r"max\(0,\s*min\(|max\(0,\s*[\w.\[\]\"()]+\s*-\s*[\w.\[\]\"()]*visible")
        others = ("fraction * width", "height - 8 * above", "visible, count - offset", "index, len(found)",
                  "selection.index, len(found)", "width - 2), most", "height - 2, height")
        hints = re.compile(r"for \w+, \w+, \w+, press in")
        for path in sorted(PACKAGE.rglob("*.py")):
            with self.subTest(module=module_of(path)):
                for line in path.read_text().splitlines():
                    if clamps.search(line) and path.name != "layout.py" and not any(o in line for o in others):
                        self.fail(f"{module_of(path)}: a scroll clamped by hand: {line.strip()}")
                self.assertEqual(1 if path.name == "text.py" else 0, len(hints.findall(path.read_text())),
                                 "only text.hint_at finds a hint")

if __name__ == "__main__":
    unittest.main()
