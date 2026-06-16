"""Tests for the consensus explainability helpers (per-spot confidence + heatmap)."""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from omicsclaw.runtime.consensus.explain import per_spot_confidence, render_nmi_heatmap


def test_per_spot_confidence_known_matrix() -> None:
    # 3 members (already aligned to the consensus label space), 4 observations.
    aligned = pd.DataFrame(
        {"m1": [1, 1, 2, 3], "m2": [1, 2, 2, 3], "m3": [1, 1, 1, 2]},
        index=["o0", "o1", "o2", "o3"],
    )

    conf = per_spot_confidence(aligned)
    assert list(conf.columns) == ["support", "entropy", "n_members"]
    assert (conf["n_members"] == 3).all()

    # o0: all three agree with consensus(1) -> support 1.0, entropy 0.
    assert conf.loc["o0", "support"] == pytest.approx(1.0)
    assert conf.loc["o0", "entropy"] == pytest.approx(0.0)

    # o1: m1=1,m2=2,m3=1 vs consensus 1 -> 2/3 agree; dist {1:2,2:1}.
    assert conf.loc["o1", "support"] == pytest.approx(2 / 3)
    expected_entropy = -(2 / 3 * math.log2(2 / 3) + 1 / 3 * math.log2(1 / 3))
    assert conf.loc["o1", "entropy"] == pytest.approx(expected_entropy)


def test_per_spot_confidence_support_bounds() -> None:
    aligned = pd.DataFrame({"a": [1, 2], "b": [2, 2], "c": [3, 2]}, index=["x", "y"])
    conf = per_spot_confidence(aligned)
    assert conf.loc["x", "support"] == pytest.approx(1 / 3)  # 3-way split, plurality 1/3
    assert conf.loc["y", "support"] == pytest.approx(1.0)    # unanimous
    assert ((conf["support"] >= 0) & (conf["support"] <= 1)).all()


def test_per_spot_confidence_operator_agnostic_on_relabeled_consensus() -> None:
    # Even if the consensus operator returns labels in a DIFFERENT space (e.g.
    # LCA's diceR class IDs), support/entropy depend only on member agreement.
    aligned = pd.DataFrame({"a": [1, 1], "b": [1, 2], "c": [1, 2]}, index=["p", "q"])
    conf = per_spot_confidence(aligned)
    assert conf.loc["p", "support"] == pytest.approx(1.0)        # all members agree
    assert conf.loc["q", "support"] == pytest.approx(2 / 3)      # 2 of 3 agree (label 2)


def test_render_nmi_heatmap_writes_png(tmp_path: Path) -> None:
    nmi = pd.DataFrame(
        [[1.0, 0.6, 0.4], [0.6, 1.0, 0.5], [0.4, 0.5, 1.0]],
        index=["a", "b", "c"], columns=["a", "b", "c"],
    )
    out = tmp_path / "nmi.png"
    result = render_nmi_heatmap(nmi, out)
    # matplotlib is a project dependency; the heatmap should render.
    assert result == out
    assert out.exists() and out.stat().st_size > 0


def test_render_nmi_heatmap_graceful_on_bad_input(tmp_path: Path) -> None:
    # Non-numeric matrix can't convert to float -> returns None, no exception.
    nmi = pd.DataFrame({"a": ["x", "y"], "b": ["z", "w"]}, index=["a", "b"])
    assert render_nmi_heatmap(nmi, tmp_path / "bad.png") is None
