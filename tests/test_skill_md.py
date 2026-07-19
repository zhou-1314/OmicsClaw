"""Tests for the v2 SKILL.md generator (omicsclaw.skill.skill_md, ADR 0037)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omicsclaw.skill.schema import parse_skill_manifest  # noqa: E402
from omicsclaw.skill.skill_md import (  # noqa: E402
    IO_MARKER,
    append_gotcha_entry,
    render_frontmatter,
    render_io_section,
    render_skill_md,
)


def _manifest(**over):
    doc = {
        "schema_version": 2,
        "id": "sk",
        "name": "sk",
        "domain": "spatial",
        "version": "1.2.0",
        "author": "OmicsClaw",
        "license": "MIT",
        "summary": {
            "load_when": "clustering a spatial AnnData",
            "skip_when": [{"condition": "single-cell data", "use": "sc-de"}],
            "tags": ["spatial", "visium"],
        },
        "runtime": {"language": "python", "entry": "sk.py"},
        "deps": {"python": ["scanpy", "numpy"]},
        "interface": {
            "inputs": {"modalities": ["visium"], "file_types": ["h5ad"]},
            "outputs": {
                "files": ["processed.h5ad", "report.md"],
                "anndata": {
                    "saves_h5ad": True,
                    "obs": ["leiden"],
                    "obsm": ["X_pca"],
                    "var": ["highly_variable"],
                    "layers": ["counts"],
                },
            },
        },
    }
    doc.update(over)
    return parse_skill_manifest(doc)


_NARRATIVE = (
    "---\nname: sk\ndescription: STALE OLD\nversion: 0.0.1\n---\n\n"
    "# sk\n\n"
    "## When to use\n\nprose body here\n\n"
    "## Inputs & Outputs\n\nHAND WRITTEN OLD TABLE\n\n"
    "## Flow\n\n1. step one\n\n"
    "## Gotchas\n\n- **boom** detail\n\n"
    "## Key CLI\n\n```bash\nrun cmd\n```\n\n"
    "## See also\n\n- a link\n"
)


def test_frontmatter_generated_from_manifest():
    fm = render_frontmatter(_manifest())
    assert "name: sk" in fm
    assert "version: 1.2.0" in fm
    assert "Load when clustering a spatial AnnData" in fm
    assert "Skip when single-cell data (use sc-de)" in fm
    assert "scanpy" in fm  # requires <- deps.python
    assert "AUTO-GENERATED header" in fm


def test_io_section_rendered_from_interface():
    io = render_io_section(_manifest())
    assert io is not None
    assert IO_MARKER in io
    assert "Modalities: visium" in io
    assert "`.h5ad`" in io
    assert "`processed.h5ad`" in io
    assert "`leiden`" in io and "`X_pca`" in io and "`highly_variable`" in io
    assert "`layers`: `counts`" in io


def test_io_section_none_when_interface_empty():
    assert render_io_section(_manifest(interface={})) is None


def test_io_section_renders_non_default_input_path_kinds():
    io = render_io_section(
        _manifest(interface={"inputs": {"path_kinds": ["directory", "freeform"]}})
    )
    assert io is not None
    assert "Input kinds: `directory`, `freeform`" in io


def test_io_section_renders_content_preconditions() -> None:
    io = render_io_section(
        _manifest(
            interface={
                "inputs": {
                    "path_kinds": ["file", "directory"],
                    "file_types": ["csv", "vcf", "fastq"],
                    "preconditions": {
                        "content": {
                            "tabular": {
                                "min_columns": 3,
                                "required_columns": ["gene_id"],
                            },
                            "vcf": {
                                "require_fileformat_header": True,
                                "required_columns": ["#CHROM", "POS"],
                            },
                            "fastq": {
                                "require_valid_record": True,
                                "pairing": "paired",
                            },
                            "directory": {
                                "any_of_signatures": ["paired-fastq", "tenx-matrix"]
                            },
                        }
                    },
                }
            }
        )
    )

    assert io is not None
    assert "Tabular structure: at least 3 columns; required: `gene_id`" in io
    assert "VCF structure: `##fileformat`; columns: `#CHROM`, `POS`" in io
    assert "FASTQ structure: valid first record; `paired` layout" in io
    assert "Directory layouts (any): `paired-fastq`, `tenx-matrix`" in io


def test_io_section_renders_method_scoped_output_guarantees():
    io = render_io_section(
        _manifest(
            interface={
                "parameters": {"hints": {"dynamical": {}}},
                "outputs": {
                    "files": ["processed.h5ad", "figures/latent.png"],
                    "anndata": {"saves_h5ad": True, "layers": ["velocity"]},
                    "method_scopes": [
                        {
                            "methods": ["dynamical"],
                            "files": ["figures/latent.png"],
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
                },
            }
        )
    )

    assert io is not None
    assert "When `--method` is `dynamical`" in io
    assert "Additional files: `figures/latent.png`" in io
    assert "`obs`: `latent_time`" in io
    assert "Produces artifact `singlecell.latent_time`" in io


def test_render_replaces_io_and_regenerates_header():
    out = render_skill_md(_manifest(), _NARRATIVE)
    # header regenerated from skill.yaml (new version, not the stale one)
    assert "version: 1.2.0" in out and "version: 0.0.1" not in out
    assert "STALE OLD" not in out
    # hand-written I&O removed, generated summary present
    assert "HAND WRITTEN OLD TABLE" not in out
    assert IO_MARKER in out


def test_render_preserves_narrative_verbatim():
    out = render_skill_md(_manifest(), _NARRATIVE)
    assert "prose body here" in out
    assert "1. step one" in out
    assert "**boom** detail" in out
    assert "run cmd" in out
    assert "a link" in out


def test_io_inserted_between_when_and_flow():
    out = render_skill_md(_manifest(), _NARRATIVE)
    assert out.index("## When to use") < out.index("## Inputs & Outputs") < out.index("## Flow")


def test_render_is_idempotent():
    m = _manifest()
    once = render_skill_md(m, _NARRATIVE)
    twice = render_skill_md(m, once)
    assert once == twice


def test_render_without_existing_io_section():
    # A skill whose body never had an Inputs & Outputs section still gets one.
    body = (
        "---\nname: sk\n---\n# sk\n\n## When to use\n\nx\n\n## Flow\n\ny\n\n"
        "## Gotchas\n\n- z\n\n## Key CLI\n\n```\nc\n```\n\n## See also\n\n- l\n"
    )
    out = render_skill_md(_manifest(), body)
    assert IO_MARKER in out
    assert out.index("## When to use") < out.index("## Inputs & Outputs") < out.index("## Flow")
    assert render_skill_md(_manifest(), out) == out  # idempotent


def test_append_gotcha_entry_only_changes_gotchas_and_removes_placeholder():
    original = render_skill_md(
        _manifest(),
        _NARRATIVE.replace("- **boom** detail", "- _None yet — append later._"),
    )
    bullet = "- **Sparse branch fails.** Use a bounded conversion. Evidence: `sk.py:1`."

    changed = append_gotcha_entry(original, bullet)

    before_prefix, before_suffix = original.split("## Gotchas", 1)
    after_prefix, after_suffix = changed.split("## Gotchas", 1)
    assert after_prefix == before_prefix
    assert before_suffix.split("## Key CLI", 1)[1] == after_suffix.split(
        "## Key CLI", 1
    )[1]
    assert "_None yet" not in changed
    assert changed.splitlines().count(bullet) == 1
    assert render_skill_md(_manifest(), changed) == changed

    with pytest.raises(ValueError, match="already exists"):
        append_gotcha_entry(changed, bullet)
