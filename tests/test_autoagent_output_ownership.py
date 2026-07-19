from __future__ import annotations

import json

import pytest

from omicsclaw.autoagent.output_ownership import (
    bind_unclaimed_trial_output,
    claim_session_output_root,
    verify_child_trial_receipt,
)
from omicsclaw.skill.execution.output_ownership import (
    bind_output_claim_audit_identity,
    claim_fresh_output_directory,
)
from omicsclaw.skill.result import SkillRunAuditIdentity


_MANIFEST_HASH = "sha256:" + "a" * 64
_SOURCE_HASH = "sha256:" + "b" * 64


def _write_valid_result(output_dir) -> None:
    (output_dir / "result.json").write_text(
        json.dumps(
            {
                "skill": "test-skill",
                "version": "1.2.3",
                "completed_at": "2026-07-17T00:00:00+00:00",
                "input_checksum": "",
                "summary": {},
                "data": {},
                "status": "ok",
            }
        ),
        encoding="utf-8",
    )


def test_session_claim_rejects_existing_empty_directory(tmp_path):
    existing = tmp_path / "existing-empty"
    existing.mkdir()

    with pytest.raises(ValueError, match="already exists"):
        claim_session_output_root(existing)

    assert list(existing.iterdir()) == []


@pytest.mark.parametrize("with_parent_escape", [False, True])
def test_session_claim_rejects_raw_symlink_ancestor_before_creation(
    tmp_path,
    with_parent_escape,
):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    requested = (
        alias / ".." / "escaped-session"
        if with_parent_escape
        else alias / "session"
    )

    with pytest.raises(ValueError, match="symbolic link"):
        claim_session_output_root(requested)

    assert not (real / "session").exists()
    assert not (tmp_path / "escaped-session").exists()


def test_trial_binding_rejects_raw_symlink_ancestor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        bind_unclaimed_trial_output(alias / "trial_0000")

    assert not (real / "trial_0000").exists()


def test_session_claim_rejects_symlink_loop_before_resolve(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)

    with pytest.raises(ValueError, match="symbolic link"):
        claim_session_output_root(loop / "session")

    assert loop.is_symlink()


def test_plain_parent_reference_remains_compatible_for_session_claim(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    requested = base / "unused" / ".." / "session"

    claimed = claim_session_output_root(requested)

    assert claimed == base / "session"
    assert claimed.is_dir()
    marker = json.loads(
        (claimed / ".omicsclaw-autoagent-session").read_text(encoding="utf-8")
    )
    assert marker["owner"] == "autoagent-session"
    assert marker["claim_id"]


def test_plain_parent_reference_remains_compatible_for_trial_binding(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    requested = base / "unused" / ".." / "trial_0000"

    bound = bind_unclaimed_trial_output(requested)

    assert bound == base / "trial_0000"
    assert not bound.exists()


def test_verified_trial_receipt_binds_frozen_authority_and_actual_runtime(
    tmp_path,
):
    output_dir = claim_fresh_output_directory(
        tmp_path / "trial",
        owner="skill:test-skill",
    )
    bind_output_claim_audit_identity(
        output_dir,
        owner="skill:test-skill",
        audit_identity=SkillRunAuditIdentity(
            skill_id="test-skill",
            skill_version="1.2.3",
            skill_hash=_MANIFEST_HASH,
            source_hash=_SOURCE_HASH,
            environment_id="env:" + "c" * 20,
        ),
        runtime_source="venv:trial-env",
    )
    _write_valid_result(output_dir)

    receipt = verify_child_trial_receipt(
        output_dir,
        canonical_skill_id="test-skill",
        skill_version="1.2.3",
        manifest_hash=_MANIFEST_HASH,
        source_hash=_SOURCE_HASH,
    )

    audit = receipt.to_audit_dict()
    assert audit["manifest_hash"] == _MANIFEST_HASH
    assert audit["source_hash"] == _SOURCE_HASH
    assert audit["environment_id"] == "env:" + "c" * 20
    assert audit["runtime_source"] == "venv:trial-env"
    assert audit["claim_sha256"].startswith("sha256:")
    assert audit["result_sha256"].startswith("sha256:")


def test_trial_receipt_rejects_missing_claim_audit_identity(tmp_path):
    output_dir = claim_fresh_output_directory(
        tmp_path / "trial",
        owner="skill:test-skill",
    )
    _write_valid_result(output_dir)

    with pytest.raises(ValueError, match="no bound execution audit identity"):
        verify_child_trial_receipt(
            output_dir,
            canonical_skill_id="test-skill",
            skill_version="1.2.3",
            manifest_hash=_MANIFEST_HASH,
            source_hash=_SOURCE_HASH,
        )


def test_trial_receipt_rejects_mismatched_frozen_source_authority(tmp_path):
    output_dir = claim_fresh_output_directory(
        tmp_path / "trial",
        owner="skill:test-skill",
    )
    bind_output_claim_audit_identity(
        output_dir,
        owner="skill:test-skill",
        audit_identity=SkillRunAuditIdentity(
            skill_id="test-skill",
            skill_version="1.2.3",
            skill_hash=_MANIFEST_HASH,
            source_hash="sha256:" + "d" * 64,
            environment_id="env:" + "c" * 20,
        ),
        runtime_source="base",
    )
    _write_valid_result(output_dir)

    with pytest.raises(ValueError, match="does not match trial authority"):
        verify_child_trial_receipt(
            output_dir,
            canonical_skill_id="test-skill",
            skill_version="1.2.3",
            manifest_hash=_MANIFEST_HASH,
            source_hash=_SOURCE_HASH,
        )


def test_trial_receipt_rejects_unknown_environment(tmp_path):
    output_dir = claim_fresh_output_directory(
        tmp_path / "trial",
        owner="skill:test-skill",
    )
    bind_output_claim_audit_identity(
        output_dir,
        owner="skill:test-skill",
        audit_identity=SkillRunAuditIdentity(
            skill_id="test-skill",
            skill_version="1.2.3",
            skill_hash=_MANIFEST_HASH,
            source_hash=_SOURCE_HASH,
            environment_id="unknown",
        ),
        runtime_source="base",
    )
    _write_valid_result(output_dir)

    with pytest.raises(ValueError, match="environment is unknown"):
        verify_child_trial_receipt(
            output_dir,
            canonical_skill_id="test-skill",
            skill_version="1.2.3",
            manifest_hash=_MANIFEST_HASH,
            source_hash=_SOURCE_HASH,
        )


@pytest.mark.parametrize("runtime_source", [None, "unknown", " base "])
def test_trial_receipt_rejects_missing_or_invalid_runtime_source(
    tmp_path,
    runtime_source,
):
    output_dir = claim_fresh_output_directory(
        tmp_path / "trial",
        owner="skill:test-skill",
    )
    bind_output_claim_audit_identity(
        output_dir,
        owner="skill:test-skill",
        audit_identity=SkillRunAuditIdentity(
            skill_id="test-skill",
            skill_version="1.2.3",
            skill_hash=_MANIFEST_HASH,
            source_hash=_SOURCE_HASH,
            environment_id="env:" + "c" * 20,
        ),
        runtime_source="base",
    )
    claim_path = output_dir / ".omicsclaw-run-claim.json"
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    if runtime_source is None:
        claim.pop("runtime_source")
    else:
        claim["runtime_source"] = runtime_source
    claim_path.write_text(json.dumps(claim), encoding="utf-8")
    _write_valid_result(output_dir)

    with pytest.raises(ValueError, match="runtime source"):
        verify_child_trial_receipt(
            output_dir,
            canonical_skill_id="test-skill",
            skill_version="1.2.3",
            manifest_hash=_MANIFEST_HASH,
            source_hash=_SOURCE_HASH,
        )
