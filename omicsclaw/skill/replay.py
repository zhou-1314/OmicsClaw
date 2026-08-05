"""Backend-owned orchestration for verified canonical Skill replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, TYPE_CHECKING

from omicsclaw.control import UnassignedScope
from omicsclaw.control.run_runtime import RunTerminalResultPending
from omicsclaw.control.simple_skill_adapter import execute_simple_skill_demo
from omicsclaw.skill.execution.reproducibility import (
    SkillReplayCapsuleError,
    load_skill_replay_capsule,
    verify_skill_replay_capsules,
)
from omicsclaw.skill.registry import ensure_registry_loaded

if TYPE_CHECKING:
    from omicsclaw.control.run_runtime import RunRuntime


@dataclass(frozen=True, slots=True)
class SkillReplayResult:
    """One fresh Run and the semantic comparison with its source Run."""

    skill: str
    success: bool
    verified: bool
    source_run_id: str = ""
    code: str = ""
    run_id: str = ""
    output_dir: str = ""
    replay_path: str = ""
    mismatches: tuple[str, ...] = ()


class SkillReplaySourceError(RuntimeError):
    """The source Run cannot safely authorize a fresh replay."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def replay_simple_skill_demo_run(
    source_run_id: str,
    *,
    run_runtime: "RunRuntime",
    run_submission_id: str,
) -> SkillReplayResult:
    """Replay a verified demo capsule selected only through an opaque Run ID.

    The Runtime resolves the local capsule path from verified terminal evidence;
    no caller-controlled workspace path crosses this boundary.  The fresh Run is
    submitted back to the same Runtime with the source Run recorded as retry
    provenance, so idempotency covers both the Skill intent and its source.
    """

    try:
        source = await run_runtime.get_terminal_result(source_run_id)
    except (KeyError, ValueError) as exc:
        raise SkillReplaySourceError("source_run_not_found") from exc
    except RunTerminalResultPending as exc:
        raise SkillReplaySourceError("source_run_not_terminal") from exc

    if not source.success or source.output is None:
        raise SkillReplaySourceError("source_run_not_succeeded")
    source_capsule_path = str(source.output.replay_path or "")
    if not source_capsule_path:
        raise SkillReplaySourceError("source_replay_capsule_missing")
    try:
        expected = load_skill_replay_capsule(source_capsule_path)
    except SkillReplayCapsuleError as exc:
        raise SkillReplaySourceError("source_replay_capsule_invalid") from exc

    revision = expected["skill_revision"]
    skill = str(revision["skill_id"])
    if source.skill_id != skill:
        raise SkillReplaySourceError("source_skill_identity_mismatch")
    try:
        current_revision = (
            ensure_registry_loaded().snapshot().skill_revision(skill)
        )
    except Exception as exc:
        raise SkillReplaySourceError("skill_revision_unavailable") from exc
    if dict(current_revision) != dict(revision):
        raise SkillReplaySourceError("skill_revision_mismatch")

    forwarded_args = tuple(
        str(item) for item in expected["invocation"]["forwarded_args"]
    )
    if expected["input"].get("kind") != "demo" or forwarded_args:
        raise SkillReplaySourceError("source_run_not_simple_demo")

    fresh = await execute_simple_skill_demo(
        skill,
        run_runtime=run_runtime,
        submission_id_factory=lambda: run_submission_id,
        scope=UnassignedScope(),
        retry_of_run_id=source_run_id,
    )
    fresh_run_id = str(_value(fresh, "run_id", "") or "")
    output_dir = str(_value(fresh, "output_dir", "") or "")
    if not bool(_value(fresh, "success", False)):
        return _failure(
            skill,
            source_run_id,
            "fresh_run_failed",
            run_id=fresh_run_id,
            output_dir=output_dir,
        )

    observed_path = str(_value(fresh, "replay_path", "") or "")
    if not observed_path:
        return _failure(
            skill,
            source_run_id,
            "fresh_replay_capsule_missing",
            run_id=fresh_run_id,
            output_dir=output_dir,
        )
    try:
        observed = load_skill_replay_capsule(observed_path)
        mismatches = verify_skill_replay_capsules(expected, observed)
    except SkillReplayCapsuleError:
        return _failure(
            skill,
            source_run_id,
            "fresh_replay_capsule_invalid",
            run_id=fresh_run_id,
            output_dir=output_dir,
            replay_path=observed_path,
        )
    return SkillReplayResult(
        skill=skill,
        success=not mismatches,
        verified=not mismatches,
        source_run_id=source_run_id,
        code="" if not mismatches else "replay_verification_failed",
        run_id=fresh_run_id,
        output_dir=output_dir,
        replay_path=observed_path,
        mismatches=mismatches,
    )


def _value(result: object, name: str, default: object) -> object:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _failure(
    skill: str,
    source_run_id: str,
    code: str,
    *,
    run_id: str = "",
    output_dir: str = "",
    replay_path: str = "",
) -> SkillReplayResult:
    return SkillReplayResult(
        skill=skill,
        success=False,
        verified=False,
        source_run_id=source_run_id,
        code=code,
        run_id=run_id,
        output_dir=output_dir,
        replay_path=replay_path,
    )


__all__ = [
    "SkillReplayResult",
    "SkillReplaySourceError",
    "replay_simple_skill_demo_run",
]
