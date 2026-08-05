"""Typed Backend front door for governed Skill authoring.

The public run-promotion contract carries only an opaque ``run_id``.  Filesystem
resolution stays behind this module, where the Backend proves the Autonomous
producer manifest, terminal completion, and durable output claim before the
existing scaffolder is allowed to read any source artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping

from omicsclaw.common.manifest import read_manifest
from omicsclaw.common.output_claim import first_filesystem_alias_component
from omicsclaw.common.run_paths import read_project_meta
from omicsclaw.runtime.policy.verification import (
    COMPLETION_STATUS_COMPLETE,
    WORKSPACE_KIND_ANALYSIS_RUN,
)
from omicsclaw.skill.execution.output_ownership import read_output_claim
from omicsclaw.skill.resource_scheduler import ExecutionResourceRequest

from .scaffolder import SkillScaffoldResult


_RUN_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_SOURCE_KINDS = frozenset({"intent", "run"})
_REQUEST_FIELDS = frozenset(
    {
        "request",
        "domain",
        "skill_name",
        "summary",
        "source",
        "input_formats",
        "primary_outputs",
        "methods",
        "trigger_keywords",
        "create_tests",
        "compute_resources",
    }
)


@dataclass(frozen=True, slots=True)
class SkillAuthoringSource:
    kind: str
    run_id: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SkillAuthoringSource":
        if not isinstance(value, Mapping):
            raise ValueError("source must be an object")
        unknown = set(value) - {"kind", "run_id"}
        if unknown:
            raise ValueError(f"source contains unknown fields: {sorted(unknown)}")
        kind = str(value.get("kind") or "").strip()
        run_id = str(value.get("run_id") or "").strip()
        if kind not in _SOURCE_KINDS:
            raise ValueError(f"source.kind must be one of {sorted(_SOURCE_KINDS)}")
        if kind == "run" and not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("source.run_id must be 32 lowercase hexadecimal characters")
        if kind == "intent" and run_id:
            raise ValueError("source.run_id is only valid when source.kind is 'run'")
        return cls(kind=kind, run_id=run_id)


@dataclass(frozen=True, slots=True)
class SkillAuthoringRequest:
    request: str
    domain: str
    source: SkillAuthoringSource
    skill_name: str = ""
    summary: str = ""
    input_formats: tuple[str, ...] = ()
    primary_outputs: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    trigger_keywords: tuple[str, ...] = ()
    create_tests: bool = True
    compute_resources: ExecutionResourceRequest | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SkillAuthoringRequest":
        if not isinstance(value, Mapping):
            raise ValueError("SkillAuthoringRequest must be an object")
        unknown = set(value) - _REQUEST_FIELDS
        if unknown:
            raise ValueError(f"SkillAuthoringRequest contains unknown fields: {sorted(unknown)}")
        if "source_analysis_dir" in value:
            raise ValueError("source_analysis_dir is not a public authoring field")
        request = str(value.get("request") or "").strip()
        domain = str(value.get("domain") or "").strip().lower()
        if not request:
            raise ValueError("request is required")
        if not domain:
            raise ValueError("domain is required")
        source = SkillAuthoringSource.from_mapping(value.get("source") or {})
        raw_compute = value.get("compute_resources")
        compute = (
            ExecutionResourceRequest.from_mapping(raw_compute)
            if isinstance(raw_compute, Mapping)
            else None
        )

        def strings(name: str) -> tuple[str, ...]:
            raw = value.get(name) or []
            if not isinstance(raw, (list, tuple)):
                raise ValueError(f"{name} must be a list")
            return tuple(str(item).strip() for item in raw if str(item).strip())

        return cls(
            request=request,
            domain=domain,
            source=source,
            skill_name=str(value.get("skill_name") or "").strip(),
            summary=str(value.get("summary") or "").strip(),
            input_formats=strings("input_formats"),
            primary_outputs=strings("primary_outputs"),
            methods=strings("methods"),
            trigger_keywords=strings("trigger_keywords"),
            create_tests=bool(value.get("create_tests", True)),
            compute_resources=compute,
        )


def resolve_claimed_autonomous_run(output_root: str | Path, run_id: str) -> Path:
    """Resolve one opaque Autonomous Run id to exactly one claimed output."""

    if not _RUN_ID_RE.fullmatch(str(run_id or "")):
        raise ValueError("run_id must be 32 lowercase hexadecimal characters")
    root = Path(output_root).expanduser()
    alias = first_filesystem_alias_component(root)
    if alias is not None:
        raise ValueError(f"output root contains a filesystem alias: {alias}")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("configured output root does not exist") from exc
    if not root.is_dir():
        raise ValueError("configured output root is not a directory")

    containers = [root]
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        if read_project_meta(child).get("project_id"):
            containers.append(child)

    matches: list[Path] = []
    expected_suffix = f"__{run_id}"
    for container in containers:
        for candidate in sorted(container.iterdir()):
            if (
                candidate.is_symlink()
                or not candidate.is_dir()
                or not candidate.name.startswith("autonomous-code__")
                or not candidate.name.endswith(expected_suffix)
            ):
                continue
            manifest = read_manifest(candidate)
            if (
                manifest is None
                or manifest.workspace is None
                or manifest.verification is None
                or manifest.workspace.kind != WORKSPACE_KIND_ANALYSIS_RUN
                or manifest.workspace.purpose != "autonomous_code"
                or manifest.verification.status != COMPLETION_STATUS_COMPLETE
                or not manifest.verification.completed
                or manifest.verification.missing_required_artifacts
                or str(manifest.metadata.get("source") or "")
                != "autonomous_code_runner"
                or str(manifest.metadata.get("run_id") or "") != run_id
            ):
                continue
            claim = read_output_claim(candidate)
            if (
                claim.get("schema_version") != 1
                or claim.get("owner") != f"autonomous:{run_id}"
                or not _RUN_ID_RE.fullmatch(str(claim.get("claim_id") or ""))
            ):
                continue
            matches.append(candidate.resolve(strict=True))

    if not matches:
        raise ValueError(f"claimed successful Autonomous Run not found: {run_id}")
    if len(matches) != 1:
        raise ValueError(f"Autonomous Run identity is ambiguous: {run_id}")
    return matches[0]


def author_skill(
    request: SkillAuthoringRequest,
    *,
    output_root: str | Path,
    skills_root: str | Path | None = None,
) -> SkillScaffoldResult:
    """Create one candidate through the typed Backend authoring boundary."""

    from .scaffolder import create_skill_scaffold

    source_dir: Path | None = None
    source_run_id = ""
    if request.source.kind == "run":
        source_run_id = request.source.run_id
        source_dir = resolve_claimed_autonomous_run(output_root, source_run_id)
    return create_skill_scaffold(
        request=request.request,
        domain=request.domain,
        skill_name=request.skill_name,
        summary=request.summary,
        input_formats=request.input_formats,
        primary_outputs=request.primary_outputs,
        methods=request.methods,
        trigger_keywords=request.trigger_keywords,
        create_tests=request.create_tests,
        skills_root=Path(skills_root) if skills_root is not None else None,
        source_analysis_dir=source_dir,
        source_run_id=source_run_id,
        output_root=Path(output_root),
        compute_resources=(
            request.compute_resources.to_dict()
            if request.compute_resources is not None
            else None
        ),
    )


__all__ = [
    "SkillAuthoringRequest",
    "SkillAuthoringSource",
    "author_skill",
    "resolve_claimed_autonomous_run",
]
