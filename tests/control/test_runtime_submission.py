from __future__ import annotations

import asyncio
import hashlib

import pytest

from omicsclaw.control import (
    ControlIntegrityError,
    ControlRuntime,
    ControlRuntimePorts,
    RawContentBlockV1,
    RawInboundV1,
    TurnAcceptanceStatus,
)
from omicsclaw.attachments import SourceAttachmentDescriptorV1
from omicsclaw.runtime.agent.events import Final


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
)


class _BytesSource:
    async def open(self, _source_attachment_id: str):
        yield PNG_BYTES


def _raw(request_id: str) -> RawInboundV1:
    return RawInboundV1(
        schema_version=1,
        surface="desktop",
        source_namespace="desktop/v1/local/owner",
        source_request_id=request_id,
        reply_target={
            "schema_version": 1,
            "kind": "desktop",
            "installation_id": "local",
            "profile_id": "owner",
            "slot": "main",
        },
        content=(RawContentBlockV1(kind="text", text="hello"),),
    )


@pytest.mark.asyncio
async def test_submit_returns_after_durable_acceptance_without_waiting_for_terminal(
    tmp_path,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    dispatch_count = 0

    async def dispatch_events(_envelope):
        nonlocal dispatch_count
        dispatch_count += 1
        started.set()
        await release.wait()
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
    raw = _raw("1" * 32)
    try:
        accepted = await asyncio.wait_for(
            runtime.submit(raw, ControlRuntimePorts()), timeout=1
        )
        assert accepted.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert accepted.receipt is not None
        assert accepted.receipt.status in {"queued", "running"}

        duplicate = await asyncio.wait_for(
            runtime.submit(raw, ControlRuntimePorts()), timeout=1
        )
        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.acceptance.turn_id == accepted.acceptance.turn_id

        waiter = asyncio.create_task(
            runtime.submit_and_wait(raw, ControlRuntimePorts())
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        terminal = await asyncio.wait_for(waiter, timeout=1)
        assert terminal.receipt is not None
        assert terminal.receipt.status == "succeeded"
        assert dispatch_count == 1
    finally:
        release.set()
        await runtime.close()


def test_local_attachment_input_is_an_explicit_composition_choice(tmp_path) -> None:
    disabled = ControlRuntime.for_local_surface(
        state_root=tmp_path / "disabled",
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
    )
    enabled = ControlRuntime.for_local_surface(
        state_root=tmp_path / "enabled",
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        attachment_input_enabled=True,
    )
    try:
        assert disabled._normalizer._config.attachment_input_enabled is False
        assert enabled._normalizer._config.attachment_input_enabled is True
        assert enabled._normalizer._attachment_failure_terminalizer is not None
    finally:
        disabled.attachment_store.close()
        disabled.transcript.close()
        disabled.repository.close()
        enabled.attachment_store.close()
        enabled.transcript.close()
        enabled.repository.close()


@pytest.mark.asyncio
async def test_local_attachment_finalize_failure_commits_canonical_terminal_ref(
    monkeypatch,
    tmp_path,
) -> None:
    dispatch_count = 0

    async def dispatch_events(_envelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("must not run")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
        attachment_input_enabled=True,
    )
    await runtime.start()
    descriptor = SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id="a" * 32,
        display_name="cell.png",
        declared_media_type="image/png",
        declared_size=len(PNG_BYTES),
        declared_sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
    )
    raw = RawInboundV1(
        schema_version=1,
        surface="desktop",
        source_namespace="desktop/v1/local/owner",
        source_request_id="2" * 32,
        reply_target={
            "schema_version": 1,
            "kind": "desktop",
            "installation_id": "local",
            "profile_id": "owner",
            "slot": "main",
        },
        content=(RawContentBlockV1(kind="text", text="describe"),),
        attachments=(descriptor,),
    )

    def fail_accept_batch(_commitment):
        raise RuntimeError("simulated finalize failure")

    monkeypatch.setattr(runtime.attachment_store, "accept_batch", fail_accept_batch)
    try:
        with pytest.raises(
            ControlIntegrityError,
            match="do not match the control-plane commitment",
        ):
            await runtime.submit(
                raw,
                ControlRuntimePorts(),
                attachment_source=_BytesSource(),
            )
        terminal = runtime.repository.list_terminal_turns()
        assert len(terminal) == 1
        receipt = terminal[0]
        assert receipt.status == "failed"
        assert receipt.terminal_code == "attachment_finalize_failed"
        terminal_ref = runtime.repository.get_turn_terminal_ref(receipt.turn_id)
        assert terminal_ref is not None
        runtime.transcript.verify_committed_terminal(
            terminal_ref.entry_id,
            terminal_ref.content_sha256,
            expected_conversation_id=receipt.conversation_id,
            expected_turn_id=receipt.turn_id,
        )
        assert dispatch_count == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_live_port_prepare_failure_terminalizes_the_accepted_turn(
    monkeypatch,
    tmp_path,
) -> None:
    dispatch_count = 0

    async def dispatch_events(_envelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("must not run")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()

    def fail_live_registration(*_args, **_kwargs):
        raise RuntimeError("simulated live-port registration failure")

    monkeypatch.setattr(runtime, "_register_live_turn", fail_live_registration)
    raw = _raw("3" * 32)
    try:
        result = await runtime.submit(raw, ControlRuntimePorts())
        assert result.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert result.acceptance.code == "dispatch_enqueue_failed"
        assert result.receipt is not None
        assert result.receipt.status == "failed"
        assert result.receipt.terminal_code == "dispatch_enqueue_failed"
        terminal_ref = runtime.repository.get_turn_terminal_ref(
            result.acceptance.turn_id
        )
        assert terminal_ref is not None
        runtime.transcript.verify_committed_terminal(
            terminal_ref.entry_id,
            terminal_ref.content_sha256,
            expected_conversation_id=result.acceptance.conversation_id,
            expected_turn_id=result.acceptance.turn_id,
        )

        duplicate = await runtime.submit(raw, ControlRuntimePorts())
        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.receipt is not None
        assert duplicate.receipt.status == "failed"
        await runtime._coordinator.wait_idle()
        assert dispatch_count == 0
        assert runtime._sequencer._entries_total == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runner_wake_failure_terminalizes_the_accepted_turn(
    monkeypatch,
    tmp_path,
) -> None:
    dispatch_count = 0

    async def dispatch_events(_envelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("must not run")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()

    def fail_wake(_conversation_id: str) -> None:
        raise RuntimeError("simulated runner wake failure")

    monkeypatch.setattr(runtime._coordinator, "_wake", fail_wake)
    raw = _raw("4" * 32)
    try:
        result = await runtime.submit(raw, ControlRuntimePorts())
        assert result.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert result.acceptance.code == "dispatch_enqueue_failed"
        assert result.receipt is not None
        assert result.receipt.status == "failed"
        assert result.receipt.terminal_code == "dispatch_enqueue_failed"
        terminal_ref = runtime.repository.get_turn_terminal_ref(
            result.acceptance.turn_id
        )
        assert terminal_ref is not None
        runtime.transcript.verify_committed_terminal(
            terminal_ref.entry_id,
            terminal_ref.content_sha256,
            expected_conversation_id=result.acceptance.conversation_id,
            expected_turn_id=result.acceptance.turn_id,
        )

        duplicate = await runtime.submit(raw, ControlRuntimePorts())
        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.receipt is not None
        assert duplicate.receipt.status == "failed"
        assert dispatch_count == 0
        assert runtime._sequencer._entries_total == 0
        assert runtime._live_turns == {}
    finally:
        await runtime.close()
