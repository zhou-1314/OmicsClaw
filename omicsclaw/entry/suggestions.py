"""``omicsclaw/entry`` — the follow-up suggestion block an answer may end with.

P6 item 2 of the front-back plan. A model instructed by
:data:`~omicsclaw.entry.assembly.FOLLOW_UP_RULES` may close its answer
with a sentinel line followed by up to three one-line follow-up
questions. Two halves live here:

:func:`split_suggestions` / :class:`SuggestionSplitter`
    The **streaming** half. Answer text reaches surfaces as
    ``TEXT_DELTA`` frames, so by the time a whole answer exists the
    block would already have been shown. The splitter is fed each delta
    as it is published, holds back only the bytes that could still turn
    out to be the start of the sentinel, and returns the visible text;
    once the sentinel has been matched everything after it is captured
    instead of shown. The turn kernel
    (:meth:`~omicsclaw.entry.turn.TurnKernel._sequence`) drives it.

:func:`strip_suggestions`
    The **after-the-fact** half, for text that is read whole rather than
    streamed — the exchange's ``reply``, which a Channel posts verbatim.
    RunResult history is deliberately *not* stripped: those bytes are
    the model's own record, matched byte-for-byte by prompt-prefix
    caches and replay evidence.

The block is optional by design (plan: "chips are OPTIONAL for the
model to produce, absent = no chips"). A model that never writes the
sentinel costs nothing: the splitter passes every delta through
untouched apart from the held-back-tail scan, and no ``SUGGESTIONS``
event is published.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "FOLLOW_UP_SENTINEL",
    "SUGGESTION_LIMIT",
    "SuggestionSplitter",
    "parse_suggestion_block",
    "split_suggestions",
    "strip_suggestions",
]

FOLLOW_UP_SENTINEL: Final = "[[FOLLOW-UPS]]"
"""The line that opens the suggestion block.

Chosen to be a token no answer text produces by accident: square
brackets are rare in prose, doubled, and the word is hyphenated and
shouted. The prompt (:data:`~omicsclaw.entry.assembly.FOLLOW_UP_RULES`)
spells this exact constant, and the parser matches it, so the two cannot
drift — a sentinel the prompt and the matcher disagreed about would
either show the raw block to users or swallow answer text."""

SUGGESTION_LIMIT: Final = 3
"""The most questions one block may carry.

The plan asks for "exactly 3"; the transport enforces the upper bound
only. A model that offered four would have its fourth silently dropped
rather than be told off, because the block is a convenience and a
correction round-trip for it costs more than a lost chip."""

_BLOCK_MAX_CHARS: Final = 2000
"""Captured-block ceiling; the block keeps its FIRST characters when the
ceiling is hit, on both the incremental and the sentinel-match path. A
model that rambles past this cannot grow an unbounded buffer inside the
kernel, and the questions are the first lines after the sentinel — the
tail that gets cut is junk by construction."""


def parse_suggestion_block(block: str) -> tuple[str, ...]:
    """The questions one captured block carries: stripped, non-empty,
    deduplicated in order, at most :data:`SUGGESTION_LIMIT`.

    Numbered-prefix tolerant (``1. `` / ``- ``), because a model told to
    write three questions per line will sometimes number them anyway.
    """

    questions: list[str] = []
    seen: set[str] = set()
    for raw_line in block.splitlines():
        line = raw_line.strip()
        # The delimiter must be followed by whitespace: "1. question"
        # strips, "3.5 Mb is a lot" does not — a decimal is prose, not
        # a list number.
        if (
            len(line) > 2
            and line[0].isdigit()
            and line[1] in ".-)"
            and line[2].isspace()
        ):
            line = line[2:].strip()
        elif len(line) > 1 and line[0] in "-*" and line[1] == " ":
            line = line[2:].strip()
        if not line or line.lower() == FOLLOW_UP_SENTINEL.lower():
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        questions.append(line)
        if len(questions) == SUGGESTION_LIMIT:
            break
    return tuple(questions)


def split_suggestions(text: str) -> tuple[str, tuple[str, ...]]:
    """Split whole text into ``(visible, questions)``.

    The after-the-fact spelling of what :class:`SuggestionSplitter` does
    across deltas; :func:`strip_suggestions` is the convenience wrapper
    that keeps only the visible half.
    """

    at = text.find(FOLLOW_UP_SENTINEL)
    if at < 0:
        return text, ()
    return text[:at].rstrip(), parse_suggestion_block(text[at + len(FOLLOW_UP_SENTINEL) :])


def strip_suggestions(text: str) -> str:
    """Whole text with any trailing suggestion block removed."""
    return split_suggestions(text)[0]


def _held_back_tail(text: str) -> int:
    """Length of the longest suffix of *text* that is a proper prefix of
    the sentinel — the bytes a delta must not show yet because they
    might still turn into the block's opening line.

    Only a *proper* prefix is held back: a suffix equal to the whole
    sentinel is the sentinel, which the caller handles by matching it.
    """

    for keep in range(min(len(text), len(FOLLOW_UP_SENTINEL) - 1), 0, -1):
        if FOLLOW_UP_SENTINEL.startswith(text[-keep:]):
            return keep
    return 0


class SuggestionSplitter:
    """One model call's text, split into the shown answer and the block.

    States: *matching* (scanning for the sentinel), then *capturing*
    (sentinel seen; everything accumulates into the block). The turn
    kernel creates one per exchange and calls :meth:`close` at each
    ``TURN_END`` — a multi-round exchange narrates between tool calls,
    and only the call the model ended with a block produces questions,
    which is the one whose chips a surface should show.

    Pure aside from its own accumulators; deterministic under test.

    **Known bound, accepted**: a literal ``[[FOLLOW-UPS]]`` inside a
    markdown code fence in the answer is still treated as the block's
    opening line, and everything after it leaves the *displayed* text.
    Distinguishing fenced from unfenced sentinel hits would mean
    parsing markdown mid-stream, and the loss is bounded twice over —
    the conversation history keeps the model's bytes verbatim, and the
    prompt rule tells the model the marker exists only at an answer's
    very end. Do not "fix" this by writing the sentinel into the
    prompt-stream text: that trades a display-only tail loss for
    corrupting the bytes caches and replays match on.
    """

    __slots__ = ("_block", "_carry")

    def __init__(self) -> None:
        self._carry = ""
        self._block: str | None = None

    def feed(self, delta: str) -> str:
        """One delta in; the bytes of it that may be shown out.

        In *matching* state the pending carry is prepended (it was held
        back last time because it might have started a sentinel) and the
        scan runs over the joined text. In *capturing* state the delta
        joins the block and nothing is shown.
        """

        if self._block is not None:
            self._block = (self._block + delta)[:_BLOCK_MAX_CHARS]
            return ""
        text = self._carry + delta
        self._carry = ""
        at = text.find(FOLLOW_UP_SENTINEL)
        if at >= 0:
            self._block = text[at + len(FOLLOW_UP_SENTINEL) :][:_BLOCK_MAX_CHARS]
            return text[:at]
        keep = _held_back_tail(text)
        self._carry = text[len(text) - keep :] if keep else ""
        return text[: len(text) - keep]

    def close(self) -> tuple[str, tuple[str, ...]]:
        """End one model call: any held-back tail, and the questions.

        The tail matters for an answer that genuinely ends in ``[[`` —
        bytes the matcher held back that never became a sentinel are
        answer text and must still be shown. Returns the pair so the
        caller can publish the tail as one final delta before the
        questions' own event.
        """

        if self._block is None:
            tail, self._carry = self._carry, ""
            return tail, ()
        block, self._block = self._block, None
        return "", parse_suggestion_block(block)
