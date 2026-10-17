"""Read and persist exchange history.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from omicsclaw.schema import Message

__all__ = ["Conversation"]


@runtime_checkable
class Conversation(Protocol):
    """The history one exchange starts from, and where it is left.

    Passed to :meth:`~omicsclaw.engine.loop.AgentEngine.exchange` or
    :meth:`~omicsclaw.engine.loop.AgentEngine.exchange_stream`, or given
    to the engine as a default at construction. Read once, written once,
    never touched during a turn.
    """

    def messages(self) -> Sequence[Message]:
        """The conversation so far, oldest first, **without a system
        message**.

        The engine prepends its own when it has a
        :class:`~omicsclaw.engine.prompt.PromptSource`, and two system
        messages are not a longer prompt — they are a stale persona
        standing beside the current one.

        Synchronous: an exchange is about to be composed out of this, and
        a history that has to be fetched should be fetched before the
        exchange rather than inside the composition of its first call.
        """
        ...

    async def commit(self, messages: Sequence[Message]) -> None:
        """Take this exchange's whole trajectory, replacing what was there.

        :param messages: Everything the run ended with — the history it
            was given, as compaction may have rewritten it, plus what
            this exchange produced — with the engine's system message
            already removed, so it can be handed straight back from
            :meth:`messages` next time.
        :raises Exception: Propagates out of the exchange, which is the
            honest outcome: a conversation that could not be recorded is
            one the next exchange would silently continue without.

        Called **once**, after the run has finished. An exchange that
        raised, or a stream the caller walked away from, commits nothing:
        a trajectory nobody completed is not a conversation to carry
        forward.
        """
        ...
