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

import pytest

from omicsclaw.context import CompactionState
from omicsclaw.entry.channel.base import Channel
from omicsclaw.entry.channel.commands import (
    SlashCommandContext,
    dispatch,
    registered_commands,
)
from omicsclaw.entry.channel.config import BaseChannelConfig
from omicsclaw.entry.channel.runtime import VALUE_REPLY_TARGET
from omicsclaw.entry.ingress import InboundMessage
from omicsclaw.entry.session import InMemorySessionStore, attach_sessions
from omicsclaw.schema import Message, Role
from tests.entry.test_channel_ingress import (  # type: ignore[import-not-found]
    OWNER,
    Transport,
    binding_for,
    deployment,
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
