"""The versioned Desktop wire contract, as ``GET /health`` publishes it.

The backend serves two kinds of route.

**Chat contract routes**, versioned here: ``POST /chat/stream`` (request
body and SSE frames), ``POST /chat/permission``, ``POST /chat/abort``,
``POST /chat/session-permission-profile``, ``GET``/``PUT /workspace``,
``GET /env/doctor`` and ``GET /health``. The backend defines this
contract and the desktop client implements the major version it names.
Optional fields and capability declarations are additive within v3. Removing
a field, changing its meaning or type, or requiring new client behavior
bumps the matching major version. Clients ignore fields they do not use
and refuse major versions they do not implement.

**Management routes**, released with the package version and not covered
by these numbers: ``GET /skills``, ``GET /skills/{domain}/{name}``,
``GET /mcp/servers``, ``GET``/``PUT /providers``, ``POST /providers/test``,
``POST /chat/title`` (whose request body carries its own
``schema_version``), and the read-only file routes ``GET /files/tree`` and
``GET /files/serve``.

* ``request_schema_version`` — the ``/chat/stream`` request body. The
  body must carry ``ingress_schema_version`` equal to it; a missing or
  different value is refused with 422. Version 3 adds ``after_seq`` (the
  sequence number to resume after) and ``resume`` (reattach to the
  exchange a ``source_request_id`` started, and never start one).
* ``sse_schema_version`` — the frames of ``/chat/stream``, the request
  and response of ``/chat/permission`` and of
  ``/chat/session-permission-profile``, and the ``/env/doctor`` payload.
  Version 3 puts an ``id:`` line on every frame an event produced, except
  the frames that end the stream, and sums ``result.usage`` over the
  whole exchange rather than over one connection.
* ``interrupt_schema_version`` — the ``/chat/abort`` request.

``abandon_grace_s`` is how long an exchange keeps running once nobody
observes it, ``None`` when this backend never cancels one for that. It
is a fact about the running process rather than about the wire, so it is
not versioned.

:data:`SERVED_PATHS` names the routes
:func:`~omicsclaw.entry.desktop.server.create_desktop_app` mounts.
"""

from __future__ import annotations

import time
from typing import Final

from ._chat_sse import CHAT_SSE_MAX_FRAME_BYTES

DESKTOP_CHAT_REQUEST_SCHEMA_VERSION: Final = 3
DESKTOP_CHAT_SSE_SCHEMA_VERSION: Final = 3
DESKTOP_CHAT_INTERRUPT_SCHEMA_VERSION: Final = 1
DESKTOP_JOBS_SCHEMA_VERSION: Final = 1

CONNECTION_EPOCH: Final = int(time.time())
"""P3: the epoch every SSE frame of this process carries, and ``/health``
publishes as ``connection_epoch``.

A wall-clock integer minted once at import, so it moves forward across a
restart: a client that comes back after the backend was replaced drops
frames whose ``epoch`` is not the one its fresh ``/health`` named, which
is what stops "the backend restarted mid-stream" from becoming "the new
process's frames answer the old process's conversation". It is a fact
about the running process (like ``abandon_grace_s``), but it rides every
frame, so it is defined here beside the numbers a client compares."""


DESKTOP_CAPABILITIES: Final = {
    "files_tree": True,
    "files_serve": True,
}


SERVED_PATHS: Final[tuple[str, ...]] = (
    "/chat/stream",
    "/chat/permission",
    "/chat/abort",
    "/chat/session-permission-profile",
    "/workspace",
    "/env/doctor",
    "/health",
    "/skills",
    "/skills/{domain}/{name}",
    "/mcp/servers",
    "/providers",
    "/providers/test",
    "/chat/title",
    "/files/tree",
    "/files/serve",
    "/jobs",
    "/jobs/{job_id}",
    "/jobs/{job_id}/events",
    "/jobs/{job_id}/cancel",
    "/jobs/{job_id}/approval/{call_id}",
)
"""The routes :func:`~omicsclaw.entry.desktop.server.create_desktop_app`
mounts, templated paths spelled as FastAPI spells them. ``/workspace``
and ``/providers`` answer ``GET`` and ``PUT``; ``/env/doctor``,
``/skills``, ``/skills/{domain}/{name}``, ``/mcp/servers``,
``/files/tree`` and ``/files/serve`` answer ``GET``; ``/health`` answers
``GET`` and ``HEAD``; the rest answer ``POST``."""


def desktop_chat_contract(
    *, abandon_grace_s: float | None = None
) -> dict[str, int | bool | float | None]:
    """Return a fresh JSON-compatible Desktop chat contract descriptor.

    ``durable_ingress_idempotency`` is ``False`` because a redelivered
    ``source_request_id`` resolves to the same exchange only while this
    process retains it; nothing survives a restart. ``gap_notice`` says
    that a cursor which fell behind the retained frames receives an
    ``event_omitted`` frame rather than a silent jump. ``abandon_grace_s``
    is reported as given.
    """

    return {
        "request_schema_version": DESKTOP_CHAT_REQUEST_SCHEMA_VERSION,
        "sse_schema_version": DESKTOP_CHAT_SSE_SCHEMA_VERSION,
        "interrupt_schema_version": DESKTOP_CHAT_INTERRUPT_SCHEMA_VERSION,
        "authoritative_ingress": True,
        "durable_ingress_idempotency": False,
        "source_request_id_required": True,
        "attachments_supported": False,
        "max_sse_frame_bytes": CHAT_SSE_MAX_FRAME_BYTES,
        "oversize_event_projection": True,
        "terminal_error_type_preserved": True,
        "gap_notice": True,
        "abandon_grace_s": abandon_grace_s,
    }


def desktop_jobs_contract() -> dict[str, int]:
    """The P1 jobs plane contract: versioned additive-only, like the chat one.

    Version 1 is ``POST /jobs`` (``{kind, skill, inputs, workspace?,
    session_id?, runtime?}``), ``GET /jobs?session_id=&status=&limit=``,
    ``GET /jobs/{id}``, the ``GET /jobs/{id}/events`` SSE stream whose
    frames carry the unified event vocabulary, an ``id:`` sequence number
    per frame, ``Last-Event-ID`` resume and ``heartbeat`` frames,
    ``POST /jobs/{id}/cancel`` and ``POST /jobs/{id}/approval/{call_id}``.

    ``runtime`` (C2) is additive within version 1: omitted or ``local``
    is the behaviour every v1 client already knows; ``remote:<ssh
    alias>`` executes ``inputs.command`` on that host through the remote
    plane and speaks the same event vocabulary, and is refused with
    ``remote_runtime_unavailable`` where no plane is bound. The job
    payload answers ``runtime`` back, and ``job.created`` carries it.
    """

    return {
        "jobs_schema_version": DESKTOP_JOBS_SCHEMA_VERSION,
    }


__all__ = [
    "CONNECTION_EPOCH",
    "DESKTOP_CAPABILITIES",
    "DESKTOP_CHAT_INTERRUPT_SCHEMA_VERSION",
    "DESKTOP_CHAT_REQUEST_SCHEMA_VERSION",
    "DESKTOP_CHAT_SSE_SCHEMA_VERSION",
    "DESKTOP_JOBS_SCHEMA_VERSION",
    "SERVED_PATHS",
    "desktop_chat_contract",
    "desktop_jobs_contract",
]
