"""Strict V1 Desktop wire Adapter for canonical simple Skill Runs."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)
from starlette.requests import Request

from omicsclaw.control import (
    RunIntegrityEvidenceCode,
    RunIntegrityIncidentPage,
    RunIntegrityIncidentRecord,
    RunIntegrityIncidentType,
    RunObservationSnapshot,
    RunRecord,
)
from omicsclaw.control.run_contract import (
    ProjectScope,
    SimpleSkillRunSubmission,
    UnassignedScope,
)
from omicsclaw.skill.resource_scheduler import ExecutionResourceRequest


DESKTOP_RUN_MAX_REQUEST_BYTES = 64 * 1024
DESKTOP_RUN_MAX_JSON_NESTING = 64
DESKTOP_RUN_READ_TIMEOUT_SECONDS = 60
DESKTOP_RUN_INCIDENT_MAX_PAGE_SIZE = 100


class DesktopRunWireError(ValueError):
    """Closed transport rejection raised before canonical Run admission."""

    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class _StrictWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DesktopProjectScopeV1(_StrictWireModel):
    kind: Literal["project"]
    project_id: str = Field(pattern=r"^[0-9a-f]{32}$", max_length=32)


class DesktopUnassignedScopeV1(_StrictWireModel):
    kind: Literal["unassigned"]


DesktopRunScopeV1 = Annotated[
    DesktopProjectScopeV1 | DesktopUnassignedScopeV1,
    Field(discriminator="kind"),
]


class DesktopDemoInputV1(_StrictWireModel):
    kind: Literal["demo"]


class DesktopExecutionResourceRequestV1(_StrictWireModel):
    cpu_cores: StrictInt = Field(ge=1)
    memory_mib: StrictInt = Field(ge=1)
    gpu_devices: StrictInt = Field(ge=0)
    threads: StrictInt = Field(ge=1)
    temporary_disk_mib: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def threads_fit_cpu(self) -> "DesktopExecutionResourceRequestV1":
        if self.threads > self.cpu_cores:
            raise ValueError("threads cannot exceed cpu_cores")
        return self

    def to_domain(self) -> ExecutionResourceRequest:
        return ExecutionResourceRequest(
            cpu_cores=self.cpu_cores,
            memory_mib=self.memory_mib,
            gpu_devices=self.gpu_devices,
            threads=self.threads,
            temporary_disk_mib=self.temporary_disk_mib,
        )


class DesktopSimpleResourceContractV1(_StrictWireModel):
    kind: Literal["simple"]
    request: DesktopExecutionResourceRequestV1


class DesktopRunSubmissionV1(_StrictWireModel):
    """First tracer subset; paths, parameters, retry and nesting fail closed."""

    schema_version: Literal[1]
    run_kind: Literal["skill"]
    scope: DesktopRunScopeV1
    skill_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$",
    )
    input: DesktopDemoInputV1
    parameters: dict[str, Any]
    resource_contract: DesktopSimpleResourceContractV1
    parent_turn_id: None = None
    retry_of_run_id: None = None

    @field_validator("parameters")
    @classmethod
    def parameters_are_reserved(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value:
            raise ValueError("parameters are not supported by the V1 tracer")
        return {}

    def to_domain(self, run_submission_id: str) -> SimpleSkillRunSubmission:
        scope = (
            ProjectScope(self.scope.project_id)
            if isinstance(self.scope, DesktopProjectScopeV1)
            else UnassignedScope()
        )
        return SimpleSkillRunSubmission(
            run_submission_id=run_submission_id,
            scope=scope,
            skill_id=self.skill_name,
            resource_request=self.resource_contract.request.to_domain(),
        )


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _reject_excessive_json_nesting(document: str, *, maximum: int) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in document:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > maximum:
                raise DesktopRunWireError("invalid_run_json", status_code=400)
        elif character in "]}":
            depth -= 1


async def decode_desktop_run_submission(
    request: Request,
    *,
    maximum_bytes: int = DESKTOP_RUN_MAX_REQUEST_BYTES,
    maximum_nesting: int = DESKTOP_RUN_MAX_JSON_NESTING,
    read_timeout_seconds: float = DESKTOP_RUN_READ_TIMEOUT_SECONDS,
) -> DesktopRunSubmissionV1:
    """Count and decode one strict JSON document before model validation."""

    raw_content_type = request.headers.get("content-type", "")
    media_type, _, raw_parameters = raw_content_type.partition(";")
    media_type = media_type.strip()
    if media_type.lower() != "application/json":
        raise DesktopRunWireError("application_json_required", status_code=415)
    parameters = [
        parameter.strip().lower()
        for parameter in raw_parameters.split(";")
        if parameter.strip()
    ]
    if len(parameters) > 1 or any(
        not parameter.startswith("charset=")
        or parameter.removeprefix("charset=").strip('"') not in {"utf-8", "utf8"}
        for parameter in parameters
    ):
        raise DesktopRunWireError("utf8_json_required", status_code=415)
    content_encoding = request.headers.get("content-encoding", "").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise DesktopRunWireError("content_encoding_not_supported", status_code=415)
    declared_lengths = request.headers.getlist("content-length")
    if len(declared_lengths) > 1:
        raise DesktopRunWireError("invalid_content_length", status_code=400)
    declared_length = declared_lengths[0] if declared_lengths else None
    if declared_length is not None:
        if not declared_length.isascii() or not declared_length.isdigit():
            raise DesktopRunWireError("invalid_content_length", status_code=400)
        parsed_length = int(declared_length, 10)
        if parsed_length > maximum_bytes:
            raise DesktopRunWireError("run_request_too_large", status_code=413)

    async def _read_counted_body() -> bytes:
        body = bytearray()
        try:
            async for chunk in request.stream():
                if len(body) + len(chunk) > maximum_bytes:
                    raise DesktopRunWireError("run_request_too_large", status_code=413)
                body.extend(chunk)
        except DesktopRunWireError:
            raise
        except Exception as exc:
            raise DesktopRunWireError(
                "run_request_read_failed", status_code=400
            ) from exc
        return bytes(body)

    try:
        raw_body = await asyncio.wait_for(
            _read_counted_body(), timeout=read_timeout_seconds
        )
    except TimeoutError as exc:
        raise DesktopRunWireError("run_request_read_timeout", status_code=408) from exc
    try:
        document_text = raw_body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DesktopRunWireError("invalid_run_json_encoding", status_code=400) from exc
    _reject_excessive_json_nesting(document_text, maximum=maximum_nesting)
    try:
        document = json.loads(
            document_text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise DesktopRunWireError("invalid_run_json", status_code=400) from exc
    if not isinstance(document, dict):
        raise DesktopRunWireError("invalid_run_json", status_code=400)
    try:
        return DesktopRunSubmissionV1.model_validate(document)
    except (ValidationError, RecursionError) as exc:
        raise DesktopRunWireError("invalid_run_submission", status_code=422) from exc


_RunStatus = Literal[
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "failed",
    "canceled",
    "interrupted",
]


class DesktopRunAcceptedV1(_StrictWireModel):
    schema_version: Literal[1]
    run_id: str
    status: _RunStatus
    duplicate: bool
    receipt_revision: int
    accepted_at_ms: int


class DesktopRunReceiptV1(_StrictWireModel):
    schema_version: Literal[1]
    run_id: str
    scope: DesktopRunScopeV1
    run_kind: Literal["skill"]
    parent_turn_id: str | None
    retry_of_run_id: str | None
    status: _RunStatus
    terminal_code: str | None
    manifest_ref: str
    created_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None
    revision: int


class DesktopRunCancelResultV1(_StrictWireModel):
    schema_version: Literal[1]
    run_id: str
    changed: bool
    code: Literal[
        "canceled_before_assignment",
        "cancel_requested",
        "cancel_already_requested",
        "already_terminal",
    ]
    receipt: DesktopRunReceiptV1


class DesktopRunIntegrityIncidentV1(_StrictWireModel):
    incident_id: str = Field(pattern=r"^[0-9a-f]{32}$", max_length=32)
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$", max_length=32)
    assignment_id: str = Field(pattern=r"^[0-9a-f]{32}$", max_length=32)
    incident_type: RunIntegrityIncidentType
    evidence_code: RunIntegrityEvidenceCode
    receipt_revision: StrictInt = Field(ge=1)
    evidence_schema_version: Literal[1]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", max_length=64)
    created_at_ms: StrictInt = Field(ge=0)


class DesktopRunIntegrityIncidentPageV1(_StrictWireModel):
    schema_version: Literal[1]
    incidents: list[DesktopRunIntegrityIncidentV1] = Field(
        max_length=DESKTOP_RUN_INCIDENT_MAX_PAGE_SIZE
    )
    next_cursor: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{32}$",
        max_length=32,
    )


def desktop_run_receipt_from_record(receipt: RunRecord) -> DesktopRunReceiptV1:
    if receipt.scope_kind == "project":
        if receipt.project_id is None:
            raise RuntimeError("Project Run Receipt has no Project ID")
        scope: DesktopProjectScopeV1 | DesktopUnassignedScopeV1 = DesktopProjectScopeV1(
            kind="project", project_id=receipt.project_id
        )
    elif receipt.scope_kind == "unassigned":
        if receipt.project_id is not None:
            raise RuntimeError("Unassigned Run Receipt carries a Project ID")
        scope = DesktopUnassignedScopeV1(kind="unassigned")
    else:
        raise RuntimeError("Run Receipt has an unsupported Scope")
    return DesktopRunReceiptV1(
        schema_version=1,
        run_id=receipt.run_id,
        scope=scope,
        run_kind=receipt.run_kind,
        parent_turn_id=receipt.parent_turn_id,
        retry_of_run_id=receipt.retry_of_run_id,
        status=receipt.status,
        terminal_code=receipt.terminal_code,
        manifest_ref=receipt.manifest_ref,
        created_at_ms=receipt.created_at_ms,
        started_at_ms=receipt.started_at_ms,
        finished_at_ms=receipt.finished_at_ms,
        revision=receipt.revision,
    )


def desktop_run_receipt_v1(
    observation: RunObservationSnapshot,
) -> DesktopRunReceiptV1:
    return desktop_run_receipt_from_record(observation.receipt)


def _desktop_run_integrity_incident_v1(
    incident: RunIntegrityIncidentRecord,
) -> DesktopRunIntegrityIncidentV1:
    return DesktopRunIntegrityIncidentV1(
        incident_id=incident.incident_id,
        run_id=incident.run_id,
        assignment_id=incident.assignment_id,
        incident_type=incident.incident_type,
        evidence_code=incident.evidence_code,
        receipt_revision=incident.receipt_revision,
        evidence_schema_version=incident.evidence_schema_version,
        evidence_sha256=incident.evidence_sha256,
        created_at_ms=incident.created_at_ms,
    )


def desktop_run_integrity_incident_page_v1(
    page: RunIntegrityIncidentPage,
) -> DesktopRunIntegrityIncidentPageV1:
    return DesktopRunIntegrityIncidentPageV1(
        schema_version=1,
        incidents=[
            _desktop_run_integrity_incident_v1(incident) for incident in page.incidents
        ],
        next_cursor=page.next_cursor,
    )


__all__ = [
    "DESKTOP_RUN_MAX_JSON_NESTING",
    "DESKTOP_RUN_MAX_REQUEST_BYTES",
    "DESKTOP_RUN_READ_TIMEOUT_SECONDS",
    "DESKTOP_RUN_INCIDENT_MAX_PAGE_SIZE",
    "DesktopRunAcceptedV1",
    "DesktopRunCancelResultV1",
    "DesktopRunIntegrityIncidentPageV1",
    "DesktopRunIntegrityIncidentV1",
    "DesktopRunReceiptV1",
    "DesktopRunSubmissionV1",
    "DesktopRunWireError",
    "decode_desktop_run_submission",
    "desktop_run_receipt_from_record",
    "desktop_run_receipt_v1",
    "desktop_run_integrity_incident_page_v1",
]
