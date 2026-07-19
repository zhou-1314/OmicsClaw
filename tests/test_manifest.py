"""Tests for pipeline manifest read/write and lineage tracking."""

import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from omicsclaw.common.manifest import (
    ArtifactRecord,
    MANIFEST_FILENAME,
    PipelineManifest,
    StepRecord,
    VerificationRecord,
    WorkspaceRecord,
    read_manifest,
    save_manifest,
    write_manifest,
)
from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME


def test_step_record_auto_timestamp():
    """StepRecord should auto-fill completed_at if not provided."""
    rec = StepRecord(skill="test-skill", version="0.1.0")
    assert rec.completed_at != ""
    assert "T" in rec.completed_at  # ISO format


def test_step_record_explicit_timestamp():
    """StepRecord should keep an explicitly provided timestamp."""
    rec = StepRecord(skill="s", version="1.0", completed_at="2025-01-01T00:00:00")
    assert rec.completed_at == "2025-01-01T00:00:00"


def test_pipeline_manifest_append():
    m = PipelineManifest()
    m.append(StepRecord(skill="a", version="1"))
    m.append(StepRecord(skill="b", version="2"))
    assert len(m.steps) == 2
    assert m.upstream_skills() == ["a", "b"]


def test_pipeline_manifest_has_skill():
    m = PipelineManifest()
    m.append(StepRecord(skill="preprocess", version="1"))
    assert m.has_skill("preprocess") is True
    assert m.has_skill("missing") is False


def test_manifest_roundtrip(tmp_path):
    """Write and read back a manifest — data should survive."""
    rec = StepRecord(
        skill="spatial-preprocess",
        version="0.1.0",
        input_file="/data/input.h5ad",
        input_checksum="sha256:abc123",
        output_file=str(tmp_path / "processed.h5ad"),
        params={"method": "leiden", "resolution": 0.8},
    )
    write_manifest(tmp_path, rec)

    loaded = read_manifest(tmp_path)
    assert loaded is not None
    assert len(loaded.steps) == 1
    step = loaded.steps[0]
    assert step.skill == "spatial-preprocess"
    assert step.params["resolution"] == 0.8
    assert step.input_checksum == "sha256:abc123"


def test_manifest_chaining(tmp_path):
    """Manifest should accumulate steps when upstream is provided."""
    dir_a = tmp_path / "step_a"
    dir_b = tmp_path / "step_b"

    # Step A
    rec_a = StepRecord(skill="preprocess", version="0.1.0", input_file="raw.h5ad")
    write_manifest(dir_a, rec_a)

    # Step B, chained from A
    upstream = read_manifest(dir_a)
    rec_b = StepRecord(skill="domains", version="0.2.0", input_file="processed.h5ad")
    write_manifest(dir_b, rec_b, upstream=upstream)

    loaded = read_manifest(dir_b)
    assert loaded is not None
    assert len(loaded.steps) == 2
    assert loaded.upstream_skills() == ["preprocess", "domains"]
    assert loaded.has_skill("preprocess")
    assert loaded.has_skill("domains")


def test_manifest_three_step_chain(tmp_path):
    """Three-step pipeline should have all three steps in final manifest."""
    dirs = [tmp_path / f"step_{i}" for i in range(3)]
    skills = ["preprocess", "domains", "de"]
    upstream = None

    for d, skill in zip(dirs, skills):
        rec = StepRecord(skill=skill, version="1.0")
        write_manifest(d, rec, upstream=upstream)
        upstream = read_manifest(d)

    final = read_manifest(dirs[-1])
    assert final is not None
    assert final.upstream_skills() == skills


def test_read_manifest_missing_dir(tmp_path):
    """Reading from a directory with no manifest returns None."""
    assert read_manifest(tmp_path / "nonexistent") is None


def test_read_manifest_corrupt_json(tmp_path):
    """Corrupt manifest.json should return None, not crash."""
    (tmp_path / MANIFEST_FILENAME).write_text("not valid json{{{")
    assert read_manifest(tmp_path) is None


@pytest.mark.parametrize("payload", ([], "manifest", 7, None))
def test_read_manifest_rejects_non_object_json(tmp_path: Path, payload) -> None:
    """A syntactically valid non-object is malformed evidence, not an exception."""

    (tmp_path / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    assert read_manifest(tmp_path) is None


def test_manifest_json_structure(tmp_path):
    """The on-disk JSON should have the expected structure."""
    rec = StepRecord(skill="test", version="1.0", params={"a": 1})
    write_manifest(tmp_path, rec)

    raw = json.loads((tmp_path / MANIFEST_FILENAME).read_text())
    assert "steps" in raw
    assert len(raw["steps"]) == 1
    assert raw["steps"][0]["skill"] == "test"
    assert raw["steps"][0]["params"] == {"a": 1}


def test_manifest_roundtrip_preserves_workspace_contract(tmp_path):
    manifest = PipelineManifest(
        steps=[StepRecord(skill="pipeline", version="0.1.0")],
        workspace=WorkspaceRecord(
            kind="analysis_run",
            purpose="research_pipeline",
            root=str(tmp_path),
            isolation_mode="workspace_dir",
            metadata={"mode": "C"},
        ),
        required_artifacts=[
            ArtifactRecord(
                name="plan",
                path="plan.md",
                required=True,
                kind="file",
                description="Research plan",
                status="present",
            )
        ],
        verification=VerificationRecord(
            status="complete",
            completed=True,
            report_path=str(tmp_path / "completion_report.json"),
            missing_required_artifacts=[],
            warnings=[],
            metadata={"checked_by": "pytest"},
        ),
        metadata={"phase": 6},
    )

    save_manifest(tmp_path, manifest)
    loaded = read_manifest(tmp_path)

    assert loaded is not None
    assert loaded.workspace is not None
    assert loaded.workspace.kind == "analysis_run"
    assert loaded.workspace.metadata["mode"] == "C"
    assert loaded.required_artifacts[0].path == "plan.md"
    assert loaded.verification is not None
    assert loaded.verification.status == "complete"
    assert loaded.metadata["phase"] == 6


def test_read_manifest_rejects_output_claim_hardlink_alias(tmp_path):
    claim = tmp_path / OUTPUT_CLAIM_FILENAME
    claim.write_text('{"owner": "runner"}', encoding="utf-8")
    os.link(claim, tmp_path / MANIFEST_FILENAME)

    assert read_manifest(tmp_path) is None


def test_read_manifest_rejects_symlink_that_escapes_output(tmp_path):
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    external = tmp_path / "external-manifest.json"
    external.write_text('{"steps": []}', encoding="utf-8")
    (output_dir / MANIFEST_FILENAME).symlink_to(external)

    assert read_manifest(output_dir) is None


def test_read_manifest_rejects_output_root_alias_erased_by_parent_reference(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "run"
    write_manifest(output_dir, StepRecord(skill="test", version="1.0"))
    deep = tmp_path / "deep"
    deep.mkdir()
    jump = tmp_path / "jump"
    jump.symlink_to(deep, target_is_directory=True)
    tainted_root = jump / ".." / output_dir.name

    assert read_manifest(tainted_root) is None


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_save_manifest_refuses_alias_without_mutating_external_file(tmp_path, alias_kind):
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    external = tmp_path / "external-manifest.json"
    original = '{"external": true}'
    external.write_text(original, encoding="utf-8")
    target = output_dir / MANIFEST_FILENAME
    if alias_kind == "symlink":
        target.symlink_to(external)
    else:
        os.link(external, target)

    with pytest.raises(RuntimeError, match="unowned manifest"):
        save_manifest(output_dir, PipelineManifest())

    assert external.read_text(encoding="utf-8") == original
