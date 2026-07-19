"""Tests for the LCA R-subprocess wrapper.

These tests only exercise the Python wrapper boundary. The "happy path"
test that actually invokes R is gated by ``pytest.mark.requires_r`` and
skipped when ``Rscript`` is not on PATH or ``diceR`` is not installed.
The graceful-degradation tests run unconditionally.
"""

from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

import pandas as pd
import pytest

from omicsclaw.runtime.consensus.operators.lca_r import (
    LCAUnavailableError,
    lca_consensus,
    rscript_available,
)


def _r_with_dicer_available() -> bool:
    if not rscript_available():
        return False
    proc = subprocess.run(
        ["Rscript", "-e", "suppressPackageStartupMessages(library(diceR)); cat('ok')"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and "ok" in proc.stdout


def test_rscript_available_returns_bool() -> None:
    # The exact value depends on the test machine; we only assert the type.
    assert isinstance(rscript_available(), bool)


def test_lca_unavailable_when_rscript_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    df = pd.DataFrame(
        {"m1": [0, 0, 1, 1], "m2": [0, 0, 1, 1]},
        index=[f"obs_{i}" for i in range(4)],
    )
    monkeypatch.setattr(shutil, "which", lambda _binary: None)
    with pytest.raises(LCAUnavailableError, match="Install via"):
        lca_consensus(df)


def test_lca_requires_two_members() -> None:
    df = pd.DataFrame({"only": [0, 0, 1]}, index=["a", "b", "c"])
    with pytest.raises(ValueError, match="at least 2"):
        lca_consensus(df)


def test_lca_subprocess_scrubs_backend_control_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.runtime.consensus.operators.lca_r.wrapper as wrapper

    observed: dict[str, object] = {}

    def fake_run(*_args, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(returncode=1, stderr="expected test failure")

    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "must-not-leak")
    monkeypatch.setenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN", "must-not-leak")
    monkeypatch.setenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD", "3")
    monkeypatch.setattr(wrapper.shutil, "which", lambda _binary: "/usr/bin/Rscript")
    monkeypatch.setattr(wrapper.subprocess, "run", fake_run)
    labels = pd.DataFrame(
        {"m1": [0, 1], "m2": [0, 1]},
        index=["obs_0", "obs_1"],
    )

    with pytest.raises(RuntimeError, match="LCA R subprocess failed"):
        wrapper.lca_consensus(labels)

    child_env = observed["env"]
    assert isinstance(child_env, dict)
    assert "OMICSCLAW_REMOTE_AUTH_TOKEN" not in child_env
    assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in child_env
    assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD" not in child_env


@pytest.mark.skipif(not _r_with_dicer_available(), reason="Rscript + diceR not installed")
def test_lca_happy_path_returns_consensus_labels() -> None:
    df = pd.DataFrame(
        {
            "m1": [0, 0, 0, 1, 1, 1, 2, 2, 2],
            "m2": [0, 0, 0, 1, 1, 1, 2, 2, 2],
            "m3": [0, 0, 0, 1, 1, 1, 2, 2, 2],
        },
        index=[f"obs_{i}" for i in range(9)],
    )
    result = lca_consensus(df, seed=42)
    assert result.method == "lca"
    assert result.labels.shape[0] == 9
    # All members agree → LCA should produce 3 distinct groups of 3.
    assert result.n_clusters_returned == 3
    groups = result.labels.groupby(result.labels.to_numpy()).indices
    assert {len(v) for v in groups.values()} == {3}
