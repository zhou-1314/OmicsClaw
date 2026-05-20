"""Slice 7 — Report + artifact writers.

Tests the markdown banner discipline, JSON schema, contradiction
regions extraction, and the 5-file artifact layout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


def _bundle(tmp_path: Path):
    """Build a minimal TypedRunBundle for testing report writers."""
    from _run_reader import TypedRunBundle  # type: ignore[import-not-found]

    consensus_labels = pd.DataFrame({
        "observation": [f"obs_{i}" for i in range(6)],
        "consensus_kmode": [0, 0, 0, 1, 1, 1],
    })
    member_scores = pd.DataFrame([
        {"member": "m0", "composite": 0.6, "cross_nmi_mean": 0.65, "intrinsic": 0.55, "max_class_frac": 0.3, "filtered": False, "filter_reason": ""},
    ])
    nmi = pd.DataFrame(
        [[1.0, 0.45], [0.45, 1.0]],
        index=["m0", "m1"],
        columns=["m0", "m1"],
    )
    return TypedRunBundle(
        typed_run_dir=tmp_path / "typed_run",
        plan={"run_id": "test_run", "operator": "kmode", "input_path": str(tmp_path / "fake.h5ad")},
        consensus_labels=consensus_labels,
        consensus_label_column="consensus_kmode",
        member_scores=member_scores,
        nmi_matrix=nmi,
        adata_path=tmp_path / "fake.h5ad",
    )


def _annotations():
    from _llm import ClusterAnnotation, EvidenceMarker  # type: ignore[import-not-found]
    return [
        ClusterAnnotation(
            cluster_id=0, n_cells=3, cell_type="CA1 pyramidal", confidence=0.84,
            evidence_markers=[EvidenceMarker(gene="Pvrl3", de_rank=1, db_source="panglaodb_brain", db_celltype="CA1 pyramidal", weight=0.85)],
            narrative="Cluster 0 expresses Pvrl3 with mean_local_purity 0.617.",
        ),
        ClusterAnnotation(
            cluster_id=1, n_cells=3, cell_type="Unknown", confidence=0.1,
            evidence_markers=[],
            narrative="Cluster 1 markers do not match any candidate.",
        ),
    ]


def _next_steps():
    from _llm import NextStep  # type: ignore[import-not-found]
    return [
        NextStep(
            skill="spatial-de",
            args_hint="--groupby consensus_kmode --comparisons cluster_0_vs_1",
            priority=1,
            evidence_refs=["cross_method_nmi.csv:row=m0,col=m1,value=0.450"],
            reason="Lowest pair-wise NMI in matrix; marker disambiguation needed.",
        ),
    ]


# --------------------------------------------------------------------------- #
# format_interpreted_report (markdown)                                        #
# --------------------------------------------------------------------------- #

def test_report_starts_with_AI_banner(tmp_path: Path) -> None:
    from _report import format_interpreted_report  # type: ignore[import-not-found]

    md = format_interpreted_report(
        bundle=_bundle(tmp_path), annotations=_annotations(), next_steps=_next_steps(),
        banner="[A+I: Interpreted on verified consensus]",
    )
    assert md.splitlines()[0] == "[A+I: Interpreted on verified consensus]"


def test_report_audit_footer_cites_typed_namespace(tmp_path: Path) -> None:
    from _report import format_interpreted_report  # type: ignore[import-not-found]

    md = format_interpreted_report(
        bundle=_bundle(tmp_path), annotations=_annotations(), next_steps=_next_steps(),
        banner="[A+I: Interpreted on verified consensus]",
    )
    assert "analysis://typed/test_run" in md, "audit footer must cite evidence base namespace"
    assert "analysis://interpreted/test_run" in md, "audit footer must declare interpreted namespace"


def test_report_includes_per_cluster_annotations(tmp_path: Path) -> None:
    from _report import format_interpreted_report  # type: ignore[import-not-found]

    md = format_interpreted_report(
        bundle=_bundle(tmp_path), annotations=_annotations(), next_steps=_next_steps(),
        banner="[A+I: Interpreted on verified consensus]",
    )
    assert "CA1 pyramidal" in md
    assert "Pvrl3" in md
    assert "Cluster 0" in md or "cluster 0" in md.lower()


def test_report_includes_next_steps_with_evidence(tmp_path: Path) -> None:
    from _report import format_interpreted_report  # type: ignore[import-not-found]

    md = format_interpreted_report(
        bundle=_bundle(tmp_path), annotations=_annotations(), next_steps=_next_steps(),
        banner="[A+I: Interpreted on verified consensus]",
    )
    assert "spatial-de" in md
    assert "cross_method_nmi.csv" in md
    assert "0.450" in md


def test_report_noLLM_banner_path(tmp_path: Path) -> None:
    from _report import format_interpreted_report  # type: ignore[import-not-found]

    md = format_interpreted_report(
        bundle=_bundle(tmp_path), annotations=[], next_steps=[],
        banner="[I-noLLM: Structural patterns only — biology annotation disabled]",
    )
    assert md.splitlines()[0] == "[I-noLLM: Structural patterns only — biology annotation disabled]"
    # Structural summary must still be there even without LLM
    assert "consensus" in md.lower()


# --------------------------------------------------------------------------- #
# format_assignments_json (machine-readable)                                  #
# --------------------------------------------------------------------------- #

def test_assignments_json_schema_version_and_namespaces(tmp_path: Path) -> None:
    from _report import format_assignments_json  # type: ignore[import-not-found]

    data = format_assignments_json(
        bundle=_bundle(tmp_path), annotations=_annotations(), next_steps=_next_steps(),
        banner="[A+I: Interpreted on verified consensus]",
    )

    assert data["schema_version"] == "0.1"
    assert data["typed_run_id"] == "test_run"
    assert data["evidence_base_namespace"] == "analysis://typed/test_run"
    assert data["interpreted_namespace"] == "analysis://interpreted/test_run"
    assert data["banner"] == "[A+I: Interpreted on verified consensus]"


def test_assignments_json_cluster_structure(tmp_path: Path) -> None:
    from _report import format_assignments_json  # type: ignore[import-not-found]

    data = format_assignments_json(
        bundle=_bundle(tmp_path), annotations=_annotations(), next_steps=_next_steps(),
        banner="[A+I: Interpreted on verified consensus]",
    )

    assert len(data["clusters"]) == 2
    c0 = data["clusters"][0]
    assert c0["id"] == 0
    assert c0["cell_type"] == "CA1 pyramidal"
    assert c0["interpretation_status"] == "interpreted"
    assert c0["evidence"]["markers"][0]["gene"] == "Pvrl3"

    c1 = data["clusters"][1]
    assert c1["cell_type"] == "Unknown"
    assert c1["interpretation_status"] == "low_confidence"
    assert c1["evidence"]["markers"] == []


def test_assignments_json_next_steps_capped_and_evidence_refs_present(tmp_path: Path) -> None:
    from _report import format_assignments_json  # type: ignore[import-not-found]

    data = format_assignments_json(
        bundle=_bundle(tmp_path), annotations=_annotations(), next_steps=_next_steps(),
        banner="[A+I: Interpreted on verified consensus]",
    )

    assert len(data["next_steps"]) == 1
    ns = data["next_steps"][0]
    assert ns["skill"] == "spatial-de"
    assert ns["evidence_refs"]


# --------------------------------------------------------------------------- #
# format_contradiction_regions                                                 #
# --------------------------------------------------------------------------- #

def test_contradiction_regions_extracts_low_nmi_pairs(tmp_path: Path) -> None:
    from _report import format_contradiction_regions  # type: ignore[import-not-found]

    df = format_contradiction_regions(_bundle(tmp_path), threshold=0.65)
    # (m0, m1) at 0.45 should appear; the 1.0 diagonal must not
    assert len(df) == 1
    row = df.iloc[0]
    assert {row["member_i"], row["member_j"]} == {"m0", "m1"}
    assert float(row["nmi"]) == pytest.approx(0.45)


def test_contradiction_regions_empty_when_all_above_threshold(tmp_path: Path) -> None:
    from _report import format_contradiction_regions  # type: ignore[import-not-found]

    bundle = _bundle(tmp_path)
    # All pairs > 0.45 threshold of 0.3 → empty result
    df = format_contradiction_regions(bundle, threshold=0.3)
    assert df.empty


# --------------------------------------------------------------------------- #
# write_artifacts (5-file orchestration + banner enforcement)                 #
# --------------------------------------------------------------------------- #

def test_write_artifacts_creates_all_5_files(tmp_path: Path) -> None:
    from _artifacts import write_artifacts  # type: ignore[import-not-found]

    out = tmp_path / "interp_out"
    de_df = pd.DataFrame([
        {"cluster": 0, "rank": 1, "gene": "Pvrl3", "score": 12.3, "pval_adj": 1e-50},
    ])
    audit = {"adata_checksum": "sha256:abc", "marker_db_source": "bundled:brain", "llm_model": "stubbed"}
    paths = write_artifacts(
        output_dir=out, bundle=_bundle(tmp_path), annotations=_annotations(),
        next_steps=_next_steps(), de_df=de_df, audit=audit,
        banner="[A+I: Interpreted on verified consensus]",
    )

    for fname in (
        "interpreted_report.md",
        "interpreted_assignments.json",
        "de_per_cluster.csv",
        "contradiction_regions.csv",
        "audit.json",
    ):
        assert (out / fname).exists(), f"missing artifact: {fname}"

    assert len(paths) == 5
    assert (out / "interpreted_report.md").read_text().splitlines()[0] == "[A+I: Interpreted on verified consensus]"


def test_write_artifacts_invariant_violation_blocks_writes(tmp_path: Path) -> None:
    """If invariants fail, NO artifacts are written (atomic-ish: enforce before write)."""
    from _errors import InvariantViolationError  # type: ignore[import-not-found]
    from _artifacts import write_artifacts  # type: ignore[import-not-found]
    from _llm import ClusterAnnotation  # type: ignore[import-not-found]

    bad_annotations = [
        ClusterAnnotation(cluster_id=0, n_cells=3, cell_type="CA1 pyramidal",
                          confidence=0.9, evidence_markers=[], narrative="bad."),
    ]
    out = tmp_path / "should_not_write"
    with pytest.raises(InvariantViolationError):
        write_artifacts(
            output_dir=out, bundle=_bundle(tmp_path), annotations=bad_annotations,
            next_steps=_next_steps(), de_df=pd.DataFrame(), audit={},
            banner="[A+I: Interpreted on verified consensus]",
        )
    assert not out.exists() or not list(out.iterdir()), "no artifacts should be written when invariants fail"
