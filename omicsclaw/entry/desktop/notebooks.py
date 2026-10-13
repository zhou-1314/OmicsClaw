"""Authenticated notebook routes; imported output and app feedback are data."""

import asyncio
import json

from omicsclaw.entry.stream import _stream_key
from omicsclaw.memory.notebook import RevisionConflict

from .notebooks_manager import TERMINAL, NotebookAnalysis, NotebookError, identifier
from .turn_submission import DesktopIngressError

NOTEBOOK_PATHS = (
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
)


def mount_notebook_routes(
    api, analysis: NotebookAnalysis, *, authorized, read_json, sessions=None
):
    from fastapi import Request
    from fastapi.responses import JSONResponse, StreamingResponse

    async def dispatch(request, nid, operation, write=False, status=200):
        if not authorized(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            if nid is not None:
                identifier(nid)
            document = await read_json(request, 2 * 1024 * 1024) if write else None
            result = await operation(document)
            return JSONResponse(result, status_code=status)
        except NotebookError as exc:
            return JSONResponse({"detail": exc.code}, status_code=exc.status)
        except DesktopIngressError as exc:
            return JSONResponse({"detail": exc.code}, status_code=exc.status_code)
        except RevisionConflict:
            return JSONResponse({"detail": "revision_conflict"}, status_code=409)
        except (ValueError, TypeError):
            return JSONResponse({"detail": "invalid_document"}, status_code=422)

    @api.get("/notebooks")
    async def listing(request: Request):
        async def run(_):
            return {"notebooks": await analysis.listing()}

        return await dispatch(request, None, run)

    @api.get("/notebooks/{notebook_id}")
    async def snapshot(request: Request, notebook_id: str):
        return await dispatch(
            request, notebook_id, lambda _: analysis.snapshot(notebook_id)
        )

    @api.put("/notebooks/{notebook_id}/cells/{cell_id}")
    async def save(request: Request, notebook_id: str, cell_id: str):
        async def run(d):
            identifier(cell_id)
            return {
                "cell": await analysis.save(
                    notebook_id, cell_id, d.get("source"), d.get("expected_revision")
                )
            }

        return await dispatch(request, notebook_id, run, write=True)

    @api.post("/notebooks/{notebook_id}/cells/{cell_id}/execute")
    async def execute(request: Request, notebook_id: str, cell_id: str):
        async def run(d):
            identifier(cell_id)
            return {
                "execution": await analysis.execute(
                    notebook_id,
                    cell_id,
                    d.get("client_command_id"),
                    d.get("expected_revision"),
                )
            }

        return await dispatch(request, notebook_id, run, write=True, status=202)

    @api.post("/notebooks/{notebook_id}/executions/{execution_id}/cancel")
    async def cancel(request: Request, notebook_id: str, execution_id: str):
        return await dispatch(
            request,
            notebook_id,
            lambda _: analysis.cancel(notebook_id, execution_id),
            write=True,
        )

    @api.get("/notebooks/{notebook_id}/executions/{execution_id}/events")
    async def events(request: Request, notebook_id: str, execution_id: str):
        if not authorized(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            identifier(notebook_id)
            await analysis.execution(notebook_id, execution_id)
            cursor = int(
                request.headers.get("last-event-id")
                or request.query_params.get("after_seq", "0")
            )
            if cursor < 0:
                raise ValueError()
        except NotebookError as exc:
            return JSONResponse({"detail": exc.code}, status_code=exc.status)
        except ValueError:
            return JSONResponse({"detail": "invalid_cursor"}, status_code=422)
        key = _stream_key(analysis.session(notebook_id), execution_id)
        _, latest = await asyncio.to_thread(analysis.events.bounds, key)
        if cursor > latest:
            return JSONResponse({"detail": "cursor_ahead"}, status_code=409)

        async def frames():
            nonlocal cursor
            ticks = 0
            while not await request.is_disconnected():
                # Read status first. The pending flag clears only after final
                # event flush, so a settled snapshot includes its replay.
                record = await analysis.execution(notebook_id, execution_id)
                settled = record.status in TERMINAL and not (record.output or {}).get(
                    "event_stream_pending"
                )
                rows = await asyncio.to_thread(
                    analysis.events.replay, key, after_seq=cursor
                )
                for row in rows:
                    if row.type == "exchange_end" and not settled:
                        break
                    cursor = row.seq
                    yield f"id: {row.seq}\ndata: {json.dumps(row.payload, ensure_ascii=False)}\n\n"
                if settled:
                    if record.output and record.output.get("durable_error"):
                        yield 'data: {"type":"durability_error","message":"Refresh notebook snapshot"}\n\n'
                    return
                ticks += 1
                if ticks % 20 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.1)

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @api.post("/notebooks/{notebook_id}/import")
    async def import_document(request: Request, notebook_id: str):
        async def run(d):
            return {
                "cells": await analysis.import_document(notebook_id, d.get("document"))
            }

        return await dispatch(request, notebook_id, run, write=True)

    @api.get("/notebooks/{notebook_id}/export")
    async def export(request: Request, notebook_id: str):
        response = await dispatch(
            request, notebook_id, lambda _: analysis.export(notebook_id)
        )
        if response.status_code == 200:
            response.headers["Content-Disposition"] = (
                f'attachment; filename="{notebook_id}.ipynb"'
            )
        return response

    @api.post("/notebooks/{notebook_id}/binding")
    async def binding(request: Request, notebook_id: str):
        async def run(d):
            sid = identifier(d.get("session_id"))
            registry = sessions() if callable(sessions) else sessions
            if registry is None or await registry.load_session(sid) is None:
                raise NotebookError("session_not_found", 404)
            if registry.is_busy(sid):
                raise NotebookError("session_busy", 409)
            return {"binding": analysis.bind(notebook_id, sid)}

        return await dispatch(request, notebook_id, run, write=True)

    @api.post("/notebooks/{notebook_id}/live-state")
    async def live_state(request: Request, notebook_id: str):
        async def run(d):
            return {"live_state": await analysis.context.publish(notebook_id, d)}

        return await dispatch(request, notebook_id, run, write=True)

    @api.get("/notebooks/{notebook_id}/live-app")
    async def app_read(request: Request, notebook_id: str):
        async def run(_):
            return {"live_app": await analysis.apps.read(notebook_id)}

        return await dispatch(request, notebook_id, run)

    @api.put("/notebooks/{notebook_id}/live-app")
    async def app_publish(request: Request, notebook_id: str):
        async def run(d):
            return {"live_app": await analysis.apps.publish(notebook_id, d)}

        return await dispatch(request, notebook_id, run, write=True)

    @api.post("/notebooks/{notebook_id}/live-app/state")
    async def app_state(request: Request, notebook_id: str):
        async def run(d):
            return {"live_state": await analysis.apps.publish_state(notebook_id, d)}

        return await dispatch(request, notebook_id, run, write=True)

    @api.post("/notebooks/{notebook_id}/feedback")
    async def feedback(request: Request, notebook_id: str):
        async def run(d):
            return {"feedback": await analysis.feedback(notebook_id, d)}

        return await dispatch(request, notebook_id, run, write=True)
