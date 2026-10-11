"""The volatile block: a text that is sent only while it changes.

:class:`~omicsclaw.context.VolatileBlock` is the diffing shell for
state that lives outside the conversation and must be restated inside
it — the précis once it migrates off the prompt, the session scratch.
The four behaviours pinned here are the whole contract: first call
sends, a change sends the full block, no change sends nothing, and an
empty render sends nothing either.
"""

from __future__ import annotations

import asyncio

from omicsclaw.context import VolatileBlock
from omicsclaw.engine import TurnAugmentor
from omicsclaw.schema import Message, Role, ToolDefinition


def _run(coro):
    return asyncio.run(coro)


def _wrap(text: str) -> Message:
    return Message(role=Role.USER, content=f"<block>\n{text}\n</block>")


def _block(values: list[str]) -> VolatileBlock:
    """A block whose renderer answers *values* in order, then repeats the last."""
    calls = 0

    def render() -> str:
        nonlocal calls
        answer = values[min(calls, len(values) - 1)]
        calls += 1
        return answer

    return VolatileBlock(render=render, wrap=_wrap)


HISTORY = (Message.user("earlier"),)
TOOLS = (ToolDefinition("read_file", "read a file", {"type": "object"}),)


def test_a_volatile_block_satisfies_the_engine_s_seam():
    block = VolatileBlock(render=lambda: "text", wrap=_wrap)

    assert isinstance(block, TurnAugmentor)


def test_the_first_call_sends_the_block_in_full():
    block = _block(["the précis"])

    sent = _run(block.augment(HISTORY, TOOLS))

    assert [m.content for m in sent] == ["<block>\nthe précis\n</block>"]


def test_an_unchanged_render_sends_nothing_after_the_first_call():
    block = _block(["the précis"])

    _run(block.augment(HISTORY, TOOLS))
    sent = _run(block.augment(HISTORY, TOOLS))

    assert sent == ()


def test_a_change_sends_the_new_text_in_full():
    block = _block(["the précis", "the précis, edited"])

    _run(block.augment(HISTORY, TOOLS))
    sent = _run(block.augment(HISTORY, TOOLS))

    assert [m.content for m in sent] == ["<block>\nthe précis, edited\n</block>"]


def test_an_empty_render_sends_nothing_even_on_the_first_call():
    block = _block([""])

    assert _run(block.augment(HISTORY, TOOLS)) == ()


def test_text_after_an_empty_render_counts_as_a_change():
    block = _block(["", "now something"])

    _run(block.augment(HISTORY, TOOLS))
    sent = _run(block.augment(HISTORY, TOOLS))

    assert [m.content for m in sent] == ["<block>\nnow something\n</block>"]


def test_every_call_re_reads_so_mid_exchange_edits_are_visible():
    """The renderer is consulted per call — a write between two calls
    shows up on the second, which is the property that makes the block a
    channel for mid-run state changes."""
    state: list[str] = ["first"]

    def render() -> str:
        return state[0]

    block = VolatileBlock(render=render, wrap=_wrap)

    _run(block.augment(HISTORY, TOOLS))
    state[0] = "second"
    sent = _run(block.augment(HISTORY, TOOLS))

    assert [m.content for m in sent] == ["<block>\nsecond\n</block>"]


def test_history_and_tools_are_ignored():
    block = _block(["the précis"])

    sent = _run(block.augment((), ()))

    assert [m.content for m in sent] == ["<block>\nthe précis\n</block>"]
