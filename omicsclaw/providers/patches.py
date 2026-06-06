"""Runtime patches and discovery helpers for LLM provider quirks.

Two leaf utilities, both must be invoked explicitly — no module-import-time
side effects. Both never raise into callers; failure modes degrade to the
unpatched behavior or an empty result.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# DeepSeek reasoning_content multi-turn passback
# ---------------------------------------------------------------------------
#
# DeepSeek V3.x/V4 thinking-mode endpoints require every historical
# assistant message in the request payload to carry a ``reasoning_content``
# field. Without it, multi-turn requests fail with HTTP 400 "The
# reasoning_content in the thinking mode must be passed back".
#
# OmicsClaw's autoagent uses the raw OpenAI SDK and does not capture
# ``reasoning_content`` from past responses, so we inject an empty string
# fallback for any assistant message that lacks the field. Empirically
# tolerated by both thinking and non-thinking DeepSeek endpoints.
# ---------------------------------------------------------------------------


def apply_deepseek_reasoning_passback(
    messages: list[Any],
) -> list[Any]:
    """Return a new list with ``reasoning_content`` injected on assistant messages.

    Caller's dicts are not mutated — non-assistant messages and non-dict items
    pass through by reference.

    Args:
        messages: chat-completions style messages list.

    Returns:
        New list. Each assistant dict that lacks ``reasoning_content`` gets
        a copy with ``reasoning_content=""`` injected.
    """
    out: list[Any] = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        if msg.get("role") != "assistant":
            out.append(msg)
            continue
        if "reasoning_content" in msg:
            out.append(msg)
            continue
        copy = dict(msg)
        copy["reasoning_content"] = ""
        out.append(copy)
    return out


# ---------------------------------------------------------------------------
# Ollama installed-model discovery
# ---------------------------------------------------------------------------


def discover_ollama_models(base_url: str | None, *, timeout: float = 5.0) -> list[str]:
    """Probe ``GET {base_url}/api/tags`` for installed models.

    Returns the list of model names, or ``[]`` on any failure (no URL,
    connection error, non-200 status, malformed JSON).
    Never raises.
    """
    if not base_url or not isinstance(base_url, str):
        return []
    try:
        import httpx

        resp = httpx.get(
            f"{base_url.rstrip('/')}/api/tags", timeout=timeout
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []
    models = data.get("models", []) if isinstance(data, dict) else []
    return [
        m.get("name", "") for m in models
        if isinstance(m, dict) and m.get("name")
    ]


async def discover_ollama_models_async(
    base_url: str | None, *, timeout: float = 1.5
) -> list[str]:
    """Async variant of :func:`discover_ollama_models`."""
    if not base_url or not isinstance(base_url, str):
        return []
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []
    models = data.get("models", []) if isinstance(data, dict) else []
    return [
        m.get("name", "") for m in models
        if isinstance(m, dict) and m.get("name")
    ]


# ---------------------------------------------------------------------------
# Ollama tool-capability classification
# ---------------------------------------------------------------------------
#
# OmicsClaw is a tool-using agent: every chat turn may call MCP tools. The
# Ollama tool API rejects requests for models that don't implement function
# calling with an HTTP 400 like ``... does not support tools``. A pattern
# heuristic lets us tag models in the UI and translate the upstream error
# into actionable guidance — without paying for an ``/api/show`` round-trip
# per model on every ``/providers`` poll. Patterns are matched against the
# tag-stripped, lowercased model name (``deepseek-r1:14b`` → ``deepseek-r1``).
#
# Sources: Ollama model library tool-support metadata as of 2025-Q4. Keep
# patterns conservative — unknowns return ``None`` so callers can pass
# through and let Ollama be authoritative.

_OLLAMA_TOOL_CAPABLE_PATTERNS: tuple[str, ...] = (
    "qwen2.5",
    "qwen3",
    "qwen3-coder",
    "qwen2.5-coder",
    "llama3.1",
    "llama3.2",
    "llama3.3",
    "llama4",
    "mistral",
    "mistral-nemo",
    "mistral-small",
    "mistral-large",
    "mixtral",
    "command-r",
    "command-r-plus",
    "command-r7b",
    "granite3",
    "granite3.1",
    "granite3.2",
    "granite3.3",
    "firefunction",
    "nemotron",
    "llama3-groq-tool-use",
    "hermes3",
    "cogito",
    "devstral",
    "phi4",
    # Google Gemma 4 (released 2026-04-02) ships native function-calling —
    # the capabilities row on https://ollama.com/library/gemma4/tags lists
    # `tools` alongside vision/thinking/audio. Distinct from gemma 1–3
    # which remain text-only without tool support.
    "gemma4",
)

_OLLAMA_TOOL_INCAPABLE_PATTERNS: tuple[str, ...] = (
    "deepseek-r1",
    "gemma3",
    "gemma2",
    "gemma",
    "phi3",
    "phi3.5",
    "llama2",
    "llama3",  # base llama3 (without .1/.2/.3 suffix) — see ordering below
    "codellama",
    "tinyllama",
    "wizardlm",
    "wizardcoder",
    "starcoder",
    "starcoder2",
    "vicuna",
    "orca",
    "neural-chat",
    "yi",
    "qwen2",  # base qwen2 — no tool support; qwen2.5 does
    "qwen",
    "nomic-embed-text",
    "mxbai-embed",
    "snowflake-arctic-embed",
    "all-minilm",
)


def _ollama_base_name(model: str) -> str:
    """Strip Ollama tag (``:7b``, ``:latest``) and lowercase."""
    if not isinstance(model, str):
        return ""
    base = model.split(":", 1)[0].strip().lower()
    # Drop any registry path prefix (``registry.ollama.ai/library/foo`` → ``foo``)
    if "/" in base:
        base = base.rsplit("/", 1)[-1]
    return base


def model_supports_tools_ollama(model: str) -> bool | None:
    """Return tool-support classification for an Ollama model name.

    Returns ``True`` / ``False`` for known families, ``None`` for unknown
    models (caller should treat as "let Ollama be authoritative"). Matching
    is on the longest-prefix tag-stripped base name, so ``qwen2.5:14b`` and
    ``qwen2.5-coder:7b`` both resolve to capable, while ``deepseek-r1:14b``
    resolves to incapable.

    Never raises.
    """
    base = _ollama_base_name(model)
    if not base:
        return None

    # Longest-match wins to disambiguate qwen2.5 (capable) from qwen2 (not).
    capable = max(
        (p for p in _OLLAMA_TOOL_CAPABLE_PATTERNS if base == p or base.startswith(p + "-") or base.startswith(p + ".")),
        key=len,
        default="",
    )
    incapable = max(
        (p for p in _OLLAMA_TOOL_INCAPABLE_PATTERNS if base == p or base.startswith(p + "-") or base.startswith(p + ".")),
        key=len,
        default="",
    )
    if capable and len(capable) >= len(incapable):
        return True
    if incapable:
        return False
    return None


def provider_has_unreliable_tool_calling(provider_name: str | None) -> bool:
    """True for providers whose models silently truncate context and miss
    tool calls — the agent loop arms the phantom-completion guard (ADR 0027)
    for these.

    Today this is Ollama only (ADR 0026: the documented local path; its
    models are tool-capable but, being small and locally served, sometimes
    narrate a fabricated completion instead of emitting the tool call).
    Cloud providers reliably emit tool calls and raise real context-overflow
    errors, so they are deliberately excluded. Extend this set if other
    self-hosted providers exhibit the same behaviour.
    """
    return (provider_name or "").strip().lower() == "ollama"


__all__ = [
    "apply_deepseek_reasoning_passback",
    "discover_ollama_models",
    "discover_ollama_models_async",
    "model_supports_tools_ollama",
    "provider_has_unreliable_tool_calling",
]
