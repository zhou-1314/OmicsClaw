"""Deterministic text content boundary for persistent Channel Delivery.

The renderer freezes only stable references, code-point ranges, and digests in
``control.db``.  The resolver reads only the referenced canonical Transcript
entry; it never invokes the Agent or searches the filesystem for replacement
content.
"""

from __future__ import annotations

import hashlib
from types import MappingProxyType
from typing import Mapping, Protocol

from omicsclaw.runtime.storage.canonical_transcript import (
    CanonicalTranscript,
    TranscriptEntry,
)
from omicsclaw.runtime.storage.transcript_db import dumps_message

from .errors import ControlIntegrityError
from .models import DeliveryItemPlan, DeliveryPlan, TurnTranscriptRef


DEFAULT_TEXT_CHUNK_CODEPOINTS = 4096
DEFAULT_MAX_TEXT_ITEMS = 64
DELIVERY_TEXT_RENDER_VERSION = 1
# Deterministic, sanitized suffix appended to a bounded fallback Item when a
# terminal reply is too long to freeze verbatim.  It is a fixed constant rather
# than Transcript content, so a truncated Item's resolved text is exactly the
# frozen Transcript prefix plus this notice.
DELIVERY_TEXT_TRUNCATION_NOTICE = (
    "\n\n[OmicsClaw: reply truncated — the full response exceeded this channel's "
    "delivery limit.]"
)
_EMPTY_TERMINAL_TEXT = MappingProxyType(
    {
        "succeeded": "Turn completed without a text response.",
        "failed": "Turn failed.",
        "canceled": "Turn canceled.",
        "interrupted": "Turn interrupted.",
    }
)


class DeliveryContentIntegrityError(ControlIntegrityError):
    """A frozen Delivery reference cannot be resolved without guessing."""


class DeliveryContentLimitError(ValueError):
    """A deterministic text plan exceeds its configured hard bound."""


class _DeliveryTextItem(Protocol):
    item_kind: str
    content_store: str
    content_ref: str
    content_sha256: str
    content_range: Mapping[str, object] | None
    render_version: int


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _entry_terminal_kind(entry: TranscriptEntry) -> str:
    entry_terminal_kind = entry.payload.get("terminal_kind")
    terminal_kind = (
        "succeeded"
        if entry_terminal_kind in {"normal", "preflight", "control"}
        else entry_terminal_kind
    )
    if terminal_kind not in _EMPTY_TERMINAL_TEXT:
        raise DeliveryContentIntegrityError(
            "terminal Transcript entry has an unsupported kind"
        )
    return terminal_kind


def _renderable_terminal_text(
    entry: TranscriptEntry,
    terminal_kind: str | None = None,
) -> str:
    derived_terminal_kind = _entry_terminal_kind(entry)
    if terminal_kind is not None and terminal_kind not in _EMPTY_TERMINAL_TEXT:
        raise ValueError(f"unsupported terminal kind: {terminal_kind}")
    if terminal_kind is not None and derived_terminal_kind != terminal_kind:
        raise DeliveryContentIntegrityError(
            "Delivery terminal kind does not match its Transcript entry"
        )
    public_text = entry.payload.get("public_text")
    if not isinstance(public_text, str):
        raise DeliveryContentIntegrityError(
            "terminal Transcript entry has no text payload"
        )
    return (
        public_text
        if public_text.strip()
        else _EMPTY_TERMINAL_TEXT[derived_terminal_kind]
    )


def _verified_terminal_entry(
    transcript: CanonicalTranscript,
    transcript_ref: TurnTranscriptRef,
) -> TranscriptEntry:
    if not isinstance(transcript_ref, TurnTranscriptRef):
        raise TypeError("transcript_ref must be a TurnTranscriptRef")
    try:
        entry = transcript.get_entry(transcript_ref.entry_id)
    except KeyError as exc:
        raise DeliveryContentIntegrityError(
            "Delivery Transcript entry is missing"
        ) from exc
    if entry.entry_kind != "terminal_message" or entry.turn_id is None:
        raise DeliveryContentIntegrityError(
            "Delivery Transcript reference is not a Turn terminal entry"
        )
    if entry.commit_state not in {"terminal_candidate", "committed"}:
        raise DeliveryContentIntegrityError(
            "Delivery Transcript terminal entry is not live"
        )
    if entry.content_sha256 != transcript_ref.content_sha256:
        raise DeliveryContentIntegrityError(
            "Delivery Transcript reference digest mismatch"
        )
    actual_digest = _text_sha256(dumps_message(dict(entry.payload)))
    if actual_digest != entry.content_sha256:
        raise DeliveryContentIntegrityError(
            "Delivery Transcript payload digest mismatch"
        )
    return entry


def freeze_terminal_text_delivery(
    transcript: CanonicalTranscript,
    transcript_ref: TurnTranscriptRef,
    terminal_kind: str,
    *,
    max_chunk_codepoints: int = DEFAULT_TEXT_CHUNK_CODEPOINTS,
    max_items: int = DEFAULT_MAX_TEXT_ITEMS,
) -> DeliveryPlan:
    """Freeze one terminal Transcript entry into deterministic text Items."""

    if (
        isinstance(max_chunk_codepoints, bool)
        or not isinstance(max_chunk_codepoints, int)
        or max_chunk_codepoints < 1
    ):
        raise ValueError("max_chunk_codepoints must be a positive integer")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise ValueError("max_items must be a positive integer")
    entry = _verified_terminal_entry(transcript, transcript_ref)
    text = _renderable_terminal_text(entry, terminal_kind)
    required_items = (len(text) + max_chunk_codepoints - 1) // max_chunk_codepoints
    if required_items > max_items:
        # An unbounded reply cannot be sent verbatim, but terminalization must
        # still produce exactly one durable Delivery rather than fail closed.
        # Freeze a single bounded fallback Item that keeps the start of the
        # reply and appends a deterministic truncation notice.  The full reply
        # stays in the immutable Transcript entry; a future media slice may
        # additionally attach it as a durable artifact reference.
        notice_end = min(
            len(DELIVERY_TEXT_TRUNCATION_NOTICE),
            max_chunk_codepoints,
        )
        prefix_budget = max_chunk_codepoints - notice_end
        prefix_len = min(len(text), prefix_budget)
        fallback_text = (
            text[:prefix_len] + DELIVERY_TEXT_TRUNCATION_NOTICE[:notice_end]
        )
        fallback_item = DeliveryItemPlan(
            item_kind="text",
            content_store="transcript",
            content_ref=transcript_ref.entry_id,
            content_sha256=_text_sha256(fallback_text),
            content_range=MappingProxyType(
                {
                    "unit": "unicode_codepoint",
                    "start": 0,
                    "end": prefix_len,
                    "truncated": True,
                    "notice_end": notice_end,
                }
            ),
            render_version=DELIVERY_TEXT_RENDER_VERSION,
        )
        return DeliveryPlan(terminal_kind=terminal_kind, items=(fallback_item,))
    items = tuple(
        DeliveryItemPlan(
            item_kind="text",
            content_store="transcript",
            content_ref=transcript_ref.entry_id,
            content_sha256=_text_sha256(text[start:end]),
            content_range=MappingProxyType(
                {"unit": "unicode_codepoint", "start": start, "end": end}
            ),
            render_version=DELIVERY_TEXT_RENDER_VERSION,
        )
        for start in range(0, len(text), max_chunk_codepoints)
        for end in (min(start + max_chunk_codepoints, len(text)),)
    )
    return DeliveryPlan(terminal_kind=terminal_kind, items=items)


def resolve_delivery_text(
    transcript: CanonicalTranscript,
    item: _DeliveryTextItem,
) -> str:
    """Resolve and verify one frozen Transcript text Item exactly.

    Missing content, unsupported render versions, ambiguous ranges, and digest
    mismatches fail closed.  This boundary intentionally has no filesystem,
    provider, or Agent fallback.
    """

    try:
        item_kind = item.item_kind
        content_store = item.content_store
        content_ref = item.content_ref
        expected_digest = item.content_sha256
        content_range = item.content_range
        render_version = item.render_version
    except AttributeError as exc:
        raise DeliveryContentIntegrityError(
            "Delivery text Item is missing required frozen fields"
        ) from exc
    if item_kind != "text":
        raise DeliveryContentIntegrityError("Delivery resolver rejects media Items")
    if content_store != "transcript":
        raise DeliveryContentIntegrityError(
            "Delivery text Item does not reference the Transcript store"
        )
    if not isinstance(content_ref, str) or not content_ref:
        raise DeliveryContentIntegrityError("Delivery Transcript reference is missing")
    if render_version != DELIVERY_TEXT_RENDER_VERSION:
        raise DeliveryContentIntegrityError("unsupported Delivery text render version")
    _required_range_keys = {"unit", "start", "end"}
    if not isinstance(content_range, Mapping) or set(content_range) not in (
        _required_range_keys,
        _required_range_keys | {"truncated"},
        _required_range_keys | {"truncated", "notice_end"},
    ):
        raise DeliveryContentIntegrityError(
            "Delivery text Item has an invalid codepoint range"
        )
    unit = content_range.get("unit")
    start = content_range.get("start")
    end = content_range.get("end")
    truncated = content_range.get("truncated", False)
    has_notice_end = "notice_end" in content_range
    notice_end = content_range.get("notice_end")
    if (
        unit != "unicode_codepoint"
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (start, end)
        )
        or not isinstance(truncated, bool)
        or (
            has_notice_end
            and (
                not truncated
                or isinstance(notice_end, bool)
                or not isinstance(notice_end, int)
                or notice_end < 0
                or notice_end > len(DELIVERY_TEXT_TRUNCATION_NOTICE)
            )
        )
    ):
        raise DeliveryContentIntegrityError(
            "Delivery text Item has an invalid codepoint range"
        )
    assert isinstance(start, int) and isinstance(end, int)

    try:
        entry = transcript.get_entry(content_ref)
    except KeyError as exc:
        raise DeliveryContentIntegrityError(
            "Delivery Transcript entry is missing"
        ) from exc
    if (
        entry.entry_kind != "terminal_message"
        or entry.turn_id is None
        or entry.commit_state != "committed"
    ):
        raise DeliveryContentIntegrityError(
            "Delivery Transcript reference is not a committed Turn terminal entry"
        )
    actual_entry_digest = _text_sha256(dumps_message(dict(entry.payload)))
    if actual_entry_digest != entry.content_sha256:
        raise DeliveryContentIntegrityError(
            "Delivery Transcript payload digest mismatch"
        )

    text = _renderable_terminal_text(entry)
    # A verbatim Item requires a non-empty slice; a bounded fallback Item may
    # freeze an empty prefix (start == end) because it still resolves to the
    # deterministic truncation notice.
    range_ok = (start <= end) if truncated else (start < end)
    if start < 0 or not range_ok or end > len(text):
        raise DeliveryContentIntegrityError(
            "Delivery text Item codepoint range is out of bounds"
        )
    resolved = text[start:end]
    if truncated:
        resolved = resolved + DELIVERY_TEXT_TRUNCATION_NOTICE[
            : notice_end if has_notice_end else len(DELIVERY_TEXT_TRUNCATION_NOTICE)
        ]
    if (
        not isinstance(expected_digest, str)
        or _text_sha256(resolved) != expected_digest
    ):
        raise DeliveryContentIntegrityError("Delivery text Item digest mismatch")
    return resolved


__all__ = [
    "DEFAULT_MAX_TEXT_ITEMS",
    "DEFAULT_TEXT_CHUNK_CODEPOINTS",
    "DELIVERY_TEXT_RENDER_VERSION",
    "DELIVERY_TEXT_TRUNCATION_NOTICE",
    "DeliveryContentIntegrityError",
    "DeliveryContentLimitError",
    "freeze_terminal_text_delivery",
    "resolve_delivery_text",
]
