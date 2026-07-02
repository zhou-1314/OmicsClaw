"""Tests for the SSE compaction-status payload helper."""
from __future__ import annotations

from omicsclaw.runtime.context.compaction import (
    CompactionEvent,
    build_compaction_status_payload,
)


def test_event_dataclass_round_trip():
    event = CompactionEvent(
        messages_compressed=12,
        tokens_saved_estimate=3400,
        applied_stages=("auto_compact",),
    )
    assert event.messages_compressed == 12
    assert event.tokens_saved_estimate == 3400
    assert event.applied_stages == ("auto_compact",)


def test_payload_with_tokens_saved():
    event = CompactionEvent(
        messages_compressed=12,
        tokens_saved_estimate=3400,
        applied_stages=("auto_compact",),
    )
    payload = build_compaction_status_payload(event)
    assert payload == {
        "notification": True,
        "subtype": "context_compressed",
        "message": (
            "Context compressed: 12 older messages summarized, "
            "~3,400 tokens saved"
        ),
        "stats": {"messagesCompressed": 12, "tokensSaved": 3400},
    }


def test_payload_without_tokens_saved_omits_token_clause():
    event = CompactionEvent(
        messages_compressed=4,
        tokens_saved_estimate=0,
        applied_stages=("snip_compact",),
    )
    payload = build_compaction_status_payload(event)
    assert payload["message"] == "Context compressed: 4 older messages summarized"
    assert payload["stats"]["tokensSaved"] == 0


def test_payload_for_zero_message_compaction_uses_generic_wording():
    event = CompactionEvent(
        messages_compressed=0,
        tokens_saved_estimate=8500,
        applied_stages=("snip_compact",),
    )
    payload = build_compaction_status_payload(event)
    assert payload["message"] == (
        "Context compressed: prompt context trimmed, ~8,500 tokens saved"
    )
    assert "0 older messages" not in payload["message"]
    assert payload["stats"] == {"messagesCompressed": 0, "tokensSaved": 8500}


def test_payload_subtype_is_stable_constant():
    """Frontend dispatch keys on this exact string — must not drift."""
    event = CompactionEvent(messages_compressed=1, tokens_saved_estimate=1, applied_stages=())
    assert build_compaction_status_payload(event)["subtype"] == "context_compressed"


def test_event_defaults_budget_status_to_none():
    # B3: the budget-status fields are optional (back-compat with 3-arg
    # constructions and dataclasses.asdict consumers).
    event = CompactionEvent(messages_compressed=1, tokens_saved_estimate=1, applied_stages=())
    assert event.budget_status is None
    assert event.local_budget_status is None


def test_payload_includes_budget_status_when_present():
    # B3: surface the already-computed context-budget pressure to the SSE toast.
    from omicsclaw.runtime.context.budget import ContextBudgetStatus

    event = CompactionEvent(
        messages_compressed=5,
        tokens_saved_estimate=100,
        applied_stages=("context_collapse",),
        budget_status=ContextBudgetStatus.WARNING,
        local_budget_status=ContextBudgetStatus.COMPRESS,
    )
    payload = build_compaction_status_payload(event)
    # Plain string enum VALUES — JSON contract independent of the str-Enum impl.
    assert payload["budgetStatus"] == "warning"
    assert payload["localBudgetStatus"] == "compress"


def test_payload_omits_budget_status_when_none():
    # Back-compat: None statuses add no keys, so the exact-equality payload
    # contract (test_payload_with_tokens_saved) stays byte-identical.
    event = CompactionEvent(
        messages_compressed=5,
        tokens_saved_estimate=100,
        applied_stages=("context_collapse",),
    )
    payload = build_compaction_status_payload(event)
    assert "budgetStatus" not in payload
    assert "localBudgetStatus" not in payload


def test_payload_accepts_plain_string_status():
    # Across the dispatcher's dataclasses.asdict / JSON boundary a status may
    # arrive as a plain string; it must render unchanged (no enum coercion).
    event = CompactionEvent(
        messages_compressed=2,
        tokens_saved_estimate=0,
        applied_stages=(),
        local_budget_status="critical",
    )
    payload = build_compaction_status_payload(event)
    assert payload["localBudgetStatus"] == "critical"
    assert "budgetStatus" not in payload
