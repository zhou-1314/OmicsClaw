"""Unit tests for the v2 declarative skill schema (ADR 0037)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omicsclaw.skill.schema import (
    SCHEMA_VERSION,
    load_skill_yaml,
    parse_skill_manifest,
    validate_skill_yaml,
)


def _minimal() -> dict:
    return {
        "schema_version": 2,
        "id": "spatial-de",
        "name": "spatial-de",
        "domain": "spatial",
        "version": "0.5.0",
        "summary": {"load_when": "ranking marker genes on a clustered spatial AnnData"},
        "runtime": {"language": "python", "entry": "spatial_de.py"},
    }


def test_minimal_manifest_parses_with_defaults():
    m = parse_skill_manifest(_minimal())
    assert m.type == "leaf"
    assert m.runtime.language == "python"
    assert m.validation.level == "smoke-only"
    assert m.lifecycle.status == "mvp"
    assert m.provenance.origin == "human"
    # Absence means unreviewed.  It must not silently become a false
    # ``network:none`` / ``output_dir_only`` security claim.
    assert m.security is None
    assert m.interface.inputs.path_kinds == ["file"]


def test_deprecated_lifecycle_requires_one_replacement_and_only_deprecated_uses_it():
    deprecated = _minimal()
    deprecated["lifecycle"] = {
        "status": "deprecated",
        "superseded_by": "spatial-next",
    }

    lifecycle = parse_skill_manifest(deprecated).lifecycle

    assert lifecycle.status == "deprecated"
    assert lifecycle.superseded_by == "spatial-next"

    missing_replacement = _minimal()
    missing_replacement["lifecycle"] = {"status": "deprecated"}
    with pytest.raises(ValidationError, match="superseded_by"):
        parse_skill_manifest(missing_replacement)

    active_with_replacement = _minimal()
    active_with_replacement["lifecycle"] = {
        "status": "stable",
        "superseded_by": "spatial-next",
    }
    with pytest.raises(ValidationError, match="only valid for deprecated"):
        parse_skill_manifest(active_with_replacement)

    self_replacement = _minimal()
    self_replacement["lifecycle"] = {
        "status": "deprecated",
        "superseded_by": self_replacement["id"],
    }
    with pytest.raises(ValidationError, match="cannot supersede itself"):
        parse_skill_manifest(self_replacement)


def test_security_review_must_be_explicit_and_complete():
    data = _minimal()
    data["security"] = {
        "data_egress": "optional",
        "network": "optional",
        "writes": "output_dir_only",
    }

    security = parse_skill_manifest(data).security
    assert security is not None
    assert security.model_dump() == data["security"]

    incomplete = _minimal()
    incomplete["security"] = {"network": "none"}
    with pytest.raises(ValidationError):
        parse_skill_manifest(incomplete)


def test_compute_resource_reservation_is_typed() -> None:
    data = _minimal()
    data["resources"] = {
        "compute": {
            "cpu_cores": 4,
            "memory_mib": 8192,
            "gpu_devices": 1,
            "threads": 4,
            "temporary_disk_mib": 16384,
        }
    }

    reservation = parse_skill_manifest(data).resources.compute

    assert reservation.model_dump() == {
        "cpu_cores": 4,
        "memory_mib": 8192,
        "gpu_devices": 1,
        "threads": 4,
        "temporary_disk_mib": 16384,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cpu_cores", 0),
        ("memory_mib", 0),
        ("gpu_devices", -1),
        ("threads", 0),
        ("temporary_disk_mib", -1),
        ("threads", True),
        ("memory_mib", "4096"),
        ("gpu_devices", 0.0),
    ],
)
def test_compute_resource_reservation_rejects_impossible_values(
    field: str,
    value: object,
) -> None:
    compute = {
        "cpu_cores": 2,
        "memory_mib": 4096,
        "gpu_devices": 0,
        "threads": 2,
        "temporary_disk_mib": 1024,
    }
    compute[field] = value
    data = _minimal()
    data["resources"] = {"compute": compute}

    with pytest.raises(ValidationError):
        parse_skill_manifest(data)

    compute[field] = 2
    compute["threads"] = 3
    data["resources"] = {"compute": compute}
    with pytest.raises(ValidationError, match="threads cannot exceed"):
        parse_skill_manifest(data)


def test_input_path_kinds_are_explicit_and_validated():
    data = _minimal()
    data["interface"] = {
        "inputs": {"path_kinds": ["file", "directory", "freeform"]}
    }
    assert parse_skill_manifest(data).interface.inputs.path_kinds == [
        "file",
        "directory",
        "freeform",
    ]

    data["interface"] = {"inputs": {"path_kinds": ["socket"]}}
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_tabular_content_preconditions_are_typed() -> None:
    data = _minimal()
    data["interface"] = {
        "inputs": {
            "file_types": ["csv"],
            "preconditions": {
                "content": {
                    "tabular": {
                        "min_columns": 3,
                        "required_columns": ["gene_id"],
                    }
                }
            },
        }
    }

    tabular = (
        parse_skill_manifest(data)
        .interface.inputs.preconditions.content.tabular
    )

    assert tabular.min_columns == 3
    assert tabular.required_columns == ["gene_id"]


def test_vcf_content_preconditions_are_typed() -> None:
    data = _minimal()
    data["interface"] = {
        "inputs": {
            "file_types": ["vcf"],
            "preconditions": {
                "content": {
                    "vcf": {
                        "require_fileformat_header": True,
                        "required_columns": ["#CHROM", "POS", "REF", "ALT"],
                        "required_info_ids": ["DP"],
                        "min_samples": 1,
                    }
                }
            },
        }
    }

    vcf = parse_skill_manifest(data).interface.inputs.preconditions.content.vcf

    assert vcf.require_fileformat_header is True
    assert vcf.required_columns == ["#CHROM", "POS", "REF", "ALT"]
    assert vcf.required_info_ids == ["DP"]
    assert vcf.min_samples == 1


def test_fastq_content_preconditions_are_typed() -> None:
    data = _minimal()
    data["interface"] = {
        "inputs": {
            "file_types": ["fastq"],
            "preconditions": {
                "content": {
                    "fastq": {
                        "require_valid_record": True,
                        "pairing": "paired",
                    }
                }
            },
        }
    }

    fastq = parse_skill_manifest(data).interface.inputs.preconditions.content.fastq

    assert fastq.require_valid_record is True
    assert fastq.pairing == "paired"


def test_directory_content_preconditions_use_governed_signatures() -> None:
    data = _minimal()
    data["interface"] = {
        "inputs": {
            "path_kinds": ["directory"],
            "preconditions": {
                "content": {
                    "directory": {
                        "any_of_signatures": ["paired-fastq", "tenx-matrix"]
                    }
                }
            },
        }
    }

    directory = (
        parse_skill_manifest(data)
        .interface.inputs.preconditions.content.directory
    )
    assert directory.any_of_signatures == ["paired-fastq", "tenx-matrix"]

    data["interface"]["inputs"]["preconditions"]["content"]["directory"] = {
        "any_of_signatures": ["invented-layout"]
    }
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_content_preconditions_must_have_a_matching_input_kind() -> None:
    data = _minimal()
    data["interface"] = {
        "inputs": {
            "file_types": ["csv"],
            "preconditions": {
                "content": {
                    "vcf": {"require_fileformat_header": True},
                }
            },
        }
    }
    with pytest.raises(ValidationError, match="VCF content probe"):
        parse_skill_manifest(data)

    data["interface"] = {
        "inputs": {
            "path_kinds": ["file"],
            "preconditions": {
                "content": {
                    "directory": {"any_of_signatures": ["tenx-matrix"]},
                }
            },
        }
    }
    with pytest.raises(ValidationError, match="directory content probe"):
        parse_skill_manifest(data)


def test_anndata_processing_state_is_explicit_and_validated():
    data = _minimal()
    data["interface"] = {
        "outputs": {
            "anndata": {
                "saves_h5ad": True,
                "processing_state": "preprocessed",
            }
        }
    }
    manifest = parse_skill_manifest(data)
    assert manifest.interface.outputs.anndata.processing_state == "preprocessed"

    data["interface"]["outputs"]["anndata"]["processing_state"] = "processed"
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_semantic_artifact_contracts_bind_output_types_to_declared_paths():
    data = _minimal()
    data["interface"] = {
        "inputs": {
            "artifacts": [
                {"kind": "genomics.variant_calls", "formats": ["vcf"]}
            ]
        },
        "outputs": {
            "files": ["tables/annotated_variants.csv"],
            "artifacts": [
                {
                    "kind": "genomics.annotated_variants",
                    "path": "tables/annotated_variants.csv",
                    "format": "csv",
                }
            ],
        },
    }

    manifest = parse_skill_manifest(data)

    assert manifest.interface.inputs.artifacts[0].kind == "genomics.variant_calls"
    assert manifest.interface.outputs.artifacts[0].path == "tables/annotated_variants.csv"

    data["interface"]["outputs"]["artifacts"][0]["path"] = "tables/not-declared.csv"
    with pytest.raises(ValidationError, match="must also appear in outputs.files"):
        parse_skill_manifest(data)


def test_output_contract_cannot_declare_internal_run_claim_as_artifact():
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    data = _minimal()
    data["interface"] = {
        "outputs": {
            "files": [OUTPUT_CLAIM_FILENAME],
            "artifacts": [
                {
                    "kind": "internal.claim",
                    "path": OUTPUT_CLAIM_FILENAME,
                    "format": "json",
                }
            ],
        }
    }

    with pytest.raises(ValidationError, match="reserved internal output"):
        parse_skill_manifest(data)


def test_output_files_reserve_internal_claim_across_path_separators():
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    for path in (
        OUTPUT_CLAIM_FILENAME,
        f"nested/{OUTPUT_CLAIM_FILENAME}",
        f"nested\\{OUTPUT_CLAIM_FILENAME}",
    ):
        data = _minimal()
        data["interface"] = {"outputs": {"files": [path]}}
        with pytest.raises(ValidationError, match="reserved internal output"):
            parse_skill_manifest(data)


def test_method_scoped_outputs_describe_conditional_files_anndata_and_artifacts():
    data = _minimal()
    data["interface"] = {
        "parameters": {"hints": {"scvelo_dynamical": {}}},
        "outputs": {
            "files": ["processed.h5ad", "figures/latent_time.png"],
            "anndata": {"saves_h5ad": True, "layers": ["velocity"]},
            "method_scopes": [
                {
                    "methods": ["scvelo_dynamical"],
                    "files": ["figures/latent_time.png"],
                    "anndata": {"obs": ["latent_time"]},
                    "artifacts": [
                        {
                            "kind": "singlecell.latent_time",
                            "path": "processed.h5ad",
                            "format": "h5ad",
                        }
                    ],
                }
            ],
        }
    }

    scope = parse_skill_manifest(data).interface.outputs.method_scopes[0]

    assert scope.methods == ["scvelo_dynamical"]
    assert scope.files == ["figures/latent_time.png"]
    assert scope.anndata.obs == ["latent_time"]
    assert scope.artifacts[0].kind == "singlecell.latent_time"


def test_method_scoped_outputs_cannot_invent_undeclared_paths():
    data = _minimal()
    data["interface"] = {
        "parameters": {"hints": {"dynamical": {}}},
        "outputs": {
            "files": ["processed.h5ad"],
            "anndata": {"saves_h5ad": True},
            "method_scopes": [
                {
                    "methods": ["dynamical"],
                    "files": ["figures/not-declared.png"],
                }
            ],
        }
    }

    with pytest.raises(ValidationError, match="method-scoped output paths"):
        parse_skill_manifest(data)


def test_method_scoped_outputs_reject_overlapping_method_guarantees():
    data = _minimal()
    data["interface"] = {
        "parameters": {"hints": {"method_a": {}}},
        "outputs": {
            "files": ["a.csv", "b.csv"],
            "method_scopes": [
                {"methods": ["method_a"], "files": ["a.csv"]},
                {"methods": ["method_a"], "files": ["b.csv"]},
            ],
        }
    }

    with pytest.raises(ValidationError, match="only one output scope"):
        parse_skill_manifest(data)


def test_method_scoped_outputs_require_canonical_method_ids():
    data = _minimal()
    data["interface"] = {
        "outputs": {
            "files": ["a.csv"],
            "method_scopes": [
                {"methods": ["Method A"], "files": ["a.csv"]},
            ],
        }
    }

    with pytest.raises(ValidationError, match="canonical method identifiers"):
        parse_skill_manifest(data)


def test_method_scoped_outputs_must_reference_declared_parameter_hint():
    data = _minimal()
    data["interface"] = {
        "parameters": {"hints": {"method_a": {}}},
        "outputs": {
            "files": ["a.csv"],
            "method_scopes": [
                {"methods": ["invented_method"], "files": ["a.csv"]},
            ],
        },
    }

    with pytest.raises(ValidationError, match="parameters.hints"):
        parse_skill_manifest(data)


def test_method_scoped_anndata_requires_a_declared_h5ad_output():
    data = _minimal()
    data["interface"] = {
        "parameters": {"hints": {"method_a": {}}},
        "outputs": {
            "files": ["report.csv"],
            "method_scopes": [
                {"methods": ["method_a"], "anndata": {"obs": ["latent_time"]}},
            ],
        },
    }

    with pytest.raises(ValidationError, match="saves_h5ad"):
        parse_skill_manifest(data)


def test_method_scoped_artifact_kinds_must_be_unique_across_all_outputs():
    data = _minimal()
    data["interface"] = {
        "parameters": {"hints": {"method_a": {}}},
        "outputs": {
            "files": ["global.csv", "scoped.csv"],
            "artifacts": [
                {"kind": "test.table", "path": "global.csv", "format": "csv"}
            ],
            "method_scopes": [
                {
                    "methods": ["method_a"],
                    "artifacts": [
                        {"kind": "test.table", "path": "scoped.csv", "format": "csv"}
                    ],
                }
            ],
        },
    }

    with pytest.raises(ValidationError, match="artifact kind"):
        parse_skill_manifest(data)


def test_method_scoped_outputs_cannot_declare_an_empty_guarantee():
    data = _minimal()
    data["interface"] = {
        "parameters": {"hints": {"method_a": {}}},
        "outputs": {"method_scopes": [{"methods": ["method_a"]}]},
    }

    with pytest.raises(ValidationError, match="at least one output guarantee"):
        parse_skill_manifest(data)


def test_reserved_flags_rejected():
    data = _minimal()
    data["interface"] = {"parameters": {"allowed_extra_flags": ["--species", "--input"]}}
    with pytest.raises(ValidationError) as ei:
        parse_skill_manifest(data)
    assert "reserved framework flags" in str(ei.value)


def test_deps_cli_rejects_interpreters():
    data = _minimal()
    data["deps"] = {"python": ["scanpy"], "cli": ["samtools", "python3"]}
    with pytest.raises(ValidationError) as ei:
        parse_skill_manifest(data)
    assert "real external binaries" in str(ei.value)


def test_unknown_top_level_key_forbidden():
    data = _minimal()
    data["bogus"] = 1
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_no_conda_bucket_in_deps():
    data = _minimal()
    data["deps"] = {"python": ["scanpy"], "conda": ["torch"]}
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_bad_enums_rejected():
    for path, bad in [
        ("type", "knowledge"),       # retired in v2
        ("version", None),
    ]:
        data = _minimal()
        data[path] = bad
        with pytest.raises(ValidationError):
            parse_skill_manifest(data)

    data = _minimal()
    data["validation"] = {"level": "gold"}
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)

    data = _minimal()
    data["runtime"] = {"language": "julia", "entry": "x.jl"}
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_schema_version_must_be_2():
    data = _minimal()
    data["schema_version"] = 1
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_runtime_language_accepts_bash():
    data = _minimal()
    data["runtime"] = {"language": "bash", "entry": "run.sh"}
    assert parse_skill_manifest(data).runtime.language == "bash"


def test_yaml_round_trip(tmp_path):
    data = _minimal()
    data["deps"] = {"python": ["scanpy", "squidpy"]}
    data["summary"]["skip_when"] = [
        {"condition": "single-cell data", "use": "sc-de", "rationale": "different modality"}
    ]
    m = parse_skill_manifest(data)
    p = tmp_path / "skill.yaml"
    p.write_text(m.to_yaml(), encoding="utf-8")
    assert validate_skill_yaml(p) == []
    reloaded = load_skill_yaml(p)
    assert reloaded.deps.python == ["scanpy", "squidpy"]
    assert reloaded.summary.skip_when[0].use == "sc-de"
    assert reloaded.schema_version == SCHEMA_VERSION


def test_validate_skill_yaml_reports_errors(tmp_path):
    p = tmp_path / "skill.yaml"
    p.write_text("schema_version: 2\nid: x\n", encoding="utf-8")  # missing required fields
    errs = validate_skill_yaml(p)
    assert errs and any("name" in e or "domain" in e or "version" in e for e in errs)


# ── degenerate / hardening cases (Codex cross-validation, 2026-06-30) ──────────
def test_schema_version_is_required():
    data = _minimal()
    del data["schema_version"]
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_empty_strings_rejected():
    for key in ("id", "name", "domain", "version"):
        data = _minimal()
        data[key] = ""
        with pytest.raises(ValidationError):
            parse_skill_manifest(data)
    data = _minimal()
    data["runtime"] = {"language": "python", "entry": ""}
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)
    data = _minimal()
    data["summary"] = {"load_when": ""}
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_unknown_domain_rejected():
    data = _minimal()
    data["domain"] = "transcriptomics"  # not one of the 8
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_non_kebab_flag_rejected():
    data = _minimal()
    data["interface"] = {"parameters": {"allowed_extra_flags": ["--Min_Genes"]}}
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_unknown_platform_and_arch_rejected():
    data = _minimal()
    data["compatibility"] = {"platforms": ["solaris"]}
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)
    data = _minimal()
    data["compatibility"] = {"architectures": ["ppc64"]}
    with pytest.raises(ValidationError):
        parse_skill_manifest(data)


def test_deps_cli_normalises_paths_and_versions():
    for bad in ["/usr/bin/python3", "python3.11", "Rscript.exe", "BASH"]:
        data = _minimal()
        data["deps"] = {"python": ["scanpy"], "cli": [bad]}
        with pytest.raises(ValidationError):
            parse_skill_manifest(data)
    # a real binary passes
    data = _minimal()
    data["deps"] = {"python": ["pysam"], "cli": ["samtools"]}
    assert parse_skill_manifest(data).deps.cli == ["samtools"]


def test_list_cleaning_dedupes_and_strips():
    data = _minimal()
    data["deps"] = {"python": ["scanpy", " scanpy ", "", "numpy"]}
    assert parse_skill_manifest(data).deps.python == ["scanpy", "numpy"]
