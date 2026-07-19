"""Typed, deterministic classification for skill execution outcomes."""

from __future__ import annotations

from enum import Enum


class SkillErrorKind(str, Enum):
    NONE = "none"
    MISSING_DEPENDENCY = "missing_dependency"
    BAD_INPUT = "bad_input"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    CANCELLED = "cancelled"
    SCRIPT_DEFECT = "script_defect"
    CONTRACT_FAILURE = "contract_failure"
    CONTRACT_VALIDATOR_FAILED = "contract_validator_failed"
    UPSTREAM_FAILED = "upstream_failed"
    UNKNOWN = "unknown"


def classify_skill_error(
    *,
    success: bool,
    exit_code: int = 0,
    stderr: str = "",
    stdout: str = "",
    cancelled: bool = False,
    contract_failure: bool = False,
) -> SkillErrorKind:
    """Classify an outcome without interpreting scientific result content."""
    if success:
        return SkillErrorKind.NONE
    if cancelled:
        return SkillErrorKind.CANCELLED
    if contract_failure:
        return SkillErrorKind.CONTRACT_FAILURE

    text = f"{stderr}\n{stdout}".lower()
    if exit_code in {137, -9} or any(
        marker in text
        for marker in ("out of memory", "memoryerror", "resource exhausted", "oom")
    ):
        return SkillErrorKind.RESOURCE_EXHAUSTED
    if any(marker in text for marker in ("timed out", "timeout", "time limit exceeded")):
        return SkillErrorKind.TIMEOUT
    if any(
        marker in text
        for marker in (
            "modulenotfounderror",
            "no module named",
            "not installed",
            "missing dependency",
            "dependency missing",
            "command not found",
        )
    ):
        return SkillErrorKind.MISSING_DEPENDENCY
    if any(
        marker in text
        for marker in (
            "invalid input",
            "input file",
            "input path",
            "precondition",
            "unsupported file",
            "no --input",
        )
    ):
        return SkillErrorKind.BAD_INPUT
    if any(
        marker in text
        for marker in (
            "traceback (most recent call last)",
            "traceback:",
            "assertionerror",
            "syntaxerror",
            "nameerror",
            "typeerror",
        )
    ):
        return SkillErrorKind.SCRIPT_DEFECT
    return SkillErrorKind.UNKNOWN


__all__ = ["SkillErrorKind", "classify_skill_error"]
