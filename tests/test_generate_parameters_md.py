"""Tests for the dual-track references/parameters.md generator (ADR 0037).

v1 (parameters.yaml) and v2 (skill.yaml.interface.parameters) render the same
body bytes for the same data — only the provenance header differs. A skill
with both files renders from skill.yaml (v2 wins).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
import yaml  # noqa: E402

from omicsclaw.skill.parameters_md import (  # noqa: E402
    AUTOGEN_HEADER,
    AUTOGEN_HEADER_V2,
    render_parameters_md,
)
from omicsclaw.skill.schema import parse_skill_manifest  # noqa: E402
from scripts import generate_parameters_md as gpm  # noqa: E402

_FLAGS = ["--resolution", "--n-neighbors"]
_HINTS = {
    "leiden": {
        "priority": "resolution → n_neighbors",
        "params": ["resolution", "n_neighbors"],
        "defaults": {"resolution": 1.0, "n_neighbors": 15},
        "requires": ["pca"],
        "tips": ["bump resolution for finer clusters"],
    }
}


def test_v1_render_uses_parameters_yaml_header():
    out = render_parameters_md({"allowed_extra_flags": _FLAGS, "param_hints": _HINTS})
    assert out.startswith(AUTOGEN_HEADER)
    assert "from parameters.yaml" in out.splitlines()[0]


def test_v2_render_uses_skill_yaml_header():
    out = render_parameters_md(
        {"allowed_extra_flags": _FLAGS, "hints": _HINTS}, source="v2"
    )
    assert out.startswith(AUTOGEN_HEADER_V2)
    assert "from skill.yaml" in out.splitlines()[0]


def test_unknown_source_rejected():
    # A typo'd source must fail loud, never silently stamp the v1 header.
    with pytest.raises(ValueError):
        render_parameters_md({"allowed_extra_flags": _FLAGS, "hints": _HINTS}, source="V2")


def test_v1_v2_body_is_byte_identical():
    # Same flags + same hint structure → identical body; only the header differs.
    v1 = render_parameters_md({"allowed_extra_flags": _FLAGS, "param_hints": _HINTS})
    v2 = render_parameters_md(
        {"allowed_extra_flags": _FLAGS, "hints": _HINTS}, source="v2"
    )
    assert v1[len(AUTOGEN_HEADER):] == v2[len(AUTOGEN_HEADER_V2):]


def test_source_refs_absent_renders_byte_identical_to_baseline():
    # P5: a hints entry with NO source_refs key must render exactly like
    # today's 2-column output — this is the regression guard the freshness
    # gate (skill_lint._check_parameters_md_fresh) depends on for the ~100+
    # existing skills, none of which have source_refs.
    baseline = render_parameters_md({"allowed_extra_flags": _FLAGS, "hints": _HINTS}, source="v2")
    assert "| name | default |" in baseline
    assert "| name | default | source |" not in baseline


def test_source_refs_present_adds_third_column():
    hints_with_refs = {
        "leiden": {
            "params": ["resolution", "n_neighbors"],
            "defaults": {"resolution": 0.8, "n_neighbors": 15},
            "source_refs": {
                "resolution": {"quote": "resolution=0.8", "char_span": [0, 14], "doc_ref": "10.1038/xyz"},
                "n_neighbors": {"todo": True},
            },
        }
    }
    out = render_parameters_md({"allowed_extra_flags": _FLAGS, "hints": hints_with_refs}, source="v2")
    assert "| name | default | source |" in out
    assert '| `resolution` | `0.8` | "resolution=0.8" (10.1038/xyz) |' in out
    assert "| `n_neighbors` | `15` | TODO |" in out


def test_source_refs_quote_pipe_character_is_escaped():
    hints_with_refs = {
        "leiden": {
            "params": ["resolution"],
            "defaults": {"resolution": 0.8},
            "source_refs": {
                "resolution": {"quote": "a|b resolution=0.8", "char_span": [0, 5], "doc_ref": "x"},
            },
        }
    }
    out = render_parameters_md({"allowed_extra_flags": _FLAGS, "hints": hints_with_refs}, source="v2")
    # Escaped (\|) so markdown doesn't parse it as a table-column delimiter —
    # an unescaped pipe would corrupt the table into extra columns.
    assert "a\\|b resolution=0.8" in out
    for line in out.splitlines():
        if "resolution=0.8" in line:
            unescaped_pipes = line.replace("\\|", "").count("|")
            assert unescaped_pipes == 4  # leading/trailing + 2 internal separators


def _v2_skill(tmp_path: Path) -> Path:
    doc = {
        "schema_version": 2,
        "id": "spatial-demo",
        "name": "spatial-demo",
        "domain": "spatial",
        "version": "1.0.0",
        "summary": {"load_when": "x", "skip_when": [{"condition": "y", "use": "z"}]},
        "runtime": {"language": "python", "entry": "spatial_demo.py"},
        "interface": {"parameters": {"allowed_extra_flags": _FLAGS, "hints": _HINTS}},
    }
    sd = tmp_path / "spatial-demo"
    sd.mkdir(parents=True)
    (sd / "skill.yaml").write_text(parse_skill_manifest(doc).to_yaml(), encoding="utf-8")
    return sd


def test_render_for_skill_v2(tmp_path):
    out = gpm.render_for_skill(_v2_skill(tmp_path))
    assert out is not None and out.startswith(AUTOGEN_HEADER_V2)
    assert "`--resolution`" in out


def test_render_for_skill_prefers_v2_when_both_present(tmp_path):
    sd = _v2_skill(tmp_path)
    # A v1 sidecar that would render a different flag set if (wrongly) read.
    (sd / "parameters.yaml").write_text(
        yaml.safe_dump({"allowed_extra_flags": ["--legacy-only"], "param_hints": {}}),
        encoding="utf-8",
    )
    out = gpm.render_for_skill(sd)
    assert out.startswith(AUTOGEN_HEADER_V2)
    assert "--legacy-only" not in out


def test_render_for_skill_v1_fallback(tmp_path):
    sd = tmp_path / "legacy-skill"
    sd.mkdir()
    (sd / "parameters.yaml").write_text(
        yaml.safe_dump({"allowed_extra_flags": _FLAGS, "param_hints": _HINTS}),
        encoding="utf-8",
    )
    out = gpm.render_for_skill(sd)
    assert out is not None and out.startswith(AUTOGEN_HEADER)


def test_render_for_skill_none_when_no_parameters(tmp_path):
    sd = tmp_path / "empty"
    sd.mkdir()
    assert gpm.render_for_skill(sd) is None


def test_render_for_skill_invalid_v2_raises(tmp_path):
    sd = tmp_path / "bad"
    sd.mkdir()
    (sd / "skill.yaml").write_text("schema_version: 2\nid: x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        gpm.render_for_skill(sd)


def test_write_or_check_reports_invalid_v2(tmp_path, capsys):
    sd = tmp_path / "bad"
    sd.mkdir()
    (sd / "skill.yaml").write_text("schema_version: 2\nid: x\n", encoding="utf-8")
    rc = gpm.write_or_check(sd, check=True)
    assert rc == 1
    assert "invalid skill.yaml" in capsys.readouterr().out


def test_discover_skill_dirs_finds_both(tmp_path):
    root = tmp_path / "skills"
    (root / "a").mkdir(parents=True)
    (root / "a" / "skill.yaml").write_text("x", encoding="utf-8")
    (root / "b").mkdir(parents=True)
    (root / "b" / "parameters.yaml").write_text("x", encoding="utf-8")
    found = gpm.discover_skill_dirs(root)
    assert (root / "a") in found
    assert (root / "b") in found
