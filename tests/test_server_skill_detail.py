"""Skill-detail metadata helpers (desktop server).

These pure functions read the richer per-skill metadata that lives on disk
(SKILL.md frontmatter + body, references/, parameters.yaml) but is not part of
the in-memory registry ``info`` dict — they back the enriched ``GET /skills``
and ``GET /skills/{domain}/{name}`` responses. Tested against a real, complete
builtin skill (``spatial-preprocess``) so the parse + classification paths run
end-to-end.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from omicsclaw.skill.registry import SKILLS_DIR

SKILL_DIR = SKILLS_DIR / "spatial" / "spatial-preprocess"
SCRIPT = SKILL_DIR / "spatial_preprocess.py"


def _server():
    from omicsclaw.surfaces.desktop import server

    return server


def _v1_skill_dir(tmp_path):
    """A synthetic v1 skill (SKILL.md frontmatter + parameters.yaml, no skill.yaml).

    Built fresh so the v1-path assertions stay valid as live skills migrate to v2.
    """
    sd = tmp_path / "v1-skill"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\n"
        "name: v1-skill\n"
        "version: 0.6.0\n"
        "author: OmicsClaw\n"
        "license: MIT\n"
        "tags: [spatial, qc]\n"
        "requires: [scanpy, numpy]\n"
        "description: Load when X. Skip when Y (use z).\n"
        "---\n"
        "# v1-skill\n",
        encoding="utf-8",
    )
    (sd / "parameters.yaml").write_text(
        "domain: spatial\nallowed_extra_flags: []\nparam_hints: {}\n", encoding="utf-8"
    )
    (sd / "v1_skill.py").write_text("def main(argv=None):\n    pass\n", encoding="utf-8")
    return sd


def test_skill_metadata_v1_reads_frontmatter(tmp_path):
    # v1 path → metadata comes from SKILL.md frontmatter (dual-track reader).
    m = _server()._skill_metadata(_v1_skill_dir(tmp_path))
    assert m is not None
    assert m.source == "v1"
    assert m.version == "0.6.0"
    assert m.author == "OmicsClaw"
    assert m.license == "MIT"
    assert "spatial" in m.tags
    assert "scanpy" in m.requires


def test_skill_metadata_none_for_missing_dir():
    s = _server()
    assert s._skill_metadata(None) is None


def test_skill_metadata_nonexistent_dir_degrades_to_defaults():
    # A registry entry can point at a directory missing on disk; property access
    # must degrade to empty defaults, not raise (so get_skill stays robust).
    m = _server()._skill_metadata(SKILLS_DIR / "does-not-exist-xyz")
    assert m is not None
    assert m.version == ""
    assert m.author == ""
    assert m.license == ""
    assert m.tags == []
    assert m.requires == []


def test_skill_source_builtin_vs_user(tmp_path):
    s = _server()
    assert s._skill_source(SCRIPT) == "builtin"
    assert s._skill_source(None) == "user"
    assert s._skill_source(tmp_path / "foo.py") == "user"


def test_skill_health_logic():
    s = _server()
    assert s._skill_health("ready", skill_md_exists=True, script_exists=True) == "healthy"
    assert s._skill_health("planned", skill_md_exists=True, script_exists=True) == "degraded"
    assert s._skill_health("ready", skill_md_exists=False, script_exists=True) == "degraded"
    assert s._skill_health("ready", skill_md_exists=True, script_exists=False) == "degraded"


def test_skill_resources_lists_script_doc_config_and_references():
    s = _server()
    res = s._skill_resources(SKILL_DIR, SCRIPT)
    by_path = {r["path"]: r["kind"] for r in res}
    assert by_path.get("spatial_preprocess.py") == "script"
    assert by_path.get("SKILL.md") == "doc"
    # config contract: skill.yaml (v2) or parameters.yaml (v1) — the live skill
    # may be on either track as the migration proceeds.
    assert by_path.get("skill.yaml") == "config" or by_path.get("parameters.yaml") == "config"
    assert by_path.get("references/methodology.md") == "reference"
    assert s._skill_resources(None, None) == []


def test_skill_references_titles_from_first_heading():
    s = _server()
    refs = s._skill_references(SKILL_DIR)
    by_path = {r["path"]: r["title"] for r in refs}
    assert by_path.get("references/methodology.md") == "Core Capabilities"
    assert refs and all(r["title"] and r["path"].startswith("references/") for r in refs)
    assert s._skill_references(SKILL_DIR / "no-refs") == []


def test_skill_diagnostics_pass_for_complete_skill():
    s = _server()
    by_label = {c["label"]: c["status"] for c in s._skill_diagnostics(SKILL_DIR, SCRIPT, "0.6.0")}
    assert by_label.get("SKILL.md") == "pass"
    assert by_label.get("entry script") == "pass"
    assert by_label.get("version declared") == "pass"
    # Runtime contract present — skill.yaml (v2) or parameters.yaml (v1); the
    # live skill may be on either track as the migration proceeds.
    assert by_label.get("skill.yaml") == "pass" or by_label.get("parameters.yaml") == "pass"
    assert by_label.get("references") == "pass"
    assert s._skill_diagnostics(None, None, None) == []


def test_skill_diagnostics_warns_when_version_absent():
    """A complete skill with NO declared version → the (warn_only) 'version
    declared' check is 'warn', not 'fail' — version is advisory."""
    s = _server()
    by_label = {c["label"]: c["status"] for c in s._skill_diagnostics(SKILL_DIR, SCRIPT, None)}
    assert by_label.get("version declared") == "warn"
    # required checks (SKILL.md + entry script) still pass for a complete skill.
    assert by_label.get("SKILL.md") == "pass"
    assert by_label.get("entry script") == "pass"


def _v2_skill_dir(tmp_path):
    """Build a complete v2 (skill.yaml) skill directory and return (dir, script)."""
    from omicsclaw.skill.schema import parse_skill_manifest

    doc = {
        "schema_version": 2,
        "id": "spatial-demo",
        "name": "spatial-demo",
        "domain": "spatial",
        "version": "2.1.0",
        "author": "OmicsClaw",
        "license": "MIT",
        "summary": {
            "load_when": "demoing v2 desktop detail",
            "skip_when": [{"condition": "single-cell data", "use": "sc-de"}],
            "tags": ["spatial", "demo"],
        },
        "runtime": {"language": "python", "entry": "spatial_demo.py"},
        "deps": {"python": ["scanpy", "numpy"]},
        "security": {
            "data_egress": "none",
            "network": "none",
            "writes": "output_dir_only",
        },
    }
    sd = tmp_path / "spatial-demo"
    sd.mkdir(parents=True)
    (sd / "skill.yaml").write_text(parse_skill_manifest(doc).to_yaml(), encoding="utf-8")
    script = sd / "spatial_demo.py"
    script.write_text("def main(argv=None):\n    pass\n", encoding="utf-8")
    (sd / "SKILL.md").write_text("---\nname: spatial-demo\n---\n# spatial-demo\n", encoding="utf-8")
    return sd, script


def test_skill_metadata_v2_reads_skill_yaml(tmp_path):
    sd, _ = _v2_skill_dir(tmp_path)
    m = _server()._skill_metadata(sd)
    assert m is not None
    assert m.source == "v2"
    assert m.version == "2.1.0"
    assert m.author == "OmicsClaw"
    assert m.license == "MIT"
    assert "demo" in m.tags
    # v2 `requires` is the deps.python surface.
    assert "scanpy" in m.requires


def test_skill_diagnostics_v2_contract_is_skill_yaml(tmp_path):
    sd, script = _v2_skill_dir(tmp_path)
    by_label = {c["label"]: c["status"] for c in _server()._skill_diagnostics(sd, script, "2.1.0")}
    assert by_label.get("skill.yaml") == "pass"          # v2 contract label
    assert "parameters.yaml" not in by_label             # not a v1 sidecar
    assert by_label.get("version declared") == "pass"


def test_skill_resources_v2_lists_skill_yaml(tmp_path):
    sd, script = _v2_skill_dir(tmp_path)
    by_path = {r["path"]: r["kind"] for r in _server()._skill_resources(sd, script)}
    assert by_path.get("skill.yaml") == "config"
    assert "parameters.yaml" not in by_path


@pytest.mark.asyncio
async def test_get_skill_endpoint_v2(tmp_path, monkeypatch):
    """GET /skills/{domain}/{name} sources detail from skill.yaml for a v2 skill."""
    from types import SimpleNamespace

    s = _server()
    sd, script = _v2_skill_dir(tmp_path)
    fake_core = SimpleNamespace(
        _skill_registry=lambda: SimpleNamespace(
            skills={
                "spatial-demo": {
                    "alias": "spatial-demo",
                    "domain": "spatial",
                    "description": "Spatial demo",
                    "script": str(script),
                }
            }
        ),
    )
    monkeypatch.setattr(s, "_core", fake_core, raising=False)

    payload = await s.get_skill("spatial", "spatial-demo")
    assert payload["version"] == "2.1.0"
    assert payload["validation_level"] == "smoke-only"
    assert payload["superseded_by"] is None
    assert payload["readiness"] == "healthy"
    assert payload["author"] == "OmicsClaw"
    assert payload["license"] == "MIT"
    assert "demo" in payload["tags"]
    assert "scanpy" in payload["requires"]
    assert payload["security"] == {
        "reviewed": True,
        "enforcement": "declarative",
        "data_egress": "none",
        "network": "none",
        "writes": "output_dir_only",
    }
    assert any(c["label"] == "skill.yaml" and c["status"] == "pass" for c in payload["diagnostics"])


@pytest.mark.asyncio
async def test_get_skill_endpoint_enriches_response(monkeypatch):
    """GET /skills/{domain}/{name} returns the enriched detail (version, source,
    health, author/license/tags, full skill_md, resources, references, diagnostics)
    while preserving the legacy core fields. Drives the real endpoint against the
    real spatial-preprocess skill via a stubbed registry."""
    from types import SimpleNamespace

    s = _server()
    fake_core = SimpleNamespace(
        _skill_registry=lambda: SimpleNamespace(
            skills={
                "spatial-preprocess": {
                    "alias": "spatial-preprocess",
                    "domain": "spatial",
                    "description": "Spatial preprocessing",
                    "script": str(SCRIPT),
                }
            }
        ),
    )
    monkeypatch.setattr(s, "_core", fake_core, raising=False)

    payload = await s.get_skill("spatial", "spatial-preprocess")

    # Legacy core fields preserved (backward compatible).
    assert payload["name"] == "spatial-preprocess"
    assert payload["domain"] == "spatial"
    assert payload["status"] == "ready"
    assert payload["script_path"] == str(SCRIPT)
    # Enriched detail.
    assert payload["version"] == "0.6.0"
    assert payload["source"] == "builtin"
    assert payload["health"] == "healthy"
    assert payload["author"] == "OmicsClaw"
    assert payload["license"] == "MIT"
    assert "spatial" in payload["tags"]
    assert payload["skill_md"] and payload["skill_md"].startswith("---")
    assert any(r["kind"] == "reference" for r in payload["resources"])
    assert any(ref["path"] == "references/methodology.md" for ref in payload["references"])
    assert any(c["label"] == "SKILL.md" and c["status"] == "pass" for c in payload["diagnostics"])


@pytest.mark.asyncio
async def test_get_skill_endpoint_surfaces_structured_contract(tmp_path, monkeypatch):
    """The detail endpoint serializes the structured skill.yaml contract the
    desktop "Data & requirements" panel needs (inputs/outputs/compute/validation
    evidence/methods/applicability/content hash) — these existed in the manifest
    but were previously dropped from the response."""
    from types import SimpleNamespace

    from omicsclaw.skill.schema import parse_skill_manifest

    # Empty ledger → run_health resolves to None deterministically.
    monkeypatch.setenv(
        "OMICSCLAW_SKILL_HEALTH_LEDGER", str(tmp_path / "empty-ledger.jsonl")
    )

    doc = {
        "schema_version": 2,
        "id": "spatial-rich",
        "name": "spatial-rich",
        "domain": "spatial",
        "version": "2.2.0",
        "summary": {
            "load_when": "clustering spatial spots",
            "skip_when": [{"condition": "single-cell data", "use": "sc-cluster"}],
            "tags": ["spatial"],
        },
        "interface": {
            "inputs": {
                "file_types": ["h5ad"],
                "preconditions": {"data_shape": {"requires_preprocessed": True}},
            },
            "outputs": {"files": ["clusters.csv"]},
            "parameters": {"hints": {"leiden": {}, "louvain": {}}},
        },
        "runtime": {"language": "python", "entry": "spatial_rich.py"},
        "deps": {"python": ["scanpy"]},
        "resources": {
            "compute": {
                "cpu_cores": 4,
                "memory_mib": 8192,
                "gpu_devices": 0,
                "threads": 4,
                "temporary_disk_mib": 1024,
            }
        },
        "validation": {"level": "demo-validated", "evidence": ["demo: 3 datasets"]},
        "security": {"data_egress": "none", "network": "none", "writes": "output_dir_only"},
    }
    sd = tmp_path / "spatial-rich"
    sd.mkdir(parents=True)
    (sd / "skill.yaml").write_text(parse_skill_manifest(doc).to_yaml(), encoding="utf-8")
    script = sd / "spatial_rich.py"
    script.write_text("def main(argv=None):\n    pass\n", encoding="utf-8")
    (sd / "SKILL.md").write_text(
        "---\nname: spatial-rich\n---\n# spatial-rich\n", encoding="utf-8"
    )

    s = _server()
    fake_core = SimpleNamespace(
        _skill_registry=lambda: SimpleNamespace(
            skills={
                "spatial-rich": {
                    "alias": "spatial-rich",
                    "domain": "spatial",
                    "description": "Rich",
                    "script": str(script),
                }
            }
        ),
    )
    monkeypatch.setattr(s, "_core", fake_core, raising=False)

    payload = await s.get_skill("spatial", "spatial-rich")

    assert payload["load_when"] == "clustering spatial spots"
    assert payload["skip_when"] == [{"condition": "single-cell data", "use": "sc-cluster"}]
    assert payload["requires_preprocessed"] is True
    assert payload["validation_evidence"] == ["demo: 3 datasets"]
    assert payload["compute_resources"]["cpu_cores"] == 4
    assert payload["compute_resources"]["memory_mib"] == 8192
    assert payload["methods"] == ["leiden", "louvain"]
    assert isinstance(payload["input_contract"], dict) and payload["input_contract"]
    assert isinstance(payload["output_contract"], dict)
    assert payload["content_hash"].startswith("sha256:")
    assert payload["run_health"] is None  # empty ledger → no recorded runs
    # Compact io summary powers the catalog card's input → output line.
    assert payload["io"]["input"] == ["h5ad"]
    assert payload["io"]["output"] == ["clusters.csv"]
    assert payload["io"]["requires_preprocessed"] is True
