"""The prompt sources, read the way the REPL really reads them.

:class:`~omicsclaw.entry.cli._repl.Repl` answers every approval request
from its own Task, so a model message carrying two calls to
concurrency-safe ASK tools — ``web_fetch`` and ``web_search`` are the two
the registry mounts — puts two :meth:`PromptSource.read` calls in flight
at once. ``tests/entry/test_cli_repl.py`` drives that case through
:class:`~omicsclaw.entry.cli.ScriptedSource`, which is a list and happily
serves both; a terminal is not a list, and these tests are about the
difference.

There is no ``pytest-asyncio`` here, so every test drives
:func:`asyncio.run` itself and bounds every await with
:func:`asyncio.wait_for` — an unserialized source shows up as a hang, and
a hang with no timeout plugin is a test run that never finishes.
"""

from __future__ import annotations

import asyncio
import gc
import io
import logging
import os
import queue
import select
import sys
import threading
import time

import pytest

from omicsclaw.entry.cli import _input
from omicsclaw.entry.cli._input import (
    FreshSource,
    PromptToolkitSource,
    ScriptedSource,
    StreamSource,
    is_interactive,
)

WAIT_S = 10.0


class OneAtATime:
    """A ``PromptSession`` double with ``prompt_toolkit``'s own guard.

    ``Application.run_async`` opens with ``assert not self._is_running,
    "Application is already running."`` (prompt_toolkit 3.0.52), so a
    second overlapping prompt raises rather than queues. Reproducing the
    assertion rather than mocking it out is the point: a double that
    tolerates re-entry is exactly the double that reported this defect as
    working code.
    """

    def __init__(self, answers: list[str]) -> None:
        self._answers = answers
        self._running = False
        self.prompts: list[str] = []

    async def prompt_async(self, prompt: str) -> str:
        assert not self._running, "Application is already running."
        self._running = True
        try:
            self.prompts.append(prompt)
            await asyncio.sleep(0)  # a real prompt suspends; this must too
            return self._answers.pop(0)
        finally:
            self._running = False


def test_the_terminal_source_serializes_two_concurrent_readers():
    """Two cards at once become two cards in a row, not an AssertionError.

    Before the lock this raised ``Application is already running`` inside
    whichever approval Task asked second. That Task then never settled its
    request, and with no approval deadline at this surface the exchange
    waited for an answer that could no longer be given — the CLI froze
    with nothing on screen to say why.
    """

    async def drive():
        session = OneAtATime(["y", "n"])
        source = PromptToolkitSource(session)
        answers = await asyncio.wait_for(
            asyncio.gather(
                source.read("approve web_fetch? "),
                source.read("approve web_search? "),
            ),
            WAIT_S,
        )
        return answers, session.prompts

    answers, prompts = asyncio.run(drive())

    assert sorted(answers) == ["n", "y"]
    assert sorted(prompts) == [
        "approve web_fetch? ",
        "approve web_search? ",
    ]


class Held:
    """A ``PromptSession`` double the test decides when to answer.

    :attr:`asked` fires once a prompt is on screen and :attr:`release`
    lets it return, so a second reader can be put on the lock at a moment
    the test knows it is queued rather than at one it hopes it is.
    """

    def __init__(self, answer: str) -> None:
        self._answer = answer
        self.asked = asyncio.Event()
        self.release = asyncio.Event()

    async def prompt_async(self, prompt: str) -> str:
        self.asked.set()
        await self.release.wait()
        return self._answer


def test_the_terminal_source_refuses_a_reader_queued_when_it_closes():
    """A question waiting for a terminal that has gone is refused, not left.

    :meth:`PromptToolkitSource.close` drops the session while a second
    read may still be queued behind the first — which is what ``Ctrl-C``
    at an approval card does. ``EOFError`` is what the REPL already treats
    as "nobody is there", so the queued approval denies instead of waiting
    on a terminal this process no longer owns.
    """

    async def drive():
        session = Held("y")
        source = PromptToolkitSource(session)
        first = asyncio.ensure_future(source.read("approve web_fetch? "))
        await asyncio.wait_for(session.asked.wait(), WAIT_S)
        second = asyncio.ensure_future(source.read("approve web_search? "))
        await asyncio.sleep(0)  # the second reader is now on the lock
        source.close()
        session.release.set()
        assert await asyncio.wait_for(first, WAIT_S) == "y"
        with pytest.raises(EOFError):
            await asyncio.wait_for(second, WAIT_S)

    asyncio.run(drive())


class OverlapWatchingStream:
    """A stream that reports whether two ``readline`` calls were ever live.

    The sleep is what makes the answer meaningful: two worker threads
    started back to back would both be inside ``readline`` for the same
    50 ms, which is the overlap a real stdin resolves by handing one typed
    line to whichever thread the kernel happens to wake. Asserting on the
    returned lines alone cannot see that — with a
    :class:`io.StringIO` the two reads usually come out in order anyway.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self._guard = threading.Lock()
        self._live = 0
        self.overlapped = False

    def readline(self) -> str:
        with self._guard:
            self._live += 1
            self.overlapped = self.overlapped or self._live > 1
        time.sleep(0.05)
        with self._guard:
            self._live -= 1
            return self._lines.pop(0) if self._lines else ""


def test_the_stream_source_serializes_two_concurrent_readers():
    """One stdin, one ``readline`` at a time — answers in the order asked.

    Two ``asyncio.to_thread(readline)`` calls on one stream both block in
    ``read(2)``, and the line the person typed goes to whichever thread
    the kernel wakes: the question they answered is not necessarily the
    one their answer settles. Serialized, the first question gets the
    first line.
    """

    async def drive():
        stream = OverlapWatchingStream(["first\n", "second\n"])
        source = StreamSource(stream)
        first = asyncio.ensure_future(source.read("q1"))
        await asyncio.sleep(0)  # let the first reader take the lock
        second = asyncio.ensure_future(source.read("q2"))
        lines = await asyncio.wait_for(asyncio.gather(first, second), WAIT_S)
        return lines, stream.overlapped

    lines, overlapped = asyncio.run(drive())

    assert lines == ["first", "second"]
    assert not overlapped, "two readline threads were live on one stream"


class HeldStream:
    """A stream whose ``readline`` blocks on a real thread until released.

    ``StreamSource`` reads on a thread of its own, so the holding has to
    be a :class:`threading.Event` — an asyncio one would never be set by a
    loop that is waiting for this thread.
    """

    def __init__(self, line: str) -> None:
        self._line = line
        self.asked = threading.Event()
        self.release = threading.Event()

    def readline(self) -> str:
        self.asked.set()
        self.release.wait(WAIT_S)
        return self._line


def test_the_stream_source_refuses_a_reader_queued_when_it_closes():
    """The closed check is re-made inside the lock, not only before it.

    A reader that passed the check before queueing would otherwise go on
    to ``readline`` a stream the source has already been told to let go.
    """

    async def drive():
        stream = HeldStream("first\n")
        source = StreamSource(stream)
        first = asyncio.ensure_future(source.read("q1"))
        while not stream.asked.is_set():
            await asyncio.sleep(0.01)
        second = asyncio.ensure_future(source.read("q2"))
        await asyncio.sleep(0)  # the second reader is now on the lock
        source.close()
        stream.release.set()
        assert await asyncio.wait_for(first, WAIT_S) == "first"
        with pytest.raises(EOFError):
            await asyncio.wait_for(second, WAIT_S)

    asyncio.run(drive())


class Typist:
    """A stream whose lines arrive when the test types them, on a real thread.

    Counts ``readline`` calls, because a second call started while the
    first is still waiting is the collision the source exists to prevent.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.asked = threading.Event()
        self._lines: "queue.Queue[str]" = queue.Queue()

    def readline(self) -> str:
        self.calls += 1
        self.asked.set()
        try:
            return self._lines.get(timeout=WAIT_S)
        except queue.Empty:
            return ""

    def type(self, line: str) -> None:
        self._lines.put(line)


def test_a_line_typed_for_a_cancelled_read_goes_to_the_next_one():
    """A read is cancelled while its ``readline`` still waits: an approval
    question outlived by its exchange, or the REPL's prompt when the
    REPL is stopped. That ``readline`` cannot be taken back, and the
    line it returns has to go somewhere. It goes to the next read, which
    waits for it rather than starting a second ``readline`` on the same
    stream — so no line is lost, and lines are handed out in the order
    they were written. Through a worker per read, the late line was set
    on a cancelled future and dropped without a word, and the next read
    raced a second thread for the line after it."""

    async def drive():
        stream = Typist()
        source = StreamSource(stream)
        first = asyncio.ensure_future(source.read("q1"))
        while not stream.asked.is_set():
            await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.wait({first})
        stream.type("typed while the first read was waiting\n")
        second = await asyncio.wait_for(source.read("q2"), WAIT_S)
        return first.cancelled(), second, stream.calls

    cancelled, second, calls = asyncio.run(drive())

    assert cancelled
    assert second == "typed while the first read was waiting"
    assert calls == 1, "a second readline was started on the same stream"


def test_a_read_left_waiting_does_not_hold_up_the_loops_shutdown():
    """Standard input a pipe that never closes, and the REPL stopped while
    it waits for a line. The ``readline`` it left behind used to run in the
    loop's default executor, which ``asyncio.run`` waits for as it shuts
    down: the process stayed up, after its release had finished, until
    somebody closed its input."""
    stream = Typist()

    async def drive():
        source = StreamSource(stream)
        reading = asyncio.ensure_future(source.read("q"))
        while not stream.asked.is_set():
            await asyncio.sleep(0.01)
        reading.cancel()
        await asyncio.wait({reading})

    started = time.monotonic()
    try:
        asyncio.run(drive())
        elapsed = time.monotonic() - started
    finally:
        stream.type("")  # let the waiting thread finish

    assert elapsed < 2.0, f"the loop's shutdown waited {elapsed:.1f}s"


class Failing:
    """A stream whose every ``readline`` raises, as a hung-up terminal's does."""

    def __init__(self) -> None:
        self.calls = 0

    def readline(self) -> str:
        self.calls += 1
        raise OSError(5, "Input/output error")


def test_the_end_of_the_stream_and_its_errors_are_what_readline_says():
    """End of input is :exc:`EOFError`, and stays so; an error from
    ``readline`` is raised to the reader that asked, and the next read
    asks again rather than being handed the same failure."""

    async def drive():
        ended = StreamSource(io.StringIO("last\r\n"))
        assert await ended.read("q") == "last"
        for _ in range(2):
            with pytest.raises(EOFError):
                await asyncio.wait_for(ended.read("q"), WAIT_S)

        stream = Failing()
        broken = StreamSource(stream)
        for _ in range(2):
            with pytest.raises(OSError, match="Input/output error"):
                await asyncio.wait_for(broken.read("q"), WAIT_S)
        return stream.calls

    assert asyncio.run(drive()) == 2


# ---- the /resume picker ----------------------------------------------------


def _picker_source(keys):
    """A terminal source over a real ``PromptSession`` reading *keys*' pipe."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.output import DummyOutput

    return PromptToolkitSource(PromptSession(input=keys, output=DummyOutput()))


def _pick(typed: str, *, default: int = 0):
    """Run one ``choose`` over three options with *typed* as the keystrokes."""
    pytest.importorskip("prompt_toolkit.shortcuts.choice_input")
    from prompt_toolkit.input import create_pipe_input

    async def drive():
        with create_pipe_input() as keys:
            source = _picker_source(keys)
            keys.send_text(typed)
            return await asyncio.wait_for(
                source.choose("pick", ["a", "b", "c"], default=default), WAIT_S
            )

    return asyncio.run(drive())


@pytest.mark.parametrize(
    "typed, default, expected",
    [
        ("\x1b[B\r", 0, 1),  # Down, Enter
        ("j\r", 0, 1),
        ("\r", 2, 2),  # Enter takes the default
        ("\x1b", 0, None),  # Esc declines
        ("\x03", 0, None),  # Ctrl-C declines
    ],
    ids=["down-enter", "j-enter", "enter-default", "escape", "ctrl-c"],
)
def test_the_picker_answers_with_the_index_chosen_or_none(typed, default, expected):
    """Driven by real keystrokes through ``prompt_toolkit``'s own input."""
    assert _pick(typed, default=default) == expected


def test_a_closed_source_refuses_to_pick():
    pytest.importorskip("prompt_toolkit.shortcuts.choice_input")
    from prompt_toolkit.input import create_pipe_input

    async def drive():
        with create_pipe_input() as keys:
            source = _picker_source(keys)
            source.close()
            await source.choose("pick", ["a"])

    with pytest.raises(EOFError):
        asyncio.run(drive())


def test_a_prompt_toolkit_without_a_picker_says_so(monkeypatch):
    """``ChoiceInput`` arrived in 3.0.52; older installs must not crash."""
    pytest.importorskip("prompt_toolkit")
    import sys

    from prompt_toolkit.input import create_pipe_input

    monkeypatch.setitem(sys.modules, "prompt_toolkit.shortcuts.choice_input", None)

    async def drive():
        with create_pipe_input() as keys:
            await _picker_source(keys).choose("pick", ["a"])

    with pytest.raises(NotImplementedError):
        asyncio.run(drive())


def test_keys_go_to_the_question_asked_first():
    """The picker waits for a line prompt already on the terminal.

    Two ``prompt_toolkit`` applications on one input do not raise; the one
    attached last takes the keystrokes. Without the queue the picker, asked
    second, would swallow the answer typed for the prompt asked first.
    """
    pytest.importorskip("prompt_toolkit.shortcuts.choice_input")
    from prompt_toolkit.input import create_pipe_input

    async def drive():
        with create_pipe_input() as keys:
            source = _picker_source(keys)
            line = asyncio.create_task(source.read("> "))
            await asyncio.sleep(0.05)
            picked = asyncio.create_task(source.choose("pick", ["a", "b"]))
            await asyncio.sleep(0.05)
            keys.send_text("hello\r")
            answer = await asyncio.wait_for(line, WAIT_S)
            await asyncio.sleep(0.05)
            keys.send_text("\x1b[B\r")
            return answer, await asyncio.wait_for(picked, WAIT_S)

    assert asyncio.run(drive()) == ("hello", 1)


# ---- what was typed before a prompt opened --------------------------------
#
# A card reads with ``read_fresh``. The terminal tests below type through
# ``prompt_toolkit``'s pipe input or a real pseudo-terminal, so what is
# held is the kernel's and the library's behaviour and not a double's.

SETTLE_S = 0.2
"""Long enough for a prompt to read what is already waiting for it, and
longer than ``_HANDOVER_S``."""


def test_both_terminal_sources_read_fresh_and_a_script_does_not():
    """A script's lines were all written before anything was asked. A
    source that dropped them would leave every scripted card unanswered.

    Mutation: give ``ScriptedSource`` a ``read_fresh`` and the REPL's
    cards stop taking a test's lines.
    """
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.input import create_pipe_input

    with create_pipe_input() as keys:
        assert isinstance(_picker_source(keys), FreshSource)
    assert isinstance(StreamSource(io.StringIO("")), FreshSource)
    assert not isinstance(ScriptedSource(()), FreshSource)


def _fresh_at_the_terminal(typed_before: str, typed_after: str):
    """Type *typed_before*, open a fresh read, then type *typed_after*.

    :returns: whether the read was still waiting before *typed_after*,
        the line it returned, and how often it reported dropping input.
    """
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.input import create_pipe_input

    async def drive():
        with create_pipe_input() as keys:
            source = _picker_source(keys)
            dropped: list[int] = []
            keys.send_text(typed_before)
            reading = asyncio.create_task(
                source.read_fresh("approve? ", discarded=lambda: dropped.append(1))
            )
            await asyncio.sleep(SETTLE_S)
            waiting = not reading.done()
            keys.send_text(typed_after)
            return waiting, await asyncio.wait_for(reading, WAIT_S), len(dropped)

    return asyncio.run(drive())


@pytest.mark.parametrize(
    ("typed_before", "typed_after", "line", "reports"),
    [
        pytest.param("yes\r", "n\r", "n", 1, id="a whole line"),
        pytest.param("ye", "s\r", "s", 1, id="half a line"),
        pytest.param("a\rb\rc", "n\r", "n", 1, id="two lines and a half"),
        pytest.param("x" * 3000 + "\r", "n\r", "n", 1, id="more than one read holds"),
        pytest.param("\x1b", "y\r", "y", 1, id="an Escape on its own"),
        pytest.param("", "y\r", "y", 0, id="nothing"),
        pytest.param("\x1b[12;1R", "y\r", "y", 0, id="the terminal's own report"),
    ],
)
def test_a_fresh_read_at_the_terminal_takes_only_what_is_typed_after_it_opens(
    typed_before, typed_after, line, reports
):
    """``yes`` and Enter typed while a tool was running would otherwise
    answer the approval card that opens next. Half a line would join what
    is typed at the card. The report is made once however much was
    dropped, and not at all when nothing was typed: a cursor position
    report that arrives late is the terminal's and nobody typed it.

    Mutations: skip the reads in ``_drop_keys`` and the first three cases
    return what was typed before; read once instead of ``_DRAIN_READS``
    times and the 3000-character case returns its tail; leave out
    ``flush_keys`` and the Escape joins the ``y`` typed at the prompt;
    call *discarded* unconditionally and the ``nothing`` case reports;
    count every key press and the last case reports.
    """
    waiting, read, reported = _fresh_at_the_terminal(typed_before, typed_after)

    assert waiting, "the read was answered by what was typed before it"
    assert read == line
    assert reported == reports


def _two_lines_in_one_write(second: str):
    """Answer one prompt with two lines sent together, then read again.

    ``prompt_toolkit`` reads both lines in one chunk, accepts the first
    and keeps the second for its next prompt. *second* names the method
    the next read uses.
    """
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.input import create_pipe_input

    async def drive():
        with create_pipe_input() as keys:
            source = _picker_source(keys)
            keys.send_text("one\rtwo\r")
            first = await asyncio.wait_for(source.read("> "), WAIT_S)
            reading = asyncio.create_task(getattr(source, second)("> "))
            await asyncio.sleep(SETTLE_S)
            waiting = not reading.done()
            keys.send_text("three\r")
            return first, waiting, await asyncio.wait_for(reading, WAIT_S)

    return asyncio.run(drive())


def test_keys_the_library_kept_from_the_last_prompt_do_not_answer_a_fresh_read():
    """The second of two pasted lines is in ``prompt_toolkit``'s own
    type-ahead store, not in the terminal, by the time the next prompt
    opens.

    Mutation: drop ``get_typeahead`` from ``_drop_keys`` and the fresh
    read returns ``two``.
    """
    assert _two_lines_in_one_write("read_fresh") == ("one", True, "three")


def test_an_ordinary_read_still_gets_what_was_typed_ahead():
    """The REPL's own prompt keeps type-ahead: a line typed while the
    last answer was still printing is the next message."""
    first, _waiting, second = _two_lines_in_one_write("read")

    assert (first, second) == ("one", "two")


def test_a_queued_fresh_read_drops_what_was_typed_until_its_own_prompt_opens():
    """Two cards queue on the terminal. What is typed while the first
    prompt is open, past the line that answers it, was typed before the
    second prompt and does not answer the second card.

    Mutation: call ``_drop_keys`` before taking the lock in
    ``PromptToolkitSource.read_fresh`` and the second read returns
    ``two``.
    """
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.input import create_pipe_input

    async def drive():
        with create_pipe_input() as keys:
            source = _picker_source(keys)
            first = asyncio.create_task(source.read_fresh("first? "))
            await asyncio.sleep(0.05)
            second = asyncio.create_task(source.read_fresh("second? "))
            await asyncio.sleep(0.05)
            keys.send_text("one\rtwo\r")
            answered = await asyncio.wait_for(first, WAIT_S)
            await asyncio.sleep(SETTLE_S)
            waiting = not second.done()
            keys.send_text("three\r")
            return answered, waiting, await asyncio.wait_for(second, WAIT_S)

    assert asyncio.run(drive()) == ("one", True, "three")


def test_a_closed_terminal_source_refuses_a_fresh_read():
    """Mutation: skip the session check in ``read_fresh`` and this is an
    :exc:`AttributeError` from ``None.input``, which a card reports as a
    terminal that could not ask rather than as nobody being there."""
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.input import create_pipe_input

    async def drive():
        with create_pipe_input() as keys:
            source = _picker_source(keys)
            source.close()
            await source.read_fresh("approve? ")

    with pytest.raises(EOFError):
        asyncio.run(drive())


class _Pty:
    """A pseudo-terminal: the stream a source reads and a way to type at it.

    The secondary end is a real terminal in its default line mode, so
    ``isatty``, the input queue and the half-typed line are the kernel's.
    """

    def __init__(self) -> None:
        self._primary, secondary = os.openpty()
        self.stream = os.fdopen(secondary, "r", encoding="utf-8")

    def type(self, text: str) -> None:
        """Type *text*, and return once the terminal has taken it in.

        The kernel hands what is written here to the terminal on a worker
        of its own, a moment later. The terminal echoes each character as
        it takes it, so the echo coming back says the text has arrived.
        A person's typing is in that state long before the next prompt
        opens; a test that reads straight after writing is not.
        """
        typed = text.encode("utf-8")
        os.write(self._primary, typed)
        awaited = len(typed.replace(b"\n", b"\r\n"))
        deadline = time.monotonic() + WAIT_S
        while awaited > 0:
            ready, _, _ = select.select(
                [self._primary], [], [], max(0.0, deadline - time.monotonic())
            )
            assert ready, f"the terminal never echoed {text!r}"
            awaited -= len(os.read(self._primary, 4096))

    def close(self) -> None:
        """Hang up first, so a ``readline`` still waiting returns and
        lets go of the stream it is reading."""
        os.close(self._primary)
        try:
            self.stream.close()
        except OSError:
            pass


@pytest.fixture
def terminal():
    pytest.importorskip("termios")
    opened = _Pty()
    try:
        yield opened
    finally:
        opened.close()


PATIENT_S = 2.0
"""A wait for a leftover ``readline`` that a busy machine cannot outlast.
It ends as soon as the thread reports, so a test only spends it when the
thread has nothing to say."""


@pytest.fixture
def patient(monkeypatch):
    """Give a leftover ``readline`` :data:`PATIENT_S` to report.

    For tests that hold what happens once it has reported. How long the
    source itself waits is held by a test that does not use this.
    """
    monkeypatch.setattr(_input, "_HANDOVER_S", PATIENT_S)


async def _echoed(echo: io.StringIO, prompt: str) -> None:
    """Wait until *prompt* has been written to *echo*: its read has the
    stream, has dropped what it drops, and is now waiting for a line."""
    deadline = time.monotonic() + WAIT_S
    while prompt not in echo.getvalue():
        assert time.monotonic() < deadline, f"{prompt!r} was never shown"
        await asyncio.sleep(0.001)


def _new_readers(known: set[threading.Thread]) -> int:
    """How many ``readline`` threads are alive that are not in *known*."""
    return sum(
        1
        for thread in threading.enumerate()
        if thread.name == "omicsclaw-stdin" and thread not in known
    )


@pytest.mark.parametrize(
    ("typed_before", "typed_after", "line", "reports"),
    [
        pytest.param("yes\n", "n\n", "n", 1, id="a whole line"),
        pytest.param("a\nb\n", "n\n", "n", 1, id="two lines"),
        pytest.param("ye", "s\n", "s", 0, id="half a line"),
        pytest.param("", "y\n", "y", 0, id="nothing"),
    ],
)
def test_a_fresh_read_of_a_terminal_stream_takes_only_what_is_typed_after_it_opens(
    terminal, typed_before, typed_after, line, reports
):
    """The source used when ``prompt_toolkit`` is not installed. The
    terminal holds a half-typed line back until Enter, so that case is
    dropped without a report.

    Mutation: skip ``termios.tcflush`` in ``_flush_typed`` and the first
    three cases return what was typed before.
    """

    async def drive():
        echo = io.StringIO()
        source = StreamSource(terminal.stream, echo=echo)
        dropped: list[int] = []
        terminal.type(typed_before)
        reading = asyncio.create_task(
            source.read_fresh("approve? ", discarded=lambda: dropped.append(1))
        )
        await _echoed(echo, "approve? ")
        await asyncio.sleep(SETTLE_S)
        waiting = not reading.done()
        terminal.type(typed_after)
        return waiting, await asyncio.wait_for(reading, WAIT_S), len(dropped)

    waiting, read, reported = asyncio.run(drive())

    assert waiting, "the read was answered by what was typed before it"
    assert read == line
    assert reported == reports


def test_a_queued_fresh_read_of_a_terminal_stream_drops_until_its_own_prompt_opens(
    terminal,
):
    """As for the ``prompt_toolkit`` source: the second of two lines typed
    at the first card's prompt is still in the terminal's queue when the
    second card gets the stream, and does not answer it.

    Mutation: drop what was typed before taking the lock in
    ``StreamSource._read`` and the second read returns ``two``.
    """

    async def drive():
        echo = io.StringIO()
        source = StreamSource(terminal.stream, echo=echo)
        first = asyncio.create_task(source.read_fresh("first? "))
        await _echoed(echo, "first? ")
        second = asyncio.create_task(source.read_fresh("second? "))
        await asyncio.sleep(0.05)  # the second reader is now on the lock
        terminal.type("one\ntwo\n")
        answered = await asyncio.wait_for(first, WAIT_S)
        await _echoed(echo, "second? ")
        await asyncio.sleep(0.05)
        waiting = not second.done()
        terminal.type("three\n")
        return answered, waiting, await asyncio.wait_for(second, WAIT_S)

    assert asyncio.run(drive()) == ("one", True, "three")


def _after_a_cancelled_read(terminal, late: str, second: str):
    """Cancel a read, type *late*, then read with the method *second* names
    and, once that read's prompt is up, type ``y``.

    :returns: whether the second read was still waiting when its prompt
        came up, the line it returned, how often it reported dropping
        input, and how many ``readline`` threads the source had alive.
    """

    async def drive():
        known = set(threading.enumerate())
        echo = io.StringIO()
        source = StreamSource(terminal.stream, echo=echo)
        dropped: list[int] = []
        first = asyncio.create_task(source.read("answer> "))
        await _echoed(echo, "answer> ")
        first.cancel()
        await asyncio.wait({first})
        terminal.type(late)
        await asyncio.sleep(0.05)
        if second == "read_fresh":
            reading = asyncio.create_task(
                source.read_fresh("approve? ", discarded=lambda: dropped.append(1))
            )
        else:
            reading = asyncio.create_task(source.read("approve? "))
        await _echoed(echo, "approve? ")
        await asyncio.sleep(0.05)
        waiting, readers = not reading.done(), _new_readers(known)
        terminal.type("y\n")
        return waiting, await asyncio.wait_for(reading, WAIT_S), len(dropped), readers

    return asyncio.run(drive())


def test_a_line_a_cancelled_read_already_took_does_not_answer_a_fresh_read(
    terminal, patient
):
    """A question's prompt is taken down at its deadline and its
    ``readline`` stays behind. The reply typed too late is read by that
    thread, out of the terminal's queue, before the approval card opens.

    Mutation: keep the ``readline`` that reported as the pending one in
    ``StreamSource._drop_typed`` and the fresh read returns ``late``.
    """
    waiting, read, reported, _readers = _after_a_cancelled_read(
        terminal, "late\n", "read_fresh"
    )

    assert waiting, "the read was answered by the line typed before it"
    assert (read, reported) == ("y", 1)


def test_an_ordinary_read_still_gets_the_line_a_cancelled_read_took(terminal):
    """With no card open, the late line goes to the REPL's own prompt."""
    _waiting, read, reported, _readers = _after_a_cancelled_read(
        terminal, "late\n", "read"
    )

    assert (read, reported) == ("late", 0)


def test_a_fresh_read_reuses_a_readline_that_is_still_waiting(terminal):
    """Nothing was typed after the cancelled read, so its ``readline`` is
    still waiting and what it returns next was typed after the prompt. A
    second ``readline`` beside it would race it for the line.

    Mutation: forget the pending ``readline`` whether or not it reported
    and two threads are alive on one stream.
    """
    waiting, read, reported, readers = _after_a_cancelled_read(
        terminal, "", "read_fresh"
    )

    assert waiting
    assert (read, reported) == ("y", 0)
    assert readers == 1, "a second readline was started on the same stream"


def test_a_line_the_cancelled_read_has_not_reported_yet_is_dropped_too(
    terminal, patient
):
    """The ``readline`` thread has taken the line out of the terminal and
    the loop has not heard of it when the fresh read starts. The queue is
    already empty, so only waiting for the thread to report tells this
    line from one typed at the prompt.

    Mutation: look at the pending ``readline`` without waiting for it in
    ``StreamSource._drop_typed`` and the fresh read returns ``late``.
    """

    async def drive():
        echo = io.StringIO()
        source = StreamSource(terminal.stream, echo=echo)
        dropped: list[int] = []
        first = asyncio.create_task(source.read("answer> "))
        await _echoed(echo, "answer> ")
        first.cancel()
        await asyncio.wait({first})
        reading = asyncio.create_task(
            source.read_fresh("approve? ", discarded=lambda: dropped.append(1))
        )
        terminal.type("late\n")
        time.sleep(0.05)  # the thread takes the line; the loop has not run
        await _echoed(echo, "approve? ")
        await asyncio.sleep(0.05)
        waiting = not reading.done()
        terminal.type("y\n")
        return waiting, await asyncio.wait_for(reading, WAIT_S), len(dropped)

    assert asyncio.run(drive()) == (True, "y", 1)


def test_a_leftover_readline_is_given_longer_than_a_switch_interval_to_report(
    terminal,
):
    """The thread that holds a line reports it once it has the
    interpreter, and on a busy loop it can wait a whole switch interval
    for that. The fresh read waits longer than that before it shows its
    prompt, or a line taken just before the prompt would pass for one
    typed at it.

    Mutation: set ``_HANDOVER_S`` to zero and the prompt is shown at once.
    """

    async def drive():
        echo = io.StringIO()
        source = StreamSource(terminal.stream, echo=echo)
        first = asyncio.create_task(source.read("answer> "))
        await _echoed(echo, "answer> ")
        first.cancel()
        await asyncio.wait({first})
        started = time.monotonic()
        reading = asyncio.create_task(source.read_fresh("approve? "))
        await _echoed(echo, "approve? ")
        waited = time.monotonic() - started
        terminal.type("y\n")
        return waited, await asyncio.wait_for(reading, WAIT_S)

    waited, line = asyncio.run(drive())

    assert line == "y"
    assert waited > 2 * sys.getswitchinterval(), f"the prompt was shown after {waited:.4f}s"


class _FailsOnceAtATerminal:
    """A terminal stand-in whose first ``readline`` fails once released."""

    def __init__(self) -> None:
        self.calls = 0
        self.asked = threading.Event()
        self.release = threading.Event()

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        self.calls += 1
        if self.calls > 1:
            return "y\n"
        self.asked.set()
        self.release.wait(WAIT_S)
        raise OSError(5, "Input/output error")


def test_a_failure_a_cancelled_read_left_behind_is_dropped_with_its_line(
    caplog, patient
):
    """The ``readline`` left running fails before the fresh read starts.
    Its failure belongs to the earlier prompt: the fresh read does not
    raise it, and it is not left for the event loop to report when the
    future is collected.

    Mutation: do not read the dropped future's exception in
    ``StreamSource._drop_typed`` and the loop logs it as never retrieved.
    """

    async def drive():
        stream = _FailsOnceAtATerminal()
        source = StreamSource(stream)
        dropped: list[int] = []
        first = asyncio.ensure_future(source.read("answer> "))
        while not stream.asked.is_set():
            await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.wait({first})
        stream.release.set()
        line = await asyncio.wait_for(
            source.read_fresh("approve? ", discarded=lambda: dropped.append(1)),
            WAIT_S,
        )
        return line, len(dropped)

    with caplog.at_level(logging.ERROR):
        result = asyncio.run(drive())
        gc.collect()

    assert result == ("y", 1)
    assert "never retrieved" not in caplog.text


def test_a_stream_that_is_not_a_terminal_hands_a_fresh_read_its_lines_in_order():
    """``oc cli < script.txt``: the file's author wrote the answer to a
    card on the line after the request that raises it. Dropping it would
    leave every card in a piped run unanswered.

    Mutation: drop the ``is_interactive`` test from ``StreamSource._read``
    and the line a cancelled read left behind is dropped.
    """

    async def drive():
        dropped: list[int] = []
        report = lambda: dropped.append(1)  # noqa: E731
        piped = StreamSource(io.StringIO("do it\ny\n"))
        lines = [await piped.read("> "), await piped.read_fresh("approve? ", discarded=report)]

        stream = Typist()
        source = StreamSource(stream)
        first = asyncio.ensure_future(source.read("q1"))
        while not stream.asked.is_set():
            await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.wait({first})
        stream.type("written before the card\n")
        await asyncio.sleep(0.05)
        lines.append(
            await asyncio.wait_for(source.read_fresh("approve? ", discarded=report), WAIT_S)
        )
        return lines, dropped

    assert asyncio.run(drive()) == (["do it", "y", "written before the card"], [])


def test_withdrawing_ends_the_prompt_s_line_and_drops_what_was_half_typed(terminal):
    """A question's deadline passes with ``Lou`` typed and no Enter. The
    cursor is still after the prompt, and the terminal still holds
    ``Lou``, which would join the next line typed.

    Mutations: do not write the newline and the echo ends at the prompt;
    do not flush in ``withdraw`` and the next read returns ``Lou``; do not
    clear ``_left_open`` and the second call writes a second newline.
    """

    async def drive():
        echo = io.StringIO()
        source = StreamSource(terminal.stream, echo=echo)
        reading = asyncio.create_task(source.read_fresh("answer [#1]> "))
        await _echoed(echo, "answer [#1]> ")
        terminal.type("Lou")
        await asyncio.sleep(0.05)
        reading.cancel()
        await asyncio.wait({reading})
        source.withdraw()
        source.withdraw()
        shown = echo.getvalue()
        terminal.type("\n")
        return shown, await asyncio.wait_for(source.read("> "), WAIT_S)

    assert asyncio.run(drive()) == ("answer [#1]> \n", "")


def test_withdrawing_does_nothing_when_no_prompt_was_left_open():
    """After an answered read the person's own Enter ended the line. A
    read cancelled while it was queued never showed its prompt, and the
    one on screen belongs to the reader ahead of it. A source that echoes
    nothing has no line to end.

    Mutations: set ``_left_open`` on any cancellation and the queued case
    gains a newline; do not clear it when a prompt is echoed and the
    answered case does.
    """

    async def drive():
        answered_echo = io.StringIO()
        answered = StreamSource(io.StringIO("a\n"), echo=answered_echo)
        await answered.read("> ")
        answered.withdraw()

        stream = HeldStream("first\n")
        queued_echo = io.StringIO()
        queued = StreamSource(stream, echo=queued_echo)
        first = asyncio.ensure_future(queued.read("first> "))
        while not stream.asked.is_set():
            await asyncio.sleep(0.01)
        second = asyncio.ensure_future(queued.read("second> "))
        await asyncio.sleep(0)  # the second reader is now on the lock
        second.cancel()
        await asyncio.wait({second})
        queued.withdraw()
        stream.release.set()
        await asyncio.wait_for(first, WAIT_S)

        silent_stream = Typist()
        silent = StreamSource(silent_stream)
        reading = asyncio.ensure_future(silent.read("> "))
        while not silent_stream.asked.is_set():
            await asyncio.sleep(0.01)
        reading.cancel()
        await asyncio.wait({reading})
        silent.withdraw()
        silent_stream.type("")  # let the waiting thread finish
        return answered_echo.getvalue(), queued_echo.getvalue()

    assert asyncio.run(drive()) == ("> ", "first> ")


def test_a_cancelled_and_reopened_prompt_is_withdrawn_only_while_it_is_open():
    """The flag follows the last prompt echoed: a prompt answered after an
    earlier one was abandoned leaves nothing to take down.

    Mutation: do not clear ``_left_open`` in ``StreamSource._read`` and
    the withdrawal after the answered read writes a newline.
    """

    async def drive():
        stream = Typist()
        echo = io.StringIO()
        source = StreamSource(stream, echo=echo)
        first = asyncio.ensure_future(source.read("answer> "))
        while not stream.asked.is_set():
            await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.wait({first})
        stream.type("next message\n")
        line = await asyncio.wait_for(source.read("> "), WAIT_S)
        source.withdraw()
        return line, echo.getvalue()

    assert asyncio.run(drive()) == ("next message", "answer> > ")


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_a_terminal_with_no_descriptor_is_read_without_flushing():
    """A stand-in that says it is a terminal and has no file descriptor,
    as an embedded console does. The fresh read reads; it does not fail.

    Mutation: let ``fileno``'s error out of ``_flush_typed`` and the read
    raises :exc:`io.UnsupportedOperation`.
    """

    async def drive():
        source = StreamSource(_Terminal("y\n"))
        return await asyncio.wait_for(source.read_fresh("approve? "), WAIT_S)

    assert asyncio.run(drive()) == "y"


def _closed() -> io.StringIO:
    stream = io.StringIO()
    stream.close()
    return stream


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        pytest.param(_Terminal(), True, id="a terminal"),
        pytest.param(io.StringIO("piped\n"), False, id="a pipe"),
        pytest.param(_closed(), False, id="a closed stream"),
        pytest.param(object(), False, id="a stream with no isatty"),
    ],
)
def test_only_a_stream_that_says_it_is_a_terminal_is_interactive(stream, expected):
    """A closed stream raises :exc:`ValueError` from ``isatty`` and a
    stand-in for stdin may have no ``isatty`` at all. Neither has a person
    typing at it, and a surface told otherwise would wait at a question
    nobody can answer.

    Mutation: answer ``True`` from the ``except`` in ``is_interactive``
    and the last two cases fail.
    """
    assert is_interactive(stream) is expected


def test_a_process_with_no_stdin_is_not_interactive(monkeypatch):
    """``sys.stdin`` is ``None`` in a process started without one."""
    monkeypatch.setattr("sys.stdin", None)

    assert is_interactive() is False
