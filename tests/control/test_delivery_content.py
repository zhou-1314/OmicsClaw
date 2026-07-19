from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from omicsclaw.control.delivery_content import (
    DELIVERY_TEXT_RENDER_VERSION,
    DeliveryContentIntegrityError,
    DeliveryContentLimitError,
    freeze_terminal_text_delivery,
    resolve_delivery_text,
)
from omicsclaw.control.models import DeliveryCandidate, TurnTranscriptRef
from omicsclaw.runtime.storage.canonical_transcript import CanonicalTranscript


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidate_from_plan(item, *, ordinal: int = 0) -> DeliveryCandidate:
    return DeliveryCandidate(
        delivery_id="delivery-1",
        item_id=f"item-{ordinal}",
        surface="channel",
        reply_target_key="target-1",
        reply_target={"schema_version": 1, "platform": "telegram", "chat_id": "1"},
        target_sequence=1,
        ordinal=ordinal,
        item_kind=item.item_kind,
        content_store=item.content_store,
        content_ref=item.content_ref,
        content_sha256=item.content_sha256,
        content_range=item.content_range,
        render_version=item.render_version,
        media_type=item.media_type,
        caption_ref=item.caption_ref,
        caption_sha256=item.caption_sha256,
        attempt_count=0,
    )


def test_freeze_terminal_text_delivery_references_exact_candidate_text(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    try:
        candidate = transcript.bind_turn("conversation-1", "turn-1").stage_terminal(
            "hello, channel",
            terminal_kind="normal",
        )

        plan = freeze_terminal_text_delivery(
            transcript,
            TurnTranscriptRef(candidate.entry_id, candidate.content_sha256),
            "succeeded",
        )

        assert plan.terminal_kind == "succeeded"
        assert len(plan.items) == 1
        item = plan.items[0]
        assert item.item_kind == "text"
        assert item.content_store == "transcript"
        assert item.content_ref == candidate.entry_id
        assert item.content_range == {
            "unit": "unicode_codepoint",
            "start": 0,
            "end": 14,
        }
        assert item.content_sha256 == _sha256_text("hello, channel")
        assert item.render_version == DELIVERY_TEXT_RENDER_VERSION
    finally:
        transcript.close()


def test_freeze_terminal_text_delivery_chunks_by_unicode_codepoint(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    try:
        text = "A😀BC界"
        candidate = transcript.bind_turn("conversation-1", "turn-1").stage_terminal(
            text,
            terminal_kind="normal",
        )

        plan = freeze_terminal_text_delivery(
            transcript,
            TurnTranscriptRef(candidate.entry_id, candidate.content_sha256),
            "succeeded",
            max_chunk_codepoints=2,
        )

        assert [dict(item.content_range or {}) for item in plan.items] == [
            {"unit": "unicode_codepoint", "start": 0, "end": 2},
            {"unit": "unicode_codepoint", "start": 2, "end": 4},
            {"unit": "unicode_codepoint", "start": 4, "end": 5},
        ]
        assert [item.content_sha256 for item in plan.items] == [
            _sha256_text("A😀"),
            _sha256_text("BC"),
            _sha256_text("界"),
        ]
    finally:
        transcript.close()


@pytest.mark.parametrize(
    ("terminal_kind", "entry_kind", "expected"),
    [
        ("succeeded", "normal", "Turn completed without a text response."),
        ("failed", "failed", "Turn failed."),
        ("canceled", "canceled", "Turn canceled."),
        ("interrupted", "interrupted", "Turn interrupted."),
    ],
)
def test_freeze_terminal_text_delivery_uses_sanitized_empty_fallback(
    tmp_path,
    terminal_kind,
    entry_kind,
    expected,
):
    transcript = CanonicalTranscript(tmp_path)
    try:
        candidate = transcript.bind_turn("conversation-1", "turn-1").stage_terminal(
            " \n\t",
            terminal_kind=entry_kind,
        )

        plan = freeze_terminal_text_delivery(
            transcript,
            TurnTranscriptRef(candidate.entry_id, candidate.content_sha256),
            terminal_kind,
        )

        assert len(plan.items) == 1
        assert plan.items[0].content_range == {
            "unit": "unicode_codepoint",
            "start": 0,
            "end": len(expected),
        }
        assert plan.items[0].content_sha256 == _sha256_text(expected)
        assert len(expected) <= 4096
    finally:
        transcript.close()


def test_freeze_terminal_text_delivery_rejects_tampered_transcript_ref(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    try:
        candidate = transcript.bind_turn("conversation-1", "turn-1").stage_terminal(
            "verified",
            terminal_kind="normal",
        )

        with pytest.raises(DeliveryContentIntegrityError, match="digest"):
            freeze_terminal_text_delivery(
                transcript,
                TurnTranscriptRef(candidate.entry_id, "0" * 64),
                "succeeded",
            )
    finally:
        transcript.close()


def test_freeze_terminal_text_delivery_rejects_plan_over_item_bound(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    try:
        candidate = transcript.bind_turn("conversation-1", "turn-1").stage_terminal(
            "abcde",
            terminal_kind="normal",
        )

        with pytest.raises(DeliveryContentLimitError, match="Item limit"):
            freeze_terminal_text_delivery(
                transcript,
                TurnTranscriptRef(candidate.entry_id, candidate.content_sha256),
                "succeeded",
                max_chunk_codepoints=2,
                max_items=2,
            )
    finally:
        transcript.close()


def test_resolve_delivery_text_returns_exact_frozen_codepoint_slice(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    try:
        candidate_ref = transcript.bind_turn("conversation-1", "turn-1").stage_terminal(
            "A😀BC界", terminal_kind="normal"
        )
        ref = TurnTranscriptRef(candidate_ref.entry_id, candidate_ref.content_sha256)
        plan = freeze_terminal_text_delivery(
            transcript,
            ref,
            "succeeded",
            max_chunk_codepoints=2,
        )
        transcript.promote_terminal(
            ref.entry_id,
            ref.content_sha256,
            expected_conversation_id="conversation-1",
            expected_turn_id="turn-1",
        )

        assert (
            resolve_delivery_text(transcript, _candidate_from_plan(plan.items[0]))
            == "A😀"
        )
    finally:
        transcript.close()


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"item_kind": "media"}, "media"),
        ({"content_store": "run_artifact"}, "Transcript store"),
        ({"content_ref": "missing-entry"}, "entry is missing"),
        ({"content_sha256": "0" * 64}, "Item digest"),
        ({"render_version": 2}, "render version"),
        (
            {
                "content_range": {
                    "unit": "unicode_codepoint",
                    "start": 0,
                    "end": 99,
                }
            },
            "out of bounds",
        ),
    ],
)
def test_resolve_delivery_text_fails_closed_for_invalid_item(
    tmp_path,
    changes,
    error,
):
    transcript = CanonicalTranscript(tmp_path)
    try:
        candidate_ref = transcript.bind_turn("conversation-1", "turn-1").stage_terminal(
            "verified", terminal_kind="normal"
        )
        ref = TurnTranscriptRef(candidate_ref.entry_id, candidate_ref.content_sha256)
        plan = freeze_terminal_text_delivery(transcript, ref, "succeeded")
        transcript.promote_terminal(
            ref.entry_id,
            ref.content_sha256,
            expected_conversation_id="conversation-1",
            expected_turn_id="turn-1",
        )
        item = replace(_candidate_from_plan(plan.items[0]), **changes)

        with pytest.raises(DeliveryContentIntegrityError, match=error):
            resolve_delivery_text(transcript, item)
    finally:
        transcript.close()


def test_resolve_delivery_text_recomputes_sanitized_empty_fallback(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    try:
        candidate_ref = transcript.bind_turn("conversation-1", "turn-1").stage_terminal(
            "\n", terminal_kind="interrupted"
        )
        ref = TurnTranscriptRef(candidate_ref.entry_id, candidate_ref.content_sha256)
        plan = freeze_terminal_text_delivery(transcript, ref, "interrupted")
        transcript.promote_terminal(
            ref.entry_id,
            ref.content_sha256,
            expected_conversation_id="conversation-1",
            expected_turn_id="turn-1",
        )

        assert (
            resolve_delivery_text(transcript, _candidate_from_plan(plan.items[0]))
            == "Turn interrupted."
        )
    finally:
        transcript.close()


def test_default_delivery_chunk_bound_is_telegram_safe(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    try:
        text = "x" * 4097
        candidate = transcript.bind_turn("conversation-1", "turn-1").stage_terminal(
            text,
            terminal_kind="normal",
        )

        plan = freeze_terminal_text_delivery(
            transcript,
            TurnTranscriptRef(candidate.entry_id, candidate.content_sha256),
            "succeeded",
        )

        assert [dict(item.content_range or {}) for item in plan.items] == [
            {"unit": "unicode_codepoint", "start": 0, "end": 4096},
            {"unit": "unicode_codepoint", "start": 4096, "end": 4097},
        ]
    finally:
        transcript.close()


def test_resolve_delivery_text_rejects_item_missing_frozen_fields(tmp_path):
    transcript = CanonicalTranscript(tmp_path)
    try:
        with pytest.raises(DeliveryContentIntegrityError, match="required frozen"):
            resolve_delivery_text(transcript, object())  # type: ignore[arg-type]
    finally:
        transcript.close()
