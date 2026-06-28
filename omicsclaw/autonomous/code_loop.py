"""Autonomous analysis dispatch + the provider chat client (ADR 0032).

The persistent-kernel mini-agent is the **single** autonomous engine; this module
is now just the entry point that probes model capability and delegates to it. The
legacy one-shot generated-code loop was removed in the single-engine
consolidation (2026-06-22).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from .contracts import AutonomousRunRequest, AutonomousRunResult

logger = logging.getLogger("omicsclaw.autonomous.code_loop")

# Process-local positive cache of model identities that have already passed the
# capability probe, so a long-lived surface (Desktop / Channel) does not pay the
# probe's LLM round-trip on every autonomous run with a stable model. Keyed by
# (provider_override, model_override). Only CAPABLE verdicts are cached: a refusal
# is never stored, so a transient endpoint failure cannot permanently poison the
# route, and the in-loop WARMUP_STEPS backstop still catches a model that passes
# the probe but later regresses. Only the production path (no injected client) is
# cached — injected clients are test-only and per-call.
_CAPABLE_MODEL_CACHE: set[tuple[str, str]] = set()


class AutonomousLLMClient(Protocol):
    """Minimal completion client used by the capability probe + mini-agent."""

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str | None: ...


class ProviderChatClient:
    """OpenAI-compatible one-shot client using OmicsClaw provider defaults.

    Used by the capability probe and the mini-agent loop. It builds a synchronous
    ``openai.OpenAI`` client from the **same** ``resolve_provider_runtime`` result
    the main async chat client resolves from (see
    ``omicsclaw/runtime/agent/session.py`` ``AsyncOpenAI(**kw)``), so credential /
    base-url / model resolution and the transport/auth/ccproxy path are shared
    instead of living in a second hand-written HTTP client that could drift. Two
    things are intentionally NOT shared: this client is synchronous (the kernel
    runs in a worker thread) and it uses the mini-agent's own per-call timeout and
    retry policy (see ``__init__``) rather than the interactive chat timeout
    policy — an autonomous budgeted loop needs a bounded, predictable per-step
    wall clock.

    The mini-agent collapses a ``None`` return into "LLM returned no content", so
    every failure mode is logged here — otherwise a missing key, a wrong model
    name, an HTTP 4xx, a timeout, and a reasoning model that only fills
    ``reasoning_content`` are all indistinguishable from each other and from a
    genuinely empty completion.
    """

    def __init__(
        self,
        *,
        model: str = "",
        provider: str = "",
        timeout: float = 120.0,
        max_retries: int = 0,
    ) -> None:
        self.model = str(model or "").strip()
        self.provider = str(provider or "").strip()
        # Reasoning models are slow and the mini-agent's prompt grows every step;
        # the old 30s ceiling timed out late steps and surfaced as bogus "no content".
        self.timeout = float(timeout)
        # SDK transport retries are OFF by default: the mini-agent loop owns
        # step-level retry/repair, and the run's wall-clock budget is only checked
        # between steps — so a per-completion cost of timeout × backoff × retries
        # could overrun the budget. max_retries=0 keeps each completion bounded by
        # the single per-call timeout (faithful to the old one-shot requests.post).
        # The interactive main client defaults to 2; this loop deliberately differs.
        self.max_retries = int(max_retries)
        self._client: Any = None
        self._client_key: tuple | None = None

    def _client_for(self, api_key: str, base_url: str) -> Any:
        """Build (and cache) the sync OpenAI SDK client for a resolved runtime.

        Cached across the steps of one run so the httpx connection pool is reused;
        rebuilt only if the resolved credentials change (they are stable within a
        run). Mirrors ``AsyncOpenAI(**kw)`` in ``runtime/agent/session.py``.
        """
        import openai

        key = (api_key, base_url, self.max_retries)
        if self._client is None or self._client_key != key:
            kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": self.max_retries}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = openai.OpenAI(**kwargs)
            self._client_key = key
        return self._client

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str | None:
        import openai

        from omicsclaw.providers.runtime import resolve_provider_runtime

        runtime = resolve_provider_runtime(provider=self.provider, model=self.model)
        if not runtime.api_key:
            logger.warning(
                "mini-agent LLM: no API key resolved (provider=%r, model=%r); "
                "set LLM_API_KEY / provider credentials.",
                self.provider, self.model,
            )
            return None

        model = runtime.model or self.model or "gpt-5-mini"
        try:
            client = self._client_for(runtime.api_key, runtime.base_url or "")
        except openai.OpenAIError as exc:
            logger.warning("mini-agent LLM client init failed (model=%s): %s", model, exc)
            return None
        base = {"model": model, "messages": [{"role": "user", "content": prompt}]}

        # Try with temperature first; reasoning models (o-series / GPT-5-thinking /
        # DeepSeek-R1 / QwQ) reject a non-default temperature with HTTP 400, so on
        # that specific error retry once without it instead of failing the step.
        payloads = ({**base, "temperature": temperature}, base)
        for attempt, payload in enumerate(payloads):
            try:
                # with_raw_response keeps the body unparsed so the lenient
                # _extract_message_text can read reasoning_content / list content
                # that the SDK's typed model would reject (quirky OpenAI-compatible
                # providers). Errors still raise the typed SDK exceptions.
                raw = client.chat.completions.with_raw_response.create(
                    **payload, timeout=self.timeout
                )
            except openai.APIStatusError as exc:
                status = getattr(exc, "status_code", None)
                body = _error_body(exc)
                if attempt == 0 and status == 400 and "temperature" in body.lower():
                    logger.info(
                        "mini-agent LLM: model %s rejected temperature; retrying without it.",
                        model,
                    )
                    continue
                # The error body can echo prompt fragments / file paths (local-first
                # concern), so the warning carries only status + model; the body goes
                # to debug for opt-in diagnosis.
                logger.warning("mini-agent LLM HTTP %s (model=%s).", status, model)
                logger.debug("mini-agent LLM HTTP %s body (model=%s): %s", status, model, body)
                return None
            except openai.OpenAIError as exc:
                # Connection / timeout / client-construction errors.
                logger.warning("mini-agent LLM request failed (model=%s): %s", model, exc)
                return None
            text = _extract_message_text(raw)
            if text is None:
                logger.warning(
                    "mini-agent LLM returned HTTP 200 but no usable text (model=%s): "
                    "content and reasoning_content were both empty.", model,
                )
            return text
        return None


def _extract_message_text(response: Any) -> str | None:
    """Pull usable text from a chat-completions response.

    Reads the raw (unparsed) body so it can fall back to ``reasoning_content`` /
    ``reasoning`` when ``content`` is empty — that is how reasoning models
    (o-series, GPT-5-thinking, DeepSeek-R1, QwQ) surface their output — and can
    join list-form ``content`` parts. The SDK's typed model drops the first and
    rejects the second, so we deliberately parse the body ourselves.
    """
    payload = _response_json(response)
    if not isinstance(payload, dict):
        return None
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, TypeError, IndexError):
        return None
    if not isinstance(message, dict):
        return None
    for key in ("content", "reasoning_content", "reasoning"):
        text = _normalize_content(message.get(key))
        if text:
            return text
    return None


def _response_json(response: Any) -> Any:
    """Get the parsed JSON body from a raw SDK response (or a test double).

    The SDK's ``with_raw_response`` wrapper exposes the unparsed body as ``.text``;
    fall back to a ``.json()`` callable or a plain dict so the helper stays easy to
    unit-test.
    """
    if isinstance(response, dict):
        return response
    getter = getattr(response, "json", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    text = getattr(response, "text", None)
    if isinstance(text, str):
        try:
            return json.loads(text)
        except ValueError:
            return None
    return None


def _error_body(exc: Any) -> str:
    """Assemble a short, lowercased-on-demand error string from an SDK exception.

    Pulls from ``.message`` and the HTTP response/body so the temperature-400
    detection works regardless of which field the provider populated. Capped so a
    verbose body cannot bloat the (debug-only) log line.
    """
    parts: list[str] = [str(getattr(exc, "message", "") or "")]
    response = getattr(exc, "response", None)
    text = getattr(response, "text", "") if response is not None else ""
    if isinstance(text, str):
        parts.append(text)
    body = getattr(exc, "body", None)
    if body is not None:
        parts.append(str(body))
    return " ".join(p for p in parts if p)[:500]


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
) -> AutonomousRunResult:
    """Synchronous wrapper around :func:`run_autonomous_code_loop_async`."""
    import asyncio

    return asyncio.run(
        run_autonomous_code_loop_async(request, llm_client=llm_client)
    )


async def run_autonomous_code_loop_async(
    request: AutonomousRunRequest,
    *,
    llm_client: AutonomousLLMClient | None = None,
) -> AutonomousRunResult:
    """Run the autonomous analysis path (ADR 0032, revised 2026-06-22).

    The persistent-kernel mini-agent is the single autonomous engine and runs
    automatically — no opt-in flag, no legacy one-shot fallback. A pre-flight
    capability gate (§8) refuses models that cannot drive the code contract;
    tiered isolation (bubblewrap when available, else an in-kernel guard) keeps it
    cross-platform.

    No mid-run approval hook: the mini-agent has no per-cell approval, so the whole
    ``autonomous_analysis_execute`` tool call is gated ONCE at the outer agent loop
    (ADR 0008 L2 REQUIRE_APPROVAL); system mutations are then contained by the
    kernel envelope / the ``OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX=1`` fail-closed
    tier. (A previous ``request_tool_approval``/``runtime_context`` kwarg pair was
    accepted but never consulted — removed to stop advertising a hook that never
    fired; ``request.metadata`` already carries surface/chat_id/session_id.)
    """
    import asyncio

    from .capability import mini_agent_gate, probe_enabled_default
    from .mini_agent_runner import refused_result, run_mini_agent_request_async

    # Skip the probe round-trip when this exact model identity already proved it
    # can drive the contract (production path only — an injected client is a
    # per-call test double and must always re-probe).
    cache_key = (request.provider_override, request.model_override)
    cacheable = llm_client is None and probe_enabled_default()
    if cacheable and cache_key in _CAPABLE_MODEL_CACHE:
        return await run_mini_agent_request_async(request, llm_client=llm_client)

    # The capability probe is a cheap "can the model emit one valid turn" check;
    # cap it at 30s (vs the loop's 120s) so an unresponsive endpoint refuses fast.
    # Transport retries are already off by default (see ProviderChatClient).
    probe_client = llm_client or ProviderChatClient(
        model=request.model_override, provider=request.provider_override, timeout=30.0
    )
    gate = await asyncio.to_thread(mini_agent_gate, probe_client)
    if gate.action == "refuse":
        return await asyncio.to_thread(refused_result, request, gate.diagnostic)
    if cacheable:
        _CAPABLE_MODEL_CACHE.add(cache_key)
    return await run_mini_agent_request_async(request, llm_client=llm_client)
