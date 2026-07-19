from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from omicsclaw.control.errors import ControlIntegrityError
from omicsclaw.control.repository import ControlStateRepository
from omicsclaw.control.runtime import ControlRuntime
from omicsclaw.runtime.storage.canonical_transcript import (
    CanonicalTranscript,
    TranscriptImportConflict,
    TranscriptIntegrityError,
    plan_legacy_import,
)
from omicsclaw.runtime.storage.transcript_import import (
    execute_transcript_migration,
)


def test_terminal_candidate_is_invisible_until_verified_promotion(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    try:
        turn = transcript.bind_turn("conversation-1", "turn-1")
        turn.append_user_message("conversation-1", "hello")
        turn.defer_terminal_message(
            "conversation-1",
            content="world",
            reasoning_content="private reasoning",
        )

        candidate = turn.stage_terminal("world")

        assert transcript.get_history("conversation-1") == [
            {"role": "user", "content": "hello"}
        ]
        assert transcript.get_entry(candidate.entry_id).commit_state == (
            "terminal_candidate"
        )

        transcript.promote_terminal(candidate.entry_id, candidate.content_sha256)

        assert transcript.get_history("conversation-1") == [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "world",
                "reasoning_content": "private reasoning",
            },
        ]
        assert transcript.get_entry(candidate.entry_id).commit_state == "committed"
    finally:
        transcript.close()


def test_terminal_candidate_digest_is_verified_and_promotion_is_idempotent(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    try:
        turn = transcript.bind_turn("conversation-1", "turn-1")
        candidate = turn.stage_terminal("done")

        with pytest.raises(TranscriptIntegrityError, match="digest"):
            transcript.promote_terminal(candidate.entry_id, "0" * 64)

        first = transcript.promote_terminal(
            candidate.entry_id, candidate.content_sha256
        )
        second = transcript.promote_terminal(
            candidate.entry_id, candidate.content_sha256
        )

        assert first == second
        assert transcript.get_history("conversation-1") == [
            {"role": "assistant", "content": "done"}
        ]
    finally:
        transcript.close()


def test_restart_rehydrates_only_active_committed_entries(tmp_path):
    first = CanonicalTranscript(tmp_path)
    turn = first.bind_turn("conversation-1", "turn-1")
    turn.append_user_message("conversation-1", [{"type": "text", "text": "hi"}])
    abandoned = turn.stage_terminal("not committed")
    turn.abandon_terminal(abandoned.entry_id, abandoned.content_sha256)
    first.close()

    second = CanonicalTranscript(tmp_path)
    try:
        assert second.get_history("conversation-1") == [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi"}],
            }
        ]
        assert second.get_entry(abandoned.entry_id).commit_state == "abandoned"
    finally:
        second.close()


def test_replace_active_history_preserves_old_entries_as_immutable_audit_rows(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    try:
        turn = transcript.bind_turn("conversation-1", "turn-1")
        turn.append_user_message("conversation-1", "old")
        old_entry_ids = transcript.list_entry_ids("conversation-1")

        turn.replace_history(
            "conversation-1",
            [{"role": "system", "content": "summary"}],
        )

        assert transcript.get_history("conversation-1") == [
            {"role": "system", "content": "summary"}
        ]
        assert set(old_entry_ids).issubset(
            set(transcript.list_entry_ids("conversation-1"))
        )
    finally:
        transcript.close()


def _make_legacy_transcript(path, *, chat_id: str = "legacy-chat") -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE transcript_chats (
            chat_key TEXT PRIMARY KEY,
            chat_id_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            last_seq INTEGER NOT NULL DEFAULT -1
        );
        CREATE TABLE transcript_messages (
            chat_key TEXT NOT NULL,
            seq INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (chat_key, seq)
        );
        """
    )
    connection.execute(
        "INSERT INTO transcript_chats VALUES (?, ?, ?, ?)",
        ("s:legacy-chat", json.dumps(chat_id), 1, 1),
    )
    connection.executemany(
        "INSERT INTO transcript_messages VALUES (?, ?, ?)",
        (
            (
                "s:legacy-chat",
                0,
                json.dumps(
                    {"role": "user", "content": "question"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
            (
                "s:legacy-chat",
                1,
                json.dumps(
                    {"role": "assistant", "content": "answer"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ),
    )
    connection.commit()
    connection.close()


def _migration_profile(*, active: bool = True, slot: str = "history") -> dict:
    return {
        "schema_version": 1,
        "streams": [
            {
                "legacy_key": "s:legacy-chat",
                "surface": "cli",
                "reply_target": {
                    "schema_version": 1,
                    "kind": "cli",
                    "installation_id": "local",
                    "profile_id": "owner",
                    "slot": slot,
                },
                "active": active,
            }
        ],
    }


def test_existing_zero_byte_or_legacy_database_cannot_be_initialized_in_place(
    tmp_path,
):
    zero_root = tmp_path / "zero"
    zero_root.mkdir()
    (zero_root / "transcripts.db").touch()
    with pytest.raises(TranscriptIntegrityError, match="migration marker"):
        CanonicalTranscript(zero_root)

    legacy_root = tmp_path / "legacy-root"
    legacy_root.mkdir()
    legacy_path = legacy_root / "transcripts.db"
    _make_legacy_transcript(legacy_path)
    legacy_bytes = legacy_path.read_bytes()
    with pytest.raises(TranscriptIntegrityError, match="migration marker"):
        CanonicalTranscript(legacy_root)
    assert legacy_path.read_bytes() == legacy_bytes
    with sqlite3.connect(legacy_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "transcript_chats" in tables
    assert "transcript_schema_migrations" not in tables


def test_offline_migration_plan_apply_verify_and_control_cutover(tmp_path):
    legacy_path = tmp_path / "legacy.db"
    _make_legacy_transcript(legacy_path)
    state_root = tmp_path / "canonical-state"
    profile = {
        "schema_version": 1,
        "streams": [
            {
                "legacy_key": "s:legacy-chat",
                "surface": "cli",
                "reply_target": {
                    "schema_version": 1,
                    "kind": "cli",
                    "installation_id": "local",
                    "profile_id": "owner",
                    "slot": "imported-history",
                },
                "active": True,
            }
        ],
    }

    plan = execute_transcript_migration(
        "plan",
        source=legacy_path,
        state_root=state_root,
        profile=profile,
    )
    assert plan["state"] == "planned"
    assert not state_root.exists()

    with pytest.raises(ValueError, match="manifest drift"):
        execute_transcript_migration(
            "apply",
            source=legacy_path,
            state_root=state_root,
            profile=profile,
            expected_manifest_sha256="0" * 64,
        )
    assert not state_root.exists()

    applied = execute_transcript_migration(
        "apply",
        source=legacy_path,
        state_root=state_root,
        expected_manifest_sha256=plan["cutover_manifest_sha256"],
        profile=profile,
    )
    verified = execute_transcript_migration(
        "verify",
        source=legacy_path,
        state_root=state_root,
        profile=profile,
    )

    assert applied["cutover_state"] == "committed"
    assert verified == applied
    assert (state_root / "control.db").is_file()
    assert (state_root / "transcripts.db").is_file()
    canonical_id = applied["mappings"][0]["conversation_id"]
    assert canonical_id != "legacy-chat"
    with sqlite3.connect(state_root / "control.db") as connection:
        assert connection.execute(
            "SELECT conversation_id FROM conversations"
        ).fetchone()[0] == canonical_id
        assert connection.execute(
            "SELECT canonical_id FROM legacy_identity_map "
            "WHERE legacy_key = 's:legacy-chat'"
        ).fetchone()[0] == canonical_id
        bound_store_id = connection.execute(
            "SELECT transcript_store_id FROM transcript_store_bindings"
        ).fetchone()[0]
    with sqlite3.connect(state_root / "transcripts.db") as connection:
        assert connection.execute(
            "SELECT store_id FROM transcript_store_identity"
        ).fetchone()[0] == bound_store_id


def test_migration_plan_requires_explicit_mapping_without_writing_state(tmp_path):
    legacy_path = tmp_path / "legacy.db"
    _make_legacy_transcript(legacy_path)
    state_root = tmp_path / "state"

    plan = execute_transcript_migration(
        "plan",
        source=legacy_path,
        state_root=state_root,
    )

    assert plan["conflicts"] == ["mapping_profile_required:s:legacy-chat"]
    assert not state_root.exists()
    with pytest.raises(TranscriptImportConflict, match="unresolved conflicts"):
        execute_transcript_migration(
            "apply",
            source=legacy_path,
            state_root=state_root,
            expected_manifest_sha256=plan["cutover_manifest_sha256"],
        )
    assert not state_root.exists()


def test_cutover_manifest_binds_legacy_identity_not_only_messages(tmp_path):
    legacy_path = tmp_path / "legacy.db"
    _make_legacy_transcript(legacy_path)
    profile = {
        "schema_version": 1,
        "streams": [
            {
                "legacy_key": "s:legacy-chat",
                "surface": "cli",
                "reply_target": {
                    "schema_version": 1,
                    "kind": "cli",
                    "installation_id": "local",
                    "profile_id": "owner",
                    "slot": "history",
                },
                "active": False,
            }
        ],
    }
    before = execute_transcript_migration(
        "plan",
        source=legacy_path,
        profile=profile,
    )
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            "UPDATE transcript_chats SET chat_id_json = ?",
            (json.dumps("changed-legacy-id"),),
        )
        connection.commit()
    after = execute_transcript_migration(
        "plan",
        source=legacy_path,
        profile=profile,
    )

    assert before["source_manifest_sha256"] != after["source_manifest_sha256"]
    assert before["cutover_manifest_sha256"] != after["cutover_manifest_sha256"]
    with pytest.raises(ValueError, match="manifest drift"):
        execute_transcript_migration(
            "apply",
            source=legacy_path,
            state_root=tmp_path / "state",
            profile=profile,
            expected_manifest_sha256=before["cutover_manifest_sha256"],
        )


def test_committed_import_verifies_baseline_without_rewriting_live_state(tmp_path):
    legacy_path = tmp_path / "legacy.db"
    _make_legacy_transcript(legacy_path)
    state_root = tmp_path / "state"
    profile = {
        "schema_version": 1,
        "streams": [
            {
                "legacy_key": "s:legacy-chat",
                "surface": "desktop",
                "reply_target": {
                    "schema_version": 1,
                    "kind": "desktop",
                    "installation_id": "local",
                    "profile_id": "owner",
                    "slot": "history",
                },
                "active": True,
            }
        ],
    }
    plan = execute_transcript_migration(
        "plan",
        source=legacy_path,
        profile=profile,
    )
    execute_transcript_migration(
        "apply",
        source=legacy_path,
        state_root=state_root,
        profile=profile,
        expected_manifest_sha256=plan["cutover_manifest_sha256"],
    )
    imported_id = plan["mappings"][0]["conversation_id"]
    transcript = CanonicalTranscript(state_root, require_existing=True)
    try:
        turn = transcript.bind_turn(imported_id, "post-cutover-turn")
        turn.append_user_message(imported_id, "post cutover")
        turn.replace_history(
            imported_id,
            [{"role": "system", "content": "compacted live view"}],
        )
    finally:
        transcript.close()

    replacement_id = "f" * 32
    with sqlite3.connect(state_root / "control.db") as connection:
        row = connection.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?",
            (imported_id,),
        ).fetchone()
        assert row is not None
        connection.execute(
            """
            INSERT INTO conversations (
                conversation_id, surface, reply_target_version,
                reply_target_key, reply_target_json, project_id, revision,
                created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, NULL, 1, ?, ?)
            """,
            (
                replacement_id,
                row[1],
                row[2],
                row[3],
                row[4],
                row[7] + 1,
                row[8] + 1,
            ),
        )
        connection.execute(
            "UPDATE active_conversation_bindings SET conversation_id = ?, "
            "revision = revision + 1 WHERE conversation_id = ?",
            (replacement_id, imported_id),
        )
        connection.commit()

    repeated = execute_transcript_migration(
        "apply",
        source=legacy_path,
        state_root=state_root,
        profile=profile,
        expected_manifest_sha256=plan["cutover_manifest_sha256"],
    )
    verified = execute_transcript_migration(
        "verify",
        source=legacy_path,
        state_root=state_root,
        profile=profile,
    )

    assert repeated["cutover_state"] == verified["cutover_state"] == "committed"
    with sqlite3.connect(state_root / "control.db") as connection:
        assert connection.execute(
            "SELECT conversation_id FROM active_conversation_bindings"
        ).fetchone()[0] == replacement_id


def test_cutover_manifest_binds_identical_content_to_source_path(tmp_path):
    source_a = tmp_path / "source-a.db"
    source_b = tmp_path / "source-b.db"
    _make_legacy_transcript(source_a)
    shutil.copy2(source_a, source_b)
    profile = _migration_profile()

    plan_a = execute_transcript_migration(
        "plan",
        source=source_a,
        profile=profile,
    )
    plan_b = execute_transcript_migration(
        "plan",
        source=source_b,
        profile=profile,
    )

    assert plan_a["source_manifest_sha256"] == plan_b["source_manifest_sha256"]
    assert plan_a["source_identity_sha256"] != plan_b["source_identity_sha256"]
    assert plan_a["cutover_manifest_sha256"] != plan_b["cutover_manifest_sha256"]
    state_root = tmp_path / "state"
    with pytest.raises(ValueError, match="manifest drift"):
        execute_transcript_migration(
            "apply",
            source=source_b,
            state_root=state_root,
            profile=profile,
            expected_manifest_sha256=plan_a["cutover_manifest_sha256"],
        )
    assert not state_root.exists()


def test_existing_backup_is_verified_before_reuse(tmp_path):
    legacy_path = tmp_path / "legacy.db"
    _make_legacy_transcript(legacy_path)
    profile = _migration_profile()
    plan = execute_transcript_migration("plan", source=legacy_path, profile=profile)
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backup = (
        state_root
        / "legacy-transcript-backups"
        / f"{plan['cutover_manifest_sha256']}.db"
    )
    backup.parent.mkdir(parents=True)
    backup.touch()

    with pytest.raises(TranscriptImportConflict, match="schema"):
        execute_transcript_migration(
            "apply",
            source=legacy_path,
            state_root=state_root,
            profile=profile,
            expected_manifest_sha256=plan["cutover_manifest_sha256"],
        )
    with ControlStateRepository(state_root) as repository:
        assert repository.list_legacy_import_states() == ()


def test_backup_publication_is_atomic_and_retryable(tmp_path, monkeypatch):
    legacy_path = tmp_path / "legacy.db"
    _make_legacy_transcript(legacy_path)
    profile = _migration_profile()
    plan = execute_transcript_migration("plan", source=legacy_path, profile=profile)
    state_root = tmp_path / "state"
    backup = (
        state_root
        / "legacy-transcript-backups"
        / f"{plan['cutover_manifest_sha256']}.db"
    )
    real_replace = os.replace

    def fail_backup_rename(source, destination):
        if Path(destination) == backup:
            raise OSError("simulated backup rename failure")
        return real_replace(source, destination)

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "replace", fail_backup_rename)
        with pytest.raises(OSError, match="backup rename failure"):
            execute_transcript_migration(
                "apply",
                source=legacy_path,
                state_root=state_root,
                profile=profile,
                expected_manifest_sha256=plan["cutover_manifest_sha256"],
            )

    assert not backup.exists()
    assert not list(backup.parent.glob(".*.tmp"))
    applied = execute_transcript_migration(
        "apply",
        source=legacy_path,
        state_root=state_root,
        profile=profile,
        expected_manifest_sha256=plan["cutover_manifest_sha256"],
    )
    assert applied["cutover_state"] == "committed"


def test_published_snapshot_rolls_forward_from_backup_without_original(
    tmp_path,
    monkeypatch,
):
    legacy_path = tmp_path / "legacy.db"
    _make_legacy_transcript(legacy_path)
    profile = _migration_profile()
    plan = execute_transcript_migration("plan", source=legacy_path, profile=profile)
    state_root = tmp_path / "state"

    def fail_after_publication(self, *args, **kwargs):
        raise RuntimeError("simulated crash after Transcript publication")

    with monkeypatch.context() as patcher:
        patcher.setattr(
            ControlStateRepository,
            "record_legacy_transcript_cutover",
            fail_after_publication,
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            execute_transcript_migration(
                "apply",
                source=legacy_path,
                state_root=state_root,
                profile=profile,
                expected_manifest_sha256=plan["cutover_manifest_sha256"],
            )

    assert (state_root / "transcripts.db").is_file()
    with ControlStateRepository(state_root) as repository:
        import_run_id = next(
            row[0]
            for row in repository._conn.execute(
                "SELECT import_run_id FROM legacy_import_runs"
            ).fetchall()
        )
        assert repository.get_legacy_import_state(import_run_id) == "planned"
    legacy_path.unlink()

    recovered = execute_transcript_migration(
        "apply",
        source=legacy_path,
        state_root=state_root,
        profile=profile,
        expected_manifest_sha256=plan["cutover_manifest_sha256"],
    )

    assert recovered["cutover_state"] == "committed"
    assert Path(recovered["backup_path"]).is_file()
    assert not list((state_root / "legacy-transcript-backups").glob("*.tmp"))


def test_validated_resume_does_not_remap_conversations(tmp_path, monkeypatch):
    legacy_path = tmp_path / "legacy.db"
    _make_legacy_transcript(legacy_path)
    profile = _migration_profile()
    plan = execute_transcript_migration("plan", source=legacy_path, profile=profile)
    state_root = tmp_path / "state"

    def fail_commit(self, import_run_id):
        raise RuntimeError("simulated crash before Control commit")

    with monkeypatch.context() as patcher:
        patcher.setattr(
            ControlStateRepository,
            "commit_legacy_import_cutover",
            fail_commit,
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            execute_transcript_migration(
                "apply",
                source=legacy_path,
                state_root=state_root,
                profile=profile,
                expected_manifest_sha256=plan["cutover_manifest_sha256"],
            )

    with ControlStateRepository(state_root) as repository:
        assert repository.list_legacy_import_states() == ("validated",)

    def forbid_remap(self, import_run_id, mappings):
        raise AssertionError("validated import attempted to remap Conversations")

    with monkeypatch.context() as patcher:
        patcher.setattr(
            ControlStateRepository,
            "import_legacy_conversations",
            forbid_remap,
        )
        recovered = execute_transcript_migration(
            "apply",
            source=legacy_path,
            state_root=state_root,
            profile=profile,
            expected_manifest_sha256=plan["cutover_manifest_sha256"],
        )
    assert recovered["cutover_state"] == "committed"


def test_empty_legacy_stream_persists_conversation_baseline(tmp_path):
    legacy_path = tmp_path / "legacy.db"
    _make_legacy_transcript(legacy_path)
    with sqlite3.connect(legacy_path) as connection:
        connection.execute("DELETE FROM transcript_messages")
        connection.execute(
            "UPDATE transcript_chats SET last_seq = -1 WHERE chat_key = ?",
            ("s:legacy-chat",),
        )
        connection.commit()
    profile = _migration_profile(active=False)
    low_level_plan = plan_legacy_import(legacy_path)
    plan = execute_transcript_migration("plan", source=legacy_path, profile=profile)
    state_root = tmp_path / "state"

    applied = execute_transcript_migration(
        "apply",
        source=legacy_path,
        state_root=state_root,
        profile=profile,
        expected_manifest_sha256=plan["cutover_manifest_sha256"],
    )

    assert low_level_plan.conversation_count == 1
    assert plan["conversation_count"] == applied["conversation_count"] == 1
    assert applied["entry_count"] == 0
    with sqlite3.connect(state_root / "transcripts.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM transcript_conversations"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT initial_message_count FROM transcript_import_conversations"
        ).fetchone()[0] == 0


def test_runtime_rejects_fresh_transcript_store_replacing_committed_cutover(
    tmp_path,
):
    legacy_path = tmp_path / "legacy.db"
    _make_legacy_transcript(legacy_path)
    profile = _migration_profile()
    plan = execute_transcript_migration("plan", source=legacy_path, profile=profile)
    state_root = tmp_path / "state"
    execute_transcript_migration(
        "apply",
        source=legacy_path,
        state_root=state_root,
        profile=profile,
        expected_manifest_sha256=plan["cutover_manifest_sha256"],
    )

    replacement_root = tmp_path / "replacement"
    replacement = CanonicalTranscript(replacement_root)
    replacement.close()
    for suffix in ("-wal", "-shm"):
        stale = state_root / f"transcripts.db{suffix}"
        if stale.exists():
            stale.unlink()
    os.replace(replacement_root / "transcripts.db", state_root / "transcripts.db")

    with pytest.raises(ControlIntegrityError, match="different Transcript Store"):
        ControlRuntime.for_local_surface(
            state_root=state_root,
            workspace_id="workspace-test",
            surface="cli",
            installation_id="local",
            profile_id="owner",
        )
