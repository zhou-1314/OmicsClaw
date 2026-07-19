"""Backend-owned conversational ingress admission for the isolated control plane.

The public Module is deliberately small: a Surface supplies immutable
``RawInboundV1`` facts, and ``IngressNormalizer.accept`` returns only a typed
receipt outcome.  Fingerprinting, Owner admission, identity planning, capacity
reservation, durable acceptance, and Envelope construction stay behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import Callable, Mapping

from omicsclaw.attachments import (
    AttachmentIntegrityError,
    AttachmentReferenceV1,
    AttachmentStore,
    InboundAttachmentSource,
)

from .models import (
    InboundEnvelopeV1,
    RawContentBlockV1,
    RawInboundV1,
    TurnAcceptanceIntent,
    TurnAcceptancePlan,
    TurnAcceptanceResult,
    TurnAcceptanceStatus,
)
from .errors import ControlIntegrityError, TurnConversationUnavailableError
from .repository import ControlStateRepository
from .turn_runtime import TurnSequencer


def _is_opaque_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_nonempty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _validate_reply_target(raw: RawInboundV1) -> None:
    target = raw.reply_target
    if target.get("schema_version") != 1 or target.get("kind") != raw.surface:
        raise ValueError("Reply Target schema does not match Surface")
    if raw.surface in {"cli", "desktop"}:
        expected = {"schema_version", "kind", "installation_id", "profile_id", "slot"}
        if set(target) != expected:
            raise ValueError("Local Reply Target fields do not match V1")
        for field_name in ("installation_id", "profile_id", "slot"):
            _require_nonempty_string(target.get(field_name), field_name)
        return

    required = {
        "schema_version",
        "kind",
        "adapter",
        "account_namespace",
        "destination_id",
    }
    # `destination_kind` disambiguates providers whose destination identifier is
    # not self-describing.  Feishu, for example, addresses the same send call by
    # chat, open, union or user ID, and the Adapter cannot infer which one a bare
    # string is.  It stays optional and platform-neutral: Telegram omits it, and
    # omitting it keeps an existing Reply Target's key byte-identical because the
    # key is a canonical-JSON digest of the whole target.
    optional = {"thread_id", "destination_kind"}
    if not required.issubset(target) or set(target) - (required | optional):
        raise ValueError("Channel Reply Target fields do not match V1")
    for field_name in ("adapter", "account_namespace", "destination_id"):
        _require_nonempty_string(target.get(field_name), field_name)
    thread_id = target.get("thread_id")
    if thread_id is not None:
        _require_nonempty_string(thread_id, "thread_id")
    destination_kind = target.get("destination_kind")
    if destination_kind is not None:
        _require_nonempty_string(destination_kind, "destination_kind")


@dataclass(frozen=True, slots=True)
class IngressBackendConfig:
    """Backend configuration; a Surface cannot replace these authorities."""

    workspace_id: str
    trusted_local_source_namespaces: Mapping[str, frozenset[str]] = field(
        default_factory=dict
    )
    # Desktop's App-server owns and persists the opaque installation ID. The
    # Backend accepts only that bounded shape under a configured local profile;
    # it does not turn installation or profile into an Owner/state partition.
    trusted_opaque_installation_profiles: Mapping[str, frozenset[str]] = field(
        default_factory=dict
    )
    owner_identities: Mapping[str, frozenset[str]] = field(default_factory=dict)
    channel_delivery_enabled: bool = False
    attachment_input_enabled: bool = False
    # Channel Adapters whose Attachment Store cutover has landed. One shared
    # Channel runtime may serve Adapters at different cutover stages, so
    # enabling attachments for one must not open inbound bytes for another.
    # Empty means no Channel Adapter may submit attachments.
    attachment_input_adapters: frozenset[str] = frozenset()
    max_outstanding_deliveries_total: int = 64
    max_outstanding_deliveries_per_account: int = 32
    max_source_identity_chars: int = 512
    max_content_blocks: int = 32
    max_text_chars: int = 1_000_000
    max_requested_options: int = 32
    max_transport_facts: int = 32
    max_raw_json_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise ValueError("workspace_id must be non-empty")
        if not isinstance(self.channel_delivery_enabled, bool):
            raise ValueError("channel_delivery_enabled must be a boolean")
        if not isinstance(self.attachment_input_enabled, bool):
            raise ValueError("attachment_input_enabled must be a boolean")
        for surface, namespaces in self.trusted_local_source_namespaces.items():
            if surface not in {"cli", "desktop"}:
                raise ValueError("Only CLI/Desktop may be trusted local sources")
            if not namespaces or any(
                not isinstance(value, str) or not value.strip() for value in namespaces
            ):
                raise ValueError("Trusted local source namespaces must be non-empty")
        for surface, profiles in self.trusted_opaque_installation_profiles.items():
            if surface != "desktop":
                raise ValueError(
                    "Only Desktop may derive a namespace from an opaque App installation"
                )
            if not profiles or any(
                not isinstance(value, str) or not value.strip() for value in profiles
            ):
                raise ValueError(
                    "Trusted opaque-installation profiles must be non-empty"
                )
        for identity_scope, identities in self.owner_identities.items():
            if not isinstance(identity_scope, str):
                raise ValueError("Owner Identity scope must be a string")
            parts = identity_scope.split("/")
            if len(parts) != 4 or parts[0] != "channel" or not all(parts[1:]):
                raise ValueError(
                    "Owner Identity scope must be channel/adapter/account/kind"
                )
            if not identities:
                raise ValueError("Owner Identity configuration must be non-empty")
            if any(
                not isinstance(value, str) or not value.strip() for value in identities
            ):
                raise ValueError("Owner Identity values must be non-empty")
        for field_name in (
            "max_source_identity_chars",
            "max_content_blocks",
            "max_text_chars",
            "max_requested_options",
            "max_transport_facts",
            "max_raw_json_bytes",
            "max_outstanding_deliveries_total",
            "max_outstanding_deliveries_per_account",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


def semantic_fingerprint_v1(raw: RawInboundV1) -> str:
    """Return the canonical V1 semantic fingerprint (never a request ID)."""

    document = raw.to_json_dict()
    payload = {
        "fingerprint_version": 1,
        "surface": document["surface"],
        "source_namespace": document["source_namespace"],
        "reply_target": document["reply_target"],
        "explicit_conversation_id": document["explicit_conversation_id"],
        "project_command": document["project_command"],
        "content": document["content"],
        "attachments": document["attachments"],
        "file_selections": document["file_selections"],
        "requested_options": document["requested_options"],
        "retry_of_turn_id": document["retry_of_turn_id"],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class IngressNormalizer:
    """Deep, Backend-owned admission Module shared by future Surfaces."""

    def __init__(
        self,
        repository: ControlStateRepository,
        admission_queue: TurnSequencer,
        config: IngressBackendConfig,
        *,
        attachment_store: AttachmentStore | None = None,
        enqueue_failure_terminalizer: Callable[[TurnAcceptanceResult], None]
        | None = None,
        attachment_failure_terminalizer: Callable[[TurnAcceptanceResult], None]
        | None = None,
    ) -> None:
        self._repository = repository
        self._admission_queue = admission_queue
        self._config = config
        self._attachment_store = attachment_store
        self._enqueue_failure_terminalizer = enqueue_failure_terminalizer
        self._attachment_failure_terminalizer = attachment_failure_terminalizer
        if config.attachment_input_enabled and attachment_store is None:
            raise ValueError(
                "attachment_input_enabled requires a Backend-owned AttachmentStore"
            )

    def accept(self, raw: RawInboundV1) -> TurnAcceptanceResult:
        return self._accept(raw)

    async def accept_async(
        self,
        raw: RawInboundV1,
        *,
        attachment_source: InboundAttachmentSource | None = None,
    ) -> TurnAcceptanceResult:
        """Accept through the production async admission path.

        The process-local source is deliberately separate from ``RawInboundV1``.
        A bounded ingress-key guard makes the durable duplicate lookup win before
        any byte access, while the Reply Target guard preserves acceptance order
        for one Conversation address without serializing unrelated addresses.
        """

        try:
            self._validate_source_identity(raw)
            target_json = json.dumps(
                dict(raw.reply_target),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError):
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                code="invalid_inbound",
            )
        ingress_material = (
            f"{raw.surface}\0{raw.source_namespace}\0{raw.source_request_id}"
        ).encode("utf-8")
        ingress_key = "ingress:" + hashlib.sha256(ingress_material).hexdigest()
        address_key = (
            "address:" + hashlib.sha256(target_json.encode("utf-8")).hexdigest()
        )
        async with self._admission_queue.admission_guard(ingress_key):
            async with self._admission_queue.admission_guard(address_key):
                if not raw.attachments:
                    if attachment_source is not None:
                        return TurnAcceptanceResult(
                            TurnAcceptanceStatus.REJECTED,
                            code="invalid_inbound",
                        )
                    return self.accept(raw)
                return await self._accept_attachments(raw, attachment_source)

    def _attachment_adapter_admitted(self, raw: RawInboundV1) -> bool:
        """Only a Channel Adapter with a landed Attachment cutover may submit bytes.

        A shared Channel runtime may serve Adapters at different cutover stages,
        so the global switch alone is not sufficient authority.  Local Surfaces
        keep using the global switch because they have no Adapter identity.
        """

        if raw.surface != "channel":
            return True
        adapter = str(raw.reply_target.get("adapter", ""))
        return adapter in self._config.attachment_input_adapters

    def _accepted_attachment_refs(
        self, turn_id: str, conversation_id: str
    ) -> tuple[AttachmentReferenceV1, ...]:
        """Read one existing Turn's ordered accepted Attachment References.

        ADR 0059 requires a duplicate to observe the original Records, so the
        control-plane batch commitment — not the Store's answer alone — decides
        what an empty result means.  A Turn with no commitment (it never carried
        attachments) and a Backend with no Attachment Store both legitimately
        read empty.  When the control plane committed to N attachments, the only
        acceptable answers are exactly those N accepted References or a
        recognised non-acceptance; anything else is an integrity incident rather
        than a silent "text Turn with no attachments".
        """

        store = self._attachment_store
        if store is None or not turn_id or not conversation_id:
            return ()
        try:
            references = store.get_turn_references(turn_id, conversation_id)
        except AttachmentIntegrityError as exc:
            raise ControlIntegrityError(
                "accepted Attachment References are unavailable for this Turn"
            ) from exc
        commitment = self._repository.get_turn_attachment_commitment(turn_id)
        if commitment is None:
            return references
        if references:
            if len(references) != commitment.record_count:
                raise ControlIntegrityError(
                    "committed Attachment References are incomplete for this Turn"
                )
            return references
        # The control plane committed to >=1 attachment yet the Store returns
        # none.  A Turn that actually ran with its attachments (succeeded) must
        # still have them, so an empty answer there is lost committed content.
        # Non-succeeded outcomes — a synchronous finalize failure in particular
        # — legitimately never produced accepted Records.
        receipt = self._repository.get_turn(turn_id)
        if receipt is not None and receipt.status == "succeeded":
            raise ControlIntegrityError(
                "accepted Attachment References are unavailable for this Turn"
            )
        return references

    def _with_accepted_attachment_refs(
        self, result: TurnAcceptanceResult
    ) -> TurnAcceptanceResult:
        """Attach the durable References to an outcome that names a Turn."""

        if result.attachment_refs:
            return result
        references = self._accepted_attachment_refs(
            result.turn_id, result.conversation_id
        )
        if not references:
            return result
        return replace(result, attachment_refs=references)

    async def _accept_attachments(
        self,
        raw: RawInboundV1,
        attachment_source: InboundAttachmentSource | None,
    ) -> TurnAcceptanceResult:
        """Publish one immutable batch before atomically committing its Turn."""

        try:
            self._validate_source_identity(raw)
        except (TypeError, ValueError, RecursionError):
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                code="invalid_inbound",
            )
        if not self._source_is_admitted(raw):
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                code="owner_denied",
            )
        try:
            turn_kind, project_id, new_conversation = self._validate_payload(raw)
        except (TypeError, ValueError, RecursionError):
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                code="invalid_inbound",
            )
        if raw.file_selections:
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                code="file_selections_not_supported",
            )
        if raw.surface == "channel" and not self._config.channel_delivery_enabled:
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                code="channel_delivery_not_supported",
            )
        if not self._admission_queue.ready:
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                code="control_not_ready",
            )

        intent = TurnAcceptanceIntent(
            surface=raw.surface,
            source_namespace=raw.source_namespace,
            source_request_id=raw.source_request_id,
            fingerprint_version=1,
            fingerprint_sha256=semantic_fingerprint_v1(raw),
            reply_target=dict(raw.reply_target),
            project_id=project_id,
            explicit_conversation_id=raw.explicit_conversation_id,
            new_conversation=new_conversation,
            turn_kind=turn_kind,
            retry_of_turn_id=raw.retry_of_turn_id,
        )
        plan = self._repository.plan_turn_acceptance(intent)
        if plan.state == "duplicate":
            return self._with_accepted_attachment_refs(
                TurnAcceptanceResult(
                    TurnAcceptanceStatus.DUPLICATE,
                    plan.turn_id,
                    plan.conversation_id,
                )
            )
        if plan.state == "conflict":
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.CONFLICT,
                plan.turn_id,
                plan.conversation_id,
                plan.code,
            )
        if plan.state == "rejected":
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                conversation_id=plan.conversation_id,
                code=plan.code,
            )

        store = self._attachment_store
        if (
            not self._config.attachment_input_enabled
            or store is None
            or attachment_source is None
            or not callable(getattr(attachment_source, "open", None))
            or not self._attachment_adapter_admitted(raw)
        ):
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                conversation_id=plan.conversation_id,
                code="attachments_not_supported",
            )
        if not _is_opaque_id(plan.proposed_turn_id) or not _is_opaque_id(
            plan.conversation_id
        ):
            raise ControlIntegrityError(
                "Attachment admission plan has no usable proposed identities"
            )

        delivery_reservation = None
        if raw.surface == "channel":
            delivery_reservation = self._admission_queue.try_reserve_delivery(
                raw.reply_target,
                max_total=self._config.max_outstanding_deliveries_total,
                max_per_account=(self._config.max_outstanding_deliveries_per_account),
            )
            if delivery_reservation is None:
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.REJECTED,
                    conversation_id=plan.conversation_id,
                    code="delivery_backpressure",
                )

        try:
            try:
                reservation = self._admission_queue.try_reserve(plan.conversation_id)
            except TurnConversationUnavailableError:
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.REJECTED,
                    code="turn_execution_unavailable",
                )
            if reservation is None:
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.REJECTED,
                    code="turn_backpressure",
                )

            publication = None
            accepted: TurnAcceptanceResult | None = None
            references: tuple[AttachmentReferenceV1, ...] = ()
            reservation_finished = False
            try:
                try:
                    publication = await store.publish_batch(
                        proposed_turn_id=plan.proposed_turn_id,
                        proposed_conversation_id=plan.conversation_id,
                        descriptors=raw.attachments,
                        source=attachment_source,
                    )
                except AttachmentIntegrityError as exc:
                    reservation.release()
                    reservation_finished = True
                    raise ControlIntegrityError(
                        "Attachment Store integrity failed during publication"
                    ) from exc
                except Exception:
                    reservation.release()
                    reservation_finished = True
                    return TurnAcceptanceResult(
                        TurnAcceptanceStatus.REJECTED,
                        conversation_id=plan.conversation_id,
                        code="attachment_rejected",
                    )

                accepted = self._repository.accept_turn(
                    intent,
                    proposed_turn_id=plan.proposed_turn_id,
                    proposed_conversation_id=plan.proposed_conversation_id,
                    expected_conversation_id=plan.conversation_id,
                    attachment_commitment=publication.commitment,
                )
                if accepted.status is not TurnAcceptanceStatus.ACCEPTED:
                    store.abandon_batch(publication.commitment.batch_id)
                    reservation.release()
                    reservation_finished = True
                    if accepted.code == "acceptance_plan_stale":
                        return TurnAcceptanceResult(
                            TurnAcceptanceStatus.REJECTED,
                            conversation_id=accepted.conversation_id,
                            code="admission_contention",
                        )
                    # A concurrent submission won this identity; the caller must
                    # still observe that Turn's original ordered Records.
                    return self._with_accepted_attachment_refs(accepted)

                try:
                    references = store.accept_batch(publication.commitment)
                    if references != publication.references:
                        raise AttachmentIntegrityError(
                            "accepted Attachment references changed after publication"
                        )
                except Exception:
                    try:
                        self._terminalize_attachment_failure(accepted)
                        reservation.discard_terminalized()
                        reservation_finished = True
                    except BaseException as terminal_error:
                        reservation.quarantine()
                        reservation_finished = True
                        raise ControlIntegrityError(
                            "Attachment failure and terminal compensation both failed; "
                            "Conversation quarantined"
                        ) from terminal_error
                    return TurnAcceptanceResult(
                        TurnAcceptanceStatus.ACCEPTED,
                        accepted.turn_id,
                        accepted.conversation_id,
                        "attachment_finalize_failed",
                    )

                receipt = self._repository.get_turn(accepted.turn_id)
                envelope_content = tuple(
                    {"kind": "text", "text": block.text} for block in raw.content
                ) + tuple(
                    {
                        "kind": "attachment",
                        "attachment_id": reference.attachment_id,
                    }
                    for reference in references
                )
                envelope = InboundEnvelopeV1(
                    schema_version=1,
                    turn_id=accepted.turn_id,
                    turn_kind=turn_kind,
                    conversation_id=accepted.conversation_id,
                    surface=raw.surface,
                    project_id=plan.project_id,
                    workspace_id=self._config.workspace_id,
                    content=envelope_content,
                    source_attribution={
                        "surface": raw.surface,
                        "source_namespace": raw.source_namespace,
                        "source_request_id": raw.source_request_id,
                        "external_subject": (
                            dict(raw.external_subject)
                            if raw.external_subject is not None
                            else None
                        ),
                    },
                    reply_target=dict(raw.reply_target),
                    requested_options=dict(raw.requested_options),
                    retry_of_turn_id=raw.retry_of_turn_id,
                    accepted_at_ms=receipt.created_at_ms,
                    attachment_refs=references,
                )
                reservation.commit(envelope)
                reservation_finished = True
                return replace(accepted, attachment_refs=references)
            except Exception:
                if not reservation_finished:
                    if (
                        accepted is not None
                        and accepted.status is TurnAcceptanceStatus.ACCEPTED
                    ):
                        try:
                            self._terminalize_enqueue_failure(accepted)
                            reservation.discard_terminalized()
                            reservation_finished = True
                        except BaseException as terminal_error:
                            reservation.quarantine()
                            reservation_finished = True
                            raise ControlIntegrityError(
                                "Accepted Turn enqueue and terminal compensation "
                                "both failed; Conversation quarantined"
                            ) from terminal_error
                        # The batch was durably accepted before enqueue failed,
                        # so this failed Turn's accepted References must travel
                        # with the novel result exactly as a duplicate lookup
                        # would later return them.
                        return TurnAcceptanceResult(
                            TurnAcceptanceStatus.ACCEPTED,
                            accepted.turn_id,
                            accepted.conversation_id,
                            "dispatch_enqueue_failed",
                            attachment_refs=references,
                        )
                    if publication is not None:
                        store.abandon_batch(publication.commitment.batch_id)
                    reservation.release()
                    reservation_finished = True
                raise
            except BaseException:
                if not reservation_finished:
                    if (
                        accepted is not None
                        and accepted.status is TurnAcceptanceStatus.ACCEPTED
                    ):
                        reservation.quarantine()
                    else:
                        reservation.release()
                raise
        finally:
            if delivery_reservation is not None:
                delivery_reservation.finish()

    def _terminalize_attachment_failure(
        self,
        accepted: TurnAcceptanceResult,
    ) -> None:
        if self._attachment_failure_terminalizer is None:
            result = self._repository.terminalize_turn(
                accepted.turn_id,
                terminal_status="failed",
                terminal_code="attachment_finalize_failed",
            )
            if not result.changed:
                raise ControlIntegrityError(
                    "Attachment finalize failure could not be terminalized"
                )
            return
        self._attachment_failure_terminalizer(accepted)

    def _terminalize_enqueue_failure(
        self,
        accepted: TurnAcceptanceResult,
    ) -> None:
        if self._enqueue_failure_terminalizer is not None:
            self._enqueue_failure_terminalizer(accepted)
            return
        result = self._repository.terminalize_turn(
            accepted.turn_id,
            terminal_status="failed",
            terminal_code="dispatch_enqueue_failed",
        )
        if not result.changed:
            raise ControlIntegrityError(
                "Accepted Turn enqueue failure could not be terminalized"
            )

    def _accept(self, raw: RawInboundV1) -> TurnAcceptanceResult:
        try:
            self._validate_source_identity(raw)
        except (TypeError, ValueError, RecursionError):
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED, code="invalid_inbound"
            )

        if not self._source_is_admitted(raw):
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED, code="owner_denied"
            )
        try:
            turn_kind, project_id, new_conversation = self._validate_payload(raw)
        except (TypeError, ValueError, RecursionError):
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED, code="invalid_inbound"
            )
        if raw.attachments:
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED, code="attachments_not_supported"
            )
        if raw.file_selections:
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED, code="file_selections_not_supported"
            )
        if raw.surface == "channel" and not self._config.channel_delivery_enabled:
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                code="channel_delivery_not_supported",
            )
        if not self._admission_queue.ready:
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                code="control_not_ready",
            )

        fingerprint = semantic_fingerprint_v1(raw)
        intent = TurnAcceptanceIntent(
            surface=raw.surface,
            source_namespace=raw.source_namespace,
            source_request_id=raw.source_request_id,
            fingerprint_version=1,
            fingerprint_sha256=fingerprint,
            reply_target=dict(raw.reply_target),
            project_id=project_id,
            explicit_conversation_id=raw.explicit_conversation_id,
            new_conversation=new_conversation,
            turn_kind=turn_kind,
            retry_of_turn_id=raw.retry_of_turn_id,
        )

        return self._admission_queue.serialize_admission(
            lambda: self._accept_intent(
                raw,
                intent,
                turn_kind=turn_kind,
            )
        )

    def _accept_intent(
        self,
        raw: RawInboundV1,
        intent: TurnAcceptanceIntent,
        *,
        turn_kind: str,
    ) -> TurnAcceptanceResult:
        plan = self._repository.plan_turn_acceptance(intent)
        if plan.state == "duplicate":
            return self._with_accepted_attachment_refs(
                TurnAcceptanceResult(
                    TurnAcceptanceStatus.DUPLICATE,
                    plan.turn_id,
                    plan.conversation_id,
                )
            )
        if plan.state == "conflict":
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.CONFLICT,
                plan.turn_id,
                plan.conversation_id,
                plan.code,
            )
        if plan.state == "rejected":
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                conversation_id=plan.conversation_id,
                code=plan.code,
            )

        delivery_reservation = None
        if raw.surface == "channel":
            delivery_reservation = self._admission_queue.try_reserve_delivery(
                raw.reply_target,
                max_total=self._config.max_outstanding_deliveries_total,
                max_per_account=self._config.max_outstanding_deliveries_per_account,
            )
            if delivery_reservation is None:
                return TurnAcceptanceResult(
                    TurnAcceptanceStatus.REJECTED,
                    conversation_id=plan.conversation_id,
                    code="delivery_backpressure",
                )
        try:
            return self._accept_planned_intent(
                raw,
                intent,
                plan=plan,
                turn_kind=turn_kind,
            )
        finally:
            if delivery_reservation is not None:
                delivery_reservation.finish()

    def _accept_planned_intent(
        self,
        raw: RawInboundV1,
        intent: TurnAcceptanceIntent,
        *,
        plan: TurnAcceptancePlan,
        turn_kind: str,
    ) -> TurnAcceptanceResult:
        try:
            reservation = self._admission_queue.try_reserve(plan.conversation_id)
        except TurnConversationUnavailableError:
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                code="turn_execution_unavailable",
            )
        if reservation is None:
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED, code="turn_backpressure"
            )
        accepted: TurnAcceptanceResult | None = None
        try:
            accepted = self._repository.accept_turn(
                intent,
                proposed_turn_id=plan.proposed_turn_id,
                proposed_conversation_id=plan.proposed_conversation_id,
                expected_conversation_id=plan.conversation_id,
            )
            if accepted.status is not TurnAcceptanceStatus.ACCEPTED:
                reservation.release()
                if accepted.code == "acceptance_plan_stale":
                    return TurnAcceptanceResult(
                        TurnAcceptanceStatus.REJECTED,
                        code="admission_contention",
                    )
                return accepted
            receipt = self._repository.get_turn(accepted.turn_id)
            if raw.project_command is None:
                envelope_content = tuple(
                    {"kind": "text", "text": block.text} for block in raw.content
                )
            else:
                envelope_content = (
                    {
                        "kind": "control_command",
                        "command": dict(raw.project_command),
                        "result": {
                            "conversation_id": accepted.conversation_id,
                            "project_id": plan.project_id,
                        },
                    },
                )
            envelope = InboundEnvelopeV1(
                schema_version=1,
                turn_id=accepted.turn_id,
                turn_kind=turn_kind,
                conversation_id=accepted.conversation_id,
                surface=raw.surface,
                project_id=plan.project_id,
                workspace_id=self._config.workspace_id,
                content=envelope_content,
                source_attribution={
                    "surface": raw.surface,
                    "source_namespace": raw.source_namespace,
                    "source_request_id": raw.source_request_id,
                    "external_subject": (
                        dict(raw.external_subject)
                        if raw.external_subject is not None
                        else None
                    ),
                },
                reply_target=dict(raw.reply_target),
                requested_options=dict(raw.requested_options),
                retry_of_turn_id=raw.retry_of_turn_id,
                accepted_at_ms=receipt.created_at_ms,
            )
            reservation.commit(envelope)
            return accepted
        except Exception:
            if accepted is None or accepted.status is not TurnAcceptanceStatus.ACCEPTED:
                reservation.release()
                raise
            try:
                if self._enqueue_failure_terminalizer is not None:
                    self._enqueue_failure_terminalizer(accepted)
                    terminalized = self._repository.get_turn(accepted.turn_id)
                    terminalized_changed = terminalized.status == "failed"
                else:
                    result = self._repository.terminalize_turn(
                        accepted.turn_id,
                        terminal_status="failed",
                        terminal_code="dispatch_enqueue_failed",
                    )
                    terminalized_changed = result.changed
            except BaseException as terminal_error:
                reservation.quarantine()
                if not isinstance(terminal_error, Exception):
                    raise
                raise ControlIntegrityError(
                    "Accepted Turn enqueue and terminal compensation both failed; "
                    "Conversation quarantined"
                ) from terminal_error
            if not terminalized_changed:
                reservation.quarantine()
                raise ControlIntegrityError(
                    "Accepted Turn could not be queued or terminalized; "
                    "Conversation quarantined"
                )
            try:
                reservation.discard_terminalized()
            except BaseException:
                reservation.quarantine()
                raise
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.ACCEPTED,
                accepted.turn_id,
                accepted.conversation_id,
                "dispatch_enqueue_failed",
            )
        except BaseException:
            if (
                accepted is not None
                and accepted.status is TurnAcceptanceStatus.ACCEPTED
            ):
                reservation.quarantine()
            else:
                reservation.release()
            raise

    def _validate_source_identity(self, raw: RawInboundV1) -> None:
        if (
            not isinstance(raw.schema_version, int)
            or isinstance(raw.schema_version, bool)
            or raw.schema_version != 1
        ):
            raise ValueError("unsupported RawInbound schema")
        if raw.surface not in {"cli", "desktop", "channel"}:
            raise ValueError("unsupported Surface")
        source_namespace = _require_nonempty_string(
            raw.source_namespace, "source_namespace"
        )
        source_request_id = _require_nonempty_string(
            raw.source_request_id, "source_request_id"
        )
        if (
            len(source_namespace) > self._config.max_source_identity_chars
            or len(source_request_id) > self._config.max_source_identity_chars
        ):
            raise ValueError("source identity exceeds configured limit")
        _validate_reply_target(raw)
        if raw.external_subject is not None:
            if set(raw.external_subject) != {"kind", "value"}:
                raise ValueError("External Subject fields do not match V1")
            _require_nonempty_string(raw.external_subject.get("kind"), "subject kind")
            _require_nonempty_string(raw.external_subject.get("value"), "subject value")
        if raw.surface != "channel" and raw.external_subject is not None:
            raise ValueError("Local input cannot supply an External Subject")

    def _validate_payload(self, raw: RawInboundV1) -> tuple[str, str | None, bool]:
        if len(raw.content) > self._config.max_content_blocks:
            raise ValueError("too many content blocks")
        if any(
            not isinstance(block, RawContentBlockV1)
            or block.kind != "text"
            or not isinstance(block.text, str)
            for block in raw.content
        ):
            raise ValueError("unsupported content block")
        if sum(len(block.text) for block in raw.content) > self._config.max_text_chars:
            raise ValueError("text exceeds configured limit")
        if raw.project_command is None and not raw.content and not raw.attachments:
            raise ValueError("content must not be empty")
        if raw.project_command is not None and (raw.content or raw.attachments):
            raise ValueError("control command cannot also carry user content")
        allowed_option_keys = {
            "mode",
            "output_style",
            "model_override",
            "max_tokens",
            "system_prompt_append",
            "provider_options",
            "surface_options",
        }
        if len(raw.requested_options) > self._config.max_requested_options:
            raise ValueError("too many requested options")
        if any(key not in allowed_option_keys for key in raw.requested_options):
            raise ValueError("unsupported requested option")
        scalar_types = (str, int, float, bool, type(None))
        for key, value in raw.requested_options.items():
            if key in {"provider_options", "surface_options"}:
                if not isinstance(value, Mapping):
                    raise ValueError(f"{key} must be a mapping")
                if len(value) > self._config.max_requested_options:
                    raise ValueError(f"too many {key}")
                for provider_key, provider_value in value.items():
                    normalized_provider_key = (
                        provider_key.lower().replace("-", "_")
                        if isinstance(provider_key, str)
                        else ""
                    )
                    if not isinstance(provider_key, str) or any(
                        marker in normalized_provider_key
                        for marker in (
                            "api_key",
                            "apikey",
                            "access_key",
                            "authorization",
                            "token",
                            "secret",
                            "password",
                            "credential",
                        )
                    ):
                        raise ValueError("credential-like provider option is forbidden")
                    if not isinstance(provider_value, scalar_types):
                        raise ValueError(f"{key} values must be JSON scalars")
                continue
            if key == "max_tokens":
                if value is not None and (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                    or value > 0xFFFFFFFF
                ):
                    raise ValueError("max_tokens must be an unsigned 32-bit integer")
                continue
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{key} must be a string or null")
        allowed_transport_keys = {
            "provider_event_kind",
            "client_version",
            "adapter_version",
        }
        if len(raw.transport_facts) > self._config.max_transport_facts or any(
            key not in allowed_transport_keys for key in raw.transport_facts
        ):
            raise ValueError("unsupported transport fact")
        if any(
            not isinstance(value, scalar_types)
            for value in raw.transport_facts.values()
        ):
            raise ValueError("transport facts must be JSON scalars")
        # Validate every value is canonical-JSON encodable, finite and bounded.
        encoded = json.dumps(
            raw.to_json_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > self._config.max_raw_json_bytes:
            raise ValueError("RawInbound exceeds configured byte limit")

        if raw.explicit_conversation_id is not None and not _is_opaque_id(
            raw.explicit_conversation_id
        ):
            raise ValueError("explicit_conversation_id must be an opaque ID")
        if raw.retry_of_turn_id is not None and not _is_opaque_id(raw.retry_of_turn_id):
            raise ValueError("retry_of_turn_id must be an opaque ID")

        project_id: str | None = None
        new_conversation = False
        turn_kind = "agent"
        if raw.project_command is not None:
            command = raw.project_command.get("kind")
            if not isinstance(command, str):
                raise ValueError("Project command kind must be a string")
            raw_project = raw.project_command.get("project_id")
            if command == "bind":
                if set(raw.project_command) != {"kind", "project_id"}:
                    raise ValueError("bind fields do not match V1")
                if not _is_opaque_id(raw_project):
                    raise ValueError("bind requires project_id")
                project_id = raw_project
            elif command == "new_conversation":
                if set(raw.project_command) - {"kind", "project_id"}:
                    raise ValueError("new_conversation fields do not match V1")
                if raw_project is not None and not _is_opaque_id(raw_project):
                    raise ValueError("new_conversation project_id must be opaque")
                project_id = raw_project
                new_conversation = True
            else:
                raise ValueError("unsupported Project command")
            turn_kind = "control_command"
        return turn_kind, project_id, new_conversation

    def _source_is_admitted(self, raw: RawInboundV1) -> bool:
        if raw.surface in {"cli", "desktop"}:
            installation_id = raw.reply_target["installation_id"]
            profile_id = raw.reply_target["profile_id"]
            expected_namespace = f"{raw.surface}/v1/{installation_id}/{profile_id}"
            exact_namespace = raw.source_namespace in (
                self._config.trusted_local_source_namespaces.get(
                    raw.surface, frozenset()
                )
            )
            opaque_desktop_installation = (
                raw.surface == "desktop"
                and _is_opaque_id(installation_id)
                and profile_id
                in self._config.trusted_opaque_installation_profiles.get(
                    "desktop", frozenset()
                )
            )
            return raw.source_namespace == expected_namespace and (
                exact_namespace or opaque_desktop_installation
            )
        subject = raw.external_subject
        if subject is None:
            return False
        kind = subject["kind"]
        value = subject["value"]
        adapter = raw.reply_target["adapter"]
        account_namespace = raw.reply_target["account_namespace"]
        expected_namespace = f"channel/{adapter}/v1/{account_namespace}"
        identity_scope = f"channel/{adapter}/{account_namespace}/{kind}"
        return bool(
            raw.source_namespace == expected_namespace
            and value in self._config.owner_identities.get(identity_scope, frozenset())
        )


__all__ = [
    "IngressBackendConfig",
    "IngressNormalizer",
    "semantic_fingerprint_v1",
]
