"""Versioned SQLite schema for authoritative Control Plane State."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3
from types import MappingProxyType
from typing import Final, Mapping

from .errors import ControlIntegrityError


MIGRATION_1_SQL = r"""
CREATE TABLE schema_migrations (
    version             INTEGER PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    checksum_sha256     TEXT NOT NULL,
    applied_at_ms       INTEGER NOT NULL
) STRICT;

CREATE TABLE projects (
    project_id          TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    lifecycle           TEXT NOT NULL CHECK (lifecycle IN ('active','archived')),
    revision            INTEGER NOT NULL CHECK (revision >= 1),
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL,
    lifecycle_at_ms     INTEGER NOT NULL
) STRICT;

CREATE TABLE conversations (
    conversation_id     TEXT PRIMARY KEY,
    surface             TEXT NOT NULL CHECK (surface IN ('cli','desktop','channel')),
    reply_target_version INTEGER NOT NULL,
    reply_target_key    TEXT NOT NULL,
    reply_target_json   TEXT NOT NULL,
    project_id          TEXT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    revision            INTEGER NOT NULL CHECK (revision >= 1),
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL,
    UNIQUE (surface, reply_target_key, conversation_id)
) STRICT;

CREATE TABLE active_conversation_bindings (
    surface             TEXT NOT NULL CHECK (surface IN ('cli','desktop','channel')),
    reply_target_key    TEXT NOT NULL,
    reply_target_version INTEGER NOT NULL,
    reply_target_json   TEXT NOT NULL,
    conversation_id     TEXT NOT NULL UNIQUE
                            REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
    revision            INTEGER NOT NULL CHECK (revision >= 1),
    updated_at_ms       INTEGER NOT NULL,
    PRIMARY KEY (surface, reply_target_key)
) STRICT;

CREATE TABLE turns (
    turn_id              TEXT PRIMARY KEY,
    conversation_id      TEXT NOT NULL
                             REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
    turn_kind            TEXT NOT NULL CHECK (turn_kind IN ('agent','control_command')),
    status               TEXT NOT NULL CHECK (status IN
                            ('queued','running','succeeded','failed','canceled','interrupted')),
    retry_of_turn_id     TEXT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
    terminal_code        TEXT NULL,
    created_at_ms        INTEGER NOT NULL,
    started_at_ms        INTEGER NULL,
    finished_at_ms       INTEGER NULL,
    revision             INTEGER NOT NULL CHECK (revision >= 1)
) STRICT;

CREATE INDEX turns_conversation_status_idx
    ON turns(conversation_id, status, created_at_ms);

CREATE TABLE ingress_bindings (
    surface              TEXT NOT NULL CHECK (surface IN ('cli','desktop','channel')),
    source_namespace     TEXT NOT NULL,
    source_request_id    TEXT NOT NULL,
    fingerprint_version  INTEGER NOT NULL,
    fingerprint_sha256   TEXT NOT NULL,
    turn_id              TEXT NOT NULL UNIQUE
                             REFERENCES turns(turn_id) ON DELETE RESTRICT,
    created_at_ms        INTEGER NOT NULL,
    PRIMARY KEY (surface, source_namespace, source_request_id)
) STRICT;

CREATE TABLE runs (
    run_id               TEXT PRIMARY KEY,
    scope_kind           TEXT NOT NULL CHECK (scope_kind IN ('project','unassigned')),
    project_id           TEXT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    run_kind             TEXT NOT NULL,
    parent_turn_id       TEXT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
    retry_of_run_id      TEXT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    status               TEXT NOT NULL CHECK (status IN
                            ('queued','running','cancel_requested','succeeded',
                             'failed','canceled','interrupted')),
    terminal_code        TEXT NULL,
    manifest_ref         TEXT NOT NULL,
    created_at_ms        INTEGER NOT NULL,
    started_at_ms        INTEGER NULL,
    finished_at_ms       INTEGER NULL,
    revision             INTEGER NOT NULL CHECK (revision >= 1),
    CHECK ((scope_kind = 'project' AND project_id IS NOT NULL) OR
           (scope_kind = 'unassigned' AND project_id IS NULL))
) STRICT;

CREATE INDEX runs_project_status_idx ON runs(project_id, status, created_at_ms);
CREATE INDEX runs_parent_turn_idx ON runs(parent_turn_id, created_at_ms);

CREATE TABLE run_submission_bindings (
    run_submission_id   TEXT PRIMARY KEY,
    fingerprint_version INTEGER NOT NULL,
    fingerprint_sha256  TEXT NOT NULL,
    run_id               TEXT NOT NULL UNIQUE
                              REFERENCES runs(run_id) ON DELETE RESTRICT,
    created_at_ms        INTEGER NOT NULL
) STRICT;

CREATE TABLE run_execution_assignments (
    run_id               TEXT PRIMARY KEY
                              REFERENCES runs(run_id) ON DELETE RESTRICT,
    assignment_id        TEXT NOT NULL UNIQUE,
    executor_kind        TEXT NOT NULL,
    execution_reference_type TEXT NULL,
    execution_reference TEXT NULL,
    assigned_at_ms       INTEGER NOT NULL,
    CHECK ((execution_reference_type IS NULL AND execution_reference IS NULL) OR
           (execution_reference_type IS NOT NULL AND execution_reference IS NOT NULL))
) STRICT;

CREATE TABLE deliveries (
    delivery_id          TEXT PRIMARY KEY,
    turn_id              TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
    conversation_id      TEXT NOT NULL
                              REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
    purpose              TEXT NOT NULL CHECK (purpose IN ('terminal','resend')),
    terminal_kind        TEXT NOT NULL CHECK (terminal_kind IN
                             ('succeeded','failed','canceled','interrupted')),
    surface              TEXT NOT NULL CHECK (surface = 'channel'),
    reply_target_version INTEGER NOT NULL,
    reply_target_key     TEXT NOT NULL,
    reply_target_json    TEXT NOT NULL,
    target_sequence      INTEGER NOT NULL CHECK (target_sequence >= 1),
    resend_of_delivery_id TEXT NULL
                               REFERENCES deliveries(delivery_id) ON DELETE RESTRICT,
    created_at_ms        INTEGER NOT NULL,
    CHECK ((purpose = 'terminal' AND resend_of_delivery_id IS NULL) OR
           (purpose = 'resend' AND resend_of_delivery_id IS NOT NULL))
) STRICT;

CREATE UNIQUE INDEX deliveries_one_terminal_per_turn
    ON deliveries(turn_id) WHERE purpose = 'terminal';
CREATE UNIQUE INDEX deliveries_target_sequence
    ON deliveries(surface, reply_target_key, target_sequence);

CREATE TABLE delivery_items (
    item_id              TEXT PRIMARY KEY,
    delivery_id          TEXT NOT NULL
                              REFERENCES deliveries(delivery_id) ON DELETE RESTRICT,
    ordinal              INTEGER NOT NULL CHECK (ordinal >= 0),
    item_kind            TEXT NOT NULL CHECK (item_kind IN ('text','media')),
    content_store        TEXT NOT NULL CHECK (content_store IN
                             ('transcript','run_artifact','tool_result')),
    content_ref          TEXT NOT NULL,
    content_sha256       TEXT NOT NULL,
    content_range_json   TEXT NULL,
    render_version       INTEGER NOT NULL,
    media_type           TEXT NULL,
    caption_ref          TEXT NULL,
    caption_sha256       TEXT NULL,
    state                TEXT NOT NULL CHECK (state IN
                             ('queued','sending','delivered','retry_wait','failed',
                              'unknown','suppressed')),
    attempt_count        INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at_ms   INTEGER NULL,
    last_error_code      TEXT NULL,
    provider_evidence_json TEXT NULL,
    blocked_by_item_id   TEXT NULL
                              REFERENCES delivery_items(item_id) ON DELETE RESTRICT,
    delivered_at_ms     INTEGER NULL,
    updated_at_ms       INTEGER NOT NULL,
    UNIQUE (delivery_id, ordinal),
    CHECK ((caption_ref IS NULL AND caption_sha256 IS NULL) OR
           (caption_ref IS NOT NULL AND caption_sha256 IS NOT NULL)),
    CHECK ((state = 'suppressed' AND blocked_by_item_id IS NOT NULL) OR
           (state != 'suppressed' AND blocked_by_item_id IS NULL))
) STRICT;

CREATE INDEX delivery_outbox_idx
    ON delivery_items(state, next_attempt_at_ms, updated_at_ms);

CREATE TABLE delivery_attempts (
    attempt_id           TEXT PRIMARY KEY,
    item_id              TEXT NOT NULL
                              REFERENCES delivery_items(item_id) ON DELETE RESTRICT,
    attempt_no           INTEGER NOT NULL CHECK (attempt_no >= 1),
    started_at_ms        INTEGER NOT NULL,
    finished_at_ms       INTEGER NULL,
    outcome              TEXT NULL CHECK (outcome IS NULL OR outcome IN
                             ('accepted','not_accepted_retryable',
                              'rejected_permanent','acceptance_unknown')),
    error_code           TEXT NULL,
    provider_evidence_json TEXT NULL,
    UNIQUE (item_id, attempt_no)
) STRICT;

CREATE TABLE project_projection_intents (
    projection_intent_id TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL
                              REFERENCES projects(project_id) ON DELETE RESTRICT,
    origin_kind          TEXT NOT NULL CHECK (origin_kind IN ('turn','run')),
    origin_id            TEXT NOT NULL,
    projection_kind      TEXT NOT NULL,
    projection_schema_version INTEGER NOT NULL CHECK (projection_schema_version >= 1),
    source_store         TEXT NOT NULL CHECK (source_store IN
                              ('transcript','run','attachment','tool_result')),
    source_ref           TEXT NOT NULL,
    content_sha256       TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN ('pending','applied','failed')),
    last_error_code      TEXT NULL,
    created_at_ms        INTEGER NOT NULL,
    updated_at_ms        INTEGER NOT NULL,
    applied_at_ms        INTEGER NULL,
    UNIQUE (project_id, origin_kind, origin_id, projection_kind,
            source_store, source_ref, content_sha256),
    CHECK ((state = 'applied' AND applied_at_ms IS NOT NULL) OR
           (state != 'applied' AND applied_at_ms IS NULL))
) STRICT;

CREATE TABLE legacy_import_runs (
    import_run_id        TEXT PRIMARY KEY,
    source_manifest_sha256 TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN
                             ('planned','validated','committed','failed')),
    started_at_ms        INTEGER NOT NULL,
    finished_at_ms       INTEGER NULL,
    cutover_at_ms        INTEGER NULL,
    report_ref           TEXT NOT NULL
) STRICT;

CREATE TABLE legacy_identity_map (
    import_run_id        TEXT NOT NULL
                              REFERENCES legacy_import_runs(import_run_id) ON DELETE RESTRICT,
    source_system        TEXT NOT NULL,
    legacy_kind          TEXT NOT NULL,
    legacy_key           TEXT NOT NULL,
    canonical_kind       TEXT NOT NULL,
    canonical_id         TEXT NULL,
    evidence_json        TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('mapped','skipped','conflict')),
    PRIMARY KEY (source_system, legacy_kind, legacy_key),
    CHECK ((status = 'mapped' AND canonical_id IS NOT NULL) OR status != 'mapped')
) STRICT;

CREATE TABLE legacy_import_conflicts (
    conflict_id          TEXT PRIMARY KEY,
    import_run_id        TEXT NOT NULL
                              REFERENCES legacy_import_runs(import_run_id) ON DELETE RESTRICT,
    source_system        TEXT NOT NULL,
    legacy_kind          TEXT NOT NULL,
    legacy_key           TEXT NOT NULL,
    reason_code          TEXT NOT NULL,
    evidence_json        TEXT NOT NULL,
    resolution           TEXT NULL,
    created_at_ms        INTEGER NOT NULL
) STRICT;

CREATE TRIGGER conversations_immutable
BEFORE UPDATE ON conversations
WHEN NEW.conversation_id != OLD.conversation_id
  OR NEW.surface != OLD.surface
  OR NEW.reply_target_version != OLD.reply_target_version
  OR NEW.reply_target_key != OLD.reply_target_key
  OR NEW.reply_target_json != OLD.reply_target_json
  OR (OLD.project_id IS NOT NEW.project_id
      AND NOT (OLD.project_id IS NULL AND NEW.project_id IS NOT NULL))
BEGIN
  SELECT RAISE(ABORT, 'immutable conversation field');
END;

CREATE TRIGGER turns_identity_immutable
BEFORE UPDATE ON turns
WHEN NEW.turn_id != OLD.turn_id
  OR NEW.conversation_id != OLD.conversation_id
  OR NEW.turn_kind != OLD.turn_kind
  OR OLD.retry_of_turn_id IS NOT NEW.retry_of_turn_id
BEGIN
  SELECT RAISE(ABORT, 'immutable turn field');
END;

CREATE TRIGGER turns_terminal_closed
BEFORE UPDATE OF status ON turns
WHEN OLD.status IN ('succeeded','failed','canceled','interrupted')
 AND NEW.status != OLD.status
BEGIN
  SELECT RAISE(ABORT, 'terminal turn cannot reopen');
END;

CREATE TRIGGER runs_identity_immutable
BEFORE UPDATE ON runs
WHEN NEW.run_id != OLD.run_id
  OR NEW.scope_kind != OLD.scope_kind
  OR OLD.project_id IS NOT NEW.project_id
  OR NEW.run_kind != OLD.run_kind
  OR OLD.parent_turn_id IS NOT NEW.parent_turn_id
  OR OLD.retry_of_run_id IS NOT NEW.retry_of_run_id
  OR NEW.manifest_ref != OLD.manifest_ref
BEGIN
  SELECT RAISE(ABORT, 'immutable run field');
END;

CREATE TRIGGER runs_terminal_closed
BEFORE UPDATE OF status ON runs
WHEN OLD.status IN ('succeeded','failed','canceled','interrupted')
 AND NEW.status != OLD.status
BEGIN
  SELECT RAISE(ABORT, 'terminal run cannot reopen');
END;

CREATE TRIGGER ingress_bindings_insert_only_update
BEFORE UPDATE ON ingress_bindings
BEGIN
  SELECT RAISE(ABORT, 'ingress binding is insert-only');
END;

CREATE TRIGGER ingress_bindings_insert_only_delete
BEFORE DELETE ON ingress_bindings
BEGIN
  SELECT RAISE(ABORT, 'ingress binding is insert-only');
END;

CREATE TRIGGER run_submission_bindings_insert_only_update
BEFORE UPDATE ON run_submission_bindings
BEGIN
  SELECT RAISE(ABORT, 'run submission binding is insert-only');
END;

CREATE TRIGGER run_submission_bindings_insert_only_delete
BEFORE DELETE ON run_submission_bindings
BEGIN
  SELECT RAISE(ABORT, 'run submission binding is insert-only');
END;

CREATE TRIGGER assignments_identity_immutable
BEFORE UPDATE ON run_execution_assignments
WHEN NEW.run_id != OLD.run_id
  OR NEW.assignment_id != OLD.assignment_id
  OR NEW.executor_kind != OLD.executor_kind
  OR NEW.assigned_at_ms != OLD.assigned_at_ms
BEGIN
  SELECT RAISE(ABORT, 'immutable assignment field');
END;

CREATE TRIGGER active_binding_address_immutable
BEFORE UPDATE ON active_conversation_bindings
WHEN NEW.surface != OLD.surface
  OR NEW.reply_target_key != OLD.reply_target_key
  OR NEW.reply_target_version != OLD.reply_target_version
  OR NEW.reply_target_json != OLD.reply_target_json
BEGIN
  SELECT RAISE(ABORT, 'immutable active binding address');
END;

CREATE TRIGGER active_binding_conversation_address_match
BEFORE INSERT ON active_conversation_bindings
WHEN NOT EXISTS (
  SELECT 1 FROM conversations AS c
  WHERE c.conversation_id = NEW.conversation_id
    AND c.surface = NEW.surface
    AND c.reply_target_key = NEW.reply_target_key
    AND c.reply_target_version = NEW.reply_target_version
    AND c.reply_target_json = NEW.reply_target_json
)
BEGIN
  SELECT RAISE(ABORT, 'active binding conversation address mismatch');
END;

CREATE TRIGGER active_binding_update_conversation_address_match
BEFORE UPDATE OF conversation_id ON active_conversation_bindings
WHEN NOT EXISTS (
  SELECT 1 FROM conversations AS c
  WHERE c.conversation_id = NEW.conversation_id
    AND c.surface = NEW.surface
    AND c.reply_target_key = NEW.reply_target_key
    AND c.reply_target_version = NEW.reply_target_version
    AND c.reply_target_json = NEW.reply_target_json
)
BEGIN
  SELECT RAISE(ABORT, 'active binding conversation address mismatch');
END;

CREATE TRIGGER deliveries_immutable
BEFORE UPDATE ON deliveries
BEGIN
  SELECT RAISE(ABORT, 'delivery is immutable');
END;

CREATE TRIGGER delivery_items_content_immutable
BEFORE UPDATE ON delivery_items
WHEN NEW.item_id != OLD.item_id
  OR NEW.delivery_id != OLD.delivery_id
  OR NEW.ordinal != OLD.ordinal
  OR NEW.item_kind != OLD.item_kind
  OR NEW.content_store != OLD.content_store
  OR NEW.content_ref != OLD.content_ref
  OR NEW.content_sha256 != OLD.content_sha256
  OR OLD.content_range_json IS NOT NEW.content_range_json
  OR NEW.render_version != OLD.render_version
  OR OLD.media_type IS NOT NEW.media_type
  OR OLD.caption_ref IS NOT NEW.caption_ref
  OR OLD.caption_sha256 IS NOT NEW.caption_sha256
BEGIN
  SELECT RAISE(ABORT, 'immutable delivery item content');
END;

CREATE TRIGGER delivery_items_terminal_closed
BEFORE UPDATE OF state ON delivery_items
WHEN OLD.state IN ('delivered','failed','unknown','suppressed')
 AND NEW.state != OLD.state
BEGIN
  SELECT RAISE(ABORT, 'terminal delivery item cannot reopen');
END;

CREATE TRIGGER projection_intent_identity_immutable
BEFORE UPDATE ON project_projection_intents
WHEN NEW.projection_intent_id != OLD.projection_intent_id
  OR NEW.project_id != OLD.project_id
  OR NEW.origin_kind != OLD.origin_kind
  OR NEW.origin_id != OLD.origin_id
  OR NEW.projection_kind != OLD.projection_kind
  OR NEW.projection_schema_version != OLD.projection_schema_version
  OR NEW.source_store != OLD.source_store
  OR NEW.source_ref != OLD.source_ref
  OR NEW.content_sha256 != OLD.content_sha256
  OR NEW.created_at_ms != OLD.created_at_ms
BEGIN
  SELECT RAISE(ABORT, 'immutable projection intent field');
END;

CREATE TRIGGER projection_intent_terminal_closed
BEFORE UPDATE OF state ON project_projection_intents
WHEN OLD.state IN ('applied','failed') AND NEW.state != OLD.state
BEGIN
  SELECT RAISE(ABORT, 'terminal projection intent cannot reopen');
END;
"""


# Historical migration inputs are immutable data, not aliases of the live
# runtime vocabulary in ``terminal_codes.py``.  Never edit these V2 snapshots:
# a new code requires migration 3 to define a new literal snapshot, audit the
# rows accepted under V2, and replace the four V2 triggers transactionally.
V2_TURN_TERMINAL_CODES_BY_STATUS: Final[Mapping[str, frozenset[str]]] = (
    MappingProxyType(
        {
            "succeeded": frozenset(),
            "failed": frozenset(
                {
                    "attachment_finalize_failed",
                    "dispatch_enqueue_failed",
                    "invalid_worker_outcome",
                    "worker_failed",
                }
            ),
            "canceled": frozenset(
                {"canceled", "canceled_before_start", "canceled_by_owner"}
            ),
            "interrupted": frozenset(
                {"control_plane_restarted", "worker_task_interrupted"}
            ),
        }
    )
)

V2_RUN_TERMINAL_CODES_BY_STATUS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "succeeded": frozenset(),
        "failed": frozenset(
            {
                "completion_commit_failed",
                "executor_failed",
                "spawn_failed",
                "submission_failed",
                "timed_out",
                "validation_failed",
            }
        ),
        "canceled": frozenset(
            {"canceled", "canceled_before_assignment", "canceled_by_owner"}
        ),
        "interrupted": frozenset({"control_plane_restarted", "execution_interrupted"}),
    }
)

TERMINAL_CODE_POLICY_TRIGGER_NAMES: Final[tuple[str, ...]] = (
    "turns_terminal_code_policy_insert",
    "turns_terminal_code_policy_update",
    "runs_terminal_code_policy_insert",
    "runs_terminal_code_policy_update",
)


def _sql_string_list(values: frozenset[str]) -> str:
    """Render a deterministic SQL literal list from trusted module constants."""

    if not values:
        raise ValueError("terminal-code SQL list must not be empty")
    for value in values:
        if not value.replace("_", "").isalnum() or not value.islower():
            raise ValueError(f"unsafe terminal-code constant: {value!r}")
    return ", ".join(f"'{value}'" for value in sorted(values))


def _terminal_code_policy_sql(
    *,
    status_ref: str,
    code_ref: str,
    nonterminal_statuses: tuple[str, ...],
    codes_by_status: Mapping[str, frozenset[str]],
) -> str:
    """Build one status-aware allowlist predicate for a table or trigger row."""

    nonterminal_sql = ", ".join(f"'{status}'" for status in nonterminal_statuses)
    branches = [
        f"({status_ref} IN ({nonterminal_sql}) AND {code_ref} IS NULL)",
        f"({status_ref} = 'succeeded' AND {code_ref} IS NULL)",
    ]
    for status in ("failed", "canceled", "interrupted"):
        codes = codes_by_status[status]
        branches.append(
            f"({status_ref} = '{status}' AND "
            f"({code_ref} IS NULL OR {code_ref} IN ({_sql_string_list(codes)})))"
        )
    return "(" + " OR ".join(branches) + ")"


def _render_terminal_code_policy_migration_sql(
    *,
    turn_codes_by_status: Mapping[str, frozenset[str]],
    run_codes_by_status: Mapping[str, frozenset[str]],
    replace_existing_triggers: bool,
) -> str:
    """Render an audit-first terminal-code policy migration.

    Migration 2 passes ``replace_existing_triggers=False`` because no prior
    policy triggers exist.  A future migration must pass ``True``: the legacy
    rows are audited first, then all four old triggers are dropped and recreated
    from that migration's new literal snapshots in the same transaction.
    """

    turn_code_policy_row = _terminal_code_policy_sql(
        status_ref="status",
        code_ref="terminal_code",
        nonterminal_statuses=("queued", "running"),
        codes_by_status=turn_codes_by_status,
    )
    turn_code_policy_new = _terminal_code_policy_sql(
        status_ref="NEW.status",
        code_ref="NEW.terminal_code",
        nonterminal_statuses=("queued", "running"),
        codes_by_status=turn_codes_by_status,
    )
    run_code_policy_row = _terminal_code_policy_sql(
        status_ref="status",
        code_ref="terminal_code",
        nonterminal_statuses=("queued", "running", "cancel_requested"),
        codes_by_status=run_codes_by_status,
    )
    run_code_policy_new = _terminal_code_policy_sql(
        status_ref="NEW.status",
        code_ref="NEW.terminal_code",
        nonterminal_statuses=("queued", "running", "cancel_requested"),
        codes_by_status=run_codes_by_status,
    )
    drop_existing_triggers_sql = ""
    if replace_existing_triggers:
        drop_existing_triggers_sql = "\n".join(
            f"DROP TRIGGER {name};" for name in TERMINAL_CODE_POLICY_TRIGGER_NAMES
        )

    return f"""
CREATE TABLE terminal_code_policy_migration_guard (
    must_be_valid INTEGER NOT NULL CHECK (must_be_valid = 1)
) STRICT;

INSERT INTO terminal_code_policy_migration_guard (must_be_valid)
SELECT 0 WHERE EXISTS (
    SELECT 1 FROM turns WHERE NOT {turn_code_policy_row}
);

INSERT INTO terminal_code_policy_migration_guard (must_be_valid)
SELECT 0 WHERE EXISTS (
    SELECT 1 FROM runs WHERE NOT {run_code_policy_row}
);

DROP TABLE terminal_code_policy_migration_guard;
{drop_existing_triggers_sql}
CREATE TRIGGER turns_terminal_code_policy_insert
BEFORE INSERT ON turns
WHEN NOT {turn_code_policy_new}
BEGIN
  SELECT RAISE(ABORT, 'invalid turn terminal code');
END;

CREATE TRIGGER turns_terminal_code_policy_update
BEFORE UPDATE OF status, terminal_code ON turns
WHEN NOT {turn_code_policy_new}
BEGIN
  SELECT RAISE(ABORT, 'invalid turn terminal code');
END;

CREATE TRIGGER runs_terminal_code_policy_insert
BEFORE INSERT ON runs
WHEN NOT {run_code_policy_new}
BEGIN
  SELECT RAISE(ABORT, 'invalid run terminal code');
END;

CREATE TRIGGER runs_terminal_code_policy_update
BEFORE UPDATE OF status, terminal_code ON runs
WHEN NOT {run_code_policy_new}
BEGIN
  SELECT RAISE(ABORT, 'invalid run terminal code');
END;
"""


MIGRATION_2_SQL = _render_terminal_code_policy_migration_sql(
    turn_codes_by_status=V2_TURN_TERMINAL_CODES_BY_STATUS,
    run_codes_by_status=V2_RUN_TERMINAL_CODES_BY_STATUS,
    replace_existing_triggers=False,
)

MIGRATION_3_SQL = r"""
CREATE TABLE turn_terminal_refs (
    turn_id              TEXT PRIMARY KEY
                              REFERENCES turns(turn_id) ON DELETE RESTRICT,
    entry_id             TEXT NOT NULL UNIQUE,
    content_sha256       TEXT NOT NULL,
    created_at_ms        INTEGER NOT NULL
) STRICT;

CREATE TRIGGER turn_terminal_refs_immutable
BEFORE UPDATE ON turn_terminal_refs
BEGIN
  SELECT RAISE(ABORT, 'immutable Turn terminal Transcript reference');
END;
"""

MIGRATION_4_SQL = r"""
CREATE TRIGGER legacy_import_runs_state_transition
BEFORE UPDATE OF state ON legacy_import_runs
WHEN NOT (
    OLD.state = NEW.state
    OR (OLD.state = 'planned' AND NEW.state IN ('validated','failed'))
    OR (OLD.state = 'validated' AND NEW.state IN ('committed','failed'))
)
BEGIN
  SELECT RAISE(ABORT, 'invalid legacy import state transition');
END;
"""

MIGRATION_5_SQL = r"""
CREATE TABLE legacy_transcript_cutovers (
    import_run_id             TEXT PRIMARY KEY
                                   REFERENCES legacy_import_runs(import_run_id)
                                   ON DELETE RESTRICT,
    cutover_manifest_sha256   TEXT NOT NULL UNIQUE,
    transcript_store_id       TEXT NOT NULL,
    import_baseline_sha256    TEXT NOT NULL,
    source_identity           TEXT NOT NULL,
    recorded_at_ms            INTEGER NOT NULL
) STRICT;

CREATE TRIGGER legacy_transcript_cutovers_immutable_update
BEFORE UPDATE ON legacy_transcript_cutovers
BEGIN
  SELECT RAISE(ABORT, 'immutable legacy Transcript cutover identity');
END;

CREATE TRIGGER legacy_transcript_cutovers_immutable_delete
BEFORE DELETE ON legacy_transcript_cutovers
BEGIN
  SELECT RAISE(ABORT, 'immutable legacy Transcript cutover identity');
END;
"""

MIGRATION_6_SQL = r"""
CREATE TABLE transcript_store_bindings (
    singleton              INTEGER PRIMARY KEY CHECK (singleton = 1),
    transcript_store_id    TEXT NOT NULL UNIQUE,
    bound_at_ms            INTEGER NOT NULL
) STRICT;

CREATE TRIGGER transcript_store_bindings_immutable_update
BEFORE UPDATE ON transcript_store_bindings
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript Store binding');
END;

CREATE TRIGGER transcript_store_bindings_immutable_delete
BEFORE DELETE ON transcript_store_bindings
BEGIN
  SELECT RAISE(ABORT, 'immutable Transcript Store binding');
END;
"""

MIGRATION_7_SQL = r"""
CREATE TABLE attachment_store_bindings (
    singleton              INTEGER PRIMARY KEY CHECK (singleton = 1),
    store_id               TEXT NOT NULL UNIQUE,
    bound_at_ms            INTEGER NOT NULL
) STRICT;

CREATE TRIGGER attachment_store_bindings_immutable_update
BEFORE UPDATE ON attachment_store_bindings
BEGIN
  SELECT RAISE(ABORT, 'immutable Attachment Store binding');
END;

CREATE TRIGGER attachment_store_bindings_immutable_delete
BEFORE DELETE ON attachment_store_bindings
BEGIN
  SELECT RAISE(ABORT, 'immutable Attachment Store binding');
END;

CREATE TABLE turn_attachment_commitments (
    turn_id                 TEXT PRIMARY KEY
                                 REFERENCES turns(turn_id) ON DELETE RESTRICT,
    attachment_store_id     TEXT NOT NULL
                                 REFERENCES attachment_store_bindings(store_id)
                                 ON DELETE RESTRICT,
    batch_id                TEXT NOT NULL UNIQUE,
    manifest_sha256         TEXT NOT NULL,
    attachment_count        INTEGER NOT NULL CHECK (attachment_count >= 1),
    created_at_ms           INTEGER NOT NULL
) STRICT;

CREATE TRIGGER turn_attachment_commitments_immutable_update
BEFORE UPDATE ON turn_attachment_commitments
BEGIN
  SELECT RAISE(ABORT, 'immutable Turn Attachment commitment');
END;

CREATE TRIGGER turn_attachment_commitments_immutable_delete
BEFORE DELETE ON turn_attachment_commitments
BEGIN
  SELECT RAISE(ABORT, 'immutable Turn Attachment commitment');
END;
"""

MIGRATION_8_SQL = r"""
CREATE UNIQUE INDEX run_execution_owner_reference_unique
ON run_execution_assignments(execution_reference_type, execution_reference)
WHERE execution_reference_type IS NOT NULL;

CREATE TRIGGER local_simple_assignment_requires_governed_owner
BEFORE INSERT ON run_execution_assignments
WHEN NEW.executor_kind = 'local-simple-skill-v1'
 AND (
    NEW.execution_reference_type IS NOT 'linux-user-systemd-bwrap-v1'
    OR NEW.execution_reference IS NULL
    OR length(NEW.execution_reference) != 44
    OR substr(NEW.execution_reference, 1, 14) != 'omicsclaw-run-'
    OR substr(NEW.execution_reference, 39, 6) != '.scope'
    OR substr(NEW.execution_reference, 15, 24) GLOB '*[^0-9a-f]*'
 )
BEGIN
  SELECT RAISE(ABORT, 'local Simple Run assignment requires governed owner');
END;

CREATE TRIGGER governed_owner_reference_format_insert
BEFORE INSERT ON run_execution_assignments
WHEN NEW.execution_reference_type = 'linux-user-systemd-bwrap-v1'
 AND NOT (
    length(NEW.execution_reference) = 44
    AND substr(NEW.execution_reference, 1, 14) = 'omicsclaw-run-'
    AND substr(NEW.execution_reference, 39, 6) = '.scope'
    AND substr(NEW.execution_reference, 15, 24) NOT GLOB '*[^0-9a-f]*'
 )
BEGIN
  SELECT RAISE(ABORT, 'invalid governed owner reference');
END;

CREATE TRIGGER governed_owner_reference_format_update
BEFORE UPDATE OF execution_reference_type, execution_reference
ON run_execution_assignments
WHEN NEW.execution_reference_type = 'linux-user-systemd-bwrap-v1'
 AND NOT (
    length(NEW.execution_reference) = 44
    AND substr(NEW.execution_reference, 1, 14) = 'omicsclaw-run-'
    AND substr(NEW.execution_reference, 39, 6) = '.scope'
    AND substr(NEW.execution_reference, 15, 24) NOT GLOB '*[^0-9a-f]*'
 )
BEGIN
  SELECT RAISE(ABORT, 'invalid governed owner reference');
END;

CREATE TRIGGER governed_owner_reference_write_once
BEFORE UPDATE OF execution_reference_type, execution_reference
ON run_execution_assignments
WHEN OLD.execution_reference_type IS NOT NULL
 AND (
    OLD.execution_reference_type IS NOT NEW.execution_reference_type
    OR OLD.execution_reference IS NOT NEW.execution_reference
 )
BEGIN
  SELECT RAISE(ABORT, 'governed owner reference is immutable');
END;
"""

MIGRATION_9_SQL = r"""
CREATE TABLE run_integrity_incidents (
    incident_sequence       INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id             TEXT NOT NULL UNIQUE
                                 CHECK (
                                    length(incident_id) = 32
                                    AND incident_id NOT GLOB '*[^0-9a-f]*'
                                 ),
    run_id                  TEXT NOT NULL
                                 REFERENCES runs(run_id) ON DELETE RESTRICT,
    assignment_id           TEXT NOT NULL
                                 CHECK (
                                    length(assignment_id) = 32
                                    AND assignment_id NOT GLOB '*[^0-9a-f]*'
                                 ),
    incident_type           TEXT NOT NULL CHECK (incident_type IN (
                                 'assignment_fence_violation',
                                 'terminal_report_conflict',
                                 'manifest_receipt_mismatch',
                                 'execution_owner_unconfirmed',
                                 'recovery_terminal_commit_failed'
                             )),
    evidence_code           TEXT NOT NULL CHECK (evidence_code IN (
                                 'assignment_missing',
                                 'assignment_id_mismatch',
                                 'terminal_state_conflict',
                                 'manifest_receipt_binding_mismatch',
                                 'manifest_assignment_mismatch',
                                 'manifest_completion_invalid',
                                 'manifest_terminal_conflict',
                                 'execution_reference_missing',
                                 'execution_owner_stop_unconfirmed',
                                 'dispatcher_owner_missing',
                                 'recovery_terminal_report_rejected',
                                 'recovery_terminal_transaction_failed'
                             )),
    receipt_revision        INTEGER NOT NULL CHECK (receipt_revision >= 1),
    evidence_schema_version INTEGER NOT NULL CHECK (evidence_schema_version = 1),
    evidence_sha256         TEXT NOT NULL CHECK (
                                 length(evidence_sha256) = 64
                                 AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'
                             ),
    created_at_ms           INTEGER NOT NULL CHECK (created_at_ms >= 0),
    CHECK (
        (incident_type = 'assignment_fence_violation'
         AND evidence_code IN ('assignment_missing', 'assignment_id_mismatch'))
        OR (incident_type = 'terminal_report_conflict'
            AND evidence_code = 'terminal_state_conflict')
        OR (incident_type = 'manifest_receipt_mismatch'
            AND evidence_code IN (
                'manifest_receipt_binding_mismatch',
                'manifest_assignment_mismatch',
                'manifest_completion_invalid',
                'manifest_terminal_conflict'
            ))
        OR (incident_type = 'execution_owner_unconfirmed'
            AND evidence_code IN (
                'execution_reference_missing',
                'execution_owner_stop_unconfirmed',
                'dispatcher_owner_missing'
            ))
        OR (incident_type = 'recovery_terminal_commit_failed'
            AND evidence_code IN (
                'recovery_terminal_report_rejected',
                'recovery_terminal_transaction_failed'
            ))
    ),
    UNIQUE (run_id, evidence_sha256)
) STRICT;

CREATE INDEX run_integrity_incidents_run_sequence_idx
ON run_integrity_incidents(run_id, incident_sequence DESC);

CREATE TRIGGER run_integrity_incidents_append_only_update
BEFORE UPDATE ON run_integrity_incidents
BEGIN
  SELECT RAISE(ABORT, 'Run integrity incidents are append-only');
END;

CREATE TRIGGER run_integrity_incidents_append_only_delete
BEFORE DELETE ON run_integrity_incidents
BEGIN
  SELECT RAISE(ABORT, 'Run integrity incidents are append-only');
END;
"""

MIGRATION_10_SQL = r"""
CREATE INDEX runs_kind_scope_created_idx
ON runs(run_kind, scope_kind, created_at_ms DESC, run_id DESC);

CREATE INDEX runs_kind_scope_status_created_idx
ON runs(run_kind, scope_kind, status, created_at_ms DESC, run_id DESC);
"""

MIGRATION_11_SQL = r"""
CREATE TABLE autoagent_capacity (
    singleton_id    INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    session_count   INTEGER NOT NULL CHECK (
                        session_count BETWEEN 0 AND 100000
                    ),
    result_bytes    INTEGER NOT NULL CHECK (
                        result_bytes BETWEEN 0 AND 1073741824
                    ),
    cancellation_count INTEGER NOT NULL CHECK (
                        cancellation_count BETWEEN 0 AND 100000
                    )
) STRICT;

INSERT INTO autoagent_capacity (
    singleton_id, session_count, result_bytes, cancellation_count
) VALUES (1, 0, 0, 0);

CREATE TABLE autoagent_start_cancellations (
    session_id                 TEXT NOT NULL CHECK (
                                   length(session_id) = 32
                                   AND session_id NOT GLOB '*[^0-9a-f]*'
                               ),
    creation_receipt_sha256    TEXT NOT NULL CHECK (
                                   length(creation_receipt_sha256) = 64
                                   AND creation_receipt_sha256
                                       NOT GLOB '*[^0-9a-f]*'
                               ),
    created_at_ms              INTEGER NOT NULL CHECK (created_at_ms >= 0),
    PRIMARY KEY (session_id, creation_receipt_sha256)
) STRICT;

CREATE TABLE autoagent_sessions (
    session_id                 TEXT PRIMARY KEY CHECK (
                                   length(session_id) = 32
                                   AND session_id NOT GLOB '*[^0-9a-f]*'
                               ),
    cwd                        TEXT NOT NULL CHECK (
                                   length(cwd) <= 4096
                                   AND instr(cwd, char(0)) = 0
                               ),
    output_dir                 TEXT NOT NULL CHECK (
                                   length(output_dir) BETWEEN 1 AND 4096
                                   AND instr(output_dir, char(0)) = 0
                               ),
    skill                      TEXT NOT NULL CHECK (
                                   length(skill) BETWEEN 1 AND 256
                                   AND instr(skill, char(0)) = 0
                               ),
    method                     TEXT NOT NULL CHECK (
                                   length(method) BETWEEN 1 AND 256
                                   AND instr(method, char(0)) = 0
                               ),
    evolution_goal             TEXT NOT NULL CHECK (
                                   length(evolution_goal) <= 16384
                                   AND instr(evolution_goal, char(0)) = 0
                               ),
    creation_receipt_sha256    TEXT NULL CHECK (
                                   creation_receipt_sha256 IS NULL
                                   OR (
                                       length(creation_receipt_sha256) = 64
                                       AND creation_receipt_sha256
                                           NOT GLOB '*[^0-9a-f]*'
                                   )
                               ),
    cancel_requested_at_ms     INTEGER NULL CHECK (
                                   cancel_requested_at_ms IS NULL
                                   OR cancel_requested_at_ms >= created_at_ms
                               ),
    execution_reference_type  TEXT NULL CHECK (
                                   execution_reference_type IS NULL
                                   OR execution_reference_type =
                                      'linux-user-systemd-bwrap-v1'
                               ),
    execution_reference       TEXT NULL CHECK (
                                   execution_reference IS NULL
                                   OR (
                                       length(execution_reference) = 44
                                       AND substr(execution_reference, 1, 14) =
                                           'omicsclaw-run-'
                                       AND substr(execution_reference, 15, 24)
                                           NOT GLOB '*[^0-9a-f]*'
                                       AND substr(execution_reference, 39, 6) =
                                           '.scope'
                                   )
                               ),
    owner_stopped_at_ms        INTEGER NULL CHECK (
                                   owner_stopped_at_ms IS NULL
                                   OR owner_stopped_at_ms >= created_at_ms
                               ),
    owner_stop_evidence        TEXT NULL CHECK (
                                   owner_stop_evidence IS NULL
                                   OR owner_stop_evidence = 'process_tree_absent_v1'
                               ),
    status                     TEXT NOT NULL CHECK (status IN (
                                   'running', 'done', 'error', 'cancelled',
                                   'interrupted'
                               )),
    result_json                TEXT NULL CHECK (
                                   result_json IS NULL
                                   OR (
                                       length(result_json) BETWEEN 2 AND 4194304
                                       AND json_valid(result_json)
                                       AND json_type(result_json) = 'object'
                                   )
                               ),
    result_sha256              TEXT NULL CHECK (
                                   result_sha256 IS NULL
                                   OR (
                                       length(result_sha256) = 64
                                       AND result_sha256 NOT GLOB '*[^0-9a-f]*'
                                   )
                               ),
    error_code                 TEXT NULL CHECK (
                                   error_code IS NULL OR error_code IN (
                                       'harness_failed',
                                       'invalid_terminal_result',
                                       'worker_crashed',
                                       'worker_start_failed',
                                       'cancelled',
                                       'backend_restart_interrupted',
                                       'backend_shutdown_interrupted',
                                       'result_capacity_exhausted',
                                       'repository_failure'
                                   )
                               ),
    error_detail               TEXT NULL CHECK (
                                   error_detail IS NULL
                                   OR length(error_detail) BETWEEN 1 AND 512
                               ),
    created_at_ms              INTEGER NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms              INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
    finished_at_ms             INTEGER NULL CHECK (
                                   finished_at_ms IS NULL OR finished_at_ms >= created_at_ms
                               ),
    revision                   INTEGER NOT NULL CHECK (revision >= 1),
    CHECK (
        (status = 'cancelled' AND cancel_requested_at_ms IS NOT NULL
         AND execution_reference_type IS NULL AND execution_reference IS NULL
         AND owner_stopped_at_ms IS NULL AND owner_stop_evidence IS NULL)
        OR
        (execution_reference_type IS NOT NULL AND execution_reference IS NOT NULL
         AND (
             (owner_stopped_at_ms IS NULL AND owner_stop_evidence IS NULL)
             OR
             (owner_stopped_at_ms IS NOT NULL AND owner_stop_evidence IS NOT NULL)
         ))
    ),
    CHECK (
        status = 'running'
        OR execution_reference IS NULL
        OR owner_stop_evidence = 'process_tree_absent_v1'
    ),
    CHECK (
        (status = 'running'
         AND result_json IS NULL AND result_sha256 IS NULL
         AND error_code IS NULL AND error_detail IS NULL
         AND finished_at_ms IS NULL)
        OR
        (status = 'done'
         AND result_json IS NOT NULL AND result_sha256 IS NOT NULL
         AND error_code IS NULL AND error_detail IS NULL
         AND finished_at_ms IS NOT NULL)
        OR
        (status IN ('error', 'cancelled', 'interrupted')
         AND result_json IS NULL AND result_sha256 IS NULL
         AND error_code IS NOT NULL AND error_detail IS NOT NULL
         AND finished_at_ms IS NOT NULL)
    )
) STRICT;

CREATE INDEX autoagent_sessions_status_created_idx
ON autoagent_sessions(status, created_at_ms DESC, session_id DESC);

CREATE TRIGGER autoagent_capacity_singleton_insert
BEFORE INSERT ON autoagent_capacity
BEGIN
  SELECT RAISE(ABORT, 'AutoAgent capacity authority is a singleton');
END;

CREATE TRIGGER autoagent_capacity_append_only_delete
BEFORE DELETE ON autoagent_capacity
BEGIN
  SELECT RAISE(ABORT, 'AutoAgent capacity authority is append-only');
END;

CREATE TRIGGER autoagent_capacity_monotonic_update
BEFORE UPDATE ON autoagent_capacity
WHEN NEW.singleton_id IS NOT OLD.singleton_id
  OR NEW.session_count < OLD.session_count
  OR NEW.result_bytes < OLD.result_bytes
  OR NEW.cancellation_count < OLD.cancellation_count
BEGIN
  SELECT RAISE(ABORT, 'AutoAgent capacity counters must be monotonic');
END;

CREATE TRIGGER autoagent_start_cancellations_capacity_admission
BEFORE INSERT ON autoagent_start_cancellations
WHEN (SELECT cancellation_count FROM autoagent_capacity WHERE singleton_id = 1)
     >= 100000
BEGIN
  SELECT RAISE(ABORT, 'AutoAgent cancellation capacity exhausted');
END;

CREATE TRIGGER autoagent_start_cancellations_capacity_account
AFTER INSERT ON autoagent_start_cancellations
BEGIN
  UPDATE autoagent_capacity
  SET cancellation_count = cancellation_count + 1
  WHERE singleton_id = 1;
END;

CREATE TRIGGER autoagent_start_cancellations_append_only_update
BEFORE UPDATE ON autoagent_start_cancellations
BEGIN
  SELECT RAISE(ABORT, 'AutoAgent start cancellations are append-only');
END;

CREATE TRIGGER autoagent_start_cancellations_append_only_delete
BEFORE DELETE ON autoagent_start_cancellations
BEGIN
  SELECT RAISE(ABORT, 'AutoAgent start cancellations are append-only');
END;

CREATE TRIGGER autoagent_sessions_capacity_admission
BEFORE INSERT ON autoagent_sessions
WHEN (SELECT session_count FROM autoagent_capacity WHERE singleton_id = 1)
     >= 100000
BEGIN
  SELECT RAISE(ABORT, 'AutoAgent session capacity exhausted');
END;

CREATE TRIGGER autoagent_active_sessions_capacity_admission
BEFORE INSERT ON autoagent_sessions
WHEN NEW.status = 'running'
 AND (SELECT count(*) FROM autoagent_sessions WHERE status = 'running') >= 4
BEGIN
  SELECT RAISE(ABORT, 'AutoAgent active session capacity exhausted');
END;

CREATE TRIGGER autoagent_sessions_capacity_account
AFTER INSERT ON autoagent_sessions
BEGIN
  UPDATE autoagent_capacity
  SET session_count = session_count + 1
  WHERE singleton_id = 1;
END;

CREATE TRIGGER autoagent_results_capacity_admission
BEFORE UPDATE OF result_json ON autoagent_sessions
WHEN OLD.result_json IS NULL AND NEW.result_json IS NOT NULL
 AND (
     (SELECT result_bytes FROM autoagent_capacity WHERE singleton_id = 1)
     + length(CAST(NEW.result_json AS BLOB))
 ) > 1073741824
BEGIN
  SELECT RAISE(ABORT, 'AutoAgent result capacity exhausted');
END;

CREATE TRIGGER autoagent_results_capacity_account
AFTER UPDATE OF result_json ON autoagent_sessions
WHEN OLD.result_json IS NULL AND NEW.result_json IS NOT NULL
BEGIN
  UPDATE autoagent_capacity
  SET result_bytes = result_bytes + length(CAST(NEW.result_json AS BLOB))
  WHERE singleton_id = 1;
END;

CREATE TRIGGER autoagent_sessions_authority_immutable
BEFORE UPDATE OF session_id, cwd, output_dir, skill, method, evolution_goal,
                 creation_receipt_sha256, execution_reference_type,
                 execution_reference, created_at_ms
ON autoagent_sessions
WHEN OLD.session_id IS NOT NEW.session_id
  OR OLD.cwd IS NOT NEW.cwd
  OR OLD.output_dir IS NOT NEW.output_dir
  OR OLD.skill IS NOT NEW.skill
  OR OLD.method IS NOT NEW.method
  OR OLD.evolution_goal IS NOT NEW.evolution_goal
  OR OLD.creation_receipt_sha256 IS NOT NEW.creation_receipt_sha256
  OR OLD.execution_reference_type IS NOT NEW.execution_reference_type
  OR OLD.execution_reference IS NOT NEW.execution_reference
  OR OLD.created_at_ms IS NOT NEW.created_at_ms
BEGIN
  SELECT RAISE(ABORT, 'immutable AutoAgent session authority');
END;

CREATE TRIGGER autoagent_sessions_terminal_transition
BEFORE UPDATE ON autoagent_sessions
WHEN NOT (
    OLD.status = 'running'
    AND NEW.status IN ('done', 'error', 'cancelled', 'interrupted')
    OR (
        OLD.status = 'running'
        AND NEW.status = 'running'
        AND OLD.cancel_requested_at_ms IS NULL
        AND NEW.cancel_requested_at_ms IS NOT NULL
    )
    OR (
        OLD.status = 'running'
        AND NEW.status = 'running'
        AND OLD.owner_stopped_at_ms IS NULL
        AND OLD.owner_stop_evidence IS NULL
        AND NEW.owner_stopped_at_ms IS NOT NULL
        AND NEW.owner_stop_evidence = 'process_tree_absent_v1'
    )
)
BEGIN
  SELECT RAISE(ABORT, 'invalid AutoAgent session state transition');
END;

CREATE TRIGGER autoagent_sessions_revision_transition
BEFORE UPDATE ON autoagent_sessions
WHEN NEW.revision != OLD.revision + 1
  OR NEW.updated_at_ms < OLD.updated_at_ms
BEGIN
  SELECT RAISE(ABORT, 'invalid AutoAgent session revision transition');
END;

CREATE TRIGGER autoagent_sessions_append_only_delete
BEFORE DELETE ON autoagent_sessions
BEGIN
  SELECT RAISE(ABORT, 'AutoAgent session authority is append-only');
END;
"""

MIGRATION_12_SQL = r"""
CREATE TABLE control_authority (
    singleton_id          INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    control_authority_id  TEXT NOT NULL UNIQUE CHECK (
                              length(control_authority_id) = 64
                              AND control_authority_id
                                  NOT GLOB '*[^0-9a-f]*'
                          )
) STRICT;

INSERT INTO control_authority (singleton_id, control_authority_id)
VALUES (1, lower(hex(randomblob(32))));

CREATE TRIGGER control_authority_singleton_insert
BEFORE INSERT ON control_authority
BEGIN
  SELECT RAISE(ABORT, 'Control authority is an immutable singleton');
END;

CREATE TRIGGER control_authority_immutable_update
BEFORE UPDATE ON control_authority
BEGIN
  SELECT RAISE(ABORT, 'Control authority is immutable');
END;

CREATE TRIGGER control_authority_append_only_delete
BEFORE DELETE ON control_authority
BEGIN
  SELECT RAISE(ABORT, 'Control authority is append-only');
END;

CREATE TRIGGER autoagent_receipt_start_cancellation_capacity_admission
BEFORE INSERT ON autoagent_sessions
WHEN NEW.creation_receipt_sha256 IS NOT NULL
 AND NOT EXISTS (
       SELECT 1 FROM autoagent_start_cancellations
       WHERE session_id = NEW.session_id
         AND creation_receipt_sha256 = NEW.creation_receipt_sha256
     )
 AND (SELECT cancellation_count FROM autoagent_capacity WHERE singleton_id = 1)
     >= 100000
BEGIN
  SELECT RAISE(ABORT, 'AutoAgent cancellation capacity exhausted');
END;
"""

MIGRATION_1_SHA256: Final = (
    "f87de47352b32e31892e9a6494d040108739fde262d386d3d3e78225d51fb48e"
)
MIGRATION_2_SHA256: Final = (
    "a452d799ca308923e5a71f7396f754f0e1a83e68a6fbd948a55a69a4f7738478"
)
MIGRATION_3_SHA256: Final = (
    "a89c6a289e7ec762f958ab2b63c5eb26f08b6c749e1253e9003c70d6f5cf9769"
)
MIGRATION_4_SHA256: Final = (
    "da99461bb52c019ba8bfea210ffac819ce44cfc7eb2055208980d70c7cfb63db"
)
MIGRATION_5_SHA256: Final = (
    "75d8a9138b56d886f954758992d7cc7fed06c0b5f272e1c1d5f5be8117ae64e2"
)
MIGRATION_6_SHA256: Final = (
    "842d331e3afce0046f7b09eb9ce6fa7397d52ea22bf68025a120fe74e978f459"
)
MIGRATION_7_SHA256: Final = (
    "392c2e4e3f9a6c25b86c8dd69b42fb676df4a0d7e7fe7c5c98bec83ac2208ec4"
)
MIGRATION_8_SHA256: Final = (
    "5464aba854a4de4b2488f29af4542ee9cc0b08d78a72a18e6a8aebaf63d6ed6b"
)
MIGRATION_9_SHA256: Final = (
    "26cf5d5e5b5aa4672f7fba558f87d9f06010a433b3e99ef2e0228f4a2b6bd85f"
)
MIGRATION_10_SHA256: Final = (
    "5c49d8b0d86a60dd728885b2234bd01096088398c26afc96a8c3146678be65ed"
)
MIGRATION_11_SHA256: Final = (
    "da2829c3f0e0ac5595a62492b003663c88a282427ab9b95bf5addc5dfa8a85a6"
)
MIGRATION_12_SHA256: Final = (
    "6857ec6aaab5e4b8fba644aeaae2319c0068c22267402c318e4549be7bda587b"
)


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    expected_checksum_sha256: str

    def __post_init__(self) -> None:
        actual = hashlib.sha256(self.sql.encode("utf-8")).hexdigest()
        if actual != self.expected_checksum_sha256:
            raise ControlIntegrityError(
                "historical migration source checksum mismatch for version "
                f"{self.version}: expected {self.expected_checksum_sha256}, got {actual}"
            )

    @property
    def checksum(self) -> str:
        return self.expected_checksum_sha256


MIGRATIONS = (
    Migration(1, "initial_control_state", MIGRATION_1_SQL, MIGRATION_1_SHA256),
    Migration(2, "close_terminal_code_vocabulary", MIGRATION_2_SQL, MIGRATION_2_SHA256),
    Migration(
        3,
        "bind_terminal_receipt_to_transcript",
        MIGRATION_3_SQL,
        MIGRATION_3_SHA256,
    ),
    Migration(
        4,
        "close_legacy_import_state_machine",
        MIGRATION_4_SQL,
        MIGRATION_4_SHA256,
    ),
    Migration(
        5,
        "bind_legacy_cutover_to_transcript_store",
        MIGRATION_5_SQL,
        MIGRATION_5_SHA256,
    ),
    Migration(
        6,
        "bind_control_to_transcript_store",
        MIGRATION_6_SQL,
        MIGRATION_6_SHA256,
    ),
    Migration(
        7,
        "commit_turn_attachment_batches",
        MIGRATION_7_SQL,
        MIGRATION_7_SHA256,
    ),
    Migration(
        8,
        "fence_governed_run_execution_owners",
        MIGRATION_8_SQL,
        MIGRATION_8_SHA256,
    ),
    Migration(
        9,
        "persist_run_integrity_incidents",
        MIGRATION_9_SQL,
        MIGRATION_9_SHA256,
    ),
    Migration(
        10,
        "index_remote_run_observation_pages",
        MIGRATION_10_SQL,
        MIGRATION_10_SHA256,
    ),
    Migration(
        11,
        "persist_autoagent_session_authority",
        MIGRATION_11_SQL,
        MIGRATION_11_SHA256,
    ),
    Migration(
        12,
        "persist_control_authority_identity",
        MIGRATION_12_SQL,
        MIGRATION_12_SHA256,
    ),
)


def apply_migrations(connection: sqlite3.Connection, *, now_ms: int) -> None:
    """Apply missing migrations and reject checksum/version drift."""
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    applied: dict[int, tuple[str, str]] = {}
    if table_exists:
        rows = connection.execute(
            "SELECT version, name, checksum_sha256 FROM schema_migrations"
        ).fetchall()
        applied = {int(row[0]): (str(row[1]), str(row[2])) for row in rows}

    known_versions = {migration.version for migration in MIGRATIONS}
    unknown = sorted(set(applied) - known_versions)
    if unknown:
        raise ControlIntegrityError(
            f"Control Database has unsupported migration versions: {unknown}"
        )

    for migration in MIGRATIONS:
        existing = applied.get(migration.version)
        if existing is not None:
            name, checksum = existing
            if name != migration.name or checksum != migration.checksum:
                raise ControlIntegrityError(
                    f"migration checksum mismatch for version {migration.version}"
                )
            continue

        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n"
            "INSERT INTO schema_migrations"
            " (version, name, checksum_sha256, applied_at_ms) VALUES "
            f"({migration.version}, '{migration.name}', "
            f"'{migration.checksum}', {int(now_ms)});\n"
            "COMMIT;"
        )
        try:
            connection.executescript(script)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    result = connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or str(result[0]).lower() != "ok":
        raise ControlIntegrityError(
            f"Control Database integrity check failed: {result}"
        )
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise ControlIntegrityError(
            f"Control Database foreign-key check failed: {violations[:5]}"
        )


__all__ = [
    "MIGRATIONS",
    "MIGRATION_1_SHA256",
    "MIGRATION_2_SHA256",
    "MIGRATION_3_SHA256",
    "MIGRATION_4_SHA256",
    "MIGRATION_5_SHA256",
    "MIGRATION_6_SHA256",
    "MIGRATION_7_SHA256",
    "MIGRATION_8_SHA256",
    "MIGRATION_9_SHA256",
    "MIGRATION_10_SHA256",
    "MIGRATION_11_SHA256",
    "MIGRATION_12_SHA256",
    "TERMINAL_CODE_POLICY_TRIGGER_NAMES",
    "V2_RUN_TERMINAL_CODES_BY_STATUS",
    "V2_TURN_TERMINAL_CODES_BY_STATUS",
    "apply_migrations",
]
