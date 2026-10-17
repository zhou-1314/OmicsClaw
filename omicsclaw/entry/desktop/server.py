"""The Desktop HTTP routes, and the plain functions behind them.

Routes (see :data:`~omicsclaw.entry.desktop.wire_contract.SERVED_PATHS`):

* ``POST /chat/stream`` — admit one chat message and stream its frames;
* ``POST /chat/permission`` — answer one ``permission_request``;
* ``POST /chat/abort`` — cancel the exchange a stream request started;
* ``POST /chat/session-permission-profile`` — put a session in the
  ``default`` or ``full_access`` permission profile;
* ``GET /workspace`` — the workspace this process serves;
* ``PUT /workspace`` — accepted only for that same workspace, since a
  process serves one workspace and changing it means a restart;
* ``GET /env/doctor`` — a checklist of what this backend was assembled
  with (:mod:`~omicsclaw.entry.desktop.doctor`);
* ``GET``/``HEAD /health`` — process facts and the wire contract;
* ``GET /skills`` and ``GET /skills/{domain}/{name}`` — the skill index
  (:mod:`~omicsclaw.entry.desktop.catalog`);
* ``GET /mcp/servers`` — the MCP servers and their connection state
  (:mod:`~omicsclaw.entry.desktop.catalog`);
* ``GET``/``PUT /providers`` and ``POST /providers/test`` — the model
  backend the deployment's ``.env`` configures
  (:mod:`~omicsclaw.entry.desktop.providers`);
* ``POST /chat/title`` — a title for a session's first message
  (:mod:`~omicsclaw.entry.desktop.title`);
* ``GET /files/tree`` and ``GET /files/serve`` — read-only views of the
  workspace's files (:mod:`~omicsclaw.entry.desktop.files`);
* ``GET``/``POST /remote/hosts`` and ``DELETE /remote/hosts/{alias}`` —
  the remote-execution host knowledge base
  (:mod:`~omicsclaw.entry.desktop.remote_hosts`), answering
  ``503 remote_execution_disabled`` on a deployment with no plane bound.

A ``/chat/stream`` message whose content is exactly ``/compact`` compacts
the session's history instead of being sent to the model. A
``/chat/stream`` body with ``resume: true`` reattaches to the exchange
its ``source_request_id`` started and never starts one.

Every write route requires ``Content-Type: application/json`` and answers
415 ``unsupported_media_type`` otherwise, including when the header is
missing. A cross-origin page can send a ``text/plain`` request to a
loopback port without a CORS preflight, but not a JSON one, and this
server grants no CORS. A refused request is answered as
``{"detail": <code>}`` with the code's status.

Everything above the HTTP boundary — :func:`open_chat_stream`,
:func:`health_payload`, :func:`workspace_payload`,
:func:`change_workspace`, the functions in
:mod:`~omicsclaw.entry.desktop.interactions` and
:func:`~omicsclaw.entry.desktop.doctor.doctor_report` — is plain ``async def`` or
``def`` over plain dictionaries. :func:`create_desktop_app` is the adapter
that puts them behind FastAPI, and it imports FastAPI **inside the
function body**, so ``import omicsclaw.entry.desktop`` succeeds where no
web framework is installed.

This module does **not** enable ``from __future__ import annotations``.
The routes are defined inside :func:`create_desktop_app`, where
``Request`` is a local name; with postponed evaluation FastAPI would see
the string ``"Request"``, fail to resolve it in this module's globals and
treat the parameter as a required query parameter, so every route would
answer 422.
"""

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Any, Final, Mapping

from omicsclaw.entry.assembly import AgentApp
from omicsclaw.entry.ingress import SenderPolicy
from omicsclaw.entry.session import (
    QueueFull,
    RegistryClosed,
    SubmissionRefused,
)
from omicsclaw.memory.artifacts import ArtifactStore
from omicsclaw.version import __version__

from .runtime_service import (
    health_payload as health_payload,
    unauthenticated_health_payload as unauthenticated_health_payload,
    BACKEND_PROCESS_EPOCH as BACKEND_PROCESS_EPOCH,
)

from .chat_service import (
    ChatStream as ChatStream,
    _compaction as _compaction,
    _maybe_fire_shadow_review as _maybe_fire_shadow_review,
    _observe as _observe,
    _observed_body as _observed_body,
    _retained_exchange as _retained_exchange,
    _same_workspace as _same_workspace,
    change_workspace as change_workspace,
    open_chat_stream as open_chat_stream,
    workspace_payload as workspace_payload,
    ChatAdmission as ChatAdmission,
    admit_chat as admit_chat,
)

from .artifacts import mount_artifacts_routes
from .catalog import mcp_servers, skill_catalog, skill_detail
from .doctor import _sandbox_snapshot, doctor_report
from .service import allow_sandbox_domain, revoke_sandbox_domain
from .files import (
    byte_span,
    content_disposition,
    file_chunks,
    file_tree,
    open_served_file,
    serve_target,
    tree_depth,
)
from .interactions import (
    DesktopInteractions,
    abort_chat,
    answer_permission,
    change_permission_profile,
)
from .jobs import mount_jobs_routes
from .jobs_manager import JobsManager
from .providers import SettingsFile, provider_listing, save_provider, test_provider
from .remote_hosts import mount_remote_host_routes
from .title import generate_title
from .turn_observation import KEEPALIVE_INTERVAL_S
from .turn_submission import (
    DEFAULT_MAX_REQUEST_BYTES,
    HANDOFF_SESSION_ID,
    DesktopIngressError,
    parse_chat_stream_document,
)
from .wire_contract import (
    CONNECTION_EPOCH,
)

__all__ = [
    "BACKEND_PROCESS_EPOCH",
    "COMPACT_COMMAND",
    "CONNECTION_EPOCH",
    "CONTROL_MAX_REQUEST_BYTES",
    "SSE_HEADERS",
    "ChatStream",
    "change_workspace",
    "create_desktop_app",
    "health_payload",
    "is_json_media_type",
    "open_chat_stream",
    "workspace_payload",
]

_log = logging.getLogger(__name__)


CONTROL_MAX_REQUEST_BYTES: Final = 64 * 1024
"""Largest body ``/chat/permission``, ``/chat/abort``,
``/chat/session-permission-profile``, ``PUT /workspace``,
``PUT /providers``, ``POST /providers/test`` and ``POST /chat/title``
will read."""

COMPACT_COMMAND: Final = "/compact"
"""A ``/chat/stream`` content that compacts the session instead of asking
the model. Surrounding whitespace is ignored; anything else is a message."""

SSE_HEADERS: Final[Mapping[str, str]] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
"""``X-Accel-Buffering`` is the one that is easy to drop and expensive to
miss: an nginx in front of the backend buffers the whole response without
it, which turns a token-by-token stream into one silent wait followed by
the complete answer."""


def is_json_media_type(content_type: str) -> bool:
    """Whether a ``Content-Type`` value names ``application/json``.

    The media type is the part before the first ``;``, stripped and
    compared case-insensitively; parameters such as ``charset`` are
    allowed. ``application/jsonx`` and ``text/application/json`` are not
    JSON.
    """
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json"


def create_desktop_app(
    app: AgentApp,
    *,
    policy: SenderPolicy | None = None,
    bearer_token: str = "",
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    keepalive_s: float | None = KEEPALIVE_INTERVAL_S,
    interactions: DesktopInteractions | None = None,
    settings: SettingsFile | None = None,
    jobs_manager: JobsManager | None = None,
    artifacts_store: ArtifactStore | None = None,
    notebook_analysis=None,
) -> Any:
    """Build the FastAPI application serving :data:`SERVED_PATHS`.

    ``fastapi`` is imported here, not at module scope, so that ``import
    omicsclaw.entry.desktop`` succeeds in an environment that has no web
    framework.

    ``bearer_token=""`` leaves the routes unauthenticated, which is only
    defensible on a loopback bind; a non-empty token is compared with
    :func:`secrets.compare_digest`, because a string ``==`` on a secret
    leaks its prefix through timing.

    *interactions* defaults to a new
    :class:`~omicsclaw.entry.desktop.interactions.DesktopInteractions`
    shared by every route of this application.

    *settings* is the deployment's ``.env``
    (:class:`~omicsclaw.entry.desktop.providers.SettingsFile`). Without it
    ``PUT /providers`` and ``POST /providers/test`` answer 503
    ``settings_unavailable`` and ``GET /providers`` lists no provider as
    configured.

    :raises ModuleNotFoundError: ``fastapi`` is not installed.
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    @asynccontextmanager
    async def lifespan(_api):
        await jobs.reconcile_remote_jobs()
        try:
            yield
        finally:
            await close_notebooks()

    api = FastAPI(title="OmicsClaw Desktop", version=__version__, lifespan=lifespan)
    shared = interactions if interactions is not None else DesktopInteractions(app)
    jobs = (
        jobs_manager
        if jobs_manager is not None
        else JobsManager(app, remote_runtime=_remote_runtime_bridge(app))
    )
    artifacts = artifacts_store if artifacts_store is not None else jobs.artifacts
    if app.shadow_review is not None:
        # P6 item 4: the detector lives on the app, the store lives here.
        # One bind, at app creation, so every route sees the same store.
        app.shadow_review.bind_store(jobs.artifacts)

    def _authorized(request: Request) -> bool:
        if not bearer_token:
            return True
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return secrets.compare_digest(presented.strip(), bearer_token)

    def _refused(exc: DesktopIngressError) -> Any:
        return JSONResponse({"detail": exc.code}, status_code=exc.status_code)

    def _unauthorized() -> Any:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    def _require_json(request: Request) -> None:
        declared = request.headers.getlist("content-type")
        if len(declared) != 1 or not is_json_media_type(declared[0]):
            raise DesktopIngressError("unsupported_media_type", status_code=415)

    async def _read_body(request: Request, limit: int) -> bytes:
        """Read the body with a running cap, refusing a lie in the header.

        A declared ``Content-Length`` over the limit is refused before a
        byte is read, and a running total is kept anyway, because the
        declaration is the client's claim and the bytes are the fact.
        """
        declared = request.headers.getlist("content-length")
        if len(declared) > 1:
            raise DesktopIngressError("invalid_content_length", status_code=400)
        if declared:
            try:
                length = int(declared[0])
            except ValueError as exc:
                raise DesktopIngressError(
                    "invalid_content_length", status_code=400
                ) from exc
            if length < 0:
                raise DesktopIngressError("invalid_content_length", status_code=400)
            if length > limit:
                raise DesktopIngressError("request_document_too_large", status_code=413)
        chunks: list[bytes] = []
        observed = 0
        async for chunk in request.stream():
            observed += len(chunk)
            if observed > limit:
                raise DesktopIngressError("request_document_too_large", status_code=413)
            chunks.append(chunk)
        return b"".join(chunks)

    async def _read_json(request: Request, limit: int) -> dict[str, Any]:
        _require_json(request)
        body = await _read_body(request, limit)
        return parse_chat_stream_document(body, max_request_bytes=limit)

    @api.post("/chat/stream")
    async def chat_stream(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        try:
            document = await _read_json(request, max_request_bytes)
            after_seq = _after_seq(document)
            stream = await open_chat_stream(
                app,
                document,
                policy=policy,
                after_seq=after_seq,
                keepalive_s=keepalive_s,
                interactions=shared,
                # Chat v3 clients require exactly type/data in each envelope.
                # Process identity remains available through /health.
            )
        except DesktopIngressError as exc:
            return _refused(exc)
        except QueueFull:
            return JSONResponse({"detail": "queue_full"}, status_code=429)
        except RegistryClosed:
            return JSONResponse({"detail": "shutting_down"}, status_code=503)
        except SubmissionRefused:  # pragma: no cover - a future subclass
            return JSONResponse({"detail": "submission_refused"}, status_code=429)

        async def frames() -> Any:
            # ``async with`` rather than a bare ``async for``: a client that
            # disconnects interrupts this generator, and the observation it
            # was holding has to be detached or the exchange never learns
            # that nobody is watching.
            async with stream.body as body_iter:
                async for frame in body_iter:
                    yield frame

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={
                **SSE_HEADERS,
                "X-OmicsClaw-Turn-Id": stream.turn_id,
                "X-OmicsClaw-Session-Id": stream.session_id,
            },
        )

    @api.post("/chat/permission")
    async def chat_permission(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        try:
            document = await _read_json(request, CONTROL_MAX_REQUEST_BYTES)
            return await answer_permission(app, shared, document)
        except DesktopIngressError as exc:
            return _refused(exc)

    @api.post("/chat/abort")
    async def chat_abort(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        try:
            document = await _read_json(request, CONTROL_MAX_REQUEST_BYTES)
            return await abort_chat(app, shared, document)
        except DesktopIngressError as exc:
            return _refused(exc)

    @api.post("/chat/session-permission-profile")
    async def chat_session_permission_profile(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        try:
            document = await _read_json(request, CONTROL_MAX_REQUEST_BYTES)
            return await change_permission_profile(app, shared, document)
        except DesktopIngressError as exc:
            return _refused(exc)

    @api.get("/chat/session/{session_id}")
    async def chat_session(request: Request, session_id: str) -> Any:
        # P6 item 5: the App's adoption check for a handoff URL. The path
        # segment is untrusted input — the closed charset answers 404
        # without a store read, and a session nothing saved answers the
        # same 404, so a deep link can never probe which ids exist.
        if not _authorized(request):
            return _unauthorized()
        registry = app.sessions
        if registry is None or not HANDOFF_SESSION_ID.fullmatch(session_id):
            return JSONResponse({"detail": "session_not_found"}, status_code=404)
        session = await registry.load_session(session_id)
        if session is None:
            return JSONResponse({"detail": "session_not_found"}, status_code=404)
        return JSONResponse(
            {
                "session_id": session_id,
                "message_count": len(session.history),
                "updated_at": session.updated_at,
            }
        )

    @api.get("/env/doctor")
    async def env_doctor(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        return doctor_report(app)

    @api.get("/sandbox/status")
    @api.get("/api/sandbox/status")
    async def get_sandbox_status(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        return _sandbox_snapshot(app)

    @api.post("/sandbox/allow-domain")
    @api.post("/api/sandbox/allow-domain")
    async def post_sandbox_allow_domain(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        try:
            document = await _read_json(request, CONTROL_MAX_REQUEST_BYTES)
        except DesktopIngressError as exc:
            return _refused(exc)
        result = allow_sandbox_domain(app, document)
        if result["status"] != 200:
            return JSONResponse(result["payload"], status_code=result["status"])
        return result["payload"]

    @api.delete("/sandbox/allow-domain")
    @api.delete("/api/sandbox/allow-domain")
    async def delete_sandbox_allow_domain(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        doc: dict[str, Any] = {}
        if not request.query_params.get("domain"):
            raw_body = await _read_body(request, CONTROL_MAX_REQUEST_BYTES)
            if raw_body.strip():
                try:
                    parsed_doc = parse_chat_stream_document(
                        raw_body, max_request_bytes=CONTROL_MAX_REQUEST_BYTES
                    )
                    if isinstance(parsed_doc, dict):
                        doc = parsed_doc
                except Exception:  # noqa: BLE001
                    doc = {}
        result = revoke_sandbox_domain(app, dict(request.query_params), doc)
        if result["status"] != 200:
            return JSONResponse(result["payload"], status_code=result["status"])
        return result["payload"]

    @api.get("/workspace")
    async def get_workspace(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        return workspace_payload(app)

    @api.put("/workspace")
    async def put_workspace(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        try:
            document = await _read_json(request, CONTROL_MAX_REQUEST_BYTES)
            return change_workspace(app, document)
        except DesktopIngressError as exc:
            return _refused(exc)

    @api.get("/skills")
    async def skills(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        return skill_catalog(app)

    @api.get("/skills/{domain}/{name}")
    async def skill(request: Request, domain: str, name: str) -> Any:
        if not _authorized(request):
            return _unauthorized()
        try:
            return skill_detail(app, domain, name)
        except DesktopIngressError as exc:
            return _refused(exc)

    @api.get("/mcp/servers")
    async def mcp(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        startup = settings.startup if settings is not None else None
        return mcp_servers(app, startup=startup)

    @api.get("/providers")
    async def get_providers(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        return provider_listing(app, settings)

    @api.put("/providers")
    async def put_providers(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        try:
            document = await _read_json(request, CONTROL_MAX_REQUEST_BYTES)
            return save_provider(app, settings, document)
        except DesktopIngressError as exc:
            return _refused(exc)

    @api.post("/providers/test")
    async def providers_test(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        try:
            document = await _read_json(request, CONTROL_MAX_REQUEST_BYTES)
            return await test_provider(settings, document)
        except DesktopIngressError as exc:
            return _refused(exc)

    @api.post("/chat/title")
    async def chat_title(request: Request) -> Any:
        if not _authorized(request):
            return _unauthorized()
        try:
            document = await _read_json(request, CONTROL_MAX_REQUEST_BYTES)
        except DesktopIngressError as exc:
            return _refused(exc)
        status, payload = await generate_title(app, document)
        return JSONResponse(payload, status_code=status)

    def _nosniff(response: Any) -> Any:
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.get("/files/tree")
    async def files_tree(request: Request) -> Any:
        if not _authorized(request):
            return _nosniff(_unauthorized())
        query = request.query_params
        try:
            depth = tree_depth(query.get("depth"))
            payload = await asyncio.to_thread(
                file_tree, app.config.workspace, query.get("path"), depth=depth
            )
        except DesktopIngressError as exc:
            return _nosniff(_refused(exc))
        return _nosniff(JSONResponse(payload))

    @api.get("/files/serve")
    async def files_serve(request: Request) -> Any:
        if not _authorized(request):
            return _nosniff(_unauthorized())
        try:
            target = await asyncio.to_thread(
                serve_target, app.config.workspace, request.query_params.get("path", "")
            )
            opened = await asyncio.to_thread(open_served_file, target.path)
        except DesktopIngressError as exc:
            return _nosniff(_refused(exc))
        try:
            span = byte_span(request.headers.get("range"), opened.size)
        except DesktopIngressError as exc:
            opened.close()
            refused = _nosniff(_refused(exc))
            if exc.status_code == 416:
                refused.headers["Content-Range"] = f"bytes */{opened.size}"
            return refused
        headers = {
            "Content-Type": target.media_type,
            "Content-Length": str(span.length),
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=60",
            "Content-Disposition": content_disposition(target.name),
            "X-Content-Type-Options": "nosniff",
        }
        if span.partial:
            headers["Content-Range"] = span.content_range
        return StreamingResponse(
            file_chunks(opened, span), status_code=span.status, headers=headers
        )

    @api.api_route("/health", methods=["GET", "HEAD"], name="health")
    async def health(request: Request) -> Any:
        if bearer_token and not request.headers.getlist("authorization"):
            return unauthenticated_health_payload(app.config.launch_id)
        if not _authorized(request):
            return _unauthorized()
        _note_runtime_header(request.headers.get("X-OmicsClaw-Runtime"))
        return health_payload(app)

    from omicsclaw.memory.database import Database

    from .notebooks import mount_notebook_routes
    from .notebooks_manager import NotebookAnalysis

    notebook_db = None
    if notebook_analysis is None:
        if app.kernels is not None:
            notebook_analysis = app.kernels.notebooks
        else:
            notebook_db = Database(
                Path(app.config.workspace) / ".omicsclaw" / "notebooks.db"
            )
            notebook_analysis = NotebookAnalysis(notebook_db, None)
    mount_notebook_routes(
        api,
        notebook_analysis,
        authorized=_authorized,
        read_json=_read_json,
        sessions=lambda: app.sessions,
    )

    async def close_notebooks():
        await notebook_analysis.close()
        if notebook_db is not None:
            notebook_db.close()

    mount_jobs_routes(api, jobs, authorized=_authorized)
    mount_remote_host_routes(api, getattr(app, "remote", None), authorized=_authorized)
    mount_artifacts_routes(api, artifacts, jobs=jobs, authorized=_authorized)

    return api


def _remote_runtime_bridge(app: AgentApp) -> Any:
    """The C2 binding this adapter owes the jobs plane: the app's remote
    plane, mounted as the manager's remote runtime.

    ``None`` — and therefore a manager with no remote runtime, exactly
    as before the binding existed — on a deployment whose
    ``remote_execution`` is off or whose plane store never opened. A
    ``jobs_manager`` passed in explicitly brings its own; this function
    only equips the default one.

    ``RemoteJobsBridge`` is imported here rather than at module scope
    for the same reason ``fastapi`` is: the adapter module stays
    importable where the remote plane is not installed, and the import
    is free either way (``omicsclaw.remote`` imports nothing from this
    package, so no cycle opens).
    """
    plane = getattr(app, "remote", None)
    if plane is None:
        return None
    from omicsclaw.remote.bridge import RemoteJobsBridge

    return RemoteJobsBridge(plane)


def _note_runtime_header(header: str | None) -> None:
    """P3: tolerate and log the runtime routing header. Never refuse it.

    ``X-OmicsClaw-Runtime: local | remote:<connId>`` is the one header a
    client sends for the unified local/remote routing the plan defers to a
    later phase. This backend serves one process and routes nothing, so
    the header changes nothing here — logging it keeps a misconfigured
    client visible to an operator without breaking it.
    """
    if header:
        _log.debug("X-OmicsClaw-Runtime: %s", header)


def _after_seq(document: Mapping[str, Any]) -> int:
    """Read the optional resume cursor, refusing anything that is not one.

    A client that never sends it observes from the oldest frame the ring
    still holds, which is the right answer for a fresh tab.
    """
    value = document.get("after_seq", 0)
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DesktopIngressError("invalid_after_seq")
    return value
