"""Integration contracts for the standard Skill Replay Capsule."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from omicsclaw.control import UnassignedScope


def _capsule(skill: str = "genomics-vcf-operations") -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "omicsclaw.skill-replay",
        "skill_revision": {
            "skill_id": skill,
            "skill_version": "1.0.0",
            "manifest_hash": "sha256:" + "a" * 64,
            "source_hash": "sha256:" + "b" * 64,
        },
        "input": {"kind": "demo"},
        "parameters": {},
        "invocation": {
            "argv": ["oc", "run", skill, "--demo"],
            "forwarded_args": [],
        },
        "environment": {
            "environment_id": "env:" + "c" * 20,
            "runtime_source": "base",
            "evidence": "reproducibility/environment.json",
        },
        "verification": {
            "mode": "fresh-run-contract",
            "result_semantic_sha256": "sha256:" + "d" * 64,
            "artifacts": [],
        },
    }


def test_replay_exact_demo_creates_fresh_unassigned_run_and_verifies(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from omicsclaw.surfaces.cli import _replay_support as replay_support

    original = _capsule()
    original_path = tmp_path / "original" / "reproducibility" / "replay.json"
    original_path.parent.mkdir(parents=True)
    original_path.write_text(json.dumps(original), encoding="utf-8")

    observed_path = tmp_path / "fresh" / "reproducibility" / "replay.json"
    observed_path.parent.mkdir(parents=True)
    observed_path.write_text(json.dumps(original), encoding="utf-8")

    revision = dict(original["skill_revision"])
    snapshot = SimpleNamespace(skill_revision=lambda _skill: revision)
    monkeypatch.setattr(
        replay_support,
        "ensure_registry_loaded",
        lambda: SimpleNamespace(snapshot=lambda: snapshot),
    )

    calls: list[tuple[str, Path, object]] = []

    def run_canonical(skill: str, *, workspace_dir: str | Path, scope: object):
        calls.append((skill, Path(workspace_dir), scope))
        return {
            "skill": skill,
            "success": True,
            "exit_code": 0,
            "output_dir": str(observed_path.parent.parent),
            "replay_path": str(observed_path),
            "run_id": "e" * 32,
        }

    monkeypatch.setattr(replay_support, "run_root_canonical_demo", run_canonical)

    result = replay_support.replay_skill_capsule(
        original_path,
        workspace_dir=tmp_path,
    )

    assert result.success is True
    assert result.verified is True
    assert result.run_id == "e" * 32
    assert result.mismatches == ()
    assert calls == [
        (
            "genomics-vcf-operations",
            tmp_path,
            UnassignedScope(),
        )
    ]


def test_root_replay_command_reports_verified_fresh_run(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    import omicsclaw.surfaces.cli._main as cli_main
    from omicsclaw.surfaces.cli import _replay_support as replay_support

    capsule_path = tmp_path / "replay.json"
    capsule_path.write_text(json.dumps(_capsule()), encoding="utf-8")
    observed: dict[str, object] = {}

    def replay(capsule, *, workspace_dir, input_paths=()):
        observed.update(
            capsule=Path(capsule),
            workspace_dir=Path(workspace_dir),
            input_paths=tuple(input_paths),
        )
        return replay_support.SkillReplayResult(
            skill="genomics-vcf-operations",
            success=True,
            verified=True,
            run_id="f" * 32,
            output_dir="/tmp/fresh-output",
            replay_path="/tmp/fresh-output/reproducibility/replay.json",
        )

    monkeypatch.setattr(replay_support, "replay_skill_capsule", replay)
    monkeypatch.setattr(sys, "argv", ["oc", "replay", str(capsule_path)])

    cli_main.main()

    assert observed == {
        "capsule": capsule_path,
        "workspace_dir": Path(cli_main.OMICSCLAW_DIR),
        "input_paths": (),
    }
    output = capsys.readouterr()
    assert "VERIFIED" in output.out
    assert "f" * 32 in output.out
    assert "/tmp/fresh-output" in output.out
    assert output.err == ""


def test_replay_capsule_rejects_runner_owned_forwarded_flags(tmp_path: Path) -> None:
    from omicsclaw.skill.execution.reproducibility import (
        SkillReplayCapsuleError,
        load_skill_replay_capsule,
    )

    capsule = _capsule()
    capsule["invocation"] = {
        "argv": [
            "oc",
            "run",
            "genomics-vcf-operations",
            "--demo",
            "--output",
            "/tmp/overwrite",
        ],
        "forwarded_args": ["--output", "/tmp/overwrite"],
    }
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(capsule), encoding="utf-8")

    with pytest.raises(SkillReplayCapsuleError, match="runner-owned"):
        load_skill_replay_capsule(path)


def test_replay_verification_uses_mapped_input_content_not_local_name() -> None:
    from omicsclaw.skill.execution.reproducibility import (
        verify_skill_replay_capsules,
    )

    original = _capsule()
    original["input"] = {
        "kind": "mapped",
        "requires_mapping": True,
        "items": [
            {
                "ordinal": 0,
                "kind": "file",
                "name": "original.vcf",
                "size_bytes": 42,
                "sha256": "sha256:" + "1" * 64,
            }
        ],
    }
    original["invocation"] = {
        "argv": [
            "oc",
            "run",
            "genomics-vcf-operations",
            "--input",
            "${INPUT_1}",
        ],
        "forwarded_args": [],
    }
    observed = json.loads(json.dumps(original))
    observed["input"]["items"][0]["name"] = "local-copy.vcf"

    assert verify_skill_replay_capsules(original, observed) == ()


@pytest.mark.asyncio
async def test_run_id_replay_uses_same_runtime_and_binds_source_provenance(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from omicsclaw.skill import replay as replay_service

    original = _capsule()
    original_path = tmp_path / "original" / "reproducibility" / "replay.json"
    fresh_path = tmp_path / "fresh" / "reproducibility" / "replay.json"
    original_path.parent.mkdir(parents=True)
    fresh_path.parent.mkdir(parents=True)
    original_path.write_text(json.dumps(original), encoding="utf-8")
    fresh_path.write_text(json.dumps(original), encoding="utf-8")

    revision = dict(original["skill_revision"])
    snapshot = SimpleNamespace(skill_revision=lambda _skill: revision)
    monkeypatch.setattr(
        replay_service,
        "ensure_registry_loaded",
        lambda: SimpleNamespace(snapshot=lambda: snapshot),
    )

    source_run_id = "a" * 32
    fresh_run_id = "b" * 32
    runtime = SimpleNamespace(
        get_terminal_result=lambda run_id: _async_value(
            SimpleNamespace(
                success=True,
                skill_id="genomics-vcf-operations",
                output=SimpleNamespace(replay_path=str(original_path)),
            )
        )
    )
    calls: list[dict[str, object]] = []

    async def execute(skill: str, **kwargs):
        calls.append({"skill": skill, **kwargs})
        return {
            "skill": skill,
            "success": True,
            "run_id": fresh_run_id,
            "replay_path": str(fresh_path),
        }

    monkeypatch.setattr(replay_service, "execute_simple_skill_demo", execute)

    result = await replay_service.replay_simple_skill_demo_run(
        source_run_id,
        run_runtime=runtime,
        run_submission_id="c" * 32,
    )

    assert result.success is True
    assert result.verified is True
    assert result.source_run_id == source_run_id
    assert result.run_id == fresh_run_id
    assert calls == [{
        "skill": "genomics-vcf-operations",
        "run_runtime": runtime,
        "submission_id_factory": calls[0]["submission_id_factory"],
        "scope": UnassignedScope(),
        "retry_of_run_id": source_run_id,
    }]
    assert calls[0]["submission_id_factory"]() == "c" * 32


@pytest.mark.asyncio
async def test_run_id_replay_failure_uses_closed_code_not_runner_stderr(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from omicsclaw.skill import replay as replay_service

    original = _capsule()
    original_path = tmp_path / "original" / "reproducibility" / "replay.json"
    original_path.parent.mkdir(parents=True)
    original_path.write_text(json.dumps(original), encoding="utf-8")
    revision = dict(original["skill_revision"])
    snapshot = SimpleNamespace(skill_revision=lambda _skill: revision)
    monkeypatch.setattr(
        replay_service,
        "ensure_registry_loaded",
        lambda: SimpleNamespace(snapshot=lambda: snapshot),
    )
    runtime = SimpleNamespace(
        get_terminal_result=lambda _run_id: _async_value(
            SimpleNamespace(
                success=True,
                skill_id="genomics-vcf-operations",
                output=SimpleNamespace(replay_path=str(original_path)),
            )
        )
    )

    async def execute(_skill: str, **_kwargs):
        return {
            "success": False,
            "stderr": "/private/workspace/token=must-not-cross",
            "run_id": "b" * 32,
        }

    monkeypatch.setattr(replay_service, "execute_simple_skill_demo", execute)

    result = await replay_service.replay_simple_skill_demo_run(
        "a" * 32,
        run_runtime=runtime,
        run_submission_id="c" * 32,
    )

    assert result.code == "fresh_run_failed"
    assert "private" not in result.code


async def _async_value(value):
    return value
