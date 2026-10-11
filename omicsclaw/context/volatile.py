"""One block of state, restated into a model call only while it changes.

:class:`VolatileBlock` satisfies the engine's ``TurnAugmentor`` protocol
structurally and imports neither the engine nor the memory layer, on the
same leaf discipline as :mod:`omicsclaw.context.nudge`: the text it
restates arrives as two callables. It exists for the blocks whose
authoritative state lives outside the conversation — the long-term
memory précis, the session scratch — and that the model must be shown
again whenever that state changes.

The augmentor seam hands the model the *sent copy* of a call, never the
run's history, so a block built per exchange re-renders on every call
and compares the text against what it last sent: the same text sends
nothing, a change sends the whole block.
"""

from __future__ import annotations

import inspect
from typing import Awaitable, Callable, Sequence, Union

from omicsclaw.schema import Message, ToolDefinition

__all__ = ["VolatileBlock"]

_UNSENT = object()

_RenderResult = Union[str, Awaitable[str]]


class VolatileBlock:
    """Re-renders on every call and sends the text only while it changes.

    The per-exchange instance is the whole of its diff state: the first
    call always sends the block (there is no earlier text to compare
    against), a call whose render equals the previous one sends nothing,
    and an empty render sends nothing either. What the engine does with
    the returned message never reaches :meth:`augment` again, so nothing
    accumulates — every call starts from the current render.

    :param render: Called on every :meth:`augment`, so an edit made
        mid-exchange is visible to the very next model call. May answer
        directly or return an awaitable — sources that read through an
        async store pass a coroutine function.
    :param wrap: Turns the rendered text into the message the model
        reads. Called only for a non-empty text that changed.
    """

    __slots__ = ("_last", "_render", "_wrap")

    def __init__(
        self,
        *,
        render: Callable[[], _RenderResult],
        wrap: Callable[[str], Message],
    ) -> None:
        self._render = render
        self._wrap = wrap
        self._last: object | str = _UNSENT

    async def augment(
        self,
        history: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
    ) -> tuple[Message, ...]:
        """The block, when its text changed since the previous call.

        :param history: Unused — the block's content comes from *render*,
            not from the conversation.
        :param tools: Unused.
        :returns: One message, or an empty tuple.
        """
        text = self._render()
        if inspect.isawaitable(text):
            text = await text
        if text == self._last:
            return ()
        self._last = text
        if not text:
            return ()
        return (self._wrap(text),)
