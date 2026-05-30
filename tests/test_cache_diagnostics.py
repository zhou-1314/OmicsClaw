"""Unit tests for prompt-prefix cache diagnostics (ADR 0017, Phase 0).

Pure-function coverage of the measurement oracle: provider token extraction,
segment hashing, and miss-reason inference. No loop / I/O integration here —
that lands in the multi-turn stub-provider test (Phase 4).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omicsclaw.runtime.agent.cache_diagnostics import (
    REASON_COLD_START,
    REASON_HISTORY_SHIFTED,
    REASON_NONE,
    REASON_SYSTEM_CHANGED,
    REASON_TOOL_LIST_CHANGED,
    CacheTokens,
    compute_segment_hash,
    extract_cache_tokens,
    infer_miss_reason,
)


# --------------------------------------------------------------------------- #
# extract_cache_tokens — per-provider formats
# --------------------------------------------------------------------------- #


def test_deepseek_explicit_hit_and_miss():
    usage = SimpleNamespace(
        prompt_tokens=1000,
        prompt_cache_hit_tokens=900,
        prompt_cache_miss_tokens=100,
    )
    tokens = extract_cache_tokens(usage)
    assert tokens == CacheTokens(hit=900, miss=100)
    assert tokens.ratio == pytest.approx(0.9)
    assert tokens.has_signal is True


def test_deepseek_cold_miss_is_distinguished_from_no_signal():
    # First turn on DeepSeek: all tokens are a miss, but the fields ARE present.
    usage = SimpleNamespace(
        prompt_tokens=1000,
        prompt_cache_hit_tokens=0,
        prompt_cache_miss_tokens=1000,
    )
    tokens = extract_cache_tokens(usage)
    assert tokens == CacheTokens(hit=0, miss=1000)
    assert tokens.has_signal is True
    assert tokens.ratio == 0.0


def test_openai_cached_tokens_in_details():
    usage = SimpleNamespace(
        prompt_tokens=1000,
        prompt_tokens_details=SimpleNamespace(cached_tokens=750),
    )
    tokens = extract_cache_tokens(usage)
    assert tokens == CacheTokens(hit=750, miss=250)


def test_openai_zero_cached_is_real_miss_when_details_present():
    usage = SimpleNamespace(
        prompt_tokens=400,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    tokens = extract_cache_tokens(usage)
    assert tokens == CacheTokens(hit=0, miss=400)
    assert tokens.has_signal is True


def test_anthropic_read_and_creation():
    usage = SimpleNamespace(
        input_tokens=50,
        cache_read_input_tokens=800,
        cache_creation_input_tokens=150,
    )
    tokens = extract_cache_tokens(usage)
    # hit = cache_read; miss = creation + uncached input
    assert tokens == CacheTokens(hit=800, miss=200)


def test_no_cache_fields_returns_no_signal():
    usage = SimpleNamespace(prompt_tokens=1234, completion_tokens=10)
    tokens = extract_cache_tokens(usage)
    assert tokens == CacheTokens(0, 0)
    assert tokens.has_signal is False
    assert tokens.ratio == 0.0


def test_none_usage_is_safe():
    assert extract_cache_tokens(None) == CacheTokens(0, 0)


def test_garbage_field_values_never_raise():
    usage = SimpleNamespace(
        prompt_cache_hit_tokens="not-a-number",
        prompt_cache_miss_tokens=None,
    )
    # _present() is True (hit field is non-None), values coerce to 0.
    assert extract_cache_tokens(usage) == CacheTokens(0, 0)


def test_deepseek_takes_precedence_over_openai_shape():
    # An object carrying both shapes resolves as DeepSeek (checked first).
    usage = SimpleNamespace(
        prompt_tokens=1000,
        prompt_cache_hit_tokens=600,
        prompt_cache_miss_tokens=400,
        prompt_tokens_details=SimpleNamespace(cached_tokens=999),
    )
    assert extract_cache_tokens(usage) == CacheTokens(hit=600, miss=400)


# --------------------------------------------------------------------------- #
# compute_segment_hash — determinism & sensitivity
# --------------------------------------------------------------------------- #


def test_hash_is_deterministic_for_strings():
    assert compute_segment_hash("system prompt") == compute_segment_hash(
        "system prompt"
    )


def test_hash_str_and_bytes_agree():
    assert compute_segment_hash("abc") == compute_segment_hash(b"abc")


def test_hash_ignores_dict_key_order_but_not_list_order():
    a = [{"name": "x", "type": "function"}, {"name": "y", "type": "function"}]
    a_reordered_keys = [
        {"type": "function", "name": "x"},
        {"type": "function", "name": "y"},
    ]
    a_reordered_list = [
        {"name": "y", "type": "function"},
        {"name": "x", "type": "function"},
    ]
    # Key order within each tool must not change the hash...
    assert compute_segment_hash(a) == compute_segment_hash(a_reordered_keys)
    # ...but tool list order (which the provider keys on) must.
    assert compute_segment_hash(a) != compute_segment_hash(a_reordered_list)


def test_hash_detects_content_change():
    base = [{"name": "x"}]
    changed = [{"name": "x"}, {"name": "z"}]
    assert compute_segment_hash(base) != compute_segment_hash(changed)


# --------------------------------------------------------------------------- #
# infer_miss_reason — every branch
# --------------------------------------------------------------------------- #


def test_cold_start_when_no_prior_hashes():
    reason = infer_miss_reason(
        prev_tool_hash=None,
        prev_system_hash=None,
        cur_tool_hash="t1",
        cur_system_hash="s1",
        tokens=CacheTokens(hit=0, miss=1000),
    )
    assert reason == REASON_COLD_START


def test_tool_list_changed():
    reason = infer_miss_reason(
        prev_tool_hash="t1",
        prev_system_hash="s1",
        cur_tool_hash="t2",
        cur_system_hash="s1",
        tokens=CacheTokens(hit=0, miss=1000),
    )
    assert reason == REASON_TOOL_LIST_CHANGED


def test_system_changed_when_tools_stable():
    reason = infer_miss_reason(
        prev_tool_hash="t1",
        prev_system_hash="s1",
        cur_tool_hash="t1",
        cur_system_hash="s2",
        tokens=CacheTokens(hit=0, miss=1000),
    )
    assert reason == REASON_SYSTEM_CHANGED


def test_history_shifted_when_prefix_stable_but_zero_hit():
    reason = infer_miss_reason(
        prev_tool_hash="t1",
        prev_system_hash="s1",
        cur_tool_hash="t1",
        cur_system_hash="s1",
        tokens=CacheTokens(hit=0, miss=1000),
    )
    assert reason == REASON_HISTORY_SHIFTED


def test_none_when_prefix_stable_and_hit():
    reason = infer_miss_reason(
        prev_tool_hash="t1",
        prev_system_hash="s1",
        cur_tool_hash="t1",
        cur_system_hash="s1",
        tokens=CacheTokens(hit=900, miss=100),
    )
    assert reason == REASON_NONE


def test_tool_change_takes_precedence_over_system_change():
    reason = infer_miss_reason(
        prev_tool_hash="t1",
        prev_system_hash="s1",
        cur_tool_hash="t2",
        cur_system_hash="s2",
        tokens=CacheTokens(hit=0, miss=1000),
    )
    assert reason == REASON_TOOL_LIST_CHANGED
