"""``TurnOutcome.reply``: the text of this exchange, and of no earlier one.

The trajectory an exchange hands back starts with the history it was
given, so the answer of the exchange before it is in there. Each test
gives an exchange a conversation that already holds an answer and reads
the reply of the exchange that follows: in every shape that writes no
text, in every shape that writes some, for each way a history can
arrive, and under each tier of compaction.

Everything runs through the real composition root over a scripted
provider. The compaction tests size their window from the measured
prompt and tool table, so a longer system prompt does not move a
conversation into a different tier.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import pathlib

import pytest

from omicsclaw.context import (
    MISSING_TOOL_RESULT,
    ContextBudget,
    Pressure,
    estimate_messages_tokens,
    is_offloaded,
    is_summary_message,
    measure,
)
from omicsclaw.entry.session import InMemorySessionStore, Session, attach_sessions
from omicsclaw.entry.turn import run_turn
from omicsclaw.schema import Message, Role, ToolCall
from omicsclaw.tools import FunctionTool, ToolPolicy
from omicsclaw.tools.base import ApprovalMode, RiskLevel
from tests.entry.test_channel_ingress import (  # type: ignore[import-not-found]
    Transport,
)
from tests.entry.test_channel_runtime import (  # type: ignore[import-not-found]
    FIRST_ANSWER,
    WITH_TEXT,
    WITHOUT_TEXT,
    message as inbound,
    started,
    until,
)
from tests.entry.test_compaction_in_loop import (  # type: ignore[import-not-found]
    Failing,
    Table,
)
from tests.entry.test_session import Canned  # type: ignore[import-not-found]
from tests.entry.test_turn_runner import (  # type: ignore[import-not-found]
    CUT_ARGUMENTS,
    Finishing,
    Reporting,
    Scripted,
    make_app,
    requesting,
    tool_call,
)

WAIT_S = 30.0
"""Bounds every run. The longest scenario makes seventeen model calls."""

THROUGH = ["run_turn", "a session"]
"""The two ways an exchange is run: the blocking call, and a session's
:class:`~omicsclaw.entry.turn.TurnRunner` over the streaming one."""

_OPEN = ToolPolicy(
    risk_level=RiskLevel.LOW, approval_mode=ApprovalMode.AUTO, concurrency_safe=True
)


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, WAIT_S))


async def _converse(app, exchanges: int, *, through: str):
    """Run *exchanges* exchanges of one conversation; return their outcomes."""
    outcomes = []
    if through == "run_turn":
        history, state = (), None
        for index in range(exchanges):
            outcome = await run_turn(
                app, history, f"question {index}", state=state, session_id="s1"
            )
            history, state = outcome.history, outcome.state
            outcomes.append(outcome)
        return outcomes
    sessions = attach_sessions(app, store=InMemorySessionStore()).sessions
    for index in range(exchanges):
        handle = await sessions.submit("s1", f"question {index}")
        outcome = await handle.wait()
        assert outcome is not None, handle.error
        outcomes.append(outcome)
    return outcomes


# ---- the shapes an exchange can take ------------------------------------


@pytest.mark.parametrize("through", THROUGH)
@pytest.mark.parametrize("earlier", [0, 1], ids=["first exchange", "second exchange"])
@pytest.mark.parametrize("shape", sorted(WITHOUT_TEXT))
def test_an_exchange_without_text_has_no_reply(tmp_path, shape, earlier, through):
    """An exchange that wrote no text has an empty reply in either position.

    The shapes are the ones the channel tests send through the reply pump.
    As a second exchange each of them has the first one's answer in its
    trajectory.
    """
    replies, overrides, stop = WITHOUT_TEXT[shape]
    before = [Message.assistant(FIRST_ANSWER)] * earlier
    provider = Finishing(*before, *replies)
    app = make_app(tmp_path, provider, tools=(Reporting(),), **overrides)

    outcomes = _run(_converse(app, earlier + 1, through=through))

    assert [outcome.reply for outcome in outcomes[:-1]] == [FIRST_ANSWER] * earlier
    assert outcomes[-1].result.stop_reason is stop
    assert outcomes[-1].reply == ""


@pytest.mark.parametrize("through", THROUGH)
@pytest.mark.parametrize("shape", sorted(WITH_TEXT))
def test_an_exchange_replies_with_the_last_text_it_wrote(tmp_path, shape, through):
    """The reply is the text of the last turn of the exchange that has any."""
    replies, overrides, answer = WITH_TEXT[shape]
    provider = Finishing(Message.assistant(FIRST_ANSWER), *replies)
    app = make_app(tmp_path, provider, tools=(Reporting(),), **overrides)

    outcomes = _run(_converse(app, 2, through=through))

    assert [outcome.reply for outcome in outcomes] == [FIRST_ANSWER, answer]


def test_a_reply_of_only_whitespace_is_returned_as_it_is(tmp_path):
    """Any non-empty content counts as text, a blank and a newline included.

    Pinned so that changing it is deliberate. Whether such a reply should
    count as no text has not been decided.
    """
    provider = Finishing(Message.assistant(FIRST_ANSWER), Message.assistant(" \n"))
    app = make_app(tmp_path, provider)

    outcomes = _run(_converse(app, 2, through="run_turn"))

    assert outcomes[-1].reply == " \n"


# ---- how a history arrives ----------------------------------------------


ANSWERED = (Message.user("question 0"), Message.assistant(FIRST_ANSWER))
"""A conversation of one exchange, as a caller might hold it."""

NOT_A_TUPLE = {
    "a list": list,
    "a generator": lambda messages: (message for message in messages),
    "a deque": collections.deque,
}


@pytest.mark.parametrize("wrap", NOT_A_TUPLE.values(), ids=NOT_A_TUPLE.keys())
def test_a_history_in_any_container_is_told_from_what_the_exchange_adds(
    tmp_path, wrap
):
    """A list, a generator and a deque are read once and recognized after."""
    provider = Finishing(Message.assistant(""))
    app = make_app(tmp_path, provider)

    outcome = _run(run_turn(app, wrap(ANSWERED), "question 1"))

    assert provider.seen[0][1:3] == ANSWERED
    assert outcome.reply == ""


@pytest.mark.parametrize("through", THROUGH)
def test_a_turn_that_lost_an_unanswered_call_is_still_history(tmp_path, through):
    """A cut-off turn is rebuilt when the next exchange opens, and stays old.

    The first exchange is cut off inside a tool call after writing a
    sentence, which is its reply. The second exchange opens by taking the
    unanswered call off that turn, so the turn it starts from is a new
    message object with the same sentence. The second exchange writes
    nothing, and that sentence is not its reply.
    """
    cut = requesting(tool_call("w1", "write_file", CUT_ARGUMENTS), text="Writing.")
    provider = Finishing((cut, "length"), Message.assistant(""))
    app = make_app(tmp_path, provider)

    first, second = _run(_converse(app, 2, through=through))

    assert first.reply == "Writing."
    rebuilt = second.result.messages[2]
    assert rebuilt == Message.assistant("Writing.")
    assert rebuilt is not cut
    assert second.reply == ""


@pytest.mark.parametrize("through", THROUGH)
def test_a_message_object_the_provider_hands_back_again_is_a_new_reply(
    tmp_path, through
):
    """One message object returned for every call is heard every time.

    A scripted provider replays the object it was given, so from the
    second exchange on the reply is an object the history already holds.
    It is still what the exchange said.
    """
    same = Message.assistant("Same words.")
    app = make_app(tmp_path, Scripted(same))

    outcomes = _run(_converse(app, 3, through=through))

    assert all(outcome.result.messages[-1] is same for outcome in outcomes)
    assert [outcome.reply for outcome in outcomes] == ["Same words."] * 3


@pytest.mark.parametrize("through", THROUGH)
def test_of_a_repeated_message_object_the_later_one_is_the_reply(tmp_path, through):
    """The earlier occurrence is the one the history held.

    The second exchange writes a sentence beside a tool call, and its
    last turn is the message object that answered the first exchange.
    Its reply is that last turn. Counting the later occurrence as the
    history one would make the sentence the reply.
    """
    same = Message.assistant("Same words.")
    looking = requesting(tool_call("c0"), text="Let me look.")
    app = make_app(tmp_path, Scripted(same, looking, same), tools=(Reporting(),))

    first, second = _run(_converse(app, 2, through=through))

    assert [m for m in second.result.messages if m is same] == [same, same]
    assert second.result.messages[-1] is same
    assert (first.reply, second.reply) == ("Same words.", "Same words.")


@pytest.mark.parametrize("through", THROUGH)
def test_the_same_words_said_again_are_this_exchanges_reply(tmp_path, through):
    """Two exchanges answer with equal text in two message objects."""
    provider = Finishing(*(Message.assistant("Same words.") for _ in range(2)))
    app = make_app(tmp_path, provider)

    outcomes = _run(_converse(app, 2, through=through))

    assert outcomes[0].result.messages[-1] is not outcomes[1].result.messages[-1]
    assert [outcome.reply for outcome in outcomes] == ["Same words."] * 2


def test_a_conversation_read_back_from_disk_is_history(tmp_path):
    """Two apps over one workspace, which is what a restart looks like."""

    async def one_exchange(provider, text):
        app = attach_sessions(make_app(tmp_path, provider))
        try:
            assert app.sessions.persistent
            handle = await app.sessions.submit("s1", text)
            return await handle.wait()
        finally:
            await app.aclose()

    first = _run(one_exchange(Finishing(Message.assistant(FIRST_ANSWER)), "question 0"))
    after_restart = Finishing(Message.assistant(""))
    second = _run(one_exchange(after_restart, "question 1"))

    assert first.reply == FIRST_ANSWER
    assert after_restart.seen[0][1:3] == ANSWERED, "the stored conversation came back"
    assert second.reply == ""


# ---- compaction ---------------------------------------------------------


def _dump(size: int = 4) -> FunctionTool:
    """A ``dump`` tool that answers with *size* characters."""
    return FunctionTool(
        "dump",
        "dump a table",
        lambda: "k" * size,
        parameters={"type": "object", "properties": {}},
        policy=_OPEN,
    )


def _budget(tokens: int) -> ContextBudget:
    return ContextBudget(
        context_tokens=tokens,
        reserve_output_tokens=0,
        reserve_tool_tokens=0,
        safety_ratio=0.0,
    )


def _sized(
    tmp_path: pathlib.Path,
    provider,
    *,
    room: int,
    summarizer=None,
    tool=None,
    **overrides,
):
    """An app whose window has *room* tokens after the prompt and the tools.

    The system prompt and the tool table are measured first and the
    window is that plus *room*, so the tier a conversation lands in
    depends on the conversation alone.
    """
    app = make_app(
        tmp_path, provider, tools=(tool or _dump(),), memory=False, **overrides
    )
    wide = dataclasses.replace(app, budget=_budget(10**9))
    floor = measure(
        (Message.system(wide.prompt.render().system_prompt),),
        wide.tools_snapshot,
        wide.budget,
    )
    return dataclasses.replace(
        app,
        budget=_budget(floor.message_tokens + floor.tool_tokens + room),
        summarizer=summarizer,
    )


def _step(index: int, text: str = "") -> Message:
    """One turn of the exchange under test: *text*, and a call to ``dump``."""
    return Message.assistant(
        text, tool_calls=(ToolCall(id=f"c{index}", name="dump", arguments="{}"),)
    )


def _steps(count: int, *, start: int = 0) -> list[Message]:
    return [_step(index) for index in range(start, start + count)]


def _earlier(pairs: int = 0) -> tuple[Message, ...]:
    """A conversation that ends on the first answer, after *pairs* older exchanges."""
    messages: list[Message] = []
    for index in range(pairs):
        messages.append(Message.user(f"old question {index} " + "q" * 300))
        messages.append(Message.assistant(f"old answer {index} " + "a" * 300))
    return (*messages, *ANSWERED)


def _room_for(history: tuple[Message, ...], *, at: float) -> int:
    """The room that puts *history* at the fraction *at* of the window."""
    return int(estimate_messages_tokens(history) / at)


def _exchange(app, history=None, text: str = "question 1"):
    """Run one exchange on top of *history*, which ends on the first answer."""
    history = _earlier() if history is None else history
    return _run(run_turn(app, history, text, session_id="s1"))


def _has(outcome, text: str) -> bool:
    """Whether a message of the trajectory starts with *text*."""
    return any(m.content.startswith(text) for m in outcome.result.messages)


def _kept(outcome) -> list:
    """The compactions of the exchange whose result replaced its history."""
    return [record for record in outcome.compactions if record.written_back]


@pytest.mark.parametrize("final", ["", "SECOND ANSWER."], ids=["no text", "an answer"])
def test_offloading_tool_results_leaves_the_reply_alone(tmp_path, final):
    """Tool results swapped for placeholders, written back, mid-exchange."""
    provider = Finishing(*_steps(6), Message.assistant(final))
    app = _sized(tmp_path, provider, room=14_000, tool=Table().tool())

    outcome = _exchange(app)

    assert any(record.offloaded for record in _kept(outcome))
    assert any(is_offloaded(m) for m in outcome.result.messages)
    assert _has(outcome, FIRST_ANSWER)
    assert outcome.reply == final


TIERS = {"soft": (Pressure.SOFT, 0.75), "full": (Pressure.FULL, 0.88)}
"""A summarizing tier, and a fraction of the window that falls inside it."""


@pytest.mark.parametrize("tier", TIERS)
def test_a_summary_that_keeps_the_earlier_answer_does_not_make_it_the_reply(
    tmp_path, tier
):
    """A summary replaces the older exchanges; the first answer stays in the tail.

    The compaction runs before the exchange's only model call and is
    written back, so the trajectory is the summary, a few old messages,
    the first answer, the request and an empty reply.
    """
    pressure, at = TIERS[tier]
    history = _earlier(pairs=20)
    app = _sized(
        tmp_path,
        Finishing(Message.assistant("")),
        room=_room_for(history, at=at),
        summarizer=Canned(),
    )

    outcome = _exchange(app, history)

    (record,) = outcome.compactions
    assert record.pressure is pressure
    assert record.summarized and record.written_back and not record.degraded
    assert is_summary_message(outcome.result.messages[1])
    assert len(outcome.result.messages) < len(history)
    assert _has(outcome, FIRST_ANSWER)
    assert outcome.reply == ""


@pytest.mark.parametrize("tier", TIERS)
def test_an_answer_given_after_a_summary_was_written_back_is_the_reply(
    tmp_path, tier
):
    """The same summary, with an answer after it: the answer is the reply.

    Written back, the summary leaves the trajectory shorter than the
    history the exchange was given. The answer is the last message, at
    an index below the length of that history, so a search that skips as
    many messages as the history held finds nothing.
    """
    pressure, at = TIERS[tier]
    history = _earlier(pairs=20)
    app = _sized(
        tmp_path,
        Finishing(Message.assistant("SECOND ANSWER.")),
        room=_room_for(history, at=at),
        summarizer=Canned(),
    )

    outcome = _exchange(app, history)

    (record,) = outcome.compactions
    assert record.pressure is pressure
    assert record.summarized and record.written_back and not record.degraded
    assert len(outcome.result.messages) < len(history)
    assert _has(outcome, FIRST_ANSWER)
    assert outcome.reply == "SECOND ANSWER."


def test_a_reply_that_repeats_a_summarized_answer_word_for_word_is_the_reply(
    tmp_path,
):
    """Messages are told apart as objects, whatever they say.

    The exchange answers in the exact words of an old answer that the
    summary has just replaced. It is a new message and it is the reply.
    """
    history = _earlier(pairs=20)
    old_answer = history[1]
    app = _sized(
        tmp_path,
        Finishing(Message.assistant(old_answer.content)),
        room=_room_for(history, at=TIERS["full"][1]),
        summarizer=Canned(),
    )

    outcome = _exchange(app, history)

    (record,) = outcome.compactions
    assert record.summarized and record.written_back
    assert [m for m in outcome.result.messages if m == old_answer] == [
        outcome.result.messages[-1]
    ]
    assert outcome.result.messages[-1] is not old_answer
    assert outcome.reply == old_answer.content


@pytest.mark.parametrize(
    "summarizer", [Failing, None], ids=["the summarizer fails", "no summarizer"]
)
@pytest.mark.parametrize("tier", TIERS)
def test_a_truncation_that_is_not_kept_does_not_change_the_reply(
    tmp_path, tier, summarizer
):
    """Without a summary the call is sent a truncated view and the history stays."""
    pressure, at = TIERS[tier]
    history = _earlier(pairs=20)
    app = _sized(
        tmp_path,
        Finishing(Message.assistant("")),
        room=_room_for(history, at=at),
        summarizer=summarizer() if summarizer is not None else None,
    )

    outcome = _exchange(app, history)

    (record,) = outcome.compactions
    assert record.pressure is pressure
    assert record.degraded and not record.written_back
    assert outcome.history[: len(history)] == history
    assert outcome.reply == ""


OVERSIZED = "question 1 " + "p" * 40_000
"""A request as large as the whole room of the window it is sent into."""

AFTER_AN_OVERSIZED_REQUEST = {
    "no text": ((Message.assistant(""),), ""),
    "a tool call and no text": ((_step(0), Message.assistant("")), ""),
    "an answer": ((Message.assistant("SECOND ANSWER."),), "SECOND ANSWER."),
}
"""What the model says once the request has been dropped, and the reply."""


def _check_the_request_was_dropped(outcome) -> None:
    """One emergency truncation dropped the request and kept the earlier answer."""
    (record,) = _kept(outcome)
    assert record.pressure is Pressure.EMERGENCY
    assert not _has(outcome, "question 1"), "the request is gone"
    assert not any(is_summary_message(m) for m in outcome.result.messages)
    assert _has(outcome, FIRST_ANSWER)


@pytest.mark.parametrize("shape", AFTER_AN_OVERSIZED_REQUEST)
def test_an_emergency_truncation_that_drops_the_request(tmp_path, shape):
    """The request does not fit and is dropped; the earlier answer fits and stays.

    Nothing in the trajectory marks where this exchange began. Its reply
    is still only what it wrote.
    """
    replies, reply = AFTER_AN_OVERSIZED_REQUEST[shape]
    app = _sized(tmp_path, Finishing(*replies), room=10_000, summarizer=Canned())

    outcome = _exchange(app, text=OVERSIZED)

    _check_the_request_was_dropped(outcome)
    assert outcome.reply == reply


@pytest.mark.parametrize("shape", AFTER_AN_OVERSIZED_REQUEST)
def test_a_channel_sends_no_earlier_answer_after_the_request_was_dropped(
    tmp_path, shape
):
    """The same truncation, through ``ChannelRuntime`` and its reply pump.

    The first message is answered. The second is too large for the
    window, so the truncation drops it and keeps the first answer. The
    chat is sent what the second exchange wrote, and the first answer
    once.
    """
    replies, reply = AFTER_AN_OVERSIZED_REQUEST[shape]
    provider = Finishing(Message.assistant(FIRST_ANSWER), *replies)

    async def scenario():
        transport = Transport()
        app = attach_sessions(
            _sized(
                tmp_path,
                provider,
                room=10_000,
                summarizer=Canned(),
                approval_timeout_s=WAIT_S,
            ),
            store=InMemorySessionStore(),
        )
        runtime = await started(app, transport)
        for index, text in enumerate(("question 0", OVERSIZED)):
            result = await runtime.submit(inbound(text, request=f"r{index}"))
            handle = result.handle
            assert handle is not None
            await handle.wait()
            await until(
                lambda: handle.turn_id not in runtime._replies, timeout=WAIT_S
            )
        await runtime.close(WAIT_S)
        return transport, handle

    transport, last = _run(scenario())

    assert last.terminal == "converged"
    _check_the_request_was_dropped(last.outcome)
    assert transport.sent == [FIRST_ANSWER, *([reply] if reply else [])]


def test_an_emergency_truncation_mid_exchange_with_placeholders(tmp_path):
    """The summarizer is down, so the history grows until it is truncated.

    The truncation drops tool results of this exchange and the repair
    answers their calls with placeholders. The request and the earlier
    answer both survive, and the exchange wrote no text.
    """
    provider = Finishing(*_steps(14), Message.assistant(""))
    app = _sized(
        tmp_path, provider, room=10_000, summarizer=Failing(), tool=_dump(3_600)
    )

    outcome = _exchange(app)

    assert Pressure.EMERGENCY in [record.pressure for record in _kept(outcome)]
    assert _has(outcome, MISSING_TOOL_RESULT)
    assert _has(outcome, "question 1")
    assert _has(outcome, FIRST_ANSWER)
    assert outcome.reply == ""


def test_text_the_exchange_wrote_and_a_summary_replaced_is_not_the_reply(tmp_path):
    """An early sentence, then sixteen silent turns and several summaries.

    The summaries replace the request and the turn that carried the
    sentence, so the text is no longer in the trajectory. The reply is
    empty: the earlier answer was summarized too, and nothing is read
    back out of a summary.
    """
    provider = Finishing(
        _step(0, "Early words."), *_steps(15, start=1), Message.assistant("")
    )
    app = _sized(
        tmp_path, provider, room=10_000, summarizer=Canned(), tool=_dump(3_600)
    )

    outcome = _exchange(app)

    assert any(record.summarized for record in _kept(outcome))
    assert not _has(outcome, "Early words.")
    assert not _has(outcome, "question 1")
    assert outcome.reply == ""


def test_text_the_exchange_wrote_after_the_last_summary_is_the_reply(tmp_path):
    """The same run with the sentence beside the last call: it is still there."""
    provider = Finishing(
        *_steps(15), _step(15, "Late words."), Message.assistant("")
    )
    app = _sized(
        tmp_path, provider, room=10_000, summarizer=Canned(), tool=_dump(3_600)
    )

    outcome = _exchange(app)

    assert any(record.summarized for record in _kept(outcome))
    assert not _has(outcome, "question 1")
    assert outcome.reply == "Late words."


def test_text_the_exchange_wrote_and_a_truncation_dropped_is_not_the_reply(tmp_path):
    """A long early sentence is dropped while the short earlier answer stays.

    An emergency truncation keeps the newest messages that fit and skips
    the ones that do not, so it can drop a large turn of this exchange
    and keep a small answer from the one before. The reply is empty;
    that answer belongs to the earlier exchange.
    """
    early = _step(0, "Early words. " + "t" * 14_000)
    provider = Finishing(early, *_steps(11, start=1), Message.assistant(""))
    app = _sized(
        tmp_path, provider, room=10_000, summarizer=Failing(), tool=_dump(3_600)
    )

    outcome = _exchange(app)

    assert Pressure.EMERGENCY in [record.pressure for record in _kept(outcome)]
    assert not _has(outcome, "Early words.")
    assert _has(outcome, FIRST_ANSWER)
    assert outcome.reply == ""


# ---- /compact -----------------------------------------------------------


STUCK = (
    Message.user("write the notes"),
    requesting(tool_call("w1", "write_file", CUT_ARGUMENTS), text="Writing."),
)
"""An exchange that was cut off inside a tool call after one sentence."""


@pytest.mark.parametrize(
    ("summarizer", "at", "written_back"),
    [(Canned, 0.05, True), (Failing, 0.72, False)],
    ids=["written back", "not written back"],
)
@pytest.mark.parametrize("tail", [(), STUCK], ids=["answered", "cut off"])
def test_a_compaction_only_exchange_has_no_reply(
    tmp_path, tail, summarizer, at, written_back
):
    """``/compact`` runs no model turn, so the exchange said nothing.

    The conversation it leaves still holds earlier answers, whether or
    not the summary was written back.
    """
    history = (*_earlier(pairs=20), *tail)
    provider = Finishing(Message.assistant("never asked"))
    app = attach_sessions(
        _sized(
            tmp_path,
            provider,
            room=_room_for(history, at=at),
            summarizer=summarizer(),
        ),
        store=InMemorySessionStore(),
    )

    async def drive():
        await app.sessions._store.save(Session(session_id="s1", history=history))
        handle = await app.sessions.compact("s1")
        return await handle.wait()

    outcome = _run(drive())

    assert provider.calls == 0
    assert outcome.compaction.forced
    assert outcome.compaction.written_back is written_back
    assert any(m.role is Role.ASSISTANT and m.content for m in outcome.history)
    assert outcome.reply == ""
