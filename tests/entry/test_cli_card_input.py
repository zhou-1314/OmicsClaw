"""A card takes only what is typed after its prompt opens.

A terminal keeps what a person types while nothing is reading. Read by
the next prompt to open, a ``y`` typed while a tool was still running
would approve the card that follows it, and so would the reply to a
question whose prompt was taken down at its deadline. The REPL's own
prompt is the other case: a line typed early there is the next message,
and stays one.

The tests drive the real :class:`~omicsclaw.entry.cli.Repl` over a real
app and a scripted provider. :class:`Keyboard` stands for the terminal:
lines typed wait in it until something reads them, which is the one
property of a terminal these tests are about. The sources that do this
against a real terminal are tested in ``test_cli_input.py``.

Every await is bounded, because a defect in this loop is a hang.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from typing import Callable

import pytest

from omicsclaw.entry.cli import PROMPT, Repl, Screen, StreamSource
from omicsclaw.entry.cli._input import FreshSource
from omicsclaw.permission import PermissionMode
from omicsclaw.schema import Message, Role, ToolCall
from omicsclaw.tools import ApprovalMode, ToolPolicy
from tests.entry.test_cli_input import Typist  # type: ignore[import-not-found]
from tests.entry.test_cli_question import (  # type: ignore[import-not-found]
    _asks,
    _build,
    _Gated,
    _model,
    _read_back,
    _says,
)
from tests.entry.test_turn_runner import (  # type: ignore[import-not-found]
    Asking,
    Scripted,
)

WAIT_S = 10.0

NOTICE = "input typed before this prompt was discarded"
HALF_LINE_NOTICE = "what is typed up to the next Enter is discarded too"
NOBODY_ASKED = "nobody was asked"

ANSWER = "answer [#{n}]> "
APPROVE = "approve ask_a [#{n}]? [y/N/a=always] "


class Keyboard:
    """A terminal as the REPL meets it: typed lines wait until they are read.

    :meth:`read` hands over the oldest waiting line, which is what a
    terminal's input queue does. :meth:`read_fresh` forgets the lines that
    were waiting when it was called and reports that it did. After
    :meth:`begin_a_line` it also reports the unfinished line and throws
    away the next line typed, as the ``prompt_toolkit`` source does.
    """

    def __init__(self) -> None:
        self._waiting: list[str] = []
        self._begun = False
        self._typed = asyncio.Event()
        self.prompts: list[str] = []
        self.withdrawn = 0

    def type(self, line: str) -> None:
        self._waiting.append(line)
        self._typed.set()

    def begin_a_line(self) -> None:
        """Type the start of a line and no Enter."""
        self._begun = True

    async def _next(self, prompt: str) -> str:
        self.prompts.append(prompt)
        while not self._waiting:
            self._typed.clear()
            await self._typed.wait()
        return self._waiting.pop(0)

    async def read(self, prompt: str) -> str:
        return await self._next(prompt)

    async def read_fresh(
        self,
        prompt: str,
        *,
        discarded: Callable[[], None] | None = None,
        unfinished: Callable[[], None] | None = None,
    ) -> str:
        begun, self._begun = self._begun, False
        if self._waiting or begun:
            self._waiting.clear()
            if discarded is not None:
                discarded()
        if begun:
            if unfinished is not None:
                unfinished()
            await self._next(prompt)
        return await self._next(prompt)

    def withdraw(self) -> None:
        self.withdrawn += 1

    def close(self) -> None:
        self._waiting.clear()


def test_the_double_is_a_source_the_repl_reads_fresh():
    assert isinstance(Keyboard(), FreshSource)


async def _until(condition: Callable[[], bool], what: str) -> None:
    """Wait, for a bounded time, until *condition* holds."""
    for _ in range(int(WAIT_S / 0.005)):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"never happened: {what}")


async def _a_moment() -> None:
    """Long enough for a line that was wrongly read to settle its card."""
    await asyncio.sleep(0.1)


def _gated() -> ToolCall:
    return ToolCall(id="g1", name="gated", arguments="{}")


def _needs_approval() -> ToolCall:
    return ToolCall(id="a1", name="ask_a", arguments="{}")


class _Session:
    """One REPL over a :class:`Keyboard`, with what the scenarios share."""

    def __init__(
        self, tmp_path, provider, *, tools=(), keyboard=None, **overrides
    ) -> None:
        self.gated = _Gated()
        self.provider = provider
        self.app = _build(
            tmp_path,
            provider,
            tools=(self.gated, Asking("ask_a"), *tools),
            **overrides,
        )
        self.buffer = io.StringIO()
        self.keyboard = keyboard if keyboard is not None else Keyboard()
        self.repl = Repl(
            self.app, source=self.keyboard, screen=Screen.into(self.buffer)
        )
        self._loop = asyncio.create_task(self.repl.run())

    @property
    def printed(self) -> str:
        return self.buffer.getvalue()

    async def shows(self, text: str) -> None:
        await _until(lambda: text in self.printed, f"{text!r} on screen")

    async def opens(self, prompt: str, times: int = 1) -> None:
        await _until(
            lambda: self.keyboard.prompts.count(prompt) >= times,
            f"the prompt {prompt!r} opened {times} time(s)",
        )

    async def leave(self) -> None:
        self.keyboard.type("/exit")
        await asyncio.wait_for(self._loop, WAIT_S)
        await asyncio.wait_for(self.app.aclose(), WAIT_S)


# ---- typed while a tool runs ------------------------------------------------


def test_a_line_typed_while_a_tool_runs_does_not_approve_the_card_that_follows(
    tmp_path,
):
    """``y`` and Enter while the tool before it is still running: the card
    opens unanswered and says why, and the ``y`` typed at it approves.

    Mutation: read the card with ``self._source.read`` in
    ``Repl._read_at_card`` and the first ``y`` approves a call nobody had
    been shown.
    """

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(_model(_gated()), _model(_needs_approval()), _says("done")),
        )
        session.keyboard.type("go")
        await session.shows("-> gated")
        session.keyboard.type("y")
        session.gated.finish.set()
        await session.opens(APPROVE.format(n=1))
        await _a_moment()
        at_the_open_card = session.printed
        session.keyboard.type("y")
        await session.shows("<- ask_a")
        await session.opens(PROMPT, times=2)
        await session.leave()
        return at_the_open_card, session.printed

    at_the_open_card, printed = asyncio.run(drive())

    assert "Approval granted [" not in at_the_open_card
    assert "<- ask_a" not in at_the_open_card
    assert NOTICE in at_the_open_card
    assert "Approval granted [" in printed and "<- ask_a ok" in printed
    assert printed.count(NOTICE) == 1


def test_a_line_typed_while_a_tool_runs_does_not_answer_the_question_that_follows(
    tmp_path,
):
    """Mutation: as above, and the model is told the person chose
    ``Louvain``."""

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(
                _model(_gated()), _model(_asks("Which clustering?")), _says("done")
            ),
        )
        session.keyboard.type("go")
        await session.shows("-> gated")
        session.keyboard.type("Louvain")
        session.gated.finish.set()
        await session.opens(ANSWER.format(n=1))
        await _a_moment()
        at_the_open_card = session.printed
        session.keyboard.type("Leiden")
        await session.shows("<- ask_user")
        await session.opens(PROMPT, times=2)
        await session.leave()
        return session, at_the_open_card

    session, at_the_open_card = asyncio.run(drive())

    assert "<- ask_user" not in at_the_open_card
    assert NOTICE in at_the_open_card
    assert _read_back(session.provider)["reply"] == "Leiden"
    assert session.keyboard.withdrawn == 0, "an answered prompt was taken down"


def test_a_card_with_nothing_typed_early_says_nothing_about_it(tmp_path):
    """Mutation: print the notice whenever a card is read and it appears
    above every card."""

    async def drive():
        session = _Session(tmp_path, Scripted(_model(_needs_approval()), _says("done")))
        session.keyboard.type("go")
        await session.opens(APPROVE.format(n=1))
        session.keyboard.type("y")
        await session.opens(PROMPT, times=2)
        await session.leave()
        return session.printed

    printed = asyncio.run(drive())

    assert "<- ask_a ok" in printed
    assert NOTICE not in printed


def test_a_line_half_typed_before_the_card_is_announced_and_dropped_whole(tmp_path):
    """``ye`` while the tool before it is still running, then ``s`` and
    Enter once the card is up. Read on its own, the ``s`` would allow the
    tool for the rest of the conversation. The card says that the
    unfinished line is discarded up to its Enter, stays open past that
    Enter, and takes the ``y`` typed afterwards.

    Mutation: do not pass ``unfinished`` to ``read_fresh`` in
    ``Repl._read_at_card`` and nothing on the card says why the ``s`` was
    not taken.
    """

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(_model(_gated()), _model(_needs_approval()), _says("done")),
        )
        session.keyboard.type("go")
        await session.shows("-> gated")
        session.keyboard.begin_a_line()
        session.gated.finish.set()
        await session.opens(APPROVE.format(n=1))
        at_the_open_card = session.printed
        session.keyboard.type("s")
        await session.opens(APPROVE.format(n=1), times=2)
        await _a_moment()
        after_the_enter = session.printed
        session.keyboard.type("y")
        await session.shows("<- ask_a")
        await session.opens(PROMPT, times=2)
        await session.leave()
        return at_the_open_card, after_the_enter, session.printed

    at_the_open_card, after_the_enter, printed = asyncio.run(drive())

    assert NOTICE in at_the_open_card and HALF_LINE_NOTICE in at_the_open_card
    assert at_the_open_card.index(NOTICE) < at_the_open_card.index(HALF_LINE_NOTICE)
    assert "Approval granted [" not in after_the_enter
    assert "Will not ask about" not in printed
    assert "<- ask_a ok" in printed
    assert (printed.count(NOTICE), printed.count(HALF_LINE_NOTICE)) == (1, 1)


# ---- two cards in one model message -------------------------------------------


@pytest.mark.parametrize(
    ("second", "prompt", "early", "at_the_card", "result"),
    [
        pytest.param(
            _needs_approval(),
            APPROVE.format(n=2),
            "y",
            "y",
            "<- ask_a",
            id="then an approval",
        ),
        pytest.param(
            _asks("Which resolution?", call_id="q2"),
            ANSWER.format(n=2),
            "0.5",
            "0.8",
            "(2) <- ask_user",
            id="then a second question",
        ),
    ],
)
def test_what_is_typed_between_two_cards_does_not_answer_the_second(
    tmp_path, second, prompt, early, at_the_card, result
):
    """Two replies arrive together while only the first card's prompt is
    open. The first answers its card. The second was typed before the
    second card's prompt, so that card opens unanswered.

    Mutation: as above, and the second reply settles a card that had not
    been shown when it was typed.
    """

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(_model(_asks("Which clustering?"), second), _says("done")),
        )
        session.keyboard.type("go")
        await session.opens(ANSWER.format(n=1))
        session.keyboard.type("Leiden")
        session.keyboard.type(early)
        await session.opens(prompt)
        await _a_moment()
        at_the_open_card = session.printed
        session.keyboard.type(at_the_card)
        await session.shows(result)
        await session.opens(PROMPT, times=2)
        await session.leave()
        return session, at_the_open_card

    session, at_the_open_card = asyncio.run(drive())

    assert result not in at_the_open_card
    assert "Approval granted [" not in at_the_open_card
    assert NOTICE in at_the_open_card
    replies = [
        message.content
        for message in session.provider.seen[-1]
        if message.role is Role.TOOL and message.name == "ask_user"
    ]
    assert '"reply": "Leiden"' in replies[0]
    if len(replies) == 2:
        assert '"reply": "0.8"' in replies[1]
    else:
        assert "<- ask_a ok" in session.printed


# ---- the reply to a question that is over ---------------------------------------


def test_a_late_reply_to_a_question_does_not_approve_the_card_that_follows(tmp_path):
    """The question's deadline passes and its prompt is taken down. The
    person then answers it with ``yes``, which nothing is reading. The
    approval card that opens next is not answered by it, and is denied at
    its own deadline.

    Mutation: as above, and ``yes`` typed for the question approves the
    tool call.
    """

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(
                _model(_asks("May I print a greeting?"), _gated()),
                _model(_needs_approval()),
                _says("done"),
            ),
            approval_timeout_s=0.3,
        )
        session.keyboard.type("go")
        await session.shows("No answer [")
        session.keyboard.type("yes")
        session.gated.finish.set()
        await session.shows("<- ask_a")
        await session.opens(PROMPT, times=2)
        await session.leave()
        return session

    session = asyncio.run(drive())

    assert "Approval granted [" not in session.printed
    assert "<- ask_a error" in session.printed
    assert "no answer before the approval deadline" in session.printed
    assert NOTICE in session.printed
    assert session.keyboard.prompts == [
        PROMPT,
        ANSWER.format(n=1),
        APPROVE.format(n=2),
        PROMPT,
    ]
    assert session.keyboard.withdrawn == 1


def test_a_late_reply_with_no_card_open_is_the_next_message(tmp_path):
    """With no card to answer, the line typed too late waits for the
    REPL's own prompt and goes to the model as the next message.

    Mutation: read the REPL's own prompt fresh as well and the line is
    dropped, and the REPL waits for a message that was already typed.
    """

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(
                _model(_asks("Which clustering?"), _gated()),
                _says("went on"),
                _says("second answer"),
            ),
            approval_timeout_s=0.3,
        )
        session.keyboard.type("go")
        await session.shows("No answer [")
        session.keyboard.type("2")
        session.gated.finish.set()
        await session.shows("second answer")
        await session.opens(PROMPT, times=3)
        await session.leave()
        return session

    session = asyncio.run(drive())

    said = [
        message.content
        for message in session.provider.seen[-1]
        if message.role is Role.USER
    ]
    assert said[-1] == "2"
    assert NOTICE not in session.printed
    assert session.keyboard.withdrawn == 1


# ---- a call that needs approval, after a question nobody answered -----------------
#
# One model message can carry ``ask_user`` and, after it, a call that needs
# approval. That call's card would open in the instant the question's
# deadline passes, so a ``yes`` typed for the question a moment too late
# would be typed after the card opened, and would approve it.


class _AsksAlone(Asking):
    """An approval tool that runs with nothing else of its message beside it."""

    policy = ToolPolicy(approval_mode=ApprovalMode.ASK, concurrency_safe=False)


def _results(sent: tuple[Message, ...], name: str) -> list[Message]:
    """The results of the calls to *name* among the messages *sent* to the model."""
    return [
        message
        for message in sent
        if message.role is Role.TOOL and message.name == name
    ]


def _approval_prompts(session: _Session) -> list[str]:
    return [one for one in session.keyboard.prompts if one.startswith("approve ")]


def test_a_call_after_a_question_nobody_answered_is_refused_without_a_prompt(
    tmp_path,
):
    """The question and a call that needs approval come in one model
    message, and the question's deadline passes. The call is refused and
    its prompt never opens. The ``yes`` typed for the question a moment
    late has no card to land on: it waits for the REPL's own prompt and is
    the next message. The model reads why the call was refused and that
    it may make the call again.

    Mutation: open the card all the same in ``Repl._ask`` and the late
    ``yes`` approves a call nobody was shown.
    """

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(
                _model(_asks("May I print a greeting?"), _needs_approval()),
                _says("went on"),
                _says("second answer"),
            ),
            approval_timeout_s=0.3,
        )
        session.keyboard.type("go")
        await session.shows("No answer [")
        session.keyboard.type("yes")
        await session.shows("second answer")
        await session.opens(PROMPT, times=3)
        await session.leave()
        return session

    session = asyncio.run(drive())

    assert _approval_prompts(session) == []
    assert "Approval granted [" not in session.printed
    assert "<- ask_a error" in session.printed
    denied = [
        line
        for line in session.printed.splitlines()
        if line.startswith("Approval denied [")
    ]
    assert len(denied) == 1 and NOBODY_ASKED in denied[0]
    (refused,) = _results(session.provider.seen[1], "ask_a")
    assert refused.is_error
    assert NOBODY_ASKED in refused.content
    assert "got no answer" in refused.content
    assert "again in a later message" in refused.content
    said = [
        message.content
        for message in session.provider.seen[-1]
        if message.role is Role.USER
    ]
    assert said[-1] == "yes"
    assert NOTICE not in session.printed


def test_the_call_made_again_in_a_later_message_is_asked_about(tmp_path):
    """Only the message that carried the unanswered question is affected.
    The model makes the call again in its next message, the card opens,
    and ``y`` typed at it approves.

    Mutation: do not clear the mark at ``TURN_END`` in ``Repl._pump`` and
    the second call is refused like the first.
    """

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(
                _model(_asks("May I print a greeting?"), _needs_approval()),
                _model(ToolCall(id="a2", name="ask_a", arguments="{}")),
                _says("done"),
            ),
            approval_timeout_s=1.0,
        )
        session.keyboard.type("go")
        await session.opens(APPROVE.format(n=3))
        session.keyboard.type("y")
        await session.shows("<- ask_a ok")
        await session.opens(PROMPT, times=2)
        await session.leave()
        return session

    session = asyncio.run(drive())

    assert _approval_prompts(session) == [APPROVE.format(n=3)]
    assert session.printed.count("Approval denied [") == 1
    assert "Approval granted [" in session.printed


def test_every_call_that_needs_approval_in_that_message_is_refused(tmp_path):
    """Two calls follow the unanswered question and run one after the
    other. Neither is asked about.

    Mutation: clear the mark once a call has been refused and the second
    call's card opens.
    """

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(
                _model(
                    _asks("May I print a greeting?"),
                    ToolCall(id="x1", name="alone_a", arguments="{}"),
                    ToolCall(id="x2", name="alone_b", arguments="{}"),
                ),
                _says("done"),
            ),
            tools=(_AsksAlone("alone_a"), _AsksAlone("alone_b")),
            approval_timeout_s=0.3,
        )
        session.keyboard.type("go")
        await session.shows("done")
        await session.opens(PROMPT, times=2)
        await session.leave()
        return session

    session = asyncio.run(drive())

    assert _approval_prompts(session) == []
    assert session.printed.count("Approval denied [") == 2
    for name in ("alone_a", "alone_b"):
        (refused,) = _results(session.provider.seen[-1], name)
        assert refused.is_error and NOBODY_ASKED in refused.content


@pytest.mark.parametrize(
    "reply",
    [pytest.param("Leiden", id="answered"), pytest.param("", id="skipped")],
)
def test_a_question_that_got_a_reply_leaves_the_call_after_it_asked_about(
    tmp_path, reply
):
    """A deadline is set and the person replies inside it, with an answer
    or with the empty line that skips. The card of the call after the
    question opens as it always did.

    Mutation: mark every settled question in ``Repl._pump``, or every one
    that was not answered, and the call is refused.
    """

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(
                _model(_asks("Which clustering?"), _needs_approval()), _says("done")
            ),
            approval_timeout_s=30.0,
        )
        session.keyboard.type("go")
        await session.opens(ANSWER.format(n=1))
        session.keyboard.type(reply)
        await session.opens(APPROVE.format(n=2))
        session.keyboard.type("y")
        await session.shows("<- ask_a ok")
        await session.opens(PROMPT, times=2)
        await session.leave()
        return session

    session = asyncio.run(drive())

    assert "Approval granted [" in session.printed
    assert NOBODY_ASKED not in session.printed


class _CannotShowTheQuestion(Keyboard):
    """A terminal that fails when the question's prompt is put up."""

    async def read_fresh(self, prompt: str, **reports) -> str:
        if prompt.startswith("answer"):
            self.prompts.append(prompt)
            raise RuntimeError("no tty")
        return await super().read_fresh(prompt, **reports)


def test_a_question_unanswered_for_another_reason_leaves_the_call_asked_about(
    tmp_path,
):
    """The question could not be put, which the model also reads as
    ``no_answer``. No deadline passed, nobody was typing a reply, and the
    call after it is asked about.

    Mutation: mark a question settled as ``no_answer`` whatever its reason
    and the call is refused.
    """

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(
                _model(_asks("Which clustering?"), _needs_approval()), _says("done")
            ),
            keyboard=_CannotShowTheQuestion(),
            approval_timeout_s=30.0,
        )
        session.keyboard.type("go")
        await session.opens(APPROVE.format(n=2))
        session.keyboard.type("y")
        await session.shows("<- ask_a ok")
        await session.opens(PROMPT, times=2)
        await session.leave()
        return session

    session = asyncio.run(drive())

    assert "the terminal could not ask: no tty" in session.printed
    assert "Approval granted [" in session.printed
    assert NOBODY_ASKED not in session.printed


def test_a_tool_allowed_for_the_conversation_runs_after_the_question(tmp_path):
    """``s`` at an earlier card stopped the asking about this tool, so its
    call would have opened no prompt. It runs after the unanswered
    question as it runs anywhere else.

    Mutation: refuse before looking at the grant in ``Repl._ask`` and the
    second call is refused.
    """

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(
                _model(_needs_approval()),
                _says("first done"),
                _model(_asks("May I print a greeting?"), _needs_approval()),
                _says("second done"),
            ),
            approval_timeout_s=1.0,
        )
        session.keyboard.type("once")
        await session.opens(APPROVE.format(n=1))
        session.keyboard.type("s")
        await session.shows("first done")
        await session.opens(PROMPT, times=2)
        session.keyboard.type("twice")
        await session.shows("No answer [")
        await session.shows("second done")
        await session.opens(PROMPT, times=3)
        await session.leave()
        return session

    session = asyncio.run(drive())

    assert _approval_prompts(session) == [APPROVE.format(n=1)]
    assert session.printed.count("<- ask_a ok") == 2
    assert "ask_a: allowed for this conversation." in session.printed
    assert NOBODY_ASKED not in session.printed


def test_a_sub_agent_s_call_after_the_question_is_asked_about(tmp_path):
    """The message that carried the unanswered question also hands a task
    to a sub-agent. A sub-agent calls its model before it calls a tool,
    so its card opens later, as the card of a next message does, and it
    is asked about. Refused, the sub-agent would read that it may make
    the call again in a later message, and be refused again for as long
    as the task ran.

    Mutation: refuse a sub-agent's request too in ``Repl._pump`` and the
    prompt below never opens.
    """
    delegating = _model(
        _asks("May I print a greeting?"),
        ToolCall(
            id="d1",
            name="task",
            arguments=json.dumps(
                {"subagent_type": "general-purpose", "prompt": "do the thing"}
            ),
        ),
    )
    asked = "approve ask_a for sub-agent general-purpose [#2]? [y/N/a=always] "

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(
                delegating,
                _model(_needs_approval()),
                _says("the sub-agent finished"),
                _says("handed back"),
            ),
            approval_timeout_s=1.0,
        )
        session.keyboard.type("go")
        await session.opens(asked)
        session.keyboard.type("y")
        await session.shows("handed back")
        await session.opens(PROMPT, times=2)
        await session.leave()
        return session

    session = asyncio.run(drive())

    assert _approval_prompts(session) == [asked]
    assert "No answer [" in session.printed
    assert "Approval granted [" in session.printed
    assert NOBODY_ASKED not in session.printed


def test_under_auto_approve_the_call_after_the_question_runs(tmp_path):
    """In ``auto-approve`` the call asks nobody, so there is no card to
    keep closed and nothing to refuse."""

    async def drive():
        session = _Session(
            tmp_path,
            Scripted(
                _model(_asks("May I print a greeting?"), _needs_approval()),
                _says("done"),
            ),
            approval_timeout_s=0.3,
            permission_mode=PermissionMode.AUTO_APPROVE,
        )
        session.keyboard.type("go")
        await session.shows("No answer [")
        await session.shows("<- ask_a ok")
        await session.opens(PROMPT, times=2)
        await session.leave()
        return session

    session = asyncio.run(drive())

    assert "Approval required [" not in session.printed
    assert "Approval denied [" not in session.printed
    assert _approval_prompts(session) == []


# ---- input that is not typed ----------------------------------------------------


def test_a_piped_script_answers_a_card_with_its_next_line(tmp_path):
    """``printf 'go\\ny\\n' | oc cli``: the whole script is in the pipe
    before the card opens, and its second line is the answer its author
    wrote for that card. A pipe has no earlier and later to tell apart.

    Mutation: throw away what is waiting in a stream that is not a
    terminal and the card meets the end of input and is denied.
    """

    async def drive():
        app = _build(
            tmp_path,
            Scripted(_model(_needs_approval()), _says("done")),
            tools=(Asking("ask_a"),),
        )
        reading, writing = os.pipe()
        os.write(writing, b"go\ny\n")
        os.close(writing)
        buffer = io.StringIO()
        with os.fdopen(reading, "r", encoding="utf-8") as piped:
            repl = Repl(app, source=StreamSource(piped), screen=Screen.into(buffer))
            await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return buffer.getvalue()

    printed = asyncio.run(drive())

    assert "Approval granted [" in printed and "<- ask_a ok" in printed
    assert NOTICE not in printed


# ---- the source used without prompt_toolkit --------------------------------------


def test_with_an_echoed_prompt_no_answer_starts_on_a_line_of_its_own(tmp_path):
    """:class:`StreamSource` writes the prompt and leaves the cursor after
    it. When the question's deadline passes, the line has to be ended
    before ``No answer`` is printed.

    Mutation: do not have the source withdraw the prompt in
    ``Repl._retract_question`` and ``No answer`` follows the prompt on its
    line.
    """

    async def drive():
        gated = _Gated()
        provider = Scripted(
            _model(_asks("Which clustering?"), _gated()), _says("went on")
        )
        app = _build(tmp_path, provider, tools=(gated,), approval_timeout_s=0.3)
        buffer = io.StringIO()
        stream = Typist()
        source = StreamSource(stream, echo=buffer)
        repl = Repl(app, source=source, screen=Screen.into(buffer))
        loop = asyncio.create_task(repl.run())
        stream.type("cluster it\n")
        await _until(lambda: "No answer [" in buffer.getvalue(), "the deadline passed")
        gated.finish.set()
        await _until(
            lambda: buffer.getvalue().count(PROMPT) == 2, "the REPL's prompt came back"
        )
        stream.type("/exit\n")
        await asyncio.wait_for(loop, WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return buffer.getvalue()

    printed = asyncio.run(drive())

    assert f"{ANSWER.format(n=1)}\nNo answer [" in printed
    assert "went on" in printed
