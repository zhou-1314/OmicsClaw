"""Tests for the v2 (skill.yaml) lint path in scripts/skill_lint.py (ADR 0037)."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omicsclaw.skill.schema import parse_skill_manifest  # noqa: E402
from scripts import skill_lint  # noqa: E402


def _v2_doc(**over) -> dict:
    data = {
        "schema_version": 2,
        "id": "spatial-demo",
        "name": "spatial-demo",
        "domain": "spatial",
        "version": "1.0.0",
        "summary": {
            "load_when": "demoing the v2 lint path",
            "skip_when": [{"condition": "single-cell data", "use": "sc-de"}],
        },
        "runtime": {"language": "python", "entry": "spatial_demo.py"},
    }
    data.update(over)
    return data


def _write(skill_dir: Path, doc: dict | None = None, *, body: str | None = None,
           script: str | None = "spatial_demo.py") -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest = parse_skill_manifest(doc or _v2_doc())
    (skill_dir / "skill.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
    if body is not None:
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    if script is not None:
        # no extra argparse flags → satisfies allowed_extra_flags check
        (skill_dir / script).write_text("def main(argv=None):\n    pass\n", encoding="utf-8")
    return skill_dir


def test_clean_v2_skill_passes(tmp_path):
    sd = _write(tmp_path / "spatial-demo")
    assert skill_lint.lint_skill(sd) == []


def test_schema_invalid_v2_reported_and_stops(tmp_path):
    sd = tmp_path / "spatial-demo"
    sd.mkdir(parents=True)
    (sd / "skill.yaml").write_text("schema_version: 2\nid: x\n", encoding="utf-8")  # missing fields
    errs = skill_lint.lint_skill(sd)
    assert errs and all(e.startswith("skill.yaml:") for e in errs)


def test_missing_skip_when_flagged(tmp_path):
    sd = _write(tmp_path / "spatial-demo", _v2_doc(summary={"load_when": "x only"}))
    errs = skill_lint.lint_skill(sd)
    assert any("skip_when" in e for e in errs)


def test_allowed_extra_flags_must_match_argparse(tmp_path):
    sd = tmp_path / "spatial-demo"
    sd.mkdir(parents=True)
    manifest = parse_skill_manifest(_v2_doc())  # no allowed_extra_flags declared
    (sd / "skill.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
    # script declares a flag the manifest omits → must be flagged
    (sd / "spatial_demo.py").write_text(
        "import argparse\n"
        "def main(argv=None):\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--resolution')\n",
        encoding="utf-8",
    )
    errs = skill_lint.lint_skill(sd)
    assert any("--resolution" in e for e in errs)
    # v2-context wording, not "parameters.yaml:"
    assert any("skill.yaml (interface.parameters)" in e for e in errs)


def test_v2_body_sections_checked_without_inputs_outputs(tmp_path):
    # A narrative body missing required v2 sections is flagged, but NOT for the
    # removed "Inputs & Outputs" section.
    body = "---\nname: spatial-demo\n---\n# spatial-demo\n## When to use\nx\n"
    sd = _write(tmp_path / "spatial-demo", body=body)
    errs = skill_lint.lint_skill(sd)
    assert any("missing required section '## Flow'" in e for e in errs)
    assert not any("Inputs & Outputs" in e for e in errs)


def test_missing_entry_script_flagged(tmp_path):
    sd = _write(tmp_path / "spatial-demo", script=None)  # skill.yaml says spatial_demo.py, none on disk
    errs = skill_lint.lint_skill(sd)
    assert any("runtime.entry" in e and "spatial_demo.py" in e for e in errs)


def test_draft_status_exempts_missing_entry(tmp_path):
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(lifecycle={"status": "draft"}),
        script=None,
    )
    errs = skill_lint.lint_skill(sd)
    assert not any("runtime.entry" in e for e in errs)


def test_discover_skills_finds_v2_only_dir(tmp_path):
    root = tmp_path / "skills"
    _write(root / "spatial" / "spatial-demo", script=None)  # skill.yaml only, no SKILL.md
    found = skill_lint.discover_skills(root)
    assert (root / "spatial" / "spatial-demo") in found


def test_v2_fresh_parameters_md_passes(tmp_path):
    from omicsclaw.skill.parameters_md import render_parameters_md
    from omicsclaw.skill.schema import load_skill_yaml

    sd = _write(tmp_path / "spatial-demo")
    refs = sd / "references"
    refs.mkdir()
    params = load_skill_yaml(sd / "skill.yaml").interface.parameters.model_dump()
    (refs / "parameters.md").write_text(
        render_parameters_md(params, source="v2"), encoding="utf-8"
    )
    assert not any("parameters.md" in e for e in skill_lint.lint_skill(sd))


def test_v2_stale_parameters_md_flagged(tmp_path):
    sd = _write(tmp_path / "spatial-demo")
    refs = sd / "references"
    refs.mkdir()
    (refs / "parameters.md").write_text("definitely stale\n", encoding="utf-8")
    assert any("parameters.md: stale" in e for e in skill_lint.lint_skill(sd))
