from __future__ import annotations

from pathlib import Path

import pytest

from omicsclaw.skill.result import (
    SkillRunAuditIdentity,
    build_skill_run_result,
    coerce_skill_run_result,
)


def test_skill_run_result_normalizes_failed_zero_exit_for_adapters():
    result = coerce_skill_run_result(
        {
            "skill": "demo-skill",
            "success": False,
            "exit_code": 0,
            "stdout": "",
            "stderr": "missing dependency",
        }
    )

    assert result.adapter_exit_code == 1
    assert result.error_text(default="skill_runner_failed") == "missing dependency"
    assert result.combined_output == "missing dependency"


def test_skill_run_result_preserves_run_skill_legacy_dict_shape():
    result = coerce_skill_run_result(
        {
            "skill": "demo-skill",
            "success": True,
            "exit_code": 0,
            "output_dir": "/tmp/demo",
            "files": ("result.json",),
            "stdout": "ok",
            "stderr": "",
            "duration_seconds": 1.25,
            "method": "demo",
            "readme_path": "/tmp/demo/README.md",
            "notebook_path": "/tmp/demo/reproducibility/analysis_notebook.ipynb",
        }
    )

    assert result.to_legacy_dict() == {
        "skill": "demo-skill",
        "success": True,
        "exit_code": 0,
        "output_dir": "/tmp/demo",
        "files": ["result.json"],
        "stdout": "ok",
        "stderr": "",
        "duration_seconds": 1.25,
        "method": "demo",
        "readme_path": "/tmp/demo/README.md",
        "notebook_path": "/tmp/demo/reproducibility/analysis_notebook.ipynb",
    }


def test_build_skill_run_result_normalizes_runner_fields_for_legacy_dict():
    result = build_skill_run_result(
        skill="demo-skill",
        success=True,
        exit_code=0,
        output_dir=Path("/tmp/demo"),
        files=[Path("result.json"), "report.md"],
        stdout="ok",
        stderr="",
        duration_seconds=1.234,
        method="demo",
        readme_path=Path("/tmp/demo/README.md"),
        notebook_path=Path("/tmp/demo/reproducibility/analysis_notebook.ipynb"),
    )

    assert result.to_legacy_dict() == {
        "skill": "demo-skill",
        "success": True,
        "exit_code": 0,
        "output_dir": "/tmp/demo",
        "files": ["result.json", "report.md"],
        "stdout": "ok",
        "stderr": "",
        "duration_seconds": 1.23,
        "method": "demo",
        "readme_path": "/tmp/demo/README.md",
        "notebook_path": "/tmp/demo/reproducibility/analysis_notebook.ipynb",
    }


def test_build_skill_run_result_treats_single_file_string_as_one_file():
    result = build_skill_run_result(
        skill="demo-skill",
        success=True,
        exit_code=0,
        output_dir="/tmp/demo",
        files="result.json",
    )

    assert result.files == ("result.json",)


def test_audit_identity_round_trips_internally_without_expanding_legacy_shape():
    identity = SkillRunAuditIdentity(
        skill_id="demo-skill",
        skill_version="1.2.3",
        skill_hash="a" * 64,
        source_hash="b" * 64,
        environment_id="env:" + "c" * 20,
    )
    built = build_skill_run_result(
        skill="demo-skill",
        success=True,
        exit_code=0,
        output_dir="/tmp/demo",
        audit_identity=identity,
    )

    assert built.audit_identity is identity
    assert "audit_identity" not in built.to_legacy_dict()
    assert "_audit_identity" not in built.to_legacy_dict()

    coerced = coerce_skill_run_result(
        built.to_legacy_dict() | {"_audit_identity": identity}
    )
    assert coerced.audit_identity == identity
    assert "_audit_identity" not in coerced.raw


def test_mapping_cannot_self_assert_frozen_audit_identity():
    forged = {
        "skill_id": "attacker-selected",
        "skill_version": "999",
        "skill_hash": "a" * 64,
        "source_hash": "b" * 64,
        "environment_id": "env:" + "c" * 20,
    }

    result = coerce_skill_run_result(
        {
            "skill": "demo-skill",
            "success": True,
            "exit_code": 0,
            "_audit_identity": forged,
        }
    )

    assert result.audit_identity is None
    assert "_audit_identity" not in result.raw


@pytest.mark.parametrize(
    ("skill_id", "skill_version"),
    [
        ("patient secret", "1.0.0"),
        ("valid-skill", "line\nbreak"),
        ("valid-skill", "v" * 129),
    ],
)
def test_audit_identity_rejects_noncanonical_or_unbounded_identity_text(
    skill_id,
    skill_version,
):
    with pytest.raises(ValueError):
        SkillRunAuditIdentity(
            skill_id=skill_id,
            skill_version=skill_version,
            skill_hash="a" * 64,
            source_hash="b" * 64,
            environment_id="env:" + "c" * 20,
        )
