from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omicsclaw.attachments.models import AttachmentBatchCommitment
from omicsclaw.control import schema as control_schema
from omicsclaw.control import (
    AssignmentStatus,
    ControlDatabaseOwnedError,
    ControlIntegrityError,
    ControlStateRepository,
    DeliveryAttemptOutcome,
    DeliveryItemPlan,
    DeliveryPlan,
    ProjectLifecycleStatus,
    ProjectionIntentInput,
    RunAcceptanceIntent,
    RunAcceptanceStatus,
    RunIntegrityEvidenceCode,
    RunIntegrityIncidentError,
    RunIntegrityIncidentIntent,
    RunIntegrityIncidentType,
    RunReport,
    TurnAcceptanceIntent,
    TurnAcceptanceStatus,
    TurnTranscriptRef,
)
from omicsclaw.control.schema import MIGRATIONS, apply_migrations
from omicsclaw.control.terminal_codes import (
    RUN_TERMINAL_CODES_BY_STATUS,
    TURN_TERMINAL_CODES_BY_STATUS,
)


ROOT = Path(__file__).resolve().parents[2]


def _transcript_ref(seed: str) -> TurnTranscriptRef:
    return TurnTranscriptRef(
        hashlib.sha256(f"entry:{seed}".encode()).hexdigest()[:32],
        hashlib.sha256(f"content:{seed}".encode()).hexdigest(),
    )


def _fingerprint(character: str) -> str:
    return character * 64


def _attachment_commitment(
    *,
    store_id: str = "c" * 32,
    batch_id: str = "d" * 32,
    turn_id: str = "a" * 32,
    conversation_id: str = "b" * 32,
    record_count: int = 2,
    records_sha256: str = "e" * 64,
) -> AttachmentBatchCommitment:
    return AttachmentBatchCommitment(
        schema_version=1,
        store_id=store_id,
        batch_id=batch_id,
        turn_id=turn_id,
        conversation_id=conversation_id,
        record_count=record_count,
        records_sha256=records_sha256,
    )


def _reply_target(kind: str = "desktop") -> dict[str, str]:
    if kind == "channel":
        return {
            "kind": "channel",
            "adapter": "telegram",
            "account_namespace": "primary",
            "destination_id": "chat-7",
        }
    return {
        "kind": kind,
        "installation_id": "test-installation",
        "profile_id": "owner",
        "slot": "main",
    }


def _turn_intent(
    request_id: str,
    *,
    fingerprint: str | None = None,
    surface: str = "desktop",
    project_id: str | None = None,
    new_conversation: bool = False,
) -> TurnAcceptanceIntent:
    return TurnAcceptanceIntent(
        surface=surface,
        source_namespace=f"{surface}/v1/test",
        source_request_id=request_id,
        fingerprint_version=1,
        fingerprint_sha256=fingerprint or _fingerprint("a"),
        reply_target=_reply_target(surface if surface != "channel" else "channel"),
        project_id=project_id,
        new_conversation=new_conversation,
    )


def _delivery_plan(text_ref: str) -> DeliveryPlan:
    return DeliveryPlan(
        terminal_kind="succeeded",
        items=(
            DeliveryItemPlan(
                item_kind="text",
                content_store="transcript",
                content_ref=text_ref,
                content_sha256=_fingerprint("c"),
            ),
            DeliveryItemPlan(
                item_kind="text",
                content_store="transcript",
                content_ref=text_ref,
                content_sha256=_fingerprint("d"),
            ),
        ),
    )


def test_repository_lock_schema_and_checksum_are_fail_closed(tmp_path):
    repo = ControlStateRepository(tmp_path)

    with pytest.raises(ControlDatabaseOwnedError):
        ControlStateRepository(tmp_path)

    db_path = repo.database_path
    repo.close()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum_sha256 = ? WHERE version = 1",
            ("0" * 64,),
        )
        connection.commit()

    with pytest.raises(ControlIntegrityError, match="migration checksum"):
        ControlStateRepository(tmp_path)


def test_control_authority_id_is_opaque_persistent_and_state_root_specific(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"

    with ControlStateRepository(first_root) as repository:
        first = repository.control_authority_id
        assert len(first) == 64
        assert all(character in "0123456789abcdef" for character in first)

    with ControlStateRepository(first_root) as reopened:
        assert reopened.control_authority_id == first

    with ControlStateRepository(second_root) as repository:
        assert repository.control_authority_id != first


def test_control_authority_id_is_database_immutable(tmp_path: Path) -> None:
    with ControlStateRepository(tmp_path) as repository:
        authority_id = repository.control_authority_id
        with sqlite3.connect(repository.database_path) as connection:
            with pytest.raises(sqlite3.IntegrityError, match="authority"):
                connection.execute(
                    "UPDATE control_authority SET control_authority_id = ? "
                    "WHERE singleton_id = 1",
                    ("f" * 64,),
                )
            connection.rollback()
            with pytest.raises(sqlite3.IntegrityError, match="authority"):
                connection.execute(
                    "DELETE FROM control_authority WHERE singleton_id = 1"
                )
            connection.rollback()

        assert repository.control_authority_id == authority_id


def test_attachment_store_binding_is_singleton_and_idempotent(tmp_path):
    store_id = "a" * 32
    with ControlStateRepository(tmp_path) as repo:
        assert repo.get_attachment_store_binding() is None

        first = repo.bind_attachment_store(store_id)
        repeated = repo.bind_attachment_store(store_id)

        assert first.changed is True
        assert first.code == "bound"
        assert repeated.changed is False
        assert repeated.code == "bound"
        assert repo.get_attachment_store_binding() == store_id
        repo.verify_attachment_store_binding(store_id)

        with pytest.raises(
            ControlIntegrityError,
            match="different Attachment Store",
        ):
            repo.bind_attachment_store("b" * 32)


def test_attachment_control_migration_is_checksum_pinned_and_binding_immutable(
    tmp_path,
):
    migration = MIGRATIONS[6]
    assert migration.version == 7
    assert migration.name == "commit_turn_attachment_batches"
    assert migration.checksum == (
        "392c2e4e3f9a6c25b86c8dd69b42fb676df4a0d7e7fe7c5c98bec83ac2208ec4"
    )
    assert hashlib.sha256(migration.sql.encode("utf-8")).hexdigest() == (
        migration.checksum
    )

    with ControlStateRepository(tmp_path) as repo:
        repo.bind_attachment_store("a" * 32)
        database_path = repo.database_path

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable Attachment Store"):
            connection.execute(
                "UPDATE attachment_store_bindings SET store_id = ?",
                ("b" * 32,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable Attachment Store"):
            connection.execute("DELETE FROM attachment_store_bindings")


def test_governed_run_owner_migration_is_pinned_unique_and_write_once(tmp_path):
    migration = next(item for item in MIGRATIONS if item.version == 8)
    assert migration.version == 8
    assert migration.name == "fence_governed_run_execution_owners"
    assert migration.checksum == (
        "5464aba854a4de4b2488f29af4542ee9cc0b08d78a72a18e6a8aebaf63d6ed6b"
    )
    assert hashlib.sha256(migration.sql.encode("utf-8")).hexdigest() == (
        migration.checksum
    )

    with ControlStateRepository(tmp_path) as repo:
        first = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="owner-first",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("a"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/owner-first",
            )
        )
        reference = "omicsclaw-run-" + "a" * 24 + ".scope"
        assigned = repo.assign_run(
            first.run_id,
            executor_kind="local-simple-skill-v1",
            execution_reference_type="linux-user-systemd-bwrap-v1",
            execution_reference=reference,
        )
        assert assigned.status is AssignmentStatus.ASSIGNED
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            repo.update_execution_reference(
                first.run_id,
                assigned.assignment_id,
                reference_type="linux-user-systemd-bwrap-v1",
                reference="omicsclaw-run-" + "b" * 24 + ".scope",
            )

        second = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="owner-second",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("b"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/owner-second",
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            repo.assign_run(
                second.run_id,
                executor_kind="local-simple-skill-v1",
                execution_reference_type="linux-user-systemd-bwrap-v1",
                execution_reference=reference,
            )

        third = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="owner-third",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("c"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/owner-third",
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="governed owner"):
            repo.assign_run(
                third.run_id,
                executor_kind="local-simple-skill-v1",
            )

        legacy = repo.assign_run(third.run_id, executor_kind="legacy-local")
        with pytest.raises(sqlite3.IntegrityError, match="invalid governed owner"):
            repo.update_execution_reference(
                third.run_id,
                legacy.assignment_id,
                reference_type="linux-user-systemd-bwrap-v1",
                reference="not-a-scope",
            )


def test_run_integrity_incident_migration_is_pinned_content_free_and_append_only(
    tmp_path,
):
    migration = next(item for item in MIGRATIONS if item.version == 9)
    assert migration.version == 9
    assert migration.name == "persist_run_integrity_incidents"
    assert migration.checksum == (
        "26cf5d5e5b5aa4672f7fba558f87d9f06010a433b3e99ef2e0228f4a2b6bd85f"
    )
    assert hashlib.sha256(migration.sql.encode("utf-8")).hexdigest() == (
        migration.checksum
    )
    with pytest.raises(ValueError, match="does not match"):
        RunIntegrityIncidentIntent(
            run_id="a" * 32,
            assignment_id="b" * 32,
            incident_type=RunIntegrityIncidentType.TERMINAL_REPORT_CONFLICT,
            evidence_code=RunIntegrityEvidenceCode.ASSIGNMENT_MISSING,
        )

    with ControlStateRepository(tmp_path) as repo:
        run = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="incident-schema",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("4"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/incident-schema",
            )
        )
        assignment = repo.assign_run(run.run_id, executor_kind="local")
        appended = repo.record_run_integrity_incident(
            RunIntegrityIncidentIntent(
                run_id=run.run_id,
                assignment_id=assignment.assignment_id,
                incident_type=RunIntegrityIncidentType.EXECUTION_OWNER_UNCONFIRMED,
                evidence_code=RunIntegrityEvidenceCode.EXECUTION_OWNER_STOP_UNCONFIRMED,
            )
        )
        database_path = repo.database_path

    assert appended.created is True
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(run_integrity_incidents)"
            ).fetchall()
        }
        assert columns == {
            "incident_sequence",
            "incident_id",
            "run_id",
            "assignment_id",
            "incident_type",
            "evidence_code",
            "receipt_revision",
            "evidence_schema_version",
            "evidence_sha256",
            "created_at_ms",
        }
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                INSERT INTO run_integrity_incidents (
                    incident_id, run_id, assignment_id, incident_type,
                    evidence_code, receipt_revision, evidence_schema_version,
                    evidence_sha256, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "9" * 32,
                    run.run_id,
                    assignment.assignment_id,
                    "terminal_report_conflict",
                    "assignment_missing",
                    2,
                    1,
                    "8" * 64,
                    1,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE run_integrity_incidents SET created_at_ms = created_at_ms + 1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM run_integrity_incidents")


def test_remote_run_observation_migration_indexes_bounded_pages(tmp_path):
    migration = next(item for item in MIGRATIONS if item.version == 10)
    assert migration.name == "index_remote_run_observation_pages"
    assert migration.checksum == (
        "5c49d8b0d86a60dd728885b2234bd01096088398c26afc96a8c3146678be65ed"
    )
    assert hashlib.sha256(migration.sql.encode("utf-8")).hexdigest() == (
        migration.checksum
    )

    with ControlStateRepository(tmp_path) as repo:
        database_path = repo.database_path

    with sqlite3.connect(database_path) as connection:
        def indexed_columns(name):
            return [
                (row[2], bool(row[3]))
                for row in connection.execute(f"PRAGMA index_xinfo({name})")
                if row[5]
            ]

        assert indexed_columns("runs_kind_scope_created_idx") == [
            ("run_kind", False),
            ("scope_kind", False),
            ("created_at_ms", True),
            ("run_id", True),
        ]
        assert indexed_columns("runs_kind_scope_status_created_idx") == [
            ("run_kind", False),
            ("scope_kind", False),
            ("status", False),
            ("created_at_ms", True),
            ("run_id", True),
        ]


def test_accept_turn_commits_attachment_batch_with_turn_and_ingress_binding(tmp_path):
    store_id = "c" * 32
    turn_id = "a" * 32
    conversation_id = "b" * 32
    commitment = _attachment_commitment(
        store_id=store_id,
        turn_id=turn_id,
        conversation_id=conversation_id,
    )

    with ControlStateRepository(tmp_path) as repo:
        repo.bind_attachment_store(store_id)
        accepted = repo.accept_turn(
            _turn_intent("attachment-request", new_conversation=True),
            proposed_turn_id=turn_id,
            proposed_conversation_id=conversation_id,
            attachment_commitment=commitment,
        )

        assert accepted.status is TurnAcceptanceStatus.ACCEPTED
        assert repo.get_turn_attachment_commitment(turn_id) == commitment
        assert repo.list_turn_attachment_commitments() == (commitment,)
        assert repo.list_turn_attachment_commitments(
            conversation_id=conversation_id
        ) == (commitment,)


def test_duplicate_turn_acceptance_does_not_write_a_new_attachment_commitment(
    tmp_path,
):
    store_id = "c" * 32
    turn_id = "a" * 32
    conversation_id = "b" * 32
    intent = _turn_intent("attachment-duplicate", new_conversation=True)
    original = _attachment_commitment(
        store_id=store_id,
        turn_id=turn_id,
        conversation_id=conversation_id,
    )
    replacement = _attachment_commitment(
        store_id=store_id,
        batch_id="f" * 32,
        turn_id=turn_id,
        conversation_id=conversation_id,
        records_sha256="0" * 64,
    )

    with ControlStateRepository(tmp_path) as repo:
        repo.bind_attachment_store(store_id)
        first = repo.accept_turn(
            intent,
            proposed_turn_id=turn_id,
            proposed_conversation_id=conversation_id,
            attachment_commitment=original,
        )
        duplicate = repo.accept_turn(intent, attachment_commitment=replacement)

        assert first.status is TurnAcceptanceStatus.ACCEPTED
        assert duplicate.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.turn_id == turn_id
        assert repo.list_turn_attachment_commitments() == (original,)


@pytest.mark.parametrize(
    ("bound_store_id", "error_pattern"),
    (
        (None, "no Attachment Store binding"),
        ("f" * 32, "different Attachment Store"),
    ),
)
def test_attachment_commitment_requires_the_bound_store_and_rolls_back_turn(
    tmp_path,
    bound_store_id,
    error_pattern,
):
    turn_id = "a" * 32
    conversation_id = "b" * 32
    intent = _turn_intent("attachment-store-mismatch", new_conversation=True)
    commitment = _attachment_commitment(
        turn_id=turn_id,
        conversation_id=conversation_id,
    )

    with ControlStateRepository(tmp_path) as repo:
        if bound_store_id is not None:
            repo.bind_attachment_store(bound_store_id)
        with pytest.raises(ControlIntegrityError, match=error_pattern):
            repo.accept_turn(
                intent,
                proposed_turn_id=turn_id,
                proposed_conversation_id=conversation_id,
                attachment_commitment=commitment,
            )

        assert repo.list_conversations() == ()
        assert repo.list_turn_attachment_commitments() == ()
        assert (
            repo.lookup_ingress_turn_id(
                surface=intent.surface,
                source_namespace=intent.source_namespace,
                source_request_id=intent.source_request_id,
            )
            is None
        )
        with pytest.raises(KeyError):
            repo.get_turn(turn_id)


@pytest.mark.parametrize(
    ("field", "value", "error_pattern"),
    (
        ("schema_version", 2, "schema_version"),
        ("store_id", "invalid", "32 lowercase hexadecimal"),
        ("batch_id", "invalid", "32 lowercase hexadecimal"),
        ("turn_id", "invalid", "32 lowercase hexadecimal"),
        ("conversation_id", "invalid", "32 lowercase hexadecimal"),
        ("record_count", 0, "positive unsigned integer"),
        ("records_sha256", "invalid", "lowercase SHA-256"),
    ),
)
def test_accept_turn_defensively_validates_attachment_commitment_and_rolls_back(
    tmp_path,
    field,
    value,
    error_pattern,
):
    turn_id = "a" * 32
    conversation_id = "b" * 32
    commitment = _attachment_commitment(
        turn_id=turn_id,
        conversation_id=conversation_id,
    )
    object.__setattr__(commitment, field, value)

    with ControlStateRepository(tmp_path) as repo:
        repo.bind_attachment_store("c" * 32)
        with pytest.raises(ValueError, match=error_pattern):
            repo.accept_turn(
                _turn_intent("invalid-attachment-commitment", new_conversation=True),
                proposed_turn_id=turn_id,
                proposed_conversation_id=conversation_id,
                attachment_commitment=commitment,
            )

        assert repo.list_conversations() == ()
        assert repo.list_turn_attachment_commitments() == ()


@pytest.mark.parametrize(
    ("committed_turn_id", "committed_conversation_id", "error_pattern"),
    (
        ("f" * 32, "b" * 32, "Turn ID does not match"),
        ("a" * 32, "f" * 32, "Conversation ID does not match"),
    ),
)
def test_attachment_commitment_ids_must_match_the_accepted_turn(
    tmp_path,
    committed_turn_id,
    committed_conversation_id,
    error_pattern,
):
    commitment = _attachment_commitment(
        turn_id=committed_turn_id,
        conversation_id=committed_conversation_id,
    )
    with ControlStateRepository(tmp_path) as repo:
        repo.bind_attachment_store(commitment.store_id)
        with pytest.raises(ValueError, match=error_pattern):
            repo.accept_turn(
                _turn_intent("attachment-id-mismatch", new_conversation=True),
                proposed_turn_id="a" * 32,
                proposed_conversation_id="b" * 32,
                attachment_commitment=commitment,
            )

        assert repo.list_conversations() == ()
        assert repo.list_turn_attachment_commitments() == ()


def test_attachment_batch_id_is_unique_and_reuse_rolls_back_second_turn(tmp_path):
    store_id = "c" * 32
    conversation_id = "b" * 32
    original = _attachment_commitment(
        store_id=store_id,
        turn_id="a" * 32,
        conversation_id=conversation_id,
    )
    reused = _attachment_commitment(
        store_id=store_id,
        turn_id="f" * 32,
        conversation_id=conversation_id,
    )
    second_intent = _turn_intent("attachment-batch-reuse")

    with ControlStateRepository(tmp_path) as repo:
        repo.bind_attachment_store(store_id)
        repo.accept_turn(
            _turn_intent("attachment-batch-original", new_conversation=True),
            proposed_turn_id=original.turn_id,
            proposed_conversation_id=conversation_id,
            attachment_commitment=original,
        )
        with pytest.raises(
            ControlIntegrityError,
            match="already committed to a different Turn",
        ):
            repo.accept_turn(
                second_intent,
                proposed_turn_id=reused.turn_id,
                attachment_commitment=reused,
            )

        assert repo.list_turn_attachment_commitments() == (original,)
        assert (
            repo.lookup_ingress_turn_id(
                surface=second_intent.surface,
                source_namespace=second_intent.source_namespace,
                source_request_id=second_intent.source_request_id,
            )
            is None
        )
        with pytest.raises(KeyError):
            repo.get_turn(reused.turn_id)


def test_attachment_commitment_rolls_back_with_accept_turn_transaction(tmp_path):
    armed = True

    def fault(name):
        if armed and name == "accept_turn.before_commit":
            raise RuntimeError("injected attachment commit fault")

    commitment = _attachment_commitment()
    intent = _turn_intent("attachment-atomic-fault", new_conversation=True)
    with ControlStateRepository(tmp_path, fault_hook=fault) as repo:
        repo.bind_attachment_store(commitment.store_id)
        with pytest.raises(RuntimeError, match="injected attachment commit fault"):
            repo.accept_turn(
                intent,
                proposed_turn_id=commitment.turn_id,
                proposed_conversation_id=commitment.conversation_id,
                attachment_commitment=commitment,
            )
        armed = False

        assert repo.list_conversations() == ()
        assert repo.list_turn_attachment_commitments() == ()
        assert (
            repo.lookup_ingress_turn_id(
                surface=intent.surface,
                source_namespace=intent.source_namespace,
                source_request_id=intent.source_request_id,
            )
            is None
        )


def test_turn_attachment_commitment_is_immutable_in_sqlite(tmp_path):
    commitment = _attachment_commitment()
    with ControlStateRepository(tmp_path) as repo:
        repo.bind_attachment_store(commitment.store_id)
        repo.accept_turn(
            _turn_intent("attachment-immutable", new_conversation=True),
            proposed_turn_id=commitment.turn_id,
            proposed_conversation_id=commitment.conversation_id,
            attachment_commitment=commitment,
        )
        database_path = repo.database_path

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="immutable Turn Attachment commitment",
        ):
            connection.execute(
                "UPDATE turn_attachment_commitments SET attachment_count = 3"
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="immutable Turn Attachment commitment",
        ):
            connection.execute("DELETE FROM turn_attachment_commitments")


def test_delivery_capacity_includes_process_local_reservations(tmp_path):
    target = _reply_target("channel")
    with ControlStateRepository(tmp_path) as repo:
        assert repo.has_delivery_capacity(
            target,
            max_total=1,
            max_per_account=1,
        )
        assert not repo.has_delivery_capacity(
            target,
            max_total=1,
            max_per_account=1,
            reserved_total=1,
        )
        assert not repo.has_delivery_capacity(
            target,
            max_total=2,
            max_per_account=1,
            reserved_for_account=1,
        )


def test_historical_control_migration_checksums_and_v2_policy_are_pinned():
    expected_checksums = (
        "f87de47352b32e31892e9a6494d040108739fde262d386d3d3e78225d51fb48e",
        "a452d799ca308923e5a71f7396f754f0e1a83e68a6fbd948a55a69a4f7738478",
    )
    assert tuple(migration.checksum for migration in MIGRATIONS[:2]) == (
        expected_checksums
    )
    assert (
        tuple(
            hashlib.sha256(migration.sql.encode("utf-8")).hexdigest()
            for migration in MIGRATIONS[:2]
        )
        == expected_checksums
    )
    assert control_schema.V2_TURN_TERMINAL_CODES_BY_STATUS == {
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
    assert control_schema.V2_RUN_TERMINAL_CODES_BY_STATUS == {
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


def test_live_terminal_code_maps_match_latest_schema_policy_snapshot():
    # When V3 is introduced, point this compatibility gate at the new V3
    # snapshots.  Never update the immutable V2 snapshots to make it pass.
    assert TURN_TERMINAL_CODES_BY_STATUS == (
        control_schema.V2_TURN_TERMINAL_CODES_BY_STATUS
    )
    assert RUN_TERMINAL_CODES_BY_STATUS == (
        control_schema.V2_RUN_TERMINAL_CODES_BY_STATUS
    )


def test_future_terminal_code_policy_migration_audits_then_replaces_v2_triggers(
    tmp_path,
):
    with ControlStateRepository(tmp_path) as repo:
        turn = repo.accept_turn(_turn_intent("terminal-code-v3-replacement"))
        database_path = repo.database_path

    turn_codes = dict(control_schema.V2_TURN_TERMINAL_CODES_BY_STATUS)
    turn_codes["failed"] = turn_codes["failed"] | {"worker_evicted"}
    replacement_sql = control_schema._render_terminal_code_policy_migration_sql(
        turn_codes_by_status=turn_codes,
        run_codes_by_status=control_schema.V2_RUN_TERMINAL_CODES_BY_STATUS,
        replace_existing_triggers=True,
    )

    assert replacement_sql.index("SELECT 1 FROM turns") < replacement_sql.index(
        "DROP TRIGGER turns_terminal_code_policy_insert"
    )
    assert replacement_sql.index(
        "DROP TRIGGER turns_terminal_code_policy_update"
    ) < replacement_sql.index("CREATE TRIGGER turns_terminal_code_policy_insert")

    with sqlite3.connect(database_path) as connection:
        connection.executescript(f"BEGIN IMMEDIATE;\n{replacement_sql}\nCOMMIT;")
        connection.execute(
            "UPDATE turns SET status = 'failed', terminal_code = ? WHERE turn_id = ?",
            ("worker_evicted", turn.turn_id),
        )


def test_future_terminal_code_policy_migration_keeps_v2_triggers_on_failed_audit(
    tmp_path,
):
    with ControlStateRepository(tmp_path) as repo:
        accepted = repo.accept_turn(_turn_intent("terminal-code-v3-audit"))
        repo.start_turn(accepted.turn_id)
        repo.terminalize_turn(
            accepted.turn_id,
            terminal_status="failed",
            terminal_code="worker_failed",
        )
        database_path = repo.database_path

    turn_codes = dict(control_schema.V2_TURN_TERMINAL_CODES_BY_STATUS)
    turn_codes["failed"] = turn_codes["failed"] - {"worker_failed"}
    replacement_sql = control_schema._render_terminal_code_policy_migration_sql(
        turn_codes_by_status=turn_codes,
        run_codes_by_status=control_schema.V2_RUN_TERMINAL_CODES_BY_STATUS,
        replace_existing_triggers=True,
    )

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="must_be_valid"):
            connection.executescript(f"BEGIN IMMEDIATE;\n{replacement_sql}\nCOMMIT;")
        connection.rollback()

        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert set(control_schema.TERMINAL_CODE_POLICY_TRIGGER_NAMES) <= trigger_names


def test_repository_lifetime_lock_is_enforced_across_processes(tmp_path):
    child = """
import sys
from omicsclaw.control import ControlDatabaseOwnedError, ControlStateRepository

try:
    repository = ControlStateRepository(sys.argv[1])
except ControlDatabaseOwnedError:
    raise SystemExit(42)
else:
    repository.close()
    raise SystemExit(0)
"""

    repository = ControlStateRepository(tmp_path)
    held = subprocess.run(
        [sys.executable, "-c", child, str(tmp_path)],
        cwd=ROOT,
        check=False,
    )
    repository.close()
    released = subprocess.run(
        [sys.executable, "-c", child, str(tmp_path)],
        cwd=ROOT,
        check=False,
    )

    assert held.returncode == 42
    assert released.returncode == 0


def test_terminal_code_migration_rejects_preexisting_untrusted_detail(tmp_path):
    database_path = tmp_path / "legacy-control.db"
    initial = MIGRATIONS[0]
    with sqlite3.connect(database_path) as connection:
        connection.executescript(initial.sql)
        connection.execute(
            """
            INSERT INTO schema_migrations (
                version, name, checksum_sha256, applied_at_ms
            ) VALUES (?, ?, ?, ?)
            """,
            (initial.version, initial.name, initial.checksum, 1),
        )
        connection.execute(
            """
            INSERT INTO conversations (
                conversation_id, surface, reply_target_version,
                reply_target_key, reply_target_json, project_id,
                revision, created_at_ms, updated_at_ms
            ) VALUES ('legacy-conversation', 'desktop', 1, 'legacy-target',
                      '{}', NULL, 1, 1, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO turns (
                turn_id, conversation_id, turn_kind, status, retry_of_turn_id,
                terminal_code, created_at_ms, started_at_ms, finished_at_ms,
                revision
            ) VALUES ('legacy-turn', 'legacy-conversation', 'agent', 'failed',
                      NULL, 'sk_sensitivecredential123', 1, 1, 1, 1)
            """
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="must_be_valid"):
            apply_migrations(connection, now_ms=2)

        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 2"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership/mode contract")
def test_control_state_root_rejects_symlink_and_exposed_permissions(tmp_path):
    real_root = tmp_path / "real"
    real_root.mkdir(mode=0o700)
    symlink_root = tmp_path / "linked"
    symlink_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ControlIntegrityError, match="must not be a symlink"):
        ControlStateRepository(symlink_root)

    exposed_root = tmp_path / "exposed"
    exposed_root.mkdir(mode=0o755)
    exposed_root.chmod(0o755)
    with pytest.raises(ControlIntegrityError, match="owner-private"):
        ControlStateRepository(exposed_root)


def test_turn_acceptance_is_atomic_idempotent_and_archive_aware(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        project = repo.create_project("PBMC study")
        intent = _turn_intent("request-1", project_id=project.project_id)

        accepted = repo.accept_turn(intent)
        duplicate = repo.accept_turn(intent)
        conflict = repo.accept_turn(
            _turn_intent(
                "request-1",
                fingerprint=_fingerprint("b"),
                project_id=project.project_id,
            )
        )

        assert accepted.status is TurnAcceptanceStatus.ACCEPTED
        assert duplicate.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.turn_id == accepted.turn_id
        assert conflict.status is TurnAcceptanceStatus.CONFLICT
        inspection = repo.inspect_ingress(
            surface=intent.surface,
            source_namespace=intent.source_namespace,
            source_request_id=intent.source_request_id,
            fingerprint_version=intent.fingerprint_version,
            fingerprint_sha256=intent.fingerprint_sha256,
        )
        assert inspection.state == "duplicate"
        assert inspection.canonical_id == accepted.turn_id

        busy = repo.archive_project(project.project_id)
        assert busy.status is ProjectLifecycleStatus.BUSY

        assert repo.start_turn(accepted.turn_id).changed is True
        repo.terminalize_turn(accepted.turn_id, terminal_status="succeeded")

        archived = repo.archive_project(project.project_id)
        assert archived.status is ProjectLifecycleStatus.CHANGED

        duplicate_after_archive = repo.accept_turn(intent)
        rejected_novel = repo.accept_turn(
            _turn_intent("request-2", project_id=project.project_id)
        )
        assert duplicate_after_archive.status is TurnAcceptanceStatus.DUPLICATE
        assert rejected_novel.status is TurnAcceptanceStatus.REJECTED
        assert rejected_novel.code == "project_archived"


def test_terminal_transcript_ref_commits_atomically_with_receipt(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        accepted = repo.accept_turn(_turn_intent("terminal-transcript-ref"))
        repo.start_turn(accepted.turn_id)
        expected = TurnTranscriptRef(
            entry_id="e" * 32,
            content_sha256="a" * 64,
        )

        terminalized = repo.terminalize_turn(
            accepted.turn_id,
            terminal_status="succeeded",
            transcript_ref=expected,
        )

        assert terminalized.changed is True
        assert repo.get_turn_terminal_ref(accepted.turn_id) == expected


def test_turn_observation_joins_receipt_project_and_terminal_ref_once(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        project = repo.create_project("Observation study")
        accepted = repo.accept_turn(
            _turn_intent("turn-observation", project_id=project.project_id)
        )

        queued = repo.get_turn_observation(accepted.turn_id)
        assert queued.receipt.status == "queued"
        assert queued.project_id == project.project_id
        assert queued.transcript_ref is None

        repo.start_turn(accepted.turn_id)
        expected = TurnTranscriptRef("d" * 32, "e" * 64)
        repo.terminalize_turn(
            accepted.turn_id,
            terminal_status="succeeded",
            transcript_ref=expected,
        )

        terminal = repo.get_turn_observation(accepted.turn_id)
        assert terminal.receipt.status == "succeeded"
        assert terminal.receipt.revision == 3
        assert terminal.project_id == project.project_id
        assert terminal.transcript_ref == expected


def test_terminal_transcript_ref_rolls_back_with_receipt_fault(tmp_path):
    armed = True

    def fault(name: str) -> None:
        if armed and name == "terminalize_turn.before_commit":
            raise RuntimeError("injected")

    with ControlStateRepository(tmp_path, fault_hook=fault) as repo:
        accepted = repo.accept_turn(_turn_intent("terminal-ref-fault"))
        repo.start_turn(accepted.turn_id)
        with pytest.raises(RuntimeError, match="injected"):
            repo.terminalize_turn(
                accepted.turn_id,
                terminal_status="succeeded",
                transcript_ref=TurnTranscriptRef("b" * 32, "c" * 64),
            )
        armed = False

        assert repo.get_turn(accepted.turn_id).status == "running"
        assert repo.get_turn_terminal_ref(accepted.turn_id) is None


def test_second_novel_turn_reuses_the_active_conversation(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        first = repo.accept_turn(_turn_intent("request-1"))
        second = repo.accept_turn(_turn_intent("request-2"))

        assert first.status is TurnAcceptanceStatus.ACCEPTED
        assert second.status is TurnAcceptanceStatus.ACCEPTED
        assert second.turn_id != first.turn_id
        assert second.conversation_id == first.conversation_id
        assert len(repo.list_conversations()) == 1


def test_turn_acceptance_plan_is_read_only_and_proposed_ids_are_opaque(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        intent = _turn_intent("request-1")
        turn_id = "a" * 32
        conversation_id = "b" * 32

        plan = repo.plan_turn_acceptance(
            intent,
            proposed_turn_id=turn_id,
            proposed_conversation_id=conversation_id,
        )

        assert plan.state == "novel"
        assert plan.turn_id == turn_id
        assert plan.conversation_id == conversation_id
        assert repo.list_conversations() == ()
        accepted = repo.accept_turn(
            intent,
            proposed_turn_id=plan.proposed_turn_id,
            proposed_conversation_id=plan.proposed_conversation_id,
        )
        assert accepted.turn_id == turn_id
        assert accepted.conversation_id == conversation_id

        with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
            repo.plan_turn_acceptance(
                _turn_intent("request-2"), proposed_turn_id="semantic-turn-id"
            )
        with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
            repo.accept_turn(
                _turn_intent("request-3"),
                proposed_conversation_id="semantic-conversation-id",
            )


def test_proposed_turn_collision_precedes_conversation_mutation(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        existing = repo.accept_turn(
            _turn_intent("request-1"),
            proposed_turn_id="a" * 32,
            proposed_conversation_id="b" * 32,
        )
        collision = repo.accept_turn(
            _turn_intent("request-2", new_conversation=True),
            proposed_turn_id=existing.turn_id,
            proposed_conversation_id="c" * 32,
        )

        assert collision.status is TurnAcceptanceStatus.CONFLICT
        assert collision.code == "proposed_turn_id_conflict"
        assert tuple(
            conversation.conversation_id for conversation in repo.list_conversations()
        ) == ("b" * 32,)


def test_accept_turn_fault_rolls_back_conversation_binding_and_receipt(tmp_path):
    armed = True

    def fail_before_commit(name: str) -> None:
        nonlocal armed
        if armed and name == "accept_turn.before_commit":
            armed = False
            raise RuntimeError("injected crash")

    with ControlStateRepository(tmp_path, fault_hook=fail_before_commit) as repo:
        intent = _turn_intent("faulted-request")
        with pytest.raises(RuntimeError, match="injected crash"):
            repo.accept_turn(intent)

        assert repo.list_conversations() == ()
        accepted = repo.accept_turn(intent)
        assert accepted.status is TurnAcceptanceStatus.ACCEPTED
        assert len(repo.list_conversations()) == 1


def test_concurrent_same_ingress_key_creates_one_turn_and_conversation(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        intent = _turn_intent("concurrent-request")
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(lambda _index: repo.accept_turn(intent), range(8))
            )

        assert (
            sum(result.status is TurnAcceptanceStatus.ACCEPTED for result in results)
            == 1
        )
        assert (
            sum(result.status is TurnAcceptanceStatus.DUPLICATE for result in results)
            == 7
        )
        assert len({result.turn_id for result in results}) == 1
        assert len(repo.list_conversations()) == 1


def test_process_kill_before_accept_commit_leaves_no_partial_authority(tmp_path):
    script = f"""
import os
from omicsclaw.control import ControlStateRepository, TurnAcceptanceIntent

def crash(name):
    if name == 'accept_turn.before_commit':
        os._exit(73)

repo = ControlStateRepository({str(tmp_path)!r}, fault_hook=crash)
repo.accept_turn(TurnAcceptanceIntent(
    surface='desktop',
    source_namespace='desktop/v1/test',
    source_request_id='process-kill',
    fingerprint_version=1,
    fingerprint_sha256={"a" * 64!r},
    reply_target={_reply_target()!r},
))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=False,
    )
    assert completed.returncode == 73

    with ControlStateRepository(tmp_path) as repo:
        accepted = repo.accept_turn(_turn_intent("process-kill"))
        assert accepted.status is TurnAcceptanceStatus.ACCEPTED
        assert len(repo.list_conversations()) == 1


def test_run_submission_assignment_and_report_are_fenced(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        project = repo.create_project("Run study")
        intent = RunAcceptanceIntent(
            run_submission_id="submission-1",
            fingerprint_version=1,
            fingerprint_sha256=_fingerprint("e"),
            run_kind="skill",
            scope_kind="project",
            project_id=project.project_id,
            manifest_ref="run-store://manifest/1",
        )

        accepted = repo.accept_run(intent)
        duplicate = repo.accept_run(intent)
        assert accepted.status is RunAcceptanceStatus.ACCEPTED
        assert duplicate.status is RunAcceptanceStatus.DUPLICATE
        assert duplicate.run_id == accepted.run_id
        inspection = repo.inspect_run_submission(
            run_submission_id=intent.run_submission_id,
            fingerprint_version=intent.fingerprint_version,
            fingerprint_sha256=intent.fingerprint_sha256,
        )
        assert inspection.state == "duplicate"
        assert inspection.canonical_id == accepted.run_id

        assignment = repo.assign_run(accepted.run_id, executor_kind="local")
        second = repo.assign_run(accepted.run_id, executor_kind="local")
        assert assignment.status is AssignmentStatus.ASSIGNED
        assert second.status is AssignmentStatus.ALREADY_ASSIGNED
        assert second.assignment_id == assignment.assignment_id

        rejected = repo.apply_run_report(
            RunReport(
                run_id=accepted.run_id,
                assignment_id="f" * 32,
                terminal_status="succeeded",
            )
        )
        assert rejected.changed is False
        assert rejected.code == "assignment_mismatch"
        incidents = repo.list_run_integrity_incidents(run_id=accepted.run_id)
        assert len(incidents.incidents) == 1
        assert incidents.incidents[0].assignment_id == "f" * 32
        assert (
            incidents.incidents[0].incident_type
            is RunIntegrityIncidentType.ASSIGNMENT_FENCE_VIOLATION
        )
        assert (
            incidents.incidents[0].evidence_code
            is RunIntegrityEvidenceCode.ASSIGNMENT_ID_MISMATCH
        )

        applied = repo.apply_run_report(
            RunReport(
                run_id=accepted.run_id,
                assignment_id=assignment.assignment_id,
                terminal_status="succeeded",
                projections=(
                    ProjectionIntentInput(
                        projection_kind="analysis_lineage",
                        source_store="run",
                        source_ref="run-store://completion/1",
                        content_sha256=_fingerprint("f"),
                    ),
                ),
            )
        )
        assert applied.changed is True
        assert repo.get_run(accepted.run_id).status == "succeeded"
        assert len(repo.list_projection_intents(project.project_id)) == 1
        assert (
            len(repo.list_run_integrity_incidents(run_id=accepted.run_id).incidents)
            == 1
        )


def test_report_without_canonical_assignment_records_fence_incident(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        accepted = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="missing-assignment-report",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("6"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/missing-assignment-report",
            )
        )
        rejected = repo.apply_run_report(
            RunReport(
                run_id=accepted.run_id,
                assignment_id="f" * 32,
                terminal_status="succeeded",
            )
        )

        assert rejected.changed is False
        assert rejected.code == "assignment_mismatch"
        assert repo.get_run(accepted.run_id).status == "queued"
        incident = repo.list_run_integrity_incidents(run_id=accepted.run_id).incidents[
            0
        ]
        assert incident.evidence_code is RunIntegrityEvidenceCode.ASSIGNMENT_MISSING


def test_run_acceptance_plan_is_read_only_and_accepts_its_proposed_id(tmp_path):
    intent = RunAcceptanceIntent(
        run_submission_id="planned-submission",
        fingerprint_version=1,
        fingerprint_sha256=_fingerprint("a"),
        run_kind="skill",
        scope_kind="unassigned",
        manifest_ref="run-store://manifest/planned",
    )
    proposed_run_id = "1" * 32

    with ControlStateRepository(tmp_path) as repo:
        plan = repo.plan_run_acceptance(
            intent,
            proposed_run_id=proposed_run_id,
        )

        assert plan.state == "novel"
        assert plan.run_id == proposed_run_id
        assert plan.proposed_run_id == proposed_run_id
        with pytest.raises(KeyError):
            repo.get_run(proposed_run_id)

        accepted = repo.accept_run(
            intent,
            proposed_run_id=plan.proposed_run_id,
        )
        assert accepted.status is RunAcceptanceStatus.ACCEPTED
        assert accepted.run_id == proposed_run_id

        duplicate = repo.plan_run_acceptance(
            intent,
            proposed_run_id="2" * 32,
        )
        assert duplicate.state == "duplicate"
        assert duplicate.run_id == proposed_run_id
        assert duplicate.proposed_run_id is None

        conflict = repo.plan_run_acceptance(
            RunAcceptanceIntent(
                run_submission_id=intent.run_submission_id,
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("b"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/different",
            )
        )
        assert conflict.state == "conflict"
        assert conflict.run_id == proposed_run_id
        assert conflict.code == "run_idempotency_conflict"


def test_proposed_run_collision_does_not_create_submission_binding(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        existing = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="existing-submission",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("c"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/existing",
            ),
            proposed_run_id="3" * 32,
        )
        collision_intent = RunAcceptanceIntent(
            run_submission_id="colliding-submission",
            fingerprint_version=1,
            fingerprint_sha256=_fingerprint("d"),
            run_kind="skill",
            scope_kind="unassigned",
            manifest_ref="run-store://manifest/collision",
        )

        collision = repo.accept_run(
            collision_intent,
            proposed_run_id=existing.run_id,
        )

        assert collision.status is RunAcceptanceStatus.CONFLICT
        assert collision.code == "proposed_run_id_conflict"
        assert (
            repo.inspect_run_submission(
                run_submission_id=collision_intent.run_submission_id,
                fingerprint_version=collision_intent.fingerprint_version,
                fingerprint_sha256=collision_intent.fingerprint_sha256,
            ).state
            == "novel"
        )


def test_queued_run_can_fail_submission_and_observation_is_atomic(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        failed = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="enqueue-failed",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("e"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/enqueue-failed",
            )
        )
        failed_result = repo.fail_queued_run(
            failed.run_id,
            terminal_code="submission_failed",
        )
        assert failed_result.changed is True
        failed_observation = repo.get_run_observation(failed.run_id)
        assert failed_observation.receipt.status == "failed"
        assert failed_observation.receipt.terminal_code == "submission_failed"
        assert failed_observation.assignment is None

        running = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="observed-run",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("f"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/observed-run",
            )
        )
        assignment = repo.assign_run(
            running.run_id,
            executor_kind="local-thread",
            execution_reference_type="task",
            execution_reference="task-1",
        )
        observation = repo.get_run_observation(running.run_id)
        assert observation.receipt.status == "running"
        assert observation.assignment is not None
        assert observation.assignment.assignment_id == assignment.assignment_id
        assert observation.assignment.executor_kind == "local-thread"
        assert observation.assignment.execution_reference_type == "task"
        assert observation.assignment.execution_reference == "task-1"

        repo.apply_run_report(
            RunReport(
                run_id=running.run_id,
                assignment_id=assignment.assignment_id,
                terminal_status="succeeded",
            )
        )
        rejected = repo.update_execution_reference(
            running.run_id,
            assignment.assignment_id,
            reference_type="task",
            reference="task-2",
        )
        assert rejected.changed is False
        assert rejected.code == "run_terminal"
        assert (
            repo.get_run_observation(running.run_id).assignment.execution_reference
            == "task-1"
        )


def test_run_startup_reconciliation_requires_confirmed_assignment_owners(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        run_ids = []
        for index in range(3):
            accepted = repo.accept_run(
                RunAcceptanceIntent(
                    run_submission_id=f"startup-{index}",
                    fingerprint_version=1,
                    fingerprint_sha256=_fingerprint(str(index + 4)),
                    run_kind="skill",
                    scope_kind="unassigned",
                    manifest_ref=f"run-store://manifest/startup-{index}",
                )
            )
            run_ids.append(accepted.run_id)
        running_assignment = repo.assign_run(run_ids[1], executor_kind="local")
        canceled_assignment = repo.assign_run(run_ids[2], executor_kind="local")
        assert running_assignment.status is AssignmentStatus.ASSIGNED
        assert canceled_assignment.status is AssignmentStatus.ASSIGNED
        assert repo.request_run_cancel(run_ids[2]).code == "cancel_requested"

        assert tuple(run.run_id for run in repo.list_nonterminal_runs()) == tuple(
            run_ids
        )
        reconciled = repo.reconcile_nonterminal_runs()

        assert reconciled.interrupted_run_ids == (run_ids[0],)
        assert reconciled.unconfirmed_run_ids == tuple(run_ids[1:])
        assert repo.apply_run_report(
            RunReport(
                run_id=run_ids[1],
                assignment_id=running_assignment.assignment_id,
                terminal_status="interrupted",
                terminal_code="control_plane_restarted",
            )
        ).changed
        assert repo.apply_run_report(
            RunReport(
                run_id=run_ids[2],
                assignment_id=canceled_assignment.assignment_id,
                terminal_status="interrupted",
                terminal_code="control_plane_restarted",
            )
        ).changed
        reconciled = repo.reconcile_nonterminal_runs()
        assert reconciled.interrupted_run_ids == ()
        assert reconciled.unconfirmed_run_ids == ()
        for run_id in run_ids:
            receipt = repo.get_run(run_id)
            assert receipt.status == "interrupted"
            assert receipt.terminal_code == "control_plane_restarted"
            assert receipt.finished_at_ms is not None
        assert repo.list_nonterminal_runs() == ()
        assert repo.reconcile_nonterminal_runs().interrupted_run_ids == ()


def test_run_observation_pages_are_bounded_newest_first_and_pure(tmp_path):
    repo = ControlStateRepository(tmp_path, clock_ms=lambda: 1_700_000_000_000)
    try:
        run_ids = tuple(character * 32 for character in ("1", "2", "3"))
        for index, run_id in enumerate(run_ids, start=1):
            accepted = repo.accept_run(
                RunAcceptanceIntent(
                    run_submission_id=f"{index:x}" * 32,
                    fingerprint_version=1,
                    fingerprint_sha256=f"{index:x}" * 64,
                    run_kind="skill",
                    scope_kind="unassigned",
                    project_id=None,
                    parent_turn_id=None,
                    retry_of_run_id=None,
                    manifest_ref=f"run-store:v1:{index:x}".ljust(45, "0"),
                ),
                proposed_run_id=run_id,
            )
            assert accepted.status is RunAcceptanceStatus.ACCEPTED

        assignment = repo.assign_run(run_ids[1], executor_kind="test-executor")
        assert assignment.status is AssignmentStatus.ASSIGNED
        assert repo.fail_queued_run(run_ids[2]).changed is True

        # The Remote canonical list cannot be crowded out by other Run kinds
        # or Project-scoped work.
        project = repo.create_project("Excluded project")
        excluded = (
            ("4" * 32, "workflow", "unassigned", None),
            ("5" * 32, "skill", "project", project.project_id),
        )
        for run_id, run_kind, scope_kind, project_id in excluded:
            accepted = repo.accept_run(
                RunAcceptanceIntent(
                    run_submission_id=run_id,
                    fingerprint_version=1,
                    fingerprint_sha256=run_id[0] * 64,
                    run_kind=run_kind,
                    scope_kind=scope_kind,
                    project_id=project_id,
                    parent_turn_id=None,
                    retry_of_run_id=None,
                    manifest_ref=f"run-store:v1:{run_id}",
                ),
                proposed_run_id=run_id,
            )
            assert accepted.status is RunAcceptanceStatus.ACCEPTED

        first = repo.list_run_observations(limit=2)
        assert tuple(item.receipt.run_id for item in first.observations) == (
            run_ids[2],
            run_ids[1],
        )
        assert first.next_cursor == run_ids[1]
        second = repo.list_run_observations(cursor=first.next_cursor, limit=2)
        assert tuple(item.receipt.run_id for item in second.observations) == (
            run_ids[0],
        )
        assert second.next_cursor is None

        running = repo.list_run_observations(status="running", limit=1)
        assert len(running.observations) == 1
        assert running.observations[0].receipt.run_id == run_ids[1]
        assert running.observations[0].assignment is not None
        assert running.observations[0].assignment.assignment_id == assignment.assignment_id
        with pytest.raises(ValueError, match="invalid Run observation cursor"):
            repo.list_run_observations(
                status="running",
                cursor=run_ids[2],
            )
        with pytest.raises(ValueError, match="between 1 and 100"):
            repo.list_run_observations(limit=101)

        # Observation cannot mutate lifecycle, Assignment, or revision.
        assert repo.get_run(run_ids[0]).revision == 1
        assert repo.get_run(run_ids[1]).revision == 2
        assert repo.get_run(run_ids[2]).revision == 2
    finally:
        repo.close()


def test_run_startup_reconciliation_rolls_back_as_one_transaction(tmp_path):
    armed = True

    def fault(name: str) -> None:
        if armed and name == "reconcile_nonterminal_runs.before_commit":
            raise RuntimeError("injected")

    with ControlStateRepository(tmp_path, fault_hook=fault) as repo:
        accepted = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="startup-fault",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("9"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/startup-fault",
            )
        )
        with pytest.raises(RuntimeError, match="injected"):
            repo.reconcile_nonterminal_runs()
        armed = False

        assert repo.get_run(accepted.run_id).status == "queued"
        assert repo.reconcile_nonterminal_runs().interrupted_run_ids == (
            accepted.run_id,
        )


def test_project_archive_treats_cancel_requested_run_as_busy(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        project = repo.create_project("Cancel study")
        run = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="cancel-submission",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("7"),
                run_kind="skill",
                scope_kind="project",
                project_id=project.project_id,
                manifest_ref="run-store://manifest/cancel",
            )
        )
        assignment = repo.assign_run(run.run_id, executor_kind="local")
        canceled = repo.request_run_cancel(run.run_id)
        assert canceled.code == "cancel_requested"
        assert (
            repo.archive_project(project.project_id).status
            is ProjectLifecycleStatus.BUSY
        )

        report = repo.apply_run_report(
            RunReport(
                run_id=run.run_id,
                assignment_id=assignment.assignment_id,
                terminal_status="canceled",
            )
        )
        assert report.changed is True
        assert (
            repo.archive_project(project.project_id).status
            is ProjectLifecycleStatus.CHANGED
        )


def test_delivery_sequence_barrier_and_failure_suppression(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        first_turn = repo.accept_turn(_turn_intent("channel-1", surface="channel"))
        second_turn = repo.accept_turn(
            _turn_intent(
                "channel-2",
                surface="channel",
                new_conversation=True,
            )
        )
        for turn_id in (first_turn.turn_id, second_turn.turn_id):
            repo.start_turn(turn_id)

        first_ref = _transcript_ref("first")
        first_terminal = repo.terminalize_turn(
            first_turn.turn_id,
            terminal_status="succeeded",
            transcript_ref=first_ref,
            delivery_plan=_delivery_plan(first_ref.entry_id),
        )
        second_ref = _transcript_ref("second")
        second_terminal = repo.terminalize_turn(
            second_turn.turn_id,
            terminal_status="succeeded",
            transcript_ref=second_ref,
            delivery_plan=_delivery_plan(second_ref.entry_id),
        )
        assert first_terminal.delivery.target_sequence == 1
        assert second_terminal.delivery.target_sequence == 2

        first_items = repo.list_delivery_items(first_terminal.delivery.delivery_id)
        second_items = repo.list_delivery_items(second_terminal.delivery.delivery_id)

        blocked = repo.begin_delivery_attempt(second_items[0].item_id)
        assert blocked.started is False
        assert blocked.code == "earlier_target_sequence"

        attempt = repo.begin_delivery_attempt(first_items[0].item_id)
        assert attempt.started is True
        repo.finish_delivery_attempt(
            attempt.attempt_id,
            DeliveryAttemptOutcome.REJECTED_PERMANENT,
            error_code="provider_rejected",
        )

        refreshed = repo.list_delivery_items(first_terminal.delivery.delivery_id)
        assert [item.state for item in refreshed] == ["failed", "suppressed"]
        assert refreshed[1].blocked_by_item_id == refreshed[0].item_id

        unblocked = repo.begin_delivery_attempt(second_items[0].item_id)
        assert unblocked.started is True


def test_begin_attempt_rechecks_the_ordinal_barrier_at_claim_time(tmp_path):
    """A higher Item cannot start until every lower ordinal is ``delivered``.

    The Pump's due-selection already screens candidates, but ADR 0063 requires
    the ordinal barrier to be re-checked inside the claim transaction so a
    stale candidate cannot expose a later chunk before its predecessor. This
    asserts the ``earlier_item_not_delivered`` recheck code directly rather
    than only transitively through the Pump.
    """

    with ControlStateRepository(tmp_path) as repo:
        turn = repo.accept_turn(_turn_intent("channel-ordinal", surface="channel"))
        repo.start_turn(turn.turn_id)
        ref = _transcript_ref("ordinal")
        terminal = repo.terminalize_turn(
            turn.turn_id,
            terminal_status="succeeded",
            transcript_ref=ref,
            delivery_plan=_delivery_plan(ref.entry_id),
        )
        items = repo.list_delivery_items(terminal.delivery.delivery_id)
        assert len(items) == 2

        # The lower Item is still ``queued``, so its higher sibling is refused.
        blocked = repo.begin_delivery_attempt(items[1].item_id)
        assert blocked.started is False
        assert blocked.code == "earlier_item_not_delivered"

        # The head Item itself is claimable; the barrier only gates the suffix.
        head = repo.begin_delivery_attempt(items[0].item_id)
        assert head.started is True


def test_concurrent_terminalization_allocates_unique_contiguous_target_sequences(
    tmp_path,
):
    with ControlStateRepository(tmp_path) as repo:
        turns = [
            repo.accept_turn(
                _turn_intent(
                    f"channel-concurrent-{index}",
                    surface="channel",
                    new_conversation=True,
                )
            )
            for index in range(2)
        ]
        for accepted in turns:
            repo.start_turn(accepted.turn_id)
        refs = [_transcript_ref(f"concurrent-{index}") for index in range(2)]
        start_together = threading.Barrier(2)

        def terminalize(index: int):
            start_together.wait(timeout=2)
            return repo.terminalize_turn(
                turns[index].turn_id,
                terminal_status="succeeded",
                transcript_ref=refs[index],
                delivery_plan=_delivery_plan(refs[index].entry_id),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(terminalize, range(2)))

        deliveries = [result.delivery for result in results]
        assert all(delivery is not None for delivery in deliveries)
        assert sorted(delivery.target_sequence for delivery in deliveries) == [1, 2]
        assert len({delivery.delivery_id for delivery in deliveries}) == 2
        assert [delivery.target_sequence for delivery in repo.list_deliveries()] == [
            1,
            2,
        ]


def test_channel_delivery_requires_the_exact_terminal_transcript_reference(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        without_ref = repo.accept_turn(
            _turn_intent("channel-without-ref", surface="channel")
        )
        repo.start_turn(without_ref.turn_id)
        transcript_ref = _transcript_ref("required")

        with pytest.raises(ValueError, match="requires a Transcript reference"):
            repo.terminalize_turn(
                without_ref.turn_id,
                terminal_status="succeeded",
                delivery_plan=_delivery_plan(transcript_ref.entry_id),
            )
        assert repo.get_turn(without_ref.turn_id).status == "running"

        with pytest.raises(ValueError, match="must reference the Turn terminal"):
            repo.terminalize_turn(
                without_ref.turn_id,
                terminal_status="succeeded",
                transcript_ref=transcript_ref,
                delivery_plan=_delivery_plan("d" * 32),
            )
        assert repo.get_turn(without_ref.turn_id).status == "running"
        assert repo.get_turn_terminal_ref(without_ref.turn_id) is None
        assert repo.list_deliveries(turn_id=without_ref.turn_id) == ()


def test_terminalization_fault_rolls_back_receipt_delivery_and_projection(tmp_path):
    armed = True

    def fail_before_commit(name: str) -> None:
        nonlocal armed
        if armed and name == "terminalize_turn.before_commit":
            armed = False
            raise RuntimeError("terminal crash")

    with ControlStateRepository(tmp_path, fault_hook=fail_before_commit) as repo:
        project = repo.create_project("Atomic terminal")
        turn = repo.accept_turn(
            _turn_intent(
                "terminal-fault",
                surface="channel",
                project_id=project.project_id,
            )
        )
        repo.start_turn(turn.turn_id)
        transcript_ref = _transcript_ref("candidate")
        projection = ProjectionIntentInput(
            projection_kind="insight",
            source_store="transcript",
            source_ref=transcript_ref.entry_id,
            content_sha256=_fingerprint("9"),
        )

        with pytest.raises(RuntimeError, match="terminal crash"):
            repo.terminalize_turn(
                turn.turn_id,
                terminal_status="succeeded",
                transcript_ref=transcript_ref,
                delivery_plan=_delivery_plan(transcript_ref.entry_id),
                projections=(projection,),
            )

        assert repo.get_turn(turn.turn_id).status == "running"
        assert repo.list_deliveries(turn_id=turn.turn_id) == ()
        assert repo.list_projection_intents(project.project_id) == ()

        terminal = repo.terminalize_turn(
            turn.turn_id,
            terminal_status="succeeded",
            transcript_ref=transcript_ref,
            delivery_plan=_delivery_plan(transcript_ref.entry_id),
            projections=(projection,),
        )
        assert terminal.changed is True
        assert len(repo.list_projection_intents(project.project_id)) == 1


def test_state_survives_close_and_reopen_without_replaying_work(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        accepted = repo.accept_turn(_turn_intent("restart-request"))
        database_path = repo.database_path

    with ControlStateRepository(tmp_path) as reopened:
        receipt = reopened.get_turn(accepted.turn_id)
        duplicate = reopened.accept_turn(_turn_intent("restart-request"))
        assert reopened.database_path == database_path
        assert receipt.status == "queued"
        assert duplicate.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.turn_id == accepted.turn_id


def test_terminal_codes_are_normalized_at_the_repository_boundary(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        accepted = repo.accept_turn(_turn_intent("terminal-code-validation"))
        repo.start_turn(accepted.turn_id)

        with pytest.raises(ValueError, match="must not have terminal_code"):
            repo.terminalize_turn(
                accepted.turn_id,
                terminal_status="succeeded",
                terminal_code="unexpected_code",
            )
        for unsafe_code in (
            "Provider secret: sk-sensitive",
            "sk_sensitivecredential123",
            "canceled_before_start",
            "x" * 65,
            "",
            42,
        ):
            with pytest.raises(ValueError, match="closed non-secret"):
                repo.terminalize_turn(
                    accepted.turn_id,
                    terminal_status="failed",
                    terminal_code=unsafe_code,  # type: ignore[arg-type]
                )

        terminalized = repo.terminalize_turn(
            accepted.turn_id,
            terminal_status="failed",
            terminal_code="worker_failed",
        )
        assert terminalized.changed is True
        assert repo.get_turn(accepted.turn_id).terminal_code == "worker_failed"


def test_run_report_rejects_a_regex_valid_non_allowlisted_terminal_code(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        accepted = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="unsafe-terminal-code",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("1"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/unsafe-terminal-code",
            )
        )
        assignment = repo.assign_run(accepted.run_id, executor_kind="local")

        for unsafe_code in (
            "sk_sensitivecredential123",
            "canceled_before_assignment",
        ):
            with pytest.raises(ValueError, match="closed non-secret"):
                repo.apply_run_report(
                    RunReport(
                        run_id=accepted.run_id,
                        assignment_id=assignment.assignment_id,
                        terminal_status="failed",
                        terminal_code=unsafe_code,  # type: ignore[arg-type]
                    )
                )

        assert repo.get_run(accepted.run_id).status == "running"


def test_run_report_accepts_only_exact_terminal_replay_for_one_assignment(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        accepted = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="terminal-replay",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("8"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/terminal-replay",
            )
        )
        assignment = repo.assign_run(accepted.run_id, executor_kind="local")
        report = RunReport(
            run_id=accepted.run_id,
            assignment_id=assignment.assignment_id,
            terminal_status="failed",
            terminal_code="executor_failed",
        )
        assert repo.apply_run_report(report).changed is True
        replay = repo.apply_run_report(report)
        assert replay.changed is False
        assert replay.code == "already_terminal"
        assert repo.list_run_integrity_incidents(run_id=accepted.run_id).incidents == ()

        with pytest.raises(
            RunIntegrityIncidentError, match="conflicting terminal"
        ) as raised:
            repo.apply_run_report(
                RunReport(
                    run_id=accepted.run_id,
                    assignment_id=assignment.assignment_id,
                    terminal_status="interrupted",
                    terminal_code="execution_interrupted",
                )
            )
        first_page = repo.list_run_integrity_incidents(run_id=accepted.run_id)
        assert len(first_page.incidents) == 1
        incident = first_page.incidents[0]
        assert raised.value.incident_id == incident.incident_id
        assert (
            incident.incident_type is RunIntegrityIncidentType.TERMINAL_REPORT_CONFLICT
        )
        assert (
            incident.evidence_code is RunIntegrityEvidenceCode.TERMINAL_STATE_CONFLICT
        )
        assert incident.receipt_revision == repo.get_run(accepted.run_id).revision

        with pytest.raises(RunIntegrityIncidentError) as repeated:
            repo.apply_run_report(
                RunReport(
                    run_id=accepted.run_id,
                    assignment_id=assignment.assignment_id,
                    terminal_status="interrupted",
                    terminal_code="execution_interrupted",
                )
            )
        assert repeated.value.incident_id == incident.incident_id
        assert len(repo.list_run_integrity_incidents().incidents) == 1


def test_terminal_conflict_incident_commit_is_atomic_and_concurrently_deduplicated(
    tmp_path,
):
    armed = False

    def fault(name: str) -> None:
        nonlocal armed
        if armed and name == "apply_run_report.before_commit":
            armed = False
            raise RuntimeError("injected incident commit rollback")

    with ControlStateRepository(tmp_path, fault_hook=fault) as repo:
        accepted = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="terminal-conflict-atomic",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("7"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/terminal-conflict-atomic",
            )
        )
        assignment = repo.assign_run(accepted.run_id, executor_kind="local")
        repo.apply_run_report(
            RunReport(
                run_id=accepted.run_id,
                assignment_id=assignment.assignment_id,
                terminal_status="succeeded",
            )
        )
        conflicting = RunReport(
            run_id=accepted.run_id,
            assignment_id=assignment.assignment_id,
            terminal_status="failed",
            terminal_code="executor_failed",
        )

        armed = True
        with pytest.raises(RuntimeError, match="rollback"):
            repo.apply_run_report(conflicting)
        assert repo.list_run_integrity_incidents().incidents == ()
        assert repo.get_run(accepted.run_id).status == "succeeded"

        barrier = threading.Barrier(8)

        def reject_conflict() -> str:
            barrier.wait()
            with pytest.raises(RunIntegrityIncidentError) as raised:
                repo.apply_run_report(conflicting)
            return raised.value.incident_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            incident_ids = set(pool.map(lambda _index: reject_conflict(), range(8)))
        assert len(incident_ids) == 1
        assert len(repo.list_run_integrity_incidents().incidents) == 1
        assert repo.get_run(accepted.run_id).status == "succeeded"


def test_run_integrity_incident_append_is_idempotent_paged_and_restart_durable(
    tmp_path,
):
    with ControlStateRepository(tmp_path) as repo:
        accepted = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="runtime-incidents",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("9"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/runtime-incidents",
            )
        )
        assignment = repo.assign_run(accepted.run_id, executor_kind="local")
        owner_intent = RunIntegrityIncidentIntent(
            run_id=accepted.run_id,
            assignment_id=assignment.assignment_id,
            incident_type=RunIntegrityIncidentType.EXECUTION_OWNER_UNCONFIRMED,
            evidence_code=RunIntegrityEvidenceCode.EXECUTION_OWNER_STOP_UNCONFIRMED,
        )
        first = repo.record_run_integrity_incident(owner_intent)
        duplicate = repo.record_run_integrity_incident(owner_intent)
        second = repo.record_run_integrity_incident(
            RunIntegrityIncidentIntent(
                run_id=accepted.run_id,
                assignment_id=assignment.assignment_id,
                incident_type=(
                    RunIntegrityIncidentType.RECOVERY_TERMINAL_COMMIT_FAILED
                ),
                evidence_code=(
                    RunIntegrityEvidenceCode.RECOVERY_TERMINAL_TRANSACTION_FAILED
                ),
            )
        )

        assert first.created is True
        assert duplicate.created is False
        assert duplicate.incident.incident_id == first.incident.incident_id
        assert second.created is True
        page_one = repo.list_run_integrity_incidents(run_id=accepted.run_id, limit=1)
        assert page_one.incidents == (second.incident,)
        assert page_one.next_cursor == second.incident.incident_id
        page_two = repo.list_run_integrity_incidents(
            run_id=accepted.run_id,
            cursor=page_one.next_cursor,
            limit=1,
        )
        assert page_two.incidents == (first.incident,)
        assert page_two.next_cursor is None
        with pytest.raises(ValueError, match="invalid incident cursor"):
            repo.list_run_integrity_incidents(
                run_id="f" * 32,
                cursor=first.incident.incident_id,
            )
        with pytest.raises(ValueError, match="between 1 and 100"):
            repo.list_run_integrity_incidents(limit=101)

    with ControlStateRepository(tmp_path) as reopened:
        observed = reopened.list_run_integrity_incidents(run_id=accepted.run_id)
        assert observed.incidents == (second.incident, first.incident)
        assert observed.next_cursor is None


def test_wrong_assignment_reference_update_records_without_mutating_owner(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        accepted = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="reference-fence-incident",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("0"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/reference-fence-incident",
            )
        )
        assignment = repo.assign_run(
            accepted.run_id,
            executor_kind="legacy-local",
            execution_reference_type="task",
            execution_reference="task-1",
        )
        rejected = repo.update_execution_reference(
            accepted.run_id,
            "f" * 32,
            reference_type="task",
            reference="task-2",
        )

        assert rejected.changed is False
        assert rejected.code == "assignment_mismatch"
        observation = repo.get_run_observation(accepted.run_id)
        assert observation.assignment is not None
        assert observation.assignment.assignment_id == assignment.assignment_id
        assert observation.assignment.execution_reference == "task-1"
        incident = repo.list_run_integrity_incidents(run_id=accepted.run_id).incidents[
            0
        ]
        assert (
            incident.incident_type
            is RunIntegrityIncidentType.ASSIGNMENT_FENCE_VIOLATION
        )
        assert incident.evidence_code is RunIntegrityEvidenceCode.ASSIGNMENT_ID_MISMATCH


@pytest.mark.parametrize(
    ("terminal_status", "terminal_code", "message"),
    (
        ("running", None, "invalid terminal Run status"),
        ("succeeded", "executor_failed", "succeeded Run"),
        ("failed", "canceled", "closed non-secret"),
    ),
)
def test_run_report_rejects_invalid_terminal_contract_at_construction(
    terminal_status,
    terminal_code,
    message,
):
    with pytest.raises(ValueError, match=message):
        RunReport(
            run_id="a" * 32,
            assignment_id="b" * 32,
            terminal_status=terminal_status,
            terminal_code=terminal_code,
        )


def test_database_triggers_reject_non_allowlisted_terminal_codes(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        turn = repo.accept_turn(_turn_intent("database-terminal-code"))
        run = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="database-terminal-code",
                fingerprint_version=1,
                fingerprint_sha256=_fingerprint("2"),
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store://manifest/database-terminal-code",
            )
        )
        database_path = repo.database_path

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="invalid turn terminal code"):
            connection.execute(
                "UPDATE turns SET status = 'failed', terminal_code = ? WHERE turn_id = ?",
                ("sk_sensitivecredential123", turn.turn_id),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="invalid run terminal code"):
            connection.execute(
                "UPDATE runs SET status = 'failed', terminal_code = ? WHERE run_id = ?",
                ("sk_sensitivecredential123", run.run_id),
            )


def test_defensive_triggers_reject_identity_mutation_and_terminal_reopen(tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        accepted = repo.accept_turn(_turn_intent("trigger-guard"))
        repo.start_turn(accepted.turn_id)
        repo.terminalize_turn(accepted.turn_id, terminal_status="succeeded")
        database_path = repo.database_path

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="terminal turn"):
            connection.execute(
                "UPDATE turns SET status = 'queued' WHERE turn_id = ?",
                (accepted.turn_id,),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="immutable conversation"):
            connection.execute(
                """
                UPDATE conversations SET reply_target_json = '{}'
                WHERE conversation_id = ?
                """,
                (accepted.conversation_id,),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="insert-only"):
            connection.execute(
                """
                UPDATE ingress_bindings SET fingerprint_sha256 = ?
                WHERE turn_id = ?
                """,
                (_fingerprint("8"), accepted.turn_id),
            )


def test_legacy_cutover_requires_explicit_validation_transition(tmp_path):
    import_run_id = "a" * 32
    with ControlStateRepository(tmp_path) as repo:
        repo.begin_legacy_import(
            import_run_id,
            source_manifest_sha256="b" * 64,
            report_ref="transcript-import://test",
        )

        with pytest.raises(ControlIntegrityError, match="not cutover-ready"):
            repo.commit_legacy_import_cutover(import_run_id)

        with pytest.raises(ControlIntegrityError, match="no immutable Transcript"):
            repo.mark_legacy_import_validated(import_run_id)

        repo.bind_transcript_store("c" * 32, import_run_id=import_run_id)
        repo.record_legacy_transcript_cutover(
            import_run_id,
            cutover_manifest_sha256="b" * 64,
            transcript_store_id="c" * 32,
            import_baseline_sha256="d" * 64,
            source_identity="/legacy/transcripts.db",
        )
        assert repo.mark_legacy_import_validated(import_run_id).code == "validated"
        with pytest.raises(ControlIntegrityError, match="not mappable"):
            repo.import_legacy_conversations(import_run_id, ())
        assert repo.commit_legacy_import_cutover(import_run_id).code == "committed"
