"""Bench onboarding + global bench preferences (Phase 5, BE-ONBOARD-8 / BE-PREF-7).

Pure async service over a ``MemoryClient`` (mirrors ``thread.py``), so the desktop
REST routes stay thin and the logic is unit-testable without a server.

- Onboarding persists across launches via the **versioned** ``core://my_user``
  (the profile blob). Completing or skipping records ``preference://bench/onboarded``
  so the frontend never re-prompts.
- ``preference://bench/cross_thread_recall`` (default **off**) is established here as
  a global bench toggle. It is currently store-only (no recall-behaviour change);
  wiring it into the recall tool is a deliberate follow-up.
- Every read defaults gracefully when a row is absent (missing → default).
"""

from __future__ import annotations

import json
import re
from typing import Any

MY_USER_URI = "core://my_user"
ONBOARDED_URI = "preference://bench/onboarded"
CROSS_THREAD_RECALL_KEY = "cross_thread_recall"

# Bench preference keys are flat slugs; reject anything that could escape the
# preference://bench/ subtree (path traversal / nested keys).
_KEY_RE = re.compile(r"^[a-zA-Z0-9_]+$")


def _bench_pref_uri(key: str) -> str:
    return f"preference://bench/{key}"


async def _recall_json(client: Any, uri: str) -> Any:
    """Recall a URI and JSON-decode its content; None when absent, raw string on
    non-JSON (tolerates legacy/plain rows). Never raises."""
    try:
        record = await client.recall(uri)
    except Exception:
        return None
    if record is None or not getattr(record, "content", None):
        return None
    try:
        return json.loads(record.content)
    except (json.JSONDecodeError, ValueError, TypeError):
        return record.content


async def onboard_user(client: Any, profile: dict[str, Any]) -> dict:
    """Persist the onboarding profile to versioned ``core://my_user`` and mark
    onboarding complete. Returns the resulting status."""
    await client.remember(
        uri=MY_USER_URI,
        content=json.dumps(dict(profile), ensure_ascii=False),
        disclosure="Bench onboarding profile",
    )
    await client.remember(
        uri=ONBOARDED_URI,
        content=json.dumps({"value": True}),
        disclosure="Bench onboarding completed",
    )
    return await onboarding_status(client)


async def skip_onboarding(client: Any) -> dict:
    """Record that onboarding was skipped (no profile written) so the frontend
    stops prompting. Returns the resulting status."""
    await client.remember(
        uri=ONBOARDED_URI,
        content=json.dumps({"value": True}),
        disclosure="Bench onboarding skipped",
    )
    return await onboarding_status(client)


async def onboarding_status(client: Any) -> dict:
    """Read the onboarding state (persists across launches). Missing rows default
    gracefully: no profile → user=None, never-onboarded → onboarded=False,
    unset toggle → cross_thread_recall=False."""
    user = await _recall_json(client, MY_USER_URI)
    onboarded_row = await _recall_json(client, ONBOARDED_URI)
    if isinstance(onboarded_row, dict):
        onboarded = bool(onboarded_row.get("value"))
    else:
        onboarded = bool(onboarded_row)
    return {
        "onboarded": onboarded,
        "user": user if isinstance(user, dict) else None,
        "cross_thread_recall": await get_bench_preference(
            client, CROSS_THREAD_RECALL_KEY, default=False
        ),
    }


async def set_bench_preference(client: Any, key: str, value: Any) -> dict:
    """Write a global bench preference at ``preference://bench/<key>``. ``key`` must
    be a flat slug (no path separators). Returns ``{key, value}``."""
    if not _KEY_RE.match(key or ""):
        raise ValueError(f"invalid bench preference key: {key!r}")
    await client.remember(
        uri=_bench_pref_uri(key),
        content=json.dumps({"value": value}),
        disclosure=f"Bench preference: {key}",
    )
    return {"key": key, "value": value}


async def get_bench_preference(client: Any, key: str, default: Any = None) -> Any:
    """Read a global bench preference, returning ``default`` when the row is absent."""
    if not _KEY_RE.match(key or ""):
        return default
    row = await _recall_json(client, _bench_pref_uri(key))
    if row is None:
        return default
    if isinstance(row, dict) and "value" in row:
        return row["value"]
    return row
