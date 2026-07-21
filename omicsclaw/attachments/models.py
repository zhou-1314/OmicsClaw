"""Immutable public contracts for the Attachment Store Module."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import AsyncIterator, Awaitable, Protocol, runtime_checkable


_LOWER_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_LOWER_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,512}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")


def _require_schema_v1(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value != 1:
        raise ValueError("schema_version must be 1")


def _require_opaque_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _LOWER_HEX_32.fullmatch(value) is None:
        raise ValueError(f"{name} must be 32 lowercase hexadecimal characters")
    return value


def _require_digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _LOWER_HEX_64.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _require_uint(value: object, name: str, *, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > maximum
    ):
        raise ValueError(f"{name} must be an unsigned integer <= {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class SourceAttachmentDescriptorV1:
    """Side-effect-free description used for fingerprinting before byte access."""

    schema_version: int
    ordinal: int
    source_attachment_id: str
    display_name: str
    declared_media_type: str | None
    declared_size: int | None
    declared_sha256: str | None

    def __post_init__(self) -> None:
        _require_schema_v1(self.schema_version)
        _require_uint(self.ordinal, "ordinal", maximum=0xFFFFFFFF)
        if (
            not isinstance(self.source_attachment_id, str)
            or _SOURCE_ID.fullmatch(self.source_attachment_id) is None
            or "://" in self.source_attachment_id
            or self.source_attachment_id in {".", ".."}
        ):
            raise ValueError("source_attachment_id has an unsafe or unsupported shape")
        if (
            not isinstance(self.display_name, str)
            or not self.display_name
            or len(self.display_name.encode("utf-8")) > 255
            or "\x00" in self.display_name
            or "/" in self.display_name
            or "\\" in self.display_name
            or self.display_name in {".", ".."}
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.display_name
            )
        ):
            raise ValueError("display_name must be a safe bounded filename")
        if self.declared_media_type is not None:
            if (
                not isinstance(self.declared_media_type, str)
                or _MEDIA_TYPE.fullmatch(self.declared_media_type) is None
            ):
                raise ValueError("declared_media_type must be a canonical media type")
        if self.declared_size is not None:
            _require_uint(
                self.declared_size,
                "declared_size",
                maximum=0xFFFFFFFFFFFFFFFF,
            )
        if self.declared_sha256 is not None:
            _require_digest(self.declared_sha256, "declared_sha256")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "ordinal": self.ordinal,
            "source_attachment_id": self.source_attachment_id,
            "display_name": self.display_name,
            "declared_media_type": self.declared_media_type,
            "declared_size": self.declared_size,
            "declared_sha256": self.declared_sha256,
        }


@dataclass(frozen=True, slots=True)
class AttachmentReferenceV1:
    """Versioned immutable reference carried by accepted Envelopes/Transcripts."""

    schema_version: int
    attachment_id: str
    ordinal: int
    content_sha256: str
    byte_size: int
    display_name: str
    media_type: str

    def __post_init__(self) -> None:
        _require_schema_v1(self.schema_version)
        _require_opaque_id(self.attachment_id, "attachment_id")
        _require_uint(self.ordinal, "ordinal", maximum=0xFFFFFFFF)
        _require_digest(self.content_sha256, "content_sha256")
        _require_uint(self.byte_size, "byte_size", maximum=0xFFFFFFFFFFFFFFFF)
        if (
            not isinstance(self.display_name, str)
            or not self.display_name
            or len(self.display_name.encode("utf-8")) > 255
            or any(marker in self.display_name for marker in ("\x00", "/", "\\"))
            or self.display_name in {".", ".."}
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.display_name
            )
        ):
            raise ValueError("display_name must be a safe bounded filename")
        if (
            not isinstance(self.media_type, str)
            or _MEDIA_TYPE.fullmatch(self.media_type) is None
        ):
            raise ValueError("media_type must be canonical")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "attachment_id": self.attachment_id,
            "ordinal": self.ordinal,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "display_name": self.display_name,
            "media_type": self.media_type,
        }


def references_sha256(references: tuple[AttachmentReferenceV1, ...]) -> str:
    payload = [reference.to_json_dict() for reference in references]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AttachmentBatchCommitment:
    """Content-free cross-store proof for one published attachment batch."""

    schema_version: int
    store_id: str
    batch_id: str
    turn_id: str
    conversation_id: str
    record_count: int
    records_sha256: str

    def __post_init__(self) -> None:
        _require_schema_v1(self.schema_version)
        _require_opaque_id(self.store_id, "store_id")
        _require_opaque_id(self.batch_id, "batch_id")
        _require_opaque_id(self.turn_id, "turn_id")
        _require_opaque_id(self.conversation_id, "conversation_id")
        if (
            not isinstance(self.record_count, int)
            or isinstance(self.record_count, bool)
            or self.record_count <= 0
            or self.record_count > 0xFFFFFFFF
        ):
            raise ValueError("record_count must be a positive unsigned integer")
        _require_digest(self.records_sha256, "records_sha256")


@dataclass(frozen=True, slots=True)
class AttachmentBatchPublication:
    commitment: AttachmentBatchCommitment
    references: tuple[AttachmentReferenceV1, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "references", tuple(self.references))
        if len(self.references) != self.commitment.record_count:
            raise ValueError("publication reference count does not match commitment")
        if tuple(reference.ordinal for reference in self.references) != tuple(
            range(len(self.references))
        ):
            raise ValueError("publication references must have contiguous ordinals")
        if references_sha256(self.references) != self.commitment.records_sha256:
            raise ValueError("publication references do not match commitment digest")


@runtime_checkable
class InboundAttachmentSource(Protocol):
    """Process-local byte capability; implementations are normally async generators."""

    def open(
        self, source_attachment_id: str
    ) -> AsyncIterator[bytes] | Awaitable[AsyncIterator[bytes]]: ...


__all__ = [
    "AttachmentBatchCommitment",
    "AttachmentBatchPublication",
    "AttachmentReferenceV1",
    "InboundAttachmentSource",
    "SourceAttachmentDescriptorV1",
    "references_sha256",
]
