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
from typing import Any

import pytest

from omicsclaw.entry.cli import PROMPT, Repl, ScriptedSource, Screen
from omicsclaw.entry.cli._transcript import ToolTranscript
from omicsclaw.entry.display import CONTINUATION_PREFIX
from omicsclaw.entry.events import TurnEvent
from omicsclaw.entry.render import TextRenderer
from omicsclaw.entry.session import attach_sessions
from omicsclaw.permission import PermissionMode
from omicsclaw.schema import Message, Role, ToolCall
from omicsclaw.tools import AskUserTool, QuestionOption, QuestionRequest
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
    ],
)
def test_a_line_typed_at_the_question_is_the_answer(tmp_path, fields, typed, selected):
    """Whatever is typed goes to the model as typed. A leading ``/`` does
    not make it a command: ``/auto`` leaves the permission mode alone and
    ``/exit`` leaves the REPL running.

    Mutation: dispatch a line that starts with ``/`` as a command in
    ``Repl._question`` and the three slash cases fail.
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
