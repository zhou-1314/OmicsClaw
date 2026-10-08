"""Which augmentors an exchange gets, and how several become one."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
from typing import Any, AsyncIterator

import pytest

from omicsclaw.context import (
    DEFAULT_MEMORY_NUDGE_TURNS,
    MEMORY_NUDGE_TEXT,
    MemoryNudge,
)
from omicsclaw.engine import AgentEngine, TurnAugmentor
from omicsclaw.entry import assembly, nudges
from omicsclaw.entry.assembly import build_app
from omicsclaw.entry.config import AppConfig, resolve_app_config
from omicsclaw.entry.memory import MEMORY_SEARCH_TOOL_NAME, MEMORY_WRITE_TOOL_NAME
from omicsclaw.entry.nudges import AugmentorChain, build_augmentor, chain
from omicsclaw.entry.session import attach_sessions
from omicsclaw.entry.turn import _assemble, run_turn
from omicsclaw.planning import INJECTION_HEADER, PlanInjector, PlanItem, PlanStatus
from omicsclaw.provider import Completion
from omicsclaw.schema import (
    Message,
    Role,
    StreamChunk,
    StreamChunkType,
    ToolCall,
    ToolDefinition,
)
from omicsclaw.subagent import TASK_TOOL_NAME

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


# ---- the memory reminder -------------------------------------------------

REMINDER = MEMORY_NUDGE_TEXT.format(tool=MEMORY_WRITE_TOOL_NAME)


def _says(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def _calls(name: str, **arguments: object) -> Message:
    return Message(
        role=Role.ASSISTANT,
        tool_calls=(
            ToolCall(id=f"c-{name}", name=name, arguments=json.dumps(arguments)),
        ),
    )


def _searches() -> Message:
    return _calls(MEMORY_SEARCH_TOOL_NAME, query="leiden")


def _reminded(provider: _ScriptedProvider) -> list[int]:
    """Which model calls, counted from 1, were sent the reminder."""
    return [
        number
        for number, sent in enumerate(provider.seen, start=1)
        if any(message.content == REMINDER for message in sent)
    ]


def test_the_reminder_fits_the_engine_s_seam():
    assert isinstance(MemoryNudge(write_tool=MEMORY_WRITE_TOOL_NAME), TurnAugmentor)


def test_a_default_deployment_reminds_before_it_restates_the_plan(
    tmp_path, monkeypatch
):
    app = _app_with(tmp_path, monkeypatch, _ScriptedProvider())
    injector = _Member("the plan")
    monkeypatch.setattr(
        nudges, "build_injector", lambda app, *, session_id="": injector
    )

    augmentor = build_augmentor(app, session_id="sess-1")

    assert isinstance(augmentor, AugmentorChain)
    first, second = augmentor._members
    assert isinstance(first, MemoryNudge)
    assert second is injector


def test_the_reminder_follows_the_configured_interval(tmp_path, monkeypatch):
    provider = _ScriptedProvider(
        replies=[_searches(), _searches(), _searches(), _searches(), _says("done")]
    )
    app = _app_with(tmp_path, monkeypatch, provider, memory_nudge_turns=2)

    _run(run_turn(app, [], "what did I tell you about leiden?", session_id="s"))

    assert _reminded(provider) == [3, 5]


def test_the_plan_block_is_still_the_last_thing_the_model_reads(
    tmp_path, monkeypatch
):
    provider = _ScriptedProvider(replies=[_searches(), _says("done")])
    app = _app_with(tmp_path, monkeypatch, provider, memory_nudge_turns=1)
    assert app.plans is not None
    app.plans.for_session("s").write(
        (PlanItem("1", "load the matrix", PlanStatus.IN_PROGRESS),)
    )

    _run(run_turn(app, [], "carry on", session_id="s"))

    second_call = provider.seen[1]
    assert second_call[-2].content == REMINDER
    assert second_call[-1].content.startswith(INJECTION_HEADER)


def test_without_memory_there_is_no_reminder(tmp_path, monkeypatch):
    provider = _ScriptedProvider()
    app = _app_with(
        tmp_path, monkeypatch, provider, memory=False, memory_nudge_turns=1
    )

    assert isinstance(build_augmentor(app, session_id="s"), PlanInjector)
    _run(run_turn(app, [_says("earlier")], "hello", session_id="s"))
    assert _reminded(provider) == []


def test_an_interval_of_zero_turns_the_reminder_off(tmp_path, monkeypatch):
    provider = _ScriptedProvider(replies=[_searches(), _searches(), _says("done")])
    app = _app_with(tmp_path, monkeypatch, provider, memory_nudge_turns=0)

    assert app.memory is not None
    assert isinstance(build_augmentor(app, session_id="s"), PlanInjector)
    _run(run_turn(app, [], "hello", session_id="s"))
    assert _reminded(provider) == []


def test_without_planning_the_engine_gets_the_reminder_itself(tmp_path, monkeypatch):
    app = _app_with(tmp_path, monkeypatch, _ScriptedProvider(), planning=False)

    assert isinstance(build_augmentor(app, session_id="s"), MemoryNudge)


def test_a_deployment_that_did_not_mount_memory_write_is_never_reminded(
    tmp_path, monkeypatch
):
    """The memory database is open, but the caller's tools leave the write out."""

    class _Lookup:
        @property
        def name(self) -> str:
            return "lookup"

        def definition(self) -> ToolDefinition:
            return ToolDefinition(name="lookup", description="looks something up")

        async def execute(self, arguments: str) -> str:
            return "nothing"

    provider = _ScriptedProvider(
        replies=[_calls("lookup"), _calls("lookup"), _says("done")]
    )
    monkeypatch.setattr(assembly, "provider_from_env", lambda p, m: provider)
    app = build_app(_config(tmp_path, memory_nudge_turns=1), tools=[_Lookup()])
    app = dataclasses.replace(
        app, engine=AgentEngine(provider, app.registry, app.config.engine_config())
    )

    assert app.memory is not None
    assert MEMORY_WRITE_TOOL_NAME not in app.registry.names()
    assert isinstance(build_augmentor(app, session_id="s"), MemoryNudge)
    _run(run_turn(app, [], "hello", session_id="s"))
    assert len(provider.seen) == 3
    assert _reminded(provider) == []


def test_a_session_of_short_exchanges_is_reminded_once_and_keeps_none_of_it(
    tmp_path, monkeypatch
):
    """Three exchanges of four model calls, none long enough to compact.

    Ten turns are behind the third call of the third exchange, so that
    call alone carries the reminder. The session, in the registry and in
    the database, holds the three questions and nothing the nudge said.
    """
    questions = ["first question", "second question", "third question"]
    provider = _ScriptedProvider(
        replies=[_searches(), _searches(), _searches(), _says("answer")] * 3
    )
    app = attach_sessions(
        _app_with(tmp_path, monkeypatch, provider), abandon_grace_s=None
    )
    assert app.sessions is not None and app.memory is not None
    assert app.config.memory_nudge_turns == DEFAULT_MEMORY_NUDGE_TURNS == 10

    async def converse() -> None:
        for question in questions:
            handle = await app.sessions.submit("sess-1", question)
            assert await handle.wait() is not None

    _run(converse())
    live = app.sessions.session("sess-1")
    stored = _run(app.sessions._store.load("sess-1"))
    app.memory.close()

    assert len(provider.seen) == 12
    assert _reminded(provider) == [11]
    assert provider.seen[10][-1].content == REMINDER
    assert live is not None and stored is not None
    for history in (live.history, stored.history):
        users = [m.content for m in history if m.role is Role.USER]
        assert users == questions
        assert not any(REMINDER in (m.content or "") for m in history)


def test_a_write_starts_the_count_again_across_exchanges(tmp_path, monkeypatch):
    provider = _ScriptedProvider(
        replies=[
            _searches(),
            _calls(MEMORY_WRITE_TOOL_NAME, action="add", content="用 leiden 聚类"),
            _says("noted"),
            _searches(),
            _searches(),
            _says("answer"),
        ]
    )
    app = attach_sessions(
        _app_with(tmp_path, monkeypatch, provider, memory_nudge_turns=2),
        abandon_grace_s=None,
    )
    assert app.sessions is not None and app.memory is not None

    async def converse() -> None:
        for question in ("remember this", "and now?"):
            handle = await app.sessions.submit("sess-1", question)
            assert await handle.wait() is not None

    _run(converse())
    kept = _run(app.memory.store.list())
    app.memory.close()

    # Calls 1-3 are the first exchange; the write is the second turn, so
    # call 3 has one turn behind it and call 5 has two.
    assert [entry.content for entry in kept] == ["用 leiden 聚类"]
    assert _reminded(provider) == [5]


def test_a_sub_agent_is_never_reminded(tmp_path, monkeypatch):
    """The delegation runs through the real ``task`` tool and a child engine."""
    provider = _ScriptedProvider(
        replies=[
            _calls(TASK_TOOL_NAME, subagent_type="general-purpose", prompt="look"),
            _searches(),
            _searches(),
            _says("the child is done"),
            _says("the parent is done"),
        ]
    )
    app = _app_with(tmp_path, monkeypatch, provider, memory_nudge_turns=1)

    outcome = _run(run_turn(app, [], "delegate this", session_id="s"))

    assert outcome.reply == "the parent is done"
    assert len(provider.seen) == 5
    child_calls = [sent for sent in provider.seen if sent[1].content == "look"]
    assert len(child_calls) == 3
    # Call 5 is the parent's second; calls 2-4 are the child's.
    assert _reminded(provider) == [5]


# ---- the setting ---------------------------------------------------------


def test_the_interval_defaults_to_the_reminder_s_own_default(tmp_path):
    assert AppConfig(workspace=tmp_path).memory_nudge_turns == 10


def test_the_interval_is_readable_from_the_command_line(tmp_path):
    config = resolve_app_config(
        argv=["--memory-nudge-turns", "3"],
        env={"OMICSCLAW_WORKSPACE": str(tmp_path)},
    )

    assert config.memory_nudge_turns == 3


def test_the_interval_is_readable_from_the_environment(tmp_path):
    config = resolve_app_config(
        argv=[],
        env={
            "OMICSCLAW_WORKSPACE": str(tmp_path),
            "OMICSCLAW_MEMORY_NUDGE_TURNS": "0",
        },
    )

    assert config.memory_nudge_turns == 0
