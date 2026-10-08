"""A question to the person: who is waiting, how it is shown, how a reply is read.

:class:`QuestionBroker` is the :data:`~omicsclaw.tools.QuestionChannel` of
one exchange. A tool that calls :func:`~omicsclaw.tools.ask_question`
reaches it, a ``QUESTION_ASKED`` frame goes out on the exchange's stream,
and whoever shows the question answers through :meth:`QuestionBroker.settle`
from another Task.

:func:`question_card`, :func:`reply_hint` and :func:`read_reply` are what a
surface shows and how it reads what comes back. They live together because
the numbers on the card are the numbers a reply is read by.

No question text and no reply is logged here: only a request's id and how
it ended.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Final

from omicsclaw.entry.display import (
    MAX_APPROVAL_BODY_CHARS,
    MAX_APPROVAL_BODY_LINES,
    inert_body,
    inert_line,
)
from omicsclaw.entry.events import TurnEvent
from omicsclaw.entry.rendezvous import Rendezvous
from omicsclaw.entry.stream import TurnStream
from omicsclaw.tools.builtin.ask_user import option_numbers
from omicsclaw.tools.context import AnswerStatus, QuestionAnswer, QuestionRequest

__all__ = [
    "NOT_ASKED_REASON",
    "QUESTION_ABANDONED_REASON",
    "QUESTION_TIMEOUT_REASON",
    "QuestionBroker",
    "question_card",
    "read_reply",
    "reply_hint",
]

_log = logging.getLogger(__name__)

QUESTION_TIMEOUT_REASON: Final = "no answer before the question deadline"
"""Reason on the ``no_answer`` a question gets when its deadline passes.
The model reads it in the tool's result."""

QUESTION_ABANDONED_REASON: Final = "the exchange ended before the question was answered"
"""Reason on the ``no_answer`` a question gets when its exchange ends first."""

NOT_ASKED_REASON: Final = (
    "not asked: an earlier question in this request went unanswered"
)
"""Reason on the ``no_answer`` returned without asking, once a question of
the same exchange has expired or the exchange is ending."""

_HINT_OPEN: Final = "Reply in your own words."
_HINT_ONE: Final = "Reply with an option number, or in your own words."
_HINT_SEVERAL: Final = (
    "Reply with one or more option numbers separated by commas, or in your "
    "own words."
)


def _unanswered(reason: str) -> QuestionAnswer:
    return QuestionAnswer(AnswerStatus.NO_ANSWER, reason=reason)


class QuestionBroker:
    """One exchange's outstanding questions to the person, addressable by id.

    The instance is the :data:`~omicsclaw.tools.QuestionChannel`: bind it
    with :func:`~omicsclaw.tools.use_tool_context`. :meth:`__call__` runs on
    the Task of the tool that asks and suspends there; :meth:`settle` runs
    on whatever Task the answer arrived on.

    A question whose deadline passes is answered ``no_answer``, and from
    then on the broker asks nothing more in this exchange: later calls
    return ``no_answer`` at once, with :data:`NOT_ASKED_REASON` and no
    frame. The same holds after :meth:`abandon`.

    Not thread-safe: call every method on the event loop's thread.
    """

    __slots__ = ("_closed", "_rendezvous", "_stream")

    def __init__(
        self,
        stream: TurnStream,
        *,
        timeout_s: float | None = None,
        numbering: Iterator[int] | None = None,
    ) -> None:
        """Wait up to *timeout_s* per question; draw ids from *numbering*.

        Args:
            stream: The exchange's stream, where the frames are published.
            timeout_s: Seconds a question waits for its answer; ``None``
                waits for as long as the exchange runs.
            numbering: Where the ``n`` of each ``<turn id>#<n>`` id comes
                from. Pass the iterator the exchange's approval broker
                draws from and no id is issued twice; ``None`` counts
                from 1.

        Raises:
            ValueError: *timeout_s* is zero or negative.
        """
        self._stream = stream
        self._closed = False
        self._rendezvous: Rendezvous[QuestionRequest, QuestionAnswer] = Rendezvous(
            stream,
            asked=self._asked,
            settled=self._settled,
            on_expired=self._expired,
            timeout_s=timeout_s,
            numbering=numbering,
        )

    async def __call__(self, request: QuestionRequest) -> QuestionAnswer:
        """Publish the question and return its answer.

        Returns:
            The answer given to :meth:`settle`; ``no_answer`` with
            :data:`QUESTION_TIMEOUT_REASON` when the deadline passed, with
            :data:`QUESTION_ABANDONED_REASON` when :meth:`abandon` ended
            the wait, or with :data:`NOT_ASKED_REASON`, at once and
            without a frame, when the broker has stopped asking.

        Raises:
            asyncio.CancelledError: The exchange was cancelled while the
                question was outstanding. The question is dropped first,
                so a later :meth:`settle` for it returns ``False``.
        """
        if self._closed:
            _log.info(
                "question not asked: turn=%s status=%s",
                self._stream.turn_id,
                AnswerStatus.NO_ANSWER.value,
            )
            return _unanswered(NOT_ASKED_REASON)
        return await self._rendezvous.ask(request)

    def settle(self, request_id: str, answer: QuestionAnswer) -> bool:
        """Answer one outstanding question.

        Returns:
            ``False`` when *request_id* is unknown or already answered,
            which is ordinary input from a person and changes nothing.
        """
        return self._rendezvous.settle(request_id, answer)

    def abandon(self, reason: str = QUESTION_ABANDONED_REASON) -> None:
        """Settle every outstanding question as unanswered, and ask no more.

        Synchronous, so it completes inside a ``finally`` of a Task that
        has already been cancelled.
        """
        self._closed = True
        self._rendezvous.abandon(_unanswered(reason))

    def pending(self) -> tuple[str, ...]:
        """Ids of the questions waiting for an answer, oldest first."""
        return self._rendezvous.pending()

    def expires_at(self, request_id: str) -> float | None:
        """Loop time at which the question's deadline passes.

        ``None`` when this broker has no deadline, and when *request_id*
        is unknown or already answered.
        """
        return self._rendezvous.expires_at(request_id)

    def _asked(self, request: QuestionRequest, request_id: str) -> TurnEvent:
        """The ``QUESTION_ASKED`` frame for one question."""
        _log.info("question asked: request=%s", request_id)
        return TurnEvent.question_asked(
            request,
            request_id,
            session_id=self._stream.session_id,
            turn_id=self._stream.turn_id,
        )

    def _settled(self, request_id: str, answer: QuestionAnswer) -> TurnEvent:
        """The ``QUESTION_SETTLED`` frame for one answer."""
        _log.info(
            "question settled: request=%s status=%s", request_id, answer.status.value
        )
        return TurnEvent.question_settled(
            request_id,
            answer,
            session_id=self._stream.session_id,
            turn_id=self._stream.turn_id,
        )

    def _expired(self) -> QuestionAnswer:
        """Stop asking in this exchange, and answer the expired question."""
        self._closed = True
        return _unanswered(QUESTION_TIMEOUT_REASON)


def question_card(
    request: QuestionRequest, ref: str, *, width: int | None = None
) -> tuple[str, bool]:
    """The question headed by *ref* and its numbered options, as inert text.

    The card is ``Question [<ref>]: <question>``, then one line per option,
    ``<n>. <label>`` with `` - <description>`` when there is one. Question
    and options together are rendered by
    :func:`~omicsclaw.entry.display.inert_body` under the approval card's
    bounds, so every line after the first begins with a prefix, and a long
    card is cut and opens with a note of its size. Each option takes one
    line, whatever line breaks its label or description holds.

    Args:
        request: The question to show.
        ref: What the person and the surface call this question, such as
            its request id.
        width: The most characters any line of the card may have, the
            header's line included; ``None`` leaves lines as long as they
            are.

    Returns:
        The card, and ``True`` when anything of it was cut.

    Raises:
        ValueError: *width* is not an integer, or leaves fewer than 10
            characters beside the card's header. The message names *width*
            as it was passed.
    """
    head = f"Question [{inert_line(ref)}]"
    lines = [request.question]
    for number, option in enumerate(request.options, start=1):
        line = f"{number}. {inert_line(option.label)}"
        if option.description:
            line += f" - {inert_line(option.description)}"
        lines.append(line)
    try:
        body, cut = inert_body(
            "\n".join(lines),
            max_lines=MAX_APPROVAL_BODY_LINES,
            max_chars=MAX_APPROVAL_BODY_CHARS,
            width=None if width is None else width - len(head) - len(": "),
        )
    except ValueError as error:
        if width is None:
            raise
        raise ValueError(
            f"width must be an integer that leaves at least 10 characters beside "
            f"the {len(head) + len(': ')} of the card's header, not {width!r}"
        ) from error
    return (f"{head}: {body}" if body else head), cut


def reply_hint(request: QuestionRequest) -> str:
    """How to reply to *request*, worded for its shape.

    One sentence: by option number or in the person's own words when there
    are options, with "one or more" when several may be chosen, and in
    their own words alone for an open question.
    """
    if not request.options:
        return _HINT_OPEN
    return _HINT_SEVERAL if request.multi_select else _HINT_ONE


def read_reply(request: QuestionRequest, text: str) -> QuestionAnswer:
    """Read a typed reply as an answer to *request*.

    Args:
        request: The question the reply answers.
        text: What the person typed.

    Returns:
        ``declined`` for a blank reply. Otherwise ``answered``, with
        ``selected`` holding the labels the reply chose: the options whose
        numbers it lists (commas or spaces between them, each from 1 to the
        number of options, exactly one unless the question allows several;
        repeats are dropped and the order kept), or the one option whose
        label the whole reply equals apart from case. Any other reply,
        including a number past the last option and several numbers for a
        question that allows one, is the person's own words and
        ``selected`` is empty. ``reply`` is always *text* as typed.
    """
    if not text.strip():
        return QuestionAnswer(AnswerStatus.DECLINED, reply=text)
    return QuestionAnswer(
        AnswerStatus.ANSWERED, reply=text, selected=_selected(request, text)
    )


def _selected(request: QuestionRequest, text: str) -> tuple[str, ...]:
    """The labels of *request*'s options that *text* chooses, in the order chosen."""
    options = request.options
    if not options:
        return ()
    numbers = option_numbers(text)
    if numbers is not None:
        if not all(1 <= number <= len(options) for number in numbers):
            return ()
        if not request.multi_select and len(numbers) != 1:
            return ()
        return tuple(options[number - 1].label for number in dict.fromkeys(numbers))
    typed = text.strip().casefold()
    for option in options:
        if option.label.casefold() == typed:
            return (option.label,)
    return ()
