"""ControlRuntime operator surface for ADR 0060 resend/retry/inspect.

These exercise the runtime wrappers end to end against a real channel
``ControlRuntime`` and Delivery Pump: an explicit Owner resend re-delivers the
frozen terminal reply through a new ``purpose=resend`` Delivery *without*
re-entering ``dispatch()`` or the Agent, honours the configured outstanding
capacity bound, and the read/retry surfaces behave for missing or delivered
Deliveries.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from omicsclaw.control import (
    ControlRuntime,
    ControlRuntimePorts,
    DeliveryAdapterResult,
    DeliveryAttemptOutcome,
    DeliveryAttemptRequest,
    DeliveryItemPlan,
    DeliveryPlan,
    RawContentBlockV1,
    RawInboundV1,
    TurnAcceptanceIntent,
    TurnTranscriptRef,
)
from omicsclaw.runtime.agent.envelope import MessageEnvelope
from omicsclaw.runtime.agent.events import Final


def _channel_raw(request_id: str, *, text: str = "hello") -> RawInboundV1:
    return RawInboundV1(
        schema_version=1,
        surface="channel",
        source_namespace="channel/telegram/v1/primary",
        source_request_id=request_id,
        external_subject={"kind": "telegram_user", "value": "42"},
        reply_target={
            "schema_version": 1,
            "kind": "channel",
            "adapter": "telegram",
            "account_namespace": "primary",
            "destination_id": "7001",
        },
        content=(RawContentBlockV1(kind="text", text=text),),
    )


def _telegram_runtime(tmp_path, *, adapter, dispatch_events, **overrides):
    return ControlRuntime.for_channel_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        adapter="telegram",
        account_namespace="primary",
        owner_identities={"channel/telegram/primary/telegram_user": frozenset({"42"})},
        delivery_adapter=adapter,
        dispatch_events=dispatch_events,
        **overrides,
    )


def _operator_delivery(runtime: ControlRuntime, request_id: str):
    repository = runtime.repository
    accepted = repository.accept_turn(
        TurnAcceptanceIntent(
            surface="channel",
            source_namespace="channel/telegram/v1/primary",
            source_request_id=request_id,
            fingerprint_version=1,
            fingerprint_sha256="f" * 64,
            reply_target={
                "kind": "channel",
                "adapter": "telegram",
                "account_namespace": "primary",
                "destination_id": "operator-chat",
            },
            new_conversation=True,
        )
    )
    repository.start_turn(accepted.turn_id)
    transcript_ref = TurnTranscriptRef(
        hashlib.sha256(request_id.encode()).hexdigest()[:32], "c" * 64
    )
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
                    content_sha256="c" * 64,
                ),
            ),
        ),
    )
    assert terminal.delivery is not None
    return terminal.delivery


@pytest.mark.asyncio
async def test_resend_delivery_redelivers_without_rerunning_turn(tmp_path):
    attempts: list[str] = []
    dispatch_count = 0

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        attempts.append(request.text)
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("resend me")

    runtime = _telegram_runtime(
        tmp_path, adapter=adapter, dispatch_events=dispatch_events
    )
    await runtime.start()
    try:
        result = await runtime.submit_and_wait(
            _channel_raw("7001:1", text="go"),
            ControlRuntimePorts(user_id="42"),
        )
        await runtime.wait_delivery_idle()
        turn_id = result.acceptance.turn_id
        (terminal,) = runtime.list_deliveries(turn_id=turn_id)
        assert attempts == ["resend me"]
        assert dispatch_count == 1

        outcome = runtime.resend_delivery(terminal.delivery_id)

        assert outcome.code == "resent"
        assert outcome.delivery is not None
        assert outcome.delivery.purpose == "resend"
        assert outcome.delivery.resend_of_delivery_id == terminal.delivery_id
        await runtime.wait_delivery_idle()

        # The reply is delivered a second time, but the Turn/Agent never reruns.
        assert attempts == ["resend me", "resend me"]
        assert dispatch_count == 1
        deliveries = runtime.list_deliveries(turn_id=turn_id)
        assert len(deliveries) == 2
        assert runtime.describe_delivery(terminal.delivery_id).state == "delivered"
        assert (
            runtime.describe_delivery(outcome.delivery.delivery_id).state == "delivered"
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_resend_and_retry_unknown_delivery_report_not_found(tmp_path):
    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("hi")

    runtime = _telegram_runtime(
        tmp_path, adapter=adapter, dispatch_events=dispatch_events
    )
    await runtime.start()
    try:
        assert runtime.resend_delivery("missing").code == "delivery_not_found"
        assert runtime.retry_delivery("missing").code == "delivery_not_found"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_delivery_operations_require_complete_delivery_authority(tmp_path):
    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="installation-test",
        profile_id="profile-test",
    )
    try:
        resend_source = _operator_delivery(runtime, "resend-source")
        resend_item = runtime.repository.list_delivery_items(
            resend_source.delivery_id
        )[0]
        resend_attempt = runtime.repository.begin_delivery_attempt(
            resend_item.item_id
        )
        runtime.repository.finish_delivery_attempt(
            resend_attempt.attempt_id,
            DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN,
        )

        retry_source = _operator_delivery(runtime, "retry-source")
        retry_item = runtime.repository.list_delivery_items(retry_source.delivery_id)[0]
        retry_attempt = runtime.repository.begin_delivery_attempt(retry_item.item_id)
        runtime.repository.finish_delivery_attempt(
            retry_attempt.attempt_id,
            DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE,
            retry_at_ms=9_999_999_999_999,
        )
        delivery_ids_before = tuple(
            delivery.delivery_id for delivery in runtime.list_deliveries()
        )

        resend = runtime.resend_delivery(resend_source.delivery_id)
        retry = runtime.retry_delivery(retry_source.delivery_id)

        assert resend.code == "delivery_unavailable"
        assert resend.delivery is None
        assert retry.code == "delivery_unavailable"
        assert retry.rearmed_items == 0
        assert tuple(
            delivery.delivery_id for delivery in runtime.list_deliveries()
        ) == delivery_ids_before
        unchanged_retry = runtime.repository.list_delivery_items(
            retry_source.delivery_id
        )[0]
        assert unchanged_retry.state == "retry_wait"
        assert unchanged_retry.next_attempt_at_ms == 9_999_999_999_999
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_delivery_mutations_are_unavailable_before_runtime_start(tmp_path):
    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("unused")

    runtime = _telegram_runtime(
        tmp_path, adapter=adapter, dispatch_events=dispatch_events
    )
    try:
        resend_source = _operator_delivery(runtime, "before-start-resend")
        resend_item = runtime.repository.list_delivery_items(
            resend_source.delivery_id
        )[0]
        resend_attempt = runtime.repository.begin_delivery_attempt(
            resend_item.item_id
        )
        runtime.repository.finish_delivery_attempt(
            resend_attempt.attempt_id,
            DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN,
        )

        retry_source = _operator_delivery(runtime, "before-start-retry")
        retry_item = runtime.repository.list_delivery_items(retry_source.delivery_id)[0]
        retry_attempt = runtime.repository.begin_delivery_attempt(retry_item.item_id)
        runtime.repository.finish_delivery_attempt(
            retry_attempt.attempt_id,
            DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE,
            retry_at_ms=9_999_999_999_999,
        )
        delivery_ids_before = tuple(
            delivery.delivery_id for delivery in runtime.list_deliveries()
        )

        resend = runtime.resend_delivery(resend_source.delivery_id)
        retry = runtime.retry_delivery(retry_source.delivery_id)

        assert resend.code == "delivery_unavailable"
        assert retry.code == "delivery_unavailable"
        assert tuple(
            delivery.delivery_id for delivery in runtime.list_deliveries()
        ) == delivery_ids_before
        unchanged_retry = runtime.repository.list_delivery_items(
            retry_source.delivery_id
        )[0]
        assert unchanged_retry.state == "retry_wait"
        assert unchanged_retry.next_attempt_at_ms == 9_999_999_999_999
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_delivery_mutations_are_unavailable_after_runtime_close(
    tmp_path, monkeypatch
):
    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("unused")

    runtime = _telegram_runtime(
        tmp_path, adapter=adapter, dispatch_events=dispatch_events
    )
    await runtime.start()
    await runtime.close()
    repository_calls: list[str] = []

    def repository_was_touched(*_args, **_kwargs):
        repository_calls.append("touched")
        raise AssertionError("closed repository was touched")

    monkeypatch.setattr(
        runtime.repository, "insert_resend_delivery", repository_was_touched
    )
    monkeypatch.setattr(
        runtime.repository, "expedite_delivery_retries", repository_was_touched
    )

    resend = runtime.resend_delivery("closed-resend")
    retry = runtime.retry_delivery("closed-retry")

    assert resend.code == "delivery_unavailable"
    assert retry.code == "delivery_unavailable"
    assert repository_calls == []


@pytest.mark.asyncio
async def test_retry_delivery_on_delivered_reports_no_retryable_items(tmp_path):
    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("done")

    runtime = _telegram_runtime(
        tmp_path, adapter=adapter, dispatch_events=dispatch_events
    )
    await runtime.start()
    try:
        result = await runtime.submit_and_wait(
            _channel_raw("7001:2", text="go"),
            ControlRuntimePorts(user_id="42"),
        )
        await runtime.wait_delivery_idle()
        (terminal,) = runtime.list_deliveries(turn_id=result.acceptance.turn_id)

        outcome = runtime.retry_delivery(terminal.delivery_id)

        assert outcome.code == "no_retryable_items"
        assert outcome.rearmed_items == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_resend_delivery_honours_outstanding_capacity_bound(tmp_path):
    calls: list[str] = []
    first_entered = asyncio.Event()
    gate = asyncio.Event()

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        calls.append(request.text)
        # Every call parks on the gate, so the test can hold exactly one
        # outstanding delivery unit at whichever point it needs to.
        first_entered.set()
        await gate.wait()
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("only-one")

    runtime = _telegram_runtime(
        tmp_path,
        adapter=adapter,
        dispatch_events=dispatch_events,
        max_outstanding_deliveries_total=1,
        max_outstanding_deliveries_per_account=1,
    )
    await runtime.start()
    try:
        result = await runtime.submit_and_wait(
            _channel_raw("7001:3", text="go"),
            ControlRuntimePorts(user_id="42"),
        )
        turn_id = result.acceptance.turn_id
        await first_entered.wait()
        (terminal,) = runtime.list_deliveries(turn_id=turn_id)

        # Let the first Delivery settle so the capacity bound -- not the
        # source-settlement rule -- is what the resend runs into below.
        gate.set()
        await runtime.wait_delivery_idle()
        assert runtime.describe_delivery(terminal.delivery_id).state == "delivered"

        # A second Turn now holds the single outstanding unit in its provider
        # call, so an otherwise-admissible resend of the settled first Delivery
        # cannot reserve one.
        gate.clear()
        first_entered.clear()
        second = asyncio.ensure_future(
            runtime.submit_and_wait(
                _channel_raw("7001:4", text="go again"),
                ControlRuntimePorts(user_id="42"),
            )
        )
        await first_entered.wait()

        blocked = runtime.resend_delivery(terminal.delivery_id)
        assert blocked.code == "delivery_backpressure"
        assert blocked.delivery is None

        gate.set()
        await second
        await runtime.wait_delivery_idle()

        # With the unit released, the explicit resend is admitted and delivered.
        allowed = runtime.resend_delivery(terminal.delivery_id)
        assert allowed.code == "resent"
        await runtime.wait_delivery_idle()
        assert calls == ["only-one", "only-one", "only-one"]
    finally:
        gate.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_resend_delivery_refuses_a_source_that_is_still_in_flight(tmp_path):
    """ADR 0060 scopes resend to a settled (`unknown`/`delivered`) outcome.

    Copying a Delivery the Pump may still deliver would show the Owner the same
    reply twice, with no record that the duplicate was intentional.
    """

    entered = asyncio.Event()
    gate = asyncio.Event()

    async def adapter(request: DeliveryAttemptRequest) -> DeliveryAdapterResult:
        entered.set()
        await gate.wait()
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("in-flight")

    runtime = _telegram_runtime(
        tmp_path, adapter=adapter, dispatch_events=dispatch_events
    )
    await runtime.start()
    try:
        result = await runtime.submit_and_wait(
            _channel_raw("7001:9", text="go"),
            ControlRuntimePorts(user_id="42"),
        )
        (terminal,) = runtime.list_deliveries(turn_id=result.acceptance.turn_id)
        await entered.wait()

        refused = runtime.resend_delivery(terminal.delivery_id)

        assert refused.code == "delivery_not_settled"
        assert refused.delivery is None
        # The refusal creates nothing: the source stands alone.
        assert len(runtime.list_deliveries()) == 1
    finally:
        gate.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_describe_delivery_rolls_up_failed_and_unknown_outcomes(tmp_path):
    """The operator-facing summary state must surface a settled failure.

    ``describe_delivery`` is how an Owner decides between ``retry_delivery`` and
    ``resend_delivery``; the ``failed``/``unknown`` rollup is the one branch
    that governs that choice. Only the ``in_progress``/``delivered`` summaries
    were previously asserted, so a regression in the terminal-failure mapping
    would have been silent.
    """

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="installation-test",
        profile_id="profile-test",
    )
    try:
        # A permanently rejected head Item rolls the Delivery up to ``failed``.
        failed_source = _operator_delivery(runtime, "rollup-failed")
        failed_item = runtime.repository.list_delivery_items(
            failed_source.delivery_id
        )[0]
        failed_attempt = runtime.repository.begin_delivery_attempt(failed_item.item_id)
        runtime.repository.finish_delivery_attempt(
            failed_attempt.attempt_id,
            DeliveryAttemptOutcome.REJECTED_PERMANENT,
            error_code="provider_rejected",
        )
        assert runtime.describe_delivery(failed_source.delivery_id).state == "failed"

        # An acceptance-unknown head Item rolls up to ``unknown`` (the source
        # must be settled before the first is terminal, so seq 1 is done here).
        unknown_source = _operator_delivery(runtime, "rollup-unknown")
        unknown_item = runtime.repository.list_delivery_items(
            unknown_source.delivery_id
        )[0]
        unknown_attempt = runtime.repository.begin_delivery_attempt(
            unknown_item.item_id
        )
        runtime.repository.finish_delivery_attempt(
            unknown_attempt.attempt_id,
            DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN,
        )
        assert runtime.describe_delivery(unknown_source.delivery_id).state == "unknown"
    finally:
        await runtime.close()
