"""Dual-track tests: LazySkillMetadata + registry prefer skill.yaml (v2) when
present, and fall back to v1 (frontmatter + parameters.yaml) otherwise (ADR 0037)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from omicsclaw.skill.lazy_metadata import LazySkillMetadata
from omicsclaw.skill.registry import OmicsRegistry
from omicsclaw.skill.schema import parse_skill_manifest


def _plain_registry_value(value):
    if isinstance(value, Mapping):
        return {key: _plain_registry_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_registry_value(item) for item in value]
    if isinstance(value, frozenset):
        return set(value)
    return value


def _v2_doc(**over) -> dict:
    data = {
        "schema_version": 2,
        "id": "spatial-demo",
        "name": "spatial-demo",
        "domain": "spatial",
        "version": "1.2.3",
        "author": "OmicsClaw",
        "license": "MIT",
        "summary": {
            "load_when": "demoing v2 wiring on a spatial AnnData",
            "skip_when": [{"condition": "single-cell data", "use": "sc-de"}],
            "trigger_keywords": ["demo", "v2 wiring"],
            "tags": ["spatial", "demo"],
            "aliases": ["spatial-demo-legacy"],
        },
        "interface": {
            "inputs": {"preconditions": {"data_shape": {"requires_preprocessed": True}}},
            "parameters": {"allowed_extra_flags": ["--resolution"], "hints": {"m": {"x": 1}}},
            "outputs": {
                "files": ["processed.h5ad", "result.json"],
                "result_json": {"required_keys": ["status"]},
                "anndata": {
                    "saves_h5ad": True,
                    "processing_state": "preprocessed",
                    "obsm": ["X_pca"],
                },
            },
        },
        "runtime": {"language": "python", "entry": "spatial_demo.py"},
        "deps": {"python": ["scanpy", "squidpy"]},
        "compatibility": {"platforms": ["linux", "macos"]},
        "validation": {"level": "fixture-validated"},
        "security": {
            "data_egress": "none",
            "network": "none",
            "writes": "output_dir_only",
        },
    }
    data.update(over)
    return data


def _write_v2(skill_dir: Path, doc: dict | None = None) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest = parse_skill_manifest(doc or _v2_doc())
    (skill_dir / "skill.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
    return skill_dir


def test_lazy_reads_all_fields_from_v2(tmp_path):
    sd = _write_v2(tmp_path / "spatial-demo")
    lazy = LazySkillMetadata(sd)
    assert lazy.source == "v2"
    assert lazy.name == "spatial-demo"
    assert lazy.requires == ["scanpy", "squidpy"]
    assert lazy.domain == "spatial"
    assert lazy.script == "spatial_demo.py"
    assert lazy.runtime_language == "python"
    assert lazy.type == "leaf"
    assert lazy.validation_level == "fixture-validated"
    assert lazy.trigger_keywords == ["demo", "v2 wiring"]
    assert lazy.allowed_extra_flags == {"--resolution"}
    assert lazy.legacy_aliases == ["spatial-demo-legacy"]
    assert lazy.saves_h5ad is True
    assert lazy.requires_preprocessed is True
    assert lazy.output_contract == {
        "files": ["processed.h5ad", "result.json"],
        "result_json": {"required_keys": ["status"]},
        "anndata": {
            "saves_h5ad": True,
            "processing_state": "preprocessed",
            "obs": [],
            "obsm": ["X_pca"],
            "var": [],
            "layers": [],
            "uns": [],
        },
    }
    assert lazy.param_hints == {"m": {"x": 1}}
    assert lazy.security_contract == {
        "data_egress": "none",
        "network": "none",
        "writes": "output_dir_only",
    }
    assert lazy.security_reviewed is True


def test_registry_preserves_declared_bash_runtime(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skill_dir = skills / "spatial" / "spatial-demo"
    _write_v2(
        skill_dir,
        _v2_doc(runtime={"language": "bash", "entry": "run.sh"}),
    )
    (skill_dir / "run.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
    )

    registry = OmicsRegistry()
    registry.load_all(skills)

    assert registry.skills["spatial-demo"]["runtime_language"] == "bash"
    assert registry.skills["spatial-demo"]["script"] == skill_dir / "run.sh"


def test_v2_description_reconstructed_canonically(tmp_path):
    sd = _write_v2(tmp_path / "spatial-demo")
    desc = LazySkillMetadata(sd).description
    assert desc.startswith("Load when demoing v2 wiring on a spatial AnnData.")
    assert "Skip when single-cell data (use sc-de)." in desc


def test_v2_artifact_contract_is_exposed_only_when_declared(tmp_path):
    doc = _v2_doc()
    doc["interface"]["inputs"]["artifacts"] = [
        {"kind": "genomics.variants", "formats": ["vcf"]}
    ]
    doc["interface"]["outputs"]["artifacts"] = [
        {"kind": "genomics.variants", "path": "processed.h5ad", "format": "h5ad"}
    ]
    lazy = LazySkillMetadata(_write_v2(tmp_path / "spatial-demo", doc))

    assert lazy.input_contract["artifacts"] == [
        {"kind": "genomics.variants", "formats": ["vcf"]}
    ]
    assert lazy.output_contract["artifacts"] == [
        {"kind": "genomics.variants", "path": "processed.h5ad", "format": "h5ad"}
    ]


def test_v2_compute_resources_reach_registry_snapshot(tmp_path):
    compute = {
        "cpu_cores": 2,
        "memory_mib": 4096,
        "gpu_devices": 0,
        "threads": 2,
        "temporary_disk_mib": 2048,
    }
    doc = _v2_doc(resources={"compute": compute})
    skills = tmp_path / "skills"
    skill_dir = _write_v2(skills / "spatial" / "spatial-demo", doc)
    (skill_dir / "spatial_demo.py").write_text(
        "def main(argv=None):\n    pass\n",
        encoding="utf-8",
    )

    lazy = LazySkillMetadata(skill_dir)
    registry = OmicsRegistry()
    registry.load_all(skills)

    assert lazy.compute_resources == compute
    assert registry.skills["spatial-demo"]["compute_resources"] == compute


def test_v2_wins_over_conflicting_v1(tmp_path):
    sd = _write_v2(tmp_path / "spatial-demo")
    (sd / "SKILL.md").write_text(
        "---\nname: WRONG\ndescription: Load when WRONG. Skip when x (use y).\n"
        "requires:\n- wrongpkg\n---\n# body\n",
        encoding="utf-8",
    )
    (sd / "parameters.yaml").write_text(
        "domain: genomics\nscript: wrong.py\ntrigger_keywords:\n- wrong\n", encoding="utf-8"
    )
    lazy = LazySkillMetadata(sd)
    assert lazy.source == "v2"
    assert lazy.name == "spatial-demo"          # not WRONG
    assert lazy.requires == ["scanpy", "squidpy"]  # not wrongpkg
    assert lazy.domain == "spatial"             # not genomics


def test_invalid_v2_falls_back_to_v1(tmp_path):
    sd = tmp_path / "spatial-demo"
    sd.mkdir(parents=True)
    (sd / "skill.yaml").write_text("schema_version: 2\nid: x\n", encoding="utf-8")  # invalid
    (sd / "SKILL.md").write_text(
        "---\nname: spatial-demo\ndescription: Load when on v1 path. Skip when x (use y).\n"
        "requires:\n- numpy\n---\n# body\n",
        encoding="utf-8",
    )
    (sd / "parameters.yaml").write_text(
        "domain: spatial\nscript: spatial_demo.py\ntrigger_keywords:\n- v1kw\n", encoding="utf-8"
    )
    lazy = LazySkillMetadata(sd)
    assert lazy.source == "v1"
    assert lazy.name == "spatial-demo"
    assert lazy.requires == ["numpy"]
    assert lazy.domain == "spatial"
    assert lazy.trigger_keywords == ["v1kw"]


def test_invalid_v2_fails_closed_in_strict_mode(tmp_path):
    sd = tmp_path / "spatial-demo"
    sd.mkdir(parents=True)
    (sd / "skill.yaml").write_text("schema_version: 2\nid: x\n", encoding="utf-8")
    (sd / "SKILL.md").write_text(
        "---\nname: spatial-demo\ndescription: legacy fallback must not mask invalid v2\n"
        "---\n# body\n",
        encoding="utf-8",
    )

    lazy = LazySkillMetadata(sd, strict_v2=True)
    with pytest.raises(ValueError):
        _ = lazy.name


def test_registry_load_all_consumes_v2(tmp_path):
    skills = tmp_path / "skills"
    sd = skills / "spatial" / "spatial-demo"
    _write_v2(sd)
    (sd / "spatial_demo.py").write_text("def main(argv=None):\n    pass\n", encoding="utf-8")

    reg = OmicsRegistry()
    reg.load_all(skills)

    assert "spatial-demo" in reg.skills
    info = reg.skills["spatial-demo"]
    assert info["domain"] == "spatial"
    assert _plain_registry_value(info["requires"]) == ["scanpy", "squidpy"]
    assert _plain_registry_value(info["trigger_keywords"]) == ["demo", "v2 wiring"]
    assert info["validation_level"] == "fixture-validated"
    assert info["origin"] == "human"
    assert info["lifecycle_status"] == "mvp"
    assert info["superseded_by"] == ""
    assert _plain_registry_value(info["skip_when"]) == [
        {"condition": "single-cell data", "use": "sc-de"}
    ]
    assert info["saves_h5ad"] is True
    assert _plain_registry_value(info["output_contract"]) == {
        "files": ["processed.h5ad", "result.json"],
        "result_json": {"required_keys": ["status"]},
        "anndata": {
            "saves_h5ad": True,
            "processing_state": "preprocessed",
            "obs": [],
            "obsm": ["X_pca"],
            "var": [],
            "layers": [],
            "uns": [],
        },
    }
    assert _plain_registry_value(info["security_contract"]) == {
        "data_egress": "none",
        "network": "none",
        "writes": "output_dir_only",
    }
    assert info["security_reviewed"] is True
    assert info["description"].startswith("Load when demoing v2 wiring")


def test_v1_output_contract_defaults_empty(tmp_path):
    sd = tmp_path / "legacy"
    sd.mkdir()
    (sd / "SKILL.md").write_text(
        "---\nname: legacy\ndescription: Load when testing. Skip when never.\n---\n",
        encoding="utf-8",
    )

    assert LazySkillMetadata(sd).output_contract == {}
    assert LazySkillMetadata(sd).security_contract == {}
    assert LazySkillMetadata(sd).security_reviewed is False


def test_registry_propagates_governance_fields(tmp_path):
    skills = tmp_path / "skills"
    sd = skills / "spatial" / "spatial-demo"
    _write_v2(
        sd,
        _v2_doc(
            lifecycle={"status": "deprecated", "superseded_by": "spatial-next"},
            provenance={"origin": "promoted"},
        ),
    )
    (sd / "spatial_demo.py").write_text("def main(argv=None):\n    pass\n", encoding="utf-8")
    replacement = skills / "spatial" / "spatial-next"
    _write_v2(
        replacement,
        _v2_doc(
            id="spatial-next",
            name="spatial-next",
            runtime={"language": "python", "entry": "spatial_next.py"},
        ),
    )
    (replacement / "spatial_next.py").write_text(
        "def main(argv=None):\n    pass\n",
        encoding="utf-8",
    )

    reg = OmicsRegistry()
    reg.load_all(skills)

    info = reg.skills["spatial-demo"]
    assert info["origin"] == "promoted"
    assert info["lifecycle_status"] == "deprecated"
    assert info["superseded_by"] == "spatial-next"


def test_registry_rejects_deprecated_skill_without_routable_replacement(tmp_path):
    skills = tmp_path / "skills"
    sd = skills / "spatial" / "spatial-demo"
    _write_v2(
        sd,
        _v2_doc(
            lifecycle={"status": "deprecated", "superseded_by": "missing-skill"},
        ),
    )
    (sd / "spatial_demo.py").write_text("def main(argv=None):\n    pass\n", encoding="utf-8")

    with pytest.raises(ValueError, match="routable replacement"):
        OmicsRegistry().load_all(skills)


def test_llm_facing_skill_enum_omits_draft_and_deprecated_entries():
    reg = OmicsRegistry()
    reg.canonical_aliases = ["draft-skill", "deprecated-skill", "stable-skill"]
    reg.skills = {
        "draft-skill": {"alias": "draft-skill", "lifecycle_status": "draft"},
        "deprecated-skill": {
            "alias": "deprecated-skill",
            "lifecycle_status": "deprecated",
            "superseded_by": "stable-skill",
        },
        "stable-skill": {"alias": "stable-skill", "lifecycle_status": "stable"},
    }
    reg._loaded = True

    assert reg.canonical_skill_aliases() == ["stable-skill"]
