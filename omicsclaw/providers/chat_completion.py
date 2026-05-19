"""Best-effort OpenAI-compatible chat-completion helper.

Used by consensus runtime (plan / extractor / synthesizer) and any future
caller that needs a one-shot LLM completion. Designed as **best-effort**:
returns ``None`` on every failure mode (missing API key, HTTP non-200,
network error, JSON parse error). Never raises. Callers are expected to
fall back deterministically.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def call_chat_completion(
    prompt: str,
    *,
    timeout: float = 30.0,
    temperature: float = 0.0,
) -> Optional[str]:
    """Issue a single OpenAI-compatible chat-completion request.

    Parameters
    ----------
    prompt :
        Single-message user prompt. The function packages it as
        ``[{"role": "user", "content": prompt}]``.
    timeout :
        Network timeout in seconds.
    temperature :
        Sampling temperature. Defaults to ``0.0`` for deterministic
        responses on JSON-shaped prompts.

    Returns
    -------
    The assistant's content string (stripped), or ``None`` on any failure.
    """
    from omicsclaw.providers.runtime import resolve_chat_endpoint

    api_key, base_url, model = resolve_chat_endpoint()
    if not api_key:
        return None
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
            },
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat_completion request failed: %s", exc)
        return None

    if response.status_code != 200:
        logger.warning("chat_completion HTTP %s: %s", response.status_code, response.text[:200])
        return None

    try:
        return str(response.json()["choices"][0]["message"]["content"]).strip()
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        logger.warning("chat_completion response parse failed: %s", exc)
        return None
