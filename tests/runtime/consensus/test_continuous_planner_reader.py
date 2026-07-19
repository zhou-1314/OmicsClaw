"""Planner, reader, and CLI-routing tests for the continuous flavour (ADR 0031)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from omicsclaw.runtime.consensus.member import ConsensusMember
from omicsclaw.runtime.consensus.planners import PseudotimeMethodPlanner
from omicsclaw.runtime.consensus.source_registry import PseudotimeArtifactReader
from omicsclaw.runtime.consensus.sources import CONSENSUS_SOURCES


def _args(**kw):
    base = dict(members=None, pseudotime_methods=None, root_cluster=None, root_cell=None)
    base.update(kw)
    return SimpleNamespace(**base)


_SRC = CONSENSUS_SOURCES["sc-consensus-pseudotime"]


# ---- planner --------------------------------------------------------------- #

def test_planner_requires_a_shared_root():
    with pytest.raises(SystemExit, match="requires a shared root"):
        PseudotimeMethodPlanner().propose(_args(), source=_SRC)


def test_planner_default_methods_with_root_cluster():
    members = PseudotimeMethodPlanner().propose(_args(root_cluster="Stem"), source=_SRC)
    assert [m.name for m in members] == ["dpt", "palantir", "via"]
    assert all(m.params["method"] == m.name for m in members)
    assert all(m.params["root-cluster"] == "Stem" for m in members)
    assert all("root-cell" not in m.params for m in members)


def test_planner_explicit_methods_with_root_cell():
    members = PseudotimeMethodPlanner().propose(
        _args(members="dpt,via", root_cell="42"), source=_SRC
    )
    assert [m.name for m in members] == ["dpt", "via"]
    assert all(m.params["root-cell"] == "42" for m in members)


def test_planner_rejects_parameterized_and_duplicate_members():
    with pytest.raises(SystemExit, match="plain method names"):
        PseudotimeMethodPlanner().propose(_args(members="dpt:foo=1", root_cluster="X"), source=_SRC)
    with pytest.raises(SystemExit, match="duplicate"):
        PseudotimeMethodPlanner().propose(_args(members="dpt,dpt", root_cluster="X"), source=_SRC)


def test_planner_rejects_out_of_scope_and_unknown_methods():
    # multi-lineage methods are deferred (ADR 0031 §3) — rejected before fan-out
    for spec_kw in (dict(members="slingshot_r"), dict(pseudotime_methods="monocle3_r"),
                    dict(members="cellrank"), dict(members="dptt")):
        with pytest.raises(SystemExit, match="v1 supports only"):
            PseudotimeMethodPlanner().propose(_args(root_cluster="X", **spec_kw), source=_SRC)
    # a partly-valid spec still fails if ANY method is out of scope
    with pytest.raises(SystemExit, match="v1 supports only"):
        PseudotimeMethodPlanner().propose(_args(members="dpt,slingshot_r", root_cluster="X"), source=_SRC)
    # valid subsets still pass
    members = PseudotimeMethodPlanner().propose(_args(members="dpt,via", root_cluster="X"), source=_SRC)
    assert [m.name for m in members] == ["dpt", "via"]


# ---- reader ---------------------------------------------------------------- #

def test_reader_reads_canonical_pseudotime(tmp_path: Path):
    anndata = pytest.importorskip("anndata")
    mdir = tmp_path / "dpt"
    mdir.mkdir()
    adata = anndata.AnnData(X=np.zeros((5, 2), dtype=float))
    adata.obs_names = [f"c{i}" for i in range(5)]
    adata.obs["pseudotime"] = [0.1, 0.2, 0.3, 0.4, 0.5]
    adata.write_h5ad(mdir / "processed.h5ad")

    reader = PseudotimeArtifactReader()
    member = ConsensusMember(name="dpt", skill_name="sc-pseudotime", params={"method": "dpt"})
    series = reader.read_labels(member, tmp_path)
    assert series is not None
    assert list(series.index) == [f"c{i}" for i in range(5)]
    assert float(series.iloc[0]) == pytest.approx(0.1)
    assert reader.read_intrinsic_quality(member, tmp_path) == 0.0


def test_reader_missing_artifact_returns_none(tmp_path: Path):
    reader = PseudotimeArtifactReader()
    member = ConsensusMember(name="absent", skill_name="sc-pseudotime", params={"method": "dpt"})
    assert reader.read_labels(member, tmp_path) is None


def test_reader_rejects_claim_alias_h5ad(tmp_path: Path):
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    anndata = pytest.importorskip("anndata")
    member_dir = tmp_path / "dpt"
    member_dir.mkdir()
    adata = anndata.AnnData(X=np.zeros((2, 1), dtype=float))
    adata.obs["pseudotime"] = [0.1, 0.9]
    claim = member_dir / OUTPUT_CLAIM_FILENAME
    adata.write_h5ad(claim)
    (member_dir / "processed.h5ad").hardlink_to(claim)
    member = ConsensusMember(
        name="dpt",
        skill_name="sc-pseudotime",
        params={"method": "dpt"},
    )

    assert PseudotimeArtifactReader().read_labels(member, tmp_path) is None


# ---- CLI operator routing -------------------------------------------------- #

def test_cli_continuous_rejects_categorical_operator(tmp_path: Path):
    from omicsclaw.runtime.consensus.run import main

    rc = main([
        "--source", "sc-consensus-pseudotime", "--output", str(tmp_path / "o"),
        "--operator", "kmode", "--root-cluster", "X", "--non-interactive",
    ])
    assert rc == 2


def test_cli_categorical_rejects_continuous_operator(tmp_path: Path):
    from omicsclaw.runtime.consensus.run import main

    rc = main([
        "--source", "sc-consensus-clustering", "--output", str(tmp_path / "o"),
        "--operator", "median", "--non-interactive",
    ])
    assert rc == 2
