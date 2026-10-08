"""Which augmentors an exchange gets, and how several become one."""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
from typing import Any, AsyncIterator

import pytest

from omicsclaw.engine import AgentEngine, TurnAugmentor
from omicsclaw.entry import assembly, nudges
from omicsclaw.entry.assembly import build_app
from omicsclaw.entry.config import AppConfig
from omicsclaw.entry.nudges import AugmentorChain, build_augmentor, chain
from omicsclaw.entry.turn import _assemble
from omicsclaw.provider import Completion
from omicsclaw.schema import (
    Message,
    Role,
    StreamChunk,
    StreamChunkType,
    ToolDefinition,
)

WAIT_S = 20.0


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, WAIT_S))


@dataclasses.dataclass
class _ScriptedProvider:
    """Replies from a script, then with ``done``, and records what it was sent."""

    replies: list[Message] = dataclasses.field(default_factory=list)
    seen: list[tuple[Message, ...]] = dataclasses.field(default_factory=list)

    @property
    def name(self) -> str:
        return "scripted"

    async def generate(self, messages, tools=None) -> Completion:
        self.seen.append(tuple(messages))
        reply = (
            self.replies.pop(0)
            if self.replies
            else Message(role=Role.ASSISTANT, content="done")
        )
        return Completion(message=reply, finish_reason="stop")

    async def _stream(self, messages, tools) -> AsyncIterator[StreamChunk]:
        completion = await self.generate(messages, tools)
        yield StreamChunk(type=StreamChunkType.DONE, message=completion.message)

    def generate_stream(self, messages, tools=None):
        return self._stream(messages, tools)

    def bind(self, **overrides: Any) -> "_ScriptedProvider":
        return self


def _config(workspace: pathlib.Path, **overrides: object) -> AppConfig:
    (workspace / "OMICSCLAW.md").write_text("You are OmicsClaw.", encoding="utf-8")
    return AppConfig(workspace=workspace, **overrides)


def _app_with(tmp_path, monkeypatch, provider: _ScriptedProvider, **overrides):
    monkeypatch.setattr(assembly, "provider_from_env", lambda p, m: provider)
    app = build_app(_config(tmp_path, **overrides))
    return dataclasses.replace(
        app, engine=AgentEngine(provider, app.registry, app.config.engine_config())
    )


class _Member:
    """Appends one message naming itself, and keeps what it was given."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.given: list[tuple[Any, Any]] = []

    async def augment(self, history, tools=()):
        self.given.append((history, tools))
        return (Message(role=Role.USER, content=self.label),)


class _Silent:
    async def augment(self, history, tools=()):
        return ()


class _Broken:
    async def augment(self, history, tools=()):
        raise RuntimeError("could not read the plan")


HISTORY = (Message.user("load the matrix"),)
TOOLS = (ToolDefinition("read_file", "read a file", {"type": "object"}),)


# ---- chain ---------------------------------------------------------------


def test_no_members_is_no_augmentor():
    assert chain() is None
    assert chain(None, None) is None


def test_a_single_member_is_handed_back_as_it_is():
    """One member needs no wrapper, so the engine gets the object itself."""
    only = _Member("only")

    assert chain(only) is only
    assert chain(None, only, None) is only


def test_several_members_become_one_augmentor():
    joined = chain(_Member("a"), None, _Member("b"))

    assert isinstance(joined, AugmentorChain)
    assert isinstance(joined, TurnAugmentor)


def test_a_chain_appends_in_member_order():
    joined = chain(_Member("first"), _Silent(), _Member("second"), _Member("third"))
    assert joined is not None

    appended = _run(joined.augment(HISTORY, TOOLS))

    assert [message.content for message in appended] == ["first", "second", "third"]


def test_every_member_is_given_the_same_history_and_tools():
    """No member sees what the member before it appended."""
    first, second = _Member("first"), _Member("second")
    joined = chain(first, second)
    assert joined is not None

    _run(joined.augment(HISTORY, TOOLS))

    assert first.given == [(HISTORY, TOOLS)]
    assert second.given[0][0] is HISTORY
    assert second.given[0][1] is TOOLS


def test_a_member_s_failure_leaves_the_chain():
    joined = chain(_Member("first"), _Broken(), _Member("never asked"))
    assert joined is not None

    with pytest.raises(RuntimeError, match="could not read the plan"):
        _run(joined.augment(HISTORY, TOOLS))


# ---- what an exchange is given -------------------------------------------


def test_with_planning_alone_the_engine_gets_the_injector_itself(
    tmp_path, monkeypatch
):
    app = _app_with(tmp_path, monkeypatch, _ScriptedProvider(), memory=False)
    injector = _Member("the plan")
    asked: list[str] = []

    def build_injector(app, *, session_id=""):
        asked.append(session_id)
        return injector

    monkeypatch.setattr(nudges, "build_injector", build_injector)

    exchange = _assemble(app, [], session_id="sess-1")

    assert exchange.augmentor is injector
    assert asked == ["sess-1"]


def test_an_exchange_with_nothing_to_add_gets_no_augmentor(tmp_path, monkeypatch):
    app = _app_with(
        tmp_path, monkeypatch, _ScriptedProvider(), memory=False, planning=False
    )

    assert build_augmentor(app, session_id="sess-1") is None
    assert _assemble(app, [], session_id="sess-1").augmentor is None


def test_a_compaction_only_exchange_builds_no_augmentor(tmp_path, monkeypatch):
    app = _app_with(tmp_path, monkeypatch, _ScriptedProvider(), memory=False)

    def build_injector(app, *, session_id=""):
        raise AssertionError("a compaction-only exchange has no turn to remind")

    monkeypatch.setattr(nudges, "build_injector", build_injector)

    assert _assemble(app, [], session_id="sess-1", plan_block=False).augmentor is None
