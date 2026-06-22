"""Autonomous analysis dispatch + the provider chat client (ADR 0032).

The persistent-kernel mini-agent is the **single** autonomous engine; this module
is now just the entry point that probes model capability and delegates to it. The
legacy one-shot generated-code loop was removed in the single-engine
consolidation (2026-06-22).
"""

from __future__ import annotations

from typing import Any, Protocol

from .contracts import AutonomousRunRequest, AutonomousRunResult


class AutonomousLLMClient(Protocol):
    """Minimal completion client used by the capability probe + mini-agent."""

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str | None: ...


class ProviderChatClient:
    """OpenAI-compatible one-shot client using OmicsClaw provider defaults."""

    def __init__(self, *, model: str = "", provider: str = "") -> None:
        self.model = str(model or "").strip()
        self.provider = str(provider or "").strip()

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str | None:
        from omicsclaw.providers.runtime import resolve_provider_runtime
        import requests

        runtime = resolve_provider_runtime(provider=self.provider, model=self.model)
        if not runtime.api_key:
            return None
        try:
            response = requests.post(
                f"{(runtime.base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {runtime.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": runtime.model or self.model or "gpt-5-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                },
                timeout=30.0,
            )
        except Exception:
            return None
        if response.status_code != 200:
            return None
        try:
            return str(response.json()["choices"][0]["message"]["content"]).strip()
        except (KeyError, TypeError, ValueError, IndexError):
            return None


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

    probe_client = llm_client or ProviderChatClient(
        model=request.model_override, provider=request.provider_override
    )
    gate = await asyncio.to_thread(mini_agent_gate, probe_client)
    if gate.action == "refuse":
        return await asyncio.to_thread(refused_result, request, gate.diagnostic)
    return await run_mini_agent_request_async(request, llm_client=llm_client)
