import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class Verdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


STATES = frozenset(
    {
        "created",
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
        "lost",
        "unknown",
    }
)
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
_ALLOWED = {
    "created": {"queued", "running", "cancelled", "failed"},
    "queued": {"running", "cancelled", "failed", "lost", "unknown"},
    "running": {
        "queued",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
        "lost",
        "unknown",
    },
    "unknown": {"queued", "running", "lost", "failed", "cancelled"},
    "lost": {"queued", "running", "failed", "cancelled"},
    "interrupted": {"queued", "running", "failed", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


def transition(current, target):
    if current == target:
        return current
    if (
        current not in STATES
        or target not in STATES
        or target not in _ALLOWED.get(current, set())
    ):
        raise ValueError(f"illegal state transition: {current} -> {target}")
    return target


def normalize_status(status):
    return {
        "done": "succeeded",
        "canceled": "cancelled",
        "cancel_requested": "running",
    }.get(status, status)


def _id(p):
    return p + "_" + uuid.uuid4().hex


def _now():
    return datetime.now(UTC).isoformat()


@dataclass
class Run:
    run_id: str = field(default_factory=lambda: _id("run"))
    session_id: str = ""
    plan_id: str = ""
    status: str = "created"
    execution_location: str = "unset"
    root_artifact_id: str | None = None
    created_at: str = field(default_factory=_now)


@dataclass
class Job:
    job_id: str = field(default_factory=lambda: _id("job"))
    run_id: str = ""
    host_id: str = ""
    scheduler: str = ""
    execution_ref: str = ""
    status: str = "created"
    command_digest: str = ""


@dataclass
class Kernel:
    kernel_id: str = field(default_factory=lambda: _id("kernel"))
    run_id: str = ""
    language: str = "python"
    host_id: str = ""
    status: str = "created"
    last_heartbeat: str = ""


@dataclass
class Artifact:
    artifact_id: str = field(default_factory=lambda: _id("artifact"))
    run_id: str = ""
    version: int = 1
    uri: str = ""
    kind: str = "file"
    inputs: list[str] = field(default_factory=list)
    code_ref: str = ""
    environment_ref: str = ""
    execution_ref: str = ""
    parent_artifact_id: str | None = None
    checksum: str = ""
    created_at: str = field(default_factory=_now)
    size: int = 0
    content_type: str = ""
    host_id: str = ""
    job_id: str = ""
    kernel_id: str = ""
    input_artifact_ids: list[str] = field(default_factory=list)


@dataclass
class Verification:
    verification_id: str = field(default_factory=lambda: _id("verification"))
    artifact_id: str = ""
    check: str = ""
    verdict: str = Verdict.INCONCLUSIVE
    evidence_ref: str = ""
    created_at: str = field(default_factory=_now)
    artifact_version: int = 1
    details: str = ""
