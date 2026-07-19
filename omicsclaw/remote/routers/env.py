"""GET /env/doctor — JSON shape of ``omicsclaw.diagnostics.build_doctor_report``.

Reuses the existing ``oc doctor`` machinery so the App's Env tab and the CLI
agree byte-for-byte on what counts as a healthy environment.
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, HTTPException

from omicsclaw.diagnostics import build_doctor_report
from omicsclaw.remote.schemas import (
    AdaptiveModeResponse,
    AdaptiveModeUpdateRequest,
    EnvDoctorCheck,
    EnvDoctorReport,
    OverlayCleanRequest,
    OverlayCleanResponse,
    OverlayInfo,
    OverlayListResponse,
)
from omicsclaw.remote.runtime_binding import get_remote_workspace

router = APIRouter(tags=["remote"])

_TRUTHY = {"1", "true", "yes", "on"}


def _kill_switch_active() -> bool:
    return os.getenv("OMICSCLAW_SKIP_ADAPTIVE_ENV", "").strip().lower() in _TRUTHY


def build_env_doctor_report_payload(*, workspace_dir: str = "") -> EnvDoctorReport:
    resolved_workspace = str(workspace_dir or "").strip()
    if not resolved_workspace:
        frozen_workspace = get_remote_workspace()
        if frozen_workspace is None:
            raise RuntimeError("remote_workspace_unavailable")
        resolved_workspace = str(frozen_workspace)

    # ``omicsclaw_dir`` mirrors what server.py exposes via /health.
    try:
        from omicsclaw.surfaces.desktop.server import _omicsclaw_project_dir
        omicsclaw_dir = str(_omicsclaw_project_dir())
    except Exception:
        omicsclaw_dir = ""

    report = build_doctor_report(
        omicsclaw_dir=omicsclaw_dir,
        workspace_dir=resolved_workspace,
    )
    return EnvDoctorReport(
        generated_at=report.generated_at,
        workspace_dir=report.workspace_dir,
        omicsclaw_dir=report.omicsclaw_dir,
        overall_status=report.overall_status,
        failure_count=report.failure_count,
        warning_count=report.warning_count,
        checks=[
            EnvDoctorCheck(
                name=check.name,
                status=check.status,
                summary=check.summary,
                details=list(check.details),
            )
            for check in report.checks
        ],
    )


@router.get("/env/doctor", response_model=EnvDoctorReport)
async def env_doctor() -> EnvDoctorReport:
    try:
        return build_env_doctor_report_payload()
    except RuntimeError as exc:
        raise HTTPException(503, detail="remote_workspace_unavailable") from exc


# ---------------------------------------------------------------------------
# Adaptive env overlay management (ADR: adaptive-environment-provisioning).
# Backed by the same ``venv_provision`` functions the ``oc env`` CLI uses; all
# are non-fatal (bad keys return False/0, never raise) and path-traversal-safe.
# Filesystem-walking calls are offloaded to a thread to keep the loop responsive.
# ---------------------------------------------------------------------------


@router.get("/env/overlays", response_model=OverlayListResponse)
async def env_overlays() -> OverlayListResponse:
    from omicsclaw.skill.execution import venv_provision as vp

    overlays = await asyncio.to_thread(vp.list_overlays)
    return OverlayListResponse(
        overlays=[OverlayInfo(**item) for item in overlays],
        total=len(overlays),
        total_bytes=sum(int(item.get("size_bytes", 0)) for item in overlays),
        env_root=str(vp.env_root()),
    )


@router.post("/env/clean", response_model=OverlayCleanResponse)
async def env_clean(req: OverlayCleanRequest) -> OverlayCleanResponse:
    from omicsclaw.skill.execution import venv_provision as vp

    if req.key:
        ok = await asyncio.to_thread(vp.remove_overlay, req.key)
        return OverlayCleanResponse(removed=1 if ok else 0, key=req.key)
    removed = await asyncio.to_thread(vp.clean_all)
    return OverlayCleanResponse(removed=removed)


@router.get("/env/adaptive-mode", response_model=AdaptiveModeResponse)
async def get_adaptive_mode() -> AdaptiveModeResponse:
    from omicsclaw.skill.execution.env_resolver import adaptive_env_mode

    return AdaptiveModeResponse(mode=adaptive_env_mode(), kill_switch=_kill_switch_active())


@router.put("/env/adaptive-mode", response_model=AdaptiveModeResponse)
async def set_adaptive_mode(req: AdaptiveModeUpdateRequest) -> AdaptiveModeResponse:
    from omicsclaw.skill.execution.env_resolver import adaptive_env_mode

    # Process-scoped override; the resolver re-reads the var per run, so it takes
    # effect immediately (not persisted to .env — applies to this server session).
    os.environ["OMICSCLAW_ADAPTIVE_ENV"] = req.mode
    return AdaptiveModeResponse(mode=adaptive_env_mode(), kill_switch=_kill_switch_active())
