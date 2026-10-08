"""When the memory reminder is given, and when it is not."""

from __future__ import annotations

import asyncio

import pytest

from omicsclaw.context import (
    DEFAULT_MEMORY_NUDGE_TURNS,
    MEMORY_NUDGE_TEXT,
    MemoryNudge,
)
from omicsclaw.schema import Message, Role, ToolCall, ToolDefinition

WRITE = "memory_write"
TOOLS = (
    ToolDefinition("read_file", "read a file", {"type": "object"}),
    ToolDefinition(WRITE, "keep something", {"type": "object"}),
)


def ask(nudge: MemoryNudge, history, tools=TOOLS) -> tuple[Message, ...]:
    return asyncio.run(nudge.augment(tuple(history), tools))


def turn(tool: str = "read_file", *, index: int = 0) -> list[Message]:
    """One assistant message calling *tool*, and the tool's answer."""
    call_id = f"{tool}-{index}"
    return [
        Message.assistant(tool_calls=(ToolCall(id=call_id, name=tool),)),
        Message.tool(tool_call_id=call_id, content="ok"),
    ]


def reading(turns: int) -> list[Message]:
    """A conversation of *turns* assistant messages, none of them a write."""
    history: list[Message] = [Message.user("start")]
    for index in range(turns):
        history.extend(turn(index=index))
    return history


def exchanges(count: int, calls: int, **nudge: object) -> list[tuple[int, int]]:
    """Run *count* exchanges of *calls* model calls each, as the engine would.

    Each exchange gets a nudge of its own, sees the conversation the
    earlier ones left, and ends with a plain answer. Nothing the nudge
    appends is kept.

    :returns: The ``(exchange, call)`` pairs, both from 1, that reminded.
    """
    history: list[Message] = []
    reminded: list[tuple[int, int]] = []
    for exchange in range(1, count + 1):
        instance = MemoryNudge(write_tool=WRITE, **nudge)
        history.append(Message.user(f"question {exchange}"))
        for call in range(1, calls + 1):
            if ask(instance, history):
                reminded.append((exchange, call))
            if call < calls:
                history.extend(turn(index=len(history)))
            else:
                history.append(Message.assistant(f"answer {exchange}"))
    return reminded


def test_a_session_of_short_exchanges_is_reminded_once_at_the_tenth_turn() -> None:
    """Three exchanges of four calls: ten turns are behind the eleventh call."""
    assert exchanges(3, 4) == [(3, 3)]


def test_a_long_exchange_is_reminded_at_every_multiple() -> None:
    assert exchanges(1, 25) == [(1, 11), (1, 21)]


def test_the_default_interval_is_ten_turns() -> None:
    assert DEFAULT_MEMORY_NUDGE_TURNS == 10
    nudge = MemoryNudge(write_tool=WRITE)
    assert ask(nudge, reading(9)) == ()
    assert len(ask(nudge, reading(10))) == 1


def test_the_reminder_is_one_user_message_naming_the_write_tool() -> None:
    nudge = MemoryNudge(write_tool=WRITE, every=2)
    (message,) = ask(nudge, reading(2))
    assert message.role is Role.USER
    assert message.content == MEMORY_NUDGE_TEXT.format(tool=WRITE)
    assert f"`{WRITE}`" in message.content
    assert "{tool}" not in message.content


def test_a_caller_s_own_text_names_the_tool_too() -> None:
    nudge = MemoryNudge(write_tool="remember", every=1, text="call {tool} now")
    tools = (ToolDefinition("remember", "keep", {"type": "object"}),)
    (message,) = ask(nudge, reading(1), tools)
    assert message.content == "call remember now"


def test_nothing_is_said_before_the_first_turn() -> None:
    nudge = MemoryNudge(write_tool=WRITE, every=1)
    assert ask(nudge, []) == ()
    assert ask(nudge, [Message.user("hello")]) == ()


def test_a_write_starts_the_count_again() -> None:
    nudge = MemoryNudge(write_tool=WRITE, every=3)
    history = reading(2)
    history.extend(turn(WRITE, index=90))
    assert ask(nudge, history) == (), "the write is turn zero of a new count"
    history.extend(turn(index=91))
    history.extend(turn(index=92))
    assert ask(nudge, history) == (), "two turns since the write"
    history.extend(turn(index=93))
    assert len(ask(nudge, history)) == 1, "three turns since the write"


def test_only_the_last_write_counts() -> None:
    nudge = MemoryNudge(write_tool=WRITE, every=2)
    history = reading(1)
    history.extend(turn(WRITE, index=90))
    history.extend(turn(index=91))
    history.extend(turn(WRITE, index=92))
    history.extend(turn(index=93))
    assert ask(nudge, history) == ()
    history.extend(turn(index=94))
    assert len(ask(nudge, history)) == 1


def test_a_write_that_is_not_the_first_call_of_its_turn_counts() -> None:
    """Three turns in all, but only one since the write.

    An interval of three tells the two apart: missing the write would
    count all three turns and remind here.
    """
    nudge = MemoryNudge(write_tool=WRITE, every=3)
    history = reading(1)
    history.append(
        Message.assistant(
            tool_calls=(
                ToolCall(id="a", name="read_file"),
                ToolCall(id="b", name=WRITE),
            )
        )
    )
    history.extend(turn(index=91))
    assert ask(nudge, history) == ()
    history.extend(turn(index=92))
    history.extend(turn(index=93))
    assert len(ask(nudge, history)) == 1


def test_there_is_no_reminder_without_the_write_tool_on_the_call() -> None:
    """Naming a tool the model cannot call would send it looking for one."""
    nudge = MemoryNudge(write_tool=WRITE, every=2)
    without = (ToolDefinition("read_file", "read a file", {"type": "object"}),)
    for turns in range(0, 9):
        assert ask(nudge, reading(turns), without) == (), turns
        assert ask(nudge, reading(turns), ()) == (), turns


def test_a_quiet_predicate_holds_the_reminder_back() -> None:
    quiet = [True]
    nudge = MemoryNudge(write_tool=WRITE, every=2, quiet=lambda: quiet[0])
    assert ask(nudge, reading(2)) == ()
    quiet[0] = False
    assert len(ask(nudge, reading(2))) == 1


def test_a_reminder_held_back_is_not_sent_late() -> None:
    quiet = [True]
    nudge = MemoryNudge(write_tool=WRITE, every=4, quiet=lambda: quiet[0])
    assert ask(nudge, reading(4)) == ()
    quiet[0] = False
    assert ask(nudge, reading(5)) == ()
    assert len(ask(nudge, reading(8))) == 1


@pytest.mark.parametrize("every", [0, -1])
def test_an_interval_of_zero_or_less_never_reminds(every: int) -> None:
    nudge = MemoryNudge(write_tool=WRITE, every=every)
    for turns in range(0, 12):
        assert ask(nudge, reading(turns)) == (), turns


def test_the_conversation_it_is_given_is_left_alone() -> None:
    nudge = MemoryNudge(write_tool=WRITE, every=2)
    history = tuple(reading(2))
    before = tuple(history)
    ask(nudge, history)
    assert history == before


def test_a_compaction_that_removes_turns_lowers_the_count() -> None:
    """The count is read off what the call is sent, so a summary lowers it."""
    nudge = MemoryNudge(write_tool=WRITE, every=4)
    compacted = [Message.user("summary of twelve turns"), *turn(index=1)]
    assert ask(nudge, compacted) == ()
    assert len(ask(nudge, [*compacted, *reading(3)[1:]])) == 1
