"""Regression tests for #3 cache-safe token-economics work (diagnose 2026-06-26).

Two cache-safe levers (ADR 0024 forbids reverting to per-turn tool filtering):

* **L1 — surface cache economics.** The desktop usage payload exposes
  ``cache_hit_ratio`` and ``fresh_input_tokens`` so the gross ``input_tokens``
  total is interpretable (most re-sent input is cheap cache hits, not fresh
  compute). Completes ADR 0024 Resolution #5's surfacing.
* **L3 — slim the ``omicsclaw`` tool schema.** The ``skill`` enum is sent every
  turn; legacy aliases are dropped from it (they still resolve at the executor),
  keeping the frozen prefix byte-stable but smaller.
"""

from __future__ import annotations

from types import SimpleNamespace

# Resolve the agent_executors <-> agent.state import cycle before touching the
# live registry/spec (see test_autonomous_digest for the same guard).
import omicsclaw.runtime.agent.state as state  # noqa: F401
from omicsclaw.skill.registry import ensure_registry_loaded


# ── L3: canonical-only skill enum ────────────────────────────────────


def test_canonical_aliases_exclude_legacy_but_legacy_still_resolves():
    reg = ensure_registry_loaded()
    canonical = reg.canonical_skill_aliases()
    # Canonical name is present; its legacy alias is not in the enum source...
    assert "bulkrna-deconvolution" in canonical
    assert "bulk-deconv" not in canonical
    # ...but the legacy alias still resolves at the executor (no capability loss).
    assert "bulk-deconv" in reg.skills
    assert "auto" not in canonical  # 'auto' is appended by the tool builder, not a skill


def test_omicsclaw_enum_is_canonical_plus_auto_and_smaller():
    reg = state.get_tool_registry()
    spec = next(s for s in reg.specs if s.name == "omicsclaw")
    enum = spec.to_openai_tool()["function"]["parameters"]["properties"]["skill"]["enum"]
    assert "auto" in enum
    assert "bulkrna-deconvolution" in enum
    # No legacy aliases leak into the per-turn schema.
    assert "bulk-deconv" not in enum
    assert "bulk-survival" not in enum
    # The enum equals canonical names + 'auto' exactly (deterministic → cache-stable).
    assert set(enum) == set(ensure_registry_loaded().canonical_skill_aliases()) | {"auto"}


def test_canonical_aliases_are_deterministic_for_cache_stability():
    reg = ensure_registry_loaded()
    assert reg.canonical_skill_aliases() == reg.canonical_skill_aliases()


# ── L1: desktop cache economics in the usage payload ─────────────────


def _usage_totals():
    return {
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "cache_read_input_tokens": 0.0,
        "cache_creation_input_tokens": 0.0,
    }


def test_usage_payload_exposes_hit_ratio_and_fresh_tokens(monkeypatch):
    from omicsclaw.surfaces.desktop import server

    # _build_token_usage calls _get_core() for pricing; a bare object skips the
    # cost path (no _get_token_price attr) without needing a live core.
    monkeypatch.setattr(server, "_core", object(), raising=False)

    # DeepSeek-shaped usage: 20k prompt tokens, 18k of them a cache hit.
    usage = SimpleNamespace(
        prompt_tokens=20000, completion_tokens=500, prompt_cache_hit_tokens=18000
    )
    payload = server._build_token_usage(usage, _usage_totals(), model="deepseek-v4-pro")

    assert payload["input_tokens"] == 20000
    assert payload["cache_read_input_tokens"] == 18000
    assert payload["cache_hit_ratio"] == 0.9
    # Fresh = gross - cached: the tokens actually re-processed at full price.
    assert payload["fresh_input_tokens"] == 2000


def test_usage_payload_cost_discounts_cache_reads(monkeypatch):
    # F: cost_usd must bill cache reads at a discount, consistent with the
    # cache_hit_ratio/fresh_input_tokens in the same payload (not full price).
    from omicsclaw.surfaces.desktop import server

    fake_core = SimpleNamespace(
        _get_token_price=lambda m: (10.0, 30.0),  # input=10, output=30 per 1M
        _cache_read_discount=lambda: 0.1,
        OMICSCLAW_MODEL="x",
    )
    monkeypatch.setattr(server, "_core", fake_core, raising=False)
    usage = SimpleNamespace(prompt_tokens=20000, completion_tokens=500, prompt_cache_hit_tokens=18000)
    payload = server._build_token_usage(usage, _usage_totals(), model="x")

    # fresh=2000@10 + cached=18000@10*0.1 + output=500@30, all /1e6
    expected = (2000 * 10.0 + 18000 * 10.0 * 0.1 + 500 * 30.0) / 1_000_000
    assert payload["cost_usd"] == round(expected, 6)
    # Strictly cheaper than billing all 20k input at full price.
    full = (20000 * 10.0 + 500 * 30.0) / 1_000_000
    assert payload["cost_usd"] < round(full, 6)


def test_usage_payload_omits_cache_fields_when_no_cache(monkeypatch):
    from omicsclaw.surfaces.desktop import server

    monkeypatch.setattr(server, "_core", object(), raising=False)
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=100)
    payload = server._build_token_usage(usage, _usage_totals(), model="x")

    assert payload["input_tokens"] == 1000
    # No cache signal -> no ratio/fresh fields (avoids a misleading 0%).
    assert "cache_hit_ratio" not in payload
    assert "fresh_input_tokens" not in payload
