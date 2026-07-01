from omicsclaw.runtime.context.budget import (
    estimate_message_size,
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
    # §9.3: usable input budget = model window − reserved output − safety margin
    # (cellclaw ModelSpec.effective_capacity).
    from omicsclaw.runtime.context.budget import effective_context_capacity

    assert effective_context_capacity(
        context_window=128_000, reserved_output=4096, safety_margin=2048
    ) == 128_000 - 4096 - 2048
    # Never negative.
    assert effective_context_capacity(
        context_window=1000, reserved_output=4096, safety_margin=2048
    ) == 0


def test_classify_context_budget_five_level_thresholds():
    # §9.3: OK <65% ≤ WARNING <80% ≤ COMPRESS <90% ≤ CRITICAL <96% ≤ BLOCK
    # (cellclaw ModelSpec.classify_usage).
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
