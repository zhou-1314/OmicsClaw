"""BE-PERSONA-7 + BE-PERSONA-BOOT-9 (Bench Phase 4) — the agent's research-stance
persona layer.

``core://agent/research_stance`` (a thin tone) boot-loads alongside ``core://agent``
and injects at the persona layer, just BELOW the base persona (SOUL.md). It is
opt-in: an empty / absent stance is a no-op, so every surface's prompt stays
byte-identical until a stance is explicitly set.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from omicsclaw.runtime.context.layers import ContextAssemblyRequest
from omicsclaw.runtime.context.assembler import (
    assemble_chat_context,
    assemble_prompt_context,
)
from omicsclaw.runtime.context.system_prompt import build_system_prompt
from omicsclaw.memory.compat import CompatMemoryStore


# --------------------------------------------------------------------------- #
# Composition order (the plan's acceptance) + empty = no-op                    #
# --------------------------------------------------------------------------- #

def test_research_stance_layer_is_subordinate_to_base_persona():
    req = ContextAssemblyRequest(
        surface="bot", base_persona="BASE PERSONA TEXT", research_stance="STANCE TONE TEXT"
    )
    names = [layer.name for layer in assemble_prompt_context(request=req).layers]
    assert "base_persona" in names and "research_stance" in names
    # subordinate: persona first, stance immediately below it.
    assert names.index("base_persona") < names.index("research_stance")
    if "surface_voice_rules" in names:
        assert names.index("research_stance") < names.index("surface_voice_rules")


def test_research_stance_empty_yields_no_layer():
    req = ContextAssemblyRequest(surface="bot", base_persona="BASE", research_stance="")
    names = [layer.name for layer in assemble_prompt_context(request=req).layers]
    assert "research_stance" not in names


def test_build_system_prompt_injects_stance_below_persona():
    empty = build_system_prompt(surface="bot", base_persona="BASE PERSONA")
    stance = build_system_prompt(
        surface="bot", base_persona="BASE PERSONA", research_stance="STANCE-TONE-XYZ"
    )
    assert "STANCE-TONE-XYZ" not in empty
    assert "STANCE-TONE-XYZ" in stance
    assert stance.index("BASE PERSONA") < stance.index("STANCE-TONE-XYZ")


def test_build_system_prompt_empty_stance_is_byte_identical():
    a = build_system_prompt(surface="bot", base_persona="BASE PERSONA")
    b = build_system_prompt(surface="bot", base_persona="BASE PERSONA", research_stance="")
    assert a == b  # empty stance == not passing it (legacy byte-identical)


# --------------------------------------------------------------------------- #
# Runtime wiring — loader -> build_system_prompt                               #
# --------------------------------------------------------------------------- #

def test_assemble_chat_context_forwards_stance_from_loader():
    seen = {}

    class FakeSessionManager:
        async def get_or_create(self, user_id, platform, chat_id, thread_id=""):
            pass

        async def load_context(self, session_id, thread_id=""):
            return ""

    async def loader(session_id):
        seen["sid"] = session_id
        return "RESEARCH STANCE FROM MEMORY"

    def fake_builder(**kwargs):
        seen["research_stance"] = kwargs.get("research_stance", "<<absent>>")
        return "PROMPT"

    asyncio.run(
        assemble_chat_context(
            chat_id="c1",
            user_content="hi",
            user_id="u1",
            platform="app",
            session_manager=FakeSessionManager(),
            system_prompt_builder=fake_builder,
            research_stance_loader=loader,
        )
    )
    assert seen["sid"] == "app:u1:c1"
    assert seen["research_stance"] == "RESEARCH STANCE FROM MEMORY"


def test_assemble_chat_context_without_loader_is_noop():
    seen = {}

    class FakeSessionManager:
        async def get_or_create(self, user_id, platform, chat_id, thread_id=""):
            pass

        async def load_context(self, session_id, thread_id=""):
            return ""

    def fake_builder(**kwargs):
        seen["research_stance"] = kwargs.get("research_stance", "<<absent>>")
        return "PROMPT"

    asyncio.run(
        assemble_chat_context(
            chat_id="c1",
            user_content="hi",
            user_id="u1",
            platform="app",
            session_manager=FakeSessionManager(),
            system_prompt_builder=fake_builder,
        )
    )
    # No loader → research_stance never added to builder kwargs (byte-identical).
    assert seen["research_stance"] == "<<absent>>"


# --------------------------------------------------------------------------- #
# Memory: recall_agent_uri + boot-load (BE-PERSONA-BOOT-9)                      #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def store(tmp_path):
    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await store.initialize()
    yield store


@pytest.mark.asyncio
async def test_recall_agent_uri_absent_then_set(store):
    session = await store.create_session("u", "telegram")
    sid = session.session_id
    uri = "core://agent/research_stance"

    # Absent row → "" (persona layer is a no-op).
    assert await store.recall_agent_uri(sid, uri) == ""

    client = await store._client_for_session(sid)
    await client.remember(uri=uri, content="STANCE JSON TONE")
    assert await store.recall_agent_uri(sid, uri) == "STANCE JSON TONE"


def test_make_research_stance_loader_none_when_no_store():
    """The loader factory degrades to None (→ no-op) when memory is unavailable,
    and returns a callable when the store exposes recall_agent_uri."""
    from omicsclaw.engine.loop import _make_research_stance_loader

    assert _make_research_stance_loader(None) is None

    class _NoStore:
        pass

    assert _make_research_stance_loader(_NoStore()) is None

    class _Store:
        async def recall_agent_uri(self, session_id, uri):
            return ""

    class _SM:
        store = _Store()

    loader = _make_research_stance_loader(_SM())
    assert loader is not None and callable(loader)


@pytest.mark.asyncio
async def test_boot_includes_research_stance_uri(store):
    session = await store.create_session("u", "telegram")
    sid = session.session_id
    client = await store._client_for_session(sid)
    await client.remember(uri="core://agent/research_stance", content="BOOTED STANCE")

    boot_text = await client.boot()
    assert "core://agent/research_stance" in boot_text
    assert "BOOTED STANCE" in boot_text
