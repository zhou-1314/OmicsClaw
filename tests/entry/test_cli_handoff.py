"""The ``/handoff`` affordance (P6 item 5): URL shape, validation, wiring.

The REPL test drives a real :class:`~omicsclaw.entry.cli.Repl` over a
scripted app, the same way ``test_cli_commands.py`` does — the command is
wiring, and a double in the middle of wiring proves only that the double
was called.
"""

from __future__ import annotations

import asyncio

import pytest

from omicsclaw.entry.cli._constants import ADDED_SLASH_COMMANDS
from omicsclaw.entry.cli._handoff import (
    DEFAULT_HANDOFF_PORT,
    HANDOFF_SESSION_ID,
    handoff_url,
    parse_handoff_port,
)
from omicsclaw.entry.cli._slash_command_support import REPL_SLASH_COMMAND_SPECS
from omicsclaw.launch._surfaces import DESKTOP_PORT
from tests.entry.test_cli_repl import (  # type: ignore[import-not-found]
    WAIT_S,
    answering,
    build,
    repl_over,
)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, WAIT_S))


# ---- the URL ---------------------------------------------------------------


def test_the_url_carries_the_session_id_and_port():
    assert handoff_url("abc123") == "omicsclaw://session?id=abc123&port=8765"
    assert (
        handoff_url("abc123", port=9001)
        == "omicsclaw://session?id=abc123&port=9001"
    )


def test_the_default_port_is_the_desktop_default():
    # The URL's port field is advisory, but the number it defaults to
    # must be the port both surfaces already agree on — the desktop
    # backend's default bind, which the App spawns it with.
    assert DEFAULT_HANDOFF_PORT == DESKTOP_PORT == 8765


@pytest.mark.parametrize(
    "session_id",
    ["", " ", "a/b", "sess;drop", "x" * 65, "a" * 64 + ".", "?", "https://"],
)
def test_unsafe_session_ids_are_refused(session_id):
    with pytest.raises(ValueError):
        handoff_url(session_id)


def test_the_accepted_charset_covers_both_surfaces_ids():
    assert HANDOFF_SESSION_ID.fullmatch("a1b2c3d4")  # CLI: 8 hex
    assert HANDOFF_SESSION_ID.fullmatch("f" * 32)  # App: 32 hex
    assert HANDOFF_SESSION_ID.fullmatch("run7.visium-1")  # slug-shaped


@pytest.mark.parametrize("port", [0, -1, 65536, 2**31, 8765.5])
def test_bad_ports_are_refused(port):
    with pytest.raises(ValueError):
        handoff_url("abc123", port=port)


def test_parse_handoff_port_reads_bare_numbers_and_defaults():
    assert parse_handoff_port("") == DEFAULT_HANDOFF_PORT
    assert parse_handoff_port("   ") == DEFAULT_HANDOFF_PORT
    assert parse_handoff_port("9001") == 9001
    assert parse_handoff_port(" 9001 ") == 9001


@pytest.mark.parametrize("arg", ["x", "0", "65536", "87.65"])
def test_parse_handoff_port_refuses_non_ports(arg):
    with pytest.raises(ValueError):
        parse_handoff_port(arg)


# ---- the catalogue ---------------------------------------------------------


def test_handoff_is_in_the_catalogue_and_the_repl_menu():
    assert any(name == "/handoff" for name, _ in ADDED_SLASH_COMMANDS)
    assert "/handoff" in {spec.name for spec in REPL_SLASH_COMMAND_SPECS}


# ---- the REPL --------------------------------------------------------------


def test_the_repl_prints_the_url_and_keeps_going(tmp_path):
    app = build(tmp_path, answering("hi"))

    async def drive():
        repl, _source, buffer = repl_over(
            app, ["/handoff", "/exit"], session_id="abc123"
        )
        await asyncio.wait_for(repl.run(), WAIT_S)
        return buffer.getvalue()

    output = run(drive())
    assert "omicsclaw://session?id=abc123&port=8765" in output


def test_the_repl_honours_an_explicit_port(tmp_path):
    app = build(tmp_path, answering("hi"))

    async def drive():
        repl, _source, buffer = repl_over(
            app, ["/handoff 9001", "/exit"], session_id="abc123"
        )
        await asyncio.wait_for(repl.run(), WAIT_S)
        return buffer.getvalue()

    output = run(drive())
    assert "omicsclaw://session?id=abc123&port=9001" in output


def test_the_repl_refuses_a_bad_port_and_keeps_going(tmp_path):
    app = build(tmp_path, answering("hi"))

    async def drive():
        repl, _source, buffer = repl_over(
            app, ["/handoff nope", "/exit"], session_id="abc123"
        )
        await asyncio.wait_for(repl.run(), WAIT_S)
        return buffer.getvalue()

    output = run(drive())
    assert "not a port number" in output
    assert "omicsclaw://" not in output
