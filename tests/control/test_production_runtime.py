from __future__ import annotations

import asyncio
import os

import pytest

from omicsclaw.attachments import SourceAttachmentDescriptorV1
from omicsclaw.control import (
    ControlIntegrityError,
    ControlRuntime,
    ControlRuntimePorts,
    DeliveryAdapterResult,
    DeliveryAttemptOutcome,
    DeliveryAttemptRequest,
    RawContentBlockV1,
    RawInboundV1,
    TurnAcceptanceStatus,
    TurnTranscriptRef,
)
from omicsclaw.control.event_hub import TurnEventHub
from omicsclaw.control.repository import ControlStateRepository
from omicsclaw.runtime.agent.envelope import MessageEnvelope
from omicsclaw.runtime.agent.events import Error, Final, StreamContent
from omicsclaw.runtime.storage.canonical_transcript import (
    CanonicalTranscript,
    TranscriptIntegrityError,
)


def _raw(
    request_id: str, *, slot: str = "main", text: str | None = None
) -> RawInboundV1:
    return RawInboundV1(
        schema_version=1,
        surface="cli",
        source_namespace="cli/v1/local/owner",
        source_request_id=request_id,
        reply_target={
            "schema_version": 1,
            "kind": "cli",
            "installation_id": "local",
            "profile_id": "owner",
            "slot": slot,
        },
        content=(RawContentBlockV1(kind="text", text=text or request_id),),
    )


def _control_raw(request_id: str, *, slot: str = "main") -> RawInboundV1:
    return RawInboundV1(
        schema_version=1,
        surface="cli",
        source_namespace="cli/v1/local/owner",
        source_request_id=request_id,
        reply_target={
            "schema_version": 1,
            "kind": "cli",
            "installation_id": "local",
            "profile_id": "owner",
            "slot": slot,
        },
        content=(),
        project_command={"kind": "new_conversation"},
    )


def _desktop_raw(
    request_id: str,
    *,
    installation_id: str,
    profile_id: str = "owner",
) -> RawInboundV1:
    return RawInboundV1(
        schema_version=1,
        surface="desktop",
        source_namespace=f"desktop/v1/{installation_id}/{profile_id}",
        source_request_id=request_id,
        reply_target={
            "schema_version": 1,
            "kind": "desktop",
            "installation_id": installation_id,
            "profile_id": profile_id,
            "slot": "main",
        },
        content=(RawContentBlockV1(kind="text", text="hello"),),
    )


def _channel_raw(
    request_id: str,
    *,
    text: str = "hello",
    destination_id: str = "7001",
    attachments=(),
) -> RawInboundV1:
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
            "destination_id": destination_id,
        },
        content=(RawContentBlockV1(kind="text", text=text),),
        attachments=attachments,
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
async def test_channel_runtime_delivers_canonical_terminal_text_once(tmp_path):
    attempts: list[DeliveryAttemptRequest] = []
    dispatch_count = 0

    async def adapter(request: DeliveryAttemptRequest):
        attempts.append(request)
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("telegram answer")

    runtime = _telegram_runtime(
        tmp_path,
        adapter=adapter,
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        raw = _channel_raw("7001:11", text="run analysis")
        first = await runtime.submit_and_wait(raw, ControlRuntimePorts(user_id="42"))
        duplicate = await runtime.submit_and_wait(
            raw,
            ControlRuntimePorts(user_id="42"),
        )
        await runtime.wait_delivery_idle()

        assert first.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert first.receipt.status == "succeeded"
        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.acceptance.turn_id == first.acceptance.turn_id
        assert dispatch_count == 1
        assert len(attempts) == 1
        assert attempts[0].text == "telegram answer"
        assert attempts[0].reply_target["destination_id"] == "7001"
        deliveries = runtime.repository.list_deliveries(
            turn_id=first.acceptance.turn_id
        )
        assert len(deliveries) == 1
        assert [
            item.state
            for item in runtime.repository.list_delivery_items(
                deliveries[0].delivery_id
            )
        ] == ["delivered"]

        rejected = await runtime.submit_and_wait(
            _channel_raw(
                "7001:12",
                attachments=(
                    SourceAttachmentDescriptorV1(
                        schema_version=1,
                        ordinal=0,
                        source_attachment_id="not-staged",
                        display_name="photo.jpg",
                        declared_media_type="image/jpeg",
                        declared_size=4,
                        declared_sha256=None,
                    ),
                ),
            ),
            ControlRuntimePorts(user_id="42"),
        )
        assert rejected.acceptance.status is TurnAcceptanceStatus.REJECTED
        assert rejected.acceptance.code == "attachments_not_supported"
        assert len(attempts) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_channel_delivery_capacity_rejects_only_novel_ingress(tmp_path):
    attempt_started = asyncio.Event()
    release_attempt = asyncio.Event()
    dispatch_count = 0

    async def adapter(_request: DeliveryAttemptRequest):
        attempt_started.set()
        await release_attempt.wait()
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("done")

    runtime = _telegram_runtime(
        tmp_path,
        adapter=adapter,
        dispatch_events=dispatch_events,
        max_outstanding_deliveries_total=1,
        max_outstanding_deliveries_per_account=1,
    )
    await runtime.start()
    try:
        first_raw = _channel_raw("7001:21")
        first = await runtime.submit_and_wait(
            first_raw,
            ControlRuntimePorts(user_id="42"),
        )
        await asyncio.wait_for(attempt_started.wait(), timeout=1)

        duplicate = await runtime.submit_and_wait(
            first_raw,
            ControlRuntimePorts(user_id="42"),
        )
        novel = await runtime.submit_and_wait(
            _channel_raw("7001:22"),
            ControlRuntimePorts(user_id="42"),
        )

        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.acceptance.turn_id == first.acceptance.turn_id
        assert novel.acceptance.status is TurnAcceptanceStatus.REJECTED
        assert novel.acceptance.code == "delivery_backpressure"
        assert dispatch_count == 1
    finally:
        release_attempt.set()
        await runtime.wait_delivery_idle()
        await runtime.close()


@pytest.mark.asyncio
async def test_concurrent_channel_admission_cannot_oversubscribe_delivery_capacity(
    tmp_path,
):
    release_attempt = asyncio.Event()
    dispatch_count = 0

    async def adapter(_request: DeliveryAttemptRequest):
        await release_attempt.wait()
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("done")

    runtime = _telegram_runtime(
        tmp_path,
        adapter=adapter,
        dispatch_events=dispatch_events,
        max_outstanding_deliveries_total=1,
        max_outstanding_deliveries_per_account=1,
    )
    await runtime.start()
    try:
        results = await asyncio.gather(
            runtime.submit_and_wait(
                _channel_raw("7001:23"),
                ControlRuntimePorts(user_id="42"),
            ),
            runtime.submit_and_wait(
                _channel_raw("7001:24"),
                ControlRuntimePorts(user_id="42"),
            ),
        )

        accepted = [
            result
            for result in results
            if result.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        ]
        rejected = [
            result
            for result in results
            if result.acceptance.status is TurnAcceptanceStatus.REJECTED
        ]
        assert len(accepted) == 1
        assert len(rejected) == 1
        assert rejected[0].acceptance.code == "delivery_backpressure"
        assert dispatch_count == 1
    finally:
        release_attempt.set()
        await runtime.wait_delivery_idle()
        await runtime.close()


@pytest.mark.asyncio
async def test_channel_startup_interrupts_without_worker_replay_and_delivers(tmp_path):
    attempts: list[DeliveryAttemptRequest] = []
    dispatch_count = 0

    async def adapter(request: DeliveryAttemptRequest):
        attempts.append(request)
        return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("must not run")

    first_runtime = _telegram_runtime(
        tmp_path,
        adapter=adapter,
        dispatch_events=dispatch_events,
    )
    await first_runtime.start()
    accepted = first_runtime._normalizer.accept(_channel_raw("7001:31"))
    assert accepted.status is TurnAcceptanceStatus.ACCEPTED
    await first_runtime.close()

    recovered = _telegram_runtime(
        tmp_path,
        adapter=adapter,
        dispatch_events=dispatch_events,
    )
    startup = await recovered.start()
    try:
        await recovered.wait_delivery_idle()

        assert startup.interrupted_turn_ids == (accepted.turn_id,)
        assert recovered.get_receipt(accepted.turn_id).status == "interrupted"
        assert dispatch_count == 0
        assert [attempt.text for attempt in attempts] == [
            "Turn interrupted by control-plane restart."
        ]
        delivery = recovered.repository.list_deliveries(turn_id=accepted.turn_id)[0]
        assert [
            item.state
            for item in recovered.repository.list_delivery_items(delivery.delivery_id)
        ] == ["delivered"]
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_desktop_runtime_accepts_opaque_installation_under_fixed_owner_profile(
    tmp_path,
):
    dispatch_count = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        accepted = await runtime.submit_and_wait(
            _desktop_raw("a" * 32, installation_id="3" * 32),
            ControlRuntimePorts(),
        )
        rejected = await runtime.submit_and_wait(
            _desktop_raw(
                "b" * 32,
                installation_id="4" * 32,
                profile_id="another-owner",
            ),
            ControlRuntimePorts(),
        )

        assert accepted.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert accepted.receipt.status == "succeeded"
        assert rejected.acceptance.status is TurnAcceptanceStatus.REJECTED
        assert rejected.acceptance.code == "owner_denied"
        assert dispatch_count == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_control_runtime_executes_cli_turn_through_durable_control_state(
    tmp_path,
):
    dispatched: list[MessageEnvelope] = []
    observed: list[object] = []

    async def dispatch_events(envelope: MessageEnvelope):
        dispatched.append(envelope)
        yield StreamContent("hello")
        yield Final("hello")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        result = await runtime.submit_and_wait(
            _raw("request-1", text="run a demo"),
            ControlRuntimePorts(response_sink=observed.append),
        )

        assert result.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert result.receipt.status == "succeeded"
        assert result.receipt.turn_id == result.acceptance.turn_id
        assert len(dispatched) == 1
        assert dispatched[0].chat_id == result.acceptance.conversation_id
        assert dispatched[0].content == "run a demo"
        assert dispatched[0].workspace == "workspace-test"
        assert [type(event) for event in observed] == [StreamContent, Final]
        terminal_ref = runtime.repository.get_turn_terminal_ref(
            result.acceptance.turn_id
        )
        assert terminal_ref is not None
        assert runtime.transcript.get_entry(terminal_ref.entry_id).commit_state == (
            "committed"
        )
        assert runtime.transcript.get_history(result.acceptance.conversation_id) == [
            {"role": "assistant", "content": "hello"}
        ]
        assert (
            runtime.repository.get_turn(result.acceptance.turn_id).status == "succeeded"
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_terminal_event_observer_sees_committed_receipt_and_transcript(tmp_path):
    terminal_observation = []

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        turn_id = ""

        def observe(event):
            if not isinstance(event, Final):
                return
            receipt = runtime.repository.get_turn(turn_id)
            ref = runtime.repository.get_turn_terminal_ref(turn_id)
            terminal_observation.append(
                (
                    receipt.status,
                    runtime.transcript.get_entry(ref.entry_id).commit_state,
                    event.text,
                )
            )

        def accepted(value: str) -> None:
            nonlocal turn_id
            turn_id = value

        result = await runtime.submit_and_wait(
            _raw("ordered-terminal"),
            ControlRuntimePorts(response_sink=observe),
            on_accepted=accepted,
        )

        assert result.receipt.status == "succeeded"
        assert terminal_observation == [("succeeded", "committed", "done")]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_response_sink_failure_detaches_without_failing_turn(tmp_path):
    async def dispatch_events(_envelope: MessageEnvelope):
        yield StreamContent("partial")
        yield Final("done")

    def broken_sink(_event) -> None:
        raise RuntimeError("renderer disconnected")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        result = await runtime.submit_and_wait(
            _raw("broken-observer"),
            ControlRuntimePorts(response_sink=broken_sink),
        )

        assert result.receipt.status == "succeeded"
        ref = runtime.repository.get_turn_terminal_ref(result.acceptance.turn_id)
        assert runtime.transcript.get_entry(ref.entry_id).commit_state == "committed"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_renderer_self_cancel_does_not_cancel_turn_or_submitter(tmp_path):
    async def dispatch_events(_envelope: MessageEnvelope):
        yield StreamContent("partial")
        yield Final("done")

    async def self_canceling_sink(_event) -> None:
        raise asyncio.CancelledError("renderer self-cancel")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    result = await runtime.submit_and_wait(
        _raw("renderer-self-cancel"),
        ControlRuntimePorts(response_sink=self_canceling_sink),
    )
    assert result.receipt.status == "succeeded"
    await runtime.close()


@pytest.mark.asyncio
async def test_snapshot_renderer_self_cancel_does_not_cancel_submitter(tmp_path):
    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("done")

    async def self_canceling_sink(_event) -> None:
        raise asyncio.CancelledError("snapshot renderer self-cancel")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    runtime._event_hub = TurnEventHub(max_turns=1)
    runtime._event_hub.open_turn("occupied-live-stream")
    await runtime.start()
    result = await runtime.submit_and_wait(
        _raw("snapshot-renderer-self-cancel"),
        ControlRuntimePorts(response_sink=self_canceling_sink),
    )
    assert result.receipt.status == "succeeded"
    await runtime.close()


@pytest.mark.asyncio
async def test_deferred_provider_payload_cannot_poison_failed_terminal(tmp_path):
    async def dispatch_events(envelope: MessageEnvelope):
        envelope.transcript_turn.defer_terminal_message(
            envelope.chat_id,
            content="poison model history",
        )
        yield Error(RuntimeError("provider failed after defer"))

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    result = await runtime.submit_and_wait(
        _raw("defer-then-error"),
        ControlRuntimePorts(),
    )
    ref = runtime.repository.get_turn_terminal_ref(result.acceptance.turn_id)
    entry = runtime.transcript.get_entry(ref.entry_id)
    assert result.receipt.status == "failed"
    assert entry.public_text == "Turn failed."
    assert entry.payload["provider_message"] == {
        "role": "assistant",
        "content": "Turn failed.",
    }
    await runtime.close()

    restarted = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    await restarted.start()
    await restarted.close()


@pytest.mark.asyncio
async def test_duplicate_cli_submission_observes_original_without_reexecution(tmp_path):
    dispatch_count = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        first = await runtime.submit_and_wait(
            _raw("same-request"),
            ControlRuntimePorts(),
        )
        duplicate = await runtime.submit_and_wait(
            _raw("same-request"),
            ControlRuntimePorts(),
        )

        assert first.receipt.status == "succeeded"
        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.acceptance.turn_id == first.acceptance.turn_id
        assert duplicate.receipt.status == "succeeded"
        assert dispatch_count == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_live_duplicate_with_evicted_history_gets_terminal_snapshot(tmp_path):
    dispatch_count = 0
    early_events_published = asyncio.Event()
    release_terminal = asyncio.Event()
    observed: list[object] = []

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield StreamContent("a")
        yield StreamContent("b")
        yield StreamContent("c")
        early_events_published.set()
        await release_terminal.wait()
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    runtime._event_hub = TurnEventHub(max_events_per_turn=2)
    await runtime.start()
    try:
        original_task = asyncio.create_task(
            runtime.submit_and_wait(
                _raw("evicted-live-duplicate"),
                ControlRuntimePorts(),
            )
        )
        await early_events_published.wait()
        duplicate_task = asyncio.create_task(
            runtime.submit_and_wait(
                _raw("evicted-live-duplicate"),
                ControlRuntimePorts(response_sink=observed.append),
            )
        )
        await asyncio.sleep(0)
        release_terminal.set()
        original, duplicate = await asyncio.gather(original_task, duplicate_task)

        assert original.receipt.status == "succeeded"
        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.receipt.status == "succeeded"
        assert dispatch_count == 1
        assert [type(event) for event in observed] == [Final]
        assert observed[0].text == "done"
    finally:
        release_terminal.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_completed_duplicate_with_evicted_history_gets_only_terminal_snapshot(
    tmp_path,
):
    observed: list[object] = []

    async def dispatch_events(_envelope: MessageEnvelope):
        yield StreamContent("a")
        yield StreamContent("b")
        yield StreamContent("c")
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    runtime._event_hub = TurnEventHub(max_events_per_turn=2)
    await runtime.start()
    try:
        original = await runtime.submit_and_wait(
            _raw("evicted-complete-duplicate"),
            ControlRuntimePorts(),
        )
        duplicate = await runtime.submit_and_wait(
            _raw("evicted-complete-duplicate"),
            ControlRuntimePorts(response_sink=observed.append),
        )

        assert original.receipt.status == "succeeded"
        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert [type(event) for event in observed] == [Final]
        assert observed[0].text == "done"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_hung_async_response_sink_cannot_hold_submission_or_close_open(tmp_path):
    sink_started = asyncio.Event()
    never = asyncio.Event()

    async def dispatch_events(_envelope: MessageEnvelope):
        yield StreamContent("partial")
        await sink_started.wait()
        yield StreamContent("overflow")
        yield Final("done")

    async def hung_sink(_event) -> None:
        sink_started.set()
        await never.wait()

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    runtime._event_hub = TurnEventHub(subscriber_queue_size=1)
    runtime._observer_drain_timeout_seconds = 0.05
    await runtime.start()
    task = asyncio.create_task(
        runtime.submit_and_wait(
            _raw("hung-renderer"),
            ControlRuntimePorts(response_sink=hung_sink),
        )
    )
    await sink_started.wait()
    result = await asyncio.wait_for(task, timeout=1)
    assert result.receipt.status == "succeeded"
    assert not runtime._observer_tasks
    await asyncio.wait_for(runtime.close(), timeout=1)


@pytest.mark.asyncio
async def test_fast_sink_gets_terminal_snapshot_after_synchronous_burst_detach(
    tmp_path,
):
    observed: list[object] = []

    async def dispatch_events(_envelope: MessageEnvelope):
        await asyncio.sleep(0)
        for index in range(100):
            yield StreamContent(str(index))
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    runtime._event_hub = TurnEventHub(subscriber_queue_size=64)
    await runtime.start()
    try:
        result = await runtime.submit_and_wait(
            _raw("burst-detach"),
            ControlRuntimePorts(response_sink=observed.append),
        )
        assert result.receipt.status == "succeeded"
        assert [type(event) for event in observed] == [Final]
        assert observed[0].text == "done"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_control_command_terminalizes_without_entering_agent_dispatch(tmp_path):
    dispatch_count = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("must not run")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        result = await runtime.submit_and_wait(
            RawInboundV1(
                schema_version=1,
                surface="cli",
                source_namespace="cli/v1/local/owner",
                source_request_id="new-conversation",
                reply_target={
                    "schema_version": 1,
                    "kind": "cli",
                    "installation_id": "local",
                    "profile_id": "owner",
                    "slot": "main",
                },
                content=(),
                project_command={"kind": "new_conversation"},
            ),
            ControlRuntimePorts(),
        )

        assert result.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert result.receipt.status == "succeeded"
        assert dispatch_count == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_control_command_startup_interruption_stays_hidden_across_restarts(
    tmp_path,
):
    first = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    await first.start()
    accepted = first._normalizer.accept(_control_raw("queued-control-restart"))
    await first.close()

    second = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    result = await second.start()
    assert result.interrupted_turn_ids == (accepted.turn_id,)
    receipt = second.repository.get_turn(accepted.turn_id)
    ref = second.repository.get_turn_terminal_ref(accepted.turn_id)
    entry = second.transcript.get_entry(ref.entry_id)
    assert receipt.status == "interrupted"
    assert entry.payload["provider_message"] is None
    assert second.transcript.get_history(accepted.conversation_id) == []
    await second.close()

    third = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    repeated = await third.start()
    assert repeated.interrupted_turn_ids == ()
    await third.close()


@pytest.mark.asyncio
async def test_control_command_enqueue_failure_stays_hidden_on_restart(
    monkeypatch,
    tmp_path,
):
    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    await runtime.start()

    def fail_queue_commit(*_args, **_kwargs):
        raise RuntimeError("injected FIFO commit failure")

    monkeypatch.setattr(runtime._sequencer, "_commit", fail_queue_commit)
    result = await runtime.submit_and_wait(
        _control_raw("control-enqueue-failure"),
        ControlRuntimePorts(),
    )
    ref = runtime.repository.get_turn_terminal_ref(result.acceptance.turn_id)
    entry = runtime.transcript.get_entry(ref.entry_id)
    assert result.receipt.status == "failed"
    assert entry.payload["provider_message"] is None
    await runtime.close()

    restarted = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    await restarted.start()
    await restarted.close()


@pytest.mark.asyncio
async def test_new_conversation_command_moves_stable_reply_target_binding(tmp_path):
    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        before = await runtime.submit_and_wait(_raw("before"), ControlRuntimePorts())
        moved = await runtime.submit_and_wait(
            RawInboundV1(
                schema_version=1,
                surface="cli",
                source_namespace="cli/v1/local/owner",
                source_request_id="move-binding",
                reply_target={
                    "schema_version": 1,
                    "kind": "cli",
                    "installation_id": "local",
                    "profile_id": "owner",
                    "slot": "main",
                },
                content=(),
                project_command={"kind": "new_conversation"},
            ),
            ControlRuntimePorts(),
        )
        after = await runtime.submit_and_wait(_raw("after"), ControlRuntimePorts())

        assert moved.acceptance.conversation_id != before.acceptance.conversation_id
        assert after.acceptance.conversation_id == moved.acceptance.conversation_id
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_control_runtime_serializes_two_live_turns_in_one_conversation(tmp_path):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def dispatch_events(envelope: MessageEnvelope):
        text = str(envelope.content)
        order.append(f"start:{text}")
        if text == "first":
            first_started.set()
            await release_first.wait()
        order.append(f"finish:{text}")
        yield Final(text)

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        first_task = asyncio.create_task(
            runtime.submit_and_wait(_raw("r1", text="first"), ControlRuntimePorts())
        )
        await first_started.wait()
        second_task = asyncio.create_task(
            runtime.submit_and_wait(_raw("r2", text="second"), ControlRuntimePorts())
        )
        await asyncio.sleep(0)

        assert order == ["start:first"]
        release_first.set()
        first, second = await asyncio.gather(first_task, second_task)

        assert first.receipt.status == "succeeded"
        assert second.receipt.status == "succeeded"
        assert first.acceptance.conversation_id == second.acceptance.conversation_id
        assert order == [
            "start:first",
            "finish:first",
            "start:second",
            "finish:second",
        ]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_control_runtime_cancels_active_turn_by_opaque_turn_id(tmp_path):
    accepted_turn_id = ""
    worker_started = asyncio.Event()

    async def dispatch_events(envelope: MessageEnvelope):
        worker_started.set()
        while not envelope.cancel_event.is_set():
            await asyncio.sleep(0)
        yield Final("must not become success")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:

        def remember_turn(turn_id: str) -> None:
            nonlocal accepted_turn_id
            accepted_turn_id = turn_id

        task = asyncio.create_task(
            runtime.submit_and_wait(
                _raw("cancel-me"),
                ControlRuntimePorts(),
                on_accepted=remember_turn,
            )
        )
        await worker_started.wait()

        canceled = runtime.cancel(accepted_turn_id)
        result = await task

        assert canceled.changed is True
        assert canceled.code == "cancel_requested"
        assert result.receipt.status == "canceled"
        assert result.receipt.terminal_code == "canceled_by_owner"
        terminal_ref = runtime.repository.get_turn_terminal_ref(accepted_turn_id)
        terminal_entry = runtime.transcript.get_entry(terminal_ref.entry_id)
        assert terminal_entry.commit_state == "committed"
        assert terminal_entry.public_text == "Turn canceled."
        assert runtime.transcript.get_history(result.acceptance.conversation_id)[
            -1
        ] == {"role": "assistant", "content": "Turn canceled."}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_control_runtime_cancels_waiting_turn_with_terminal_transcript(tmp_path):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_turn_id = ""

    async def dispatch_events(envelope: MessageEnvelope):
        if envelope.content == "first":
            first_started.set()
            await release_first.wait()
        yield Final(str(envelope.content))

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        first_task = asyncio.create_task(
            runtime.submit_and_wait(
                _raw("waiting-first", text="first"),
                ControlRuntimePorts(),
            )
        )
        await first_started.wait()

        def remember_second(turn_id: str) -> None:
            nonlocal second_turn_id
            second_turn_id = turn_id

        second_task = asyncio.create_task(
            runtime.submit_and_wait(
                _raw("waiting-second", text="second"),
                ControlRuntimePorts(),
                on_accepted=remember_second,
            )
        )
        while not second_turn_id:
            await asyncio.sleep(0)

        canceled = runtime.cancel(second_turn_id)
        second = await second_task
        release_first.set()
        first = await first_task

        assert canceled.code == "canceled_waiting"
        assert second.receipt.status == "canceled"
        assert first.receipt.status == "succeeded"
        ref = runtime.repository.get_turn_terminal_ref(second_turn_id)
        assert runtime.transcript.get_entry(ref.entry_id).public_text == (
            "Turn canceled."
        )
    finally:
        release_first.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_runner_integrity_failure_unblocks_submitter_instead_of_hanging(
    monkeypatch,
    tmp_path,
):
    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()

    def fail_terminalization(*_args, **_kwargs):
        raise RuntimeError("injected terminal persistence failure")

    monkeypatch.setattr(
        runtime.repository,
        "terminalize_turn",
        fail_terminalization,
    )
    with pytest.raises(
        RuntimeError,
        match="runner failed before terminal Receipt",
    ):
        await asyncio.wait_for(
            runtime.submit_and_wait(
                _raw("terminalization-fails"),
                ControlRuntimePorts(),
            ),
            timeout=1,
        )

    with pytest.raises(
        RuntimeError,
        match="injected terminal persistence failure",
    ):
        await runtime.close()


@pytest.mark.asyncio
async def test_startup_abandons_pre_receipt_candidate_then_commits_interruption(
    tmp_path,
):
    first = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    await first.start()
    acceptance = first._normalizer.accept(_raw("crash-before-receipt"))
    stale = first.transcript.bind_turn(
        acceptance.conversation_id,
        acceptance.turn_id,
    ).stage_terminal("uncommitted success")
    await first.close()

    second = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    result = await second.start()
    try:
        receipt = second.repository.get_turn(acceptance.turn_id)
        terminal_ref = second.repository.get_turn_terminal_ref(acceptance.turn_id)

        assert result.interrupted_turn_ids == (acceptance.turn_id,)
        assert receipt.status == "interrupted"
        assert receipt.terminal_code == "control_plane_restarted"
        assert second.transcript.get_entry(stale.entry_id).commit_state == "abandoned"
        terminal_entry = second.transcript.get_entry(terminal_ref.entry_id)
        assert terminal_entry.commit_state == "committed"
        assert terminal_entry.public_text == (
            "Turn interrupted by control-plane restart."
        )
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_startup_promotes_candidate_committed_with_terminal_receipt(tmp_path):
    first = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    await first.start()
    acceptance = first._normalizer.accept(_raw("crash-after-receipt"))
    first.repository.start_turn(acceptance.turn_id)
    candidate = first.transcript.bind_turn(
        acceptance.conversation_id,
        acceptance.turn_id,
    ).stage_terminal("durable answer")
    first.repository.terminalize_turn(
        acceptance.turn_id,
        terminal_status="succeeded",
        transcript_ref=TurnTranscriptRef(
            candidate.entry_id,
            candidate.content_sha256,
        ),
    )
    assert first.transcript.get_history(acceptance.conversation_id) == []
    await first.close()

    second = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    result = await second.start()
    try:
        assert result.interrupted_turn_ids == ()
        assert second.transcript.get_entry(candidate.entry_id).commit_state == (
            "committed"
        )
        assert second.transcript.get_history(acceptance.conversation_id) == [
            {"role": "assistant", "content": "durable answer"}
        ]
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_canceling_submit_observer_does_not_cancel_or_orphan_worker(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    turn_id = ""

    async def dispatch_events(_envelope: MessageEnvelope):
        started.set()
        await release.wait()
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    try:
        task = asyncio.create_task(
            runtime.submit_and_wait(
                _raw("observer-canceled"),
                ControlRuntimePorts(),
            )
        )
        while not runtime._live_turns:
            await asyncio.sleep(0)
        turn_id = next(iter(runtime._live_turns))
        await started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert turn_id in runtime._live_turns

        release.set()
        await runtime._coordinator.wait_idle()

        receipt = runtime.repository.get_turn(turn_id)
        ref = runtime.repository.get_turn_terminal_ref(turn_id)
        assert receipt.status == "succeeded"
        assert ref is not None
        assert runtime.transcript.get_entry(ref.entry_id).commit_state == "committed"
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_event_hub_capacity_never_owns_turn_execution(tmp_path):
    observed: list[object] = []

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    runtime._event_hub = TurnEventHub(max_turns=1)
    runtime._event_hub.open_turn("occupied-live-stream")
    await runtime.start()
    try:
        result = await runtime.submit_and_wait(
            _raw("hub-capacity"),
            ControlRuntimePorts(response_sink=observed.append),
        )

        assert result.receipt.status == "succeeded"
        assert [type(event) for event in observed] == [Final]
        assert (
            runtime.repository.get_turn_terminal_ref(result.acceptance.turn_id)
            is not None
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_enqueue_failure_compensation_commits_terminal_transcript(
    monkeypatch,
    tmp_path,
):
    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    await runtime.start()

    def fail_queue_commit(*_args, **_kwargs):
        raise RuntimeError("injected FIFO commit failure")

    monkeypatch.setattr(runtime._sequencer, "_commit", fail_queue_commit)
    observed: list[object] = []
    try:
        result = await runtime.submit_and_wait(
            _raw("enqueue-failure"),
            ControlRuntimePorts(response_sink=observed.append),
        )

        assert result.acceptance.code == "dispatch_enqueue_failed"
        assert result.receipt.status == "failed"
        ref = runtime.repository.get_turn_terminal_ref(result.acceptance.turn_id)
        assert ref is not None
        entry = runtime.transcript.get_entry(ref.entry_id)
        assert entry.commit_state == "committed"
        assert entry.public_text == "Turn failed."
        assert [type(event) for event in observed] == [Final]
        assert observed[0].kind == "normal"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_rejects_cross_turn_terminal_candidate_reference(tmp_path):
    runtime: ControlRuntime

    async def dispatch_events(_envelope: MessageEnvelope):
        candidate = runtime.transcript.bind_turn(
            "other-conversation",
            "f" * 32,
        ).stage_terminal("done")
        yield Final(
            "done",
            transcript_entry_id=candidate.entry_id,
            transcript_content_sha256=candidate.content_sha256,
        )

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    with pytest.raises(RuntimeError, match="runner failed before terminal Receipt"):
        await runtime.submit_and_wait(
            _raw("foreign-candidate"),
            ControlRuntimePorts(),
        )
    receipt = runtime.repository.list_nonterminal_turns()[0]
    assert receipt.status == "running"
    assert runtime.repository.get_turn_terminal_ref(receipt.turn_id) is None
    with pytest.raises(TranscriptIntegrityError, match="different Conversation"):
        await runtime.close()


@pytest.mark.asyncio
async def test_runner_failure_drops_duplicate_observer_and_event_stream(tmp_path):
    runtime: ControlRuntime
    worker_started = asyncio.Event()
    release_failure = asyncio.Event()
    observed: list[object] = []

    async def dispatch_events(_envelope: MessageEnvelope):
        worker_started.set()
        await release_failure.wait()
        candidate = runtime.transcript.bind_turn(
            "other-conversation",
            "e" * 32,
        ).stage_terminal("done")
        yield Final(
            "done",
            transcript_entry_id=candidate.entry_id,
            transcript_content_sha256=candidate.content_sha256,
        )

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    original = asyncio.create_task(
        runtime.submit_and_wait(
            _raw("runner-failure-duplicate"),
            ControlRuntimePorts(),
        )
    )
    await worker_started.wait()
    duplicate = asyncio.create_task(
        runtime.submit_and_wait(
            _raw("runner-failure-duplicate"),
            ControlRuntimePorts(response_sink=observed.append),
        )
    )
    while not runtime._observer_tasks:
        await asyncio.sleep(0)
    turn_id = next(iter(runtime._live_turns))
    release_failure.set()
    outcomes = await asyncio.gather(original, duplicate, return_exceptions=True)

    assert all(isinstance(outcome, RuntimeError) for outcome in outcomes)
    assert not runtime._observer_tasks
    assert turn_id not in runtime._live_turns
    with pytest.raises(KeyError):
        runtime._event_hub.retained_frames(turn_id)
    with pytest.raises(TranscriptIntegrityError):
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_rejects_worker_that_promotes_candidate_before_receipt(tmp_path):
    runtime: ControlRuntime

    async def dispatch_events(envelope: MessageEnvelope):
        candidate = envelope.transcript_turn.stage_terminal("done")
        runtime.transcript.promote_terminal(
            candidate.entry_id,
            candidate.content_sha256,
            expected_conversation_id=envelope.chat_id,
        )
        yield Final(
            "done",
            transcript_entry_id=candidate.entry_id,
            transcript_content_sha256=candidate.content_sha256,
        )

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    with pytest.raises(RuntimeError, match="runner failed before terminal Receipt"):
        await runtime.submit_and_wait(
            _raw("premature-promote"),
            ControlRuntimePorts(),
        )
    receipt = runtime.repository.list_nonterminal_turns()[0]
    assert receipt.status == "running"
    assert runtime.repository.get_turn_terminal_ref(receipt.turn_id) is None
    with pytest.raises(TranscriptIntegrityError):
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_kind", "model_visible", "provider_content", "error"),
    [
        ("failed", True, "done", "invalid Event kind"),
        ("normal", False, "done", "model-visibility mismatch"),
        ("normal", True, "poison model history", "provider/public content mismatch"),
    ],
)
async def test_runtime_rejects_semantically_invalid_worker_terminal_candidate(
    tmp_path,
    terminal_kind,
    model_visible,
    provider_content,
    error,
):
    async def dispatch_events(envelope: MessageEnvelope):
        envelope.transcript_turn.defer_terminal_message(
            envelope.chat_id,
            content=provider_content,
        )
        candidate = envelope.transcript_turn.stage_terminal(
            "done",
            terminal_kind=terminal_kind,
            model_visible=model_visible,
        )
        yield Final(
            "done",
            kind=terminal_kind,
            transcript_entry_id=candidate.entry_id,
            transcript_content_sha256=candidate.content_sha256,
        )

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    with pytest.raises(RuntimeError, match="runner failed before terminal Receipt"):
        await runtime.submit_and_wait(
            _raw(
                f"invalid-terminal-{terminal_kind}-{model_visible}-{provider_content}"
            ),
            ControlRuntimePorts(),
        )
    assert runtime.repository.list_nonterminal_turns()[0].status == "running"
    with pytest.raises(
        (ControlIntegrityError, TranscriptIntegrityError, RuntimeError),
        match=error,
    ):
        await runtime.close()


@pytest.mark.asyncio
async def test_startup_fails_closed_for_terminal_receipt_without_transcript_ref(
    tmp_path,
):
    first = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    await first.start()
    accepted = first._normalizer.accept(_raw("legacy-terminal-gap"))
    first.repository.terminalize_turn(
        accepted.turn_id,
        terminal_status="failed",
        terminal_code="dispatch_enqueue_failed",
    )
    await first.close()

    second = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    with pytest.raises(ControlIntegrityError, match="no Transcript reference"):
        await second.start()
    await second.close()


@pytest.mark.asyncio
async def test_existing_control_state_cannot_recreate_missing_transcript_store(
    tmp_path,
):
    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
        dispatch_events=lambda _envelope: _one_final(),
    )
    await runtime.start()
    await runtime.submit_and_wait(_raw("durable-before-loss"), ControlRuntimePorts())
    await runtime.close()
    for suffix in ("", "-wal", "-shm"):
        path = tmp_path / f"transcripts.db{suffix}"
        if path.exists():
            path.unlink()

    with pytest.raises(ControlIntegrityError, match="transcripts.db is missing"):
        ControlRuntime.for_local_surface(
            state_root=tmp_path,
            workspace_id="workspace-test",
            surface="cli",
            installation_id="local",
            profile_id="owner",
        )


@pytest.mark.asyncio
async def test_queued_control_state_rejects_schema_valid_fresh_transcript_store(
    tmp_path,
):
    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    await runtime.start()
    runtime._normalizer.accept(_raw("queued-before-store-replacement"))
    await runtime.close()

    replacement_root = tmp_path / "fresh-replacement"
    replacement = CanonicalTranscript(replacement_root)
    replacement.close()
    for suffix in ("-wal", "-shm"):
        stale = tmp_path / f"transcripts.db{suffix}"
        if stale.exists():
            stale.unlink()
    os.replace(replacement_root / "transcripts.db", tmp_path / "transcripts.db")

    with pytest.raises(ControlIntegrityError, match="different Transcript Store"):
        ControlRuntime.for_local_surface(
            state_root=tmp_path,
            workspace_id="workspace-test",
            surface="cli",
            installation_id="local",
            profile_id="owner",
        )


@pytest.mark.asyncio
async def test_queued_control_state_rejects_zero_byte_transcript_replacement(tmp_path):
    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    await runtime.start()
    runtime._normalizer.accept(_raw("queued-before-zero-replacement"))
    await runtime.close()
    for suffix in ("", "-wal", "-shm"):
        path = tmp_path / f"transcripts.db{suffix}"
        if path.exists():
            path.unlink()
    (tmp_path / "transcripts.db").touch()

    with pytest.raises(ControlIntegrityError, match="migration marker"):
        ControlRuntime.for_local_surface(
            state_root=tmp_path,
            workspace_id="workspace-test",
            surface="cli",
            installation_id="local",
            profile_id="owner",
        )


@pytest.mark.asyncio
async def test_runtime_rejects_incomplete_legacy_cutover_marker(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    transcript_store_id = transcript.transcript_store_id
    transcript.close()
    with ControlStateRepository(tmp_path) as repository:
        repository.begin_legacy_import(
            "a" * 32,
            source_manifest_sha256="b" * 64,
            report_ref="transcript-import://incomplete",
        )
        repository.bind_transcript_store(
            transcript_store_id,
            import_run_id="a" * 32,
        )

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="cli",
        installation_id="local",
        profile_id="owner",
    )
    with pytest.raises(ControlIntegrityError, match="cutover is incomplete"):
        await runtime.start()
    await runtime.close()


async def _one_final():
    yield Final("done")
