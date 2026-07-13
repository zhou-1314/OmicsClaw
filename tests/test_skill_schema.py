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
    # iron-rule defaults are explicit and safe
    assert m.security.data_egress == "none"
    assert m.security.network == "none"
    assert m.security.writes == "output_dir_only"
    assert m.interface.inputs.path_kinds == ["file"]


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
