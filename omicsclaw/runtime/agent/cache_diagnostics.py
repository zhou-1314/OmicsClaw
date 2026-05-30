"""Prompt-prefix cache diagnostics (ADR 0017).

Provider-neutral helpers that turn the **Stable prefix invariant** from a hope
into a measured property. Three pure pieces:

* :func:`extract_cache_tokens` — read ``(hit, miss)`` prompt tokens from a
  provider usage object (DeepSeek / OpenAI / Anthropic), never raising.
* :func:`compute_segment_hash` — a deterministic hash of a prefix segment
  (the serialized tool list, or the stable system prompt).
* :func:`infer_miss_reason` — given the prior turn's segment hashes and this
  turn's, attribute a cache miss to ``tool-list-changed`` / ``system-changed``
  / ``history-shifted`` / ``cold-start`` (or ``none`` on a healthy hit).

These are intentionally free of any loop / store / I/O dependency so they can
be unit-tested in isolation and reused by every surface. The cross-turn store
that remembers the prior hashes lives in ``omicsclaw.runtime.agent.state``;
the wiring that calls these from the loop lives in ``query_engine``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger("omicsclaw.runtime.agent.cache_diagnostics")

# Miss-reason vocabulary (mirrors docs/CONTEXT.md §"Prompt Prefix & Caching").
REASON_NONE = "none"
REASON_COLD_START = "cold-start"
REASON_TOOL_LIST_CHANGED = "tool-list-changed"
REASON_SYSTEM_CHANGED = "system-changed"
REASON_HISTORY_SHIFTED = "history-shifted"


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _present(usage: Any, name: str) -> bool:
    """True when ``usage`` carries ``name`` with a non-None value.

    Robust to both ``SimpleNamespace`` (tests) and SDK pydantic models, where
    an unsupported field is either absent or explicitly ``None``.
    """
    return getattr(usage, name, None) is not None


@dataclass(frozen=True, slots=True)
class CacheTokens:
    """Prompt tokens split into cache hit (cheap) vs miss (full price)."""

    hit: int = 0
    miss: int = 0

    @property
    def total(self) -> int:
        return self.hit + self.miss

    @property
    def ratio(self) -> float:
        """Hit ratio in ``[0, 1]``; ``0.0`` when there is no cache signal."""
        total = self.total
        return self.hit / total if total else 0.0

    @property
    def has_signal(self) -> bool:
        """Whether the provider reported any cache accounting at all."""
        return self.total > 0


def extract_cache_tokens(usage: Any) -> CacheTokens:
    """Read ``(hit, miss)`` prompt tokens from a provider usage object.

    Detection is by *format* (which fields the object carries), not by which
    value is non-zero, so a genuine zero-hit miss is distinguished from "no
    cache accounting":

    * **DeepSeek** — ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
      (top level; they sum to ``prompt_tokens``).
    * **Anthropic** — ``cache_read_input_tokens`` (hit) +
      ``cache_creation_input_tokens`` and ``input_tokens`` (both full price ⇒
      miss).
    * **OpenAI** — ``prompt_tokens_details.cached_tokens`` (hit);
      ``miss = prompt_tokens - hit``.

    Returns ``CacheTokens(0, 0)`` when the object carries no recognized cache
    fields (e.g. a local model with no provider cache). Never raises.
    """
    if usage is None:
        return CacheTokens(0, 0)

    # DeepSeek: explicit hit + miss at the top level.
    if _present(usage, "prompt_cache_hit_tokens") or _present(
        usage, "prompt_cache_miss_tokens"
    ):
        return CacheTokens(
            _to_int(getattr(usage, "prompt_cache_hit_tokens", 0)),
            _to_int(getattr(usage, "prompt_cache_miss_tokens", 0)),
        )

    # Anthropic: cache_read = hit; cache_creation + uncached input = miss.
    if _present(usage, "cache_read_input_tokens") or _present(
        usage, "cache_creation_input_tokens"
    ):
        hit = _to_int(getattr(usage, "cache_read_input_tokens", 0))
        miss = _to_int(getattr(usage, "cache_creation_input_tokens", 0)) + _to_int(
            getattr(usage, "input_tokens", 0)
        )
        return CacheTokens(hit, miss)

    # OpenAI: cached_tokens nested in prompt_tokens_details.
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None and _present(details, "cached_tokens"):
        hit = _to_int(getattr(details, "cached_tokens", 0))
        prompt_tokens = _to_int(getattr(usage, "prompt_tokens", 0))
        return CacheTokens(hit, max(0, prompt_tokens - hit))

    # No recognized cache accounting.
    return CacheTokens(0, 0)


def compute_segment_hash(payload: Any) -> str:
    """Deterministic SHA-256 of a prefix segment.

    ``str``/``bytes`` are hashed directly; anything else (e.g. the tool list,
    a ``list[dict]``) is canonicalized with ``json.dumps(sort_keys=True)`` so
    incidental dict-key reordering is not mistaken for a real prefix change
    while list order (which the provider *does* key on) is preserved.
    """
    if isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = json.dumps(
            payload, sort_keys=True, default=str, ensure_ascii=False
        ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def infer_miss_reason(
    *,
    prev_tool_hash: str | None,
    prev_system_hash: str | None,
    cur_tool_hash: str,
    cur_system_hash: str,
    tokens: CacheTokens,
) -> str:
    """Attribute this turn's cache outcome.

    The segment hashes are authoritative for *what we control* (tool list and
    stable system prefix); the hit/miss tokens disambiguate the
    prefix-stable-yet-missed case:

    * no prior hashes            → ``cold-start``
    * tool hash changed          → ``tool-list-changed``
    * system hash changed        → ``system-changed``
    * prefix stable, zero hit    → ``history-shifted``
    * prefix stable, some hit    → ``none`` (healthy)
    """
    if prev_tool_hash is None and prev_system_hash is None:
        return REASON_COLD_START
    if cur_tool_hash != prev_tool_hash:
        return REASON_TOOL_LIST_CHANGED
    if cur_system_hash != prev_system_hash:
        return REASON_SYSTEM_CHANGED
    # Prefix (tools + system) is byte-stable this turn.
    if tokens.hit == 0 and tokens.miss > 0:
        return REASON_HISTORY_SHIFTED
    return REASON_NONE


@dataclass(frozen=True, slots=True)
class CacheTurnDiagnostics:
    """One turn's cache outcome, emitted to surfaces / logs / tests."""

    hit_tokens: int
    miss_tokens: int
    hit_ratio: float
    miss_reason: str
    tool_hash: str
    system_hash: str

    @property
    def has_signal(self) -> bool:
        return (self.hit_tokens + self.miss_tokens) > 0


@dataclass(slots=True)
class _ChatCacheState:
    prev_tool_hash: str | None = None
    prev_system_hash: str | None = None
    session_hit: int = 0
    session_miss: int = 0


class CacheDiagnosticsStore:
    """Cross-turn cache state keyed by ``chat_id``.

    Remembers the prior turn's prefix-segment hashes so the next turn can
    attribute a cache miss, and accumulates per-session hit/miss totals. The
    module-level singleton :data:`CACHE_DIAGNOSTICS` shares the lifecycle of
    the other per-chat stores in ``omicsclaw.runtime.agent.state`` (it is a
    telemetry sink, like ``billing._usage``, not injected state).
    """

    def __init__(self) -> None:
        self._by_chat: dict[Any, _ChatCacheState] = {}

    def record(
        self,
        chat_id: Any,
        *,
        tool_hash: str,
        system_hash: str,
        tokens: CacheTokens,
    ) -> CacheTurnDiagnostics:
        """Attribute one LLM call's cache outcome and roll forward the state."""
        state = self._by_chat.get(chat_id)
        if state is None:
            state = _ChatCacheState()
            self._by_chat[chat_id] = state
        reason = infer_miss_reason(
            prev_tool_hash=state.prev_tool_hash,
            prev_system_hash=state.prev_system_hash,
            cur_tool_hash=tool_hash,
            cur_system_hash=system_hash,
            tokens=tokens,
        )
        state.prev_tool_hash = tool_hash
        state.prev_system_hash = system_hash
        state.session_hit += tokens.hit
        state.session_miss += tokens.miss
        if tokens.has_signal:
            _LOGGER.debug(
                "cache chat=%s hit=%d miss=%d ratio=%.2f reason=%s",
                chat_id,
                tokens.hit,
                tokens.miss,
                tokens.ratio,
                reason,
            )
        return CacheTurnDiagnostics(
            hit_tokens=tokens.hit,
            miss_tokens=tokens.miss,
            hit_ratio=tokens.ratio,
            miss_reason=reason,
            tool_hash=tool_hash,
            system_hash=system_hash,
        )

    def session_hit_ratio(self, chat_id: Any) -> float:
        state = self._by_chat.get(chat_id)
        if state is None:
            return 0.0
        total = state.session_hit + state.session_miss
        return state.session_hit / total if total else 0.0

    def reset(self, chat_id: Any) -> None:
        """Forget one chat's cache state (e.g. on /new or session reset)."""
        self._by_chat.pop(chat_id, None)

    def clear(self) -> None:
        """Forget all cache state (test isolation)."""
        self._by_chat.clear()


# Process-wide telemetry sink (see CacheDiagnosticsStore docstring).
CACHE_DIAGNOSTICS = CacheDiagnosticsStore()


__all__ = [
    "CACHE_DIAGNOSTICS",
    "CacheDiagnosticsStore",
    "CacheTokens",
    "CacheTurnDiagnostics",
    "REASON_COLD_START",
    "REASON_HISTORY_SHIFTED",
    "REASON_NONE",
    "REASON_SYSTEM_CHANGED",
    "REASON_TOOL_LIST_CHANGED",
    "compute_segment_hash",
    "extract_cache_tokens",
    "infer_miss_reason",
]
