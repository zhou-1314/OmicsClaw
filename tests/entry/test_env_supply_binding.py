"""The entry binding's own seams: the tilde-root regressions and listing.

Review B1's reproductions, pinned: a ``~``-relative env root (the
default configuration) must reach the filesystem as the real home for
every probe and every install verification — not just for the listing,
which was the only caller that expanded it. ``HOME`` is pointed at a
tmp directory so the tests see exactly what a default-config deployment
would.

Also pins n6: ``list_environments`` answers the probe's own
``exists``/``ready``/``missing`` vocabulary per environment.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import pytest

from omicsclaw.entry.config import AppConfig
from omicsclaw.entry.env_supply import EnvSupplyBinding, build_env_supply
from omicsclaw.envsupply import PROBE_SENTINEL


@pytest.fixture()
def fake_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A tmp HOME; the default tilde root now resolves under it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OMICSCLAW_ENV_SUPPLY_ENV_ROOT", raising=False)
    return home


def _plant_interpreter(home: pathlib.Path) -> pathlib.Path:
    """A real, runnable interpreter at $HOME/.omicsclaw/envs/main/bin/python."""
    target = home / ".omicsclaw" / "envs" / "main" / "bin" / "python"
    target.parent.mkdir(parents=True)
    target.symlink_to(sys.executable)
    return target


def _run(coro):
    return asyncio.run(coro)


# ---- B1: the tilde root reaches the filesystem ---------------------------------


def test_a_tilde_root_probes_the_real_home(fake_home):
    python = _plant_interpreter(fake_home)
    binding = EnvSupplyBinding()  # the default "~/.omicsclaw/envs" root
    report = _run(binding.probe_environment(env="main"))
    assert report["exists"] is True, report
    assert report["interpreter"] == str(python)
    # this repo's test interpreter has ipykernel; the symlink runs it
    assert report["ready"] is True


def test_the_default_config_binding_probes_the_real_home(fake_home):
    python = _plant_interpreter(fake_home)
    binding = build_env_supply(
        AppConfig(workspace=fake_home)
    )  # env_supply off changes nothing here
    report = _run(binding.probe_environment(env="main"))
    assert report["exists"] is True, report
    assert report["interpreter"] == str(python)


def test_a_tilde_root_install_verifies_the_real_home(fake_home):
    _plant_interpreter(fake_home)
    script_argv: list[list[str]] = []

    def fake_install_runner(argv, *, on_line=None, timeout=0):
        script_argv.append(list(argv))
        for line in (
            "===OMICSCLAW_ENV_STEP:python===",
            "===OMICSCLAW_ENV_STEP:venv===",
            "===OMICSCLAW_ENV_STEP:packages===",
            "===OMICSCLAW_ENV_STEP:verify===",
        ):
            if on_line is not None:
                on_line(line)
        return 0

    def fake_probe_runner(argv):
        from omicsclaw.envsupply import ProbeOutcome

        return ProbeOutcome(
            0,
            PROBE_SENTINEL + "\n" + json.dumps({"ipykernel": True}) + "\n",
        )

    binding = EnvSupplyBinding(
        install_runner=fake_install_runner, probe_runner=fake_probe_runner
    )
    result = _run(binding.install_environment(env="main"))
    # the install ran, and the verify probe reached the expanded path —
    # the exact step that used to convert success into "cannot import
    # ipykernel" when the root stayed literal
    assert result["verified"] is True
    assert result["interpreter"] == str(
        fake_home / ".omicsclaw" / "envs" / "main" / "bin" / "python"
    )
    assert len(script_argv) == 1
    assert script_argv[0][:2] == ["sh", "-c"]
    assert str(fake_home) in script_argv[0][2]


# ---- n6: the listing speaks the probe's vocabulary ------------------------------


def test_listing_probes_each_environment(fake_home):
    ready_python = _plant_interpreter(fake_home)
    other = fake_home / ".omicsclaw" / "envs" / "spatial"
    (other / "bin").mkdir(parents=True)

    def fake_probe_runner(argv):
        from omicsclaw.envsupply import ProbeOutcome

        if str(ready_python) in argv:
            # every asked-about name answers present, the way a healthy
            # env would for the probe's full default list
            found = {name: True for name in argv[3:]}
        else:
            found = {name: False for name in argv[3:]}
        return ProbeOutcome(
            0, PROBE_SENTINEL + "\n" + json.dumps(found) + "\n"
        )

    binding = EnvSupplyBinding(probe_runner=fake_probe_runner)
    found = _run(binding.list_environments())
    by_name = {entry["name"]: entry for entry in found}
    assert set(by_name) == {"main", "spatial"}
    assert by_name["main"]["ready"] is True
    assert by_name["main"]["missing"] == []
    # a directory with no interpreter file is listed but not probed
    assert by_name["spatial"]["exists"] is False
    assert by_name["spatial"]["ready"] is False


def test_an_empty_root_lists_nothing(fake_home):
    binding = EnvSupplyBinding()
    assert _run(binding.list_environments()) == []


def test_the_shared_patterns_are_one_implementation():
    # review n7: the tool and the jobs plane validate through the same
    # compiled objects, imported from the schema package
    from omicsclaw.entry.desktop.jobs_manager import (
        ENV_NAME_PATTERN as JM_ENV,
        PACKAGE_NAME_PATTERN as JM_PKG,
    )
    from omicsclaw.schema import (
        ENV_NAME_PATTERN,
        PACKAGE_NAME_PATTERN,
    )
    from omicsclaw.tools.builtin import manage_env

    assert manage_env._ENV_NAME is ENV_NAME_PATTERN is JM_ENV
    assert manage_env._PACKAGE_NAME is PACKAGE_NAME_PATTERN is JM_PKG
