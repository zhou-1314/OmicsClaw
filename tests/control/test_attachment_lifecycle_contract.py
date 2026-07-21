"""Cross-store contract tests for ADR 0059 accepted-attachment immutability.

ADR 0059 names six operations that must never delete or rewrite an accepted
Attachment Record or its Blob: Project archive/restore, ``/new``, Active
Conversation Binding replacement, SSE disconnect, Turn cancellation and
Transcript compaction.

The Store already protects Records structurally with SQLite triggers and
``ON DELETE RESTRICT`` foreign keys, and those triggers have their own unit
tests.  What was missing is a test that actually *performs* each lifecycle
operation against an attachment-backed Turn and then re-asserts the cross-store
contract.  Without these, a future archive or compaction implementation could
regress the guarantee while every existing test stayed green.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from omicsclaw.attachments import SourceAttachmentDescriptorV1
from omicsclaw.control import (
    ControlRuntime,
    ControlRuntimePorts,
    DeliveryAdapterResult,
    DeliveryAttemptOutcome,
    ProjectLifecycleStatus,
    RawContentBlockV1,
    RawInboundV1,
    TurnAcceptanceStatus,
)
from omicsclaw.runtime.agent.envelope import MessageEnvelope
from omicsclaw.runtime.agent.events import Final
from omicsclaw.runtime.agent.query_engine import (
    QueryEngineCallbacks,
    QueryEngineConfig,
    QueryEngineContext,
    run_query_engine,
)
from omicsclaw.runtime.context.compaction import ContextCompactionConfig
from omicsclaw.runtime.storage.tool_result import ToolResultStore
from omicsclaw.runtime.storage.transcript import TranscriptStore
from omicsclaw.runtime.tools.registry import ToolRegistry


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _BytesSource:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values
        self.opens: list[str] = []

    async def open(self, source_attachment_id: str):
        self.opens.append(source_attachment_id)
        yield self.values[source_attachment_id]


async def _delivery_adapter(*_args, **_kwargs) -> DeliveryAdapterResult:
    return DeliveryAdapterResult(
        outcome=DeliveryAttemptOutcome.ACCEPTED,
        provider_evidence={"message_id": 1},
    )


def _descriptor(source_id: str) -> SourceAttachmentDescriptorV1:
    return SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id=source_id,
        display_name="cells.png",
        declared_media_type="image/png",
        declared_size=len(PNG_BYTES),
        declared_sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
    )


def _reply_target() -> dict:
    return {
        "schema_version": 1,
        "kind": "channel",
        "adapter": "telegram",
        "account_namespace": "primary",
        "destination_id": "7001",
    }


def _raw(request_id: str, *, text: str = "describe this image") -> RawInboundV1:
    return RawInboundV1(
        schema_version=1,
        surface="channel",
        source_namespace="channel/telegram/v1/primary",
        source_request_id=request_id,
        external_subject={"kind": "telegram_user", "value": "42"},
        reply_target=_reply_target(),
        content=(RawContentBlockV1(kind="text", text=text),),
        attachments=(_descriptor("photo-1"),),
    )


def _new_conversation_command(
    request_id: str, *, project_id: str | None = None
) -> RawInboundV1:
    """A real `/new`: a control command that replaces the Active Binding."""

    return RawInboundV1(
        schema_version=1,
        surface="channel",
        source_namespace="channel/telegram/v1/primary",
        source_request_id=request_id,
        external_subject={"kind": "telegram_user", "value": "42"},
        reply_target=_reply_target(),
        content=(),
        attachments=(),
        project_command={
            "kind": "new_conversation",
            **({"project_id": project_id} if project_id is not None else {}),
        },
    )


def _runtime(state_root, *, dispatch_events) -> ControlRuntime:
    return ControlRuntime.for_channel_surface(
        state_root=state_root,
        workspace_id="workspace-test",
        adapter="telegram",
        account_namespace="primary",
        owner_identities={"channel/telegram/primary/telegram_user": frozenset({"42"})},
        delivery_adapter=_delivery_adapter,
        dispatch_events=dispatch_events,
        attachment_input_enabled=True,
    )


async def _dispatch_ok(_envelope: MessageEnvelope):
    yield Final("done")


class _StaticLLM:
    def __init__(self) -> None:
        self.chat = self
        self.completions = self
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="compacted", tool_calls=None)
                )
            ],
        )


def _store_counts(store) -> tuple[int, int]:
    """Read (record_count, blob_count) without mutating the Store."""

    connection = sqlite3.connect(f"file:{store.database_path}?mode=ro", uri=True)
    try:
        records = connection.execute(
            "SELECT COUNT(*) FROM attachment_records"
        ).fetchone()[0]
        blobs = connection.execute("SELECT COUNT(*) FROM attachment_blobs").fetchone()[
            0
        ]
    finally:
        connection.close()
    return int(records), int(blobs)


def _blob_files(store) -> int:
    return sum(1 for path in store.blob_root.rglob("*") if path.is_file())


class _AttachmentSnapshot:
    """Immutable pre-operation view of one Turn's accepted attachment state."""

    def __init__(self, runtime: ControlRuntime, turn_id: str, conversation_id: str):
        self.turn_id = turn_id
        self.conversation_id = conversation_id
        store = runtime.attachment_store
        self.references = store.get_turn_references(turn_id, conversation_id)
        assert self.references, "snapshot requires an attachment-backed Turn"
        self.payloads = tuple(
            store.resolve_bytes(reference) for reference in self.references
        )
        self.counts = _store_counts(store)
        self.blob_files = _blob_files(store)


def assert_attachment_unchanged(
    runtime: ControlRuntime,
    snapshot: _AttachmentSnapshot,
    *,
    operation: str,
    allow_new_records: bool = False,
) -> None:
    """Re-assert the full ADR 0059 cross-store contract after one operation.

    ``allow_new_records`` is for operations that legitimately accept further
    attachment Turns.  Even then nothing may be removed, so the counts are
    still asserted to be non-decreasing rather than simply ignored.
    """

    store = runtime.attachment_store
    references = store.get_turn_references(snapshot.turn_id, snapshot.conversation_id)
    assert references == snapshot.references, (
        f"{operation} changed the ordered Attachment References"
    )
    for reference, expected in zip(references, snapshot.payloads):
        resolved = store.resolve_bytes(reference)
        assert resolved == expected, f"{operation} changed accepted Blob bytes"
        assert hashlib.sha256(resolved).hexdigest() == reference.content_sha256, (
            f"{operation} broke the Record/Blob digest binding"
        )
    counts = _store_counts(store)
    blob_files = _blob_files(store)
    if allow_new_records:
        assert counts >= snapshot.counts and blob_files >= snapshot.blob_files, (
            f"{operation} removed an accepted Record or Blob"
        )
    else:
        assert counts == snapshot.counts, (
            f"{operation} changed Record/Blob row counts"
        )
        assert blob_files == snapshot.blob_files, (
            f"{operation} changed the on-disk Blob tree"
        )
    assert (
        runtime.repository.get_turn_attachment_commitment(snapshot.turn_id) is not None
    ), f"{operation} dropped the control-plane batch commitment"


async def _accept_attachment_turn(
    runtime: ControlRuntime, request_id: str = "7001:900"
) -> _AttachmentSnapshot:
    result = await runtime.submit_and_wait(
        _raw(request_id),
        ControlRuntimePorts(user_id="42"),
        attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
    )
    assert result.acceptance.status is TurnAcceptanceStatus.ACCEPTED
    assert result.attachment_refs
    return _AttachmentSnapshot(
        runtime, result.acceptance.turn_id, result.acceptance.conversation_id
    )


@pytest.mark.asyncio
async def test_project_archive_and_restore_preserve_accepted_attachments(tmp_path):
    """Archive the Project that owns the attachment-backed Conversation."""

    runtime = _runtime(tmp_path, dispatch_events=_dispatch_ok)
    await runtime.start()
    try:
        project = runtime.repository.create_project("Spatial pilot")
        bound = await runtime.submit_and_wait(
            _new_conversation_command("7001:project", project_id=project.project_id),
            ControlRuntimePorts(user_id="42"),
        )
        assert bound.acceptance.status is TurnAcceptanceStatus.ACCEPTED

        snapshot = await _accept_attachment_turn(runtime)
        assert snapshot.conversation_id == bound.acceptance.conversation_id
        conversation = runtime.repository.get_conversation(snapshot.conversation_id)
        assert conversation.project_id == project.project_id

        archived = runtime.repository.archive_project(project.project_id)
        assert archived.status is ProjectLifecycleStatus.CHANGED
        assert runtime.repository.get_project(project.project_id).lifecycle == (
            "archived"
        ), "archive must durably flip the Project lifecycle"
        assert_attachment_unchanged(runtime, snapshot, operation="archive_project")

        restored = runtime.repository.restore_project(project.project_id)
        assert restored.status is ProjectLifecycleStatus.CHANGED
        assert (
            runtime.repository.get_project(project.project_id).lifecycle == "active"
        ), "restore must durably flip the Project lifecycle back"
        assert_attachment_unchanged(runtime, snapshot, operation="restore_project")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_new_conversation_and_binding_replacement_preserve_attachments(tmp_path):
    runtime = _runtime(tmp_path, dispatch_events=_dispatch_ok)
    await runtime.start()
    try:
        snapshot = await _accept_attachment_turn(runtime)

        # A real `/new`: a new_conversation control command activates a fresh
        # Conversation and replaces the Active Conversation Binding at this
        # Reply Target.
        started = await runtime.submit_and_wait(
            _new_conversation_command("7001:new"),
            ControlRuntimePorts(user_id="42"),
        )
        assert started.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert (
            started.acceptance.conversation_id != snapshot.conversation_id
        ), "the new_conversation command must activate a different Conversation"

        # A follow-up attachment Turn now lands in the newly bound Conversation,
        # proving the Active Binding really moved rather than reusing the first.
        followup = await runtime.submit_and_wait(
            _raw("7001:901", text="start over"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
        )
        assert followup.acceptance.status is TurnAcceptanceStatus.ACCEPTED
        assert followup.acceptance.conversation_id == started.acceptance.conversation_id
        assert followup.acceptance.conversation_id != snapshot.conversation_id
        assert_attachment_unchanged(
            runtime,
            snapshot,
            operation="new conversation + binding replacement",
            allow_new_records=True,
        )

        # The original Turn keeps its own Records even though the binding has
        # moved on; the new Turn owns distinct Records sharing one Blob.
        assert followup.acceptance.turn_id != snapshot.turn_id
        assert (
            followup.attachment_refs[0].attachment_id
            != snapshot.references[0].attachment_id
        )
        assert (
            followup.attachment_refs[0].content_sha256
            == snapshot.references[0].content_sha256
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_desktop_sse_disconnect_preserves_accepted_attachments(
    tmp_path, monkeypatch
):
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    running = asyncio.Event()
    release = asyncio.Event()

    async def slow_dispatch(_envelope: MessageEnvelope):
        running.set()
        await release.wait()
        yield Final("done")

    runtime = _runtime(tmp_path, dispatch_events=slow_dispatch)
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)
    turn_id = ""

    def accepted(value: str) -> None:
        nonlocal turn_id
        turn_id = value

    task = asyncio.create_task(
        runtime.submit_and_wait(
            _raw("7001:sse"),
            ControlRuntimePorts(user_id="42"),
            attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
            on_accepted=accepted,
        )
    )
    try:
        await asyncio.wait_for(running.wait(), timeout=5)
        snapshot = _AttachmentSnapshot(
            runtime,
            turn_id,
            runtime.repository.get_turn(turn_id).conversation_id,
        )

        response = await server.observe_turn_events(turn_id)
        first_chunk = await anext(response.body_iterator)
        first_text = (
            first_chunk.decode() if isinstance(first_chunk, bytes) else str(first_chunk)
        )
        assert first_text.startswith("event: snapshot\n")
        assert len(runtime._event_hub._streams[turn_id].subscribers) == 1
        await response.body_iterator.aclose()
        assert len(runtime._event_hub._streams[turn_id].subscribers) == 0
        assert runtime.repository.get_turn(turn_id).status == "running"
        assert_attachment_unchanged(
            runtime, snapshot, operation="Desktop SSE disconnect"
        )

        release.set()
        await task
        assert_attachment_unchanged(
            runtime, snapshot, operation="completion after Desktop SSE disconnect"
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_turn_cancellation_preserves_accepted_attachments(tmp_path):
    release = asyncio.Event()
    running = asyncio.Event()

    async def slow_dispatch(_envelope: MessageEnvelope):
        running.set()
        await release.wait()
        yield Final("done")

    runtime = _runtime(tmp_path, dispatch_events=slow_dispatch)
    await runtime.start()
    try:
        # The bystander Turn must be allowed to settle, so open the gate first.
        release.set()
        settled = await _accept_attachment_turn(runtime, "7001:910")
        release.clear()
        running.clear()

        pending = asyncio.create_task(
            runtime.submit_and_wait(
                _raw("7001:911", text="cancel me"),
                ControlRuntimePorts(user_id="42"),
                attachment_source=_BytesSource({"photo-1": PNG_BYTES}),
            )
        )
        await asyncio.wait_for(running.wait(), timeout=5)
        nonterminal = runtime.repository.list_nonterminal_turns()
        assert len(nonterminal) == 1
        target = nonterminal[0]
        in_flight = _AttachmentSnapshot(runtime, target.turn_id, target.conversation_id)

        cancel_result = runtime.cancel(target.turn_id)
        assert cancel_result.changed, "the cancellation must actually take effect"
        assert cancel_result.code == "cancel_requested"
        release.set()
        await pending
        # Cancellation is requested while the dispatch is still gated, so the
        # Turn must terminalize as canceled — never succeeded — once released.
        assert runtime.repository.get_turn(target.turn_id).status == "canceled"

        # The bystander snapshot predates the second Turn's Records, so only the
        # target snapshot can assert exact counts.
        assert_attachment_unchanged(
            runtime,
            settled,
            operation="cancel (bystander)",
            allow_new_records=True,
        )
        assert_attachment_unchanged(runtime, in_flight, operation="cancel (target)")
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_transcript_compaction_preserves_accepted_attachments(tmp_path):
    dispatched: list[MessageEnvelope] = []

    async def capture(envelope: MessageEnvelope):
        dispatched.append(envelope)
        yield Final("done")

    runtime = _runtime(tmp_path, dispatch_events=capture)
    await runtime.start()
    try:
        snapshot = await _accept_attachment_turn(runtime)

        # Rendering durable history for a model call reads Blob bytes; it must
        # neither mutate nor consume them.
        adapter = dispatched[0].content_adapter
        assert adapter is not None
        durable = {"role": "user", "content": dispatched[0].stored_user_content}
        rendered = adapter.render_messages([durable])
        assert adapter.restore_messages(rendered) == [durable]

        # Run the real QueryEngine collapse and its replace_history persistence
        # seam. The provider sees rendered image data, while persistence must
        # restore the exact durable attachment_ref form.
        transcript_store = TranscriptStore()
        chat_id = "attachment-compaction"
        old_history = []
        for index in range(8):
            old_history.extend(
                (
                    {
                        "role": "user",
                        "content": f"old question {index} " + ("Q" * 1600),
                    },
                    {
                        "role": "assistant",
                        "content": f"old answer {index} " + ("A" * 1600),
                    },
                )
            )
        # One attachment occurrence is old enough to be summarized and one is
        # in the retained tail. This covers both compaction transformations.
        transcript_store.replace_history(chat_id, [durable, *old_history, durable])
        compacted_events = []
        llm = _StaticLLM()
        final = await run_query_engine(
            llm=llm,
            context=QueryEngineContext(
                chat_id=chat_id,
                session_id="attachment-compaction",
                system_prompt="SYSTEM",
                user_message_content="continue",
                content_adapter=adapter,
            ),
            tool_runtime=ToolRegistry([]).build_runtime({}),
            transcript_store=transcript_store,
            tool_result_store=ToolResultStore(
                storage_dir=tmp_path / "query-tool-results"
            ),
            config=QueryEngineConfig(
                model="fake-model",
                context_compaction=ContextCompactionConfig(
                    max_prompt_tokens=4000,
                    collapse_trigger_ratio=0.45,
                    auto_compact_trigger_ratio=0.99,
                    collapse_preserve_messages=4,
                    collapse_preserve_tokens=2000,
                    protected_tail_messages=2,
                    collapse_llm_summary_enabled=False,
                ),
            ),
            callbacks=QueryEngineCallbacks(
                on_context_compacted=lambda event: compacted_events.append(event)
            ),
        )
        assert final == "compacted"
        assert compacted_events and compacted_events[0].messages_compressed > 0
        assert llm.calls
        persisted = transcript_store.get_history(chat_id)
        persisted_json = json.dumps(persisted, sort_keys=True)
        assert "attachment_ref" in persisted_json
        assert "OMICSCLAW_ATTACHMENT_V1" not in persisted_json
        assert "image_url" not in persisted_json
        assert "data:image" not in persisted_json
        assert_attachment_unchanged(
            runtime, snapshot, operation="transcript compaction"
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_accepted_attachments_survive_backend_restart(tmp_path):
    runtime = _runtime(tmp_path, dispatch_events=_dispatch_ok)
    await runtime.start()
    try:
        snapshot = await _accept_attachment_turn(runtime)
    finally:
        await runtime.close()

    reopened = _runtime(tmp_path, dispatch_events=_dispatch_ok)
    await reopened.start()
    try:
        assert_attachment_unchanged(reopened, snapshot, operation="backend restart")
    finally:
        await reopened.close()
