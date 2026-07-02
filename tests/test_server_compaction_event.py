"""Tests for the SSE compaction-event handler the desktop server wires up."""
from __future__ import annotations

import json
import importlib.util
import sys
import types
from unittest.mock import MagicMock


def _stub_optional_modules() -> None:
    """server.py pulls in httpx via llm_timeout; stub for unit tests."""
    if "httpx" not in sys.modules:
        httpx_stub = types.ModuleType("httpx")

        class _StubHTTPError(Exception):
            pass

        httpx_stub.HTTPError = _StubHTTPError  # type: ignore[attr-defined]
        httpx_stub.ConnectError = _StubHTTPError  # type: ignore[attr-defined]
        httpx_stub.TimeoutException = _StubHTTPError  # type: ignore[attr-defined]
        httpx_stub.get = lambda *_, **__: None  # type: ignore[attr-defined]
        sys.modules["httpx"] = httpx_stub
    for stub_name in ("openai", "tiktoken", "fastapi"):
        if stub_name not in sys.modules and importlib.util.find_spec(stub_name) is None:
            sys.modules[stub_name] = types.ModuleType(stub_name)


def test_handler_pushes_status_frame_to_queue() -> None:
    _stub_optional_modules()
    try:
        from omicsclaw.surfaces.desktop._compaction_event_bridge import (
            make_compaction_event_handler,
        )
    except ImportError as exc:
        import pytest
        pytest.skip(f"server bridge unavailable: {exc}")

    from omicsclaw.runtime.context.compaction import CompactionEvent

    queue = MagicMock()
    handler = make_compaction_event_handler(queue)

    event = CompactionEvent(
        messages_compressed=7,
        tokens_saved_estimate=2100,
        applied_stages=("auto_compact",),
    )
    handler(event)

    queue.put_nowait.assert_called_once()
    frame = queue.put_nowait.call_args[0][0]
    assert frame["type"] == "status"
    payload = json.loads(frame["data"])
    assert payload["notification"] is True
    assert payload["subtype"] == "context_compressed"
    assert payload["stats"]["messagesCompressed"] == 7
    assert payload["stats"]["tokensSaved"] == 2100
    assert "7 older messages" in payload["message"]
    assert "~2,100 tokens" in payload["message"]


def test_handler_accepts_neutral_payload_dict_from_dispatcher() -> None:
    """ADR 0006: the dispatcher delivers a neutral dict payload (the
    serialised CompactionEvent), not the dataclass itself. The handler must
    coerce that dict — regression for the desktop consumer silently dropping
    every compaction toast (``'dict' object has no attribute
    'messages_compressed'``)."""
    _stub_optional_modules()
    try:
        from omicsclaw.surfaces.desktop._compaction_event_bridge import (
            make_compaction_event_handler,
        )
    except ImportError as exc:
        import pytest
        pytest.skip(f"server bridge unavailable: {exc}")

    queue = MagicMock()
    handler = make_compaction_event_handler(queue)

    # Exactly the shape the dispatcher's ContextCompacted.payload carries.
    handler(
        {
            "messages_compressed": 7,
            "tokens_saved_estimate": 2100,
            "applied_stages": ["auto_compact"],
        }
    )

    queue.put_nowait.assert_called_once()
    payload = json.loads(queue.put_nowait.call_args[0][0]["data"])
    assert payload["stats"]["messagesCompressed"] == 7
    assert payload["stats"]["tokensSaved"] == 2100


def test_handler_carries_budget_status_from_neutral_dict() -> None:
    """B3: the dispatcher-serialised payload (dataclasses.asdict) carries the
    budget-status values across the surface boundary; the SSE frame must expose
    them so the desktop app can render context-budget pressure."""
    _stub_optional_modules()
    try:
        from omicsclaw.surfaces.desktop._compaction_event_bridge import (
            make_compaction_event_handler,
        )
    except ImportError as exc:
        import pytest
        pytest.skip(f"server bridge unavailable: {exc}")

    queue = MagicMock()
    handler = make_compaction_event_handler(queue)
    handler(
        {
            "messages_compressed": 3,
            "tokens_saved_estimate": 500,
            "applied_stages": ["context_collapse"],
            "budget_status": "warning",
            "local_budget_status": "critical",
        }
    )

    payload = json.loads(queue.put_nowait.call_args[0][0]["data"])
    assert payload["budgetStatus"] == "warning"
    assert payload["localBudgetStatus"] == "critical"


def test_handler_omits_budget_status_when_absent_from_dict() -> None:
    """A neutral dict without budget-status keys (older producers) must not add
    them — backward-compatible with the pre-B3 payload shape."""
    _stub_optional_modules()
    try:
        from omicsclaw.surfaces.desktop._compaction_event_bridge import (
            make_compaction_event_handler,
        )
    except ImportError as exc:
        import pytest
        pytest.skip(f"server bridge unavailable: {exc}")

    queue = MagicMock()
    handler = make_compaction_event_handler(queue)
    handler(
        {
            "messages_compressed": 3,
            "tokens_saved_estimate": 500,
            "applied_stages": ["context_collapse"],
        }
    )

    payload = json.loads(queue.put_nowait.call_args[0][0]["data"])
    assert "budgetStatus" not in payload
    assert "localBudgetStatus" not in payload


def test_handler_is_synchronous_and_must_not_be_awaited() -> None:
    """``server.py`` consumes the dispatch event with a plain call, NOT
    ``await`` — regression for ``TypeError: object NoneType can't be used in
    'await' expression``. The handler returns ``None`` (not a coroutine)."""
    _stub_optional_modules()
    try:
        from omicsclaw.surfaces.desktop._compaction_event_bridge import (
            make_compaction_event_handler,
        )
    except ImportError as exc:
        import pytest
        pytest.skip(f"server bridge unavailable: {exc}")

    import asyncio

    handler = make_compaction_event_handler(MagicMock())
    result = handler({"messages_compressed": 1, "tokens_saved_estimate": 1, "applied_stages": []})
    assert result is None
    assert not asyncio.iscoroutine(result)
