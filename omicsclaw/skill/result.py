"""Shared result model for skill runner adapters."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .outcomes import classify_skill_error


_SHA256_OR_UNKNOWN = re.compile(r"(?:(?:sha256:)?[0-9a-f]{64}|unknown)\Z")
_ENVIRONMENT_OR_UNKNOWN = re.compile(r"(?:env:[0-9a-f]{20}|unknown)\Z")
_CANONICAL_SKILL_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class SkillRunAuditIdentity:
    """Frozen, privacy-minimal identity captured before subprocess spawn.

    This value is internal execution provenance.  It is deliberately omitted
    from the legacy result mapping so stable public adapter contracts do not
    grow audit-only fields.
    """

    skill_id: str
    skill_version: str
    skill_hash: str
    source_hash: str
    environment_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.skill_id, str) or not _CANONICAL_SKILL_ID.fullmatch(
            self.skill_id
        ):
            raise ValueError("audit skill_id must be a canonical Skill identifier")
        if (
            not isinstance(self.skill_version, str)
            or not self.skill_version
            or len(self.skill_version) > 128
            or self.skill_version != self.skill_version.strip()
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.skill_version
            )
        ):
            raise ValueError("audit skill_version must be bounded control-free text")
        if not _SHA256_OR_UNKNOWN.fullmatch(self.skill_hash):
            raise ValueError("audit skill_hash must be a SHA-256 digest or unknown")
        if not _SHA256_OR_UNKNOWN.fullmatch(self.source_hash):
            raise ValueError("audit source_hash must be a SHA-256 digest or unknown")
        if not _ENVIRONMENT_OR_UNKNOWN.fullmatch(self.environment_id):
            raise ValueError(
                "audit environment_id must be a governed fingerprint or unknown"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "skill_id": self.skill_id,
            "skill_version": self.skill_version,
            "skill_hash": self.skill_hash,
            "source_hash": self.source_hash,
            "environment_id": self.environment_id,
        }


@dataclass(frozen=True, slots=True)
class SkillRunResult:
    """Normalized view over the public ``run_skill()`` result dictionary."""

    skill: str
    success: bool
    exit_code: int
    output_dir: str | None = None
    files: tuple[str, ...] = ()
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    method: str | None = None
    readme_path: str = ""
    notebook_path: str = ""
    # Adaptive-env provenance: which interpreter served this run —
    # ``base`` | ``skip`` | ``probe`` | ``venv:<key>`` (ADR: adaptive-environment-provisioning).
    runtime_source: str = "base"
    error_kind: str = "none"
    audit_identity: SkillRunAuditIdentity | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def adapter_exit_code(self) -> int:
        """Exit code adapters should expose to job/bot callers."""
        if self.success:
            return self.exit_code
        return self.exit_code if self.exit_code != 0 else 1

    @property
    def combined_output(self) -> str:
        if self.stdout and self.stderr:
            return self.stdout + "\n" + self.stderr
        return self.stdout or self.stderr

    @property
    def output_path(self) -> Path | None:
        return Path(self.output_dir) if self.output_dir else None

    def error_text(self, *, default: str = "unknown error", tail_chars: int | None = None) -> str:
        text = self.stderr or self.stdout or default
        if tail_chars is not None and tail_chars > 0:
            return text[-tail_chars:]
        return text

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return the public dict shape expected by existing ``run_skill`` callers.

        ``runtime_source`` is intentionally NOT emitted here — this dict pins the
        stable shape existing callers assert against. Read provenance from the model
        attribute (``result.runtime_source``) or the ``raw`` mapping instead.
        """
        return {
            "skill": self.skill,
            "success": self.success,
            "exit_code": self.exit_code,
            "output_dir": self.output_dir,
            "files": list(self.files),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "method": self.method,
            "readme_path": self.readme_path,
            "notebook_path": self.notebook_path,
        }


def _int_or_default(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_or_default(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_files(files: Any) -> tuple[str, ...]:
    if not files:
        return ()
    if isinstance(files, (str, bytes, Path)):
        return (str(files),)
    return tuple(str(item) for item in files)


def coerce_skill_run_result(result: Mapping[str, Any]) -> SkillRunResult:
    """Coerce a runner result mapping into a normalized result model."""
    skill = str(result.get("skill") or "")
    success = bool(result.get("success", False))
    exit_code = _int_or_default(result.get("exit_code"), 0)
    output_dir_value = result.get("output_dir")
    method_value = result.get("method")
    raw_audit_identity = result.get("_audit_identity")
    audit_identity = (
        raw_audit_identity
        if isinstance(raw_audit_identity, SkillRunAuditIdentity)
        else None
    )
    return SkillRunResult(
        skill=skill,
        success=success,
        exit_code=exit_code,
        output_dir=str(output_dir_value) if output_dir_value else None,
        files=_normalize_files(result.get("files")),
        stdout=str(result.get("stdout") or ""),
        stderr=str(result.get("stderr") or ""),
        duration_seconds=_float_or_default(result.get("duration_seconds"), 0.0),
        method=str(method_value) if method_value else None,
        readme_path=str(result.get("readme_path") or ""),
        notebook_path=str(result.get("notebook_path") or ""),
        runtime_source=str(result.get("runtime_source") or "base"),
        error_kind=str(
            result.get("error_kind")
            or classify_skill_error(
                success=success,
                exit_code=exit_code,
                stderr=str(result.get("stderr") or ""),
                stdout=str(result.get("stdout") or ""),
            ).value
        ),
        audit_identity=audit_identity,
        raw={key: value for key, value in result.items() if key != "_audit_identity"},
    )


def build_skill_run_result(
    *,
    skill: str,
    success: bool,
    exit_code: int,
    output_dir: str | Path | None,
    files: Iterable[str | Path] = (),
    stdout: str = "",
    stderr: str = "",
    duration_seconds: float = 0.0,
    method: str | None = None,
    readme_path: str | Path | None = "",
    notebook_path: str | Path | None = "",
    runtime_source: str = "base",
    error_kind: str = "",
    audit_identity: SkillRunAuditIdentity | None = None,
) -> SkillRunResult:
    """Build a normalized result from runner-native values."""
    return SkillRunResult(
        skill=str(skill),
        success=bool(success),
        exit_code=int(exit_code),
        output_dir=str(output_dir) if output_dir else None,
        files=_normalize_files(files),
        stdout=str(stdout or ""),
        stderr=str(stderr or ""),
        duration_seconds=round(float(duration_seconds or 0.0), 2),
        method=str(method) if method else None,
        readme_path=str(readme_path or ""),
        notebook_path=str(notebook_path or ""),
        runtime_source=str(runtime_source or "base"),
        error_kind=(
            str(error_kind)
            if error_kind
            else classify_skill_error(
                success=bool(success),
                exit_code=int(exit_code),
                stderr=str(stderr or ""),
                stdout=str(stdout or ""),
            ).value
        ),
        audit_identity=audit_identity,
    )


def result_json_fallback(result: SkillRunResult) -> str:
    """Serialize the result for log fallback text.

    Prefers the captured ``raw`` mapping when the result came from
    ``coerce_skill_run_result`` (which preserves any extra keys the runner
    emitted), otherwise falls back to ``to_legacy_dict()`` so the snapshot
    is non-empty for natively-built ``SkillRunResult`` instances.
    """
    payload: Mapping[str, Any] = result.raw or result.to_legacy_dict()
    return json.dumps(payload, ensure_ascii=False, default=str)


__all__ = [
    "SkillRunAuditIdentity",
    "SkillRunResult",
    "build_skill_run_result",
    "coerce_skill_run_result",
    "result_json_fallback",
]
