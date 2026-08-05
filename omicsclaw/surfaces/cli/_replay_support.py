"""CLI Adapter for executing and verifying one Skill Replay Capsule."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from omicsclaw.control import UnassignedScope
from omicsclaw.skill.execution.reproducibility import (
    SkillReplayCapsuleError,
    load_skill_replay_capsule,
    verify_replay_input_mappings,
    verify_skill_replay_capsules,
)
from omicsclaw.skill.registry import ensure_registry_loaded
from omicsclaw.skill.runner import run_skill

from ._canonical_run_support import run_root_canonical_demo


@dataclass(frozen=True, slots=True)
class SkillReplayResult:
    """One fresh replay attempt and its semantic verification outcome."""

    skill: str
    success: bool
    verified: bool
    code: str = ""
    run_id: str = ""
    output_dir: str = ""
    replay_path: str = ""
    mismatches: tuple[str, ...] = ()


def replay_skill_capsule(
    capsule_path: str | Path,
    *,
    workspace_dir: str | Path,
    input_paths: Sequence[str] = (),
) -> SkillReplayResult:
    """Create one fresh Skill Run and compare its Replay Capsule.

    Exact demos use the canonical RunRuntime Adapter with explicit
    ``UnassignedScope`` so replay never consumes mutable project-navigation
    state. Other already-supported standard Skill invocations retain the
    shared runner until their Control Run Adapter is migrated.
    """

    try:
        expected = load_skill_replay_capsule(capsule_path)
    except SkillReplayCapsuleError:
        return _failure("", "replay_capsule_invalid")
    revision = expected["skill_revision"]
    skill = str(revision["skill_id"])
    try:
        current_revision = ensure_registry_loaded().snapshot().skill_revision(skill)
    except Exception:
        return _failure(skill, "skill_revision_unavailable")
    if dict(current_revision) != dict(revision):
        return _failure(
            skill,
            "skill_revision_mismatch",
            mismatches=("skill_revision_mismatch",),
        )

    input_document = expected["input"]
    try:
        mapped_inputs = verify_replay_input_mappings(input_document, input_paths)
    except SkillReplayCapsuleError:
        return _failure(
            skill,
            "input_evidence_mismatch",
            mismatches=("input_evidence_mismatch",),
        )
    forwarded_args = tuple(
        str(item) for item in expected["invocation"]["forwarded_args"]
    )
    if input_document.get("kind") == "demo" and not forwarded_args:
        fresh = run_root_canonical_demo(
            skill,
            workspace_dir=workspace_dir,
            scope=UnassignedScope(),
        )
    else:
        fresh = run_skill(
            skill,
            input_path=mapped_inputs[0] if len(mapped_inputs) == 1 else None,
            input_paths=list(mapped_inputs) if len(mapped_inputs) > 1 else None,
            demo=input_document.get("kind") == "demo",
            extra_args=list(forwarded_args) if forwarded_args else None,
        )

    if not bool(_value(fresh, "success", False)):
        return _failure(
            skill,
            str(_value(fresh, "stderr", "") or "fresh_run_failed"),
            run_id=str(_value(fresh, "run_id", "") or ""),
            output_dir=str(_value(fresh, "output_dir", "") or ""),
        )
    observed_path = str(_value(fresh, "replay_path", "") or "")
    if not observed_path:
        return _failure(
            skill,
            "fresh_replay_capsule_missing",
            run_id=str(_value(fresh, "run_id", "") or ""),
            output_dir=str(_value(fresh, "output_dir", "") or ""),
        )
    try:
        observed = load_skill_replay_capsule(observed_path)
        mismatches = verify_skill_replay_capsules(expected, observed)
    except SkillReplayCapsuleError:
        return _failure(
            skill,
            "fresh_replay_capsule_invalid",
            run_id=str(_value(fresh, "run_id", "") or ""),
            output_dir=str(_value(fresh, "output_dir", "") or ""),
            replay_path=observed_path,
        )
    return SkillReplayResult(
        skill=skill,
        success=not mismatches,
        verified=not mismatches,
        code="" if not mismatches else "replay_verification_failed",
        run_id=str(_value(fresh, "run_id", "") or ""),
        output_dir=str(_value(fresh, "output_dir", "") or ""),
        replay_path=observed_path,
        mismatches=mismatches,
    )


def _value(result: object, name: str, default: object) -> object:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _failure(
    skill: str,
    code: str,
    *,
    run_id: str = "",
    output_dir: str = "",
    replay_path: str = "",
    mismatches: tuple[str, ...] = (),
) -> SkillReplayResult:
    return SkillReplayResult(
        skill=skill,
        success=False,
        verified=False,
        code=code,
        run_id=run_id,
        output_dir=output_dir,
        replay_path=replay_path,
        mismatches=mismatches,
    )


__all__ = ["SkillReplayResult", "replay_skill_capsule"]
