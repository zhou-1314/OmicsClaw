"""Checksum-pinned strict SQLite schema owned only by AttachmentStore."""

from __future__ import annotations

import hashlib


MIGRATION_1_SQL = r"""
CREATE TABLE attachment_schema_migrations (
    version             INTEGER PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    checksum_sha256     TEXT NOT NULL,
    applied_at_ms       INTEGER NOT NULL
) STRICT;

CREATE TABLE attachment_store_identity (
    singleton           INTEGER PRIMARY KEY CHECK (singleton = 1),
    store_id            TEXT NOT NULL UNIQUE
) STRICT;

INSERT INTO attachment_store_identity (singleton, store_id)
VALUES (1, lower(hex(randomblob(16))));

CREATE TABLE attachment_blobs (
    content_sha256      TEXT PRIMARY KEY,
    byte_size           INTEGER NOT NULL CHECK (byte_size >= 0),
    relative_path       TEXT NOT NULL UNIQUE,
    created_at_ms       INTEGER NOT NULL,
    verified_at_ms      INTEGER NOT NULL
) STRICT;

CREATE TABLE attachment_batches (
    batch_id                 TEXT PRIMARY KEY,
    proposed_turn_id         TEXT NOT NULL UNIQUE,
    proposed_conversation_id TEXT NOT NULL,
    records_sha256           TEXT NULL,
    record_count             INTEGER NULL CHECK (record_count IS NULL OR record_count > 0),
    state                    TEXT NOT NULL CHECK (state IN
                                  ('staging','published','accepted','abandoned')),
    created_at_ms            INTEGER NOT NULL,
    expires_at_ms            INTEGER NOT NULL,
    updated_at_ms            INTEGER NOT NULL,
    CHECK ((state = 'staging' AND records_sha256 IS NULL AND record_count IS NULL)
        OR (state != 'staging' AND records_sha256 IS NOT NULL AND record_count IS NOT NULL))
) STRICT;

CREATE TABLE attachment_records (
    attachment_id            TEXT PRIMARY KEY,
    batch_id                 TEXT NOT NULL
                                  REFERENCES attachment_batches(batch_id) ON DELETE RESTRICT,
    turn_id                  TEXT NOT NULL,
    conversation_id          TEXT NOT NULL,
    ordinal                  INTEGER NOT NULL CHECK (ordinal >= 0),
    content_sha256           TEXT NOT NULL
                                  REFERENCES attachment_blobs(content_sha256) ON DELETE RESTRICT,
    byte_size                INTEGER NOT NULL CHECK (byte_size >= 0),
    display_name             TEXT NOT NULL,
    declared_media_type      TEXT NULL,
    detected_media_type      TEXT NOT NULL,
    source_descriptor_json   TEXT NOT NULL,
    source_descriptor_sha256 TEXT NOT NULL,
    state                    TEXT NOT NULL CHECK (state IN
                                  ('provisional','accepted','orphaned','integrity_failed')),
    created_at_ms            INTEGER NOT NULL,
    accepted_at_ms           INTEGER NULL,
    integrity_code           TEXT NULL,
    UNIQUE (turn_id, ordinal),
    CHECK ((state IN ('accepted','integrity_failed') AND accepted_at_ms IS NOT NULL)
        OR (state IN ('provisional','orphaned') AND accepted_at_ms IS NULL))
) STRICT;

CREATE INDEX attachment_batches_state_expiry_idx
    ON attachment_batches(state, expires_at_ms, updated_at_ms);
CREATE INDEX attachment_records_batch_ordinal_idx
    ON attachment_records(batch_id, ordinal);
CREATE INDEX attachment_records_turn_ordinal_idx
    ON attachment_records(turn_id, ordinal);

CREATE TRIGGER attachment_store_identity_immutable_update
BEFORE UPDATE ON attachment_store_identity
BEGIN
  SELECT RAISE(ABORT, 'immutable Attachment Store identity');
END;

CREATE TRIGGER attachment_store_identity_immutable_delete
BEFORE DELETE ON attachment_store_identity
BEGIN
  SELECT RAISE(ABORT, 'immutable Attachment Store identity');
END;

CREATE TRIGGER attachment_blobs_immutable_update
BEFORE UPDATE ON attachment_blobs
WHEN NEW.content_sha256 != OLD.content_sha256
  OR NEW.byte_size != OLD.byte_size
  OR NEW.relative_path != OLD.relative_path
  OR NEW.created_at_ms != OLD.created_at_ms
BEGIN
  SELECT RAISE(ABORT, 'immutable Attachment Blob');
END;

CREATE TRIGGER attachment_blob_delete_requires_unreferenced
BEFORE DELETE ON attachment_blobs
WHEN EXISTS (
  SELECT 1 FROM attachment_records WHERE content_sha256 = OLD.content_sha256
)
BEGIN
  SELECT RAISE(ABORT, 'referenced Attachment Blob cannot be deleted');
END;

CREATE TRIGGER attachment_batches_identity_immutable
BEFORE UPDATE ON attachment_batches
WHEN NEW.batch_id != OLD.batch_id
  OR NEW.proposed_turn_id != OLD.proposed_turn_id
  OR NEW.proposed_conversation_id != OLD.proposed_conversation_id
  OR NEW.created_at_ms != OLD.created_at_ms
BEGIN
  SELECT RAISE(ABORT, 'immutable Attachment batch identity');
END;

CREATE TRIGGER attachment_batches_state_transition
BEFORE UPDATE OF state ON attachment_batches
WHEN NOT (
  (OLD.state = 'staging' AND NEW.state IN ('published','abandoned'))
  OR (OLD.state = 'published' AND NEW.state IN ('accepted','abandoned'))
  OR NEW.state = OLD.state
)
BEGIN
  SELECT RAISE(ABORT, 'invalid Attachment batch state transition');
END;

CREATE TRIGGER attachment_records_identity_immutable
BEFORE UPDATE ON attachment_records
WHEN NEW.attachment_id != OLD.attachment_id
  OR NEW.batch_id != OLD.batch_id
  OR NEW.turn_id != OLD.turn_id
  OR NEW.conversation_id != OLD.conversation_id
  OR NEW.ordinal != OLD.ordinal
  OR NEW.content_sha256 != OLD.content_sha256
  OR NEW.byte_size != OLD.byte_size
  OR NEW.display_name != OLD.display_name
  OR OLD.declared_media_type IS NOT NEW.declared_media_type
  OR NEW.detected_media_type != OLD.detected_media_type
  OR NEW.source_descriptor_json != OLD.source_descriptor_json
  OR NEW.source_descriptor_sha256 != OLD.source_descriptor_sha256
  OR NEW.created_at_ms != OLD.created_at_ms
BEGIN
  SELECT RAISE(ABORT, 'immutable Attachment Record identity');
END;

CREATE TRIGGER attachment_records_state_transition
BEFORE UPDATE OF state ON attachment_records
WHEN NOT (
  (OLD.state = 'provisional' AND NEW.state IN
      ('accepted','orphaned','integrity_failed'))
  OR (OLD.state = 'accepted' AND NEW.state = 'integrity_failed')
  OR NEW.state = OLD.state
)
BEGIN
  SELECT RAISE(ABORT, 'invalid Attachment Record state transition');
END;

CREATE TRIGGER accepted_attachment_record_cannot_delete
BEFORE DELETE ON attachment_records
WHEN OLD.state IN ('accepted','integrity_failed')
BEGIN
  SELECT RAISE(ABORT, 'accepted Attachment Record cannot be deleted');
END;
"""

# Runtime recomputes this before opening a database so both source drift and
# modification of a previously applied migration fail closed.
MIGRATION_1_SHA256 = "213d7671f6695bdd8f0d5faf88edcbd578d8d543d7538c60a228e3f23304bef2"


# ADR 0059 keeps a Blob alive until no accepted Attachment Record, Run input, or
# other governed durable reference needs it.  Records are covered by the
# migration 1 trigger; this migration adds the durable external half.
#
# The claims deliberately live inside `attachments.db` rather than behind a
# caller-supplied predicate: a cross-store lookup that fails, times out, or
# races a concurrent publisher would otherwise read as "unreferenced" and
# delete accepted content.  A local row plus a BEFORE DELETE trigger makes the
# guarantee fail closed even if a future garbage-collection query forgets it.
MIGRATION_2_SQL = r"""
CREATE TABLE attachment_blob_retention_claims (
    claim_id            TEXT PRIMARY KEY,
    content_sha256      TEXT NOT NULL
                             REFERENCES attachment_blobs(content_sha256) ON DELETE RESTRICT,
    holder_kind         TEXT NOT NULL CHECK (holder_kind IN
                             ('run_input','transcript','external')),
    holder_ref          TEXT NOT NULL CHECK (length(holder_ref) BETWEEN 1 AND 255),
    claim_version       INTEGER NOT NULL CHECK (claim_version = 1),
    created_at_ms       INTEGER NOT NULL,
    UNIQUE (holder_kind, holder_ref, content_sha256)
) STRICT;

CREATE INDEX attachment_blob_retention_claims_blob_idx
    ON attachment_blob_retention_claims(content_sha256);

CREATE TRIGGER attachment_blob_retention_claims_immutable
BEFORE UPDATE ON attachment_blob_retention_claims
BEGIN
  SELECT RAISE(ABORT, 'immutable Attachment Blob retention claim');
END;

CREATE TRIGGER attachment_blob_delete_requires_no_retention_claim
BEFORE DELETE ON attachment_blobs
WHEN EXISTS (
  SELECT 1 FROM attachment_blob_retention_claims
  WHERE content_sha256 = OLD.content_sha256
)
BEGIN
  SELECT RAISE(ABORT, 'retained Attachment Blob cannot be deleted');
END;
"""

# Pinned source checksum. Like MIGRATION_1_SHA256 this is a fixed literal, not a
# value recomputed from the SQL at import time: a computed checksum would always
# equal its own source and could never detect an accidental edit to
# MIGRATION_2_SQL. Any deliberate change to the SQL must update this literal (and
# is caught by test_attachment_schema_pinning + verify_migration_source).
MIGRATION_2_SHA256 = "cc659fb02bd351d89d73a61891f264909af99a1444189cf255683e80e1c6a305"


def _build_migrations() -> tuple[tuple[int, str, str, str], ...]:
    return (
        (
            1,
            "initial_attachment_store",
            MIGRATION_1_SQL,
            MIGRATION_1_SHA256,
        ),
        (
            2,
            "blob_retention_claims",
            MIGRATION_2_SQL,
            MIGRATION_2_SHA256,
        ),
    )


MIGRATIONS: tuple[tuple[int, str, str, str], ...] = _build_migrations()


def verify_migration_source() -> None:
    actual = hashlib.sha256(MIGRATION_1_SQL.encode("utf-8")).hexdigest()
    if actual != MIGRATION_1_SHA256:
        raise RuntimeError("Attachment Store migration source checksum mismatch")
    for version, name, sql, checksum in MIGRATIONS:
        if hashlib.sha256(sql.encode("utf-8")).hexdigest() != checksum:
            raise RuntimeError(
                f"Attachment Store migration {version} ({name}) checksum mismatch"
            )


__all__ = [
    "MIGRATION_1_SHA256",
    "MIGRATION_1_SQL",
    "MIGRATION_2_SHA256",
    "MIGRATION_2_SQL",
    "MIGRATIONS",
    "verify_migration_source",
]
