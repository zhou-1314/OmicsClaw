"""Slash commands, after the globals they used to read were deleted.

The registry crossed from ``omicsclaw/surfaces/channels/commands/`` almost
unchanged; the handler bodies did not. Every one of them read something out
of the deleted ``omicsclaw.runtime.agent.state`` — a transcript store, a
data directory, a provider name — and each now reads the same fact off the
assembled :class:`~omicsclaw.entry.assembly.AgentApp`.

Three commands were removed rather than reconnected (``/forget``,
``/compact``, ``/plan``), and the test that matters most in this file is the
one asserting ``/help`` does not still advertise them: a help text is the
one place a stale list is read by exactly the person it misleads.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import warnings

import pytest

from omicsclaw.context import CompactionState
from omicsclaw.entry.channel.base import Channel
from omicsclaw.entry.channel.commands import (
    SlashCommandContext,
    _registry,
    dispatch,
    registered_commands,
)
from omicsclaw.entry.channel.config import BaseChannelConfig
from omicsclaw.entry.channel.runtime import VALUE_REPLY_TARGET
from omicsclaw.entry.ingress import InboundMessage
from omicsclaw.entry.session import InMemorySessionStore, attach_sessions
from omicsclaw.provider import Completion
from omicsclaw.schema import Message, Role
from tests.entry.test_channel_ingress import (  # type: ignore[import-not-found]
    OWNER,
    Transport,
    binding_for,
    deployment,
)
from tests.entry.test_channel_runtime import (  # type: ignore[import-not-found]
    until,
)
from tests.entry.test_turn_runner import (  # type: ignore[import-not-found]
    Scripted,
    make_app,
)
from omicsclaw.entry.channel.runtime import ChannelRuntime

WAIT_S = 5.0

EXPECTED = (
    "/clear",
    "/compact",
    "/demo",
    "/examples",
    "/files",
    "/help",
    "/new",
    "/outputs",
    "/recent",
    "/skills",
    "/status",
    "/version",
)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, WAIT_S))


def context(app=None, *, text: str, session_id: str = "feishu:c1", workspace=""):
    return SlashCommandContext(
        chat_id="c1",
        user_id=OWNER,
        platform="feishu",
        user_text=text,
        workspace=str(workspace or (app.config.workspace if app else "")),
        app=app,
        session_id=session_id,
    )


def test_the_registered_set_is_exactly_what_survived_the_port():
    """Named, not counted. A count tells you something changed, not what."""
    assert registered_commands() == EXPECTED


def test_the_two_blocked_commands_are_absent():
    """``/forget`` touches the memory layer, which is not in this step.

    Half of a "complete reset" is worse than none: the person asked for
    their memory to be deleted and would be told it was. ``/compact`` was
    the third until plan 0035 gave it a lane-serialized implementation.
    """
    for name in ("/forget", "/plan"):
        assert name not in registered_commands()


def test_help_advertises_exactly_what_is_registered(tmp_path):
    """Generated from the registry, so it cannot drift from it."""
    reply = run(dispatch(context(text="/help")))

    assert reply is not None
    for name in registered_commands():
        assert f"- {name}" in reply
    for name in ("/forget", "/plan"):
        assert name not in reply


def test_anything_that_is_not_a_command_falls_through(tmp_path):
    """``None`` means "hand it to the agent", which is the common path."""
    assert run(dispatch(context(text="analyse this slide"))) is None
    assert run(dispatch(context(text="/nonesuch"))) is None


def test_commands_are_matched_case_insensitively(tmp_path):
    assert run(dispatch(context(text="  /HELP  "))) is not None


def test_skills_reports_the_index_this_deployment_loaded(tmp_path):
    """The same scan the system prompt was built from, not a second one."""

    async def scenario():
        app = deployment(tmp_path)[0]
        return await dispatch(context(app, text="/skills"))

    reply = run(scenario())

    assert reply == "No skills are loaded in this deployment."


def test_skills_renders_the_domain_summary_when_there_are_skills(tmp_path):
    """One scan, two readers: this and the prompt section must agree."""

    async def scenario():
        skills = tmp_path / "skills" / "spatial" / "spatial-de"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text(
            "---\nname: spatial-de\ndescription: Differential expression.\n---\n",
            encoding="utf-8",
        )
        app = deployment(tmp_path)[0]
        return app, await dispatch(context(app, text="/skills"))

    app, reply = run(scenario())

    assert len(app.skills) == 1
    assert reply == app.skills.domain_summary()
    assert "spatial-de" in reply


def test_status_reports_the_assembled_deployment(tmp_path):
    """What was actually wired, not what a module global once said."""

    async def scenario():
        app = deployment(tmp_path)[0]
        return app, await dispatch(context(app, text="/status"))

    app, reply = run(scenario())

    assert reply is not None
    assert "Uptime:" in reply
    assert f"Provider: {app.provider.name}" in reply
    assert f"Tools available: {len(app.tools_snapshot)}" in reply
    assert str(app.config.workspace) in reply


def test_status_without_a_deployment_says_so_rather_than_raising(tmp_path):
    """A command handler raising lands inside a vendor SDK's callback."""
    reply = run(dispatch(context(text="/status")))

    assert reply is not None
    assert "not bound to a deployment" in reply


def test_clear_empties_the_conversation_this_message_came_from(tmp_path):
    """And the carried summary with it.

    Keeping the compaction state over a cleared history would leave the
    next exchange summarising a conversation that no longer exists — and
    the summary is the half the model reads.
    """

    async def scenario():
        app, _spy = deployment(tmp_path)
        runtime = ChannelRuntime(app, [binding_for(Transport())])
        await runtime.start()
        result = await runtime.submit(
            InboundMessage(
                text="hello",
                session_id="feishu:c1",
                source_request_id="r1",
                sender=OWNER,
                surface="feishu",
                values={
                    VALUE_REPLY_TARGET: {"adapter": "feishu", "destination_id": "c1"}
                },
            )
        )
        assert result.handle is not None
        await result.handle.wait()
        session = app.sessions.session("feishu:c1")
        assert session is not None and session.history, "nothing to clear"
        # A summary the previous compaction left behind. Planted rather than
        # earned: one short exchange never triggers compaction, and without
        # it this test cannot tell a ``/clear`` that keeps the summary from
        # one that drops it — measured, that mutation survived.
        session.compaction = CompactionState(summary="they discussed a slide")
        reply = await dispatch(context(app, text="/clear"))
        await runtime.close(WAIT_S)
        return reply, session

    reply, session = run(scenario())

    assert reply == "Conversation history cleared."
    assert session.history == ()
    assert session.compaction.summary == ""


def test_clear_without_a_conversation_says_so(tmp_path):
    async def scenario():
        app = deployment(tmp_path)[0]
        return await dispatch(context(app, text="/clear", session_id=""))

    assert "not in one" in run(scenario())


# ---- /clear and /new against the store, a restart and a busy lane -------


CLEARING = [
    ("/clear", "Conversation history cleared."),
    ("/new", "New conversation started."),
]


class Gated(Scripted):
    """``Scripted`` whose call number *hold* waits to be released."""

    def __init__(self, *replies: Message, hold: int = 0) -> None:
        super().__init__(*replies)
        self.hold = hold
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, messages, tools=None):
        if self.calls == self.hold:
            self.entered.set()
            await self.release.wait()
        return await super().generate(messages, tools)


class ReadOnly(InMemorySessionStore):
    """A store that can be told to refuse every save."""

    def __init__(self) -> None:
        super().__init__()
        self.refusing = False

    async def save(self, session) -> None:
        if self.refusing:
            raise OSError("database is locked")
        await super().save(session)


def inbound(text: str, request: str, *, surface: str = "feishu") -> InboundMessage:
    return InboundMessage(
        text=text,
        session_id=f"{surface}:c1",
        source_request_id=request,
        sender=OWNER,
        surface=surface,
        values={VALUE_REPLY_TARGET: {"adapter": surface, "destination_id": "c1"}},
    )


async def exchange(app, text: str, request: str, *, surface: str = "feishu"):
    """One message through a runtime of its own, waited to its end."""
    runtime = ChannelRuntime(app, [binding_for(Transport(), adapter=surface)])
    await runtime.start()
    result = await runtime.submit(inbound(text, request, surface=surface))
    assert result.handle is not None
    await result.handle.wait()
    await runtime.close(WAIT_S)
    return result.handle


def asked(messages) -> list[str]:
    """The user messages of one model call, in order."""
    return [m.content for m in messages if m.role is Role.USER]


def answers(messages) -> list[str]:
    """The assistant messages of one model call, in order."""
    return [m.content for m in messages if m.role is Role.ASSISTANT]


@pytest.mark.parametrize("verb, reply", CLEARING)
def test_clear_reaches_a_conversation_saved_before_this_process_started(
    tmp_path, verb, reply
):
    """After a restart the conversation is in the database and not in memory.

    Two apps over one workspace stand in for the two processes. The
    second has not loaded the conversation when the command arrives.
    """

    async def scenario():
        earlier = deployment(tmp_path, Scripted(Message.assistant("FIRST ANSWER.")))[0]
        await exchange(earlier, "first question", "r0")
        await earlier.aclose()

        provider = Scripted(Message.assistant("SECOND ANSWER."))
        app = deployment(tmp_path, provider)[0]
        assert app.sessions.session("feishu:c1") is None
        said = await dispatch(context(app, text=verb))
        await exchange(app, "second question", "r1")
        await app.aclose()
        return said, provider.seen[-1]

    said, seen = run(scenario())

    assert said == reply
    assert asked(seen) == ["second question"]
    assert answers(seen) == []


@pytest.mark.parametrize("verb, reply", CLEARING)
def test_a_cleared_conversation_is_still_empty_after_a_restart(tmp_path, verb, reply):
    """The command writes the store itself.

    Left to the next exchange's save, a process that stops between the
    command and the next message brings the old history back.
    """

    async def scenario():
        earlier = deployment(tmp_path, Scripted(Message.assistant("FIRST ANSWER.")))[0]
        await exchange(earlier, "first question", "r0")
        said = await dispatch(context(earlier, text=verb))
        await earlier.aclose()

        provider = Scripted(Message.assistant("SECOND ANSWER."))
        app = deployment(tmp_path, provider)[0]
        await exchange(app, "second question", "r1")
        await app.aclose()
        return said, provider.seen[-1]

    said, seen = run(scenario())

    assert said == reply
    assert asked(seen) == ["second question"]
    assert answers(seen) == []


@pytest.mark.parametrize("verb, reply", CLEARING)
def test_clear_is_answered_after_the_exchange_that_was_running(tmp_path, verb, reply):
    """A running exchange writes its history back when it ends.

    The command waits for it. The exchange keeps the context it was
    given, and the first message after the command starts with none.
    """

    async def scenario():
        provider = Gated(
            Message.assistant("FIRST ANSWER."),
            Message.assistant("SECOND ANSWER."),
            Message.assistant("THIRD ANSWER."),
            hold=1,
        )
        app = deployment(tmp_path, provider)[0]
        await exchange(app, "first question", "r0")
        runtime = ChannelRuntime(app, [binding_for(Transport())])
        await runtime.start()
        second = await runtime.submit(inbound("second question", "r1"))
        await asyncio.wait_for(provider.entered.wait(), WAIT_S)

        command = asyncio.ensure_future(dispatch(context(app, text=verb)))
        for _ in range(10):
            await asyncio.sleep(0)
        early = command.done()
        provider.release.set()
        said = await command
        finished = second.handle.done
        third = await runtime.submit(inbound("third question", "r2"))
        await third.handle.wait()
        await runtime.close(WAIT_S)
        await app.aclose()
        return early, said, finished, provider.seen

    early, said, finished, seen = run(scenario())

    assert not early, "the command answered while the exchange was running"
    assert said == reply
    assert finished, "the command answered before the exchange ahead of it ended"
    assert asked(seen[1]) == ["first question", "second question"]
    assert asked(seen[2]) == ["third question"]
    assert answers(seen[2]) == []


def test_clear_on_a_conversation_nobody_saved_stores_nothing(tmp_path):
    """The command answers as it does for an empty conversation."""

    async def scenario():
        app, spy = deployment(tmp_path)
        said = await dispatch(context(app, text="/clear"))
        listed = await spy.inner.list_sessions()
        await app.aclose()
        return said, listed

    said, listed = run(scenario())

    assert said == "Conversation history cleared."
    assert list(listed) == []


def owned_channel(app, transport):
    """A runtime, and a real ``Channel`` bound to it whose owner may run commands."""
    runtime = ChannelRuntime(app, [binding_for(transport, adapter="stub")])
    channel = _StubChannel(BaseChannelConfig(allowed_senders={OWNER}))
    channel.bind_control_runtime(runtime)
    return runtime, channel


TARGET = {"adapter": "stub", "destination_id": "c1"}


@pytest.mark.parametrize("verb", ["/clear", "/new"])
def test_a_clear_the_store_cannot_save_is_answered_as_a_failed_command(tmp_path, verb):
    """The command must not say the history is gone when the store kept it.

    The conversation is left as it was, so the next message still has
    its context.
    """

    async def scenario():
        store = ReadOnly()
        provider = Scripted(
            Message.assistant("FIRST ANSWER."), Message.assistant("SECOND ANSWER.")
        )
        app = attach_sessions(
            make_app(tmp_path, provider, tools=(), approval_timeout_s=WAIT_S),
            store=store,
        )
        await exchange(app, "first question", "r0", surface="stub")
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()
        store.refusing = True
        handled = await channel.answer_slash_command(TARGET, "c1", OWNER, verb)
        await runtime.close(WAIT_S)
        store.refusing = False
        await exchange(app, "second question", "r1", surface="stub")
        return handled, transport.sent, provider.seen[-1]

    handled, said, seen = run(scenario())

    assert handled is True
    assert said == ["That command failed."]
    assert asked(seen) == ["first question", "second question"]


def test_a_clear_the_queue_has_no_room_for_is_answered_as_a_failed_command(tmp_path):
    """A full queue refuses the command as it refuses a message."""

    async def scenario():
        provider = Gated(Message.assistant("ANSWER."))
        app = attach_sessions(
            make_app(
                tmp_path,
                provider,
                tools=(),
                approval_timeout_s=WAIT_S,
                max_queued_per_session=1,
            )
        )
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()
        await runtime.submit(inbound("first question", "r0", surface="stub"))
        await asyncio.wait_for(provider.entered.wait(), WAIT_S)
        await runtime.submit(inbound("second question", "r1", surface="stub"))
        await channel.answer_slash_command(TARGET, "c1", OWNER, "/clear")
        said = [text for text in transport.sent if not text.startswith("Queued")]
        provider.release.set()
        await runtime.close(WAIT_S)
        await app.aclose()
        return said

    assert run(scenario()) == ["That command failed."]


# ---- a command that waits for the queue leaves its callback free --------


WAITING = [
    ("/clear", "Conversation history cleared."),
    ("/new", "New conversation started."),
    ("/compact", "Nothing to compact yet: the conversation is already short."),
]
"""The commands that run in the conversation's lane, and what each says
about a conversation of one short exchange."""


class Echoing(Scripted):
    """Answers every request by naming it, and holds the requests in *held*.

    The request is read from the messages, so two conversations running
    at once each get their own answer, and one can be held while the
    other is answered.
    """

    def __init__(self, *held: str) -> None:
        super().__init__()
        self.held = set(held)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, messages, tools=None):
        request = asked(messages)[-1]
        if request in self.held:
            self.entered.set()
            await self.release.wait()
        self.seen.append(tuple(messages))
        self.calls += 1
        return Completion(message=Message.assistant(f"answer to {request}"))

    def call_for(self, request: str):
        """The messages of the model call that answered *request*."""
        (call,) = [seen for seen in self.seen if asked(seen)[-1] == request]
        return call


class Slow(InMemorySessionStore):
    """A store whose save of an emptied conversation waits to be released."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def save(self, session) -> None:
        if not session.history:
            self.entered.set()
            await self.release.wait()
        await super().save(session)


class Unreachable(Transport):
    """A delivery adapter whose platform call raises."""

    async def __call__(self, request):
        raise RuntimeError("the platform is unreachable")


async def loop_passes(count: int = 10) -> None:
    """Let whatever is ready to run, run."""
    for _ in range(count):
        await asyncio.sleep(0)


def command_tasks() -> list[str]:
    """The names of the command tasks alive on the running loop."""
    return sorted(
        task.get_name()
        for task in asyncio.all_tasks()
        if task.get_name().startswith("omicsclaw-command-")
    )


def run_watching_the_loop(scenario):
    """Run *scenario*, and return its result with what the loop complained of.

    asyncio reports a Task that ended with an exception nobody retrieved
    to the loop's exception handler when the Task is collected.
    """

    async def watched():
        complaints: list[str] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: complaints.append(str(context.get("message")))
        )
        result = await scenario()
        gc.collect()
        await loop_passes()
        return result, complaints

    return run(watched())


@pytest.mark.parametrize("verb, reply", WAITING)
def test_a_command_behind_a_running_exchange_does_not_hold_its_callback(
    tmp_path, verb, reply
):
    """The callback returns once the work is queued, and the reply follows.

    A platform is handed its next message when the callback returns, so a
    callback that waited would stop that for as long as the exchange
    ahead of the command runs.
    """

    async def scenario():
        provider = Gated(Message.assistant("FIRST ANSWER."))
        app = deployment(tmp_path, provider)[0]
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()
        first = await runtime.submit(inbound("first question", "r0", surface="stub"))
        await asyncio.wait_for(provider.entered.wait(), WAIT_S)

        command = asyncio.ensure_future(
            channel.answer_slash_command(TARGET, "c1", OWNER, verb)
        )
        await loop_passes()
        returned = command.done()
        early = list(transport.sent)
        provider.release.set()
        handled = await command
        await until(lambda: reply in transport.sent)
        ahead = first.handle.done
        await runtime.close(WAIT_S)
        await app.aclose()
        return returned, early, handled, ahead, transport.sent

    returned, early, handled, ahead, sent = run(scenario())

    assert returned, "the callback waited for the exchange ahead of the command"
    assert early == [], "the command was answered while the exchange was running"
    assert handled is True
    assert ahead, "the command was answered before the exchange ahead of it ended"
    assert sorted(sent) == sorted(["FIRST ANSWER.", reply])


@pytest.mark.parametrize("verb", ["/help", "/status", "/skills", "/files"])
def test_a_command_with_nothing_to_wait_for_is_answered_in_its_callback(
    tmp_path, verb
):
    """The reply is out when the callback returns, and no task is left."""

    async def scenario():
        app = deployment(tmp_path)[0]
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()
        handled = await channel.answer_slash_command(TARGET, "c1", OWNER, verb)
        said, tasks = list(transport.sent), command_tasks()
        await runtime.close(WAIT_S)
        return handled, said, tasks

    handled, said, tasks = run(scenario())

    assert handled is True
    assert len(said) == 1
    assert tasks == []


@pytest.mark.parametrize("verb", ["/clear", "/new"])
def test_a_message_right_behind_the_command_starts_from_an_empty_conversation(
    tmp_path, verb
):
    """The clear is in the queue by the time the command's callback returns.

    The message is submitted with nothing run in between, while the
    exchange ahead of the command is still running. It must land behind
    the clear.
    """

    async def scenario():
        provider = Gated(
            Message.assistant("FIRST ANSWER."),
            Message.assistant("SECOND ANSWER."),
            Message.assistant("THIRD ANSWER."),
            hold=1,
        )
        app = deployment(tmp_path, provider)[0]
        await exchange(app, "first question", "r0", surface="stub")
        runtime, channel = owned_channel(app, Transport())
        await runtime.start()
        await runtime.submit(inbound("second question", "r1", surface="stub"))
        await asyncio.wait_for(provider.entered.wait(), WAIT_S)

        await channel.answer_slash_command(TARGET, "c1", OWNER, verb)
        third = await runtime.submit(inbound("third question", "r2", surface="stub"))
        provider.release.set()
        assert third.handle is not None, third.acceptance
        await third.handle.wait()
        await runtime.close(WAIT_S)
        await app.aclose()
        return provider.seen

    seen = run(scenario())

    assert asked(seen[1]) == ["first question", "second question"]
    assert asked(seen[2]) == ["third question"]
    assert answers(seen[2]) == []


@pytest.mark.parametrize("verb, reply", CLEARING)
def test_a_message_behind_a_slow_clear_waits_for_it(tmp_path, verb, reply):
    """The store takes its time over the clear and nothing overtakes it.

    The callback has returned, the reply has not been sent, and the next
    message has not reached the model. All three hold until the store has
    the empty conversation.
    """

    async def scenario():
        store = Slow()
        provider = Scripted(
            Message.assistant("FIRST ANSWER."), Message.assistant("SECOND ANSWER.")
        )
        app = attach_sessions(
            make_app(tmp_path, provider, tools=(), approval_timeout_s=WAIT_S),
            store=store,
        )
        await exchange(app, "first question", "r0", surface="stub")
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()

        await channel.answer_slash_command(TARGET, "c1", OWNER, verb)
        await asyncio.wait_for(store.entered.wait(), WAIT_S)
        second = await runtime.submit(inbound("second question", "r1", surface="stub"))
        await loop_passes()
        early = [text for text in transport.sent if not text.startswith("Queued")]
        calls = provider.calls
        store.release.set()
        assert second.handle is not None, second.acceptance
        await second.handle.wait()
        await runtime.close(WAIT_S)
        return early, calls, transport.sent, provider.seen[-1]

    early, calls, sent, seen = run(scenario())

    assert early == [], "the command was answered before the store was written"
    assert calls == 1, "the next message ran before the clear had finished"
    assert reply in sent
    assert asked(seen) == ["second question"]
    assert answers(seen) == []


def test_a_queued_command_that_fails_is_logged_and_answered_from_its_task(
    tmp_path, caplog
):
    """The failure happens after the callback has returned.

    It is still logged with its traceback and answered, and the task that
    carried it is forgotten with nothing left for the loop to report.
    """

    async def scenario():
        store = ReadOnly()
        app = attach_sessions(
            make_app(
                tmp_path,
                Scripted(Message.assistant("FIRST ANSWER.")),
                tools=(),
                approval_timeout_s=WAIT_S,
            ),
            store=store,
        )
        await exchange(app, "first question", "r0", surface="stub")
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()
        store.refusing = True
        handled = await channel.answer_slash_command(TARGET, "c1", OWNER, "/clear")
        early = list(transport.sent)
        await until(lambda: transport.sent)
        await until(lambda: not command_tasks())
        await runtime.close(WAIT_S)
        return handled, early, transport.sent

    with caplog.at_level(logging.ERROR, logger="omicsclaw.entry.channel.base"):
        (handled, early, said), complaints = run_watching_the_loop(scenario)

    assert handled is True
    assert early == [], "the callback waited for the store"
    assert said == ["That command failed."]
    assert complaints == []
    failures = [
        record
        for record in caplog.records
        if record.name == "omicsclaw.entry.channel.base"
        and record.getMessage() == "stub command /clear failed"
    ]
    assert len(failures) == 1
    assert failures[0].exc_info is not None, "the traceback was not logged"


def test_a_reply_the_platform_call_raises_on_is_logged_by_type(tmp_path, caplog):
    """The error is retrieved from the task, so the loop has nothing to report.

    The log names the task and the type of the error and leaves out its
    text. The runtime forgets the task once it has ended.
    """

    async def scenario():
        app = deployment(tmp_path)[0]
        runtime, channel = owned_channel(app, Unreachable())
        await runtime.start()
        await channel.answer_slash_command(TARGET, "c1", OWNER, "/clear")
        await until(lambda: not command_tasks())
        await loop_passes()
        held = set(runtime._commands)
        await runtime.close(WAIT_S)
        return held

    with caplog.at_level(logging.ERROR, logger="omicsclaw.entry.channel.runtime"):
        held, complaints = run_watching_the_loop(scenario)

    assert held == set()
    assert complaints == []
    errors = [
        record.getMessage()
        for record in caplog.records
        if record.name == "omicsclaw.entry.channel.runtime"
    ]
    assert errors == ["omicsclaw-command-stub/clear failed (RuntimeError)"]


# ---- a command still waiting when the process stops ----------------------


def test_close_waits_for_a_command_that_ends_inside_the_grace(tmp_path):
    """The command is owed its reply as an exchange is owed its answer."""

    async def scenario():
        store = Slow()
        app = attach_sessions(
            make_app(
                tmp_path,
                Scripted(Message.assistant("FIRST ANSWER.")),
                tools=(),
                approval_timeout_s=WAIT_S,
            ),
            store=store,
        )
        await exchange(app, "first question", "r0", surface="stub")
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()
        await channel.answer_slash_command(TARGET, "c1", OWNER, "/clear")
        await asyncio.wait_for(store.entered.wait(), WAIT_S)

        closing = asyncio.ensure_future(runtime.close(WAIT_S))
        await loop_passes()
        waited = not closing.done()
        store.release.set()
        await closing
        return waited, transport.sent, set(runtime._commands), command_tasks()

    waited, said, held, tasks = run(scenario())

    assert waited, "close returned while a command was still waiting"
    assert said == ["Conversation history cleared."]
    assert held == set()
    assert tasks == []


def test_close_cancels_a_command_that_outlives_the_grace(tmp_path):
    """It is reaped and sends nothing. Its clear is left with the registry.

    The registry then finishes the clear inside its own grace, so the
    conversation ends up empty and nobody was told.
    """

    async def scenario():
        store = Slow()
        app = attach_sessions(
            make_app(
                tmp_path,
                Scripted(Message.assistant("FIRST ANSWER.")),
                tools=(),
                approval_timeout_s=WAIT_S,
            ),
            store=store,
        )
        await exchange(app, "first question", "r0", surface="stub")
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()
        await channel.answer_slash_command(TARGET, "c1", OWNER, "/clear")
        await asyncio.wait_for(store.entered.wait(), WAIT_S)

        await runtime.close(0.0)
        held, tasks = set(runtime._commands), command_tasks()
        store.release.set()
        await asyncio.wait_for(app.sessions.shutdown(WAIT_S), WAIT_S)
        await loop_passes()
        kept = await store.load("stub:c1")
        return held, tasks, transport.sent, kept.history

    (held, tasks, said, kept), complaints = run_watching_the_loop(scenario)

    assert held == set()
    assert tasks == []
    assert said == []
    assert kept == ()
    assert complaints == []


def test_a_clear_still_queued_when_the_process_stops_is_not_answered(tmp_path):
    """The runtime closes, then the registry, which is the launcher's order.

    The command's task is gone before the registry gives up on the
    clear, so no reply is sent, and the conversation is as it was.
    """

    async def scenario():
        provider = Gated(
            Message.assistant("FIRST ANSWER."),
            Message.assistant("SECOND ANSWER."),
            hold=1,
        )
        app = attach_sessions(
            make_app(tmp_path, provider, tools=(), approval_timeout_s=WAIT_S)
        )
        await exchange(app, "first question", "r0", surface="stub")
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()
        await runtime.submit(inbound("second question", "r1", surface="stub"))
        await asyncio.wait_for(provider.entered.wait(), WAIT_S)
        await channel.answer_slash_command(TARGET, "c1", OWNER, "/clear")

        await runtime.close(0.0)
        await asyncio.wait_for(app.sessions.shutdown(0.05), WAIT_S)
        await loop_passes()
        kept = app.sessions.session("stub:c1").history
        return transport.sent, [m.content for m in kept]

    said, kept = run(scenario())

    assert said == []
    assert kept == ["first question", "FIRST ANSWER."]


def test_a_clear_the_registry_cancelled_is_answered_as_a_failed_command(tmp_path):
    """The registry shuts down while the runtime is still open.

    Nothing was cleared, so the command must not say it was.
    """

    async def scenario():
        provider = Gated(
            Message.assistant("FIRST ANSWER."),
            Message.assistant("SECOND ANSWER."),
            hold=1,
        )
        app = attach_sessions(
            make_app(tmp_path, provider, tools=(), approval_timeout_s=WAIT_S)
        )
        await exchange(app, "first question", "r0", surface="stub")
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()
        await runtime.submit(inbound("second question", "r1", surface="stub"))
        await asyncio.wait_for(provider.entered.wait(), WAIT_S)
        await channel.answer_slash_command(TARGET, "c1", OWNER, "/clear")

        await asyncio.wait_for(app.sessions.shutdown(0.05), WAIT_S)
        await runtime.close(WAIT_S)
        kept = app.sessions.session("stub:c1").history
        said = [text for text in transport.sent if text != "Cancelled."]
        return said, [m.content for m in kept]

    said, kept = run(scenario())

    assert said == ["That command failed."]
    assert kept == ["first question", "FIRST ANSWER."]


def test_a_command_that_arrives_while_the_runtime_closes_starts_no_task(
    tmp_path, caplog
):
    """Nothing would reap the task, so there is none and no reply.

    The clear was queued before the runtime was asked, and it still runs.
    """

    async def scenario():
        app = deployment(tmp_path, Scripted(Message.assistant("FIRST ANSWER.")))[0]
        await exchange(app, "first question", "r0", surface="stub")
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()
        await runtime.close(0.0)
        handled = await channel.answer_slash_command(TARGET, "c1", OWNER, "/clear")
        tasks = command_tasks()
        await app.aclose()
        return handled, tasks, transport.sent, app.sessions.session("stub:c1").history

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with caplog.at_level(logging.WARNING, logger="omicsclaw.entry.channel.base"):
            handled, tasks, said, kept = run(scenario())
        gc.collect()

    assert handled is True
    assert tasks == []
    assert said == []
    assert kept == ()
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "omicsclaw.entry.channel.base"
    ] == ["stub command /clear gets no reply: the runtime is not accepting"]
    assert not [w for w in caught if "never awaited" in str(w.message)]


def test_a_queued_command_with_nothing_to_say_gets_the_same_words_as_any_other(
    tmp_path, monkeypatch
):
    """An empty reply is replaced as it is for a command answered at once.

    The handler is a stand-in registered for this test alone: no built-in
    command ends its queued work with nothing to say.
    """

    async def quiet(_ctx):
        async def reply() -> str:
            return ""

        return reply

    monkeypatch.setitem(_registry._REGISTRY, "/quiet", quiet)

    async def scenario():
        app = deployment(tmp_path)[0]
        transport = Transport()
        runtime, channel = owned_channel(app, transport)
        await runtime.start()
        await channel.answer_slash_command(TARGET, "c1", OWNER, "/quiet")
        await runtime.close(WAIT_S)
        return transport.sent

    assert run(scenario()) == ["Nothing to show."]


def test_a_channel_with_no_runtime_leaves_a_command_that_would_wait_unanswered(
    monkeypatch, caplog
):
    """There is nothing to hold the task, so the reply is never waited for.

    No built-in command queues work without a runtime, so the handler
    here is a stand-in registered for this test alone.
    """
    waited: list[str] = []

    async def later(_ctx):
        async def reply() -> str:
            waited.append("waited")
            return "done"

        return reply

    monkeypatch.setitem(_registry._REGISTRY, "/later", later)

    async def scenario():
        channel = _StubChannel(BaseChannelConfig(allowed_senders={OWNER}))
        handled = await channel.answer_slash_command(TARGET, "c1", OWNER, "/later")
        await loop_passes()
        return handled, command_tasks()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with caplog.at_level(logging.WARNING, logger="omicsclaw.entry.channel.base"):
            handled, tasks = run(scenario())
        gc.collect()

    assert handled is True
    assert tasks == []
    assert waited == []
    assert [record.getMessage() for record in caplog.records] == [
        "stub command /later gets no reply: the runtime is not accepting"
    ]
    assert not [w for w in caught if "never awaited" in str(w.message)]


def test_files_and_outputs_answer_rather_than_raise_on_an_empty_workspace(tmp_path):
    """Both were wrapped in bare ``except`` clauses; both now report.

    A missing directory is the ordinary state of a fresh deployment, not an
    error worth a traceback in a chat window.
    """

    async def scenario():
        app = deployment(tmp_path)[0]
        return (
            await dispatch(context(app, text="/files")),
            await dispatch(context(app, text="/outputs")),
            await dispatch(context(app, text="/recent")),
        )

    files, outputs, recent = run(scenario())

    assert "data" in files
    assert "output" in outputs
    assert recent == "No recent analyses found."


def test_files_lists_what_is_in_the_data_directory(tmp_path):
    async def scenario():
        data = tmp_path / "data"
        data.mkdir()
        (data / "visium.h5ad").write_bytes(b"x" * 2048)
        app = deployment(tmp_path)[0]
        return await dispatch(context(app, text="/files"))

    reply = run(scenario())

    assert "visium.h5ad" in reply
    assert "MB" in reply


def _module(root, name, *, status=None, report=None, modified=None):
    import json
    import os

    results = root / "results" / name
    (results / "provenance").mkdir(parents=True)
    if status is not None:
        (results / "provenance" / "manifest.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    if report is not None:
        nn, slug = name.split("_", 1)
        (results / f"M{nn}_{slug}_REPORT.md").write_text(report, encoding="utf-8")
    if modified is not None:
        target = results / "provenance" / "manifest.json" if status is not None else results
        os.utime(target, (modified, modified))


def test_recent_lists_the_newest_modules_with_status_and_report_headline(tmp_path):
    _module(tmp_path, "01_qc", status="accepted", report="# QC of PBMC3k\nbody\n", modified=1_000)
    _module(tmp_path, "02_preprocess", status="replayed", report="intro\n# Preprocessing\n", modified=2_000)
    _module(tmp_path, "03_clustering", status="draft", modified=3_000)
    _module(tmp_path, "04_annotation", modified=4_000)
    (tmp_path / "results" / "_archive").mkdir()

    async def scenario():
        app = deployment(tmp_path)[0]
        return await dispatch(context(app, text="/recent"))

    reply = run(scenario())

    assert reply.startswith("Last 3 analyses:")
    assert reply.index("04_annotation") < reply.index("03_clustering") < reply.index("02_preprocess")
    assert "01_qc" not in reply and "_archive" not in reply
    assert "02_preprocess [REPLAYED]" in reply and "Preprocessing" in reply
    assert "03_clustering [DRAFT]" in reply and "No report yet" in reply


def test_recent_ignores_old_output_runs(tmp_path):
    async def scenario():
        run_dir = tmp_path / "output" / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "report.md").write_text("# Spatial domains identified\n", encoding="utf-8")
        app = deployment(tmp_path)[0]
        return await dispatch(context(app, text="/recent"))

    assert run(scenario()) == "No recent analyses found."


def test_version_counts_the_deployment_rather_than_asserting_a_number(tmp_path):
    async def scenario():
        app = deployment(tmp_path)[0]
        return app, await dispatch(context(app, text="/version"))

    app, reply = run(scenario())

    assert f"Tools: {len(app.tools_snapshot)}" in reply
    assert f"Skills: {len(app.skills)}" in reply


# ---- the context every adapter assembles the same way ------------------


def test_the_shared_context_carries_the_session_the_two_stateful_commands_act_on(
    tmp_path,
):
    """``/clear`` and ``/compact`` act on a conversation, or on nothing.

    ``SlashCommandContext.session_id`` defaults to the empty string, which
    those two handlers read as "there is no conversation" and report as
    success. An adapter that assembled its own context and forgot the field
    would therefore answer ``/clear`` cheerfully, with the history intact —
    which is why the assembly is shared rather than copied per platform.
    """

    async def scenario():
        app, _spy = deployment(tmp_path)
        channel = _StubChannel()
        runtime = ChannelRuntime(app, [binding_for(Transport(), adapter="stub")])
        await runtime.start()
        channel.bind_control_runtime(runtime)
        ctx = channel.command_context("c1", OWNER, "/clear")
        await runtime.close(0.0)
        return app, ctx

    app, ctx = run(scenario())

    assert ctx.session_id == "stub:c1"
    assert ctx.platform == "stub"
    assert ctx.chat_id == "c1"
    assert ctx.user_id == OWNER
    assert ctx.app is app
    assert ctx.workspace == str(app.config.workspace)


def test_an_unbound_channel_still_assembles_a_context():
    """So the static commands answer before a runtime exists.

    A handler that needs the deployment says so in its reply; raising here
    instead would surface inside a provider callback, where nobody sees it.
    """
    ctx = _StubChannel().command_context("c1", None, "/help")

    assert ctx.app is None
    assert ctx.workspace == ""
    assert ctx.session_id == "stub:c1"


class _StubChannel(Channel):
    """A real ``Channel`` with no platform, for the base-class behaviour."""

    name = "stub"

    async def start(self) -> None:  # pragma: no cover - never started
        self._running = True

    async def stop(self) -> None:  # pragma: no cover - never started
        self._running = False
