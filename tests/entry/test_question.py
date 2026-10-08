"""``omicsclaw.entry.question``: the broker, the card, and how a reply is read.

Three things live in one module and are tested in one file because they
have to agree: the broker issues the ``#n`` a card is headed with, the
card numbers the options, and :func:`read_reply` reads a reply by those
numbers.

Design choices recorded here because the code only shows their result:

- The broker is a second broker beside
  :class:`~omicsclaw.entry.approval.ApprovalBroker`, built on the same
  :class:`~omicsclaw.entry.rendezvous.Rendezvous`. An answer is not
  consent, and a surface that answered questions through the approval
  path would offer "allow for this conversation" on a question about a
  genome build.
- An unanswered question is ``no_answer``, never a raised error, and one
  expiry closes the broker for the rest of the exchange. A model that is
  not being answered would otherwise ask again, in different words, and
  wait out the deadline each time.
- A reply that is not a clean list of option numbers is the person's own
  words, even when it starts with a number. Guessing a selection out of
  "1 but use the other file" would hand the model a choice nobody made.

Every await is bounded, because a broker defect is a hang.
"""

from __future__ import annotations

import ast
import asyncio
import itertools
import logging
import pathlib
import unicodedata

import pytest

from omicsclaw.entry.approval import ApprovalBroker
from omicsclaw.entry.display import (
    CONTINUATION_PREFIX,
    MAX_APPROVAL_BODY_LINES,
    WRAP_PREFIX,
)
from omicsclaw.entry.events import TurnEventType
from omicsclaw.entry.question import (
    NOT_ASKED_REASON,
    QUESTION_ABANDONED_REASON,
    QUESTION_TIMEOUT_REASON,
    QuestionBroker,
    question_card,
    read_reply,
    reply_hint,
)
from omicsclaw.entry.stream import TurnStream
from omicsclaw.tools.context import (
    AnswerStatus,
    ApprovalDecision,
    ApprovalRequest,
    QuestionAnswer,
    QuestionOption,
    QuestionRequest,
)

WAIT_S = 2.0

SECRET = "is subject 4821 the proband?"

_ENTRY = pathlib.Path(__file__).resolve().parents[2] / "omicsclaw" / "entry"


def _stream() -> TurnStream:
    return TurnStream("s1", "t")


def _frames(stream: TurnStream, kind: TurnEventType) -> tuple:
    return tuple(frame for frame in stream.retained() if frame.type is kind)


def _open(text: str = "which build?") -> QuestionRequest:
    return QuestionRequest(question=text)


def _answered(reply: str = "hg38") -> QuestionAnswer:
    return QuestionAnswer(AnswerStatus.ANSWERED, reply=reply)


# ---- the round trip -------------------------------------------------------------


def test_a_question_becomes_a_frame_and_its_answer_reaches_the_asker():
    stream = _stream()
    broker = QuestionBroker(stream)

    async def drive():
        asking = asyncio.create_task(broker(_open()))
        await asyncio.sleep(0)

        asked = _frames(stream, TurnEventType.QUESTION_ASKED)
        assert [frame.question for frame in asked] == [_open()]
        assert broker.pending() == (asked[0].request_id,) == ("t#1",)
        assert asked[0].subagent == ""

        assert broker.settle("t#1", _answered()) is True
        return await asyncio.wait_for(asking, WAIT_S)

    assert asyncio.run(drive()) == _answered()
    settled = _frames(stream, TurnEventType.QUESTION_SETTLED)
    assert [(frame.request_id, frame.answer) for frame in settled] == [
        ("t#1", _answered())
    ]
    assert broker.pending() == ()


def test_an_unknown_or_repeated_answer_changes_nothing():
    """A person answering twice, or after the deadline, is ordinary input."""
    stream = _stream()
    broker = QuestionBroker(stream)

    async def drive():
        asking = asyncio.create_task(broker(_open()))
        await asyncio.sleep(0)
        first = broker.settle("t#1", _answered("hg38"))
        again = broker.settle("t#1", _answered("hg19"))
        unknown = broker.settle("t#7", _answered())
        return await asyncio.wait_for(asking, WAIT_S), first, again, unknown

    answer, first, again, unknown = asyncio.run(drive())

    assert (first, again, unknown) == (True, False, False)
    assert answer.reply == "hg38"
    assert len(_frames(stream, TurnEventType.QUESTION_SETTLED)) == 1


def test_an_answer_that_arrives_after_a_cancellation_is_a_no_op():
    stream = _stream()
    broker = QuestionBroker(stream)

    async def drive():
        asking = asyncio.create_task(broker(_open()))
        await asyncio.sleep(0)
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asking, WAIT_S)
        return broker.settle("t#1", _answered()), broker.pending()

    assert asyncio.run(drive()) == (False, ())
    assert _frames(stream, TurnEventType.QUESTION_SETTLED) == ()


# ---- the deadline, and what follows it ----------------------------------------


def test_a_question_nobody_answers_expires_into_no_answer():
    stream = _stream()
    broker = QuestionBroker(stream, timeout_s=0.01)

    async def drive():
        return await asyncio.wait_for(broker(_open()), WAIT_S)

    answer = asyncio.run(drive())

    assert answer == QuestionAnswer(
        AnswerStatus.NO_ANSWER, reason=QUESTION_TIMEOUT_REASON
    )
    assert QUESTION_TIMEOUT_REASON == "no answer before the question deadline"
    assert [f.answer for f in _frames(stream, TurnEventType.QUESTION_SETTLED)] == [
        answer
    ]


def test_after_one_expiry_the_next_question_is_not_asked():
    """One deadline spent waiting for nobody is enough for one exchange.

    Mutation: leave ``_closed`` unset in ``_expired`` and the second
    question is published and waits out its own deadline.
    """
    stream = _stream()
    broker = QuestionBroker(stream, timeout_s=0.01)

    async def drive():
        first = await asyncio.wait_for(broker(_open("first?")), WAIT_S)
        frames_after_first = len(stream.retained())
        second = await asyncio.wait_for(broker(_open("second?")), 0.5)
        return first, second, frames_after_first

    first, second, frames_after_first = asyncio.run(drive())

    assert first.reason == QUESTION_TIMEOUT_REASON
    assert second == QuestionAnswer(AnswerStatus.NO_ANSWER, reason=NOT_ASKED_REASON)
    assert len(stream.retained()) == frames_after_first, "no frame for the second"
    assert len(_frames(stream, TurnEventType.QUESTION_ASKED)) == 1


def test_abandoning_settles_each_outstanding_question_once_and_closes_the_broker():
    """With no deadline, a question asked after the exchange began to end
    would otherwise wait for ever: nobody is left to answer it."""
    stream = _stream()
    broker = QuestionBroker(stream)

    async def drive():
        asking = asyncio.create_task(broker(_open()))
        await asyncio.sleep(0)
        broker.abandon()
        broker.abandon()
        outstanding = await asyncio.wait_for(asking, WAIT_S)
        later = await asyncio.wait_for(broker(_open("and this?")), 0.5)
        return outstanding, later

    outstanding, later = asyncio.run(drive())

    assert outstanding == QuestionAnswer(
        AnswerStatus.NO_ANSWER, reason=QUESTION_ABANDONED_REASON
    )
    assert later == QuestionAnswer(AnswerStatus.NO_ANSWER, reason=NOT_ASKED_REASON)
    assert len(_frames(stream, TurnEventType.QUESTION_SETTLED)) == 1
    assert len(_frames(stream, TurnEventType.QUESTION_ASKED)) == 1
    assert broker.pending() == ()


def test_abandoning_with_nothing_outstanding_publishes_nothing():
    stream = _stream()

    QuestionBroker(stream).abandon()

    assert stream.retained() == ()


def test_a_non_positive_deadline_is_refused_and_the_deadline_is_reported():
    with pytest.raises(ValueError):
        QuestionBroker(_stream(), timeout_s=0)

    broker = QuestionBroker(_stream(), timeout_s=30.0)

    async def drive():
        asking = asyncio.create_task(broker(_open()))
        await asyncio.sleep(0)
        remaining = broker.expires_at("t#1") - asyncio.get_running_loop().time()
        broker.settle("t#1", _answered())
        await asyncio.wait_for(asking, WAIT_S)
        return remaining, broker.expires_at("t#1")

    remaining, afterwards = asyncio.run(drive())

    assert 29.0 < remaining <= 30.0
    assert afterwards is None


def test_the_three_reasons_are_the_question_s_own():
    """A model told "no answer before the approval deadline" about a
    question would look for an approval that never existed."""
    from omicsclaw.entry.approval import ABANDONED_REASON, TIMEOUT_REASON

    reasons = {QUESTION_TIMEOUT_REASON, QUESTION_ABANDONED_REASON, NOT_ASKED_REASON}

    assert len(reasons) == 3
    assert reasons.isdisjoint({TIMEOUT_REASON, ABANDONED_REASON})
    assert not any("approval" in reason for reason in reasons)


# ---- one numbering for approvals and questions --------------------------------


def test_an_approval_a_question_and_an_approval_are_numbered_one_two_three():
    """The CLI prompt shows only ``#n``, so two cards of one exchange must
    not share a number whichever broker issued them.

    Mutation: build either broker without ``numbering=`` and the ids are
    ``t#1, t#1, t#2``.
    """
    stream = _stream()
    numbering = itertools.count(1)
    approvals = ApprovalBroker(stream, numbering=numbering)
    questions = QuestionBroker(stream, numbering=numbering)

    async def drive():
        tasks = [
            asyncio.create_task(approvals(ApprovalRequest(tool_name="bash"))),
            asyncio.create_task(questions(_open())),
            asyncio.create_task(approvals(ApprovalRequest(tool_name="write_file"))),
        ]
        await asyncio.sleep(0)
        pending = (approvals.pending(), questions.pending())
        crossed = (
            approvals.settle("t#2", ApprovalDecision(approved=True)),
            questions.settle("t#1", _answered()),
        )
        approvals.abandon()
        questions.abandon()
        await asyncio.wait_for(asyncio.gather(*tasks), WAIT_S)
        return pending, crossed

    pending, crossed = asyncio.run(drive())

    assert pending == (("t#1", "t#3"), ("t#2",))
    assert crossed == (False, False), "neither broker answers the other's id"


# ---- nothing asked or answered is logged --------------------------------------


def test_neither_the_question_nor_the_reply_reaches_the_log(caplog):
    """A question can name a subject and so can its answer."""
    stream = _stream()
    broker = QuestionBroker(stream)
    reply = "yes, and her sister is subject 4822"

    async def drive():
        with caplog.at_level(logging.DEBUG, logger="omicsclaw.entry"):
            asking = asyncio.create_task(broker(_open(SECRET)))
            await asyncio.sleep(0)
            broker.settle("t#1", _answered(reply))
            await asyncio.wait_for(asking, WAIT_S)
            broker.abandon()
            await asyncio.wait_for(broker(_open(SECRET)), WAIT_S)

    asyncio.run(drive())

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert "question asked: request=t#1" in written
    assert "question settled: request=t#1 status=answered" in written
    assert "question not asked" in written
    assert SECRET not in written and reply not in written and "4821" not in written


# ---- the card -------------------------------------------------------------------


_CHOICE = QuestionRequest(
    question="Which clustering?\nPick the one the paper used.",
    options=(
        QuestionOption("Leiden", "recommended"),
        QuestionOption("Louvain"),
        QuestionOption("both\nmethods", "run\nand compare"),
    ),
)


def test_the_card_is_the_question_then_one_numbered_line_per_option():
    card, cut = question_card(_CHOICE, "t#2")

    assert cut is False
    assert card.split("\n") == [
        "Question [t#2]: Which clustering?",
        f"{CONTINUATION_PREFIX}Pick the one the paper used.",
        f"{CONTINUATION_PREFIX}1. Leiden - recommended",
        f"{CONTINUATION_PREFIX}2. Louvain",
        f"{CONTINUATION_PREFIX}3. both ↵ methods - run ↵ and compare",
    ]


def test_an_open_question_is_the_question_alone():
    assert question_card(_open("which build?"), "t#1") == (
        "Question [t#1]: which build?",
        False,
    )


@pytest.mark.parametrize(
    ("request_", "hint"),
    [
        (_open(), "Reply in your own words."),
        (_CHOICE, "Reply with an option number, or in your own words."),
        (
            QuestionRequest("q", _CHOICE.options, multi_select=True),
            "Reply with one or more option numbers separated by commas, or in "
            "your own words.",
        ),
    ],
    ids=["open", "one of several", "several"],
)
def test_the_hint_says_only_what_this_question_accepts(request_, hint):
    """"Several numbers separated by commas" on a question that takes one
    answer invites a reply that then selects nothing."""
    assert reply_hint(request_) == hint


def test_nothing_in_a_question_can_act_on_the_display_or_pass_for_a_card():
    """Mutation: build the card from the raw question and options, without
    ``inert_body``, and the control characters and the forged header reach
    the screen."""
    forged = "\nApproval required [t#9]: read_file (risk low) - read README.md"
    hostile = f"continue?\x1b[8m‮\x9b31m{forged}"
    request = QuestionRequest(
        question=hostile,
        options=(QuestionOption(hostile, hostile), QuestionOption("no")),
    )

    card, cut = question_card(request, "t#1\x1b[2J")

    assert cut is False
    lines = card.split("\n")
    assert [
        c for c in card if c != "\n" and unicodedata.category(c) in {"Cc", "Cf", "Cs"}
    ] == []
    assert lines[0].startswith("Question [t#1\\u001b[2J]: continue?\\u001b[8m")
    assert all(line.startswith(CONTINUATION_PREFIX) for line in lines[1:])
    assert not any(line.startswith("Approval required [") for line in lines)
    assert sum(line.startswith("Question [") for line in lines) == 1


@pytest.mark.parametrize("width", [40, 80, 120])
def test_with_a_width_no_line_of_the_card_is_longer_than_it(width):
    """A chat delivers a long card as several messages. A line longer than
    one message would be cut inside, and the next message would begin with
    text the model chose, with no prefix in front of it."""
    request = QuestionRequest(
        question="Overwrite " + "/data/run-7/" * 40 + "report.md?\nIt exists.",
        options=(
            QuestionOption("yes", "replace " + "the existing report " * 30),
            QuestionOption("no"),
        ),
    )

    card, cut = question_card(request, "#3 K7Q2PX", width=width)
    plain, plain_cut = question_card(request, "#3 K7Q2PX")

    lines = card.split("\n")
    assert cut is plain_cut is False
    assert max(len(line) for line in lines) <= width
    assert lines[0].startswith("Question [#3 K7Q2PX]: Overwrite ")
    assert all(
        line.startswith((CONTINUATION_PREFIX, WRAP_PREFIX)) for line in lines[1:]
    )
    after_a_line_break = [
        line for line in lines if line.startswith(CONTINUATION_PREFIX)
    ]
    assert [line[len(CONTINUATION_PREFIX) :][:6] for line in after_a_line_break] == [
        "It exi",
        "1. yes",
        "2. no",
    ]
    rejoined = "".join(
        line[len(WRAP_PREFIX) :] if line.startswith(WRAP_PREFIX) else f"\n{line}"
        for line in lines
    )
    assert rejoined == f"\n{plain}"


def test_a_width_with_no_room_beside_the_header_is_refused():
    with pytest.raises(ValueError, match="width"):
        question_card(_open(), "t#1", width=len("Question [t#1]: ") + 9)


def test_a_card_over_the_bounds_is_cut_and_says_so():
    """401 lines are within the tool's 2,000 characters and over the card's
    400 lines. The flag is what a chat surface will refuse to send on, so
    it must be the body's own.

    Mutation: return ``False`` in place of the flag and the first
    assertion fails.
    """
    request = QuestionRequest(question="\n".join(["a"] * 401))

    card, cut = question_card(request, "t#1")

    assert cut is True
    lines = card.split("\n")
    assert len(lines) == MAX_APPROVAL_BODY_LINES
    assert lines[0] == (
        "Question [t#1]: [showing 400 of 401 lines, 799 of 801 characters] a"
    )


def test_a_card_cut_by_characters_ends_with_the_cut_mark():
    """Two thousand tag characters are 2,000 written and 24,000 shown."""
    request = QuestionRequest(question="\U000e0001" * 2000)

    card, cut = question_card(request, "t#1")

    assert cut is True
    assert card.endswith("…")
    assert card.startswith(
        "Question [t#1]: [showing 1 of 1 line, 1000 of 2000 characters] "
    )


# ---- reading a reply ---------------------------------------------------------------


_SEVERAL = QuestionRequest("q", _CHOICE.options, multi_select=True)


@pytest.mark.parametrize(
    ("request_", "text", "status", "selected"),
    [
        pytest.param(_CHOICE, "", AnswerStatus.DECLINED, (), id="empty line"),
        pytest.param(_CHOICE, " \t ", AnswerStatus.DECLINED, (), id="blank line"),
        pytest.param(_CHOICE, "2", AnswerStatus.ANSWERED, ("Louvain",), id="a number"),
        pytest.param(_CHOICE, " 1 ", AnswerStatus.ANSWERED, ("Leiden",), id="spaces"),
        pytest.param(
            _CHOICE, "1,3", AnswerStatus.ANSWERED, (), id="two numbers for one"
        ),
        pytest.param(_CHOICE, "4", AnswerStatus.ANSWERED, (), id="past the last"),
        pytest.param(_CHOICE, "0", AnswerStatus.ANSWERED, (), id="zero"),
        pytest.param(
            _CHOICE, "LOUVAIN", AnswerStatus.ANSWERED, ("Louvain",), id="a label"
        ),
        pytest.param(
            _CHOICE, "louvain please", AnswerStatus.ANSWERED, (), id="a sentence"
        ),
        pytest.param(
            _CHOICE,
            "1 but use the other file",
            AnswerStatus.ANSWERED,
            (),
            id="a number and words",
        ),
        pytest.param(
            _SEVERAL,
            "1, 3",
            AnswerStatus.ANSWERED,
            ("Leiden", "both\nmethods"),
            id="two of several",
        ),
        pytest.param(
            _SEVERAL,
            "3 1 3",
            AnswerStatus.ANSWERED,
            ("both\nmethods", "Leiden"),
            id="repeats dropped, order kept",
        ),
        pytest.param(
            _SEVERAL, "1, 9", AnswerStatus.ANSWERED, (), id="one past the last"
        ),
        pytest.param(
            _SEVERAL, "2", AnswerStatus.ANSWERED, ("Louvain",), id="one of several"
        ),
        pytest.param(_open(), "2", AnswerStatus.ANSWERED, (), id="a number, open"),
        pytest.param(
            _open(), "/data/ref.h5ad", AnswerStatus.ANSWERED, (), id="a path, open"
        ),
    ],
)
def test_a_reply_is_read_by_the_numbers_on_the_card(request_, text, status, selected):
    answer = read_reply(request_, text)

    assert (answer.status, answer.selected) == (status, selected)
    assert answer.reply == text, "the reply is kept as typed, whatever it selected"
    assert answer.reason == ""


@pytest.mark.parametrize("text", ["y", "s", "a", "/auto", "yes", "n", "no", "好"])
def test_the_words_an_approval_card_understands_are_only_text_here(text):
    """On an approval card ``s`` grants a session and ``/auto`` changes the
    mode. On a question they are what the person typed: an answer is not
    consent, and no reply to a question grants anything."""
    answer = read_reply(_CHOICE, text)

    assert answer == QuestionAnswer(AnswerStatus.ANSWERED, reply=text)


def test_a_number_reads_as_the_option_before_a_label_that_spells_it():
    """The tool refuses numeric labels; a request built some other way is
    still read one way, by position."""
    request = QuestionRequest("q", (QuestionOption("2"), QuestionOption("1")))

    assert read_reply(request, "1").selected == ("2",)


# ---- the boundary with the approval vocabulary ---------------------------------


def _imports(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_question_module_and_the_approval_modules_do_not_import_each_other():
    """Questions and approvals share the waiting and the display, and
    nothing else: neither broker knows the other, and the approval reply
    vocabulary (``entry/replies.py``, once it exists) and this module stay
    apart, so an approval verb can never be read on a question."""
    question = _imports(_ENTRY / "question.py")

    assert not {name for name in question if name.endswith((".approval", ".replies"))}
    assert "omicsclaw.entry.question" not in _imports(_ENTRY / "approval.py")
    assert "omicsclaw.entry.question" not in _imports(_ENTRY / "rendezvous.py")
    replies = _ENTRY / "replies.py"
    if replies.exists():
        assert not {name for name in _imports(replies) if name.endswith("question")}
