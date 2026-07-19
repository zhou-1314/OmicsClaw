from __future__ import annotations

import asyncio
import hashlib

import pytest

from omicsclaw.control import (
    ControlStateRepository,
    DeliveryAdapterResult,
    DeliveryAttemptOutcome,
    DeliveryAttemptRequest,
    DeliveryCandidate,
    DeliveryCapacitySnapshot,
    DeliveryItemPlan,
    DeliveryPlan,
    DeliveryPump,
    DeliveryStartupRecoveryResult,
    TurnAcceptanceIntent,
    TurnTranscriptRef,
)
from omicsclaw.control.delivery import DeliveryAdapterTerminationError
from omicsclaw.control.delivery_content import DeliveryContentIntegrityError


def _transcript_ref(seed: str) -> TurnTranscriptRef:
    return TurnTranscriptRef(
        hashlib.sha256(f"entry:{seed}".encode()).hexdigest()[:32],
        hashlib.sha256(f"content:{seed}".encode()).hexdigest(),
    )


def _fingerprint(character: str) -> str:
    return character * 64


def _channel_intent(
    request_id: str,
    destination_id: str,
    *,
    account_namespace: str = "primary",
) -> TurnAcceptanceIntent:
    return TurnAcceptanceIntent(
        surface="channel",
        source_namespace=f"channel/telegram/v1/{account_namespace}",
        source_request_id=request_id,
        fingerprint_version=1,
        fingerprint_sha256=_fingerprint(request_id[0]),
        reply_target={
            "kind": "channel",
            "adapter": "telegram",
            "account_namespace": account_namespace,
            "destination_id": destination_id,
        },
        new_conversation=True,
    )


def _delivery_plan(content_ref: str) -> DeliveryPlan:
    return DeliveryPlan(
        terminal_kind="succeeded",
        items=(
            DeliveryItemPlan(
                item_kind="text",
                content_store="transcript",
                content_ref=content_ref,
                content_sha256=_fingerprint("c"),
            ),
        ),
    )


def _terminal_delivery(
    repository: ControlStateRepository,
    *,
    request_id: str,
    destination_id: str,
    account_namespace: str = "primary",
):
    accepted = repository.accept_turn(
        _channel_intent(
            request_id,
            destination_id,
            account_namespace=account_namespace,
        )
    )
    repository.start_turn(accepted.turn_id)
    transcript_ref = _transcript_ref(request_id)
    terminal = repository.terminalize_turn(
        accepted.turn_id,
        terminal_status="succeeded",
        transcript_ref=transcript_ref,
        delivery_plan=_delivery_plan(transcript_ref.entry_id),
    )
    assert terminal.delivery is not None
    return terminal.delivery


def test_delivery_adapter_evidence_is_bounded_and_secret_free() -> None:
    with pytest.raises(ValueError, match="credentials"):
        DeliveryAdapterResult(
            DeliveryAttemptOutcome.ACCEPTED,
            provider_evidence={"authorization_token": "secret"},
        )
    with pytest.raises(ValueError, match="bounded scalars"):
        DeliveryAdapterResult(
            DeliveryAttemptOutcome.ACCEPTED,
            provider_evidence={"provider_message_id": "x" * 257},
        )


def test_repository_revalidates_provider_evidence_at_command_boundary(tmp_path) -> None:
    with ControlStateRepository(tmp_path, clock_ms=lambda: 1_000) as repository:
        delivery = _terminal_delivery(
            repository,
            request_id="a-evidence-boundary",
            destination_id="chat-7",
        )
        claim = repository.claim_delivery_attempt(
            repository.list_due_delivery_candidates()[0]
        )
        assert claim.request is not None

        with pytest.raises(ValueError, match="credentials"):
            repository.finish_delivery_attempt(
                claim.request.attempt_id,
                DeliveryAttemptOutcome.ACCEPTED,
                provider_evidence={"authorization_token": "secret"},
            )

        assert (
            repository.list_delivery_items(delivery.delivery_id)[0].state == "sending"
        )
        repository.finish_delivery_attempt(
            claim.request.attempt_id,
            DeliveryAttemptOutcome.ACCEPTED,
        )


def test_due_candidates_and_claim_recheck_the_target_barrier(tmp_path):
    with ControlStateRepository(tmp_path, clock_ms=lambda: 1_000) as repository:
        first = _terminal_delivery(
            repository,
            request_id="a-first",
            destination_id="chat-7",
        )
        second = _terminal_delivery(
            repository,
            request_id="b-second",
            destination_id="chat-7",
        )

        candidates = repository.list_due_delivery_candidates()

        assert len(candidates) == 1
        candidate = candidates[0]
        assert isinstance(candidate, DeliveryCandidate)
        assert candidate.delivery_id == first.delivery_id
        assert candidate.target_sequence == 1
        assert candidate.reply_target["destination_id"] == "chat-7"

        claim = repository.claim_delivery_attempt(candidate)

        assert claim.claimed is True
        assert isinstance(claim.request, DeliveryAttemptRequest)
        assert claim.request.candidate == candidate
        assert claim.request.attempt_no == 1
        assert repository.list_due_delivery_candidates() == ()
        assert second.target_sequence == 2


def test_channel_capacity_moves_from_future_turn_to_actual_delivery(tmp_path):
    with ControlStateRepository(tmp_path, clock_ms=lambda: 1_000) as repository:
        accepted = repository.accept_turn(_channel_intent("a-capacity", "chat-7"))

        future = repository.channel_delivery_capacity()

        assert isinstance(future, DeliveryCapacitySnapshot)
        assert future.future_deliveries == 1
        assert future.actual_deliveries == 0
        assert future.actual_items == 0
        assert future.total_deliveries == 1

        repository.start_turn(accepted.turn_id)
        transcript_ref = _transcript_ref("capacity")
        terminal = repository.terminalize_turn(
            accepted.turn_id,
            terminal_status="succeeded",
            transcript_ref=transcript_ref,
            delivery_plan=_delivery_plan(transcript_ref.entry_id),
        )
        assert terminal.delivery is not None

        actual = repository.channel_delivery_capacity()

        assert actual.future_deliveries == 0
        assert actual.actual_deliveries == 1
        assert actual.actual_items == 1
        assert actual.total_deliveries == 1

        claim = repository.claim_delivery_attempt(
            repository.list_due_delivery_candidates()[0]
        )
        assert claim.request is not None
        repository.finish_delivery_attempt(
            claim.request.attempt_id,
            DeliveryAttemptOutcome.ACCEPTED,
        )

        empty = repository.channel_delivery_capacity()
        assert empty.future_deliveries == 0
        assert empty.actual_deliveries == 0
        assert empty.actual_items == 0
        assert empty.total_deliveries == 0


def test_startup_recovery_marks_sending_unknown_and_suppresses_suffix(tmp_path):
    repository = ControlStateRepository(tmp_path, clock_ms=lambda: 1_000)
    first = repository.accept_turn(_channel_intent("a-crash", "chat-7"))
    second = repository.accept_turn(_channel_intent("b-after", "chat-7"))
    for accepted in (first, second):
        repository.start_turn(accepted.turn_id)
    first_ref = _transcript_ref("crash")
    first_terminal = repository.terminalize_turn(
        first.turn_id,
        terminal_status="succeeded",
        transcript_ref=first_ref,
        delivery_plan=DeliveryPlan(
            terminal_kind="succeeded",
            items=(
                DeliveryItemPlan(
                    item_kind="text",
                    content_store="transcript",
                    content_ref=first_ref.entry_id,
                    content_sha256=_fingerprint("c"),
                ),
                DeliveryItemPlan(
                    item_kind="text",
                    content_store="transcript",
                    content_ref=first_ref.entry_id,
                    content_sha256=_fingerprint("d"),
                ),
            ),
        ),
    )
    second_ref = _transcript_ref("after")
    second_terminal = repository.terminalize_turn(
        second.turn_id,
        terminal_status="succeeded",
        transcript_ref=second_ref,
        delivery_plan=_delivery_plan(second_ref.entry_id),
    )
    assert first_terminal.delivery is not None
    assert second_terminal.delivery is not None
    claim = repository.claim_delivery_attempt(
        repository.list_due_delivery_candidates()[0]
    )
    assert claim.request is not None
    sending_item_id = claim.request.candidate.item_id
    open_attempt_id = claim.request.attempt_id
    repository.close()

    with ControlStateRepository(tmp_path, clock_ms=lambda: 2_000) as reopened:
        recovery = reopened.reconcile_delivery_startup()

        assert isinstance(recovery, DeliveryStartupRecoveryResult)
        assert recovery.unknown_item_ids == (sending_item_id,)
        assert recovery.closed_attempt_ids == (open_attempt_id,)
        first_items = reopened.list_delivery_items(first_terminal.delivery.delivery_id)
        assert [item.state for item in first_items] == ["unknown", "suppressed"]
        assert first_items[1].blocked_by_item_id == sending_item_id

        due = reopened.list_due_delivery_candidates()
        assert [candidate.delivery_id for candidate in due] == [
            second_terminal.delivery.delivery_id
        ]
        assert reopened.reconcile_delivery_startup().unknown_item_ids == ()


def test_delivery_capacity_gate_counts_total_and_adapter_account_scope(tmp_path):
    account_a = {
        "kind": "channel",
        "adapter": "telegram",
        "account_namespace": "primary",
        "destination_id": "chat-a",
    }
    account_b = {
        "kind": "channel",
        "adapter": "telegram",
        "account_namespace": "secondary",
        "destination_id": "chat-b",
    }
    with ControlStateRepository(tmp_path, clock_ms=lambda: 1_000) as repository:
        assert repository.has_delivery_capacity(
            account_a,
            max_total=1,
            max_per_account=1,
        )

        accepted = repository.accept_turn(_channel_intent("a-gate", "chat-a"))

        assert not repository.has_delivery_capacity(
            account_a,
            max_total=1,
            max_per_account=1,
        )
        assert repository.has_delivery_capacity(
            account_b,
            max_total=2,
            max_per_account=1,
        )

        repository.start_turn(accepted.turn_id)
        transcript_ref = _transcript_ref("gate")
        terminal = repository.terminalize_turn(
            accepted.turn_id,
            terminal_status="succeeded",
            transcript_ref=transcript_ref,
            delivery_plan=_delivery_plan(transcript_ref.entry_id),
        )
        assert terminal.delivery is not None

        assert not repository.has_delivery_capacity(
            account_a,
            max_total=2,
            max_per_account=1,
        )
        assert repository.has_delivery_capacity(
            account_b,
            max_total=2,
            max_per_account=1,
        )


@pytest.mark.asyncio
async def test_delivery_pump_sends_one_claimed_attempt_and_marks_it_delivered(tmp_path):
    requests: list[DeliveryAttemptRequest] = []

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        requests.append(request)
        return DeliveryAdapterResult(
            DeliveryAttemptOutcome.ACCEPTED,
            provider_evidence={"provider_message_id": "message-7"},
        )

    with ControlStateRepository(tmp_path, clock_ms=lambda: 1_000) as repository:
        delivery = _terminal_delivery(
            repository,
            request_id="a-pump",
            destination_id="chat-7",
        )
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): adapter},
            content_resolver=lambda _candidate: "resolved reply",
        )

        await pump.start()
        pump.wake()
        await pump.wait_idle()
        await pump.close()

        assert len(requests) == 1
        assert requests[0].candidate.delivery_id == delivery.delivery_id
        assert requests[0].candidate.reply_target["destination_id"] == "chat-7"
        assert requests[0].attempt_no == 1
        assert requests[0].item_id == requests[0].candidate.item_id
        assert requests[0].delivery_id == delivery.delivery_id
        assert requests[0].reply_target["destination_id"] == "chat-7"
        assert requests[0].text == "resolved reply"
        assert [
            item.state for item in repository.list_delivery_items(delivery.delivery_id)
        ] == ["delivered"]


@pytest.mark.asyncio
async def test_delivery_pump_runs_different_reply_targets_concurrently(tmp_path):
    entered: set[str] = set()
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        entered.add(str(request.candidate.reply_target["destination_id"]))
        if len(entered) == 2:
            both_entered.set()
        await release.wait()
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    with ControlStateRepository(tmp_path, clock_ms=lambda: 1_000) as repository:
        _terminal_delivery(repository, request_id="a-one", destination_id="chat-1")
        _terminal_delivery(repository, request_id="b-two", destination_id="chat-2")
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): adapter},
            content_resolver=lambda candidate: candidate.content_ref,
        )

        await pump.start()
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        assert entered == {"chat-1", "chat-2"}
        release.set()
        await pump.wait_idle()
        await pump.close()


@pytest.mark.asyncio
async def test_delivery_pump_never_claims_a_foreign_adapter_account(tmp_path):
    requests: list[DeliveryAttemptRequest] = []

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        requests.append(request)
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    with ControlStateRepository(tmp_path, clock_ms=lambda: 1_000) as repository:
        foreign = _terminal_delivery(
            repository,
            request_id="a-foreign-account",
            destination_id="chat-foreign",
            account_namespace="bot-older",
        )
        local = _terminal_delivery(
            repository,
            request_id="b-local-account",
            destination_id="chat-local",
            account_namespace="bot-current",
        )
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "bot-current"): adapter},
            content_resolver=lambda candidate: candidate.content_ref,
        )

        await pump.start()
        await pump.wait_idle()
        await pump.close()

        assert [request.delivery_id for request in requests] == [local.delivery_id]
        assert [
            item.state for item in repository.list_delivery_items(local.delivery_id)
        ] == ["delivered"]
        assert [
            item.state for item in repository.list_delivery_items(foreign.delivery_id)
        ] == ["queued"]
        assert [
            candidate.delivery_id
            for candidate in repository.list_due_delivery_candidates(
                adapter_accounts=(("telegram", "bot-older"),)
            )
        ] == [foreign.delivery_id]


@pytest.mark.asyncio
async def test_slow_target_does_not_block_later_work_for_another_target(tmp_path):
    release_slow = asyncio.Event()
    fast_second_entered = asyncio.Event()
    fast_sequences: list[int] = []

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        destination_id = request.reply_target["destination_id"]
        if destination_id == "chat-slow":
            await release_slow.wait()
        else:
            fast_sequences.append(request.candidate.target_sequence)
            if request.candidate.target_sequence == 2:
                fast_second_entered.set()
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    with ControlStateRepository(tmp_path, clock_ms=lambda: 1_000) as repository:
        _terminal_delivery(repository, request_id="a-slow", destination_id="chat-slow")
        _terminal_delivery(
            repository, request_id="b-fast-1", destination_id="chat-fast"
        )
        _terminal_delivery(
            repository, request_id="c-fast-2", destination_id="chat-fast"
        )
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): adapter},
            content_resolver=lambda candidate: candidate.content_ref,
        )

        await pump.start()
        try:
            await asyncio.wait_for(fast_second_entered.wait(), timeout=0.2)
        finally:
            release_slow.set()
            await pump.wait_idle()
            await pump.close()

        assert fast_sequences == [1, 2]


@pytest.mark.asyncio
async def test_delivery_pump_serializes_attempts_for_one_reply_target(tmp_path):
    entered: list[int] = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        entered.append(request.candidate.target_sequence)
        if len(entered) == 1:
            first_entered.set()
            await release_first.wait()
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    with ControlStateRepository(tmp_path, clock_ms=lambda: 1_000) as repository:
        _terminal_delivery(repository, request_id="a-one", destination_id="chat-7")
        _terminal_delivery(repository, request_id="b-two", destination_id="chat-7")
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): adapter},
            content_resolver=lambda candidate: candidate.content_ref,
        )

        await pump.start()
        await asyncio.wait_for(first_entered.wait(), timeout=1)
        await asyncio.sleep(0)
        assert entered == [1]
        release_first.set()
        await pump.wait_idle()
        await pump.close()

        assert entered == [1, 2]


@pytest.mark.asyncio
async def test_delivery_pump_treats_adapter_exception_as_unknown_and_suppresses_suffix(
    tmp_path,
):
    calls = 0

    async def adapter(_request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider outcome cannot be proven")

    with ControlStateRepository(tmp_path, clock_ms=lambda: 1_000) as repository:
        accepted = repository.accept_turn(_channel_intent("a-unknown", "chat-7"))
        repository.start_turn(accepted.turn_id)
        transcript_ref = _transcript_ref("unknown")
        terminal = repository.terminalize_turn(
            accepted.turn_id,
            terminal_status="succeeded",
            transcript_ref=transcript_ref,
            delivery_plan=DeliveryPlan(
                terminal_kind="succeeded",
                items=(
                    DeliveryItemPlan(
                        item_kind="text",
                        content_store="transcript",
                        content_ref=transcript_ref.entry_id,
                        content_sha256=_fingerprint("c"),
                    ),
                    DeliveryItemPlan(
                        item_kind="text",
                        content_store="transcript",
                        content_ref=transcript_ref.entry_id,
                        content_sha256=_fingerprint("d"),
                    ),
                ),
            ),
        )
        assert terminal.delivery is not None
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): adapter},
            content_resolver=lambda candidate: candidate.content_ref,
        )

        await pump.start()
        await pump.wait_idle()
        await pump.close()

        items = repository.list_delivery_items(terminal.delivery.delivery_id)
        assert calls == 1
        assert [item.state for item in items] == ["unknown", "suppressed"]
        assert items[0].last_error_code == "delivery_adapter_exception"
        assert items[1].blocked_by_item_id == items[0].item_id


@pytest.mark.asyncio
async def test_delivery_pump_content_failure_creates_no_attempt_and_suppresses_suffix(
    tmp_path,
):
    calls = 0

    async def adapter(_request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        nonlocal calls
        calls += 1
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    def fail_resolution(_candidate: DeliveryCandidate) -> str:
        raise DeliveryContentIntegrityError("digest mismatch")

    with ControlStateRepository(tmp_path, clock_ms=lambda: 1_000) as repository:
        accepted = repository.accept_turn(_channel_intent("a-content", "chat-7"))
        repository.start_turn(accepted.turn_id)
        transcript_ref = _transcript_ref("content")
        terminal = repository.terminalize_turn(
            accepted.turn_id,
            terminal_status="succeeded",
            transcript_ref=transcript_ref,
            delivery_plan=DeliveryPlan(
                terminal_kind="succeeded",
                items=(
                    DeliveryItemPlan(
                        item_kind="text",
                        content_store="transcript",
                        content_ref=transcript_ref.entry_id,
                        content_sha256=_fingerprint("c"),
                    ),
                    DeliveryItemPlan(
                        item_kind="text",
                        content_store="transcript",
                        content_ref=transcript_ref.entry_id,
                        content_sha256=_fingerprint("d"),
                    ),
                ),
            ),
        )
        assert terminal.delivery is not None
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): adapter},
            content_resolver=fail_resolution,
        )

        await pump.start()
        await pump.wait_idle()
        await pump.close()

        items = repository.list_delivery_items(terminal.delivery.delivery_id)
        assert calls == 0
        assert [item.state for item in items] == ["failed", "suppressed"]
        assert items[0].attempt_count == 0
        assert items[0].last_error_code == "content_integrity_failed"
        assert items[1].blocked_by_item_id == items[0].item_id


@pytest.mark.asyncio
async def test_transient_content_resolver_failure_preserves_queued_item(tmp_path):
    async def unreachable_adapter(
        _request: DeliveryAttemptRequest,
    ) -> DeliveryAdapterResult:
        raise AssertionError("provider must not run when content resolution fails")

    def transient_failure(_candidate: DeliveryCandidate) -> str:
        raise RuntimeError("temporary Transcript read failure")

    with ControlStateRepository(tmp_path) as repository:
        delivery = _terminal_delivery(
            repository,
            request_id="a-transient-content",
            destination_id="chat-7",
        )
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): unreachable_adapter},
            content_resolver=transient_failure,
        )

        await pump.start()
        with pytest.raises(RuntimeError, match="temporary Transcript read failure"):
            await pump.wait_idle()
        with pytest.raises(RuntimeError, match="temporary Transcript read failure"):
            await pump.close()

        item = repository.list_delivery_items(delivery.delivery_id)[0]
        assert item.state == "queued"
        assert item.attempt_count == 0
        assert item.last_error_code is None


@pytest.mark.asyncio
async def test_adapter_timeout_closes_attempt_as_unknown_and_bounds_shutdown(tmp_path):
    started = asyncio.Event()
    never = asyncio.Event()

    async def adapter(_request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        started.set()
        await never.wait()
        raise AssertionError("unreachable")

    with ControlStateRepository(tmp_path) as repository:
        delivery = _terminal_delivery(
            repository,
            request_id="a-provider-timeout",
            destination_id="chat-7",
        )
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): adapter},
            content_resolver=lambda candidate: candidate.content_ref,
            attempt_timeout_seconds=0.01,
        )

        await pump.start()
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(pump.close(), timeout=0.2)

        item = repository.list_delivery_items(delivery.delivery_id)[0]
        assert item.state == "unknown"
        assert item.attempt_count == 1
        assert item.last_error_code == "delivery_adapter_timeout"


@pytest.mark.asyncio
async def test_cancellation_resistant_adapter_halts_pump_before_next_target_sequence(
    tmp_path,
):
    cancellation_seen = asyncio.Event()
    release_lingering = asyncio.Event()
    lingering_done = asyncio.Event()
    second_started = asyncio.Event()

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        if request.candidate.target_sequence == 2:
            second_started.set()
            return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            try:
                await release_lingering.wait()
                return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)
            finally:
                lingering_done.set()

    with ControlStateRepository(tmp_path) as repository:
        first = _terminal_delivery(
            repository,
            request_id="a-cancellation-resistant",
            destination_id="chat-7",
        )
        second = _terminal_delivery(
            repository,
            request_id="b-must-not-overtake",
            destination_id="chat-7",
        )
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): adapter},
            content_resolver=lambda candidate: candidate.content_ref,
            attempt_timeout_seconds=0.01,
            cancellation_grace_seconds=0.01,
        )

        await pump.start()
        await asyncio.wait_for(cancellation_seen.wait(), timeout=1)
        with pytest.raises(DeliveryAdapterTerminationError, match="Pump halted"):
            await asyncio.wait_for(pump.wait_idle(), timeout=1)

        pump.wake()
        await asyncio.sleep(0.02)
        assert not second_started.is_set()
        assert [
            item.state for item in repository.list_delivery_items(first.delivery_id)
        ] == ["unknown"]
        assert [
            item.state for item in repository.list_delivery_items(second.delivery_id)
        ] == ["queued"]

        release_lingering.set()
        await asyncio.wait_for(lingering_done.wait(), timeout=1)
        with pytest.raises(DeliveryAdapterTerminationError, match="Pump halted"):
            await pump.close()


@pytest.mark.asyncio
async def test_delivery_pump_bounds_safe_retries_and_exhausts_the_item(tmp_path):
    attempt_numbers: list[int] = []

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        attempt_numbers.append(request.attempt_no)
        return DeliveryAdapterResult(
            DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE,
            error_code="provider_busy",
            retry_after_ms=0,
        )

    with ControlStateRepository(tmp_path) as repository:
        delivery = _terminal_delivery(
            repository,
            request_id="a-retry",
            destination_id="chat-7",
        )
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): adapter},
            content_resolver=lambda candidate: candidate.content_ref,
            max_attempts=3,
            retry_base_ms=1,
            retry_max_ms=4,
        )

        await pump.start()
        await asyncio.wait_for(pump.wait_idle(), timeout=1)
        await pump.close()

        item = repository.list_delivery_items(delivery.delivery_id)[0]
        assert attempt_numbers == [1, 2, 3]
        assert item.attempt_count == 3
        assert item.state == "failed"
        assert item.next_attempt_at_ms is None
        assert item.last_error_code == "delivery_attempts_exhausted"


@pytest.mark.asyncio
async def test_delivery_pump_uses_provider_hint_then_exponential_backoff(tmp_path):
    now = [1_000]
    hints = [150, 0]

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        return DeliveryAdapterResult(
            DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE,
            retry_after_ms=hints[request.attempt_no - 1],
        )

    async def wait_for_retry(repository, delivery_id: str, attempt_count: int):
        while True:
            item = repository.list_delivery_items(delivery_id)[0]
            if item.state == "retry_wait" and item.attempt_count == attempt_count:
                return item
            await asyncio.sleep(0)

    with ControlStateRepository(tmp_path, clock_ms=lambda: now[0]) as repository:
        delivery = _terminal_delivery(
            repository,
            request_id="a-backoff",
            destination_id="chat-7",
        )
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): adapter},
            content_resolver=lambda candidate: candidate.content_ref,
            max_attempts=3,
            retry_base_ms=100,
            retry_max_ms=1_000,
            retry_jitter_ms=lambda _window: 0,
            clock_ms=lambda: now[0],
        )

        await pump.start()
        first = await asyncio.wait_for(
            wait_for_retry(repository, delivery.delivery_id, 1),
            timeout=1,
        )
        assert first.next_attempt_at_ms == 1_150

        now[0] = 1_150
        pump.wake()
        second = await asyncio.wait_for(
            wait_for_retry(repository, delivery.delivery_id, 2),
            timeout=1,
        )
        assert second.next_attempt_at_ms == 1_350

        await pump.close()


@pytest.mark.asyncio
async def test_provider_retry_hint_is_not_shortened_by_local_backoff_cap(tmp_path):
    now = [1_000]

    async def adapter(_request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        return DeliveryAdapterResult(
            DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE,
            retry_after_ms=3_600_000,
        )

    with ControlStateRepository(tmp_path, clock_ms=lambda: now[0]) as repository:
        delivery = _terminal_delivery(
            repository,
            request_id="a-long-provider-hint",
            destination_id="chat-7",
        )
        pump = DeliveryPump(
            repository,
            adapters={("telegram", "primary"): adapter},
            content_resolver=lambda candidate: candidate.content_ref,
            retry_base_ms=100,
            retry_max_ms=1_000,
            retry_hint_max_ms=7 * 24 * 60 * 60 * 1_000,
            retry_jitter_ms=lambda _window: 0,
            clock_ms=lambda: now[0],
        )

        await pump.start()
        while True:
            item = repository.list_delivery_items(delivery.delivery_id)[0]
            if item.state == "retry_wait":
                break
            await asyncio.sleep(0)
        assert item.next_attempt_at_ms == 3_601_000
        await pump.close()


@pytest.mark.asyncio
async def test_retry_wait_survives_restart_and_resumes_same_delivery_attempt_series(
    tmp_path,
):
    now = [1_000]
    attempt_numbers: list[int] = []

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        attempt_numbers.append(request.attempt_no)
        if request.attempt_no == 1:
            return DeliveryAdapterResult(
                DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE,
                error_code="provider_busy",
                retry_after_ms=100,
            )
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    repository = ControlStateRepository(tmp_path, clock_ms=lambda: now[0])
    delivery = _terminal_delivery(
        repository,
        request_id="a-restart-retry",
        destination_id="chat-7",
    )
    first_pump = DeliveryPump(
        repository,
        adapters={("telegram", "primary"): adapter},
        content_resolver=lambda candidate: candidate.content_ref,
        retry_base_ms=50,
        retry_max_ms=1_000,
        retry_jitter_ms=lambda _window: 0,
        clock_ms=lambda: now[0],
    )

    await first_pump.start()
    while True:
        item = repository.list_delivery_items(delivery.delivery_id)[0]
        if item.state == "retry_wait":
            break
        await asyncio.sleep(0)
    assert item.next_attempt_at_ms == 1_100
    await first_pump.close()
    repository.close()

    now[0] = 1_100
    with ControlStateRepository(tmp_path, clock_ms=lambda: now[0]) as reopened:
        second_pump = DeliveryPump(
            reopened,
            adapters={("telegram", "primary"): adapter},
            content_resolver=lambda candidate: candidate.content_ref,
            retry_base_ms=50,
            retry_max_ms=1_000,
            retry_jitter_ms=lambda _window: 0,
            clock_ms=lambda: now[0],
        )
        await second_pump.start()
        await second_pump.wait_idle()
        await second_pump.close()

        recovered_item = reopened.list_delivery_items(delivery.delivery_id)[0]
        assert attempt_numbers == [1, 2]
        assert recovered_item.state == "delivered"
        assert recovered_item.attempt_count == 2
