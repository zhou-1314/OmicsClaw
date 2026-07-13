from __future__ import annotations

from omicsclaw.skill.preconditions import (
    InputProfile,
    PreconditionStatus,
    evaluate_skill_preconditions,
    preflight_skill_execution,
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


def test_preparation_recommendation_never_points_to_the_blocked_skill_itself() -> None:
    assessment = evaluate_skill_preconditions(
        "spatial-preprocess",
        InputProfile(
            file_type="h5ad",
            modality="visium",
            obsm=set(),
        ),
    )

    assert assessment.execution_ready is False
    assert "obsm.spatial" in assessment.missing
    assert "spatial-preprocess" not in assessment.recommended_preparation


def test_multi_input_execution_gate_fails_when_any_local_input_is_missing(
    tmp_path,
) -> None:
    import anndata as ad
    import numpy as np

    from omicsclaw.skill.registry import OmicsRegistry

    valid = tmp_path / "valid.h5ad"
    missing = tmp_path / "missing.h5ad"
    ad.AnnData(np.ones((2, 2))).write_h5ad(valid)
    registry = OmicsRegistry()
    registry.skills = {
        "multi": {
            "alias": "multi",
            "domain": "singlecell",
            "input_contract": {
                "modalities": ["scrna"],
                "file_types": [],
                "preconditions": {},
            },
        }
    }

    assessment = preflight_skill_execution(
        "multi",
        input_paths=[str(valid), str(missing)],
        registry=registry,
    )

    assert assessment is not None
    assert assessment.execution_ready is False
    assert assessment.status is PreconditionStatus.BLOCKED
    assert "input[2].inspection" in assessment.missing


def test_non_h5ad_execution_still_enforces_observable_env_and_config(tmp_path) -> None:
    from omicsclaw.skill.registry import OmicsRegistry

    input_path = tmp_path / "proteins.csv"
    input_path.write_text("protein_id,intensity\nP1,1.0\n", encoding="utf-8")
    registry = OmicsRegistry()
    registry.skills = {
        "contract-skill": {
            "alias": "contract-skill",
            "domain": "proteomics",
            "input_contract": {
                "modalities": ["proteomics"],
                "file_types": ["csv"],
                "preconditions": {
                    "data_shape": {"obs": ["sample_id"]},
                    "env": ["OMICSCLAW_TEST_MISSING_ENV"],
                    "config": ["instrument_profile"],
                },
            },
        }
    }

    assessment = preflight_skill_execution(
        "contract-skill",
        input_path=str(input_path),
        registry=registry,
    )

    assert assessment is not None
    assert assessment.status is PreconditionStatus.BLOCKED
    assert assessment.missing == [
        "env.OMICSCLAW_TEST_MISSING_ENV",
        "config.instrument_profile",
    ]
    assert "obs.sample_id" not in assessment.missing


def test_literature_free_text_ending_in_a_known_suffix_is_not_a_local_path() -> None:
    for text in (
        "Review the supplementary data.csv",
        "Summarize this study about processed.h5ad",
    ):
        assessment = preflight_skill_execution("literature", input_path=text)
        assert assessment is None


def test_declared_zarr_directory_is_compatible_for_routing_and_execution(
    tmp_path,
) -> None:
    from omicsclaw.skill.registry import OmicsRegistry

    input_dir = tmp_path / "xenium.zarr"
    input_dir.mkdir()
    registry = OmicsRegistry()
    registry.skills = {
        "xenium-skill": {
            "alias": "xenium-skill",
            "domain": "spatial",
            "input_contract": {
                "modalities": [],
                "file_types": ["h5ad", "zarr"],
                "path_kinds": ["file", "directory"],
                "preconditions": {},
            },
        }
    }

    profile = probe_input_profile(input_dir)
    routing_assessment = evaluate_skill_preconditions(
        "xenium-skill",
        profile,
        registry=registry,
    )
    execution_assessment = preflight_skill_execution(
        "xenium-skill",
        input_path=str(input_dir),
        registry=registry,
    )

    assert profile.file_type == "zarr"
    assert routing_assessment.execution_ready is True
    assert execution_assessment is not None
    assert execution_assessment.execution_ready is True


def test_dotted_directory_name_is_not_a_file_type_for_either_gate(tmp_path) -> None:
    from omicsclaw.skill.registry import OmicsRegistry

    input_dir = tmp_path / "tenx_matrix.v1"
    input_dir.mkdir()
    registry = OmicsRegistry()
    registry.skills = {
        "directory-skill": {
            "alias": "directory-skill",
            "domain": "singlecell",
            "input_contract": {
                "modalities": [],
                "file_types": ["h5ad"],
                "path_kinds": ["file", "directory"],
                "preconditions": {},
            },
        }
    }

    profile = probe_input_profile(input_dir)
    routing_assessment = evaluate_skill_preconditions(
        "directory-skill",
        profile,
        registry=registry,
    )
    execution_assessment = preflight_skill_execution(
        "directory-skill",
        input_path=str(input_dir),
        registry=registry,
    )

    assert profile.path_kind == "directory"
    assert profile.file_type == ""
    assert routing_assessment.status is not PreconditionStatus.BLOCKED
    assert execution_assessment is not None
    assert execution_assessment.execution_ready is True


def test_zarr_directory_remains_incompatible_when_not_declared(tmp_path) -> None:
    from omicsclaw.skill.registry import OmicsRegistry

    input_dir = tmp_path / "xenium.zarr"
    input_dir.mkdir()
    registry = OmicsRegistry()
    registry.skills = {
        "tenx-only-skill": {
            "alias": "tenx-only-skill",
            "domain": "singlecell",
            "input_contract": {
                "modalities": [],
                "file_types": ["h5ad", "h5"],
                "path_kinds": ["file", "directory"],
                "preconditions": {},
            },
        }
    }

    profile = probe_input_profile(input_dir)
    routing_assessment = evaluate_skill_preconditions(
        "tenx-only-skill",
        profile,
        registry=registry,
    )
    execution_assessment = preflight_skill_execution(
        "tenx-only-skill",
        input_path=str(input_dir),
        registry=registry,
    )

    assert profile.path_kind == "directory"
    assert profile.file_type == "zarr"
    assert routing_assessment.status is PreconditionStatus.BLOCKED
    assert execution_assessment is not None
    assert execution_assessment.status is PreconditionStatus.BLOCKED
