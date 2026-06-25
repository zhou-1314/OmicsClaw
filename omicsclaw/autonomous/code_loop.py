"""Autonomous analysis dispatch + the provider chat client (ADR 0032).

The persistent-kernel mini-agent is the **single** autonomous engine; this module
is now just the entry point that probes model capability and delegates to it. The
legacy one-shot generated-code loop was removed in the single-engine
consolidation (2026-06-22).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .contracts import AutonomousRunRequest, AutonomousRunResult

logger = logging.getLogger("omicsclaw.autonomous.code_loop")


class AutonomousLLMClient(Protocol):
    """Minimal completion client used by the capability probe + mini-agent."""

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str | None: ...


class ProviderChatClient:
    """OpenAI-compatible one-shot client using OmicsClaw provider defaults.

    Used by the capability probe and the mini-agent loop. The mini-agent collapses
    a ``None`` return into "LLM returned no content", so every failure mode is
    logged here — otherwise a missing key, a wrong model name, an HTTP 4xx, a
    timeout, and a reasoning model that only fills ``reasoning_content`` are all
    indistinguishable from each other and from a genuinely empty completion.
    """

    def __init__(self, *, model: str = "", provider: str = "", timeout: float = 120.0) -> None:
        self.model = str(model or "").strip()
        self.provider = str(provider or "").strip()
        # Reasoning models are slow and the mini-agent's prompt grows every step;
        # the old 30s ceiling timed out late steps and surfaced as bogus "no content".
        self.timeout = float(timeout)

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str | None:
        from omicsclaw.providers.runtime import resolve_provider_runtime
        import requests

        runtime = resolve_provider_runtime(provider=self.provider, model=self.model)
        if not runtime.api_key:
            logger.warning(
                "mini-agent LLM: no API key resolved (provider=%r, model=%r); "
                "set LLM_API_KEY / provider credentials.",
                self.provider, self.model,
            )
            return None

        url = f"{(runtime.base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {runtime.api_key}",
            "Content-Type": "application/json",
        }
        model = runtime.model or self.model or "gpt-5-mini"
        base = {"model": model, "messages": [{"role": "user", "content": prompt}]}

        # Try with temperature first; reasoning models (o-series / GPT-5-thinking /
        # DeepSeek-R1 / QwQ) reject a non-default temperature with HTTP 400, so on
        # that specific error retry once without it instead of failing the step.
        payloads = ({**base, "temperature": temperature}, base)
        for attempt, payload in enumerate(payloads):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            except Exception as exc:
                logger.warning("mini-agent LLM request failed (model=%s): %s", model, exc)
                return None
            if response.status_code == 200:
                text = _extract_message_text(response)
                if text is None:
                    logger.warning(
                        "mini-agent LLM returned HTTP 200 but no usable text (model=%s): "
                        "content and reasoning_content were both empty.", model,
                    )
                return text
            body = (getattr(response, "text", "") or "")[:300]
            if attempt == 0 and response.status_code == 400 and "temperature" in body.lower():
                logger.info("mini-agent LLM: model %s rejected temperature; retrying without it.", model)
                continue
            # The error body can echo prompt fragments / file paths (local-first
            # concern), so the warning carries only status + model; the body goes
            # to debug for opt-in diagnosis.
            logger.warning("mini-agent LLM HTTP %s (model=%s).", response.status_code, model)
            logger.debug("mini-agent LLM HTTP %s body (model=%s): %s", response.status_code, model, body)
            return None
        return None


def _extract_message_text(response: Any) -> str | None:
    """Pull usable text from a chat-completions response.

    Falls back to ``reasoning_content`` / ``reasoning`` when ``content`` is empty —
    that is how reasoning models (o-series, GPT-5-thinking, DeepSeek-R1, QwQ)
    surface their output. The old code read only ``content`` and so treated every
    reasoning-model reply as empty.
    """
    try:
        message = response.json()["choices"][0]["message"]
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if not isinstance(message, dict):
        return None
    for key in ("content", "reasoning_content", "reasoning"):
        text = _normalize_content(message.get(key))
        if text:
            return text
    return None


def _normalize_content(value: Any) -> str:
    """Normalize a chat message field to plain text.

    ``content`` is usually a string, but some providers return a list of typed
    parts (``[{"type": "text", "text": "..."}]``); join their text rather than
    handing the mini-agent a Python ``repr`` of the list.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p).strip()
    return ""


def run_autonomous_code_loop(
    request: AutonomousRunRequest,
    *,
    llm_client: AutonomousLLMClient | None = None,
    request_tool_approval: Any = None,
    runtime_context: dict[str, Any] | None = None,
) -> AutonomousRunResult:
    """Synchronous wrapper around :func:`run_autonomous_code_loop_async`."""
    import asyncio

    return asyncio.run(
        run_autonomous_code_loop_async(
            request,
            llm_client=llm_client,
            request_tool_approval=request_tool_approval,
            runtime_context=runtime_context,
        )
    )


async def run_autonomous_code_loop_async(
    request: AutonomousRunRequest,
    *,
    llm_client: AutonomousLLMClient | None = None,
    request_tool_approval: Any = None,
    runtime_context: dict[str, Any] | None = None,
) -> AutonomousRunResult:
    """Run the autonomous analysis path (ADR 0032, revised 2026-06-22).

    The persistent-kernel mini-agent is the single autonomous engine and runs
    automatically — no opt-in flag, no legacy one-shot fallback. A pre-flight
    capability gate (§8) refuses models that cannot drive the code contract;
    tiered isolation (bubblewrap when available, else an in-kernel guard) keeps it
    cross-platform.

    ``request_tool_approval`` / ``runtime_context`` are accepted for call-site
    compatibility; the mini-agent has no mid-run approval (system mutations are
    blocked by the kernel envelope), so they are currently unused.
    """
    import asyncio

    from .capability import mini_agent_gate
    from .mini_agent_runner import refused_result, run_mini_agent_request_async

    # The capability probe is a cheap "can the model emit one valid turn" check;
    # cap it at 30s so an unresponsive endpoint refuses fast instead of blocking
    # for the full per-turn timeout × probe attempts.
    probe_client = llm_client or ProviderChatClient(
        model=request.model_override, provider=request.provider_override, timeout=30.0
    )
    gate = await asyncio.to_thread(mini_agent_gate, probe_client)
    if gate.action == "refuse":
        return await asyncio.to_thread(refused_result, request, gate.diagnostic)
    return await run_mini_agent_request_async(request, llm_client=llm_client)
