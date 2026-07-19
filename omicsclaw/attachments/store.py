"""Deep Attachment Store Module over strict SQLite and content-addressed files."""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import errno
import hashlib
import inspect
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time
from typing import Callable, Iterable, Iterator, Sequence

if os.name == "nt":  # pragma: no cover - exercised on Windows CI.
    import msvcrt
else:  # pragma: no cover - branch choice is platform-specific.
    import fcntl

from .models import (
    AttachmentBatchCommitment,
    AttachmentBatchPublication,
    AttachmentReferenceV1,
    InboundAttachmentSource,
    SourceAttachmentDescriptorV1,
    references_sha256,
)
from .schema import MIGRATIONS, verify_migration_source


# Governed external reference kinds that may retain a Blob beyond its Records.
# Kept closed so a typo cannot silently create an unreadable retention class.
_RETENTION_HOLDER_KINDS = frozenset({"run_input", "transcript", "external"})


def _require_digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AttachmentValidationError(f"{name} must be a lowercase SHA-256 digest")
    return value


DEFAULT_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_BATCH_BYTES = 50 * 1024 * 1024
DEFAULT_RESOLVE_MAX_BYTES = 64 * 1024 * 1024
_ALLOWED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


class AttachmentStoreError(RuntimeError):
    """Base class for Attachment Store failures."""


class AttachmentRejectedError(AttachmentStoreError, ValueError):
    """The proposed batch or supplied bytes violate the accepted input policy."""


class AttachmentIntegrityError(AttachmentStoreError):
    """Durable Attachment metadata and bytes no longer agree."""


class AttachmentNotAcceptedError(AttachmentRejectedError):
    """A caller attempted to resolve a provisional or unknown attachment."""


class AttachmentStoreClosedError(AttachmentStoreError):
    """The Attachment Store has already released its durable handles."""


# Compatibility aliases kept inside the new module while callers move to the
# frozen public names above.
AttachmentError = AttachmentStoreError
AttachmentValidationError = AttachmentRejectedError


@dataclass(frozen=True, slots=True)
class AttachmentStoreRecoveryResult:
    """Auditable summary of one deterministic reconciliation pass."""

    accepted_batch_ids: tuple[str, ...]
    abandoned_batch_ids: tuple[str, ...]
    deleted_orphan_record_count: int
    deleted_orphan_blob_count: int


@dataclass(frozen=True, slots=True)
class _StagedRecord:
    attachment_id: str
    descriptor: SourceAttachmentDescriptorV1
    descriptor_json: str
    descriptor_sha256: str
    content_sha256: str
    byte_size: int
    detected_media_type: str
    relative_path: str


def _now_ms() -> int:
    return int(time.time() * 1_000)


def _new_id() -> str:
    return secrets.token_hex(16)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _detect_media_type(payload_prefix: bytes, byte_size: int) -> str:
    if payload_prefix.startswith(b"\x89PNG\r\n\x1a\n") and byte_size >= 24:
        return "image/png"
    if payload_prefix.startswith(b"\xff\xd8\xff") and byte_size >= 4:
        return "image/jpeg"
    if payload_prefix.startswith((b"GIF87a", b"GIF89a")) and byte_size >= 14:
        return "image/gif"
    if (
        len(payload_prefix) >= 12
        and payload_prefix[:4] == b"RIFF"
        and payload_prefix[8:12] == b"WEBP"
        and byte_size >= 20
    ):
        return "image/webp"
    raise AttachmentValidationError("attachment content is not an accepted image type")


class AttachmentStore:
    """Own immutable Attachment Records, Blob bytes, and crash reconciliation."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        max_attachments: int = 8,
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
        provisional_grace_ms: int = 60 * 60 * 1_000,
        clock_ms: Callable[[], int] = _now_ms,
        fault_hook: Callable[[str], None] | None = None,
        require_existing: bool = False,
    ) -> None:
        for name, value in (
            ("max_attachments", max_attachments),
            ("max_attachment_bytes", max_attachment_bytes),
            ("max_batch_bytes", max_batch_bytes),
            ("provisional_grace_ms", provisional_grace_ms),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if max_attachment_bytes > max_batch_bytes:
            raise ValueError("max_attachment_bytes cannot exceed max_batch_bytes")
        if not callable(clock_ms):
            raise TypeError("clock_ms must be callable")
        if fault_hook is not None and not callable(fault_hook):
            raise TypeError("fault_hook must be callable")

        verify_migration_source()
        root = Path(state_root).expanduser().absolute()
        if root.is_symlink():
            raise AttachmentIntegrityError(
                "Attachment Store root must not be a symlink"
            )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_root = root.resolve()
        self.database_path = self.state_root / "attachments.db"
        self.lifetime_lock_path = self.state_root / "attachments.lock"
        self.blob_root = self.state_root / "attachment_blobs"
        self.staging_root = self.state_root / "attachment_staging"
        for path, label in (
            (self.database_path, "database"),
            (self.lifetime_lock_path, "lifetime lock"),
            (self.blob_root, "Blob root"),
            (self.staging_root, "staging root"),
        ):
            if path.is_symlink():
                raise AttachmentIntegrityError(
                    f"Attachment Store {label} must not be a symlink"
                )
        database_existed = self.database_path.exists()
        if require_existing and not database_existed:
            raise AttachmentIntegrityError("required attachments.db is missing")
        if database_existed and not self.database_path.is_file():
            raise AttachmentIntegrityError("attachments.db is not a regular file")
        self.blob_root.mkdir(mode=0o700, exist_ok=True)
        self.staging_root.mkdir(mode=0o700, exist_ok=True)
        self._clock_ms = clock_ms
        self.max_attachments = max_attachments
        self.max_attachment_bytes = max_attachment_bytes
        self.max_batch_bytes = max_batch_bytes
        self.provisional_grace_ms = provisional_grace_ms
        self._fault_hook = fault_hook
        self._lock = threading.RLock()
        self._closed = False
        self._lifetime_lock_fd = self._acquire_lifetime_lock()
        try:
            self._connection = sqlite3.connect(
                self.database_path,
                isolation_level=None,
                check_same_thread=False,
                timeout=5.0,
            )
        except BaseException:
            self._release_lifetime_lock()
            self._closed = True
            raise
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._apply_migration()
            self._assert_integrity()
            self._harden_paths()
        except BaseException:
            self._connection.close()
            self._release_lifetime_lock()
            self._closed = True
            raise

    def _acquire_lifetime_lock(self) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lifetime_lock_path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            if os.name == "nt":  # pragma: no cover - Windows only.
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no branch - one platform branch per run.
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except (OSError, BlockingIOError) as exc:
            with suppress(UnboundLocalError, OSError):
                os.close(descriptor)
            raise AttachmentIntegrityError(
                "Attachment Store is already open or its lifetime lock is unsafe"
            ) from exc

    def _release_lifetime_lock(self) -> None:
        descriptor = getattr(self, "_lifetime_lock_fd", None)
        if descriptor is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover - Windows only.
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:  # pragma: no branch - one platform branch per run.
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            self._lifetime_lock_fd = None

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise AttachmentStoreClosedError("AttachmentStore is closed")
        return self._connection

    def _now(self) -> int:
        return int(self._clock_ms())

    def _checkpoint(self, name: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(name)

    @staticmethod
    def _require_managed_directory(path: Path, label: str) -> None:
        if path.is_symlink() or not path.is_dir():
            raise AttachmentIntegrityError(
                f"Attachment Store {label} is missing or unsafe"
            )

    def _harden_paths(self) -> None:
        os.chmod(self.state_root, 0o700)
        for directory in (self.blob_root, self.staging_root):
            if directory.is_symlink() or not directory.is_dir():
                raise AttachmentIntegrityError(
                    "Attachment Store managed directory is unsafe"
                )
            os.chmod(directory, 0o700)
        for shard in self.blob_root.iterdir():
            if shard.is_symlink() or not shard.is_dir():
                raise AttachmentIntegrityError("Attachment Blob shard is unsafe")
            os.chmod(shard, 0o700)
            for blob in shard.iterdir():
                if blob.is_symlink() or not blob.is_file():
                    raise AttachmentIntegrityError("Attachment Blob entry is unsafe")
                self._harden_blob_file(blob)
        for path in (
            self.database_path,
            self.lifetime_lock_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            if path.is_symlink():
                raise AttachmentIntegrityError(
                    "Attachment Store managed file must not be a symlink"
                )
            if path.exists():
                if not path.is_file():
                    raise AttachmentIntegrityError(
                        "Attachment Store managed file is not regular"
                    )
                os.chmod(path, 0o600)

    @staticmethod
    def _harden_blob_file(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise AttachmentIntegrityError(
                        "Attachment Blob is not a regular file"
                    )
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except AttachmentIntegrityError:
            raise
        except OSError as exc:
            raise AttachmentIntegrityError("Attachment Blob is unsafe") from exc

    def _apply_migration(self) -> None:
        marker = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='attachment_schema_migrations'"
        ).fetchone()
        applied: list[tuple[int, str, str]] = []
        if marker is not None:
            applied = [
                (int(row["version"]), str(row["name"]), str(row["checksum_sha256"]))
                for row in self._conn.execute(
                    "SELECT version, name, checksum_sha256 "
                    "FROM attachment_schema_migrations ORDER BY version"
                ).fetchall()
            ]
        # An existing database must be a prefix of the known migration list:
        # a tampered or newer-than-known schema fails closed rather than being
        # "upgraded" on top of state this build does not understand.
        if len(applied) > len(MIGRATIONS):
            raise AttachmentIntegrityError(
                "attachments.db has unsupported or modified migrations"
            )
        for existing, (version, name, _sql, checksum) in zip(applied, MIGRATIONS):
            if existing != (version, name, checksum):
                raise AttachmentIntegrityError(
                    "attachments.db has unsupported or modified migrations"
                )
        pending = MIGRATIONS[len(applied) :]
        if not pending:
            return
        now = self._now()
        statements = ["BEGIN IMMEDIATE;"]
        for version, name, sql, checksum in pending:
            statements.append(sql)
            statements.append(
                "\nINSERT INTO attachment_schema_migrations "
                "(version, name, checksum_sha256, applied_at_ms) VALUES "
                f"({version}, '{name}', '{checksum}', {now});\n"
            )
        statements.append("COMMIT;")
        try:
            self._conn.executescript("".join(statements))
        except BaseException:
            with suppress(sqlite3.DatabaseError):
                self._conn.execute("ROLLBACK")
            raise

    def _assert_integrity(self) -> None:
        result = self._conn.execute("PRAGMA integrity_check").fetchone()
        if result is None or str(result[0]).lower() != "ok":
            raise AttachmentIntegrityError(
                f"attachments.db integrity check failed: {result}"
            )
        violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise AttachmentIntegrityError(
                f"attachments.db foreign-key check failed: {violations[:5]}"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._conn
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    @property
    def store_id(self) -> str:
        with self._lock:
            rows = self._conn.execute(
                "SELECT store_id FROM attachment_store_identity"
            ).fetchall()
        if len(rows) != 1:
            raise AttachmentIntegrityError(
                "Attachment Store has no unique opaque identity"
            )
        value = str(rows[0]["store_id"])
        if len(value) != 32 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise AttachmentIntegrityError("Attachment Store identity is malformed")
        return value

    def _validate_descriptors(
        self, descriptors: Sequence[SourceAttachmentDescriptorV1]
    ) -> tuple[SourceAttachmentDescriptorV1, ...]:
        values = tuple(descriptors)
        if not values or len(values) > self.max_attachments:
            raise AttachmentValidationError(
                "attachment count exceeds configured limits"
            )
        if any(not isinstance(value, SourceAttachmentDescriptorV1) for value in values):
            raise AttachmentValidationError(
                "descriptors must be SourceAttachmentDescriptorV1 values"
            )
        if tuple(value.ordinal for value in values) != tuple(range(len(values))):
            raise AttachmentValidationError(
                "attachment ordinals must be unique, ordered, and contiguous"
            )
        source_ids = tuple(value.source_attachment_id for value in values)
        if len(set(source_ids)) != len(source_ids):
            raise AttachmentValidationError(
                "source_attachment_id values must be unique"
            )
        declared_total = 0
        for value in values:
            if (
                value.declared_media_type is not None
                and value.declared_media_type not in _ALLOWED_MEDIA_TYPES
            ):
                raise AttachmentValidationError("declared media type is not accepted")
            if value.declared_size is not None:
                if value.declared_size > self.max_attachment_bytes:
                    raise AttachmentValidationError(
                        "declared attachment size exceeds configured limit"
                    )
                declared_total += value.declared_size
        if declared_total > self.max_batch_bytes:
            raise AttachmentValidationError(
                "declared batch size exceeds configured limit"
            )
        return values

    async def publish_batch(
        self,
        *,
        proposed_turn_id: str,
        proposed_conversation_id: str,
        descriptors: Sequence[SourceAttachmentDescriptorV1],
        source: InboundAttachmentSource,
    ) -> AttachmentBatchPublication:
        AttachmentBatchCommitment(
            schema_version=1,
            store_id=self.store_id,
            batch_id="0" * 32,
            turn_id=proposed_turn_id,
            conversation_id=proposed_conversation_id,
            record_count=1,
            records_sha256="0" * 64,
        )
        values = self._validate_descriptors(descriptors)
        if not callable(getattr(source, "open", None)):
            raise TypeError("source must implement InboundAttachmentSource.open")

        batch_id = _new_id()
        now = self._now()
        self._require_managed_directory(self.staging_root, "staging root")
        batch_staging = self.staging_root / batch_id
        batch_staging.mkdir(mode=0o700)
        _fsync_directory(self.staging_root)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO attachment_batches (
                    batch_id, proposed_turn_id, proposed_conversation_id,
                    records_sha256, record_count, state,
                    created_at_ms, expires_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, NULL, NULL, 'staging', ?, ?, ?)
                """,
                (
                    batch_id,
                    proposed_turn_id,
                    proposed_conversation_id,
                    now,
                    now + self.provisional_grace_ms,
                    now,
                ),
            )
        self._checkpoint("after_batch_staging")

        staged: list[_StagedRecord] = []
        actual_batch_bytes = 0
        try:
            for descriptor in values:
                record = await self._stage_record(
                    batch_id=batch_id,
                    staging_root=batch_staging,
                    descriptor=descriptor,
                    source=source,
                    remaining_batch_bytes=self.max_batch_bytes - actual_batch_bytes,
                )
                actual_batch_bytes += record.byte_size
                staged.append(record)
            references = tuple(
                AttachmentReferenceV1(
                    schema_version=1,
                    attachment_id=record.attachment_id,
                    ordinal=record.descriptor.ordinal,
                    content_sha256=record.content_sha256,
                    byte_size=record.byte_size,
                    display_name=record.descriptor.display_name,
                    media_type=record.detected_media_type,
                )
                for record in staged
            )
            records_digest = references_sha256(references)
            self._checkpoint("before_batch_publish")
            with self._lock:
                with self._transaction() as connection:
                    for record in staged:
                        connection.execute(
                            """
                            INSERT INTO attachment_records (
                                attachment_id, batch_id, turn_id, conversation_id,
                                ordinal, content_sha256, byte_size, display_name,
                                declared_media_type, detected_media_type,
                                source_descriptor_json, source_descriptor_sha256,
                                state, created_at_ms, accepted_at_ms, integrity_code
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      'provisional', ?, NULL, NULL)
                            """,
                            (
                                record.attachment_id,
                                batch_id,
                                proposed_turn_id,
                                proposed_conversation_id,
                                record.descriptor.ordinal,
                                record.content_sha256,
                                record.byte_size,
                                record.descriptor.display_name,
                                record.descriptor.declared_media_type,
                                record.detected_media_type,
                                record.descriptor_json,
                                record.descriptor_sha256,
                                now,
                            ),
                        )
                    changed = connection.execute(
                        """
                        UPDATE attachment_batches
                        SET records_sha256 = ?, record_count = ?, state = 'published',
                            updated_at_ms = ?
                        WHERE batch_id = ? AND state = 'staging'
                        """,
                        (records_digest, len(staged), self._now(), batch_id),
                    ).rowcount
                    if changed != 1:
                        raise AttachmentIntegrityError(
                            "Attachment batch left staging before publication"
                        )
                published_batch, published_rows = self._load_batch(batch_id)
                if (
                    published_batch is None
                    or str(published_batch["state"]) != "published"
                    or self._rows_to_references(published_rows) != references
                ):
                    raise AttachmentIntegrityError(
                        "published Attachment Records failed durable verification"
                    )
                self._verify_reference_rows(published_rows)
                self._checkpoint("after_batch_publish")
                commitment = AttachmentBatchCommitment(
                    schema_version=1,
                    store_id=self.store_id,
                    batch_id=batch_id,
                    turn_id=proposed_turn_id,
                    conversation_id=proposed_conversation_id,
                    record_count=len(staged),
                    records_sha256=records_digest,
                )
                return AttachmentBatchPublication(commitment, references)
        except BaseException:
            self._mark_staging_abandoned(batch_id)
            raise
        finally:
            self._remove_empty_staging(batch_staging)

    async def _stage_record(
        self,
        *,
        batch_id: str,
        staging_root: Path,
        descriptor: SourceAttachmentDescriptorV1,
        source: InboundAttachmentSource,
        remaining_batch_bytes: int,
    ) -> _StagedRecord:
        staged_path = staging_root / f"{descriptor.ordinal}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor_fd = os.open(staged_path, flags, 0o600)
        digest = hashlib.sha256()
        byte_size = 0
        prefix = bytearray()
        stream: object | None = None
        try:
            opened = source.open(descriptor.source_attachment_id)
            if inspect.isawaitable(opened):
                opened = await opened
            if not hasattr(opened, "__aiter__"):
                raise AttachmentValidationError(
                    "InboundAttachmentSource.open did not return an async iterator"
                )
            stream = opened
            async for chunk in opened:
                if not isinstance(chunk, bytes):
                    raise AttachmentValidationError("attachment chunks must be bytes")
                if not chunk:
                    continue
                byte_size += len(chunk)
                if (
                    byte_size > self.max_attachment_bytes
                    or byte_size > remaining_batch_bytes
                ):
                    raise AttachmentValidationError(
                        "actual attachment bytes exceed configured limits"
                    )
                if len(prefix) < 64:
                    prefix.extend(chunk[: 64 - len(prefix)])
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor_fd, view)
                    view = view[written:]
            os.fsync(descriptor_fd)
        finally:
            try:
                close_stream = getattr(stream, "aclose", None)
                if callable(close_stream):
                    await close_stream()
            finally:
                os.close(descriptor_fd)
        self._checkpoint("after_blob_fsync")
        _fsync_directory(staging_root)

        actual_digest = digest.hexdigest()
        detected = _detect_media_type(bytes(prefix), byte_size)
        if (
            descriptor.declared_size is not None
            and descriptor.declared_size != byte_size
        ):
            raise AttachmentValidationError(
                "declared attachment size does not match bytes"
            )
        if (
            descriptor.declared_sha256 is not None
            and descriptor.declared_sha256 != actual_digest
        ):
            raise AttachmentValidationError(
                "declared attachment digest does not match bytes"
            )
        if (
            descriptor.declared_media_type is not None
            and descriptor.declared_media_type != detected
        ):
            raise AttachmentValidationError("declared media type does not match bytes")

        with self._lock:
            relative_path = self._publish_blob(
                staged_path,
                actual_digest,
                byte_size,
                created_at_ms=self._now(),
            )
            self._checkpoint("after_blob_publish")
            now = self._now()
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO attachment_blobs (
                        content_sha256, byte_size, relative_path,
                        created_at_ms, verified_at_ms
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(content_sha256) DO NOTHING
                    """,
                    (actual_digest, byte_size, relative_path, now, now),
                )
                row = connection.execute(
                    "SELECT byte_size, relative_path FROM attachment_blobs "
                    "WHERE content_sha256 = ?",
                    (actual_digest,),
                ).fetchone()
                if (
                    row is None
                    or int(row["byte_size"]) != byte_size
                    or str(row["relative_path"]) != relative_path
                ):
                    raise AttachmentIntegrityError(
                        "content-addressed Blob metadata conflicts with existing content"
                    )
        self._checkpoint("after_blob_row")
        descriptor_json = _canonical_json(descriptor.to_json_dict())
        return _StagedRecord(
            attachment_id=_new_id(),
            descriptor=descriptor,
            descriptor_json=descriptor_json,
            descriptor_sha256=hashlib.sha256(
                descriptor_json.encode("utf-8")
            ).hexdigest(),
            content_sha256=actual_digest,
            byte_size=byte_size,
            detected_media_type=detected,
            relative_path=relative_path,
        )

    def _publish_blob(
        self,
        staged_path: Path,
        digest: str,
        byte_size: int,
        *,
        created_at_ms: int,
    ) -> str:
        self._require_managed_directory(self.blob_root, "Blob root")
        shard = self.blob_root / digest[:2]
        if shard.is_symlink():
            raise AttachmentIntegrityError(
                "Attachment Blob shard must not be a symlink"
            )
        shard_created = False
        try:
            shard.mkdir(mode=0o700)
            shard_created = True
        except FileExistsError:
            if shard.is_symlink() or not shard.is_dir():
                raise AttachmentIntegrityError("Attachment Blob shard is unsafe")
        os.chmod(shard, 0o700)
        if shard_created:
            _fsync_directory(self.blob_root)
        final = shard / digest
        relative = final.relative_to(self.state_root).as_posix()
        try:
            os.link(staged_path, final, follow_symlinks=False)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            final_fd = os.open(final, flags)
            try:
                os.fchmod(final_fd, 0o600)
                timestamp_ns = created_at_ms * 1_000_000
                os.utime(final_fd, ns=(timestamp_ns, timestamp_ns))
                os.fsync(final_fd)
            finally:
                os.close(final_fd)
            _fsync_directory(shard)
            staged_path.unlink()
            _fsync_directory(staged_path.parent)
        except FileExistsError:
            self._harden_blob_file(final)
            self._verify_blob_file(final, digest, byte_size)
            staged_path.unlink(missing_ok=True)
            _fsync_directory(staged_path.parent)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                self._harden_blob_file(final)
                self._verify_blob_file(final, digest, byte_size)
                staged_path.unlink(missing_ok=True)
            else:
                raise
        self._harden_blob_file(final)
        self._verify_blob_file(final, digest, byte_size)
        return relative

    @staticmethod
    def _read_verified_blob_file(
        path: Path,
        digest: str,
        byte_size: int,
        *,
        capture: bool,
    ) -> bytes | None:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        file_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
            file_flags |= os.O_NOFOLLOW
        try:
            directory_fd = os.open(path.parent, directory_flags)
            try:
                file_fd = os.open(path.name, file_flags, dir_fd=directory_fd)
            finally:
                os.close(directory_fd)
            with os.fdopen(file_fd, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != byte_size:
                    raise AttachmentIntegrityError(
                        "Attachment Blob size or file type mismatch"
                    )
                actual = hashlib.sha256()
                chunks: list[bytes] | None = [] if capture else None
                total = 0
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    actual.update(chunk)
                    if chunks is not None:
                        chunks.append(chunk)
        except AttachmentIntegrityError:
            raise
        except OSError as exc:
            raise AttachmentIntegrityError(
                "Attachment Blob is missing, unreadable, or unsafe"
            ) from exc
        if total != byte_size:
            raise AttachmentIntegrityError(
                "Attachment Blob size mismatch while reading"
            )
        if actual.hexdigest() != digest:
            raise AttachmentIntegrityError("Attachment Blob digest mismatch")
        return b"".join(chunks) if chunks is not None else None

    @classmethod
    def _verify_blob_file(cls, path: Path, digest: str, byte_size: int) -> None:
        cls._read_verified_blob_file(
            path,
            digest,
            byte_size,
            capture=False,
        )

    def _blob_path(self, relative_path: str, digest: str) -> Path:
        self._require_managed_directory(self.blob_root, "Blob root")
        expected = f"attachment_blobs/{digest[:2]}/{digest}"
        if relative_path != expected:
            raise AttachmentIntegrityError(
                "Attachment Blob path does not match its content digest"
            )
        return self.state_root / expected

    def _mark_staging_abandoned(self, batch_id: str) -> None:
        with suppress(sqlite3.DatabaseError, RuntimeError):
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT state FROM attachment_batches WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                if row is not None and str(row["state"]) == "staging":
                    connection.execute(
                        """
                        UPDATE attachment_batches
                        SET records_sha256 = ?, record_count = 1,
                            state = 'abandoned', updated_at_ms = ?
                        WHERE batch_id = ? AND state = 'staging'
                        """,
                        ("0" * 64, self._now(), batch_id),
                    )

    @staticmethod
    def _remove_empty_staging(path: Path) -> None:
        if not path.exists() or path.is_symlink():
            return
        for child in path.iterdir():
            if child.is_file() and not child.is_symlink():
                child.unlink(missing_ok=True)
        with suppress(OSError):
            path.rmdir()

    def _rows_to_references(
        self, rows: Iterable[sqlite3.Row]
    ) -> tuple[AttachmentReferenceV1, ...]:
        return tuple(
            AttachmentReferenceV1(
                schema_version=1,
                attachment_id=str(row["attachment_id"]),
                ordinal=int(row["ordinal"]),
                content_sha256=str(row["content_sha256"]),
                byte_size=int(row["byte_size"]),
                display_name=str(row["display_name"]),
                media_type=str(row["detected_media_type"]),
            )
            for row in rows
        )

    def _load_batch(
        self, batch_id: str
    ) -> tuple[sqlite3.Row | None, tuple[sqlite3.Row, ...]]:
        with self._lock:
            batch = self._conn.execute(
                "SELECT * FROM attachment_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            rows = self._conn.execute(
                """
                SELECT r.*, b.relative_path
                FROM attachment_records AS r
                JOIN attachment_blobs AS b USING (content_sha256)
                WHERE r.batch_id = ?
                ORDER BY r.ordinal
                """,
                (batch_id,),
            ).fetchall()
        return batch, tuple(rows)

    def _verify_commitment(
        self,
        commitment: AttachmentBatchCommitment,
        batch: sqlite3.Row,
        references: tuple[AttachmentReferenceV1, ...],
    ) -> None:
        if commitment.store_id != self.store_id:
            raise AttachmentIntegrityError("commitment names another Attachment Store")
        if (
            commitment.batch_id != str(batch["batch_id"])
            or commitment.turn_id != str(batch["proposed_turn_id"])
            or commitment.conversation_id != str(batch["proposed_conversation_id"])
            or commitment.record_count != int(batch["record_count"] or 0)
            or commitment.records_sha256 != str(batch["records_sha256"] or "")
            or commitment.record_count != len(references)
            or commitment.records_sha256 != references_sha256(references)
        ):
            raise AttachmentIntegrityError(
                "Attachment batch commitment does not match published Records"
            )

    def accept_batch(
        self, commitment: AttachmentBatchCommitment
    ) -> tuple[AttachmentReferenceV1, ...]:
        with self._lock:
            return self._accept_batch_locked(commitment)

    def _accept_batch_locked(
        self, commitment: AttachmentBatchCommitment
    ) -> tuple[AttachmentReferenceV1, ...]:
        if not isinstance(commitment, AttachmentBatchCommitment):
            raise TypeError("commitment must be AttachmentBatchCommitment")
        batch, rows = self._load_batch(commitment.batch_id)
        if batch is None:
            raise AttachmentIntegrityError("committed Attachment batch is missing")
        references = self._rows_to_references(rows)
        self._verify_commitment(commitment, batch, references)
        state = str(batch["state"])
        if state == "accepted":
            if any(str(row["state"]) == "integrity_failed" for row in rows):
                raise AttachmentIntegrityError(
                    "Attachment batch has a recorded integrity failure"
                )
            try:
                self._verify_reference_rows(rows)
            except AttachmentIntegrityError as exc:
                for row in rows:
                    self._mark_integrity_failed(
                        str(row["attachment_id"]), type(exc).__name__
                    )
                raise
            return references
        if state != "published":
            raise AttachmentIntegrityError(
                f"Attachment batch cannot be accepted from state {state}"
            )
        integrity_error: AttachmentIntegrityError | None = None
        try:
            self._verify_reference_rows(rows)
        except AttachmentIntegrityError as exc:
            integrity_error = exc
        self._checkpoint("before_batch_accept")
        now = self._now()
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT state FROM attachment_batches WHERE batch_id = ?",
                (commitment.batch_id,),
            ).fetchone()
            if current is None or str(current["state"]) != "published":
                raise AttachmentIntegrityError(
                    "Attachment batch left published state before acceptance"
                )
            if integrity_error is None:
                changed_records = connection.execute(
                    "UPDATE attachment_records "
                    "SET state = 'accepted', accepted_at_ms = ? "
                    "WHERE batch_id = ? AND state = 'provisional'",
                    (now, commitment.batch_id),
                ).rowcount
            else:
                changed_records = connection.execute(
                    "UPDATE attachment_records "
                    "SET state = 'integrity_failed', accepted_at_ms = ?, "
                    "integrity_code = ? "
                    "WHERE batch_id = ? AND state = 'provisional'",
                    (now, type(integrity_error).__name__, commitment.batch_id),
                ).rowcount
            if changed_records != len(rows):
                raise AttachmentIntegrityError(
                    "Attachment Records changed before batch acceptance"
                )
            changed_batch = connection.execute(
                "UPDATE attachment_batches SET state = 'accepted', updated_at_ms = ? "
                "WHERE batch_id = ? AND state = 'published'",
                (now, commitment.batch_id),
            ).rowcount
            if changed_batch != 1:
                raise AttachmentIntegrityError(
                    "Attachment batch changed before acceptance"
                )
        self._checkpoint("after_batch_accept")
        if integrity_error is not None:
            raise integrity_error
        return references

    def _verify_reference_rows(self, rows: Iterable[sqlite3.Row]) -> None:
        for row in rows:
            path = self._blob_path(
                str(row["relative_path"]),
                str(row["content_sha256"]),
            )
            self._verify_blob_file(
                path,
                str(row["content_sha256"]),
                int(row["byte_size"]),
            )

    def abandon_batch(self, batch_id: str) -> bool:
        with self._lock:
            return self._abandon_batch_locked(batch_id)

    def _abandon_batch_locked(self, batch_id: str) -> bool:
        if not isinstance(batch_id, str) or len(batch_id) != 32:
            raise ValueError("batch_id must be an opaque ID")
        self._checkpoint("before_batch_abandon")
        changed = False
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM attachment_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None or str(row["state"]) == "abandoned":
                return False
            if str(row["state"]) == "accepted":
                raise AttachmentIntegrityError(
                    "accepted Attachment batch cannot be abandoned"
                )
            connection.execute(
                "UPDATE attachment_records SET state = 'orphaned' "
                "WHERE batch_id = ? AND state = 'provisional'",
                (batch_id,),
            )
            if str(row["state"]) == "staging":
                connection.execute(
                    """
                    UPDATE attachment_batches
                    SET records_sha256 = ?, record_count = 1,
                        state = 'abandoned', updated_at_ms = ?
                    WHERE batch_id = ? AND state = 'staging'
                    """,
                    ("0" * 64, self._now(), batch_id),
                )
            else:
                connection.execute(
                    "UPDATE attachment_batches "
                    "SET state = 'abandoned', updated_at_ms = ? "
                    "WHERE batch_id = ? AND state = 'published'",
                    (self._now(), batch_id),
                )
            changed = True
        self._remove_empty_staging(self.staging_root / batch_id)
        self._checkpoint("after_batch_abandon")
        return changed

    def get_turn_references(
        self, turn_id: str, conversation_id: str
    ) -> tuple[AttachmentReferenceV1, ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT r.* FROM attachment_records AS r
                JOIN attachment_batches AS b USING (batch_id)
                WHERE r.turn_id = ? AND r.conversation_id = ?
                  AND r.state IN ('accepted','integrity_failed')
                  AND b.state = 'accepted'
                ORDER BY r.ordinal
                """,
                (turn_id, conversation_id),
            ).fetchall()
            mismatched = self._conn.execute(
                "SELECT 1 FROM attachment_records WHERE turn_id = ? "
                "AND conversation_id != ? LIMIT 1",
                (turn_id, conversation_id),
            ).fetchone()
        if mismatched is not None:
            raise AttachmentIntegrityError(
                "Attachment Turn belongs to another Conversation"
            )
        return self._rows_to_references(rows)

    def claim_blob_retention(
        self, content_sha256: str, *, holder_kind: str, holder_ref: str
    ) -> str:
        """Durably retain one Blob on behalf of an external governed reference.

        ADR 0059 requires a Blob to survive until no accepted Record, Run input
        or other governed durable reference needs it.  A holder that publishes
        a reference to accepted content -- a Run Manifest input, for example --
        must claim retention *before* publishing, so that garbage collection
        can never observe the window where the Record is gone but the external
        reference already exists.

        Idempotent for one ``(holder_kind, holder_ref, content_sha256)`` triple.
        """

        _require_digest(content_sha256, "content_sha256")
        if holder_kind not in _RETENTION_HOLDER_KINDS:
            raise AttachmentValidationError("unsupported retention holder_kind")
        if (
            not isinstance(holder_ref, str)
            or not holder_ref
            or len(holder_ref) > 255
            or "\x00" in holder_ref
        ):
            raise AttachmentValidationError("holder_ref must be a bounded string")
        with self._transaction() as connection:
            blob = connection.execute(
                "SELECT 1 FROM attachment_blobs WHERE content_sha256 = ?",
                (content_sha256,),
            ).fetchone()
            if blob is None:
                raise AttachmentNotAcceptedError(
                    "cannot retain an Attachment Blob that does not exist"
                )
            existing = connection.execute(
                "SELECT claim_id FROM attachment_blob_retention_claims "
                "WHERE holder_kind = ? AND holder_ref = ? AND content_sha256 = ?",
                (holder_kind, holder_ref, content_sha256),
            ).fetchone()
            if existing is not None:
                return str(existing["claim_id"])
            claim_id = _new_id()
            connection.execute(
                "INSERT INTO attachment_blob_retention_claims ("
                "claim_id, content_sha256, holder_kind, holder_ref, "
                "claim_version, created_at_ms) VALUES (?, ?, ?, ?, 1, ?)",
                (
                    claim_id,
                    content_sha256,
                    holder_kind,
                    holder_ref,
                    self._now(),
                ),
            )
        return claim_id

    def release_blob_retention(self, *, holder_kind: str, holder_ref: str) -> int:
        """Drop every retention claim held by one external reference."""

        if holder_kind not in _RETENTION_HOLDER_KINDS:
            raise AttachmentValidationError("unsupported retention holder_kind")
        with self._transaction() as connection:
            return int(
                connection.execute(
                    "DELETE FROM attachment_blob_retention_claims "
                    "WHERE holder_kind = ? AND holder_ref = ?",
                    (holder_kind, holder_ref),
                ).rowcount
            )

    def blob_retention_holders(
        self, content_sha256: str
    ) -> tuple[tuple[str, str], ...]:
        """Read the ordered ``(holder_kind, holder_ref)`` pairs retaining a Blob."""

        _require_digest(content_sha256, "content_sha256")
        with self._lock:
            rows = self._conn.execute(
                "SELECT holder_kind, holder_ref FROM attachment_blob_retention_claims "
                "WHERE content_sha256 = ? ORDER BY holder_kind, holder_ref",
                (content_sha256,),
            ).fetchall()
        return tuple((str(row["holder_kind"]), str(row["holder_ref"])) for row in rows)

    def resolve_bytes(
        self,
        reference: AttachmentReferenceV1,
        max_bytes: int = DEFAULT_RESOLVE_MAX_BYTES,
    ) -> bytes:
        if not isinstance(reference, AttachmentReferenceV1):
            raise TypeError("reference must be AttachmentReferenceV1")
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT r.*, b.relative_path, x.state AS batch_state
                FROM attachment_records AS r
                JOIN attachment_blobs AS b USING (content_sha256)
                JOIN attachment_batches AS x USING (batch_id)
                WHERE r.attachment_id = ?
                """,
                (reference.attachment_id,),
            ).fetchone()
        if row is None:
            raise AttachmentNotAcceptedError("Attachment Reference is unknown")
        actual_reference = self._rows_to_references((row,))[0]
        if actual_reference != reference:
            raise AttachmentIntegrityError("Attachment Reference metadata mismatch")
        if str(row["state"]) == "integrity_failed":
            raise AttachmentIntegrityError(
                "Attachment Record has a recorded integrity failure"
            )
        if str(row["state"]) != "accepted" or str(row["batch_state"]) != "accepted":
            raise AttachmentNotAcceptedError("Attachment Record is not accepted")
        if reference.byte_size > max_bytes:
            raise AttachmentValidationError("Attachment exceeds resolve byte limit")
        path = self._blob_path(
            str(row["relative_path"]),
            reference.content_sha256,
        )
        try:
            payload = self._read_verified_blob_file(
                path,
                reference.content_sha256,
                reference.byte_size,
                capture=True,
            )
            assert payload is not None
            return payload
        except (OSError, AttachmentIntegrityError) as exc:
            self._mark_integrity_failed(reference.attachment_id, type(exc).__name__)
            if isinstance(exc, AttachmentIntegrityError):
                raise
            raise AttachmentIntegrityError(
                "accepted Attachment Blob is unreadable"
            ) from exc

    def _mark_integrity_failed(self, attachment_id: str, code: str) -> None:
        with suppress(sqlite3.DatabaseError, RuntimeError):
            with self._transaction() as connection:
                connection.execute(
                    "UPDATE attachment_records SET state = 'integrity_failed', "
                    "integrity_code = ? WHERE attachment_id = ? AND state = 'accepted'",
                    (code[:128], attachment_id),
                )

    def reconcile(
        self, commitments: tuple[AttachmentBatchCommitment, ...]
    ) -> AttachmentStoreRecoveryResult:
        with self._lock:
            return self._reconcile_locked(commitments)

    def _reconcile_locked(
        self, commitments: tuple[AttachmentBatchCommitment, ...]
    ) -> AttachmentStoreRecoveryResult:
        """Promote authoritative commitments and abandon expired provisional batches."""

        if not isinstance(commitments, tuple):
            raise TypeError("commitments must be a tuple")
        values = commitments
        by_batch: dict[str, AttachmentBatchCommitment] = {}
        for value in values:
            if not isinstance(value, AttachmentBatchCommitment):
                raise TypeError("commitments must contain AttachmentBatchCommitment")
            if value.batch_id in by_batch:
                raise AttachmentIntegrityError("duplicate Attachment batch commitment")
            by_batch[value.batch_id] = value
        accepted_batch_ids: list[str] = []
        for value in values:
            self.accept_batch(value)
            accepted_batch_ids.append(value.batch_id)
        with self._lock:
            batches = self._conn.execute(
                "SELECT batch_id, state, expires_at_ms FROM attachment_batches"
            ).fetchall()
        now = self._now()
        abandoned_batch_ids: list[str] = []
        for batch in batches:
            batch_id = str(batch["batch_id"])
            state = str(batch["state"])
            if state == "accepted" and batch_id not in by_batch:
                raise AttachmentIntegrityError(
                    "accepted Attachment batch has no authoritative commitment"
                )
            if (
                state in {"staging", "published"}
                and batch_id not in by_batch
                and int(batch["expires_at_ms"]) <= now
            ):
                if self.abandon_batch(batch_id):
                    abandoned_batch_ids.append(batch_id)
        deleted_records, deleted_blobs = self._garbage_collect_orphans()
        return AttachmentStoreRecoveryResult(
            accepted_batch_ids=tuple(accepted_batch_ids),
            abandoned_batch_ids=tuple(abandoned_batch_ids),
            deleted_orphan_record_count=deleted_records,
            deleted_orphan_blob_count=deleted_blobs,
        )

    def _garbage_collect_orphans(self) -> tuple[int, int]:
        grace_threshold = self._now() - self.provisional_grace_ms
        with self._transaction() as connection:
            deleted_records = connection.execute(
                "DELETE FROM attachment_records WHERE state = 'orphaned'"
            ).rowcount
            rows = connection.execute(
                """
                SELECT b.content_sha256, b.relative_path
                FROM attachment_blobs AS b
                WHERE NOT EXISTS (
                    SELECT 1 FROM attachment_records AS r
                    WHERE r.content_sha256 = b.content_sha256
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM attachment_blob_retention_claims AS c
                    WHERE c.content_sha256 = b.content_sha256
                )
                  AND b.created_at_ms <= ?
                """,
                (grace_threshold,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM attachment_blobs WHERE content_sha256 = ?",
                ((str(row["content_sha256"]),) for row in rows),
            )
        for row in rows:
            path = self._blob_path(
                str(row["relative_path"]),
                str(row["content_sha256"]),
            )
            if path.is_symlink():
                raise AttachmentIntegrityError("orphan Attachment Blob is a symlink")
            with suppress(FileNotFoundError):
                path.unlink()
        untracked_count = self._remove_untracked_blobs(grace_threshold)
        self._remove_expired_staging_files()
        return int(deleted_records), len(rows) + untracked_count

    def _remove_untracked_blobs(self, grace_threshold_ms: int) -> int:
        """Remove only old, canonical CAS files that have no database row."""

        self._require_managed_directory(self.blob_root, "Blob root")
        with self._lock:
            known = {
                str(row["relative_path"])
                for row in self._conn.execute(
                    "SELECT relative_path FROM attachment_blobs"
                ).fetchall()
            }
        removed = 0
        for shard in self.blob_root.iterdir():
            if shard.is_symlink() or not shard.is_dir():
                raise AttachmentIntegrityError("Attachment Blob shard is unsafe")
            if len(shard.name) != 2 or any(
                character not in "0123456789abcdef" for character in shard.name
            ):
                raise AttachmentIntegrityError(
                    "Attachment Blob shard name is malformed"
                )
            for path in shard.iterdir():
                if path.is_symlink() or not path.is_file():
                    raise AttachmentIntegrityError("Attachment Blob entry is unsafe")
                if (
                    len(path.name) != 64
                    or not path.name.startswith(shard.name)
                    or any(
                        character not in "0123456789abcdef" for character in path.name
                    )
                ):
                    raise AttachmentIntegrityError(
                        "Attachment Blob filename is malformed"
                    )
                relative = path.relative_to(self.state_root).as_posix()
                if relative in known:
                    continue
                if path.stat().st_mtime_ns // 1_000_000 > grace_threshold_ms:
                    continue
                path.unlink()
                removed += 1
            with suppress(OSError):
                shard.rmdir()
        return removed

    def _remove_expired_staging_files(self) -> None:
        self._require_managed_directory(self.staging_root, "staging root")
        threshold_seconds = (self._now() - self.provisional_grace_ms) / 1_000
        for path in self.staging_root.iterdir():
            if path.is_symlink():
                raise AttachmentIntegrityError("Attachment staging entry is a symlink")
            if path.stat().st_mtime > threshold_seconds:
                continue
            if path.is_dir():
                self._remove_empty_staging(path)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.close()
            finally:
                self._release_lifetime_lock()
                self._closed = True

    def __enter__(self) -> "AttachmentStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "AttachmentError",
    "AttachmentIntegrityError",
    "AttachmentNotAcceptedError",
    "AttachmentRejectedError",
    "AttachmentStore",
    "AttachmentStoreClosedError",
    "AttachmentStoreError",
    "AttachmentStoreRecoveryResult",
    "AttachmentValidationError",
]
