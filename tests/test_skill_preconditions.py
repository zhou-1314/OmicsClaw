from __future__ import annotations

from omicsclaw.skill.preconditions import (
    InputProfile,
    PreconditionStatus,
    evaluate_skill_preconditions,
    probe_input_profile,
)
from omicsclaw.skill.capability_resolver import resolve_capability


def test_sc_clustering_is_eligible_when_declared_input_contract_is_satisfied() -> None:
    assessment = evaluate_skill_preconditions(
        "sc-clustering",
        InputProfile(
            file_type="h5ad",
            modality="scrna",
            preprocessed=True,
            obsm={"X_pca"},
        ),
    )

    assert assessment.status is PreconditionStatus.ELIGIBLE
    assert assessment.evaluated is True
    assert assessment.execution_ready is True
    assert assessment.missing == []
    assert assessment.reasons == []


def test_sc_clustering_needs_preparation_when_pca_is_missing() -> None:
    assessment = evaluate_skill_preconditions(
        "sc-clustering",
        InputProfile(
            file_type="h5ad",
            modality="scrna",
            preprocessed=False,
            obsm=set(),
        ),
    )

    assert assessment.status is PreconditionStatus.NEEDS_PREPARATION
    assert assessment.execution_ready is False
    assert assessment.missing == ["preprocessed", "obsm.X_pca"]
    assert assessment.recommended_preparation == ["sc-preprocessing"]


def test_sc_clustering_is_blocked_for_incompatible_file_type_and_modality() -> None:
    assessment = evaluate_skill_preconditions(
        "sc-clustering",
        InputProfile(
            file_type="vcf",
            modality="genomics",
            preprocessed=True,
            obsm={"X_pca"},
        ),
    )

    assert assessment.status is PreconditionStatus.BLOCKED
    assert assessment.execution_ready is False
    assert assessment.missing == ["file_type", "modality"]
    assert "expected one of ['h5ad']" in assessment.reasons[0]
    assert "expected one of ['scrna']" in assessment.reasons[1]


def test_failed_h5ad_inspection_is_never_execution_ready() -> None:
    assessment = evaluate_skill_preconditions(
        "sc-ambient-removal",
        InputProfile(
            file_type="h5ad",
            inspection_error="invalid HDF5 signature",
        ),
    )

    assert assessment.status is PreconditionStatus.BLOCKED
    assert assessment.evaluated is True
    assert assessment.execution_ready is False
    assert "inspection" in assessment.missing
    assert "invalid HDF5 signature" in assessment.reasons[0]


def test_uninspected_required_identity_is_not_eligible() -> None:
    assessment = evaluate_skill_preconditions(
        "sc-ambient-removal",
        InputProfile(),
    )

    assert assessment.status is PreconditionStatus.NEEDS_PREPARATION
    assert assessment.evaluated is True
    assert assessment.execution_ready is False
    assert assessment.missing == ["file_type", "modality"]


def test_resolver_preserves_semantic_top1_but_marks_it_not_execution_ready() -> None:
    decision = resolve_capability(
        "cluster my scRNA-seq cells with Leiden",
        domain_hint="singlecell",
        input_profile=InputProfile(
            file_type="h5ad",
            modality="scrna",
            preprocessed=False,
            obsm=set(),
        ),
    )

    assert decision.chosen_skill == "sc-clustering"
    assert decision.precondition_status == "needs_preparation"
    assert decision.precondition_evaluated is True
    assert decision.execution_ready is False
    assert decision.missing_preconditions == ["preprocessed", "obsm.X_pca"]
    assert decision.recommended_preparation == ["sc-preprocessing"]


def test_resolver_readable_path_overrides_an_untrusted_profile(tmp_path, monkeypatch) -> None:
    import omicsclaw.skill.capability_resolver as resolver_module

    input_path = tmp_path / "observed.h5ad"
    input_path.touch()
    observed = InputProfile(
        file_type="h5ad",
        modality="scrna",
        preprocessed=False,
        obsm=set(),
    )
    monkeypatch.setattr(
        resolver_module,
        "probe_input_profile",
        lambda path: observed,
        raising=False,
    )

    decision = resolve_capability(
        "cluster my scRNA-seq cells with Leiden",
        file_path=str(input_path),
        input_profile=InputProfile(
            file_type="h5ad",
            modality="scrna",
            preprocessed=True,
            obsm={"X_pca"},
        ),
    )

    assert decision.chosen_skill == "sc-clustering"
    assert decision.precondition_status == "needs_preparation"
    assert decision.execution_ready is False


def test_probe_h5ad_reads_shape_contract_without_loading_matrix(tmp_path) -> None:
    import anndata as ad
    import numpy as np

    input_path = tmp_path / "preprocessed.h5ad"
    adata = ad.AnnData(np.ones((3, 2)))
    adata.obs["sample"] = ["a", "a", "b"]
    adata.var["symbol"] = ["G1", "G2"]
    adata.layers["counts"] = adata.X.copy()
    adata.obsm["X_pca"] = np.ones((3, 2))
    adata.uns["omicsclaw_input_contract"] = {
        "domain": "singlecell",
        "modality": "scrna",
    }
    adata.uns["omicsclaw_matrix_contract"] = {
        "X": "normalized_expression",
    }
    adata.write_h5ad(input_path)

    profile = probe_input_profile(input_path)

    assert profile.file_type == "h5ad"
    assert profile.modality == "scrna"
    assert profile.preprocessed is True
    assert profile.obs == {"sample"}
    assert profile.var == {"symbol"}
    assert profile.layers == {"counts"}
    assert profile.obsm == {"X_pca"}
    assert "omicsclaw_matrix_contract" in (profile.uns or set())
    assert profile.inspection_error == ""


def test_h5ad_probe_cache_invalidates_when_file_identity_changes(tmp_path, monkeypatch) -> None:
    import omicsclaw.skill.preconditions as preconditions

    input_path = tmp_path / "cached.h5ad"
    input_path.write_bytes(b"first")
    calls = {"count": 0}

    def fake_read(path: str) -> dict:
        calls["count"] += 1
        return {"obs": {f"read_{calls['count']}"}, "inspection_error": ""}

    preconditions._cached_h5ad_profile.cache_clear()
    monkeypatch.setattr(preconditions, "_read_h5ad_profile", fake_read)

    first = probe_input_profile(input_path)
    second = probe_input_profile(input_path)
    input_path.write_bytes(b"second-version")
    third = probe_input_profile(input_path)

    assert first.obs == second.obs == {"read_1"}
    assert third.obs == {"read_2"}
    assert calls["count"] == 2


def test_probe_uses_the_semantic_extension_not_all_dotted_name_segments(tmp_path) -> None:
    input_path = tmp_path / "patient.v1.h5ad"
    input_path.write_bytes(b"not-an-h5ad")

    profile = probe_input_profile(input_path)

    assert profile.file_type == "h5ad"
    assert profile.inspection_error


def test_probe_normalises_compressed_omics_files_to_the_declared_type(tmp_path) -> None:
    input_path = tmp_path / "reads.fastq.gz"
    input_path.touch()

    profile = probe_input_profile(input_path)

    assert profile.file_type == "fastq"


def test_generic_data_shape_env_and_config_contracts_are_evaluated() -> None:
    from omicsclaw.skill.registry import OmicsRegistry

    registry = OmicsRegistry()
    registry.skills = {
        "contract-skill": {
            "alias": "contract-skill",
            "domain": "proteomics",
            "input_contract": {
                "modalities": ["ms"],
                "file_types": ["csv"],
                "preconditions": {
                    "data_shape": {
                        "obs": ["sample"],
                        "var": ["protein_id"],
                        "layers": ["intensity"],
                        "uns": ["normalization"],
                    },
                    "env": ["PROTEOMICS_HOME"],
                    "config": ["instrument_profile"],
                },
            },
        }
    }

    assessment = evaluate_skill_preconditions(
        "contract-skill",
        InputProfile(
            file_type="csv",
            modality="ms",
            obs={"sample"},
            var={"protein_id"},
            layers={"intensity"},
            uns={"normalization"},
            env={"PROTEOMICS_HOME"},
            config=set(),
        ),
        registry=registry,
    )

    assert assessment.status is PreconditionStatus.BLOCKED
    assert assessment.missing == ["config.instrument_profile"]
    assert assessment.recommended_preparation == []


def test_spatial_domains_does_not_block_an_auto_computable_pca() -> None:
    assessment = evaluate_skill_preconditions(
        "spatial-domains",
        InputProfile(
            file_type="h5ad",
            modality="spatial",
            preprocessed=False,
            obsm={"spatial"},
        ),
    )

    assert assessment.status is PreconditionStatus.ELIGIBLE
    assert assessment.execution_ready is True
    assert "obsm.X_pca" not in assessment.missing
