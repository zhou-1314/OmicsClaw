"""The Desktop jobs routes: ``/jobs*``, the P1 execution plane.

Mounted into :func:`~omicsclaw.entry.desktop.server.create_desktop_app`
by :func:`mount_jobs_routes`, in the style the rest of that module set:
every route checks the bearer token itself, refuses a body that is not
exactly one ``application/json``, reads it under a running cap, and
answers a refusal as ``{"detail": <code>}`` with the code status. The
logic lives in :mod:`~omicsclaw.entry.desktop.jobs_manager`; this module
is the adapter, and imports FastAPI inside the mounting function for the
same reason ``server.py`` does.

``GET /jobs/{id}/events`` speaks SSE with the shape the chat stream
already established: an ``id: <seq>`` line on every frame an event
produced, then one ``data:`` line carrying ``{"type": ..., "data": ...,
"epoch": ...}``. The ``epoch`` is the process
:data:`~omicsclaw.entry.desktop.wire_contract.CONNECTION_EPOCH`, so a
client that restarted the backend under a live connection can tell the
frames of the new process from the ones it already consumed and drop
them (P3). A heartbeat frame — no ``id:`` line, type ``heartbeat`` —
keeps intermediaries from closing an idle analysis, and is discarded by
``EventSource`` reconnect bookkeeping precisely because it has no id.

``Last-Event-ID`` (the header, or ``?last_event_id=`` for a client that
cannot set headers) resumes from the per-job ring the manager persists:
the route replays everything with ``seq > last`` before it listens live,
and the terminal ``job.done`` / ``job.failed`` frame closes the stream.
"""

# This module deliberately does NOT enable `from __future__ import annotations`:
# the routes are defined inside mount_jobs_routes, where Request is a local
# name; with postponed evaluation FastAPI would see the string "Request",
# fail to resolve it in this module globals and treat the parameter as a
# required query parameter, so every route would answer 422 — the same trap
# server.py documents for itself.

import asyncio
import json
import logging
from typing import Any, Callable, Final

from omicsclaw.tools.context import ApprovalDecision

from .jobs_manager import (
    HEARTBEAT_INTERVAL_S,
    TERMINAL_EVENT_TYPES,
    JobError,
    JobsManager,
)
from .turn_submission import DesktopIngressError
from .wire_contract import CONNECTION_EPOCH

__all__ = ["JOBS_MAX_REQUEST_BYTES", "mount_jobs_routes"]

_log = logging.getLogger(__name__)

JOBS_MAX_REQUEST_BYTES: Final = 512 * 1024
"""Largest body ``POST /jobs`` and ``POST /jobs/{id}/approval/{call_id}``
will read. Inputs are form-shaped, not datasets: a path string, not the
matrix it names."""

RUNTIME_HEADER: Final = "X-OmicsClaw-Runtime"
"""P3: how a client says which runtime it wants the work to run on.

``local`` or ``remote:<connId>``. This backend serves one process on one
machine and routes nothing, so the header is tolerated, logged, and
echoed back on the jobs responses — a deployment that gains routing
later has a seam already named."""


def _note_runtime_header(header: str | None) -> None:
    if header:
        _log.debug("%s: %s", RUNTIME_HEADER, header)


def mount_jobs_routes(
    api: Any,
    jobs: JobsManager,
    *,
    authorized: Callable[[Any], bool],
) -> None:
    """Define the ``/jobs*`` routes on *api*.

    *authorized* is the mounting application's bearer check, so jobs
    routes answer 401 exactly like the chat routes do.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse, StreamingResponse

    from ._chat_sse import render_chat_sse_frame
    from .server import SSE_HEADERS, is_json_media_type

    def _unauthorized() -> Any:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    def _refused(exc: DesktopIngressError) -> Any:
        return JSONResponse({"detail": exc.code}, status_code=exc.status_code)

    def _job_error(exc: JobError) -> Any:
        return JSONResponse({"detail": exc.code}, status_code=exc.status_code)

    async def _read_json(request: Request) -> dict[str, Any]:
        declared = request.headers.getlist("content-type")
        if len(declared) != 1 or not is_json_media_type(declared[0]):
            raise DesktopIngressError("unsupported_media_type", status_code=415)
        declared_length = request.headers.getlist("content-length")
        if declared_length:
            try:
                length = int(declared_length[0])
            except ValueError as exc:
                raise DesktopIngressError(
                    "invalid_content_length", status_code=400
                ) from exc
            if length < 0 or length > JOBS_MAX_REQUEST_BYTES:
                raise DesktopIngressError(
                    "request_document_too_large", status_code=413
                )
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > JOBS_MAX_REQUEST_BYTES:
                raise DesktopIngressError(
                    "request_document_too_large", status_code=413
                )
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise DesktopIngressError("invalid_json", status_code=400) from exc
        if not isinstance(document, dict):
            raise DesktopIngressError("invalid_json_body", status_code=400)
        return document

    @api.post("/jobs")
    async def create_job(request: Request) -> Any:
        if not authorized(request):
            return _unauthorized()
        _note_runtime_header(request.headers.get(RUNTIME_HEADER))
        try:
            document = await _read_json(request)
            kind = document.get("kind", "skill_run")
            if not isinstance(kind, str):
                raise DesktopIngressError("invalid_kind", status_code=422)
            skill = document.get("skill", "")
            if skill is not None and not isinstance(skill, str):
                raise DesktopIngressError("invalid_skill", status_code=422)
            session_id = document.get("session_id", "")
            if session_id is not None and not isinstance(session_id, str):
                raise DesktopIngressError("invalid_session_id", status_code=422)
            workspace = document.get("workspace", "")
            if workspace is not None and not isinstance(workspace, str):
                raise DesktopIngressError("invalid_workspace", status_code=422)
            inputs = document.get("inputs")
            # P4's code_run carries its payload at the top level
            # (``{kind, code, inputs?{adata_path?}}``, the shape the plan
            # spells) rather than inside ``inputs``: the code is the job,
            # not an argument to a skill. Folded into ``inputs`` here so
            # the runner reads one bag; a skill_run body is untouched.
            if kind == "code_run":
                code = document.get("code")
                if not isinstance(code, str) or not code.strip():
                    raise DesktopIngressError("code_required", status_code=422)
                bag = dict(inputs) if isinstance(inputs, dict) else {}
                bag["code"] = code
                inputs = bag
            record = await jobs.create_job(
                kind=kind,
                skill=skill or "",
                inputs=inputs,
                session_id=session_id or "",
                workspace=workspace or "",
            )
        except DesktopIngressError as exc:
            return _refused(exc)
        except JobError as exc:
            return _job_error(exc)
        payload = record.as_payload()
        return JSONResponse(
            {"job_id": record.id, "job": payload},
            headers={RUNTIME_HEADER: request.headers.get(RUNTIME_HEADER, "local")},
        )

    @api.get("/jobs")
    async def list_jobs(request: Request) -> Any:
        if not authorized(request):
            return _unauthorized()
        _note_runtime_header(request.headers.get(RUNTIME_HEADER))
        query = request.query_params
        limit_raw = query.get("limit", "100")
        try:
            limit = int(limit_raw)
        except ValueError:
            return JSONResponse({"detail": "invalid_limit"}, status_code=422)
        records = jobs.list_jobs(
            session_id=query.get("session_id", ""),
            status=query.get("status", ""),
            limit=limit,
        )
        return JSONResponse(
            {"jobs": [record.as_payload() for record in records]},
            headers={RUNTIME_HEADER: request.headers.get(RUNTIME_HEADER, "local")},
        )

    @api.get("/jobs/{job_id}")
    async def get_job(request: Request, job_id: str) -> Any:
        if not authorized(request):
            return _unauthorized()
        record = jobs.get_job(job_id)
        if record is None:
            return JSONResponse({"detail": "job_not_found"}, status_code=404)
        return JSONResponse(
            {"job": record.as_payload()},
            headers={RUNTIME_HEADER: request.headers.get(RUNTIME_HEADER, "local")},
        )

    @api.get("/jobs/{job_id}/events")
    async def job_events(request: Request, job_id: str) -> Any:
        if not authorized(request):
            return _unauthorized()
        _note_runtime_header(request.headers.get(RUNTIME_HEADER))
        record = jobs.get_job(job_id)
        if record is None:
            return JSONResponse({"detail": "job_not_found"}, status_code=404)

        last_event_id = request.headers.get("last-event-id") or request.query_params.get(
            "last_event_id", ""
        )
        try:
            cursor = int(last_event_id) if str(last_event_id).strip() else 0
        except ValueError:
            return JSONResponse({"detail": "invalid_last_event_id"}, status_code=422)
        if cursor < 0:
            return JSONResponse({"detail": "invalid_last_event_id"}, status_code=422)

        def frame(seq: int | None, event_type: str, payload: Any) -> str:
            return render_chat_sse_frame(
                event_type, payload, event_id=seq, epoch=CONNECTION_EPOCH
            )

        start = cursor

        async def stream() -> Any:
            cursor = start
            queue = jobs.subscribe(job_id)
            try:
                state = record.status
                while True:
                    replayed = jobs.replay_events(job_id, cursor)
                    for seq, event_type, payload in replayed:
                        cursor = max(cursor, seq)
                        yield frame(seq, event_type, payload)
                        if event_type in TERMINAL_EVENT_TYPES:
                            return
                    try:
                        seq, event_type, payload = await asyncio.wait_for(
                            queue.get(), timeout=HEARTBEAT_INTERVAL_S
                        )
                    except TimeoutError:
                        state = (jobs.get_job(job_id) or record).status
                        yield frame(None, "heartbeat", {"state": state})
                        continue
                    if seq <= cursor:
                        continue
                    cursor = seq
                    yield frame(seq, event_type, payload)
                    if event_type in TERMINAL_EVENT_TYPES:
                        return
            finally:
                jobs.unsubscribe(job_id, queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                **SSE_HEADERS,
                "X-OmicsClaw-Job-Id": job_id,
                RUNTIME_HEADER: request.headers.get(RUNTIME_HEADER, "local"),
            },
        )

    @api.post("/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, job_id: str) -> Any:
        if not authorized(request):
            return _unauthorized()
        _note_runtime_header(request.headers.get(RUNTIME_HEADER))
        outcome = jobs.cancel(job_id)
        if outcome == "unknown":
            return JSONResponse({"detail": "job_not_found"}, status_code=404)
        if outcome == "finished":
            return JSONResponse({"detail": "job_already_finished"}, status_code=409)
        record = jobs.get_job(job_id)
        return JSONResponse(
            {
                "job_id": job_id,
                "job": record.as_payload() if record is not None else None,
            },
            headers={RUNTIME_HEADER: request.headers.get(RUNTIME_HEADER, "local")},
        )

    @api.post("/jobs/{job_id}/approval/{call_id}")
    async def job_approval(request: Request, job_id: str, call_id: str) -> Any:
        if not authorized(request):
            return _unauthorized()
        _note_runtime_header(request.headers.get(RUNTIME_HEADER))
        try:
            document = await _read_json(request)
            decision = document.get("decision")
            if decision not in ("approve", "deny"):
                raise DesktopIngressError("invalid_decision", status_code=422)
            took_effect = jobs.settle_approval(
                job_id,
                call_id,
                ApprovalDecision(
                    approved=decision == "approve",
                    reason="" if decision == "approve" else "denied in the desktop app",
                ),
            )
        except DesktopIngressError as exc:
            return _refused(exc)
        if not took_effect:
            return JSONResponse({"detail": "approval_not_outstanding"}, status_code=404)
        return JSONResponse(
            {"job_id": job_id, "call_id": call_id, "decision": decision},
            headers={RUNTIME_HEADER: request.headers.get(RUNTIME_HEADER, "local")},
        )
