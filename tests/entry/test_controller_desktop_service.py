"""Typed stdio routes use existing managers and refuse missing profiles explicitly."""

import asyncio
from types import SimpleNamespace
import pytest
from omicsclaw.entry import AppConfig, attach_sessions, build_app
from omicsclaw.entry.assembly import DeploymentCapabilities
from omicsclaw.entry.desktop.jobs_manager import JobsManager
from omicsclaw.entry.desktop.interactions import DesktopInteractions
from tests.entry.test_turn_runner import Scripted
from omicsclaw.schema import Message, Role


def test_desktop_service_has_real_workspace_and_jobs_authority_and_explicit_caps(
    tmp_path,
):
    from omicsclaw.entry.desktop.service import DesktopService

    async def run():
        app = attach_sessions(
            build_app(
                AppConfig(workspace=tmp_path, durable_turn_events=True),
                provider=Scripted(Message(role=Role.ASSISTANT, content="done")),
                resolved_model="mac",
                capabilities=DeploymentCapabilities(notebook=False),
            )
        )
        jobs = JobsManager(app, register_default_runners=False)
        service = DesktopService(app, jobs, DesktopInteractions(app))
        try:
            workspace = await service.dispatch({"method": "GET", "path": "/workspace"})
            assert workspace["payload"]["workspace"] == str(tmp_path)
            records = await service.dispatch({"method": "GET", "path": "/jobs"})
            assert records["payload"] == {"jobs": []}
            missing = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/jobs",
                    "document": {
                        "kind": "skill_run",
                        "skill": "anything",
                        "inputs": {},
                    },
                }
            )
            assert (
                missing["status"] == 409
                and missing["payload"]["capability"] == "science_runner"
            )
            notebook = await service.dispatch({"method": "GET", "path": "/notebooks"})
            assert (
                notebook["status"] == 409
                and notebook["payload"]["capability"] == "notebook"
            )
            with pytest.raises(Exception):
                await service.dispatch(
                    {"method": "GET", "path": "https://localhost/secrets"}
                )
        finally:
            jobs.store.database.close()
            await app.aclose()

    asyncio.run(run())
