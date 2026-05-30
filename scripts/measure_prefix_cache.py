"""Measure DeepSeek prompt-prefix cache hit rate, before vs after ADR 0024.

A controlled A/B over one multi-turn omics dialogue, using the REAL OmicsClaw
system prompt + tool list, hitting the configured DeepSeek endpoint and reading
its actual ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``:

* **before** — reproduces the pre-ADR-0024 cache-busters: the tool list is
  re-gated per query (``surface_only=False``) and the query-volatile layers are
  baked back into the *system* prefix, so the prefix changes every turn.
* **after**  — the ADR-0024 behaviour: a frozen (surface-only) tool list and a
  query-independent stable system prefix; volatile content rides the user turn.

Usage (reads the project ``.env`` for ``LLM_API_KEY`` / ``LLM_BASE_URL`` /
``OMICSCLAW_MODEL``)::

    python scripts/measure_prefix_cache.py

Costs a handful of real API calls. Not a test; run manually to (re)fill the
before/after numbers in docs/plans/0024-prompt-prefix-caching.md.
"""

from __future__ import annotations

import asyncio
import os

from openai import AsyncOpenAI

from omicsclaw.common.runtime_env import load_project_dotenv
from omicsclaw.runtime.context.assembler import assemble_prompt_context
from omicsclaw.runtime.context.layers import ContextAssemblyRequest
from omicsclaw.runtime.tools.builders.agent import (
    build_bot_tool_specs,
    build_default_bot_tool_context,
)
from omicsclaw.runtime.tools.registry import select_tool_specs

_ROOT = "/data/beifen/zhouwg_data/project/OmicsClaw"

# Diverse turns: each trips a different query-keyword gate (file / method /
# web / memory / pdf), so the BEFORE arm's tool list + system prefix churn.
TURNS = [
    "Load my Visium data at /tmp/sample.h5ad and run quality control.",
    "Now detect spatial domains with leiden clustering.",
    "Which method should I use for batch correction — Harmony or scVI?",
    "Search the web for recent CARD deconvolution benchmarks.",
    "请记住我以后默认用 leiden 聚类。",
    "Extract the GEO accession from /tmp/paper.pdf.",
]


def _assembly(query: str):
    """Return (stable_system, volatile_message) from the real assembler."""
    a = assemble_prompt_context(
        request=ContextAssemblyRequest(
            surface="bot",
            base_persona="",
            query=query,
            include_knowhow=True,
        )
    )
    return a.system_prompt, a.message_context


def _tools(query: str, *, surface_only: bool):
    specs = build_bot_tool_specs(build_default_bot_tool_context())
    req = ContextAssemblyRequest(surface="bot", query=query)
    return [s.to_openai_tool() for s in select_tool_specs(specs, request=req, surface_only=surface_only)]


def _hit_miss(usage) -> tuple[int, int]:
    return (
        int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0),
        int(getattr(usage, "prompt_cache_miss_tokens", 0) or 0),
    )


async def _run_arm(client: AsyncOpenAI, model: str, *, stable: bool) -> list[tuple[int, int]]:
    """Run the dialogue once; return [(hit, miss)] per turn."""
    history: list[dict] = []
    per_turn: list[tuple[int, int]] = []

    # AFTER: frozen tool list + query-independent system, computed once.
    frozen_tools = _tools("", surface_only=True) if stable else None
    stable_system, _ = _assembly("") if stable else (None, None)

    for query in TURNS:
        sys_stable, volatile = _assembly(query)
        if stable:
            system = stable_system
            tools = frozen_tools
            user_content = (f"{volatile}\n\n{query}" if volatile else query)
        else:
            # BEFORE: volatile baked into system (per-turn) + re-gated tools.
            system = f"{sys_stable}\n\n{volatile}" if volatile else sys_stable
            tools = _tools(query, surface_only=False)
            user_content = query

        history.append({"role": "user", "content": user_content})
        messages = [{"role": "system", "content": system}] + history
        resp = await client.chat.completions.create(
            model=model, max_tokens=40, messages=messages, tools=tools or None,
        )
        per_turn.append(_hit_miss(resp.usage))
        history.append(
            {"role": "assistant", "content": resp.choices[0].message.content or "(ok)"}
        )
    return per_turn


def _report(label: str, per_turn: list[tuple[int, int]]) -> tuple[int, int]:
    print(f"\n=== {label} ===")
    print(f"{'turn':>4} {'hit':>8} {'miss':>8} {'ratio':>7}")
    th = tm = 0
    for i, (h, m) in enumerate(per_turn, 1):
        th += h
        tm += m
        r = h / (h + m) if (h + m) else 0.0
        print(f"{i:>4} {h:>8} {m:>8} {r:>6.0%}")
    agg = th / (th + tm) if (th + tm) else 0.0
    print(f"  total hit={th} miss={tm}  session hit-ratio={agg:.1%}")
    return th, tm


async def main() -> None:
    load_project_dotenv(_ROOT)
    key = os.environ.get("LLM_API_KEY", "")
    base = os.environ.get("LLM_BASE_URL", "")
    model = os.environ.get("OMICSCLAW_MODEL", "") or "deepseek-chat"
    if not key:
        raise SystemExit("LLM_API_KEY not set (checked project .env)")
    print(f"model={model} base={base or '(default)'}  turns={len(TURNS)}")

    kw = {"api_key": key}
    if base:
        kw["base_url"] = base
    async with AsyncOpenAI(**kw) as client:
        # AFTER first (warms its own stable prefix); BEFORE uses different
        # per-turn prefixes so there is no cross-arm contamination.
        after = await _run_arm(client, model, stable=True)
        before = await _run_arm(client, model, stable=False)

    bh, bm = _report("BEFORE (pre-ADR-0024: per-turn tool/system churn)", before)
    ah, am = _report("AFTER (ADR-0024: stable prefix + append-only history)", after)
    br = bh / (bh + bm) if (bh + bm) else 0.0
    ar = ah / (ah + am) if (ah + am) else 0.0
    print("\n=== SUMMARY ===")
    print(f"before session hit-ratio = {br:.1%}")
    print(f"after  session hit-ratio = {ar:.1%}")


if __name__ == "__main__":
    asyncio.run(main())
