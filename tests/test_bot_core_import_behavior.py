from __future__ import annotations

import importlib
import sys

from omicsclaw.core.registry import OmicsRegistry


def _fresh_import_bot_core():
    for module_name in (
        "bot.core",
        "omicsclaw.runtime.context_assembler",
        "omicsclaw.runtime.context_layers",
    ):
        sys.modules.pop(module_name, None)
    return importlib.import_module("bot.core")


def test_importing_bot_core_does_not_force_registry_load(monkeypatch):
    calls: list[tuple[tuple, dict]] = []
    real_load_all = OmicsRegistry.load_all

    def spy(self, *args, **kwargs):
        calls.append((args, kwargs))
        return real_load_all(self, *args, **kwargs)

    monkeypatch.setattr(OmicsRegistry, "load_all", spy)

    _fresh_import_bot_core()

    assert calls == []


def test_bot_core_loads_registry_on_demand(monkeypatch):
    calls: list[tuple[tuple, dict]] = []
    real_load_all = OmicsRegistry.load_all

    def spy(self, *args, **kwargs):
        calls.append((args, kwargs))
        return real_load_all(self, *args, **kwargs)

    monkeypatch.setattr(OmicsRegistry, "load_all", spy)

    core = _fresh_import_bot_core()
    assert calls == []

    tool_registry = core.get_tool_registry()

    assert tool_registry is not None
    assert len(calls) >= 1
