from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import json

import pytest

from omicsclaw.attachments import (
    AttachmentStoreError,
    SourceAttachmentDescriptorV1,
)
from omicsclaw.control import (
    ControlIntegrityError,
    ControlRuntime,
    ControlRuntimePorts,
    DeliveryAdapterResult,
    DeliveryAttemptOutcome,
    RawContentBlockV1,
    RawInboundV1,
    TurnAcceptanceStatus,
)
from omicsclaw.runtime.agent.envelope import MessageEnvelope
from omicsclaw.runtime.agent.events import Final


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _BytesSource:
    def __init__(
        self,
        values: dict[str, bytes],
        *,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.values = values
        self.started = started
        self.release = release
        self.opens: list[str] = []

    async def open(self, source_attachment_id: str):
        self.opens.append(source_attachment_id)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        payload = self.values[source_attachment_id]
        midpoint = max(1, len(payload) // 2)
        yield payload[:midpoint]
        yield payload[midpoint:]


class _SimulatedProcessCrash(BaseException):
    pass


async def _delivery_adapter(_request):
    return DeliveryAdapterResult(DeliveryAttemptOutcome.ACCEPTED)


def _descriptor(
    source_id: str,
    *,
    declared_media_type: str = "image/png",
    declared_size: int | None = None,
    declared_sha256: str | None = None,
) -> SourceAttachmentDescriptorV1:
    return SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id=source_id,
        display_name="image.png",
        declared_media_type=declared_media_type,
        declared_size=len(PNG_BYTES) if declared_size is None else declared_size,
        declared_sha256=(
            hashlib.sha256(PNG_BYTES).hexdigest()
            if declared_sha256 is None
            else declared_sha256
        ),
    )


def _channel_raw(
    request_id: str,
    *,
    source_id: str | None = "photo-1",
    adapter: str = "telegram",
    subject_kind: str | None = None,
    text: str | None = "describe this image",
    descriptor: SourceAttachmentDescriptorV1 | None = None,
    file_selections=(),
) -> RawInboundV1:
    attachments = ()
    if source_id is not None:
        attachments = (descriptor or _descriptor(source_id),)
    return RawInboundV1(
        schema_version=1,
        surface="channel",
        source_namespace=f"channel/{adapter}/v1/primary",
        source_request_id=request_id,
        external_subject={
            "kind": subject_kind or f"{adapter}_user",
            "value": "42",
        },
        reply_target={
            "schema_version": 1,
            "kind": "channel",
            "adapter": adapter,
            "account_namespace": "primary",
            "destination_id": "7001",
        },
        content=(
            (RawContentBlockV1(kind="text", text=text),) if text is not None else ()
        ),
        attachments=attachments,
        file_selections=file_selections,
    )


def _local_raw(
    request_id: str,
    *,
    surface: str,
    source_id: str,
) -> RawInboundV1:
    return RawInboundV1(
        schema_version=1,
        surface=surface,
        source_namespace=f"{surface}/v1/local/owner",
        source_request_id=request_id,
        reply_target={
            "schema_version": 1,
            "kind": surface,
            "installation_id": "local",
            "profile_id": "owner",
            "slot": "main",
        },
        content=(RawContentBlockV1(kind="text", text="describe"),),
        attachments=(_descriptor(source_id),),
    )


def _channel_runtime(
    state_root,
    *,
    dispatch_events,
    adapter: str = "telegram",
    attachment_input_enabled: bool = True,
) -> ControlRuntime:
    return ControlRuntime.for_channel_surface(
        state_root=state_root,
        workspace_id="workspace-test",
        adapter=adapter,
        account_namespace="primary",
        owner_identities={
            f"channel/{adapter}/primary/{adapter}_user": frozenset({"42"})
        },
        delivery_adapter=_delivery_adapter,
        dispatch_events=dispatch_events,
        attachment_input_enabled=attachment_input_enabled,
    )


@pytest.mark.asyncio
async def test_concurrent_duplicate_attachment_opens_source_once_and_runs_one_turn(
    tmp_path,
):
    source_started = asyncio.Event()
    release_source = asyncio.Event()
    source = _BytesSource(
        {"photo-1": PNG_BYTES},
        started=source_started,
        release=release_source,
    )
    dispatch_count = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("done")

    runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await runtime.start()
    raw = _channel_raw("7001:101")
    try:
        first_task = asyncio.create_task(
            runtime.submit_and_wait(
                raw,
                ControlRuntimePorts(user_id="42"),
                attachment_source=source,
            )
        )
        await asyncio.wait_for(source_started.wait(), timeout=1)
        duplicate_task = asyncio.create_task(
            runtime.submit_and_wait(
                raw,
                ControlRuntimePorts(user_id="42"),
                attachment_source=source,
            )
        )
        await asyncio.sleep(0)
        release_source.set()
        first, duplicate = await asyncio.gather(first_task, duplicate_task)

        assert {
            first.acceptance.status,
            duplicate.acceptance.status,
        } == {
            TurnAcceptanceStatus.ACCEPTED,
            TurnAcceptanceStatus.DUPLICATE,
        }
        assert first.acceptance.turn_id == duplicate.acceptance.turn_id
        assert source.opens == ["photo-1"]
        assert dispatch_count == 1
        assert len(runtime.repository.list_turn_attachment_commitments()) == 1
        # ADR 0059: the duplicate observes the same ordered Records as the
        # novel acceptance, without opening a second byte source.
        assert len(first.attachment_refs) == 1
        assert first.attachment_refs == duplicate.attachment_refs
        assert first.attachment_refs == first.acceptance.attachment_refs
    finally:
        release_source.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_sequential_duplicate_returns_original_ordered_attachment_records(
    tmp_path,
):
    """ADR 0059: a settled retry re-observes the original Records verbatim."""

    source = _BytesSource({"photo-1": PNG_BYTES})

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("done")

    runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await runtime.start()
    raw = _channel_raw("7001:404")
    try:
        first = await runtime.submit_and_wait(
            raw,
            ControlRuntimePorts(user_id="42"),
            attachment_source=source,
        )
        assert first.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert len(first.attachment_refs) == 1

        # The Turn is already terminal here, so this exercises the "every Turn
        # state" clause rather than an in-flight race.
        duplicate = await runtime.submit_and_wait(
            raw,
            ControlRuntimePorts(user_id="42"),
            attachment_source=source,
        )
        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.attachment_refs == first.attachment_refs
        assert source.opens == ["photo-1"]

        reference = duplicate.attachment_refs[0]
        assert reference.ordinal == 0
        assert reference.content_sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
        assert reference.byte_size == len(PNG_BYTES)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_text_only_duplicate_reports_no_attachment_records(tmp_path):
    """A Turn that never carried bytes must not gain phantom References."""

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("done")

    runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await runtime.start()
    raw = _channel_raw("7001:405", source_id=None)
    try:
        first = await runtime.submit_and_wait(raw, ControlRuntimePorts(user_id="42"))
        duplicate = await runtime.submit_and_wait(
            raw, ControlRuntimePorts(user_id="42")
        )
        assert first.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert first.attachment_refs == ()
        assert duplicate.attachment_refs == ()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_attachment_admission_orders_publish_control_accept_then_worker_and_refs_only(
    tmp_path,
    monkeypatch,
):
    order: list[str] = []
    dispatched: list[MessageEnvelope] = []

    async def dispatch_events(envelope: MessageEnvelope):
        order.append("worker")
        dispatched.append(envelope)
        yield Final("done")

    runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await runtime.start()
    original_publish = runtime.attachment_store.publish_batch
    original_accept_turn = runtime.repository.accept_turn
    original_accept_batch = runtime.attachment_store.accept_batch

    async def observed_publish(**kwargs):
        publication = await original_publish(**kwargs)
        order.append("publish")
        return publication

    def observed_accept_turn(*args, **kwargs):
        result = original_accept_turn(*args, **kwargs)
        if result.status is TurnAcceptanceStatus.ACCEPTED:
            order.append("control_commit")
        return result

    def observed_accept_batch(commitment):
        references = original_accept_batch(commitment)
        order.append("accept_batch")
        return references

    monkeypatch.setattr(runtime.attachment_store, "publish_batch", observed_publish)
    monkeypatch.setattr(runtime.repository, "accept_turn", observed_accept_turn)
    monkeypatch.setattr(runtime.attachment_store, "accept_batch", observed_accept_batch)
    try:
        result = await runtime.submit_and_wait(
            _channel_raw("7001:102"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
        )

        assert result.receipt is not None and result.receipt.status == "succeeded"
        assert order == ["publish", "control_commit", "accept_batch", "worker"]
        commitment = runtime.repository.get_turn_attachment_commitment(
            result.acceptance.turn_id
        )
        assert commitment is not None
        references = runtime.attachment_store.get_turn_references(
            result.acceptance.turn_id,
            result.acceptance.conversation_id,
        )
        assert len(references) == 1
        assert len(dispatched) == 1
        expected_durable_content = [
            {"type": "text", "text": "describe this image"},
            {
                "type": "attachment_ref",
                "attachment": references[0].to_json_dict(),
            },
        ]
        assert dispatched[0].content == expected_durable_content
        assert dispatched[0].stored_user_content == expected_durable_content
        durable_json = json.dumps(expected_durable_content, sort_keys=True)
        assert "data:" not in durable_json
        assert "base64" not in durable_json
        assert "photo-1" not in durable_json
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ("digest", "media_type", "size"))
async def test_invalid_attachment_bytes_leave_no_turn_or_commitment(
    tmp_path,
    failure_kind,
):
    dispatch_count = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("must not run")

    if failure_kind == "digest":
        descriptor = _descriptor("bad-photo", declared_sha256="0" * 64)
    elif failure_kind == "media_type":
        descriptor = _descriptor("bad-photo", declared_media_type="image/jpeg")
    else:
        descriptor = _descriptor("bad-photo", declared_size=len(PNG_BYTES) + 1)

    runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await runtime.start()
    source = _BytesSource({"bad-photo": PNG_BYTES})
    request_id = f"7001:bad-{failure_kind}"
    try:
        result = await runtime.submit_and_wait(
            _channel_raw(
                request_id,
                source_id="bad-photo",
                descriptor=descriptor,
            ),
            ControlRuntimePorts(user_id="42"),
            attachment_source=source,
        )

        assert result.acceptance.status is TurnAcceptanceStatus.REJECTED
        assert result.acceptance.code == "attachment_rejected"
        assert result.receipt is None
        assert (
            runtime.lookup_ingress_turn_id(
                surface="channel",
                source_namespace="channel/telegram/v1/primary",
                source_request_id=request_id,
            )
            is None
        )
        assert runtime.repository.list_turn_attachment_commitments() == ()
        assert runtime.repository.list_nonterminal_turns() == ()
        assert runtime.repository.list_terminal_turns() == ()
        assert source.opens == ["bad-photo"]
        assert dispatch_count == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_equal_bytes_from_distinct_requests_keep_distinct_records_and_one_blob(
    tmp_path,
):
    dispatch_count = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("done")

    runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await runtime.start()
    try:
        first = await runtime.submit_and_wait(
            _channel_raw("7001:103", source_id="photo-a"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-a": PNG_BYTES}),
        )
        second = await runtime.submit_and_wait(
            _channel_raw("7001:104", source_id="photo-b"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-b": PNG_BYTES}),
        )

        assert first.acceptance.turn_id != second.acceptance.turn_id
        first_ref = runtime.attachment_store.get_turn_references(
            first.acceptance.turn_id,
            first.acceptance.conversation_id,
        )[0]
        second_ref = runtime.attachment_store.get_turn_references(
            second.acceptance.turn_id,
            second.acceptance.conversation_id,
        )[0]
        assert first_ref.attachment_id != second_ref.attachment_id
        assert first_ref.content_sha256 == second_ref.content_sha256
        assert runtime.attachment_store.resolve_bytes(first_ref) == PNG_BYTES
        assert runtime.attachment_store.resolve_bytes(second_ref) == PNG_BYTES
        assert (
            len(
                [
                    path
                    for path in runtime.attachment_store.blob_root.rglob("*")
                    if path.is_file()
                ]
            )
            == 1
        )
        assert dispatch_count == 2
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_followup_text_turn_rerenders_durable_attachment_history(tmp_path):
    dispatched: list[MessageEnvelope] = []

    async def dispatch_events(envelope: MessageEnvelope):
        dispatched.append(envelope)
        yield Final("done")

    runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await runtime.start()
    try:
        first = await runtime.submit_and_wait(
            _channel_raw("7001:followup-1", source_id="photo-followup"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-followup": PNG_BYTES}),
        )
        second = await runtime.submit_and_wait(
            _channel_raw(
                "7001:followup-2",
                source_id=None,
                text="look at that image again",
            ),
            ControlRuntimePorts(user_id="42"),
        )

        assert first.receipt is not None and first.receipt.status == "succeeded"
        assert second.receipt is not None and second.receipt.status == "succeeded"
        assert len(dispatched) == 2
        adapter = dispatched[1].content_adapter
        assert adapter is not None
        durable_message = {
            "role": "user",
            "content": dispatched[0].stored_user_content,
        }
        rendered = adapter.render_messages([durable_message])
        assert any(block.get("type") == "image_url" for block in rendered[0]["content"])
        assert adapter.restore_messages(rendered) == [durable_message]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_accept_batch_failure_terminalizes_committed_turn_without_worker(
    tmp_path,
    monkeypatch,
):
    dispatch_count = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("must not run")

    runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await runtime.start()

    def fail_accept_batch(_commitment):
        raise AttachmentStoreError("simulated post-Control promotion failure")

    monkeypatch.setattr(runtime.attachment_store, "accept_batch", fail_accept_batch)
    try:
        result = await runtime.submit_and_wait(
            _channel_raw("7001:105"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
        )

        assert result.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert result.acceptance.code == "attachment_finalize_failed"
        assert result.receipt is not None
        assert result.receipt.status == "failed"
        assert result.receipt.terminal_code is not None
        assert result.receipt.terminal_code == "attachment_finalize_failed"
        assert (
            runtime.repository.get_turn_attachment_commitment(result.acceptance.turn_id)
            is not None
        )
        assert (
            runtime.attachment_store.get_turn_references(
                result.acceptance.turn_id,
                result.acceptance.conversation_id,
            )
            == ()
        )
        assert dispatch_count == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_startup_promotes_control_committed_publication_then_interrupts_no_replay(
    tmp_path,
    monkeypatch,
):
    dispatch_count = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("must not run")

    first_runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await first_runtime.start()

    def crash_after_control_commit(_commitment):
        raise _SimulatedProcessCrash("process stopped before Attachment promotion")

    monkeypatch.setattr(
        first_runtime.attachment_store,
        "accept_batch",
        crash_after_control_commit,
    )
    raw = _channel_raw("7001:106")
    try:
        with pytest.raises(_SimulatedProcessCrash):
            await first_runtime.submit_and_wait(
                raw,
                ControlRuntimePorts(user_id="42"),
                attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
            )
        turn_id = first_runtime.lookup_ingress_turn_id(
            surface="channel",
            source_namespace="channel/telegram/v1/primary",
            source_request_id="7001:106",
        )
        assert turn_id is not None
        queued = first_runtime.repository.get_turn(turn_id)
        assert queued.status == "queued"
        commitment = first_runtime.repository.get_turn_attachment_commitment(turn_id)
        assert commitment is not None
        assert (
            first_runtime.attachment_store.get_turn_references(
                turn_id,
                queued.conversation_id,
            )
            == ()
        )
    finally:
        await first_runtime.close()

    recovered = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    startup = await recovered.start()
    try:
        assert startup.interrupted_turn_ids == (turn_id,)
        receipt = recovered.repository.get_turn(turn_id)
        assert receipt.status == "interrupted"
        references = recovered.attachment_store.get_turn_references(
            turn_id,
            receipt.conversation_id,
        )
        assert len(references) == 1
        assert recovered.attachment_store.resolve_bytes(references[0]) == PNG_BYTES
        assert dispatch_count == 0
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_file_references_and_unenabled_surfaces_remain_fail_closed(tmp_path):
    dispatch_count = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("must not run")

    telegram = _channel_runtime(
        tmp_path / "telegram",
        dispatch_events=dispatch_events,
    )
    await telegram.start()
    try:
        file_reference = replace(
            _channel_raw("7001:file", source_id=None),
            file_selections=({"path": "/tmp/not-an-upload.h5ad"},),
        )
        result = await telegram.submit_and_wait(
            file_reference,
            ControlRuntimePorts(user_id="42"),
        )
        assert result.acceptance.status is TurnAcceptanceStatus.REJECTED
        assert result.acceptance.code == "file_selections_not_supported"
    finally:
        await telegram.close()

    for surface in ("cli", "desktop"):
        runtime = ControlRuntime.for_local_surface(
            state_root=tmp_path / surface,
            workspace_id="workspace-test",
            surface=surface,
            installation_id="local",
            profile_id="owner",
            dispatch_events=dispatch_events,
        )
        await runtime.start()
        source_id = f"{surface}-photo"
        source = _BytesSource({source_id: PNG_BYTES})
        try:
            result = await runtime.submit_and_wait(
                _local_raw(
                    f"{surface}-attachment",
                    surface=surface,
                    source_id=source_id,
                ),
                ControlRuntimePorts(user_id="42"),
                attachment_source=source,
            )
            assert result.acceptance.status is TurnAcceptanceStatus.REJECTED
            assert result.acceptance.code == "attachments_not_supported"
            assert source.opens == []
        finally:
            await runtime.close()

    slack = _channel_runtime(
        tmp_path / "slack",
        adapter="slack",
        dispatch_events=dispatch_events,
        attachment_input_enabled=False,
    )
    await slack.start()
    slack_source = _BytesSource({"slack-photo": PNG_BYTES})
    try:
        result = await slack.submit_and_wait(
            _channel_raw(
                "slack:1",
                source_id="slack-photo",
                adapter="slack",
            ),
            ControlRuntimePorts(user_id="42"),
            attachment_source=slack_source,
        )
        assert result.acceptance.status is TurnAcceptanceStatus.REJECTED
        assert result.acceptance.code == "attachments_not_supported"
        assert slack_source.opens == []
        assert dispatch_count == 0
    finally:
        await slack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prepared_content",
    (
        "inspect data:image/png;base64,QUJD",
        [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,QUJD"},
            }
        ],
        [{"type": "attachment_ref", "attachment": {"bad": "reference"}}],
    ),
)
async def test_non_attachment_content_factory_cannot_bypass_durable_content_policy(
    tmp_path,
    prepared_content,
):
    dispatch_count = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("must not run")

    async def content_factory(_envelope):
        return prepared_content

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    raw = replace(
        _local_raw(
            "desktop-content-factory-bypass",
            surface="desktop",
            source_id="unused-photo",
        ),
        attachments=(),
    )
    try:
        result = await runtime.submit_and_wait(
            raw,
            ControlRuntimePorts(
                user_id="42",
                content_factory=content_factory,
            ),
        )

        assert result.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert result.receipt is not None
        assert result.receipt.status == "failed"
        assert dispatch_count == 0
        history = runtime.transcript.get_history(result.acceptance.conversation_id)
        durable_json = json.dumps(history, sort_keys=True)
        assert not any(message.get("role") == "user" for message in history)
        assert "data:image" not in durable_json
        assert "attachment_ref" not in durable_json
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_enqueue_failure_result_carries_accepted_attachment_refs(
    tmp_path,
    monkeypatch,
):
    """A post-accept enqueue fault must not drop the accepted References.

    The batch is durably accepted before the FIFO commit fails, so the novel
    ``dispatch_enqueue_failed`` result must report the same ordered References a
    later duplicate lookup returns.  Before the fix the novel result carried
    none while the duplicate carried them, an observable contract split.
    """

    dispatch_count = 0

    async def dispatch_events(_envelope: MessageEnvelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("must not run")

    runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await runtime.start()

    def fail_queue_commit(*_args, **_kwargs):
        raise RuntimeError("injected FIFO commit failure")

    monkeypatch.setattr(runtime._sequencer, "_commit", fail_queue_commit)
    try:
        novel = await runtime.submit_and_wait(
            _channel_raw("7001:enqueue"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
        )

        assert novel.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert novel.acceptance.code == "dispatch_enqueue_failed"
        assert novel.receipt is not None and novel.receipt.status == "failed"
        assert len(novel.acceptance.attachment_refs) == 1
        assert dispatch_count == 0

        # A duplicate of the same failed Turn returns exactly the same
        # References, so novel and duplicate now agree.
        duplicate = await runtime.submit_and_wait(
            _channel_raw("7001:enqueue"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
        )
        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.acceptance.attachment_refs == novel.acceptance.attachment_refs
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_duplicate_of_finalize_failed_turn_reads_empty_without_incident(
    tmp_path,
    monkeypatch,
):
    """A finalize failure never accepted a Record, so empty is legitimate.

    The batch was not accepted, so the committed-but-unavailable guard must not
    misfire: the Turn is terminally ``failed`` and honestly has no accepted
    attachments, not lost content.
    """

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("must not run")

    runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await runtime.start()

    def fail_accept_batch(_commitment):
        raise AttachmentStoreError("simulated finalize failure")

    monkeypatch.setattr(runtime.attachment_store, "accept_batch", fail_accept_batch)
    try:
        novel = await runtime.submit_and_wait(
            _channel_raw("7001:finalize"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
        )
        assert novel.acceptance.code == "attachment_finalize_failed"
        assert novel.receipt is not None and novel.receipt.status == "failed"
        assert novel.acceptance.attachment_refs == ()

        duplicate = await runtime.submit_and_wait(
            _channel_raw("7001:finalize"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
        )
        assert duplicate.acceptance.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.acceptance.attachment_refs == ()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_lost_committed_refs_for_succeeded_turn_is_an_integrity_incident(
    tmp_path,
    monkeypatch,
):
    """A succeeded Turn that committed attachments must still have them.

    If the Store silently returns no References for a Turn the control plane
    committed to and that actually ran, that is lost content and must fail
    closed rather than resolve to a bare text Turn.
    """

    async def dispatch_events(_envelope: MessageEnvelope):
        yield Final("done")

    runtime = _channel_runtime(tmp_path, dispatch_events=dispatch_events)
    await runtime.start()
    try:
        accepted = await runtime.submit_and_wait(
            _channel_raw("7001:corrupt"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
        )
        assert accepted.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert accepted.receipt is not None and accepted.receipt.status == "succeeded"
        assert accepted.acceptance.attachment_refs

        # Simulate the Store losing the committed accepted Records.
        monkeypatch.setattr(
            runtime.attachment_store,
            "get_turn_references",
            lambda *_args, **_kwargs: (),
        )
        with pytest.raises(ControlIntegrityError):
            await runtime.submit_and_wait(
                _channel_raw("7001:corrupt"),
                ControlRuntimePorts(user_id="42"),
                attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
            )
    finally:
        await runtime.close()
