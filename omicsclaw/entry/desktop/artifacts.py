"""The Desktop artifacts routes: ``/artifacts*``, the P2 results plane.

Mounted into :func:`~omicsclaw.entry.desktop.server.create_desktop_app`
by :func:`mount_artifacts_routes`, in the style :mod:`~omicsclaw.entry.desktop.jobs`
set: every route checks the bearer token itself, answers a refusal as
``{"detail": <code>}`` with the code's status, and imports FastAPI
inside the mounting function. The logic lives in
:mod:`omicsclaw.memory.artifacts` (the store) and
:mod:`~omicsclaw.entry.desktop.artifacts_manager` (the scanner); this
module is the adapter.

Two routes:

* ``GET /artifacts?job_id=&session_id=&kind=&limit=`` — the list, newest
  first, filterable by the two scopes a row carries and by kind.
* ``GET /artifacts/{id}`` — the record plus its **lineage**: the job
  that produced it (inputs, skill, status — everything a person needs to
  answer "what did I run to get this") and the parent artifacts the row
  names, empty until a capture channel starts linking them.

**Content is served by the existing ``GET /files/serve``**, a deliberate
reuse rather than a new ``/artifacts/{id}/content``: every artifact path
is inside the workspace (the scanner prunes hidden directories and
``save_artifact`` resolves through the workspace boundary), and
``/files/serve`` already answers absolute in-workspace paths with
Range support, the right media type and the hidden/outside refusals —
which are the same refusals this plane wants. A second serving route
would re-implement that for the privilege of an indirection. The
artifact's ``path`` field is the ``path`` query parameter; the desktop
client already speaks it.
"""

# This module deliberately does NOT enable `from __future__ import annotations`:
# the routes are defined inside mount_artifacts_routes, where Request is a
# local name; with postponed evaluation FastAPI would see the string "Request",
# fail to resolve it in this module globals and treat the parameter as a
# required query parameter, so every route would answer 422 — the same trap
# server.py and jobs.py document for themselves.

import logging
from typing import Any, Callable, Final

from omicsclaw.memory.artifacts import ARTIFACT_KINDS, ArtifactStore

from .jobs_manager import JobsManager

__all__ = ["mount_artifacts_routes"]

_log = logging.getLogger(__name__)

RUNTIME_HEADER: Final = "X-OmicsClaw-Runtime"
"""The same routing header the jobs routes tolerate and echo."""


def mount_artifacts_routes(
    api: Any,
    store: ArtifactStore,
    *,
    jobs: JobsManager | None = None,
    authorized: Callable[[Any], bool],
) -> None:
    """Define the ``/artifacts*`` routes on *api*.

    *jobs* is the plane whose rows the lineage half reads; ``None``
    (or a job this backend no longer has) answers ``lineage.job = null``,
    because an artifact outliving its job row is a state the store
    allows and the wire must not lie about.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    def _unauthorized() -> Any:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    def _runtime(request: Request) -> dict[str, str]:
        return {RUNTIME_HEADER: request.headers.get(RUNTIME_HEADER, "local")}

    @api.get("/artifacts")
    async def list_artifacts(request: Request) -> Any:
        if not authorized(request):
            return _unauthorized()
        query = request.query_params
        try:
            limit = int(query.get("limit", "100"))
        except ValueError:
            return JSONResponse({"detail": "invalid_limit"}, status_code=422)
        if limit < 1 or limit > 500:
            return JSONResponse({"detail": "invalid_limit"}, status_code=422)
        kind = query.get("kind", "")
        if kind and kind not in ARTIFACT_KINDS:
            return JSONResponse({"detail": "invalid_kind"}, status_code=422)
        records = store.list_artifacts(
            job_id=query.get("job_id", ""),
            session_id=query.get("session_id", ""),
            kind=kind,
            limit=limit,
        )
        return JSONResponse(
            {"artifacts": [record.as_payload() for record in records]},
            headers=_runtime(request),
        )

    @api.get("/artifacts/{artifact_id}")
    async def get_artifact(request: Request, artifact_id: str) -> Any:
        if not authorized(request):
            return _unauthorized()
        record = store.get_artifact(artifact_id)
        if record is None:
            return JSONResponse({"detail": "artifact_not_found"}, status_code=404)
        lineage: dict[str, Any] = {"job": None, "parents": []}
        if record.job_id:
            job = jobs.get_job(record.job_id) if jobs is not None else None
            lineage["job"] = job.as_payload() if job is not None else None
        for parent_id in record.parent_ids:
            parent = store.get_artifact(parent_id)
            if parent is not None:
                lineage["parents"].append(parent.as_payload())
        return JSONResponse(
            {"artifact": record.as_payload(), "lineage": lineage},
            headers=_runtime(request),
        )
