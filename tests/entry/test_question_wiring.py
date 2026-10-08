"""``ask_user`` wired into a deployment: the switch, the exchange, the sub-agent.

Every test builds a real :class:`~omicsclaw.entry.assembly.AgentApp` over
a scripted provider, because what is under test sits between the pieces:
whether the tool is mounted, whether an exchange binds the channel it asks
through, what the engine's scheduler does with a question, and what a
delegation carries across.

Design choices recorded here because the code only shows their result:

- One switch, ``AppConfig.ask_user``, decides both the mount and the
  binding. A tool mounted without a channel tells the model nobody can be
  asked, and a channel without the tool is dead weight.
- The switch is off by default in this layer and nothing in
  :mod:`omicsclaw.launch` turns it on yet. With it on and no surface
  answering, a question would wait until the exchange ended, which on a
  CLI with no approval deadline is for ever.
- A sub-agent never gets ``ask_user``. The person watched the parent hand
  a task over and saw nothing of the sub-agent's work, so a question from
  inside it would arrive without the context needed to answer it.

Every await is bounded, because a wiring defect here is a hang.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any

import pytest

from omicsclaw.entry import assembly
from omicsclaw.entry.approval import ApprovalBroker
from omicsclaw.entry.assembly import build_app
from omicsclaw.entry.config import AppConfig, resolve_app_config
from omicsclaw.entry.events import TurnEventType
from omicsclaw.entry.question import (
    NOT_ASKED_REASON,
    QUESTION_ABANDONED_REASON,
    QUESTION_TIMEOUT_REASON,
    QuestionBroker,
    read_reply,
)
from omicsclaw.entry.session import attach_sessions
from omicsclaw.entry.stream import TurnStream
from omicsclaw.entry.subagent import (
    _WITHHELD_FROM_SUB_AGENTS,
    GENERAL_PURPOSE,
    ChildRunner,
)
from omicsclaw.entry.turn import TurnHandle, TurnRunner, run_turn
from omicsclaw.permission import PermissionMode
from omicsclaw.schema import Message, Role, ToolCall, ToolDefinition
from omicsclaw.subagent import SUBAGENT_VALUE_KEY, TASK_TOOL_NAME
from omicsclaw.tools import (
    AnswerStatus,
    ApprovalDecision,
    ApprovalMode,
    ApprovalRequest,
    AskUserTool,
    QuestionAnswer,
    QuestionRequest,
    ToolContext,
    ToolPolicy,
    ask_question,
    current_context,
    use_tool_context,
)
from tests.entry.test_turn_runner import (  # type: ignore[import-not-found]
    Scripted,
    make_app,
)

WAIT_S = 5.0

ASK_USER = "ask_user"


def _bounded(main):
    return asyncio.run(asyncio.wait_for(main, WAIT_S))


def _says(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def _calls(*calls: tuple[str, dict[str, Any]]) -> Message:
    """One assistant message with these tool calls, in this order."""
    return Message(
        role=Role.ASSISTANT,
        tool_calls=tuple(
            ToolCall(id=f"c{index}", name=name, arguments=json.dumps(arguments))
            for index, (name, arguments) in enumerate(calls)
        ),
    )


def _asks(question: str, **fields: Any) -> tuple[str, dict[str, Any]]:
    return (ASK_USER, {"question": question, **fields})


def _observations(messages, name: str = ASK_USER) -> list[Message]:
    return [m for m in messages if m.role is Role.TOOL and m.name == name]


class Probe:
    """A tool that records the tool context it ran in."""

    policy = ToolPolicy(approval_mode=ApprovalMode.AUTO, concurrency_safe=True)
    name = "probe"

    def __init__(self) -> None:
        self.seen: list[ToolContext] = []

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="probe",
            description="Records the context it ran in.",
            input_schema={"type": "object", "properties": {}},
        )

    async def execute(self, arguments: str) -> str:
        self.seen.append(current_context())
        return "probed"


async def _answer_every_question(handle: TurnHandle, reply: str) -> list[str]:
    """Consume the exchange, answering each question with *reply* typed."""
    answered: list[str] = []
    async with handle.observe() as observation:
        async for frame in observation:
            if frame.type is TurnEventType.QUESTION_ASKED:
                answered.append(frame.request_id)
                await handle.answer(
                    frame.request_id, read_reply(frame.question, reply)
                )
    return answered


# ---- the switch -----------------------------------------------------------------


def test_the_switch_is_off_until_a_surface_can_answer_and_both_spellings_set_it(
    tmp_path,
):
    assert AppConfig(workspace=tmp_path).ask_user is False
    assert resolve_app_config(argv=[], env={}).ask_user is False
    assert resolve_app_config(argv=["--ask-user", "true"], env={}).ask_user is True
    assert resolve_app_config(argv=[], env={"OMICSCLAW_ASK_USER": "on"}).ask_user
    assert not resolve_app_config(
        argv=["--ask-user", "false"], env={"OMICSCLAW_ASK_USER": "true"}
    ).ask_user


def test_ask_user_is_mounted_after_the_memory_tools_only_when_the_switch_is_on(
    tmp_path,
):
    """Behind the memory pair and ahead of MCP and ``task``, which are
    appended after the foundation tools."""
    off = make_app(tmp_path, Scripted())
    on = make_app(tmp_path, Scripted(), ask_user=True)
    try:
        without = [definition.name for definition in off.tools_snapshot]
        with_it = [definition.name for definition in on.tools_snapshot]
    finally:
        _bounded(off.aclose())
        _bounded(on.aclose())

    assert ASK_USER not in without
    assert with_it == [
        *without[: without.index("memory_write") + 1],
        ASK_USER,
        *without[without.index("memory_write") + 1 :],
    ]
    assert with_it[-1] == TASK_TOOL_NAME


def test_a_deployment_that_brings_its_own_tools_mounts_what_it_brought(tmp_path):
    app = make_app(tmp_path, Scripted(), tools=[Probe()], ask_user=True)

    assert app.registry.names() == ("probe", TASK_TOOL_NAME)


# ---- one exchange, one question ----------------------------------------------------


def test_a_question_asked_mid_exchange_is_answered_through_the_handle(tmp_path):
    """The whole path: the model calls ``ask_user``, a ``QUESTION_ASKED``
    frame reaches an observer, the observer answers through the handle, the
    tool returns the answer and the exchange goes on to converge."""
    provider = Scripted(
        _calls(_asks("Which build?", options=[{"label": "hg38"}, {"label": "hg19"}])),
        _says("aligning to hg38"),
    )

    async def drive():
        app = attach_sessions(make_app(tmp_path, provider, ask_user=True))
        handle = await app.sessions.submit("s1", "align the reads")
        answered = await _answer_every_question(handle, "1")
        outcome = await handle.wait()
        frames = handle.stream.retained()
        await app.aclose()
        return handle, answered, outcome, frames

    handle, answered, outcome, frames = _bounded(drive())

    assert handle.terminal == "converged"
    assert answered == [f"{handle.turn_id}#1"]
    (observation,) = _observations(outcome.history)
    assert observation.is_error is False
    assert json.loads(observation.content) == {
        "status": "answered",
        "question": "Which build?",
        "selected": ["hg38"],
        "reply": "1",
    }
    assert outcome.reply == "aligning to hg38"
    kinds = [frame.type for frame in frames]
    assert kinds.index(TurnEventType.QUESTION_ASKED) < kinds.index(
        TurnEventType.QUESTION_SETTLED
    )
    assert kinds[-1] is TurnEventType.EXCHANGE_END


def test_the_question_reaches_the_exchange_s_own_broker_only_with_the_switch_on(
    tmp_path,
):
    """Mutation: bind ``self._questions`` whatever the configuration says
    and the second deployment's probe sees a channel."""

    async def drive(**overrides):
        probe = Probe()
        provider = Scripted(_calls(("probe", {})), _says("done"))
        app = attach_sessions(
            make_app(tmp_path, provider, tools=[probe], **overrides)
        )
        handle = await app.sessions.submit("s1", "go")
        await handle.wait()
        await app.aclose()
        return probe.seen[0].question, handle

    bound, handle = _bounded(drive(ask_user=True))
    unbound, _handle = _bounded(drive())

    assert bound is handle.questions
    assert unbound is None


def test_run_turn_carries_an_outer_question_channel_in_to_the_tools(tmp_path):
    """``run_turn`` rebinds the tool context to add the session id, and a
    rebinding that names no question channel has none.

    Mutation: drop ``question=outer.question`` from ``_session_bound`` and
    the probe sees ``None``.
    """
    probe = Probe()
    app = make_app(
        tmp_path, Scripted(_calls(("probe", {})), _says("done")), tools=[probe]
    )

    async def channel(request: QuestionRequest) -> QuestionAnswer:
        return QuestionAnswer(AnswerStatus.DECLINED)

    async def drive() -> None:
        with use_tool_context(question=channel):
            await run_turn(app, (), "go", session_id="s1")

    _bounded(drive())

    assert probe.seen[0].question is channel
    assert probe.seen[0].values["session_id"] == "s1"


def test_a_runner_given_no_brokers_runs_and_tells_the_model_nobody_can_be_asked(
    tmp_path,
):
    """A caller that builds a :class:`TurnRunner` by hand may pass neither
    broker. The exchange still runs, and the tool is an error the model
    can act on rather than a hang."""
    provider = Scripted(_calls(_asks("Which build?")), _says("assuming hg38"))
    app = make_app(tmp_path, provider, tools=[AskUserTool()], ask_user=True)

    async def drive():
        runner = TurnRunner(
            app, TurnStream("s1", "t1"), session_id="s1", turn_id="t1", user_text="go"
        )
        return await runner.run()

    outcome = _bounded(drive())

    (observation,) = _observations(outcome.result.messages)
    assert observation.is_error is True
    assert "do not call ask_user again" in observation.content
    assert outcome.reply == "assuming hg38"


# ---- the scheduler: one question at a time -------------------------------------------


def test_a_question_waits_for_the_calls_before_it_and_holds_back_the_ones_after(
    tmp_path,
):
    """``read_file``, ``ask_user``, ``ask_user`` in one model message. The
    read has finished before the first question is shown, and the second
    question does not exist until the first is answered: the person never
    faces two cards at once."""
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    provider = Scripted(
        _calls(
            ("read_file", {"path": "notes.txt"}),
            _asks("first?"),
            _asks("second?"),
        ),
        _says("done"),
    )

    async def drive():
        app = attach_sessions(make_app(tmp_path, provider, ask_user=True))
        handle = await app.sessions.submit("s1", "go")
        seen: list[tuple[str, list[str], bool]] = []
        async with handle.observe() as observation:
            async for frame in observation:
                if frame.type is not TurnEventType.QUESTION_ASKED:
                    continue
                await asyncio.sleep(0.05)
                retained = handle.stream.retained()
                seen.append(
                    (
                        frame.question.question,
                        list(handle.questions.pending()),
                        any(
                            f.type is TurnEventType.TOOL_RESULT
                            and f.engine.tool_result.name == "read_file"
                            and f.seq < frame.seq
                            for f in retained
                        ),
                    )
                )
                await handle.answer(
                    frame.request_id, QuestionAnswer(AnswerStatus.DECLINED)
                )
        await handle.wait()
        await app.aclose()
        return handle, seen

    handle, seen = _bounded(drive())

    assert handle.terminal == "converged"
    assert seen == [
        ("first?", [f"{handle.turn_id}#1"], True),
        ("second?", [f"{handle.turn_id}#2"], True),
    ]


# ---- permission modes ------------------------------------------------------------


def _one_question(tmp_path, **overrides):
    """An exchange that asks one question, answered "hg38" if it is put."""
    provider = Scripted(_calls(_asks("Which build?")), _says("done"))

    async def drive():
        app = attach_sessions(make_app(tmp_path, provider, ask_user=True, **overrides))
        handle = await app.sessions.submit("s1", "go")
        answered = await _answer_every_question(handle, "hg38")
        outcome = await handle.wait()
        approvals = [
            frame
            for frame in handle.stream.retained()
            if frame.type is TurnEventType.APPROVAL_REQUIRED
        ]
        await app.aclose()
        return answered, _observations(outcome.history)[0], approvals

    return _bounded(drive())


def test_a_read_only_deployment_can_still_ask(tmp_path):
    """Asking changes nothing, and the tool says so in its policy."""
    answered, observation, approvals = _one_question(
        tmp_path, permission_mode=PermissionMode.READ_ONLY
    )

    assert len(answered) == 1 and approvals == []
    assert json.loads(observation.content)["reply"] == "hg38"


@pytest.mark.parametrize(
    "mode", [PermissionMode.DEFAULT, PermissionMode.AUTO_APPROVE]
)
def test_no_permission_mode_answers_for_the_person_or_asks_to_ask(tmp_path, mode):
    """``auto-approve`` stops approval cards. A question is not one: it
    still reaches the person, and no approval card precedes it."""
    answered, observation, approvals = _one_question(tmp_path, permission_mode=mode)

    assert len(answered) == 1 and approvals == []
    assert json.loads(observation.content)["status"] == "answered"


def test_a_deny_rule_refuses_the_question_before_it_is_put(tmp_path):
    rules = tmp_path / ".omicsclaw" / "settings.json"
    rules.parent.mkdir()
    rules.write_text(json.dumps({"permissions": {"deny": [ASK_USER]}}))

    answered, observation, _approvals = _one_question(tmp_path)

    assert answered == []
    assert observation.is_error is True
    assert "refused" in observation.content


# ---- the deadline, a cancellation, an exchange that ends ---------------------------


def test_a_question_past_its_deadline_is_no_answer_and_the_next_is_not_asked(tmp_path):
    """The deadline is ``approval_timeout_s``. The model reads why there is
    no answer, and a second question in the same exchange comes straight
    back instead of spending the deadline again."""
    provider = Scripted(
        _calls(_asks("first?")), _calls(_asks("second?")), _says("went on without")
    )

    async def drive():
        app = attach_sessions(
            make_app(tmp_path, provider, ask_user=True, approval_timeout_s=0.05)
        )
        handle = await app.sessions.submit("s1", "go")
        outcome = await handle.wait()
        frames = handle.stream.retained()
        await app.aclose()
        return handle, outcome, frames

    handle, outcome, frames = _bounded(drive())

    assert handle.terminal == "converged"
    first, second = (json.loads(m.content) for m in _observations(outcome.history))
    assert (first["status"], first["reason"]) == ("no_answer", QUESTION_TIMEOUT_REASON)
    assert (second["status"], second["reason"]) == ("no_answer", NOT_ASKED_REASON)
    assert second["question"] == "second?"
    asked = [f for f in frames if f.type is TurnEventType.QUESTION_ASKED]
    assert [frame.question.question for frame in asked] == ["first?"]


def test_cancelling_the_exchange_drops_the_question_and_keeps_the_conversation(
    tmp_path,
):
    provider = Scripted(_calls(_asks("Which build?")), _says("never reached"))

    async def drive():
        app = attach_sessions(make_app(tmp_path, provider, ask_user=True))
        handle = await app.sessions.submit("s1", "go")
        async with handle.observe() as observation:
            async for frame in observation:
                if frame.type is TurnEventType.QUESTION_ASKED:
                    request_id = frame.request_id
                    handle.cancel()
        await handle.wait()
        await handle.answer(request_id, QuestionAnswer(AnswerStatus.ANSWERED, "late"))
        session = app.sessions.session("s1")
        frames = handle.stream.retained()
        await app.aclose()
        return handle, session, frames

    handle, session, frames = _bounded(drive())

    assert handle.terminal == "cancelled"
    assert session is None or tuple(session.history) == ()
    assert handle.questions.pending() == ()
    assert not [f for f in frames if f.type is TurnEventType.QUESTION_SETTLED]
    assert frames[-1].terminal == "cancelled"


class _LeavesAQuestionBehind:
    """A tool that asks from a Task of its own and returns without waiting."""

    policy = ToolPolicy(approval_mode=ApprovalMode.AUTO, concurrency_safe=True)
    name = "detach"

    def __init__(self) -> None:
        self.asking: asyncio.Task[QuestionAnswer] | None = None

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="detach",
            description="Asks and does not wait.",
            input_schema={"type": "object", "properties": {}},
        )

    async def execute(self, arguments: str) -> str:
        self.asking = asyncio.create_task(
            ask_question(QuestionRequest(question="still there?"))
        )
        await asyncio.sleep(0)
        return "left it asking"


def test_a_question_still_outstanding_when_the_exchange_ends_is_settled(tmp_path):
    """Whatever is left waiting when the exchange is over gets its answer
    from the runner's ``finally``, and the settlement is on the stream
    ahead of the terminal frame. With no deadline it would wait for ever.

    Mutation: drop the question broker's ``abandon()`` from
    ``TurnRunner.run`` and the wait below times out.
    """
    tool = _LeavesAQuestionBehind()
    provider = Scripted(_calls(("detach", {})), _says("done"))
    app = make_app(tmp_path, provider, tools=[tool], ask_user=True)

    async def drive():
        stream = TurnStream("s1", "t1")
        runner = TurnRunner(
            app,
            stream,
            session_id="s1",
            turn_id="t1",
            user_text="go",
            questions=QuestionBroker(stream),
        )
        await runner.run()
        assert tool.asking is not None
        answer = await asyncio.wait_for(tool.asking, 0.5)
        return answer, [frame.type for frame in stream.retained()]

    answer, kinds = _bounded(drive())

    assert answer == QuestionAnswer(
        AnswerStatus.NO_ANSWER, reason=QUESTION_ABANDONED_REASON
    )
    assert kinds[-2:] == [TurnEventType.QUESTION_SETTLED, TurnEventType.EXCHANGE_END]


# ---- approvals and questions of one exchange -------------------------------------


def test_a_handle_numbers_its_approvals_and_questions_from_one_counter():
    """Mutation: build ``TurnHandle.questions`` without the handle's
    counter and the question below is a second ``t1#1``."""
    handle = TurnHandle(session_id="s1", turn_id="t1")

    async def drive():
        tasks = [
            asyncio.create_task(handle.approvals(ApprovalRequest(tool_name="bash"))),
            asyncio.create_task(handle.questions(QuestionRequest(question="q"))),
            asyncio.create_task(handle.approvals(ApprovalRequest(tool_name="bash"))),
        ]
        await asyncio.sleep(0)
        pending = (handle.approvals.pending(), handle.questions.pending())
        await handle.approve("t1#1", ApprovalDecision(approved=True))
        await handle.answer("t1#2", QuestionAnswer(AnswerStatus.DECLINED))
        await handle.answer("t1#3", QuestionAnswer(AnswerStatus.DECLINED))
        still = (handle.approvals.pending(), handle.questions.pending())
        handle.approvals.abandon()
        await asyncio.gather(*tasks)
        return pending, still

    pending, still = _bounded(drive())

    assert pending == (("t1#1", "t1#3"), ("t1#2",))
    assert "t1#3" in still[0], "an answer does not settle an approval"


# ---- sub-agents ----------------------------------------------------------------------


def _task_call(prompt: str = "do the thing") -> ToolCall:
    return ToolCall(
        id="d1",
        name=TASK_TOOL_NAME,
        arguments=json.dumps({"subagent_type": "general-purpose", "prompt": prompt}),
    )


class _SeenTools(Scripted):
    """:class:`Scripted`, also recording the tool table each call was shown."""

    def __init__(self, *replies: Message) -> None:
        super().__init__(*replies)
        self.seen_tools: list[tuple[str, ...]] = []

    async def generate(self, messages, tools=None):
        self.seen_tools.append(tuple(tool.name for tool in tools or ()))
        return await super().generate(messages, tools)


def test_a_sub_agent_is_not_given_ask_user_though_the_parent_has_it(tmp_path):
    """Mutation: remove ``ask_user`` from ``_WITHHELD_FROM_SUB_AGENTS`` and
    the child's table holds it."""
    provider = _SeenTools(_says("the sub-agent's conclusion"))
    app = make_app(tmp_path, provider, ask_user=True)

    try:
        result = _bounded(app.registry.execute(_task_call()))
    finally:
        _bounded(app.aclose())

    assert not result.is_error, result.output
    assert ASK_USER in app.registry.names()
    assert ASK_USER not in provider.seen_tools[0]
    assert "read_file" in provider.seen_tools[0]


def test_the_reason_ask_user_is_withheld_reads_after_the_tool_s_name():
    """The reason is quoted in the ``task`` tool's description, which the
    model reads, and in the warning below. It is a phrase whose subject is
    the tool, and it names no tool and no policy machinery itself."""
    reason = _WITHHELD_FROM_SUB_AGENTS[ASK_USER]

    assert reason == "asks a person, and a sub-agent has nobody to ask"
    assert f"{ASK_USER}, which {reason}" in GENERAL_PURPOSE.description


def test_an_agent_file_that_lists_ask_user_is_warned_and_runs_without_it(
    tmp_path, caplog
):
    agents = tmp_path / ".omicsclaw" / "agents"
    agents.mkdir(parents=True)
    (agents / "asker.md").write_text(
        "---\nname: asker\ndescription: asks\ntools: read_file, ask_user\n---\n\n"
        "You ask things.\n",
        encoding="utf-8",
    )
    provider = _SeenTools(_says("answered"))

    with caplog.at_level(logging.WARNING, logger="omicsclaw.entry"):
        app = make_app(tmp_path, provider, ask_user=True)
    try:
        call = ToolCall(
            id="d1",
            name=TASK_TOOL_NAME,
            arguments=json.dumps({"subagent_type": "asker", "prompt": "go"}),
        )
        result = _bounded(app.registry.execute(call))
    finally:
        _bounded(app.aclose())

    warned = [r.getMessage() for r in caplog.records if "asker" in r.getMessage()]
    assert len(warned) == 1
    assert f"{ASK_USER}, which {_WITHHELD_FROM_SUB_AGENTS[ASK_USER]}" in warned[0]
    assert not result.is_error, result.output
    assert provider.seen_tools[0] == ("read_file",)


def test_inside_a_delegation_there_is_nobody_to_ask_and_approval_is_the_parent_s(
    tmp_path,
):
    """A sub-agent's ``bash`` still asks the person who started the parent
    turn, through the parent's broker. A question has no such path: the
    tool context a delegation runs in has no question channel.

    On this path the ``task`` tool has already rebound the context without
    one before the delegate is reached, so this test holds whatever
    :meth:`ChildRunner.delegate` does; the next test is the one that pins
    the delegate's own rebinding.
    """
    probe = Probe()
    provider = Scripted(_calls(("probe", {})), _says("the sub-agent is done"))
    app = make_app(tmp_path, provider, tools=[probe], ask_user=True)
    approvals = ApprovalBroker(TurnStream("s1", "t1"))
    questions = QuestionBroker(TurnStream("s1", "t1"))

    async def drive():
        with use_tool_context(
            approval=approvals, question=questions, values={"session_id": "s1"}
        ):
            return await app.registry.execute(_task_call())

    result = _bounded(drive())

    assert not result.is_error, result.output
    (inside,) = probe.seen
    assert inside.question is None
    assert inside.approval is approvals
    assert inside.values[SUBAGENT_VALUE_KEY] == "general-purpose"
    assert inside.values["session_id"] == "s1"


def test_a_delegation_that_does_not_come_through_the_task_tool_is_bound_the_same(
    tmp_path,
):
    """``ChildRunner.delegate`` does the rebinding itself, so a caller that
    reaches it directly still names the sub-agent on approval cards and
    still has no question channel inside.

    Mutations: carry ``outer.question`` inwards in ``delegate`` and the
    probe sees the caller's channel; leave ``SUBAGENT_VALUE_KEY`` out and
    the name is missing.
    """
    probe = Probe()
    provider = Scripted(_calls(("probe", {})), _says("done"))
    app = make_app(tmp_path, provider, tools=[probe], ask_user=True)
    runner = ChildRunner(
        provider=app.provider, parent=app.registry, config=app.config
    )
    questions = QuestionBroker(TurnStream("s1", "t1"))

    async def drive():
        with use_tool_context(question=questions, values={"session_id": "s1"}):
            conclusion = await runner.delegate(GENERAL_PURPOSE, "do the thing")
            return conclusion, current_context()

    conclusion, after = _bounded(drive())

    assert conclusion == "done"
    (inside,) = probe.seen
    assert inside.question is None
    assert inside.values[SUBAGENT_VALUE_KEY] == "general-purpose"
    assert after.question is questions, "the caller's own context is restored"
    assert SUBAGENT_VALUE_KEY not in after.values
