from __future__ import annotations

import base64
from dataclasses import replace
import hashlib

import pytest

from omicsclaw.attachments import (
    AttachmentIntegrityError,
    AttachmentNotAcceptedError,
    AttachmentRejectedError,
    AttachmentStoreRecoveryResult,
    AttachmentStore,
    SourceAttachmentDescriptorV1,
)


TURN_ID = "1" * 32
CONVERSATION_ID = "2" * 32
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + (b"\x00" * 16)
GIF_BYTES = b"GIF89a" + (b"\x00" * 14)
WEBP_BYTES = b"RIFF\x0c\x00\x00\x00WEBP" + (b"\x00" * 8)


class BytesSource:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values
        self.opens: list[str] = []

    async def open(self, source_attachment_id: str):
        self.opens.append(source_attachment_id)
        payload = self.values[source_attachment_id]
        midpoint = max(1, len(payload) // 2)
        yield payload[:midpoint]
        yield payload[midpoint:]


class ManualClock:
    def __init__(self, now_ms: int = 1_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


def png_descriptor(source_id: str = "photo-1", ordinal: int = 0):
    return SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=ordinal,
        source_attachment_id=source_id,
        display_name=f"cell-map-{ordinal}.png",
        declared_media_type="image/png",
        declared_size=len(PNG_BYTES),
        declared_sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
    )


@pytest.mark.asyncio
async def test_publish_accept_and_resolve_one_attachment(tmp_path):
    source = BytesSource({"photo-1": PNG_BYTES})
    descriptor = SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id="photo-1",
        display_name="cell-map.png",
        declared_media_type="image/png",
        declared_size=len(PNG_BYTES),
        declared_sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
    )
    store = AttachmentStore(tmp_path)
    try:
        publication = await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(descriptor,),
            source=source,
        )

        assert source.opens == ["photo-1"]
        assert publication.commitment.store_id == store.store_id
        assert publication.commitment.turn_id == TURN_ID
        assert publication.commitment.conversation_id == CONVERSATION_ID
        assert publication.commitment.record_count == 1
        assert store.get_turn_references(TURN_ID, CONVERSATION_ID) == ()

        references = store.accept_batch(publication.commitment)

        assert references == publication.references
        assert store.get_turn_references(TURN_ID, CONVERSATION_ID) == references
        assert store.resolve_bytes(references[0]) == PNG_BYTES
        assert references[0].content_sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
        assert references[0].media_type == "image/png"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reconcile_removes_untracked_blob_after_publish_crash(tmp_path):
    clock = ManualClock()

    def crash_after_blob_publish(checkpoint: str) -> None:
        if checkpoint == "after_blob_publish":
            raise RuntimeError("simulated process crash")

    store = AttachmentStore(
        tmp_path,
        clock_ms=clock,
        provisional_grace_ms=100,
        fault_hook=crash_after_blob_publish,
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor(),),
            source=BytesSource({"photo-1": PNG_BYTES}),
        )
    store.close()

    blob_root = tmp_path / "attachment_blobs"
    assert [path for path in blob_root.rglob("*") if path.is_file()]

    clock.now_ms += 101
    recovered = AttachmentStore(
        tmp_path,
        clock_ms=clock,
        provisional_grace_ms=100,
        require_existing=True,
    )
    try:
        result = recovered.reconcile(())

        assert isinstance(result, AttachmentStoreRecoveryResult)
        assert not [path for path in blob_root.rglob("*") if path.is_file()]
        assert result.deleted_orphan_blob_count == 1
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_batch_is_all_or_nothing_when_one_attachment_fails(tmp_path):
    clock = ManualClock()
    source = BytesSource({"photo-1": PNG_BYTES, "photo-2": PNG_BYTES})
    invalid_second = SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=1,
        source_attachment_id="photo-2",
        display_name="second.png",
        declared_media_type="image/png",
        declared_size=len(PNG_BYTES),
        declared_sha256="0" * 64,
    )
    store = AttachmentStore(
        tmp_path,
        clock_ms=clock,
        provisional_grace_ms=100,
    )
    try:
        with pytest.raises(AttachmentRejectedError, match="digest"):
            await store.publish_batch(
                proposed_turn_id=TURN_ID,
                proposed_conversation_id=CONVERSATION_ID,
                descriptors=(png_descriptor(), invalid_second),
                source=source,
            )

        assert source.opens == ["photo-1", "photo-2"]
        assert store.get_turn_references(TURN_ID, CONVERSATION_ID) == ()
        clock.now_ms += 101
        result = store.reconcile(())
        assert result.deleted_orphan_blob_count == 1
        assert not [path for path in store.blob_root.rglob("*") if path.is_file()]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_equal_bytes_create_distinct_records_that_share_one_blob(tmp_path):
    store = AttachmentStore(tmp_path)
    try:
        first = await store.publish_batch(
            proposed_turn_id="3" * 32,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor("first"),),
            source=BytesSource({"first": PNG_BYTES}),
        )
        second = await store.publish_batch(
            proposed_turn_id="4" * 32,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor("second"),),
            source=BytesSource({"second": PNG_BYTES}),
        )
        store.accept_batch(first.commitment)
        store.accept_batch(second.commitment)

        assert first.references[0].attachment_id != second.references[0].attachment_id
        assert first.references[0].content_sha256 == second.references[0].content_sha256
        assert store.resolve_bytes(first.references[0]) == PNG_BYTES
        assert store.resolve_bytes(second.references[0]) == PNG_BYTES
        assert len([path for path in store.blob_root.rglob("*") if path.is_file()]) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reconcile_promotes_control_committed_batch_after_restart(tmp_path):
    first_process = AttachmentStore(tmp_path)
    publication = await first_process.publish_batch(
        proposed_turn_id=TURN_ID,
        proposed_conversation_id=CONVERSATION_ID,
        descriptors=(png_descriptor(),),
        source=BytesSource({"photo-1": PNG_BYTES}),
    )
    first_process.close()

    recovered = AttachmentStore(tmp_path, require_existing=True)
    try:
        result = recovered.reconcile((publication.commitment,))

        assert result.accepted_batch_ids == (publication.commitment.batch_id,)
        assert (
            recovered.get_turn_references(TURN_ID, CONVERSATION_ID)
            == publication.references
        )
        assert recovered.resolve_bytes(publication.references[0]) == PNG_BYTES
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_reconcile_abandons_expired_uncommitted_batch(tmp_path):
    clock = ManualClock()
    store = AttachmentStore(
        tmp_path,
        clock_ms=clock,
        provisional_grace_ms=100,
    )
    try:
        publication = await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor(),),
            source=BytesSource({"photo-1": PNG_BYTES}),
        )
        clock.now_ms += 101

        result = store.reconcile(())

        assert result.abandoned_batch_ids == (publication.commitment.batch_id,)
        assert result.deleted_orphan_record_count == 1
        assert result.deleted_orphan_blob_count == 1
        assert store.get_turn_references(TURN_ID, CONVERSATION_ID) == ()
        with pytest.raises(AttachmentRejectedError):
            store.resolve_bytes(publication.references[0])
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reconcile_fails_closed_if_accepted_batch_lacks_commitment(tmp_path):
    store = AttachmentStore(tmp_path)
    try:
        publication = await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor(),),
            source=BytesSource({"photo-1": PNG_BYTES}),
        )
        store.accept_batch(publication.commitment)

        with pytest.raises(AttachmentIntegrityError, match="authoritative"):
            store.reconcile(())
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_type", "payload"),
    (
        ("image/jpeg", JPEG_BYTES),
        ("image/png", PNG_BYTES),
        ("image/gif", GIF_BYTES),
        ("image/webp", WEBP_BYTES),
    ),
)
async def test_first_cut_accepts_only_supported_image_signatures(
    tmp_path, media_type, payload
):
    descriptor = SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id="image-1",
        display_name="image.bin",
        declared_media_type=media_type,
        declared_size=len(payload),
        declared_sha256=hashlib.sha256(payload).hexdigest(),
    )
    store = AttachmentStore(tmp_path)
    try:
        publication = await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(descriptor,),
            source=BytesSource({"image-1": payload}),
        )
        assert publication.references[0].media_type == media_type
    finally:
        store.close()


@pytest.mark.asyncio
async def test_non_image_bytes_are_rejected_without_publication(tmp_path):
    descriptor = SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id="document-1",
        display_name="notes.txt",
        declared_media_type=None,
        declared_size=None,
        declared_sha256=None,
    )
    store = AttachmentStore(tmp_path)
    try:
        with pytest.raises(AttachmentRejectedError, match="image"):
            await store.publish_batch(
                proposed_turn_id=TURN_ID,
                proposed_conversation_id=CONVERSATION_ID,
                descriptors=(descriptor,),
                source=BytesSource({"document-1": b"not an image"}),
            )
        assert store.get_turn_references(TURN_ID, CONVERSATION_ID) == ()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_declared_oversize_is_rejected_before_opening_source(tmp_path):
    source = BytesSource({"photo-1": PNG_BYTES})
    descriptor = png_descriptor()
    store = AttachmentStore(
        tmp_path,
        max_attachment_bytes=len(PNG_BYTES) - 1,
        max_batch_bytes=len(PNG_BYTES),
    )
    try:
        with pytest.raises(AttachmentRejectedError, match="declared"):
            await store.publish_batch(
                proposed_turn_id=TURN_ID,
                proposed_conversation_id=CONVERSATION_ID,
                descriptors=(descriptor,),
                source=source,
            )
        assert source.opens == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_corrupted_accepted_blob_stays_an_integrity_incident(tmp_path):
    store = AttachmentStore(tmp_path)
    try:
        publication = await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor(),),
            source=BytesSource({"photo-1": PNG_BYTES}),
        )
        reference = store.accept_batch(publication.commitment)[0]
        blob_path = (
            store.blob_root / reference.content_sha256[:2] / reference.content_sha256
        )
        blob_path.write_bytes(b"X" + PNG_BYTES[1:])

        with pytest.raises(AttachmentIntegrityError, match="digest"):
            store.resolve_bytes(reference)
        assert (
            store.get_turn_references(TURN_ID, CONVERSATION_ID)
            == publication.references
        )
        with pytest.raises(AttachmentIntegrityError, match="integrity"):
            store.resolve_bytes(reference)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_accept_is_idempotent_after_post_commit_fault(tmp_path):
    fired = False

    def fail_once(checkpoint: str) -> None:
        nonlocal fired
        if checkpoint == "after_batch_accept" and not fired:
            fired = True
            raise RuntimeError("lost acknowledgement")

    store = AttachmentStore(tmp_path, fault_hook=fail_once)
    try:
        publication = await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor(),),
            source=BytesSource({"photo-1": PNG_BYTES}),
        )

        with pytest.raises(RuntimeError, match="lost acknowledgement"):
            store.accept_batch(publication.commitment)
        assert store.accept_batch(publication.commitment) == publication.references
        assert (
            store.get_turn_references(TURN_ID, CONVERSATION_ID)
            == publication.references
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_accept_rejects_tampered_commitment(tmp_path):
    store = AttachmentStore(tmp_path)
    try:
        publication = await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor(),),
            source=BytesSource({"photo-1": PNG_BYTES}),
        )
        tampered = replace(publication.commitment, records_sha256="f" * 64)

        with pytest.raises(AttachmentIntegrityError, match="commitment"):
            store.accept_batch(tampered)
        assert store.get_turn_references(TURN_ID, CONVERSATION_ID) == ()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_gc_keeps_blob_still_referenced_by_accepted_record(tmp_path):
    clock = ManualClock()
    store = AttachmentStore(
        tmp_path,
        clock_ms=clock,
        provisional_grace_ms=100,
    )
    try:
        orphan = await store.publish_batch(
            proposed_turn_id="3" * 32,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor("orphan"),),
            source=BytesSource({"orphan": PNG_BYTES}),
        )
        retained = await store.publish_batch(
            proposed_turn_id="4" * 32,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor("retained"),),
            source=BytesSource({"retained": PNG_BYTES}),
        )
        store.accept_batch(retained.commitment)
        clock.now_ms += 101

        result = store.reconcile((retained.commitment,))

        assert result.abandoned_batch_ids == (orphan.commitment.batch_id,)
        assert result.deleted_orphan_record_count == 1
        assert result.deleted_orphan_blob_count == 0
        assert store.resolve_bytes(retained.references[0]) == PNG_BYTES
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resolve_requires_acceptance_and_honors_read_bound(tmp_path):
    store = AttachmentStore(tmp_path)
    try:
        publication = await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor(),),
            source=BytesSource({"photo-1": PNG_BYTES}),
        )
        reference = publication.references[0]
        with pytest.raises(AttachmentNotAcceptedError):
            store.resolve_bytes(reference)

        store.accept_batch(publication.commitment)
        with pytest.raises(AttachmentRejectedError, match="resolve byte limit"):
            store.resolve_bytes(reference, max_bytes=len(PNG_BYTES) - 1)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_publish_reverifies_every_blob_before_returning_commitment(tmp_path):
    digest = hashlib.sha256(PNG_BYTES).hexdigest()

    def corrupt_after_blob_row(checkpoint: str) -> None:
        if checkpoint == "after_blob_row":
            blob_path = tmp_path / "attachment_blobs" / digest[:2] / digest
            blob_path.write_bytes(b"X" + PNG_BYTES[1:])

    store = AttachmentStore(tmp_path, fault_hook=corrupt_after_blob_row)
    try:
        with pytest.raises(AttachmentIntegrityError, match="digest"):
            await store.publish_batch(
                proposed_turn_id=TURN_ID,
                proposed_conversation_id=CONVERSATION_ID,
                descriptors=(png_descriptor(),),
                source=BytesSource({"photo-1": PNG_BYTES}),
            )
        assert store.get_turn_references(TURN_ID, CONVERSATION_ID) == ()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_stream_is_closed_when_actual_bytes_exceed_limit(tmp_path):
    closed = False

    class OversizeSource:
        async def open(self, _source_attachment_id: str):
            nonlocal closed
            try:
                yield PNG_BYTES
            finally:
                closed = True

    descriptor = SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id="photo-1",
        display_name="photo.png",
        declared_media_type=None,
        declared_size=None,
        declared_sha256=None,
    )
    store = AttachmentStore(
        tmp_path,
        max_attachment_bytes=16,
        max_batch_bytes=32,
    )
    try:
        with pytest.raises(AttachmentRejectedError, match="actual"):
            await store.publish_batch(
                proposed_turn_id=TURN_ID,
                proposed_conversation_id=CONVERSATION_ID,
                descriptors=(descriptor,),
                source=OversizeSource(),
            )
        assert closed
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reconcile_cleans_staging_directory_after_early_fault(tmp_path):
    clock = ManualClock()

    def fail_after_batch_staging(checkpoint: str) -> None:
        if checkpoint == "after_batch_staging":
            raise RuntimeError("crash before byte access")

    store = AttachmentStore(
        tmp_path,
        clock_ms=clock,
        provisional_grace_ms=100,
        fault_hook=fail_after_batch_staging,
    )
    with pytest.raises(RuntimeError, match="before byte access"):
        await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor(),),
            source=BytesSource({"photo-1": PNG_BYTES}),
        )
    store.close()
    assert list((tmp_path / "attachment_staging").iterdir())

    clock.now_ms += 101
    recovered = AttachmentStore(
        tmp_path,
        clock_ms=clock,
        provisional_grace_ms=100,
        require_existing=True,
    )
    try:
        result = recovered.reconcile(())
        assert len(result.abandoned_batch_ids) == 1
        assert not list(recovered.staging_root.iterdir())
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_control_committed_corruption_becomes_sticky_integrity_record(tmp_path):
    store = AttachmentStore(tmp_path)
    try:
        publication = await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(png_descriptor(),),
            source=BytesSource({"photo-1": PNG_BYTES}),
        )
        reference = publication.references[0]
        blob_path = (
            store.blob_root / reference.content_sha256[:2] / reference.content_sha256
        )
        blob_path.write_bytes(b"X" + PNG_BYTES[1:])

        with pytest.raises(AttachmentIntegrityError, match="digest"):
            store.accept_batch(publication.commitment)
        assert (
            store.get_turn_references(TURN_ID, CONVERSATION_ID)
            == publication.references
        )
        with pytest.raises(AttachmentIntegrityError, match="recorded integrity"):
            store.accept_batch(publication.commitment)
        with pytest.raises(AttachmentIntegrityError, match="recorded integrity"):
            store.resolve_bytes(reference)
    finally:
        store.close()
