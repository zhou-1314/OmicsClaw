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


def test_prepare_model_messages_reports_local_budget_status(tmp_path):
    # §9.3 slice 3: local budget status (chars/max_prompt_chars) is available
    # whenever a char budget is configured — unlike the window status, it stays
    # decision-useful for large-window models.
    from omicsclaw.runtime.context.budget import ContextBudgetStatus

    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_chars=10_000,
        snip_message_chars=1_000_000,  # isolate: never snip
        collapse_trigger_ratio=0.99,
        auto_compact_trigger_ratio=0.999,  # isolate: never collapse here
    )
    small = prepare_model_messages(
        system_prompt="SYS",
        history=[{"role": "user", "content": "x" * 1_000}],
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    full = prepare_model_messages(
        system_prompt="SYS",
        history=[{"role": "user", "content": "x" * 9_800}],
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    assert small.local_budget_status == ContextBudgetStatus.OK  # ~10%
    assert full.local_budget_status == ContextBudgetStatus.BLOCK  # ~98%


def test_prepare_model_messages_local_budget_status_none_without_char_budget(tmp_path):
    # Backward-compatible: no char budget -> no local status.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(max_prompt_chars=None)
    prepared = prepare_model_messages(
        system_prompt="SYS",
        history=[{"role": "user", "content": "hello"}],
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    assert prepared.local_budget_status is None


def test_collapse_target_ratio_scales_preserve_with_budget(tmp_path):
    # §9.3 slice 3: with a target ratio set, the collapse preserve budget scales
    # to a fraction of max_prompt_chars instead of the fixed constant, so a large
    # budget keeps proportionally more recent context per compaction (replacing
    # the magic 12000-char constant, which under a target no longer applies).
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    base = dict(
        max_prompt_chars=40_000,
        snip_message_chars=1_000_000,
        protected_tail_messages=2,
        collapse_trigger_ratio=0.50,  # threshold 20000
        auto_compact_trigger_ratio=0.999,  # isolate collapse
        collapse_preserve_messages=100,  # high -> char budget binds
        collapse_preserve_chars=500,
    )
    history = _pairs(40, "A" * 400)  # ~33600 chars > 20000 -> collapse fires

    fixed = prepare_model_messages(
        system_prompt="SYSTEM",
        history=history,
        chat_id="c",
        tool_result_store=store,
        config=ContextCompactionConfig(**base),
    )
    scaled = prepare_model_messages(
        system_prompt="SYSTEM",
        history=history,
        chat_id="c",
        tool_result_store=store,
        config=ContextCompactionConfig(**base, collapse_target_ratio=0.40),
    )

    assert STAGE_CONTEXT_COLLAPSE in fixed.applied_stages
    assert STAGE_CONTEXT_COLLAPSE in scaled.applied_stages
    # Fixed keeps ~500 chars; budget-relative keeps ~16000 (0.40 * 40000).
    assert scaled.estimated_chars > fixed.estimated_chars
    assert len(scaled.messages) > len(fixed.messages)


def test_byte_stable_collapse_with_target_ratio(tmp_path):
    # §9.3 slice 3: budget-relative targets must preserve the one-compaction =
    # one-rewarm invariant (F2). This holds as long as target_ratio is safely
    # below the collapse trigger so the re-warmed next turn does not re-collapse.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_chars=40_000,
        snip_message_chars=1_000_000,
        protected_tail_messages=2,
        collapse_trigger_ratio=0.80,  # threshold 32000
        auto_compact_trigger_ratio=0.999,
        collapse_preserve_messages=100,
        collapse_preserve_chars=2_000,
        collapse_target_ratio=0.50,  # target 20000 << trigger 32000
    )
    first = prepare_model_messages(
        system_prompt="SYSTEM",
        history=_pairs(60, "A" * 300),
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in first.applied_stages
    _assert_one_compaction_one_rewarm(
        first, config=config, tool_result_store=store, chat_id="c"
    )


def test_byte_stable_collapse_with_target_ratio_large_summary(tmp_path):
    # §9.3 slice 3 (codex P1 regression): the collapse summary can be large AND
    # can GROW between the two passes — compacted tool-result paths are rendered
    # verbatim with no length cap, so a smaller pass-2 budget omits more of them
    # and surfaces more/longer refs. Sizing the tail against only the pre-summary
    # (or pass-1) system leaves total > target and re-collapses next turn (F2
    # break). The final hard-trim against the ACTUAL pass-2 summary must keep the
    # total within target regardless of summary growth.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    very_long_path = "/tmp/oc/" + ("deep/" * 120)  # ~600-char verbatim path

    def compacted_tool_msg(call_id, path):
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": (
                "[tool result compacted]\ntool: inspect_data\nbytes: 99999\n"
                f"full_result_path: {path}\npreview:\n..."
            ),
        }

    # Interleave many long-path tool refs with filler so pass-1 and pass-2 omit
    # different ref counts -> the pass-2 summary grows past pass-1's estimate.
    head = []
    for i in range(8):
        head.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": "inspect_data", "arguments": "{}"},
                    }
                ],
            }
        )
        head.append(compacted_tool_msg(f"c{i}", very_long_path + f"r{i}.json"))
        head.append({"role": "user", "content": f"between {i} " + ("W" * 120)})
    history = head + _pairs(6, "Q" * 140)

    config = ContextCompactionConfig(
        max_prompt_chars=8_000,
        snip_message_chars=1_000_000,
        protected_tail_messages=2,
        collapse_trigger_ratio=0.55,  # threshold 4400
        auto_compact_trigger_ratio=0.999,  # isolate collapse
        collapse_preserve_messages=8,
        collapse_preserve_chars=200,
        collapse_target_ratio=0.40,  # total target 3200
    )
    first = prepare_model_messages(
        system_prompt="SYSTEM",  # must match _hoist_next_turn's hardcoded base
        history=history,
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in first.applied_stages
    # Summary is genuinely large (long verbatim paths) — exercises the real
    # target + summary > trigger failure mode, not a trivial summary.
    assert len(first.persisted_summary) > 1_500
    # The final total must be within target (below the trigger), so the next turn
    # cannot re-collapse.
    assert first.estimated_chars <= int(
        config.max_prompt_chars * config.collapse_trigger_ratio
    )
    _assert_one_compaction_one_rewarm(
        first, config=config, tool_result_store=store, chat_id="c"
    )


def test_compress_to_target_lifts_message_cap_and_is_system_size_invariant(tmp_path):
    # §9.3 slice 3: with a target ratio the char budget drives — the small
    # message-count cap is lifted so a chatty history fills toward the target
    # (not a sliver), and the TOTAL prompt converges to target_ratio *
    # max_prompt_chars *independent of system-prompt size* (system overhead is
    # subtracted from the tail budget).
    store = ToolResultStore(storage_dir=tmp_path / "tr")

    def prep(system_prompt):
        return prepare_model_messages(
            system_prompt=system_prompt,
            history=_pairs(120, "A" * 200),  # many small msgs -> would hit msg cap
            chat_id="c",
            tool_result_store=store,
            config=ContextCompactionConfig(
                max_prompt_chars=40_000,
                snip_message_chars=1_000_000,
                protected_tail_messages=2,
                collapse_trigger_ratio=0.80,  # threshold 32000
                auto_compact_trigger_ratio=0.999,
                collapse_preserve_messages=4,  # small cap -> must be lifted
                collapse_preserve_chars=1_000,
                collapse_target_ratio=0.50,  # total target 20000
            ),
        )

    small_sys = prep("SYS")
    big_sys = prep("S" * 8_000)

    assert STAGE_CONTEXT_COLLAPSE in small_sys.applied_stages
    # Message cap (4) is lifted: the tail fills far past 4 messages.
    assert len(small_sys.messages) > 20
    # Total converges to ~target (20000, plus bounded summary overhead) and stays
    # under the collapse trigger — invariant to system size (subtract-system).
    assert 18_000 <= small_sys.estimated_chars <= 26_000
    assert 18_000 <= big_sys.estimated_chars <= 26_000
    assert abs(small_sys.estimated_chars - big_sys.estimated_chars) < 4_000


def test_compress_to_target_bounds_tail_when_system_exceeds_target(tmp_path):
    # §9.3 slice 3 edge: with a target ratio, no fixed preserve_chars floor, and a
    # system prompt that alone meets the target, the tail must still be bounded.
    # The lifted message cap (-1) must not combine with a None char budget to keep
    # the whole history and overflow.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_chars=20_000,
        snip_message_chars=1_000_000,
        protected_tail_messages=2,
        collapse_trigger_ratio=0.50,  # threshold 10000
        auto_compact_trigger_ratio=0.999,
        collapse_preserve_messages=4,
        collapse_preserve_chars=None,  # no floor
        collapse_target_ratio=0.50,  # total target 10000
    )
    prepared = prepare_model_messages(
        system_prompt="S" * 11_000,  # system alone > target (10000)
        history=_pairs(40, "A" * 200),  # ~16000 chars of history
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in prepared.applied_stages
    # Tail bounded to the newest block, NOT the whole 80-message history.
    assert len(prepared.messages) <= 2


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
