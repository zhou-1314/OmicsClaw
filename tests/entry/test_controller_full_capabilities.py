"""End-to-end tests for SSH Controller full capabilities (file mutations, session ops, job retry/delete, memory service)."""

from __future__ import annotations

import asyncio
import base64

from omicsclaw.entry import AppConfig, attach_sessions, build_app
from omicsclaw.entry.assembly import DeploymentCapabilities
from omicsclaw.entry.desktop.interactions import DesktopInteractions
from omicsclaw.entry.desktop.jobs_manager import JobRecord, JobsManager
from omicsclaw.entry.desktop.service import DesktopService
from omicsclaw.entry.memory import open_memory
from omicsclaw.memory.sessions import StoredSession
from omicsclaw.schema import Message, Role
from tests.entry.test_turn_runner import Scripted


def _make_service(tmp_path, *, with_runner: bool = True):
    config = AppConfig(workspace=tmp_path, durable_turn_events=True, memory=True)
    app = attach_sessions(
        build_app(
            config,
            provider=Scripted(Message(role=Role.ASSISTANT, content="done")),
            resolved_model="mac",
            capabilities=DeploymentCapabilities(notebook=False),
        ),
        abandon_grace_s=0,
    )
    jobs = JobsManager(app, register_default_runners=False)
    if with_runner:
        async def _dummy_runner(record, ctx):
            return {"status": "succeeded"}

        jobs.register_runner("skill_run", _dummy_runner)
    service = DesktopService(app, jobs, DesktopInteractions(app))
    return app, jobs, service


def test_file_mutations_and_browse_and_traversal_guard(tmp_path):
    async def run():
        app, jobs, service = _make_service(tmp_path)
        try:
            # 1. Write UTF-8 text file with create_parents=True
            res_write = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/files/write",
                    "document": {
                        "path": "sub/hello.txt",
                        "content": "hello spatial world",
                        "encoding": "utf-8",
                        "overwrite": True,
                        "create_parents": True,
                    },
                }
            )
            assert res_write["status"] == 200
            assert (tmp_path / "sub" / "hello.txt").read_text(encoding="utf-8") == "hello spatial world"
            assert res_write["payload"]["bytes_written"] == len(b"hello spatial world")

            # 2. Write base64 file into .omicsclaw/uploads/<session_id>/
            raw_bytes = b"\x89PNG\r\n\x1a\nfake-png-payload"
            res_b64 = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/files/write",
                    "document": {
                        "path": ".omicsclaw/uploads/sess-1/plot.png",
                        "content": base64.b64encode(raw_bytes).decode("ascii"),
                        "encoding": "base64",
                        "overwrite": True,
                        "create_parents": True,
                    },
                }
            )
            assert res_b64["status"] == 200
            assert (tmp_path / ".omicsclaw" / "uploads" / "sess-1" / "plot.png").read_bytes() == raw_bytes

            # 3. Refuse overwrite when overwrite=False
            res_conflict = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/files/write",
                    "document": {
                        "path": "sub/hello.txt",
                        "content": "new",
                        "overwrite": False,
                    },
                }
            )
            assert res_conflict["status"] == 409
            assert res_conflict["payload"]["error"] == "already_exists"

            # 4. Path traversal guard (403)
            res_escape = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/files/write",
                    "document": {
                        "path": "../../outside.txt",
                        "content": "bad",
                    },
                }
            )
            assert res_escape["status"] == 403
            assert res_escape["payload"]["error"] == "path_outside_workspace"

            # 4b. Symlink escape guard and root deletion/rename guard (403)
            outside_target = tmp_path.parent / "outside_secret_target.txt"
            outside_target.write_text("secret", encoding="utf-8")
            escape_link = tmp_path / "escape_link.txt"
            escape_link.symlink_to(outside_target)
            res_symlink = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/files/write",
                    "document": {"path": "escape_link.txt", "content": "overwrite"},
                }
            )
            assert res_symlink["status"] == 403
            assert res_symlink["payload"]["error"] == "path_outside_workspace"
            escape_link.unlink(missing_ok=True)
            outside_target.unlink(missing_ok=True)

            res_del_root = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/files/delete",
                    "document": {"path": ".", "recursive": True},
                }
            )
            assert res_del_root["status"] == 403
            assert res_del_root["payload"]["error"] == "forbidden_target"

            # 5. Mkdir and Browse
            res_mkdir = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/files/mkdir",
                    "document": {"path": "analysis/results", "create_parents": True},
                }
            )
            assert res_mkdir["status"] == 200
            assert (tmp_path / "analysis" / "results").is_dir()

            res_browse = await service.dispatch(
                {
                    "method": "GET",
                    "path": "/files/browse",
                    "query": {},
                }
            )
            assert res_browse["status"] == 200
            dir_names = [d["name"] for d in res_browse["payload"]["directories"]]
            assert "analysis" in dir_names and "sub" in dir_names
            assert res_browse["payload"]["parent"] is None

            # 6. Rename and Delete
            res_rename = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/files/rename",
                    "document": {
                        "from": "sub/hello.txt",
                        "to": "sub/renamed.txt",
                    },
                }
            )
            assert res_rename["status"] == 200
            assert not (tmp_path / "sub" / "hello.txt").exists()
            assert (tmp_path / "sub" / "renamed.txt").exists()

            res_del = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/files/delete",
                    "document": {"path": "sub", "recursive": True},
                }
            )
            assert res_del["status"] == 200
            assert not (tmp_path / "sub").exists()
        finally:
            await asyncio.sleep(0.05)
            jobs.store.database.close()
            await app.aclose()

    asyncio.run(run())


def test_session_delete_and_rewind(tmp_path):
    async def run():
        app, jobs, service = _make_service(tmp_path)
        try:
            session = StoredSession(
                session_id="sess-test-1",
                history=(
                    Message(role=Role.USER, content="turn 1"),
                    Message(role=Role.ASSISTANT, content="reply 1"),
                    Message(role=Role.USER, content="turn 2"),
                    Message(role=Role.ASSISTANT, content="reply 2"),
                ),
            )
            await app.sessions._store.save(session)

            # Rewind to keep first 2 messages
            rewound = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/chat/rewind",
                    "document": {"session_id": "sess-test-1", "keep_count": 2},
                }
            )
            assert rewound["status"] == 200
            assert rewound["payload"]["rewound"] is True
            loaded = await app.sessions.load_session("sess-test-1")
            assert loaded is not None and len(loaded.history) == 2

            # Delete session
            deleted = await service.dispatch(
                {
                    "method": "DELETE",
                    "path": "/chat/session/sess-test-1",
                }
            )
            assert deleted["status"] == 200
            assert deleted["payload"]["deleted"] is True
            assert await app.sessions.load_session("sess-test-1") is None
        finally:
            await asyncio.sleep(0.05)
            jobs.store.database.close()
            await app.aclose()

    asyncio.run(run())


def test_job_retry_and_delete(tmp_path):
    async def run():
        app, jobs, service = _make_service(tmp_path, with_runner=True)
        try:
            failed_job = JobRecord(
                id="job-failed-1",
                session_id="sess-1",
                kind="skill_run",
                skill="spatial-preprocess",
                inputs={"data": "sample.h5ad"},
                status="failed",
                error="simulated failure",
                created_at=100.0,
                finished_at=105.0,
            )
            jobs.store.insert_job(failed_job)

            retry_res = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/jobs/job-failed-1/retry",
                    "document": {"source_request_id": "retry-req-001"},
                }
            )
            assert retry_res["status"] == 201
            assert retry_res["payload"]["job"]["skill"] == "spatial-preprocess"
            assert retry_res["payload"]["job_id"] != "job-failed-1"

            del_res = await service.dispatch(
                {
                    "method": "DELETE",
                    "path": "/jobs/job-failed-1",
                }
            )
            assert del_res["status"] == 200
            assert del_res["payload"]["deleted"] is True
            assert jobs.get_job("job-failed-1") is None

            # Running job cannot be deleted (409 job_not_terminal)
            running_job = JobRecord(
                id="job-running-1",
                session_id="sess-1",
                kind="skill_run",
                skill="spatial-preprocess",
                inputs={},
                status="running",
                created_at=110.0,
            )
            jobs.store.insert_job(running_job)
            del_running = await service.dispatch(
                {"method": "DELETE", "path": "/jobs/job-running-1"}
            )
            assert del_running["status"] == 409
            assert del_running["payload"]["error"] == "job_not_terminal"

            # Without runner registered, job mutations return 409 science_runner
            no_runner_jobs = JobsManager(app, register_default_runners=False)
            no_runner_service = DesktopService(app, no_runner_jobs, DesktopInteractions(app))
            cap_res = await no_runner_service.dispatch(
                {"method": "POST", "path": "/jobs/job-running-1/retry", "document": {}}
            )
            assert cap_res["status"] == 409
            assert cap_res["payload"]["capability"] == "science_runner"
        finally:
            await asyncio.sleep(0.05)
            jobs.store.database.close()
            await app.aclose()

    asyncio.run(run())


def test_memory_service_crud_and_queries(tmp_path):
    async def run():
        app, jobs, service = _make_service(tmp_path)
        try:
            created = await service.dispatch(
                {
                    "method": "POST",
                    "path": "/memory/create",
                    "document": {
                        "title": "Visium HD QC threshold",
                        "content": "Use min_counts=500 for Visium HD mouse brain",
                        "domain": "analysis",
                        "priority": 8,
                        "tags": ["visium", "qc"],
                    },
                }
            )
            assert created["status"] == 200
            entry_id = created["payload"]["id"]
            assert created["payload"]["uri"] == f"analysis://{entry_id}"

            domains = await service.dispatch({"method": "GET", "path": "/memory/domains"})
            assert domains["status"] == 200
            analysis_domain = next(
                d for d in domains["payload"]["domains"] if d["domain"] == "analysis"
            )
            assert analysis_domain["node_count"] == 1

            searched = await service.dispatch(
                {
                    "method": "GET",
                    "path": "/memory/search",
                    "query": {"q": "Visium"},
                }
            )
            assert searched["status"] == 200
            assert len(searched["payload"]["results"]) == 1
            assert searched["payload"]["results"][0]["id"] == entry_id

            recent = await service.dispatch(
                {
                    "method": "GET",
                    "path": "/memory/recent",
                    "query": {"limit": "10"},
                }
            )
            assert recent["status"] == 200
            assert len(recent["payload"]["results"]) == 1

            scoped = await service.dispatch(
                {
                    "method": "GET",
                    "path": "/memory/scoped",
                    "query": {"workspace": str(tmp_path)},
                }
            )
            assert scoped["status"] == 200
            assert len(scoped["payload"]["items"]) == 1

            updated = await service.dispatch(
                {
                    "method": "PUT",
                    "path": "/memory/update",
                    "document": {
                        "path": entry_id,
                        "domain": "analysis",
                        "content": "Use min_counts=600 for Visium HD mouse brain",
                    },
                }
            )
            assert updated["status"] == 200
            assert updated["payload"]["updated"] is True

            browsed = await service.dispatch(
                {
                    "method": "GET",
                    "path": "/memory/browse",
                    "query": {"domain": "analysis", "path": entry_id},
                }
            )
            assert browsed["status"] == 200
            assert "min_counts=600" in browsed["payload"]["node"]["content"]

            deleted = await service.dispatch(
                {
                    "method": "DELETE",
                    "path": "/memory/delete",
                    "query": {"path": entry_id, "domain": "analysis"},
                }
            )
            assert deleted["status"] == 200
            assert deleted["payload"]["deleted"] is True
        finally:
            await asyncio.sleep(0.05)
            jobs.store.database.close()
            await app.aclose()

    asyncio.run(run())
