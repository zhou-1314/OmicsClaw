"""Where a line of input comes from — which is a parameter, not a fact.

The reference harness's CLI is ``runCLI(ctx, eng, io.Reader, idx)``
(``cli.go:36``): the reader is an argument, so the loop can be driven by a
test without a terminal. This module is that argument. :class:`Repl
<omicsclaw.entry.cli._repl.Repl>` never touches :data:`sys.stdin` and
never imports ``prompt_toolkit``; it asks a :class:`PromptSource` for the
next line and is told when there are no more.

Three implementations, in the order a deployment prefers them:

:class:`PromptToolkitSource` — history, completion, a styled prompt. Built
by :func:`open_prompt_source` **only** when standard input is a terminal
and the package is installed.

:class:`StreamSource` — ``readline`` on any text stream. What a pipe gets,
and what ``oc cli < script.txt`` gets. The read happens on a daemon
thread of its own, so that a blocking ``readline`` stops neither the
event loop the session registry's lanes live on nor that loop's
shutdown.

:class:`ScriptedSource` — a fixed list, for tests and for
``--prompt-file``.

**A source is read from more than one task at a time.** :class:`Repl
<omicsclaw.entry.cli._repl.Repl>` answers each approval request from its
own Task so that the event pump never stops consuming, and one model
message may carry two calls to concurrency-safe tools that both ask —
``web_fetch`` and ``web_search`` are exactly that pair. Concurrent
:meth:`PromptSource.read` is therefore part of the contract, and a source
backed by **one** device has to queue its readers rather than let them
collide: ``prompt_toolkit`` asserts ``Application is already running`` on
the second overlapping prompt, and two ``readline`` threads on one stdin
hand the same typed line to whichever wakes first.

**A terminal keeps what is typed while nothing is reading.** Whole lines,
half a line, a line handed to a reader that was then cancelled: the next
:meth:`PromptSource.read` gets all of it. The REPL's own prompt wants
that, and a card does not, because a ``y`` typed while a tool was running
would answer the approval card that opens afterwards. A card therefore
reads through :class:`FreshSource` where the source offers it.

**Nothing here is imported at module scope that is not installed
everywhere** (plan 0031 trap 13). ``prompt_toolkit`` is imported inside
:func:`open_prompt_source` in plainly visible ``import`` syntax — not
through :func:`importlib.import_module`, which is the form plan 0028's
handover records as the one a static check cannot see — and the fallback
to :class:`StreamSource` is what makes the package usable without it.
"""

from __future__ import annotations

import asyncio
import struct
import sys
import threading
from typing import (
    Any,
    Callable,
    Iterable,
    Protocol,
    Sequence,
    TextIO,
    runtime_checkable,
)

from ._slash_command_support import (
    REPL_SLASH_COMMAND_SPECS,
    SlashCommandSpec,
    complete_slash_command_rows,
    slash_token,
)

__all__ = [
    "ChoiceSource",
    "FreshSource",
    "PromptSource",
    "PromptToolkitSource",
    "ScriptedSource",
    "StreamSource",
    "is_interactive",
    "open_prompt_source",
]


class PromptSource(Protocol):
    """One line of user input at a time, until there are none.

    May be read from several tasks at once. An implementation that owns a
    device only one reader can hold is required to serialize them itself
    — see this module's docstring for who calls it that way and why.
    """

    async def read(self, prompt: str) -> str:
        """The next line, without its newline.

        :raises EOFError: the input ended, or the source was closed while
            this read was queued behind another. A REPL treats this
            exactly as it treats ``/exit`` — the reference harness gives
            EOF and ``exit`` the same exit (``cli.go:36-79``).
        """
        ...

    def close(self) -> None:
        """Release whatever the source holds. Idempotent."""
        ...


@runtime_checkable
class ChoiceSource(Protocol):
    """A source that can also let a person pick one of several options.

    Optional: a surface checks ``isinstance(source, ChoiceSource)`` and
    falls back to printing the options when it is not one. Only
    :class:`PromptToolkitSource` implements it; a line-oriented source
    does not, because reading "the next line" as a choice would consume a
    piped script's next question.
    """

    async def choose(
        self, message: str, options: Sequence[str], *, default: int = 0
    ) -> int | None:
        """The index of the option picked, or ``None`` if the person declined.

        :raises EOFError: the source was closed.
        :raises NotImplementedError: this installation cannot show a picker.
        """
        ...


@runtime_checkable
class FreshSource(Protocol):
    """A source that can read only what is typed from now on.

    Optional, like :class:`ChoiceSource`: a surface checks
    ``isinstance(source, FreshSource)`` and reads with
    :meth:`PromptSource.read` when it is not one. Both terminal sources
    implement it. :class:`ScriptedSource` does not: its lines were all
    written before anything was asked, and they are the answers.
    """

    async def read_fresh(
        self,
        prompt: str,
        *,
        discarded: Callable[[], None] | None = None,
        unfinished: Callable[[], None] | None = None,
    ) -> str:
        """The next line typed after *prompt* is shown.

        What was typed earlier is thrown away once this reader holds the
        terminal. With readers queued, that happens as this one's prompt
        opens, which can be long after the call.

        A line begun before the prompt and finished at it was not typed
        after the prompt either. A source that can see such a line throws
        the whole of it away: what is typed at the prompt up to the next
        Enter, that Enter included, and only a line typed after that is
        returned. A source that cannot see it returns the rest of the
        line, and says which it is in its own ``read_fresh``.

        :param prompt: as for :meth:`PromptSource.read`.
        :param discarded: called once, before the prompt is shown, when
            the source saw that there was something to throw away.
        :param unfinished: called once, after *discarded* and before the
            prompt is shown, when the source saw that the last line among
            it had no Enter yet.
        :returns: the line, without its newline.
        :raises EOFError: as :meth:`PromptSource.read`.
        """
        ...

    def withdraw(self) -> None:
        """Take down the prompt of a read that was just cancelled.

        Afterwards the cursor is at the start of a line, and nothing
        half-typed at that prompt is left for a later read.
        """
        ...


class ScriptedSource:
    """A fixed sequence of lines, then :exc:`EOFError`.

    The double every test in ``tests/entry/test_cli_repl.py`` drives the
    loop with, and also what ``--prompt-file`` uses in production: one
    element, the whole file (see
    :func:`~omicsclaw.entry.cli.__main__.single_prompt`).

    Not a :class:`FreshSource`: a card reads the next line of the script
    like any other prompt.
    """

    __slots__ = ("_lines", "prompts")

    def __init__(self, lines: Iterable[str]) -> None:
        self._lines = list(lines)
        self.prompts: list[str] = []
        """Every prompt string that was shown, in order. A test asserting
        that the loop came back to the prompt after a cancellation is
        asserting on this."""

    async def read(self, prompt: str) -> str:
        """The next line, **suspending once** on the way.

        The :func:`asyncio.sleep` is not decoration, and it is the same
        trade ``InMemorySessionStore.save`` documents: every real source
        suspends — a worker thread, a terminal, a socket — and one that
        never yields hides a defect rather than exposing it. A loop that
        stopped exiting on EOF would spin here without ever reaching the
        event loop, so the :func:`asyncio.wait_for` that every test in
        this repository wraps its awaits in would never get to fire and
        the run would hang instead of failing. Costing one loop iteration
        to keep a hang a *failure* is worth it on a machine with no
        timeout plugin installed.
        """
        await asyncio.sleep(0)
        self.prompts.append(prompt)
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)

    def close(self) -> None:
        self._lines.clear()


class StreamSource:
    """``readline`` on a text stream, off the event loop's thread.

    Each ``readline`` runs on a daemon thread of its own, and its line is
    handed back to the loop. A ``readline`` still waiting holds up neither
    the loop, where the registry's lane pumps and any MCP connection live,
    nor the loop's shutdown, so the process can exit while its input stays
    open. The thread cannot be interrupted while it waits; it ends at the
    next line or at end of input.

    One ``readline`` at a time, because there is one stream. A read
    cancelled while its ``readline`` still waits leaves that ``readline``
    running, and the next read returns its line rather than starting
    another: every line read is returned to exactly one reader, in the
    order the stream gave them. A line read after :meth:`close`, or after
    the loop has closed, is discarded.

    :meth:`read_fresh` is the exception to "every line": at a terminal it
    throws away what was typed before its prompt.
    """

    __slots__ = (
        "_closed",
        "_echo",
        "_left_open",
        "_pending",
        "_reading",
        "_stream",
        "_write",
    )

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        echo: TextIO | None = None,
    ) -> None:
        """*echo* receives the prompt string; ``None`` prints nothing.

        A piped run wants no prompt in its output, an interactive fallback
        wants one, and which it is is the caller's to know.
        """
        self._stream = stream if stream is not None else sys.stdin
        self._write = echo
        self._closed = False
        self._reading = asyncio.Lock()
        self._pending: asyncio.Future[str] | None = None
        # The echo stream, while the last prompt written to it belongs to
        # a read that was cancelled and the cursor still sits on its line.
        self._left_open: TextIO | None = None

    async def read(self, prompt: str) -> str:
        """The next line, waiting for any earlier reader to be answered.

        The closed check is repeated inside the lock: a source closed
        while this call was queued has no line left to give, and saying so
        with :exc:`EOFError` is what lets a caller fail closed instead of
        waiting on a stream nobody owns.

        :param prompt: Written to the echo stream first, if there is one.
        :returns: The line, without its line ending.
        :raises EOFError: At the end of the stream, or once closed.
        :raises asyncio.CancelledError: When the awaiting Task is
            cancelled; a line still being read goes to the next read.
        :raises Exception: Whatever ``readline`` raised.
        """
        return await self._read(prompt, fresh=False, discarded=None)

    async def read_fresh(
        self,
        prompt: str,
        *,
        discarded: Callable[[], None] | None = None,
        unfinished: Callable[[], None] | None = None,
    ) -> str:
        """The next line typed after *prompt* is shown, at a terminal.

        Once this reader holds the stream, and before the prompt is
        echoed, what was typed earlier is thrown away: lines waiting in
        the terminal's input queue, a line the terminal holds half-typed,
        and a line that a ``readline`` left running by a cancelled read
        has already returned. *discarded* is called when a whole line was
        thrown away. A half-typed line goes without the call, because the
        terminal reports nothing of a line until Enter.

        For the same reason this source cannot tell that a line was left
        unfinished. *unfinished* is never called, and what is typed at
        the prompt to finish such a line is returned as the line: after
        ``ye`` typed early, ``s`` and Enter at the prompt return ``s``.

        A stream that is not a terminal has no earlier and later: the
        lines of a pipe or a file were all written in advance, in the
        order their author meant them. They are handed out in that order,
        as :meth:`read` does, and *discarded* is never called.

        :param prompt: Written to the echo stream first, if there is one.
        :param discarded: Called at most once, before the prompt.
        :param unfinished: Accepted for :class:`FreshSource` and never
            called.
        :returns: The line, without its line ending.
        :raises EOFError: At the end of the stream, or once closed.
        :raises asyncio.CancelledError: As :meth:`read`.
        :raises Exception: Whatever ``readline`` raised.
        """
        return await self._read(prompt, fresh=True, discarded=discarded)

    async def _read(
        self,
        prompt: str,
        *,
        fresh: bool,
        discarded: Callable[[], None] | None,
    ) -> str:
        """One line for *prompt*; see :meth:`read` and :meth:`read_fresh`."""
        if self._closed:
            raise EOFError
        async with self._reading:
            if self._closed:
                raise EOFError
            if fresh and is_interactive(self._stream):
                if await self._drop_typed() and discarded is not None:
                    discarded()
            if self._write is not None:
                self._write.write(prompt)
                self._write.flush()
            self._left_open = None
            if self._pending is None:
                self._pending = _readline_on_a_thread(self._stream)
            pending = self._pending
            try:
                line = await asyncio.shield(pending)
            except asyncio.CancelledError:
                if pending.cancelled():
                    self._pending = None
                self._left_open = self._write
                raise
            except BaseException:
                self._pending = None
                raise
            self._pending = None
        if line == "":
            raise EOFError
        return line.rstrip("\n").rstrip("\r")

    async def _drop_typed(self) -> bool:
        """Throw away what was typed at the terminal before now.

        The input queue is emptied first. A ``readline`` left running by
        a cancelled read is then given :data:`_HANDOVER_S` to report. One
        that reports had its line before the queue was emptied, and the
        line is dropped. One that stays silent is still waiting for a
        line nobody has typed, and is kept for the caller to read.

        :returns: Whether a whole line was thrown away.
        """
        dropped = _flush_typed(self._stream)
        pending = self._pending
        if pending is not None:
            done, _waiting = await asyncio.wait({pending}, timeout=_HANDOVER_S)
            if done:
                self._pending = None
                pending.exception()  # read, so the loop does not report it
                dropped = True
        return dropped

    def withdraw(self) -> None:
        """End the line that a cancelled read left its prompt on.

        The prompt was echoed without a newline and the Enter that would
        have ended the line never came. This writes that newline, and
        empties the terminal's input queue of whatever was half-typed at
        the prompt. It does nothing when the last read was answered, when
        the cancelled read was still queued and had shown no prompt, or
        when prompts are not echoed.
        """
        echo, self._left_open = self._left_open, None
        if echo is None:
            return
        _flush_typed(self._stream)
        echo.write("\n")
        echo.flush()

    def close(self) -> None:
        self._closed = True


_HANDOVER_S = 0.05
"""How long :meth:`StreamSource.read_fresh` waits, before it shows its
prompt, for a ``readline`` left running by an earlier read to report a
line it already holds.

The thread reports as soon as it gets the interpreter back: well under a
millisecond on an idle loop, up to one switch interval (5 ms by default)
on a busy one. A line that arrives during the wait was typed before the
prompt was shown, so it is dropped whichever side of the wait's start it
was typed on."""


def _flush_typed(stream: TextIO) -> bool:
    """Empty the input queue of the terminal behind *stream*.

    Does nothing when *stream* has no terminal behind it, and on a
    platform that has no ``termios``.

    :param stream: The stream a source reads.
    :returns: Whether at least one whole line was waiting and was
        emptied. A half-typed line is emptied with the rest and is not
        counted, because the terminal reports nothing of a line until
        Enter.
    """
    try:
        import fcntl
        import termios
    except ImportError:
        return False
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    try:
        counted = fcntl.ioctl(descriptor, termios.FIONREAD, struct.pack("i", 0))
        waiting = struct.unpack("i", counted)[0] > 0
    except OSError:
        waiting = False
    try:
        termios.tcflush(descriptor, termios.TCIFLUSH)
    except (OSError, termios.error):
        return False
    return waiting


def _readline_on_a_thread(stream: TextIO) -> "asyncio.Future[str]":
    """Start ``stream.readline()`` on a daemon thread; its outcome, as a future.

    The future belongs to the running loop and is set with the line, or
    with the exception ``readline`` raised. If the loop has closed by the
    time ``readline`` returns, the outcome is discarded.

    :param stream: The stream to read one line from.
    :returns: A future for the line.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def settle(line: str, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(line)

    def read() -> None:
        try:
            line, error = stream.readline(), None
        except BaseException as raised:  # noqa: BLE001 - handed to the reader
            line, error = "", raised
        try:
            loop.call_soon_threadsafe(settle, line, error)
        except RuntimeError:  # the loop has closed
            pass

    threading.Thread(target=read, name="omicsclaw-stdin", daemon=True).start()
    return future


_DRAIN_READS = 1024
"""The most reads :func:`_drop_keys` makes of one input. Each returns up
to 1024 bytes, so about a megabyte of typed-ahead input is thrown away
and a terminal that never stops sending cannot hold the event loop."""


def _drop_keys(keys: Any, begun: bool = False) -> tuple[bool, bool]:
    """Throw away the key presses waiting at a ``prompt_toolkit`` input.

    They wait in two places. ``prompt_toolkit`` keeps the keys it read
    past the end of one prompt and feeds them to the next. The terminal
    queues what is typed while no prompt is open, and in raw mode a read
    returns all of it, a half-typed line included.

    :param keys: The session's ``Input``.
    :param begun: Whether a line was unfinished before any of these keys
        were pressed, begun at a prompt that has since been taken down.
    :returns: Whether a person had pressed any of them, and whether a
        line is left unfinished: a character or a paste after the last
        Enter, or, when there is no Enter among them, *begun*. A key that
        puts no text on the line, such as an arrow or an Escape, starts
        no line. What the terminal sent of its own accord is thrown away
        without counting (see :func:`_pressed_by_a_person`).
    """
    from prompt_toolkit.input.typeahead import get_typeahead
    from prompt_toolkit.keys import Keys

    waiting = list(get_typeahead(keys))
    with keys.raw_mode():
        for _ in range(_DRAIN_READS):
            # ``flush_keys`` hands over a sequence the parser was still
            # holding open, such as an Escape pressed on its own. Left
            # there, it would join the first key typed at the prompt.
            presses = keys.read_keys() or keys.flush_keys()
            if not presses:
                break
            waiting.extend(presses)
    pressed = _pressed_by_a_person(waiting)
    unfinished = begun
    for press in pressed:
        if press.key in (Keys.ControlM, Keys.ControlJ):
            # Enter. A terminal in its line mode stores it as a line feed.
            unfinished = False
        elif not isinstance(press.key, Keys) or press.key is Keys.BracketedPaste:
            unfinished = True
    return bool(pressed), unfinished


def _pressed_by_a_person(waiting: Sequence[Any]) -> list[Any]:
    """*waiting* without what the terminal sent of its own accord.

    Two kinds of key press are left out. A cursor position report, which
    ``prompt_toolkit`` recognises. And a control sequence it does not
    recognise, such as the focus report (``ESC [ I``) of a terminal that
    was left reporting focus: read as typed, its characters would look
    like the start of a line. A sequence that was cut short is kept,
    because the rest of it will arrive at the prompt as characters.

    :param waiting: Key presses, in the order they were read.
    :returns: The ones a person pressed, in the same order.
    """
    from prompt_toolkit.keys import Keys

    pressed: list[Any] = []
    index = 0
    while index < len(waiting):
        after = _control_sequence_end(waiting, index)
        if after:
            index = after
            continue
        if waiting[index].key is not Keys.CPRResponse:
            pressed.append(waiting[index])
        index += 1
    return pressed


def _control_sequence_end(presses: Sequence[Any], start: int) -> int:
    """Where the unrecognised control sequence at ``presses[start]`` ends.

    ``prompt_toolkit`` hands such a sequence over one key press a byte:
    an Escape, ``[``, any number of parameter and intermediate bytes
    (``0x20`` to ``0x3F``) and one final byte (``0x40`` to ``0x7E``).

    :returns: The index after the final byte. ``0`` when the key presses
        from *start* are not such a sequence, or stop before its final
        byte.
    """
    from prompt_toolkit.keys import Keys

    if presses[start].key is not Keys.Escape:
        return 0
    if start + 1 >= len(presses) or presses[start + 1].key != "[":
        return 0
    for index in range(start + 2, len(presses)):
        key = presses[index].key
        if isinstance(key, Keys):
            return 0
        if "\x40" <= key <= "\x7e":
            return index + 1
        if not "\x20" <= key <= "\x3f":
            return 0
    return 0


def _holds_text(session: Any) -> bool:
    """Whether text typed at *session*'s prompt is still there, not entered."""
    buffer = getattr(session, "default_buffer", None)
    return bool(getattr(buffer, "text", ""))


class _PickerCancelled(Exception):
    """Raised inside the picker by Esc or Ctrl-C, and caught by ``choose``."""


class PromptToolkitSource:
    """A real terminal prompt: history, suggestions, completion.

    Constructed only by :func:`open_prompt_source`, which is also the only
    place that imports ``prompt_toolkit``. The session object is held
    opaquely (``Any``) so that this class's annotations cost no import
    either.

    **One question on the terminal at a time.** A ``PromptSession`` owns a
    single ``Application`` and re-entering it trips
    ``assert not self._is_running`` — an :exc:`AssertionError` raised in
    whichever task asked second. That is not a cosmetic failure: the task
    that dies is the one that would have answered an approval, and an
    approval nobody answers is an exchange that waits forever, because a
    terminal deployment sets no approval deadline on purpose. The lock
    turns two simultaneous cards into two cards in a row.
    """

    __slots__ = ("_begun", "_reading", "_session")

    def __init__(self, session: Any) -> None:
        self._session = session
        self._reading = asyncio.Lock()
        # A prompt of this source was cancelled with a line begun at it
        # and not entered. The next read takes that line over.
        self._begun = False

    async def read(self, prompt: str) -> str:
        """Show *prompt* once the terminal is free, and return the answer.

        A line that a cancelled prompt left unfinished ends here: whatever
        is typed to finish it is part of the line this returns, and a
        later :meth:`read_fresh` knows nothing of it.

        :raises EOFError: the source was closed, including while this call
            was queued behind another question.
        """
        async with self._reading:
            session = self._session
            if session is None:
                raise EOFError
            self._begun = False
            return await self._prompt(session, prompt)

    async def _prompt(self, session: Any, prompt: str, *, begun: bool = False) -> str:
        """Show *prompt* and return the line typed at it.

        When the prompt is cancelled, whether it leaves a line unfinished
        is kept for the next read: it does when text was typed at it and
        not entered, and when *begun* says a line already was unfinished.
        The text itself goes with the prompt.

        :param begun: Whether what is typed at this prompt finishes a
            line begun before it.
        """
        try:
            return await session.prompt_async(prompt)
        except asyncio.CancelledError:
            self._begun = begun or _holds_text(session)
            raise

    async def read_fresh(
        self,
        prompt: str,
        *,
        discarded: Callable[[], None] | None = None,
        unfinished: Callable[[], None] | None = None,
    ) -> str:
        """Show *prompt* once the terminal is free, and return what is typed at it.

        Keys pressed before the prompt opens are thrown away first, whole
        lines and half lines alike: those the terminal queued while no
        prompt was open, and those ``prompt_toolkit`` read past the end of
        the previous prompt and kept for the next. *discarded* is called
        when there were any.

        When they left a line unfinished, the rest of that line goes with
        it. *unfinished* is called, the prompt is shown, and what is typed
        at it up to the next Enter is read and thrown away. The prompt is
        then shown a second time, for the line this returns. Until that
        Enter the prompt behaves as any other: Ctrl-C raises
        :exc:`KeyboardInterrupt`, and the text thrown away is written to
        the session's history like a line that was kept.

        A line is also unfinished when the last prompt of this source was
        cancelled with text typed at it and no Enter has been pressed
        since, as when a question's prompt is taken down at its deadline
        with half a word in it. That text went with its prompt, and both
        callbacks are called for it as for keys dropped here.

        :param discarded: Called at most once, before the prompt.
        :param unfinished: Called at most once, after *discarded* and
            before the prompt.
        :raises EOFError: the source was closed, including while this call
            was queued behind another question.
        """
        async with self._reading:
            session = self._session
            if session is None:
                raise EOFError
            begun, self._begun = self._begun, False
            typed, half_typed = _drop_keys(session.input, begun)
            if (typed or begun) and discarded is not None:
                discarded()
            if half_typed:
                if unfinished is not None:
                    unfinished()
                await self._prompt(session, prompt, begun=True)
            return await self._prompt(session, prompt)

    def withdraw(self) -> None:
        """Nothing to take down.

        A ``prompt_async`` that is cancelled redraws its prompt as
        finished, moves the cursor to the next line and forgets the text
        typed at it. That a line was begun there is kept by the read that
        was cancelled, for the next :meth:`read_fresh`.
        """

    async def choose(
        self, message: str, options: Sequence[str], *, default: int = 0
    ) -> int | None:
        """Show *options* as an arrow-key list and return the one picked.

        Up/Down (or ``j``/``k``, or a digit) move, Enter picks, Esc and
        Ctrl-C decline. Runs on the same input and output as the line
        prompt, and waits for the terminal like :meth:`read` does.

        :returns: the index of the option picked, or ``None`` if declined.
        :raises EOFError: the source was closed.
        :raises NotImplementedError: the installed ``prompt_toolkit`` has
            no ``ChoiceInput`` (it arrived in 3.0.52).
        """
        try:
            from prompt_toolkit.application import create_app_session
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.shortcuts.choice_input import ChoiceInput
        except ImportError as exc:
            raise NotImplementedError(
                "this prompt_toolkit has no ChoiceInput"
            ) from exc

        bindings = KeyBindings()

        @bindings.add("escape", eager=True)
        def _decline(event: Any) -> None:
            event.app.exit(exception=_PickerCancelled())

        async with self._reading:
            session = self._session
            if session is None:
                raise EOFError
            picker = ChoiceInput(
                message=message,
                options=[(index, text) for index, text in enumerate(options)],
                default=default,
                key_bindings=bindings,
                interrupt_exception=_PickerCancelled,
            )
            try:
                with create_app_session(
                    input=session.input, output=session.output
                ):
                    return await picker.prompt_async()
            except _PickerCancelled:
                return None

    def close(self) -> None:
        self._session = None


def _history_path() -> Any:
    """``~/.config/omicsclaw/history``, created if its directory is not.

    Ported from ``interactive.py``'s ``get_config_dir()`` usage (its lines
    2075-2077) minus two things: the SQLite store that function also
    served, and its ``XDG_CONFIG_HOME`` lookup. The lookup is dropped
    rather than moved because plan 0031 Q8 leaves **one** reader of the
    environment in this package and it is ``resolve_app_config``; a
    deployment that needs the history somewhere else should gain an
    ``AppConfig`` field, which is a decision with one home instead of two.
    """
    from pathlib import Path

    directory = Path.home() / ".config" / "omicsclaw"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "history"


def build_completer(
    *,
    specs: Sequence[SlashCommandSpec] = REPL_SLASH_COMMAND_SPECS,
) -> Any:
    """The slash-command and file-path completer.

    A line that is still its first token and names a command (see
    :func:`~omicsclaw.entry.cli._slash_command_support.slash_token`)
    completes against *specs*. Anything else completes its last word as a
    path when that word starts with ``./``, ``/`` or ``~/`` — including
    ``/data/ru``, which is a path and not a command.

    Imports ``prompt_toolkit`` in the body, so naming this function costs
    nothing.
    """
    from prompt_toolkit.completion import Completer, Completion, PathCompleter
    from prompt_toolkit.document import Document

    class _OmniCompleter(Completer):
        def __init__(self) -> None:
            self.path_completer = PathCompleter(expanduser=True)

        def get_completions(self, document: Document, complete_event: Any):
            text = document.text_before_cursor

            # 1. Slash commands
            if (
                text.startswith("/")
                and " " not in text.strip()
                and (text == "/" or slash_token(text) is not None)
            ):
                for cmd, desc in complete_slash_command_rows(text, specs):
                    if cmd.startswith(text):
                        yield Completion(
                            cmd,
                            start_position=-len(text),
                            display=f"{cmd:<20}",
                            display_meta=desc,
                        )
                return

            # 2. File path completion
            words = text.split(" ")
            last_word = words[-1]
            if last_word.startswith(("./", "/", "~/")):
                path_doc = Document(
                    text=last_word, cursor_position=len(last_word)
                )
                try:
                    completions = self.path_completer.get_completions(
                        path_doc, complete_event
                    )
                    for comp in completions:
                        # PathCompleter yields the rest of the name, not the
                        # whole word, so its own start position is kept.
                        yield Completion(
                            comp.text,
                            start_position=comp.start_position,
                            display=comp.display,
                            display_meta="File Path",
                        )
                except Exception:
                    pass

    return _OmniCompleter()


def is_interactive(stream: TextIO | None = None) -> bool:
    """Whether *stream* is a terminal a person is typing at.

    Args:
        stream: The input to test; ``None`` tests :data:`sys.stdin`.

    Returns:
        ``False`` for a pipe or a file, and for a stream that is closed or
        has no ``isatty``.
    """
    source = stream if stream is not None else sys.stdin
    try:
        return bool(source.isatty())
    except (AttributeError, ValueError):
        return False


def open_prompt_source(
    *,
    stream: TextIO | None = None,
    interactive: bool | None = None,
) -> PromptSource:
    """The best source this process can have, degrading rather than failing.

    Three outcomes, decided in this order:

    1. *not a terminal* — :class:`StreamSource`. The harness makes the
       same test at ``main.go:453-468`` before choosing between its TUI
       and its line reader, and it is what makes
       ``oc cli < questions.txt`` work.
    2. *a terminal, ``prompt_toolkit`` installed* —
       :class:`PromptToolkitSource`.
    3. *a terminal, not installed* — :class:`StreamSource` echoing the
       prompt, which is the ``input()`` experience without ``input()``'s
       habit of holding the loop's thread.

    The import sits in branch 2's body on purpose. A module-level import
    would make ``prompt_toolkit`` a hard dependency of *importing* this
    package, and plan 0031 §9-4 asserts in a subprocess that it is not.
    """
    source = stream if stream is not None else sys.stdin
    if interactive is None:
        interactive = is_interactive(source)
    if not interactive:
        return StreamSource(source)

    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.shortcuts import CompleteStyle
        from prompt_toolkit.styles import Style
    except ImportError:
        return StreamSource(source, echo=sys.stdout)

    # Ported from interactive.py's _COMPLETION_STYLE (its lines 347-353).
    style = Style.from_dict(
        {
            "completion-menu": "bg:default noreverse",
            "completion-menu.completion": "bg:default #888888",
            "completion-menu.completion.current": "bg:default default bold",
            "completion-menu.meta.completion": "bg:default #666666",
            "completion-menu.meta.completion.current": "bg:default #aaaaaa bold",
        }
    )
    session = PromptSession(
        history=FileHistory(str(_history_path())),
        auto_suggest=AutoSuggestFromHistory(),
        completer=build_completer(),
        complete_style=CompleteStyle.COLUMN,
        complete_while_typing=True,
        style=style,
    )
    return PromptToolkitSource(session)
