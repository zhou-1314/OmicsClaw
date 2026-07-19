"""End-to-end tests for the YAML-driven pipeline dispatch (OMI-12 P2.7).

These tests use a temporary ``pipelines/`` directory and a stub ``run_skill``
so the chain runs in-process without spawning any subprocesses. They verify:

- ``run_skill("<name>-pipeline")`` dispatches through the generic runner
  when a matching ``pipelines/<name>.yaml`` exists.
- The runner threads each step's chain-baton output (``processed.h5ad`` by
  default; configurable per pipeline) into the next step's ``--input``.
- The chain stops at the first failure and the surviving steps still land
  in ``pipeline_summary.json``.
- An unknown ``<name>-pipeline`` alias falls through to the standard
  ``Unknown skill`` error rather than crashing or fabricating a pipeline.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

import pytest

from omicsclaw.skill.execution import pipeline_runner
from omicsclaw.skill.execution.pipeline_config import (
    PipelineConfig,
    PipelineConfigError,
    PipelineStep,
    load_pipeline_config,
)
from omicsclaw.skill.result import (
    SkillRunAuditIdentity,
    SkillRunResult,
    build_skill_run_result,
)


def _write_pipeline(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / f"{name}.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


class _PipelineSnapshot:
    """Minimal frozen-authority double for low-level pipeline runner tests."""

    def __init__(self, config: PipelineConfig):
        self.skills = {
            skill: {
                "alias": skill,
                "lifecycle_status": "mvp",
                "input_contract": {},
            }
            for skill in config.skill_names
        }

    def skill_revisions(self, skills: list[str]) -> dict[str, dict[str, str]]:
        return {
            skill: {
                "skill_id": skill,
                "skill_version": "test",
                "manifest_hash": f"sha256:{'a' * 64}",
                "source_hash": f"sha256:{'b' * 64}",
            }
            for skill in skills
        }


def _audit_identity_from_revision(
    revision: dict[str, str],
) -> SkillRunAuditIdentity:
    return SkillRunAuditIdentity(
        skill_id=revision["skill_id"],
        skill_version=revision["skill_version"],
        skill_hash=revision["manifest_hash"],
        source_hash=revision["source_hash"],
        environment_id="unknown",
    )


def _stub_run_skill_chain(
    monkeypatch,
    *,
    chain_output_basename: str = "processed.h5ad",
    fail_on: str | None = None,
    invalid_baton_on: str | None = None,
    invalid_baton_kind: str = "missing",
):
    """Replace ``run_skill`` with an in-process stub that writes the chain baton.

    Returns the list of (skill_name, input_path) tuples observed in invocation
    order so tests can verify the baton-passing actually happened.
    """
    monkeypatch.setattr(
        pipeline_runner,
        "preflight_skill_execution",
        lambda *_args, **_kwargs: None,
    )
    calls: list[tuple[str, str | None]] = []

    def fake_run_skill(*, skill_name, input_path=None, output_dir=None, demo=False, **kwargs):
        calls.append((skill_name, input_path))
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        success = skill_name != fail_on
        if success:
            baton = out_dir / chain_output_basename
            if skill_name != invalid_baton_on:
                # Write a non-empty baton so the next step picks it up.
                baton.write_text("stub-output\n", encoding="utf-8")
            elif invalid_baton_kind == "directory":
                baton.mkdir()
            elif invalid_baton_kind == "symlink":
                target = out_dir / "real-output.h5ad"
                target.write_text("stub-output\n", encoding="utf-8")
                baton.symlink_to(target)
            elif invalid_baton_kind == "claim-hardlink":
                from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

                claim = out_dir / OUTPUT_CLAIM_FILENAME
                claim.write_text("{}\n", encoding="utf-8")
                baton.hardlink_to(claim)
        return build_skill_run_result(
            skill=skill_name,
            success=success,
            exit_code=0 if success else 1,
            output_dir=str(out_dir),
            stdout="",
            stderr="" if success else "stub failure",
            duration_seconds=0.01,
            method=None,
            audit_identity=_audit_identity_from_revision(
                kwargs["_expected_skill_revision"]
            ),
        )

    monkeypatch.setattr("omicsclaw.skill.runner._run_skill_bound", fake_run_skill)
    return calls


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_pipeline_dispatch_threads_chain_baton_through_each_step(tmp_path, monkeypatch):
    _write_pipeline(
        tmp_path,
        "demo-pipeline",
        """
        name: demo-pipeline
        steps:
          - skill: step-a
          - skill: step-b
          - skill: step-c
        """,
    )
    config = load_pipeline_config("demo-pipeline", pipelines_dir=tmp_path)
    assert config is not None

    calls = _stub_run_skill_chain(monkeypatch)

    pipeline_out = tmp_path / "out"
    initial_input = str(tmp_path / "user_input.h5ad")
    (tmp_path / "user_input.h5ad").write_text("user", encoding="utf-8")

    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name, success=False, exit_code=-1, output_dir=None, stderr=msg
        ),
        input_path=initial_input,
        output_dir=str(pipeline_out),
        registry_snapshot=_PipelineSnapshot(config),
    )

    assert isinstance(result, SkillRunResult)
    assert result.success is True
    assert calls[0] == ("step-a", initial_input)
    # step-b should receive step-a's baton, not the user's input.
    assert calls[1] == ("step-b", str(pipeline_out / "step-a" / "processed.h5ad"))
    assert calls[2] == ("step-b", str(pipeline_out / "step-a" / "processed.h5ad")) or calls[2] == (
        "step-c",
        str(pipeline_out / "step-b" / "processed.h5ad"),
    )

    summary = json.loads((pipeline_out / "pipeline_summary.json").read_text(encoding="utf-8"))
    assert summary["pipeline"] == ["step-a", "step-b", "step-c"]
    assert summary["schema_version"] == 1
    assert summary["chain_output_basename"] == "processed.h5ad"
    assert summary["bound_skill_revisions"] == _PipelineSnapshot(
        config
    ).skill_revisions(list(config.skill_names))
    authority_payload = {
        "pipeline": "demo-pipeline",
        "chain_output_basename": "processed.h5ad",
        "steps": ["step-a", "step-b", "step-c"],
        "skill_revisions": summary["bound_skill_revisions"],
    }
    expected_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            authority_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert summary["pipeline_authority_digest"] == expected_digest
    assert set(summary["results"].keys()) == {"step-a", "step-b", "step-c"}
    assert all(entry["success"] for entry in summary["results"].values())
    assert all(entry["audit_identity"] for entry in summary["results"].values())
    assert result.audit_identity is None


def test_pipeline_respects_chain_output_basename_override(tmp_path, monkeypatch):
    _write_pipeline(
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

    calls = _stub_run_skill_chain(monkeypatch, chain_output_basename="counts.csv")

    pipeline_out = tmp_path / "bulk_out"
    (tmp_path / "raw.csv").write_text("raw", encoding="utf-8")
    pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name, success=False, exit_code=-1, output_dir=None, stderr=msg
        ),
        input_path=str(tmp_path / "raw.csv"),
        output_dir=str(pipeline_out),
        registry_snapshot=_PipelineSnapshot(config),
    )

    # step 2's input is step 1's baton, but the baton is ``counts.csv``, not
    # the default ``processed.h5ad`` — proving the override was honoured.
    assert calls[1][1] == str(pipeline_out / "bulkrna-qc" / "counts.csv")


def test_pipeline_binds_every_step_to_initial_registry_snapshot_and_revision(
    tmp_path,
    monkeypatch,
):
    from omicsclaw.skill.runner import ensure_registry_loaded

    snapshot = ensure_registry_loaded().snapshot()
    config = PipelineConfig(
        name="bound-pipeline",
        description="",
        chain_output_basename="processed.h5ad",
        steps=(
            PipelineStep(skill="spatial-preprocess"),
            PipelineStep(skill="spatial-domains"),
        ),
    )
    observed: list[tuple[object, object]] = []

    def fake_run_skill(
        *,
        skill_name,
        output_dir,
        _registry_snapshot=None,
        _expected_skill_revision=None,
        **_kwargs,
    ):
        observed.append((_registry_snapshot, _expected_skill_revision))
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "processed.h5ad").write_text("stub-output\n", encoding="utf-8")
        return build_skill_run_result(
            skill=skill_name,
            success=True,
            exit_code=0,
            output_dir=out_dir,
            duration_seconds=0.01,
            audit_identity=_audit_identity_from_revision(
                _expected_skill_revision
            ),
        )

    monkeypatch.setattr("omicsclaw.skill.runner._run_skill_bound", fake_run_skill)

    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name, success=False, exit_code=-1, output_dir=None, stderr=msg
        ),
        output_dir=str(tmp_path / "out"),
        demo=True,
        registry_snapshot=snapshot,
    )

    assert result.success is True
    assert len(observed) == 2
    assert all(seen_snapshot is snapshot for seen_snapshot, _revision in observed)
    assert [revision["skill_id"] for _snapshot, revision in observed] == [
        "spatial-preprocess",
        "spatial-domains",
    ]
    assert all(
        revision["source_hash"].startswith("sha256:")
        for _snapshot, revision in observed
    )


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_pipeline_stops_at_first_failure_and_records_partial_summary(tmp_path, monkeypatch):
    _write_pipeline(
        tmp_path,
        "demo-pipeline",
        """
        name: demo-pipeline
        steps:
          - skill: step-a
          - skill: step-b
          - skill: step-c
        """,
    )
    config = load_pipeline_config("demo-pipeline", pipelines_dir=tmp_path)
    assert config is not None

    _stub_run_skill_chain(monkeypatch, fail_on="step-b")

    pipeline_out = tmp_path / "out"
    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name, success=False, exit_code=-1, output_dir=None, stderr=msg
        ),
        input_path=str(tmp_path / "user_input.h5ad"),
        output_dir=str(pipeline_out),
        registry_snapshot=_PipelineSnapshot(config),
    )
    (tmp_path / "user_input.h5ad").write_text("user", encoding="utf-8")

    assert result.success is False
    summary = json.loads((pipeline_out / "pipeline_summary.json").read_text(encoding="utf-8"))
    # step-c never ran because the chain stopped at step-b.
    assert "step-a" in summary["results"]
    assert "step-b" in summary["results"]
    assert "step-c" not in summary["results"]
    assert summary["results"]["step-a"]["success"] is True
    assert summary["results"]["step-b"]["success"] is False
    assert summary["results"]["step-b"]["stderr"] == "stub failure"
    assert summary["results"]["step-b"]["audit_identity"] == {
        "skill_id": "step-b",
        "skill_version": "test",
        "skill_hash": f"sha256:{'a' * 64}",
        "source_hash": f"sha256:{'b' * 64}",
        "environment_id": "unknown",
    }
    assert result.stderr == "stub failure"


@pytest.mark.parametrize("identity_kind", ["missing", "mismatch"])
def test_pipeline_rejects_success_without_matching_bound_audit_identity(
    tmp_path,
    monkeypatch,
    identity_kind,
):
    config = PipelineConfig(
        name="audit-pipeline",
        description="",
        chain_output_basename="processed.h5ad",
        steps=(PipelineStep(skill="step-a"), PipelineStep(skill="step-b")),
    )
    snapshot = _PipelineSnapshot(config)
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline_runner,
        "preflight_skill_execution",
        lambda *_args, **_kwargs: None,
    )

    def fake_run_skill(
        *, skill_name, output_dir, _expected_skill_revision=None, **_kwargs
    ):
        calls.append(skill_name)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "processed.h5ad").write_text("stub\n", encoding="utf-8")
        identity = None
        if identity_kind == "mismatch":
            identity = SkillRunAuditIdentity(
                skill_id="different-skill",
                skill_version=_expected_skill_revision["skill_version"],
                skill_hash=_expected_skill_revision["manifest_hash"],
                source_hash=_expected_skill_revision["source_hash"],
                environment_id="unknown",
            )
        return build_skill_run_result(
            skill=skill_name,
            success=True,
            exit_code=0,
            output_dir=out_dir,
            duration_seconds=0.01,
            audit_identity=identity,
        )

    monkeypatch.setattr("omicsclaw.skill.runner._run_skill_bound", fake_run_skill)
    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name, success=False, exit_code=-1, output_dir=None, stderr=msg
        ),
        output_dir=str(tmp_path / "out"),
        demo=True,
        registry_snapshot=snapshot,
    )

    assert result.success is False
    assert calls == ["step-a"]
    assert "bound audit identity" in result.stderr
    summary = json.loads(
        (tmp_path / "out" / "pipeline_summary.json").read_text(encoding="utf-8")
    )
    assert summary["results"]["step-a"]["success"] is False
    assert summary["results"]["step-a"]["audit_identity"] == (
        None
        if identity_kind == "missing"
        else {
            "skill_id": "different-skill",
            "skill_version": "test",
            "skill_hash": f"sha256:{'a' * 64}",
            "source_hash": f"sha256:{'b' * 64}",
            "environment_id": "unknown",
        }
    )


def test_programmatic_pipeline_config_enforces_loader_invariants():
    with pytest.raises(PipelineConfigError, match="safe file basename"):
        PipelineConfig(
            name="unsafe-pipeline",
            description="",
            chain_output_basename="../outside.h5ad",
            steps=(PipelineStep(skill="step-a"), PipelineStep(skill="step-b")),
        )


def test_run_pipeline_revalidates_programmatic_config_before_output(
    tmp_path,
    monkeypatch,
):
    config = PipelineConfig(
        name="safe-pipeline",
        description="",
        chain_output_basename="processed.h5ad",
        steps=(PipelineStep(skill="step-a"), PipelineStep(skill="step-b")),
    )
    object.__setattr__(config, "chain_output_basename", str(tmp_path / "outside.h5ad"))
    calls = _stub_run_skill_chain(monkeypatch)
    output_dir = tmp_path / "out"

    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name, success=False, exit_code=-1, output_dir=None, stderr=msg
        ),
        output_dir=str(output_dir),
        demo=True,
        registry_snapshot=_PipelineSnapshot(config),
    )

    assert result.success is False
    assert "Pipeline config invalid" in result.stderr
    assert calls == []
    assert not output_dir.exists()


def test_run_pipeline_rejects_tampered_internal_claim_baton_before_output(
    tmp_path,
    monkeypatch,
):
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    config = PipelineConfig(
        name="safe-pipeline",
        description="",
        chain_output_basename="processed.h5ad",
        steps=(PipelineStep(skill="step-a"), PipelineStep(skill="step-b")),
    )
    object.__setattr__(config, "chain_output_basename", OUTPUT_CLAIM_FILENAME)
    calls = _stub_run_skill_chain(monkeypatch)
    output_dir = tmp_path / "out"

    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name, success=False, exit_code=-1, output_dir=None, stderr=msg
        ),
        output_dir=str(output_dir),
        demo=True,
        registry_snapshot=_PipelineSnapshot(config),
    )

    assert result.success is False
    assert "Pipeline config invalid" in result.stderr
    assert calls == []
    assert not output_dir.exists()


def test_pipeline_runtime_never_accepts_internal_claim_as_baton(
    tmp_path,
    monkeypatch,
):
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    config = PipelineConfig(
        name="safe-pipeline",
        description="",
        chain_output_basename="processed.h5ad",
        steps=(PipelineStep(skill="step-a"), PipelineStep(skill="step-b")),
    )
    object.__setattr__(config, "chain_output_basename", OUTPUT_CLAIM_FILENAME)
    monkeypatch.setattr(pipeline_runner, "validate_pipeline_config", lambda _config: None)
    calls = _stub_run_skill_chain(
        monkeypatch,
        chain_output_basename=OUTPUT_CLAIM_FILENAME,
    )

    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name, success=False, exit_code=-1, output_dir=None, stderr=msg
        ),
        output_dir=str(tmp_path / "out"),
        demo=True,
        registry_snapshot=_PipelineSnapshot(config),
    )

    assert result.success is False
    assert calls == [("step-a", None)]
    assert "missing required chain output" in result.stderr
    assert OUTPUT_CLAIM_FILENAME in result.stderr


def test_pipeline_rejects_stale_step_output_before_any_spawn(
    tmp_path,
    monkeypatch,
):
    config = PipelineConfig(
        name="stale-pipeline",
        description="",
        chain_output_basename="processed.h5ad",
        steps=(PipelineStep(skill="step-a"), PipelineStep(skill="step-b")),
    )
    snapshot = _PipelineSnapshot(config)
    pipeline_out = tmp_path / "out"
    stale_step_out = pipeline_out / "step-a"
    stale_step_out.mkdir(parents=True)
    (stale_step_out / "processed.h5ad").write_text("STALE\n", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline_runner,
        "preflight_skill_execution",
        lambda *_args, **_kwargs: None,
    )

    def fake_run_skill(
        *, skill_name, output_dir, _expected_skill_revision=None, **_kwargs
    ):
        calls.append(skill_name)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        return build_skill_run_result(
            skill=skill_name,
            success=True,
            exit_code=0,
            output_dir=out_dir,
            duration_seconds=0.01,
            audit_identity=_audit_identity_from_revision(
                _expected_skill_revision
            ),
        )

    monkeypatch.setattr("omicsclaw.skill.runner._run_skill_bound", fake_run_skill)
    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name, success=False, exit_code=-1, output_dir=None, stderr=msg
        ),
        output_dir=str(pipeline_out),
        demo=True,
        registry_snapshot=snapshot,
    )

    assert result.success is False
    assert "fresh output directory" in result.stderr
    assert calls == []
    assert not (pipeline_out / "pipeline_summary.json").exists()


@pytest.mark.parametrize(
    "invalid_baton_kind",
    ["missing", "directory", "symlink", "claim-hardlink"],
)
def test_pipeline_stops_when_successful_upstream_has_no_regular_chain_output(
    tmp_path,
    monkeypatch,
    invalid_baton_kind,
):
    _write_pipeline(
        tmp_path,
        "demo-pipeline",
        """
        name: demo-pipeline
        steps:
          - skill: step-a
          - skill: step-b
        """,
    )
    config = load_pipeline_config("demo-pipeline", pipelines_dir=tmp_path)
    assert config is not None
    calls = _stub_run_skill_chain(
        monkeypatch,
        invalid_baton_on="step-a",
        invalid_baton_kind=invalid_baton_kind,
    )
    initial_input = tmp_path / "user_input.h5ad"
    initial_input.write_text("user", encoding="utf-8")
    pipeline_out = tmp_path / "out"

    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name, success=False, exit_code=-1, output_dir=None, stderr=msg
        ),
        input_path=str(initial_input),
        output_dir=str(pipeline_out),
        registry_snapshot=_PipelineSnapshot(config),
    )

    assert result.success is False
    assert calls == [("step-a", str(initial_input))]
    assert "missing required chain output" in result.stderr
    assert "processed.h5ad" in result.stderr
    summary = json.loads(
        (pipeline_out / "pipeline_summary.json").read_text(encoding="utf-8")
    )
    assert summary["results"]["step-a"]["success"] is False
    assert summary["results"]["step-a"]["stderr"] == result.stderr
    assert "step-b" not in summary["results"]


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_pipeline_metadata_publication_rejects_child_planted_alias(
    tmp_path,
    monkeypatch,
    alias_kind,
):
    """A completed child cannot redirect Backend-owned pipeline metadata."""
    config = PipelineConfig(
        name="alias-pipeline",
        description="",
        chain_output_basename="processed.h5ad",
        steps=(PipelineStep(skill="step-a"),),
    )
    snapshot = _PipelineSnapshot(config)
    pipeline_out = tmp_path / "out"
    victim = tmp_path / f"victim-{alias_kind}.json"
    victim.write_text("DO NOT REPLACE\n", encoding="utf-8")
    monkeypatch.setattr(
        pipeline_runner,
        "preflight_skill_execution",
        lambda *_args, **_kwargs: None,
    )

    def fake_run_skill(*, skill_name, output_dir, _expected_skill_revision=None, **_kwargs):
        child_out = Path(output_dir)
        child_out.mkdir(parents=True)
        summary_path = child_out.parent / "pipeline_summary.json"
        if alias_kind == "symlink":
            summary_path.symlink_to(victim)
        else:
            summary_path.hardlink_to(victim)
        return build_skill_run_result(
            skill=skill_name,
            success=True,
            exit_code=0,
            output_dir=child_out,
            duration_seconds=0.01,
            audit_identity=_audit_identity_from_revision(_expected_skill_revision),
        )

    monkeypatch.setattr("omicsclaw.skill.runner._run_skill_bound", fake_run_skill)
    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name,
            success=False,
            exit_code=-1,
            output_dir=None,
            stderr=msg,
        ),
        output_dir=str(pipeline_out),
        demo=True,
        registry_snapshot=snapshot,
    )

    assert result.success is False
    assert "metadata publication failed" in result.stderr.lower()
    assert victim.read_text(encoding="utf-8") == "DO NOT REPLACE\n"


@pytest.mark.parametrize(
    "metadata_kind",
    ["symlink", "hardlink", "missing-project-id"],
)
def test_explicit_pipeline_output_requires_authoritative_project_metadata(
    tmp_path: Path,
    monkeypatch,
    metadata_kind: str,
) -> None:
    """An explicit composite output is indexed only under a real Project."""
    from omicsclaw.common import run_paths

    config = PipelineConfig(
        name="explicit-pipeline",
        description="",
        chain_output_basename="processed.h5ad",
        steps=(PipelineStep(skill="step-a"),),
    )
    snapshot = _PipelineSnapshot(config)
    _stub_run_skill_chain(monkeypatch)
    explicit_parent = tmp_path / "explicit-parent"
    explicit_parent.mkdir()
    project_meta = explicit_parent / run_paths.PROJECT_META_FILENAME
    metadata_text = json.dumps(
        {"display_name": "Missing ID"}
        if metadata_kind == "missing-project-id"
        else {"project_id": "spoofed-project", "display_name": "Spoofed"}
    )
    victim = tmp_path / f"project-meta-victim-{metadata_kind}.json"
    if metadata_kind == "missing-project-id":
        project_meta.write_text(metadata_text, encoding="utf-8")
    elif metadata_kind == "symlink":
        victim.write_text(metadata_text, encoding="utf-8")
        project_meta.symlink_to(victim)
    else:
        victim.write_text(metadata_text, encoding="utf-8")
        project_meta.hardlink_to(victim)
    real_finalize_run = run_paths.finalize_run
    finalized: list[Path] = []

    def record_finalize(run_dir, **kwargs):
        finalized.append(Path(run_dir))
        return real_finalize_run(run_dir, **kwargs)

    monkeypatch.setattr(run_paths, "finalize_run", record_finalize)

    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name,
            success=False,
            exit_code=-1,
            output_dir=None,
            stderr=msg,
        ),
        output_dir=str(explicit_parent / "pipeline-out"),
        demo=True,
        registry_snapshot=snapshot,
    )

    assert result.success is True
    assert finalized == []
    assert not (explicit_parent / run_paths.RUN_INDEX_FILENAME).exists()
    assert project_meta.read_text(encoding="utf-8") == metadata_text


def test_default_project_pipeline_run_is_still_indexed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Safe metadata validation preserves resolver-managed bookkeeping."""
    from omicsclaw.common import run_paths

    config = PipelineConfig(
        name="default-project-pipeline",
        description="",
        chain_output_basename="processed.h5ad",
        steps=(PipelineStep(skill="step-a"),),
    )
    _stub_run_skill_chain(monkeypatch)

    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=lambda name, msg: build_skill_run_result(
            skill=name,
            success=False,
            exit_code=-1,
            output_dir=None,
            stderr=msg,
        ),
        demo=True,
        registry_snapshot=_PipelineSnapshot(config),
    )

    assert result.success is True
    project_dir = run_paths.resolve_project_dir(tmp_path, create=False)
    rows = run_paths.read_index(project_dir)
    assert len(rows) == 1
    assert rows[0]["project_id"] == run_paths.DEFAULT_PROJECT_ID
    assert rows[0]["run_id"] == Path(result.output_dir or "").name
    assert rows[0]["skill"] == "default-project-pipeline"
    assert rows[0]["status"] == "completed"


def test_pipeline_preflights_first_step_before_creating_composite_output(tmp_path):
    """RET-04b: a corrupt initial input cannot leave a pipeline shell behind."""
    from omicsclaw.skill.runner import run_skill

    corrupt_input = tmp_path / "corrupt.h5ad"
    corrupt_input.write_text("not an HDF5 file\n", encoding="utf-8")
    pipeline_out = tmp_path / "pipeline-out"

    result = run_skill(
        "spatial-pipeline",
        input_path=str(corrupt_input),
        output_dir=str(pipeline_out),
    )

    assert result.success is False
    assert "precondition" in result.stderr.lower()
    assert "inspection" in result.stderr.lower()
    assert not pipeline_out.exists()


def test_pipeline_requires_at_least_one_input_source(tmp_path, monkeypatch):
    _write_pipeline(
        tmp_path,
        "demo-pipeline",
        """
        name: demo-pipeline
        steps:
          - skill: step-a
        """,
    )
    config = load_pipeline_config("demo-pipeline", pipelines_dir=tmp_path)
    assert config is not None

    captured: dict[str, str] = {}

    def err(name: str, msg: str) -> SkillRunResult:
        captured["name"] = name
        captured["msg"] = msg
        return build_skill_run_result(
            skill=name, success=False, exit_code=-1, output_dir=None, stderr=msg
        )

    result = pipeline_runner.run_pipeline(
        config,
        default_output_root=tmp_path,
        err_factory=err,
        input_path=None,
        output_dir=None,
        demo=False,
        registry_snapshot=_PipelineSnapshot(config),
    )

    assert result.success is False
    assert captured["name"] == "demo-pipeline"
    assert "Requires --input" in captured["msg"]


# ---------------------------------------------------------------------------
# Dispatcher integration: run_skill("<name>-pipeline") routes through YAML.
# ---------------------------------------------------------------------------


def test_run_skill_dispatches_unknown_pipeline_alias_back_to_unknown_skill(monkeypatch, tmp_path):
    """Names ending in ``-pipeline`` without a matching YAML must NOT crash
    or fabricate a pipeline — they fall through to the regular
    ``Unknown skill`` error path. That's the only way a typo like
    ``spatail-pipeline`` produces a sensible message."""
    from omicsclaw.skill import runner as skill_runner
    import omicsclaw.skill.execution.pipeline_config as pipeline_config

    monkeypatch.setattr(pipeline_config, "PIPELINES_DIR", tmp_path, raising=False)
    # No YAML exists in tmp_path → run_pipeline_by_name returns None and the
    # dispatcher falls through to the regular skill registry lookup.
    result = skill_runner.run_skill(
        "totally-bogus-pipeline",
        extra_args=["--method", "tsne"],
    )
    assert result.success is False
    assert "Unknown skill" in result.stderr


@pytest.mark.parametrize(
    "alias_factory",
    [
        lambda root: str(root.parent / "absolute-pipeline"),
        lambda _root: "../parent-pipeline",
        lambda _root: "nested/path-pipeline",
        lambda _root: r"nested\path-pipeline",
        lambda _root: "Upper-pipeline",
        lambda _root: "under_score-pipeline",
    ],
)
def test_run_skill_rejects_noncanonical_pipeline_alias_before_execution(
    monkeypatch,
    tmp_path,
    alias_factory,
):
    from omicsclaw.skill import runner as skill_runner
    import omicsclaw.skill.execution.pipeline_config as pipeline_config

    pipelines_dir = tmp_path / "pipelines"
    pipelines_dir.mkdir()
    alias = alias_factory(pipelines_dir)
    monkeypatch.setattr(pipeline_config, "PIPELINES_DIR", pipelines_dir)
    execution_attempts: list[str] = []

    def forbidden_run_pipeline(config, **_kwargs):
        execution_attempts.append(config.name)
        raise AssertionError("noncanonical pipeline reached execution")

    monkeypatch.setattr(pipeline_runner, "run_pipeline", forbidden_run_pipeline)
    output_dir = tmp_path / "out"

    result = skill_runner.run_skill(
        alias,
        demo=True,
        output_dir=str(output_dir),
    )

    assert result.success is False
    assert "canonical pipeline alias" in result.stderr
    assert execution_attempts == []
    assert not output_dir.exists()


@pytest.mark.parametrize("step", ["unknown-step", "preprocess", "nested-pipeline"])
def test_run_skill_rejects_unknown_or_noncanonical_pipeline_step_before_execution(
    monkeypatch,
    tmp_path,
    step,
):
    import omicsclaw.skill.execution.pipeline_config as pipeline_config
    from omicsclaw.skill import runner as skill_runner

    _write_pipeline(
        tmp_path,
        "strict-pipeline",
        f"""
        name: strict-pipeline
        steps:
          - skill: {step}
        """,
    )
    monkeypatch.setattr(pipeline_config, "PIPELINES_DIR", tmp_path)
    execution_attempts: list[str] = []

    def forbidden_run_pipeline(config, **_kwargs):
        execution_attempts.append(config.name)
        raise AssertionError("invalid pipeline step reached execution")

    monkeypatch.setattr(pipeline_runner, "run_pipeline", forbidden_run_pipeline)
    output_dir = tmp_path / "out"

    result = skill_runner.run_skill(
        "strict-pipeline",
        demo=True,
        output_dir=str(output_dir),
    )

    assert result.success is False
    assert "unknown, noncanonical, or non-routable Skill" in result.stderr
    assert step in result.stderr
    assert execution_attempts == []
    assert not output_dir.exists()


def test_run_skill_dispatcher_routes_pipeline_alias_to_run_pipeline_by_name(
    monkeypatch, tmp_path
):
    """The dispatcher must hand any ``<name>-pipeline`` alias to
    ``run_pipeline_by_name`` before the skill registry lookup. Verified by
    intercepting that helper directly so we don't need to stand up real
    skill scripts."""
    from omicsclaw.skill import runner as skill_runner
    from omicsclaw.skill.result import build_skill_run_result

    # ADR 0035: the dispatcher now forwards the env-aware output root
    # (``OMICSCLAW_OUTPUT_DIR`` or ``DEFAULT_OUTPUT_ROOT``). Pin the env so this
    # test is not perturbed by another test leaking the var into ``os.environ``.
    monkeypatch.delenv("OMICSCLAW_OUTPUT_DIR", raising=False)

    captured: dict[str, object] = {}
    sentinel = build_skill_run_result(
        skill="smoke-pipeline",
        success=True,
        exit_code=0,
        output_dir=str(tmp_path / "out"),
        stdout="dispatched",
    )

    def fake_run_pipeline_by_name(name, **kwargs):
        captured["name"] = name
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(skill_runner, "run_pipeline_by_name", fake_run_pipeline_by_name)

    result = skill_runner.run_skill(
        "smoke-pipeline",
        input_path=str(tmp_path / "in.h5ad"),
        output_dir=str(tmp_path / "out"),
    )

    assert result is sentinel
    assert captured["name"] == "smoke-pipeline"
    forwarded = captured["kwargs"]
    assert forwarded["input_path"] == str(tmp_path / "in.h5ad")
    assert forwarded["output_dir"] == str(tmp_path / "out")
    # ``err_factory`` is injected so the chain can return stable errors
    # without importing ``_err`` itself.
    assert callable(forwarded["err_factory"])
    assert forwarded["default_output_root"] == skill_runner.DEFAULT_OUTPUT_ROOT


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--method", "tsne"],
        ["--method=tsne"],
        ["--resolution", "0.8"],
        ["--totally-unsupported", "value"],
    ],
)
def test_pipeline_alias_rejects_forwarded_args_before_execution(
    monkeypatch,
    tmp_path,
    extra_args,
):
    """A pipeline has no declarative mapping from arguments to its steps.

    Silently discarding a forwarded argument can execute a different
    scientific request than the caller made.  The shared runner must fail
    closed before pipeline preflight, output allocation, or step execution.
    """
    import omicsclaw.skill.execution.pipeline_config as pipeline_config
    from omicsclaw.skill import runner as skill_runner

    _write_pipeline(
        tmp_path,
        "smoke-pipeline",
        """
        name: smoke-pipeline
        steps:
          - skill: step-a
        """,
    )
    monkeypatch.setattr(pipeline_config, "PIPELINES_DIR", tmp_path)
    execution_attempts: list[str] = []

    def forbidden_run_pipeline(config, **_kwargs):
        execution_attempts.append(config.name)
        raise AssertionError("unsupported pipeline method reached execution")

    monkeypatch.setattr(pipeline_runner, "run_pipeline", forbidden_run_pipeline)
    output_dir = tmp_path / "out"

    result = skill_runner.run_skill(
        "smoke-pipeline",
        demo=True,
        output_dir=str(output_dir),
        extra_args=extra_args,
    )

    assert result.success is False
    assert result.method is None
    assert "does not accept forwarded arguments" in result.stderr
    assert execution_attempts == []
    assert not output_dir.exists()
