"""Tests for the typed-consensus source registry + per-skill artifact readers."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from omicsclaw.runtime.consensus.member import ConsensusMember


# --------------------------------------------------------------------------- #
# Registry shape                                                              #
# --------------------------------------------------------------------------- #

def test_registry_contains_v1_sources() -> None:
    from omicsclaw.runtime.consensus.sources import TYPED_CONSENSUS_REGISTRY

    assert set(TYPED_CONSENSUS_REGISTRY.keys()) == {
        "spatial-domains", "sc-clustering", "sc-integrate-cluster", "sc-pseudotime",
    }


def test_registry_values_are_typed_consensus_sources() -> None:
    from omicsclaw.runtime.consensus.source_registry import ConsensusSource
    from omicsclaw.runtime.consensus.sources import TYPED_CONSENSUS_REGISTRY

    for source in TYPED_CONSENSUS_REGISTRY.values():
        assert isinstance(source, ConsensusSource)
        assert hasattr(source.reader, "read_labels")
        assert hasattr(source.reader, "read_intrinsic_quality")


# --------------------------------------------------------------------------- #
# SpatialDomainsArtifactReader                                                #
# --------------------------------------------------------------------------- #

def _write_spatial_member_artifacts(
    output_root: Path,
    member_name: str,
    obs: list[str],
    labels: list[str],
    mean_local_purity: float,
) -> None:
    member_dir = output_root / member_name
    figure_dir = member_dir / "figure_data"
    figure_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"observation": obs, "spatial_domain": labels}).to_csv(
        figure_dir / "spatial_full.csv", index=False
    )
    (member_dir / "summary.json").write_text(
        json.dumps({"summary": {"mean_local_purity": mean_local_purity}})
    )


def test_spatial_reader_loads_labels(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.source_registry import SpatialDomainsArtifactReader

    member = ConsensusMember(name="banksy", skill_name="spatial-domains", params={"method": "banksy"})
    _write_spatial_member_artifacts(tmp_path, "banksy", ["s1", "s2", "s3"], ["L1", "L1", "L2"], 0.81)
    reader = SpatialDomainsArtifactReader()

    labels = reader.read_labels(member, tmp_path)
    assert labels is not None
    assert list(labels.index) == ["s1", "s2", "s3"]
    assert labels.to_list() == ["L1", "L1", "L2"]


def test_spatial_reader_reads_intrinsic_from_summary_json(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.source_registry import SpatialDomainsArtifactReader

    member = ConsensusMember(name="banksy", skill_name="spatial-domains", params={"method": "banksy"})
    _write_spatial_member_artifacts(tmp_path, "banksy", ["s1"], ["L1"], 0.73)
    assert SpatialDomainsArtifactReader().read_intrinsic_quality(member, tmp_path) == pytest.approx(0.73)


def test_spatial_reader_missing_artifact_returns_none(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.source_registry import SpatialDomainsArtifactReader

    member = ConsensusMember(name="ghost", skill_name="spatial-domains", params={"method": "x"})
    assert SpatialDomainsArtifactReader().read_labels(member, tmp_path) is None


def test_spatial_reader_rejects_claim_alias_labels(tmp_path: Path) -> None:
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME
    from omicsclaw.runtime.consensus.source_registry import SpatialDomainsArtifactReader

    member = ConsensusMember(
        name="banksy",
        skill_name="spatial-domains",
        params={"method": "banksy"},
    )
    member_dir = tmp_path / "banksy"
    figure_dir = member_dir / "figure_data"
    figure_dir.mkdir(parents=True)
    claim = member_dir / OUTPUT_CLAIM_FILENAME
    claim.write_text("observation,spatial_domain\ns1,L1\n", encoding="utf-8")
    (figure_dir / "domain_spatial_points.csv").hardlink_to(claim)

    assert SpatialDomainsArtifactReader().read_labels(member, tmp_path) is None


def test_spatial_reader_missing_summary_returns_zero(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.source_registry import SpatialDomainsArtifactReader

    member = ConsensusMember(name="banksy", skill_name="spatial-domains", params={"method": "banksy"})
    # labels only, no summary.json
    figure_dir = tmp_path / "banksy" / "figure_data"
    figure_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"observation": ["s1"], "spatial_domain": ["L1"]}).to_csv(
        figure_dir / "spatial_full.csv", index=False
    )
    assert SpatialDomainsArtifactReader().read_intrinsic_quality(member, tmp_path) == 0.0


def test_spatial_reader_globs_alternate_filenames(tmp_path: Path) -> None:
    """spatial-domains may write `spatial_umap.csv` etc; the reader falls back via glob."""
    from omicsclaw.runtime.consensus.source_registry import SpatialDomainsArtifactReader

    member = ConsensusMember(name="banksy", skill_name="spatial-domains", params={"method": "banksy"})
    figure_dir = tmp_path / "banksy" / "figure_data"
    figure_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"observation": ["s1", "s2"], "spatial_domain": ["A", "B"]}).to_csv(
        figure_dir / "spatial_umap.csv", index=False
    )
    labels = SpatialDomainsArtifactReader().read_labels(member, tmp_path)
    assert labels is not None
    assert labels.to_list() == ["A", "B"]


# --------------------------------------------------------------------------- #
# SpatialDomainsArtifactReader — REAL spatial-domains skill schema             #
#                                                                              #
# Locks the v1.x bug fix: the production spatial-domains skill writes          #
# ``figure_data/domain_spatial_points.csv`` and ``result.json`` (with          #
# ``summary.mean_local_purity`` nested), not the mock names used elsewhere     #
# in this file. The reader must handle both.                                   #
# --------------------------------------------------------------------------- #

def _write_real_spatial_domains_artifacts(
    output_root: Path,
    member_name: str,
    obs: list[str],
    labels: list[str],
    mean_local_purity: float,
) -> None:
    """Mirror exactly what skills/spatial/spatial-domains/ writes."""
    import json as _json
    member_dir = output_root / member_name
    figure_dir = member_dir / "figure_data"
    figure_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "observation": obs,
            "x": list(range(len(obs))),
            "y": list(range(len(obs))),
            "spatial_domain": labels,
            "domain_local_purity": [0.5] * len(obs),
        }
    ).to_csv(figure_dir / "domain_spatial_points.csv", index=False)
    (member_dir / "result.json").write_text(
        _json.dumps(
            {
                "skill": "spatial-domains",
                "summary": {
                    "method": "leiden",
                    "mean_local_purity": mean_local_purity,
                    "n_domains": len(set(labels)),
                },
            }
        )
    )


def test_spatial_reader_loads_labels_from_real_skill_schema(tmp_path: Path) -> None:
    """The production skill writes domain_spatial_points.csv — reader must find it."""
    from omicsclaw.runtime.consensus.source_registry import SpatialDomainsArtifactReader

    member = ConsensusMember(
        name="leiden_resolution-1.0",
        skill_name="spatial-domains",
        params={"method": "leiden"},
    )
    _write_real_spatial_domains_artifacts(
        tmp_path, "leiden_resolution-1.0", ["s1", "s2", "s3"], ["0", "0", "1"], 0.494
    )
    labels = SpatialDomainsArtifactReader().read_labels(member, tmp_path)
    assert labels is not None
    assert list(labels.index) == ["s1", "s2", "s3"]
    assert labels.to_list() == ["0", "0", "1"]


def test_spatial_reader_reads_intrinsic_from_result_json(tmp_path: Path) -> None:
    """When summary.json is absent, the reader must fall back to result.json:summary.mean_local_purity."""
    from omicsclaw.runtime.consensus.source_registry import SpatialDomainsArtifactReader

    member = ConsensusMember(
        name="leiden_resolution-1.0",
        skill_name="spatial-domains",
        params={"method": "leiden"},
    )
    _write_real_spatial_domains_artifacts(
        tmp_path, "leiden_resolution-1.0", ["s1"], ["0"], 0.4936
    )
    assert SpatialDomainsArtifactReader().read_intrinsic_quality(member, tmp_path) == pytest.approx(0.4936)


# --------------------------------------------------------------------------- #
# ScClusteringArtifactReader                                                  #
# --------------------------------------------------------------------------- #

def _write_sc_member_artifacts(
    output_root: Path,
    member_name: str,
    cluster_method: str,
    cells: list[str],
    labels: list[str],
    silhouette: float,
) -> None:
    member_dir = output_root / member_name
    figure_dir = member_dir / "figure_data"
    figure_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "cell_id": cells,
            "embedding_key": "X_umap",
            "coord1": list(range(len(cells))),
            "coord2": list(range(len(cells))),
            cluster_method: labels,
        }
    ).to_csv(figure_dir / "embedding_points.csv", index=False)
    pd.DataFrame(
        [
            {"metric": "n_cells", "value": len(cells)},
            {"metric": "silhouette_score", "value": silhouette},
        ]
    ).to_csv(figure_dir / "clustering_summary.csv", index=False)


def test_sc_reader_loads_labels_using_member_cluster_method(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.source_registry import ScClusteringArtifactReader

    member = ConsensusMember(
        name="leiden_r1.0",
        skill_name="sc-clustering",
        params={"cluster-method": "leiden", "resolution": "1.0"},
    )
    _write_sc_member_artifacts(tmp_path, "leiden_r1.0", "leiden", ["c1", "c2", "c3"], ["A", "A", "B"], 0.55)

    labels = ScClusteringArtifactReader().read_labels(member, tmp_path)
    assert labels is not None
    assert list(labels.index) == ["c1", "c2", "c3"]
    assert labels.to_list() == ["A", "A", "B"]


def test_sc_reader_handles_louvain_member(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.source_registry import ScClusteringArtifactReader

    member = ConsensusMember(
        name="louvain_r0.5",
        skill_name="sc-clustering",
        params={"cluster-method": "louvain", "resolution": "0.5"},
    )
    _write_sc_member_artifacts(tmp_path, "louvain_r0.5", "louvain", ["c1", "c2"], ["X", "Y"], 0.40)

    labels = ScClusteringArtifactReader().read_labels(member, tmp_path)
    assert labels is not None
    assert labels.to_list() == ["X", "Y"]


def test_sc_reader_reads_silhouette_from_clustering_summary(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.source_registry import ScClusteringArtifactReader

    member = ConsensusMember(
        name="leiden_r1.0",
        skill_name="sc-clustering",
        params={"cluster-method": "leiden", "resolution": "1.0"},
    )
    _write_sc_member_artifacts(tmp_path, "leiden_r1.0", "leiden", ["c1"], ["A"], 0.55)
    assert ScClusteringArtifactReader().read_intrinsic_quality(member, tmp_path) == pytest.approx(0.55)


def test_sc_reader_missing_clustering_summary_returns_zero(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.source_registry import ScClusteringArtifactReader

    member = ConsensusMember(
        name="leiden_r1.0",
        skill_name="sc-clustering",
        params={"cluster-method": "leiden", "resolution": "1.0"},
    )
    # labels only, no clustering_summary.csv
    figure_dir = tmp_path / "leiden_r1.0" / "figure_data"
    figure_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {"cell_id": ["c1"], "embedding_key": "X_umap", "coord1": [0], "coord2": [0], "leiden": ["A"]}
    ).to_csv(figure_dir / "embedding_points.csv", index=False)

    assert ScClusteringArtifactReader().read_intrinsic_quality(member, tmp_path) == 0.0


def test_sc_reader_missing_artifact_returns_none(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.source_registry import ScClusteringArtifactReader

    member = ConsensusMember(
        name="ghost",
        skill_name="sc-clustering",
        params={"cluster-method": "leiden", "resolution": "1.0"},
    )
    assert ScClusteringArtifactReader().read_labels(member, tmp_path) is None
