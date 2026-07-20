"""Typed control-plane commands and immutable observation records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from omicsclaw.attachments import (
    AttachmentReferenceV1,
    SourceAttachmentDescriptorV1,
)

from .terminal_codes import (
    RUN_TERMINAL_CODES_BY_STATUS,
    RunTerminalCode,
    TurnTerminalCode,
    is_allowed_run_terminal_code,
)


def _freeze_json(value: Any) -> Any:
    """Detach and recursively freeze JSON-shaped containers.

    The control contracts cross an admission seam. A frozen dataclass alone is
    insufficient when it retains caller-owned dictionaries, so mappings become
    read-only proxies and sequences become tuples before fingerprinting or
    queueing can observe them.
    """

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    frozen = _freeze_json(value)
    assert isinstance(frozen, Mapping)
    return frozen


def _thaw_json(value: Any) -> Any:
    """Return a detached JSON-compatible projection of a frozen value."""

    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def validate_delivery_provider_evidence(value: Mapping[str, Any]) -> None:
    """Reject oversized, nested or credential-shaped provider evidence."""

    if not isinstance(value, Mapping):
        raise TypeError("provider_evidence must be a mapping")
    if len(value) > 16:
        raise ValueError("provider_evidence has too many fields")
    encoded_chars = 0
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ValueError("provider_evidence keys must be bounded strings")
        normalized = key.lower().replace("-", "_")
        if any(
            marker in normalized
            for marker in (
                "authorization",
                "credential",
                "password",
                "secret",
                "token",
                "api_key",
                "apikey",
            )
        ):
            raise ValueError("provider_evidence must not contain credentials")
        if item is None or isinstance(item, (bool, int)):
            rendered = str(item)
        elif isinstance(item, str) and len(item) <= 256:
            rendered = item
        else:
            raise ValueError("provider_evidence values must be bounded scalars")
        encoded_chars += len(key) + len(rendered)
    if encoded_chars > 4_096:
        raise ValueError("provider_evidence exceeds its bounded size")


class TurnAcceptanceStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    REJECTED = "rejected"


class RunAcceptanceStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    REJECTED = "rejected"


class ProjectLifecycleStatus(str, Enum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    BUSY = "busy"
    NOT_FOUND = "not_found"
    REVISION_CONFLICT = "revision_conflict"


class AssignmentStatus(str, Enum):
    ASSIGNED = "assigned"
    ALREADY_ASSIGNED = "already_assigned"
    STATE_CONFLICT = "state_conflict"
    NOT_FOUND = "not_found"


class RunIntegrityIncidentType(str, Enum):
    """Closed classes of durable, content-free Run integrity evidence."""

    ASSIGNMENT_FENCE_VIOLATION = "assignment_fence_violation"
    TERMINAL_REPORT_CONFLICT = "terminal_report_conflict"
    MANIFEST_RECEIPT_MISMATCH = "manifest_receipt_mismatch"
    EXECUTION_OWNER_UNCONFIRMED = "execution_owner_unconfirmed"
    RECOVERY_TERMINAL_COMMIT_FAILED = "recovery_terminal_commit_failed"


class RunIntegrityEvidenceCode(str, Enum):
    """Closed reason vocabulary used to build versioned evidence digests."""

    ASSIGNMENT_MISSING = "assignment_missing"
    ASSIGNMENT_ID_MISMATCH = "assignment_id_mismatch"
    TERMINAL_STATE_CONFLICT = "terminal_state_conflict"
    MANIFEST_RECEIPT_BINDING_MISMATCH = "manifest_receipt_binding_mismatch"
    MANIFEST_ASSIGNMENT_MISMATCH = "manifest_assignment_mismatch"
    MANIFEST_COMPLETION_INVALID = "manifest_completion_invalid"
    MANIFEST_TERMINAL_CONFLICT = "manifest_terminal_conflict"
    EXECUTION_REFERENCE_MISSING = "execution_reference_missing"
    EXECUTION_OWNER_STOP_UNCONFIRMED = "execution_owner_stop_unconfirmed"
    DISPATCHER_OWNER_MISSING = "dispatcher_owner_missing"
    RECOVERY_TERMINAL_REPORT_REJECTED = "recovery_terminal_report_rejected"
    RECOVERY_TERMINAL_TRANSACTION_FAILED = "recovery_terminal_transaction_failed"


class DeliveryAttemptOutcome(str, Enum):
    ACCEPTED = "accepted"
    NOT_ACCEPTED_RETRYABLE = "not_accepted_retryable"
    REJECTED_PERMANENT = "rejected_permanent"
    ACCEPTANCE_UNKNOWN = "acceptance_unknown"


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    project_id: str
    display_name: str
    lifecycle: str
    revision: int
    created_at_ms: int
    updated_at_ms: int
    lifecycle_at_ms: int


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    conversation_id: str
    surface: str
    reply_target_key: str
    reply_target: Mapping[str, Any]
    project_id: str | None
    revision: int
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class TurnRecord:
    turn_id: str
    conversation_id: str
    turn_kind: str
    status: str
    retry_of_turn_id: str | None
    terminal_code: TurnTerminalCode | None
    created_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None
    revision: int


@dataclass(frozen=True, slots=True)
class TurnTranscriptRef:
    """Content-free pointer committed atomically with one terminal Receipt."""

    entry_id: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class TurnObservationRecord:
    """One transactionally consistent Control-side Turn observation.

    ``project_id`` is a projection of the immutable/current Conversation
    reference, not a field added to the minimal durable Turn Receipt.  The
    Transcript reference remains content-free and is verified by
    ``ControlRuntime`` against the independently authoritative Transcript
    Store before a Surface may expose it.
    """

    receipt: TurnRecord
    project_id: str | None
    transcript_ref: TurnTranscriptRef | None


@dataclass(frozen=True, slots=True)
class RawContentBlockV1:
    """One side-effect-free text block at ingress.

    Stage 2a control commands use ``RawInboundV1.project_command`` so they
    cannot be disguised as free-form text.
    """

    kind: str
    text: str

    def to_json_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text}


@dataclass(frozen=True, slots=True)
class RawInboundV1:
    """Pure JSON-compatible transport facts presented to the control plane."""

    schema_version: int
    surface: str
    source_namespace: str
    source_request_id: str
    reply_target: Mapping[str, Any]
    content: tuple[RawContentBlockV1, ...]
    external_subject: Mapping[str, Any] | None = None
    explicit_conversation_id: str | None = None
    project_command: Mapping[str, Any] | None = None
    attachments: tuple[SourceAttachmentDescriptorV1, ...] = field(default_factory=tuple)
    file_selections: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    requested_options: Mapping[str, Any] = field(default_factory=dict)
    retry_of_turn_id: str | None = None
    transport_facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "reply_target", _freeze_mapping(self.reply_target, "reply_target")
        )
        object.__setattr__(self, "content", tuple(self.content))
        object.__setattr__(
            self,
            "external_subject",
            (
                _freeze_mapping(self.external_subject, "external_subject")
                if self.external_subject is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "project_command",
            (
                _freeze_mapping(self.project_command, "project_command")
                if self.project_command is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "attachments",
            tuple(self.attachments),
        )
        if any(
            not isinstance(item, SourceAttachmentDescriptorV1)
            for item in self.attachments
        ):
            raise TypeError(
                "attachments must contain SourceAttachmentDescriptorV1 values"
            )
        object.__setattr__(
            self,
            "file_selections",
            tuple(
                _freeze_mapping(item, "file_selection") for item in self.file_selections
            ),
        )
        object.__setattr__(
            self,
            "requested_options",
            _freeze_mapping(self.requested_options, "requested_options"),
        )
        object.__setattr__(
            self,
            "transport_facts",
            _freeze_mapping(self.transport_facts, "transport_facts"),
        )

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "surface": self.surface,
            "source_namespace": self.source_namespace,
            "source_request_id": self.source_request_id,
            "external_subject": _thaw_json(self.external_subject),
            "reply_target": _thaw_json(self.reply_target),
            "explicit_conversation_id": self.explicit_conversation_id,
            "project_command": _thaw_json(self.project_command),
            "content": [block.to_json_dict() for block in self.content],
            "attachments": [item.to_json_dict() for item in self.attachments],
            "file_selections": _thaw_json(self.file_selections),
            "requested_options": _thaw_json(self.requested_options),
            "retry_of_turn_id": self.retry_of_turn_id,
            "transport_facts": _thaw_json(self.transport_facts),
        }


@dataclass(frozen=True, slots=True)
class InboundEnvelopeV1:
    """Immutable accepted Turn facts; live execution ports never appear here."""

    schema_version: int
    turn_id: str
    turn_kind: str
    conversation_id: str
    surface: str
    project_id: str | None
    workspace_id: str
    content: tuple[Mapping[str, Any], ...]
    source_attribution: Mapping[str, Any]
    reply_target: Mapping[str, Any]
    requested_options: Mapping[str, Any]
    retry_of_turn_id: str | None
    accepted_at_ms: int
    attachment_refs: tuple[AttachmentReferenceV1, ...] = field(default_factory=tuple)
    file_refs: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content",
            tuple(_freeze_mapping(block, "content block") for block in self.content),
        )
        object.__setattr__(self, "attachment_refs", tuple(self.attachment_refs))
        if any(
            not isinstance(item, AttachmentReferenceV1) for item in self.attachment_refs
        ):
            raise TypeError("attachment_refs must contain AttachmentReferenceV1 values")
        object.__setattr__(
            self,
            "file_refs",
            tuple(_freeze_mapping(item, "file reference") for item in self.file_refs),
        )
        object.__setattr__(
            self,
            "source_attribution",
            _freeze_mapping(self.source_attribution, "source_attribution"),
        )
        object.__setattr__(
            self, "reply_target", _freeze_mapping(self.reply_target, "reply_target")
        )
        object.__setattr__(
            self,
            "requested_options",
            _freeze_mapping(self.requested_options, "requested_options"),
        )

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "turn_id": self.turn_id,
            "turn_kind": self.turn_kind,
            "conversation_id": self.conversation_id,
            "surface": self.surface,
            "project_id": self.project_id,
            "workspace": {"workspace_id": self.workspace_id},
            "content": _thaw_json(self.content),
            "attachment_refs": [
                reference.to_json_dict() for reference in self.attachment_refs
            ],
            "file_refs": _thaw_json(self.file_refs),
            "source_attribution": _thaw_json(self.source_attribution),
            "reply_target": _thaw_json(self.reply_target),
            "requested_options": _thaw_json(self.requested_options),
            "retry_of_turn_id": self.retry_of_turn_id,
            "accepted_at_ms": self.accepted_at_ms,
        }


@dataclass(frozen=True, slots=True)
class TurnAcceptancePlan:
    """Read-only proposed/canonical identity view before durable acceptance."""

    state: str
    turn_id: str = ""
    conversation_id: str = ""
    project_id: str | None = None
    proposed_turn_id: str | None = None
    proposed_conversation_id: str | None = None
    code: str = ""


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    scope_kind: str
    project_id: str | None
    run_kind: str
    parent_turn_id: str | None
    retry_of_run_id: str | None
    status: str
    terminal_code: RunTerminalCode | None
    manifest_ref: str
    created_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None
    revision: int


@dataclass(frozen=True, slots=True)
class AutoAgentSessionRecord:
    """Durable AutoAgent lifecycle and immutable start authority."""

    session_id: str
    cwd: str
    output_dir: str
    skill: str
    method: str
    evolution_goal: str
    creation_receipt_sha256: str | None
    cancel_requested_at_ms: int | None
    execution_reference_type: str | None
    execution_reference: str | None
    owner_stopped_at_ms: int | None
    owner_stop_evidence: str | None
    status: str
    result: Mapping[str, Any] | None
    result_sha256: str | None
    error_code: str | None
    error_detail: str | None
    created_at_ms: int
    updated_at_ms: int
    finished_at_ms: int | None
    revision: int

    def __post_init__(self) -> None:
        if self.result is not None:
            object.__setattr__(
                self,
                "result",
                _freeze_mapping(self.result, "result"),
            )


@dataclass(frozen=True, slots=True)
class AutoAgentCancellationResult:
    """Receipt-bound cancellation command outcome."""

    status: str
    session: AutoAgentSessionRecord | None


@dataclass(frozen=True, slots=True)
class RunAssignmentRecord:
    """Immutable observation of the one executor assignment for a Run."""

    run_id: str
    assignment_id: str
    executor_kind: str
    execution_reference_type: str | None
    execution_reference: str | None
    assigned_at_ms: int


@dataclass(frozen=True, slots=True)
class RunObservationSnapshot:
    """One transactionally consistent Run Receipt and Assignment snapshot."""

    receipt: RunRecord
    assignment: RunAssignmentRecord | None


@dataclass(frozen=True, slots=True)
class RunObservationPage:
    """One bounded newest-first keyset page of durable Run observations."""

    observations: tuple[RunObservationSnapshot, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RunIntegrityIncidentRecord:
    """One append-only, content-free Run integrity observation."""

    incident_id: str
    run_id: str
    assignment_id: str
    incident_type: RunIntegrityIncidentType
    evidence_code: RunIntegrityEvidenceCode
    receipt_revision: int
    evidence_schema_version: int
    evidence_sha256: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class RunIntegrityIncidentIntent:
    """Typed runtime request to append one closed integrity fact."""

    run_id: str
    assignment_id: str
    incident_type: RunIntegrityIncidentType
    evidence_code: RunIntegrityEvidenceCode

    def __post_init__(self) -> None:
        if not isinstance(self.incident_type, RunIntegrityIncidentType):
            object.__setattr__(
                self,
                "incident_type",
                RunIntegrityIncidentType(self.incident_type),
            )
        if not isinstance(self.evidence_code, RunIntegrityEvidenceCode):
            object.__setattr__(
                self,
                "evidence_code",
                RunIntegrityEvidenceCode(self.evidence_code),
            )
        allowed = {
            RunIntegrityIncidentType.ASSIGNMENT_FENCE_VIOLATION: {
                RunIntegrityEvidenceCode.ASSIGNMENT_MISSING,
                RunIntegrityEvidenceCode.ASSIGNMENT_ID_MISMATCH,
            },
            RunIntegrityIncidentType.TERMINAL_REPORT_CONFLICT: {
                RunIntegrityEvidenceCode.TERMINAL_STATE_CONFLICT,
            },
            RunIntegrityIncidentType.MANIFEST_RECEIPT_MISMATCH: {
                RunIntegrityEvidenceCode.MANIFEST_RECEIPT_BINDING_MISMATCH,
                RunIntegrityEvidenceCode.MANIFEST_ASSIGNMENT_MISMATCH,
                RunIntegrityEvidenceCode.MANIFEST_COMPLETION_INVALID,
                RunIntegrityEvidenceCode.MANIFEST_TERMINAL_CONFLICT,
            },
            RunIntegrityIncidentType.EXECUTION_OWNER_UNCONFIRMED: {
                RunIntegrityEvidenceCode.EXECUTION_REFERENCE_MISSING,
                RunIntegrityEvidenceCode.EXECUTION_OWNER_STOP_UNCONFIRMED,
                RunIntegrityEvidenceCode.DISPATCHER_OWNER_MISSING,
            },
            RunIntegrityIncidentType.RECOVERY_TERMINAL_COMMIT_FAILED: {
                RunIntegrityEvidenceCode.RECOVERY_TERMINAL_REPORT_REJECTED,
                RunIntegrityEvidenceCode.RECOVERY_TERMINAL_TRANSACTION_FAILED,
            },
        }
        if self.evidence_code not in allowed[self.incident_type]:
            raise ValueError("evidence_code does not match incident_type")


@dataclass(frozen=True, slots=True)
class RunIntegrityIncidentAppendResult:
    created: bool
    incident: RunIntegrityIncidentRecord


@dataclass(frozen=True, slots=True)
class RunIntegrityIncidentPage:
    incidents: tuple[RunIntegrityIncidentRecord, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    delivery_id: str
    turn_id: str
    conversation_id: str
    purpose: str
    terminal_kind: str
    surface: str
    reply_target_key: str
    reply_target: Mapping[str, Any]
    target_sequence: int
    resend_of_delivery_id: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class DeliveryItemRecord:
    item_id: str
    delivery_id: str
    ordinal: int
    item_kind: str
    content_store: str
    content_ref: str
    content_sha256: str
    state: str
    attempt_count: int
    next_attempt_at_ms: int | None
    last_error_code: str | None
    blocked_by_item_id: str | None
    # ADR 0060 requires a terminal Item to retain provider evidence for audit.
    # It is stored per Item as the outcome of the deciding Attempt; the full
    # per-Attempt history is read separately via `list_delivery_attempts`.
    provider_evidence: Mapping[str, Any] | None = None
    delivered_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DeliveryAttemptRecord:
    """One recorded provider call against a Delivery Item.

    This is the audit unit ADR 0060 promises for `delivered`, `failed` and
    `unknown` Items: it says how many times the Backend really called the
    provider, when, and what each call returned.
    """

    attempt_id: str
    item_id: str
    attempt_no: int
    started_at_ms: int
    finished_at_ms: int | None
    outcome: str | None
    error_code: str | None
    provider_evidence: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class DeliveryCandidate:
    """One due Outbox head, still requiring a transactional barrier claim."""

    delivery_id: str
    item_id: str
    surface: str
    reply_target_key: str
    reply_target: Mapping[str, Any]
    target_sequence: int
    ordinal: int
    item_kind: str
    content_store: str
    content_ref: str
    content_sha256: str
    content_range: Mapping[str, Any] | None
    render_version: int
    media_type: str | None
    caption_ref: str | None
    caption_sha256: str | None
    attempt_count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reply_target",
            _freeze_mapping(self.reply_target, "reply_target"),
        )
        if self.content_range is not None:
            object.__setattr__(
                self,
                "content_range",
                _freeze_mapping(self.content_range, "content_range"),
            )


@dataclass(frozen=True, slots=True)
class DeliveryAttemptRequest:
    """The exact immutable provider-call request created by a durable claim."""

    attempt_id: str
    attempt_no: int
    candidate: DeliveryCandidate
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")

    @property
    def item_id(self) -> str:
        return self.candidate.item_id

    @property
    def delivery_id(self) -> str:
        return self.candidate.delivery_id

    @property
    def reply_target(self) -> Mapping[str, Any]:
        return self.candidate.reply_target


@dataclass(frozen=True, slots=True)
class DeliveryAdapterResult:
    """One Adapter call's classified result, safe to persist verbatim."""

    outcome: DeliveryAttemptOutcome
    error_code: str | None = None
    provider_evidence: Mapping[str, Any] | None = None
    retry_after_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, DeliveryAttemptOutcome):
            object.__setattr__(
                self,
                "outcome",
                DeliveryAttemptOutcome(self.outcome),
            )
        if self.error_code is not None and not isinstance(self.error_code, str):
            raise TypeError("error_code must be a string or None")
        if self.error_code is not None and (
            not self.error_code or len(self.error_code) > 128
        ):
            raise ValueError("error_code must be non-empty and bounded")
        if self.provider_evidence is not None:
            validate_delivery_provider_evidence(self.provider_evidence)
            object.__setattr__(
                self,
                "provider_evidence",
                _freeze_mapping(self.provider_evidence, "provider_evidence"),
            )
        if self.retry_after_ms is not None and (
            not isinstance(self.retry_after_ms, int)
            or isinstance(self.retry_after_ms, bool)
            or self.retry_after_ms < 0
        ):
            raise ValueError("retry_after_ms must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class DeliveryAttemptClaim:
    claimed: bool
    code: str
    request: DeliveryAttemptRequest | None = None


@dataclass(frozen=True, slots=True)
class DeliveryCapacitySnapshot:
    """Durable Channel capacity split across future Turns and actual Outbox work."""

    future_deliveries: int
    actual_deliveries: int
    actual_items: int

    @property
    def total_deliveries(self) -> int:
        return self.future_deliveries + self.actual_deliveries


@dataclass(frozen=True, slots=True)
class DeliveryStartupRecoveryResult:
    unknown_item_ids: tuple[str, ...]
    closed_attempt_ids: tuple[str, ...]
    suppressed_item_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeliveryStatusSummary:
    """One Delivery plus its ordered Items and derived operational state.

    ``state`` is the Owner/operator-facing rollup described in the design:
    ``delivered`` once every Item is delivered, otherwise the outcome of the
    first non-delivered Item in ordinal order (``in_progress`` while it is still
    queued/sending/retry_wait, ``failed``/``unknown`` once it reaches that
    terminal outcome, or ``blocked`` for a suppressed head).
    """

    delivery: DeliveryRecord
    items: tuple[DeliveryItemRecord, ...]
    state: str


@dataclass(frozen=True, slots=True)
class DeliveryOperationOutcome:
    """The typed result of an explicit Owner/operator Delivery action.

    ``code`` is one of ``resent``, ``retry_rearmed``, ``no_retryable_items``,
    ``delivery_not_found``, ``delivery_not_settled``,
    ``delivery_backpressure`` or ``delivery_unavailable``.  A ``resent``
    outcome carries the freshly created ``purpose=resend`` Delivery; a
    ``retry_rearmed`` outcome reports how many ``retry_wait`` Items had their
    backoff expedited.
    """

    code: str
    delivery: DeliveryRecord | None = None
    rearmed_items: int = 0


@dataclass(frozen=True, slots=True)
class ProjectionIntentRecord:
    projection_intent_id: str
    project_id: str
    origin_kind: str
    origin_id: str
    projection_kind: str
    projection_schema_version: int
    source_store: str
    source_ref: str
    content_sha256: str
    state: str
    last_error_code: str | None
    created_at_ms: int
    updated_at_ms: int
    applied_at_ms: int | None


@dataclass(frozen=True, slots=True)
class TurnAcceptanceIntent:
    surface: str
    source_namespace: str
    source_request_id: str
    fingerprint_version: int
    fingerprint_sha256: str
    reply_target: Mapping[str, Any]
    project_id: str | None = None
    explicit_conversation_id: str | None = None
    new_conversation: bool = False
    turn_kind: str = "agent"
    retry_of_turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class TurnAcceptanceResult:
    """Admission outcome plus the Turn's ordered accepted Attachment References.

    ADR 0059 requires that a matching duplicate return the original Turn *and*
    its original ordered Attachment Records, so the references belong to the
    shared Normalizer contract rather than to any single Surface Adapter.
    """

    status: TurnAcceptanceStatus
    turn_id: str = ""
    conversation_id: str = ""
    code: str = ""
    attachment_refs: tuple[AttachmentReferenceV1, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "attachment_refs", tuple(self.attachment_refs))
        if any(
            not isinstance(item, AttachmentReferenceV1) for item in self.attachment_refs
        ):
            raise TypeError("attachment_refs must contain AttachmentReferenceV1 values")


@dataclass(frozen=True, slots=True)
class RunAcceptanceIntent:
    run_submission_id: str
    fingerprint_version: int
    fingerprint_sha256: str
    run_kind: str
    scope_kind: str
    manifest_ref: str
    project_id: str | None = None
    parent_turn_id: str | None = None
    retry_of_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunAcceptancePlan:
    """Read-only proposed/canonical Run identity before durable acceptance."""

    state: str
    run_id: str = ""
    proposed_run_id: str | None = None
    code: str = ""


@dataclass(frozen=True, slots=True)
class RunAcceptanceResult:
    status: RunAcceptanceStatus
    run_id: str = ""
    code: str = ""


@dataclass(frozen=True, slots=True)
class StateChangeResult:
    changed: bool
    code: str


@dataclass(frozen=True, slots=True)
class TurnStartupReconciliationResult:
    """Non-replayable Turn receipts interrupted by one startup barrier."""

    interrupted_turn_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunStartupReconciliationResult:
    """Receipts closed or left quarantined by one no-replay barrier."""

    interrupted_run_ids: tuple[str, ...]
    unconfirmed_run_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AutoAgentStartupReconciliationResult:
    """Process-local AutoAgent workers closed without replay on startup."""

    interrupted_session_ids: tuple[str, ...]
    unconfirmed_session_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnTerminalOutcome:
    """Untrusted terminal intent returned by one process-local Turn Worker.

    Construction remains permissive on purpose: Workers are an untyped plugin
    boundary and may return malformed objects. ``TurnSequencer`` is the single
    normalization seam that converts every invalid value to a closed generic
    outcome before persistence.
    """

    terminal_status: str
    terminal_code: TurnTerminalCode | None = None


@dataclass(frozen=True, slots=True)
class TurnExecutionResult:
    """Observation of one TurnSequencer activation attempt."""

    state: str
    turn_id: str = ""
    conversation_id: str = ""
    terminal_status: str = ""
    terminal_code: TurnTerminalCode | None = None
    event_published: bool = True


@dataclass(frozen=True, slots=True)
class ProjectLifecycleResult:
    status: ProjectLifecycleStatus
    project: ProjectRecord | None = None
    code: str = ""


@dataclass(frozen=True, slots=True)
class AssignmentResult:
    status: AssignmentStatus
    assignment_id: str = ""
    code: str = ""


@dataclass(frozen=True, slots=True)
class ProjectionIntentInput:
    projection_kind: str
    source_store: str
    source_ref: str
    content_sha256: str
    projection_schema_version: int = 1


@dataclass(frozen=True, slots=True)
class DeliveryItemPlan:
    item_kind: str
    content_store: str
    content_ref: str
    content_sha256: str
    content_range: Mapping[str, Any] | None = None
    render_version: int = 1
    media_type: str | None = None
    caption_ref: str | None = None
    caption_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    terminal_kind: str
    items: tuple[DeliveryItemPlan, ...]


@dataclass(frozen=True, slots=True)
class TerminalizeTurnResult:
    changed: bool
    code: str
    delivery: DeliveryRecord | None = None


@dataclass(frozen=True, slots=True)
class RunReport:
    """Typed executor report validated before it reaches Repository authority."""

    run_id: str
    assignment_id: str
    terminal_status: str
    terminal_code: RunTerminalCode | None = None
    projections: tuple[ProjectionIntentInput, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.terminal_status, str)
            or self.terminal_status not in RUN_TERMINAL_CODES_BY_STATUS
        ):
            raise ValueError("invalid terminal Run status")
        if self.terminal_status == "succeeded":
            if self.terminal_code is not None:
                raise ValueError("succeeded Run must not have terminal_code")
        elif self.terminal_code is not None and not is_allowed_run_terminal_code(
            self.terminal_status,
            self.terminal_code,
        ):
            raise ValueError(
                "Run terminal_code must be a closed non-secret code "
                f"for status {self.terminal_status}"
            )
        object.__setattr__(self, "projections", tuple(self.projections))


@dataclass(frozen=True, slots=True)
class AttemptStartResult:
    started: bool
    code: str
    attempt_id: str = ""
    item_id: str = ""
    attempt_no: int = 0


@dataclass(frozen=True, slots=True)
class IdempotencyInspection:
    state: str
    canonical_id: str = ""
    code: str = ""


__all__ = [
    "AutoAgentSessionRecord",
    "AutoAgentStartupReconciliationResult",
    "AssignmentResult",
    "AssignmentStatus",
    "AttemptStartResult",
    "ConversationRecord",
    "DeliveryAdapterResult",
    "DeliveryAttemptClaim",
    "DeliveryAttemptOutcome",
    "DeliveryAttemptRequest",
    "DeliveryCandidate",
    "DeliveryCapacitySnapshot",
    "DeliveryStartupRecoveryResult",
    "DeliveryItemPlan",
    "DeliveryAttemptRecord",
    "DeliveryItemRecord",
    "DeliveryPlan",
    "DeliveryRecord",
    "IdempotencyInspection",
    "ProjectLifecycleResult",
    "ProjectLifecycleStatus",
    "ProjectRecord",
    "ProjectionIntentInput",
    "ProjectionIntentRecord",
    "RawContentBlockV1",
    "RawInboundV1",
    "RunAcceptancePlan",
    "RunAcceptanceIntent",
    "RunAcceptanceResult",
    "RunAcceptanceStatus",
    "RunAssignmentRecord",
    "RunIntegrityEvidenceCode",
    "RunIntegrityIncidentAppendResult",
    "RunIntegrityIncidentIntent",
    "RunIntegrityIncidentPage",
    "RunIntegrityIncidentRecord",
    "RunIntegrityIncidentType",
    "RunObservationSnapshot",
    "RunRecord",
    "RunReport",
    "RunStartupReconciliationResult",
    "StateChangeResult",
    "TerminalizeTurnResult",
    "TurnExecutionResult",
    "InboundEnvelopeV1",
    "TurnAcceptancePlan",
    "TurnAcceptanceIntent",
    "TurnAcceptanceResult",
    "TurnAcceptanceStatus",
    "TurnRecord",
    "TurnTranscriptRef",
    "TurnStartupReconciliationResult",
    "TurnTerminalOutcome",
]
