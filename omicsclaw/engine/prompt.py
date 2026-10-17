"""Resolve the prompt for an execution loop.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["PromptSource", "RenderedPrompt"]


@runtime_checkable
class RenderedPrompt(Protocol):
    """One render of a system prompt, as its renderer hands it over.

    The engine reads a single member and copies nothing else, so an
    implementation is free to carry as much diagnostic weight beside it
    as it likes — this Protocol is a floor, not a shape.

    Reachable afterwards as
    :attr:`~omicsclaw.engine.types.RunResult.prompt`, which is how a
    caller reads the statistics of the render its exchange actually used
    instead of rendering a second time and hoping the two agree.
    """

    @property
    def system_prompt(self) -> str:
        """The one string that becomes message zero of an exchange."""
        ...


@runtime_checkable
class PromptSource(Protocol):
    """Renders the system prompt an exchange opens with."""

    def render(self) -> RenderedPrompt:
        """Render the prompt as it stands *now*.

        Called **once per exchange**, before the first model call and
        never again inside it. Once per turn would let an edit to a
        prompt file change the persona halfway through a single exchange
        — and would re-read those files once per turn for the privilege.

        Synchronous, matching the renderers that exist: assembling a
        prompt is reading files and joining strings. An implementation
        that genuinely needs to await something should do that work
        before the exchange and render from what it already holds.

        :raises Exception: Propagates out of the exchange. A prompt that
            could not be rendered is not a prompt worth guessing at —
            the run would proceed under a persona nobody wrote.
        """
        ...
