"""Define the sections used to build system prompts.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeAlias

__all__ = [
    "RenderedSection",
    "Section",
    "SectionSource",
    "static",
    "text_from_file",
]

SectionSource: TypeAlias = Callable[[], str]
"""Produces one section's body. Called on **every** render, never cached."""


@dataclass(frozen=True, slots=True)
class Section:
    """One block of the system prompt, and where its text comes from."""

    key: str
    """Stable identifier, for :meth:`~omicsclaw.context.prompt.
    PromptAssembler.without` and for diagnostics. Not rendered."""

    heading: str
    """Rendered above the body, separated by a blank line. ``""`` for a
    block that carries no title, such as the base persona."""

    source: SectionSource
    """Where the body comes from."""

    enabled: bool = True
    """``False`` drops the section without removing it from the list."""


@dataclass(frozen=True, slots=True)
class RenderedSection:
    """What one section became on one particular render."""

    key: str
    content: str
    """Heading and body, exactly as they appear in the system prompt."""

    estimated_tokens: int


def static(text: str) -> SectionSource:
    """A source for text that is already in hand."""
    return lambda: text


def text_from_file(
    path: str | os.PathLike[str],
    *,
    encoding: str = "utf-8",
) -> SectionSource:
    """A source that reads *path* fresh on every render.

    The only place in this package that touches a filesystem, and it is
    a convenience rather than a mechanism: the caller decides which
    files are prompt material by building sections, because encoding
    ``OMICSCLAW.md`` and the per-domain ``skills/<domain>/INDEX.md``
    files as parameters of this layer would nail
    that policy to the bottom of the stack.

    **A file that is not there yields ``""``**, which makes the section
    disappear whole — the harness's treatment of a missing ``AGENTS.md``
    (``builder.go:107-110``) and what plan 0030 §5.1 asks for of an
    absent prompt file: the section is empty and the caller supplies its
    own fallback. Absence is a state, not a failure.

    Every other error — unreadable, undecodable, a directory — is a
    failure and propagates, per the module docstring. Nothing is cached:
    a file edited mid-run is read again on the next render.
    """

    def read() -> str:
        try:
            return Path(path).read_text(encoding=encoding)
        except FileNotFoundError:
            return ""

    return read
