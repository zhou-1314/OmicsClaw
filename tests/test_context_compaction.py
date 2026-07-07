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

import asyncio


def _prep(**kwargs):
    # prepare_model_messages is async (F6 threads an optional LLM refine through
    # the collapse/auto stages). These synchronous tests drive it via asyncio.run;
    # with no ``llm`` passed the path is byte-for-byte identical to the old sync
    # deterministic behavior.
    return asyncio.run(prepare_model_messages(**kwargs))


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

    prepared = _prep(
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
            max_prompt_tokens=50_000,
            snip_message_chars=240,
            protected_tail_messages=1,
            micro_keep_recent_tool_messages=0,
            collapse_trigger_ratio=0.95,
            auto_compact_trigger_ratio=0.99,
        ),
    )

    assert prepared.applied_stages == (STAGE_SNIP_COMPACT, STAGE_MICRO_COMPACT)
    assert "## Context Collapse" not in prepared.system_prompt
    assert "snip compacted older message" in prepared.messages[0]["content"]
    # F11: snip must NOT rewrite historical tool_call arguments — truncating a JSON
    # args string yields invalid args re-sent to the model every turn. Arguments
    # are preserved verbatim; oversized args are folded by the collapse stage.
    assert (
        prepared.messages[1]["tool_calls"][0]["function"]["arguments"]
        == '{"payload":"' + ("B" * 1200) + '"}'
    )
    assert "[tool result micro-compacted]" in prepared.messages[2]["content"]


def test_snip_preserves_tool_call_arguments(tmp_path):
    # F11: snip compresses message CONTENT (and tool RESULTS via micro), but must
    # leave historical assistant tool_call arguments byte-identical — truncating a
    # JSON args string produces invalid args re-sent to the model every turn, and
    # (when a collapse stage co-fires) persists the corruption. Oversized args are
    # handled by the collapse stage, not by rewriting the call in place.
    import json

    store = ToolResultStore(storage_dir=tmp_path / "tr")
    big_args = '{"path":"/data/sample.h5ad","payload":"' + ("Z" * 4000) + '"}'
    prepared = _prep(
        system_prompt="SYSTEM",
        history=[
            {"role": "user", "content": "please run " + ("Q" * 4000)},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "run_skill", "arguments": big_args},
                    }
                ],
            },
            {"role": "user", "content": "latest"},
        ],
        chat_id="c",
        tool_result_store=store,
        config=ContextCompactionConfig(
            max_prompt_tokens=1_000_000,  # never collapse/auto — isolate snip
            snip_message_chars=200,
            protected_tail_messages=1,
            collapse_trigger_ratio=0.99,
            auto_compact_trigger_ratio=0.999,
        ),
    )

    assert STAGE_SNIP_COMPACT in prepared.applied_stages
    # message content is still snipped ...
    assert "snip compacted older message" in prepared.messages[0]["content"]
    # ... but the tool_call arguments are preserved verbatim and stay valid JSON.
    args = prepared.messages[1]["tool_calls"][0]["function"]["arguments"]
    assert args == big_args
    json.loads(args)


def test_prepare_model_messages_runs_context_collapse_before_auto_compact(tmp_path):
    result_store = ToolResultStore(storage_dir=tmp_path / "tool_results")
    history = []
    for index in range(8):
        history.append({"role": "user", "content": f"user step {index} " + ("A" * 180)})
        history.append({"role": "assistant", "content": f"assistant step {index} " + ("B" * 180)})

    prepared = _prep(
        system_prompt="SYSTEM",
        history=history,
        chat_id="chat-collapse",
        tool_result_store=result_store,
        config=ContextCompactionConfig(
            max_prompt_tokens=1_000,
            snip_message_chars=4_000,
            protected_tail_messages=2,
            collapse_trigger_ratio=0.45,
            auto_compact_trigger_ratio=0.75,
            collapse_preserve_messages=4,
            collapse_preserve_tokens=150,
            auto_compact_preserve_messages=2,
            auto_compact_preserve_tokens=60,
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

    prepared = _prep(
        system_prompt="SYSTEM",
        history=history,
        chat_id="chat-auto",
        tool_result_store=result_store,
        config=ContextCompactionConfig(
            max_prompt_tokens=550,
            snip_message_chars=4_000,
            protected_tail_messages=2,
            collapse_trigger_ratio=0.35,
            auto_compact_trigger_ratio=0.55,
            collapse_preserve_messages=8,
            collapse_preserve_tokens=300,
            auto_compact_preserve_messages=2,
            auto_compact_preserve_tokens=55,
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
    return _prep(
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
        max_prompt_tokens=1_000, snip_message_chars=4_000, protected_tail_messages=2,
        collapse_trigger_ratio=0.45, auto_compact_trigger_ratio=0.99,
        collapse_preserve_messages=4, collapse_preserve_tokens=150,
    )
    first = _prep(
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
        max_prompt_tokens=550, snip_message_chars=4_000, protected_tail_messages=2,
        collapse_trigger_ratio=0.35, auto_compact_trigger_ratio=0.55,
        collapse_preserve_messages=8, collapse_preserve_tokens=300,
        auto_compact_preserve_messages=2, auto_compact_preserve_tokens=55,
    )
    first = _prep(
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
        max_prompt_tokens=4_000, snip_message_chars=4_000, protected_tail_messages=1,
        reactive_preserve_messages=2, reactive_preserve_tokens=200,
    )
    first = _prep(
        system_prompt="SYSTEM", history=_pairs(8, "A" * 180), chat_id="c",
        tool_result_store=store, config=config, force_reactive_compact=True,
    )
    assert STAGE_REACTIVE_COMPACT in first.applied_stages
    _assert_one_compaction_one_rewarm(
        first, config=config, tool_result_store=store, chat_id="c"
    )


def test_prepare_model_messages_budget_status_mirrors_token_status(tmp_path):
    # ADR 0039 / S3: the retired window-relative status is gone. `budget_status`
    # now carries the SAME single actionable token status as `local_budget_status`
    # (both wire keys kept for one release, both sourced from max_prompt_tokens).
    from omicsclaw.runtime.context.budget import ContextBudgetStatus

    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_tokens=2_500,
        snip_message_chars=1_000_000,
        collapse_trigger_ratio=0.99,  # isolate: never compact here
        auto_compact_trigger_ratio=0.999,
    )
    full = _prep(
        system_prompt="SYS",
        history=[{"role": "user", "content": "x" * 9_800}],  # ~2450 tok / 2500 -> ~98%
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    assert full.budget_status == full.local_budget_status == ContextBudgetStatus.BLOCK
    # No budget configured -> both None (backward-compatible).
    none_cfg = ContextCompactionConfig(max_prompt_tokens=None)
    p = _prep(
        system_prompt="SYS", history=[{"role": "user", "content": "hi"}],
        chat_id="c", tool_result_store=store, config=none_cfg,
    )
    assert p.budget_status is None and p.local_budget_status is None


def test_prepare_model_messages_reports_local_budget_status(tmp_path):
    # §9.3 slice 3: local budget status (chars/max_prompt_chars) is available
    # whenever a char budget is configured — unlike the window status, it stays
    # decision-useful for large-window models.
    from omicsclaw.runtime.context.budget import ContextBudgetStatus

    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_tokens=2_500,
        snip_message_chars=1_000_000,  # isolate: never snip
        collapse_trigger_ratio=0.99,
        auto_compact_trigger_ratio=0.999,  # isolate: never collapse here
    )
    small = _prep(
        system_prompt="SYS",
        history=[{"role": "user", "content": "x" * 1_000}],
        chat_id="c",
        tool_result_store=store,
        config=config,
    )
    full = _prep(
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
    config = ContextCompactionConfig(max_prompt_tokens=None)
    prepared = _prep(
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
        max_prompt_tokens=10_000,
        snip_message_chars=1_000_000,
        protected_tail_messages=2,
        collapse_trigger_ratio=0.50,  # threshold 5000
        auto_compact_trigger_ratio=0.999,  # isolate collapse
        collapse_preserve_messages=100,  # high -> token budget binds
        collapse_preserve_tokens=125,
    )
    history = _pairs(40, "A" * 400)  # ~33600 chars > 20000 -> collapse fires

    fixed = _prep(
        system_prompt="SYSTEM",
        history=history,
        chat_id="c",
        tool_result_store=store,
        config=ContextCompactionConfig(**base),
    )
    scaled = _prep(
        system_prompt="SYSTEM",
        history=history,
        chat_id="c",
        tool_result_store=store,
        config=ContextCompactionConfig(**base, collapse_target_ratio=0.40),
    )

    assert STAGE_CONTEXT_COLLAPSE in fixed.applied_stages
    assert STAGE_CONTEXT_COLLAPSE in scaled.applied_stages
    # Fixed keeps ~500 chars; budget-relative keeps ~16000 (0.40 * 40000).
    assert scaled.estimated_tokens > fixed.estimated_tokens
    assert len(scaled.messages) > len(fixed.messages)


def test_byte_stable_collapse_with_target_ratio(tmp_path):
    # §9.3 slice 3: budget-relative targets must preserve the one-compaction =
    # one-rewarm invariant (F2). This holds as long as target_ratio is safely
    # below the collapse trigger so the re-warmed next turn does not re-collapse.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_tokens=10_000,
        snip_message_chars=1_000_000,
        protected_tail_messages=2,
        collapse_trigger_ratio=0.80,  # threshold 8000
        auto_compact_trigger_ratio=0.999,
        collapse_preserve_messages=100,
        collapse_preserve_tokens=500,
        collapse_target_ratio=0.50,  # target 5000 << trigger 8000
    )
    first = _prep(
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
        max_prompt_tokens=2_000,
        snip_message_chars=1_000_000,
        protected_tail_messages=2,
        collapse_trigger_ratio=0.55,  # threshold 1100
        auto_compact_trigger_ratio=0.999,  # isolate collapse
        collapse_preserve_messages=8,
        collapse_preserve_tokens=50,
        collapse_target_ratio=0.40,  # total target 800
    )
    first = _prep(
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
    assert first.estimated_tokens <= int(
        config.max_prompt_tokens * config.collapse_trigger_ratio
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
        return _prep(
            system_prompt=system_prompt,
            history=_pairs(120, "A" * 200),  # many small msgs -> would hit msg cap
            chat_id="c",
            tool_result_store=store,
            config=ContextCompactionConfig(
                max_prompt_tokens=10_000,
                snip_message_chars=1_000_000,
                protected_tail_messages=2,
                collapse_trigger_ratio=0.80,  # threshold 8000
                auto_compact_trigger_ratio=0.999,
                collapse_preserve_messages=4,  # small cap -> must be lifted
                collapse_preserve_tokens=250,
                collapse_target_ratio=0.50,  # total target 5000
            ),
        )

    small_sys = prep("SYS")
    big_sys = prep("S" * 8_000)

    assert STAGE_CONTEXT_COLLAPSE in small_sys.applied_stages
    # Message cap (4) is lifted: the tail fills far past 4 messages.
    assert len(small_sys.messages) > 20
    # Total converges to ~target (20000, plus bounded summary overhead) and stays
    # under the collapse trigger — invariant to system size (subtract-system).
    assert 4_000 <= small_sys.estimated_tokens <= 7_500
    assert 4_000 <= big_sys.estimated_tokens <= 7_500
    assert abs(small_sys.estimated_tokens - big_sys.estimated_tokens) < 1_500


def test_compress_to_target_bounds_tail_when_system_exceeds_target(tmp_path):
    # §9.3 slice 3 edge: with a target ratio, no fixed preserve_chars floor, and a
    # system prompt that alone meets the target, the tail must still be bounded.
    # The lifted message cap (-1) must not combine with a None char budget to keep
    # the whole history and overflow.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = ContextCompactionConfig(
        max_prompt_tokens=5_000,
        snip_message_chars=1_000_000,
        protected_tail_messages=2,
        collapse_trigger_ratio=0.50,  # threshold 2500
        auto_compact_trigger_ratio=0.999,
        collapse_preserve_messages=4,
        collapse_preserve_tokens=None,  # no floor
        collapse_target_ratio=0.50,  # total target 2500
    )
    prepared = _prep(
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
        max_prompt_tokens=1_000, snip_message_chars=4_000, protected_tail_messages=2,
        collapse_trigger_ratio=0.45, auto_compact_trigger_ratio=0.99,
        collapse_preserve_messages=4, collapse_preserve_tokens=150,
    )
    first = _prep(
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
    second = _prep(
        system_prompt="SYSTEM", history=combined_history, chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in second.applied_stages
    _assert_one_compaction_one_rewarm(
        second, config=config, tool_result_store=store, chat_id="c"
    )


# ---------------------------------------------------------------------------
# F6 — LLM-condensed collapse summary (opt-in). The LLM refine fires ONLY on the
# target-active collapse/auto path; it is capped at the template's length so the
# byte-stable re-hoist (F2, one compaction = one re-warm) holds, and falls back to
# the deterministic template on any timeout/error/empty/oversized output.
# ---------------------------------------------------------------------------

_LLM_MARKER = "LLM-EPISODE-SUMMARY::user goal + tool paths condensed"


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMsg(content)


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, outer):
        self._outer = outer

    async def create(self, **kwargs):
        return await self._outer._create(**kwargs)


class _FakeChat:
    def __init__(self, outer):
        self.completions = _FakeCompletions(outer)


class _FakeSummaryLLM:
    """Minimal OpenAI-shaped client exposing chat.completions.create for F6."""

    def __init__(self, *, content=None, error=None, delay=0.0):
        self._content = content
        self._error = error
        self._delay = delay
        self.calls = []
        self.chat = _FakeChat(self)

    async def _create(self, *, model, max_tokens, messages, stream):
        self.calls.append(
            {"model": model, "max_tokens": max_tokens, "messages": messages, "stream": stream}
        )
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return _FakeResp(self._content)


def _prep_llm(llm, **kwargs):
    kwargs.setdefault("llm_model", "fake-model")
    return asyncio.run(prepare_model_messages(llm=llm, **kwargs))


def _f6_config(**overrides):
    base = dict(
        max_prompt_tokens=1_000,
        snip_message_chars=4_000,
        protected_tail_messages=2,
        collapse_trigger_ratio=0.45,
        collapse_target_ratio=0.35,
        auto_compact_trigger_ratio=0.99,
        collapse_preserve_messages=4,
        collapse_preserve_tokens=150,
        collapse_llm_summary_enabled=True,
        llm_summary_min_omitted=4,
    )
    base.update(overrides)
    return ContextCompactionConfig(**base)


def _collapse_section_body(persisted_summary, heading="Context Collapse"):
    marker = f"### {heading}\n\n"
    start = persisted_summary.index(marker) + len(marker)
    rest = persisted_summary[start:]
    end = rest.find("\n\n---\n\n")
    return (rest if end == -1 else rest[:end]).strip()


def test_f6_llm_summary_sent_equals_persisted(tmp_path):
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    llm = _FakeSummaryLLM(content=_LLM_MARKER)
    config = _f6_config()
    first = _prep_llm(
        llm, system_prompt="SYSTEM", history=_pairs(12, "A" * 180), chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in first.applied_stages
    assert len(llm.calls) == 1  # exactly one refine call on the collapse turn
    # The LLM text is what got sent AND persisted (approach A: same summary_sections).
    assert _LLM_MARKER in first.system_prompt
    assert _collapse_section_body(first.persisted_summary) == _LLM_MARKER


def test_f6_byte_stable_across_hoist_with_llm_summary(tmp_path):
    # F2: a single collapse with an LLM summary must re-hoist byte-for-byte and
    # NOT re-collapse next turn (the length cap keeps N+1 within the same budget
    # the template already satisfied).
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    llm = _FakeSummaryLLM(content=_LLM_MARKER)
    config = _f6_config()
    first = _prep_llm(
        llm, system_prompt="SYSTEM", history=_pairs(12, "A" * 180), chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in first.applied_stages
    assert _LLM_MARKER in first.system_prompt
    # Hoist twice with no LLM (verbatim summary ride); prefix stays byte-identical.
    _assert_one_compaction_one_rewarm(
        first, config=config, tool_result_store=store, chat_id="c"
    )


def test_f6_llm_error_falls_back_to_template(tmp_path):
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    llm = _FakeSummaryLLM(error=RuntimeError("boom"))
    config = _f6_config()
    first = _prep_llm(
        llm, system_prompt="SYSTEM", history=_pairs(12, "A" * 180), chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in first.applied_stages
    assert len(llm.calls) == 1
    assert _LLM_MARKER not in first.system_prompt
    # The deterministic template header is present, and it stays byte-stable.
    assert "earlier message(s) were compacted" in first.persisted_summary
    _assert_one_compaction_one_rewarm(
        first, config=config, tool_result_store=store, chat_id="c"
    )


def test_f6_llm_timeout_falls_back_to_template(tmp_path):
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    llm = _FakeSummaryLLM(content=_LLM_MARKER, delay=1.0)
    config = _f6_config(llm_summary_timeout_s=0.05)
    first = _prep_llm(
        llm, system_prompt="SYSTEM", history=_pairs(12, "A" * 180), chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in first.applied_stages
    assert _LLM_MARKER not in first.system_prompt
    assert "earlier message(s) were compacted" in first.persisted_summary


def test_f6_empty_and_oversized_summaries_rejected(tmp_path):
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = _f6_config()
    # Empty output -> template.
    empty_llm = _FakeSummaryLLM(content="   ")
    empty = _prep_llm(
        empty_llm, system_prompt="SYSTEM", history=_pairs(12, "A" * 180), chat_id="c",
        tool_result_store=store, config=config,
    )
    assert _LLM_MARKER not in empty.system_prompt
    assert "earlier message(s) were compacted" in empty.persisted_summary
    # Oversized output (> template length) -> template (the F2 length cap).
    huge_llm = _FakeSummaryLLM(content="Z" * 100_000)
    huge = _prep_llm(
        huge_llm, system_prompt="SYSTEM", history=_pairs(12, "A" * 180), chat_id="c2",
        tool_result_store=store, config=config,
    )
    assert "Z" * 100_000 not in huge.system_prompt
    assert "earlier message(s) were compacted" in huge.persisted_summary
    _assert_one_compaction_one_rewarm(
        huge, config=config, tool_result_store=store, chat_id="c2"
    )


def test_f6_tier1_lossless_skip_below_threshold(tmp_path):
    # Omitted set smaller than llm_summary_min_omitted -> no LLM call, template kept.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    llm = _FakeSummaryLLM(content=_LLM_MARKER)
    config = _f6_config(llm_summary_min_omitted=1000)
    first = _prep_llm(
        llm, system_prompt="SYSTEM", history=_pairs(12, "A" * 180), chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in first.applied_stages
    assert llm.calls == []
    assert _LLM_MARKER not in first.system_prompt


def test_f6_disabled_flag_never_calls_llm(tmp_path):
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    llm = _FakeSummaryLLM(content=_LLM_MARKER)
    config = _f6_config(collapse_llm_summary_enabled=False)
    first = _prep_llm(
        llm, system_prompt="SYSTEM", history=_pairs(12, "A" * 180), chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in first.applied_stages
    assert llm.calls == []
    assert _LLM_MARKER not in first.system_prompt


def test_f6_reactive_path_stays_deterministic(tmp_path):
    # The emergency reactive/413 path must never fire the LLM refine even if a
    # client is threaded through, because it early-returns before the target branch.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    llm = _FakeSummaryLLM(content=_LLM_MARKER)
    config = _f6_config(reactive_preserve_messages=2, reactive_preserve_tokens=300)
    first = _prep_llm(
        llm, system_prompt="SYSTEM", history=_pairs(12, "A" * 180), chat_id="c",
        tool_result_store=store, config=config, force_reactive_compact=True,
    )
    assert STAGE_REACTIVE_COMPACT in first.applied_stages
    assert llm.calls == []
    assert _LLM_MARKER not in first.system_prompt


def test_f6_bytewise_denser_summary_rejected(tmp_path):
    # The F2 cap bounds BOTH code points and UTF-8 bytes: a summary with the SAME
    # code-point count as the template but more bytes (CJK vs ASCII) is rejected,
    # so a denser-encoded summary cannot silently balloon the real provider payload.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = _f6_config()
    hist = _pairs(12, "A" * 180)
    det = _prep(  # llm=None -> deterministic template
        system_prompt="SYSTEM", history=hist, chat_id="c0",
        tool_result_store=store, config=config,
    )
    tmpl = _collapse_section_body(det.persisted_summary)
    n = len(tmpl)
    cjk = "中" * n  # n code points (<= cap) but 3n bytes (> cap)
    assert len(cjk) <= n and len(cjk.encode("utf-8")) > len(tmpl.encode("utf-8"))
    cjk_res = _prep_llm(
        _FakeSummaryLLM(content=cjk), system_prompt="SYSTEM", history=hist,
        chat_id="c1", tool_result_store=store, config=config,
    )
    assert cjk not in cjk_res.system_prompt
    assert _collapse_section_body(cjk_res.persisted_summary) == tmpl  # fell back
    # An ASCII summary of the SAME code-point count passes both caps -> accepted.
    ascii_ok = "a" * n
    ok_res = _prep_llm(
        _FakeSummaryLLM(content=ascii_ok), system_prompt="SYSTEM", history=hist,
        chat_id="c2", tool_result_store=store, config=config,
    )
    assert _collapse_section_body(ok_res.persisted_summary) == ascii_ok  # accepted


def test_f6_token_denser_summary_rejected(tmp_path, monkeypatch):
    # ADR 0039: the F2 cap is now TOKEN-based. A summary that is FEWER chars than the
    # template but tokenizes LARGER (denser tokens) must be rejected — else the
    # re-hoisted system could re-cross the token trigger and re-collapse (F2 break).
    import omicsclaw.runtime.context.compaction as _compaction

    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = _f6_config()
    hist = _pairs(12, "A" * 180)
    det = _prep(  # llm=None -> deterministic template
        system_prompt="SYSTEM", history=hist, chat_id="c0",
        tool_result_store=store, config=config,
    )
    tmpl = _collapse_section_body(det.persisted_summary)

    marker = "SHORT"  # a short LLM summary — far fewer chars than the template
    assert len(marker) < len(tmpl)

    real = _compaction.estimate_text_tokens

    def fake_tokens(text, **kw):
        # The short LLM summary tokenizes huge; everything else is measured for real.
        return 10_000 if marker in text else real(text, **kw)

    monkeypatch.setattr(_compaction, "estimate_text_tokens", fake_tokens)

    res = _prep_llm(
        _FakeSummaryLLM(content=marker), system_prompt="SYSTEM", history=hist,
        chat_id="c1", tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in res.applied_stages
    # Rejected on the token cap despite being char-shorter -> the template is used.
    assert marker not in res.system_prompt
    assert _collapse_section_body(res.persisted_summary) == tmpl


def test_drops_required_tokens_unit():
    # B1 content-fidelity gate: a path (>=2 segments) or error marker present in the
    # omitted content but missing from the summary triggers a fallback.
    from omicsclaw.runtime.context.compaction import _drops_required_tokens

    omitted = "ran /data/study/sample.h5ad and hit a Traceback in step 2"
    assert _drops_required_tokens(omitted, "a summary without the path or error")
    assert not _drops_required_tokens(
        omitted, "kept /data/study/sample.h5ad and the Traceback"
    )
    assert not _drops_required_tokens("no special tokens here", "any summary")
    # A DIFFERENT (superstring) path does NOT satisfy the required path — extracted-
    # token comparison, not raw substring membership.
    assert _drops_required_tokens(
        "used /data/study/sample.h5ad", "used /data/study/sample.h5ad.bak"
    )
    # Trailing sentence punctuation on the omitted path must not cause a false reject.
    assert not _drops_required_tokens(
        "read /data/study/sample.h5ad.", "kept /data/study/sample.h5ad here"
    )
    # Pending-work markers are protected too (ADR 0039 D5).
    assert _drops_required_tokens("there is a pending TODO", "all done, nothing left")


def test_f6_drops_required_path_falls_back_to_template(tmp_path):
    # B1: an LLM summary that OMITS a file path present in the omitted content is
    # rejected (no retry) -> the deterministic template is used instead.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = _f6_config()
    path = "/data/study/sample.h5ad"
    hist = [
        {"role": "user", "content": f"please read {path} " + ("Q" * 200)},
        {"role": "assistant", "content": "reading " + ("R" * 200)},
    ] + _pairs(10, "A" * 180)
    res = _prep_llm(
        _FakeSummaryLLM(content="condensed the whole conversation, ran some analysis"),
        system_prompt="SYSTEM", history=hist, chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in res.applied_stages
    # The path was omitted; the LLM summary dropped it -> gate rejected it.
    assert "condensed the whole conversation" not in res.persisted_summary


def test_collapse_llm_summary_default_on():
    # ADR 0039 D5: default-ON now that the token cap + B1 gate make it safe.
    assert ContextCompactionConfig().collapse_llm_summary_enabled is True


def test_f6_llm_sees_omitted_history_and_antimimicry_prompt(tmp_path):
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    llm = _FakeSummaryLLM(content="SHORT")
    config = _f6_config()
    first = _prep_llm(
        llm, system_prompt="SYSTEM", history=_pairs(12, "A" * 180), chat_id="c",
        tool_result_store=store, config=config,
    )
    assert STAGE_CONTEXT_COLLAPSE in first.applied_stages
    assert len(llm.calls) == 1
    system_msg = llm.calls[0]["messages"][0]
    user_msg = llm.calls[0]["messages"][1]
    # The oldest omitted message reaches the summarizer (full omitted set fed).
    assert "user step 0" in user_msg["content"]
    # The summarizer is instructed against reproducing tool-call syntax (anti-mimicry).
    assert system_msg["role"] == "system"
    assert "tool-call syntax" in system_msg["content"]


def test_f6_tool_call_shaped_summary_rejected(tmp_path):
    # P2 anti-mimicry: a summary that imitates a tool/function call must be rejected
    # to the template so it can't be persisted into the context and induce the model
    # to mimic tool calls on later turns. Plain past-tense prose is still accepted.
    store = ToolResultStore(storage_dir=tmp_path / "tr")
    config = _f6_config()
    hist = _pairs(12, "A" * 180)
    det = _prep(
        system_prompt="SYSTEM", history=hist, chat_id="c0",
        tool_result_store=store, config=config,
    )
    tmpl = _collapse_section_body(det.persisted_summary)

    for i, bad in enumerate(
        [
            '{"tool": "run_skill", "arguments": {}}',
            '<tool_call name="Read">/data</tool_call>',
            'The assistant did work; "arguments": {"path": "/x"} were used.',
        ]
    ):
        res = _prep_llm(
            _FakeSummaryLLM(content=bad), system_prompt="SYSTEM", history=hist,
            chat_id=f"bad{i}", tool_result_store=store, config=config,
        )
        assert bad not in res.system_prompt
        assert _collapse_section_body(res.persisted_summary) == tmpl  # fell back

    good = "The user asked to profile data; the assistant read config.py and edited it."
    ok = _prep_llm(
        _FakeSummaryLLM(content=good), system_prompt="SYSTEM", history=hist,
        chat_id="good", tool_result_store=store, config=config,
    )
    assert _collapse_section_body(ok.persisted_summary) == good  # accepted (no markers)
