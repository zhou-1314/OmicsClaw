"""Ephemeral provider rendering for durable Attachment References.

The canonical Transcript contains only ``attachment_ref`` blocks.  This
adapter resolves accepted immutable bytes immediately before a model call and
reverses its own marker/image pairs before any history replacement.  Base64
image data is therefore a process-local capability, never durable state.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
from typing import Any

from .models import AttachmentReferenceV1
from .store import AttachmentIntegrityError, AttachmentStoreError


_ALLOWED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
_ATTACHMENT_BLOCK_KEYS = frozenset({"type", "attachment"})
_REFERENCE_KEYS = frozenset(
    {
        "schema_version",
        "attachment_id",
        "ordinal",
        "content_sha256",
        "byte_size",
        "display_name",
        "media_type",
    }
)
_MARKER_PREFIX = "[[OMICSCLAW_ATTACHMENT_V1:"
_MARKER_SUFFIX = "]]"
_DATA_URI_PATTERN = re.compile(r"data:[^\s,]*,", re.IGNORECASE)
_PROVIDER_ATTACHMENT_BLOCK_TYPES = frozenset({"image", "image_url", "input_image"})
DEFAULT_MAX_RENDER_IMAGES = 8
DEFAULT_MAX_RENDER_BYTES = 50 * 1024 * 1024


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _durable_block(reference: AttachmentReferenceV1) -> dict[str, object]:
    return {
        "type": "attachment_ref",
        "attachment": reference.to_json_dict(),
    }


def _marker(reference: AttachmentReferenceV1) -> str:
    # The canonical JSON contains both the opaque Attachment ID and its digest.
    # Re-emitting and byte-comparing this representation during parsing rejects
    # alternate encodings that could make a marker ambiguous.
    return _MARKER_PREFIX + _canonical_json(reference.to_json_dict()) + _MARKER_SUFFIX


def _reference_from_payload(payload: object) -> AttachmentReferenceV1:
    if not isinstance(payload, dict) or frozenset(payload) != _REFERENCE_KEYS:
        raise AttachmentIntegrityError(
            "attachment_ref must contain one exact AttachmentReferenceV1 payload"
        )
    try:
        return AttachmentReferenceV1(**payload)
    except (TypeError, ValueError) as exc:
        raise AttachmentIntegrityError("attachment_ref payload is malformed") from exc


def _reference_from_block(block: object) -> AttachmentReferenceV1:
    if (
        not isinstance(block, dict)
        or frozenset(block) != _ATTACHMENT_BLOCK_KEYS
        or block.get("type") != "attachment_ref"
    ):
        raise AttachmentIntegrityError("attachment_ref block has an invalid shape")
    return _reference_from_payload(block.get("attachment"))


def _reference_from_marker(value: str) -> AttachmentReferenceV1:
    if not value.startswith(_MARKER_PREFIX) or not value.endswith(_MARKER_SUFFIX):
        raise AttachmentIntegrityError("Attachment provider marker is malformed")
    encoded = value[len(_MARKER_PREFIX) : -len(_MARKER_SUFFIX)]
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AttachmentIntegrityError(
            "Attachment provider marker is malformed"
        ) from exc
    reference = _reference_from_payload(payload)
    if not hmac.compare_digest(value, _marker(reference)):
        raise AttachmentIntegrityError("Attachment provider marker is not canonical")
    return reference


def _is_marker_block(block: object) -> bool:
    return (
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].startswith(_MARKER_PREFIX)
    )


def _data_uri_for(reference: AttachmentReferenceV1, payload: bytes) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{reference.media_type};base64,{encoded}"


def _contains_data_uri(value: object) -> bool:
    """Detect any data URI recursively, including text copied by a model."""

    if isinstance(value, str):
        return _DATA_URI_PATTERN.search(value) is not None
    if isinstance(value, dict):
        return any(_contains_data_uri(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_data_uri(item) for item in value)
    return False


def _contains_reserved_marker(value: object) -> bool:
    if isinstance(value, str):
        return _MARKER_PREFIX in value
    if isinstance(value, dict):
        return any(_contains_reserved_marker(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_reserved_marker(item) for item in value)
    return False


def _is_unmarked_provider_attachment(block: object) -> bool:
    return (
        isinstance(block, dict)
        and block.get("type") in _PROVIDER_ATTACHMENT_BLOCK_TYPES
    )


class AttachmentContentAdapter:
    """Render accepted image References for providers and restore them safely."""

    def __init__(
        self,
        store: object,
        *,
        max_render_images: int = DEFAULT_MAX_RENDER_IMAGES,
        max_render_bytes: int = DEFAULT_MAX_RENDER_BYTES,
    ) -> None:
        resolver = getattr(store, "resolve_bytes", None)
        if not callable(resolver):
            raise TypeError("store must expose a callable resolve_bytes(reference)")
        for name, value in (
            ("max_render_images", max_render_images),
            ("max_render_bytes", max_render_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._store = store
        self._max_render_images = max_render_images
        self._max_render_bytes = max_render_bytes

    def _preflight_render(self, messages: list[dict[str, Any]]) -> None:
        """Validate the complete provider payload budget before byte access."""

        image_count = 0
        byte_count = 0
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                if _contains_data_uri(content):
                    raise AttachmentIntegrityError(
                        "unmarked data URI cannot enter provider message history"
                    )
                if _contains_reserved_marker(content):
                    raise AttachmentIntegrityError(
                        "reserved Attachment marker appeared in durable history"
                    )
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "attachment_ref":
                    reference = _reference_from_block(block)
                    if reference.media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
                        raise AttachmentIntegrityError(
                            "Attachment Reference is not an accepted provider image type"
                        )
                    image_count += 1
                    byte_count += reference.byte_size
                    if image_count > self._max_render_images:
                        raise AttachmentIntegrityError(
                            "Attachment provider render exceeds its image-count limit"
                        )
                    if byte_count > self._max_render_bytes:
                        raise AttachmentIntegrityError(
                            "Attachment provider render exceeds its aggregate byte limit"
                        )
                    continue
                if _is_marker_block(block):
                    raise AttachmentIntegrityError(
                        "reserved Attachment marker appeared in durable history"
                    )
                if _contains_reserved_marker(block):
                    raise AttachmentIntegrityError(
                        "reserved Attachment marker appeared in durable history"
                    )
                if _contains_data_uri(block):
                    raise AttachmentIntegrityError(
                        "unmarked data URI appeared in durable history"
                    )
                if _is_unmarked_provider_attachment(block):
                    raise AttachmentIntegrityError(
                        "unmarked provider Attachment block appeared in durable history"
                    )

    def _resolve_verified(self, reference: AttachmentReferenceV1) -> bytes:
        if reference.media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
            raise AttachmentIntegrityError(
                "Attachment Reference is not an accepted provider image type"
            )
        try:
            payload = self._store.resolve_bytes(reference)
        except AttachmentIntegrityError:
            raise
        except (AttachmentStoreError, TypeError, ValueError) as exc:
            # Unknown, provisional, or metadata-mismatched References are all
            # integrity failures at the durable-to-provider seam.
            raise AttachmentIntegrityError(
                "Attachment Reference cannot be resolved as accepted content"
            ) from exc
        if not isinstance(payload, bytes):
            raise AttachmentIntegrityError("Attachment Store returned non-byte content")
        if len(payload) != reference.byte_size:
            raise AttachmentIntegrityError("resolved Attachment size mismatch")
        actual_digest = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(actual_digest, reference.content_sha256):
            raise AttachmentIntegrityError("resolved Attachment digest mismatch")
        return payload

    @staticmethod
    def _copy_messages(messages: list[dict]) -> list[dict[str, Any]]:
        if not isinstance(messages, list) or any(
            not isinstance(message, dict) for message in messages
        ):
            raise AttachmentIntegrityError("messages must be a list of mappings")
        return copy.deepcopy(messages)

    def render_messages(self, messages: list[dict]) -> list[dict]:
        """Replace durable Reference blocks with ephemeral marker/image pairs."""

        rendered = self._copy_messages(messages)
        self._preflight_render(rendered)
        for message in rendered:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            provider_blocks: list[object] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "attachment_ref":
                    reference = _reference_from_block(block)
                    payload = self._resolve_verified(reference)
                    provider_blocks.extend(
                        (
                            {"type": "text", "text": _marker(reference)},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": _data_uri_for(reference, payload),
                                },
                            },
                        )
                    )
                    continue
                provider_blocks.append(block)
            message["content"] = provider_blocks
        return rendered

    def restore_messages(self, messages: list[dict]) -> list[dict]:
        """Restore only exact adjacent marker/image pairs to durable References."""

        restored = self._copy_messages(messages)
        for message in restored:
            content = message.get("content")
            if not isinstance(content, list):
                if _contains_data_uri(content):
                    raise AttachmentIntegrityError(
                        "unmarked data URI cannot be persisted"
                    )
                if _contains_reserved_marker(content):
                    raise AttachmentIntegrityError(
                        "reserved Attachment marker cannot be persisted"
                    )
                continue
            durable_blocks: list[object] = []
            index = 0
            while index < len(content):
                block = content[index]
                if _is_marker_block(block):
                    if frozenset(block) != frozenset({"type", "text"}):
                        raise AttachmentIntegrityError(
                            "Attachment provider marker block has extra fields"
                        )
                    reference = _reference_from_marker(block["text"])
                    if index + 1 >= len(content):
                        raise AttachmentIntegrityError(
                            "Attachment provider marker has no image"
                        )
                    image_block = content[index + 1]
                    if (
                        not isinstance(image_block, dict)
                        or frozenset(image_block) != frozenset({"type", "image_url"})
                        or image_block.get("type") != "image_url"
                        or not isinstance(image_block.get("image_url"), dict)
                        or frozenset(image_block["image_url"]) != frozenset({"url"})
                        or not isinstance(image_block["image_url"].get("url"), str)
                    ):
                        raise AttachmentIntegrityError(
                            "Attachment provider marker is not followed by its image"
                        )
                    payload = self._resolve_verified(reference)
                    expected_uri = _data_uri_for(reference, payload)
                    if not hmac.compare_digest(
                        image_block["image_url"]["url"], expected_uri
                    ):
                        raise AttachmentIntegrityError(
                            "Attachment provider image does not match its marker"
                        )
                    durable_blocks.append(_durable_block(reference))
                    index += 2
                    continue
                if isinstance(block, dict) and block.get("type") == "attachment_ref":
                    # A compaction implementation may retain a durable block
                    # untouched.  Revalidate it before allowing persistence.
                    reference = _reference_from_block(block)
                    self._resolve_verified(reference)
                    durable_blocks.append(_durable_block(reference))
                    index += 1
                    continue
                if _contains_reserved_marker(block):
                    raise AttachmentIntegrityError(
                        "reserved Attachment marker cannot be persisted"
                    )
                if _contains_data_uri(block):
                    raise AttachmentIntegrityError(
                        "unmarked data URI cannot be persisted"
                    )
                if _is_unmarked_provider_attachment(block):
                    raise AttachmentIntegrityError(
                        "unmarked provider Attachment block cannot be persisted"
                    )
                durable_blocks.append(block)
                index += 1
            message["content"] = durable_blocks
        return restored


__all__ = [
    "AttachmentContentAdapter",
    "DEFAULT_MAX_RENDER_BYTES",
    "DEFAULT_MAX_RENDER_IMAGES",
]
