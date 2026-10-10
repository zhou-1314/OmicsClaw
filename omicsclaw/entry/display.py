"""Text made safe to show a person, for every surface.

A reason, a tool name, an argument or a line of output reaches a terminal
or a chat from a model that may have read a hostile file. Everything here
returns text in which no character can act on the display: control and
format characters are escaped by
:func:`~omicsclaw.tools.preview.escape_unsafe`, and line breaks are either
drawn as a mark (:func:`inert_line`), kept behind a fixed prefix
(:func:`inert_body`), so the text cannot start a line of its own, or
kept as they are (:func:`inert_prose`), for an answer that is printed on
lines of its own. :func:`inert_body` renders the body of a card, bounded
and folded, and :func:`inert_body_note` repeats its note.
:func:`approval_body` is the body of an approval card, with
credential-named values in the call's arguments hidden by
:func:`~omicsclaw.tools.preview.redact_credentials`;
:func:`approval_body_note` repeats its note, and
:func:`unreadable_arguments_note` is what stands for arguments that cannot
be read as JSON.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Final, NamedTuple

from omicsclaw.tools import ApprovalRequest
from omicsclaw.tools.preview import escape_unsafe, redact_credentials

__all__ = [
    "CONTINUATION_PREFIX",
    "LINE_BREAK_MARK",
    "MAX_APPROVAL_BODY_CHARS",
    "MAX_APPROVAL_BODY_LINES",
    "TALL_APPROVAL_BODY_CHARS",
    "TALL_APPROVAL_BODY_LINES",
    "WRAP_PREFIX",
    "approval_body",
    "approval_body_note",
    "inert_body",
    "inert_body_note",
    "inert_line",
    "inert_prose",
    "unreadable_arguments_note",
]

CONTINUATION_PREFIX: Final = "  │ "
"""What a line of an :func:`inert_body` begins with when it follows a line
break of the text."""

WRAP_PREFIX: Final = "  ┆ "
"""What a line of an :func:`inert_body` begins with when it continues a line
of the text that was wider than the ``width`` asked for. As long as
:data:`CONTINUATION_PREFIX`."""

LINE_BREAK_MARK: Final = " ↵ "
"""What a line break is drawn as by :func:`inert_line`."""

MAX_APPROVAL_BODY_LINES: Final = 400
"""Lines an :func:`approval_body` keeps before it cuts, by default."""

MAX_APPROVAL_BODY_CHARS: Final = 12_000
"""Characters an :func:`approval_body` keeps before it cuts, by default."""

TALL_APPROVAL_BODY_LINES: Final = 20
"""Lines past which an :func:`inert_body` begins with a note of its size."""

TALL_APPROVAL_BODY_CHARS: Final = 2000
"""Characters past which an :func:`inert_body` begins with a note of its
size."""

_ESCAPE_CHARS: Final = 6
"""Characters in one ``\\uXXXX`` escape."""

_MIN_WIDTH: Final = len(CONTINUATION_PREFIX) + _ESCAPE_CHARS
"""The narrowest ``width`` an :func:`inert_body` can keep: a prefix and one
escape."""


def inert_line(text: str) -> str:
    """*text* as one line that cannot act on the display showing it.

    A line break (``\\n`` or ``\\r\\n``) is drawn as :data:`LINE_BREAK_MARK`
    and a tab as a space; every other character
    :func:`~omicsclaw.tools.preview.escape_unsafe` treats as unsafe is
    written as its ``\\uXXXX`` escape.

    Args:
        text: The text to show.

    Returns:
        The line; ``""`` for ``""``.
    """
    flat = text.replace("\r\n", "\n").replace("\t", " ")
    return escape_unsafe(flat, keep="\n").replace("\n", LINE_BREAK_MARK)


def inert_prose(text: str) -> str:
    """*text* over as many lines as it has, none of them able to act on the display.

    Line breaks (``\\n``, with ``\\r\\n`` read as one) and tabs are kept;
    every other character :func:`~omicsclaw.tools.preview.escape_unsafe`
    treats as unsafe is written as its ``\\uXXXX`` escape. No line is
    prefixed and nothing is cut.

    Args:
        text: The text to show.

    Returns:
        The text; ``""`` for ``""``.
    """
    return escape_unsafe(text.replace("\r\n", "\n"), keep="\n\t")


def inert_body(
    text: str,
    *,
    max_lines: int,
    max_chars: int,
    width: int | None = None,
) -> tuple[str, bool]:
    """*text* as the body of a card, and whether it was cut.

    Characters are made inert as by :func:`inert_prose`. Every run of two
    or more blank lines, empty or of spaces and tabs only, is shown as one
    empty line. Every line that follows a line break of *text* begins with
    :data:`CONTINUATION_PREFIX`; the first has no prefix, because it follows
    the card's header on the line the caller has started.

    A body that would show more than *max_lines* lines or *max_chars*
    characters, counted as shown, after folding and escaping, is cut to fit
    both. A line is cut only between the characters it is written with,
    never inside an escape, and a line of which only the start is shown
    ends with ``…``.

    The first line begins with a note, the one :func:`inert_body_note`
    returns, when the body was cut, when blank lines were folded, or when
    it has more than :data:`TALL_APPROVAL_BODY_LINES` lines or shows more
    than :data:`TALL_APPROVAL_BODY_CHARS` characters. The note counts lines
    and characters as *text* is written, before escaping, folding and
    cutting; a line break is ``\\n`` and a CRLF is one line break of two
    characters: ``[502 lines, 518 characters; 499 blank lines folded]``.
    When the body was cut, it gives what is shown of those in the same
    units, a folded run counting as all the lines it stands for and a line
    of which only the start is shown counting as shown, and the blank lines
    folded are those among the lines shown: ``[showing 64 of 70 lines, 77
    of 89 characters; 49 blank lines folded]``.

    With *width*, every line of the result is then broken into pieces of at
    most *width* characters, its prefix included. A piece that continues a
    line begins with :data:`WRAP_PREFIX`, and no ``\\uXXXX`` escape is
    split between two pieces. Cutting and the note do not depend on
    *width*: they are decided before any line is broken, and *max_lines*
    counts lines as *text* has them.

    Args:
        text: The text to show.
        max_lines: Lines shown before the cut.
        max_chars: Characters shown before the cut; the note, ``…`` and the
            prefixes are not counted.
        width: The most characters any line of the result may have, or
            ``None`` to leave lines as long as they are.

    Returns:
        The body, ``""`` for ``""``; and ``True`` when anything of it was
        cut, ``False`` when all of it is shown.

    Raises:
        ValueError: *max_lines* or *max_chars* is not a positive integer, or
            *width* is not an integer of at least 10, which is a prefix and
            one escape.
    """
    body = _body((text,) if text else (), max_lines, max_chars, width)
    return body.text, body.cut


def inert_body_note(text: str, *, max_lines: int, max_chars: int) -> str:
    """The note :func:`inert_body` begins the body of *text* with.

    Args:
        text: The text to show.
        max_lines: As for :func:`inert_body`.
        max_chars: As for :func:`inert_body`.

    Returns:
        The bracketed note, or ``""`` when the body carries none.

    Raises:
        ValueError: *max_lines* or *max_chars* is not a positive integer.
    """
    return _body((text,) if text else (), max_lines, max_chars, None).note


def approval_body(
    request: ApprovalRequest,
    *,
    max_lines: int = MAX_APPROVAL_BODY_LINES,
    max_chars: int = MAX_APPROVAL_BODY_CHARS,
    width: int | None = None,
) -> tuple[str, bool]:
    """The body of the approval card for *request*, and whether it was cut.

    The body is the request's reason followed, unless
    :attr:`~omicsclaw.tools.ApprovalRequest.reason_shows_call`, by a line
    ``arguments:`` and the call's arguments: indented JSON with sorted keys
    and every credential-named value hidden by
    :func:`~omicsclaw.tools.preview.redact_credentials`, or
    :func:`unreadable_arguments_note` when they cannot be read as JSON, and
    nothing for a blank payload or an empty object. It is rendered as
    :func:`inert_body` renders a text, with the bounds counted together
    over reason and arguments.

    Args:
        request: The request the card asks about.
        max_lines: Lines shown before the cut.
        max_chars: Characters shown before the cut; the note, ``…`` and the
            prefixes are not counted.
        width: As for :func:`inert_body`.

    Returns:
        The body, ``""`` when there is neither a reason nor arguments to
        show; and ``True`` when anything of it was cut, ``False`` when all
        of it is shown.

    Raises:
        ValueError: *max_lines* or *max_chars* is not a positive integer, or
            *width* is not an integer of at least 10.
    """
    body = _body(_approval_parts(request), max_lines, max_chars, width)
    return body.text, body.cut


def approval_body_note(
    request: ApprovalRequest,
    *,
    max_lines: int = MAX_APPROVAL_BODY_LINES,
    max_chars: int = MAX_APPROVAL_BODY_CHARS,
) -> str:
    """The note :func:`approval_body` begins *request*'s body with.

    Args:
        request: The request the card asks about.
        max_lines: As for :func:`approval_body`.
        max_chars: As for :func:`approval_body`.

    Returns:
        The bracketed note, or ``""`` when the body carries none.

    Raises:
        ValueError: *max_lines* or *max_chars* is not a positive integer.
    """
    return _body(_approval_parts(request), max_lines, max_chars, None).note


def unreadable_arguments_note(arguments: str) -> str:
    """What is shown in place of a tool call's arguments that are not JSON.

    Args:
        arguments: The raw payload.

    Returns:
        A note giving the payload's length and none of its content.
    """
    return f"(not shown: {len(arguments)} characters that could not be read as JSON)"


class _Body(NamedTuple):
    """A card's body, whether it was cut, and the note it opens with."""

    text: str
    cut: bool
    note: str


class _Row(NamedTuple):
    """One line of a card's body and the written lines it shows."""

    shown: str
    """The line as shown: escaped, or ``""`` for a folded run."""
    written: str
    """The written line it shows, without its line break; ``""`` for a run."""
    lines: int
    """Written lines it stands for: 1, or the length of a folded run."""
    chars: int
    """Written characters of those lines and of the line breaks among them."""
    after: int
    """Characters of the line break after it: 1, 2 for a CRLF, 0 at the end."""


_BLANK_LINE = re.compile(r"[ \t]*")

_CUT_MARK = "…"

_UNIT = re.compile(r"\\u[0-9a-fA-F]{4}|.", re.DOTALL)
"""What a line is broken between: one ``\\uXXXX`` escape, or one character."""


def _arguments_text(arguments: str) -> str:
    """*arguments* as indented JSON with credentials hidden, not yet escaped.

    ``""`` for a blank payload or an empty object;
    :func:`unreadable_arguments_note` when it is not JSON.
    """
    if not arguments.strip():
        return ""
    try:
        decoded = json.loads(arguments)
        if decoded == {}:
            return ""
        return json.dumps(
            redact_credentials(decoded), indent=2, sort_keys=True, ensure_ascii=False
        )
    except (ValueError, RecursionError):
        return unreadable_arguments_note(arguments)


def _approval_parts(request: ApprovalRequest) -> list[str]:
    """What an approval card's body is written from, each part on lines of its own.

    The reason when there is one, then ``arguments: `` and the arguments
    unless the reason shows the call or there are none to show.
    """
    parts = []
    if request.reason:
        parts.append(request.reason)
    if not request.reason_shows_call:
        arguments = _arguments_text(request.arguments)
        if arguments:
            parts.append(f"arguments: {arguments}")
    return parts


def _body(
    parts: Sequence[str], max_lines: int, max_chars: int, width: int | None
) -> _Body:
    """The body written from *parts*, as :func:`inert_body` describes it.

    Each part starts on a line of its own. A ``\\r`` that ends one part is a
    character of that part's last line, not half of a CRLF with the line
    break that separates it from the next part.
    """
    _require_positive("max_lines", max_lines)
    _require_positive("max_chars", max_chars)
    if width is not None:
        _require_width(width)
    if not parts:
        return _Body("", False, "")
    written = _written_lines(parts)
    rows = _fold(written)
    shown = _cut_rows(rows, max_lines, max_chars)
    total_lines = len(written)
    total_chars = sum(len(line) + after for line, after in written)
    drawn = sum(len(row.shown) for row in rows) + len(rows) - 1
    note = ""
    if (
        shown.cut
        or shown.folded
        or total_lines > TALL_APPROVAL_BODY_LINES
        or drawn > TALL_APPROVAL_BODY_CHARS
    ):
        if shown.cut:
            size = (
                f"showing {shown.lines} of {_counted(total_lines, 'line')}, "
                f"{shown.chars} of {_counted(total_chars, 'character')}"
            )
        else:
            size = (
                f"{_counted(total_lines, 'line')}, "
                f"{_counted(total_chars, 'character')}"
            )
        if shown.folded:
            size += f"; {_counted(shown.folded, 'blank line')} folded"
        note = f"[{size}]"
    kept = list(shown.rows) or [""]
    if note:
        kept[0] = f"{note} {kept[0]}"
    if width is None:
        return _Body(f"\n{CONTINUATION_PREFIX}".join(kept), shown.cut, note)
    return _Body(_broken(kept, width), shown.cut, note)


def _broken(rows: list[str], width: int) -> str:
    """*rows* joined into a body no line of which is longer than *width*.

    The first row starts without a prefix and every later row with
    :data:`CONTINUATION_PREFIX`; each piece that continues a row starts
    with :data:`WRAP_PREFIX`. Prefixes count towards *width*.
    """
    room = width - len(WRAP_PREFIX)
    lines: list[str] = []
    for index, row in enumerate(rows):
        prefix = CONTINUATION_PREFIX if index else ""
        for piece in _pieces(row, width - len(prefix), room):
            lines.append(f"{prefix}{piece}")
            prefix = WRAP_PREFIX
    return "\n".join(lines)


def _pieces(row: str, first: int, rest: int) -> list[str]:
    """*row* in order, the first piece at most *first* characters and each
    later one at most *rest*, broken only between :data:`_UNIT` matches.

    Always at least one piece, ``""`` for an empty row.
    """
    pieces: list[str] = []
    piece: list[str] = []
    used, room = 0, first
    for unit in _UNIT.findall(row):
        if used + len(unit) > room:
            pieces.append("".join(piece))
            piece, used, room = [], 0, rest
        piece.append(unit)
        used += len(unit)
    pieces.append("".join(piece))
    return pieces


def _counted(count: int, noun: str) -> str:
    """*count* and *noun*, the noun plural unless *count* is one."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class _Shown(NamedTuple):
    """What of a card's rows is shown, counted as written."""

    rows: tuple[str, ...]
    """The rows shown, the last ending with the cut mark when cut short."""
    lines: int
    """Written lines shown, a folded run counting as all of its lines."""
    chars: int
    """Written characters shown, with the line breaks among them."""
    folded: int
    """Blank lines folded away among the lines shown."""
    cut: bool
    """Whether anything was not shown."""


def _cut_rows(rows: list[_Row], max_lines: int, max_chars: int) -> _Shown:
    """The start of *rows* that fits *max_lines* rows and *max_chars* characters.

    Characters are counted as shown, one for each line break between rows;
    a row that does not fit is shown up to its last whole character that
    does, with :data:`_CUT_MARK` after it, or not at all when none does.
    """
    kept: list[str] = []
    budget = max_chars
    lines = chars = folded = 0
    partial = False
    for index, row in enumerate(rows[:max_lines]):
        if index:
            if budget < 1:
                break
            budget -= 1
        text, taken = row.shown, row.chars
        if len(text) > budget:
            text, taken = _start_of(row.written, budget)
            if not taken:
                break
            text += _CUT_MARK
            partial = True
        if index:
            chars += rows[index - 1].after
        kept.append(text)
        lines += row.lines
        chars += taken
        folded += row.lines - 1
        if partial:
            break
        budget -= len(text)
    cut = partial or len(kept) < len(rows)
    return _Shown(tuple(kept), lines, chars, folded, cut)


def _written_lines(parts: Sequence[str]) -> list[tuple[str, int]]:
    """The lines of *parts* joined by line breaks, each with its line break's length.

    A line is split at ``\\n``; the ``\\r`` of a ``\\r\\n`` within a part is
    counted in the line break, not the line. The last line's break is 0.
    """
    written: list[tuple[str, int]] = []
    for number, part in enumerate(parts):
        lines = part.split("\n")
        for index, line in enumerate(lines):
            if index < len(lines) - 1:
                if line.endswith("\r"):
                    written.append((line[:-1], 2))
                else:
                    written.append((line, 1))
            else:
                written.append((line, 1 if number < len(parts) - 1 else 0))
    return written


def _fold(written: list[tuple[str, int]]) -> list[_Row]:
    """*written* as rows, every run of two or more blank lines one empty row."""
    rows: list[_Row] = []
    blank: list[tuple[str, int]] = []

    def close_run() -> None:
        if len(blank) < 2:
            rows.extend(_Row(line, line, 1, len(line), after) for line, after in blank)
        else:
            chars = sum(len(line) + after for line, after in blank) - blank[-1][1]
            rows.append(_Row("", "", len(blank), chars, blank[-1][1]))
        blank.clear()

    for line, after in written:
        if _BLANK_LINE.fullmatch(line):
            blank.append((line, after))
            continue
        close_run()
        rows.append(_Row(inert_prose(line), line, 1, len(line), after))
    close_run()
    return rows


def _start_of(line: str, budget: int) -> tuple[str, int]:
    """The longest start of *line* whose escaped form fits *budget*.

    Returns:
        That start escaped, and how many characters of *line* it holds.
    """
    width = 0
    for count, char in enumerate(line):
        width += len(inert_prose(char))
        if width > budget:
            return inert_prose(line[:count]), count
    return inert_prose(line), len(line)


def _require_positive(name: str, value: int) -> None:
    """Raise :exc:`ValueError` unless *value* is a positive integer."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer, not {value!r}")


def _require_width(width: int) -> None:
    """Raise :exc:`ValueError` unless *width* is an integer, :data:`_MIN_WIDTH` or more."""
    if isinstance(width, bool) or not isinstance(width, int) or width < _MIN_WIDTH:
        raise ValueError(
            f"width must be an integer of at least {_MIN_WIDTH}, the length of "
            f"a line prefix and one escape, not {width!r}"
        )
