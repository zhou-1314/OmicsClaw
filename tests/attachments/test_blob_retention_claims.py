"""ADR 0059 durable Blob retention for governed external references.

An accepted Attachment Record already pins its Blob.  These tests cover the
other half of the ADR's retention rule -- "no accepted Record, **Run input** or
other governed durable reference requires it" -- which had no representation
before.  The claims are stored inside ``attachments.db`` rather than resolved
through a caller-supplied predicate, so a cross-store lookup that fails or
races cannot be mistaken for "unreferenced".
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3

import pytest

from omicsclaw.attachments import (
    AttachmentNotAcceptedError,
    AttachmentRejectedError,
    AttachmentStore,
    SourceAttachmentDescriptorV1,
)


TURN_ID = "1" * 32
CONVERSATION_ID = "2" * 32
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()


class BytesSource:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values

    async def open(self, source_attachment_id: str):
        yield self.values[source_attachment_id]


class ManualClock:
    def __init__(self, now_ms: int = 1_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


def _descriptor() -> SourceAttachmentDescriptorV1:
    return SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id="photo-1",
        display_name="image.png",
        declared_media_type="image/png",
        declared_size=len(PNG_BYTES),
        declared_sha256=PNG_SHA256,
    )


async def _publish_orphan_blob(store: AttachmentStore) -> str:
    """Publish a batch and abandon it, leaving a Record-free Blob behind."""

    publication = await store.publish_batch(
        proposed_turn_id=TURN_ID,
        proposed_conversation_id=CONVERSATION_ID,
        descriptors=(_descriptor(),),
        source=BytesSource({"photo-1": PNG_BYTES}),
    )
    store.abandon_batch(publication.commitment.batch_id)
    return PNG_SHA256


@pytest.mark.asyncio
async def test_retention_claim_keeps_an_otherwise_collectable_blob(tmp_path):
    clock = ManualClock()
    store = AttachmentStore(tmp_path, clock_ms=clock, provisional_grace_ms=100)
    try:
        digest = await _publish_orphan_blob(store)
        store.claim_blob_retention(
            digest, holder_kind="run_input", holder_ref="run-abc"
        )

        clock.now_ms += 1_000
        result = store.reconcile(())

        assert result.deleted_orphan_blob_count == 0
        assert [path for path in store.blob_root.rglob("*") if path.is_file()]
        assert store.blob_retention_holders(digest) == (("run_input", "run-abc"),)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_releasing_the_last_claim_makes_the_blob_collectable_again(tmp_path):
    clock = ManualClock()
    store = AttachmentStore(tmp_path, clock_ms=clock, provisional_grace_ms=100)
    try:
        digest = await _publish_orphan_blob(store)
        store.claim_blob_retention(
            digest, holder_kind="run_input", holder_ref="run-abc"
        )
        clock.now_ms += 1_000
        assert store.reconcile(()).deleted_orphan_blob_count == 0

        released = store.release_blob_retention(
            holder_kind="run_input", holder_ref="run-abc"
        )
        assert released == 1
        assert store.blob_retention_holders(digest) == ()

        assert store.reconcile(()).deleted_orphan_blob_count == 1
        assert not [path for path in store.blob_root.rglob("*") if path.is_file()]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_multiple_holders_each_independently_pin_one_blob(tmp_path):
    clock = ManualClock()
    store = AttachmentStore(tmp_path, clock_ms=clock, provisional_grace_ms=100)
    try:
        digest = await _publish_orphan_blob(store)
        store.claim_blob_retention(
            digest, holder_kind="run_input", holder_ref="run-abc"
        )
        store.claim_blob_retention(
            digest, holder_kind="transcript", holder_ref="turn-xyz"
        )

        store.release_blob_retention(holder_kind="run_input", holder_ref="run-abc")
        clock.now_ms += 1_000
        assert store.reconcile(()).deleted_orphan_blob_count == 0

        store.release_blob_retention(holder_kind="transcript", holder_ref="turn-xyz")
        assert store.reconcile(()).deleted_orphan_blob_count == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_claiming_is_idempotent_per_holder_and_blob(tmp_path):
    store = AttachmentStore(tmp_path, provisional_grace_ms=100)
    try:
        digest = await _publish_orphan_blob(store)
        first = store.claim_blob_retention(
            digest, holder_kind="run_input", holder_ref="run-abc"
        )
        second = store.claim_blob_retention(
            digest, holder_kind="run_input", holder_ref="run-abc"
        )
        assert first == second
        assert store.blob_retention_holders(digest) == (("run_input", "run-abc"),)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_database_refuses_to_delete_a_retained_blob(tmp_path):
    """Defense in depth: the guarantee does not depend on the GC query."""

    store = AttachmentStore(tmp_path, provisional_grace_ms=100)
    try:
        digest = await _publish_orphan_blob(store)
        store.claim_blob_retention(
            digest, holder_kind="external", holder_ref="kg-packet-1"
        )
        # Bypass `_garbage_collect_orphans` entirely and attempt the raw delete
        # a future buggy collector might issue.
        with pytest.raises(sqlite3.IntegrityError, match="retained Attachment Blob"):
            with store._transaction() as connection:
                connection.execute(
                    "DELETE FROM attachment_blobs WHERE content_sha256 = ?",
                    (digest,),
                )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_retention_claims_survive_restart(tmp_path):
    clock = ManualClock()
    store = AttachmentStore(tmp_path, clock_ms=clock, provisional_grace_ms=100)
    try:
        digest = await _publish_orphan_blob(store)
        store.claim_blob_retention(
            digest, holder_kind="run_input", holder_ref="run-abc"
        )
    finally:
        store.close()

    clock.now_ms += 1_000
    reopened = AttachmentStore(
        tmp_path, clock_ms=clock, provisional_grace_ms=100, require_existing=True
    )
    try:
        assert reopened.blob_retention_holders(digest) == (("run_input", "run-abc"),)
        assert reopened.reconcile(()).deleted_orphan_blob_count == 0
        assert [path for path in reopened.blob_root.rglob("*") if path.is_file()]
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_claim_validation_fails_closed(tmp_path):
    store = AttachmentStore(tmp_path, provisional_grace_ms=100)
    try:
        digest = await _publish_orphan_blob(store)

        with pytest.raises(AttachmentRejectedError, match="holder_kind"):
            store.claim_blob_retention(
                digest, holder_kind="whatever", holder_ref="run-abc"
            )
        with pytest.raises(AttachmentRejectedError, match="holder_ref"):
            store.claim_blob_retention(digest, holder_kind="run_input", holder_ref="")
        with pytest.raises(AttachmentRejectedError, match="SHA-256"):
            store.claim_blob_retention(
                "not-a-digest", holder_kind="run_input", holder_ref="run-abc"
            )
        with pytest.raises(AttachmentNotAcceptedError):
            store.claim_blob_retention(
                "0" * 64, holder_kind="run_input", holder_ref="run-abc"
            )
        assert store.blob_retention_holders(digest) == ()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_accepted_record_and_claim_pin_independently(tmp_path):
    """A claim must not be needed for, nor weaken, ordinary Record retention."""

    clock = ManualClock()
    store = AttachmentStore(tmp_path, clock_ms=clock, provisional_grace_ms=100)
    try:
        publication = await store.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(_descriptor(),),
            source=BytesSource({"photo-1": PNG_BYTES}),
        )
        references = store.accept_batch(publication.commitment)
        assert len(references) == 1

        clock.now_ms += 1_000
        assert (
            store.reconcile((publication.commitment,)).deleted_orphan_blob_count == 0
        )
        assert store.blob_retention_holders(PNG_SHA256) == ()
        assert store.resolve_bytes(references[0]) == PNG_BYTES
    finally:
        store.close()
