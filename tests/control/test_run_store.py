from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from omicsclaw.common.report import write_result_json
from omicsclaw.control.run_contract import UnassignedScope
from omicsclaw.control.run_store import (
    FilesystemRunStore,
    RunManifestHeader,
    RunStoreIntegrityError,
)
from omicsclaw.skill.result import SkillRunAuditIdentity, SkillRunResult
from omicsclaw.skill.resource_scheduler import ExecutionResourceRequest


RUN_ID = "a" * 32
SUBMISSION_ID = "b" * 32
FINGERPRINT = "c" * 64
REVISION = {
    "skill_id": "genomics-vcf-operations",
    "skill_version": "0.5.0",
    "manifest_hash": "d" * 64,
    "source_hash": "e" * 64,
}
RESOURCE = ExecutionResourceRequest(1, 1024, 0, 1, 2048)
ASSIGNMENT_ID = "f" * 32


def _header(*, run_id: str = RUN_ID) -> RunManifestHeader:
    return RunManifestHeader(
        run_id=run_id,
        run_submission_id=SUBMISSION_ID,
        fingerprint_version=1,
        fingerprint_sha256=FINGERPRINT,
        run_kind="skill",
        scope=UnassignedScope(),
        inputs={
            "skill_id": "genomics-vcf-operations",
            "input": {"kind": "demo"},
        },
        parameters={},
        resource_contract={"kind": "simple", "request": RESOURCE.to_dict()},
        skill_revision=REVISION,
    )


def _successful_result(artifacts: Path) -> SkillRunResult:
    write_result_json(
        artifacts,
        skill="genomics-vcf-operations",
        version="0.5.0",
        input_checksum="",
        summary={"n_variants": 1},
        data={"ok": True},
    )
    (artifacts / "filtered.vcf").write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
    return SkillRunResult(
        skill="genomics-vcf-operations",
        success=True,
        exit_code=0,
        output_dir=str(artifacts),
        files=("filtered.vcf", "result.json"),
        audit_identity=SkillRunAuditIdentity(
            skill_id="genomics-vcf-operations",
            skill_version="0.5.0",
            skill_hash="d" * 64,
            source_hash="e" * 64,
            environment_id="env:" + "f" * 20,
        ),
    )


def test_header_uses_opaque_reference_and_keeps_runner_leaf_fresh(
    tmp_path: Path,
) -> None:
    store = FilesystemRunStore(tmp_path / "output")

    provisional = store.create_header(_header())

    assert provisional.manifest_ref.startswith("run-store:v1:")
    assert str(tmp_path) not in provisional.manifest_ref
    assert provisional.artifacts_dir.parent.name.startswith("genomics-vcf-operations__")
    assert not provisional.artifacts_dir.exists()
    manifest = store.read_manifest(provisional.manifest_ref)
    assert manifest["header"]["run_id"] == RUN_ID
    assert manifest["header"]["scope"] == {"kind": "unassigned"}
    assert manifest["completion"] is None


def test_provisional_header_can_be_abandoned_but_accepted_header_cannot(
    tmp_path: Path,
) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())
    root = provisional.artifacts_dir.parent

    store.abandon(provisional.manifest_ref)
    assert not root.exists()

    accepted = store.create_header(_header())
    store.mark_accepted(accepted.manifest_ref)
    with pytest.raises(RunStoreIntegrityError, match="accepted"):
        store.abandon(accepted.manifest_ref)


def test_receipt_binding_verifies_run_kind_scope_and_acceptance(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())

    with pytest.raises(RunStoreIntegrityError, match="binding mismatch"):
        store.verify_receipt_binding(
            provisional.manifest_ref,
            run_id=RUN_ID,
            run_kind="skill",
            scope_kind="unassigned",
            project_id=None,
        )

    store.mark_accepted(provisional.manifest_ref)
    observed = store.verify_receipt_binding(
        provisional.manifest_ref,
        run_id=RUN_ID,
        run_kind="skill",
        scope_kind="unassigned",
        project_id=None,
    )
    assert observed["header"]["run_id"] == RUN_ID
    with pytest.raises(RunStoreIntegrityError, match="binding mismatch"):
        store.verify_receipt_binding(
            provisional.manifest_ref,
            run_id="9" * 32,
            run_kind="skill",
            scope_kind="unassigned",
            project_id=None,
        )


def test_verified_completion_records_relative_artifact_digests(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())
    store.mark_accepted(provisional.manifest_ref)
    provisional.artifacts_dir.mkdir()
    result = _successful_result(provisional.artifacts_dir)

    evidence = store.commit_success(
        provisional.manifest_ref, result, assignment_id=ASSIGNMENT_ID
    )

    assert len(evidence.manifest_sha256) == 64
    manifest = store.verify_success(
        provisional.manifest_ref, assignment_id=ASSIGNMENT_ID
    )
    inventory = {item["path"]: item for item in manifest["completion"]["artifacts"]}
    assert set(inventory) == {"filtered.vcf", "result.json"}
    assert inventory["filtered.vcf"]["size_bytes"] > 0
    assert len(inventory["filtered.vcf"]["sha256"]) == 64
    assert (
        manifest["completion"]["result_envelope_sha256"]
        == inventory["result.json"]["sha256"]
    )


def test_terminal_projection_exposes_only_verified_owned_output_paths(
    tmp_path: Path,
) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())
    store.mark_accepted(provisional.manifest_ref)
    provisional.artifacts_dir.mkdir()
    result = _successful_result(provisional.artifacts_dir)
    (provisional.artifacts_dir / "README.md").write_text("guide\n", encoding="utf-8")
    notebook = provisional.artifacts_dir / "reproducibility" / "analysis_notebook.ipynb"
    notebook.parent.mkdir()
    notebook.write_text("{}\n", encoding="utf-8")
    store.commit_success(
        provisional.manifest_ref,
        result,
        assignment_id=ASSIGNMENT_ID,
    )

    header = store.project_receipt_header(
        provisional.manifest_ref,
        run_id=RUN_ID,
        run_kind="skill",
        scope_kind="unassigned",
        project_id=None,
    )
    projected = store.project_verified_terminal(
        provisional.manifest_ref,
        run_id=RUN_ID,
        run_kind="skill",
        scope_kind="unassigned",
        project_id=None,
        assignment_id=ASSIGNMENT_ID,
        terminal_status="succeeded",
        terminal_code=None,
    )

    assert header.skill_id == "genomics-vcf-operations"
    assert projected.skill_id == header.skill_id
    assert projected.output is not None
    output = projected.output
    assert output.output_dir == str(provisional.artifacts_dir)
    assert output.readme_path == str(provisional.artifacts_dir / "README.md")
    assert output.notebook_path == str(notebook)

    (provisional.artifacts_dir / "filtered.vcf").write_text(
        "tampered\n", encoding="utf-8"
    )
    with pytest.raises(RunStoreIntegrityError, match="inventory drifted"):
        store.project_verified_terminal(
            provisional.manifest_ref,
            run_id=RUN_ID,
            run_kind="skill",
            scope_kind="unassigned",
            project_id=None,
            assignment_id=ASSIGNMENT_ID,
            terminal_status="succeeded",
            terminal_code=None,
        )


def test_verified_artifact_inventory_and_descriptor_reads_are_deeply_fenced(
    tmp_path: Path,
) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())
    store.mark_accepted(provisional.manifest_ref)
    provisional.artifacts_dir.mkdir()
    result = _successful_result(provisional.artifacts_dir)
    store.commit_success(
        provisional.manifest_ref,
        result,
        assignment_id=ASSIGNMENT_ID,
    )

    inventory = store.project_verified_artifacts(
        provisional.manifest_ref,
        run_id=RUN_ID,
        run_kind="skill",
        scope_kind="unassigned",
        project_id=None,
        assignment_id=ASSIGNMENT_ID,
        terminal_status="succeeded",
        terminal_code=None,
    )
    item = next(
        artifact
        for artifact in inventory.artifacts
        if artifact.relative_path == "filtered.vcf"
    )
    assert inventory.skill_id == "genomics-vcf-operations"
    assert item.sha256
    assert item.size_bytes == len(b"##fileformat=VCFv4.2\n")

    skill_id, opened = store.open_verified_artifact(
        provisional.manifest_ref,
        run_id=RUN_ID,
        run_kind="skill",
        scope_kind="unassigned",
        project_id=None,
        assignment_id=ASSIGNMENT_ID,
        terminal_status="succeeded",
        terminal_code=None,
        relative_path="filtered.vcf",
    )
    assert skill_id == inventory.skill_id
    assert opened.read_chunk(offset=2, max_bytes=6) == b"filefo"
    assert opened.read_chunk(offset=item.size_bytes) == b""
    opened.close()
    opened.close()
    with pytest.raises(RunStoreIntegrityError, match="closed"):
        opened.read_chunk(offset=0, max_bytes=1)


def test_verified_artifact_open_rejects_traversal_and_post_commit_tamper(
    tmp_path: Path,
) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())
    store.mark_accepted(provisional.manifest_ref)
    provisional.artifacts_dir.mkdir()
    result = _successful_result(provisional.artifacts_dir)
    store.commit_success(
        provisional.manifest_ref,
        result,
        assignment_id=ASSIGNMENT_ID,
    )

    common = {
        "run_id": RUN_ID,
        "run_kind": "skill",
        "scope_kind": "unassigned",
        "project_id": None,
        "assignment_id": ASSIGNMENT_ID,
        "terminal_status": "succeeded",
        "terminal_code": None,
    }
    with pytest.raises(ValueError, match="normalized relative"):
        store.open_verified_artifact(
            provisional.manifest_ref,
            relative_path="../filtered.vcf",
            **common,
        )
    with pytest.raises(KeyError):
        store.open_verified_artifact(
            provisional.manifest_ref,
            relative_path="missing.vcf",
            **common,
        )

    (provisional.artifacts_dir / "filtered.vcf").write_text(
        "tampered\n",
        encoding="utf-8",
    )
    with pytest.raises(RunStoreIntegrityError, match="inventory drifted"):
        store.open_verified_artifact(
            provisional.manifest_ref,
            relative_path="filtered.vcf",
            **common,
        )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_verified_descriptor_never_reopens_a_swapped_artifact_path(
    tmp_path: Path,
) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())
    store.mark_accepted(provisional.manifest_ref)
    provisional.artifacts_dir.mkdir()
    result = _successful_result(provisional.artifacts_dir)
    store.commit_success(
        provisional.manifest_ref,
        result,
        assignment_id=ASSIGNMENT_ID,
    )
    _, opened = store.open_verified_artifact(
        provisional.manifest_ref,
        run_id=RUN_ID,
        run_kind="skill",
        scope_kind="unassigned",
        project_id=None,
        assignment_id=ASSIGNMENT_ID,
        terminal_status="succeeded",
        terminal_code=None,
        relative_path="filtered.vcf",
    )
    target = provisional.artifacts_dir / "filtered.vcf"
    original = provisional.artifacts_dir / "filtered.original"
    target.rename(original)
    target.write_text("attacker replacement\n", encoding="utf-8")
    try:
        try:
            observed = opened.read_chunk(offset=0)
        except RunStoreIntegrityError:
            observed = b""
        assert observed != b"attacker replacement\n"
    finally:
        opened.close()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_completion_rejects_hardlinked_or_symlinked_artifacts(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())
    store.mark_accepted(provisional.manifest_ref)
    provisional.artifacts_dir.mkdir()
    result = _successful_result(provisional.artifacts_dir)
    external = tmp_path / "external.txt"
    external.write_text("not owned", encoding="utf-8")
    os.link(external, provisional.artifacts_dir / "hardlink.txt")

    with pytest.raises(RunStoreIntegrityError, match="unsafe artifact"):
        store.commit_success(
            provisional.manifest_ref, result, assignment_id=ASSIGNMENT_ID
        )


def test_completion_rejects_result_or_frozen_revision_mismatch(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())
    store.mark_accepted(provisional.manifest_ref)
    provisional.artifacts_dir.mkdir()
    result = _successful_result(provisional.artifacts_dir)
    bad_payload = json.loads(
        (provisional.artifacts_dir / "result.json").read_text(encoding="utf-8")
    )
    bad_payload.pop("completed_at")
    (provisional.artifacts_dir / "result.json").write_text(
        json.dumps(bad_payload), encoding="utf-8"
    )

    with pytest.raises(RunStoreIntegrityError, match="result envelope"):
        store.commit_success(
            provisional.manifest_ref, result, assignment_id=ASSIGNMENT_ID
        )


def test_failure_completion_never_verifies_as_success(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())
    store.mark_accepted(provisional.manifest_ref)

    store.commit_failure(
        provisional.manifest_ref,
        terminal_code="executor_failed",
        execution_evidence={"exit_code": 1, "error_kind": "skill_failed"},
        assignment_id=ASSIGNMENT_ID,
    )

    with pytest.raises(RunStoreIntegrityError, match="not successful"):
        store.verify_success(provisional.manifest_ref, assignment_id=ASSIGNMENT_ID)


def test_reference_cannot_be_retargeted_or_detached_from_header(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    first = store.create_header(_header(run_id="1" * 32))
    second = store.create_header(_header(run_id="2" * 32))
    first_token = first.manifest_ref.rsplit(":", 1)[1]
    first_reference = (
        tmp_path / "output" / ".run-store" / "refs" / f"{first_token}.json"
    )
    reference_payload = json.loads(first_reference.read_text(encoding="utf-8"))
    reference_payload["relative_root"] = second.artifacts_dir.parent.relative_to(
        tmp_path / "output"
    ).as_posix()
    first_reference.write_text(json.dumps(reference_payload), encoding="utf-8")

    with pytest.raises(RunStoreIntegrityError, match="binding"):
        store.read_manifest(first.manifest_ref)

    manifest_path = second.artifacts_dir.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["header"]["fingerprint_sha256"] = "9" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RunStoreIntegrityError, match="binding"):
        store.read_manifest(second.manifest_ref)


def test_success_binds_result_skill_version_and_runner_identity(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())
    store.mark_accepted(provisional.manifest_ref)
    provisional.artifacts_dir.mkdir()
    result = _successful_result(provisional.artifacts_dir)
    payload_path = provisional.artifacts_dir / "result.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["version"] = "9.9.9"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RunStoreIntegrityError, match="frozen Skill revision"):
        store.commit_success(
            provisional.manifest_ref, result, assignment_id=ASSIGNMENT_ID
        )

    second_store = FilesystemRunStore(tmp_path / "second")
    second = second_store.create_header(_header())
    second_store.mark_accepted(second.manifest_ref)
    second.artifacts_dir.mkdir()
    second_result = replace(
        _successful_result(second.artifacts_dir), skill="other-skill"
    )
    with pytest.raises(RunStoreIntegrityError, match="frozen revision"):
        second_store.commit_success(
            second.manifest_ref,
            second_result,
            assignment_id=ASSIGNMENT_ID,
        )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_store_rejects_internal_or_default_directory_aliases(tmp_path: Path) -> None:
    output = tmp_path / "output"
    external = tmp_path / "external"
    output.mkdir()
    external.mkdir()
    marker = external / "marker.txt"
    marker.write_text("owned elsewhere", encoding="utf-8")
    os.symlink(external, output / ".run-store", target_is_directory=True)
    with pytest.raises(RunStoreIntegrityError, match="alias"):
        FilesystemRunStore(output)
    assert marker.read_text(encoding="utf-8") == "owned elsewhere"

    safe_output = tmp_path / "safe-output"
    store = FilesystemRunStore(safe_output)
    os.symlink(external, safe_output / "default", target_is_directory=True)
    with pytest.raises(RunStoreIntegrityError, match="alias"):
        store.create_header(_header())
    assert marker.read_text(encoding="utf-8") == "owned elsewhere"


def test_stop_and_failure_evidence_are_assignment_fenced(tmp_path: Path) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    stopped = store.create_header(_header(run_id="3" * 32))
    store.mark_accepted(stopped.manifest_ref)
    store.commit_stop(
        stopped.manifest_ref,
        terminal_status="canceled",
        terminal_code="canceled_by_owner",
        assignment_id=ASSIGNMENT_ID,
    )
    store.verify_stop(
        stopped.manifest_ref,
        terminal_status="canceled",
        terminal_code="canceled_by_owner",
        assignment_id=ASSIGNMENT_ID,
    )
    with pytest.raises(ValueError, match="assignment_id"):
        store.verify_stop(
            stopped.manifest_ref,
            terminal_status="canceled",
            terminal_code="canceled_by_owner",
            assignment_id="not-an-assignment",
        )

    restarted = store.create_header(_header(run_id="5" * 32))
    store.mark_accepted(restarted.manifest_ref)
    store.commit_stop(
        restarted.manifest_ref,
        terminal_status="interrupted",
        terminal_code="control_plane_restarted",
        assignment_id=ASSIGNMENT_ID,
    )
    store.verify_stop(
        restarted.manifest_ref,
        terminal_status="interrupted",
        terminal_code="control_plane_restarted",
        assignment_id=ASSIGNMENT_ID,
    )

    failed = store.create_header(_header(run_id="4" * 32))
    store.mark_accepted(failed.manifest_ref)
    evidence = {"exit_code": 1, "error_kind": "skill_failed"}
    store.commit_failure(
        failed.manifest_ref,
        terminal_code="executor_failed",
        execution_evidence=evidence,
        assignment_id=ASSIGNMENT_ID,
    )
    store.verify_failure(
        failed.manifest_ref,
        terminal_code="executor_failed",
        execution_evidence=evidence,
        assignment_id=ASSIGNMENT_ID,
    )


def test_large_verified_inventory_uses_store_limit_not_request_limit(
    tmp_path: Path,
) -> None:
    store = FilesystemRunStore(tmp_path / "output")
    provisional = store.create_header(_header())
    store.mark_accepted(provisional.manifest_ref)
    provisional.artifacts_dir.mkdir()
    result = _successful_result(provisional.artifacts_dir)
    for index in range(600):
        (provisional.artifacts_dir / f"artifact-{index:04d}.txt").write_text(
            "evidence", encoding="utf-8"
        )

    store.commit_success(
        provisional.manifest_ref,
        result,
        assignment_id=ASSIGNMENT_ID,
    )
    manifest = store.verify_success(
        provisional.manifest_ref,
        assignment_id=ASSIGNMENT_ID,
    )
    assert len(manifest["completion"]["artifacts"]) == 602
