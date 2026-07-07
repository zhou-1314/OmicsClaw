from types import SimpleNamespace

import pytest

from omicsclaw.surfaces.cli._llm_bridge_support import (
    append_interruption_notice,
    build_usage_delta,
    seed_core_conversation,
    sync_core_conversation,
)
from omicsclaw.runtime.storage.transcript import _INTERRUPTED_TOOL_PLACEHOLDER, TranscriptStore
from omicsclaw.runtime.storage.transcript_db import TranscriptDB

# F10 follow-up: sanitize_tool_history now REPAIRS an interrupted tool-call bundle
# (preserve assistant + synthesize a placeholder for the missing result) instead
# of whole-dropping it. The CLI bridge/session use the same sanitizer, so their
# transcripts gain the assistant + placeholder rather than losing the bundle.
_CALL_1_ASSISTANT = {
    "role": "assistant",
    "content": "",
    "tool_calls": [{"id": "call-1", "type": "function"}],
}
_CALL_1_PLACEHOLDER = {
    "role": "tool",
    "tool_call_id": "call-1",
    "content": _INTERRUPTED_TOOL_PLACEHOLDER,
}


def _build_core(db=None) -> SimpleNamespace:
    # Mirror ``omicsclaw.runtime.agent.state``: ``conversations`` /
    # ``_conversation_access`` are aliases of the store's dicts, so the bridge
    # helpers must mutate through the store (which mirrors an enabled db), not by
    # poking the aliased dict directly (ADR 0040 mirror-consistency).
    store = TranscriptStore(db=db)
    return SimpleNamespace(
        transcript_store=store,
        conversations=store.messages_by_chat,
        _conversation_access=store.access_by_chat,
    )


def test_seed_core_conversation_sanitizes_history_and_extracts_text_blocks():
    core = _build_core()
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function"}],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "please"},
                {"type": "text", "text": "retry"},
            ],
        },
    ]

    user_text = seed_core_conversation(core, "chat-1", messages, touched_at=123.0)

    assert user_text == "please retry"
    assert core.conversations["chat-1"] == [
        {"role": "user", "content": "hello"},
        _CALL_1_ASSISTANT,
        _CALL_1_PLACEHOLDER,
    ]
    assert core._conversation_access["chat-1"] == 123.0


def test_sync_core_conversation_sanitizes_and_mutates_message_list():
    core = _build_core()
    core.conversations["chat-2"] = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function"}],
        },
        {"role": "assistant", "content": "final answer"},
    ]
    messages = [{"role": "user", "content": "stale"}]

    updated = sync_core_conversation(core, "chat-2", messages)

    assert updated == [
        {"role": "user", "content": "hello"},
        _CALL_1_ASSISTANT,
        _CALL_1_PLACEHOLDER,
        {"role": "assistant", "content": "final answer"},
    ]
    assert messages == updated
    assert core.conversations["chat-2"] == updated


def test_append_interruption_notice_syncs_first_and_updates_messages():
    core = _build_core()
    core.conversations["chat-3"] = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function"}],
        },
    ]
    messages = []

    updated = append_interruption_notice(
        core,
        "chat-3",
        text="Conversation interrupted",
        messages=messages,
    )

    assert updated == [
        {"role": "user", "content": "hello"},
        _CALL_1_ASSISTANT,
        _CALL_1_PLACEHOLDER,
        {"role": "user", "content": "Conversation interrupted"},
    ]
    assert messages == updated
    assert core.conversations["chat-3"] == updated


def test_seed_then_append_persists_full_history_across_restart(tmp_path):
    """ADR 0040 mirror-bypass regression: resuming a CLI/TUI session seeds the
    conversation from the saved messages, then the agent loop appends the live
    turn incrementally. The seed MUST reach the transcripts.db (via the store's
    replace_history) BEFORE those appends — else ``db.append`` finds no chat row,
    starts at seq 0, and a restart rehydrates only the live turn (the seeded
    history is silently lost)."""
    path = tmp_path / "transcripts.db"
    store = TranscriptStore(db=TranscriptDB(path))
    core = SimpleNamespace(
        transcript_store=store,
        conversations=store.messages_by_chat,
        _conversation_access=store.access_by_chat,
    )

    # Resume: [old q, old a] is the saved history; the trailing user message is
    # excluded from the seed because the loop appends it itself.
    seed_core_conversation(
        core,
        "cli",
        [
            {"role": "user", "content": "old q"},
            {"role": "assistant", "content": "old a"},
            {"role": "user", "content": "new q"},
        ],
    )
    # The agent loop appends the live turn through the store (mirrors each write).
    store.append_user_message("cli", "new q")
    store.append_assistant_message("cli", content="new a")
    store.db.close()

    # Cold restart: a fresh store on the same db rehydrates the FULL transcript.
    store2 = TranscriptStore(db=TranscriptDB(path))
    assert store2.get_history("cli") == [
        {"role": "user", "content": "old q"},
        {"role": "assistant", "content": "old a"},
        {"role": "user", "content": "new q"},
        {"role": "assistant", "content": "new a"},
    ]


def test_seed_sync_interruption_mirror_to_db(tmp_path):
    """Each bridge helper keeps the db mirror consistent with memory."""
    path = tmp_path / "transcripts.db"
    core = _build_core(db=TranscriptDB(path))

    seed_core_conversation(
        core,
        "c",
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
         {"role": "user", "content": "c"}],
    )
    assert core.transcript_store.db.rehydrate("c") == core.conversations["c"]

    core.conversations["c"].append({"role": "assistant", "content": "d"})
    sync_core_conversation(core, "c")
    assert core.transcript_store.db.rehydrate("c") == core.conversations["c"]

    append_interruption_notice(core, "c", text="interrupted")
    assert core.transcript_store.db.rehydrate("c") == core.conversations["c"]
    assert core.conversations["c"][-1] == {"role": "user", "content": "interrupted"}


def test_build_usage_delta_prefers_snapshot_cost_delta():
    delta = build_usage_delta(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "api_calls": 1,
            "estimated_cost_usd": 0.01,
        },
        {
            "prompt_tokens": 160,
            "completion_tokens": 90,
            "total_tokens": 250,
            "api_calls": 3,
            "estimated_cost_usd": 0.015,
            "model": "gpt-test",
            "provider": "test-provider",
            "input_price_per_1m": 1.0,
            "output_price_per_1m": 2.0,
        },
    )

    assert delta.prompt_tokens == 60
    assert delta.completion_tokens == 40
    assert delta.total_tokens == 100
    assert delta.api_calls == 2
    assert delta.estimated_cost_usd == pytest.approx(0.005)
    assert delta.model == "gpt-test"
    assert delta.provider == "test-provider"
    assert delta.has_usage is True


def test_build_usage_delta_falls_back_to_token_pricing_when_cost_missing():
    delta = build_usage_delta(
        {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "api_calls": 2,
            "estimated_cost_usd": 0.0,
        },
        {
            "prompt_tokens": 1010,
            "completion_tokens": 520,
            "total_tokens": 1530,
            "api_calls": 2,
            "estimated_cost_usd": 0.0,
            "input_price_per_1m": 2.0,
            "output_price_per_1m": 4.0,
        },
    )

    assert delta.prompt_tokens == 1000
    assert delta.completion_tokens == 500
    assert delta.total_tokens == 1500
    assert delta.api_calls == 0
    assert delta.estimated_cost_usd == pytest.approx(0.004)
    assert delta.has_usage is True


def test_build_usage_delta_clamps_negative_values_to_zero():
    delta = build_usage_delta(
        {
            "prompt_tokens": 100,
            "completion_tokens": 100,
            "total_tokens": 200,
            "api_calls": 5,
            "estimated_cost_usd": 1.0,
        },
        {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "api_calls": 1,
            "estimated_cost_usd": 0.5,
        },
    )

    assert delta.prompt_tokens == 0
    assert delta.completion_tokens == 0
    assert delta.total_tokens == 0
    assert delta.api_calls == 0
    assert delta.estimated_cost_usd == 0.0
    assert delta.has_usage is False
