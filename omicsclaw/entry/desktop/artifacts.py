"""The Desktop artifacts routes: ``/artifacts*``, the P2 results plane.

Mounted on the Desktop FastAPI app by :func:`mount_artifacts_routes`, in
the style :mod:`~omicsclaw.entry.desktop.jobs` and
:mod:`~omicsclaw.entry.desktop.notebooks` establish: every route checks
``authorized(request)`` first, answers a validation error as
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
  answer "what did I run to get this"), the parent artifacts the row
  names, and Claude Science-style provenance when present on disk or in
  metadata (``artifact_slice``, ``object_versions``,
  ``assumption_checks``, ``verification_mode``, ``findings``).

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

import json
import logging
from pathlib import Path
from typing import Any, Callable, Final

from omicsclaw.memory.artifacts import ARTIFACT_KINDS, ArtifactRecord, ArtifactStore

from .jobs_manager import JobsManager

__all__ = ["mount_artifacts_routes"]

_log = logging.getLogger(__name__)

RUNTIME_HEADER: Final = "X-OmicsClaw-Runtime"
"""The same routing header the jobs routes tolerate and echo."""


def _candidate_provenance_dirs(artifact_path: str) -> list[Path]:
    """Return candidate provenance directories near *artifact_path*."""
    if not artifact_path:
        return []
    try:
        p = Path(artifact_path).resolve()
    except OSError:
        return []
    base = p.parent if p.is_file() or p.suffix else p
    dirs: list[Path] = []
    current = base
    for _ in range(5):
        for rel in ("provenance", ".omicsclaw/provenance"):
            cand = current / rel
            if cand not in dirs:
                dirs.append(cand)
        if current.parent == current:
            break
        current = current.parent
    return dirs


def _enrich_claude_science_lineage(
    record: ArtifactRecord,
    lineage: dict[str, Any],
) -> None:
    """Enrich *lineage* with backward artifact slice, object versions,
    statistical assumption contracts, verification mode, and ShadowReview
    findings from metadata or workspace provenance receipts when available.
    """
    meta = dict(record.meta or {})
    for key in (
        "artifact_slice",
        "object_versions",
        "assumption_checks",
        "verification_mode",
        "findings",
    ):
        if key in meta and meta[key] is not None:
            lineage[key] = meta[key]

    artifact_name = Path(record.path).name if record.path else ""
    for prov_dir in _candidate_provenance_dirs(record.path):
        receipt_path = prov_dir / "replay_receipt.json"
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except Exception:
                receipt = None
            if isinstance(receipt, dict):
                if "verification_mode" not in lineage and receipt.get("verification_mode"):
                    lineage["verification_mode"] = receipt.get("verification_mode")
                if "object_versions" not in lineage and isinstance(
                    receipt.get("object_versions"), list
                ):
                    lineage["object_versions"] = receipt["object_versions"]
                if "assumption_checks" not in lineage:
                    checks = receipt.get("assumption_checks") or receipt.get("skill_calls")
                    if isinstance(checks, list):
                        lineage["assumption_checks"] = checks
                if "artifact_slice" not in lineage:
                    matched_slice = None
                    if isinstance(receipt.get("artifact_lineage"), dict):
                        art_map = receipt["artifact_lineage"]
                        for candidate_key, slice_val in art_map.items():
                            if (
                                candidate_key == record.path
                                or (
                                    record.path
                                    and record.path.endswith(
                                        "/" + str(candidate_key).lstrip("/")
                                    )
                                )
                                or (
                                    artifact_name
                                    and Path(str(candidate_key)).name == artifact_name
                                )
                            ):
                                matched_slice = (
                                    dict(slice_val)
                                    if isinstance(slice_val, dict)
                                    else slice_val
                                )
                                break
                    if (
                        isinstance(matched_slice, dict)
                        and (
                            not matched_slice.get("synthesized_code")
                            or not matched_slice.get("target_path")
                        )
                        and isinstance(receipt.get("steps"), list)
                    ):
                        try:
                            from omicsclaw.entry.notebook.service import (
                                compute_backward_artifact_slice,
                            )

                            target_art = (
                                matched_slice.get("target_artifact")
                                or matched_slice.get("target_path")
                                or artifact_name
                                or record.path
                            )
                            computed = compute_backward_artifact_slice(
                                receipt, target_artifact=target_art
                            )
                            matched_slice.setdefault(
                                "target_path",
                                computed.get("target_path") or str(target_art or ""),
                            )
                            matched_slice.setdefault(
                                "synthesized_code", computed.get("synthesized_code", "")
                            )
                            matched_slice.setdefault(
                                "script", computed.get("script", "")
                            )
                        except Exception:
                            pass
                    elif (
                        matched_slice is None
                        and isinstance(receipt.get("steps"), list)
                        and (artifact_name or record.path)
                    ):
                        try:
                            from omicsclaw.entry.notebook.service import (
                                compute_backward_artifact_slice,
                            )

                            computed = compute_backward_artifact_slice(
                                receipt, target_artifact=artifact_name or record.path
                            )
                            if computed.get("kept_indices"):
                                matched_slice = computed
                        except Exception:
                            pass
                    if matched_slice is not None:
                        lineage["artifact_slice"] = matched_slice

        findings_path = prov_dir / "findings.json"
        if "findings" not in lineage and findings_path.is_file():
            try:
                raw_findings = json.loads(findings_path.read_text(encoding="utf-8"))
            except Exception:
                raw_findings = None
            items = (
                raw_findings.get("findings")
                if isinstance(raw_findings, dict)
                else raw_findings
                if isinstance(raw_findings, list)
                else None
            )
            if isinstance(items, list):
                if record.session_id:
                    filtered = [
                        f
                        for f in items
                        if isinstance(f, dict)
                        and (not f.get("session_id") or f.get("session_id") == record.session_id)
                    ]
                    lineage["findings"] = filtered
                else:
                    lineage["findings"] = [f for f in items if isinstance(f, dict)]


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
        _enrich_claude_science_lineage(record, lineage)
        return JSONResponse(
            {"artifact": record.as_payload(), "lineage": lineage},
            headers=_runtime(request),
        )
