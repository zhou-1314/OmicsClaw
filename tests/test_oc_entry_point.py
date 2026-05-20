"""Regression tests for the ``oc`` / ``omicsclaw`` console-script entry points.

These tests guard against the "stale wrapper script" failure mode that occurs
when ``omicsclaw`` module paths are refactored (e.g. ``omicsclaw.cli`` was
moved under ``omicsclaw.surfaces.cli`` in ADR 0005) but ``pip install -e .``
is not re-run in the active environment. setuptools regenerates the console
scripts and the ``*.dist-info/entry_points.txt`` only at install time, so a
stale environment keeps importing the old module path and crashes with
``ModuleNotFoundError`` before ``main()`` ever runs.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
from importlib.metadata import entry_points

import pytest

EXPECTED = {
    "oc": "omicsclaw.surfaces.cli.launcher:main",
    "omicsclaw": "omicsclaw.surfaces.cli.launcher:main",
    "oc-chat": "omicsclaw.surfaces.cli:main",
    "omicsclaw-chat": "omicsclaw.surfaces.cli:main",
}


def _console_script(name: str):
    for ep in entry_points(group="console_scripts"):
        if ep.name == name:
            return ep
    return None


@pytest.mark.parametrize("name,target", sorted(EXPECTED.items()))
def test_console_script_metadata_matches_pyproject(name: str, target: str) -> None:
    ep = _console_script(name)
    assert ep is not None, f"console script {name!r} is not registered"
    assert ep.value == target, (
        f"{name!r} entry point is {ep.value!r}; expected {target!r}. "
        "Run `pip install -e .` to refresh after a surfaces refactor."
    )


@pytest.mark.parametrize("name,target", sorted(EXPECTED.items()))
def test_console_script_target_imports(name: str, target: str) -> None:
    module_path, _, attr = target.partition(":")
    module = importlib.import_module(module_path)
    assert callable(getattr(module, attr)), f"{target} is not callable"


@pytest.mark.skipif(shutil.which("oc") is None, reason="oc not on PATH")
def test_oc_wrapper_runs_without_import_error() -> None:
    """The installed ``oc`` wrapper must import its target module cleanly."""
    result = subprocess.run(
        ["oc", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert "ModuleNotFoundError" not in combined, (
        "oc wrapper imports a stale module (run `pip install -e .`):\n"
        f"{combined[-800:]}"
    )
