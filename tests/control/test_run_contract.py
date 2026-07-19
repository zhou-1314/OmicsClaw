from __future__ import annotations

import json

import pytest

from omicsclaw.control.run_contract import (
    ProjectScope,
    SimpleSkillRunSubmission,
    UnassignedScope,
    canonical_run_fingerprint,
)
from omicsclaw.skill.resource_scheduler import ExecutionResourceRequest


RESOURCE = ExecutionResourceRequest(
    cpu_cores=1,
    memory_mib=1024,
    gpu_devices=0,
    threads=1,
    temporary_disk_mib=2048,
)


def _submission(**overrides) -> SimpleSkillRunSubmission:
    values = {
        "run_submission_id": "1" * 32,
        "scope": UnassignedScope(),
        "skill_id": "genomics-vcf-operations",
        "resource_request": RESOURCE,
    }
    values.update(overrides)
    return SimpleSkillRunSubmission(**values)


def test_typed_run_scope_rejects_default_sentinel_and_invalid_project_id() -> None:
    assert UnassignedScope().to_dict() == {"kind": "unassigned"}
    assert ProjectScope("a" * 32).to_dict() == {
        "kind": "project",
        "project_id": "a" * 32,
    }

    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        ProjectScope("default")
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        ProjectScope("A" * 32)


def test_simple_submission_is_strict_demo_only_canonical_semantics() -> None:
    submission = _submission()

    assert submission.run_kind == "skill"
    assert submission.inputs == {
        "skill_id": "genomics-vcf-operations",
        "input": {"kind": "demo"},
    }
    assert submission.parameters == {}
    assert submission.resource_contract == {
        "kind": "simple",
        "request": RESOURCE.to_dict(),
    }

    with pytest.raises(ValueError, match="Run Submission ID"):
        _submission(run_submission_id="submission")
    with pytest.raises(ValueError, match="canonical Skill"):
        _submission(skill_id="VCF Ops")


def test_canonical_fingerprint_is_versioned_stable_and_excludes_submission_id() -> None:
    first = _submission(run_submission_id="1" * 32)
    retry_transport = _submission(run_submission_id="2" * 32)

    version, digest, document = canonical_run_fingerprint(first)
    assert version == 1
    assert len(digest) == 64
    assert canonical_run_fingerprint(retry_transport)[:2] == (version, digest)
    assert "run_submission_id" not in document
    assert (
        json.loads(
            json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )
        == document
    )

    project = _submission(scope=ProjectScope("a" * 32))
    assert canonical_run_fingerprint(project)[1] != digest


def test_resource_semantics_are_part_of_the_fingerprint() -> None:
    larger = ExecutionResourceRequest(
        cpu_cores=2,
        memory_mib=1024,
        gpu_devices=0,
        threads=1,
        temporary_disk_mib=2048,
    )

    assert (
        canonical_run_fingerprint(_submission())[1]
        != canonical_run_fingerprint(_submission(resource_request=larger))[1]
    )
