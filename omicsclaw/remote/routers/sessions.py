"""Retired legacy Remote Session-resume compatibility Interface.

The response shape remains available for older App clients, but this Adapter
has no authority to discover or resume scientific work.  In particular it
does not resolve a Workspace, inspect historical ``job.json`` files, or touch
the canonical Run Runtime.  Canonical Run observation and cancellation live
on the Run/Job Interfaces instead.
"""

from __future__ import annotations

from fastapi import APIRouter

from omicsclaw.remote.schemas import SessionResumeResponse

router = APIRouter(tags=["remote"])


@router.post("/sessions/{session_id}/resume", response_model=SessionResumeResponse)
async def resume_session(session_id: str) -> SessionResumeResponse:
    return SessionResumeResponse(
        session_id=session_id,
        resumed=False,
        reason="legacy_session_resume_retired",
        active_job_ids=[],
    )
