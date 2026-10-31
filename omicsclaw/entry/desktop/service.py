"""Typed Desktop transport adapter. State and execution belong to existing managers."""

from __future__ import annotations

import asyncio
import base64
import inspect
import re
import time

from omicsclaw.memory.artifacts import ARTIFACT_KINDS

from .catalog import mcp_servers, skill_catalog, skill_detail
from .connectors_service import (
    check_connector,
    connector_listing,
    permission_rules_payload,
    set_connector_enabled,
)
from .chat_service import change_workspace, workspace_payload
from .doctor import _sandbox_snapshot, doctor_report
from .files import (
    browse_workspace_directories,
    byte_span,
    content_disposition,
    delete_workspace_entry,
    file_tree,
    mkdir_workspace,
    open_served_file,
    rename_workspace_entry,
    serve_target,
    tree_depth,
    write_workspace_file,
)
from .git_service import execute_git
from .interactions import abort_chat, answer_permission, answer_question, change_permission_profile
from .job_service import (
    create_job_from_document,
    event_cursor,
    job_event_page,
    run_payload,
    settle_job_approval,
)
from .jobs_manager import JobError
from .memory_service import dispatch_memory_request
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
            has_kernel = getattr(app, "kernels", None) is not None
            payload["capabilities"].update(
                {
                    "kernel": has_kernel,
                    "kernel_r": bool(has_kernel and payload["capabilities"].get("kernel_r", False)),
                    "mcp": False,
                    "attachments": False,
                    "science_runner": "skill_run" in jobs.runner_kinds(),
                }
            )
            payload["capability_details"] = {
                "notebook": {"state": "enabled", "provider": "notebook_owner_v1"},
                "sandbox": {
                    "state": (
                        "active"
                        if getattr(app.sandbox, "active", False)
                        else (
                            "degraded"
                            if getattr(app.sandbox, "degraded", False)
                            else "off"
                        )
                    ),
                    "backend": str(
                        getattr(
                            getattr(app.sandbox, "backend", None),
                            "value",
                            getattr(app.sandbox, "backend", "none"),
                        )
                        or "none"
                    ),
                },
                "science_runner": {
                    "state": (
                        "enabled" if "skill_run" in jobs.runner_kinds() else "disabled"
                    )
                },
                "mcp": {
                    "state": "disabled",
                    "reason": "disabled_in_ssh_controller_stage2",
                },
                "attachments": {
                    "state": "disabled",
                    "reason": "requires_stage4_data_channel",
                },
            }
            selected = jobs.runner_for("skill_run")
            payload["skill_python_executable"] = (
                str(selected.interpreter) if selected is not None else ""
            )
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
        elif method == "POST" and path == "/chat/question":
            payload = await answer_question(app, self.interactions, document)
        elif method == "POST" and path == "/chat/abort":
            payload = await abort_chat(app, self.interactions, document)
        elif method == "POST" and path == "/chat/session-permission-profile":
            payload = await change_permission_profile(app, self.interactions, document)
        elif method == "DELETE" and re.fullmatch(
            r"/chat/session/[A-Za-z0-9_.:-]+", path
        ):
            payload = await delete_chat_session(app, path.split("/")[3])
        elif method == "POST" and path == "/chat/rewind":
            payload = await rewind_chat_session(app, document)
        elif method == "GET" and path == "/files/tree":
            payload = await asyncio.to_thread(
                file_tree,
                app.config.workspace,
                query.get("path"),
                depth=tree_depth(query.get("depth")),
            )
        elif method == "GET" and path == "/files/browse":
            payload = await asyncio.to_thread(
                browse_workspace_directories,
                app.config.workspace,
                query.get("dir"),
            )
        elif method == "POST" and path == "/files/write":
            payload = await asyncio.to_thread(
                write_workspace_file,
                app.config.workspace,
                str(document.get("path") or ""),
                document.get("content", ""),
                encoding=str(document.get("encoding") or "utf-8"),
                overwrite=bool(document.get("overwrite", True)),
                create_parents=bool(
                    document.get(
                        "create_parents", document.get("createParents", False)
                    )
                ),
            )
        elif method == "POST" and path == "/files/mkdir":
            payload = await asyncio.to_thread(
                mkdir_workspace,
                app.config.workspace,
                str(document.get("path") or ""),
                create_parents=bool(
                    document.get(
                        "create_parents", document.get("createParents", True)
                    )
                ),
            )
        elif method == "POST" and path == "/files/rename":
            payload = await asyncio.to_thread(
                rename_workspace_entry,
                app.config.workspace,
                str(document.get("from") or document.get("from_path") or ""),
                str(document.get("to") or document.get("to_path") or ""),
                overwrite=bool(document.get("overwrite", False)),
            )
        elif method == "POST" and path == "/files/delete":
            payload = await asyncio.to_thread(
                delete_workspace_entry,
                app.config.workspace,
                str(document.get("path") or ""),
                recursive=bool(document.get("recursive", False)),
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
            if (
                not isinstance(document.get("source_request_id"), str)
                or not document["source_request_id"]
            ):
                raise DesktopIngressError("source_request_id_required", status_code=422)
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
        elif method == "POST" and re.fullmatch(
            r"/jobs/[A-Za-z0-9_.-]+/approval/[A-Za-z0-9_.-]+", path
        ):
            pieces = path.split("/")
            payload = settle_job_approval(jobs, pieces[2], pieces[4], document)
        elif method == "POST" and re.fullmatch(r"/jobs/[A-Za-z0-9_.-]+/retry", path):
            if "skill_run" not in jobs.runner_kinds():
                return capability_required("science_runner")
            job_id = path.split("/")[2]
            original = jobs.get_job(job_id)
            if original is None:
                raise DesktopIngressError("job_not_found", status_code=404)
            if original.status not in {
                "failed",
                "canceled",
                "cancelled",
                "interrupted",
                "succeeded",
            }:
                raise DesktopIngressError("job_not_terminal", status_code=409)
            if original.kind not in jobs.runner_kinds():
                return capability_required("science_runner")
            retry_inputs = dict(original.inputs or {})
            retry_doc = {
                "kind": original.kind,
                "skill": original.skill,
                "inputs": retry_inputs,
                "session_id": original.session_id,
                "workspace": str(app.config.workspace),
                "runtime": original.runtime or "local",
                "source_request_id": str(
                    document.get("source_request_id")
                    or f"retry-{job_id}-{int(time.time() * 1000)}"
                ),
            }
            if original.kind == "code_run":
                retry_doc["code"] = retry_inputs.get("code", "")
                retry_doc["language"] = retry_inputs.get("language", "python")
                retry_doc["location"] = retry_inputs.get("location", "local")
            elif original.kind == "env_install":
                retry_doc["env"] = retry_inputs.get("env", "main")
                retry_doc["packages"] = retry_inputs.get("packages", [])
            record = await create_job_from_document(jobs, retry_doc)
            return {
                "status": 201,
                "payload": {"job_id": record.id, "job": record.as_payload()},
            }
        elif method == "DELETE" and re.fullmatch(r"/jobs/[A-Za-z0-9_.-]+", path):
            if "skill_run" not in jobs.runner_kinds():
                return capability_required("science_runner")
            job_id = path.split("/")[2]
            record = jobs.get_job(job_id)
            if record is None:
                raise DesktopIngressError("job_not_found", status_code=404)
            if record.status in {"queued", "running", "cancel_requested"}:
                raise DesktopIngressError("job_not_terminal", status_code=409)
            if hasattr(jobs.store, "delete_job"):
                jobs.store.delete_job(job_id)
            payload = {"deleted": True, "job_id": job_id, "success": True}
        elif method == "PUT" and re.fullmatch(r"/jobs/[A-Za-z0-9_.-]+", path):
            if "skill_run" not in jobs.runner_kinds():
                return capability_required("science_runner")
            job_id = path.split("/")[2]
            record = jobs.get_job(job_id)
            if record is None:
                raise DesktopIngressError("job_not_found", status_code=404)
            changes = {
                k: document[k]
                for k in ("status", "error")
                if k in document and isinstance(document[k], str)
            }
            if changes:
                jobs.store.update_job(job_id, **changes)
                record = jobs.get_job(job_id) or record
            payload = {"job": record.as_payload()}
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
        elif method == "GET" and path in {"/sandbox/status", "/api/sandbox/status"}:
            payload = _sandbox_snapshot(app)
        elif method == "GET" and path in {"/compute/status", "/api/compute/status"}:
            req_sid = (query or {}).get("session_id") if isinstance(query, dict) else None
            if isinstance(req_sid, list):
                req_sid = req_sid[0] if req_sid else None
            payload = compute_status_payload(app, session_id=str(req_sid) if req_sid else None)
        elif method == "POST" and path in {
            "/sandbox/allow-domain",
            "/api/sandbox/allow-domain",
        }:
            return allow_sandbox_domain(app, document)
        elif method == "DELETE" and path in {
            "/sandbox/allow-domain",
            "/api/sandbox/allow-domain",
        }:
            return revoke_sandbox_domain(app, query, document)
        elif method == "GET" and path == "/connectors":
            payload = await asyncio.to_thread(connector_listing, app)
        elif method == "PUT" and re.fullmatch(r"/connectors/[A-Za-z0-9_.-]+", path):
            payload = await asyncio.to_thread(
                set_connector_enabled, app, path.split("/")[2], document
            )
        elif method == "POST" and re.fullmatch(
            r"/connectors/[A-Za-z0-9_.-]+/check", path
        ):
            payload = await check_connector(app, path.split("/")[2])
        elif method == "GET" and path == "/permissions/rules":
            payload = await asyncio.to_thread(permission_rules_payload, app)
        elif path.startswith("/memory/"):
            payload = await dispatch_memory_request(
                app, method, path, query, document
            )
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


def allow_sandbox_domain(app, document):
    domain = document.get("domain") if isinstance(document, dict) else None
    if not isinstance(domain, str) or not domain.strip():
        return {
            "status": 400,
            "payload": {
                "detail": "domain must be a non-empty string",
                "error": "invalid_domain",
            },
        }
    persist = (
        bool(document.get("persist", False)) if isinstance(document, dict) else False
    )
    binding = getattr(app, "sandbox", None)
    if binding is None or not hasattr(binding, "allow_domain"):
        return {
            "status": 400,
            "payload": {
                "detail": "sandbox binding unavailable",
                "error": "sandbox_unavailable",
            },
        }
    try:
        binding.allow_domain(domain, persist=persist)
    except ValueError as exc:
        return {
            "status": 400,
            "payload": {"detail": str(exc), "error": "domain_denied"},
        }
    return {"status": 200, "payload": _sandbox_snapshot(app)}


def revoke_sandbox_domain(app, query, document):
    domain = query.get("domain") if isinstance(query, dict) else None
    if not domain and isinstance(document, dict):
        domain = document.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        return {
            "status": 400,
            "payload": {
                "detail": "domain must be a non-empty string",
                "error": "invalid_domain",
            },
        }
    binding = getattr(app, "sandbox", None)
    if binding is not None and hasattr(binding, "revoke_domain"):
        binding.revoke_domain(domain)
    return {"status": 200, "payload": _sandbox_snapshot(app)}




async def delete_chat_session(app, session_id: str) -> dict:
    sessions = getattr(app, "sessions", None)
    if sessions is not None:
        if hasattr(sessions, "is_busy") and sessions.is_busy(session_id):
            raise DesktopIngressError("session_busy", status_code=409)
        if hasattr(sessions, "_sessions") and isinstance(sessions._sessions, dict):
            sessions._sessions.pop(session_id, None)
        if hasattr(sessions, "_lanes") and isinstance(sessions._lanes, dict):
            sessions._lanes.pop(session_id, None)
        if hasattr(sessions, "delete") and callable(sessions.delete):
            res = sessions.delete(session_id)
            if inspect.isawaitable(res):
                await res
        elif hasattr(sessions, "_store") and hasattr(sessions._store, "delete"):
            res = sessions._store.delete(session_id)
            if inspect.isawaitable(res):
                await res
    return {"deleted": True, "session_id": session_id}


async def rewind_chat_session(app, document: dict) -> dict:
    session_id = document.get("session_id") or document.get("sessionId")
    if not isinstance(session_id, str) or not session_id.strip():
        raise DesktopIngressError("session_id_required", status_code=422)
    keep_count = document.get("keep_count", document.get("keepCount"))
    sessions = getattr(app, "sessions", None)
    if sessions is not None:
        if hasattr(sessions, "is_busy") and sessions.is_busy(session_id):
            raise DesktopIngressError("session_busy", status_code=409)
        session = None
        if hasattr(sessions, "load_session"):
            session = await sessions.load_session(session_id)
        elif hasattr(sessions, "load"):
            res = sessions.load(session_id)
            session = await res if inspect.isawaitable(res) else res
        if session is not None:
            if isinstance(keep_count, int) and not isinstance(keep_count, bool) and keep_count >= 0:
                session.history = tuple(session.history[:keep_count])
            else:
                session.history = ()
                if hasattr(session, "compaction") and session.compaction is not None:
                    session.compaction = type(session.compaction)()
            session.updated_at = time.time()
            if hasattr(sessions, "_store") and hasattr(sessions._store, "save"):
                res = sessions._store.save(session)
                if inspect.isawaitable(res):
                    await res
            elif hasattr(sessions, "save"):
                res = sessions.save(session)
                if inspect.isawaitable(res):
                    await res
    return {"rewound": True, "session_id": session_id}


def _harvest_workspace_kernel_ledger(
    ws, *, max_files: int = 24, tail_bytes: int = 131072
) -> tuple[dict[str, dict[str, dict]], dict[str, list[dict]]]:
    import json
    from pathlib import Path

    by_session_objects: dict[str, dict[str, dict]] = {}
    by_session_skills: dict[str, list[dict]] = {}
    if ws is None:
        return by_session_objects, by_session_skills
    ws_path = Path(ws)
    if not ws_path.is_dir():
        return by_session_objects, by_session_skills

    found_files: list[tuple[float, Path]] = []
    seen_resolved: set[Path] = set()
    direct_candidates = (
        ws_path / ".omicsclaw" / "provenance" / "kernel_ledger.jsonl",
        ws_path / "provenance" / "kernel_ledger.jsonl",
        ws_path / ".omicsclaw" / "provenance" / "session_ledger.jsonl",
        ws_path / "provenance" / "session_ledger.jsonl",
    )
    for cand in direct_candidates:
        try:
            if cand.is_file():
                rp = cand.resolve()
                if rp not in seen_resolved:
                    seen_resolved.add(rp)
                    found_files.append((cand.stat().st_mtime, cand))
        except OSError:
            continue

    kernels_dir = ws_path / ".omicsclaw" / "kernels"
    if kernels_dir.is_dir():
        try:
            for kchild in kernels_dir.iterdir():
                if not kchild.is_dir():
                    continue
                kcand = kchild / "ledger.jsonl"
                if kcand.is_file():
                    rp = kcand.resolve()
                    if rp not in seen_resolved:
                        seen_resolved.add(rp)
                        found_files.append((kcand.stat().st_mtime, kcand))
        except OSError:
            pass

    try:
        for child in ws_path.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            for sub_rel in (
                Path(".omicsclaw") / "provenance" / "kernel_ledger.jsonl",
                Path("provenance") / "kernel_ledger.jsonl",
                Path(".omicsclaw") / "provenance" / "session_ledger.jsonl",
                Path("kernel_ledger.jsonl"),
            ):
                sub_cand = child / sub_rel
                try:
                    if sub_cand.is_file():
                        rp = sub_cand.resolve()
                        if rp not in seen_resolved:
                            seen_resolved.add(rp)
                            found_files.append((sub_cand.stat().st_mtime, sub_cand))
                except OSError:
                    continue
    except OSError:
        pass

    if not found_files:
        return by_session_objects, by_session_skills

    # Take up to max_files newest ledgers, then process oldest-to-newest so newer revisions win
    selected = sorted(found_files, key=lambda item: item[0], reverse=True)[:max_files]
    selected.sort(key=lambda item: item[0])

    for _mtime, ledger_file in selected:
        try:
            size = ledger_file.stat().st_size
            if size <= 0:
                continue
            with ledger_file.open("rb") as fh:
                if size > tail_bytes:
                    fh.seek(size - tail_bytes)
                    raw_bytes = fh.read()
                    nl_idx = raw_bytes.find(b"\n")
                    if nl_idx != -1:
                        raw_bytes = raw_bytes[nl_idx + 1 :]
                else:
                    raw_bytes = fh.read()
            lines = raw_bytes.decode("utf-8", errors="replace").splitlines()[-500:]
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                inferred_sid = ledger_file.parent.name if ledger_file.parent.parent.name == "kernels" else ""
                sid = str(rec.get("session_id") or inferred_sid).strip()
                ts = rec.get("timestamp")
                exec_count = rec.get("execution_count")
                exec_id = str(rec.get("execution_id") or "")
                for obj in rec.get("object_versions") or ():
                    if not isinstance(obj, dict):
                        continue
                    name = str(obj.get("name") or obj.get("var_name") or "").strip()
                    if not name:
                        continue
                    merged = dict(obj)
                    merged["name"] = name
                    merged["session_id"] = sid
                    if ts is not None:
                        merged["timestamp"] = ts
                    if exec_count is not None:
                        merged["execution_count"] = exec_count
                    if exec_id and not merged.get("execution_id"):
                        merged["execution_id"] = exec_id
                    prev_obj = by_session_objects.setdefault(sid, {}).get(name)
                    if prev_obj is not None:
                        prev_ts = float(prev_obj.get("timestamp") or 0.0)
                        curr_ts = float(merged.get("timestamp") or 0.0)
                        if curr_ts < prev_ts:
                            continue
                    by_session_objects.setdefault(sid, {})[name] = merged
                for sk in rec.get("skills_loaded") or ():
                    if isinstance(sk, str) and sk.strip():
                        sk_copy = {"skill": sk.strip(), "session_id": sid}
                    elif isinstance(sk, dict):
                        sk_copy = dict(sk)
                        sk_copy["session_id"] = sid
                    else:
                        continue
                    bucket = by_session_skills.setdefault(sid, [])
                    if not any(
                        x.get("skill") == sk_copy.get("skill")
                        and x.get("module") == sk_copy.get("module")
                        for x in bucket
                    ):
                        bucket.append(sk_copy)
        except Exception:
            continue
    return by_session_objects, by_session_skills


def compute_status_payload(app, *, environments=None, session_id: str | None = None) -> dict:
    from pathlib import Path
    import json

    ws = Path(app.config.workspace) if getattr(app, "config", None) else None
    by_session_objects, by_session_skills = _harvest_workspace_kernel_ledger(ws)

    kernels = []
    all_handles = []
    seen_handle_ids = set()
    seen_pids = set()
    mgr = getattr(getattr(app, "kernels", None), "manager", None)
    if mgr is not None and hasattr(mgr, "handles") and mgr.handles is not None:
        try:
            raw_map = getattr(mgr.handles, "_handles", None)
            if raw_map is None:
                raw_map = getattr(mgr.handles, "_by_id", {})
            for h in list(raw_map.values()):
                rev = list(h.revision) if isinstance(h.revision, (list, tuple)) else h.revision
                h_dict = {
                    "adata_id": h.adata_id,
                    "session_id": h.session_id,
                    "revision": rev,
                    "summary": h.summary,
                    "synced_at": h.synced_at,
                    "expression": f"OV_KERNEL_ADATA[{h.adata_id!r}]",
                }
                all_handles.append(h_dict)
                seen_handle_ids.add(h.adata_id)
        except Exception:
            pass

    if mgr is not None and hasattr(mgr, "snapshot"):
        try:
            for item in mgr.snapshot():
                if not item.get("alive", True):
                    continue
                pid = item.get("pid")
                if pid:
                    seen_pids.add(int(pid))
                rss_kb = item.get("peak_rss_kb") or 0
                sid = str(item.get("session_id") or "")
                session_handles = [h for h in all_handles if h.get("session_id") == sid]
                session_objects = list(by_session_objects.get(sid, {}).values())
                session_skills = list(by_session_skills.get(sid, []))
                kernels.append({
                    "session_id": sid,
                    "language": item.get("language", "python"),
                    "location": item.get("location", "local"),
                    "pid": pid,
                    "rss_bytes": int(rss_kb) * 1024,
                    "cells": item.get("cells", 0),
                    "status": "busy" if item.get("executing", False) else "idle",
                    "state": "busy" if item.get("executing", False) else "ready",
                    "executing": bool(item.get("executing", False)),
                    "age_s": item.get("age_s"),
                    "idle_s": item.get("idle_s"),
                    "restart_needed": bool(item.get("restart_needed", False)),
                    "handles": session_handles,
                    "object_versions": session_objects,
                    "skills_loaded": session_skills,
                })
        except Exception:
            pass
    if ws is not None:
        mem_db = ws / ".omicsclaw" / "memory.db"
        if mem_db.is_file():
            try:
                import sqlite3
                with sqlite3.connect(str(mem_db), timeout=1.0) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT instance_id, notebook_id, language, state, process_json "
                        "FROM notebook_runtime_kernels WHERE state IN ('reserved', 'ready', 'busy')"
                    ).fetchall()
                    for r in rows:
                        proc = json.loads(r["process_json"] or "null") or {}
                        pid = proc.get("pid")
                        if pid and int(pid) in seen_pids:
                            continue
                        rss_bytes = 0
                        if pid:
                            seen_pids.add(int(pid))
                            try:
                                for line in Path(f"/proc/{int(pid)}/status").read_text().splitlines():
                                    if line.startswith("VmRSS:"):
                                        rss_bytes = int(line.split()[1]) * 1024
                                        break
                            except Exception:
                                pass
                        nb_sid = str(r["notebook_id"] or "")
                        kernels.append({
                            "session_id": nb_sid,
                            "kernel_instance_id": r["instance_id"],
                            "language": r["language"],
                            "state": r["state"],
                            "status": "busy" if r["state"] == "busy" else "idle",
                            "executing": r["state"] == "busy",
                            "pid": pid,
                            "rss_bytes": rss_bytes,
                            "handles": [h for h in all_handles if h.get("session_id") == nb_sid],
                            "object_versions": list(by_session_objects.get(nb_sid, {}).values()),
                            "skills_loaded": list(by_session_skills.get(nb_sid, [])),
                        })
            except Exception:
                pass
    env_count = 0
    if environments is not None and hasattr(environments, "page"):
        try:
            env_count = len(environments.page(limit=50).get("environments", []))
        except Exception:
            pass
    target_sid = (session_id or "").strip()
    flat_objects = []
    for sid_key, obj_map in by_session_objects.items():
        if target_sid and sid_key and sid_key != target_sid:
            continue
        flat_objects.extend(obj_map.values())
    flat_objects.sort(key=lambda x: float(x.get("timestamp") or 0.0), reverse=True)
    flat_skills = []
    for sid_key, sk_list in by_session_skills.items():
        if target_sid and sid_key and sid_key != target_sid:
            continue
        for sk in sk_list:
            if not any(x.get("skill") == sk.get("skill") and x.get("module") == sk.get("module") for x in flat_skills):
                flat_skills.append(sk)
    if target_sid:
        kernels = [k for k in kernels if not k.get("session_id") or k.get("session_id") == target_sid]
        all_handles = [h for h in all_handles if not h.get("session_id") or h.get("session_id") == target_sid]
        flat_objects = [o for o in flat_objects if not o.get("session_id") or o.get("session_id") == target_sid]
        flat_skills = [s for s in flat_skills if not s.get("session_id") or s.get("session_id") == target_sid]
    return {
        "active_kernels_count": len(kernels),
        "kernels_rss_bytes": sum(int(k.get("rss_bytes") or 0) for k in kernels),
        "kernels": kernels,
        "environments_count": env_count,
        "handles": all_handles,
        "object_versions": flat_objects,
        "skills_loaded": flat_skills,
    }
