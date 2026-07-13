"""Tests for the v2 (skill.yaml deps.python) path of audit_skill_requires (ADR 0037)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "audit_skill_requires", _ROOT / "scripts" / "audit_skill_requires.py"
)
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def _write_v2(skill_dir: Path, deps_python, *, entry="sk.py", hints=None) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    from omicsclaw.skill.schema import parse_skill_manifest

    doc = {
        "schema_version": 2, "id": "sk", "name": "sk", "domain": "spatial",
        "version": "1.0.0",
        "summary": {"load_when": "x", "skip_when": [{"condition": "y", "use": "z"}]},
        "runtime": {"language": "python", "entry": entry},
        "deps": {"python": list(deps_python)},
    }
    if hints:
        doc["interface"] = {"parameters": {"hints": hints}}
    manifest = parse_skill_manifest(doc)
    (skill_dir / "skill.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
    return skill_dir


def test_skill_yaml_raw_and_script_names(tmp_path):
    sd = _write_v2(tmp_path / "sk", ["scanpy"], entry="sk.py")
    assert audit._skill_yaml_raw(sd)["deps"]["python"] == ["scanpy"]
    assert audit.skill_script_names(sd) == ["sk.py"]
    # v1 fallback still works when no skill.yaml
    assert audit._skill_yaml_raw(tmp_path / "nope") is None


def test_param_hint_backends_reads_v2_hints(tmp_path):
    sd = _write_v2(tmp_path / "sk", ["scanpy"], hints={"m": {"requires": ["palantir"]}})
    # palantir is a registry-known backend surfaced via param hints.
    assert "palantir" in audit.param_hint_backends(sd)


def test_write_deps_python_v2_round_trip(tmp_path):
    sd = _write_v2(tmp_path / "sk", ["numpy"])
    assert audit.write_deps_python_v2(sd, ["anndata", "numpy", "scanpy"]) is True
    assert audit._skill_yaml_raw(sd)["deps"]["python"] == ["anndata", "numpy", "scanpy"]
    # idempotent: unchanged → no write
    assert audit.write_deps_python_v2(sd, ["anndata", "numpy", "scanpy"]) is False


def test_audit_skill_reads_declared_from_deps_python(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    sd = _write_v2(skills_root / "spatial" / "sk", [])  # declared empty
    (sd / "sk.py").write_text("import scanpy\n", encoding="utf-8")
    monkeypatch.setattr(audit, "SKILLS", skills_root)

    r = audit.audit_skill(sd)
    assert r["contract"] == "v2"
    assert "scanpy" in r["recommended"]
    assert "scanpy" in r["missing"]        # declared empty → scanpy is missing
    assert "scanpy" in r["final"]          # UNION includes it


def test_audit_skill_accepts_staged_path_outside_skills_root(tmp_path):
    sd = _write_v2(tmp_path / "staging" / "sk", [])
    (sd / "sk.py").write_text("import numpy\n", encoding="utf-8")

    result = audit.audit_skill(sd)

    assert result["skill"] == str(sd.resolve())
    assert "numpy" in result["missing"]


def test_malformed_v2_stays_v2_contract_and_write_is_noop(tmp_path, monkeypatch):
    # A v2-only dir whose skill.yaml is broken YAML must still be contract=v2
    # (by marker presence), so --write routes to the v2 no-op writer instead of
    # trying to rewrite a non-existent SKILL.md (Codex must-fix).
    skills_root = tmp_path / "skills"
    sd = skills_root / "spatial" / "sk"
    sd.mkdir(parents=True)
    (sd / "skill.yaml").write_text("deps: [oops\n", encoding="utf-8")  # invalid YAML, no SKILL.md
    monkeypatch.setattr(audit, "SKILLS", skills_root)

    r = audit.audit_skill(sd)
    assert r["contract"] == "v2"
    assert audit.write_deps_python_v2(sd, ["numpy"]) is False  # no crash, no-op


def test_audit_skill_v2_declared_satisfies(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    sd = _write_v2(skills_root / "spatial" / "sk", ["anndata", "scanpy"])
    (sd / "sk.py").write_text("import scanpy\n", encoding="utf-8")
    monkeypatch.setattr(audit, "SKILLS", skills_root)

    r = audit.audit_skill(sd)
    assert r["contract"] == "v2"
    assert r["missing"] == []              # scanpy+anndata declared → nothing missing
