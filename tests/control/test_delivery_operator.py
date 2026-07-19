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

import pytest

from omicsclaw.control import (
    ControlRuntime,
    ControlRuntimePorts,
    DeliveryAdapterResult,
    DeliveryAttemptOutcome,
    DeliveryAttemptRequest,
    RawContentBlockV1,
    RawInboundV1,
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
        if len(calls) == 1:
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

        # The first Delivery is still in flight (one outstanding unit), so a
        # resend cannot reserve a second unit against the bound of one.
        blocked = runtime.resend_delivery(terminal.delivery_id)
        assert blocked.code == "delivery_backpressure"
        assert blocked.delivery is None

        gate.set()
        await runtime.wait_delivery_idle()
        assert runtime.describe_delivery(terminal.delivery_id).state == "delivered"

        # With the unit released, the explicit resend is admitted and delivered.
        allowed = runtime.resend_delivery(terminal.delivery_id)
        assert allowed.code == "resent"
        await runtime.wait_delivery_idle()
        assert calls == ["only-one", "only-one"]
    finally:
        gate.set()
        await runtime.close()
