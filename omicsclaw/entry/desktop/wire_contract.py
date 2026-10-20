"""Define the versioned Desktop wire contract published by health.

Optional fields and capabilities can be additive within a major version.
Removing a field or changing its type or meaning requires the matching major
version change. Management routes follow the package release contract.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from __future__ import annotations

import time
from typing import Final

from ._chat_sse import CHAT_SSE_MAX_FRAME_BYTES

DESKTOP_CHAT_REQUEST_SCHEMA_VERSION: Final = 3
DESKTOP_CHAT_SSE_SCHEMA_VERSION: Final = 3
DESKTOP_CHAT_INTERRUPT_SCHEMA_VERSION: Final = 1
DESKTOP_JOBS_SCHEMA_VERSION: Final = 1
DESKTOP_ARTIFACTS_SCHEMA_VERSION: Final = 1

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
    "artifacts": True,
    # P4: session-scoped persistent analysis kernels are served — the
    # ``python`` tool on the chat face and ``kind:"code_run"`` on the jobs
    # face run cells on them. Additive within the same health payload:
    # a client that does not know the key ignores it.
    "kernel": True,
}


SERVED_PATHS: Final[tuple[str, ...]] = (
    "/chat/stream",
    "/chat/permission",
    "/chat/question",
    "/chat/abort",
    "/chat/session-permission-profile",
    "/chat/session/{session_id}",
    "/workspace",
    "/project/settings",
    "/env/doctor",
    "/sandbox/status",
    "/sandbox/allow-domain",
    "/api/sandbox/status",
    "/api/sandbox/allow-domain",
    "/health",
    "/skills",
    "/skills/{domain}/{name}",
    "/mcp/servers",
    "/providers",
    "/providers/test",
    "/chat/title",
    "/files/tree",
    "/files/serve",
    "/files/archive",
    "/runs",
    "/runs/{run_id}",
    "/runs/{run_id}/events",
    "/runs/{run_id}/events/stream",
    "/runs/{run_id}/verifications",
    "/jobs",
    "/jobs/{job_id}",
    "/jobs/{job_id}/events",
    "/jobs/{job_id}/cancel",
    "/jobs/{job_id}/approval/{call_id}",
    "/artifacts",
    "/artifacts/{artifact_id}",
    "/remote/hosts",
    "/remote/hosts/{alias}",
    "/remote/hosts/aliases",
    "/remote/hosts/{alias}/files/tree",
    "/remote/hosts/{alias}/files/serve",
    "/notebooks",
    "/notebooks/{notebook_id}",
    "/notebooks/{notebook_id}/cells/{cell_id}",
    "/notebooks/{notebook_id}/cells/{cell_id}/execute",
    "/notebooks/{notebook_id}/executions/{execution_id}/events",
    "/notebooks/{notebook_id}/executions/{execution_id}/cancel",
    "/notebooks/{notebook_id}/import",
    "/notebooks/{notebook_id}/export",
    "/notebooks/{notebook_id}/feedback",
    "/notebooks/{notebook_id}/binding",
    "/notebooks/{notebook_id}/live-state",
    "/notebooks/{notebook_id}/live-app",
    "/notebooks/{notebook_id}/live-app/state",
    "/compute/status",
)
"""The routes :func:`~omicsclaw.entry.desktop.server.create_desktop_app`
mounts, templated paths spelled as FastAPI spells them. ``/workspace``
and ``/providers`` answer ``GET`` and ``PUT``; ``/env/doctor``,
``/skills``, ``/skills/{domain}/{name}``, ``/mcp/servers``,
``/files/tree`` and ``/files/serve`` answer ``GET``; ``/health`` answers
``GET`` and ``HEAD``; ``/remote/hosts/{alias}`` answers ``DELETE``;
``/remote/hosts/aliases`` and the two ``/remote/hosts/{alias}/files/*``
routes answer ``GET``; ``/chat/session/{session_id}`` also answers ``GET``;
the rest answer ``POST``."""


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


def desktop_artifacts_contract() -> dict[str, int]:
    """The P2 artifacts plane contract: versioned additive-only, like the
    jobs one.

    Version 1 is ``GET /artifacts?job_id=&session_id=&kind=&limit=``
    (newest first, kind one of ``figure | table | h5ad | rds | report |
    pdf | other``) and ``GET /artifacts/{id}`` returning the record plus
    ``lineage: {job, parents}``. Byte content is served by the existing
    ``/files/serve`` on the artifact's ``path`` — no content route in
    this contract. Events: the job stream gained the ``artifact.created``
    frame type ``{artifact_id, kind, title, path}``, which is additive
    within the jobs contract's event vocabulary.
    """

    return {
        "artifacts_schema_version": DESKTOP_ARTIFACTS_SCHEMA_VERSION,
    }


__all__ = [
    "CONNECTION_EPOCH",
    "DESKTOP_ARTIFACTS_SCHEMA_VERSION",
    "DESKTOP_CAPABILITIES",
    "DESKTOP_CHAT_INTERRUPT_SCHEMA_VERSION",
    "DESKTOP_CHAT_REQUEST_SCHEMA_VERSION",
    "DESKTOP_CHAT_SSE_SCHEMA_VERSION",
    "DESKTOP_JOBS_SCHEMA_VERSION",
    "SERVED_PATHS",
    "desktop_artifacts_contract",
    "desktop_chat_contract",
    "desktop_jobs_contract",
]
