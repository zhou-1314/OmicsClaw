"""Reminders appended to what one model call is sent.

:class:`MemoryNudge` satisfies the engine's ``TurnAugmentor`` protocol
structurally and imports neither the engine nor the memory layer: the
tool it names arrives as a string.
"""

from __future__ import annotations

from typing import Callable, Sequence

from omicsclaw.schema import Message, Role, ToolDefinition

__all__ = ["DEFAULT_MEMORY_NUDGE_TURNS", "MEMORY_NUDGE_TEXT", "MemoryNudge"]


DEFAULT_MEMORY_NUDGE_TURNS = 10
"""Model turns between two reminders to write long-term memory."""

MEMORY_NUDGE_TEXT = (
    "If this conversation has produced something worth keeping across sessions — "
    "a preference the user stated, a stable fact about this project, a decision and "
    "why it was made — record it now with `{tool}`. Otherwise ignore this note."
)
"""What the reminder says. ``{tool}`` is replaced with the write tool's name."""


class MemoryNudge:
    """Reminds the model, every few turns, to keep what should outlive the session.

    The turns are counted from the conversation each call is given, so an
    instance holds no count of its own: one built for every exchange
    reminds at the same moments as one that lived for the whole session.

    :param write_tool: Name of the tool that writes long-term memory.
    :param every: Model turns between reminders, counted from the last
        call to *write_tool*. ``0`` or less never reminds.
    :param text: The reminder. ``{tool}`` in it is replaced with
        *write_tool*.
    :param quiet: Asked on every call that would remind. While it returns
        true no reminder is given, and the one skipped is not sent later.
    """

    __slots__ = ("_every", "_message", "_quiet", "_write_tool")

    def __init__(
        self,
        *,
        write_tool: str,
        every: int = DEFAULT_MEMORY_NUDGE_TURNS,
        text: str = MEMORY_NUDGE_TEXT,
        quiet: Callable[[], bool] | None = None,
    ) -> None:
        self._write_tool = write_tool
        self._every = every
        self._message = Message(role=Role.USER, content=text.format(tool=write_tool))
        self._quiet = quiet

    async def augment(
        self,
        history: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
    ) -> tuple[Message, ...]:
        """The reminder, when the turns since the last write are a multiple of *every*.

        The count is the assistant messages in *history* after the last
        one that called the write tool, or all of them when none did. A
        compaction that replaces assistant messages with a summary
        therefore lowers it.

        :param history: The conversation this call will be sent, after
            compaction.
        :param tools: The tool definitions this call will carry. Without
            the write tool among them there is no reminder.
        :returns: One user message, or an empty tuple.
        """
        if self._every <= 0:
            return ()
        if not any(tool.name == self._write_tool for tool in tools):
            return ()
        turns = 0
        for message in reversed(history):
            if message.role is not Role.ASSISTANT:
                continue
            if any(call.name == self._write_tool for call in message.tool_calls):
                break
            turns += 1
        if turns == 0 or turns % self._every:
            return ()
        if self._quiet is not None and self._quiet():
            return ()
        return (self._message,)
