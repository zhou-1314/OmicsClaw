from omicsclaw.runtime.context.budget import (
    estimate_message_size,
    estimate_message_tokens,
    trim_history_to_budget,
)


def test_estimate_message_size_accounts_for_text_and_tool_call_fields():
    size = estimate_message_size(
        {
            "role": "assistant",
            "content": "hello",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "inspect_data", "arguments": '{"path":"a.h5ad"}'},
                }
            ],
        }
    )

    assert size >= len("assistanthello")
    assert size >= len("call-1")
    assert size >= len("inspect_data")


def test_trim_history_to_budget_keeps_newest_contiguous_suffix():
    trimmed = trim_history_to_budget(
        [
            {"role": "user", "content": "old user"},
            {"role": "assistant", "content": "old assistant"},
            {"role": "user", "content": "latest user"},
            {"role": "assistant", "content": "latest assistant"},
        ],
        max_messages=2,
    )

    assert trimmed == [
        {"role": "user", "content": "latest user"},
        {"role": "assistant", "content": "latest assistant"},
    ]


def test_trim_history_to_budget_treats_tool_bundle_as_single_suffix_block():
    history = [
        {"role": "user", "content": "older context"},
        {"role": "assistant", "content": "older answer"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "inspect_data", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "tool output"},
        {"role": "assistant", "content": "final answer"},
    ]

    trimmed = trim_history_to_budget(history, max_messages=3)

    assert trimmed == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "inspect_data", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "tool output"},
        {"role": "assistant", "content": "final answer"},
    ]


def test_trim_history_to_budget_still_keeps_latest_block_when_char_budget_is_tiny():
    trimmed = trim_history_to_budget(
        [
            {"role": "user", "content": "older context"},
            {"role": "assistant", "content": "this response is very long and should dominate the budget"},
        ],
        max_messages=10,
        max_chars=5,
    )

    assert trimmed == [
        {
            "role": "assistant",
            "content": "this response is very long and should dominate the budget",
        }
    ]


def test_estimate_message_size_counts_inline_image_block():
    # F4: an inline base64 image otherwise contributes only its ~9-char
    # "image_url" type string, so a multimodal turn is near-invisible to the
    # char budget and can silently overflow the model window.
    base64_blob = "A" * 100_000
    size = estimate_message_size(
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{base64_blob}"},
                }
            ],
        }
    )
    # Image now costs a real, bounded budget (was ~13 chars: role + type);
    # threshold locks the surcharge design against silent degradation to ~0.
    assert size >= 2000


def test_estimate_message_size_does_not_count_full_base64_image_length():
    base64_blob = "A" * 100_000
    size = estimate_message_size(
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{base64_blob}"},
                }
            ],
        }
    )
    # Bounded surcharge, NOT the raw base64 length — else every image turn would
    # blow max_prompt_chars and trigger reactive collapse.
    assert size < len(base64_blob)


def test_estimate_message_size_charges_each_image_block():
    def message_with(n_images):
        return {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
                for _ in range(n_images)
            ],
        }

    one = estimate_message_size(message_with(1))
    two = estimate_message_size(message_with(2))
    # Each image is charged (per-block surcharge), so two cost meaningfully more.
    assert two - one >= 1000


def test_estimate_message_size_still_counts_text_blocks_in_list_content():
    size = estimate_message_size(
        {"role": "user", "content": [{"type": "text", "text": "hello world"}]}
    )
    # The image short-circuit must not skip normal text blocks.
    assert size >= len("user") + len("text") + len("hello world")


def test_estimate_message_size_counts_text_in_mixed_image_block():
    # A block carrying BOTH an image payload and text must count both — the image
    # surcharge must not short-circuit past same-block text.
    caption = "a long descriptive caption of the tissue section"
    image_only = estimate_message_size(
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:,x"}}]}
    )
    with_caption = estimate_message_size(
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:,x"},
                    "text": caption,
                }
            ],
        }
    )
    assert with_caption >= image_only + len(caption)


def test_estimate_message_size_counts_anthropic_style_image_block():
    size = estimate_message_size(
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
                }
            ],
        }
    )
    # Anthropic-style block (type=image, no image_url key) is still charged.
    assert size >= 2000


def test_estimate_message_size_counts_text_even_when_image_url_key_present():
    # A predominantly-text block that also carries an image_url key must not lose
    # its text (the image detector may over-charge, but must never drop text).
    caption = "important caption text"
    size = estimate_message_size(
        {
            "role": "user",
            "content": [{"type": "text", "text": caption, "image_url": {"url": "data:,x"}}],
        }
    )
    assert size >= len(caption)


def test_effective_context_capacity_subtracts_reserve_and_margin():
    # §9.3: usable input budget = model window − reserved output − safety margin.
    from omicsclaw.runtime.context.budget import effective_context_capacity

    assert effective_context_capacity(
        context_window=128_000, reserved_output=4096, safety_margin=2048
    ) == 128_000 - 4096 - 2048
    # Never negative.
    assert effective_context_capacity(
        context_window=1000, reserved_output=4096, safety_margin=2048
    ) == 0


def test_classify_context_budget_five_level_thresholds():
    # §9.3: OK <65% ≤ WARNING <80% ≤ COMPRESS <90% ≤ CRITICAL <96% ≤ BLOCK.
    from omicsclaw.runtime.context.budget import (
        ContextBudgetStatus,
        classify_context_budget,
    )

    cap = 1000
    assert classify_context_budget(640, cap) == ContextBudgetStatus.OK
    assert classify_context_budget(650, cap) == ContextBudgetStatus.WARNING
    assert classify_context_budget(800, cap) == ContextBudgetStatus.COMPRESS
    assert classify_context_budget(900, cap) == ContextBudgetStatus.CRITICAL
    assert classify_context_budget(960, cap) == ContextBudgetStatus.BLOCK
    # Degenerate capacity never divides by zero; treat as fully used.
    assert classify_context_budget(10, 0) == ContextBudgetStatus.BLOCK


def test_local_budget_status_classifies_against_prompt_char_budget():
    # §9.3 slice 3: local pressure is chars/max_prompt_chars — the real binding
    # compaction budget. Unlike the window-relative status (which is ~always OK
    # for large-window models because the char budget caps context far below the
    # window), this stays decision-useful and can drive compress-to-target.
    from omicsclaw.runtime.context.budget import (
        ContextBudgetStatus,
        local_budget_status,
    )

    budget = 100_000
    assert local_budget_status(10_000, budget) == ContextBudgetStatus.OK  # 10%
    assert local_budget_status(70_000, budget) == ContextBudgetStatus.WARNING  # 70%
    assert local_budget_status(85_000, budget) == ContextBudgetStatus.COMPRESS  # 85%
    assert local_budget_status(93_000, budget) == ContextBudgetStatus.CRITICAL  # 93%
    assert local_budget_status(99_000, budget) == ContextBudgetStatus.BLOCK  # 99%


# --------------------------------------------------------------------------- #
# ADR 0039 Batch 1 — token estimator (estimate_message_tokens)
# --------------------------------------------------------------------------- #


def test_estimate_message_tokens_counts_text_and_tool_call_fields():
    # ADR 0039 D1: token analogue of estimate_message_size — same structural walk,
    # counted in tokens. Text-bearing fields (incl. tool-call arguments) contribute.
    tokens = estimate_message_tokens(
        {
            "role": "assistant",
            "content": "hello there, this is a message",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "inspect_data",
                        "arguments": '{"path":"a.h5ad","layer":"counts"}',
                    },
                }
            ],
        }
    )
    content_only = estimate_message_tokens(
        {"role": "assistant", "content": "hello there, this is a message"}
    )
    assert tokens > content_only > 0


def test_estimate_message_tokens_is_smaller_than_char_estimate():
    # ADR 0039: one coherent unit. For ASCII text the token count is ~chars/4,
    # always well below the char count — a sanity check that we count tokens.
    message = {"role": "user", "content": "The quick brown fox jumps over the lazy dog. " * 20}
    tokens = estimate_message_tokens(message)
    chars = estimate_message_size(message)
    assert 0 < tokens < chars
    assert tokens >= chars // 6  # not absurdly small


def test_estimate_message_tokens_charges_bounded_image_surcharge_not_base64():
    # ADR 0039 S2 (multimodal): an inline base64 image is charged a bounded
    # per-image token surcharge, NOT tokenized (which would catastrophically
    # over-count the base64) and NOT dropped (which would under-count to ~0).
    from omicsclaw.runtime.context.budget import _IMAGE_BUDGET_TOKENS

    huge_b64 = "A" * 100_000  # ~100 KB → ~25k tokens if it were tokenized
    tokens = estimate_message_tokens(
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{huge_b64}"}}
            ],
        }
    )
    assert _IMAGE_BUDGET_TOKENS <= tokens < _IMAGE_BUDGET_TOKENS + 500


def test_estimate_message_tokens_charges_each_image_surcharge():
    from omicsclaw.runtime.context.budget import _IMAGE_BUDGET_TOKENS

    def message_with(n_images):
        return {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
                for _ in range(n_images)
            ],
        }

    one = estimate_message_tokens(message_with(1))
    two = estimate_message_tokens(message_with(2))
    # Each extra image adds the bounded surcharge (plus its own tiny type token,
    # mirroring estimate_message_size which also counts the block "type" string).
    assert _IMAGE_BUDGET_TOKENS <= two - one < _IMAGE_BUDGET_TOKENS + 20


def test_estimate_message_tokens_preserves_text_in_mixed_image_block():
    # A block carrying both text and an image_url must still count its text; the
    # image surcharge must not short-circuit past real text.
    caption = "important caption text that must be counted as tokens " * 5
    with_caption = estimate_message_tokens(
        {"role": "user", "content": [{"type": "text", "text": caption, "image_url": {"url": "data:,x"}}]}
    )
    image_only = estimate_message_tokens(
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:,x"}}]}
    )
    assert with_caption > image_only


def test_estimate_message_tokens_empty_message_is_near_zero():
    # No text, no tool calls → ~0 tokens (only the tiny role string).
    assert estimate_message_tokens({"role": "user", "content": ""}) <= 1


def test_estimate_message_tokens_anthropic_image_block_bounded():
    # Anthropic-style block (type=image, base64 under source.data) is charged the
    # bounded surcharge; the base64 must not leak into the count.
    from omicsclaw.runtime.context.budget import _IMAGE_BUDGET_TOKENS

    tokens = estimate_message_tokens(
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "A" * 50_000},
                }
            ],
        }
    )
    assert _IMAGE_BUDGET_TOKENS <= tokens < _IMAGE_BUDGET_TOKENS + 500


def test_estimate_message_tokens_counts_tool_call_id_and_reasoning():
    # Parity with estimate_message_size: tool_call_id and reasoning_content both
    # contribute to the token footprint.
    with_id = estimate_message_tokens({"role": "tool", "tool_call_id": "call-abcdef", "content": "r"})
    without_id = estimate_message_tokens({"role": "tool", "content": "r"})
    assert with_id > without_id

    with_reasoning = estimate_message_tokens(
        {"role": "assistant", "content": "x", "reasoning_content": "long chain of thought " * 20}
    )
    without_reasoning = estimate_message_tokens({"role": "assistant", "content": "x"})
    assert with_reasoning > without_reasoning


def test_estimate_message_tokens_handles_non_dict_function_and_tool_call():
    # Robustness parity: a non-dict function block or non-dict tool_call must be
    # counted via str(), never raise.
    non_dict_function = estimate_message_tokens(
        {
            "role": "assistant",
            "content": "x",
            "tool_calls": [{"id": "c", "type": "function", "function": "not-a-dict-blob"}],
        }
    )
    non_dict_tool_call = estimate_message_tokens(
        {"role": "assistant", "content": "x", "tool_calls": ["not-a-dict-tool-call"]}
    )
    content_only = estimate_message_tokens({"role": "assistant", "content": "x"})
    assert non_dict_function > content_only
    assert non_dict_tool_call > content_only


def test_estimate_text_tokens_counts_string():
    from omicsclaw.runtime.context.budget import estimate_text_tokens

    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("hello world this is some text") > 0
    text = "a" * 100
    assert 0 < estimate_text_tokens(text) < len(text)  # tokens < chars for ASCII


def test_trim_history_to_budget_accepts_token_size_fn():
    # ADR 0039: the same block-aware trim, measured in tokens. Under the same
    # numeric budget the token-sized trim keeps MORE messages than the char-sized
    # one (tokens ~= chars/4 are smaller, so more fit).
    history = [{"role": "user", "content": "word " * 50} for _ in range(10)]
    by_chars = trim_history_to_budget(history, max_messages=-1, max_chars=600)
    by_tokens = trim_history_to_budget(
        history, max_messages=-1, max_chars=600, size_fn=estimate_message_tokens
    )
    assert len(by_tokens) > len(by_chars)


def test_estimate_prompt_tokens_sums_system_and_messages():
    from omicsclaw.runtime.context.compaction import (
        estimate_prompt_chars,
        estimate_prompt_tokens,
    )

    system = "You are a helpful multi-omics agent. " * 10
    messages = [
        {"role": "user", "content": "please cluster " * 20},
        {"role": "assistant", "content": "running clustering " * 20},
    ]
    toks = estimate_prompt_tokens(system, messages)
    chars = estimate_prompt_chars(system, messages)
    assert 0 < toks < chars  # token estimate below char estimate


def test_local_budget_status_none_when_char_budget_unbounded():
    # No local char budget configured (None / <=0) -> no local status, so the
    # field stays absent rather than misreporting BLOCK on an unbounded prompt.
    from omicsclaw.runtime.context.budget import local_budget_status

    assert local_budget_status(50_000, None) is None
    assert local_budget_status(50_000, 0) is None
    assert local_budget_status(50_000, -1) is None
