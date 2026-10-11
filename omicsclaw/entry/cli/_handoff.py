"""The ``/handoff`` URL the CLI prints to hand a session to the App (P6 item 5).

The URL is ``omicsclaw://session?id=<session_id>&port=<port>``. The id is
the **same** identifier the session carries in the shared workspace
database (``<workspace>/.omicsclaw/memory.db``) — the CLI and the desktop
backend both persist sessions there, so the App adopts the session by id
through its own backend and no history is shipped in the URL.

The port field is advisory, and that is a decision with evidence rather
than a guess. The CLI cannot know the port a *running* desktop server
was bound to; what it can say is the port both sides default to —
:data:`DEFAULT_HANDOFF_PORT` mirrors ``omicsclaw.launch._surfaces.
DESKTOP_PORT`` (8765), which the OmicsClaw App also spawns its backend
with (``src/lib/backend-contract.ts``). The App therefore treats the
field as a sanity hint — a mismatch between the URL and its configured
backend port is worth a warning, but routing always goes through the
App's own backend. ``/handoff <port>`` lets a user who serves on a
non-default port say so.
"""

from __future__ import annotations

import re

__all__ = [
    "DEFAULT_HANDOFF_PORT",
    "HANDOFF_SESSION_ID",
    "handoff_url",
    "parse_handoff_port",
]

DEFAULT_HANDOFF_PORT = 8765
"""The port the URL names when the user gives none. See the module
docstring for why this number and why the field is advisory."""

HANDOFF_SESSION_ID = re.compile(r"\A[0-9A-Za-z._-]{1,64}\Z")
"""The closed charset a session id must fit to enter a handoff URL.

The CLI's own ids are 8 hex characters and the desktop's are 32; the
wider set is the one the backend's ``_session_slug`` already tolerates,
and the length cap keeps the URL from carrying an unbounded payload."""


def handoff_url(session_id: str, port: int = DEFAULT_HANDOFF_PORT) -> str:
    """The handoff URL for one session.

    :param session_id: The persisted session identifier.
    :param port: The desktop port the URL advertises.
    :returns: ``omicsclaw://session?id=...&port=...``.
    :raises ValueError: The id is empty, out of charset or over length,
        or *port* is not a port number. A handoff URL is printed for a
        person to open; a value that cannot be a session id must not
        render one that looks valid.
    """
    if not HANDOFF_SESSION_ID.fullmatch(str(session_id)):
        raise ValueError(
            f"session id {session_id!r} is not safe to put in a handoff URL"
        )
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ValueError(f"{port!r} is not a port number")
    return f"omicsclaw://session?id={session_id}&port={port}"


def parse_handoff_port(arg: str) -> int:
    """The optional ``/handoff [port]`` argument as a port number.

    :param arg: The raw text after ``/handoff``; ``""`` means the
        default.
    :returns: A port in ``1..65535``.
    :raises ValueError: The argument is not that.
    """
    raw = (arg or "").strip()
    if not raw:
        return DEFAULT_HANDOFF_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{raw!r} is not a port number") from exc
    if not 0 < port < 65536:
        raise ValueError(f"{port} is not a port number")
    return port
