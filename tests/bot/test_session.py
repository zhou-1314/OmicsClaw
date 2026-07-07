"""Unit tests for ``omicsclaw.runtime.agent.session`` — SessionManager + LRU eviction.

The module owns chat-session state plus the ``init()`` lifecycle that
binds the LLM client and provider config onto ``omicsclaw.runtime.agent.state``'s globals.
These tests drive the SessionManager and the LRU eviction in isolation —
``init()`` itself is exercised through the existing ``tests/test_oauth_
regressions.py`` and ``tests/test_bot_core_timeout.py`` files (the
backward-compat regression net), so we don't duplicate that here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture
def fake_store():
    """Minimal store stub matching the SessionManager contract."""

    class _FakeStore:
        def __init__(self):
            self.sessions: dict[str, dict] = {}
            self.calls: list[tuple[str, tuple, dict]] = []

        async def get_session(self, session_id):
            self.calls.append(("get_session", (session_id,), {}))
            return self.sessions.get(session_id)

        async def create_session(self, user_id, platform, chat_id, *, session_id, thread_id=""):
            # thread_id mirrors CompatMemoryStore.create_session (Bench, ADR 0023).
            self.calls.append(
                ("create_session", (user_id, platform, chat_id),
                 {"session_id": session_id, "thread_id": thread_id})
            )
            self.sessions[session_id] = {
                "user_id": user_id,
                "platform": platform,
                "chat_id": chat_id,
                "session_id": session_id,
                "thread_id": thread_id,
                "created_at": datetime.now(timezone.utc),
            }
            return self.sessions[session_id]

        async def update_session(self, session_id, fields):
            self.calls.append(("update_session", (session_id,), fields))
            self.sessions[session_id].update(fields)
            return self.sessions[session_id]

        async def get_memories(self, session_id, kind, *, limit, thread_id=""):
            # thread_id mirrors CompatMemoryStore.get_memories (Bench, BE-RECALL-6 /
            # AN-CTXRECALL-11). load_context passes it only for the thread-carrying
            # types (dataset/analysis); the global types call without it.
            self.calls.append(
                ("get_memories", (session_id, kind), {"limit": limit, "thread_id": thread_id})
            )
            return []

    return _FakeStore()


@pytest.mark.asyncio
async def test_session_manager_creates_session_when_missing(fake_store):
    """First call for a (platform, user, chat) triple constructs the session."""
    from omicsclaw.runtime.agent.session import SessionManager

    mgr = SessionManager(fake_store)
    session = await mgr.get_or_create("alice", "telegram", "12345")

    assert session["user_id"] == "alice"
    assert session["session_id"] == "telegram:alice:12345"
    create_calls = [c for c in fake_store.calls if c[0] == "create_session"]
    assert len(create_calls) == 1


@pytest.mark.asyncio
async def test_session_manager_updates_existing_session(fake_store):
    """Second call for the same triple touches last_activity instead of
    re-creating — preserves session state across reconnects."""
    from omicsclaw.runtime.agent.session import SessionManager

    mgr = SessionManager(fake_store)
    await mgr.get_or_create("bob", "feishu", "777")
    fake_store.calls.clear()

    await mgr.get_or_create("bob", "feishu", "777")

    update_calls = [c for c in fake_store.calls if c[0] == "update_session"]
    create_calls = [c for c in fake_store.calls if c[0] == "create_session"]
    assert len(update_calls) == 1
    assert len(create_calls) == 0
    assert "last_activity" in update_calls[0][2]


@pytest.mark.asyncio
async def test_session_manager_load_context_returns_empty_when_no_memories(fake_store):
    """No stored memories → empty string (chat surface omits the context block)."""
    from omicsclaw.runtime.agent.session import SessionManager

    mgr = SessionManager(fake_store)
    await mgr.get_or_create("carol", "slack", "abc")

    ctx = await mgr.load_context("slack:carol:abc")

    assert ctx == ""


@pytest.mark.asyncio
async def test_session_manager_load_context_renders_dataset_block(fake_store):
    """A dataset memory surfaces as the labelled "Current Dataset" block."""
    from omicsclaw.runtime.agent.session import SessionManager

    fake_store.get_memories = _make_get_memories({
        "dataset": [SimpleNamespace(
            file_path="/tmp/sample.h5ad",
            platform="visium",
            n_obs=4321,
            n_vars=2000,
            preprocessing_state="raw",
        )],
    })

    mgr = SessionManager(fake_store)
    ctx = await mgr.load_context("anywhere")

    assert "**Current Dataset**" in ctx
    assert "/tmp/sample.h5ad" in ctx
    assert "visium" in ctx
    assert "4321" in ctx
    assert "preprocessed=raw" in ctx


@pytest.mark.asyncio
async def test_session_manager_load_context_swallows_per_kind_errors(fake_store):
    """A failing dataset fetch must not block analyses / preferences from
    rendering — each ``get_memories`` call is wrapped individually in the
    production loader, so this test pins that behaviour."""
    from omicsclaw.runtime.agent.session import SessionManager

    async def _flaky_get_memories(session_id, kind, *, limit, thread_id=""):
        if kind == "dataset":
            raise RuntimeError("decryption failed")
        if kind == "preference":
            return [SimpleNamespace(key="theme", value="dark")]
        return []

    fake_store.get_memories = _flaky_get_memories

    mgr = SessionManager(fake_store)
    ctx = await mgr.load_context("anywhere")

    assert "**User Preferences**" in ctx
    assert "theme: dark" in ctx
    assert "**Current Dataset**" not in ctx


@pytest.mark.asyncio
async def test_load_context_scopes_only_thread_carrying_types(fake_store):
    """AN-CTXRECALL-11: passive injection scopes ONLY the thread-carrying types.

    ``dataset`` / ``analysis`` carry a ``thread_id`` and must be scoped to the
    active thread; the global user-level types (``preference`` / ``insight`` /
    ``project_context``) carry none, so passing ``thread_id`` would filter every
    one of them out (the "starve the globals" bug from BE-RECALL-6). This pins
    that load_context forwards the thread_id only to dataset+analysis."""
    from omicsclaw.runtime.agent.session import SessionManager

    mgr = SessionManager(fake_store)
    await mgr.load_context("app:alice:t", thread_id="t-glioma")

    by_kind = {
        kind: kwargs["thread_id"]
        for (name, (sid, kind), kwargs) in fake_store.calls
        if name == "get_memories"
    }
    assert by_kind["dataset"] == "t-glioma"
    assert by_kind["analysis"] == "t-glioma"
    assert by_kind["preference"] == ""
    assert by_kind["insight"] == ""
    assert by_kind["project_context"] == ""


@pytest.mark.asyncio
async def test_load_context_empty_thread_id_is_unscoped(fake_store):
    """An empty thread_id is byte-identical to the legacy load: every type is
    fetched un-scoped (thread_id="" on every call)."""
    from omicsclaw.runtime.agent.session import SessionManager

    mgr = SessionManager(fake_store)
    await mgr.load_context("app:alice:t")

    threads = {
        kwargs["thread_id"]
        for (name, _args, kwargs) in fake_store.calls
        if name == "get_memories"
    }
    assert threads == {""}


def test_received_files_is_module_dict():
    """``omicsclaw.runtime.agent.session.received_files`` is the canonical dict; ``omicsclaw.runtime.agent.state``
    re-exports it. ``omicsclaw/app/_attachments.py`` reads via
    ``omicsclaw.runtime.agent.state.received_files`` — both paths must point at the same object."""
    import omicsclaw.runtime.agent.state
    import omicsclaw.runtime.agent.session

    assert omicsclaw.runtime.agent.state.received_files is omicsclaw.runtime.agent.session.received_files
    omicsclaw.runtime.agent.session.received_files["sentinel"] = {"chat_id": "test"}
    try:
        assert omicsclaw.runtime.agent.state.received_files["sentinel"]["chat_id"] == "test"
    finally:
        omicsclaw.runtime.agent.session.received_files.pop("sentinel", None)


def test_evict_lru_conversations_keeps_tool_results_for_evicted_chats(monkeypatch):
    """ADR 0040 D6 / B2: LRU eviction clears the in-memory working set ONLY — it must
    NOT delete tool-result blobs (a later revisit rehydrates the transcript from
    transcripts.db, whose full_result_path refs must still resolve). Only an explicit
    /clear deletes durable state."""
    cleared: list = []

    fake_transcript_store = SimpleNamespace(
        max_conversations=0,
        evict_lru_conversations=lambda: ["chat-1", "chat-2"],
    )
    fake_tool_result_store = SimpleNamespace(
        clear=lambda chat_id: cleared.append(chat_id),
    )

    import omicsclaw.runtime.agent.state as _core
    monkeypatch.setattr(_core, "transcript_store", fake_transcript_store, raising=False)
    monkeypatch.setattr(_core, "tool_result_store", fake_tool_result_store, raising=False)
    monkeypatch.setattr(_core, "MAX_CONVERSATIONS", 50, raising=False)

    from omicsclaw.runtime.agent.session import _evict_lru_conversations

    _evict_lru_conversations()

    # Eviction is memory-only: blobs are NOT deleted (they survive for rehydrate).
    assert cleared == []
    assert fake_transcript_store.max_conversations == 50


def test_clear_conversation_fans_out_to_both_stores(monkeypatch):
    """ADR 0040 D6: /clear is the ONLY path that deletes durable state and MUST fan
    out to BOTH the transcript store (rows) AND the tool-result store (blobs), so no
    orphan blob survives. ``clear_conversation`` is the single seam every surface
    (channels, TUI, interactive REPL) routes through, so the fan-out cannot be
    forgotten by one surface (the CLI/TUI orphaned-blob bug)."""
    cleared_transcript: list = []
    cleared_tool: list = []

    fake_transcript_store = SimpleNamespace(
        clear=lambda chat_id: cleared_transcript.append(chat_id)
    )
    fake_tool_result_store = SimpleNamespace(
        clear=lambda chat_id: cleared_tool.append(chat_id)
    )

    import omicsclaw.runtime.agent.state as _core
    monkeypatch.setattr(_core, "transcript_store", fake_transcript_store, raising=False)
    monkeypatch.setattr(_core, "tool_result_store", fake_tool_result_store, raising=False)

    _core.clear_conversation("chat-42")

    assert cleared_transcript == ["chat-42"]
    assert cleared_tool == ["chat-42"]


def _make_get_memories(by_kind: dict[str, list]):
    async def _impl(session_id, kind, *, limit, thread_id=""):
        return by_kind.get(kind, [])
    return _impl
