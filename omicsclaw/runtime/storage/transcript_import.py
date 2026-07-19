"""Offline, profile-driven cutover into the canonical Transcript Store."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Mapping

from omicsclaw.control.errors import ControlIntegrityError
from omicsclaw.control.repository import ControlStateRepository
from omicsclaw.control.runtime import default_control_state_root

from .canonical_transcript import (
    CanonicalTranscript,
    TranscriptImportConflict,
    TranscriptIntegrityError,
    inspect_legacy_streams,
    plan_legacy_import,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _import_run_id(cutover_manifest_sha256: str) -> str:
    seed = f"canonical-transcript-cutover-v3\0{cutover_manifest_sha256}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _load_profile(profile: str | Path | Mapping[str, Any] | None) -> dict[str, Any]:
    if profile is None:
        return {"schema_version": 1, "streams": []}
    if isinstance(profile, Mapping):
        payload = dict(profile)
    else:
        path = Path(profile).expanduser().resolve()
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Transcript migration profile must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported Transcript migration profile schema")
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise ValueError("Transcript migration profile streams must be a list")
    return payload


def _validate_reply_target(surface: str, target: Mapping[str, Any]) -> None:
    if target.get("schema_version") != 1 or target.get("kind") != surface:
        raise ValueError("migration ReplyTarget schema does not match Surface")
    if surface in {"cli", "desktop"}:
        expected = {
            "schema_version",
            "kind",
            "installation_id",
            "profile_id",
            "slot",
        }
        if set(target) != expected:
            raise ValueError("local migration ReplyTarget fields do not match V1")
        if any(not str(target[field]).strip() for field in expected - {"schema_version"}):
            raise ValueError("local migration ReplyTarget fields must be non-empty")
        return
    if surface != "channel":
        raise ValueError("migration Surface must be cli, desktop or channel")
    required = {
        "schema_version",
        "kind",
        "adapter",
        "account_namespace",
        "destination_id",
    }
    if not required.issubset(target) or set(target) - (required | {"thread_id"}):
        raise ValueError("Channel migration ReplyTarget fields do not match V1")
    if any(not str(target[field]).strip() for field in required - {"schema_version"}):
        raise ValueError("Channel migration ReplyTarget fields must be non-empty")


def _build_plan(
    source: str | Path,
    profile: str | Path | Mapping[str, Any] | None,
    *,
    source_identity: str | Path | None = None,
) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve()
    source_identity_path = Path(source_identity or source_path).expanduser().resolve()
    source_report = plan_legacy_import(source_path)
    streams = inspect_legacy_streams(source_path)
    profile_payload = _load_profile(profile)
    profile_manifest = _digest_text(_canonical_json(profile_payload))

    raw_entries = profile_payload["streams"]
    entries_by_key: dict[str, dict[str, Any]] = {}
    conflicts: list[str] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("Transcript migration stream entries must be objects")
        legacy_key = str(raw_entry.get("legacy_key", "")).strip()
        if not legacy_key:
            raise ValueError("Transcript migration legacy_key must be non-empty")
        if legacy_key in entries_by_key:
            raise ValueError(f"duplicate migration profile key: {legacy_key}")
        entries_by_key[legacy_key] = raw_entry

    source_keys = {stream.chat_key for stream in streams}
    for extra in sorted(set(entries_by_key) - source_keys):
        conflicts.append(f"profile_stream_not_found:{extra}")

    mappings: list[dict[str, Any]] = []
    active_addresses: set[tuple[str, str]] = set()
    for stream in streams:
        raw_entry = entries_by_key.get(stream.chat_key)
        if raw_entry is None:
            conflicts.append(f"mapping_profile_required:{stream.chat_key}")
            continue
        surface = str(raw_entry.get("surface", "")).strip()
        reply_target = raw_entry.get("reply_target")
        if not isinstance(reply_target, Mapping):
            raise ValueError(
                f"migration profile {stream.chat_key} has no ReplyTarget"
            )
        target = dict(reply_target)
        _validate_reply_target(surface, target)
        active = raw_entry.get("active", False)
        if not isinstance(active, bool):
            raise ValueError("migration profile active must be boolean")
        address = (surface, _digest_text(_canonical_json(target)))
        if active and address in active_addresses:
            conflicts.append(f"multiple_active_conversations:{stream.chat_key}")
        if active:
            active_addresses.add(address)
        identity_seed = _canonical_json(
            {
                "version": 1,
                "source_manifest_sha256": source_report.source_manifest_sha256,
                "legacy_key": stream.chat_key,
                "surface": surface,
                "reply_target": target,
            }
        )
        conversation_id = _digest_text(identity_seed)[:32]
        mappings.append(
            {
                "legacy_key": stream.chat_key,
                "conversation_id": conversation_id,
                "surface": surface,
                "reply_target": target,
                "active": active,
                "message_count": stream.message_count,
                "evidence": {
                    "legacy_chat_id_json": stream.chat_id_json,
                    "source_manifest_sha256": source_report.source_manifest_sha256,
                    "profile_manifest_sha256": profile_manifest,
                },
            }
        )

    mapping_manifest = _digest_text(_canonical_json(mappings))
    cutover_manifest = _digest_text(
        _canonical_json(
            {
                "version": 3,
                "source_identity_sha256": _digest_text(str(source_identity_path)),
                "source_manifest_sha256": source_report.source_manifest_sha256,
                "mapping_manifest_sha256": mapping_manifest,
            }
        )
    )
    return {
        "state": "planned",
        "source_path": str(source_identity_path),
        "source_identity": str(source_identity_path),
        "source_identity_sha256": _digest_text(str(source_identity_path)),
        "source_manifest_sha256": source_report.source_manifest_sha256,
        "profile_manifest_sha256": profile_manifest,
        "mapping_manifest_sha256": mapping_manifest,
        "cutover_manifest_sha256": cutover_manifest,
        "conversation_count": len(streams),
        "entry_count": source_report.entry_count,
        "mappings": mappings,
        "conflicts": conflicts,
    }


def _require_digest(value: object, name: str) -> str:
    normalized = str(value)
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return normalized


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_backup_manifest(path: Path, expected_manifest_sha256: str) -> None:
    report = plan_legacy_import(path)
    if report.source_manifest_sha256 != expected_manifest_sha256:
        raise TranscriptIntegrityError(
            "legacy Transcript backup manifest does not match the planned source"
        )


def _sqlite_backup(
    source: Path,
    destination: Path,
    *,
    expected_manifest_sha256: str,
) -> None:
    """Publish a verified SQLite snapshot via fsync plus atomic rename."""

    expected_manifest = _require_digest(
        expected_manifest_sha256,
        "expected_manifest_sha256",
    )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink():
        raise TranscriptIntegrityError("legacy Transcript backup must not be a symlink")
    if destination.exists():
        if not destination.is_file():
            raise TranscriptIntegrityError(
                "legacy Transcript backup path is not a regular file"
            )
        _verify_backup_manifest(destination, expected_manifest)
        return

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()
        _verify_backup_manifest(temporary, expected_manifest)
        os.chmod(temporary, 0o600)
        _fsync_file_and_directory(temporary)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _mapped_snapshot(
    snapshot: Path,
    destination: Path,
    mappings: list[dict[str, Any]],
    *,
    source_manifest_sha256: str,
) -> None:
    if destination.exists():
        destination.unlink()
    _sqlite_backup(
        snapshot,
        destination,
        expected_manifest_sha256=source_manifest_sha256,
    )
    connection = sqlite3.connect(destination)
    try:
        for mapping in mappings:
            cursor = connection.execute(
                "UPDATE transcript_chats SET chat_id_json = ? WHERE chat_key = ?",
                (
                    json.dumps(mapping["conversation_id"], separators=(",", ":")),
                    mapping["legacy_key"],
                ),
            )
            if cursor.rowcount != 1:
                raise TranscriptImportConflict(
                    f"legacy stream disappeared: {mapping['legacy_key']}"
                )
        connection.commit()
    finally:
        connection.close()
    _fsync_file_and_directory(destination)


def _fsync_file_and_directory(path: Path) -> None:
    file_descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
    _fsync_directory(path.parent)


def _cutover_manifest_from_plan(plan: Mapping[str, Any]) -> str:
    mappings = plan.get("mappings")
    if not isinstance(mappings, list):
        raise TranscriptIntegrityError("persisted Transcript plan has no mappings")
    mapping_manifest = _digest_text(_canonical_json(mappings))
    if mapping_manifest != plan.get("mapping_manifest_sha256"):
        raise TranscriptIntegrityError("persisted Transcript mapping plan drift")
    source_identity = str(plan.get("source_identity", ""))
    source_identity_sha256 = _digest_text(source_identity)
    if source_identity_sha256 != plan.get("source_identity_sha256"):
        raise TranscriptIntegrityError("persisted Transcript source identity drift")
    source_manifest = _require_digest(
        plan.get("source_manifest_sha256"),
        "source_manifest_sha256",
    )
    return _digest_text(
        _canonical_json(
            {
                "version": 3,
                "source_identity_sha256": source_identity_sha256,
                "source_manifest_sha256": source_manifest,
                "mapping_manifest_sha256": mapping_manifest,
            }
        )
    )


def _validate_persisted_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(plan)
    cutover_manifest = _cutover_manifest_from_plan(payload)
    if cutover_manifest != payload.get("cutover_manifest_sha256"):
        raise TranscriptIntegrityError("persisted Transcript cutover plan drift")
    if payload.get("source_path") != payload.get("source_identity"):
        raise TranscriptIntegrityError("persisted Transcript source path drift")
    mappings = payload["mappings"]
    conflicts = payload.get("conflicts")
    if not isinstance(conflicts, list):
        raise TranscriptIntegrityError("persisted Transcript conflicts are invalid")
    conversation_count = int(payload.get("conversation_count", -1))
    if conversation_count < len(mappings) or (
        not conflicts and conversation_count != len(mappings)
    ):
        raise TranscriptIntegrityError("persisted Transcript Conversation count drift")
    return payload


def _plan_artifact_path(root: Path, import_run_id: str) -> Path:
    return _safe_state_path(
        root,
        Path("legacy-transcript-imports") / import_run_id / "plan.json",
    )


def _safe_state_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise TranscriptIntegrityError("Transcript state path is unsafe")
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise TranscriptIntegrityError("Transcript state path escapes its state root")
    return candidate


def _write_plan_artifact(
    path: Path,
    *,
    plan: Mapping[str, Any],
    import_run_id: str,
    backup_relative: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "schema_version": 1,
        "import_run_id": import_run_id,
        "backup_relative": backup_relative.as_posix(),
        "plan": dict(plan),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".plan.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_plan_artifact(
    path: Path,
    *,
    expected_import_run_id: str,
    expected_manifest_sha256: str,
) -> tuple[dict[str, Any], Path]:
    if path.is_symlink() or not path.is_file():
        raise TranscriptIntegrityError(
            "Transcript plan artifact must be a regular non-symlink file"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise TranscriptIntegrityError("unsupported Transcript plan artifact")
    if payload.get("import_run_id") != expected_import_run_id:
        raise TranscriptIntegrityError("Transcript plan artifact import identity drift")
    raw_plan = payload.get("plan")
    if not isinstance(raw_plan, dict):
        raise TranscriptIntegrityError("Transcript plan artifact has no plan")
    plan = _validate_persisted_plan(raw_plan)
    if plan["cutover_manifest_sha256"] != expected_manifest_sha256:
        raise TranscriptIntegrityError("Transcript plan artifact cutover drift")
    backup_relative = Path(str(payload.get("backup_relative", "")))
    if (
        not backup_relative.parts
        or backup_relative.is_absolute()
        or ".." in backup_relative.parts
    ):
        raise TranscriptIntegrityError("Transcript plan backup path is unsafe")
    return plan, backup_relative


def _verify_backup_against_plan(backup: Path, plan: Mapping[str, Any]) -> None:
    if backup.is_symlink() or not backup.is_file():
        raise TranscriptIntegrityError(
            "legacy Transcript backup must be a regular non-symlink file"
        )
    report = plan_legacy_import(backup)
    if report.source_manifest_sha256 != str(plan["source_manifest_sha256"]):
        raise TranscriptIntegrityError(
            "legacy Transcript backup manifest does not match the planned source"
        )
    if (
        report.conversation_count != int(plan["conversation_count"])
        or report.entry_count != int(plan["entry_count"])
    ):
        raise TranscriptIntegrityError(
            "legacy Transcript backup counts do not match the plan"
        )
    streams = inspect_legacy_streams(backup)
    mappings = plan["mappings"]
    expected = {
        str(mapping["legacy_key"]): (
            str(mapping["evidence"]["legacy_chat_id_json"]),
            int(mapping["message_count"]),
        )
        for mapping in mappings
    }
    actual = {
        stream.chat_key: (stream.chat_id_json, stream.message_count)
        for stream in streams
    }
    if actual != expected:
        raise TranscriptIntegrityError(
            "legacy Transcript backup stream identity does not match the plan"
        )


def _clear_staged_transcript(stage_root: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = stage_root / f"transcripts.db{suffix}"
        if path.exists():
            path.unlink()


def execute_transcript_migration(
    command: str,
    *,
    source: str | Path,
    state_root: str | Path | None = None,
    expected_manifest_sha256: str = "",
    profile: str | Path | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute read-only ``plan`` or locked ``apply``/``verify`` cutover."""

    if command == "plan":
        return _build_plan(source, profile)
    if command not in {"apply", "verify"}:
        raise ValueError(f"unsupported Transcript migration command: {command}")
    if command == "apply" and not expected_manifest_sha256:
        raise ValueError("apply requires the exact cutover SHA-256 emitted by plan")
    if expected_manifest_sha256:
        _require_digest(expected_manifest_sha256, "expected_manifest_sha256")

    configured_root = Path(
        state_root or default_control_state_root()
    ).expanduser().absolute()
    if configured_root.is_symlink():
        raise ControlIntegrityError("Control state root must not be a symlink")
    root = configured_root.resolve()
    plan: dict[str, Any] | None = None
    backup_relative: Path | None = None
    artifact_path: Path | None = None
    if expected_manifest_sha256:
        expected_run_id = _import_run_id(expected_manifest_sha256)
        candidate = _plan_artifact_path(root, expected_run_id)
        if candidate.is_file():
            plan, backup_relative = _load_plan_artifact(
                candidate,
                expected_import_run_id=expected_run_id,
                expected_manifest_sha256=expected_manifest_sha256,
            )
            artifact_path = candidate

    if plan is None:
        live_plan = _validate_persisted_plan(_build_plan(source, profile))
        if (
            expected_manifest_sha256
            and expected_manifest_sha256 != live_plan["cutover_manifest_sha256"]
        ):
            raise ValueError(
                "source or mapping manifest drift: pass the exact cutover SHA-256 "
                "emitted by plan"
            )
        plan = live_plan
        expected_manifest_sha256 = str(plan["cutover_manifest_sha256"])
        import_run_id = _import_run_id(expected_manifest_sha256)
        artifact_path = _plan_artifact_path(root, import_run_id)
        if artifact_path.is_file():
            persisted_plan, persisted_backup = _load_plan_artifact(
                artifact_path,
                expected_import_run_id=import_run_id,
                expected_manifest_sha256=expected_manifest_sha256,
            )
            if persisted_plan != plan:
                raise TranscriptIntegrityError(
                    "live and persisted Transcript cutover plans do not match"
                )
            plan = persisted_plan
            backup_relative = persisted_backup
    else:
        import_run_id = _import_run_id(str(plan["cutover_manifest_sha256"]))

    requested_source_identity = str(Path(source).expanduser().resolve())
    if requested_source_identity != str(plan["source_identity"]):
        raise ValueError(
            "source identity drift: the cutover plan is bound to its original path"
        )
    if plan["conflicts"]:
        raise TranscriptImportConflict(
            "Transcript migration profile has unresolved conflicts: "
            + ", ".join(plan["conflicts"][:5])
        )
    import_run_id = _import_run_id(str(plan["cutover_manifest_sha256"]))
    if backup_relative is None:
        backup_relative = (
            Path("legacy-transcript-backups")
            / f"{plan['cutover_manifest_sha256']}.db"
        )
    backup_path = _safe_state_path(root, backup_relative)
    stage_root = _safe_state_path(
        root,
        Path(f".transcript-import-{import_run_id}"),
    )
    mapped_source = stage_root / "mapped-source.db"
    final_path = root / "transcripts.db"
    report_ref = f"transcript-import://{plan['cutover_manifest_sha256']}"
    artifact_path = artifact_path or _plan_artifact_path(root, import_run_id)

    with ControlStateRepository(root) as repository:
        if not backup_path.exists():
            live_evidence = _validate_persisted_plan(_build_plan(source, profile))
            if live_evidence != plan:
                raise ValueError("legacy source changed before its consistent snapshot")
            _sqlite_backup(
                Path(source).expanduser().resolve(),
                backup_path,
                expected_manifest_sha256=str(plan["source_manifest_sha256"]),
            )
        _verify_backup_against_plan(backup_path, plan)
        if command == "apply" and not artifact_path.exists():
            _write_plan_artifact(
                artifact_path,
                plan=plan,
                import_run_id=import_run_id,
                backup_relative=backup_relative,
            )

        stage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _mapped_snapshot(
            backup_path,
            mapped_source,
            plan["mappings"],
            source_manifest_sha256=str(plan["source_manifest_sha256"]),
        )

        existing_state = repository.get_legacy_import_state(import_run_id)
        if command == "verify" and existing_state != "committed":
            raise RuntimeError(
                "canonical Transcript exists without committed control cutover"
            )

        if command == "apply":
            if final_path.exists() and existing_state is None:
                raise TranscriptImportConflict(
                    "canonical transcripts.db already exists outside this cutover"
                )
            if existing_state is None:
                repository.begin_legacy_import(
                    import_run_id,
                    source_manifest_sha256=str(plan["cutover_manifest_sha256"]),
                    report_ref=report_ref,
                )
                existing_state = "planned"

            if existing_state == "planned":
                repository.begin_legacy_import(
                    import_run_id,
                    source_manifest_sha256=str(plan["cutover_manifest_sha256"]),
                    report_ref=report_ref,
                )
                repository.import_legacy_conversations(
                    import_run_id,
                    plan["mappings"],
                )

            if existing_state == "planned" and not final_path.exists():
                _clear_staged_transcript(stage_root)
                staged = CanonicalTranscript(stage_root)
                try:
                    staged._import_mapped_snapshot(
                        mapped_source,
                        import_run_id=import_run_id,
                        cutover_manifest_sha256=str(
                            plan["cutover_manifest_sha256"]
                        ),
                        source_identity=str(plan["source_identity"]),
                        expected_conversations=plan["mappings"],
                        backup_path=backup_path,
                    )
                    staged.verify_legacy_import(
                        mapped_source,
                        import_run_id=import_run_id,
                        cutover_manifest_sha256=str(
                            plan["cutover_manifest_sha256"]
                        ),
                        source_identity=str(plan["source_identity"]),
                        require_initial_view=True,
                    )
                finally:
                    staged.close()
                staged_path = stage_root / "transcripts.db"
                _fsync_file_and_directory(staged_path)
                os.replace(staged_path, final_path)
                os.chmod(final_path, 0o600)
                _fsync_file_and_directory(final_path)

            if existing_state not in {"planned", "validated", "committed"}:
                raise ControlIntegrityError(
                    f"legacy Transcript cutover cannot resume from {existing_state}"
                )

            transcript = CanonicalTranscript(root, require_existing=True)
            try:
                report = transcript.verify_legacy_import(
                    mapped_source,
                    import_run_id=import_run_id,
                    cutover_manifest_sha256=str(plan["cutover_manifest_sha256"]),
                    source_identity=str(plan["source_identity"]),
                    require_initial_view=existing_state != "committed",
                )
                if existing_state == "planned":
                    repository.verify_legacy_conversations(
                        import_run_id,
                        plan["mappings"],
                        require_active_binding=True,
                    )
                    identity = transcript.get_cutover_identity(import_run_id)
                    if identity is None:  # pragma: no cover - verified above
                        raise TranscriptIntegrityError(
                            "published Transcript has no cutover identity"
                        )
                    repository.bind_transcript_store(
                        identity.transcript_store_id,
                        import_run_id=import_run_id,
                    )
                    repository.record_legacy_transcript_cutover(
                        import_run_id,
                        cutover_manifest_sha256=identity.cutover_manifest_sha256,
                        transcript_store_id=identity.transcript_store_id,
                        import_baseline_sha256=identity.import_baseline_sha256,
                        source_identity=identity.source_identity,
                    )
                    repository.mark_legacy_import_validated(import_run_id)
                    existing_state = "validated"

                repository.verify_transcript_store_binding(
                    transcript.transcript_store_id
                )
                control_identity = repository.get_legacy_transcript_cutover(
                    import_run_id
                )
                if control_identity is None:
                    raise ControlIntegrityError(
                        "Control has no immutable Transcript cutover identity"
                    )
                repository.verify_transcript_store_binding(
                    transcript.transcript_store_id
                )
                transcript.verify_cutover_identity(**control_identity)

                if existing_state == "validated":
                    repository.verify_legacy_conversations(
                        import_run_id,
                        plan["mappings"],
                        require_active_binding=True,
                    )
                    repository.commit_legacy_import_cutover(import_run_id)
                elif existing_state == "committed":
                    repository.verify_legacy_conversations(
                        import_run_id,
                        plan["mappings"],
                        require_active_binding=False,
                    )
            finally:
                transcript.close()
        else:
            transcript = CanonicalTranscript(root, require_existing=True)
            try:
                report = transcript.verify_legacy_import(
                    mapped_source,
                    import_run_id=import_run_id,
                    cutover_manifest_sha256=str(plan["cutover_manifest_sha256"]),
                    source_identity=str(plan["source_identity"]),
                    require_initial_view=False,
                )
                control_identity = repository.get_legacy_transcript_cutover(
                    import_run_id
                )
                if control_identity is None:
                    raise ControlIntegrityError(
                        "Control has no immutable Transcript cutover identity"
                    )
                transcript.verify_cutover_identity(**control_identity)
            finally:
                transcript.close()
            repository.verify_legacy_conversations(
                import_run_id,
                plan["mappings"],
                require_active_binding=False,
            )

        payload = dict(plan)
        payload.update(
            {
                "state": report.state,
                "import_run_id": import_run_id,
                "cutover_state": repository.get_legacy_import_state(import_run_id),
                "database_path": str(final_path),
                "backup_path": str(backup_path),
            }
        )
        return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline canonical Transcript migration (runtime must be stopped)",
    )
    parser.add_argument("command", choices=("plan", "apply", "verify"))
    parser.add_argument("--source", required=True, help="Legacy transcripts.db path")
    parser.add_argument(
        "--profile",
        default=None,
        help="Owner-reviewed JSON mapping profile (required before apply)",
    )
    parser.add_argument(
        "--state-root",
        default=None,
        help="Canonical state root (default: control runtime state root)",
    )
    parser.add_argument(
        "--manifest-sha256",
        default="",
        help="Exact cutover manifest emitted by plan; required by apply",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = execute_transcript_migration(
        args.command,
        source=args.source,
        profile=args.profile,
        state_root=args.state_root,
        expected_manifest_sha256=args.manifest_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through CLI
    raise SystemExit(main())


__all__ = ["execute_transcript_migration", "main"]
