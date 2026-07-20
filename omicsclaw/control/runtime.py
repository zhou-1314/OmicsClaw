"""Production composition root for the local conversational control plane.

``ControlRuntime`` is the small Interface used by Surfaces.  It owns durable
admission, FIFO execution, terminal persistence, and the temporary adapter to
the legacy ``dispatch(MessageEnvelope)`` agent seam.  Process-local callbacks
and cancellation capabilities live only in ``ControlRuntimePorts``; they are
never serialized into ``control.db`` or an accepted ``InboundEnvelopeV1``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import inspect
import os
from pathlib import Path
import threading
from typing import Any

from omicsclaw.attachments import (
    AttachmentIntegrityError,
    AttachmentReferenceV1,
    AttachmentStore,
    InboundAttachmentSource,
)
from omicsclaw.attachments.rendering import AttachmentContentAdapter
from omicsclaw.runtime.agent.envelope import MessageEnvelope
from omicsclaw.runtime.agent.events import Error, Event, Final
from omicsclaw.runtime.storage.canonical_transcript import (
    CanonicalTranscript,
    TranscriptEntryRef,
    TranscriptIntegrityError,
    TurnTranscriptAdapter,
)

from .errors import (
    ControlIntegrityError,
    DeliveryCapacityExceededError,
    DeliveryResendNotSettledError,
)
from .delivery import DeliveryAdapter, DeliveryPump
from .delivery_content import (
    freeze_terminal_text_delivery,
    resolve_delivery_text,
)
from .event_hub import (
    EventHistoryGap,
    EventHubCapacityError,
    EventHubLoopAffinityError,
    EventObserverDetached,
    TurnEventGap,
    TurnEventHub,
    TurnEventObservation,
)
from .event_hub import TurnEventFrame
from .ingress import IngressBackendConfig, IngressNormalizer
from .models import (
    DeliveryOperationOutcome,
    DeliveryPlan,
    DeliveryAttemptRecord,
    DeliveryRecord,
    DeliveryStatusSummary,
    InboundEnvelopeV1,
    RawInboundV1,
    StateChangeResult,
    TurnAcceptanceResult,
    TurnAcceptanceStatus,
    TurnObservationRecord,
    TurnRecord,
    TurnStartupReconciliationResult,
    TurnTerminalOutcome,
    TurnTranscriptRef,
)
from .repository import ControlStateRepository
from .terminal_codes import TurnTerminalCode
from .turn_runtime import (
    TurnExecutionContext,
    TurnExecutionCoordinator,
    TurnSequencer,
)


ResponseSink = Callable[[Event], object]
ContentFactory = Callable[[InboundEnvelopeV1], str | list | Awaitable[str | list]]
McpServersFactory = Callable[[], tuple[Any, ...] | Awaitable[tuple[Any, ...]]]
DispatchEvents = Callable[[MessageEnvelope], AsyncIterator[Event]]
_MAX_CHANNEL_RETRY_MS = 7 * 24 * 60 * 60 * 1_000


class _ObserverOutcome(Enum):
    TERMINAL = "terminal"
    HISTORY_GAP = "history_gap"
    HUB_DETACHED = "hub_detached"
    RENDERER_UNAVAILABLE = "renderer_unavailable"


@dataclass(frozen=True, slots=True)
class ControlRuntimePorts:
    """Fresh live capabilities and trusted legacy Agent inputs for one Turn."""

    response_sink: ResponseSink | None = None
    content_factory: ContentFactory | None = None
    user_id: str | None = None
    workspace: str = ""
    pipeline_workspace: str = ""
    scoped_memory_scope: str = ""
    mcp_servers: tuple[str, ...] | None = None
    mcp_servers_factory: McpServersFactory | None = None
    output_style: str = ""
    plan_context: str = ""
    model_override: str = ""
    extra_api_params: dict[str, Any] | None = None
    max_tokens_override: int = 0
    system_prompt_append: str = ""
    mode: str = ""
    thread_id: str = ""
    stage: str = ""
    usage_accumulator: Any = None
    request_tool_approval: Any = None
    policy_state: Any = None


@dataclass(frozen=True, slots=True)
class ChannelSurfaceBinding:
    """One Channel Adapter's contribution to the shared Channel control plane.

    A Channel describes itself with this and never composes its own runtime:
    `control.db` takes an exclusive lifetime lock, so a second per-Channel
    runtime in the same process would fail deep inside startup.
    """

    adapter: str
    account_namespace: str
    owner_identities: Mapping[str, frozenset[str]]
    delivery_adapter: DeliveryAdapter
    attachment_input_enabled: bool = False

    def __post_init__(self) -> None:
        adapter = self.adapter.strip() if isinstance(self.adapter, str) else ""
        account = (
            self.account_namespace.strip()
            if isinstance(self.account_namespace, str)
            else ""
        )
        if not adapter or not account:
            raise ValueError("Channel adapter and account_namespace must be non-empty")
        object.__setattr__(self, "adapter", adapter)
        object.__setattr__(self, "account_namespace", account)
        expected_scope_prefix = f"channel/{adapter}/{account}/"
        if not self.owner_identities or not any(
            scope.startswith(expected_scope_prefix) for scope in self.owner_identities
        ):
            raise ValueError(
                "Channel runtime requires an Owner Identity scope for its account"
            )
        if not callable(self.delivery_adapter):
            raise TypeError("delivery_adapter must be callable")


@dataclass(frozen=True, slots=True)
class ControlRuntimeResult:
    """Admission observation plus the durable Receipt, when one exists."""

    acceptance: TurnAcceptanceResult
    receipt: TurnRecord | None

    @property
    def attachment_refs(self) -> tuple[AttachmentReferenceV1, ...]:
        """This Turn's ordered accepted Attachment References.

        Delegates to the acceptance outcome so novel and duplicate submissions
        read one Normalizer-owned answer instead of a Surface-local re-query.
        """

        return self.acceptance.attachment_refs


@dataclass(frozen=True, slots=True)
class TurnObservationSnapshot:
    """Verified durable truth plus non-authoritative live interaction state."""

    receipt: TurnRecord
    project_id: str | None
    transcript_ref: TurnTranscriptRef | None
    interaction_snapshot: Mapping[str, Any] | None = None


@dataclass(slots=True)
class ControlTurnObservation:
    """One read-only Turn snapshot plus an atomically opened Event stream."""

    snapshot: TurnObservationSnapshot
    event_stream: TurnEventObservation | None
    unavailable_reason: str | None = None

    @property
    def gap(self) -> TurnEventGap | None:
        return self.event_stream.gap if self.event_stream is not None else None

    def __aiter__(self) -> "ControlTurnObservation":
        return self

    async def __anext__(self) -> TurnEventFrame:
        if self.event_stream is None:
            raise StopAsyncIteration
        return await anext(self.event_stream)

    async def aclose(self) -> None:
        if self.event_stream is not None:
            await self.event_stream.aclose()


@dataclass(slots=True)
class _LiveTurn:
    conversation_id: str
    ports: ControlRuntimePorts
    transcript_turn: TurnTranscriptAdapter
    completion: asyncio.Future[TurnRecord]
    observer_task: asyncio.Task[_ObserverOutcome] | None = None
    pending_terminal_event: Event | None = None
    event_stream_available: bool = True


def default_control_state_root() -> Path:
    """Resolve the private local state directory without opening it."""

    configured = os.environ.get("OMICSCLAW_CONTROL_STATE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    xdg_state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / "omicsclaw"
    return Path.home() / ".local" / "state" / "omicsclaw"


# The control-plane Surface vocabulary ("cli" | "desktop" | "channel") is
# distinct from the legacy Session/Memory platform Literal
# ("telegram" | "feishu" | "cli" | "tui" | "app") — "desktop" is not a valid
# platform there, "app" is (see `desktop_namespace()` in `omicsclaw/memory`).
_SURFACE_TO_LEGACY_SESSION_PLATFORM = {"cli": "cli", "desktop": "app"}


def _legacy_session_platform(envelope: InboundEnvelopeV1) -> str:
    """Translate an Inbound Envelope's Surface into the legacy Session
    platform Literal, so passing it to ``SessionManager``/``Session`` never
    raises a validation error the caller only logs as non-fatal.
    """
    if envelope.surface == "channel":
        adapter = str(envelope.reply_target.get("adapter", ""))
        if adapter in {"telegram", "feishu"}:
            return adapter
        return "app"
    return _SURFACE_TO_LEGACY_SESSION_PLATFORM.get(envelope.surface, envelope.surface)


async def _default_dispatch_events(
    envelope: MessageEnvelope,
) -> AsyncIterator[Event]:
    # Keep the legacy agent dependency lazy so importing ``omicsclaw.control``
    # remains lightweight and isolated control tests can inject an Adapter.
    from omicsclaw.runtime.agent.dispatcher import dispatch

    async for event in dispatch(envelope):
        yield event


class ControlRuntime:
    """Own one process-local control plane from ingress through terminal Receipt."""

    def __init__(
        self,
        *,
        repository: ControlStateRepository,
        normalizer: IngressNormalizer,
        sequencer: TurnSequencer,
        transcript: CanonicalTranscript,
        attachment_store: AttachmentStore,
        workspace_id: str,
        dispatch_events: DispatchEvents = _default_dispatch_events,
        event_hub: TurnEventHub | None = None,
        delivery_pump: DeliveryPump | None = None,
        max_outstanding_deliveries_total: int | None = None,
        max_outstanding_deliveries_per_account: int | None = None,
    ) -> None:
        self.repository = repository
        self.transcript = transcript
        self.attachment_store = attachment_store
        self.workspace_id = workspace_id
        self._normalizer = normalizer
        self._sequencer = sequencer
        self._dispatch_events = dispatch_events
        self._event_hub = event_hub or TurnEventHub()
        self._delivery_pump = delivery_pump
        # Explicit operator actions require the complete Channel Delivery
        # authority: a Pump plus both outstanding-capacity bounds. A local
        # CLI/Desktop runtime intentionally has none of those capabilities.
        self._delivery_max_total = max_outstanding_deliveries_total
        self._delivery_max_per_account = max_outstanding_deliveries_per_account
        self._live_turns: dict[str, _LiveTurn] = {}
        self._observer_tasks: set[asyncio.Task[_ObserverOutcome]] = set()
        self._observer_drain_timeout_seconds = 1.0
        self._started = False
        self._closed = False
        self._coordinator = TurnExecutionCoordinator(
            normalizer,
            sequencer,
            self._run_agent_worker,
            prepare_terminal=self._prepare_terminal_candidate,
            prepare_delivery=self._prepare_terminal_delivery,
            promote_terminal=self._promote_terminal_candidate,
            publish_runner_failure=self._publish_runner_failure,
        )

    @classmethod
    def for_local_surface(
        cls,
        *,
        workspace_id: str,
        surface: str,
        installation_id: str,
        profile_id: str,
        state_root: str | Path | None = None,
        dispatch_events: DispatchEvents = _default_dispatch_events,
        max_entries_per_conversation: int = 8,
        max_entries_total: int = 64,
        attachment_input_enabled: bool = False,
    ) -> "ControlRuntime":
        """Compose the production repository, normalizer, sequencer and Worker.

        Local attachment ingress remains fail-closed unless the owning Surface
        composition root opts in.  This keeps CLI input disabled while the
        Desktop multipart Adapter is cut over independently.
        """

        if surface not in {"cli", "desktop"}:
            raise ValueError("local ControlRuntime surface must be cli or desktop")
        if not installation_id.strip() or not profile_id.strip():
            raise ValueError("local installation_id and profile_id must be non-empty")
        source_namespace = (
            f"{surface}/v1/{installation_id.strip()}/{profile_id.strip()}"
        )
        repository = ControlStateRepository(state_root or default_control_state_root())
        transcript: CanonicalTranscript | None = None
        attachment_store: AttachmentStore | None = None
        try:
            transcript_path = repository.state_root / "transcripts.db"
            if repository.has_conversational_state() and not transcript_path.exists():
                raise ControlIntegrityError(
                    "canonical transcripts.db is missing for existing Control state"
                )
            try:
                transcript = CanonicalTranscript(
                    repository.state_root,
                    require_existing=repository.has_conversational_state(),
                )
            except TranscriptIntegrityError as exc:
                raise ControlIntegrityError(
                    f"canonical Transcript Store is not valid: {exc}"
                ) from exc
            repository.bind_transcript_store(transcript.transcript_store_id)
            attachment_binding = repository.get_attachment_store_binding()
            attachment_store = AttachmentStore(
                repository.state_root,
                require_existing=attachment_binding is not None,
            )
            if attachment_binding is None:
                repository.bind_attachment_store(attachment_store.store_id)
            else:
                repository.verify_attachment_store_binding(attachment_store.store_id)
            sequencer = TurnSequencer(
                repository,
                max_entries_per_conversation=max_entries_per_conversation,
                max_entries_total=max_entries_total,
            )
            normalizer = IngressNormalizer(
                repository,
                sequencer,
                IngressBackendConfig(
                    workspace_id=workspace_id,
                    trusted_local_source_namespaces={
                        surface: frozenset({source_namespace})
                    },
                    trusted_opaque_installation_profiles=(
                        {"desktop": frozenset({profile_id.strip()})}
                        if surface == "desktop"
                        else {}
                    ),
                    attachment_input_enabled=attachment_input_enabled,
                ),
                attachment_store=attachment_store,
                enqueue_failure_terminalizer=lambda accepted: (
                    cls._terminalize_enqueue_failure(
                        repository,
                        transcript,
                        accepted,
                    )
                ),
                attachment_failure_terminalizer=lambda accepted: (
                    cls._terminalize_enqueue_failure(
                        repository,
                        transcript,
                        accepted,
                        terminal_code="attachment_finalize_failed",
                    )
                ),
            )
            return cls(
                repository=repository,
                normalizer=normalizer,
                sequencer=sequencer,
                transcript=transcript,
                attachment_store=attachment_store,
                workspace_id=workspace_id,
                dispatch_events=dispatch_events,
            )
        except BaseException:
            if attachment_store is not None:
                attachment_store.close()
            if transcript is not None:
                transcript.close()
            repository.close()
            raise

    @classmethod
    def for_channel_surface(
        cls,
        *,
        workspace_id: str,
        adapter: str,
        account_namespace: str,
        owner_identities: Mapping[str, frozenset[str]],
        delivery_adapter: DeliveryAdapter,
        attachment_input_enabled: bool = False,
        **runtime_options: Any,
    ) -> "ControlRuntime":
        """Compose an authoritative Channel runtime serving exactly one Adapter.

        Thin wrapper over :meth:`for_channel_surfaces` retained because a single
        Adapter is the common case and the older signature is widely used.
        """

        return cls.for_channel_surfaces(
            workspace_id=workspace_id,
            bindings=(
                ChannelSurfaceBinding(
                    adapter=adapter,
                    account_namespace=account_namespace,
                    owner_identities=owner_identities,
                    delivery_adapter=delivery_adapter,
                    attachment_input_enabled=attachment_input_enabled,
                ),
            ),
            **runtime_options,
        )

    @classmethod
    def for_channel_surfaces(
        cls,
        *,
        workspace_id: str,
        bindings: Sequence["ChannelSurfaceBinding"],
        state_root: str | Path | None = None,
        dispatch_events: DispatchEvents = _default_dispatch_events,
        max_entries_per_conversation: int = 8,
        max_entries_total: int = 64,
        max_outstanding_deliveries_total: int = 64,
        max_outstanding_deliveries_per_account: int = 32,
        max_active_delivery_attempts: int = 16,
        max_delivery_retry_hint_ms: int = _MAX_CHANNEL_RETRY_MS,
    ) -> "ControlRuntime":
        """Compose ONE authoritative runtime serving every cut-over Channel.

        `control.db` takes an exclusive lifetime lock, so one Backend process
        owns exactly one control plane. Adapters are therefore composed here
        rather than by each Channel: the Delivery Pump already resolves by
        ``(adapter, account_namespace)``, so several Channels share one
        repository, Sequencer, Transcript Store and Pump without any of them
        being able to claim another's Reply Target sequence.

        Attachment input stays fail-closed per Adapter: enabling it for one
        Channel must not silently open inbound bytes for a Channel whose
        Attachment Store cutover has not landed.
        """

        bindings = tuple(bindings)
        if not bindings:
            raise ValueError("at least one Channel Surface binding is required")
        merged_owner_identities: dict[str, frozenset[str]] = {}
        delivery_adapters: dict[tuple[str, str], DeliveryAdapter] = {}
        attachment_adapters: set[str] = set()
        for binding in bindings:
            if not isinstance(binding, ChannelSurfaceBinding):
                raise TypeError("bindings must contain ChannelSurfaceBinding values")
            account = (binding.adapter, binding.account_namespace)
            if account in delivery_adapters:
                raise ValueError(
                    "duplicate Channel Surface binding for "
                    f"{binding.adapter}/{binding.account_namespace}"
                )
            delivery_adapters[account] = binding.delivery_adapter
            for scope, subjects in binding.owner_identities.items():
                if scope in merged_owner_identities:
                    raise ValueError(f"duplicate Owner Identity scope: {scope}")
                merged_owner_identities[scope] = frozenset(subjects)
            if binding.attachment_input_enabled:
                attachment_adapters.add(binding.adapter)

        repository = ControlStateRepository(state_root or default_control_state_root())
        transcript: CanonicalTranscript | None = None
        attachment_store: AttachmentStore | None = None
        try:
            transcript_path = repository.state_root / "transcripts.db"
            if repository.has_conversational_state() and not transcript_path.exists():
                raise ControlIntegrityError(
                    "canonical transcripts.db is missing for existing Control state"
                )
            try:
                transcript = CanonicalTranscript(
                    repository.state_root,
                    require_existing=repository.has_conversational_state(),
                )
            except TranscriptIntegrityError as exc:
                raise ControlIntegrityError(
                    f"canonical Transcript Store is not valid: {exc}"
                ) from exc
            repository.bind_transcript_store(transcript.transcript_store_id)
            attachment_binding = repository.get_attachment_store_binding()
            attachment_store = AttachmentStore(
                repository.state_root,
                require_existing=attachment_binding is not None,
            )
            if attachment_binding is None:
                repository.bind_attachment_store(attachment_store.store_id)
            else:
                repository.verify_attachment_store_binding(attachment_store.store_id)
            sequencer = TurnSequencer(
                repository,
                max_entries_per_conversation=max_entries_per_conversation,
                max_entries_total=max_entries_total,
            )
            normalizer = IngressNormalizer(
                repository,
                sequencer,
                IngressBackendConfig(
                    workspace_id=workspace_id,
                    owner_identities=merged_owner_identities,
                    channel_delivery_enabled=True,
                    attachment_input_enabled=bool(attachment_adapters),
                    attachment_input_adapters=frozenset(attachment_adapters),
                    max_outstanding_deliveries_total=(max_outstanding_deliveries_total),
                    max_outstanding_deliveries_per_account=(
                        max_outstanding_deliveries_per_account
                    ),
                ),
                attachment_store=attachment_store,
                enqueue_failure_terminalizer=lambda accepted: (
                    cls._terminalize_enqueue_failure(
                        repository,
                        transcript,
                        accepted,
                        channel_delivery=True,
                    )
                ),
                attachment_failure_terminalizer=lambda accepted: (
                    cls._terminalize_enqueue_failure(
                        repository,
                        transcript,
                        accepted,
                        channel_delivery=True,
                        terminal_code="attachment_finalize_failed",
                    )
                ),
            )
            delivery_pump = DeliveryPump(
                repository,
                adapters=delivery_adapters,
                content_resolver=lambda item: resolve_delivery_text(
                    transcript,
                    item,
                ),
                max_active_attempts=max_active_delivery_attempts,
                retry_hint_max_ms=max_delivery_retry_hint_ms,
            )
            return cls(
                repository=repository,
                normalizer=normalizer,
                sequencer=sequencer,
                transcript=transcript,
                attachment_store=attachment_store,
                workspace_id=workspace_id,
                dispatch_events=dispatch_events,
                delivery_pump=delivery_pump,
                max_outstanding_deliveries_total=max_outstanding_deliveries_total,
                max_outstanding_deliveries_per_account=(
                    max_outstanding_deliveries_per_account
                ),
            )
        except BaseException:
            if attachment_store is not None:
                attachment_store.close()
            if transcript is not None:
                transcript.close()
            repository.close()
            raise

    async def start(self) -> TurnStartupReconciliationResult:
        if self._closed:
            raise RuntimeError("ControlRuntime is closed")
        if self._started:
            return TurnStartupReconciliationResult(())
        incomplete_imports = tuple(
            state
            for state in self.repository.list_legacy_import_states()
            if state != "committed"
        )
        if incomplete_imports:
            raise ControlIntegrityError(
                "legacy Transcript cutover is incomplete; runtime remains closed"
            )
        try:
            for identity in self.repository.list_committed_legacy_transcript_cutovers():
                self.transcript.verify_cutover_identity(**identity)
        except TranscriptIntegrityError as exc:
            raise ControlIntegrityError(
                f"legacy Transcript cutover identity verification failed: {exc}"
            ) from exc
        try:
            self.repository.verify_attachment_store_binding(
                self.attachment_store.store_id
            )
            self.attachment_store.reconcile(
                self.repository.list_turn_attachment_commitments()
            )
        except AttachmentIntegrityError as exc:
            raise ControlIntegrityError(
                "Attachment Store reconciliation failed its authority checks"
            ) from exc
        self._reconcile_terminal_candidates()
        self._validate_terminal_transcripts()
        result = await self._coordinator.start(
            prepare_interrupted_terminal=self._prepare_startup_interruption,
            prepare_interrupted_delivery=self._prepare_startup_delivery,
            finalize_interrupted_terminal=self._finalize_startup_interruption,
        )
        if self._delivery_pump is not None:
            await self._delivery_pump.start()
        self._started = True
        return result

    @staticmethod
    def _terminalize_enqueue_failure(
        repository: ControlStateRepository,
        transcript: CanonicalTranscript,
        accepted: TurnAcceptanceResult,
        *,
        channel_delivery: bool = False,
        terminal_code: TurnTerminalCode = "dispatch_enqueue_failed",
    ) -> None:
        """Close an accepted-but-unqueueable Turn through the canonical seam."""

        turn = repository.get_turn(accepted.turn_id)
        adapter = transcript.bind_turn(
            accepted.conversation_id,
            accepted.turn_id,
        )
        candidate = adapter.stage_terminal(
            "Turn failed.",
            terminal_kind="failed",
            model_visible=turn.turn_kind == "agent",
        )
        ControlRuntime._validate_terminal_payload(
            turn_id=turn.turn_id,
            turn_kind=turn.turn_kind,
            status="failed",
            payload=transcript.get_entry(candidate.entry_id).payload,
        )
        ref = TurnTranscriptRef(candidate.entry_id, candidate.content_sha256)
        delivery_plan = (
            freeze_terminal_text_delivery(transcript, ref, "failed")
            if channel_delivery
            else None
        )
        result = repository.terminalize_turn(
            accepted.turn_id,
            terminal_status="failed",
            terminal_code=terminal_code,
            transcript_ref=ref,
            delivery_plan=delivery_plan,
        )
        if not result.changed:
            raise ControlIntegrityError(
                "accepted pre-dispatch failure could not commit its terminal Receipt"
            )
        transcript.promote_terminal(
            candidate.entry_id,
            candidate.content_sha256,
            expected_conversation_id=accepted.conversation_id,
            expected_turn_id=accepted.turn_id,
        )

    async def _accept_and_start(
        self,
        raw: RawInboundV1,
        ports: ControlRuntimePorts,
        *,
        attachment_source: InboundAttachmentSource | None = None,
        on_accepted: Callable[[str], object] | None = None,
    ) -> tuple[TurnAcceptanceResult, _LiveTurn | None]:
        """Commit one submission, install live ports, and wake its runner."""

        if not self._started or self._closed:
            raise RuntimeError("ControlRuntime must be started before submission")

        live: _LiveTurn | None = None

        def prepare_accepted(accepted: TurnAcceptanceResult) -> None:
            nonlocal live
            live = self._register_live_turn(
                accepted.turn_id,
                accepted.conversation_id,
                ports,
            )

        def compensate_accepted_failure(accepted: TurnAcceptanceResult) -> None:
            # Durable acceptance and FIFO commit already won.  Any ordinary
            # failure across live-port installation plus runner wake must become
            # one canonical failed Receipt before submission returns.
            compensated = self._coordinator.fail_waiting(
                accepted.turn_id,
                prepare_waiting_terminal=(
                    self._prepare_waiting_dispatch_failure_candidate
                ),
                prepare_waiting_delivery=(
                    self._prepare_waiting_dispatch_failure_delivery
                ),
                finalize_waiting_terminal=self._finalize_waiting_dispatch_failure,
            )
            if not compensated.changed or compensated.code != "failed_waiting":
                raise ControlIntegrityError(
                    "accepted Turn activation failure was not compensated"
                )

        acceptance = await self._coordinator.submit(
            raw,
            attachment_source=attachment_source,
            prepare_accepted=prepare_accepted,
            compensate_accepted_failure=compensate_accepted_failure,
        )
        if (
            acceptance.status
            in {TurnAcceptanceStatus.ACCEPTED, TurnAcceptanceStatus.DUPLICATE}
            and on_accepted is not None
        ):
            observed = on_accepted(acceptance.turn_id)
            if inspect.isawaitable(observed):
                await observed
        return acceptance, live

    async def submit(
        self,
        raw: RawInboundV1,
        ports: ControlRuntimePorts,
        *,
        attachment_source: InboundAttachmentSource | None = None,
        on_accepted: Callable[[str], object] | None = None,
    ) -> ControlRuntimeResult:
        """Return after durable Turn acceptance while execution continues.

        This is the submission Interface used by request/receipt protocols such
        as Desktop ``POST /v1/turns``.  It never waits for terminal execution.
        Duplicate callers receive the current durable Receipt without attaching
        a second execution or opening an attachment byte source.
        """

        acceptance, live = await self._accept_and_start(
            raw,
            ports,
            attachment_source=attachment_source,
            on_accepted=on_accepted,
        )
        if acceptance.status is TurnAcceptanceStatus.ACCEPTED:
            if acceptance.code in {
                "attachment_finalize_failed",
                "dispatch_enqueue_failed",
            }:
                self._wake_delivery_pump()
            elif live is None:  # pragma: no cover - Coordinator contract breach
                raise RuntimeError("accepted Turn was not prepared before activation")
            return ControlRuntimeResult(
                acceptance,
                self.repository.get_turn(acceptance.turn_id),
            )
        if acceptance.status is TurnAcceptanceStatus.DUPLICATE:
            return ControlRuntimeResult(
                acceptance,
                self.repository.get_turn(acceptance.turn_id),
            )
        return ControlRuntimeResult(acceptance, None)

    async def submit_and_wait(
        self,
        raw: RawInboundV1,
        ports: ControlRuntimePorts,
        *,
        attachment_source: InboundAttachmentSource | None = None,
        on_accepted: Callable[[str], object] | None = None,
    ) -> ControlRuntimeResult:
        """Accept one raw Turn and wait for its authoritative terminal Receipt."""

        acceptance, live = await self._accept_and_start(
            raw,
            ports,
            attachment_source=attachment_source,
            on_accepted=on_accepted,
        )
        if acceptance.status is TurnAcceptanceStatus.ACCEPTED:
            if acceptance.code in {
                "attachment_finalize_failed",
                "dispatch_enqueue_failed",
            }:
                self._wake_delivery_pump()
                receipt = self.repository.get_turn(acceptance.turn_id)
                if ports.response_sink is not None:
                    await self._publish_retained_or_terminal_snapshot(
                        receipt,
                        ports.response_sink,
                    )
                return ControlRuntimeResult(acceptance, receipt)
            if live is None:  # pragma: no cover - Coordinator contract breach
                raise RuntimeError("accepted Turn was not prepared before activation")
            observer_outcome: _ObserverOutcome | None = None
            try:
                receipt = await asyncio.shield(live.completion)
                if live.observer_task is not None:
                    observer_outcome = await self._await_observer(live.observer_task)
            except BaseException:
                # The caller owns only its observer.  The runner-owned live
                # Turn must remain available for terminalization and retries.
                if live.observer_task is not None and not live.observer_task.done():
                    live.observer_task.cancel()
                if live.observer_task is not None:
                    await asyncio.gather(
                        live.observer_task,
                        return_exceptions=True,
                    )
                raise
            if ports.response_sink is not None and (
                not live.event_stream_available
                or observer_outcome
                in {_ObserverOutcome.HISTORY_GAP, _ObserverOutcome.HUB_DETACHED}
            ):
                await self._publish_terminal_snapshot(receipt, ports.response_sink)
            return ControlRuntimeResult(acceptance, receipt)

        if acceptance.status is TurnAcceptanceStatus.DUPLICATE:
            live = self._live_turns.get(acceptance.turn_id)
            if live is not None:
                observer_task = (
                    self._start_observer(
                        acceptance.turn_id,
                        ports.response_sink,
                    )
                    if ports.response_sink is not None
                    else None
                )
                observer_outcome: _ObserverOutcome | None = None
                try:
                    receipt = await asyncio.shield(live.completion)
                    if observer_task is not None:
                        observer_outcome = await self._await_observer(observer_task)
                except BaseException:
                    if observer_task is not None and not observer_task.done():
                        observer_task.cancel()
                    if observer_task is not None:
                        await asyncio.gather(observer_task, return_exceptions=True)
                    raise
                if ports.response_sink is not None and (
                    not live.event_stream_available
                    or observer_outcome
                    in {
                        _ObserverOutcome.HISTORY_GAP,
                        _ObserverOutcome.HUB_DETACHED,
                    }
                ):
                    await self._publish_terminal_snapshot(
                        receipt,
                        ports.response_sink,
                    )
            else:
                receipt = self.repository.get_turn(acceptance.turn_id)
                if ports.response_sink is not None:
                    await self._publish_retained_or_terminal_snapshot(
                        receipt,
                        ports.response_sink,
                    )
            return ControlRuntimeResult(acceptance, receipt)

        return ControlRuntimeResult(acceptance, None)

    def cancel(self, turn_id: str) -> StateChangeResult:
        """Cancel a waiting Turn or request cooperative active cancellation."""

        result = self._coordinator.cancel(
            turn_id,
            prepare_waiting_terminal=self._prepare_waiting_cancel_candidate,
            prepare_waiting_delivery=self._prepare_waiting_delivery,
            finalize_waiting_terminal=self._finalize_waiting_cancel,
        )
        if result.changed and result.code == "canceled_waiting":
            self._resolve_completion(self.repository.get_turn(turn_id))
        return result

    def get_receipt(self, turn_id: str) -> TurnRecord:
        return self.repository.get_turn(turn_id)

    def get_turn_snapshot(self, turn_id: str) -> TurnObservationSnapshot:
        """Return one verified, read-only observation without execution effects."""

        record: TurnObservationRecord = self.repository.get_turn_observation(turn_id)
        receipt = record.receipt
        terminal = receipt.status in {
            "succeeded",
            "failed",
            "canceled",
            "interrupted",
        }
        if terminal:
            if record.transcript_ref is None:
                raise ControlIntegrityError(
                    "terminal Receipt has no canonical Transcript reference"
                )
            self.transcript.verify_committed_terminal(
                record.transcript_ref.entry_id,
                record.transcript_ref.content_sha256,
                expected_conversation_id=receipt.conversation_id,
                expected_turn_id=receipt.turn_id,
            )
            entry = self.transcript.get_entry(record.transcript_ref.entry_id)
            self._validate_receipt_terminal_kind(receipt, entry.payload)
        elif record.transcript_ref is not None:
            raise ControlIntegrityError(
                "nonterminal Receipt unexpectedly owns a Transcript reference"
            )
        return TurnObservationSnapshot(
            receipt=receipt,
            project_id=record.project_id,
            transcript_ref=record.transcript_ref,
            # Interaction resolution is a separate milestone.  Keeping the
            # explicit nullable field freezes the observation shape without
            # fabricating a durable approval record.
            interaction_snapshot=None,
        )

    def open_turn_observation(
        self,
        turn_id: str,
        *,
        after_sequence: int = -1,
    ) -> ControlTurnObservation:
        """Open snapshot/replay/live observation without submitting a Turn."""

        try:
            stream = self._event_hub.open_observation(
                turn_id,
                after_sequence=after_sequence,
            )
        except KeyError:
            stream = None
        try:
            # Register at the process-local Event seam first. Receipt terminal
            # commit always precedes terminal Event publication, so the
            # subsequent durable read is current at the open point while any
            # later frame is already captured by this observer.
            snapshot = self.get_turn_snapshot(turn_id)
        except BaseException:
            if stream is not None:
                stream.close()
            raise
        unavailable_reason = None
        if stream is None and snapshot.receipt.status in {"queued", "running"}:
            unavailable_reason = "buffer_unavailable"
        return ControlTurnObservation(
            snapshot=snapshot,
            event_stream=stream,
            unavailable_reason=unavailable_reason,
        )

    def lookup_ingress_turn_id(
        self,
        *,
        surface: str,
        source_namespace: str,
        source_request_id: str,
    ) -> str | None:
        """Resolve an existing durable ingress key without submitting a Turn."""

        return self.repository.lookup_ingress_turn_id(
            surface=surface,
            source_namespace=source_namespace,
            source_request_id=source_request_id,
        )

    async def observe_events(
        self,
        turn_id: str,
        *,
        after_sequence: int = -1,
    ) -> AsyncIterator[TurnEventFrame]:
        """Observe/reconnect without submitting or entering the Sequencer."""

        self.repository.get_turn(turn_id)
        async for frame in self._event_hub.subscribe(
            turn_id,
            after_sequence=after_sequence,
        ):
            yield frame

    def _publish_event(
        self,
        turn_id: str,
        event: Event,
        *,
        terminal: bool = False,
    ) -> bool:
        """Publish when observation capacity exists, without owning execution."""

        live = self._live_turns.get(turn_id)
        if live is None or not live.event_stream_available:
            return False
        try:
            self._event_hub.publish(turn_id, event, terminal=terminal)
        except (KeyError, EventHubCapacityError, EventHubLoopAffinityError):
            # Event observation is never execution authority. A bounded or
            # misbound process-local Hub degrades to durable snapshot-only
            # observation rather than changing the Turn outcome.
            live.event_stream_available = False
            return False
        return True

    def _validate_terminal_transcripts(self) -> None:
        """Fail closed on every cross-store terminal authority gap."""

        for receipt in self.repository.list_terminal_turns():
            ref = self.repository.get_turn_terminal_ref(receipt.turn_id)
            if ref is None:
                raise ControlIntegrityError(
                    f"terminal Turn {receipt.turn_id} has no Transcript reference"
                )
            self.transcript.verify_committed_terminal(
                ref.entry_id,
                ref.content_sha256,
                expected_conversation_id=receipt.conversation_id,
                expected_turn_id=receipt.turn_id,
            )
            self._validate_receipt_terminal_kind(
                receipt,
                self.transcript.get_entry(ref.entry_id).payload,
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._coordinator.close()
        finally:
            observer_tasks = tuple(self._observer_tasks)
            for task in observer_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                *observer_tasks,
                return_exceptions=True,
            )
            try:
                if self._delivery_pump is not None:
                    await self._delivery_pump.close()
            finally:
                try:
                    self.attachment_store.close()
                finally:
                    self.transcript.close()
                    self.repository.close()

    async def wait_delivery_idle(self) -> None:
        """Wait until the current due Delivery wave has reached durable state."""

        if not self._started or self._closed:
            raise RuntimeError("ControlRuntime must be running")
        if self._delivery_pump is not None:
            await self._delivery_pump.wait_idle()

    def list_deliveries(
        self, *, turn_id: str | None = None
    ) -> tuple[DeliveryRecord, ...]:
        """Owner/operator read: every Delivery, or those for one Turn."""

        return self.repository.list_deliveries(turn_id=turn_id)

    def describe_delivery(self, delivery_id: str) -> DeliveryStatusSummary | None:
        """Owner/operator read: one Delivery, its Items and rolled-up state."""

        return self.repository.describe_delivery(delivery_id)

    def list_delivery_attempts(
        self, delivery_id: str
    ) -> tuple[DeliveryAttemptRecord, ...]:
        """Owner/operator audit: every provider call made for one Delivery.

        `describe_delivery` answers "where does this Delivery stand"; this
        answers "what was actually attempted, and what did the provider say" --
        the evidence needed to decide between :meth:`retry_delivery` and
        :meth:`resend_delivery` for an `unknown` outcome.
        """

        return self.repository.list_delivery_attempts(delivery_id)

    def resend_delivery(self, delivery_id: str) -> DeliveryOperationOutcome:
        """Explicit Owner resend of an existing Delivery's frozen content.

        Creates a new ``purpose=resend`` Delivery linked to ``delivery_id``,
        reusing its immutable content references without touching the Turn, a
        tool or a Run.  It is the correct recovery after an ``unknown`` or
        already-``delivered`` outcome, where reopening the original Items is
        unsafe.  A Delivery that still has a live Item is refused with
        ``delivery_not_settled``: the Pump may yet deliver it, and copying it now
        would show the Owner the same reply twice.

        The settlement and capacity rules are evaluated inside the insert's own
        transaction rather than here, so two concurrent operator resends cannot
        both observe the last free capacity unit and both insert.
        """

        if not self._delivery_operations_available():
            return DeliveryOperationOutcome("delivery_unavailable")
        try:
            record = self.repository.insert_resend_delivery(
                delivery_id,
                max_total=self._delivery_max_total,
                max_per_account=self._delivery_max_per_account,
            )
        except KeyError:
            return DeliveryOperationOutcome("delivery_not_found")
        except DeliveryResendNotSettledError:
            return DeliveryOperationOutcome("delivery_not_settled")
        except DeliveryCapacityExceededError:
            return DeliveryOperationOutcome("delivery_backpressure")
        self._wake_delivery_pump()
        return DeliveryOperationOutcome("resent", delivery=record)

    def retry_delivery(self, delivery_id: str) -> DeliveryOperationOutcome:
        """Expedite an in-place safe retry of a Delivery's waiting Items.

        Pulls every ``retry_wait`` Item's backoff forward so the Pump reclaims
        it immediately.  It never reopens a terminal ``failed``/``unknown``
        Item — those require an explicit :meth:`resend_delivery` — so a Delivery
        with no waiting Item returns ``no_retryable_items``.
        """

        if not self._delivery_operations_available():
            return DeliveryOperationOutcome("delivery_unavailable")
        try:
            rearmed = self.repository.expedite_delivery_retries(delivery_id)
        except KeyError:
            return DeliveryOperationOutcome("delivery_not_found")
        if rearmed <= 0:
            return DeliveryOperationOutcome("no_retryable_items")
        self._wake_delivery_pump()
        return DeliveryOperationOutcome("retry_rearmed", rearmed_items=rearmed)

    def _delivery_operations_available(self) -> bool:
        return (
            self._delivery_pump is not None
            and self._delivery_max_total is not None
            and self._delivery_max_per_account is not None
        )

    def _register_live_turn(
        self,
        turn_id: str,
        conversation_id: str,
        ports: ControlRuntimePorts,
    ) -> _LiveTurn:
        existing = self._live_turns.get(turn_id)
        if existing is not None:
            return existing
        completion: asyncio.Future[TurnRecord] = (
            asyncio.get_running_loop().create_future()
        )
        completion.add_done_callback(
            lambda finished: (
                finished.exception() if not finished.cancelled() else None
            )
        )
        transcript_turn = self.transcript.bind_turn(conversation_id, turn_id)
        event_stream_available = True
        try:
            self._event_hub.open_turn(turn_id)
        except EventHubCapacityError:
            # Observation capacity is deliberately not execution authority.
            # The durable Turn continues and can still be observed by Receipt
            # plus canonical terminal Transcript after completion.
            event_stream_available = False
        live = _LiveTurn(
            conversation_id=conversation_id,
            ports=ports,
            transcript_turn=transcript_turn,
            completion=completion,
            event_stream_available=event_stream_available,
        )
        self._live_turns[turn_id] = live
        if ports.response_sink is not None and event_stream_available:
            live.observer_task = self._start_observer(
                turn_id,
                ports.response_sink,
            )
        return live

    async def _run_agent_worker(
        self,
        context: TurnExecutionContext,
    ) -> TurnTerminalOutcome:
        # Structured control Turns have already applied their authoritative
        # mutation during admission. They acquire the same Conversation lease
        # for ordering, but never become free-form prompts to the Agent.
        if context.envelope.turn_kind == "control_command":
            return TurnTerminalOutcome("succeeded")

        live = self._live_turns.get(context.envelope.turn_id)
        if live is None:
            return TurnTerminalOutcome("failed", "worker_failed")

        legacy_cancel_event = threading.Event()
        cancellation_mirror = asyncio.create_task(
            self._mirror_cancellation(context, legacy_cancel_event)
        )
        saw_final = False
        try:
            message = await self._build_message_envelope(
                context.envelope,
                live.ports,
                legacy_cancel_event,
            )
            async for event in self._dispatch_events(message):
                if isinstance(event, Error):
                    live.pending_terminal_event = event
                    return TurnTerminalOutcome("failed", "worker_failed")
                if isinstance(event, Final):
                    live.pending_terminal_event = event
                    saw_final = True
                    break
                self._publish_event(context.envelope.turn_id, event)
            if saw_final:
                return TurnTerminalOutcome("succeeded")
            return TurnTerminalOutcome("failed", "worker_failed")
        except asyncio.CancelledError:
            legacy_cancel_event.set()
            raise
        except Exception:
            return TurnTerminalOutcome("failed", "worker_failed")
        finally:
            cancellation_mirror.cancel()
            await asyncio.gather(cancellation_mirror, return_exceptions=True)

    async def _build_message_envelope(
        self,
        envelope: InboundEnvelopeV1,
        ports: ControlRuntimePorts,
        cancel_event: threading.Event,
    ) -> MessageEnvelope:
        content: str | list
        stored_user_content: str | list | None = None
        # Every later Turn must re-render Attachment References already present
        # in durable Conversation history, even when the current Turn is text.
        content_adapter = AttachmentContentAdapter(self.attachment_store)
        if envelope.attachment_refs:
            if ports.content_factory is not None:
                raise ControlIntegrityError(
                    "Attachment Turns cannot replace Backend-owned content rendering"
                )
            references = {
                reference.attachment_id: reference
                for reference in envelope.attachment_refs
            }
            if len(references) != len(envelope.attachment_refs):
                raise ControlIntegrityError(
                    "Inbound Envelope repeats an Attachment Reference"
                )
            durable_blocks: list[dict[str, object]] = []
            observed_attachment_ids: list[str] = []
            for block in envelope.content:
                kind = block.get("kind")
                if kind == "text" and set(block) == {"kind", "text"}:
                    durable_blocks.append({"type": "text", "text": str(block["text"])})
                    continue
                if kind == "attachment" and set(block) == {
                    "kind",
                    "attachment_id",
                }:
                    attachment_id = str(block["attachment_id"])
                    reference = references.get(attachment_id)
                    if reference is None:
                        raise ControlIntegrityError(
                            "Inbound Envelope names an unknown Attachment Reference"
                        )
                    observed_attachment_ids.append(attachment_id)
                    durable_blocks.append(
                        {
                            "type": "attachment_ref",
                            "attachment": reference.to_json_dict(),
                        }
                    )
                    continue
                raise ControlIntegrityError(
                    "Attachment Turn contains an unsupported durable content block"
                )
            if observed_attachment_ids != [
                reference.attachment_id for reference in envelope.attachment_refs
            ]:
                raise ControlIntegrityError(
                    "Inbound Envelope content and Attachment References diverge"
                )
            content = durable_blocks
            stored_user_content = durable_blocks
        elif ports.content_factory is not None:
            prepared = ports.content_factory(envelope)
            if inspect.isawaitable(prepared):
                prepared = await prepared
            # The callback is a temporary local-Surface preparation seam, not
            # an alternate attachment ingress.  Authoritative non-Attachment
            # Turns are text-only; provider blocks, data URIs and reserved
            # markers must fail before dispatch can append them to Transcript.
            if not isinstance(prepared, str):
                raise ControlIntegrityError(
                    "Non-Attachment content_factory output must be text"
                )
            try:
                restored = content_adapter.restore_messages(
                    [{"role": "user", "content": prepared}]
                )
            except AttachmentIntegrityError as exc:
                raise ControlIntegrityError(
                    "Non-Attachment content_factory output is not durable text"
                ) from exc
            if restored != [{"role": "user", "content": prepared}]:
                raise ControlIntegrityError(
                    "Non-Attachment content_factory output changed during validation"
                )
            content = prepared
        else:
            text_blocks = [
                str(block.get("text", ""))
                for block in envelope.content
                if block.get("kind") == "text"
            ]
            content = (
                "\n".join(text_blocks)
                if len(text_blocks) == len(envelope.content)
                else [dict(block) for block in envelope.content]
            )

        options = dict(envelope.requested_options)
        provider_options = options.get("provider_options")
        extra_api_params = dict(ports.extra_api_params or {})
        if isinstance(provider_options, Mapping):
            extra_api_params.update(provider_options)
        mcp_servers = ports.mcp_servers
        if ports.mcp_servers_factory is not None:
            resolved_mcp_servers = ports.mcp_servers_factory()
            if inspect.isawaitable(resolved_mcp_servers):
                resolved_mcp_servers = await resolved_mcp_servers
            mcp_servers = tuple(resolved_mcp_servers)
        return MessageEnvelope(
            chat_id=envelope.conversation_id,
            content=content,
            user_id=ports.user_id,
            platform=_legacy_session_platform(envelope),
            workspace=ports.workspace or envelope.workspace_id,
            pipeline_workspace=ports.pipeline_workspace,
            scoped_memory_scope=ports.scoped_memory_scope,
            mcp_servers=mcp_servers,
            output_style=str(options.get("output_style") or ports.output_style),
            plan_context=ports.plan_context,
            model_override=str(options.get("model_override") or ports.model_override),
            extra_api_params=extra_api_params or None,
            max_tokens_override=int(
                options.get("max_tokens") or ports.max_tokens_override
            ),
            system_prompt_append=str(
                options.get("system_prompt_append") or ports.system_prompt_append
            ),
            mode=str(options.get("mode") or ports.mode),
            thread_id=ports.thread_id,
            stage=ports.stage,
            usage_accumulator=ports.usage_accumulator,
            request_tool_approval=ports.request_tool_approval,
            policy_state=ports.policy_state,
            stored_user_content=stored_user_content,
            content_adapter=content_adapter,
            transcript_turn=self._live_turns[envelope.turn_id].transcript_turn,
            cancel_event=cancel_event,
        )

    async def _mirror_cancellation(
        self,
        context: TurnExecutionContext,
        legacy_cancel_event: threading.Event,
    ) -> None:
        while not legacy_cancel_event.is_set():
            if context.cancellation.requested:
                legacy_cancel_event.set()
                return
            await asyncio.sleep(0.01)

    async def _prepare_terminal_candidate(
        self,
        envelope: InboundEnvelopeV1,
        outcome: TurnTerminalOutcome,
    ) -> TurnTranscriptRef:
        live = self._live_turns.get(envelope.turn_id)
        if live is None:
            raise RuntimeError("terminal candidate preparation lost live Turn")

        pending = live.pending_terminal_event
        pending_ref: TranscriptEntryRef | None = None
        if isinstance(pending, Final) and pending.transcript_entry_id:
            pending_ref = TranscriptEntryRef(
                pending.transcript_entry_id,
                pending.transcript_content_sha256,
            )

        if envelope.turn_kind == "control_command":
            candidate = live.transcript_turn.stage_terminal(
                "",
                terminal_kind="control",
                model_visible=False,
            )
            return TurnTranscriptRef(candidate.entry_id, candidate.content_sha256)

        if outcome.terminal_status == "succeeded":
            if not isinstance(pending, Final):
                raise RuntimeError("successful Turn has no terminal Final Event")
            if pending.kind not in {"normal", "preflight"}:
                raise RuntimeError("successful Agent Final has an invalid Event kind")
            if pending_ref is not None:
                candidate = self.transcript.verify_terminal_candidate(
                    pending_ref.entry_id,
                    pending_ref.content_sha256,
                    expected_conversation_id=envelope.conversation_id,
                    expected_turn_id=envelope.turn_id,
                    expected_public_text=pending.text,
                    expected_terminal_kind=pending.kind,
                )
            else:
                candidate = live.transcript_turn.stage_terminal(
                    pending.text,
                    terminal_kind=pending.kind,
                )
            self._validate_terminal_payload(
                turn_id=envelope.turn_id,
                turn_kind=envelope.turn_kind,
                status=outcome.terminal_status,
                payload=self.transcript.get_entry(candidate.entry_id).payload,
            )
            return TurnTranscriptRef(candidate.entry_id, candidate.content_sha256)

        if pending_ref is not None:
            live.transcript_turn.abandon_terminal(
                pending_ref.entry_id,
                pending_ref.content_sha256,
            )
        else:
            live.transcript_turn.discard_pending_terminal_message()

        public_text = {
            "failed": "Turn failed.",
            "canceled": "Turn canceled.",
            "interrupted": "Turn interrupted.",
        }.get(outcome.terminal_status, "Turn failed.")
        candidate = live.transcript_turn.stage_terminal(
            public_text,
            terminal_kind=outcome.terminal_status,
            model_visible=envelope.turn_kind == "agent",
        )
        self._validate_terminal_payload(
            turn_id=envelope.turn_id,
            turn_kind=envelope.turn_kind,
            status=outcome.terminal_status,
            payload=self.transcript.get_entry(candidate.entry_id).payload,
        )
        if outcome.terminal_status in {"canceled", "interrupted"}:
            live.pending_terminal_event = Final(public_text)
        elif pending is None:
            live.pending_terminal_event = Final(public_text)
        return TurnTranscriptRef(candidate.entry_id, candidate.content_sha256)

    async def _prepare_terminal_delivery(
        self,
        envelope: InboundEnvelopeV1,
        outcome: TurnTerminalOutcome,
        transcript_ref: TurnTranscriptRef | None,
    ) -> DeliveryPlan | None:
        if envelope.surface != "channel":
            return None
        if self._delivery_pump is None or transcript_ref is None:
            raise ControlIntegrityError(
                "Channel terminalization has no Delivery Pump or Transcript ref"
            )
        return freeze_terminal_text_delivery(
            self.transcript,
            transcript_ref,
            outcome.terminal_status,
        )

    def _prepare_waiting_cancel_candidate(
        self,
        envelope: InboundEnvelopeV1,
    ) -> TurnTranscriptRef:
        live = self._live_turns.get(envelope.turn_id)
        if live is None:
            raise RuntimeError("waiting cancellation lost live Turn")
        live.pending_terminal_event = Final("Turn canceled.")
        candidate = live.transcript_turn.stage_terminal(
            "Turn canceled.",
            terminal_kind="canceled",
            model_visible=envelope.turn_kind == "agent",
        )
        self._validate_terminal_payload(
            turn_id=envelope.turn_id,
            turn_kind=envelope.turn_kind,
            status="canceled",
            payload=self.transcript.get_entry(candidate.entry_id).payload,
        )
        return TurnTranscriptRef(candidate.entry_id, candidate.content_sha256)

    def _prepare_waiting_delivery(
        self,
        envelope: InboundEnvelopeV1,
        transcript_ref: TurnTranscriptRef | None,
    ) -> DeliveryPlan | None:
        if envelope.surface != "channel":
            return None
        if self._delivery_pump is None or transcript_ref is None:
            raise ControlIntegrityError(
                "Channel cancellation has no Delivery Pump or Transcript ref"
            )
        return freeze_terminal_text_delivery(
            self.transcript,
            transcript_ref,
            "canceled",
        )

    def _prepare_waiting_dispatch_failure_candidate(
        self,
        envelope: InboundEnvelopeV1,
    ) -> TurnTranscriptRef:
        adapter = self.transcript.bind_turn(
            envelope.conversation_id,
            envelope.turn_id,
        )
        candidate = adapter.stage_terminal(
            "Turn failed.",
            terminal_kind="failed",
            model_visible=envelope.turn_kind == "agent",
        )
        self._validate_terminal_payload(
            turn_id=envelope.turn_id,
            turn_kind=envelope.turn_kind,
            status="failed",
            payload=self.transcript.get_entry(candidate.entry_id).payload,
        )
        return TurnTranscriptRef(candidate.entry_id, candidate.content_sha256)

    def _prepare_waiting_dispatch_failure_delivery(
        self,
        envelope: InboundEnvelopeV1,
        transcript_ref: TurnTranscriptRef | None,
    ) -> DeliveryPlan | None:
        if envelope.surface != "channel":
            return None
        if self._delivery_pump is None or transcript_ref is None:
            raise ControlIntegrityError(
                "Channel dispatch failure has no Delivery Pump or Transcript ref"
            )
        return freeze_terminal_text_delivery(
            self.transcript,
            transcript_ref,
            "failed",
        )

    def _finalize_waiting_dispatch_failure(
        self,
        receipt: TurnRecord,
        transcript_ref: TurnTranscriptRef | None,
    ) -> None:
        if transcript_ref is None:
            raise RuntimeError("waiting dispatch failure has no Transcript reference")
        committed_ref = self.repository.get_turn_terminal_ref(receipt.turn_id)
        if committed_ref != transcript_ref:
            raise RuntimeError(
                "waiting dispatch failure Receipt lost Transcript reference"
            )
        self.transcript.promote_terminal(
            transcript_ref.entry_id,
            transcript_ref.content_sha256,
            expected_conversation_id=receipt.conversation_id,
            expected_turn_id=receipt.turn_id,
        )
        self._wake_delivery_pump()
        partial_live = self._live_turns.pop(receipt.turn_id, None)
        if partial_live is not None:
            if (
                partial_live.observer_task is not None
                and not partial_live.observer_task.done()
            ):
                partial_live.observer_task.cancel()
            if not partial_live.completion.done():
                partial_live.completion.set_result(receipt)
        self._event_hub.abandon_turn(receipt.turn_id)

    def _finalize_waiting_cancel(
        self,
        receipt: TurnRecord,
        transcript_ref: TurnTranscriptRef | None,
    ) -> None:
        if transcript_ref is None:
            raise RuntimeError("waiting cancellation has no Transcript reference")
        committed_ref = self.repository.get_turn_terminal_ref(receipt.turn_id)
        if committed_ref != transcript_ref:
            raise RuntimeError("waiting cancellation Receipt lost Transcript reference")
        self.transcript.promote_terminal(
            transcript_ref.entry_id,
            transcript_ref.content_sha256,
            expected_conversation_id=receipt.conversation_id,
            expected_turn_id=receipt.turn_id,
        )
        self._wake_delivery_pump()
        live = self._live_turns.get(receipt.turn_id)
        if live is None or live.pending_terminal_event is None:
            raise RuntimeError("waiting cancellation lost terminal Event")
        self._publish_event(
            receipt.turn_id,
            live.pending_terminal_event,
            terminal=True,
        )
        self._resolve_completion(receipt)

    async def _promote_terminal_candidate(
        self,
        receipt: TurnRecord,
        transcript_ref: TurnTranscriptRef,
    ) -> None:
        committed_ref = self.repository.get_turn_terminal_ref(receipt.turn_id)
        if committed_ref != transcript_ref:
            raise RuntimeError("terminal Receipt lost its Transcript reference")
        self.transcript.promote_terminal(
            transcript_ref.entry_id,
            transcript_ref.content_sha256,
            expected_conversation_id=receipt.conversation_id,
            expected_turn_id=receipt.turn_id,
        )
        self._wake_delivery_pump()
        await self._publish_terminal(receipt)

    async def _publish_terminal(self, receipt: TurnRecord) -> None:
        live = self._live_turns.get(receipt.turn_id)
        if live is None:
            raise RuntimeError("terminal Event publication lost live Turn")
        event = live.pending_terminal_event
        if event is None:
            text = {
                "succeeded": "",
                "failed": "Turn failed.",
                "canceled": "Turn canceled.",
                "interrupted": "Turn interrupted.",
            }.get(receipt.status, "")
            event = Final(text)
        self._publish_event(receipt.turn_id, event, terminal=True)
        self._resolve_completion(receipt)

    async def _observe_response_sink(
        self,
        turn_id: str,
        response_sink: ResponseSink,
    ) -> _ObserverOutcome:
        try:
            async for frame in self._event_hub.subscribe(turn_id):
                published = response_sink(frame.event)
                if inspect.isawaitable(published):
                    await published
                if frame.terminal:
                    return _ObserverOutcome.TERMINAL
        except EventHistoryGap:
            return _ObserverOutcome.HISTORY_GAP
        except EventObserverDetached:
            return _ObserverOutcome.HUB_DETACHED
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return _ObserverOutcome.RENDERER_UNAVAILABLE
        except Exception:
            # A renderer is an observer Adapter. Detach it; execution and the
            # durable Receipt continue independently through the Event Hub.
            return _ObserverOutcome.RENDERER_UNAVAILABLE
        return _ObserverOutcome.RENDERER_UNAVAILABLE

    def _start_observer(
        self,
        turn_id: str,
        response_sink: ResponseSink,
    ) -> asyncio.Task[_ObserverOutcome]:
        task = asyncio.create_task(self._observe_response_sink(turn_id, response_sink))
        self._observer_tasks.add(task)
        task.add_done_callback(self._observer_tasks.discard)
        return task

    async def _await_observer(
        self,
        task: asyncio.Task[_ObserverOutcome],
    ) -> _ObserverOutcome:
        """Bound renderer drain time so an observer cannot hold submission open."""

        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._observer_drain_timeout_seconds,
            )
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return _ObserverOutcome.RENDERER_UNAVAILABLE
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return _ObserverOutcome.RENDERER_UNAVAILABLE

    async def _publish_terminal_snapshot(
        self,
        receipt: TurnRecord,
        response_sink: ResponseSink,
    ) -> None:
        ref = self.repository.get_turn_terminal_ref(receipt.turn_id)
        if ref is None:
            raise ControlIntegrityError(
                "terminal Receipt has no canonical Transcript reference"
            )
        self.transcript.verify_committed_terminal(
            ref.entry_id,
            ref.content_sha256,
            expected_conversation_id=receipt.conversation_id,
            expected_turn_id=receipt.turn_id,
        )
        entry = self.transcript.get_entry(ref.entry_id)
        self._validate_receipt_terminal_kind(receipt, entry.payload)
        event_kind = (
            "preflight"
            if str(entry.payload.get("terminal_kind", "")) == "preflight"
            else "normal"
        )
        event = Final(
            entry.public_text,
            kind=event_kind,
            transcript_entry_id=ref.entry_id,
            transcript_content_sha256=ref.content_sha256,
        )
        await self._publish_to_response_sink(event, response_sink)

    async def _publish_to_response_sink(
        self,
        event: Event,
        response_sink: ResponseSink,
    ) -> bool:
        try:
            published = response_sink(event)
            if inspect.isawaitable(published):
                await asyncio.wait_for(
                    published,
                    timeout=self._observer_drain_timeout_seconds,
                )
            return True
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return False
        except Exception:
            return False

    async def _publish_retained_or_terminal_snapshot(
        self,
        receipt: TurnRecord,
        response_sink: ResponseSink,
    ) -> None:
        """Replay retained process events, falling back to durable terminal truth."""

        try:
            frames = self._event_hub.retained_frames(receipt.turn_id)
        except KeyError:
            frames = ()
        if frames and frames[0].sequence == 1 and frames[-1].terminal:
            for frame in frames:
                if not await self._publish_to_response_sink(
                    frame.event,
                    response_sink,
                ):
                    return
            return
        await self._publish_terminal_snapshot(receipt, response_sink)

    @staticmethod
    def _validate_receipt_terminal_kind(
        receipt: TurnRecord,
        payload: Mapping[str, object],
    ) -> None:
        ControlRuntime._validate_terminal_payload(
            turn_id=receipt.turn_id,
            turn_kind=receipt.turn_kind,
            status=receipt.status,
            payload=payload,
        )

    @staticmethod
    def _validate_terminal_payload(
        *,
        turn_id: str,
        turn_kind: str,
        status: str,
        payload: Mapping[str, object],
    ) -> None:
        actual = str(payload.get("terminal_kind", ""))
        if status == "succeeded":
            expected = (
                {"control"}
                if turn_kind == "control_command"
                else {"normal", "preflight"}
            )
        else:
            expected = {status}
        if actual not in expected:
            raise ControlIntegrityError(
                f"terminal Turn {turn_id} Receipt/Transcript kind mismatch"
            )
        model_visible = isinstance(payload.get("provider_message"), Mapping)
        if model_visible != (turn_kind == "agent"):
            raise ControlIntegrityError(
                f"terminal Turn {turn_id} Transcript model-visibility mismatch"
            )
        if model_visible:
            provider_message = payload["provider_message"]
            assert isinstance(provider_message, Mapping)
            provider_content = provider_message.get("content")
            public_text = payload.get("public_text")
            normalized_provider_content = (
                provider_content.strip() if isinstance(provider_content, str) else None
            )
            if (
                provider_message.get("role") != "assistant"
                or not isinstance(provider_content, str)
                or not isinstance(public_text, str)
                or (
                    bool(normalized_provider_content)
                    and public_text != normalized_provider_content
                    and not public_text.endswith(f"\n\n{normalized_provider_content}")
                )
            ):
                raise ControlIntegrityError(
                    f"terminal Turn {turn_id} provider/public content mismatch"
                )

    def _reconcile_terminal_candidates(self) -> None:
        terminal_statuses = {"succeeded", "failed", "canceled", "interrupted"}
        for candidate in self.transcript.list_terminal_candidates():
            entry = self.transcript.get_entry(candidate.entry_id)
            if entry.turn_id is None:
                raise RuntimeError("terminal candidate has no Turn attribution")
            try:
                receipt = self.repository.get_turn(entry.turn_id)
            except KeyError:
                self.transcript.abandon_terminal(
                    candidate.entry_id,
                    candidate.content_sha256,
                )
                continue
            committed_ref = self.repository.get_turn_terminal_ref(entry.turn_id)
            if receipt.status in terminal_statuses:
                if committed_ref is None:
                    raise ControlIntegrityError(
                        "terminal Receipt has an unreferenced Transcript candidate"
                    )
                if (
                    committed_ref.entry_id != candidate.entry_id
                    or committed_ref.content_sha256 != candidate.content_sha256
                ):
                    raise RuntimeError(
                        "terminal Receipt and Transcript candidate disagree"
                    )
                self.transcript.promote_terminal(
                    candidate.entry_id,
                    candidate.content_sha256,
                    expected_conversation_id=receipt.conversation_id,
                    expected_turn_id=receipt.turn_id,
                )
            else:
                self.transcript.abandon_terminal(
                    candidate.entry_id,
                    candidate.content_sha256,
                )

    def _prepare_startup_interruption(
        self,
        turn: TurnRecord,
    ) -> TurnTranscriptRef:
        adapter = self.transcript.bind_turn(turn.conversation_id, turn.turn_id)
        candidate = adapter.stage_terminal(
            "Turn interrupted by control-plane restart.",
            terminal_kind="interrupted",
            model_visible=turn.turn_kind == "agent",
        )
        self._validate_terminal_payload(
            turn_id=turn.turn_id,
            turn_kind=turn.turn_kind,
            status="interrupted",
            payload=self.transcript.get_entry(candidate.entry_id).payload,
        )
        return TurnTranscriptRef(candidate.entry_id, candidate.content_sha256)

    def _prepare_startup_delivery(
        self,
        turn: TurnRecord,
        transcript_ref: TurnTranscriptRef,
    ) -> DeliveryPlan | None:
        conversation = self.repository.get_conversation(turn.conversation_id)
        if conversation.surface != "channel":
            return None
        if self._delivery_pump is None:
            raise ControlIntegrityError(
                "nonterminal Channel Turn cannot recover without a Delivery Pump"
            )
        return freeze_terminal_text_delivery(
            self.transcript,
            transcript_ref,
            "interrupted",
        )

    def _finalize_startup_interruption(
        self,
        receipt: TurnRecord,
        transcript_ref: TurnTranscriptRef,
    ) -> None:
        committed_ref = self.repository.get_turn_terminal_ref(receipt.turn_id)
        if committed_ref != transcript_ref:
            raise RuntimeError("startup Receipt lost its Transcript reference")
        self.transcript.promote_terminal(
            transcript_ref.entry_id,
            transcript_ref.content_sha256,
            expected_conversation_id=receipt.conversation_id,
            expected_turn_id=receipt.turn_id,
        )

    def _wake_delivery_pump(self) -> None:
        if self._delivery_pump is not None and self._started and not self._closed:
            self._delivery_pump.wake()

    def _resolve_completion(self, receipt: TurnRecord) -> None:
        live = self._live_turns.get(receipt.turn_id)
        if live is not None and not live.completion.done():
            live.completion.set_result(receipt)
        self._live_turns.pop(receipt.turn_id, None)

    def _publish_runner_failure(
        self,
        conversation_id: str,
        _failure: BaseException,
    ) -> None:
        for turn_id, live in tuple(self._live_turns.items()):
            if live.conversation_id != conversation_id or live.completion.done():
                continue
            live.completion.set_exception(
                RuntimeError("Control Turn runner failed before terminal Receipt")
            )
            if live.observer_task is not None and not live.observer_task.done():
                live.observer_task.cancel()
            self._event_hub.abandon_turn(turn_id)
            self._live_turns.pop(turn_id, None)


__all__ = [
    "ContentFactory",
    "ControlRuntime",
    "ControlRuntimePorts",
    "ControlRuntimeResult",
    "DispatchEvents",
    "ResponseSink",
    "default_control_state_root",
]
