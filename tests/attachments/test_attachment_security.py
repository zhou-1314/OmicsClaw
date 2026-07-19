from __future__ import annotations

import base64
import hashlib
import sqlite3
import stat

import pytest

from omicsclaw.attachments import (
    AttachmentIntegrityError,
    AttachmentStore,
    SourceAttachmentDescriptorV1,
)
from omicsclaw.attachments.schema import MIGRATION_1_SHA256, MIGRATION_1_SQL


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_attachment_schema_migration_checksum_is_pinned():
    assert (
        hashlib.sha256(MIGRATION_1_SQL.encode("utf-8")).hexdigest()
        == MIGRATION_1_SHA256
    )


class Source:
    async def open(self, _source_attachment_id: str):
        yield PNG_BYTES


@pytest.mark.asyncio
async def test_private_modes_cover_database_directories_and_blobs(tmp_path):
    descriptor = SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id="photo-1",
        display_name="photo.png",
        declared_media_type="image/png",
        declared_size=len(PNG_BYTES),
        declared_sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
    )
    store = AttachmentStore(tmp_path)
    try:
        publication = await store.publish_batch(
            proposed_turn_id="1" * 32,
            proposed_conversation_id="2" * 32,
            descriptors=(descriptor,),
            source=Source(),
        )
        reference = publication.references[0]
        blob_path = (
            store.blob_root / reference.content_sha256[:2] / reference.content_sha256
        )

        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.blob_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.staging_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.database_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(blob_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(blob_path.stat().st_mode) == 0o600
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resolve_rejects_blob_replaced_by_symlink(tmp_path):
    descriptor = SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id="photo-1",
        display_name="photo.png",
        declared_media_type="image/png",
        declared_size=len(PNG_BYTES),
        declared_sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
    )
    store = AttachmentStore(tmp_path / "state")
    try:
        publication = await store.publish_batch(
            proposed_turn_id="1" * 32,
            proposed_conversation_id="2" * 32,
            descriptors=(descriptor,),
            source=Source(),
        )
        reference = store.accept_batch(publication.commitment)[0]
        blob_path = (
            store.blob_root / reference.content_sha256[:2] / reference.content_sha256
        )
        external = tmp_path / "external.png"
        external.write_bytes(PNG_BYTES)
        blob_path.unlink()
        blob_path.symlink_to(external)

        with pytest.raises(AttachmentIntegrityError, match="unsafe"):
            store.resolve_bytes(reference)
    finally:
        store.close()


def test_store_rejects_symlink_root(tmp_path):
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(AttachmentIntegrityError, match="symlink"):
        AttachmentStore(linked_root)


def test_store_rejects_symlink_database(tmp_path):
    external = tmp_path / "external.db"
    external.touch()
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "attachments.db").symlink_to(external)

    with pytest.raises(AttachmentIntegrityError, match="symlink"):
        AttachmentStore(state_root)


def test_reopen_rejects_modified_migration_checksum(tmp_path):
    store = AttachmentStore(tmp_path)
    store.close()
    with sqlite3.connect(tmp_path / "attachments.db") as connection:
        connection.execute(
            "UPDATE attachment_schema_migrations SET checksum_sha256 = ?",
            ("0" * 64,),
        )

    with pytest.raises(AttachmentIntegrityError, match="modified migrations"):
        AttachmentStore(tmp_path, require_existing=True)
