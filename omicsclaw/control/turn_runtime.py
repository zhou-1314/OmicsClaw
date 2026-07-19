"""Process-local whole-Turn execution ownership for the control plane.

The public Interface deliberately keeps live execution capabilities behind one
deep Module.  Ingress reserves and commits immutable Envelopes; Workers receive
only a fresh execution context after the durable Receipt moves to ``running``;
terminal persistence and Conversation-lease release cannot be reordered by a
Surface caller.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
import threading
from typing import AsyncIterator, Awaitable, Callable, Mapping, TypeVar

from omicsclaw.attachments import InboundAttachmentSource

from .errors import (
    ControlIntegrityError,
    TurnConversationUnavailableError,
)
from .models import (
    DeliveryPlan,
    InboundEnvelopeV1,
    StateChangeResult,
    TurnAcceptanceResult,
    TurnAcceptanceStatus,
    TurnExecutionResult,
    TurnRecord,
    TurnStartupReconciliationResult,
    TurnTerminalOutcome,
    TurnTranscriptRef,
)
from .repository import ControlStateRepository
from .terminal_codes import is_allowed_turn_terminal_code


_TERMINAL_WORKER_STATUSES = frozenset({"succeeded", "failed", "canceled"})
_WORKER_TERMINAL_CODES_BY_STATUS = {
    "failed": frozenset({"worker_failed"}),
    "canceled": frozenset({"canceled", "canceled_by_owner"}),
}
_AdmissionResultT = TypeVar("_AdmissionResultT")


class TurnCancellation:
    """Read-only cooperative cancellation capability for one active Turn."""

    __slots__ = ("_event",)

    def __init__(self, event: threading.Event) -> None:
        self._event = event

    @property
    def requested(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True, slots=True)
class TurnExecutionContext:
    """Fresh process-local Worker facts created only after Turn activation."""

    envelope: InboundEnvelopeV1
    cancellation: TurnCancellation


@dataclass(slots=True)
class _ActiveTurn:
    envelope: InboundEnvelopeV1
    cancel_event: threading.Event
    finishing: bool = False


class _TurnQueueReservation:
    __slots__ = ("_sequencer", "_state", "_turn_id", "conversation_id")

    def __init__(self, sequencer: "TurnSequencer", conversation_id: str) -> None:
        self._sequencer = sequencer
        self.conversation_id = conversation_id
        self._state = "reserved"
        self._turn_id: str | None = None

    def commit(self, envelope: InboundEnvelopeV1) -> None:
        self._sequencer._commit(self, envelope)

    def release(self) -> None:
        self._sequencer._release(self)

    def discard_terminalized(self) -> None:
        """Release a reservation only after durable enqueue compensation won."""

        self._sequencer._discard_terminalized(self)

    def quarantine(self) -> None:
        """Retain capacity and block the Conversation after uncertain ownership."""

        self._sequencer._quarantine_reservation(self)


@dataclass(slots=True)
class _AsyncAdmissionGuard:
    lock: asyncio.Lock
    users: int = 0


class _DeliveryCapacityReservation:
    """One process-local future-Delivery slot held until Control commit."""

    __slots__ = ("_sequencer", "_state", "account")

    def __init__(self, sequencer: "TurnSequencer", account: tuple[str, str]) -> None:
        self._sequencer = sequencer
        self.account = account
        self._state = "reserved"

    def finish(self) -> None:
        """Release the local shadow after either durable commit or rejection."""

        self._sequencer._finish_delivery_reservation(self)


TurnWorker = Callable[[TurnExecutionContext], Awaitable[TurnTerminalOutcome]]
TerminalEventPublisher = Callable[[TurnRecord], Awaitable[None]]
TerminalCandidatePreparer = Callable[
    [InboundEnvelopeV1, TurnTerminalOutcome],
    Awaitable[TurnTranscriptRef | None],
]
TerminalDeliveryPreparer = Callable[
    [InboundEnvelopeV1, TurnTerminalOutcome, TurnTranscriptRef | None],
    Awaitable[DeliveryPlan | None],
]
TerminalTranscriptPromoter = Callable[
    [TurnRecord, TurnTranscriptRef],
    Awaitable[None],
]
WaitingTerminalPreparer = Callable[[InboundEnvelopeV1], TurnTranscriptRef | None]
WaitingDeliveryPreparer = Callable[
    [InboundEnvelopeV1, TurnTranscriptRef | None], DeliveryPlan | None
]
WaitingTerminalFinalizer = Callable[
    [TurnRecord, TurnTranscriptRef | None],
    None,
]
StartupTerminalPreparer = Callable[[TurnRecord], TurnTranscriptRef]
StartupDeliveryPreparer = Callable[[TurnRecord, TurnTranscriptRef], DeliveryPlan | None]
StartupTerminalFinalizer = Callable[[TurnRecord, TurnTranscriptRef], None]
AcceptedTurnPreparer = Callable[[TurnAcceptanceResult], None]
AcceptedTurnFailureCompensator = Callable[[TurnAcceptanceResult], None]
RunnerFailurePublisher = Callable[[str, BaseException], None]


class TurnSequencer:
    """Bounded FIFO plus one non-bypassable active Turn per Conversation.

    Capacity counts reservations, waiting Envelopes and active Turns until the
    durable Receipt is terminal.  This slightly conservative accounting gives
    the Module one exact-release invariant and bounds every keyed live entry.
    """

    def __init__(
        self,
        repository: ControlStateRepository,
        *,
        max_entries_per_conversation: int,
        max_entries_total: int,
    ) -> None:
        for name, value in (
            ("max_entries_per_conversation", max_entries_per_conversation),
            ("max_entries_total", max_entries_total),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        self._repository = repository
        self.max_entries_per_conversation = max_entries_per_conversation
        self.max_entries_total = max_entries_total
        self._admission_lock = threading.RLock()
        self._lock = threading.RLock()
        self._ready = False
        self._entries_total = 0
        self._entries_by_conversation: dict[str, int] = defaultdict(int)
        self._queues: dict[str, deque[InboundEnvelopeV1]] = defaultdict(deque)
        self._active: dict[str, _ActiveTurn] = {}
        self._turn_locations: dict[str, tuple[str, str]] = {}
        self._quarantined_conversations: set[str] = set()
        self._async_admission_guards: dict[str, _AsyncAdmissionGuard] = {}
        self._delivery_reservations_total = 0
        self._delivery_reservations_by_account: dict[tuple[str, str], int] = (
            defaultdict(int)
        )

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready

    def reconcile_startup(
        self,
        *,
        prepare_interrupted_terminal: StartupTerminalPreparer | None = None,
        prepare_interrupted_delivery: StartupDeliveryPreparer | None = None,
        finalize_interrupted_terminal: StartupTerminalFinalizer | None = None,
    ) -> TurnStartupReconciliationResult:
        """Run the explicit no-replay startup barrier before opening ingress."""

        with self._lock:
            if self._ready:
                return TurnStartupReconciliationResult(())
            if self._entries_total or self._queues or self._active:
                raise RuntimeError("TurnSequencer startup requires empty live state")
            nonterminal = self._repository.list_nonterminal_turns()
            transcript_refs = None
            if prepare_interrupted_terminal is not None:
                transcript_refs = {
                    turn.turn_id: prepare_interrupted_terminal(turn)
                    for turn in nonterminal
                }
            delivery_plans = None
            if prepare_interrupted_delivery is not None:
                if transcript_refs is None:
                    raise ControlIntegrityError(
                        "startup Delivery plans require Transcript references"
                    )
                delivery_plans = {
                    turn.turn_id: plan
                    for turn in nonterminal
                    if (
                        plan := prepare_interrupted_delivery(
                            turn,
                            transcript_refs[turn.turn_id],
                        )
                    )
                    is not None
                }
            result = self._repository.reconcile_nonterminal_turns(
                transcript_refs=transcript_refs,
                delivery_plans=delivery_plans,
            )
            if transcript_refs is not None:
                if finalize_interrupted_terminal is None:
                    raise ControlIntegrityError(
                        "startup Transcript refs have no finalization Interface"
                    )
                for turn_id in result.interrupted_turn_ids:
                    finalize_interrupted_terminal(
                        self._repository.get_turn(turn_id),
                        transcript_refs[turn_id],
                    )
            self._ready = True
            return result

    def try_reserve(self, conversation_id: str) -> _TurnQueueReservation | None:
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be non-empty")
        with self._lock:
            self._require_ready()
            self._require_available(conversation_id)
            if self._entries_total >= self.max_entries_total:
                return None
            if (
                self._entries_by_conversation.get(conversation_id, 0)
                >= self.max_entries_per_conversation
            ):
                return None
            self._entries_total += 1
            self._entries_by_conversation[conversation_id] += 1
            return _TurnQueueReservation(self, conversation_id)

    def serialize_admission(
        self,
        operation: Callable[[], _AdmissionResultT],
    ) -> _AdmissionResultT:
        """Run one complete plan-to-Envelope admission in process order.

        The shared Sequencer owns this guard so multiple Normalizer instances
        cannot durably accept in one order and append FIFO entries in another.
        Worker execution uses a separate lock and remains cross-Conversation
        concurrent.
        """

        with self._admission_lock:
            return operation()

    @asynccontextmanager
    async def admission_guard(self, key: str) -> AsyncIterator[None]:
        """Serialize one bounded ingress/address key without blocking its loop.

        Guards live on the shared Sequencer, so multiple Normalizer instances
        in the same Backend cannot download or commit the same ingress key in
        parallel. Unrelated Reply Targets retain cross-Conversation admission
        concurrency while Turn capacity remains globally bounded.
        """

        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("admission guard key must be a bounded string")
        with self._lock:
            entry = self._async_admission_guards.get(key)
            if entry is None:
                entry = _AsyncAdmissionGuard(asyncio.Lock())
                self._async_admission_guards[key] = entry
            entry.users += 1
        await entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._lock:
                entry.users -= 1
                if entry.users < 0:  # pragma: no cover - internal invariant
                    raise ControlIntegrityError("admission guard accounting underflow")
                if entry.users == 0:
                    current = self._async_admission_guards.get(key)
                    if current is not entry or entry.lock.locked():
                        raise ControlIntegrityError("admission guard ownership drift")
                    self._async_admission_guards.pop(key, None)

    def try_reserve_delivery(
        self,
        reply_target: Mapping[str, object],
        *,
        max_total: int,
        max_per_account: int,
    ) -> _DeliveryCapacityReservation | None:
        """Atomically include in-flight admissions in Delivery backpressure."""

        if reply_target.get("kind") != "channel":
            raise ValueError("Delivery reservation requires a Channel Reply Target")
        adapter = str(reply_target.get("adapter", "")).strip()
        account_namespace = str(reply_target.get("account_namespace", "")).strip()
        if not adapter or not account_namespace:
            raise ValueError("Delivery reservation requires adapter account identity")
        account = (adapter, account_namespace)
        with self._lock:
            reserved_total = self._delivery_reservations_total
            reserved_account = self._delivery_reservations_by_account.get(account, 0)
            if not self._repository.has_delivery_capacity(
                reply_target,
                max_total=max_total,
                max_per_account=max_per_account,
                reserved_total=reserved_total,
                reserved_for_account=reserved_account,
            ):
                return None
            self._delivery_reservations_total += 1
            self._delivery_reservations_by_account[account] += 1
            return _DeliveryCapacityReservation(self, account)

    def _finish_delivery_reservation(
        self,
        reservation: _DeliveryCapacityReservation,
    ) -> None:
        with self._lock:
            if reservation._state != "reserved":
                return
            account_count = self._delivery_reservations_by_account.get(
                reservation.account,
                0,
            )
            if self._delivery_reservations_total <= 0 or account_count <= 0:
                raise ControlIntegrityError("Delivery reservation accounting underflow")
            self._delivery_reservations_total -= 1
            if account_count == 1:
                self._delivery_reservations_by_account.pop(reservation.account, None)
            else:
                self._delivery_reservations_by_account[reservation.account] = (
                    account_count - 1
                )
            reservation._state = "finished"

    def _commit(
        self,
        reservation: _TurnQueueReservation,
        envelope: InboundEnvelopeV1,
    ) -> None:
        if envelope.conversation_id != reservation.conversation_id:
            raise ValueError("Envelope Conversation does not match reservation")
        with self._lock:
            if reservation._state != "reserved":
                raise RuntimeError("Turn reservation is already finished")
            self._require_available(reservation.conversation_id)
            if envelope.turn_id in self._turn_locations:
                raise ControlIntegrityError(
                    "Turn already exists in live execution state"
                )
            reservation._state = "committing"
            reservation._turn_id = envelope.turn_id
            self._queues[reservation.conversation_id].append(envelope)
            self._turn_locations[envelope.turn_id] = (
                "waiting",
                reservation.conversation_id,
            )
            reservation._state = "committed"

    def _release(self, reservation: _TurnQueueReservation) -> None:
        with self._lock:
            if reservation._state != "reserved":
                return
            self._decrement(reservation.conversation_id)
            reservation._state = "released"

    def _discard_terminalized(self, reservation: _TurnQueueReservation) -> None:
        """Remove any possibly committed Envelope after durable failure wins."""

        with self._lock:
            if reservation._state == "released":
                return
            if reservation._state == "quarantined":
                raise ControlIntegrityError(
                    "Quarantined reservation cannot release execution capacity"
                )
            turn_id = reservation._turn_id
            if reservation._state in {"committing", "committed"}:
                if turn_id is None:
                    raise ControlIntegrityError(
                        "Committed reservation lost Turn identity"
                    )
                location = self._turn_locations.get(turn_id)
                if location is None:
                    if reservation._state == "committed":
                        raise ControlIntegrityError(
                            "Committed reservation lost its live Turn index"
                        )
                    queue = self._queues.get(reservation.conversation_id)
                    matches = (
                        [
                            index
                            for index, envelope in enumerate(queue)
                            if envelope.turn_id == turn_id
                        ]
                        if queue is not None
                        else []
                    )
                    if len(matches) > 1:
                        raise ControlIntegrityError(
                            "Partially committed Turn has duplicate FIFO entries"
                        )
                    if matches:
                        assert queue is not None
                        del queue[matches[0]]
                        if not queue:
                            self._queues.pop(reservation.conversation_id, None)
                else:
                    state, conversation_id = location
                    if (
                        state != "waiting"
                        or conversation_id != reservation.conversation_id
                    ):
                        raise ControlIntegrityError(
                            "Compensated Turn is no longer a waiting reservation"
                        )
                    queue = self._queues.get(conversation_id)
                    if queue is None:
                        raise ControlIntegrityError("Compensated Turn queue is missing")
                    for index, envelope in enumerate(queue):
                        if envelope.turn_id == turn_id:
                            del queue[index]
                            break
                    else:
                        raise ControlIntegrityError(
                            "Compensated Turn queue entry is missing"
                        )
                    if not queue:
                        self._queues.pop(conversation_id, None)
                    self._turn_locations.pop(turn_id, None)
            elif reservation._state != "reserved":
                raise ControlIntegrityError("Unknown Turn reservation state")
            self._decrement(reservation.conversation_id)
            reservation._state = "released"

    def _quarantine_reservation(self, reservation: _TurnQueueReservation) -> None:
        with self._lock:
            self._quarantined_conversations.add(reservation.conversation_id)
            if reservation._state != "released":
                reservation._state = "quarantined"

    async def execute_next(
        self,
        conversation_id: str,
        worker: TurnWorker,
        *,
        prepare_terminal: TerminalCandidatePreparer | None = None,
        prepare_delivery: TerminalDeliveryPreparer | None = None,
        promote_terminal: TerminalTranscriptPromoter | None = None,
        publish_terminal: TerminalEventPublisher | None = None,
    ) -> TurnExecutionResult:
        active_or_result = self._activate_next(conversation_id)
        if isinstance(active_or_result, TurnExecutionResult):
            return active_or_result
        active = active_or_result
        context = TurnExecutionContext(
            envelope=active.envelope,
            cancellation=TurnCancellation(active.cancel_event),
        )
        task_cancelled = False
        try:
            outcome = await worker(context)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            task_cancelled = current_task is not None and current_task.cancelling() > 0
            outcome = TurnTerminalOutcome("interrupted", "worker_task_interrupted")
        except Exception:
            outcome = TurnTerminalOutcome("failed", "worker_failed")
        except BaseException:
            # A non-Exception failure leaves the durable Receipt at running and
            # this process still owns the active lease. Preserve both facts,
            # quarantine the whole Conversation, and let process-control or
            # custom BaseExceptions propagate without false terminalization.
            self._quarantine(conversation_id)
            raise
        outcome = self._begin_terminalization(active, outcome)
        transcript_ref = None
        if prepare_terminal is not None:
            try:
                transcript_ref = await prepare_terminal(active.envelope, outcome)
            except BaseException:
                self._quarantine(conversation_id)
                raise
        delivery_plan = None
        if prepare_delivery is not None:
            try:
                delivery_plan = await prepare_delivery(
                    active.envelope,
                    outcome,
                    transcript_ref,
                )
            except BaseException:
                self._quarantine(conversation_id)
                raise
        try:
            terminalized = self._repository.terminalize_turn(
                active.envelope.turn_id,
                terminal_status=outcome.terminal_status,
                terminal_code=outcome.terminal_code,
                transcript_ref=transcript_ref,
                delivery_plan=delivery_plan,
            )
        except BaseException:
            self._quarantine(conversation_id)
            raise
        if not terminalized.changed:
            self._quarantine(conversation_id)
            raise ControlIntegrityError(
                "Active Turn could not commit its terminal Receipt"
            )
        event_published = False
        try:
            receipt = self._repository.get_turn(active.envelope.turn_id)
            if transcript_ref is not None:
                if promote_terminal is None:
                    self._quarantine(conversation_id)
                    raise ControlIntegrityError(
                        "Terminal Transcript reference has no promotion Interface"
                    )
                try:
                    await promote_terminal(receipt, transcript_ref)
                except BaseException:
                    self._quarantine(conversation_id)
                    raise
            if publish_terminal is not None and not task_cancelled:
                try:
                    await publish_terminal(receipt)
                    event_published = True
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    task_cancelled = (
                        current_task is not None and current_task.cancelling() > 0
                    )
                except Exception:
                    pass
        finally:
            # The durable terminal Receipt already won. Even an observer/Event
            # callback failure cannot retain execution authority or replay it.
            self._finish_active(active)
        if task_cancelled:
            raise asyncio.CancelledError
        return TurnExecutionResult(
            state="executed",
            turn_id=active.envelope.turn_id,
            conversation_id=conversation_id,
            terminal_status=receipt.status,
            terminal_code=receipt.terminal_code,
            event_published=event_published,
        )

    async def drain(
        self,
        conversation_id: str,
        worker: TurnWorker,
        *,
        prepare_terminal: TerminalCandidatePreparer | None = None,
        prepare_delivery: TerminalDeliveryPreparer | None = None,
        promote_terminal: TerminalTranscriptPromoter | None = None,
        publish_terminal: TerminalEventPublisher | None = None,
    ) -> tuple[TurnExecutionResult, ...]:
        """Execute available Turns in FIFO order until empty or already active."""

        results: list[TurnExecutionResult] = []
        while True:
            result = await self.execute_next(
                conversation_id,
                worker,
                prepare_terminal=prepare_terminal,
                prepare_delivery=prepare_delivery,
                promote_terminal=promote_terminal,
                publish_terminal=publish_terminal,
            )
            if result.state != "executed":
                return tuple(results)
            results.append(result)

    def cancel(
        self,
        turn_id: str,
        *,
        prepare_waiting_terminal: WaitingTerminalPreparer | None = None,
        prepare_waiting_delivery: WaitingDeliveryPreparer | None = None,
        finalize_waiting_terminal: WaitingTerminalFinalizer | None = None,
    ) -> StateChangeResult:
        """Cancel one waiting Turn or request cooperative active cancellation."""

        with self._lock:
            self._require_ready()
            location = self._turn_locations.get(turn_id)
            if location is None:
                try:
                    receipt = self._repository.get_turn(turn_id)
                except KeyError:
                    return StateChangeResult(False, "turn_not_found")
                if receipt.status in {"succeeded", "failed", "canceled", "interrupted"}:
                    return StateChangeResult(False, "already_terminal")
                return StateChangeResult(False, "execution_not_found")
            state, conversation_id = location
            self._require_available(conversation_id)
            if state == "active":
                active = self._active.get(conversation_id)
                if active is None or active.envelope.turn_id != turn_id:
                    raise ControlIntegrityError("Active Turn index mismatch")
                if active.finishing:
                    return StateChangeResult(False, "completion_in_progress")
                if active.cancel_event.is_set():
                    return StateChangeResult(False, "cancel_already_requested")
                active.cancel_event.set()
                return StateChangeResult(True, "cancel_requested")
            return self._terminalize_waiting_locked(
                turn_id,
                conversation_id,
                terminal_status="canceled",
                terminal_code="canceled_before_start",
                result_code="canceled_waiting",
                prepare_waiting_terminal=prepare_waiting_terminal,
                prepare_waiting_delivery=prepare_waiting_delivery,
                finalize_waiting_terminal=finalize_waiting_terminal,
            )

    def fail_waiting(
        self,
        turn_id: str,
        *,
        prepare_waiting_terminal: WaitingTerminalPreparer | None = None,
        prepare_waiting_delivery: WaitingDeliveryPreparer | None = None,
        finalize_waiting_terminal: WaitingTerminalFinalizer | None = None,
    ) -> StateChangeResult:
        """Fail one accepted Turn whose live execution ports could not be installed."""

        with self._lock:
            self._require_ready()
            location = self._turn_locations.get(turn_id)
            if location is None:
                raise ControlIntegrityError(
                    "Accepted Turn preparation failure lost its waiting execution entry"
                )
            state, conversation_id = location
            self._require_available(conversation_id)
            if state != "waiting":
                raise ControlIntegrityError(
                    "Accepted Turn preparation failure is no longer waiting"
                )
            return self._terminalize_waiting_locked(
                turn_id,
                conversation_id,
                terminal_status="failed",
                terminal_code="dispatch_enqueue_failed",
                result_code="failed_waiting",
                prepare_waiting_terminal=prepare_waiting_terminal,
                prepare_waiting_delivery=prepare_waiting_delivery,
                finalize_waiting_terminal=finalize_waiting_terminal,
            )

    def _terminalize_waiting_locked(
        self,
        turn_id: str,
        conversation_id: str,
        *,
        terminal_status: str,
        terminal_code: str,
        result_code: str,
        prepare_waiting_terminal: WaitingTerminalPreparer | None,
        prepare_waiting_delivery: WaitingDeliveryPreparer | None,
        finalize_waiting_terminal: WaitingTerminalFinalizer | None,
    ) -> StateChangeResult:
        """Commit a waiting terminal Receipt before releasing its FIFO capacity."""

        queue = self._queues.get(conversation_id)
        if queue is None:
            raise ControlIntegrityError("Waiting Turn queue is missing")
        envelope = next(
            (item for item in queue if item.turn_id == turn_id),
            None,
        )
        if envelope is None:
            raise ControlIntegrityError("Waiting Turn index mismatch")
        transcript_ref = None
        delivery_plan = None
        try:
            if prepare_waiting_terminal is not None:
                transcript_ref = prepare_waiting_terminal(envelope)
            if prepare_waiting_delivery is not None:
                delivery_plan = prepare_waiting_delivery(
                    envelope,
                    transcript_ref,
                )
            terminalized = self._repository.terminalize_turn(
                turn_id,
                terminal_status=terminal_status,
                terminal_code=terminal_code,
                transcript_ref=transcript_ref,
                delivery_plan=delivery_plan,
            )
            if not terminalized.changed:
                raise ControlIntegrityError(
                    "Waiting Turn could not commit its terminal Receipt"
                )
            receipt = self._repository.get_turn(turn_id)
            if finalize_waiting_terminal is not None:
                finalize_waiting_terminal(receipt, transcript_ref)
            elif transcript_ref is not None:
                raise ControlIntegrityError(
                    "Waiting terminal Transcript reference has no finalizer"
                )
        except BaseException:
            self._quarantined_conversations.add(conversation_id)
            raise
        for index, queued in enumerate(queue):
            if queued.turn_id == turn_id:
                del queue[index]
                break
        else:  # pragma: no cover - guarded before durable terminalization
            self._quarantined_conversations.add(conversation_id)
            raise ControlIntegrityError("Waiting Turn index mismatch")
        if not queue:
            self._queues.pop(conversation_id, None)
        self._turn_locations.pop(turn_id, None)
        self._decrement(conversation_id)
        return StateChangeResult(True, result_code)

    def _activate_next(self, conversation_id: str) -> _ActiveTurn | TurnExecutionResult:
        with self._lock:
            self._require_ready()
            self._require_available(conversation_id)
            if conversation_id in self._active:
                return TurnExecutionResult(
                    state="conversation_active",
                    conversation_id=conversation_id,
                )
            queue = self._queues.get(conversation_id)
            if not queue:
                return TurnExecutionResult(
                    state="empty", conversation_id=conversation_id
                )
            envelope = queue[0]
            started = self._repository.start_turn(envelope.turn_id)
            if not started.changed:
                self._quarantine(conversation_id)
                raise ControlIntegrityError(
                    f"Turn activation failed with {started.code}; Conversation quarantined"
                )
            try:
                active = _ActiveTurn(
                    envelope=envelope,
                    cancel_event=threading.Event(),
                )
                self._active[conversation_id] = active
                self._turn_locations[envelope.turn_id] = (
                    "active",
                    conversation_id,
                )
                queue.popleft()
                if not queue:
                    self._queues.pop(conversation_id, None)
                return active
            except BaseException:
                # The durable Receipt already says running. Any uncertainty in
                # the process-local ownership transition must retain capacity
                # and block successors until restart reconciliation.
                self._quarantined_conversations.add(conversation_id)
                raise

    def _normalize_outcome(
        self,
        outcome: object,
        cancel_event: threading.Event,
    ) -> TurnTerminalOutcome:
        if cancel_event.is_set():
            return TurnTerminalOutcome("canceled", "canceled_by_owner")
        if not isinstance(outcome, TurnTerminalOutcome):
            return TurnTerminalOutcome("failed", "invalid_worker_outcome")
        status = outcome.terminal_status
        if not isinstance(status, str):
            return TurnTerminalOutcome("failed", "invalid_worker_outcome")
        # Canonical built-in strings keep hostile/unhashable ``str`` subclasses
        # away from the finite-set and allowlist membership checks below.
        status = str.__str__(status)
        code = outcome.terminal_code
        if status == "interrupted":
            if isinstance(code, str) and str.__str__(code) == "worker_task_interrupted":
                return TurnTerminalOutcome(status, str.__str__(code))
            return TurnTerminalOutcome("failed", "invalid_worker_outcome")
        if status not in _TERMINAL_WORKER_STATUSES:
            return TurnTerminalOutcome("failed", "invalid_worker_outcome")
        if status == "succeeded":
            return TurnTerminalOutcome("succeeded")
        fallback_code = "worker_failed" if status == "failed" else "canceled"
        if not isinstance(code, str):
            return TurnTerminalOutcome(status, fallback_code)
        code = str.__str__(code)
        if (
            not is_allowed_turn_terminal_code(status, code)
            or code not in _WORKER_TERMINAL_CODES_BY_STATUS[status]
        ):
            code = fallback_code
        return TurnTerminalOutcome(status, code)

    def _begin_terminalization(
        self,
        active: _ActiveTurn,
        outcome: object,
    ) -> TurnTerminalOutcome:
        """Choose the terminal winner atomically against explicit cancellation."""

        conversation_id = active.envelope.conversation_id
        with self._lock:
            current = self._active.get(conversation_id)
            if current is not active or active.finishing:
                raise ControlIntegrityError("Turn terminalization ownership mismatch")
            normalized = self._normalize_outcome(outcome, active.cancel_event)
            active.finishing = True
            return normalized

    def _finish_active(self, active: _ActiveTurn) -> None:
        conversation_id = active.envelope.conversation_id
        with self._lock:
            current = self._active.get(conversation_id)
            if current is not active:
                raise ControlIntegrityError("Turn execution lease ownership mismatch")
            self._active.pop(conversation_id, None)
            self._turn_locations.pop(active.envelope.turn_id, None)
            self._decrement(conversation_id)

    def _quarantine(self, conversation_id: str) -> None:
        with self._lock:
            self._quarantined_conversations.add(conversation_id)

    def _require_ready(self) -> None:
        if not self._ready:
            raise RuntimeError("TurnSequencer startup reconciliation is incomplete")

    def _require_available(self, conversation_id: str) -> None:
        if conversation_id in self._quarantined_conversations:
            raise TurnConversationUnavailableError(
                f"Conversation {conversation_id} is quarantined"
            )

    def _decrement(self, conversation_id: str) -> None:
        count = self._entries_by_conversation.get(conversation_id, 0)
        if count <= 0 or self._entries_total <= 0:
            raise ControlIntegrityError("TurnSequencer capacity accounting underflow")
        self._entries_total -= 1
        if count == 1:
            self._entries_by_conversation.pop(conversation_id, None)
        else:
            self._entries_by_conversation[conversation_id] = count - 1


class TurnExecutionCoordinator:
    """Async submit-to-Worker adapter with one lost-wakeup-safe drain per Conversation."""

    def __init__(
        self,
        normalizer,
        sequencer: TurnSequencer,
        worker: TurnWorker,
        *,
        prepare_terminal: TerminalCandidatePreparer | None = None,
        prepare_delivery: TerminalDeliveryPreparer | None = None,
        promote_terminal: TerminalTranscriptPromoter | None = None,
        publish_terminal: TerminalEventPublisher | None = None,
        publish_runner_failure: RunnerFailurePublisher | None = None,
    ) -> None:
        self._normalizer = normalizer
        self._sequencer = sequencer
        self._worker = worker
        self._prepare_terminal = prepare_terminal
        self._prepare_delivery = prepare_delivery
        self._promote_terminal = promote_terminal
        self._publish_terminal = publish_terminal
        self._publish_runner_failure = publish_runner_failure
        self._started = False
        self._accepting = False
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._wake_events: dict[str, asyncio.Event] = {}
        self._runner_failures: deque[BaseException] = deque()

    async def start(
        self,
        *,
        prepare_interrupted_terminal: StartupTerminalPreparer | None = None,
        prepare_interrupted_delivery: StartupDeliveryPreparer | None = None,
        finalize_interrupted_terminal: StartupTerminalFinalizer | None = None,
    ) -> TurnStartupReconciliationResult:
        if self._started:
            return TurnStartupReconciliationResult(())
        result = self._sequencer.reconcile_startup(
            prepare_interrupted_terminal=prepare_interrupted_terminal,
            prepare_interrupted_delivery=prepare_interrupted_delivery,
            finalize_interrupted_terminal=finalize_interrupted_terminal,
        )
        self._started = True
        self._accepting = True
        return result

    async def submit(
        self,
        raw,
        *,
        attachment_source: InboundAttachmentSource | None = None,
        prepare_accepted: AcceptedTurnPreparer | None = None,
        compensate_accepted_failure: AcceptedTurnFailureCompensator | None = None,
    ) -> TurnAcceptanceResult:
        """Accept one Turn, prepare live execution state, then wake its runner.

        ``prepare_accepted`` is a process-local composition hook, not a Surface
        callback.  It runs synchronously after durable acceptance and queue
        commit but before the Conversation runner can activate the Turn.
        """

        if not self._accepting:
            return TurnAcceptanceResult(
                TurnAcceptanceStatus.REJECTED,
                code="control_not_ready",
            )
        result = await self._normalizer.accept_async(
            raw,
            attachment_source=attachment_source,
        )
        if result.status is TurnAcceptanceStatus.ACCEPTED and result.code not in {
            "attachment_finalize_failed",
            "dispatch_enqueue_failed",
        }:
            try:
                if prepare_accepted is not None:
                    prepare_accepted(result)
                self._wake(result.conversation_id)
            except Exception:
                if compensate_accepted_failure is None:
                    raise
                compensate_accepted_failure(result)
                result = TurnAcceptanceResult(
                    TurnAcceptanceStatus.ACCEPTED,
                    turn_id=result.turn_id,
                    conversation_id=result.conversation_id,
                    code="dispatch_enqueue_failed",
                )
        return result

    def cancel(
        self,
        turn_id: str,
        *,
        prepare_waiting_terminal: WaitingTerminalPreparer | None = None,
        prepare_waiting_delivery: WaitingDeliveryPreparer | None = None,
        finalize_waiting_terminal: WaitingTerminalFinalizer | None = None,
    ) -> StateChangeResult:
        return self._sequencer.cancel(
            turn_id,
            prepare_waiting_terminal=prepare_waiting_terminal,
            prepare_waiting_delivery=prepare_waiting_delivery,
            finalize_waiting_terminal=finalize_waiting_terminal,
        )

    def fail_waiting(
        self,
        turn_id: str,
        *,
        prepare_waiting_terminal: WaitingTerminalPreparer | None = None,
        prepare_waiting_delivery: WaitingDeliveryPreparer | None = None,
        finalize_waiting_terminal: WaitingTerminalFinalizer | None = None,
    ) -> StateChangeResult:
        """Compensate an accepted Turn before any runner can activate it."""

        return self._sequencer.fail_waiting(
            turn_id,
            prepare_waiting_terminal=prepare_waiting_terminal,
            prepare_waiting_delivery=prepare_waiting_delivery,
            finalize_waiting_terminal=finalize_waiting_terminal,
        )

    async def wait_idle(self) -> None:
        while self._runners:
            tasks = tuple(self._runners.values())
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )
        if self._runner_failures:
            raise self._runner_failures.popleft()

    async def close(self) -> None:
        self._accepting = False
        await self.wait_idle()

    def _wake(self, conversation_id: str) -> None:
        event = self._wake_events.setdefault(conversation_id, asyncio.Event())
        event.set()
        runner = self._runners.get(conversation_id)
        if runner is None or runner.done():
            runner = asyncio.create_task(self._run_conversation(conversation_id))
            self._runners[conversation_id] = runner
            runner.add_done_callback(
                lambda completed, key=conversation_id: self._runner_done(key, completed)
            )

    async def _run_conversation(self, conversation_id: str) -> None:
        event = self._wake_events[conversation_id]
        try:
            while True:
                event.clear()
                await self._sequencer.drain(
                    conversation_id,
                    self._worker,
                    prepare_terminal=self._prepare_terminal,
                    prepare_delivery=self._prepare_delivery,
                    promote_terminal=self._promote_terminal,
                    publish_terminal=self._publish_terminal,
                )
                if event.is_set():
                    continue
                return
        except Exception as error:
            # Coordinator-created tasks are an owned implementation detail. Keep
            # their failure observable through wait_idle()/close() without
            # leaking an un-retrieved background Task exception.
            self._record_runner_failure(conversation_id, error)
        except BaseException:
            # Never consume process-control or custom BaseExceptions here. The
            # Task done callback records a custom failure for wait_idle()/close()
            # while preserving its original identity and traceback.
            raise

    def _runner_done(
        self,
        conversation_id: str,
        completed: asyncio.Task[None],
    ) -> None:
        current = self._runners.get(conversation_id)
        if current is completed:
            self._runners.pop(conversation_id, None)
            self._wake_events.pop(conversation_id, None)
        if completed.cancelled():
            self._record_runner_failure(
                conversation_id,
                RuntimeError(
                    f"Turn runner for Conversation {conversation_id} was canceled"
                ),
            )
            return
        failure = completed.exception()
        if failure is not None:
            self._record_runner_failure(conversation_id, failure)

    def _record_runner_failure(
        self,
        conversation_id: str,
        failure: BaseException,
    ) -> None:
        self._runner_failures.append(failure)
        if self._publish_runner_failure is not None:
            try:
                self._publish_runner_failure(conversation_id, failure)
            except Exception:
                pass


__all__ = [
    "AcceptedTurnFailureCompensator",
    "AcceptedTurnPreparer",
    "RunnerFailurePublisher",
    "StartupTerminalFinalizer",
    "StartupDeliveryPreparer",
    "StartupTerminalPreparer",
    "TerminalCandidatePreparer",
    "TerminalDeliveryPreparer",
    "TerminalEventPublisher",
    "TerminalTranscriptPromoter",
    "TurnCancellation",
    "TurnExecutionContext",
    "TurnExecutionCoordinator",
    "TurnSequencer",
    "TurnWorker",
    "WaitingTerminalFinalizer",
    "WaitingDeliveryPreparer",
    "WaitingTerminalPreparer",
]
