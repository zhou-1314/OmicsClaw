"""The question card in the terminal: what is shown, what a line typed means.

Every test builds a real app over a scripted provider and drives the real
:class:`~omicsclaw.entry.cli.Repl` with a scripted input source, so what
is asserted is what the model reads back and what a person sees.

Design choices recorded here because the code only shows their result:

- The reply is read by :meth:`Repl._read_card`, the reader the approval
  card uses. Ctrl-C at the prompt, the input ending, the reading Task
  being cancelled and the source failing each settle the card once, and a
  second card that reimplemented those four paths could forget one. With
  no deadline on a terminal, a forgotten path is an exchange that never
  ends.
- There are no slash commands at a question's prompt. ``/data/ref.h5ad``
  is an answer a person really gives, and a prompt that ran ``/auto``
  instead of passing it on would be an approval control on something that
  is not an approval.
- An empty line skips. A person who presses Enter to get past a question
  they do not want to answer has said so, and the model is told not to
  ask again.
- Ctrl-C cancels the exchange and not the REPL, as it does everywhere
  else in the terminal.

Every await is bounded, because a defect in this loop is a hang.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import logging
import pathlib
import types
from typing import Any

import pytest

from omicsclaw.entry.cli import PROMPT, Repl, ScriptedSource, Screen
from omicsclaw.entry.cli import _activity
from omicsclaw.entry.cli._transcript import ToolTranscript
from omicsclaw.entry.display import CONTINUATION_PREFIX
from omicsclaw.entry.events import TurnEvent
from omicsclaw.entry.question import QUESTION_TIMEOUT_REASON
from omicsclaw.entry.render import TextRenderer
from omicsclaw.entry.session import attach_sessions
from omicsclaw.permission import PermissionMode
from omicsclaw.schema import Message, Role, ToolCall, ToolDefinition
from omicsclaw.tools import (
    ApprovalMode,
    AskUserTool,
    QuestionOption,
    QuestionRequest,
    ToolPolicy,
)
from tests.entry.test_cli_activity import (  # type: ignore[import-not-found]
    Slow,
    frames,
    terminal_screen,
)
from tests.entry.test_turn_runner import (  # type: ignore[import-not-found]
    Asking,
    Scripted,
    make_app,
)

WAIT_S = 10.0

ASK = "answer [#1]> "

LEGEND = "empty line skips · Ctrl-C cancels the request"

OPTIONS = [
    {"label": "Leiden", "description": "recommended"},
    {"label": "Louvain"},
    {"label": "both"},
]


def _asks(question: str, call_id: str = "q1", **fields: Any) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="ask_user",
        arguments=json.dumps({"question": question, **fields}),
    )


def _model(*calls: ToolCall) -> Message:
    return Message(role=Role.ASSISTANT, tool_calls=tuple(calls))


def _says(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def _build(tmp_path: pathlib.Path, provider, *, tools=(), **overrides):
    return attach_sessions(
        make_app(tmp_path, provider, tools=(AskUserTool(), *tools), **overrides)
    )


def _read_back(provider: Scripted) -> dict[str, Any]:
    """The ``ask_user`` result the model was sent on its next call."""
    results = [
        message
        for message in provider.seen[-1]
        if message.role is Role.TOOL and message.name == "ask_user"
    ]
    assert results, "the model never received an ask_user result"
    assert not results[-1].is_error, results[-1].content
    return json.loads(results[-1].content)


def _converse(tmp_path, provider, lines, *, source=None, repl_class=Repl, **overrides):
    """Run the REPL over *lines* and return it, its source and what it printed."""

    async def drive():
        app = _build(tmp_path, provider, **overrides)
        buffer = io.StringIO()
        reading = source if source is not None else ScriptedSource(lines)
        repl = repl_class(app, source=reading, screen=Screen.into(buffer))
        await asyncio.wait_for(repl.run(), WAIT_S)
        session = app.sessions.session(repl.state.session_id)
        history = tuple(session.history) if session is not None else ()
        mode = app.permission.mode
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return repl, reading, buffer.getvalue(), history, mode

    return asyncio.run(drive())


# ---- answering ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "typed", "selected"),
    [
        pytest.param({"options": OPTIONS}, "2", ["Louvain"], id="a number"),
        pytest.param(
            {"options": OPTIONS, "multi_select": True},
            "1, 3",
            ["Leiden", "both"],
            id="two numbers of several",
        ),
        pytest.param(
            {"options": OPTIONS},
            "whichever the paper used",
            [],
            id="a sentence",
        ),
        pytest.param({}, "/data/ref.h5ad", [], id="a path that starts with a slash"),
        pytest.param({"options": OPTIONS}, "y", [], id="an approval word"),
        pytest.param({"options": OPTIONS}, "/auto", [], id="a command's name"),
        pytest.param({}, "/exit", [], id="the command that ends the REPL"),
        pytest.param(
            {"options": OPTIONS}, "  2 ", ["Louvain"], id="a number with spaces around it"
        ),
    ],
)
def test_a_line_typed_at_the_question_is_the_answer(tmp_path, fields, typed, selected):
    """Whatever is typed goes to the model as typed, spaces included. A
    leading ``/`` does not make it a command: ``/auto`` leaves the
    permission mode alone and ``/exit`` leaves the REPL running.

    Mutations: dispatch a line that starts with ``/`` as a command in
    ``Repl._question`` and the three slash cases fail; hand ``read_reply``
    the stripped line and the reply of the last case loses its spaces.
    """
    provider = Scripted(
        _model(_asks("Which clustering?", **fields)), _says("understood")
    )

    repl, source, printed, _history, mode = _converse(
        tmp_path, provider, ["cluster it", typed, "/exit"]
    )

    assert _read_back(provider) == {
        "status": "answered",
        "question": "Which clustering?",
        "selected": selected,
        "reply": typed,
    }
    assert source.prompts == [PROMPT, ASK, PROMPT]
    assert "understood" in printed
    assert "is not available in this build" not in printed
    assert mode is PermissionMode.DEFAULT
    assert not repl._asking


def test_the_card_then_how_to_reply_then_the_legend_are_on_screen(tmp_path):
    """The card and the reply hint are one block. The legend comes from
    the Task that reads the reply, so the ``-> ask_user`` line of the tool
    call can land between them, as ``-> bash`` does on an approval card."""
    provider = Scripted(
        _model(_asks("Which clustering?", options=OPTIONS)), _says("understood")
    )

    _repl, _source, printed, _history, _mode = _converse(
        tmp_path, provider, ["cluster it", "1", "/exit"]
    )

    lines = printed.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Question ["))
    assert lines[start].endswith("#1]: Which clustering?")
    assert lines[start + 1 : start + 5] == [
        f"{CONTINUATION_PREFIX}1. Leiden - recommended",
        f"{CONTINUATION_PREFIX}2. Louvain",
        f"{CONTINUATION_PREFIX}3. both",
        "Reply with an option number, or in your own words.",
    ]
    assert lines.index(f"  {LEGEND}") > start + 4
    assert "Skipped [" not in printed and "No answer [" not in printed


# ---- skipping, and not getting an answer ----------------------------------------


def test_an_empty_line_skips_the_question_and_the_exchange_goes_on(tmp_path):
    """Mutation: read an empty line as ``ANSWERED("")`` and the model is
    handed an empty reply as though the person had chosen it."""
    provider = Scripted(
        _model(_asks("Which clustering?", options=OPTIONS)), _says("my own choice")
    )

    _repl, source, printed, _history, _mode = _converse(
        tmp_path, provider, ["cluster it", "", "/exit"]
    )

    read = _read_back(provider)
    assert read["status"] == "declined"
    assert "Do not ask this again" in read["note"]
    assert "Skipped [" in printed
    assert "my own choice" in printed
    assert source.prompts == [PROMPT, ASK, PROMPT]


def test_input_that_ends_at_the_question_is_no_answer_not_a_skip(tmp_path):
    """Nobody chose to skip: the input ran out. The model is told nobody
    answered, which sends it on with what does not depend on the answer
    and has it restate the question, where a skip would tell it to decide
    for itself.

    Mutation: settle the missing line as ``DECLINED`` and the status below
    is ``declined``.
    """
    provider = Scripted(_model(_asks("Which clustering?")), _says("went on"))

    _repl, source, printed, _history, _mode = _converse(
        tmp_path, provider, ["cluster it"]
    )

    read = _read_back(provider)
    assert (read["status"], read["reason"]) == (
        "no_answer",
        "no operator at the terminal",
    )
    assert "No answer [" in printed and "no operator at the terminal" in printed
    assert "went on" in printed
    assert source.prompts[:2] == [PROMPT, ASK]


class _BrokenAtTheQuestion(ScriptedSource):
    """A terminal that cannot show the question's prompt."""

    async def read(self, prompt: str) -> str:
        if prompt.startswith("answer"):
            self.prompts.append(prompt)
            raise RuntimeError("no tty")
        return await super().read(prompt)


def test_a_question_that_cannot_be_put_is_settled_and_the_person_is_told(tmp_path):
    provider = Scripted(_model(_asks("Which clustering?")), _says("went on"))

    _repl, _source, printed, _history, _mode = _converse(
        tmp_path,
        provider,
        [],
        source=_BrokenAtTheQuestion(["cluster it", "/exit"]),
    )

    read = _read_back(provider)
    assert (read["status"], read["reason"]) == (
        "no_answer",
        "the terminal could not ask: no tty",
    )
    assert "Could not ask about the question: no tty. Not answered." in printed
    assert "went on" in printed


# ---- Ctrl-C at the question ------------------------------------------------------


class _InterruptedAtTheQuestion(ScriptedSource):
    """A person who presses Ctrl-C at the question's prompt.

    ``prompt_toolkit`` reads with the terminal in raw mode, where Ctrl-C is
    a key: ``prompt_async`` raises :exc:`KeyboardInterrupt` in the Task
    that is reading, and the SIGINT handler never runs.
    """

    async def read(self, prompt: str) -> str:
        if prompt.startswith("answer"):
            await asyncio.sleep(0)
            self.prompts.append(prompt)
            raise KeyboardInterrupt
        return await super().read(prompt)


class _Recording(Repl):
    """A REPL that keeps the handle of every exchange it ran."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.handles = []

    async def ask(self, text):
        handle = await super().ask(text)
        self.handles.append(handle)
        return handle


def test_ctrl_c_at_the_question_cancels_the_exchange_and_not_the_repl(tmp_path):
    """The key press is a :exc:`KeyboardInterrupt` raised inside the Task
    reading the reply. Left alone it leaves that Task, leaves
    :func:`asyncio.run`, and ends ``oc cli`` with exit code 130 in the
    middle of a conversation.

    Mutation: read the reply with ``self._source.read`` in place of
    ``self._read_card`` and the interrupt escapes.
    """
    provider = Scripted(_model(_asks("Which clustering?")), _says("second answer"))

    try:
        repl, source, printed, history, _mode = _converse(
            tmp_path,
            provider,
            [],
            source=_InterruptedAtTheQuestion(["cluster it", "and now?", "/exit"]),
            repl_class=_Recording,
        )
    except KeyboardInterrupt:
        pytest.fail("Ctrl-C at the question escaped the REPL")

    interrupted, answered = repl.handles
    assert interrupted.terminal == "cancelled"
    assert answered.terminal == "converged"
    assert "Cancelled." in printed
    assert source.prompts == [PROMPT, ASK, PROMPT, PROMPT]
    assert [message.content for message in history] == ["and now?", "second answer"]
    assert not repl._asking


# ---- numbering, reaping, the transcript ------------------------------------------


def test_an_approval_and_a_question_of_one_exchange_are_numbered_one_and_two(
    tmp_path,
):
    """The two prompts show nothing but ``#n`` to tell their cards apart."""
    provider = Scripted(
        _model(
            ToolCall(id="a1", name="ask_a", arguments="{}"),
            _asks("Which clustering?"),
        ),
        _says("both settled"),
    )

    _repl, source, printed, _history, _mode = _converse(
        tmp_path,
        provider,
        ["do both", "y", "Leiden", "/exit"],
        tools=(Asking("ask_a"),),
    )

    assert source.prompts == [
        PROMPT,
        "approve ask_a [#1]? [y/N/a=always] ",
        "answer [#2]> ",
        PROMPT,
    ]
    assert _read_back(provider)["reply"] == "Leiden"
    assert "<- ask_a ok" in printed and "both settled" in printed


class _NeverAnswers(ScriptedSource):
    """A person who leaves the question's prompt open."""

    async def read(self, prompt: str) -> str:
        if prompt.startswith("answer"):
            self.prompts.append(prompt)
            await asyncio.Event().wait()
        return await super().read(prompt)


def test_a_question_nobody_answered_does_not_outlive_its_exchange(tmp_path, caplog):
    """The Task reading the reply is tracked like an approval's, so an
    exchange cancelled under an open question takes it down. Otherwise the
    next line typed would answer a question from an exchange that is gone.

    Mutation: do not add the Task to ``_asking`` and it is still pending
    when the exchange has ended.
    """

    async def drive():
        provider = Scripted(_model(_asks("Which clustering?")), _says("unused"))
        app = _build(tmp_path, provider)
        buffer = io.StringIO()
        source = _NeverAnswers(["cluster it"])
        repl = Repl(app, source=source, screen=Screen.into(buffer))
        loop = asyncio.create_task(repl.run())
        for _ in range(400):
            await asyncio.sleep(0)
            if ASK in source.prompts:
                break
        asking = tuple(repl._asking)
        assert repl.interrupt() is True
        await asyncio.wait_for(loop, WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return repl, asking, buffer.getvalue()

    with caplog.at_level(logging.ERROR):
        repl, asking, printed = asyncio.run(drive())
        gc.collect()

    assert len(asking) == 1 and asking[0].done()
    assert "Cancelled." in printed
    assert not repl._asking
    assert "never retrieved" not in caplog.text


def test_a_failed_question_task_is_logged_and_not_merely_dropped(
    tmp_path, caplog, monkeypatch
):
    """A Task that could not settle its question still leaves ``_asking``,
    and its exception is read and logged. Left unread, the event loop
    reports it when the Task is collected, into a terminal somebody is
    reading.

    Mutation: do not give the Task ``_forget_asking`` as its done callback
    and it stays in ``_asking`` with nothing logged.
    """

    async def boom(self, handle, request_id, event) -> None:
        raise RuntimeError("could not settle the question")

    async def drive():
        app = _build(tmp_path, Scripted(_says("unused")))
        repl = Repl(
            app, source=ScriptedSource(["/exit"]), screen=Screen.into(io.StringIO())
        )
        repl._ask_question(None, types.SimpleNamespace(request_id="r1"))
        started = tuple(repl._asking)
        await asyncio.wait_for(
            asyncio.gather(*started, return_exceptions=True), WAIT_S
        )
        await asyncio.sleep(0)  # done callbacks run on the next iteration
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return repl, started

    monkeypatch.setattr(Repl, "_question", boom)
    with caplog.at_level(logging.ERROR, logger="omicsclaw.entry.cli._repl"):
        repl, started = asyncio.run(drive())

    assert len(started) == 1
    assert not repl._asking, "the finished task was dropped from the set"
    assert "could not settle the question" in caplog.text


# ---- the live line, and a question whose deadline passes --------------------------


class _AtTheQuestion(ScriptedSource):
    """A person at the question's prompt, and what the screen received
    while that prompt was open.

    With a *reply* they type it after *pause_s*. With none they leave the
    prompt open until it is taken down.
    """

    def __init__(self, lines, buffer, *, reply=None, pause_s=0.25) -> None:
        super().__init__(lines)
        self._buffer = buffer
        self._reply = reply
        self._pause_s = pause_s
        self.during: list[str] = []
        self.answered_at = 0
        self.on_screen_when_taken_down: str | None = None

    async def read(self, prompt: str) -> str:
        if not prompt.startswith("answer"):
            return await super().read(prompt)
        self.prompts.append(prompt)
        mark = len(self._buffer.getvalue())
        try:
            if self._reply is None:
                await asyncio.Event().wait()
            await asyncio.sleep(self._pause_s)
        except asyncio.CancelledError:
            self.on_screen_when_taken_down = self._buffer.getvalue()
            raise
        self.during.append(self._buffer.getvalue()[mark:])
        self.answered_at = len(self._buffer.getvalue())
        return self._reply


def _a_question_then_a_slow_tool(tmp_path, monkeypatch):
    """Ask, have the person take a while to reply, then run a tool that
    takes a while. Returns the source and everything written to a screen
    that claims to be a terminal."""
    monkeypatch.setattr(_activity, "TICK_S", 0.01)

    async def drive():
        provider = Scripted(
            _model(
                _asks("Which clustering?"),
                ToolCall(id="s1", name="slowly", arguments="{}"),
            ),
            _says("settled"),
        )
        app = _build(tmp_path, provider, tools=(Slow(delay_s=0.25),))
        screen, buffer = terminal_screen()
        source = _AtTheQuestion([], buffer, reply="Leiden")
        repl = Repl(app, source=source, screen=screen, animated=True)
        await asyncio.wait_for(repl.ask("cluster it"), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return source, buffer.getvalue()

    source, printed = asyncio.run(drive())
    assert source.during, "the question's prompt was never put"
    assert "settled" in printed
    return source, printed


def test_nothing_is_painted_while_a_question_is_open(tmp_path, monkeypatch):
    """On a real terminal the open prompt owns the cursor, and a spinner
    frame written under it lands in the middle of what the person types.

    Mutation: do not call ``activity.hold()`` in ``Repl._ask_question``
    and frames are painted while the prompt is open.
    """
    source, _printed = _a_question_then_a_slow_tool(tmp_path, monkeypatch)

    for window in source.during:
        assert "\x1b" not in window, f"painted under the prompt: {window!r}"


def test_the_live_line_comes_back_once_the_question_is_answered(
    tmp_path, monkeypatch
):
    """A tool runs after the reply, so the line has work to show. A hold
    that was never given back would leave the rest of the exchange with
    nothing on screen.

    Mutation: drop the ``finally`` that calls ``activity.release()`` from
    ``Repl._answer_question`` and no frame is painted after the reply.
    """
    source, printed = _a_question_then_a_slow_tool(tmp_path, monkeypatch)

    assert frames(printed[source.answered_at :]), (
        "the line never came back after the question was answered"
    )


class _Gated:
    """A tool that works until the test lets it finish."""

    name = "gated"
    policy = ToolPolicy(approval_mode=ApprovalMode.AUTO, concurrency_safe=False)

    def __init__(self) -> None:
        self.finish = asyncio.Event()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Works until told to stop.",
            input_schema={"type": "object", "properties": {}},
        )

    async def execute(self, arguments: str) -> str:
        await self.finish.wait()
        return "gated done"


async def _happens(condition) -> bool:
    """Whether *condition* comes to hold within a bounded wait."""
    for _ in range(600):
        if condition():
            return True
        await asyncio.sleep(0.005)
    return False


def test_a_question_whose_deadline_passes_has_its_prompt_taken_down(
    tmp_path, monkeypatch
):
    """With a deadline set, a question is settled while its prompt is still
    open. The Task reading the reply ends before ``No answer`` is printed,
    so the line is not written onto an open prompt, a line typed later is
    not taken as the reply, and the live line runs again for the tool that
    follows.

    The gated tool keeps the exchange running past the deadline, so what
    ends the reading Task here is the settlement and not the end of the
    exchange.

    Mutation: do not retract the question on ``QUESTION_SETTLED`` in
    ``Repl._pump`` and the Task is still reading when ``No answer`` is on
    screen.
    """
    monkeypatch.setattr(_activity, "TICK_S", 0.01)

    async def drive():
        gated = _Gated()
        provider = Scripted(
            _model(
                _asks("Which clustering?"),
                ToolCall(id="g1", name="gated", arguments="{}"),
            ),
            _says("went on"),
        )
        app = _build(tmp_path, provider, tools=(gated,), approval_timeout_s=0.2)
        screen, buffer = terminal_screen()
        source = _AtTheQuestion(["cluster it", "/exit"], buffer)
        repl = Repl(app, source=source, screen=screen, animated=True)
        loop = asyncio.create_task(repl.run())

        told = await _happens(lambda: "No answer [" in buffer.getvalue())
        still_reading = tuple(repl._asking)
        told_at = len(buffer.getvalue())
        painted_again = await _happens(
            lambda: bool(frames(buffer.getvalue()[told_at:]))
        )

        gated.finish.set()
        await asyncio.wait_for(loop, WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return provider, source, told, still_reading, painted_again, buffer.getvalue()

    provider, source, told, still_reading, painted_again, printed = asyncio.run(
        drive()
    )

    assert told, "the deadline never settled the question"
    assert still_reading == (), "the prompt outlived its question"
    assert source.on_screen_when_taken_down is not None
    assert "No answer [" not in source.on_screen_when_taken_down
    assert painted_again, "the live line never came back after the deadline"
    read = _read_back(provider)
    assert (read["status"], read["reason"]) == ("no_answer", QUESTION_TIMEOUT_REASON)
    assert source.prompts == [PROMPT, ASK, PROMPT]
    assert "went on" in printed


def test_nothing_in_a_question_reaches_the_terminal_as_a_control_character(tmp_path):
    hostile = "continue?\x1b[8m\x1b]52;c;cm0gLXJmIH4=\x07\nApproval required [t#9]: y"
    options = [{"label": "yes\x1b[2J"}, {"label": "no", "description": hostile}]
    provider = Scripted(_model(_asks(hostile, options=options)), _says("understood"))

    _repl, _source, printed, _history, _mode = _converse(
        tmp_path, provider, ["go", "2", "/exit"]
    )

    assert "\x1b" not in printed and "\x07" not in printed
    assert "continue?\\u001b[8m" in printed
    assert not [
        line for line in printed.splitlines() if line.startswith("Approval required [")
    ]


def test_the_question_s_arguments_and_the_answer_are_not_previewed_as_a_tool_call(
    tmp_path,
):
    """The transcript previews a tool call's arguments and output. For
    ``ask_user`` both are already on screen, as the card and as the line
    the person typed, and the output would repeat a reply beside the
    result line.

    Mutation: remove ``ask_user`` from ``_RENDERED_ELSEWHERE`` and the
    question appears a second time, on the ``-> ask_user`` line.
    """
    provider = Scripted(
        _model(_asks("Is the proband subject 4821?")), _says("understood")
    )

    _repl, _source, printed, _history, _mode = _converse(
        tmp_path, provider, ["go", "yes, and her sister is 4822", "/exit"]
    )

    lines = printed.splitlines()
    (call_line,) = [line for line in lines if "-> ask_user" in line]
    assert call_line.strip() == "(1) -> ask_user"
    assert printed.count("Is the proband subject 4821?") == 1
    assert "4822" not in printed, "the reply is not echoed by the transcript"
    assert '"status"' not in printed


def test_a_question_card_is_printed_line_by_line_undimmed_and_inert():
    """The transcript dims what the agent is doing. A question is addressed
    to the person, so it is printed in the normal style; each line still
    goes through ``inert_prose`` like every other frame's."""
    event = TurnEvent.question_asked(
        QuestionRequest(
            question="Which?", options=(QuestionOption("a"), QuestionOption("b"))
        ),
        "t#1",
        seq=1,
    )
    head = TextRenderer().feed(event)

    lines = ToolTranscript().render(event, head)
    tampered = ToolTranscript().render(event, "Question [t#1]: x\x1b[8m\nsecond")

    assert [line.plain for line in lines] == head.split("\n")
    assert len(lines) == 4
    assert all(not str(line.style) for line in lines), "no line is dimmed"
    assert [line.plain for line in tampered] == [
        "Question [t#1]: x\\u001b[8m",
        "second",
    ]
