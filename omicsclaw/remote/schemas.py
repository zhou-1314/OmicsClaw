"""Pydantic models for the remote control-plane contract.

Every field name and JSON shape MUST stay in lockstep with the App side
(``OmicsClaw-App`` ``src/lib/dataset-ref.ts`` / ``jobs-client.ts``).
Treat this file as the single source of truth for the wire format.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# /connections/test  +  /env/doctor
# ---------------------------------------------------------------------------


class ConnectionTestResponse(BaseModel):
    ok: bool
    version: str
    server_time: str  # ISO-8601 UTC
    extras: dict[str, Any] = Field(default_factory=dict)


class EnvDoctorCheck(BaseModel):
    name: str
    status: Literal["ok", "warn", "fail", "info"]
    summary: str
    details: list[str] = Field(default_factory=list)


class EnvDoctorReport(BaseModel):
    generated_at: str
    workspace_dir: str
    omicsclaw_dir: str
    overall_status: Literal["ok", "warn", "fail"]
    failure_count: int
    warning_count: int
    checks: list[EnvDoctorCheck]


# Adaptive env overlay management (ADR: adaptive-environment-provisioning).
# Shapes mirror ``venv_provision.list_overlays()`` and the ``oc env`` CLI.


class OverlayInfo(BaseModel):
    key: str
    valid: bool
    pip_specs: list[str] = Field(default_factory=list)
    base_python: str = ""
    created: Optional[float] = None
    size_bytes: int = 0
    path: str


class OverlayListResponse(BaseModel):
    overlays: list[OverlayInfo]
    total: int
    total_bytes: int
    env_root: str


class OverlayCleanRequest(BaseModel):
    key: Optional[str] = None


class OverlayCleanResponse(BaseModel):
    removed: int
    key: Optional[str] = None


class AdaptiveModeResponse(BaseModel):
    mode: Literal["on", "probe", "off"]
    kill_switch: bool


class AdaptiveModeUpdateRequest(BaseModel):
    mode: Literal["on", "probe", "off"]


# ---------------------------------------------------------------------------
# /datasets
# ---------------------------------------------------------------------------


DatasetStatus = Literal["local-only", "uploading", "synced", "stale"]


class DatasetRef(BaseModel):
    dataset_id: str
    display_name: str
    storage_uri: str           # file:///abs/path | ssh://host/abs/path
    execution_target: str      # required: 'local' | 'remote:<profile_id>'
    checksum: str              # sha256-of-first-64k + ":" + size_bytes
    size_bytes: int
    modified_at: str           # ISO-8601 UTC
    status: DatasetStatus = "synced"


class DatasetListResponse(BaseModel):
    datasets: list[DatasetRef]
    total: int
    workspace: str


class DatasetImportRemoteRequest(BaseModel):
    remote_path: str           # absolute path on the server
    display_name: str = ""
    execution_target: str


# ---------------------------------------------------------------------------
# /jobs
# ---------------------------------------------------------------------------


JobStatus = Literal[
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "failed",
    "canceled",
    "interrupted",
]


class JobSubmitRequest(BaseModel):
    """Legacy chat-stream display projection; not a scientific executor wire."""

    model_config = ConfigDict(extra="forbid")

    workspace: str = ""
    session_id: str = ""
    skill: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


class Job(BaseModel):
    job_id: str
    session_id: str = ""
    skill: str
    status: JobStatus
    workspace: str
    inputs: dict[str, Any]
    params: dict[str, Any]
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    artifact_root: Optional[str] = None
    # Adaptive-env provenance: which interpreter served the run
    # ("base" | "skip" | "probe" | "venv:<key>"). Optional/None for jobs created
    # before this field existed (backward-compatible deserialization).
    runtime_source: Optional[str] = None
    # Canonical Remote Jobs are deterministic compatibility projections over
    # one authoritative Run. Historical Job JSON and chat-stream display Jobs
    # leave these fields unset.
    run_id: Optional[str] = None
    receipt_revision: Optional[int] = None
    terminal_code: Optional[str] = None
    assignment_id: Optional[str] = None
    compatibility_kind: Literal["canonical_run", "legacy_job", "chat_stream"] = (
        "legacy_job"
    )


class JobListResponse(BaseModel):
    jobs: list[Job]
    total: int
    next_cursor: Optional[str] = None


class JobSubmitResponse(BaseModel):
    job_id: str
    status: JobStatus
    run_id: Optional[str] = None
    duplicate: bool = False
    receipt_revision: Optional[int] = None


# ---------------------------------------------------------------------------
# /artifacts
# ---------------------------------------------------------------------------


class Artifact(BaseModel):
    artifact_id: str
    job_id: str
    relative_path: str
    size_bytes: int
    mime_type: str
    created_at: str
    run_id: Optional[str] = None
    sha256: Optional[str] = None


class ArtifactListResponse(BaseModel):
    artifacts: list[Artifact]
    total: int
    next_cursor: Optional[str] = None


# ---------------------------------------------------------------------------
# /sessions/:id/resume (retired compatibility response shape)
# ---------------------------------------------------------------------------


class SessionResumeResponse(BaseModel):
    session_id: str
    resumed: bool
    reason: str = ""
    active_job_ids: list[str] = Field(default_factory=list)
