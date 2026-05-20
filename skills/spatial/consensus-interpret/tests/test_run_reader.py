"""Slice 1 — TypedRunBundle reader (T1 preflight per ADR 0012).

Tests cover happy path + every T1 fail-fast path:
- TypedRunInvalidError when plan.json / consensus_labels.tsv missing/malformed
- AdataMismatchError when adata path absent or obs index disjoint
- --adata override precedence
- backward-compat: plan.json without input_path requires --adata override
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


# --------------------------------------------------------------------------- #
# Fixture helpers                                                             #
# --------------------------------------------------------------------------- #

def _make_typed_run(
    tmp_path: Path,
    *,
    run_dir_name: str = "typed_run",
    observations: list[str] | None = None,
    include_input_path: bool = True,
    adata_relative_to_typed_run: str = "../adata.h5ad",
    operator: str = "kmode",
) -> tuple[Path, Path]:
    """Lay out a minimal valid typed-consensus run dir + adata file.

    Returns ``(typed_run_dir, adata_path)``.
    """
    if observations is None:
        observations = [f"obs_{i}" for i in range(8)]

    typed_run_dir = tmp_path / run_dir_name
    typed_run_dir.mkdir(parents=True, exist_ok=True)

    adata_path = (typed_run_dir / adata_relative_to_typed_run).resolve()
    adata_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(observations)
    adata = ad.AnnData(
        X=np.random.default_rng(0).random((n, 5)).astype("float32"),
        obs=pd.DataFrame(index=pd.Index(observations, name="cell_id")),
        var=pd.DataFrame(index=[f"gene_{i}" for i in range(5)]),
    )
    adata.write_h5ad(adata_path)

    # consensus_labels.tsv
    consensus_col = f"consensus_{operator}"
    pd.DataFrame({
        "observation": observations,
        consensus_col: [i % 3 for i in range(n)],
    }).to_csv(typed_run_dir / "consensus_labels.tsv", sep="\t", index=False)

    # member_scores.csv (6-column ADR 0011 schema)
    pd.DataFrame([
        {"member": "m0", "composite": 0.62, "cross_nmi_mean": 0.65, "intrinsic": 0.58, "max_class_frac": 0.33, "filtered": False, "filter_reason": ""},
        {"member": "m1", "composite": 0.55, "cross_nmi_mean": 0.60, "intrinsic": 0.48, "max_class_frac": 0.20, "filtered": False, "filter_reason": ""},
    ]).to_csv(typed_run_dir / "member_scores.csv", index=False)

    # cross_method_nmi.csv (2×2)
    pd.DataFrame(
        [[1.0, 0.65], [0.65, 1.0]],
        index=["m0", "m1"],
        columns=["m0", "m1"],
    ).to_csv(typed_run_dir / "cross_method_nmi.csv")

    # plan.json
    plan: dict[str, object] = {
        "run_id": run_dir_name,
        "operator": operator,
        "members": [{"name": "m0", "params": {"method": "leiden"}}, {"name": "m1", "params": {"method": "louvain"}}],
        "alpha": 0.6, "beta": 0.4, "max_class_frac": 0.8,
    }
    if include_input_path:
        plan["input_path"] = str(adata_path)
    (typed_run_dir / "plan.json").write_text(json.dumps(plan, indent=2))

    return typed_run_dir, adata_path


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #

def test_load_typed_run_returns_bundle_with_canonical_fields(tmp_path: Path) -> None:
    from _run_reader import load_typed_run  # type: ignore[import-not-found]

    typed_run_dir, adata_path = _make_typed_run(tmp_path)
    bundle = load_typed_run(typed_run_dir)

    assert bundle.typed_run_dir == typed_run_dir.resolve()
    assert bundle.adata_path == adata_path.resolve()
    assert bundle.plan["run_id"] == "typed_run"
    assert bundle.consensus_label_column == "consensus_kmode"
    assert "observation" in bundle.consensus_labels.columns
    assert bundle.consensus_label_column in bundle.consensus_labels.columns
    assert len(bundle.consensus_labels) == 8
    assert "member" in bundle.member_scores.columns
    assert bundle.nmi_matrix.shape == (2, 2)


def test_load_typed_run_dispatches_label_column_from_operator(tmp_path: Path) -> None:
    """consensus_label_column derives from plan.operator."""
    from _run_reader import load_typed_run  # type: ignore[import-not-found]

    typed_run_dir, _ = _make_typed_run(tmp_path, operator="weighted")
    bundle = load_typed_run(typed_run_dir)

    assert bundle.consensus_label_column == "consensus_weighted"


# --------------------------------------------------------------------------- #
# T1 fail-fast paths                                                          #
# --------------------------------------------------------------------------- #

def test_load_typed_run_missing_plan_json_raises_TypedRunInvalid(tmp_path: Path) -> None:
    from _errors import TypedRunInvalidError  # type: ignore[import-not-found]
    from _run_reader import load_typed_run  # type: ignore[import-not-found]

    typed_run_dir, _ = _make_typed_run(tmp_path)
    (typed_run_dir / "plan.json").unlink()

    with pytest.raises(TypedRunInvalidError, match="plan.json"):
        load_typed_run(typed_run_dir)


def test_load_typed_run_missing_consensus_labels_raises_TypedRunInvalid(tmp_path: Path) -> None:
    from _errors import TypedRunInvalidError  # type: ignore[import-not-found]
    from _run_reader import load_typed_run  # type: ignore[import-not-found]

    typed_run_dir, _ = _make_typed_run(tmp_path)
    (typed_run_dir / "consensus_labels.tsv").unlink()

    with pytest.raises(TypedRunInvalidError, match="consensus_labels.tsv"):
        load_typed_run(typed_run_dir)


def test_load_typed_run_malformed_plan_json_raises_TypedRunInvalid(tmp_path: Path) -> None:
    from _errors import TypedRunInvalidError  # type: ignore[import-not-found]
    from _run_reader import load_typed_run  # type: ignore[import-not-found]

    typed_run_dir, _ = _make_typed_run(tmp_path)
    (typed_run_dir / "plan.json").write_text("{not valid json")

    with pytest.raises(TypedRunInvalidError, match="malformed|JSON|parse"):
        load_typed_run(typed_run_dir)


def test_load_typed_run_missing_adata_at_plan_path_raises_AdataMismatch(tmp_path: Path) -> None:
    from _errors import AdataMismatchError  # type: ignore[import-not-found]
    from _run_reader import load_typed_run  # type: ignore[import-not-found]

    typed_run_dir, adata_path = _make_typed_run(tmp_path)
    adata_path.unlink()

    with pytest.raises(AdataMismatchError, match="adata"):
        load_typed_run(typed_run_dir)


def test_load_typed_run_obs_disjoint_raises_AdataMismatch(tmp_path: Path) -> None:
    """consensus_labels observations must be a subset of adata.obs.index."""
    from _errors import AdataMismatchError  # type: ignore[import-not-found]
    from _run_reader import load_typed_run  # type: ignore[import-not-found]

    typed_run_dir, adata_path = _make_typed_run(tmp_path, observations=[f"obs_{i}" for i in range(8)])

    # Overwrite consensus_labels.tsv with observation ids that don't exist in adata
    pd.DataFrame({
        "observation": [f"ghost_{i}" for i in range(8)],
        "consensus_kmode": [i % 3 for i in range(8)],
    }).to_csv(typed_run_dir / "consensus_labels.tsv", sep="\t", index=False)

    with pytest.raises(AdataMismatchError, match="obs|observation"):
        load_typed_run(typed_run_dir)


# --------------------------------------------------------------------------- #
# Override + backward-compat                                                  #
# --------------------------------------------------------------------------- #

def test_load_typed_run_adata_override_takes_precedence(tmp_path: Path) -> None:
    """--adata <path> overrides plan.json:input_path."""
    from _run_reader import load_typed_run  # type: ignore[import-not-found]

    typed_run_dir, _ = _make_typed_run(tmp_path)

    # Build a second adata at a different location, with same obs
    alt_adata = tmp_path / "alt_adata.h5ad"
    ad.AnnData(
        X=np.zeros((8, 3), dtype="float32"),
        obs=pd.DataFrame(index=[f"obs_{i}" for i in range(8)]),
        var=pd.DataFrame(index=["a", "b", "c"]),
    ).write_h5ad(alt_adata)

    bundle = load_typed_run(typed_run_dir, adata_override=alt_adata)
    assert bundle.adata_path == alt_adata.resolve()


def test_load_typed_run_no_input_path_requires_adata_override(tmp_path: Path) -> None:
    """Slice 0.4 backward-compat: legacy typed runs without input_path in
    plan.json must surface a clear T1 error pointing the user at --adata."""
    from _errors import TypedRunInvalidError  # type: ignore[import-not-found]
    from _run_reader import load_typed_run  # type: ignore[import-not-found]

    typed_run_dir, _ = _make_typed_run(tmp_path, include_input_path=False)

    with pytest.raises(TypedRunInvalidError, match="input_path|--adata"):
        load_typed_run(typed_run_dir)


def test_load_typed_run_no_input_path_with_override_succeeds(tmp_path: Path) -> None:
    """If user supplies --adata, lack of plan.json:input_path is OK."""
    from _run_reader import load_typed_run  # type: ignore[import-not-found]

    typed_run_dir, adata_path = _make_typed_run(tmp_path, include_input_path=False)
    bundle = load_typed_run(typed_run_dir, adata_override=adata_path)

    assert bundle.adata_path == adata_path.resolve()
