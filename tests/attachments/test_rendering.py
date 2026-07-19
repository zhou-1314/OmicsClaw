from __future__ import annotations

import asyncio
import base64
import copy
import hashlib

import pytest

from omicsclaw.attachments.models import (
    AttachmentReferenceV1,
    SourceAttachmentDescriptorV1,
)
from omicsclaw.attachments.rendering import AttachmentContentAdapter
from omicsclaw.attachments.store import AttachmentIntegrityError, AttachmentStore


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _BytesSource:
    async def open(self, source_attachment_id: str):
        assert source_attachment_id == "telegram-photo-1"
        yield PNG_BYTES


class _RecordingStore:
    def __init__(self, store: AttachmentStore) -> None:
        self.store = store
        self.resolved: list[AttachmentReferenceV1] = []

    def resolve_bytes(self, reference: AttachmentReferenceV1) -> bytes:
        self.resolved.append(reference)
        return self.store.resolve_bytes(reference)


@pytest.fixture
def accepted_attachment(tmp_path):
    store = AttachmentStore(tmp_path)
    descriptor = SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id="telegram-photo-1",
        display_name="cell-map.png",
        declared_media_type="image/png",
        declared_size=len(PNG_BYTES),
        declared_sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
    )
    publication = asyncio.run(
        store.publish_batch(
            proposed_turn_id="1" * 32,
            proposed_conversation_id="2" * 32,
            descriptors=(descriptor,),
            source=_BytesSource(),
        )
    )
    reference = store.accept_batch(publication.commitment)[0]
    try:
        yield store, reference
    finally:
        store.close()


def _durable_message(reference: AttachmentReferenceV1) -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this cell map."},
                {
                    "type": "attachment_ref",
                    "attachment": reference.to_json_dict(),
                },
            ],
        }
    ]


def _durable_history(
    reference: AttachmentReferenceV1,
    count: int,
) -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "attachment_ref",
                    "attachment": reference.to_json_dict(),
                }
            ],
        }
        for _ in range(count)
    ]


def test_render_resolves_and_revalidates_accepted_reference(accepted_attachment):
    store, reference = accepted_attachment
    recording_store = _RecordingStore(store)
    adapter = AttachmentContentAdapter(recording_store)
    durable = _durable_message(reference)
    before = copy.deepcopy(durable)

    rendered = adapter.render_messages(durable)

    assert durable == before
    assert rendered is not durable
    assert rendered[0]["content"][0] == {
        "type": "text",
        "text": "Describe this cell map.",
    }
    marker = rendered[0]["content"][1]
    assert marker["type"] == "text"
    assert reference.attachment_id in marker["text"]
    assert reference.content_sha256 in marker["text"]
    image = rendered[0]["content"][2]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
    )
    assert recording_store.resolved == [reference]


def test_render_accepts_exact_aggregate_count_and_byte_boundaries(
    accepted_attachment,
):
    store, reference = accepted_attachment
    recording_store = _RecordingStore(store)
    adapter = AttachmentContentAdapter(
        recording_store,
        max_render_images=2,
        max_render_bytes=reference.byte_size * 2,
    )

    rendered = adapter.render_messages(_durable_history(reference, 2))

    assert len(rendered) == 2
    assert recording_store.resolved == [reference, reference]


@pytest.mark.parametrize(
    ("max_render_images", "max_render_bytes", "error"),
    [
        (1, len(PNG_BYTES) * 2, "image-count limit"),
        (2, len(PNG_BYTES) * 2 - 1, "aggregate byte limit"),
    ],
)
def test_render_rejects_cumulative_history_before_any_resolve(
    accepted_attachment,
    max_render_images,
    max_render_bytes,
    error,
):
    store, reference = accepted_attachment
    recording_store = _RecordingStore(store)
    adapter = AttachmentContentAdapter(
        recording_store,
        max_render_images=max_render_images,
        max_render_bytes=max_render_bytes,
    )

    with pytest.raises(AttachmentIntegrityError, match=error):
        adapter.render_messages(_durable_history(reference, 2))

    assert recording_store.resolved == []


def test_restore_removes_ephemeral_bytes_without_mutating_input(accepted_attachment):
    store, reference = accepted_attachment
    adapter = AttachmentContentAdapter(store)
    durable = _durable_message(reference)
    rendered = adapter.render_messages(durable)
    rendered_before = copy.deepcopy(rendered)

    restored = adapter.restore_messages(rendered)

    assert rendered == rendered_before
    assert restored == durable
    assert "base64" not in repr(restored)


@pytest.mark.parametrize("tamper", ["image", "marker"])
def test_restore_rejects_tampered_marker_or_image(accepted_attachment, tamper):
    store, reference = accepted_attachment
    adapter = AttachmentContentAdapter(store)
    rendered = adapter.render_messages(_durable_message(reference))
    tampered = copy.deepcopy(rendered)
    if tamper == "image":
        url = tampered[0]["content"][2]["image_url"]["url"]
        tampered[0]["content"][2]["image_url"]["url"] = url[:-1] + "A"
    else:
        tampered[0]["content"][1]["text"] = tampered[0]["content"][1]["text"].replace(
            reference.content_sha256, "0" * 64
        )

    with pytest.raises(AttachmentIntegrityError):
        adapter.restore_messages(tampered)


def test_unknown_reference_fails_before_provider_render(accepted_attachment):
    store, reference = accepted_attachment
    adapter = AttachmentContentAdapter(store)
    unknown_payload = reference.to_json_dict()
    unknown_payload["attachment_id"] = "f" * 32
    messages = [
        {
            "role": "user",
            "content": [{"type": "attachment_ref", "attachment": unknown_payload}],
        }
    ]

    with pytest.raises(AttachmentIntegrityError, match="cannot be resolved"):
        adapter.render_messages(messages)


def test_restore_rejects_unmarked_data_uri(accepted_attachment):
    store, _reference = accepted_attachment
    adapter = AttachmentContentAdapter(store)
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                }
            ],
        }
    ]

    with pytest.raises(AttachmentIntegrityError, match="unmarked data URI"):
        adapter.restore_messages(messages)


def test_restored_reference_can_be_rendered_again_on_next_turn(accepted_attachment):
    store, reference = accepted_attachment
    recording_store = _RecordingStore(store)
    adapter = AttachmentContentAdapter(recording_store)
    durable = _durable_message(reference)

    first_render = adapter.render_messages(durable)
    restored = adapter.restore_messages(first_render)
    second_render = adapter.render_messages(restored)

    assert restored == durable
    assert second_render == first_render
    assert recording_store.resolved == [reference, reference, reference]


def test_plain_text_messages_remain_value_equivalent(accepted_attachment):
    store, _reference = accepted_attachment
    adapter = AttachmentContentAdapter(store)
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "world"}],
        },
    ]

    rendered = adapter.render_messages(messages)
    restored = adapter.restore_messages(rendered)

    assert rendered == messages
    assert restored == messages
    assert rendered is not messages
    assert rendered[0] is not messages[0]
