"""Typed Desktop transport adapter. State and execution belong to existing managers."""

from __future__ import annotations

import asyncio
import base64
import re

from omicsclaw.memory.artifacts import ARTIFACT_KINDS

from .catalog import mcp_servers, skill_catalog, skill_detail
from .chat_service import change_workspace, workspace_payload
from .doctor import doctor_report
from .files import (
    byte_span,
    content_disposition,
    file_tree,
    open_served_file,
    serve_target,
    tree_depth,
)
from .git_service import execute_git
from .interactions import abort_chat, answer_permission, change_permission_profile
from .job_service import (
    create_job_from_document,
    event_cursor,
    job_event_page,
    run_payload,
)
from .jobs_manager import JobError
from .runtime_service import health_payload
from .turn_submission import DesktopIngressError
from .usage_service import usage_statistics


def capability_required(name):
    return {
        "status": 409,
        "payload": {
            "error": "capability_required",
            "capability": name,
            "enabled": False,
        },
    }


def bounded_limit(value, default=100):
    try:
        number = int(value if value is not None else default)
    except (ValueError, TypeError):
        raise DesktopIngressError("invalid_limit") from None
    if not 1 <= number <= 500:
        raise DesktopIngressError("invalid_limit")
    return number


class DesktopService:
    def __init__(self, app, jobs, interactions):
        self.app, self.jobs, self.interactions = app, jobs, interactions

    async def dispatch(self, params):
        if set(params) - {"method", "path", "query", "document", "headers"}:
            raise DesktopIngressError("unknown_rpc_field")
        method, path = params.get("method"), params.get("path")
        if (
            method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
            or not isinstance(path, str)
            or not path.startswith("/")
            or any(value in path for value in ("//", "..", "?", "#", "\\", "\0"))
        ):
            raise DesktopIngressError("invalid_rpc_path")
        query = params.get("query") or {}
        document = params.get("document") or {}
        headers = params.get("headers") or {}
        if (
            not isinstance(query, dict)
            or not isinstance(document, dict)
            or not isinstance(headers, dict)
        ):
            raise DesktopIngressError("invalid_rpc_document")
        try:
            return await self._dispatch(method, path, query, document, headers)
        except DesktopIngressError as error:
            return {
                "status": error.status_code,
                "payload": {"error": error.code, "detail": error.code},
            }
        except JobError as error:
            return {
                "status": error.status_code,
                "payload": {"error": error.code, "detail": error.code},
            }

    async def _dispatch(self, method, path, query, document, headers):
        app, jobs = self.app, self.jobs
        payload = None
        if method == "POST" and path == "/git/execute":
            payload = await execute_git(app.config.workspace, document)
        elif method in {"GET", "HEAD"} and path == "/health":
            payload = health_payload(app)
            payload["capabilities"].update(
                {
                    "kernel": False,
                    "kernel_r": False,
                    "mcp": False,
                    "attachments": False,
                    "science_runner": False,
                }
            )
            payload["skill_python_executable"] = ""
            payload["transport"] = "ssh-stdio"
        elif method == "GET" and path == "/workspace":
            payload = workspace_payload(app)
        elif method == "PUT" and path == "/workspace":
            payload = change_workspace(app, document)
        elif method == "GET" and path == "/env/doctor":
            payload = doctor_report(app)
        elif method == "GET" and path == "/usage/stats":
            if set(query) - {"days", "timezoneOffsetMinutes"}:
                raise DesktopIngressError("invalid_usage_window")
            try:
                days = int(query.get("days", "30"))
                offset = int(query.get("timezoneOffsetMinutes", "0"))
            except (ValueError, TypeError):
                raise DesktopIngressError("invalid_usage_window") from None
            payload = usage_statistics(
                app.memory.database, days=days, timezone_offset_minutes=offset
            )
        elif method == "GET" and path == "/skills":
            payload = skill_catalog(app)
        elif method == "GET" and re.fullmatch(
            r"/skills/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path
        ):
            _, _, domain, name = path.split("/")
            payload = skill_detail(app, domain, name)
        elif method == "GET" and path == "/mcp/servers":
            payload = {**mcp_servers(app), "capability": "mcp", "enabled": False}
        elif method == "POST" and path == "/chat/permission":
            payload = await answer_permission(app, self.interactions, document)
        elif method == "POST" and path == "/chat/abort":
            payload = await abort_chat(app, self.interactions, document)
        elif method == "POST" and path == "/chat/session-permission-profile":
            payload = await change_permission_profile(app, self.interactions, document)
        elif method == "GET" and path == "/files/tree":
            payload = await asyncio.to_thread(
                file_tree,
                app.config.workspace,
                query.get("path"),
                depth=tree_depth(query.get("depth")),
            )
        elif method == "GET" and path == "/files/serve":
            return await self._file(query, headers)
        elif path.startswith("/files/"):
            return capability_required("file_mutation")
        elif path.startswith("/notebooks"):
            return capability_required("notebook")
        elif path.startswith("/remote/hosts"):
            return capability_required("multi_target")
        elif method == "GET" and path == "/jobs":
            records = jobs.list_jobs(
                session_id=query.get("session_id", ""),
                status=query.get("status", ""),
                limit=bounded_limit(query.get("limit")),
            )
            payload = {"jobs": [row.as_payload() for row in records]}
        elif method == "POST" and path == "/jobs":
            if document.get("kind", "skill_run") not in jobs.runner_kinds():
                return capability_required("science_runner")
            record = await create_job_from_document(jobs, document)
            payload = {"job_id": record.id, "job": record.as_payload()}
        elif re.fullmatch(r"/jobs/[A-Za-z0-9_.-]+", path) and method == "GET":
            record = jobs.get_job(path.split("/")[2])
            if record is None:
                raise DesktopIngressError("job_not_found", status_code=404)
            payload = {"job": record.as_payload()}
        elif re.fullmatch(r"/jobs/[A-Za-z0-9_.-]+/cancel", path) and method == "POST":
            job_id = path.split("/")[2]
            outcome = await jobs.cancel_job(job_id)
            if outcome == "unknown":
                raise DesktopIngressError("job_not_found", status_code=404)
            if outcome == "finished":
                raise DesktopIngressError("job_already_finished", status_code=409)
            record = jobs.get_job(job_id)
            payload = {"job_id": job_id, "job": record.as_payload() if record else None}
        elif re.fullmatch(r"/jobs/[A-Za-z0-9_.-]+/events", path) and method == "GET":
            job_id = path.split("/")[2]
            payload = job_event_page(jobs, job_id, query.get("afterSeq", "0"))
        elif path.startswith("/jobs/"):
            return capability_required("science_runner")
        elif method == "GET" and path == "/artifacts":
            kind = query.get("kind", "")
            if kind and kind not in ARTIFACT_KINDS:
                raise DesktopIngressError("invalid_kind")
            records = jobs.artifacts.list_artifacts(
                job_id=query.get("job_id", ""),
                session_id=query.get("session_id", ""),
                kind=kind,
                limit=bounded_limit(query.get("limit")),
            )
            payload = {"artifacts": [row.as_payload() for row in records]}
        elif method == "GET" and re.fullmatch(r"/artifacts/[A-Za-z0-9_.-]+", path):
            record = jobs.artifacts.get_artifact(path.split("/")[2])
            if record is None:
                raise DesktopIngressError("artifact_not_found", status_code=404)
            job = jobs.get_job(record.job_id) if record.job_id else None
            payload = {
                "artifact": record.as_payload(),
                "lineage": {
                    "job": job.as_payload() if job else None,
                    "parents": [
                        parent.as_payload()
                        for pid in record.parent_ids
                        if (parent := jobs.artifacts.get_artifact(pid)) is not None
                    ],
                },
            }
        elif method == "GET" and path == "/runs":
            await jobs.refresh_projections()
            records = [
                run
                for run in jobs.run_store.list("run")
                if not query.get("session_id") or run.session_id == query["session_id"]
            ]
            payload = {
                "runs": [
                    run_payload(jobs, row)
                    for row in records[-bounded_limit(query.get("limit")) :][::-1]
                ]
            }
        elif method == "GET" and re.fullmatch(r"/runs/[A-Za-z0-9_.-]+", path):
            await jobs.refresh_projections()
            run = jobs.run_store.load(path.split("/")[2])
            if run is None or run.__class__.__name__ != "Run":
                raise DesktopIngressError("run_not_found", status_code=404)
            payload = run_payload(jobs, run)
        elif method == "GET" and re.fullmatch(r"/runs/[A-Za-z0-9_.-]+/events", path):
            run_id = path.split("/")[2]
            await jobs.refresh_projections()
            if jobs.run_store.load(run_id) is None:
                raise DesktopIngressError("run_not_found", status_code=404)
            payload = {
                "events": jobs.run_store.events(
                    after_seq=event_cursor(query.get("last_event_id", "0")),
                    run_id=run_id,
                    limit=500,
                )
            }
        else:
            raise DesktopIngressError("unsupported_rpc_route", status_code=404)
        return {"status": 200, "payload": payload}

    async def _file(self, query, headers):
        target = await asyncio.to_thread(
            serve_target, self.app.config.workspace, query.get("path", "")
        )
        opened = await asyncio.to_thread(open_served_file, target.path)
        try:
            span = byte_span(headers.get("range"), opened.size)
            if span.length > 131072:
                return capability_required("large_file_channel")

            def read():
                opened.handle.seek(span.start)
                return opened.handle.read(span.length)

            raw = await asyncio.to_thread(read)
            return {
                "status": span.status,
                "bytes": base64.b64encode(raw).decode(),
                "headers": {
                    "content-type": target.media_type,
                    "content-length": str(len(raw)),
                    "accept-ranges": "bytes",
                    "content-disposition": content_disposition(target.name),
                    "x-content-type-options": "nosniff",
                    **({"content-range": span.content_range} if span.partial else {}),
                },
            }
        finally:
            opened.close()
