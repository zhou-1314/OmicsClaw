"""The augmentors one exchange hands the engine, joined into one.

The engine consults a single
:class:`~omicsclaw.engine.TurnAugmentor` before each model call.
:func:`build_augmentor` decides which augmentors a main-agent exchange
gets and in what order, and :func:`chain` turns them into the one object
the engine takes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from omicsclaw.context import MemoryNudge
from omicsclaw.engine import TurnAugmentor
from omicsclaw.schema import Message, ToolDefinition

from .memory import MEMORY_WRITE_TOOL_NAME
from .planning import build_injector

if TYPE_CHECKING:  # pragma: no cover - a type-only import
    from .assembly import AgentApp

__all__ = ["AugmentorChain", "build_augmentor", "chain"]


class AugmentorChain:
    """Asks each member in order and returns everything they append.

    :param members: Augmentors to consult, first to last.
    """

    __slots__ = ("_members",)

    def __init__(self, members: Sequence[TurnAugmentor]) -> None:
        self._members = tuple(members)

    async def augment(
        self,
        history: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
    ) -> tuple[Message, ...]:
        """Every member's messages for this model call, in member order.

        Members are awaited one after another, and each is given the
        same *history*: none sees what an earlier member appended.

        :param history: The conversation this call will be sent, after
            compaction.
        :param tools: The tool definitions this call will carry.
        :returns: The members' messages, concatenated.
        :raises Exception: Whatever a member raises. Members after it are
            not consulted.
        """
        appended: list[Message] = []
        for member in self._members:
            appended.extend(await member.augment(history, tools))
        return tuple(appended)


def chain(*members: TurnAugmentor | None) -> TurnAugmentor | None:
    """The given members as one augmentor.

    :param members: Augmentors in the order they should be consulted.
        ``None`` entries are dropped.
    :returns: ``None`` when no member is left, the member itself when
        exactly one is, and an :class:`AugmentorChain` over two or more.
    """
    present = [member for member in members if member is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return AugmentorChain(present)


def build_augmentor(app: "AgentApp", *, session_id: str = "") -> TurnAugmentor | None:
    """The augmentor for one main-agent exchange of *session_id*, or ``None``.

    The memory reminder comes first and the plan injector last, so the
    plan block stays the last thing the model reads. Building it restores
    the session's plan, as
    :func:`~omicsclaw.entry.planning.build_injector` does.

    Sub-agent exchanges do not go through here and get no memory
    reminder.

    :param app: The deployment the exchange runs in.
    :param session_id: The session whose plan is injected.
    :returns: What the engine should consult before each model call of
        the exchange, or ``None`` when the deployment has nothing to add.
    """
    return chain(_memory_nudge(app), build_injector(app, session_id=session_id))


def _memory_nudge(app: "AgentApp") -> MemoryNudge | None:
    """The memory reminder for *app*, or ``None`` when it has none.

    :returns: ``None`` when the deployment keeps no long-term memory or
        :attr:`~omicsclaw.entry.config.AppConfig.memory_nudge_turns` is
        ``0`` or less.
    """
    every = app.config.memory_nudge_turns
    if app.memory is None or every <= 0:
        return None
    return MemoryNudge(write_tool=MEMORY_WRITE_TOOL_NAME, every=every)
