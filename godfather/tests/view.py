"""Slotfather's dashboard module as its moved tests knew it: one namespace over Godfather's tui, overview, adapter
and app modules. A name is looked up in each in turn, and setting one (mock.patch.object) sets it on the module
that defines it, so a patch reaches the code that reads it."""
from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

import helpers  # noqa: F401 - puts src/ on sys.path

NAMES = ("godfather.views.overview", "godfather.views.queue", "godfather.views.details", "godfather.views.events",
         "godfather.views.machine", "godfather.views.overlays", "godfather.views.common", "godfather.views.settings",
         "godfather.views.settings_draw", "godfather.widgets",
         "godfather.layout", "godfather.state", "godfather.modes", "godfather.tui", "godfather.app",
         "godfather.sources", "godfather.version", "godfather.news", "godfather.adapters.slotfather")


class View:
    __slots__ = ("_modules",)

    def __init__(self, modules: list[ModuleType]) -> None:
        object.__setattr__(self, "_modules", modules)

    @property
    def __dict__(self) -> dict[str, Any]:  # type: ignore[override]
        """Every module's names, so mock.patch takes them as this object's own and restores them by setattr."""
        merged: dict[str, Any] = {}
        for module in reversed(self._modules):
            merged.update(vars(module))
        return merged

    def _owner(self, name: str) -> ModuleType:
        for module in self._modules:
            if name in vars(module):
                return module
        raise AttributeError(name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._owner(name), name)

    def __setattr__(self, name: str, value: Any) -> None:
        try:
            setattr(self._owner(name), name, value)
        except AttributeError:
            setattr(self._modules[0], name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._owner(name), name)


def fresh() -> Any:
    """Every module reloaded, in import order, so module state (theme, marks, what's new) starts over."""
    modules = []
    for name in ("godfather.theme", "godfather.keys", "godfather.tui", "godfather.layout", "godfather.widgets",
                 "godfather.news", "godfather.views.common", "godfather.views.queue", "godfather.views.events", "godfather.views.details",
                 "godfather.views.machine", "godfather.views.overlays", "godfather.views.settings_draw",
                 "godfather.views.settings", "godfather.views.overview", "godfather.state", "godfather.modes",
                 "godfather.app"):
        importlib.reload(importlib.import_module(name))
    for name in NAMES:
        modules.append(importlib.import_module(name))
    return View(modules)


def current() -> Any:
    return View([importlib.import_module(name) for name in NAMES])
