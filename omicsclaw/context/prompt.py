"""Assemble ordered sections into one system message followed by conversation.

The system message occupies index zero, so provider adapters and cache
breakpoints see the same placement. Section renderers supply current values.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from omicsclaw.schema import Message

from .sections import RenderedSection, Section
from .tokens import estimate_text_tokens

__all__ = [
    "AssembledPrompt",
    "PromptAssembler",
    "assemble",
]

_SECTION_SEPARATOR = "\n\n"
"""Between sections, and between a heading and its body. ``builder.go:189``."""


@dataclass(frozen=True, slots=True)
class AssembledPrompt:
    """One render: the sections that produced text, in arrival order."""

    sections: tuple[RenderedSection, ...]
    """Empty sections are already gone — see :meth:`PromptAssembler.render`."""

    @property
    def system_prompt(self) -> str:
        """The single string that becomes message zero."""
        return _SECTION_SEPARATOR.join(s.content for s in self.sections)

    @property
    def section_stats(self) -> tuple[tuple[str, int], ...]:
        """``(key, estimated_tokens)`` per surviving section.

        For diagnostics: the question "which block is eating the window"
        has no other answer once the sections are joined into one string.
        """
        return tuple((s.key, s.estimated_tokens) for s in self.sections)

    @property
    def total_estimated_tokens(self) -> int:
        """What the whole prompt costs, by :mod:`omicsclaw.context.tokens`."""
        return sum(s.estimated_tokens for s in self.sections)


@dataclass(frozen=True, slots=True)
class PromptAssembler:
    """An ordered list of sections. Immutable; every edit returns a new one."""

    sections: tuple[Section, ...] = ()

    def with_section(self, section: Section) -> PromptAssembler:
        """Append *section*. **Arrival order is render order** — see module."""
        return PromptAssembler(sections=(*self.sections, section))

    def without(self, key: str) -> PromptAssembler:
        """Drop every section with this key. Absent keys are not an error."""
        kept = tuple(s for s in self.sections if s.key != key)
        return PromptAssembler(sections=kept)

    def render(self) -> AssembledPrompt:
        """Call every enabled source and lay the results out.

        Three rules, all of them the harness's:

        * A source returning ``""`` takes its heading with it and
          vanishes (``builder.go:107-119``, ``builder_test.go:105-111``).
          A prompt with a bare ``## Long-term memory`` over nothing
          teaches the model that the section exists and is empty.
        * Whatever a source returns is placed **verbatim** — not
          truncated, reflowed, re-indented or wrapped. Rendering belongs
          outside this layer, so second-guessing it here would be
          editing text this package cannot read.
        * The call happens now, on this render, every render.

        Sources run in order and their exceptions propagate; a source
        that raises stops the render, and the caller decides what a
        prompt missing that block would have been worth.

        **This is where the prefix cache is spent.** Calling every
        source afresh is what makes a mid-run ``memory_write`` visible,
        and it is also what breaks the cached prefix whenever one of
        them returns something new. See the module docstring; the shape
        that once avoided this — volatile text on the user turn — was
        ruled out on 2026-09-18.
        """
        rendered: list[RenderedSection] = []
        for section in self.sections:
            if not section.enabled:
                continue
            body = section.source()
            if not body:
                continue
            if section.heading:
                content = f"{section.heading}{_SECTION_SEPARATOR}{body}"
            else:
                content = body
            rendered.append(
                RenderedSection(
                    key=section.key,
                    content=content,
                    estimated_tokens=estimate_text_tokens(content),
                )
            )
        return AssembledPrompt(sections=tuple(rendered))


def assemble(
    prompt: AssembledPrompt,
    history: Sequence[Message],
    user_text: str = "",
) -> tuple[Message, ...]:
    """``[system, *history, user?]`` — the sequence an engine is handed.

    **This function contributes exactly one**
    :attr:`~omicsclaw.schema.Role.SYSTEM` **message and puts it at index
    0, whatever the prompt is made of** — see the module docstring for
    why several would be two different prompts. It does not inspect
    *history*, so that is a statement about what ``assemble`` adds and
    not about what comes back.

    **The difference is reachable, through this package's own loop.**
    ``compact(..., pinned=1)`` preserves the system message it was given
    and returns it inside the compacted history; feeding that straight
    back in here produces **two** system messages, the model reads a
    stale persona beside the current one, and
    ``_mark_last_system_message`` (``openai_provider.py:212-250``) puts
    the cache breakpoint on index 1. The remedy is one slice at the call
    site — hand back the history *without* the prefix that was pinned::

        smaller, record, state = await compact(conversation, budget, pinned=1)
        conversation = assemble(prompt.render(), smaller[1:], "下一步")

    and it is the caller's rather than this function's because dropping
    a system message out of *history* here would be this layer silently
    discarding a message a caller deliberately kept. ``assemble`` adds;
    it does not edit what it was handed. A caller that genuinely wants
    the older prompt preserved keeps it, and the two-system conversation
    is then a decision rather than an accident.

    Two boundaries, both deliberate:

    * An empty *user_text* appends **nothing**. History may already end
      with the user's turn — a caller compacting and re-running is the
      ordinary case — and a blank turn appended on top of it is a turn
      the model has to interpret.
    * A non-empty *user_text* appends **one** user message whose content
      is *user_text* byte for byte. No prefix, no heading, no wrapper.
      The replaced layer put ``"## User Request"`` in front of it; that
      heading existed solely to separate the user's words from the
      volatile context riding on the same turn, and with that context
      now in the system prompt the heading separates nothing.

    A tuple, like every return value in this package: handing back a
    slice of the caller's list means one ``append`` later corrupts a
    conversation nobody thought they had shared.
    """
    messages = [Message.system(prompt.system_prompt), *history]
    if user_text:
        messages.append(Message.user(user_text))
    return tuple(messages)
