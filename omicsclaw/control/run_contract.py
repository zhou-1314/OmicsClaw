"""Typed V1 contract for one canonical simple Skill Run.

The public Interface is deliberately smaller than the eventual Run union.  It
implements the first tracer only: one canonical Skill, demo input, empty
parameters, and one complete static resource request.  Workflow and governed
dynamic envelopes remain outside this Module until their own execution paths
use the same RunRuntime.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, TypeAlias

from omicsclaw.skill.resource_scheduler import ExecutionResourceRequest


RUN_FINGERPRINT_VERSION = 1
_OPAQUE_ID = re.compile(r"[0-9a-f]{32}\Z")
_CANONICAL_SKILL_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")


def _require_opaque_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise ValueError(f"{label} must be 32 lowercase hexadecimal characters")
    return value


@dataclass(frozen=True, slots=True)
class ProjectScope:
    """Immutable Project-associated Run Scope."""

    project_id: str

    def __post_init__(self) -> None:
        _require_opaque_id(self.project_id, "Project ID")

    @property
    def kind(self) -> str:
        return "project"

    def to_dict(self) -> dict[str, str]:
        return {"kind": "project", "project_id": self.project_id}


@dataclass(frozen=True, slots=True)
class UnassignedScope:
    """Explicit non-Project Run Scope; it carries no sentinel Project ID."""

    @property
    def kind(self) -> str:
        return "unassigned"

    @property
    def project_id(self) -> None:
        return None

    def to_dict(self) -> dict[str, str]:
        return {"kind": "unassigned"}


RunScope: TypeAlias = ProjectScope | UnassignedScope


@dataclass(frozen=True, slots=True)
class SimpleSkillRunSubmission:
    """Normalized executable intent for the V1 simple-Skill tracer.

    ``run_submission_id`` is transport retry identity and is intentionally not
    part of :func:`canonical_run_fingerprint`.  Complete resource semantics do
    enter the digest, allowing a matching historical duplicate to be found
    before current Registry, Project lifecycle, or capacity checks.
    """

    run_submission_id: str
    scope: RunScope
    skill_id: str
    resource_request: ExecutionResourceRequest

    def __post_init__(self) -> None:
        _require_opaque_id(self.run_submission_id, "Run Submission ID")
        if not isinstance(self.scope, (ProjectScope, UnassignedScope)):
            raise ValueError("scope must be ProjectScope or UnassignedScope")
        if not isinstance(self.skill_id, str) or not _CANONICAL_SKILL_ID.fullmatch(
            self.skill_id
        ):
            raise ValueError("skill_id must be a canonical Skill identifier")
        if not isinstance(self.resource_request, ExecutionResourceRequest):
            raise ValueError("resource_request must be complete")

    @property
    def run_kind(self) -> str:
        return "skill"

    @property
    def inputs(self) -> dict[str, Any]:
        return {"skill_id": self.skill_id, "input": {"kind": "demo"}}

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    @property
    def resource_contract(self) -> dict[str, Any]:
        return {"kind": "simple", "request": self.resource_request.to_dict()}

    def fingerprint_document(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_kind": self.run_kind,
            "scope": self.scope.to_dict(),
            "parent_turn_id": None,
            "retry_of_run_id": None,
            "inputs": self.inputs,
            "parameters": self.parameters,
            "resource_contract": self.resource_contract,
        }


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the bounded V1 canonical JSON spelling used for fingerprints."""

    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Run semantics must be canonical JSON") from exc
    encoded = rendered.encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("Run semantics exceed the V1 canonical size limit")
    return encoded


def canonical_run_fingerprint(
    submission: SimpleSkillRunSubmission,
) -> tuple[int, str, dict[str, Any]]:
    """Compute the Backend-owned digest of caller-declared Run semantics."""

    if not isinstance(submission, SimpleSkillRunSubmission):
        raise TypeError("submission must be SimpleSkillRunSubmission")
    document = submission.fingerprint_document()
    versioned = {
        "fingerprint_version": RUN_FINGERPRINT_VERSION,
        "request": document,
    }
    digest = hashlib.sha256(canonical_json_bytes(versioned)).hexdigest()
    return RUN_FINGERPRINT_VERSION, digest, document


__all__ = [
    "ProjectScope",
    "RUN_FINGERPRINT_VERSION",
    "RunScope",
    "SimpleSkillRunSubmission",
    "UnassignedScope",
    "canonical_json_bytes",
    "canonical_run_fingerprint",
]
