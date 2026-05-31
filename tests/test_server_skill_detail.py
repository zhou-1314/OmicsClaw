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


def test_skill_frontmatter_parses_yaml():
    fm = _server()._skill_frontmatter(SKILL_DIR)
    assert fm.get("name") == "spatial-preprocess"
    assert fm.get("version") == "0.6.0"
    assert fm.get("author") == "OmicsClaw"
    assert fm.get("license") == "MIT"
    assert "spatial" in (fm.get("tags") or [])


def test_skill_frontmatter_missing_is_empty():
    s = _server()
    assert s._skill_frontmatter(None) == {}
    assert s._skill_frontmatter(SKILLS_DIR / "does-not-exist") == {}


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
    assert by_path.get("parameters.yaml") == "config"
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
    fm = s._skill_frontmatter(SKILL_DIR)
    by_label = {c["label"]: c["status"] for c in s._skill_diagnostics(SKILL_DIR, SCRIPT, fm)}
    assert by_label.get("SKILL.md") == "pass"
    assert by_label.get("entry script") == "pass"
    assert by_label.get("version declared") == "pass"
    assert by_label.get("parameters.yaml") == "pass"
    assert by_label.get("references") == "pass"
    assert s._skill_diagnostics(None, None, {}) == []


def test_skill_diagnostics_warns_when_version_absent():
    """A complete skill with NO frontmatter version → the (warn_only) 'version
    declared' check is 'warn', not 'fail' — version is advisory."""
    s = _server()
    by_label = {c["label"]: c["status"] for c in s._skill_diagnostics(SKILL_DIR, SCRIPT, {})}
    assert by_label.get("version declared") == "warn"
    # required checks (SKILL.md + entry script) still pass for a complete skill.
    assert by_label.get("SKILL.md") == "pass"
    assert by_label.get("entry script") == "pass"


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
