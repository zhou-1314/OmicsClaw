"""Tests for ``omicsclaw.skill.execution.pipeline_config`` (OMI-12 P2.7).

These pin the YAML schema and the validation errors the loader raises.
The end-to-end runner integration is exercised by
``test_runtime_pipeline_dispatch.py``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME
from omicsclaw.skill.execution.pipeline_config import (
    PIPELINES_DIR,
    PipelineConfig,
    PipelineConfigError,
    PipelineStep,
    list_available_pipelines,
    load_pipeline_config,
    pipeline_config_path,
)


# ---------------------------------------------------------------------------
# Default config — shipped at repo root in ``pipelines/spatial-pipeline.yaml``.
# ---------------------------------------------------------------------------


def test_default_pipelines_dir_resolves_to_repo_root():
    assert PIPELINES_DIR.name == "pipelines"
    assert PIPELINES_DIR.exists(), (
        "pipelines/ must ship with the package — the runner depends on it"
    )


def test_shipped_spatial_pipeline_yaml_loads():
    config = load_pipeline_config("spatial-pipeline")
    assert config is not None
    assert config.name == "spatial-pipeline"
    assert config.chain_output_basename == "processed.h5ad"
    assert config.skill_names == (
        "spatial-preprocess",
        "spatial-domains",
        "spatial-de",
        "spatial-genes",
        "spatial-statistics",
    )


def test_pipeline_chain_output_cannot_use_internal_run_claim(tmp_path: Path):
    with pytest.raises(PipelineConfigError, match="safe file basename"):
        PipelineConfig(
            name="claim-pipeline",
            description="",
            chain_output_basename=OUTPUT_CLAIM_FILENAME,
            steps=(PipelineStep(skill="step-a"), PipelineStep(skill="step-b")),
        )

    _write_yaml(
        tmp_path,
        "claim-pipeline",
        f"""
        name: claim-pipeline
        chain_output_basename: {OUTPUT_CLAIM_FILENAME}
        steps:
          - skill: step-a
          - skill: step-b
        """,
    )
    with pytest.raises(PipelineConfigError, match="safe file basename"):
        load_pipeline_config("claim-pipeline", pipelines_dir=tmp_path)


def test_list_available_pipelines_includes_spatial():
    available = list_available_pipelines()
    assert "spatial-pipeline" in available


def test_list_available_pipelines_excludes_noncanonical_and_outside_symlinks(
    tmp_path: Path,
):
    pipelines_dir = tmp_path / "pipelines"
    pipelines_dir.mkdir()
    _write_yaml(
        pipelines_dir,
        "valid-pipeline",
        "name: valid-pipeline\nsteps:\n  - skill: step-a\n",
    )
    _write_yaml(
        pipelines_dir,
        "Upper-pipeline",
        "name: Upper-pipeline\nsteps:\n  - skill: step-a\n",
    )
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        "name: linked-pipeline\nsteps:\n  - skill: step-a\n",
        encoding="utf-8",
    )
    (pipelines_dir / "linked-pipeline.yaml").symlink_to(outside)

    assert list_available_pipelines(pipelines_dir=pipelines_dir) == [
        "valid-pipeline"
    ]


def test_missing_yaml_returns_none(tmp_path: Path):
    assert load_pipeline_config("nonexistent-pipeline", pipelines_dir=tmp_path) is None


def test_pipeline_config_path_targets_yaml_extension(tmp_path: Path):
    path = pipeline_config_path("foo-pipeline", pipelines_dir=tmp_path)
    assert path == tmp_path / "foo-pipeline.yaml"


def test_pipeline_lookup_rejects_path_shaped_or_noncanonical_aliases_before_read(
    tmp_path: Path,
    monkeypatch,
):
    pipelines_dir = tmp_path / "pipelines"
    pipelines_dir.mkdir()
    absolute_alias = str(tmp_path / "absolute-pipeline")
    cases = {
        absolute_alias: tmp_path / "absolute-pipeline.yaml",
        "../parent-pipeline": tmp_path / "parent-pipeline.yaml",
        "nested/path-pipeline": pipelines_dir / "nested" / "path-pipeline.yaml",
        r"nested\path-pipeline": pipelines_dir / r"nested\path-pipeline.yaml",
        "Upper-pipeline": pipelines_dir / "Upper-pipeline.yaml",
        "under_score-pipeline": pipelines_dir / "under_score-pipeline.yaml",
    }
    for alias, path in cases.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump({"name": alias, "steps": [{"skill": "step-a"}]}),
            encoding="utf-8",
        )

    reads: list[Path] = []
    real_read_text = Path.read_text

    def tracking_read_text(path: Path, *args, **kwargs):
        reads.append(path)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracking_read_text)

    for alias in cases:
        with pytest.raises(PipelineConfigError, match="canonical pipeline alias"):
            load_pipeline_config(alias, pipelines_dir=pipelines_dir)

    assert reads == []


def test_pipeline_lookup_rejects_symlink_that_resolves_outside_root(tmp_path: Path):
    pipelines_dir = tmp_path / "pipelines"
    pipelines_dir.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        yaml.safe_dump(
            {"name": "linked-pipeline", "steps": [{"skill": "step-a"}]}
        ),
        encoding="utf-8",
    )
    (pipelines_dir / "linked-pipeline.yaml").symlink_to(outside)

    with pytest.raises(PipelineConfigError, match="inside the pipelines directory"):
        load_pipeline_config("linked-pipeline", pipelines_dir=pipelines_dir)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _write_yaml(tmp_path: Path, name: str, contents: str) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(textwrap.dedent(contents), encoding="utf-8")
    return path


def test_invalid_yaml_raises(tmp_path: Path):
    _write_yaml(tmp_path, "bad-pipeline", "name: bad-pipeline\nsteps: [oops")
    with pytest.raises(PipelineConfigError, match="invalid YAML"):
        load_pipeline_config("bad-pipeline", pipelines_dir=tmp_path)


@pytest.mark.parametrize(
    "contents",
    [
        """
        name: duplicate-pipeline
        steps:
          - skill: intended-step
        steps:
          - skill: effective-step
        """,
        """
        name: duplicate-pipeline
        steps:
          - skill: intended-step
            skill: effective-step
        """,
    ],
)
def test_duplicate_yaml_mapping_key_fails_closed(tmp_path: Path, contents: str):
    _write_yaml(tmp_path, "duplicate-pipeline", contents)

    with pytest.raises(PipelineConfigError, match="duplicate YAML mapping key"):
        load_pipeline_config("duplicate-pipeline", pipelines_dir=tmp_path)


def test_top_level_must_be_mapping(tmp_path: Path):
    _write_yaml(tmp_path, "listy-pipeline", "- not\n- a\n- mapping\n")
    with pytest.raises(PipelineConfigError, match="top-level must be a mapping"):
        load_pipeline_config("listy-pipeline", pipelines_dir=tmp_path)


@pytest.mark.parametrize("field", ["chain_output_basenam", "pipeline_args"])
def test_unknown_top_level_field_fails_closed(tmp_path: Path, field: str):
    path = tmp_path / "strict-pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "strict-pipeline",
                "steps": [{"skill": "step-a"}],
                field: "silently-wrong",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PipelineConfigError, match="unsupported top-level fields"):
        load_pipeline_config("strict-pipeline", pipelines_dir=tmp_path)


def test_name_must_match_filename(tmp_path: Path):
    """A renamed file with a stale ``name:`` value must fail loudly so the
    dispatcher and the config can't disagree silently."""
    _write_yaml(
        tmp_path,
        "renamed-pipeline",
        """
        name: original-pipeline
        steps:
          - skill: foo
        """,
    )
    with pytest.raises(PipelineConfigError, match="does not match"):
        load_pipeline_config("renamed-pipeline", pipelines_dir=tmp_path)


def test_name_must_end_with_pipeline_suffix(tmp_path: Path):
    _write_yaml(
        tmp_path,
        "missing-suffix",
        """
        name: missing-suffix
        steps:
          - skill: foo
        """,
    )
    with pytest.raises(PipelineConfigError, match="-pipeline"):
        load_pipeline_config("missing-suffix", pipelines_dir=tmp_path)


def test_steps_must_be_non_empty_list(tmp_path: Path):
    _write_yaml(
        tmp_path,
        "empty-pipeline",
        """
        name: empty-pipeline
        steps: []
        """,
    )
    with pytest.raises(PipelineConfigError, match="non-empty list"):
        load_pipeline_config("empty-pipeline", pipelines_dir=tmp_path)


def test_step_requires_skill_key(tmp_path: Path):
    _write_yaml(
        tmp_path,
        "broken-pipeline",
        """
        name: broken-pipeline
        steps:
          - notes: this step has no skill
        """,
    )
    with pytest.raises(PipelineConfigError, match=r"step #0 ``skill``"):
        load_pipeline_config("broken-pipeline", pipelines_dir=tmp_path)


def test_duplicate_pipeline_step_is_rejected_before_output_collision(tmp_path: Path):
    _write_yaml(
        tmp_path,
        "duplicate-pipeline",
        """
        name: duplicate-pipeline
        steps:
          - skill: step-a
          - skill: step-a
        """,
    )

    with pytest.raises(PipelineConfigError, match="duplicate step skill"):
        load_pipeline_config("duplicate-pipeline", pipelines_dir=tmp_path)


@pytest.mark.parametrize("field", ["args", "method"])
def test_unknown_step_field_fails_closed(tmp_path: Path, field: str):
    path = tmp_path / "strict-pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "strict-pipeline",
                "steps": [{"skill": "step-a", field: "silently-dropped"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PipelineConfigError, match=r"step #0 has unsupported fields"):
        load_pipeline_config("strict-pipeline", pipelines_dir=tmp_path)


@pytest.mark.parametrize(
    "skill",
    [
        "/tmp/step-a",
        "../step-a",
        "nested/step-a",
        r"nested\step-a",
        "Upper-step",
        "under_score",
    ],
)
def test_pipeline_step_requires_canonical_skill_alias(tmp_path: Path, skill: str):
    path = tmp_path / "unsafe-pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {"name": "unsafe-pipeline", "steps": [{"skill": skill}]}
        ),
        encoding="utf-8",
    )

    with pytest.raises(PipelineConfigError, match="canonical skill alias"):
        load_pipeline_config("unsafe-pipeline", pipelines_dir=tmp_path)


def test_chain_output_basename_default_when_omitted(tmp_path: Path):
    _write_yaml(
        tmp_path,
        "tiny-pipeline",
        """
        name: tiny-pipeline
        steps:
          - skill: foo
        """,
    )
    config = load_pipeline_config("tiny-pipeline", pipelines_dir=tmp_path)
    assert config is not None
    assert config.chain_output_basename == "processed.h5ad"


def test_chain_output_basename_can_be_overridden(tmp_path: Path):
    _write_yaml(
        tmp_path,
        "bulk-pipeline",
        """
        name: bulk-pipeline
        chain_output_basename: counts.csv
        steps:
          - skill: bulkrna-qc
          - skill: bulkrna-de
        """,
    )
    config = load_pipeline_config("bulk-pipeline", pipelines_dir=tmp_path)
    assert config is not None
    assert config.chain_output_basename == "counts.csv"
    assert config.skill_names == ("bulkrna-qc", "bulkrna-de")


@pytest.mark.parametrize(
    "chain_output_basename",
    [
        "/tmp/escape.h5ad",
        "../escape.h5ad",
        "nested/escape.h5ad",
        r"..\escape.h5ad",
        r"C:\escape.h5ad",
        "bad:name.h5ad",
        "bad?.h5ad",
        "bad*.h5ad",
        "bad|name.h5ad",
        "trailing-dot.",
        "trailing-space ",
        "CON",
        "CON .txt",
        "nul.h5ad",
        "LPT1.csv",
        "COM1 .csv",
        "é" * 126 + ".h5ad",
    ],
)
def test_chain_output_must_be_a_safe_basename(
    tmp_path: Path,
    chain_output_basename: str,
):
    path = tmp_path / "unsafe-pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "unsafe-pipeline",
                "chain_output_basename": chain_output_basename,
                "steps": [{"skill": "step-a"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PipelineConfigError, match="safe file basename"):
        load_pipeline_config("unsafe-pipeline", pipelines_dir=tmp_path)
