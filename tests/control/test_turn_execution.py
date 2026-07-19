from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path
import subprocess
import sys

import pytest
import omicsclaw.control.turn_runtime as turn_runtime_module

from omicsclaw.control import (
    ControlIntegrityError,
    ControlStateRepository,
    InboundEnvelopeV1,
    IngressBackendConfig,
    IngressNormalizer,
    RawContentBlockV1,
    RawInboundV1,
    TurnAcceptanceIntent,
    TurnAcceptanceStatus,
    TurnConversationUnavailableError,
    TurnExecutionCoordinator,
    TurnSequencer,
    TurnTerminalOutcome,
)


_INSTALLATION_ID = "test-installation"
_PROFILE_ID = "owner"
_ROOT = Path(__file__).resolve().parents[2]


class _CatastrophicWorkerFailure(BaseException):
    """Non-Exception Worker failure used to exercise the ownership boundary."""


class _HostileWorkerString(str):
    """String subclass whose overridable methods must never reach authority."""

    def __hash__(self):
        raise AssertionError("Worker string __hash__ must not be called")

    def __eq__(self, other):
        raise AssertionError("Worker string __eq__ must not be called")

    def __str__(self):
        raise AssertionError("Worker string __str__ must not be called")


def _raw(request_id: str, *, slot: str = "main") -> RawInboundV1:
    return RawInboundV1(
        schema_version=1,
        surface="desktop",
        source_namespace=f"desktop/v1/{_INSTALLATION_ID}/{_PROFILE_ID}",
        source_request_id=request_id,
        reply_target={
            "schema_version": 1,
            "kind": "desktop",
            "installation_id": _INSTALLATION_ID,
            "profile_id": _PROFILE_ID,
            "slot": slot,
        },
        content=(RawContentBlockV1(kind="text", text=request_id),),
    )


def _normalizer(
    repository: ControlStateRepository,
    sequencer: TurnSequencer,
) -> IngressNormalizer:
    return IngressNormalizer(
        repository,
        sequencer,
        IngressBackendConfig(
            workspace_id="workspace-test",
            trusted_local_source_namespaces={
                "desktop": frozenset({f"desktop/v1/{_INSTALLATION_ID}/{_PROFILE_ID}"})
            },
        ),
    )


def _direct_intent(
    request_id: str,
    *,
    surface: str = "desktop",
) -> TurnAcceptanceIntent:
    if surface == "channel":
        reply_target = {
            "schema_version": 1,
            "kind": "channel",
            "adapter": "telegram",
            "account_namespace": "primary",
            "destination_id": "owner-chat",
        }
        namespace = "channel/telegram/v1/primary"
    else:
        reply_target = {
            "schema_version": 1,
            "kind": "desktop",
            "installation_id": _INSTALLATION_ID,
            "profile_id": _PROFILE_ID,
            "slot": "main",
        }
        namespace = f"desktop/v1/{_INSTALLATION_ID}/{_PROFILE_ID}"
    return TurnAcceptanceIntent(
        surface=surface,
        source_namespace=namespace,
        source_request_id=request_id,
        fingerprint_version=1,
        fingerprint_sha256=request_id[0] * 64,
        reply_target=reply_target,
    )


def test_ingress_stays_closed_until_startup_reconciliation_completes(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=4,
        )
        normalizer = _normalizer(repository, sequencer)

        rejected = normalizer.accept(_raw("before-startup"))

        assert rejected.status is TurnAcceptanceStatus.REJECTED
        assert rejected.code == "control_not_ready"
        assert repository.list_conversations() == ()

        reconciled = sequencer.reconcile_startup()
        accepted = normalizer.accept(_raw("after-startup"))

        assert reconciled.interrupted_turn_ids == ()
        assert sequencer.ready is True
        assert accepted.status is TurnAcceptanceStatus.ACCEPTED


def test_reservation_release_is_idempotent_and_commit_is_single_use(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=1,
            max_entries_total=1,
        )
        sequencer.reconcile_startup()
        released = sequencer.try_reserve("released-conversation")
        assert released is not None

        released.release()
        released.release()

        committed = sequencer.try_reserve("committed-conversation")
        assert committed is not None
        envelope = InboundEnvelopeV1(
            schema_version=1,
            turn_id="a" * 32,
            turn_kind="agent",
            conversation_id="committed-conversation",
            surface="desktop",
            project_id=None,
            workspace_id="workspace-test",
            content=({"kind": "text", "text": "hello"},),
            source_attribution={"surface": "desktop"},
            reply_target={
                "schema_version": 1,
                "kind": "desktop",
                "installation_id": _INSTALLATION_ID,
                "profile_id": _PROFILE_ID,
                "slot": "main",
            },
            requested_options={},
            retry_of_turn_id=None,
            accepted_at_ms=1,
        )
        committed.commit(envelope)

        with pytest.raises(RuntimeError, match="already finished"):
            committed.commit(envelope)
        committed.release()
        assert sequencer.try_reserve("must-remain-full") is None


def test_startup_reconciliation_interrupts_local_receipts_without_replay(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        queued = repository.accept_turn(_direct_intent("a-queued"))
        running = repository.accept_turn(_direct_intent("b-running"))
        terminal = repository.accept_turn(_direct_intent("c-terminal"))
        repository.start_turn(running.turn_id)
        repository.start_turn(terminal.turn_id)
        repository.terminalize_turn(terminal.turn_id, terminal_status="succeeded")

    worker_calls: list[str] = []
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=4,
        )
        reconciled = sequencer.reconcile_startup()
        repeated = sequencer.reconcile_startup()
        duplicate = repository.accept_turn(_direct_intent("a-queued"))

        assert reconciled.interrupted_turn_ids == (
            queued.turn_id,
            running.turn_id,
        )
        assert repeated.interrupted_turn_ids == ()
        assert repository.get_turn(queued.turn_id).status == "interrupted"
        assert repository.get_turn(running.turn_id).status == "interrupted"
        assert repository.get_turn(terminal.turn_id).status == "succeeded"
        assert duplicate.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.turn_id == queued.turn_id
        assert worker_calls == []


def test_startup_reconciliation_fails_closed_for_channel_without_delivery_store(
    tmp_path,
):
    with ControlStateRepository(tmp_path) as repository:
        channel = repository.accept_turn(_direct_intent("c-channel", surface="channel"))
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=4,
        )

        with pytest.raises(ControlIntegrityError, match="Channel"):
            sequencer.reconcile_startup()

        assert sequencer.ready is False
        assert repository.get_turn(channel.turn_id).status == "queued"


def test_process_kill_after_receipt_commit_is_interrupted_and_never_replayed(tmp_path):
    script = f"""
import os
from omicsclaw.control import (
    ControlStateRepository, IngressBackendConfig, IngressNormalizer,
    RawContentBlockV1, RawInboundV1, TurnSequencer,
)

class KillBeforeEnqueue(TurnSequencer):
    def _commit(self, reservation, envelope):
        os._exit(74)

repo = ControlStateRepository({str(tmp_path)!r})
sequencer = KillBeforeEnqueue(
    repo,
    max_entries_per_conversation=2,
    max_entries_total=2,
)
sequencer.reconcile_startup()
normalizer = IngressNormalizer(
    repo,
    sequencer,
    IngressBackendConfig(
        workspace_id='workspace-test',
        trusted_local_source_namespaces={{
            'desktop': frozenset({{'desktop/v1/test-installation/owner'}}),
        }},
    ),
)
normalizer.accept(RawInboundV1(
    schema_version=1,
    surface='desktop',
    source_namespace='desktop/v1/test-installation/owner',
    source_request_id='committed-before-kill',
    reply_target={{
        'schema_version': 1,
        'kind': 'desktop',
        'installation_id': 'test-installation',
        'profile_id': 'owner',
        'slot': 'main',
    }},
    content=(RawContentBlockV1(kind='text', text='committed-before-kill'),),
))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_ROOT,
        check=False,
    )

    assert completed.returncode == 74
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=2,
        )
        reconciled = sequencer.reconcile_startup()
        duplicate = _normalizer(repository, sequencer).accept(
            _raw("committed-before-kill")
        )

        assert len(reconciled.interrupted_turn_ids) == 1
        assert duplicate.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.turn_id == reconciled.interrupted_turn_ids[0]
        assert repository.get_turn(duplicate.turn_id).status == "interrupted"


@pytest.mark.asyncio
async def test_whole_turn_execution_is_serial_per_conversation_and_cross_concurrent(
    tmp_path,
):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=3,
            max_entries_total=6,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        first = normalizer.accept(_raw("a-first"))
        second = normalizer.accept(_raw("a-second"))
        other = normalizer.accept(_raw("b-first", slot="secondary"))

        active_by_conversation: defaultdict[str, int] = defaultdict(int)
        max_by_conversation: defaultdict[str, int] = defaultdict(int)
        global_active = 0
        max_global_active = 0
        started: list[str] = []
        finished: list[str] = []
        first_pair_started = asyncio.Event()

        async def worker(context):
            nonlocal global_active, max_global_active
            conversation_id = context.envelope.conversation_id
            active_by_conversation[conversation_id] += 1
            max_by_conversation[conversation_id] = max(
                max_by_conversation[conversation_id],
                active_by_conversation[conversation_id],
            )
            global_active += 1
            max_global_active = max(max_global_active, global_active)
            started.append(context.envelope.turn_id)
            if context.envelope.turn_id in {first.turn_id, other.turn_id}:
                if global_active == 2:
                    first_pair_started.set()
                await asyncio.wait_for(first_pair_started.wait(), timeout=1)
            await asyncio.sleep(0)
            finished.append(context.envelope.turn_id)
            global_active -= 1
            active_by_conversation[conversation_id] -= 1
            return TurnTerminalOutcome("succeeded")

        same_results, other_results = await asyncio.gather(
            sequencer.drain(first.conversation_id, worker),
            sequencer.drain(other.conversation_id, worker),
        )

        assert tuple(result.turn_id for result in same_results) == (
            first.turn_id,
            second.turn_id,
        )
        assert tuple(result.turn_id for result in other_results) == (other.turn_id,)
        assert max_by_conversation[first.conversation_id] == 1
        assert max_by_conversation[other.conversation_id] == 1
        assert max_global_active == 2
        assert started.index(first.turn_id) < started.index(second.turn_id)
        assert finished.index(first.turn_id) < started.index(second.turn_id)
        assert repository.get_turn(first.turn_id).status == "succeeded"
        assert repository.get_turn(second.turn_id).status == "succeeded"
        assert repository.get_turn(other.turn_id).status == "succeeded"


@pytest.mark.asyncio
async def test_concurrent_activation_cannot_bypass_one_active_turn(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=3,
            max_entries_total=4,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        first = normalizer.accept(_raw("first"))
        second = normalizer.accept(_raw("second"))
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_worker(_context):
            started.set()
            await release.wait()
            return TurnTerminalOutcome("succeeded")

        active_task = asyncio.create_task(
            sequencer.execute_next(first.conversation_id, blocking_worker)
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        blocked = await sequencer.execute_next(
            first.conversation_id,
            lambda _context: pytest.fail("second Turn started concurrently"),
        )
        release.set()
        completed = await active_task
        next_result = await sequencer.execute_next(
            first.conversation_id,
            lambda _context: asyncio.sleep(0, result=TurnTerminalOutcome("succeeded")),
        )

        assert blocked.state == "conversation_active"
        assert completed.turn_id == first.turn_id
        assert next_result.turn_id == second.turn_id


@pytest.mark.asyncio
async def test_local_active_transition_failure_quarantines_until_restart(
    tmp_path,
    monkeypatch,
):
    real_active_turn = turn_runtime_module._ActiveTurn
    fail_once = True

    def failing_active_turn(*args, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise MemoryError("active state allocation failed")
        return real_active_turn(*args, **kwargs)

    monkeypatch.setattr(turn_runtime_module, "_ActiveTurn", failing_active_turn)
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=2,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        accepted = normalizer.accept(_raw("active-transition-fails"))

        with pytest.raises(MemoryError, match="active state allocation"):
            await sequencer.execute_next(
                accepted.conversation_id,
                lambda _context: pytest.fail("Worker must not start"),
            )

        blocked = normalizer.accept(_raw("must-not-bypass-running"))
        assert repository.get_turn(accepted.turn_id).status == "running"
        assert blocked.status is TurnAcceptanceStatus.REJECTED
        assert blocked.code == "turn_execution_unavailable"

    with ControlStateRepository(tmp_path) as recovered_repository:
        recovered = TurnSequencer(
            recovered_repository,
            max_entries_per_conversation=2,
            max_entries_total=2,
        ).reconcile_startup()

        assert recovered.interrupted_turn_ids == (accepted.turn_id,)
        receipt = recovered_repository.get_turn(accepted.turn_id)
        assert receipt.status == "interrupted"
        assert receipt.terminal_code == "control_plane_restarted"


@pytest.mark.asyncio
async def test_waiting_cancel_has_no_worker_effect_and_active_cancel_is_cooperative(
    tmp_path,
):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=3,
            max_entries_total=4,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        active = normalizer.accept(_raw("active"))
        waiting = normalizer.accept(_raw("waiting"))
        worker_calls: list[str] = []
        started = asyncio.Event()

        async def worker(context):
            worker_calls.append(context.envelope.turn_id)
            started.set()
            while not context.cancellation.requested:
                await asyncio.sleep(0)
            return TurnTerminalOutcome("succeeded")

        task = asyncio.create_task(
            sequencer.execute_next(active.conversation_id, worker)
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        waiting_cancel = sequencer.cancel(waiting.turn_id)
        active_cancel = sequencer.cancel(active.turn_id)
        result = await asyncio.wait_for(task, timeout=1)

        assert waiting_cancel.code == "canceled_waiting"
        assert active_cancel.code == "cancel_requested"
        assert result.terminal_status == "canceled"
        assert worker_calls == [active.turn_id]
        assert repository.get_turn(active.turn_id).status == "canceled"
        assert repository.get_turn(waiting.turn_id).status == "canceled"


@pytest.mark.asyncio
async def test_worker_failure_is_sanitized_and_releases_the_next_turn(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=3,
            max_entries_total=4,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        first = normalizer.accept(_raw("first-fails"))
        second = normalizer.accept(_raw("second-runs"))

        async def worker(context):
            if context.envelope.turn_id == first.turn_id:
                raise RuntimeError("secret provider failure details")
            return TurnTerminalOutcome("succeeded")

        results = await sequencer.drain(first.conversation_id, worker)

        assert tuple(result.terminal_status for result in results) == (
            "failed",
            "succeeded",
        )
        assert repository.get_turn(first.turn_id).terminal_code == "worker_failed"
        assert "secret" not in repository.get_turn(first.turn_id).terminal_code
        assert repository.get_turn(second.turn_id).status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_observer_method", ("wait_idle", "close"))
async def test_custom_worker_baseexception_quarantines_and_is_supervised(
    tmp_path,
    first_observer_method,
):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=2,
        )
        normalizer = _normalizer(repository, sequencer)
        worker_started = asyncio.Event()
        release_worker = asyncio.Event()
        failure = _CatastrophicWorkerFailure("catastrophic Worker boundary failure")

        async def worker(_context):
            worker_started.set()
            await release_worker.wait()
            raise failure

        coordinator = TurnExecutionCoordinator(normalizer, sequencer, worker)
        await coordinator.start()
        first = await coordinator.submit(_raw("baseexception-active"))
        await asyncio.wait_for(worker_started.wait(), timeout=1)
        successor = await coordinator.submit(_raw("baseexception-successor"))

        release_worker.set()
        with pytest.raises(_CatastrophicWorkerFailure) as raised:
            observe_failure = getattr(coordinator, first_observer_method)
            await asyncio.wait_for(observe_failure(), timeout=1)
        second_observer_method = (
            "close" if first_observer_method == "wait_idle" else "wait_idle"
        )
        observe_already_reported = getattr(coordinator, second_observer_method)
        await asyncio.wait_for(observe_already_reported(), timeout=1)

        rejected = normalizer.accept(_raw("baseexception-new-admission"))
        with pytest.raises(TurnConversationUnavailableError, match="quarantined"):
            await sequencer.execute_next(
                first.conversation_id,
                lambda _context: pytest.fail("quarantined successor must not execute"),
            )

        assert raised.value is failure
        assert repository.get_turn(first.turn_id).status == "running"
        assert repository.get_turn(successor.turn_id).status == "queued"
        assert rejected.status is TurnAcceptanceStatus.REJECTED
        assert rejected.code == "turn_execution_unavailable"
        assert sequencer.try_reserve("unrelated-capacity-probe") is None


@pytest.mark.asyncio
async def test_worker_returned_non_allowlisted_code_is_sanitized(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=1,
            max_entries_total=1,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        accepted = normalizer.accept(_raw("worker-sensitive-code"))

        async def worker(_context):
            return TurnTerminalOutcome(
                "failed",
                "sk_sensitivecredential123",  # type: ignore[arg-type]
            )

        result = await sequencer.execute_next(accepted.conversation_id, worker)

        assert result.terminal_status == "failed"
        assert result.terminal_code == "worker_failed"
        assert repository.get_turn(accepted.turn_id).terminal_code == "worker_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "malformed_status",
        "malformed_code",
        "expected_status",
        "expected_code",
    ),
    (
        ([], None, "failed", "invalid_worker_outcome"),
        ({}, [], "failed", "invalid_worker_outcome"),
        (None, {}, "failed", "invalid_worker_outcome"),
        (7, 9, "failed", "invalid_worker_outcome"),
        ("failed", [], "failed", "worker_failed"),
        ("canceled", {}, "canceled", "canceled"),
        (
            _HostileWorkerString("failed"),
            _HostileWorkerString("worker_failed"),
            "failed",
            "worker_failed",
        ),
    ),
    ids=(
        "list-status",
        "dict-status",
        "none-status",
        "int-status",
        "list-failure-code",
        "dict-cancel-code",
        "hostile-string-subclasses",
    ),
)
async def test_malformed_worker_outcome_is_safely_normalized_and_releases(
    tmp_path,
    malformed_status,
    malformed_code,
    expected_status,
    expected_code,
):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=2,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        malformed = normalizer.accept(_raw("worker-malformed-outcome"))
        successor = normalizer.accept(_raw("worker-successor"))

        async def worker(context):
            # This value object is deliberately permissive: the Sequencer, not
            # Worker construction, is the untrusted outcome validation seam.
            if context.envelope.turn_id == malformed.turn_id:
                return TurnTerminalOutcome(
                    malformed_status,
                    malformed_code,
                )  # type: ignore[arg-type]
            return TurnTerminalOutcome("succeeded")

        results = await sequencer.drain(malformed.conversation_id, worker)
        admitted_after_release = normalizer.accept(_raw("after-malformed-outcome"))
        after_release = await sequencer.execute_next(
            malformed.conversation_id,
            worker,
        )

        assert tuple(result.turn_id for result in results) == (
            malformed.turn_id,
            successor.turn_id,
        )
        assert results[0].terminal_status == expected_status
        assert results[0].terminal_code == expected_code
        # Exact built-in identity is intentional: an ``isinstance`` check
        # would also accept the hostile ``str`` subclass under test.
        assert type(results[0].terminal_status) is str  # noqa: E721
        assert type(results[0].terminal_code) is str  # noqa: E721
        assert repository.get_turn(malformed.turn_id).status == expected_status
        assert repository.get_turn(malformed.turn_id).terminal_code == expected_code
        assert repository.get_turn(successor.turn_id).status == "succeeded"
        assert admitted_after_release.status is TurnAcceptanceStatus.ACCEPTED
        assert after_release.turn_id == admitted_after_release.turn_id
        assert after_release.terminal_status == "succeeded"


@pytest.mark.asyncio
async def test_active_turn_keeps_capacity_until_terminal_release(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=2,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        active = normalizer.accept(_raw("capacity-active"))
        waiting = normalizer.accept(_raw("capacity-waiting"))
        blocked_raw = _raw("capacity-blocked")
        started = asyncio.Event()
        release = asyncio.Event()

        async def worker(_context):
            started.set()
            await release.wait()
            return TurnTerminalOutcome("succeeded")

        task = asyncio.create_task(
            sequencer.execute_next(active.conversation_id, worker)
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        blocked = normalizer.accept(blocked_raw)
        canceled = sequencer.cancel(waiting.turn_id)
        admitted_after_release = normalizer.accept(blocked_raw)
        release.set()
        await asyncio.wait_for(task, timeout=1)

        assert blocked.status is TurnAcceptanceStatus.REJECTED
        assert blocked.code == "turn_backpressure"
        assert canceled.code == "canceled_waiting"
        assert admitted_after_release.status is TurnAcceptanceStatus.ACCEPTED


@pytest.mark.asyncio
async def test_worker_task_cancellation_is_interrupted_not_owner_canceled(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=2,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        first = normalizer.accept(_raw("worker-canceled"))
        second = normalizer.accept(_raw("successor"))

        async def worker(context):
            if context.envelope.turn_id == first.turn_id:
                raise asyncio.CancelledError
            return TurnTerminalOutcome("succeeded")

        results = await sequencer.drain(first.conversation_id, worker)

        assert tuple(result.turn_id for result in results) == (
            first.turn_id,
            second.turn_id,
        )
        assert repository.get_turn(first.turn_id).status == "interrupted"
        assert repository.get_turn(first.turn_id).terminal_code == (
            "worker_task_interrupted"
        )
        assert repository.get_turn(second.turn_id).status == "succeeded"


@pytest.mark.asyncio
async def test_external_task_cancellation_interrupts_only_active_turn(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=2,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        first = normalizer.accept(_raw("task-canceled"))
        second = normalizer.accept(_raw("still-waiting"))
        started = asyncio.Event()

        async def blocking_worker(_context):
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        task = asyncio.create_task(
            sequencer.drain(first.conversation_id, blocking_worker)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert repository.get_turn(first.turn_id).status == "interrupted"
        assert repository.get_turn(first.turn_id).terminal_code == (
            "worker_task_interrupted"
        )
        assert repository.get_turn(second.turn_id).status == "queued"

        successor = await sequencer.execute_next(
            first.conversation_id,
            lambda _context: asyncio.sleep(0, result=TurnTerminalOutcome("succeeded")),
        )
        assert successor.turn_id == second.turn_id


@pytest.mark.asyncio
async def test_coordinator_wakes_during_active_turn_and_duplicate_never_replays(
    tmp_path,
):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=3,
            max_entries_total=4,
        )
        normalizer = _normalizer(repository, sequencer)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        worker_calls: list[str] = []

        async def worker(context):
            worker_calls.append(context.envelope.turn_id)
            if len(worker_calls) == 1:
                first_started.set()
                await release_first.wait()
            return TurnTerminalOutcome("succeeded")

        coordinator = TurnExecutionCoordinator(normalizer, sequencer, worker)
        startup = await coordinator.start()
        first = await coordinator.submit(_raw("coordinator-first"))
        await asyncio.wait_for(first_started.wait(), timeout=1)

        second = await coordinator.submit(_raw("coordinator-second"))
        duplicate = await coordinator.submit(_raw("coordinator-second"))
        release_first.set()
        await asyncio.wait_for(coordinator.wait_idle(), timeout=1)
        await coordinator.close()

        assert startup.interrupted_turn_ids == ()
        assert first.status is TurnAcceptanceStatus.ACCEPTED
        assert second.status is TurnAcceptanceStatus.ACCEPTED
        assert duplicate.status is TurnAcceptanceStatus.DUPLICATE
        assert worker_calls == [first.turn_id, second.turn_id]
        assert repository.get_turn(first.turn_id).status == "succeeded"
        assert repository.get_turn(second.turn_id).status == "succeeded"


@pytest.mark.asyncio
async def test_coordinator_prepares_live_state_before_worker_activation(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=1,
            max_entries_total=1,
        )
        normalizer = _normalizer(repository, sequencer)
        prepared_turn_ids: set[str] = set()
        worker_observations: list[bool] = []

        async def worker(context):
            worker_observations.append(context.envelope.turn_id in prepared_turn_ids)
            return TurnTerminalOutcome("succeeded")

        coordinator = TurnExecutionCoordinator(normalizer, sequencer, worker)
        await coordinator.start()
        accepted = await coordinator.submit(
            _raw("prepare-before-wake"),
            prepare_accepted=lambda result: prepared_turn_ids.add(result.turn_id),
        )
        await coordinator.close()

        assert accepted.status is TurnAcceptanceStatus.ACCEPTED
        assert worker_observations == [True]


@pytest.mark.asyncio
async def test_terminal_publisher_failure_releases_without_replay(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=2,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        first = normalizer.accept(_raw("publish-fails"))
        second = normalizer.accept(_raw("after-publish-failure"))
        worker_calls: list[str] = []

        async def worker(context):
            worker_calls.append(context.envelope.turn_id)
            return TurnTerminalOutcome("succeeded")

        async def publisher(_receipt):
            raise RuntimeError("event boundary unavailable")

        results = await sequencer.drain(
            first.conversation_id,
            worker,
            publish_terminal=publisher,
        )

        assert tuple(result.turn_id for result in results) == (
            first.turn_id,
            second.turn_id,
        )
        assert all(result.event_published is False for result in results)
        assert worker_calls == [first.turn_id, second.turn_id]
        assert repository.get_turn(first.turn_id).status == "succeeded"
        assert repository.get_turn(second.turn_id).status == "succeeded"


@pytest.mark.asyncio
async def test_terminal_commit_failure_quarantines_conversation_and_keeps_lease(
    tmp_path,
):
    armed = False

    def fault_hook(name: str) -> None:
        if armed and name == "terminalize_turn.before_commit":
            raise RuntimeError("terminal commit fault")

    with ControlStateRepository(tmp_path, fault_hook=fault_hook) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=3,
            max_entries_total=4,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        first = normalizer.accept(_raw("commit-fails"))
        second = normalizer.accept(_raw("must-not-start"))
        armed = True

        with pytest.raises(RuntimeError, match="terminal commit fault"):
            await sequencer.execute_next(
                first.conversation_id,
                lambda _context: asyncio.sleep(
                    0, result=TurnTerminalOutcome("succeeded")
                ),
            )

        assert repository.get_turn(first.turn_id).status == "running"
        assert repository.get_turn(second.turn_id).status == "queued"
        with pytest.raises(ControlIntegrityError, match="quarantined"):
            await sequencer.execute_next(
                first.conversation_id,
                lambda _context: pytest.fail("successor must not execute"),
            )

        rejected = normalizer.accept(_raw("new-after-fault"))
        assert rejected.status is TurnAcceptanceStatus.REJECTED
        assert rejected.code == "turn_execution_unavailable"


@pytest.mark.asyncio
async def test_cancel_during_terminal_publication_reports_completion_in_progress(
    tmp_path,
):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=2,
        )
        sequencer.reconcile_startup()
        normalizer = _normalizer(repository, sequencer)
        accepted = normalizer.accept(_raw("terminal-race"))
        publishing = asyncio.Event()
        release_publish = asyncio.Event()

        async def publisher(_receipt):
            publishing.set()
            await release_publish.wait()

        task = asyncio.create_task(
            sequencer.execute_next(
                accepted.conversation_id,
                lambda _context: asyncio.sleep(
                    0, result=TurnTerminalOutcome("succeeded")
                ),
                publish_terminal=publisher,
            )
        )
        await asyncio.wait_for(publishing.wait(), timeout=1)

        cancel = sequencer.cancel(accepted.turn_id)
        release_publish.set()
        result = await asyncio.wait_for(task, timeout=1)

        assert cancel.changed is False
        assert cancel.code == "completion_in_progress"
        assert result.terminal_status == "succeeded"
        assert repository.get_turn(accepted.turn_id).status == "succeeded"


@pytest.mark.asyncio
async def test_close_waits_for_other_conversations_before_reporting_runner_failure(
    tmp_path,
):
    fail_next_terminal_commit = False
    terminal_fault_injected = False

    def fault_hook(name: str) -> None:
        nonlocal terminal_fault_injected
        if (
            fail_next_terminal_commit
            and not terminal_fault_injected
            and name == "terminalize_turn.before_commit"
        ):
            terminal_fault_injected = True
            raise RuntimeError("one conversation failed terminal commit")

    with ControlStateRepository(tmp_path, fault_hook=fault_hook) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=4,
        )
        normalizer = _normalizer(repository, sequencer)
        other_started = asyncio.Event()
        release_other = asyncio.Event()

        async def worker(context):
            if context.envelope.reply_target["slot"] == "secondary":
                other_started.set()
                await release_other.wait()
            return TurnTerminalOutcome("succeeded")

        coordinator = TurnExecutionCoordinator(normalizer, sequencer, worker)
        await coordinator.start()
        fail_next_terminal_commit = True
        await coordinator.submit(_raw("faulted-conversation"))
        other = await coordinator.submit(_raw("slow-conversation", slot="secondary"))
        await asyncio.wait_for(other_started.wait(), timeout=1)

        close_task = asyncio.create_task(coordinator.close())
        await asyncio.sleep(0)
        assert close_task.done() is False

        release_other.set()
        with pytest.raises(RuntimeError, match="one conversation failed"):
            await asyncio.wait_for(close_task, timeout=1)

        assert terminal_fault_injected is True
        assert repository.get_turn(other.turn_id).status == "succeeded"


@pytest.mark.asyncio
async def test_runner_canceled_before_first_schedule_is_cleaned_up(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=1,
            max_entries_total=1,
        )
        normalizer = _normalizer(repository, sequencer)
        coordinator = TurnExecutionCoordinator(
            normalizer,
            sequencer,
            lambda _context: asyncio.sleep(0, result=TurnTerminalOutcome("succeeded")),
        )
        await coordinator.start()
        accepted = await coordinator.submit(_raw("cancel-before-schedule"))
        runner = coordinator._runners[accepted.conversation_id]

        runner.cancel()

        with pytest.raises(RuntimeError, match="Turn runner.*was canceled"):
            await asyncio.wait_for(coordinator.close(), timeout=1)
        assert coordinator._runners == {}
        assert repository.get_turn(accepted.turn_id).status == "queued"


@pytest.mark.asyncio
async def test_wait_idle_observer_cancellation_does_not_cancel_active_worker(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        sequencer = TurnSequencer(
            repository,
            max_entries_per_conversation=2,
            max_entries_total=2,
        )
        normalizer = _normalizer(repository, sequencer)
        started = asyncio.Event()
        release = asyncio.Event()
        cancellation_seen: list[bool] = []

        async def worker(context):
            started.set()
            await release.wait()
            cancellation_seen.append(context.cancellation.requested)
            return TurnTerminalOutcome("succeeded")

        coordinator = TurnExecutionCoordinator(normalizer, sequencer, worker)
        await coordinator.start()
        accepted = await coordinator.submit(_raw("observer-detach"))
        await asyncio.wait_for(started.wait(), timeout=1)

        observer = asyncio.create_task(coordinator.wait_idle())
        await asyncio.sleep(0)
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer

        assert repository.get_turn(accepted.turn_id).status == "running"
        release.set()
        await asyncio.wait_for(coordinator.wait_idle(), timeout=1)

        assert cancellation_seen == [False]
        assert repository.get_turn(accepted.turn_id).status == "succeeded"
