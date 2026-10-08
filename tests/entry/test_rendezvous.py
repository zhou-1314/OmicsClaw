"""``omicsclaw.entry.rendezvous``: the waiting that approvals and questions share.

The requests here are plain strings and so are the answers, because the
primitive must not care what is asked: an approval broker and a question
broker are both built on it, and a behaviour that only held for one of
their types would be a behaviour of that broker.

``tests/entry/test_approval.py`` runs the same mechanism through
:class:`~omicsclaw.entry.approval.ApprovalBroker` and is the proof that
extracting it changed nothing; this file covers what only the primitive
has: an injected counter, a caller-supplied expiry answer, and
:meth:`~omicsclaw.entry.rendezvous.Rendezvous.expires_at`.

Every await is bounded, because a defect here is a hang and this suite has
no timeout plugin.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from omicsclaw.entry.approval import ApprovalBroker
from omicsclaw.entry.events import TurnEvent, TurnEventType
from omicsclaw.entry.rendezvous import Rendezvous
from omicsclaw.entry.stream import TurnStream
from omicsclaw.entry.turn import TurnHandle
from omicsclaw.tools.context import ApprovalDecision, ApprovalRequest

WAIT_S = 2.0

EXPIRED = "nobody answered"


def _asked(request: str, request_id: str) -> TurnEvent:
    return TurnEvent(
        type=TurnEventType.APPROVAL_REQUIRED,
        seq=0,
        session_id="s1",
        turn_id="t1",
        request_id=request_id,
    )


def _settled(request_id: str, answer: str) -> TurnEvent:
    return TurnEvent(
        type=TurnEventType.APPROVAL_SETTLED,
        seq=0,
        session_id="s1",
        turn_id="t1",
        request_id=request_id,
    )


def _rendezvous(stream: TurnStream, **kwargs) -> Rendezvous[str, str]:
    return Rendezvous(
        stream, asked=_asked, settled=_settled, on_expired=lambda: EXPIRED, **kwargs
    )


def _ids(stream: TurnStream, kind: TurnEventType) -> list[str]:
    return [frame.request_id for frame in stream.retained() if frame.type is kind]


# ---- numbering -----------------------------------------------------------


def test_ids_count_from_one_under_the_turn_id_by_default():
    stream = TurnStream("s1", "t1")
    rendezvous = _rendezvous(stream)

    async def drive():
        first = asyncio.create_task(rendezvous.ask("a"))
        second = asyncio.create_task(rendezvous.ask("b"))
        await asyncio.sleep(0)
        pending = rendezvous.pending()
        for request_id in pending:
            rendezvous.settle(request_id, "ok")
        await asyncio.wait_for(asyncio.gather(first, second), WAIT_S)
        return pending

    assert asyncio.run(drive()) == ("t1#1", "t1#2")


def test_an_injected_counter_numbers_the_requests():
    """Mutation: ignore *numbering* in ``__init__`` and the id is ``t1#1``."""
    stream = TurnStream("s1", "t1")
    rendezvous = _rendezvous(stream, numbering=itertools.count(7))

    async def drive():
        asking = asyncio.create_task(rendezvous.ask("a"))
        await asyncio.sleep(0)
        pending = rendezvous.pending()
        rendezvous.settle(pending[0], "ok")
        await asyncio.wait_for(asking, WAIT_S)
        return pending

    assert asyncio.run(drive()) == ("t1#7",)


def test_two_rendezvous_sharing_a_counter_never_issue_the_same_id():
    """An approval and a question of one exchange are told apart by ``#n``
    alone at the CLI prompt, so the two brokers must not both issue ``#1``.

    Mutation: ignore *numbering* and the ids are ``t1#1, t1#1, t1#2``.
    """
    stream = TurnStream("s1", "t1")
    numbering = itertools.count(1)
    approvals = _rendezvous(stream, numbering=numbering)
    questions = _rendezvous(stream, numbering=numbering)

    async def drive():
        tasks = [
            asyncio.create_task(approvals.ask("a")),
            asyncio.create_task(questions.ask("q")),
            asyncio.create_task(approvals.ask("b")),
        ]
        await asyncio.sleep(0)
        seen = (approvals.pending(), questions.pending())
        for request_id in approvals.pending():
            approvals.settle(request_id, "ok")
        for request_id in questions.pending():
            questions.settle(request_id, "ok")
        await asyncio.wait_for(asyncio.gather(*tasks), WAIT_S)
        return seen

    assert asyncio.run(drive()) == (("t1#1", "t1#3"), ("t1#2",))
    assert _ids(stream, TurnEventType.APPROVAL_REQUIRED) == ["t1#1", "t1#2", "t1#3"]


# ---- answering -----------------------------------------------------------


def test_an_answer_reaches_the_asker_and_is_announced_once():
    stream = TurnStream("s1", "t1")
    rendezvous = _rendezvous(stream)

    async def drive():
        asking = asyncio.create_task(rendezvous.ask("a"))
        await asyncio.sleep(0)
        took = rendezvous.settle("t1#1", "yes")
        again = rendezvous.settle("t1#1", "no")
        unknown = rendezvous.settle("t1#9", "yes")
        return await asyncio.wait_for(asking, WAIT_S), took, again, unknown

    assert asyncio.run(drive()) == ("yes", True, False, False)
    assert _ids(stream, TurnEventType.APPROVAL_SETTLED) == ["t1#1"]
    assert rendezvous.pending() == ()


def test_an_expired_request_is_answered_by_the_caller_s_callback():
    """The primitive has no answer of its own: what "nobody answered" means
    is an approval's denial in one broker and a question's ``no_answer`` in
    the other."""
    stream = TurnStream("s1", "t1")
    rendezvous = _rendezvous(stream, timeout_s=0.01)

    async def drive():
        return await asyncio.wait_for(rendezvous.ask("a"), WAIT_S)

    assert asyncio.run(drive()) == EXPIRED
    assert _ids(stream, TurnEventType.APPROVAL_SETTLED) == ["t1#1"]
    assert rendezvous.pending() == ()


def test_a_cancelled_request_is_dropped_without_a_settlement_frame():
    """A cancelled exchange has no answer to report, and a surface learns
    the request is over from the exchange's own ending.

    Mutation: publish a settlement in the ``CancelledError`` branch of
    ``ask`` and the frame list below is no longer empty.
    """
    stream = TurnStream("s1", "t1")
    rendezvous = _rendezvous(stream)

    async def drive():
        asking = asyncio.create_task(rendezvous.ask("a"))
        await asyncio.sleep(0)
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asking, WAIT_S)
        return rendezvous.pending(), rendezvous.settle("t1#1", "late")

    assert asyncio.run(drive()) == ((), False)
    assert _ids(stream, TurnEventType.APPROVAL_SETTLED) == []


def test_abandoning_answers_every_outstanding_request_with_one_frame_each():
    stream = TurnStream("s1", "t1")
    rendezvous = _rendezvous(stream)

    async def drive():
        tasks = [asyncio.create_task(rendezvous.ask(name)) for name in "abc"]
        await asyncio.sleep(0)
        rendezvous.abandon("gone")
        rendezvous.abandon("gone again")
        return await asyncio.wait_for(asyncio.gather(*tasks), WAIT_S)

    assert asyncio.run(drive()) == ["gone", "gone", "gone"]
    assert _ids(stream, TurnEventType.APPROVAL_SETTLED) == ["t1#1", "t1#2", "t1#3"]
    assert rendezvous.pending() == ()


# ---- the deadline --------------------------------------------------------


def test_expires_at_is_the_loop_time_the_request_was_published_plus_the_timeout():
    stream = TurnStream("s1", "t1")
    rendezvous = _rendezvous(stream, timeout_s=30.0)

    async def drive():
        loop = asyncio.get_running_loop()
        before = loop.time()
        asking = asyncio.create_task(rendezvous.ask("a"))
        await asyncio.sleep(0)
        after = loop.time()
        deadline = rendezvous.expires_at("t1#1")
        rendezvous.settle("t1#1", "ok")
        await asyncio.wait_for(asking, WAIT_S)
        return before, deadline, after

    before, deadline, after = asyncio.run(drive())

    assert deadline is not None
    assert before + 30.0 <= deadline <= after + 30.0


def test_expires_at_is_none_without_a_deadline_for_an_unknown_id_and_once_answered():
    """Mutation: drop the ``done()`` check in ``expires_at`` and the
    answered request still reports its deadline."""
    stream = TurnStream("s1", "t1")
    waiting = _rendezvous(stream)
    timed = _rendezvous(stream, timeout_s=30.0, numbering=itertools.count(5))

    async def drive():
        untimed_task = asyncio.create_task(waiting.ask("a"))
        timed_task = asyncio.create_task(timed.ask("b"))
        await asyncio.sleep(0)
        without_deadline = waiting.expires_at("t1#1")
        unknown = timed.expires_at("t1#99")
        outstanding = timed.expires_at("t1#5")
        timed.settle("t1#5", "ok")
        answered_not_yet_resumed = timed.expires_at("t1#5")
        waiting.settle("t1#1", "ok")
        await asyncio.wait_for(asyncio.gather(untimed_task, timed_task), WAIT_S)
        return (
            without_deadline,
            unknown,
            outstanding,
            answered_not_yet_resumed,
            timed.expires_at("t1#5"),
        )

    without_deadline, unknown, outstanding, answered, resumed = asyncio.run(drive())

    assert without_deadline is None
    assert unknown is None
    assert outstanding is not None
    assert answered is None
    assert resumed is None


@pytest.mark.parametrize("timeout_s", [0, -5, -0.001])
def test_a_non_positive_timeout_is_refused_at_construction(timeout_s):
    """Mutation: drop the check in ``__init__`` and nothing is raised here,
    nor in ``test_approval.py``'s non-positive deadline test."""
    with pytest.raises(ValueError, match="timeout_s"):
        _rendezvous(TurnStream("s1", "t1"), timeout_s=timeout_s)


# ---- the approval broker passes both through ------------------------------


def test_the_approval_broker_uses_the_counter_it_is_given_and_reports_its_deadline():
    """What a surface needs from the broker to show a card that expires:
    an id that no other broker of the exchange issues, and when the
    question stops being answerable."""
    stream = TurnStream("s1", "t1")
    broker = ApprovalBroker(stream, timeout_s=30.0, numbering=itertools.count(4))

    async def drive():
        loop = asyncio.get_running_loop()
        asking = asyncio.create_task(broker(ApprovalRequest(tool_name="bash")))
        await asyncio.sleep(0)
        pending = broker.pending()
        deadline = broker.expires_at(pending[0])
        now = loop.time()
        broker.settle(pending[0], ApprovalDecision(approved=True))
        await asyncio.wait_for(asking, WAIT_S)
        return pending, deadline - now, broker.expires_at(pending[0])

    pending, remaining, afterwards = asyncio.run(drive())

    assert pending == ("t1#4",)
    assert 29.0 < remaining <= 30.0
    assert afterwards is None


def test_a_turn_handle_gives_its_approvals_the_exchange_s_own_counter():
    """The counter lives on the handle so that whatever else asks the
    person something during the exchange can draw from it too."""
    handle = TurnHandle(session_id="s1", turn_id="t1")

    async def drive():
        request = ApprovalRequest(tool_name="bash")
        asking = asyncio.create_task(handle.approvals(request))
        await asyncio.sleep(0)
        first = handle.approvals.pending()
        handle.approvals.abandon()
        await asyncio.wait_for(asking, WAIT_S)
        return first, next(handle._numbering)

    assert asyncio.run(drive()) == (("t1#1",), 2)
