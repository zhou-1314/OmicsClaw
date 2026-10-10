"""Re-injection before every model call, and the planning gate."""

from __future__ import annotations

import asyncio

import pytest

from omicsclaw.planning import (
    DEFAULT_GATE_TURNS,
    INJECTION_HEADER,
    PLANNING_GATE_TEXT,
    PROGRESS_TOOL_NAMES,
    PlanBook,
    PlanInjector,
    PlanItem,
    PlanStatus,
)
from omicsclaw.schema import Message, Role, ToolCall

P, R, C = PlanStatus.PENDING, PlanStatus.IN_PROGRESS, PlanStatus.COMPLETED


def _store(*items: PlanItem):
    store = PlanBook().for_session("s")
    if items:
        store.write(items)
    return store


def _augment(injector: PlanInjector, history=()) -> tuple[Message, ...]:
    return asyncio.run(injector.augment(tuple(history), ()))


def _acted(*names: str) -> Message:
    return Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=tuple(
            ToolCall(id=f"c{n}", name=name, arguments="{}")
            for n, name in enumerate(names)
        ),
    )


def _said(text: str = "thinking") -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def _turns(*turns: Message) -> tuple[Message, ...]:
    """Model turns as the engine records them, each followed by one
    ``Role.TOOL`` result for every call it made."""
    messages: list[Message] = []
    for turn in turns:
        messages.append(turn)
        messages.extend(
            Message.tool(tool_call_id=call.id, name=call.name, content="…output…")
            for call in turn.tool_calls
        )
    return tuple(messages)


def _exchange(*turns: Message, ask: str = "do the analysis") -> tuple[Message, ...]:
    """One exchange as the engine records it: the user's message, then
    the model's turns with their results."""
    return (Message.user(ask), *_turns(*turns))


def _reads(count: int) -> tuple[Message, ...]:
    return tuple(_acted("read_file") for _ in range(count))


def _read_only_turns(count: int) -> tuple[Message, ...]:
    """One exchange in which the model read a file *count* times."""
    return _exchange(*_reads(count))


ASK = "ask_user"

ENDINGS: dict[str, tuple[str, bool] | None] = {
    "answered": (
        '{"status": "answered", "question": "Which group is the control?",'
        ' "selected": ["group A"], "reply": "1"}',
        False,
    ),
    "skipped": (
        '{"status": "declined", "question": "Which group is the control?",'
        ' "note": "The person chose not to answer."}',
        False,
    ),
    "deadline passed": (
        '{"status": "no_answer", "question": "Which group is the control?",'
        ' "reason": "nobody answered in time", "note": "Nobody answered."}',
        False,
    ),
    "refused": ("ask_user: nobody can be asked here", True),
    "result compacted away": (
        "[tool result unavailable: the context was compacted]",
        False,
    ),
    "no result recorded": None,
}
"""How a question can end, as the history shows it: the result's text and
whether it is an error. ``None`` leaves the call without a result."""


def _question(
    ending: str = "answered",
    *,
    before: tuple[str, ...] = (),
    after: tuple[str, ...] = (),
) -> tuple[Message, ...]:
    """One turn that called ``ask_user``, with what the history holds after it.

    *before* and *after* name other tools the same message called, ahead
    of the question and behind it.
    """
    turn = _acted(*before, ASK, *after)
    result = ENDINGS[ending]
    messages = [turn]
    for call in turn.tool_calls:
        if call.name != ASK:
            content, is_error = "…output…", False
        elif result is not None:
            content, is_error = result
        else:
            continue
        messages.append(
            Message.tool(
                tool_call_id=call.id,
                name=call.name,
                content=content,
                is_error=is_error,
            )
        )
    return tuple(messages)


# ---- the plan block ------------------------------------------------------


def test_no_plan_means_nothing_is_appended():
    assert _augment(PlanInjector(_store(), gate_turns=0)) == ()


def test_a_finished_plan_appends_nothing():
    store = _store(PlanItem("1", "done", C))

    assert _augment(PlanInjector(store, gate_turns=0)) == ()


def test_an_outstanding_plan_is_appended_as_one_user_message():
    store = _store(PlanItem("1", "load the matrix", R))

    appended = _augment(PlanInjector(store, gate_turns=0))

    assert len(appended) == 1
    assert appended[0].role is Role.USER
    assert appended[0].content.startswith(INJECTION_HEADER)
    assert "load the matrix" in appended[0].content


def test_the_block_is_recomputed_every_call_so_a_write_shows_up_next_turn():
    store = _store(PlanItem("1", "first", R))
    injector = PlanInjector(store, gate_turns=0)
    _augment(injector)

    store.write((PlanItem("1", "first", C), PlanItem("2", "second", R)))

    assert "second" in _augment(injector)[0].content


def test_nothing_accumulates_across_calls():
    """Each call starts from the conversation again; the block is not history."""
    store = _store(PlanItem("1", "step", R))
    injector = PlanInjector(store, gate_turns=0)

    first = _augment(injector)
    second = _augment(injector)

    assert first == second
    assert len(second) == 1


def test_the_injector_never_writes_to_the_plan():
    store = _store(PlanItem("1", "step", R))
    before = store.read()

    _augment(PlanInjector(store, gate_turns=0), _read_only_turns(20))

    assert store.read() == before


# ---- the planning gate ---------------------------------------------------


def test_the_gate_fires_after_enough_read_only_turns():
    injector = PlanInjector(_store(), gate_turns=3)

    appended = _augment(injector, _read_only_turns(3))

    assert [m.content for m in appended] == [PLANNING_GATE_TEXT]


def test_the_gate_does_not_fire_early():
    injector = PlanInjector(_store(), gate_turns=3)

    assert _augment(injector, _read_only_turns(2)) == ()


def test_the_gate_fires_at_most_once_per_exchange():
    injector = PlanInjector(_store(), gate_turns=2)

    assert _augment(injector, _read_only_turns(5)) != ()
    assert _augment(injector, _read_only_turns(9)) == ()


def test_a_session_that_already_has_a_plan_is_never_nudged():
    """It is being told about the plan every turn; nudging it is confusing.

    This also covers a plan restored from a previous session, which the
    reference harness's in-engine counter cannot see.
    """
    injector = PlanInjector(_store(PlanItem("1", "step", R)), gate_turns=2)

    appended = _augment(injector, _read_only_turns(9))

    assert len(appended) == 1
    assert appended[0].content.startswith(INJECTION_HEADER)


def test_a_completed_plan_still_counts_as_having_planned():
    injector = PlanInjector(_store(PlanItem("1", "step", C)), gate_turns=2)

    assert _augment(injector, _read_only_turns(9)) == ()


def test_a_recent_write_disarms_the_gate():
    injector = PlanInjector(_store(), gate_turns=3)
    history = _exchange(*_reads(3), _acted("write_file"))

    assert _augment(injector, history) == ()


def test_a_recent_plan_write_disarms_the_gate():
    injector = PlanInjector(_store(), gate_turns=3)
    history = _exchange(*_reads(3), _acted("plan_write"))

    assert _augment(injector, history) == ()


def test_a_write_before_the_window_does_not_disarm_the_gate():
    """Only the last ``gate_turns`` turns are examined. A write earlier in
    the same exchange leaves the gate armed."""
    injector = PlanInjector(_store(), gate_turns=3)
    history = _exchange(_acted("write_file"), *_reads(3))

    assert _augment(injector, history) != ()


def test_bash_does_not_count_as_progress():
    """It is how you run a script and how you grep; counting it disarms
    the gate for a model that is only exploring."""
    injector = PlanInjector(_store(), gate_turns=2)
    history = _exchange(_acted("bash"), _acted("bash"), _acted("bash"))

    assert _augment(injector, history) != ()


def test_progress_tools_are_the_two_that_change_the_workspace():
    assert PROGRESS_TOOL_NAMES == frozenset({"write_file", "edit_file"})


def test_a_turn_that_only_talked_counts_as_a_turn():
    """Every assistant message is one turn, with or without a tool call.

    The engine ends a run at a turn that calls no tool, so a later call
    finds one behind it only when a caller continues the conversation
    without a new user message. The request is then still the same one,
    and the turn that only talked is one more turn spent on it.
    """
    injector = PlanInjector(_store(), gate_turns=3)
    resumed = _exchange(*_reads(2), _said("still looking"))

    assert _augment(injector, resumed) != ()


def test_a_turn_with_parallel_calls_counts_once():
    """Two turns of two read-only calls each are two turns, one short of
    the threshold."""
    injector = PlanInjector(_store(), gate_turns=3)
    history = _exchange(
        _acted("read_file", "read_file"), _acted("read_file", "read_file")
    )

    assert _augment(injector, history) == ()


def test_a_history_with_no_user_message_is_counted_whole():
    injector = PlanInjector(_store(), gate_turns=2)

    assert _augment(injector, (_acted("bash"), _acted("bash"))) != ()


# ---- which turns the gate counts -----------------------------------------


def test_one_line_answers_in_earlier_exchanges_do_not_arm_the_gate():
    """A chat of short answers has no exploration to interrupt.

    Each earlier exchange is one question and one turn that only talked.
    None of those turns belongs to the request the model is about to
    answer.
    """
    injector = PlanInjector(_store(), gate_turns=3)
    chat = _exchange(_said("2"), ask="1+1?") * 5

    assert _augment(injector, (*chat, *_exchange(ask="and 2+3?"))) == ()


def test_reading_in_earlier_exchanges_does_not_arm_the_gate():
    injector = PlanInjector(_store(), gate_turns=3)
    earlier = _exchange(*_reads(6), _said("found it"))

    assert _augment(injector, (*earlier, *_exchange(ask="and the other file?"))) == ()


def test_a_new_exchange_starts_its_own_count():
    """One turn short of the threshold, whatever the session did before."""
    injector = PlanInjector(_store(), gate_turns=3)
    earlier = _exchange(*_reads(6), _said("found it"))

    assert _augment(injector, (*earlier, *_read_only_turns(2))) == ()


def test_enough_read_only_turns_in_a_later_exchange_fire_the_gate():
    injector = PlanInjector(_store(), gate_turns=3)
    earlier = _exchange(*_reads(6), _said("found it"))

    appended = _augment(injector, (*earlier, *_read_only_turns(3)))

    assert [m.content for m in appended] == [PLANNING_GATE_TEXT]


def test_a_write_in_an_earlier_exchange_does_not_disarm_this_one():
    injector = PlanInjector(_store(), gate_turns=3)
    earlier = _exchange(_acted("write_file"), _said("written"))

    assert _augment(injector, (*earlier, *_read_only_turns(3))) != ()


def test_reading_after_a_write_in_an_earlier_exchange_is_not_counted_here():
    """The earlier exchange wrote a file, read twice and answered.

    Its last three turns hold no write. They are still not this
    exchange's turns, and this exchange has made one.
    """
    injector = PlanInjector(_store(), gate_turns=3)
    earlier = _exchange(_acted("write_file"), *_reads(2), _said("checked"))

    assert _augment(injector, (*earlier, *_read_only_turns(1))) == ()


def test_turns_after_a_compaction_summary_are_counted():
    """A summary stands where the request was once compaction replaced it.

    The turns kept after it are the ones the model can still see, and
    enough of them fire the gate.
    """
    injector = PlanInjector(_store(), gate_turns=3)
    summary = Message.user("[Context Compaction]\n## Anchors\n…")
    kept = _read_only_turns(3)[1:]

    assert _augment(injector, (summary, *kept)) != ()


# ---- a question to the person --------------------------------------------


@pytest.mark.parametrize("ending", ENDINGS)
def test_a_question_restarts_the_count_however_it_ended(ending):
    """Two turns read, one asked, two more read.

    Only the two after the question are counted, whatever the history
    holds as the question's result.
    """
    injector = PlanInjector(_store(), gate_turns=3)
    history = (*_exchange(*_reads(2)), *_question(ending), *_turns(*_reads(2)))

    assert _augment(injector, history) == ()


@pytest.mark.parametrize("ending", ENDINGS)
def test_enough_read_only_turns_after_a_question_fire_the_gate(ending):
    injector = PlanInjector(_store(), gate_turns=3)
    history = (*_exchange(*_reads(2)), *_question(ending), *_turns(*_reads(3)))

    appended = _augment(injector, history)

    assert [m.content for m in appended] == [PLANNING_GATE_TEXT]


def test_the_calls_after_a_question_one_by_one():
    """The question is the third turn of the exchange, where the count
    would have been complete.

    The call that carries the answer has no nudge, and neither have the
    two after it. The fourth has, with three turns of reading behind it,
    and the fifth has none because the exchange has had its one.
    """
    injector = PlanInjector(_store(), gate_turns=3)
    history = [*_exchange(*_reads(2)), *_question()]

    nudged = []
    for _ in range(5):
        nudged.append(bool(_augment(injector, history)))
        history.extend(_turns(_acted("read_file")))

    assert nudged == [False, False, False, True, False]


def test_a_question_that_opens_the_exchange_is_not_a_turn_of_reading():
    """The turn that asked is counted no more than the user's message is:
    the first turn after either one is turn one."""
    asked_first = (Message.user("compare the two groups"), *_question())

    short = (*asked_first, *_turns(*_reads(2)))
    enough = (*asked_first, *_turns(*_reads(3)))

    assert _augment(PlanInjector(_store(), gate_turns=3), short) == ()
    assert _augment(PlanInjector(_store(), gate_turns=3), enough) != ()


def test_the_count_runs_from_the_latest_question():
    """Three turns of reading follow the first question and two follow
    the second."""
    history = (
        Message.user("compare the two groups"),
        *_question(),
        *_turns(*_reads(3)),
        *_question("skipped"),
        *_turns(*_reads(2)),
    )

    assert _augment(PlanInjector(_store(), gate_turns=3), history) == ()
    assert (
        _augment(
            PlanInjector(_store(), gate_turns=3),
            (*history, *_turns(_acted("read_file"))),
        )
        != ()
    )


@pytest.mark.parametrize(
    "others",
    [
        {"before": ("read_file",)},
        {"after": ("read_file",)},
        {"before": ("bash",), "after": ("read_file", "read_file")},
    ],
    ids=["a call before it", "a call after it", "calls on both sides"],
)
def test_a_message_that_asked_and_called_other_tools_restarts_the_count(others):
    """The whole message is one turn, wherever the question sits in it."""
    asked = (*_exchange(*_reads(2)), *_question(**others))

    short = (*asked, *_turns(*_reads(2)))
    enough = (*asked, *_turns(*_reads(3)))

    assert _augment(PlanInjector(_store(), gate_turns=3), short) == ()
    assert _augment(PlanInjector(_store(), gate_turns=3), enough) != ()


def test_two_questions_in_one_message_restart_the_count_once():
    asked = (*_exchange(*_reads(2)), *_turns(_acted(ASK, ASK)))

    short = (*asked, *_turns(*_reads(2)))
    enough = (*asked, *_turns(*_reads(3)))

    assert _augment(PlanInjector(_store(), gate_turns=3), short) == ()
    assert _augment(PlanInjector(_store(), gate_turns=3), enough) != ()


def test_a_question_brings_no_second_nudge_in_the_same_exchange():
    """One nudge to an exchange, also when the model asks after it."""
    injector = PlanInjector(_store(), gate_turns=3)
    before = _read_only_turns(3)
    assert _augment(injector, before) != ()

    after = (*before, *_question(), *_turns(*_reads(3)))

    assert _augment(injector, after) == ()


def test_only_a_call_to_ask_user_restarts_the_count():
    """Tools whose names begin or end with ``ask_user``, an MCP server's
    own ``ask_user`` among them, and a result that quotes the name are
    an ordinary turn each."""
    injector = PlanInjector(_store(), gate_turns=3)
    quoting = (
        _acted("read_file"),
        Message.tool(
            tool_call_id="c0",
            name="read_file",
            content='ask_user returned {"status": "answered"}',
        ),
    )
    history = (
        *_exchange(_acted("mcp__lab__ask_user"), _acted("ask_user_group")),
        *quoting,
    )

    assert _augment(injector, history) != ()


def test_a_delegation_is_one_turn_like_any_other():
    """A sub-agent's turns are not in this history. The ``task`` call and
    its result are all the gate sees of them."""
    delegated = _acted("task")

    short = _exchange(delegated, delegated)
    enough = _exchange(delegated, delegated, delegated)

    assert _augment(PlanInjector(_store(), gate_turns=3), short) == ()
    assert _augment(PlanInjector(_store(), gate_turns=3), enough) != ()


def test_a_question_in_an_earlier_exchange_changes_nothing_here():
    earlier = (*_exchange(*_reads(1)), *_question(), _said("group A it is"))

    short = (*earlier, *_read_only_turns(2))
    enough = (*earlier, *_read_only_turns(3))

    assert _augment(PlanInjector(_store(), gate_turns=3), short) == ()
    assert _augment(PlanInjector(_store(), gate_turns=3), enough) != ()


def test_a_history_with_no_user_message_is_counted_back_to_a_question():
    injector = PlanInjector(_store(), gate_turns=3)
    kept = (*_turns(*_reads(4)), *_question(), *_turns(*_reads(2)))

    assert _augment(injector, kept) == ()


def test_zero_turns_disables_the_gate_and_leaves_the_block():
    store = _store(PlanItem("1", "step", R))
    injector = PlanInjector(store, gate_turns=0)

    appended = _augment(injector, _read_only_turns(30))

    assert len(appended) == 1
    assert appended[0].content.startswith(INJECTION_HEADER)


def test_zero_or_fewer_turns_disable_the_gate_when_there_is_no_plan():
    """With no plan and a long read-only exchange, only the setting keeps
    the gate quiet."""
    for turns in (0, -1):
        injector = PlanInjector(_store(), gate_turns=turns)

        assert _augment(injector, _read_only_turns(30)) == ()


def test_a_history_shortened_by_compaction_does_not_fire_the_gate():
    """Named in plan 0039 §4.4 rather than fixed.

    A compaction has just handed the model a fresh summary, which is the
    one turn on which an extra instruction is least likely to help.
    """
    injector = PlanInjector(_store(), gate_turns=4)
    compacted = (Message(role=Role.USER, content="[summary of 20 turns]"),)

    assert _augment(injector, compacted) == ()


def test_the_nudge_names_the_tool_it_is_asking_for():
    assert "plan_write" in PLANNING_GATE_TEXT


def test_the_default_gate_is_a_fraction_of_the_turn_budget():
    """Re-derived from ``EngineConfig.max_turns``, not ported from 12.

    If the budget moves, this number should be reconsidered; the test
    exists so that moving one without the other is visible.
    """
    from omicsclaw.engine import EngineConfig

    assert 0 < DEFAULT_GATE_TURNS < EngineConfig().max_turns // 4


# ---- both at once --------------------------------------------------------


def test_the_nudge_comes_before_the_block_when_both_would_appear():
    """They never co-occur today — the gate only fires with no plan — but
    if that changes, "make a plan" must precede the plan it asks for."""
    store = _store()
    injector = PlanInjector(store, gate_turns=2)

    async def go():
        history = _read_only_turns(3)
        # The gate is evaluated against an empty plan, then the plan is
        # read; writing between the two is what makes both appear.
        original = store.is_empty

        class _Racing:
            @property
            def is_empty(self):
                return original

            def read(self):
                return (PlanItem("1", "step", R),)

        return await PlanInjector(_Racing(), gate_turns=2).augment(history, ())

    appended = asyncio.run(go())

    assert appended[0].content == PLANNING_GATE_TEXT
    assert appended[1].content.startswith(INJECTION_HEADER)
