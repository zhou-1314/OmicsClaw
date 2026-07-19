"""Strict Remote wire Adapter for canonical demo-only Skill Jobs.

The compatibility noun remains ``Job`` on HTTP, while the normalized domain
intent is a canonical ``SimpleSkillRunSubmission``.  Complete resource
semantics travel on the wire so matching idempotent duplicates can be found
before consulting today's Registry or resource gates.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)
from starlette.requests import Request

from omicsclaw.control.run_contract import SimpleSkillRunSubmission, UnassignedScope
from omicsclaw.remote.schemas import JobSubmitRequest
from omicsclaw.skill.resource_scheduler import ExecutionResourceRequest


REMOTE_JOB_MAX_REQUEST_BYTES = 64 * 1024
REMOTE_JOB_MAX_JSON_NESTING = 64
REMOTE_JOB_READ_TIMEOUT_SECONDS = 60


class RemoteJobWireError(ValueError):
    """Content-free transport rejection raised before Run admission."""

    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class _StrictWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RemoteUnassignedScopeV1(_StrictWireModel):
    kind: Literal["unassigned"]


class RemoteDemoInputsV1(_StrictWireModel):
    demo: StrictBool

    @field_validator("demo")
    @classmethod
    def demo_must_be_enabled(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("demo must be true")
        return True


class RemoteExecutionResourceRequestV1(_StrictWireModel):
    cpu_cores: StrictInt = Field(ge=1)
    memory_mib: StrictInt = Field(ge=1)
    gpu_devices: StrictInt = Field(ge=0)
    threads: StrictInt = Field(ge=1)
    temporary_disk_mib: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def threads_fit_cpu(self) -> "RemoteExecutionResourceRequestV1":
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


class RemoteSimpleResourceContractV1(_StrictWireModel):
    kind: Literal["simple"]
    request: RemoteExecutionResourceRequestV1


class RemoteCanonicalJobSubmissionV1(_StrictWireModel):
    """The exact first Remote tracer; datasets, params and retry fail closed."""

    schema_version: Literal[1]
    skill: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$",
    )
    scope: RemoteUnassignedScopeV1
    inputs: RemoteDemoInputsV1
    params: dict[str, Any]
    resource_contract: RemoteSimpleResourceContractV1

    @field_validator("params")
    @classmethod
    def params_are_reserved(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value:
            raise ValueError("params are not supported by the V1 tracer")
        return {}

    def to_domain(self, run_submission_id: str) -> SimpleSkillRunSubmission:
        return SimpleSkillRunSubmission(
            run_submission_id=run_submission_id,
            scope=UnassignedScope(),
            skill_id=self.skill,
            resource_request=self.resource_contract.request.to_domain(),
        )


RemoteDecodedJobSubmission = RemoteCanonicalJobSubmissionV1 | JobSubmitRequest


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
                raise RemoteJobWireError("invalid_job_json", status_code=400)
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise RemoteJobWireError("invalid_job_json", status_code=400)
    if depth != 0 or in_string:
        raise RemoteJobWireError("invalid_job_json", status_code=400)


def _is_chat_stream_document(document: dict[str, Any]) -> bool:
    params = document.get("params")
    return bool(
        document.get("skill") == "chat"
        and isinstance(params, dict)
        and params.get("job_kind") == "chat_stream"
    )


async def decode_remote_job_submission(
    request: Request,
    *,
    maximum_bytes: int = REMOTE_JOB_MAX_REQUEST_BYTES,
    maximum_nesting: int = REMOTE_JOB_MAX_JSON_NESTING,
    read_timeout_seconds: float = REMOTE_JOB_READ_TIMEOUT_SECONDS,
) -> RemoteDecodedJobSubmission:
    """Count and strictly decode one JSON document before any side effect."""

    raw_content_type = request.headers.get("content-type", "")
    media_type, _, raw_parameters = raw_content_type.partition(";")
    if media_type.strip().lower() != "application/json":
        raise RemoteJobWireError("application_json_required", status_code=415)
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
        raise RemoteJobWireError("utf8_json_required", status_code=415)
    if request.headers.get("content-encoding", "").strip().lower() not in {
        "",
        "identity",
    }:
        raise RemoteJobWireError("content_encoding_not_supported", status_code=415)
    declared_lengths = request.headers.getlist("content-length")
    if len(declared_lengths) > 1:
        raise RemoteJobWireError("invalid_content_length", status_code=400)
    if declared_lengths:
        declared = declared_lengths[0]
        if not declared.isascii() or not declared.isdigit():
            raise RemoteJobWireError("invalid_content_length", status_code=400)
        if int(declared, 10) > maximum_bytes:
            raise RemoteJobWireError("job_request_too_large", status_code=413)

    async def _read_counted_body() -> bytes:
        body = bytearray()
        try:
            async for chunk in request.stream():
                if len(body) + len(chunk) > maximum_bytes:
                    raise RemoteJobWireError("job_request_too_large", status_code=413)
                body.extend(chunk)
        except RemoteJobWireError:
            raise
        except Exception as exc:
            raise RemoteJobWireError("job_request_read_failed", status_code=400) from exc
        return bytes(body)

    try:
        raw_body = await asyncio.wait_for(
            _read_counted_body(), timeout=read_timeout_seconds
        )
    except TimeoutError as exc:
        raise RemoteJobWireError("job_request_read_timeout", status_code=408) from exc
    try:
        document_text = raw_body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RemoteJobWireError("invalid_job_json_encoding", status_code=400) from exc
    _reject_excessive_json_nesting(document_text, maximum=maximum_nesting)
    try:
        document = json.loads(
            document_text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise RemoteJobWireError("invalid_job_json", status_code=400) from exc
    if not isinstance(document, dict):
        raise RemoteJobWireError("invalid_job_json", status_code=400)
    try:
        if _is_chat_stream_document(document):
            return JobSubmitRequest.model_validate(document)
        return RemoteCanonicalJobSubmissionV1.model_validate(document)
    except (ValidationError, RecursionError) as exc:
        raise RemoteJobWireError("invalid_job_submission", status_code=422) from exc


__all__ = [
    "REMOTE_JOB_MAX_JSON_NESTING",
    "REMOTE_JOB_MAX_REQUEST_BYTES",
    "REMOTE_JOB_READ_TIMEOUT_SECONDS",
    "RemoteCanonicalJobSubmissionV1",
    "RemoteDecodedJobSubmission",
    "RemoteJobWireError",
    "decode_remote_job_submission",
]
