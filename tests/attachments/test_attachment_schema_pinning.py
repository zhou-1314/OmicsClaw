"""Attachment Store migration source pinning and the v1->v2 upgrade path.

Two gaps this closes:

* Migration 2's expected checksum must be a fixed source literal, not a value
  recomputed from the SQL at import time.  A computed checksum always equals its
  own source, so ``verify_migration_source`` could never notice an accidental
  edit to ``MIGRATION_2_SQL``.
* The retention tests all build the newest schema directly.  Production instead
  upgrades a database that already holds accepted Records/Blobs from Migration 1
  only, and that path was untested.
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3

import pytest

from omicsclaw.attachments import AttachmentStore, SourceAttachmentDescriptorV1
from omicsclaw.attachments import schema
from omicsclaw.attachments import store as store_module


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
TURN_ID = "1" * 32
CONVERSATION_ID = "2" * 32


class _OneShotSource:
    async def open(self, _source_attachment_id: str):
        yield PNG_BYTES


def _png_descriptor() -> SourceAttachmentDescriptorV1:
    return SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id="photo-1",
        display_name="cells.png",
        declared_media_type="image/png",
        declared_size=len(PNG_BYTES),
        declared_sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
    )


def _applied_versions(database_path) -> list[int]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT version FROM attachment_schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        connection.close()
    return [int(row[0]) for row in rows]


def test_migration_2_checksum_is_a_pinned_literal_not_a_tautology():
    # Editing MIGRATION_2_SQL changes its computed hash but not the pinned
    # literal, so this guard fails until the literal is updated on purpose —
    # exactly the drift detection a self-recomputed checksum cannot provide.
    assert (
        schema.MIGRATION_2_SHA256
        == hashlib.sha256(schema.MIGRATION_2_SQL.encode("utf-8")).hexdigest()
    )
    assert schema.MIGRATION_2_SHA256 == (
        "cc659fb02bd351d89d73a61891f264909af99a1444189cf255683e80e1c6a305"
    )
    # The pinned pair is what the migration ships to the database, and the
    # source self-check must accept it.
    assert schema.MIGRATIONS[1][3] == schema.MIGRATION_2_SHA256
    schema.verify_migration_source()


@pytest.mark.asyncio
async def test_attachment_store_upgrades_migration_1_db_preserving_records(
    tmp_path, monkeypatch
):
    # First process: only Migration 1 exists, as in a database created before
    # the blob-retention migration shipped.
    monkeypatch.setattr(store_module, "MIGRATIONS", (store_module.MIGRATIONS[0],))
    v1 = AttachmentStore(tmp_path)
    try:
        publication = await v1.publish_batch(
            proposed_turn_id=TURN_ID,
            proposed_conversation_id=CONVERSATION_ID,
            descriptors=(_png_descriptor(),),
            source=_OneShotSource(),
        )
        references = v1.accept_batch(publication.commitment)
        assert len(references) == 1
        content_sha256 = references[0].content_sha256
    finally:
        v1.close()
    assert _applied_versions(tmp_path / "attachments.db") == [1]

    # Second process: the full migration set is present, so opening the same
    # database applies Migration 2 in place.
    monkeypatch.undo()
    upgraded = AttachmentStore(tmp_path, require_existing=True)
    try:
        assert _applied_versions(upgraded.database_path) == [1, 2]

        # The pre-existing accepted Record and Blob survive the upgrade intact.
        surviving = upgraded.get_turn_references(TURN_ID, CONVERSATION_ID)
        assert len(surviving) == 1
        assert surviving[0].content_sha256 == content_sha256
        assert upgraded.resolve_bytes(surviving[0]) == PNG_BYTES

        # Migration 2's retention machinery is usable against the old Blob.
        claim = upgraded.claim_blob_retention(
            content_sha256, holder_kind="external", holder_ref="run-1"
        )
        assert claim
    finally:
        upgraded.close()
