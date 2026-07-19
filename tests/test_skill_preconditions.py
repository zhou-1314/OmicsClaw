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


def test_tabular_probe_blocks_a_declared_missing_column(tmp_path) -> None:
    from omicsclaw.skill.registry import OmicsRegistry

    input_path = tmp_path / "counts.csv"
    input_path.write_text("gene_id,ctrl_1\nG1,4\n", encoding="utf-8")
    registry = OmicsRegistry()
    registry.skills = {
        "tabular-skill": {
            "alias": "tabular-skill",
            "domain": "bulkrna",
            "input_contract": {
                "modalities": [],
                "file_types": ["csv"],
                "path_kinds": ["file"],
                "preconditions": {
                    "content": {
                        "tabular": {
                            "min_columns": 3,
                            "required_columns": ["gene_id", "treat_1"],
                        }
                    }
                },
            },
        }
    }

    profile = probe_input_profile(input_path)
    assessment = evaluate_skill_preconditions(
        "tabular-skill",
        profile,
        registry=registry,
        require_verified_modality=False,
    )

    assert profile.table_columns == {"gene_id", "ctrl_1"}
    assert profile.table_column_count == 2
    assert assessment.status is PreconditionStatus.BLOCKED
    assert assessment.missing == [
        "content.tabular.min_columns",
        "content.tabular.column.treat_1",
    ]


def test_compressed_vcf_probe_reads_header_facts_without_scanning_records(
    tmp_path,
) -> None:
    import gzip

    from omicsclaw.skill.registry import OmicsRegistry

    input_path = tmp_path / "variants.vcf.gz"
    with gzip.open(input_path, "wt", encoding="utf-8") as handle:
        handle.write(
            "##fileformat=VCFv4.2\n"
            "##INFO=<ID=DP,Number=1,Type=Integer,Description=\"Depth\">\n"
            "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\n"
            "chr1\t1\t.\tA\tG\t30\tPASS\tDP=9\tGT\t0/1\n"
        )
    registry = OmicsRegistry()
    registry.skills = {
        "vcf-skill": {
            "alias": "vcf-skill",
            "domain": "genomics",
            "input_contract": {
                "modalities": [],
                "file_types": ["vcf"],
                "path_kinds": ["file"],
                "preconditions": {
                    "content": {
                        "vcf": {
                            "require_fileformat_header": True,
                            "required_columns": ["#CHROM", "POS", "REF", "ALT"],
                            "required_info_ids": ["DP"],
                            "required_format_ids": ["GT"],
                            "min_samples": 2,
                        }
                    }
                },
            },
        }
    }

    profile = probe_input_profile(input_path)
    assessment = evaluate_skill_preconditions(
        "vcf-skill",
        profile,
        registry=registry,
        require_verified_modality=False,
    )

    assert profile.vcf_fileformat == "VCFv4.2"
    assert profile.vcf_columns == {
        "#CHROM",
        "POS",
        "ID",
        "REF",
        "ALT",
        "QUAL",
        "FILTER",
        "INFO",
        "FORMAT",
        "S1",
    }
    assert profile.vcf_info_ids == {"DP"}
    assert profile.vcf_format_ids == {"GT"}
    assert profile.vcf_sample_count == 1
    assert assessment.status is PreconditionStatus.BLOCKED
    assert assessment.missing == ["content.vcf.min_samples"]


def test_fastq_probe_validates_first_record_and_declared_pairing(tmp_path) -> None:
    from omicsclaw.skill.registry import OmicsRegistry

    input_path = tmp_path / "sample_R1.fastq"
    input_path.write_text("@read1\nACGT\n+\nFFFF\n", encoding="utf-8")
    registry = OmicsRegistry()
    registry.skills = {
        "paired-fastq-skill": {
            "alias": "paired-fastq-skill",
            "domain": "spatial",
            "input_contract": {
                "modalities": [],
                "file_types": ["fastq"],
                "path_kinds": ["file"],
                "preconditions": {
                    "content": {
                        "fastq": {
                            "require_valid_record": True,
                            "pairing": "paired",
                        }
                    }
                },
            },
        }
    }

    profile = probe_input_profile(input_path)
    assessment = evaluate_skill_preconditions(
        "paired-fastq-skill",
        profile,
        registry=registry,
        require_verified_modality=False,
    )

    assert profile.fastq_record_valid is True
    assert profile.fastq_pairing == "single"
    assert assessment.status is PreconditionStatus.BLOCKED
    assert assessment.missing == ["content.fastq.pairing"]


def test_directory_probe_matches_a_declared_semantic_signature(tmp_path) -> None:
    from omicsclaw.skill.registry import OmicsRegistry

    input_dir = tmp_path / "filtered_feature_bc_matrix"
    input_dir.mkdir()
    for name in ("matrix.mtx.gz", "barcodes.tsv.gz", "features.tsv.gz"):
        (input_dir / name).touch()
    registry = OmicsRegistry()
    registry.skills = {
        "count-import": {
            "alias": "count-import",
            "domain": "singlecell",
            "input_contract": {
                "modalities": [],
                "file_types": [],
                "path_kinds": ["directory"],
                "preconditions": {
                    "content": {
                        "directory": {
                            "any_of_signatures": ["paired-fastq", "tenx-matrix"]
                        }
                    }
                },
            },
        }
    }

    profile = probe_input_profile(input_dir)
    assessment = evaluate_skill_preconditions(
        "count-import",
        profile,
        registry=registry,
        require_verified_modality=False,
        require_verified_file_type=False,
    )

    assert profile.directory_signatures == {"tenx-matrix"}
    assert assessment.status is PreconditionStatus.ELIGIBLE
    assert assessment.execution_ready is True


def test_execution_gate_accepts_an_explicit_fastq_mate_path(tmp_path) -> None:
    from omicsclaw.skill.registry import OmicsRegistry

    read1 = tmp_path / "read1" / "sample_R1.fastq"
    read2 = tmp_path / "read2" / "sample_R2.fastq"
    read1.parent.mkdir()
    read2.parent.mkdir()
    for path in (read1, read2):
        path.write_text("@read\nACGT\n+\nFFFF\n", encoding="utf-8")
    registry = OmicsRegistry()
    registry.skills = {
        "paired-fastq-skill": {
            "alias": "paired-fastq-skill",
            "domain": "spatial",
            "input_contract": {
                "modalities": [],
                "file_types": ["fastq"],
                "path_kinds": ["file"],
                "preconditions": {
                    "content": {
                        "fastq": {
                            "require_valid_record": True,
                            "pairing": "paired",
                        }
                    }
                },
            },
        }
    }

    assessment = preflight_skill_execution(
        "paired-fastq-skill",
        input_path=str(read1),
        companion_paths=[str(read2)],
        registry=registry,
    )

    assert assessment is not None
    assert assessment.status is PreconditionStatus.ELIGIBLE
    assert assessment.execution_ready is True


def test_real_non_h5ad_contracts_block_structurally_invalid_inputs(tmp_path) -> None:
    short_counts = tmp_path / "counts.csv"
    short_counts.write_text("gene_id,ctrl_1\nG1,1\n", encoding="utf-8")
    malformed_vcf = tmp_path / "variants.vcf"
    malformed_vcf.write_text("chr1\t1\t.\tA\tG\n", encoding="utf-8")
    empty_fastq_dir = tmp_path / "fastqs"
    empty_fastq_dir.mkdir()

    assessments = [
        preflight_skill_execution("bulkrna-de", input_path=str(short_counts)),
        preflight_skill_execution(
            "genomics-vcf-operations",
            input_path=str(malformed_vcf),
        ),
        preflight_skill_execution("sc-fastq-qc", input_path=str(empty_fastq_dir)),
    ]

    assert all(assessment is not None for assessment in assessments)
    assert [assessment.status for assessment in assessments] == [
        PreconditionStatus.BLOCKED,
        PreconditionStatus.BLOCKED,
        PreconditionStatus.BLOCKED,
    ]
    assert assessments[0].missing == ["content.tabular.min_columns"]
    assert assessments[1].missing == ["content.vcf.inspection"]
    assert assessments[2].missing == ["content.directory.signature"]


def test_vcf_execution_accepts_gzip_and_always_materialises_declared_artifact(
    tmp_path,
) -> None:
    import gzip

    from omicsclaw.skill.runner import run_skill

    input_path = tmp_path / "variants.vcf.gz"
    with gzip.open(input_path, "wt", encoding="utf-8") as handle:
        handle.write(
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "chr1\t1\t.\tA\tG\t30\tPASS\tDP=9\n"
        )
    output_dir = tmp_path / "output"

    result = run_skill(
        "genomics-vcf-operations",
        input_path=str(input_path),
        output_dir=str(output_dir),
    )

    assert result.success is True, result.stderr
    assert (output_dir / "tables" / "variants.csv").exists()
    assert (output_dir / "filtered.vcf").exists()


def test_starsolo_velocity_directory_signature_satisfies_real_contract(
    tmp_path,
) -> None:
    velocity_dir = tmp_path / "Solo.out" / "Velocyto" / "raw"
    velocity_dir.mkdir(parents=True)
    for name in (
        "spliced.mtx",
        "unspliced.mtx",
        "barcodes.tsv",
        "features.tsv",
    ):
        (velocity_dir / name).touch()

    profile = probe_input_profile(tmp_path)
    assessment = preflight_skill_execution(
        "sc-velocity-prep",
        input_path=str(tmp_path),
    )

    assert profile.directory_signatures == {
        "starsolo-output",
        "starsolo-velocity",
    }
    assert assessment is not None
    assert assessment.status is PreconditionStatus.ELIGIBLE


def test_truncated_directory_probe_cannot_hard_prove_a_signature_is_absent(
    tmp_path,
) -> None:
    from omicsclaw.skill.registry import OmicsRegistry

    for index in range(2050):
        (tmp_path / f"filler-{index:04d}.txt").touch()
    registry = OmicsRegistry()
    registry.skills = {
        "directory-skill": {
            "alias": "directory-skill",
            "domain": "singlecell",
            "input_contract": {
                "modalities": [],
                "file_types": [],
                "path_kinds": ["directory"],
                "preconditions": {
                    "content": {
                        "directory": {
                            "any_of_signatures": ["tenx-matrix"],
                        }
                    }
                },
            },
        }
    }

    profile = probe_input_profile(tmp_path)
    assessment = evaluate_skill_preconditions(
        "directory-skill",
        profile,
        registry=registry,
        require_verified_modality=False,
        require_verified_file_type=False,
    )

    assert profile.directory_probe_truncated is True
    assert assessment.status is PreconditionStatus.NEEDS_PREPARATION
    assert assessment.execution_ready is False
    assert assessment.missing == ["content.directory.signature"]
