from omicsclaw.runtime.context.compaction import (
    STAGE_AUTO_COMPACT,
    STAGE_CONTEXT_COLLAPSE,
    STAGE_MICRO_COMPACT,
    STAGE_REACTIVE_COMPACT,
    STAGE_SNIP_COMPACT,
    ContextCompactionConfig,
    prepare_model_messages,
    wrap_compaction_summary,
)
from omicsclaw.runtime.storage.tool_result import ToolResultStore


def test_prepare_model_messages_applies_snip_and_micro_before_heavy_stages(tmp_path):
    result_store = ToolResultStore(
        storage_dir=tmp_path / "tool_results",
        inline_bytes=64,
        preview_chars=1200,
    )
    record = result_store.record(
        chat_id="chat-light",
        tool_call_id="call-1",
        tool_name="inspect_data",
        output="X" * 1200,
        success=True,
    )

    prepared = prepare_model_messages(
        system_prompt="SYSTEM",
        history=[
            {"role": "user", "content": "older user " + ("A" * 1200)},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "inspect_data",
                            "arguments": '{"payload":"' + ("B" * 1200) + '"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": record.content},
            {"role": "user", "content": "latest request"},
        ],
        chat_id="chat-light",
        tool_result_store=result_store,
        config=ContextCompactionConfig(
            max_prompt_chars=50_000,
            snip_message_chars=240,
            snip_tool_argument_chars=180,
            protected_tail_messages=1,
            micro_keep_recent_tool_messages=0,
            collapse_trigger_ratio=0.95,
            auto_compact_trigger_ratio=0.99,
        ),
    )

    assert prepared.applied_stages == (STAGE_SNIP_COMPACT, STAGE_MICRO_COMPACT)
    assert "## Context Collapse" not in prepared.system_prompt
    assert "snip compacted older message" in prepared.messages[0]["content"]
    assert "snip compacted older tool arguments" in prepared.messages[1]["tool_calls"][0]["function"]["arguments"]
    assert "[tool result micro-compacted]" in prepared.messages[2]["content"]


def test_prepare_model_messages_runs_context_collapse_before_auto_compact(tmp_path):
    result_store = ToolResultStore(storage_dir=tmp_path / "tool_results")
    history = []
    for index in range(8):
        history.append({"role": "user", "content": f"user step {index} " + ("A" * 180)})
        history.append({"role": "assistant", "content": f"assistant step {index} " + ("B" * 180)})

    prepared = prepare_model_messages(
        system_prompt="SYSTEM",
        history=history,
        chat_id="chat-collapse",
        tool_result_store=result_store,
        config=ContextCompactionConfig(
            max_prompt_chars=4_000,
            snip_message_chars=4_000,
            protected_tail_messages=2,
            collapse_trigger_ratio=0.45,
            auto_compact_trigger_ratio=0.75,
            collapse_preserve_messages=4,
            collapse_preserve_chars=600,
            auto_compact_preserve_messages=2,
            auto_compact_preserve_chars=240,
        ),
    )

    assert STAGE_CONTEXT_COLLAPSE in prepared.applied_stages
    assert STAGE_AUTO_COMPACT not in prepared.applied_stages
    # F2: summaries render under one canonical heading with a per-stage sub-section.
    assert "## Persisted Compacted Context" in prepared.system_prompt
    assert "### Context Collapse" in prepared.system_prompt
    assert len(prepared.messages) <= 4


def test_prepare_model_messages_runs_auto_compact_when_collapse_is_not_enough(tmp_path):
    result_store = ToolResultStore(storage_dir=tmp_path / "tool_results")
    history = []
    for index in range(10):
        history.append({"role": "user", "content": f"user step {index} " + ("A" * 260)})
        history.append({"role": "assistant", "content": f"assistant step {index} " + ("B" * 260)})

    prepared = prepare_model_messages(
        system_prompt="SYSTEM",
        history=history,
        chat_id="chat-auto",
        tool_result_store=result_store,
        config=ContextCompactionConfig(
            max_prompt_chars=2_200,
            snip_message_chars=4_000,
            protected_tail_messages=2,
            collapse_trigger_ratio=0.35,
            auto_compact_trigger_ratio=0.55,
            collapse_preserve_messages=8,
            collapse_preserve_chars=1_200,
            auto_compact_preserve_messages=2,
            auto_compact_preserve_chars=220,
        ),
    )

    assert STAGE_CONTEXT_COLLAPSE in prepared.applied_stages
    assert STAGE_AUTO_COMPACT in prepared.applied_stages
    # F2: canonical heading wraps both stage sub-sections.
    assert "## Persisted Compacted Context" in prepared.system_prompt
    assert "### Auto Compacted Context" in prepared.system_prompt
    assert len(prepared.messages) <= 2


def _pairs(count, filler):
    history = []
    for index in range(count):
        history.append({"role": "user", "content": f"user step {index} " + filler})
        history.append({"role": "assistant", "content": f"assistant step {index} " + filler})
    return history


def _hoist_next_turn(prepared, *, config, tool_result_store, chat_id):
    # Simulate the next turn: the persisted summary rides as messages[0], the
    # preserved tail follows, and nothing new triggers a fresh compaction.
    next_history = [
        {"role": "system", "content": wrap_compaction_summary(prepared.persisted_summary)},
        *prepared.messages,
    ]
    return prepare_model_messages(
        system_prompt="SYSTEM",
        history=next_history,
        chat_id=chat_id,
        tool_result_store=tool_result_store,
        config=config,
    )


def _assert_one_compaction_one_rewarm(first, *, config, tool_result_store, chat_id):
    # F2 invariant: the compaction-turn system prompt must be byte-identical to
    # the next turn's hoisted system, and stay stable on the turn after that —
    # i.e. a single compaction causes exactly one prefix re-warm (ADR 0024).
    assert first.applied_stages
    assert first.persisted_summary.strip()
    second = _hoist_next_turn(
        first, config=config, tool_result_store=tool_result_store, chat_id=chat_id
    )
    assert second.system_prompt == first.system_prompt
    third = _hoist_next_turn(
        second, config=config, tool_result_store=tool_result_store, chat_id=chat_id
    )
    assert third.system_prompt == first.system_prompt


def test_byte_stable_single_collapse(tmp_path):
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_chars=4_000, snip_message_chars=4_000, protected_tail_messages=2,
        collapse_trigger_ratio=0.45, auto_compact_trigger_ratio=0.99,
        collapse_preserve_messages=4, collapse_preserve_chars=600,
    )
    first = prepare_model_messages(
        system_prompt="SYSTEM", history=_pairs(8, "A" * 180), chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in first.applied_stages
    _assert_one_compaction_one_rewarm(
        first, config=config, tool_result_store=store, chat_id="c"
    )


def test_byte_stable_collapse_plus_auto(tmp_path):
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_chars=2_200, snip_message_chars=4_000, protected_tail_messages=2,
        collapse_trigger_ratio=0.35, auto_compact_trigger_ratio=0.55,
        collapse_preserve_messages=8, collapse_preserve_chars=1_200,
        auto_compact_preserve_messages=2, auto_compact_preserve_chars=220,
    )
    first = prepare_model_messages(
        system_prompt="SYSTEM", history=_pairs(10, "A" * 260), chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in first.applied_stages
    assert STAGE_AUTO_COMPACT in first.applied_stages
    _assert_one_compaction_one_rewarm(
        first, config=config, tool_result_store=store, chat_id="c"
    )


def test_byte_stable_reactive(tmp_path):
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_chars=4_000, snip_message_chars=4_000, protected_tail_messages=1,
        reactive_preserve_messages=2, reactive_preserve_chars=200,
    )
    first = prepare_model_messages(
        system_prompt="SYSTEM", history=_pairs(8, "A" * 180), chat_id="c",
        tool_result_store=store, config=config, force_reactive_compact=True,
    )
    assert STAGE_REACTIVE_COMPACT in first.applied_stages
    _assert_one_compaction_one_rewarm(
        first, config=config, tool_result_store=store, chat_id="c"
    )


def test_prepare_model_messages_reports_observational_budget_status(tmp_path):
    # §9.3 slice 2: when the model context window is known, PreparedModelMessages
    # carries an observational token-budget status (no behavior change).
    from omicsclaw.runtime.context.budget import ContextBudgetStatus

    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_chars=1_000_000,  # never compact — isolate status computation
        snip_message_chars=1_000_000,  # never snip the big message
        context_window_tokens=10_000,  # effective = 10000 - 4096 - 2048 = 3856 tok
    )
    small = prepare_model_messages(
        system_prompt="SYS",
        history=[{"role": "user", "content": "x" * 3_000}],
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    big = prepare_model_messages(
        system_prompt="SYS",
        history=[{"role": "user", "content": "x" * 30_000}],
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    # ~1000 tok / 3856 ~ 26% -> OK ; ~10000 tok / 3856 > 96% -> BLOCK
    assert small.budget_status == ContextBudgetStatus.OK
    assert big.budget_status == ContextBudgetStatus.BLOCK


def test_prepare_model_messages_budget_status_none_without_window(tmp_path):
    # Backward-compatible: no configured window -> no observational status.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(max_prompt_chars=1_000_000)
    prepared = prepare_model_messages(
        system_prompt="SYS",
        history=[{"role": "user", "content": "hello"}],
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    assert prepared.budget_status is None


def test_prepare_model_messages_budget_status_blocks_on_zero_window(tmp_path):
    # A configured but zero-capacity window is BLOCK, not None (is-None guard).
    from omicsclaw.runtime.context.budget import ContextBudgetStatus

    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(max_prompt_chars=1_000_000, context_window_tokens=0)
    prepared = prepare_model_messages(
        system_prompt="SYS",
        history=[{"role": "user", "content": "hi"}],
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    assert prepared.budget_status == ContextBudgetStatus.BLOCK


def test_byte_stable_prior_summary_plus_new_collapse(tmp_path):
    # The critical algebra: a turn that BOTH hoists a prior persisted summary AND
    # produces a new collapse section must still equal its own next-turn hoist.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_chars=4_000, snip_message_chars=4_000, protected_tail_messages=2,
        collapse_trigger_ratio=0.45, auto_compact_trigger_ratio=0.99,
        collapse_preserve_messages=4, collapse_preserve_chars=600,
    )
    first = prepare_model_messages(
        system_prompt="SYSTEM", history=_pairs(8, "A" * 180), chat_id="c",
        tool_result_store=store, config=config,
    )
    assert first.persisted_summary.strip()
    # Next turn carries the prior summary AND a fresh batch that collapses again.
    combined_history = [
        {"role": "system", "content": wrap_compaction_summary(first.persisted_summary)},
        *first.messages,
        *_pairs(8, "C" * 180),
    ]
    second = prepare_model_messages(
        system_prompt="SYSTEM", history=combined_history, chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in second.applied_stages
    _assert_one_compaction_one_rewarm(
        second, config=config, tool_result_store=store, chat_id="c"
    )
